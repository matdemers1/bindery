import { useEffect, useRef } from "react";

/**
 * The one definition of "something opened on top of the page".
 *
 * Five overlays had grown the same `fixed inset-0` shape independently, and
 * none of them said what they were: no `role="dialog"`, no focus moved in, no
 * focus trapped, no focus given back, no Escape. One of them is the
 * confirmation for the move that destroys plaintext, so "nothing was announced
 * and the virtual cursor stayed on the page behind" is not a cosmetic problem.
 *
 * Mount it only while the overlay is open — every call site already renders it
 * conditionally — because mount and unmount are what move focus in and hand it
 * back. Clicking the backdrop still dismisses; it is now the second way out
 * rather than the only one.
 */
const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
  'textarea:not([disabled]), video[controls], [tabindex]:not([tabindex="-1"])';

export default function Modal({
  label,
  onClose,
  className,
  backdropClassName = "fixed inset-0 z-50 flex items-center justify-center bg-ink/85 p-6",
  children,
}: {
  /** What the dialog is, spoken on open. Match the visible heading. */
  label: string;
  onClose: () => void;
  className?: string;
  backdropClassName?: string;
  children: React.ReactNode;
}) {
  const panel = useRef<HTMLDivElement>(null);
  // Read once, during the first render, rather than in the cleanup or in the
  // mount effect. Not the cleanup, because by the time the dialog closes the
  // trigger is no longer what has focus. Not the effect either: a dialog whose
  // content carries `autoFocus` — the command palette's input — has already
  // taken focus by the time effects run, so the effect would record the
  // dialog's own input as the thing to hand focus back to, and Escape would
  // leave it on `<body>`.
  const restoreTo = useRef<HTMLElement | null>(
    document.activeElement as HTMLElement | null,
  );

  useEffect(() => {
    const node = panel.current;
    const first = node?.querySelector<HTMLElement>(FOCUSABLE);
    if (first) first.focus();
    else node?.focus();
    // A dialog whose breakpoint is hiding it — the Ask sources sheet above
    // `lg` — cannot take focus, so it must not give any back either.
    if (!node?.contains(document.activeElement)) restoreTo.current = null;
    return () => restoreTo.current?.focus();
  }, []);

  // Keys are handled on the panel rather than on `window` because dialogs nest
  // here — the vault confirmation opens on top of the photo lightbox — and the
  // innermost one has to own the keystroke. Bubbling would close the dialog
  // nobody asked to close, and wrap Tab against the wrong list of stops. Focus
  // is trapped inside, so every keystroke starts in here anyway.
  //
  // Only for keys pressed in this panel's own DOM. React bubbles through the
  // component tree, so a portalled dialog rendered from inside this one — the
  // library Modal for "Move to vault" inside the photo lightbox — delivers its
  // keys here too, and Escape closed the lightbox along with the confirmation.
  function onKeyDown(event: React.KeyboardEvent) {
    if (!panel.current?.contains(event.target as Node)) return;
    if (event.key !== "Escape" && event.key !== "Tab") return;
    event.stopPropagation();
    if (event.key === "Escape") return onClose();
    if (!panel.current) return;
    const stops = [...panel.current.querySelectorAll<HTMLElement>(FOCUSABLE)];
    if (stops.length === 0) return event.preventDefault();
    const first = stops[0];
    const last = stops[stops.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && (active === first || active === panel.current)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  }

  return (
    <div className={backdropClassName} onClick={onClose}>
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-label={label}
        tabIndex={-1}
        onKeyDown={onKeyDown}
        onClick={(event) => event.stopPropagation()}
        className={className}
      >
        {children}
      </div>
    </div>
  );
}
