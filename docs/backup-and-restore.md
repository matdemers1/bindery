# Backup, export and the restore drill

The premise of this phase: **you could lose the server tomorrow and lose
nothing** — and you could abandon Bindery entirely and still have a usable
archive.

Everything below is designed to be checkable rather than assumed.

## The order matters

```
integrity check  →  pg_dump  →  copy blobs  →  encrypt offsite copy
```

Two rules, both load-bearing:

1. **Integrity before backup.** A backup taken over a corrupt blob is a corrupt
   backup, faithfully replicated and eventually rotated into every generation
   you have. `run_backup` refuses to proceed on a failing check unless forced.
2. **Postgres before blobs.** Blobs are content-addressed, immutable and
   append-only, so a blob written *after* the dump is simply unreferenced by it
   — a harmless orphan. The other order can produce a dump referencing a blob
   the backup does not contain, which is an unrestorable archive. The safe order
   is safe by construction, not by locking.

## Day to day

```bash
make integrity   # re-hash every original; non-zero exit if anything failed
make backup      # integrity, then dump, then blobs, then a manifest
make export      # originals + static index; works with the stack stopped
make mirror      # rebuild the browsable folder tree
```

All four are also buttons on the **Trust** screen. `make integrity` and
`make backup` exit non-zero on failure, so they are safe to put in cron without
a wrapper that has to interpret log output.

## The restore drill

This is the deliverable. Not "backups are configured" — **"I restored to an
empty container and found the DD-214."**

```bash
make drill b=/data/backups/20260828-031500
```

It stands up a clean Postgres on a throwaway port, restores the dump into it,
verifies that every original the restored database references is actually
present in the backup, and then searches the restored archive. It never touches
the live stack, and it tears its scratch container down even on failure.

It searches for `DD-214` by default; pass a second argument for anything else:

```bash
scripts/restore-drill.sh /data/backups/20260828-031500 "rating decision"
```

The drill can fail, and that is the entire point. A drill that always passes is
a ceremony.

> **Run it after every schema migration and at least quarterly.** The failure
> mode it catches — a backup that restores cleanly but is missing blobs, or a
> dump that no longer matches the code — is invisible until the day you need it.

## Abandonment insurance

`make export` writes a directory that does not need Bindery, Docker, Postgres or
Python:

```
index.html                       open in any browser; no server
U.S. Dept of Veterans Affairs/
  2009/2009-06-14 - Certificate of Release or Discharge.pdf
_bundles/
  2019-04-12 scan_0012 (12 pages)/
    index.html                   what is on which page
    original.pdf                 not split — originals are never modified
documents.json                   the same metadata, for a machine
```

Verify it the way the exit demo does: **stop the whole stack**, then open
`index.html` and navigate the archive.

## The go-bag

The vital tier only — birth certificate, DD-214, deed, passport — encrypted with
AES-256 into a standard zip that 7-Zip and Keka can open. Built from the Trust
screen with a passphrase of at least 12 characters.

The passphrase is never stored, here or on the server. Write it down somewhere
that is not this machine. A lost passphrase is a lost go-bag.

## 3-2-1

| Copy | Where | Made by |
|------|-------|---------|
| 1 | Live RAID-5 pool, `/media/Main-Storage/bindery/data` | the running stack |
| 2 | Local backup target, `BINDERY_BACKUP_ROOT` | `make backup` |
| 3 | Offsite, encrypted | **Not built yet — see below** |

> **Copy 3 does not exist yet.** `encrypt_for_offsite()` and `verify_encrypted()`
> are implemented and tested, but the only callers are in
> `tests/test_trust_and_export.py` — no route, no CLI verb, no Makefile target.
> `encrypt_for_offsite()` writes ciphertext to a *local* path and returns;
> nothing ships it anywhere. **Both existing copies are in the same building on
> the same array**, so today this scheme survives a dead disk and not a fire.
>
> Phase 13 builds the missing leg: replication to S3 under a KMS key, with a
> restore drill that pulls from the bucket. See `docs/offsite-replication.md`
> once it lands, and ADR-010 in the vault for why the offsite copy is
> server-side rather than client-side encrypted.

The offsite copy is encrypted because it is, by definition, somewhere you do not
control. `verify_encrypted` is cheap and catches the failure that matters: an
encryption step that silently no-opped and left medical records in plaintext on
someone else's disk.

RAID-5 is redundancy, not backup. It survives a dead disk; it does not survive a
mistaken bulk edit, a filesystem bug, or the building.
