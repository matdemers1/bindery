# CLAUDE.md — Bindery

Self-hosted document archive. **Read `D3 Cloud Vault/Bindery/` before doing any work** — the planning corpus there is the source of truth for every decision below.

## What Bindery Is

Bindery finds the document you can't find. It OCRs and indexes at the **page** level, decomposes bundled PDFs into their real constituent documents without modifying originals, and classifies with Claude against a taxonomy it already knows.

**Success criterion:** find the DD-214 in under 10 seconds.
**Kill criterion:** loss of trust in the automated filing.

## The Governing Principle — Auditable Automation

> Automate by default; make every automated decision cheap to inspect and one click to reverse.

Trust is earned after the fact, not by asking permission first. This is a **constraint on every feature that touches AI output**:

- Provenance on every AI-written field — which prompt version, which model, what confidence, which source snippet and page
- Append-only audit trail on all mutations, human and machine
- Undo for every automated action, including un-filing and un-segmenting
- Confidence surfaced in the UI, not only used at the gate

## Non-Negotiable Invariants

Breaking any of these is a bug, not a tradeoff:

1. **Originals are never modified.** Content-addressed, immutable, write-once. Exports are derivative artifacts.
2. **A document is a page range over a source file** — `(source_file_id, page_start, page_end)`. Bundles are sliced virtually. There is no separate segment table.
3. **Nothing is ever automatically deleted.** No unattended destructive code path may exist.
4. **Library is the access boundary.** Every document belongs to exactly one. Filtering happens at the repository layer, never at individual call sites.
5. **Auto-file gating reads structural signals, not the model's self-reported confidence.** LLM confidence is poorly calibrated; gate on known-form matches, `existing_ids`-only tags, pre-existing correspondents, labeled dates, and rule hits.
6. **Reused taxonomy resolves by ID and never passes through normalization or translation.** `existing_ids` and `new_names` are separate response fields.
7. **Retrieval never depends on the Claude API.** A document ingested during an outage is OCR'd, paged, and fully searchable — only classification defers.
8. **Nothing fails silently.** Every failed document surfaces somewhere a human will see it.
9. **No published host ports.** Ingress is Cloudflare Tunnel only. Since
   2026-08-30 the tunnel is the *only* thing in front of the app — Cloudflare
   Access is gone, so Bindery's own login page faces the open internet and
   `api/auth/throttle.py` is load-bearing rather than defence in depth.
10. **Migrations are applied explicitly**, never on container boot.

## Stack

Python 3.13 · FastAPI · SQLAlchemy 2.0 (async) · Alembic · PostgreSQL 16 (`pg_trgm`, `pgvector`) · OCRmyPDF 17 + Tesseract 5 · React 19 + Vite + Tailwind v4 (dark-first) · Docker Compose · Cloudflare Tunnel (Access removed 2026-08-30, ADR-008) · Claude Opus 5 behind an `AIProvider` adapter.

**No Redis.** The job queue is Postgres `SELECT … FOR UPDATE SKIP LOCKED`, and the `job` table is the observability surface.

## Pipeline

`ingest → normalize (OCR) → page → segment → embed → classify → rules → file → mirror`

**Every stage persists its output artifact and is independently replayable**, keyed on `(source_file_id, stage, prompt_version)`. Re-classify without re-OCR. Re-segment without re-ingest. This is what makes prompt and OCR iteration affordable — do not break it.

## Dev Commands

The compose file lives in `infra/`, so every raw invocation needs
`--env-file .env` to pick up the repo-root env. The Makefile exists only to carry
that flag — use it.

```bash
make up            # start the stack (no tunnel); `make tunnel` adds ingress
make migrate       # alembic upgrade head — explicit, never on boot
make logs          # watch the pipeline
make test          # full suite, against a throwaway bindery_test database
make ps            # no row may show a host->container port mapping (REQ-104)
make create-user email=you@example.com library=Household
```

Tests run **inside the stack**, not on the host: there are no published ports, so
the database is unreachable from outside. `make test` uses the `dev` target of
`Dockerfile.api` and a separate `bindery_test` database.

```bash
make test                                    # everything
docker compose --env-file .env -f infra/docker-compose.yml --profile test \
  run --rm test python -m pytest tests/test_permissions.py   # the leak suite (Phase 7)
```

## Current State — deployed and running; the R-01 gate is still open

