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
  page_number: number;
  /** Contains <mark> tags from ts_headline. Rendered through a sanitizer. */
  snippet: string;
  rank: number;
}

export interface SearchResult {
  source_file_id: string;
  library_id: string;
  original_filename: string | null;
  page_count: number | null;
  state: string;
  received_at: string;
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
  states?: string[];
  sourceFileId?: string;
  limit?: number;
}

function searchQueryString({ q, libraryIds, states, sourceFileId, limit }: SearchParams) {
  const params = new URLSearchParams({ q });
  libraryIds?.forEach((id) => params.append("library_id", id));
  states?.forEach((state) => params.append("state", state));
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
  pageBoxes: (id: string, page: number, signal?: AbortSignal) =>
    request<PageBoxes>(`/files/${id}/pages/${page}/boxes`, { signal }),

  pipeline: () => request<PipelineStatus>("/pipeline"),
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
};
