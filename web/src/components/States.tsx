import { ApiError } from "../api";
import { Alert, Button, EmptyState, Link as TextLink } from "@d3cloud/ui";
import { Link as RouterLink } from "react-router";

/**
 * The four states every screen owes the reader (T-8.7).
 *
 * They exist as shared components because the wording matters more than the
 * markup and was drifting: a screen that says "Error" has told the reader
 * nothing they can act on, and a spinner with no label leaves them unsure
 * whether anything is happening at all.
 *
 * `Denied` is the one usually missing. A 403 in a household archive is not a
 * bug — it is the boundary working — and it should read that way rather than as
 * a failure the reader caused.
 */
export function Denied({ what = "this" }: { what?: string }) {
  return (
    <EmptyState kind="no-access" heading="Not yours to see">
      You do not have access to {what}. In Bindery access is per library, not per
      document, so this means you are not a member of the library it is in — ask
      an owner to add you on the{" "}
      {/* A router link now. The plain <a href> this replaces reloaded the whole
          app to reach a page that is already part of it. */}
      <TextLink asChild variant="inline">
        <RouterLink to="/libraries">Libraries</RouterLink>
      </TextLink>{" "}
      screen.
    </EmptyState>
  );
}

export function ErrorState({
  error,
  onRetry,
}: {
  error: unknown;
  onRetry?: () => void;
}) {
  if (error instanceof ApiError && error.status === 403) {
    return <Denied />;
  }
  const message =
    error instanceof ApiError
      ? error.message
      : error instanceof Error
        ? error.message
        : String(error);

  return (
    <Alert
      tone="danger"
      dynamic
      title="That did not work."
      actions={
        onRetry ? (
          <Button variant="danger-ghost" size="sm" onClick={onRetry}>
            Try again
          </Button>
        ) : undefined
      }
    >
      {/* The server's message verbatim: it is written for a person, and a
          generic replacement would throw away the only diagnosis available. */}
      {message}
    </Alert>
  );
}