**Live on the ZimaOS host at `bindery.d3cloud.io`** since 2026-08-28 — see
`docs/zimaos-deploy.md`, and read its "two quirks" section before touching the
host, because ZimaOS's read-only root and split `HOME` break SSH keys and
private registry pulls in ways that look like unrelated problems.

**Phase 1** (retrieval) and **Phase 2** (bundles, known forms) are built and
verified. **Phase 3** (classification, provenance, the gate, rules, undo) is
built — deliberately, with its own entry gate unmet.

> **The R-01 OCR accuracy figure has never been measured**, because the golden
> corpus has no real fixtures. Phase 3's plan says not to start without it. It
> was started anyway. The consequence to keep in mind: `REQ-058` (auto-file
> precision >= 95%) is unscored, and the gate's weights were reasoned rather
> than calibrated. The gate is a pure function of stored signals, so every past
> decision can be re-derived once a figure exists — but until then this is
> carried debt, not resolved risk.

Still open:
- **T-1.12** — the golden-corpus OCR figure. `make ocr-report`; add real
  fixtures to `tests/corpus/<name>/`. **This gates Phase 3's REQ-058 and
  Phase 2's REQ-035 boundary F1.**
- **T-1.13** — the Brother Scan-to-SMB spike. Needs the scanner.
- **No live Claude API call has ever been made.** Prompt quality, cost per
  document and cache hit rate are unmeasured. With `ANTHROPIC_API_KEY` unset the
  archive works and classification defers, which is the designed behaviour
  (REQ-055), not a broken state.

Not started: Phase 4 (backlog import).

## Layering rule

`worker/` may import `api/`; **`api/` must never import `worker/`.** Anything
both processes need — config, models, session, queue, blob store, ingest —
lives in `api/`, because that is the only package both images carry. `worker/`
holds the pipeline entrypoint, its stages, and the ingest adapters.

## Pipeline shape today

`watched folder | upload → normalize → page → segment → embed → classify → rules`

`file` and `mirror` are later phases and are deliberately absent from `STAGES`,
so a job naming one dead-letters rather than silently succeeding. The filing
decision currently happens at the end of `rules`.

## Classification rules

- **The gate never reads the model's confidence.** It reads structural facts:
  a known-form match, a rule firing, taxonomy that came back entirely as
  `existing_ids`, a date quoted from a labelled field. Confidence is stored and
  displayed (REQ-065) and is not an input to `decide()`. If you are tempted to
  add it, re-read R-02 — this is the kill criterion.
- **Gate decisions must stay a pure function of stored signals.** No clock, no
  randomness, no database read inside `decide()`. `gate.replay()` re-derives any
  past decision, which is what makes recalibration possible after the fact.
- **`existing_ids` resolve by id and by nothing else** — no normalisation, no
  fuzzy match. Ids are re-validated against the document's library *after* the
  call, because filtering the candidates does not stop a model returning an id
  it was never shown. An unresolvable id zeroes the gate score.
- **Provenance is written in the same transaction as the values it justifies.**
  A classification without evidence is a bug, not a degraded result.
- **Only ambiguous seams reach the model.** Heuristics settle the easy ones; the
  LLM confirmation pass sees the rest, which keeps segmentation cost
  proportional to difficulty rather than page count (R-06).
- **Embeddings are local and lexical** (ADR-007). Re-embedding the archive costs
  CPU and nothing else, so `make enqueue-stage stage=embed` is cheap.

## Segmentation rules

- **Segments are superseded, never deleted.** Live segments are the rows with
  `superseded_at IS NULL`; everything else is history. This is what makes undo
  (REQ-037) a flag flip, and it is why the exclusion constraint is partial. The
  same pattern applies to `document_tag.removed_at`.
- **A segmentation is a whole-file operation.** The complete cover is submitted
  at once, because a partial edit has no valid intermediate state — and because
  the old ranges must be retired before the new ones are inserted or the
  exclusion constraint rejects them.
- **A cover must be gapless.** The database refuses overlaps; gaps it cannot
  see, so `api/segments.validate` refuses them with an explanation. A page in no
  document is a page that has quietly become unfindable.
- **Heuristics propose, they do not decide.** A file with no confident boundary
  becomes one document spanning every page. The segment stage refuses to touch a
  file a human has already segmented.
