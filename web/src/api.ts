// Thin fetch wrapper. Auth rides on HTTP-only cookies, so nothing here ever
// touches a token — there is no token for JavaScript to leak.
//
// Sectioned by domain, in the order below. It used to be sectioned by the
// build phase that added each part, which only helped someone who had lived
// through the phases: "which phase added photographs?" is not a question a
// stranger can answer, and it was the question this file made them answer
// before they could find anything. The scheme had also stopped being
// maintained — the vault sat between phase 6 and phase 7, and accounts, added
// long before either, sat last — and an index nobody maintains is worse than
// none, because readers still trust it.
//
//   session · libraries and files · search · documents · segments · pages
//   provenance · review · rules · archive · settings · imports · bulk edits
//   entities · pipeline · trust · the private vault · household · ask
//   health · api tokens · diagnostics · photographs · unify · upload
//   corrections · media metadata · direct file urls · accounts · version
//
// Each `api` method sits under its domain's banner, and each domain's types
// under a banner of the same name further down.

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    /** Seconds, from the `Retry-After` header on a 429. */
    readonly retryAfter?: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api${path}`, { credentials: "same-origin", ...init });
  if (response.status === 204) return undefined as T;

  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const retryAfter = Number(response.headers.get("retry-after"));
    throw new ApiError(
      response.status,
      body?.detail ?? response.statusText,
      Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter : undefined,
    );
  }
  return body as T;
}

// --- Session and libraries -------------------------------------------------

export interface User {
  id: string;
  email: string;
  display_name: string | null;
  is_admin: boolean;
}

export interface Library {
  id: string;
  name: string;
  kind: string;
}

// --- Files and uploads -----------------------------------------------------

export interface SourceFile {
  id: string;
  library_id: string;
  sha256: string;
  byte_size: number;
  mime_type: string | null;
  original_filename: string | null;
  ingest_source: string;
  page_count: number | null;
  state: string;
  received_at: string;
}

export interface UploadResult {
  source_file: SourceFile;
  duplicate: boolean;
}

// --- Search ----------------------------------------------------------------

export interface FileMatches {
  query: string;
  /** Every page of the file that matches, in page order (D-03). */
  pages: number[];
}

export interface PageHit {
  /** Absolute position in the source file. */
  page_number: number;
  /** Position within this document — page numbers are always disambiguated. */
  document_page_number: number;
  /** Contains <mark> tags from ts_headline. Rendered through a sanitizer. */
  snippet: string;
  rank: number;
}

export interface SearchResult {
  document_id: string;
  source_file_id: string;
  library_id: string;
  title: string | null;
  original_filename: string | null;
  page_start: number;
  page_end: number;
  file_page_count: number | null;
  state: string;
  received_at: string;
  known_form_code: string | null;
  known_form_name: string | null;
  best_page: PageHit;
  matching_pages: number;
}

export interface Facet {
  value: string;
  label: string;
  count: number;
}

export interface SearchResponse {
  query: string;
  total: number;
  results: SearchResult[];
  facets: Record<string, Facet[]>;
  suggestions: string[];
}

export interface SearchParams {
  q: string;
  libraryIds?: string[];
  knownFormCodes?: string[];
  sourceFileId?: string;
  limit?: number;
  /** The type/correspondent/year breakdown. Omit it when you discard it —
   *  it is a second aggregation over the same matched pages. */
  facets?: boolean;
}

// --- Documents -------------------------------------------------------------

export interface Document {
  id: string;
  library_id: string;
  source_file_id: string;
  page_start: number;
  page_end: number;
  title: string | null;
  summary: string | null;
  document_date: string | null;
  sensitivity: string;
  redundancy: string;
  review_state: string;
  is_backlog: boolean;
  known_form_id: string | null;
  correspondent_id: string | null;
  document_type_id: string | null;
  created_at: string;
}

export interface KnownForm {
  id: string;
  code: string;
  name: string;
  description: string | null;
  enabled: boolean;
}

export interface DocumentDetail {
  document: Document;
  source_file: SourceFile;
  known_form: KnownForm | null;
  pages: PageSummary[];
  /** Empty for a document nobody has corrected and no classifier has claimed. */
  field_sources: FieldSourceRef[];
  tags: TagRef[];
  /** Present for photographs and videos; null for a scan of a form. */
  media: MediaMetadata | null;
}

// --- Segments: dividing a bundle into the documents inside it --------------

export interface SegmentList {
  source_file_id: string;
  page_count: number;
  segments: Document[];
}

export interface SegmentInput {
  page_start: number;
  page_end: number;
  title: string | null;
}

// --- Pages, and the geometry of the words on them --------------------------

export interface PageSummary {
  page_number: number;
  render_path: string | null;
  thumb_path: string | null;
}

export interface SourceFileDetail {
  source_file: SourceFile;
  pages: PageSummary[];
}

export interface Word {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
  t: string;
}

export interface PageBoxes {
  number: number;
  width: number;
  height: number;
  lines: { words: Word[] }[];
}

// --- Provenance: what was decided about a document, and by what ------------

export interface TagRef {
  id: string;
  name: string;
  /** ai | rule | human — what makes an AI value visually distinct. */
  source: "ai" | "rule" | "human";
}

export interface ClassificationRecord {
  id: string;
  model: string;
  prompt_version: string;
  /** The model's own numbers. Shown, never decisive. */
  confidence: Record<string, number>;
  /** The facts the gate actually read. */
  structural_signals: Record<string, unknown>;
  gate_decision: string | null;
  gate_reasons: string[];
  usage: Record<string, number>;
  created_at: string;
}

export interface FieldProvenanceRecord {
  field_name: string;
  /** Absolute page in the source file. */
  page_number: number | null;
  snippet: string | null;
  confidence: number | null;
}

export interface Extraction {
  characters: number;
  pages: number;
  empty_pages: number;
}

export interface OcrPageText {
  page_number: number;
  text: string;
  characters: number;
}

export interface OcrText {
  source_file_id: string;
  original_filename: string | null;
  pages: OcrPageText[];
  characters: number;
  empty_pages: number;
}

export interface WhyPanel {
  document: Document;
  classification: ClassificationRecord | null;
  provenance: FieldProvenanceRecord[];
  tags: TagRef[];
  extraction: Extraction;
  source_file_id: string;
  /** Who decided each field — the half provenance cannot express. */
  field_sources: FieldSourceRef[];
}

// --- The review queue ------------------------------------------------------

export interface ReviewQueue {
  total: number;
  documents: Document[];
}

// --- Filing rules ----------------------------------------------------------

export interface RuleRecord {
  id: string;
  library_id: string;
  name: string;
  enabled: boolean;
  priority: number;
  conditions: { all: { field: string; operator: string; value: string }[] };
  actions: Record<string, unknown>;
  created_at: string;
}

export interface RuleDryRun {
  rule_id: string;
  examined: number;
  matched: number;
  truncated: boolean;
  matches: { document_id: string; title: string | null; changes: Record<string, unknown> }[];
}

// --- The archive browser ---------------------------------------------------

export interface ArchiveEntry {
  document_id: string;
  source_file_id: string;
  title: string | null;
  original_filename: string | null;
  page_start: number;
  page_end: number;
  file_page_count: number | null;
  document_date: string | null;
  received_at: string;
  /** How it got in: web_upload | watched_folder | camera | bulk_import */
  ingest_source: string;
  correspondent: string | null;
  document_type: string | null;
  known_form: string | null;
  review_state: string;
  sensitivity: string;
  is_backlog: boolean;
  sha256: string;
  tags: TagRef[];
}

export interface ArchiveStats {
  documents: number;
  files: number;
  pages: number;
  needs_review: number;
  backlog_pending: number;
  unclassified: number;
}

export interface Archive {
  total: number;
  entries: ArchiveEntry[];
  stats: ArchiveStats;
}

export interface Tree {
  group_by: string;
  groups: { label: string; count: number }[];
}

// --- Settings, and the offsite copy they configure -------------------------

export interface ModelChoice {
  id: string;
  name: string;
  blurb: string;
  input_per_mtok: number;
  output_per_mtok: number;
}

export interface Settings {
  available_models: ModelChoice[];
  anthropic_key_configured: boolean;
  anthropic_key_hint: string | null;
  model: string;
  prompt_version: string;
  notify_webhook_configured: boolean;
  notify_webhook_hint: string | null;
  /**
   * Offsite replication (ADR-010). Only the secret is withheld — the key id,
   * bucket, region and KMS key are returned in full, because each is something
   * you need to read back during a restore or a credential rotation.
   */
  aws_access_key_id: string | null;
  aws_secret_configured: boolean;
  aws_secret_hint: string | null;
  offsite_bucket: string | null;
  offsite_region: string | null;
  offsite_kms_key_id: string | null;
  oidc_issuer: string | null;
  oidc_client_id: string | null;
  oidc_secret_configured: boolean;
  oidc_secret_hint: string | null;
  sso_mode: string;
}

export interface OffsiteRun {
  id: string;
  kind: string;
  state: string;
  trigger: string;
  started_at: string;
  finished_at: string | null;
  detail: string | null;
  dump_key: string | null;
  dump_bytes: number;
  blobs_uploaded: number;
  blobs_skipped: number;
  bytes_sent: number;
  failures: string[] | null;
}

export interface OffsiteStatus {
  configured: boolean;
  /** Null when nothing has ever succeeded — which reads as stale, not as new. */
  last_success_at: string | null;
  last_success_age_seconds: number | null;
  stale: boolean;
  in_flight: string | null;
  last_daily_at: string | null;
  last_weekly_at: string | null;
  runs: OffsiteRun[];
}

export interface OffsiteTest {
  ok: boolean;
  detail: string;
  encryption: string | null;
  kms_key_arn: string | null;
  bucket_key_enabled: boolean | null;
  /** What was proven, in order, so a partial failure says how far it got. */
  checks: string[];
}

export interface SettingsTest {
  ok: boolean;
  detail: string;
  model: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
}

// --- Backlog imports -------------------------------------------------------

export interface ImportSession {
  id: string;
  library_id: string;
  root_path: string;
  state: string;
  pass_number: number;
  sample_size: number;
  dry_run: {
    total_files?: number;
    total_bytes?: number;
    by_extension?: Record<string, number>;
    already_in_archive?: number;
    duplicates_within_batch?: number;
    new_files?: number;
    estimated_pages?: number;
    skipped_unsupported?: number;
    skipped_hidden?: number;
    errors?: { path: string; error: string }[];
  };
  cost_estimate: {
    estimated_pages?: number;
    interactive_usd?: number;
    batch_usd?: number;
    exceeds_alarm?: boolean;
    alarm_threshold_usd?: number;
  };
  progress: Record<string, number>;
  last_error: string | null;
  created_at: string;
  /** Bound for the vault: sealed as each file's pipeline finishes, while unlocked. */
  to_vault: boolean;
  vaulted: number;
  /** Finished, and waiting for the vault to be open. "Unlock to continue", not "done". */
  awaiting_vault: number;
  vault_unlocked: boolean;
}

export interface ImportItem {
  path: string;
  state: string;
  byte_size: number | null;
  sha256: string | null;
  source_file_id: string | null;
  error: string | null;
}

// --- Bulk edits ------------------------------------------------------------

export interface BulkResult {
  matched: number;
  operation_id: string | null;
  changes: { document_id: string; title: string | null; changes: Record<string, unknown> }[];
}

// --- Entities: correspondents, types, tags and assets ----------------------

export interface CorrespondentRef {
  id: string;
  name: string;
  kind: string | null;
  aliases: string[];
  document_count: number;
}

export interface AssetRef {
  id: string;
  library_id: string;
  kind: string;
  name: string;
  attributes: Record<string, string>;
  document_count: number;
}

export interface TimelineEntry {
  document_id: string;
  title: string | null;
  date: string;
  dated_precisely: boolean;
  correspondent: string | null;
  source_file_id: string;
  page_start: number;
}

export interface MergePreview {
  from_name: string;
  into_name: string;
  document_count: number;
  alias_count: number;
  documents: { id: string; title: string | null }[];
  operation_id: string | null;
}

export interface TaxonomyHealth {
  total_tags: number;
  used_once: number;
  unused: number;
  orphan_ratio: number;
  exceeds_alarm: boolean;
  near_duplicate_tags: { a_id: string; a_name: string; b_id: string; b_name: string; similarity: number }[];
  near_duplicate_correspondents: { a_id: string; a_name: string; b_id: string; b_name: string; similarity: number }[];
}

export interface DuplicatePair {
  id: string;
  document_a_id: string;
  document_b_id: string;
  a_title: string | null;
  b_title: string | null;
  similarity: number;
}

// --- The pipeline ----------------------------------------------------------

export interface PendingReason {
  code: "never_attempted" | "provider_unavailable" | "failed" | "declined";
  label: string;
  detail: string;
  count: number;
  /**
   * Whether re-running AI review on this bucket could change anything. False for a
   * refusal — the model has already looked and said no (ADR-011) — and the screen must
   * not offer an action whose only outcome is the same answer.
   */
  rerunnable: boolean;
  document_ids: string[];
}

export interface PendingReview {
  total: number;
  reasons: PendingReason[];
}

export interface Job {
  id: string;
  source_file_id: string | null;
  document_id: string | null;
  stage: string;
  state: string;
  attempts: number;
  last_error: string | null;
  scheduled_for: string;
  updated_at: string;
  /** Set when someone has seen a dead letter. Never deleted, retried or hidden. */
  acknowledged_at?: string | null;
}

export interface PipelineStatus {
  counts: { stage: string; state: string; count: number }[];
  attention: Job[];
  /** Correctly refused inputs — listed, never alarmed on. */
  declined: Job[];
  in_flight: Job[];
}

function searchQueryString({
  q,
  libraryIds,
  knownFormCodes,
  sourceFileId,
  limit,
  facets,
}: SearchParams) {
  const params = new URLSearchParams({ q });
  libraryIds?.forEach((id) => params.append("library_id", id));
  knownFormCodes?.forEach((code) => params.append("known_form", code));
  if (sourceFileId) params.set("source_file_id", sourceFileId);
  if (limit) params.set("limit", String(limit));
  if (facets === false) params.set("facets", "false");
  return params.toString();
}

export const api = {
  // --- Session -------------------------------------------------------------

  me: () => request<User>("/auth/me"),
  login: (email: string, password: string, code?: string) =>
    request<User>("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password, code }),
    }),
  logout: () => request<void>("/auth/logout", { method: "POST" }),

  // --- Libraries and the files in them -------------------------------------

  libraries: () => request<Library[]>("/libraries"),
  sourceFiles: () => request<SourceFile[]>("/source-files"),

  // --- Search --------------------------------------------------------------

  search: (params: SearchParams, signal?: AbortSignal) =>
    request<SearchResponse>(`/search?${searchQueryString(params)}`, { signal }),

  // --- Documents, pages and segments ---------------------------------------

  file: (id: string) => request<SourceFileDetail>(`/files/${id}`),
  document: (id: string) => request<DocumentDetail>(`/documents/${id}`),

  segments: (fileId: string) => request<SegmentList>(`/files/${fileId}/segments`),
  replaceSegments: (fileId: string, segments: SegmentInput[]) =>
    request<SegmentList>(`/files/${fileId}/segments`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ segments }),
    }),
  undoSegments: (fileId: string) =>
    request<SegmentList>(`/files/${fileId}/segments/undo`, { method: "POST" }),
  /** Which pages of one file match a query — the set behind “find next”. */
  fileMatches: (id: string, q: string) =>
    request<FileMatches>(`/files/${id}/matches?q=${encodeURIComponent(q)}`),
  pageBoxes: (id: string, page: number, signal?: AbortSignal) =>
    request<PageBoxes>(`/files/${id}/pages/${page}/boxes`, { signal }),

  // --- The pipeline --------------------------------------------------------

  pipeline: () => request<PipelineStatus>("/pipeline"),

  // --- The archive browser -------------------------------------------------

  archive: (params: Record<string, string | string[]> = {}) => {
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (Array.isArray(value)) value.forEach((v) => search.append(key, v));
      else if (value) search.set(key, value);
    }
    return request<Archive>(`/archive?${search}`);
  },
  tree: (groupBy: string) => request<Tree>(`/archive/tree?group_by=${groupBy}`),

  // --- Settings ------------------------------------------------------------

  settings: () => request<Settings>("/settings"),
  updateSettings: (
    body: Partial<
      Record<
        | "anthropic_api_key"
        | "model"
        | "prompt_version"
        | "notify_webhook_url"
        | "aws_access_key_id"
        | "aws_secret_access_key"
        | "offsite_bucket"
        | "offsite_region"
        | "offsite_kms_key_id"
        | "oidc_issuer"
        | "oidc_client_id"
        | "oidc_client_secret"
        | "sso_mode",
        string
      >
    >,
  ) =>
    request<Settings>("/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  testAi: () => request<SettingsTest>("/settings/test-ai", { method: "POST" }),
  testOffsite: () => request<OffsiteTest>("/settings/test-offsite", { method: "POST" }),

  // --- Backlog imports -----------------------------------------------------

  imports: () => request<ImportSession[]>("/imports"),
  importPresets: () => request<ImportPresets>("/imports/presets"),
  importLog: (id: string) => request<ImportLogLine[]>(`/imports/${id}/log`),
  startImport: (body: { library_id: string; root_path: string; sample_size?: number; to_vault?: boolean }) =>
    request<ImportSession>("/imports", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  importSession: (id: string) => request<ImportSession>(`/imports/${id}`),
  importItems: (id: string, state?: string) =>
    request<ImportItem[]>(`/imports/${id}/items${state ? `?state=${state}` : ""}`),
  sampleImport: (id: string) =>
    request<ImportSession>(`/imports/${id}/sample`, { method: "POST" }),
  setImportVault: (id: string, to_vault: boolean) =>
    request<ImportSession>(`/imports/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ to_vault }),
    }),
  runImport: (id: string, batch = 50) =>
    request<ImportSession>(`/imports/${id}/run?batch=${batch}`, { method: "POST" }),
  pauseImport: (id: string) =>
    request<ImportSession>(`/imports/${id}/pause`, { method: "POST" }),
  curateImport: (id: string) =>
    request<ImportSession>(`/imports/${id}/curate`, { method: "POST" }),

  // --- Bulk edits ----------------------------------------------------------

  bulkPreview: (documentIds: string[], actions: Record<string, unknown>) =>
    request<BulkResult>("/bulk/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ document_ids: documentIds, actions }),
    }),
  bulkApply: (documentIds: string[], actions: Record<string, unknown>) =>
    request<BulkResult>("/bulk/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ document_ids: documentIds, actions }),
    }),
  bulkUndo: (operationId: string) =>
    request<BulkResult>(`/bulk/${operationId}/undo`, { method: "POST" }),

  // --- Entities: correspondents, types, tags and assets --------------------

  correspondents: () => request<CorrespondentRef[]>("/correspondents"),
  tagOptions: () => request<TaxonomyOption[]>("/tags"),
  documentTypeOptions: () => request<TaxonomyOption[]>("/document-types"),
  editDocument: (id: string, changes: DocumentEdit) =>
    request<DocumentEditResult>(`/documents/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(changes),
    }),
  addAlias: (id: string, alias: string) =>
    request<CorrespondentRef>(
      `/correspondents/${id}/aliases?alias=${encodeURIComponent(alias)}`,
      { method: "POST" },
    ),
  previewMerge: (sourceId: string, targetId: string) =>
    request<MergePreview>("/correspondents/merge/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_id: sourceId, target_id: targetId }),
    }),
  mergeCorrespondents: (sourceId: string, targetId: string) =>
    request<MergePreview>("/correspondents/merge", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_id: sourceId, target_id: targetId }),
    }),
  mergeTags: (sourceId: string, targetId: string) =>
    request<MergePreview>("/tags/merge", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_id: sourceId, target_id: targetId }),
    }),
  undoMerge: (operationId: string) =>
    request<{ restored: number }>(`/merges/${operationId}/undo`, { method: "POST" }),

  assets: () => request<AssetRef[]>("/assets"),
  createAsset: (body: { library_id: string; kind: string; name: string; attributes: Record<string, string> }) =>
    request<AssetRef>("/assets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  attachAsset: (assetId: string, documentId: string) =>
    request<{ attached: boolean }>(`/assets/${assetId}/documents/${documentId}`, {
      method: "POST",
    }),
  assetTimeline: (assetId: string) =>
    request<{ asset: AssetRef; entries: TimelineEntry[] }>(`/assets/${assetId}/timeline`),

  taxonomyHealth: () => request<TaxonomyHealth>("/taxonomy/health"),
  duplicates: () => request<DuplicatePair[]>("/duplicates"),
  scanDuplicates: () => request<{ found: number }>("/duplicates/scan", { method: "POST" }),
  similar: (documentId: string) =>
    request<{ results: { document_id: string; title: string | null; similarity: number }[] }>(
      `/documents/${documentId}/similar`,
    ),

  shelves: () => request<{ id: string; name: string; query: Record<string, unknown>; is_packet: boolean }[]>("/shelves"),

  // --- The review queue ----------------------------------------------------

  review: () => request<ReviewQueue>("/review"),
  why: (documentId: string) => request<WhyPanel>(`/documents/${documentId}/why`),

  /** Exactly what OCR read, page by page, verbatim. */
  ocrText: (sourceFileId: string) => request<OcrText>(`/files/${sourceFileId}/text`),
  /** Read the file again from the original. Everything rebuilt is derived. */
  rescan: (sourceFileId: string) =>
    request<{ source_file_id: string; queued: boolean; detail: string }>(
      `/source-files/${sourceFileId}/rescan`,
      { method: "POST" },
    ),
  undo: (documentId: string) =>
    request<Document>(`/documents/${documentId}/undo`, { method: "POST" }),
  accept: (documentId: string) =>
    request<Document>(`/documents/${documentId}/accept`, { method: "POST" }),

  // --- Filing rules --------------------------------------------------------

  rules: () => request<RuleRecord[]>("/rules"),
  createRule: (body: {
    library_id: string;
    name: string;
    conditions: unknown;
    actions: unknown;
  }) =>
    request<RuleRecord>("/rules", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  dryRunRule: (ruleId: string) =>
    request<RuleDryRun>(`/rules/${ruleId}/dry-run`, { method: "POST" }),
  setRuleEnabled: (ruleId: string, enabled: boolean) =>
    request<RuleRecord>(`/rules/${ruleId}/${enabled ? "enable" : "disable"}`, {
      method: "POST",
    }),
  // --- Jobs ----------------------------------------------------------------

  acknowledgeJob: (jobId: string, undo = false) =>
    request<Job>(`/pipeline/jobs/${jobId}/acknowledge${undo ? "?undo=true" : ""}`, {
      method: "POST",
    }),
  retryJob: (id: string) => request<Job>(`/pipeline/jobs/${id}/retry`, { method: "POST" }),

  /** What is waiting for AI review, and why it is waiting. */
  pendingReview: () => request<PendingReview>("/pipeline/reclassify/pending"),
  reclassify: (body: { document_ids?: string[]; all_pending?: boolean; reasons?: string[] }) =>
    request<{ queued: number; requested: number }>("/pipeline/reclassify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),


  // --- Trust: the folder tree, export, integrity, mirror and backup --------

  /** One level of the same tree the mirror and the export write to disk. */
  fileTree: (path = "") =>
    request<FileTree>(`/tree${path ? `?path=${encodeURIComponent(path)}` : ""}`),

  /** Pinned on the home screen: the day you need these is not a day to search. */
  vital: () => request<Document[]>("/vital"),

  exportFull: (name = "bindery-export") =>
    request<ExportResult>("/export/full", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }),
  /** The passphrase is sent once and never stored, here or on the server. */
  exportGoBag: (passphrase: string) =>
    request<ExportResult>("/export/go-bag", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ passphrase }),
    }),

  integrityCheck: () => request<IntegrityReport>("/integrity/check", { method: "POST" }),
  offsiteStatus: () => request<OffsiteStatus>("/offsite"),
  offsiteReplicate: (kind: "daily" | "weekly" = "daily") =>
    request<OffsiteStatus>(`/offsite/replicate?kind=${kind}`, { method: "POST" }),
  rebuildMirror: () => request<MirrorResult>("/mirror/rebuild", { method: "POST" }),
  runBackup: (force = false) =>
    request<BackupResult>(`/backup/run${force ? "?force=true" : ""}`, { method: "POST" }),

  audit: (filters: AuditFilters = {}) => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value !== undefined && value !== "") query.set(key, String(value));
    }
    return request<AuditPage>(`/audit${query.size ? `?${query}` : ""}`);
  },

  // --- The private vault ---------------------------------------------------

  vault: () => request<VaultState>("/vault"),
  vaultSetup: (passphrase: string, pin: string) =>
    request<VaultState>("/vault/setup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ passphrase, pin }),
    }),
  vaultUnlock: (secret: { pin?: string; passphrase?: string }) =>
    request<VaultState>("/vault/unlock", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(secret),
    }),
  vaultLock: () => request<VaultState>("/vault/lock", { method: "POST" }),
  vaultSetPin: (pin: string) =>
    request<VaultState>("/vault/pin", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pin }),
    }),
  vaultItems: () => request<VaultItem[]>("/vault/items"),
  vaultMoveIn: (documentId: string) =>
    request<VaultItem>(`/vault/items/${documentId}`, { method: "POST" }),
  vaultMoveOut: (documentId: string) =>
    request<VaultItem>(`/vault/items/${documentId}`, { method: "DELETE" }),
  vaultSearch: (q: string, signal?: AbortSignal) =>
    request<VaultSearchResults>(`/vault/search?q=${encodeURIComponent(q)}`, { signal }),

  // --- Household: members, libraries and moving files between them ---------

  householdLibraries: () => request<LibraryDetail[]>("/household/libraries"),
  createLibrary: (name: string, kind: string) =>
    request<LibraryDetail>("/household/libraries", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, kind }),
    }),
  setMemberRole: (libraryId: string, email: string, role: string) =>
    request<Member>(`/household/libraries/${libraryId}/members`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, role }),
    }),

  /** Preview first: a move makes documents vanish from one person's view. */
  previewMove: (sourceFileId: string, toLibraryId: string) =>
    request<MovePlan>(`/source-files/${sourceFileId}/move/preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ to_library_id: toLibraryId }),
    }),
  moveFile: (sourceFileId: string, toLibraryId: string) =>
    request<MovePlan>(`/source-files/${sourceFileId}/move`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ to_library_id: toLibraryId }),
    }),


  // --- Ask -----------------------------------------------------------------

  ask: (question: string) =>
    request<AskAnswer>("/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    }),

  // --- Health --------------------------------------------------------------

  healthPanel: () => request<HealthPanel>("/health/panel"),

  /**
   * The sidebar's two numbers, and only those.
   *
   * The badges follow the `jobs` topic, which moves on every stage of every
   * file, so this is the most frequently requested read in the application. It
   * used to be answered by `healthPanel` and `review` together — roughly twenty
   * queries and two and a half kilobytes — of which a count and a boolean were
   * kept. The panel is still what the Trust screen asks for.
   */
  healthBadge: () => request<HealthBadge>("/health/badge"),

  // --- API tokens ----------------------------------------------------------

  apiTokens: () => request<ApiTokenRecord[]>("/tokens"),
  createApiToken: (body: {
    name: string;
    scopes: string[];
    library_ids?: string[];
    expires_in_days?: number | null;
  }) =>
    request<IssuedApiToken>("/tokens", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  revokeApiToken: (id: string) =>
    request<ApiTokenRecord>(`/tokens/${id}/revoke`, { method: "POST" }),


  // --- Diagnostics: the log and per-file progress --------------------------

  logs: (params: {
    source_file_id?: string;
    level?: string;
    q?: string;
    before_sequence?: number;
    limit?: number;
  } = {}) => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== "") query.set(key, String(value));
    }
    return request<LogPage>(`/logs${query.size ? `?${query}` : ""}`);
  },

  pipelineFiles: (ids?: string[]) => {
    const query = new URLSearchParams();
    (ids ?? []).forEach((id) => query.append("ids", id));
    return request<PipelineFiles>(`/pipeline/files${query.size ? `?${query}` : ""}`);
  },


  // --- Photographs and videos ----------------------------------------------

  photos: (
    params: { q?: string; undescribed?: boolean; limit?: number; offset?: number; kind?: "image" | "video" } = {},
  ) => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== "" && value !== false) query.set(key, String(value));
    }
    return request<PhotoWall>(`/photos${query.size ? `?${query}` : ""}`);
  },

  /** Ask which entries of a kind are the same thing. Changes nothing. */
  // --- Unify: proposing that two entries are the same thing ----------------

  unifyPreview: (kind: UnifyKind = "correspondent") =>
    request<UnifyProposal>(`/taxonomy/unify/preview?kind=${kind}`, { method: "POST" }),
  unifyApply: (kind: UnifyKind, canonicalId: string, memberIds: string[]) =>
    request<MergePreview>(`/taxonomy/unify/apply?kind=${kind}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ canonical_id: canonicalId, member_ids: memberIds }),
    }),

  // --- Upload --------------------------------------------------------------

  upload: (libraryId: string, file: File) => {
    const form = new FormData();
    form.append("library_id", libraryId);
    form.append("file", file);
    return request<UploadResult>("/upload", { method: "POST", body: form });
  },
};


// --- Trust: the folder tree, export, integrity, backup and the audit log ---

export interface FileTreeNode {
  path: string;
  name: string;
  kind: "folder" | "document" | "bundle";
  document_id: string | null;
  source_file_id: string | null;
  title: string | null;
  document_date: string | null;
  correspondent: string | null;
  document_type: string | null;
  tags: string[];
  page_start: number | null;
  page_end: number | null;
  page_count: number | null;
  ingest_source: string | null;
  received_at: string | null;
  original_filename: string | null;
  sensitivity: string | null;
  review_state: string | null;
  byte_size: number | null;
  library_id: string | null;
  library_name: string | null;
  child_count: number;
}

export interface FileTree {
  root: string;
  nodes: FileTreeNode[];
}

export interface ExportResult {
  path: string;
  document_count: number;
  file_count: number;
  byte_size: number;
  encrypted: boolean;
  missing_blobs: string[];
}

export interface IntegrityReport {
  started_at: string;
  finished_at: string | null;
  checked: number;
  bytes_read: number;
  ok: number;
  healthy: boolean;
  missing: { sha256: string; original_filename: string | null }[];
  corrupt: { sha256: string; original_filename: string | null; actual_sha256: string }[];
  orphan_count: number;
  orphans: string[];  /** Encrypted rather than absent — the vault working, not loss. */
  sealed: Record<string, unknown>[];
}

export interface MirrorResult {
  root: string;
  linked: number;
  copied: number;
  missing: number;
  bundles: number;
  removed: number;
}

export interface BackupResult {
  path: string;
  blob_count: number;
  byte_size: number;
  integrity_healthy: boolean;
  manifest: Record<string, unknown>;
}

export interface AuditEventRecord {
  id: string;
  sequence: number;
  entity_type: string;
  entity_id: string;
  action: string;
  actor_type: string;
  actor_label: string | null;
  rule_id: string | null;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  created_at: string;
}

export interface AuditPage {
  events: AuditEventRecord[];
  next_before_sequence: number | null;
}

export interface AuditFilters {
  entity_id?: string;
  entity_type?: string;
  actor_type?: string;
  action?: string;
  since?: string;
  until?: string;
  before_sequence?: number;
  limit?: number;
}


// --- Household: members, libraries and moving files between them -----------

export interface Member {
  user_id: string;
  email: string;
  display_name: string | null;
  role: "owner" | "contributor" | "reader";
}

export interface LibraryDetail {
  id: string;
  name: string;
  kind: string;
  your_role: "owner" | "contributor" | "reader";
  members: Member[];
}

export interface MovePlan {
  source_file_id: string;
  from_library_id: string;
  to_library_id: string;
  document_count: number;
  documents: { id: string; title: string | null; page_start: number; page_end: number }[];
  cleared_correspondents: string[];
  cleared_types: string[];
  cleared_tags: string[];
  loses_metadata: boolean;
}


// --- Ask, health and API tokens --------------------------------------------

export interface AskCitation {
  document_id: string;
  source_file_id: string;
  title: string;
  page_number: number;
  quote: string;
}

export interface AskAnswer {
  question: string;
  /** Null whenever there is nothing honest to say — including an uncited answer. */
  answer: string | null;
  citations: AskCitation[];
  consulted: { document_id: string; source_file_id: string; title: string; page_number: number }[];
  unavailable_reason: string | null;
  model: string | null;
}

export interface HealthAlert {
  severity: "warning" | "critical";
  code: string;
  message: string;
  detail: Record<string, unknown>;
}

export interface HealthBadge {
  review_total: number;
  healthy: boolean;
}

export interface HealthPanel {
  checked_at: string;
  healthy: boolean;
  queue_depth: Record<string, number>;
  running: number;
  failed_24h: number;
  dead_letter: number;
  stuck_jobs: { id: string; stage: string; locked_by: string | null; locked_for_seconds: number | null }[];
  oldest_queued_seconds: number | null;
  stalled: boolean;
  files_by_state: Record<string, number>;
  spend_30d_usd: number;
  spend_by_day: { day: string; usd: number }[];
  alerts: HealthAlert[];
}

export interface ApiTokenRecord {
  id: string;
  name: string;
  prefix: string;
  scopes: string[];
  library_ids: string[];
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
  created_at: string;
}

export interface IssuedApiToken extends ApiTokenRecord {
  secret: string;
}


// --- Diagnostics: the log and per-file progress ----------------------------

export interface LogEntry {
  id: string;
  sequence: number;
  level: "debug" | "info" | "warning" | "error" | "critical";
  logger: string;
  message: string;
  detail: string | null;
  source_file_id: string | null;
  document_id: string | null;
  job_id: string | null;
  stage: string | null;
  context: Record<string, string>;
  created_at: string;
}

export interface LogPage {
  entries: LogEntry[];
  next_before_sequence: number | null;
  pending_writes: number;
}

export interface FileProgress {
  source_file_id: string;
  original_filename: string | null;
  byte_size: number;
  page_count: number | null;
  state: string;
  received_at: string;
  ingest_source: string;
  document_count: number;
  active_stage: string | null;
  failed_stage: string | null;
  last_error: string | null;
  attempts: number;
  dead_lettered: boolean;
}

export interface PipelineFiles {
  files: FileProgress[];
  stages: string[];
}


// --- Photographs and videos ------------------------------------------------

export interface Photo {
  document_id: string;
  source_file_id: string;
  page: number;
  title: string | null;
  summary: string | null;
  original_filename: string | null;
  received_at: string;
  document_date: string | null;
  text_chars: number;
  described: boolean;
  kind: "image" | "video";
  media: MediaMetadata | null;
}

export interface PhotoWall {
  total: number;
  photos: Photo[];
}

// --- Unify: proposing that two entries are the same thing ------------------

export interface UnifyGroup {
  canonical: string;
  canonical_id: string | null;
  reason: string;
  document_count: number;
  members: { id: string; name: string; documents: number }[];
}

export type UnifyKind = "correspondent" | "document_type" | "tag";

export interface UnifyProposal {
  kind: UnifyKind;
  considered: number;
  model: string | null;
  unavailable_reason: string | null;
  groups: UnifyGroup[];
}

/** Blob URLs. Authenticated and library-scoped server-side; no token in the URL. */
// --------------------------------------------------------------------------
// The private vault
// --------------------------------------------------------------------------

/**
 * What a locked vault will say about itself: that it exists, and that it is
 * shut. Never how much is in it — a count is already a statement about the
 * contents.
 */
export interface VaultState {
  exists: boolean;
  unlocked: boolean;
  pin_enabled: boolean;
  pin_failures: number;
}

export interface VaultItem {
  document_id: string;
  title: string | null;
  original_filename: string | null;
  byte_size: number;
  page_count: number;
  vaulted_at: string | null;
  warnings: string[];
  media_type: string | null;
  is_image: boolean;
  is_video: boolean;
  media: MediaMetadata | null;
}

export interface VaultSearchHit {
  document_id: string;
  title: string | null;
  page_number: number;
  snippet: string;
}

export interface VaultSearchResults {
  query: string;
  total: number;
  hits: VaultSearchHit[];
  pages_scanned: number;
  elapsed_ms: number;
  slow: boolean;
}

// --------------------------------------------------------------------------
// Corrections: editing what was filed, and who decided each field
// --------------------------------------------------------------------------

/** Who set one field. What makes an AI value look different from yours. */
export interface FieldSourceRef {
  field_name: string;
  source: "ai" | "rule" | "human" | "file";
  set_by: string | null;
  set_at: string | null;
  event_id: string | null;
}

export interface TaxonomyOption {
  id: string;
  name: string;
  document_count: number;
}

/**
 * A correction. Every key optional, and **omitting one is not the same as
 * sending null**: omit to leave a field alone, send null to clear it. The
 * client mirrors the server here — only keys actually present are sent.
 */
export interface DocumentEdit {
  title?: string | null;
  summary?: string | null;
  document_date?: string | null;
  correspondent_id?: string | null;
  document_type_id?: string | null;
  create_correspondent?: string;
  create_document_type?: string;
  add_tag_ids?: string[];
  remove_tag_ids?: string[];
  create_tags?: string[];
}

export interface DocumentEditResult {
  document: Document;
  changed: string[];
  tags_added: string[];
  tags_removed: string[];
  created: Record<string, string>;
  event_id: string | null;
}

// --- Media metadata --------------------------------------------------------

/** What the file said about itself: EXIF, and what ffprobe read off a video. */
export interface MediaMetadata {
  kind: "image" | "video";
  width: number | null;
  height: number | null;
  duration_seconds: number | null;
  captured_at: string | null;
  camera_make: string | null;
  camera_model: string | null;
  latitude: number | null;
  longitude: number | null;
  codec: string | null;
  frame_rate: number | null;
  browser_playable: boolean;
}

// --- Backlog imports: presets and the run log ------------------------------

export interface ImportPresets {
  inbox: string;
}

export interface ImportLogLine {
  at: string;
  level: string;
  message: string;
  source_file_id: string | null;
  stage: string | null;
}

// --- URLs the browser fetches directly — images, PDFs, downloads -----------

export const fileUrl = {
  render: (id: string, page: number) => `/api/files/${id}/pages/${page}/render`,
  thumb: (id: string, page: number) => `/api/files/${id}/pages/${page}/thumb`,
  pdf: (id: string) => `/api/files/${id}/pdf`,
  /** Just this document's pages, extracted as a standalone PDF. */
  documentPdf: (documentId: string) => `/api/documents/${documentId}/pdf`,
  /**
   * The decrypted original of a vaulted document. Only answers while the vault
   * is open, and the response carries `Cache-Control: no-store` so the browser
   * does not keep a copy on disk after it closes again.
   */
  vaultOriginal: (documentId: string) => `/api/vault/items/${documentId}/original`,
  /** The stored bytes with their own media type, ranges honoured — for playback. */
  original: (fileId: string) => `/api/files/${fileId}/original`,
  /** One frame of a video, for the wall. 404 for anything without one. */
  poster: (fileId: string) => `/api/files/${fileId}/poster`,
};


// --------------------------------------------------------------------------
// Accounts, invitations and administration
// --------------------------------------------------------------------------

export interface Quota {
  used_bytes: number;
  quota_bytes: number | null;
  files: number;
}

export interface Account {
  id: string;
  email: string;
  display_name: string | null;
  is_admin: boolean;
  totp_enabled: boolean;
  storage: Quota;
}

export interface AdminAccount {
  id: string;
  email: string;
  display_name: string | null;
  is_admin: boolean;
  is_active: boolean;
  suspended_at: string | null;
  locked_until: string | null;
  totp_enabled: boolean;
  storage_quota_bytes: number | null;
  used_bytes: number;
  created_at: string;
}

export interface InvitePreview {
  email: string;
  library_name: string;
  expires_at: string;
  note: string | null;
  storage_quota_bytes: number | null;
}

export interface AdminInvitation {
  id: string;
  email: string;
  library_name: string;
  expires_at: string;
  accepted_at: string | null;
  revoked_at: string | null;
  note: string | null;
}

export const accountsApi = {
  me: () => request<Account>("/account"),
  changePassword: (current_password: string, new_password: string) =>
    request<void>("/account/password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current_password, new_password }),
    }),

  totpStatus: () => request<{ enabled: boolean; required: boolean }>("/account/totp"),
  totpStart: () =>
    request<{ secret: string; uri: string; qr_svg: string }>("/account/totp/start", { method: "POST" }),
  totpConfirm: (code: string) =>
    request<string[]>("/account/totp/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    }),
  totpDisable: () => request<void>("/account/totp", { method: "DELETE" }),

  previewInvite: (token: string) =>
    request<InvitePreview>(`/invitations/${encodeURIComponent(token)}`),
  acceptInvite: (token: string, password: string, display_name: string | null) =>
    request<User>(`/invitations/${encodeURIComponent(token)}/accept`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password, display_name }),
    }),
  redeemReset: (email: string, code: string, new_password: string) =>
    request<void>("/account/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, code, new_password }),
    }),

  // Administration — accounts only. Nothing here returns document data.
  accounts: () => request<AdminAccount[]>("/admin/accounts"),
  invitations: () => request<AdminInvitation[]>("/admin/invitations"),
  invite: (body: {
    email: string;
    library_name: string;
    storage_quota_bytes: number | null;
    note: string | null;
  }) =>
    request<{ token: string; email: string; expires_at: string; path: string }>(
      "/admin/invitations",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ),
  revokeInvite: (id: string) =>
    request<void>(`/admin/invitations/${id}`, { method: "DELETE" }),
  resetCode: (userId: string) =>
    request<{ code: string; email: string; expires_in_hours: number }>(
      `/admin/accounts/${userId}/reset-code`,
      { method: "POST" },
    ),
  suspend: (userId: string) =>
    request<{ sessions_ended: number }>(`/admin/accounts/${userId}/suspend`, {
      method: "POST",
    }),
  restore: (userId: string) =>
    request<void>(`/admin/accounts/${userId}/restore`, { method: "POST" }),
  unlock: (userId: string) =>
    request<void>(`/admin/accounts/${userId}/unlock`, { method: "POST" }),
  setQuota: (userId: string, storage_quota_bytes: number | null) =>
    request<void>(`/admin/accounts/${userId}/quota`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ storage_quota_bytes }),
    }),
  grantAdmin: (userId: string) =>
    request<void>(`/admin/accounts/${userId}/admin`, { method: "POST" }),
};


export interface ServiceBuild {
  commit: string;
  short: string;
  built_at: string;
  ref: string;
  last_seen_at: string;
  stale: boolean;
}

export interface VersionReport {
  services: Record<string, ServiceBuild>;
  mismatch: boolean;
  schema: { applied: string | null; expected: string | null; in_sync: boolean | null };
}

export const versionApi = {
  report: () => request<VersionReport>("/version"),
};

// ---------------------------------------------------------------------------
// First-run setup (Phase 19, REQ-200, REQ-201) — unauthenticated until claimed
// ---------------------------------------------------------------------------

export interface SetupState {
  state: "unclaimed" | "needs_second_factor" | "complete";
}

export interface SetupClaim {
  /** The printed setup code, in any formatting — dashes and case are ignored. */
  code: string;
  email: string;
  password: string;
  display_name?: string | null;
  library_name: string;
}

export const setupApi = {
  state: () => request<SetupState>("/setup"),
  /** Creates the owner's account and signs it in. 400 wrong code · 409 claimed · 422 weak password. */
  claim: (body: SetupClaim) =>
    request<User>("/setup/claim", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  /** After two-factor is confirmed: the owner becomes administrator. */
  complete: () => request<User>("/setup/complete", { method: "POST" }),
};

// ---------------------------------------------------------------------------
// Sign in with D3 Auth (Phase 20, REQ-203 … REQ-209)
// ---------------------------------------------------------------------------

export interface OidcStatus {
  /** `off`, `optional` or `required`. `off` means say nothing about a provider at all. */
  mode: string;
  /** Whether the provider answered its health probe just now. */
  ready: boolean;
  issuer: string | null;
}

export interface OidcLink {
  mode: string;
  issuer: string | null;
  linked: boolean;
  preferred_username: string | null;
  linked_at: string | null;
}

/** Whole-document navigations, not fetches: both ends of this are redirects the browser follows. */
export const oidcPaths = {
  signIn: "/api/auth/oidc/start",
  connect: "/api/auth/oidc/link/start",
};

export const oidcApi = {
  status: () => request<OidcStatus>("/auth/oidc/status"),
  link: () => request<OidcLink>("/auth/oidc/link"),
  disconnect: (password: string) =>
    request<void>("/auth/oidc/disconnect", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ password }).toString(),
    }),
};
