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
  upload: (libraryId: string, file: File) => {
    const form = new FormData();
    form.append("library_id", libraryId);
    form.append("file", file);
    return request<UploadResult>("/upload", { method: "POST", body: form });
  },
};