- **Known-form matching is re-run whenever boundaries move.** A moved boundary
  can turn a DD-214 into a fragment, and a stale fact is worse than none.

## Conventions

- **Adapter pattern** for every external service (AI, notifications, future cloud replication)
- **Audit logging on all mutations** — ecosystem standard, and here it is also the trust surface
- **Prompt files are versioned** (`worker/ai/prompts/classify_v1.md`); the version is written to every classification row
- **AI calls are never live in the default test run** — tests use recorded responses
- Repository-layer permission scoping in `api/db/repository.py`; no call site implements its own check
- **Enum columns use `pg_enum()` from `api/db/base.py`**, which persists member
  *values*. Plain `sa.Enum(SomeEnum)` writes member *names* and will not match
  the lowercase types the migrations create.
- `tests/test_no_destructive_paths.py` enforces REQ-090 by scanning `api/` and
  `worker/`. If it fails, revoke or tombstone — do not loosen the pattern list.
- **The `test` and `test-worker` compose services bind-mount `api/`, `worker/`,
  `tests/`, `alembic/`, `infra/`, `scripts/`, `web/src`, `web/public` and
  `pyproject.toml`.** Without those mounts the suite runs whatever source was
  baked into the image and reports a pass on code you have already changed —
  which happened once, and cost a stale green run. `infra/` was added later for
  the same reason: a guard asserting the worker image can run `pg_dump` was
  reading the Dockerfile baked into the test image and reporting on a version of
  the repository that no longer existed.

## Trust, export and resilience (Phase 6)

`docs/backup-and-restore.md` is the runbook. Four things to keep straight:

- **The ordering rule.** Integrity check → `pg_dump` → copy blobs. Blobs are
  content-addressed and immutable, so a blob newer than the dump is a harmless
  orphan; the other order can produce a dangling reference. `run_backup` refuses
  to run over a failing integrity check unless explicitly forced.
- **The export must work with the stack stopped.** `api/export/archive_export.py`
  writes a semantic folder tree plus a self-contained `index.html` — no scripts,
  no external assets, no absolute paths. A test asserts all three, because
  "works without Bindery" is a property that decays silently.
- **The mirror is the one place that deletes files**, and it is safe only because
  nothing in it is an original: every entry is a hardlink to an immutable 0444
  blob, and the whole tree is regenerated from the database. REQ-090 is about
  originals and records; it is not violated by tidying a derived index.
- **The restore drill is the deliverable**, not the backup. `make drill b=<dir>`
  restores into a throwaway container and searches the restored data for the
  DD-214. Run it after every schema migration.

Bundles never become one file per document anywhere — not in the export, not in
the mirror. A twelve-page scan holding three documents is one file plus an
`index.html` naming the page ranges, because splitting it would modify an
original.

## Household and libraries (Phase 7)

The library is the access boundary (ADR-005). A permission bug here is not an
inconvenience — it is the disclosure of one household member's medical history
to another — so this phase is defended by tests, not by inspection.

- **`api/db/scope.py` is the boundary.** `Depends(current_scope)` hands a route
  queries that are already filtered. `require_visible` and `require_write`
  return **404, not 403**, for an invisible library: a 403 confirms the thing
  exists, and a probe should learn nothing.
- **`tests/test_permission_boundary.py` is the leak suite.** Three users, three
  libraries, one document that must never leak, and a parametrised sweep over
  every read path. It caught the Phase 6 audit endpoint, which called the
  permission helper and then discarded the answer.
- **The route-coverage guard is the part that keeps it true.** It enumerates the
  OpenAPI schema and fails if a GET route under `/api` is not exercised by the
  sweep. Add new endpoints to `EVERY_READ_PATH`, or to `NOT_LIBRARY_SCOPED`
  with a reason. It also asserts it examined a non-zero number of routes,
  because its first version passed while examining none.
- **A move is file-scoped, and defers a constraint.**
  `fk_document_source_file_library` ties a document's library to its file's, and
  no row order satisfies it midway, so `api/moves.py` sets the constraint
  DEFERRED for that transaction (migration 0009). Taxonomy does not travel:
  cross-library tags are *revoked*, never deleted, and named in the audit
  `before`.

## Ask, health and hardening (Phase 8)

