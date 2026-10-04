/**
 * The Bindery mark (BND-T-21.1), ported from d3cloud.io's ProductMark
 * (DI-REQ-040), concept "Dog-ear".
 *
 * Every D3 Cloud product keeps the planisphere's ring, so membership of the
 * family reads at a glance. Inside it, the same rules: ink lines, round joints
 * where lines meet, and exactly one lit star in the product's own colour.
 * Bindery's is a page with its corner folded over — the thing you dog-ear so
 * you can find it again, which is the whole job — with two lines of text on it,
 * three joints at its corners, and the lit star where the fold creases.
 *
 * The ink is `currentColor`, so the mark takes the text colour of wherever it
 * is placed, never the accent. The star is the only colour, the same in every
 * theme. `public/favicon.svg` is the same drawing at icon weight, with the ink
 * fixed per colour scheme because a favicon cannot read CSS variables.
 *
 * Two variants, and the split is deliberate. The archive holds medical records,
 * discharge papers and deeds; a mascot winking at you from the chrome of that
 * would undercut it. So `mark` is what appears in the navigation, on the
 * sign-in screens and anywhere the app is being businesslike, and `mascot`
 * appears only where warmth actually helps — an empty archive, a first run, a
 * screen with nothing on it yet. The mascot is the same page in the same ring
 * with the same star: only what is written on the page changes, a face in
 * place of the two lines of text. The silhouette is identical, so it is never
 * mistaken for a different product.
 */

const NAME = "Bindery";

// d3-allow: Bindery's lit star from d3cloud.io (DI-REQ-040) — a brand constant, the same in both themes, not a theme colour
export const BINDERY_STAR = "#E8B86D";

export function Logo({
  size = 24,
  variant = "mark",
  decorative = false,
  className = "",
}: {
  /** Rendered width and height in px. At 72 and above the lines are drawn finer, as on the site. */
  size?: number;
  variant?: "mark" | "mascot";
  /**
   * Set when the word "Bindery" is written beside the mark (or the mark is
   * ornament beside a heading), so a screen reader does not read it twice.
   * Left off, the mark stands alone and is announced as an image named
   * "Bindery".
   */
  decorative?: boolean;
  className?: string;
}) {
  // The site's two weights: heavier at icon sizes, finer at display sizes.
  const display = size >= 72;
  const w = display ? 2.2 : 3.5;
  const joint = display ? 2.6 : 3.4;
  const lit = display ? 4.4 : 5.5;
  const ink = {
    stroke: "currentColor",
    strokeWidth: w,
    strokeLinecap: "round",
    strokeLinejoin: "round",
  } as const;

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      fill="none"
      className={className}
      data-variant={variant}
      {...(decorative ? { "aria-hidden": true } : { role: "img", "aria-label": NAME })}
    >
      <circle cx="32" cy="32" r="26" {...ink} />
      {/* The page, and the fold of its top-right corner. */}
      <path d="M21 13 L37 13 L45 21 L45 51 L21 51 Z M37 13 L37 21 L45 21" {...ink} />

      {/* What is written on the page — the only part the two variants differ in. */}
      <g data-part="page-content">
        {variant === "mascot" ? (
          <>
            {/* The page looking back at you. Centred on the page's open
                area, below the star, so the face never touches the fold. */}
            <circle cx="28.5" cy="33" r={joint * 0.8} fill="currentColor" />
            <circle cx="37.5" cy="33" r={joint * 0.8} fill="currentColor" />
            <path d="M28 40 Q33 44.5 38 40" {...ink} strokeWidth={w * 0.85} />
          </>
        ) : (
          <path
            d="M26 30 L39 30 M26 38 L36 38"
            {...ink}
            strokeWidth={w * 0.75}
            strokeDasharray="2.5 3.5"
            opacity="0.8"
          />
        )}
      </g>

      <circle cx="21" cy="13" r={joint} fill="currentColor" />
      <circle cx="21" cy="51" r={joint} fill="currentColor" />
      <circle cx="45" cy="51" r={joint} fill="currentColor" />
      <circle cx="37" cy="21" r={lit} style={{ fill: BINDERY_STAR }} />
    </svg>
  );
}

/** Icon plus name, for the top of the sidebar. */
export function Wordmark({ collapsed = false }: { collapsed?: boolean }) {
  return (
    <span className="flex items-center gap-2.5 text-fg">
      {/* Decorative even when collapsed: every placement sits inside a link
          named "Bindery home", which is the name a screen reader needs. */}
      <Logo size={26} decorative className="shrink-0" />
      {!collapsed && (
        /* d3-allow: brand lockup — the wordmark is sized to the 26px mark beside it, not to the type scale. */
        <span className="text-[15px] font-semibold tracking-tight">Bindery</span>
      )}
    </span>
  );
}
