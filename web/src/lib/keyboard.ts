/**
 * Whether a keystroke belongs to whatever the person is typing into.
 *
 * The single-key shortcuts on Review and in the viewer are bound to `window`,
 * so they see every keystroke on the page — including the ones meant for the
 * correction form rendered inside it. Checking for `HTMLInputElement` alone
 * missed the textarea, which is how typing "a" in a summary accepted the
 * document under review. `closest` rather than a tag check so a control nested
 * inside a contenteditable region counts too (WCAG 2.1.4).
 */
export function isTypingTarget(target: EventTarget | null): boolean {
  return (
    target instanceof HTMLElement &&
    target.closest("input, textarea, select, [contenteditable]:not([contenteditable='false'])") !==
      null
  );
}

/**
 * Whether a keystroke is aimed at a control that already handles it.
 *
 * A single-key shortcut bound to `window` fires even when the focused element
 * *is* the button the shortcut duplicates, so tabbing to "Accept" and pressing
 * "a" ran the action twice — and pressing it on a chip inside the open
 * correction panel accepted the document the person was in the middle of
 * correcting. A shortcut is for reaching a control you are not on; when focus
 * is already on one, the control owns the key (WCAG 2.1.4).
 */
export function isInteractiveTarget(target: EventTarget | null): boolean {
  return (
    target instanceof HTMLElement &&
    target.closest("button, a[href], [role='button'], [role='tab'], [role='option']") !== null
  );
}

/**
 * Whether single-key shortcuts are active. WCAG 2.1.4 needs one of: an off
 * switch, a remap, or activation only while the component has focus.
 *
 * This app cannot take the third — the shortcuts exist precisely so you can
 * triage without tabbing to anything, so they are live when *nothing* is
 * focused, which is the inverse of the criterion. Scoping them away from
 * controls and from an open correction panel removed the harm; it did not
 * satisfy the criterion. This is the off switch, and it is what does.
 *
 * Per browser, not per account: it is an input preference, and the person who
 * needs it needs it on the machine they are using. Read defensively, because
 * `localStorage` throws outright in some privacy modes and a shortcut
 * preference must never be the reason a screen fails to render.
 */
const SHORTCUTS_KEY = "bindery.shortcuts";

export function shortcutsEnabled(): boolean {
  try {
    return window.localStorage.getItem(SHORTCUTS_KEY) !== "off";
  } catch {
    return true;
  }
}

export function setShortcutsEnabled(on: boolean): void {
  try {
    window.localStorage.setItem(SHORTCUTS_KEY, on ? "on" : "off");
  } catch {
    // A preference that cannot be stored is not worth an error to the person
    // who set it; the default is the safe one either way.
  }
}
