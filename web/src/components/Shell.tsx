import { useCallback, useEffect, useRef, useState } from "react";
import { NavLink } from "react-router";
import {
  Activity,
  BookOpen,
  ChevronUp,
  ClipboardCheck,
  FolderTree,
  Images,
  Import,
  Library as LibraryIcon,
  LogOut,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  Search,
  Settings,
  ShieldCheck,
  Sparkles,
  Tags,
  UserCircle,
  Users,
  UsersRound,
  Vault,
  Workflow,
} from "lucide-react";

import { api, type Library, type User } from "../api";
import VersionBadge from "./VersionBadge";
import { useLiveQuery } from "../live/LiveProvider";
import { Wordmark } from "./brand/Logo";

/**
 * The application shell.
 *
 * A sidebar rather than a top bar because eleven destinations do not fit
 * across a page — they were shrinking to fit, which made the whole app read as
 * a row of similar-looking words. Vertically there is room to group them, and
 * grouping is what makes eleven feel like three.
 *
 * The groups answer three different questions: *find something*, *deal with
 * something*, *look after the archive*. Most days only the first group is
 * touched, which is why it is at the top and why Ask is the landing screen.
 *
 * **Keep holds only what changes on its own** — Trust and Pipeline. Libraries,
 * People, Your account, Settings and Guides moved into the account menu at the
 * bottom, because they are things you set up once and then stop thinking about,
 * and they were making a list of sixteen out of a list of eleven. Nothing was
 * removed; everything is still one click away.
 *
 * Colour is load-bearing here, not decoration. The only coloured things in the
 * navigation are counts of work waiting for you and a warning when something
 * needs doing — so a spot of colour in the sidebar always means "something
 * changed that you might care about", and never "this is a button".
 *
 * That claim stopped being true for a while and this is the repair. The Trust
 * badge ran off `healthy`, which was false while three files sat in
 * `dead_letter` — a 10x5 pixel image, a 1500x10 pixel image and an XFA form,
 * all correctly refused, none of them fixable. A warning that is always on is
 * not a warning. Those are `declined` now and the badge counts only work
 * somebody can still act on (ADR-011).
 */
type Item = {
  to: string;
  label: string;
  icon: typeof Search;
  hint?: string;
  badge?: "review" | "health";
  adminOnly?: boolean;
};

const GROUPS: { title: string; items: Item[] }[] = [
  {
    title: "Find",
    items: [
      { to: "/", label: "Ask", icon: Sparkles, hint: "Answers, with the page they came from" },
      { to: "/search", label: "Search", icon: Search, hint: "Every page in the archive" },
      { to: "/archive", label: "Archive", icon: LibraryIcon, hint: "Browse everything" },
      { to: "/files", label: "Files", icon: FolderTree, hint: "The folder tree" },
      { to: "/photos", label: "Photos", icon: Images, hint: "Everything you recognise by looking" },
      {
        to: "/vault",
        label: "Vault",
        icon: Vault,
        hint: "Behind a second passphrase, and out of everything above",
      },
    ],
  },
  {
    title: "Tend",
    items: [
      { to: "/review", label: "Review", icon: ClipboardCheck, badge: "review" },
      { to: "/organise", label: "Organise", icon: Tags },
      { to: "/import", label: "Import", icon: Import },
      { to: "/rules", label: "Rules", icon: Workflow },
    ],
  },
  {
    title: "Keep",
    items: [
      { to: "/trust", label: "Trust", icon: ShieldCheck, badge: "health" },
      { to: "/pipeline", label: "Pipeline", icon: Activity },
    ],
  },
];

/**
 * Set up once, then forgotten — so they live in the account menu rather than
 * occupying five of sixteen rows in a navigation you read every day.
 *
 * Reachable in one click, and `tests/test_shell_navigation` asserts every route
 * still has a link somewhere in the shell. Tidying a sidebar by quietly
 * stranding a screen is not tidying.
 */
const ACCOUNT_ITEMS: Item[] = [
  { to: "/account", label: "Your account", icon: UserCircle },
  { to: "/libraries", label: "Libraries", icon: Users },
  { to: "/people", label: "People", icon: UsersRound, adminOnly: true },
  { to: "/settings", label: "Settings", icon: Settings },
  { to: "/help", label: "Guides", icon: BookOpen },
];

