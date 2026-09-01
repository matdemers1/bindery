"""Vault object format v2: independently decryptable chunks (ADR-013, REQ-192).

The one-message format from ADR-012 had a property that did not matter until
videos: nothing can be read until all of it has been decrypted. A browser plays
video by asking for byte ranges, and under one message every range means the
whole file in memory. This format decrypts only the chunks a range covers.

    "BVLT"  u8 version  u32 chunk_size  u64 plaintext_length  u32 chunk_count
    then chunk_count x [ 12-byte nonce | GCM ciphertext | 16-byte tag ]

Each chunk's associated data binds it to the document, to its own position, and
to the whole file's shape — so reordering, substituting a chunk from another
object, truncating the file and lying in the header are all detected as
decryption failures, exactly as tampering was under the old format. The header
itself is not separately authenticated: its two load-bearing fields ride in
every chunk's associated data, so a header that lies fails on the first chunk.

An empty file has one empty chunk, so the header is always bound to something.
"""

import struct
import uuid
from dataclasses import dataclass
from pathlib import Path

from api.vault import crypto

MAGIC = b"BVLT"
VERSION = 2
# 1 MiB: a range request touches at most two chunks for anything a browser
# asks for, and the overhead is 28 bytes per chunk.
CHUNK_SIZE = 1024 * 1024
TAG_BYTES = 16
_HEADER = struct.Struct(">4sBIQI")  # magic, version, chunk_size, length, count
HEADER_BYTES = _HEADER.size


class NotChunked(ValueError):
    """The bytes do not start with the v2 header — a v1 object, or not ours."""


@dataclass(frozen=True)
class Header:
    chunk_size: int
    plaintext_length: int
    chunk_count: int

    def chunk_plaintext_length(self, index: int) -> int:
        if index == self.chunk_count - 1:
            return self.plaintext_length - index * self.chunk_size
        return self.chunk_size

    def chunk_offset(self, index: int) -> int:
        """Where chunk `index` starts in the file. Fixed-size, so arithmetic —
        no index table to read and no table to lie."""
        full = crypto.NONCE_BYTES + self.chunk_size + TAG_BYTES
        return HEADER_BYTES + index * full

    def chunk_stored_length(self, index: int) -> int:
        return crypto.NONCE_BYTES + self.chunk_plaintext_length(index) + TAG_BYTES


def is_chunked(head: bytes) -> bool:
    return head[:4] == MAGIC


def parse_header(head: bytes) -> Header:
    if len(head) < HEADER_BYTES or head[:4] != MAGIC:
        raise NotChunked("no BVLT header")
    _, version, chunk_size, length, count = _HEADER.unpack(head[:HEADER_BYTES])
    if version != VERSION:
        raise crypto.WrongSecret(f"unknown vault object version {version}")
    if chunk_size == 0 or count == 0:
        raise crypto.WrongSecret("a vault object header that describes nothing")
    return Header(chunk_size=chunk_size, plaintext_length=length, chunk_count=count)


def chunk_count_for(length: int, chunk_size: int = CHUNK_SIZE) -> int:
    return max(1, -(-length // chunk_size))


def _associated(document_id: uuid.UUID, index: int, header: Header) -> bytes:
    return document_id.bytes + struct.pack(
        ">IIQ", index, header.chunk_count, header.plaintext_length
    )


def seal_bytes(
    plaintext: bytes, key: bytes, document_id: uuid.UUID, *, chunk_size: int = CHUNK_SIZE
) -> bytes:
    """Whole file in, whole object out. Used by the seal, which already holds
    the plaintext; ranges are for reading, not writing."""
    count = chunk_count_for(len(plaintext), chunk_size)
    header = Header(chunk_size=chunk_size, plaintext_length=len(plaintext), chunk_count=count)
    out = [_HEADER.pack(MAGIC, VERSION, chunk_size, len(plaintext), count)]
    for index in range(count):
        piece = plaintext[index * chunk_size : (index + 1) * chunk_size]
        out.append(crypto.encrypt(piece, key, associated=_associated(document_id, index, header)))
    return b"".join(out)


class Reader:
    """Decrypt only what is asked for, straight from the file on disk.

    Holds no plaintext between calls. A 2 GB video seeked to the middle costs
    one or two chunks of memory, not two gigabytes.
    """

    def __init__(self, path: Path, key: bytes, document_id: uuid.UUID):
        self.path = path
        self.key = key
        self.document_id = document_id
        with path.open("rb") as handle:
            self.header = parse_header(handle.read(HEADER_BYTES))
        # A file shorter or longer than its header claims is refused up front,
        # before any chunk is trusted. The associated data would catch it too,
        # but this is cheaper and says what is wrong.
        expected = self.header.chunk_offset(self.header.chunk_count - 1) + (
            self.header.chunk_stored_length(self.header.chunk_count - 1)
        )
        if path.stat().st_size != expected:
            raise crypto.WrongSecret(
                "vault object length does not match its header — truncated or altered"
            )

    @property
    def length(self) -> int:
        return self.header.plaintext_length

    def _chunk(self, handle, index: int) -> bytes:
        handle.seek(self.header.chunk_offset(index))
        blob = handle.read(self.header.chunk_stored_length(index))
        return crypto.decrypt(
            blob, self.key, associated=_associated(self.document_id, index, self.header)
        )

    def read_all(self) -> bytes:
        with self.path.open("rb") as handle:
            return b"".join(self._chunk(handle, i) for i in range(self.header.chunk_count))

    def read_range(self, start: int, end: int) -> bytes:
        """Bytes `start` through `end` inclusive, HTTP-Range style."""
        if start < 0 or end < start or end >= self.length:
            raise ValueError(f"range {start}-{end} outside 0-{self.length - 1}")
        first = start // self.header.chunk_size
        last = end // self.header.chunk_size
        with self.path.open("rb") as handle:
            pieces = [self._chunk(handle, i) for i in range(first, last + 1)]
        joined = b"".join(pieces)
        offset = start - first * self.header.chunk_size
        return joined[offset : offset + (end - start + 1)]

    def verify(self) -> str:
        """Decrypt every chunk and return the plaintext's hex digest, holding
        one chunk at a time. What the seal's read-back and the re-seal use."""
        import hashlib

        digest = hashlib.sha256()
        with self.path.open("rb") as handle:
            for index in range(self.header.chunk_count):
                digest.update(self._chunk(handle, index))
        return digest.hexdigest()
