import { useCallback, useEffect, useRef, useState } from "react";

import { api, type Library, type SourceFile, type User } from "../api";

export default function LibraryView({
  user,
  onSignedOut,
}: {
  user: User;
  onSignedOut: () => Promise<void>;
}) {
  const [libraries, setLibraries] = useState<Library[]>([]);
  const [files, setFiles] = useState<SourceFile[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    const [nextLibraries, nextFiles] = await Promise.all([api.libraries(), api.sourceFiles()]);
    setLibraries(nextLibraries);
    setFiles(nextFiles);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function upload(selected: FileList | null) {
    const target = libraries[0];
    if (!selected?.length || !target) return;

    setBusy(true);
    try {
      const results = await Promise.all(
        Array.from(selected).map((file) => api.upload(target.id, file)),
      );
      const duplicates = results.filter((result) => result.duplicate).length;
      setNotice(
        duplicates
          ? `${results.length} accepted, ${duplicates} already in the archive.`
          : `${results.length} accepted.`,
      );
      await load();
    } catch {
      setNotice("Upload failed.");
    } finally {
      setBusy(false);
      if (input.current) input.current.value = "";
    }
  }

  return (
    <div className="mx-auto max-w-4xl p-8">
      <header className="mb-8 flex items-baseline justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Bindery</h1>
          <p className="text-sm text-muted">
            {libraries.map((library) => library.name).join(" · ") || "No libraries"}
          </p>
        </div>
        <button onClick={onSignedOut} className="text-sm text-muted hover:underline">
          {user.email} — sign out
        </button>
      </header>

      <label
        onDragOver={(event) => event.preventDefault()}
        onDrop={(event) => {
          event.preventDefault();
          void upload(event.dataTransfer.files);
        }}
        className="block cursor-pointer rounded-xl border border-dashed border-edge bg-surface p-10 text-center"
      >
        <input
          ref={input}
          type="file"
          multiple
          className="hidden"
          onChange={(event) => void upload(event.target.files)}
        />
        <span className="text-muted">
          {busy ? "Uploading…" : "Drop files here, or click to choose"}
        </span>
      </label>

      {notice && <p className="mt-3 text-sm text-accent">{notice}</p>}

      <h2 className="mt-10 mb-3 text-sm font-medium tracking-wide text-muted uppercase">
        Source files
      </h2>

      {files.length === 0 ? (
        <p className="rounded-lg border border-edge p-6 text-sm text-muted">
          Nothing yet. Uploaded originals appear here, unmodified.
        </p>
      ) : (
        <ul className="divide-y divide-edge rounded-lg border border-edge">
          {files.map((file) => (
            <li key={file.id} className="flex items-baseline justify-between gap-4 p-4">
              <div className="min-w-0">
                <p className="truncate">{file.original_filename ?? "(no filename)"}</p>
                <p className="font-mono text-xs text-muted">
                  {file.sha256.slice(0, 16)}… · {formatBytes(file.byte_size)}
                </p>
              </div>
              <span className="shrink-0 rounded-full border border-edge px-2 py-0.5 text-xs text-muted">
                {file.state}
              </span>
            </li>
          ))}
        </ul>
      )}

      <p className="mt-8 text-xs text-muted">
        Phase 0. Files are stored and listed; nothing is processed yet — OCR, page-level
        indexing and search arrive in Phase 1.
      </p>
    </div>
  );
}

function formatBytes(bytes: number): string {
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}
