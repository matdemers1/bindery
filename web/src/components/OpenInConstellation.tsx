import { Button } from "@d3cloud/ui";
import { Smartphone } from "lucide-react";

/**
 * "Open in D3 Constellation" (BND-T-23.1): the same document in the native app, by a
 * `d3constellation://<host>/bindery/document/<id>?page=<n>` link. The host is this server's, so
 * the app picks the matching connection and the link never crosses to another Bindery.
 *
 * Offered only on Apple devices, the only place the app exists — iPadOS Safari calls itself a
 * Mac, so `Macintosh` covers it.
 */
export function constellationLink(
  documentId: string,
  page: number,
  host: string = window.location.host,
): string {
  return `d3constellation://${host}/bindery/document/${encodeURIComponent(documentId)}?page=${page}`;
}

export function onAppleDevice(userAgent: string = navigator.userAgent): boolean {
  return /iPhone|iPad|Macintosh/.test(userAgent);
}

function go(url: string) {
  window.location.href = url;
}

export function OpenInConstellation({
  documentId,
  page,
  open = go,
}: {
  documentId: string;
  page: number;
  /** How the link is followed; a test passes its own. */
  open?: (url: string) => void;
}) {
  if (typeof navigator === "undefined" || !onAppleDevice()) return null;
  return (
    <Button
      type="button"
      variant="secondary"
      size="sm"
      icon={<Smartphone size={14} />}
      onClick={() => open(constellationLink(documentId, page))}
    >
      Open in D3 Constellation
    </Button>
  );
}
