/**
 * Where someone is in learning the archive (REQ-146, REQ-147).
 *
 * Two of the three states are *derived* rather than stored, which is
 * deliberate: an empty archive is the same fact for everyone, and a stored flag
 * saying "onboarded" that disagrees with an empty archive would be a lie the
 * screen has to reconcile.
 *
 * The one thing that has to be remembered is whether a search has ever come
 * back with something, because the flow is not finished when a dialog is
 * dismissed — it is finished when the person has found one of their own
 * documents. That is the whole promise, and until it has happened for them
 * once, it is a claim.
 *
 * Stored per browser rather than per account. A household tool used from a
 * laptop and a phone will narrate twice, which is a small cost against a schema
 * change and a migration. Every access is guarded: a private window, cleared
 * site data, or a browser refusing storage must render the ordinary screen, not
 * an error.
 */

const KEY = "bindery.onboarding.foundSomething";

function key(userId: string): string {
  return `${KEY}.${userId}`;
}

export function hasFoundSomething(userId: string): boolean {
  try {
    return window.localStorage.getItem(key(userId)) === "yes";
  } catch {
    // Storage refused. Treat it as "not yet" — the cost is a strip that stays
    // visible, which is far better than a screen that fails to render.
    return false;
  }
}

export function rememberFoundSomething(userId: string): void {
  try {
    window.localStorage.setItem(key(userId), "yes");
  } catch {
    // Nothing to do and nothing worth telling the user.
  }
}

export function forgetOnboarding(userId: string): void {
  try {
    window.localStorage.removeItem(key(userId));
  } catch {
    /* ignore */
  }
}
