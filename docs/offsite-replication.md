# Offsite replication — the third copy

Copies 1 and 2 live in the same building on the same array. This is the one that
does not. Everything here was run for real on 2026-08-30; the commands are the
record, not an illustration.

Design and reasoning: **ADR-010** in the vault. This file is how it was built and
how to rebuild it.

## What exists

| Thing | Value |
|---|---|
| Account | `150056528345` |
| Region | `us-east-1` |
| Bucket | `bindery-offsite-d3cloud` |
| KMS alias | `alias/bindery-offsite` |
| KMS key id | `6c1e27d6-5bee-4383-8d9b-b0b653e4ff85` |
| IAM user | `bindery-offsite` |
| Inline policy | `bindery-offsite-write-only` |

None of the above is secret. The key id in particular **must** be recoverable
without the archive, because a restore has to know which key to ask for — it is
in `.deploy/SECRETS.md` for that reason alone.

## What replicates, and what deliberately does not

| Path | Size | Offsite |
|---|--:|:-:|
| `blobs/` | 318 MB / 496 files | **yes** — irreplaceable |
| `pg_dump -Fc` | 4.3 MB | **yes** |
| `derived/` | 790 MB | no — reproducible from blobs |
| `inbox/household/` | 13 GB | no — a mirror of a Documents folder, 6.5 GB of it Minecraft |

Only 845 of the inbox's 80,800 files are PDFs or images, and 496 are already
blobs. The ~349 that are neither have **no offsite copy and no local backup
copy** either, since `backup.sh` copies `blobs/` only. That gap is R-22 and the
fix is to ingest them.

## Object layout

```
blobs/<sha256[0:2]>/<sha256>              put-once, never expires
dumps/daily/<ISO8601>.dump                expires after 7 days
dumps/weekly/<ISO week>.dump              expires after 35 days
manifests/{daily,weekly}/<...>.json       never expires
_probe/connection-test                    expires after 1 day
```

The weekly key is the **ISO week** rather than a timestamp, so a re-run in the
same week overwrites its own generation instead of consuming one of the five the
retention window holds.

Verified against the live bucket on 2026-08-30: every key the code generates is
matched by the lifecycle rule intended for it — daily dumps by
`expire-daily-dumps`, weekly by `expire-weekly-dumps`, probes by
`expire-connection-probes`, and manifests by nothing, which is deliberate. They
are kilobytes, and they are the record of what each generation contained.

`_probe/` exists because the connection test writes a real object and reads it
back, and **Bindery cannot delete it** — so the bucket has to. Without that rule
the prefix would accumulate one immortal object per press of the button.

## The two rules that matter

**Bindery cannot delete.** Not from the bucket, not the key. Rotation is a
lifecycle rule; deletion is a human with console access. This is what makes the
offsite copy survive a compromised Zima — ransomware reaches copies 1 and 2 and
stops there.

**Every lifecycle rule carries a prefix filter.** A bucket-wide expiry would
silently delete `blobs/` — the archive itself — and Bindery, holding no delete
permission, would neither cause it nor notice it. The single unfiltered rule
permitted is `AbortIncompleteMultipartUpload`, which expires nothing that
exists. Guarded two ways, because they catch different mistakes:

```bash
make lifecycle-check   # audits the LIVE bucket; non-zero on any finding
```

The worker also runs that audit on its own, hourly, from the same loop that
drives replication — it already holds the client and the credentials, and it is
one signed API call. A rule that would delete something meant to be kept raises a
`critical` alert, which leaves the building through the notifier; a credential
that cannot read the lifecycle configuration raises a `warning`, because a check
that examines nothing and reports nothing wrong is the shape of every silent
monitoring failure. Until then this audit had one caller, and it was a human
typing the command above (CR-099).

The test suite audits `infra/aws/lifecycle.json`, which is what gets deployed —
that catches a bad rule at the moment it is written. `make lifecycle-check`
audits what the bucket actually has, which is a different question the moment
somebody edits a rule in the console.

Both use the same audit, and both check the rules against **keys built by the
real key builders** rather than hard-coded strings. A test asserting
`dumps/daily/...` would keep passing after the key format changed, which is
exactly the drift worth catching.

