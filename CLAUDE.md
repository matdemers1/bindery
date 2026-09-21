# CLAUDE.md — Bindery

Self-hosted document archive. **Read Foreman before doing any work** — `foreman_brief BND` for where it stands, then its documents as resources. Foreman is the source of truth for every decision below (ADR-009); the vault is a read-only archive of the state before the 2026-09-20 cutover.

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
make help          # every target in the Makefile, from its own `##` comments
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

**Built: phases 0–11 and 13–20** — retrieval, bundles and known forms,
classification with provenance and the gate, backlog import, entities,
trust/export/resilience, household and libraries, ask/health/hardening, the
first-real-corpus consolidation, accounts and administration, documentation and
versioning, offsite replication, signal and navigation, the build gate, the
private vault, corrections, media/metadata/inbox, the front door (first-run setup and the entry screens), and Sign in with D3 Auth. **Planned next: Phase 12**
(later features). The per-phase truth is
Foreman's phases and tasks for `BND` — this line is a pointer, not a second
copy, because the copy is what went eleven phases stale.

Phase 3 was built **deliberately, with its own entry gate unmet**, and that debt
is still carried:

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
- **Taxonomy belongs to exactly one library, and the column is NOT NULL**
  (migration 0032). `tag`, `correspondent` and `document_type` used to permit a
  null, and four read paths widened to include such rows — a row belonging to
  every account at once, inside the one boundary ADR-005 calls the access
  boundary. Nothing ever created one, which is why it was cheap to close; the
  leak suite asserts the constraint at the database, because a query clause can
  be re-added and a NOT NULL cannot.
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
- **`api/ai_client.py` is the only place an Anthropic client is constructed**, and the only
  definition of what an exception from the API means. It lives in `api/` because that is the
  package both images carry, so the pipeline stage and the request a person is waiting on share
  one swap point — which is what ADR-003 is for: sending the OCR text of medical, identity and
  financial records off the network is an accepted trade, and the adapter is the reversal path.
  There were three clients, each with its own idea of the model default and of which failures
  were worth retrying, and `tests/test_one_ai_adapter.py` is what keeps there being one.
- **Audit logging on all mutations** — ecosystem standard, and here it is also the trust surface
- **Prompt files are versioned** (`worker/ai/prompts/classify_v1.md`); the version is written to every classification row
- **AI calls are never live in the default test run** — tests use recorded responses
- Repository-layer permission scoping in `api/db/repository.py`; no call site implements its own check
- **Enum columns use `pg_enum()` from `api/db/base.py`**, which persists member
  *values*. Plain `sa.Enum(SomeEnum)` writes member *names* and will not match
  the lowercase types the migrations create.
- `tests/test_no_destructive_paths.py` enforces REQ-090 by scanning `api/` and
  `worker/`. If it fails, revoke or tombstone — do not loosen the pattern list.
- **Python type checking is gated, at the level the code already passes.**
  `make typecheck` runs mypy over `api/` and `worker/` and compares the result to
  `scripts/mypy-baseline.json`; CI's lint job runs the same thing. This is a
  deliberate posture and it is written down here because the absence of one was
  the actual defect (CR-089): mypy sat in the `dev` extra for eleven phases with
  no `[tool.mypy]` section, no invocation, no gate and no note, against a
  codebase where 95% of functions carry annotations and there is not one
  `# type: ignore`. The expensive half was paid for and nothing read it.
  - **Not strict, and not by accident.** `disallow_untyped_defs` across 30k lines
    of SQLAlchemy 2.0 declarative models produces a number nobody burns down, and
    a gate that is red on arrival is one somebody switches off. There are 79
    known errors, and the gate is that a file may not *gain* one. Deliberately
    one-directional: a change that fixes a type error must not turn somebody
    else's build red. A run that finds fewer says so loudly and passes — refresh
    the baseline when you see that, or it keeps room for a regression.
  - **The environment is part of the answer.** A library that is installed and
    typed is checked; the same library absent is `Any`. Baseline and gate both
    run with the base + `dev` groups and no `worker` extra — the compose `test`
    service and CI's lint runner. Anywhere else, the diff is the environment.
  - Refresh with `python scripts/typecheck.py --update` (inside the `test`
    service, so the environment matches) and read the diff. Tighten a flag in
    `[tool.mypy]` once the baseline reaches zero, not before.
