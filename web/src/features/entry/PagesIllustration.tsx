/**
 * A bundle fanned out, with one page lifted and a line on it marked.
 *
 * The mark is `--color-mark`, the colour the viewer draws around a matched word
 * — so the picture on the sign-in page is the thing the app actually does, in
 * the colour it actually does it in. The sheet is paper-white because scans are.
 */
export function PagesIllustration({ className = "" }: { className?: string }) {
  const behind = [0, 1, 2];
  return (
    <svg viewBox="0 0 300 265" fill="none" aria-hidden="true" className={className}>
      {behind.map((i) => (
        <g
          key={i}
          transform={`translate(${30 + i * 24} ${24 + i * 13}) rotate(${-10 + i * 5})`}
          className="entry-page"
          style={{ "--entry-page-i": i } as React.CSSProperties}
        >
          <rect width="150" height="196" rx="6" className="fill-surface stroke-border" />
          {[24, 36, 48, 60, 72].map((y) => (
            <rect key={y} x="16" y={y} width="104" height="4" rx="2" className="fill-border" />
          ))}
        </g>
      ))}
      <g transform="translate(102 63) rotate(5)" className="entry-page entry-page--lifted">
        <rect width="150" height="196" rx="6" className="entry-paper" />
        {[22, 34, 46, 58].map((y) => (
          <rect key={y} x="16" y={y} width={y === 34 ? 96 : 118} height="4.5" rx="2.25" className="entry-ink" />
        ))}
        <rect x="12" y="80" width="126" height="20" rx="4" className="entry-mark stroke-mark" strokeWidth="2.2" />
        <rect x="16" y="87" width="80" height="5.5" rx="2.75" className="fill-mark" opacity="0.85" />
        {[112, 124, 136, 148, 160].map((y) => (
          <rect key={y} x="16" y={y} width={y % 24 === 0 ? 84 : 112} height="4.5" rx="2.25" className="entry-ink" />
        ))}
        <text x="136" y="186" textAnchor="end" className="fill-fg-faint font-mono" fontSize="8">
          p. 47 / 100
        </text>
      </g>
    </svg>
  );
}