Verified on the live bucket on 2026-08-30 by deploying a bucket-wide 90-day
expiry to the (empty) bucket and confirming the audit reported it — the
unfiltered rule, the blob pool, and both manifest kinds — then reverting.

## Turning it on for the first time (the Zima)

The host runs a standalone compose file at `/DATA/AppData/bindery` — no
repository, no `.env`, container names `bindery-api` / `bindery-worker`. Every
command below assumes that, and every one of them is deliberate: nothing about
this deploy is automatic, because nothing that holds your passport should
restart without you choosing the moment.

**1. Back up before migrating.** R-10, and this deploy carries three
migrations (0021–0023).

```bash
ssh root@<zima-lan-ip> /DATA/AppData/bindery/backup.sh
```

**2. Verify that backup rather than assuming it.** A backup nobody has
restored from is a hypothesis, and that applies most on the day you are about
to change the schema.

```bash
ssh root@<zima-lan-ip>
PG_IMAGE=pgvector/pgvector:pg16 /DATA/AppData/bindery/restore-drill.sh \
  /media/Main-Storage/Backups/bindery
```

**3. Pull the new images.** `DOCKER_CONFIG` is not optional — the daemon reads
`/root/.docker` and `docker login` wrote `/DATA/.docker`.

```bash
cd /DATA/AppData/bindery
DOCKER_CONFIG=/DATA/.docker docker compose pull
docker compose up -d
```

**4. Apply the migrations explicitly** (REQ-114 — never on boot).

```bash
docker exec bindery-api alembic upgrade head    # → 0023_offsite_object_absent
```

Then confirm the footer in the UI no longer says *migration pending*, and that
api and worker report the same commit.

**5. Copy the updated drill.** It is a file on the host, not part of an image,
so a `docker compose pull` does not update it.

```bash
scp scripts/restore-drill.sh root@<zima-lan-ip>:/DATA/AppData/bindery/
```

**6. Configure the credentials in the UI**, not on the host: Settings →
Offsite replication. Bucket `bindery-offsite-d3cloud`, region `us-east-1`, KMS
`alias/bindery-offsite`. Then **Test connection** — it writes and reads a real
object and will say which key encrypted it.

**7. Let the first run happen.** A weekly generation is due immediately on an
archive that has never replicated, and the worker checks every ten minutes.
Trust → Export & resilience shows it. The first run uploads the whole pool —
318 MB across 496 objects at last measure — and every run after it uploads only
what is new.

**8. The drill, on the real archive.** This is the point of the phase.

```bash
API_CONTAINER=bindery-api PG_IMAGE=pgvector/pgvector:pg16 \
  /DATA/AppData/bindery/restore-drill.sh --from-s3 "DD-214"
```

**9. Then check the audit is honest.** `make lifecycle-check` against the live
bucket, and confirm the Trust screen's age is minutes rather than *never*.

> The exit demo goes further than step 8 and is worth doing once: a scratch
> machine, an AWS login out of the password manager, and no `.deploy/SECRETS.md`
> and no Zima at all.

## Rebuilding it from nothing

```bash
aws kms create-key --description "Bindery offsite backup encryption (ADR-010)" \
  --key-usage ENCRYPT_DECRYPT --key-spec SYMMETRIC_DEFAULT
aws kms enable-key-rotation --key-id <key-id>
aws kms create-alias --alias-name alias/bindery-offsite --target-key-id <key-id>

aws s3api create-bucket --bucket bindery-offsite-d3cloud --region us-east-1
aws s3api put-public-access-block --bucket bindery-offsite-d3cloud \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-versioning --bucket bindery-offsite-d3cloud \
  --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket bindery-offsite-d3cloud \
  --server-side-encryption-configuration file://infra/aws/bucket-encryption.json
aws s3api put-bucket-lifecycle-configuration --bucket bindery-offsite-d3cloud \
  --lifecycle-configuration file://infra/aws/lifecycle.json

aws iam create-user --user-name bindery-offsite
aws iam put-user-policy --user-name bindery-offsite \
  --policy-name bindery-offsite-write-only \
  --policy-document file://infra/aws/bindery-offsite-policy.json
```

