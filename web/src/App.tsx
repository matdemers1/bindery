import { useCallback, useEffect, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router";

import { ApiError, api, type Library, type User } from "./api";
import CommandPalette from "./features/palette/CommandPalette";
import PipelinePage from "./features/pipeline/PipelinePage";
import ArchivePage from "./features/archive/ArchivePage";
import FilesPage from "./features/files/FilesPage";
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
import Login from "./pages/Login";

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

  if (state.status === "loading") {
    return <Centered>Loading…</Centered>;
  }
  if (state.status === "out") {
    return <Login onSignedIn={refresh} />;
  }

  return (
    <BrowserRouter>
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
          <Route path="/" element={<SearchPage libraries={libraries} />} />
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
          <Route path="/trust" element={<TrustPage />} />
          <Route path="/import" element={<ImportPage libraries={libraries} />} />
          <Route path="/organise" element={<OrganisePage libraries={libraries} />} />
          <Route path="/review" element={<ReviewPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/rules" element={<RulesPage libraries={libraries} />} />
          <Route path="/pipeline" element={<PipelinePage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Shell>
      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} />
    </BrowserRouter>
  );
}

function Centered({ children }: { children: React.ReactNode }) {
  return <div className="flex h-full items-center justify-center text-muted">{children}</div>;
}
