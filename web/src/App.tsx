import { useCallback, useEffect, useState } from "react";

import { ApiError, api, type User } from "./api";
import Login from "./pages/Login";
import LibraryView from "./pages/Library";

type State = { status: "loading" } | { status: "out" } | { status: "in"; user: User };

export default function App() {
  const [state, setState] = useState<State>({ status: "loading" });

  const refresh = useCallback(async () => {
    try {
      setState({ status: "in", user: await api.me() });
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

  if (state.status === "loading") {
    return <Centered>Loading…</Centered>;
  }

  if (state.status === "out") {
    return <Login onSignedIn={refresh} />;
  }

  return (
    <LibraryView
      user={state.user}
      onSignedOut={async () => {
        await api.logout();
        setState({ status: "out" });
      }}
    />
  );
}

function Centered({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-full items-center justify-center text-muted">{children}</div>
  );
}
