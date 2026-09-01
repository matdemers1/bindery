"""The chunked vault format, alone, before anything stores with it (ADR-013).

Every property a range-serving format has to have, plus every tamper the
one-message format used to catch — it must catch them all, per chunk.
"""

import hashlib
import os
import uuid

import pytest

from api.vault import chunked, crypto

KEY = crypto.new_data_key()
DOC = uuid.uuid4()
CS = 1024  # a small chunk so the tests can afford several of them


def _seal(payload: bytes, *, key=KEY, doc=DOC, chunk=CS) -> bytes:
    return chunked.seal_bytes(payload, key, doc, chunk_size=chunk)


def _reader(tmp_path, blob: bytes, *, key=KEY, doc=DOC) -> chunked.Reader:
    path = tmp_path / "object"
    path.write_bytes(blob)
    return chunked.Reader(path, key, doc)


# --------------------------------------------------------------------------
# Round trips over every awkward size
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "length",
    [0, 1, CS - 1, CS, CS + 1, 3 * CS + CS // 2, 4 * CS],
    ids=["empty", "one byte", "chunk-1", "exactly one chunk", "chunk+1", "3.5 chunks", "4 chunks"],
)
def test_the_bytes_come_back_whole(tmp_path, length):
    payload = os.urandom(length)
    reader = _reader(tmp_path, _seal(payload))
    assert reader.read_all() == payload
    assert reader.length == length
    assert reader.header.chunk_count == max(1, -(-length // CS))


def test_verify_hashes_one_chunk_at_a_time(tmp_path):
    payload = os.urandom(3 * CS + 7)
    reader = _reader(tmp_path, _seal(payload))
    assert reader.verify() == hashlib.sha256(payload).hexdigest()


# --------------------------------------------------------------------------
# Ranges — the reason the format exists
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "start,end",
    [
        (0, 0),
        (0, CS - 1),
        (0, CS),
        (CS - 1, CS),
        (CS, 2 * CS - 1),
        (CS + 10, CS + 20),
        (CS // 2, 2 * CS + CS // 2),
        (3 * CS, 3 * CS + CS // 2 - 1),
        (0, 3 * CS + CS // 2 - 1),
    ],
    ids=[
        "first byte", "first chunk", "spills one byte", "straddles boundary",
        "second chunk exactly", "inside second", "three chunks", "last partial chunk",
        "everything",
    ],
)
def test_a_range_is_exactly_the_bytes_asked_for(tmp_path, start, end):
    payload = os.urandom(3 * CS + CS // 2)
    reader = _reader(tmp_path, _seal(payload))
    assert reader.read_range(start, end) == payload[start : end + 1]


def test_a_range_past_the_end_is_refused(tmp_path):
    reader = _reader(tmp_path, _seal(b"x" * 100))
    with pytest.raises(ValueError):
        reader.read_range(50, 100)
    with pytest.raises(ValueError):
        reader.read_range(10, 5)


def test_the_length_is_known_without_decrypting_anything(tmp_path):
    """What lets a 206 response carry Content-Range before any chunk is opened."""
    payload = os.urandom(2 * CS + 3)
    blob = _seal(payload)
    header = chunked.parse_header(blob[: chunked.HEADER_BYTES])
    assert header.plaintext_length == len(payload)


# --------------------------------------------------------------------------
# Every tamper the one-message format caught, caught per chunk
# --------------------------------------------------------------------------


def _chunk_span(header: chunked.Header, index: int) -> tuple[int, int]:
    start = header.chunk_offset(index)
    return start, start + header.chunk_stored_length(index)


def test_swapping_two_chunks_is_refused(tmp_path):
    """Each chunk is bound to its position, so a reordered file is not the
    same file with the bytes in a different order — it is a broken one."""
    blob = bytearray(_seal(os.urandom(3 * CS)))
    header = chunked.parse_header(bytes(blob[: chunked.HEADER_BYTES]))
    a0, a1 = _chunk_span(header, 0)
    b0, b1 = _chunk_span(header, 1)
    blob[a0:a1], blob[b0:b1] = blob[b0:b1], blob[a0:a1]

    reader = _reader(tmp_path, bytes(blob))
    with pytest.raises(crypto.WrongSecret):
        reader.read_all()


def test_a_chunk_from_another_object_is_refused(tmp_path):
    """Same key, same document, same position — a chunk lifted from a
    different file is still refused, because the shape differs."""
    a = bytearray(_seal(os.urandom(3 * CS)))
    b = _seal(os.urandom(2 * CS))
    header_a = chunked.parse_header(bytes(a[: chunked.HEADER_BYTES]))
    header_b = chunked.parse_header(b[: chunked.HEADER_BYTES])
    s0, s1 = _chunk_span(header_a, 0)
    t0, t1 = _chunk_span(header_b, 0)
    a[s0:s1] = b[t0:t1]

    with pytest.raises(crypto.WrongSecret):
        _reader(tmp_path, bytes(a)).read_all()


def test_a_truncated_object_is_refused_before_any_chunk_is_trusted(tmp_path):
    blob = _seal(os.urandom(3 * CS))
    with pytest.raises(crypto.WrongSecret, match="truncated"):
        _reader(tmp_path, blob[:-1])


def test_a_header_lying_about_the_length_is_refused(tmp_path):
    """The header is not separately authenticated. It does not need to be:
    its load-bearing fields ride in every chunk's associated data."""
    blob = bytearray(_seal(os.urandom(2 * CS)))
    header = chunked.parse_header(bytes(blob[: chunked.HEADER_BYTES]))
    lied = chunked._HEADER.pack(
        chunked.MAGIC, chunked.VERSION, header.chunk_size,
        header.plaintext_length - 1, header.chunk_count,
    )
    blob[: chunked.HEADER_BYTES] = lied
    with pytest.raises(crypto.WrongSecret):
        _reader(tmp_path, bytes(blob)).read_all()


def test_the_wrong_document_id_is_refused(tmp_path):
    blob = _seal(os.urandom(CS))
    with pytest.raises(crypto.WrongSecret):
        _reader(tmp_path, blob, doc=uuid.uuid4()).read_all()


def test_the_wrong_key_is_refused(tmp_path):
    blob = _seal(os.urandom(CS))
    with pytest.raises(crypto.WrongSecret):
        _reader(tmp_path, blob, key=crypto.new_data_key()).read_all()


def test_a_flipped_bit_in_one_chunk_leaves_the_others_readable(tmp_path):
    """Range reads that avoid the damaged chunk still work; the damaged one is
    refused rather than returned wrong. Corruption is local, and detected."""
    blob = bytearray(_seal(os.urandom(3 * CS)))
    header = chunked.parse_header(bytes(blob[: chunked.HEADER_BYTES]))
    s0, _ = _chunk_span(header, 1)
    blob[s0 + 20] ^= 0x01

    reader = _reader(tmp_path, bytes(blob))
    assert len(reader.read_range(0, CS - 1)) == CS
    with pytest.raises(crypto.WrongSecret):
        reader.read_range(CS, 2 * CS - 1)


def test_a_v1_object_is_recognised_as_not_chunked(tmp_path):
    """`open_object` dispatches on this. A v1 object must never be mistaken for
    a v2 one, or the four already in production stop opening."""
    v1 = crypto.encrypt(b"legacy", KEY, associated=str(DOC).encode())
    assert not chunked.is_chunked(v1)
    with pytest.raises(chunked.NotChunked):
        chunked.parse_header(v1)


def test_a_random_nonce_per_chunk():
    """Two seals of the same bytes must not share nonces — GCM with a repeated
    nonce under one key is a catastrophic failure, not a weakness."""
    a = _seal(b"same" * 600)
    b = _seal(b"same" * 600)
    h = chunked.HEADER_BYTES
    assert a[h : h + 12] != b[h : h + 12]