`BucketKeyEnabled` is **not optional**. Without it every blob upload is a
separate KMS request, and so is every object read during a restore drill.

## The order a run goes in

```
pg_dump to disk  →  sync blobs  →  upload the dump  →  upload the manifest
```

**This is not the local backup's order, and the difference is load-bearing.**
Locally the rule is dump, then copy blobs: blobs are append-only, so a blob
copied after the dump is an unreferenced orphan, which is harmless.

Replication adds a case the local copy does not have — it is incremental and it
can be interrupted. Send the dump first, fail partway through the blobs, and the
*bucket* holds a dump referencing objects that are not in it. That is a dangling
reference, and it persists until some later run happens to finish.

So the dump is written to disk at the moment it would have been taken anyway,
and is the last thing sent. Everything it references is already up; anything
uploaded after it is an orphan.

For the same reason, **a run with any failed blob does not send its dump at
all.** A dump in the bucket is a promise that its blobs are there too, and a
partial run cannot make that promise.

The integrity check gates all of it, for the reason the local backup gives: a
backup taken over a corrupt blob is a corrupt backup, faithfully replicated and
eventually rotated into every generation you hold.

## The cadence

Two schedules, deliberately expressed differently.

**Daily is an interval** — due when the newest success is more than 20 hours
old. Twenty rather than 24 so a run does not creep an hour later each day until
it lands in the middle of the afternoon, and an interval rather than a clock
time so a machine switched off overnight runs when it comes back instead of
skipping a day and reporting success.

**Weekly is a calendar week**, because the object key *is* the ISO week. The
bucket can hold one weekly generation per week, so "one per ISO week" is not a
policy choice — it is the only thing the naming scheme can express. A seven-day
interval would drift across a week boundary and silently leave a week with no
generation at all. (Sunday the 30th and Monday the 24th are the same ISO week,
six days apart. "Last week" and "seven days ago" are different questions.)

A failed run never counts as a success, and no successful run at all reads as
**stale**, not as green. A dashboard that is reassuring because nothing has
happened yet is the failure the Trust screen exists to prevent.

The loop lives in the worker, checks every ten minutes, and is silent when
nothing is configured. It is deliberately **not** a `JobStage` — see ADR-010 for
why that would silently stop after the first run.

## When it tells you

The Trust screen shows all of this, and a screen only helps someone who opens
it. So replication staleness also raises an alert on the health panel, through
the same notifier a stalled pipeline uses. The failure being defended against is
replication that quietly stopped in March and is noticed in November.

`Notifier.dispatch` sends **only critical** alerts, so severity is the
difference between "visible to anyone looking" and "pages you":

| State | Severity | Pages? |
|---|---|:-:|
| Not configured — both copies in one building | warning | no |
| Configured, first run not yet made | warning | no |
| Succeeded recently, but failing since | warning | no |
| Configured, every attempt has failed | **critical** | yes |
| Last success more than 48 hours ago | **critical** | yes |

Not-configured is deliberately not critical: every fresh install is in that
state, and an alert that fires on first boot teaches people the channel is
noise. It still appears on the panel, because "there is no offsite copy" is R-09
and staying quiet about it is how that risk spent two days looking closed.

The failing-since-last-success warning is the early one. A success ten hours ago
followed by two failures is not stale yet — the threshold is 48 hours — but it
is on its way, and saying so buys a day and a half.

## The connection test

**Settings → Offsite replication → Test connection.** It writes an object,
reads it back, and checks four things independently: that the bytes match, that
the object came back encrypted with `aws:kms`, that it was stored under the
configured key, and whether Bucket Keys are on.

A `ListBucket` would have been simpler and would prove nothing — `PutObject`,
`kms:GenerateDataKey`, `GetObject` and `kms:Decrypt` are four permissions that
fail independently, and reachability exercises none of them.

The put names the KMS key **explicitly** rather than relying on the bucket
default. That is what makes a wrong key id detectable: an omitted `SSEKMSKeyId`
would silently fall back to the bucket's default key and report success for a
key that is not the one configured.

