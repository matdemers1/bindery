import { useRef, useState } from "react";
import { NavLink } from "react-router";

import { api, type Library, type User } from "../api";

export default function Shell({
  user,
  libraries,
  children,
  onOpenPalette,
  onUploaded,
  onSignedOut,
}: {
  user: User;
  libraries: Library[];
  children: React.ReactNode;
  onOpenPalette: () => void;
  onUploaded: () => void;
  onSignedOut: () => void;
}) {
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  async function upload(files: FileList | null) {
    const target = libraries[0];
    if (!files?.length || !target) return;
    setBusy(true);
    try {
      const results = await Promise.all(
        Array.from(files).map((file) => api.upload(target.id, file)),
      );
      const duplicates = results.filter((result) => result.duplicate).length;
      setNotice(
        duplicates
          ? `${results.length} accepted · ${duplicates} already in the archive`
          : `${results.length} accepted — OCR is running`,
      );
      onUploaded();
    } catch {
      setNotice("Upload failed.");
    } finally {
      setBusy(false);
      if (input.current) input.current.value = "";
      setTimeout(() => setNotice(null), 6000);
    }
  }

  return (
    <div
      className="min-h-full"
      onDragOver={(event) => event.preventDefault()}
      onDrop={(event) => {
        event.preventDefault();
        void upload(event.dataTransfer.files);
      }}
    >
      <header className="sticky top-0 z-40 border-b border-edge bg-ink/90 backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center gap-6 px-6 py-3">
          <NavLink to="/" className="text-base font-semibold tracking-tight">
            Bindery
          </NavLink>

          <nav className="flex items-center gap-1 text-sm">
            <Tab to="/">Search</Tab>
            <Tab to="/archive">Archive</Tab>
            <Tab to="/review">Review</Tab>
            <Tab to="/organise">Organise</Tab>
            <Tab to="/rules">Rules</Tab>
            <Tab to="/import">Import</Tab>
            <Tab to="/pipeline">Pipeline</Tab>
            <Tab to="/settings">Settings</Tab>
          </nav>

          <div className="ml-auto flex items-center gap-2">
            <button
              onClick={onOpenPalette}
              className="rounded-md border border-edge px-3 py-1.5 text-sm text-muted hover:border-accent/60"
            >
              Jump to… <span className="ml-1 font-mono text-xs">⌘K</span>
            </button>
            <input
              ref={input}
              type="file"
              multiple
              className="hidden"
              onChange={(event) => void upload(event.target.files)}
            />
            <button
              onClick={() => input.current?.click()}
              disabled={busy || libraries.length === 0}
              className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-ink disabled:opacity-50"
            >
              {busy ? "Uploading…" : "Add files"}
            </button>
            <button
              onClick={onSignedOut}
              title={user.email}
              className="text-sm text-muted hover:underline"
            >
              Sign out
            </button>
          </div>
        </div>
        {notice && (
          <div className="border-t border-edge bg-surface px-6 py-1.5 text-center text-xs text-accent">
            {notice}
          </div>
        )}
      </header>

      <main>{children}</main>
    </div>
  );
}

function Tab({ to, children }: { to: string; children: React.ReactNode }) {
  return (
    <NavLink
      to={to}
      end={to === "/"}
      className={({ isActive }) =>
        `rounded-md px-2.5 py-1.5 ${isActive ? "bg-surface text-neutral-100" : "text-muted hover:text-neutral-200"}`
      }
    >
      {children}
    </NavLink>
  );
}
