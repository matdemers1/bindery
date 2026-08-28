// Thin fetch wrapper. Auth rides on HTTP-only cookies, so nothing here ever
// touches a token — there is no token for JavaScript to leak.

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api${path}`, { credentials: "same-origin", ...init });
  if (response.status === 204) return undefined as T;

  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiError(response.status, body?.detail ?? response.statusText);
  }
  return body as T;
}

export interface User {
  id: string;
  email: string;
  display_name: string | null;
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

export interface WhyPanel {
  document: Document;
  classification: ClassificationRecord | null;
  provenance: FieldProvenanceRecord[];
  tags: TagRef[];
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

export interface Settings {
  anthropic_key_configured: boolean;
  anthropic_key_hint: string | null;
  model: string;
  prompt_version: string;
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

export interface Job {
  id: string;
  source_file_id: string | null;
  stage: string;
  state: string;
  attempts: number;
  last_error: string | null;
  scheduled_for: string;
  updated_at: string;
}

export interface PipelineStatus {
  counts: { stage: string; state: string; count: number }[];
  attention: Job[];
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
  login: (email: string, password: string) =>
    request<User>("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
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
  updateSettings: (body: Partial<Record<"anthropic_api_key" | "model" | "prompt_version", string>>) =>
    request<Settings>("/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  testAi: () => request<SettingsTest>("/settings/test-ai", { method: "POST" }),

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

  review: () => request<ReviewQueue>("/review"),
  why: (documentId: string) => request<WhyPanel>(`/documents/${documentId}/why`),
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
  retryJob: (id: string) => request<Job>(`/pipeline/jobs/${id}/retry`, { method: "POST" }),

  upload: (libraryId: string, file: File) => {
    const form = new FormData();
    form.append("library_id", libraryId);
    form.append("file", file);
    return request<UploadResult>("/upload", { method: "POST", body: form });
  },
};

/** Blob URLs. Authenticated and library-scoped server-side; no token in the URL. */
export const fileUrl = {
  render: (id: string, page: number) => `/api/files/${id}/pages/${page}/render`,
  thumb: (id: string, page: number) => `/api/files/${id}/pages/${page}/thumb`,
  pdf: (id: string) => `/api/files/${id}/pdf`,
  /** Just this document's pages, extracted as a standalone PDF. */
  documentPdf: (documentId: string) => `/api/documents/${documentId}/pdf`,
};