const COLLAPSED_KEY = "bindery.sidebar.collapsed";

/**
 * Hiding a link is presentation, not permission.
 *
 * `/people` is gated by `require_admin` at the API, which answers 404 to
 * everyone else. This only keeps the sidebar honest — a link that always 404s
 * is worse than no link.
 */
function visibleGroups(isAdmin: boolean) {
  return GROUPS.map((group) => ({
    ...group,
    items: group.items.filter((item) => !item.adminOnly || isAdmin),
  }));
}

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
  const [dragging, setDragging] = useState(false);
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem(COLLAPSED_KEY) === "1",
  );
  const [reviewCount, setReviewCount] = useState(0);
  const [unhealthy, setUnhealthy] = useState(false);
  const [accountOpen, setAccountOpen] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    localStorage.setItem(COLLAPSED_KEY, collapsed ? "1" : "0");
  }, [collapsed]);

  const refreshBadges = useCallback(async () => {
    // Failures are swallowed on purpose: a badge is a nicety, and a navigation
    // that throws up an error because a count could not be fetched is worse
    // than a navigation with no count.
    const [review, health] = await Promise.allSettled([api.review(), api.healthPanel()]);
    if (review.status === "fulfilled") setReviewCount(review.value.total);
    if (health.status === "fulfilled") setUnhealthy(!health.value.healthy);
  }, []);

  // The counts move the moment something changes rather than up to a minute
  // later. This is the bug that made the sidebar keep claiming work you had
  // already dealt with.
  useLiveQuery(["review", "jobs", "documents"], refreshBadges, { fallbackMs: 60_000 });

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
      void refreshBadges();
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
      className="flex min-h-full"
      onDragOver={(event) => {
        event.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={(event) => {
        event.preventDefault();
        setDragging(false);
        void upload(event.dataTransfer.files);
      }}
    >
      <a href="#content" className="skip-link text-sm">
        Skip to content
      </a>

      <aside
        className={`sticky top-0 flex h-screen shrink-0 flex-col border-r border-edge bg-surface/40 transition-[width] duration-150 ${
          collapsed ? "w-[4.25rem]" : "w-56"
        }`}
      >
        <div className="flex items-center justify-between px-4 py-4">
          <NavLink to="/" aria-label="Bindery home">
            <Wordmark collapsed={collapsed} />
          </NavLink>
        </div>

        <nav aria-label="Sections" className="flex-1 overflow-y-auto px-2.5 pb-2">
          {visibleGroups(user.is_admin ?? false).map((group) => (
            <div key={group.title} className="mb-4">
              {!collapsed && (
                <h2 className="px-2.5 pb-1.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-muted">
                  {group.title}
                </h2>
              )}
              <ul className="space-y-0.5">
                {group.items.map((item) => (
                  <li key={item.to}>
                    <Tab
                      item={item}
                      collapsed={collapsed}
                      badge={
                        item.badge === "review" && reviewCount > 0
                          ? String(reviewCount)
                          : item.badge === "health" && unhealthy
                            ? "!"
                            : null
                      }
                    />
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </nav>

        <div className="border-t border-edge p-2.5">
          <input
            ref={input}
            type="file"
            multiple
            className="hidden"
            onChange={(event) => void upload(event.target.files)}
          />
          {/* A link to a page, not a file dialog. Opening the OS picker here
              meant everything after it happened somewhere you could not watch:
              the dialog closed, a toast said a number, and the files went off
              to be processed with no way to see what became of them. */}
          <NavLink
            to="/add"
            title="Add files"
            className="flex w-full items-center justify-center gap-2 rounded-lg bg-accent px-3 py-2 text-sm font-medium text-ink"
          >
            <Plus size={16} strokeWidth={2.5} />
            {!collapsed && "Add files"}
          </NavLink>

          <button
            onClick={onOpenPalette}
            title="Jump to a page (⌘K)"
            className="mt-1.5 flex w-full items-center justify-center gap-2 rounded-lg border border-edge px-3 py-2 text-sm text-muted hover:text-neutral-100"
          >
            <Search size={15} />
            {!collapsed && (
              <>
                Jump to…
                <kbd className="ml-auto font-mono text-[11px]">⌘K</kbd>
              </>
            )}
          </button>

          <VersionBadge collapsed={collapsed} />

          {/* Set-up screens, one click away and out of the daily list. Opened
              upwards because the trigger is at the bottom of the viewport. */}
          {accountOpen && (
            <ul className="mt-2 space-y-0.5 border-t border-edge pt-2">
              {ACCOUNT_ITEMS.filter(
                (item) => !item.adminOnly || user.is_admin,
              ).map((item) => (
                <li key={item.to}>
                  <Tab item={item} collapsed={collapsed} badge={null} />
                </li>
              ))}
            </ul>
          )}

          <div
            className={`mt-2 flex items-center gap-1 ${
              collapsed ? "flex-col" : "justify-between"
            }`}
          >
            <button
              onClick={() => setAccountOpen((open) => !open)}
              title={user.email}
              aria-expanded={accountOpen}
              aria-label="Account and setup"
              className={`flex min-w-0 items-center gap-2 rounded p-1.5 text-sm ${
                accountOpen ? "text-neutral-100" : "text-muted hover:text-neutral-100"
              }`}
            >
              <UserCircle size={16} className="shrink-0" />
              {!collapsed && (
                <>
                  <span className="truncate">{user.email.split("@")[0]}</span>
                  <ChevronUp
                    size={14}
                    className={`shrink-0 transition-transform ${accountOpen ? "" : "rotate-180"}`}
                  />
                </>
              )}
            </button>
            <div className={`flex items-center gap-1 ${collapsed ? "flex-col" : ""}`}>
              <button
                onClick={() => setCollapsed((value) => !value)}
                title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
                aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
                className="rounded p-1.5 text-muted hover:text-neutral-100"
              >
                {collapsed ? <PanelLeftOpen size={16} /> : <PanelLeftClose size={16} />}
              </button>
              <button
                onClick={onSignedOut}
                title={`Sign out (${user.email})`}
                aria-label="Sign out"
                className="rounded p-1.5 text-muted hover:text-neutral-100"
              >
                <LogOut size={16} />
              </button>
            </div>
          </div>
        </div>
      </aside>

      <div className="min-w-0 flex-1">
        {notice && (
          <div
            role="status"
            aria-live="polite"
            className="border-b border-edge bg-surface px-6 py-1.5 text-center text-xs text-accent"
          >
            {notice}
          </div>
        )}
        <main id="content" tabIndex={-1} className="px-6 py-8">
          {children}
        </main>
      </div>

      {/* Drop anywhere. Shown only while a drag is actually over the window, so
          it is a confirmation rather than a permanent instruction. */}
      {dragging && (
        <div className="pointer-events-none fixed inset-0 z-50 flex items-center justify-center bg-ink/70">
          <p className="rounded-xl border-2 border-dashed border-accent px-8 py-6 text-lg text-accent">
            {busy ? "Adding…" : "Drop to add to the archive"}
          </p>
        </div>
      )}
    </div>
  );
}

function Tab({
  item,
  collapsed,
  badge,
}: {
  item: Item;
  collapsed: boolean;
  badge: string | null;
}) {
  const Icon = item.icon;
  return (
    <NavLink
      to={item.to}
      end={item.to === "/"}
      title={collapsed ? item.label : item.hint}
      className={({ isActive }) =>
        `group flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm transition-colors ${
          isActive
            ? "bg-accent/12 font-medium text-accent"
            : "text-muted hover:bg-edge/50 hover:text-neutral-100"
        } ${collapsed ? "justify-center" : ""}`
      }
    >
      <Icon size={17} className="shrink-0" />
      {!collapsed && <span className="truncate">{item.label}</span>}
      {badge && (
        <span
          className={`ml-auto rounded-full px-1.5 text-[11px] font-medium ${
            badge === "!"
              ? "bg-red-500/20 text-red-300"
              : "bg-accent/20 text-accent"
          } ${collapsed ? "absolute translate-x-3 -translate-y-3" : ""}`}
        >
          {badge}
        </span>
      )}
    </NavLink>
  );
}
