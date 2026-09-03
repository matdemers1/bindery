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

| Copy | Where | Made by | Verified by |
|------|-------|---------|-------------|
| 1 | Live RAID-5 pool, `/media/Main-Storage/bindery/data` | the running stack | `make integrity` |
| 2 | Local generations, `BINDERY_BACKUP_ROOT` (default `/data/backups`) | `make backup`, nightly from `infra/zimaos/bindery-backup.cron` | `make drill b=<generation>` |
| 3 | Offsite: S3 under a KMS key | the worker's replication loop, daily + weekly | `make drill-offsite` |

**All three exist.** Copy 3 is Phase 13, built and running: `api/offsite.py`
replicates to S3 with SSE-KMS, the worker checks every ten minutes and runs on
the cadence `offsite_run` says is due, every object it ships is recorded in a
ledger, and each run reconciles that ledger against the bucket and re-uploads
anything that has gone missing. The Trust screen shows the last run and the
health panel raises a critical alert — which reaches the notifier, and therefore
a phone — when replication has stopped. `docs/offsite-replication.md` is the
detail; ADR-010 is why the offsite copy is server-side rather than
client-side encrypted.

**After a fire, this is the procedure:**

```bash
make drill-offsite term="rating decision"
```

That restores from the bucket alone — no local backup directory, no blob pool,
no live stack — downloads every original the restored database references,
re-hashes each one against the address the database asked for, and searches the
result. It is the only step that proves copy 3 is real, so run it on a schedule
rather than on the day you need it.

Two things about copy 3 that are easy to get wrong:

- **The vault pepper is deliberately *not* offsite.** It only stops a stolen
  database being enough to attack a six-digit PIN, so the bucket holding the
  dump is the one place it must not live. It is in copy 2. Losing it costs the
  PIN, never data.
- **The lifecycle audit now runs itself.** A bucket-wide expiry rule would
  delete the blob prefix — the archive itself — with no error and no alert,
  because Bindery has no delete permission and would not be the one doing it
  (ADR-010, R-21). The worker's replication loop re-reads the live rules hourly
  and raises a **critical** alert — which means a notification, not just a line
  on the Health screen — when a rule would delete something meant to be kept, and
  a warning when the credential cannot read the rules at all.
  `make lifecycle-check` is still there and still worth running the moment
  anyone touches the bucket's configuration, rather than waiting an hour.
  It **reports**; it never acts. Expiry is performed by S3 and by nothing else.

The offsite copy is encrypted because it is, by definition, somewhere you do not
control. `verify_encrypted` is cheap and catches the failure that matters: an
encryption step that silently no-opped and left medical records in plaintext on
someone else's disk.

RAID-5 is redundancy, not backup. It survives a dead disk; it does not survive a
mistaken bulk edit, a filesystem bug, or the building.
