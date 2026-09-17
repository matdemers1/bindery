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
      return "D3 Auth could not sign you in. If you have never used this archive before, ask whoever runs it for access.";
    case "unavailable":
      return "D3 Auth is unavailable right now. You can still sign in with a password.";
    case "too-many-attempts":
      return retryMessage(Number(parameters.get("retry_after")) || undefined);
    default:
      return "That sign-in did not complete. Try again.";
  }
}
