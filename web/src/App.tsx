import { useCallback, useEffect, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router";

import { ApiError, api, type Library, type User } from "./api";
import CommandPalette from "./features/palette/CommandPalette";
import PipelinePage from "./features/pipeline/PipelinePage";
import AddPage from "./features/add/AddPage";
import ArchivePage from "./features/archive/ArchivePage";
import AskPage from "./features/ask/AskPage";
import FilesPage from "./features/files/FilesPage";
import PhotosPage from "./features/photos/PhotosPage";
import LibrariesPage from "./features/household/LibrariesPage";
import ImportPage from "./features/import/ImportPage";
import OrganisePage from "./features/organise/OrganisePage";
import ReviewPage from "./features/review/ReviewPage";
import RulesPage from "./features/rules/RulesPage";
import SearchPage from "./features/search/SearchPage";
import SettingsPage from "./features/settings/SettingsPage";
import TrustPage from "./features/trust/TrustPage";
import SegmentationPage from "./features/segmentation/SegmentationPage";
import ViewerPage from "./features/viewer/ViewerPage";
import Shell from "./components/Shell";
import { LiveProvider } from "./live/LiveProvider";
import Login from "./pages/Login";
import AccountPage from "./features/accounts/AccountPage";
import AdminPage from "./features/accounts/AdminPage";
import JoinPage from "./features/accounts/JoinPage";
import ResetPage from "./features/accounts/ResetPage";
import HelpPage from "./features/help/HelpPage";
import VaultPage from "./features/vault/VaultPage";

type State = { status: "loading" } | { status: "out" } | { status: "in"; user: User };

export default function App() {
  const [state, setState] = useState<State>({ status: "loading" });
  const [libraries, setLibraries] = useState<Library[]>([]);
  const [paletteOpen, setPaletteOpen] = useState(false);

  const refresh = useCallback(async () => {
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
    // An async data load: the state is genuinely unavailable on the first
    // render, so the extra pass is the point rather than a mistake.
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

  if (state.status === "loading") {
    return <Centered>Loading…</Centered>;
  }
  if (state.status === "out") {
    return <Login onSignedIn={refresh} />;
  }

  return (
    <BrowserRouter>
      <LiveProvider>
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
