// `ts_headline` returns text wrapped in <mark>. Rendering server markup with
// dangerouslySetInnerHTML would trust OCR text from a scanned document, so the
// string is parsed here instead and only <mark> survives.

const MARK = /<mark>(.*?)<\/mark>/gs;

export default function Snippet({ html }: { html: string }) {
  const parts: React.ReactNode[] = [];
  let cursor = 0;

  for (const match of html.matchAll(MARK)) {
    const start = match.index ?? 0;
    if (start > cursor) parts.push(decode(html.slice(cursor, start)));
    parts.push(
      <mark key={start} className="rounded-sm bg-accent/25 px-0.5 text-inherit">
        {decode(match[1])}
      </mark>,
    );
    cursor = start + match[0].length;
  }
  if (cursor < html.length) parts.push(decode(html.slice(cursor)));

  return <>{parts}</>;
}

function decode(text: string): string {
  return text
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&amp;/g, "&");
}
