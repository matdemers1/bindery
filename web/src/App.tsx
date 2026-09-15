import { Suspense, lazy, useCallback, useEffect, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router";

import { ApiError, api, setupApi, type Library, type SetupState, type User } from "./api";
import CommandPalette from "./features/palette/CommandPalette";
import PipelinePage from "./features/pipeline/PipelinePage";
import AddPage from "./features/add/AddPage";
import ArchivePage from "./features/archive/ArchivePage";
import AskPage from "./features/ask/AskPage";
import FilesPage from "./features/files/FilesPage";
import PhotosPage from "./features/photos/PhotosPage";
import LibrariesPage from "./features/household/LibrariesPage";
import ReviewPage from "./features/review/ReviewPage";
import SearchPage from "./features/search/SearchPage";
import ViewerPage from "./features/viewer/ViewerPage";
import RouteFocus from "./components/RouteFocus";
import Shell from "./components/Shell";
import { LiveProvider } from "./live/LiveProvider";
import Login from "./pages/Login";
import AccountPage from "./features/accounts/AccountPage";
import JoinPage from "./features/accounts/JoinPage";
import ResetPage from "./features/accounts/ResetPage";
import VaultPage from "./features/vault/VaultPage";

// Split out of the entry chunk. Everything above is on a path somebody walks
// on an ordinary day — find a document, look at it, deal with what is waiting.
// These are the ones you go to on purpose and rarely: setup, administration,
// the one-off import, the audit screens. Statically imported, they were part
// of the 436 KB that someone who only ever opens Search still had to download
// before the first screen could paint.
const AdminPage = lazy(() => import("./features/accounts/AdminPage"));
const HelpPage = lazy(() => import("./features/help/HelpPage"));
const ImportPage = lazy(() => import("./features/import/ImportPage"));
const OrganisePage = lazy(() => import("./features/organise/OrganisePage"));
const RulesPage = lazy(() => import("./features/rules/RulesPage"));
const SegmentationPage = lazy(() => import("./features/segmentation/SegmentationPage"));
const SettingsPage = lazy(() => import("./features/settings/SettingsPage"));
const TrustPage = lazy(() => import("./features/trust/TrustPage"));
// Seen once in an archive's life, by one person.
const SetupFlow = lazy(() => import("./features/entry/SetupFlow"));

type State = { status: "loading" } | { status: "out" } | { status: "in"; user: User };

export default function App() {
  const [state, setState] = useState<State>({ status: "loading" });
  const [libraries, setLibraries] = useState<Library[]>([]);
  const [paletteOpen, setPaletteOpen] = useState(false);
  // Whether this archive still needs claiming (Phase 19). Read on every refresh,
  // because claiming and finishing setup both change it. A failure to read it
  // falls back to the ordinary sign-in, which is right for every archive that
  // has ever been set up.
  const [setup, setSetup] = useState<SetupState["state"] | null>(null);

  const refresh = useCallback(async () => {
    setSetup(await setupApi.state().then((s) => s.state, () => "complete" as const));
    try {
      const user = await api.me();
      setState({ status: "in", user });
      setLibraries(await api.libraries());
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        setState({ status: "out" });
        return;
      }
      throw error;
    }
  }, []);

  useEffect(() => {
    // Not `useLiveQuery`, and it cannot be: this component renders the
    // `LiveProvider` that the hook reads, so the context does not exist yet at
    // this point in the tree. It is also not a live query — it establishes the
    // session the socket is opened for, and a signed-out answer here is what
    // decides whether there is a provider at all.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void refresh();
  }, [refresh]);

  // ⌘K / Ctrl-K from anywhere. Registered at the root so no screen has to
  // remember to wire it up.
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key.toLowerCase() === "k" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        setPaletteOpen((open) => !open);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // An invitation link has to work before there is a session — it is how the
  // session comes to exist. Read straight from the path rather than through the
  // router, because the router lives inside the signed-in tree.
  const joinToken = window.location.pathname.startsWith("/join/")
    ? decodeURIComponent(window.location.pathname.slice("/join/".length))
    : null;
  if (joinToken && state.status !== "in") {
    return <JoinPage token={joinToken} />;
  }

  // And so does a reset code, for the same reason: it is the way back in for
  // somebody who cannot get in. Not a `<Route>` — the router below is inside
  // the signed-in tree, so a route would be unreachable by exactly the person
  // who needs it. Signed in, this falls through to the catch-all and lands on
  // Ask, which is where "reset my password" belongs from inside a session.
  if (window.location.pathname === "/reset" && state.status !== "in") {
    return <ResetPage />;
  }

  if (state.status === "loading" || setup === null) {
    return <Centered>Loading…</Centered>;
  }

  // A fresh install is claimed in the browser, and an owner who signed out
  // half-way resumes at the second factor once they sign back in.
  if ((setup === "unclaimed" && state.status === "out") ||
      (setup === "needs_second_factor" && state.status === "in")) {
    return (
      <Suspense fallback={<Centered>Loading…</Centered>}>
        <SetupFlow stage={setup === "unclaimed" ? "claim" : "secure"} onChanged={refresh} />
      </Suspense>
    );
  }
  if (state.status === "out") {
    return <Login onSignedIn={refresh} />;
  }

  return (
    <BrowserRouter>
      <LiveProvider>
        <RouteFocus />
        <Shell
        user={state.user}
        libraries={libraries}
        onOpenPalette={() => setPaletteOpen(true)}
        onUploaded={refresh}
        onSignedOut={async () => {
          await api.logout();
          setState({ status: "out" });
        }}
      >
        {/* Inside `Shell`, so the chrome stays put while a split screen
            arrives and only the panel it lives in changes. Same wording as the
            session's own loading state — the app already says this. */}
        <Suspense fallback={<Centered>Loading…</Centered>}>
        <Routes>
          {/* Ask is the front door: it is the only screen that answers the
              question people actually arrive with. Search keeps its own URL
              because "find the thing I know I have" is a different job. */}
          <Route
            path="/"
            element={
              <AskPage
                libraries={libraries}
                onUploaded={refresh}
                userId={state.user.id}
              />
            }
          />
          <Route
            path="/search"
            element={<SearchPage libraries={libraries} userId={state.user.id} />}
          />
          {/* Documents are the normal path: search returns page ranges, not files. */}
          <Route
            path="/document/:documentId/page/:pageNumber"
            element={<ViewerPage mode="document" />}
          />
          <Route path="/document/:documentId" element={<Navigate to="page/1" replace />} />
          {/* The whole bundle, for when the cut itself is what you're looking at. */}
          <Route path="/file/:fileId/page/:pageNumber" element={<ViewerPage mode="file" />} />
          <Route path="/file/:fileId/segments" element={<SegmentationPage />} />
          <Route path="/file/:fileId" element={<Navigate to="page/1" replace />} />
          <Route path="/archive" element={<ArchivePage />} />
          <Route path="/files" element={<FilesPage />} />
          <Route path="/photos" element={<PhotosPage />} />
          <Route path="/vault" element={<VaultPage />} />
          <Route
            path="/add"
            element={<AddPage libraries={libraries} onUploaded={refresh} />}
          />
          <Route path="/ask" element={<Navigate to="/" replace />} />
          <Route path="/trust" element={<TrustPage />} />
          <Route path="/libraries" element={<LibrariesPage />} />
          <Route path="/import" element={<ImportPage libraries={libraries} />} />
          <Route path="/organise" element={<OrganisePage libraries={libraries} />} />
          <Route path="/review" element={<ReviewPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/account" element={<AccountPage />} />
          <Route path="/help" element={<HelpPage />} />
          <Route path="/people" element={<AdminPage />} />
          {/* Someone already signed in who opens an invitation link should end
              up somewhere sensible rather than at a form they cannot use. */}
          <Route path="/join/*" element={<Navigate to="/" replace />} />
          <Route path="/rules" element={<RulesPage libraries={libraries} />} />
          <Route path="/pipeline" element={<PipelinePage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
        </Suspense>
        </Shell>
        {/* Mounted only while open, so each invocation starts empty without
            an effect to clear it. */}
        {paletteOpen && <CommandPalette onClose={() => setPaletteOpen(false)} />}
      </LiveProvider>
    </BrowserRouter>
  );
}

function Centered({ children }: { children: React.ReactNode }) {
  return <div className="flex h-full items-center justify-center text-muted">{children}</div>;
}