- **An uncited answer is discarded, not shown with a caveat** (`api/ask.py`). A
  caveat is read once; an answer is believed. Citations come from the API's
  citations feature and index into the exact blocks sent, so the page number is
  a lookup rather than something the model wrote. Retrieval is Postgres, so
  `/api/ask` still returns the matching pages with no key and no network.
- **Question retrieval runs precise-then-broad.** Content words ANDed, then the
  four most selective ORed. "when did I last get the brakes done?" contains one
  word a receipt has.
- **The health panel separates queue depth, failures and stalls**, because they
  mean different things. A stall — queued work, nothing running — is the one
  that never announces itself. The monitor runs in the *worker*, since the
  condition is the worker not working.
- **Notifications dedupe per alert code** on a six-hour cooldown and clear when
  the condition resolves. A failed webhook is logged and never propagates.
- **API tokens are re-intersected with their creator's memberships on every
  request**, so removing a membership shrinks every token immediately without
  anyone hunting for them. Only the hash is stored; the secret is shown once.
- **Latency, measured at 100K pages:** search p95 15 ms (budget 300), palette
  p95 15 ms (budget 100), ask retrieval 41 ms. The `continuation sheet` case —
  one term matching a fifth of the archive — is reported at ~650–800 ms and
  deliberately not gated, at both the search and ask layers.

## Two UI rules worth keeping

- **Page padding lives in `Shell`'s `<main>`, once.** Screens choose their own
  `mx-auto max-w-*` because a table wants more room than prose, but they do not
  repeat the gutter. Every screen used to, and the four added last simply
  forgot — which is how Trust ended up flush against the window edge.
- **`repository.visible_jobs` reaches a job by *either* key.** It joined only
  through `source_file`, which was true of every job when it was written and
  stopped being true when classify began enqueueing by `document_id`. The
  effect was that every classification failure was invisible on the one screen
  whose stated purpose is that nothing fails silently.

## Re-running AI review

`api/reclassify.py`. The archive is deliberately useful with no API key, so
adding documents first and a key later is the *normal* path, not a recovery
path — and it needs to be reachable without a shell on the host.

The two situations are reported separately and must stay that way: **"never
attempted"** (arrived before a key existed — not a failure) and **"gave up"**
(attempted, dead-lettered). Calling the first a failure is alarming and wrong.

"Waiting" means `review_state == PENDING_CLASSIFICATION`, never merely "has no
classification row" — a document filed by a rule or by hand has no
classification and is finished, and offering to run AI over it is offering to
overwrite a person's work.

## OCR, rescan and replay

- **A page ocrmypdf declines to touch produces silence, not a failure.** Vector
  content rather than an image makes it skip the page "to avoid losing detail"
  and **exit 0** — the file arrives `processed`, unsearchable, with nothing to
  retry. `normalize` checks the word count it already extracts and re-runs with
  `--force-ocr` when it is zero. Safe because there was nothing to lose: a
  digital-native PDF has a text layer, so a non-zero count, so REQ-017 holds.
- **Stage cascades use `requeue_stage`, never `enqueue`.** `enqueue` is
  idempotent and refuses to disturb an existing job — right for a first run,
  fatal for a replay. With `enqueue`, a rescan re-OCR'd a real file, recovered
  262 words, and stopped dead because paging had already succeeded once. On a
  first run the two are identical.
- **`GET /api/files/{id}/text` is the raw extraction**, verbatim. A search
  finding nothing is ambiguous until you can see whether the word was ever read
  correctly — which matters most on handwriting, where it is least likely.

## Choosing a model

`api/models.py` is the closed set (Opus 5 / Sonnet 5 / Haiku 4.5). Two reasons
it is closed: a typo'd id fails *every* classification at the worker, hours
later, as dead letters with no obvious cause; and the spend figure is
meaningless without knowing which model produced the tokens — they differ by
about 5x. Each classification is costed at the rates of the model that actually
ran, so switching to Haiku does not make last month cheaper, and an unrecognised
model is costed at the top rate so the tripwire errs towards alarming.

## Diagnostics

`api/eventlog.py`. Every log line the application writes is also persisted to
`event_log`, so a failure is explainable from the screen you noticed it on
rather than from the host's terminal scrollback.

It is a `logging.Handler` rather than a bespoke `log_event()` call, which is the
whole reason it works: there were already 75 log statements saying the right
things to the wrong place, and a handler adopts all of them — including the ones
written next year by someone who has never read the module.

