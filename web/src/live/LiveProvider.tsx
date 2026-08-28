import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";

/**
 * One socket for the whole application (T-8.13).
 *
 * Screens used to each run their own timer, which was wrong in both directions
 * at once: the server was asked constantly while nothing was happening, and a
 * screen still showed stale data for up to a whole interval after something
 * did — which is why the review badge went on claiming work that had already
 * been accepted.
 *
 * So the server pushes a hint saying *something in this topic changed*, and
 * whoever cares refetches. Hints rather than state: pushing full state would
 * mean maintaining a second definition of every screen's data, and the first
 * time the two disagreed the screen would be quietly wrong.
 *
 * Two things make it behave under load:
 *
 * **Coalescing.** Twenty files finishing produces twenty hints and one refetch,
 * because subscribers are notified on a short trailing debounce.
 *
 * **A real fallback.** If the socket cannot be established — an old proxy, a
 * hostile network — subscribers are told, and fall back to a slow poll. Silently
 * showing a frozen screen would be worse than either.
 */
export type Topic =
  | "files"
  | "jobs"
  | "review"
  | "documents"
  | "logs"
  | "settings";

type Handler = () => void;

type LiveValue = {
  /** Run `handler` shortly after anything in `topics` changes. */
  subscribe: (topics: Topic[], handler: Handler) => () => void;
  /** Announce a local change so other screens update without a round trip. */
  publishLocal: (topics: Topic[]) => void;
  connected: boolean;
  /** True when pushes are unavailable and subscribers should poll instead. */
  degraded: boolean;
};

const LiveContext = createContext<LiveValue | null>(null);

// Long enough to collapse a burst, short enough to feel immediate.
const COALESCE_MS = 250;
const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 30000;

export function LiveProvider({ children }: { children: ReactNode }) {
  const [connected, setConnected] = useState(false);
  const [degraded, setDegraded] = useState(false);

  const subscribers = useRef(new Map<Handler, Set<Topic>>());
  const pending = useRef(new Set<Topic>());
  const flushTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const flush = useCallback(() => {
    flushTimer.current = null;
    const topics = pending.current;
    pending.current = new Set();
    if (topics.size === 0) return;
    for (const [handler, wanted] of subscribers.current) {
      for (const topic of wanted) {
        if (topics.has(topic)) {
          handler();
          break;
        }
      }
    }
  }, []);

  const mark = useCallback(
    (topics: Topic[]) => {
      topics.forEach((topic) => pending.current.add(topic));
      if (flushTimer.current === null) {
        flushTimer.current = setTimeout(flush, COALESCE_MS);
      }
    },
    [flush],
  );

  const subscribe = useCallback((topics: Topic[], handler: Handler) => {
    subscribers.current.set(handler, new Set(topics));
    return () => {
      subscribers.current.delete(handler);
    };
  }, []);

  useEffect(() => {
    let socket: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let backoff = RECONNECT_BASE_MS;
    let closed = false;

    function open() {
      if (closed) return;
      const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
      socket = new WebSocket(`${scheme}//${window.location.host}/api/live`);

      socket.onopen = () => {
        backoff = RECONNECT_BASE_MS;
        setConnected(true);
        setDegraded(false);
        // Anything that happened while disconnected was missed, so treat a
        // reconnection as "everything may have changed".
        mark(["files", "jobs", "review", "documents", "logs", "settings"]);
      };

      socket.onmessage = (message) => {
        try {
          const event = JSON.parse(message.data);
          const topics: Topic[] = event.topics ?? [];
          if (topics.includes("ping" as Topic)) return;
          if (topics.includes("connected" as Topic)) {
            // The server tells us whether its own listener is up. If it is not,
            // no hints will arrive and pretending otherwise freezes the UI.
            setDegraded(event.detail?.pushing === false);
            return;
          }
          mark(topics);
        } catch {
          // A malformed frame is not worth tearing the connection down for.
        }
      };

      socket.onclose = () => {
        setConnected(false);
        if (closed) return;
        // Degraded only after a failure to reconnect, so a routine reload does
        // not make every screen start polling.
        retry = setTimeout(() => {
          setDegraded(true);
          open();
        }, backoff);
        backoff = Math.min(backoff * 2, RECONNECT_MAX_MS);
      };

      socket.onerror = () => socket?.close();
    }

    open();
    return () => {
      closed = true;
      if (retry) clearTimeout(retry);
      if (flushTimer.current) clearTimeout(flushTimer.current);
      socket?.close();
    };
  }, [mark]);

  const value = useMemo<LiveValue>(
    () => ({ subscribe, publishLocal: mark, connected, degraded }),
    [subscribe, mark, connected, degraded],
  );

  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>;
}

export function useLive(): LiveValue {
  const value = useContext(LiveContext);
  if (!value) throw new Error("useLive must be used inside a LiveProvider");
  return value;
}

/**
 * Keep something in sync with the server.
 *
 * Refetches once on mount, then whenever the server says one of `topics`
 * changed. `fallbackMs` is used only while pushes are unavailable — it is the
 * safety net, not the mechanism, so it is deliberately slow.
 */
export function useLiveQuery(
  topics: Topic[],
  load: () => void | Promise<void>,
  { fallbackMs = 15000 }: { fallbackMs?: number } = {},
) {
  const { subscribe, degraded } = useLive();
  const latest = useRef(load);
  latest.current = load;

  // Topics are declared inline at every call site, so a fresh array each render
  // would resubscribe forever. The contents are what matter.
  const key = topics.join(",");

  useEffect(() => {
    const run = () => void latest.current();
    run();
    return subscribe(key.split(",") as Topic[], run);
  }, [key, subscribe]);

  useEffect(() => {
    if (!degraded) return;
    const timer = setInterval(() => void latest.current(), fallbackMs);
    return () => clearInterval(timer);
  }, [degraded, fallbackMs]);
}
