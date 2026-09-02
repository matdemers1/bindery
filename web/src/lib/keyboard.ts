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