- **The wire contract is checked, not trusted.** `api/schemas.py` and
  `web/src/api.ts` are two hand-written halves of the same JSON.
  `tsc --noEmit` proves the client agrees with itself; `make contract`
  (`scripts/check_api_contract.py`, and a step in CI's lint gate) proves every
  field the client reads exists on the server. Without it a renamed response
  field passed all five gates and arrived in the browser as `undefined` on a
  screen the e2e specs did not assert on — and on the Why panel, a blank field is
  indistinguishable from an AI that had nothing to say (CR-059). A new interface
  must pair with a model, be added to `ALIASES`, or be listed in `UNPAIRED` with
  a reason.
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
- **The offsite bucket's lifecycle rules are audited hourly by the worker**, from
  the loop that drives replication (`_refresh_lifecycle_audit` in
  `worker/runner.py`, `offsite.check_lifecycle`). A rule that would delete
  something meant to be kept — `blobs/` or `vault/` — is a **critical** alert and
  leaves the building through the notifier; a credential that cannot read the
  configuration is a warning, because a check that examines nothing and reports
  nothing wrong is the shape of every silent monitoring failure. It reports and
  never acts: **Bindery must never delete an S3 object** (invariant 3, ADR-010).
  `make lifecycle-check` is the same audit on demand. Until CR-099 that command
  was the only caller, which made R-21 — the one failure able to erase the
  offsite archive — guarded by somebody remembering to type something.

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

`docs/zimaos-deploy.md`. CI (`.github/workflows/build.yml`) is five gates in
series — **lint → unit → integration → e2e → images** — and the last one publishes
`ghcr.io/matdemers1/bindery/{api,worker,web}` tagged `:main`, `:latest` and
`:sha-<commit>`. The ZimaOS host pulls `:main`; rolling back means pinning a
`:sha-` tag.

Sequential on purpose: parallel finishes sooner and also spends a full e2e run —
three image builds, a Postgres, a seeded corpus, a browser — to tell you about a
lint error. Cheapest gate first.

- **lint** — `ruff`, the API contract check, `mypy` against its baseline,
  `pip-audit`, `npm audit`, then `eslint --max-warnings 0`, `tsc --noEmit` for
  the web app and the e2e specs, and the vitest unit suite. **`npm audit` fails
  on an advisory and only warns when it cannot reach the registry** — it is the
  one check here that makes a live network call, and npmjs.org returning 503
  blocked a finished, four-gates-green release from deploying until the two
  cases were told apart. `pip-audit` has no such coupling; it reads the lockfile
  this repository ships.

  Everything cheap and database-free belongs in this gate rather than one gate
  later. Before it existed the only typecheck was `tsc -b` inside
  `Dockerfile.web`, which runs *after* the tests, so a type error surfaced as an
  opaque Docker build failure.
- **unit** — the default pytest run, excluding `live_api` and `slow`.
- **integration** — `pytest -m slow` in the **worker** image, where the OCR
  toolchain lives. ~30 tests.
- **e2e** — Playwright against the real compose stack: build, migrate
  explicitly, seed, then drive a browser. `make e2e` runs it locally against
  `make up`.
- **images** — the publish, `main` only. Every Dockerfile's shipped stage is
  `runtime` and CI must keep saying so.

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

## Sign in with D3 Auth (Phase 20)

`api/oidc.py` holds the rules, `api/routers/oidc.py` the routes, `web/src/features/entry/SignInWithD3Auth.tsx`
and `web/src/features/settings/ConnectD3Auth.tsx` the two places it shows. Bindery is the
reference relying party for [D3 Auth](https://auth.d3cloud.io); the provider's consumer contract
is the specification.

- **Off until an operator configures it**, and `optional` by intent: adding a second way in must
  not weaken the first. Nothing changes for an account that never uses it.
- **A sign-in through the provider ends in an ordinary Bindery session**, minted by
  `issue_session` exactly as a password login mints one. Nothing downstream of `current_user`
  knows SSO exists — which is what keeps the permission suite meaningful.
- **Identity is `(iss, sub)`.** Never email. An address is a display value that changes, is
  reused, and at some providers is chosen by the person claiming it. An address that already
  exists here is refused with "connect it from Settings instead", never linked.
- **The PKCE verifier, state and nonce live in a signed, short-lived cookie scoped to the
  callback.** Not a table keyed on `state` — that table is readable by whoever supplies the
  state, which is D3 Auth's own finding F-12: login-CSRF, and role theft through linking.
- **JIT provisioning needs a role.** The provider is deny-by-default, so arriving with a role is
  an administrator's decision that already happened. No role provisions nothing.
- **`admin` does not make an administrator until Bindery's own TOTP is enrolled** (REQ-156). An
  administrator here can reset every other password; a claim made at another server does not
  lift that.
- **Back-channel logout is idempotent by `jti` in Postgres**, not in the process: a retry landing
  elsewhere must not end a session the person has since started again. A repeat is a 200, or the
  provider retries for nothing and marks the app slow to revoke.
- **Disconnecting tombstones** (`unlinked_at`), and is named `disconnect` rather than `unlink`
  because `unlink` is how a file is deleted and the destructive-paths guard reads call sites by
  name.
- **The client is pinned to a commit** of the provider's repository and installed from a source
  archive — no git, no registry, no credentials, so a stranger's `docker compose build` works.
  pip refuses a direct reference that is also a constraint, so it is deliberately absent from
  `requirements.lock`. It ships `py.typed`, and must keep doing so: without the marker mypy skips
  the package, every call into it is `Any`, and `refresh_roles` was reading `roles` off a
  `Session` that carries them on its identity — with a test fake that had invented the same shape.
- **The api has to be told it is behind TLS.** Cloudflare terminates it and the tunnel speaks
  plain http to nginx, so `request.url` says `http://` unless nginx forwards `X-Forwarded-Proto`
  *and* uvicorn is started with `--proxy-headers --forwarded-allow-ips '*'` — it trusts only
  127.0.0.1 otherwise, and nginx is another container. Missing either half, the first real
  sign-in sent `http://bindery.d3cloud.io/api/auth/oidc/callback` to a provider holding the https
  form and was refused with `invalid_redirect_uri`. A redirect URI is compared character for
  character by design: that comparison is what stops an authorization code being delivered
  somewhere else.
- **Settings is where it is configured**, in `D3AuthProvider.tsx` — issuer, client id, secret and
  mode, administrator-only at both ends. The phase shipped without this card and was reachable
  only by editing a compose file over SSH, which is the answer that screen exists to replace.
  The server refuses `optional` or `required` while any of the three is missing, judged on what
  the archive will hold *after* the write, since the form sends the provider and the mode
  together.
- `infra/bindery.d3auth.json` is the manifest to register; copy the values from the provider's
  **connection sheet**, never from documentation.

## The front door and first-run setup (Phase 19)

`api/first_run.py`, `api/routers/setup.py`, `web/src/features/entry/`.

- **A fresh install is claimed in the browser with a setup code.** It is
  printed at api start while `app_user` is empty (`[bindery setup]` lines) and
  by `python -m api.cli setup-code`. It goes to **stdout, never `logging`**:
  `eventlog` persists every log record to a table the diagnostics screen reads.
  At rest it is an HMAC keyed on `JWT_SECRET`, expires in a day, and is cleared
  at claim. The tunnel makes a fresh instance internet-facing; without the code,
  whoever found the URL first would own the archive.
- **Claim → Secure → recovery codes → administrator.** Admin is granted by
  `accounts.grant_admin`, which refuses without TOTP (REQ-156). Setup goes
  through that rule, never around it. `setup_owner_user_id` makes an abandoned
  setup resume at Secure on the next sign-in.
- **`create-user` makes an ordinary account unless given `--owner`.** Implying
  owner from an empty archive sent CI's e2e account to the setup screen.
- **Codes are canonicalised before hashing.** `CodeInput` never sends dashes,
  and people type spaces; `canonical_recovery_code` and `canonical_reset_code`
  restore the issued form. Do not compare codes as typed.
- **A recovery code retires the authenticator it stood in for** (REQ-210). Spending one *is*
  the account saying the authenticator is gone, so the sign-in that accepted it clears the TOTP
  secret, supersedes the remaining codes — same sheet of paper, same moment — and revokes admin
  (REQ-156), and the next sign-in lands on Secure and hands the rights back. Without it the next
  sign-in asks again for the code they have already shown they cannot produce, and ten codes
  later the only way in is a shell on the host. `check_second_factor` returns *which* factor
  answered, because a caller cannot act on a lost authenticator it was never told about.
  A setup owner is recorded only when no administrator is left; with another admin holding the
  box the way back is enrolment from Settings and that admin re-granting.
- **Kit components carry no margin.** Space entry layouts from the parent grid.

## Media, metadata and the inbox (Phase 18)

- **Vault objects are chunked** (ADR-013): 1 MiB AES-GCM chunks, each bound by
  associated data to the document, its position and the file's shape. A range
  request decrypts only the chunks it covers. v1 objects stay readable and are
  re-sealed on the next unlock — with the seal's ordering rule, nothing retired
  before its replacement has been read back and hashed. **The 423 for a locked
  vault comes before any chunk is touched**; a range is never a way to read a
  byte of a locked vault.
- **`media_metadata` is what the file said about itself.** EXIF via Pillow,
  video via ffprobe. A capture date fills `document_date` as source `file` when
  nothing else has. A vaulted picture's metadata moves into `sealed_meta` with
  the title, because "where and when this was taken" is the one thing about a
  vaulted photograph that would otherwise stay readable.
- **Videos never enter OCR or classification.** `normalize` probes, writes a
  poster, creates one filed document, and cascades nothing. Formats a browser
  plays stream via `/files/{id}/original` (FileResponse honours Range); the
  rest are download-only and the screen says so.
- **A vault-bound import is sealed by the api, not the worker** — the key never
  leaves the api process. `api/vault/sweep.py` runs every 15 s and seals any
  document from such an import whose pipeline has finished, while the owner's
  vault is open. Refused up front if the vault is shut at creation. "Finished
  and waiting" is a named state, because it looks like a bug otherwise.
- **The unlock session lives in the api *process*.** `docker exec bindery-api
  python -c "sessions.is_unlocked(...)"` spawns a **new** process that shares no
  memory with uvicorn, so it always answers `False`. It is not a way to check
  whether a vault is open, and reading it as one turns a working vault into an
  apparent bug. Ask the running app — `GET /api/vault` — or look at what the
  sweep actually did.
- **The watched folder reads `/data/inbox/<library-slug>/` only.** A file at the
  inbox root is found by Import's inbox preset and ignored by the watcher. By
  design — the folder is how a file knows its library.

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

## Public repository — the corpus is not in it

Bindery is public under Apache-2.0 (ADR-014). The archive it serves is private;
the code that serves it is not.

`tests/corpus/` is the golden corpus of **real** personal records — a DD-214, VA
medical records, financial statements. Those documents are **not in this
repository and must never be committed**: `.gitignore` ignores everything in that
directory except the Python harness and its README, and the corpus lives on the
maintainer's machine.

This is a live hazard rather than a historical note, because **T-1.12 is still
open and its instruction is "add real fixtures to `tests/corpus/`"**. Add them to
the working copy, run `make ocr-report`, and let them stay untracked. If you ever
find yourself writing `git add -f` under that path, stop.

## Planning Corpus

Foreman `BND` — its documents, ADRs, risks and glossary · Glossary · UX Flows & Screen Inventory · Risk Register · Test Strategy · Requirements Register (157 REQs) · Scope of Work (phases 0–18, plus 3.5 and 8.5) · Phase Plans ×14 · ADR-001 … ADR-013

Start a coding session with `/start-development bindery`.
