import { useEffect, useRef } from "react";
import { useLocation } from "react-router";

/**
 * What a page load does for free, and a single-page app has to do by hand.
 *
 * `<Routes>` swaps everything under `<main id="content">` while the screen
 * reader's virtual cursor sits on an element that has just been unmounted:
 * nothing is announced, and the next Tab restarts at the skip link, so every
 * navigation costs a keyboard user the whole sidebar again.
 *
 * Two rules keep this from being worse than the problem:
 *
 * - **Never on the first render.** The landing screen has not replaced
 *   anything, and moving focus there would fight the mount.
 * - **Never over a screen that has already claimed focus.** Ask and Search
 *   both `autoFocus` their box, which is the point of those screens; if what
 *   has focus is already inside the new page, leave it alone. Focus moves only
 *   when it would otherwise be nowhere — on the sidebar link just unmounted
 *   from under it, or on `<body>`.
 */
const TITLES: Record<string, string> = {
  "/": "Ask",
  "/search": "Search",
  "/archive": "Archive",
  "/files": "Files",
  "/photos": "Photos",
  "/vault": "Vault",
  "/add": "Add files",
  "/trust": "Trust",
  "/libraries": "Libraries",
  "/import": "Import",
  "/organise": "Organise",
  "/review": "Review",
  "/settings": "Settings",
  "/account": "Your account",
  "/help": "Guides",
  "/people": "People",
  "/rules": "Rules",
  "/pipeline": "Pipeline",
};

function titleFor(pathname: string): string {
  const exact = TITLES[pathname];
  if (exact) return exact;
  if (pathname.endsWith("/segments")) return "Segments";
  if (pathname.startsWith("/document/")) return "Document";
  if (pathname.startsWith("/file/")) return "File";
  return "Bindery";
}

export default function RouteFocus() {
  const { pathname } = useLocation();
  const mounted = useRef(false);

  useEffect(() => {
    if (!mounted.current) {
      mounted.current = true;
      return;
    }
    const main = document.getElementById("content");
    if (!main) return;
    const active = document.activeElement;
    if (active && active !== document.body && main.contains(active)) return;
    main.focus({ preventScroll: true });
  }, [pathname]);

  // Rendered with content from the very first paint rather than inserted when
  // the first message arrives: a live region created at the same moment as its
  // text is not reliably announced.
  return (
    <div role="status" aria-live="polite" className="sr-only">
      {titleFor(pathname)}
    </div>
  );
}