Three rules, each of which is a specific piece of that file:

- **Logging must never break the thing it is logging.** `emit` cannot raise and
  cannot block; it drops onto a bounded queue and returns. Overflow drops the
  record, counts it, and reports the count *through the log*.
- **A log row must not roll back with the failure it describes.** The drain owns
  its own session.
- **Context travels with the work.** `eventlog.bind()` in the job runner tags
  every line a stage writes with the job, file, stage and library — so the
  boundary that governs a document governs its diagnostics, because log messages
  routinely contain filenames.

Log level matches the *outcome*, not the event: a retryable attempt is a
warning, a dead-letter is an error. Logging attempts that later succeed at ERROR
fills the error filter with noise, and an error filter you learn to ignore is
the same as not having one.

Not pruned. A few hundred bytes per job is tens of megabytes over the archive's
life, and a scheduled delete would be the one thing here that removes rows on
its own.

## Real-time updates

`api/events.py` + `api/routers/live.py` + `web/src/live/LiveProvider.tsx`.
**Do not add a polling timer to a screen.** Every screen used to own one, which
was wrong in both directions: the server was asked constantly while nothing
happened, and a screen still showed stale data for a whole interval after
something did — which is why the review badge kept claiming work already
accepted. One page had a dependency-array feedback loop and reached *sixty
requests a second*.

- **Transport is Postgres `LISTEN`/`NOTIFY`.** The worker is a separate
  container, so notification has to cross a process boundary, and the database
  is the only thing both already talk to. No Redis to run, back up or lose.
- **`pg_notify` inside a transaction fires on commit and is discarded on
  rollback**, so it is impossible to announce a change that did not happen.
  Always publish in the same session as the mutation.
- **Payloads are hints, not state** — a topic and a library. Clients refetch
  what they display, so the server stays the single definition of every
  screen's data. Pushing state would mean a second definition that can disagree.
- **`useLiveQuery(topics, load)`** is the only sanctioned pattern on the client.
  It refetches on mount, on a hint, and — only while pushes are unavailable — on
  a slow fallback timer.
- **nginx needs the upgrade dance** (`proxy_http_version 1.1`, `Upgrade`,
  `Connection`). Without it the handshake gets a plain 200 and the socket never
  forms — and because the dev server proxies WebSockets natively, that failure
  appears *only* in the deployed stack.

## Formats

Two families, and only one pipeline.

- **Scans and photographs** — `.pdf .jpg .jpeg .png .tif .tiff .heic .heif` —
  go straight to OCR. Photographs also have their EXIF read first (Phase 18):
  capture date, camera, dimensions, location, into `media_metadata`.
- **Videos** — everything ffprobe reads — are **not** OCR'd or classified.
  `normalize` probes them, writes a poster frame, and creates one filed
  document whose page text is the metadata summary, so the clip is findable by
  its date, camera and length. No downstream job is ever created for them.
  ffmpeg is worker-only, like Pillow.
- **Office documents** — Word, Excel, PowerPoint, OpenDocument, RTF, CSV, TXT,
  MD — are rendered to PDF by headless LibreOffice in `worker/convert.py` and
  then travel the *ordinary* path. That is the whole design: handling them
  natively would mean a second implementation of paging, viewing, citation and
  export for every format. The original is never replaced — the PDF is a derived
  artifact beside the blob, and an export still hands back the .xlsx.

`convert.CONVERTIBLE` and `walker.SUPPORTED` must stay in step, or the importer
skips a format the pipeline can handle — `tests/test_ocr_escalation.py` asserts
it, along with the fact that the scanned and office sets never overlap.

The supported list came from auditing a real 80,000-file archive rather than
from guessing, and every addition was verified against an actual file before
being added. Deliberate exclusions, each for its own reason:

| Excluded | Why |
|---|---|
| `.one`, `.onetoc2` | **Genuinely archive material** — college notes — but no converter works. LibreOffice fails to load it outright. A gap to report, not to paper over. |
| `.psd`, `.indd`, `.skp` | Design sources, not documents |
| `.zip`, `.gz`, `.rar` | Unpacking is a separate decision with its own hazards — nesting, bombs, and what "the original" means afterwards |
| code, build output, 3D printing, game data, audio, binaries | Not archive material, and they would bury the things that are. Video *was* on this list until Phase 18 |

