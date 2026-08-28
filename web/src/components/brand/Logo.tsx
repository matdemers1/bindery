/**
 * The Bindery mark.
 *
 * A bound ledger: spine on the left with its two stitches, page edges on the
 * fore-edge, and a ribbon bookmark in the accent gold. Bookbinding is the right
 * metaphor — this is a thing that takes loose paper and makes it one findable
 * object — and it survives being drawn at 18px in a sidebar, which most clever
 * marks do not.
 *
 * Two variants, and the split is deliberate. The archive holds medical records,
 * discharge papers and deeds; a mascot winking at you from the chrome of that
 * would undercut it. So `mark` is what appears in the navigation and anywhere
 * the app is being businesslike, and `mascot` — the same ledger, with eyes —
 * appears only where warmth actually helps: an empty archive, a first run, a
 * screen with nothing on it yet.
 */
export function Logo({
  size = 24,
  variant = "mark",
  className = "",
}: {
  size?: number;
  variant?: "mark" | "mascot";
  className?: string;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      aria-hidden
      className={className}
    >
      {/* Cover */}
      <rect
        x="5"
        y="4"
        width="22"
        height="24"
        rx="2.5"
        className="fill-surface stroke-current"
        strokeWidth="1.7"
      />
      {/* Spine, and the stitches that make it a bound thing rather than a box */}
      <path d="M11 4V28" className="stroke-current" strokeWidth="1.7" />
      <path
        d="M7.6 11H10.4M7.6 21H10.4"
        className="stroke-current"
        strokeWidth="1.4"
        strokeLinecap="round"
        opacity="0.5"
      />

      {variant === "mascot" ? (
        <>
          {/* The same ledger, looking back at you. Empty states only — the
              face sits left of the ribbon so the two never collide. */}
          <circle cx="14.6" cy="15.5" r="1.45" className="fill-current" />
          <circle cx="18.2" cy="15.5" r="1.45" className="fill-current" />
          <path
            d="M14.5 19.3C15.3 20.4 17.5 20.4 18.3 19.3"
            className="stroke-current"
            strokeWidth="1.4"
            strokeLinecap="round"
            opacity="0.75"
          />
        </>
      ) : null}

      {/* Ribbon bookmark — the one piece of colour, so the mark still reads as
          Bindery in a monochrome row of icons. Kept to the fore-edge side: over
          the middle it cut straight through the mascot's face. */}
      <path
        d="M20.5 4H23.8V14.2L22.15 12.4L20.5 14.2V4Z"
        className="fill-accent"
      />
    </svg>
  );
}

/** Icon plus name, for the top of the sidebar. */
export function Wordmark({ collapsed = false }: { collapsed?: boolean }) {
  return (
    <span className="flex items-center gap-2.5 text-neutral-100">
      <Logo size={26} className="shrink-0" />
      {!collapsed && (
        <span className="text-[15px] font-semibold tracking-tight">Bindery</span>
      )}
    </span>
  );
}
