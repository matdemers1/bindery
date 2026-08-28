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
9. **No published host ports.** Ingress is Cloudflare Tunnel only.
10. **Migrations are applied explicitly**, never on container boot.

## Stack

Python 3.13 · FastAPI · SQLAlchemy 2.0 (async) · Alembic · PostgreSQL 16 (`pg_trgm`, `pgvector`) · OCRmyPDF 17 + Tesseract 5 · React 19 + Vite + Tailwind v4 (dark-first) · Docker Compose · Cloudflare Tunnel + Access · Claude Opus 5 behind an `AIProvider` adapter.

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
  `tests/`, `alembic/` and `pyproject.toml`.** Without those mounts the suite
  runs whatever source was baked into the image and reports a pass on code you
  have already changed — which happened once, and cost a stale green run.

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

## Deployment

`docs/zimaos-deploy.md`. CI (`.github/workflows/build.yml`) runs lint and the
default suite, then publishes `ghcr.io/matdemers1/bindery/{api,worker,web}` tagged
`:main`, `:latest` and `:sha-<commit>`. The ZimaOS host pulls `:main`; rolling
back means pinning a `:sha-` tag.

Every Dockerfile names its shipped stage **`runtime`**. The api and worker
Dockerfiles also have a `dev` stage carrying pytest and the whole source tree —
CI must keep passing `target: runtime`, or the deployed images ship the test
harness.

`infra/zimaos/bindery.zimaos.yaml` is the CasaOS custom-app manifest. It must
never gain a `ports:` key: ingress is the Cloudflare Tunnel only (REQ-104).

## ⚠️ Private repository

`tests/corpus/` holds real personal documents (DD-214, VA medical records, financial statements) as the golden corpus. **This repository must never be made public.**

## Planning Corpus

`D3 Cloud Vault/Bindery/` — Discovery Roadmap · Discovery & Requirements · Research Notes · Feature Ideas · Architecture · Data Model · Glossary · UX Flows & Screen Inventory · Risk Register · Test Strategy · Requirements Register (131 REQs) · Scope of Work (10 phases) · Phase Plans ×10 · ADR-001 … ADR-006

Start a coding session with `/start-development bindery`.