`.html` is supported because a saved order confirmation or pay statement is
ordinary archive material. Be aware that generated documentation — a Javadoc
tree, for instance — is also HTML, so pointing the importer at a code folder
picks it up. That is a folder-choice problem, not a format problem.

Gotchas worth keeping: `soffice` **exits 0 on several failures**, so the output
file's existence is the only trustworthy signal; each conversion needs its own
`-env:UserInstallation` profile or two concurrent runs silently produce nothing;
and spreadsheets export through `calc_pdf_Export` so a wide bank statement
scales to fit instead of losing its right-hand columns off the page.

## Deployment

`docs/zimaos-deploy.md`. CI (`.github/workflows/build.yml`) is four gates in
series — **lint → unit → integration → e2e** — and only then publishes
`ghcr.io/matdemers1/bindery/{api,worker,web}` tagged `:main`, `:latest` and
`:sha-<commit>`. The ZimaOS host pulls `:main`; rolling back means pinning a
`:sha-` tag.

Sequential on purpose: parallel finishes sooner and also spends a full e2e run —
three image builds, a Postgres, a seeded corpus, a browser — to tell you about a
lint error. Cheapest gate first.

- **lint** — `ruff`, then `eslint --max-warnings 0` and `tsc --noEmit` for the
  web app and the e2e specs. Before this the only typecheck was `tsc -b` inside
  `Dockerfile.web`, which runs *after* the tests, so a type error surfaced as an
  opaque Docker build failure.
- **unit** — the default pytest run, excluding `live_api` and `slow`.
- **integration** — `pytest -m slow` in the **worker** image, where the OCR
  toolchain lives. ~30 tests.
- **e2e** — Playwright against the real compose stack: build, migrate
  explicitly, seed, then drive a browser. `make e2e` runs it locally against
  `make up`.

**Actions does not support YAML merge keys.** An `env: &anchor` plus `<<: *anchor`
parses locally and makes Actions refuse the whole file with "this run likely
failed because of a workflow file issue" — no line number, no failing job.
Repeat the block instead.

Every Dockerfile names its shipped stage **`runtime`**. The api and worker
Dockerfiles also have a `dev` stage carrying pytest and the whole source tree —
CI must keep passing `target: runtime`, or the deployed images ship the test
harness.

`infra/zimaos/bindery.zimaos.yaml` is the CasaOS custom-app manifest. It must
never gain a `ports:` key: ingress is the Cloudflare Tunnel only (REQ-104).

## Corrections (Phase 17)

`PATCH /documents/{id}`, `api/editing.py`, `api/field_source.py`. The archive
could find any document in ten seconds and could not fix one of them.

- **`field_source` is what makes a correction survive.** Per field, not per
  document: the model still improves the nine fields nobody corrected and
  leaves the one they did. Not `field_provenance` — that hangs off a
  classification and records the page and snippet behind an AI value, which is
  provenance of *evidence*. This is provenance of *authority*, and a document
  can have excellent evidence for a value a person has since overruled.
- **The failure to design against is the silent revert**, not a bad edit. Bad
  edits undo. Correcting a date and having AI review put it back three days
  later teaches people not to bother correcting anything.
- **Absent is not null.** A PATCH that omits `title` leaves it alone; one that
  sends `null` clears it. Without the distinction there is no way to remove a
  wrong date, only to replace it with another wrong date. A no-op is also not
  recorded — otherwise opening the form and saving freezes every field.
- **Creating taxonomy is an explicit act** (`create_correspondent`,
  `create_tags`), never a name falling through to a create because it matched
  nothing. Invariant 6 is about how reuse resolves, and a form that posts names
  resolves by string.
- **Undo releases the claims and restores the tags**, read from a manifest the
  edit recorded. Inferring "which tags did this edit touch" from timestamps
  sweeps up what a *later* edit did.
- **`field_source` rows are released, never deleted** — `released_at`, the same
  shape as `document_tag.removed_at`. The destructive-paths guard says revoke or
  tombstone rather than loosen the pattern list, and it was right.

## The private vault (Phase 16)

A second lock with its own passphrase, for documents that would otherwise not go
in the archive at all. `api/vault/`, ADR-012.

