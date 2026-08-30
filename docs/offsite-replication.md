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
exists. A guard that asserts this against the live bucket is **T-13.9 and not yet built** — until it is, this rule is enforced by reading the table below and nothing else.

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
scripts/restore-drill.sh --from-s3   # T-13.10 — not built yet
```

Restore into a clean container from the bucket alone and find the DD-214. The
exit demo goes further and is worth doing once for real: a scratch machine, an
AWS login out of the password manager, and no `.deploy/SECRETS.md` at all.

Then disable the KMS key and confirm the same restore becomes impossible — a
stop-button that has never been tested is not a stop-button.
