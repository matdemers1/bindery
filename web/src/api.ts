// Thin fetch wrapper. Auth rides on HTTP-only cookies, so nothing here ever
// touches a token — there is no token for JavaScript to leak.

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
}

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
}

export interface ReviewQueue {
  total: number;
  documents: Document[];
}

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
}

export interface ImportItem {
  path: string;
  state: string;
  byte_size: number | null;
  sha256: string | null;
  source_file_id: string | null;
  error: string | null;
}

export interface BulkResult {
  matched: number;
  operation_id: string | null;
  changes: { document_id: string; title: string | null; changes: Record<string, unknown> }[];
}

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

export interface PendingReason {
  code: "never_attempted" | "provider_unavailable" | "failed";
  label: string;
  detail: string;
  count: number;
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

export interface SearchParams {
  q: string;
  libraryIds?: string[];
  knownFormCodes?: string[];
  sourceFileId?: string;
  limit?: number;
}

function searchQueryString({
  q,
  libraryIds,
  knownFormCodes,
  sourceFileId,
  limit,
}: SearchParams) {
  const params = new URLSearchParams({ q });
  libraryIds?.forEach((id) => params.append("library_id", id));
  knownFormCodes?.forEach((code) => params.append("known_form", code));
  if (sourceFileId) params.set("source_file_id", sourceFileId);
  if (limit) params.set("limit", String(limit));
  return params.toString();
}

export const api = {
  me: () => request<User>("/auth/me"),
  login: (email: string, password: string, code?: string) =>
    request<User>("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password, code }),
    }),
  logout: () => request<void>("/auth/logout", { method: "POST" }),

  libraries: () => request<Library[]>("/libraries"),
  sourceFiles: () => request<SourceFile[]>("/source-files"),

  search: (params: SearchParams, signal?: AbortSignal) =>
    request<SearchResponse>(`/search?${searchQueryString(params)}`, { signal }),

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
  pageBoxes: (id: string, page: number, signal?: AbortSignal) =>
    request<PageBoxes>(`/files/${id}/pages/${page}/boxes`, { signal }),

  pipeline: () => request<PipelineStatus>("/pipeline"),

  archive: (params: Record<string, string | string[]> = {}) => {
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (Array.isArray(value)) value.forEach((v) => search.append(key, v));
      else if (value) search.set(key, value);
    }
    return request<Archive>(`/archive?${search}`);
  },
  tree: (groupBy: string) => request<Tree>(`/archive/tree?group_by=${groupBy}`),

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
        | "offsite_kms_key_id",
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

  imports: () => request<ImportSession[]>("/imports"),
  startImport: (body: { library_id: string; root_path: string; sample_size?: number }) =>
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
  runImport: (id: string, batch = 50) =>
    request<ImportSession>(`/imports/${id}/run?batch=${batch}`, { method: "POST" }),
  pauseImport: (id: string) =>
    request<ImportSession>(`/imports/${id}/pause`, { method: "POST" }),
  curateImport: (id: string) =>
    request<ImportSession>(`/imports/${id}/curate`, { method: "POST" }),

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

  correspondents: () => request<CorrespondentRef[]>("/correspondents"),
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


  // --- Phase 6 -----------------------------------------------------------

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


  // --- Phase 7 -----------------------------------------------------------

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


  // --- Phase 8 -----------------------------------------------------------

  ask: (question: string) =>
    request<AskAnswer>("/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    }),

  healthPanel: () => request<HealthPanel>("/health/panel"),

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


  photos: (params: { q?: string; undescribed?: boolean; limit?: number; offset?: number } = {}) => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== "" && value !== false) query.set(key, String(value));
    }
    return request<PhotoWall>(`/photos${query.size ? `?${query}` : ""}`);
  },

  /** Ask which entries of a kind are the same thing. Changes nothing. */
  unifyPreview: (kind: UnifyKind = "correspondent") =>
    request<UnifyProposal>(`/taxonomy/unify/preview?kind=${kind}`, { method: "POST" }),
  unifyApply: (kind: UnifyKind, canonicalId: string, memberIds: string[]) =>
    request<MergePreview>(`/taxonomy/unify/apply?kind=${kind}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ canonical_id: canonicalId, member_ids: memberIds }),
    }),

  upload: (libraryId: string, file: File) => {
    const form = new FormData();
    form.append("library_id", libraryId);
    form.append("file", file);
    return request<UploadResult>("/upload", { method: "POST", body: form });
  },
};


// --- Phase 6: trust, export and resilience -------------------------------

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
  orphans: string[];
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


// --- Phase 7: household and libraries ------------------------------------

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


// --- Phase 8: ask, health, tokens ----------------------------------------

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


// --- Diagnostics and per-file progress -----------------------------------

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
}

export interface PhotoWall {
  total: number;
  photos: Photo[];
}

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
export const fileUrl = {
  render: (id: string, page: number) => `/api/files/${id}/pages/${page}/render`,
  thumb: (id: string, page: number) => `/api/files/${id}/pages/${page}/thumb`,
  pdf: (id: string) => `/api/files/${id}/pdf`,
  /** Just this document's pages, extracted as a standalone PDF. */
  documentPdf: (documentId: string) => `/api/documents/${documentId}/pdf`,
};


// --------------------------------------------------------------------------
// Accounts (Phase 10)
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
    request<{ secret: string; uri: string }>("/account/totp/start", { method: "POST" }),
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