Verified against the live bucket on 2026-08-30:

| Given | AWS does |
|---|---|
| The correct key ARN | Stores it, reports the ARN and `BucketKeyEnabled: true` |
| `alias/bindery-offsite` | Resolves it and reports the **key** ARN back |
| A key UUID that does not exist | Refuses the put — `KMS.NotFoundException` |
| An alias that does not exist | Refuses the put — `KMS.NotFoundException` |

So a typo in either spelling fails at write time, and a real-but-different key
is caught by comparing the returned ARN.

## Verifying the policy without issuing a credential

The simulator answers "what would this user be allowed to do" with no access key
in existence:

```bash
aws iam simulate-principal-policy \
  --policy-source-arn arn:aws:iam::150056528345:user/bindery-offsite \
  --action-names s3:DeleteObject \
  --resource-arns 'arn:aws:s3:::bindery-offsite-d3cloud/blobs/ab/abc123'
```

Verified 2026-08-30 — `PutObject`, `GetObject`, `ListBucket`,
`kms:GenerateDataKey`, `kms:Decrypt` **allowed**; `DeleteObject`,
`DeleteObjectVersion`, `DeleteBucket`, `PutLifecycleConfiguration`,
`kms:ScheduleKeyDeletion`, `kms:DisableKey` **explicitDeny**.

## The credential

Created in the console **by a human**, and pasted into Bindery's own Settings
screen. It is never generated by tooling that would print it into a log, a
terminal history, or a transcript.

It is stored the same way the Anthropic key is: Fernet-encrypted in the
`setting` table under a `JWT_SECRET`-derived key, masked to the last four
characters on read, database-over-environment precedence.

> Rotate it by creating a second key, pasting the new one, confirming a
> replication run succeeds, and only then deactivating the old one. The IAM user
> may hold two keys at once precisely so rotation needs no outage.

## Two things a human still owns

- **A CloudWatch alarm on `ScheduleKeyDeletion`.** A scheduled key deletion is
  the one event that makes the entire bucket permanently unreadable. The waiting
  period cannot be pre-set — `PendingWindowInDays` is null on an enabled key and
  only exists once deletion is scheduled — so the alarm *is* the protection. It
  needs an SNS topic to a real address, which is outside Bindery because there is
  no email service here.
- **Never put root credentials on the Zima.** Provisioning was done as root
  because one-time setup is what root is for. The host gets `bindery-offsite`
  and nothing else.

## The drill

A backup nobody has restored from is a hypothesis, and that applies to this copy
exactly as it applied to the local one:

```bash
scripts/restore-drill.sh --from-s3 [search-term]
```

Restores into a clean container from the bucket alone — no local backup
directory, no blob pool, no live stack — and searches the result.

It is a **stronger** check than the local drill, because the offsite copy makes
it possible. The local version asks whether a blob is present in a directory.
This one downloads every original the restored database references and
re-hashes each against the content address that database asked for. A blob that
is present but wrong is the failure a presence check cannot see, and it is the
one that matters: a backup that restores cleanly and hands back different bytes
is worse than one that fails loudly.

Blobs are fetched *after* the restore, once the database has said which ones it
needs. That is cheaper than pulling the whole pool, and it is what a real
recovery does.

### It can fail, which is the point

Verified on 2026-08-30, against the live bucket:

| Done to it | What the drill did |
|---|---|
| Searched for a term not in the archive | `DRILL FAILED — could not find it` |
| Deleted one original from the bucket | Named the exact blob, refused, `it is not restorable` |
| Ran `reconcile`, then a sync | Detected the one gap, re-uploaded **one** object, not 36 |
| Re-ran the drill | Passed |

That third row is the self-healing property, and it did not work when first
written — see the note on `absent_at` below.

### The exit demo

Worth doing once for real, and it goes further than the drill: a scratch
machine, an AWS login out of the password manager, and no `.deploy/SECRETS.md`
at all.

Then disable the KMS key and confirm the same restore becomes impossible — a
stop-button that has never been tested is not a stop-button.
