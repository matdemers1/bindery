/** The throttle names a delay; saying so beats a form that just stops working. */
export function retryMessage(retryAfter: number | undefined): string {
  const seconds = retryAfter ?? 60;
  return `Too many attempts. Try again in ${
    seconds < 90 ? `${seconds} seconds` : `${Math.ceil(seconds / 60)} minutes`
  }.`;
}

/** What the callback sends people back with, in this screen's words rather than a URL's.
 *
 * The route redirects with a short code and never a sentence: a message carried in a query
 * string is a message an attacker can choose, and this screen is the one place a person decides
 * whether to trust what they are reading.
 */
export function ssoMessage(search: string): string | null {
  const parameters = new URLSearchParams(search);
  const code = parameters.get("sso");
  if (!code) return null;
  switch (code) {
    case "expired":
      return "That sign-in took too long. Try again.";
    case "refused":
      return "D3 Auth would not complete that sign-in. Try again.";
    case "no-access":
      return "That D3 Auth account has no access to this archive yet. Ask whoever runs it to give you access.";
    case "connect-first":
      return "An account with that email address already exists here. Sign in with your password, then connect D3 Auth from Settings.";
    case "already-linked":
      return "That D3 Auth account is already connected to an account here.";
    case "failed":
      // Deliberately not "ask for access": this is the archive failing, and sending somebody to
      // an administrator to ask for something they already have wastes both their time.
      return "Bindery could not complete that sign-in. That is a fault here rather than something you did — the details are in Settings → Diagnostics.";
    case "unavailable":
      return "D3 Auth is unavailable right now. You can still sign in with a password.";
    case "too-many-attempts":
      return retryMessage(Number(parameters.get("retry_after")) || undefined);
    default:
      return "That sign-in did not complete. Try again.";
  }
}