- **`api/vault/store.py` is the only file allowed to delete anything.**
  `tests/test_no_destructive_paths.py` names it as the single exemption to
  REQ-090 and asserts the exemption does not spread. The ordering is the whole
  feature: encrypt → write → **read back from disk** → compare to the source
  hash → only then unlink. It is asserted structurally, because a test that
  proves it by deleting a real document is a test that can lose one.
- **The read-back must fail closed.** Comparing hashes only catches a ciphertext
  that decrypts to *different* bytes; the likelier corruption is a flipped bit,
  which fails the AEAD tag. That escaped the refusal branch as an unhandled
  error until the tests found it.
- **`api/vault/boundary.py` is the single definition of what is hidden**, imported
  by both `api/db/scope.py` and `api/db/repository.py`. It exists because it was
  written twice and only one copy was updated — five of the leak suite's eleven
  failures had that one cause.
- **A vaulted document is hidden whether the vault is open or shut.** The first
  version let them back into every view while unlocked; the vault stays open
  for fifteen minutes, and a privacy feature that depends on remembering to
  lock it is not one anyone can rely on. Unlocked governs whether the vault can
  be *read*, not whether its contents leak into the archive.
- **Applying the boundary is not enough — a route has to be made to.**
  `/api/photos` predated the vault and never gained the clause, so vaulted
  photographs stayed on the wall in both states. The leak suite now runs every
  assertion locked *and* unlocked, on an image fixture, and carries a
  route-coverage guard. Same lesson as Phase 7's library boundary: the fix that
  lasts is the test that fails when a new route forgets.
- **Vault objects are not content-addressed.** The name is `secrets.token_hex(32)`,
  because a content address is an existence oracle: anyone holding a copy of a
  file could confirm the archive holds it without decrypting anything. They live
  outside `blobs/`, which is why `copy_blobs` and `sync_blobs` cannot see them
  and both needed their own path.
- **The pepper is in the local backup and never offsite.** It only stops a stolen
  *database* from being enough to attack a six-digit PIN, so the bucket holding
  the dump is the one place it must not be. Losing it costs the PIN, never data.
- **Backup, offsite, export and the restore drill all had to learn about it.**
  A vaulted document has no plaintext original, so the drill and the export both
  reported a healthy archive as corrupt until they were taught the difference.
- **Search decrypts and scans in memory.** A Postgres index would perform far
  better and would write the plaintext back to disk, which is the thing the
  vault exists to prevent. Linear cost, reported rather than hidden.

## Documentation is part of the change

`web/public/help/guides.json` is the user-facing documentation, and
`tests/test_docs.py` is what keeps it true. Adding a screen without a guide
fails the build; so does a guide for a screen that no longer exists, a missing
screenshot, and a screenshot older than the code it shows.

The staleness check is the one worth understanding. It compares the commit that
last touched a screen's source against the commit its screenshot was captured
at — deliberately **not** a pixel comparison, which differs between machines on
antialiasing alone and would be switched off within a month.

```bash
make up                       # a local stack
make screenshots              # captures from it, writes the manifest
```

Capture runs **inside the compose network**, sharing the web container's network
namespace. Two reasons, both learned the hard way: there are no published ports
(REQ-104), and the session cookie is `Secure`, so a browser reached over plain
`http://web` silently discards it — the login succeeds, every later request
401s, and the screen sits on the login form looking like a wrong password.
`localhost` is the one origin Chrome treats as trustworthy without TLS.

**Screenshots go in the repository, so they must never come from the real
archive.** `scripts/seed-demo.py` generates four invented documents and ingests
them; the cast on the People screen is Mum, Dad and Sister, and none of them
exist. The PDFs are generated rather than committed — a handful of bytes of code
beats four binaries nobody can diff, and they carry real selectable text, so
OCR, the known-form matcher and search all have something true to do.

## ⚠️ Private repository

`tests/corpus/` holds real personal documents (DD-214, VA medical records, financial statements) as the golden corpus. **This repository must never be made public.**

## Planning Corpus

`D3 Cloud Vault/Bindery/` — Discovery Roadmap · Discovery & Requirements · Research Notes · Feature Ideas · Architecture · Data Model · Glossary · UX Flows & Screen Inventory · Risk Register · Test Strategy · Requirements Register (131 REQs) · Scope of Work (10 phases) · Phase Plans ×10 · ADR-001 … ADR-006

Start a coding session with `/start-development bindery`.
