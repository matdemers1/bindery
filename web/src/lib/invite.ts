/**
 * The invitation token in a link, from any of the shapes one has had: `/invite/<token>` (what an
 * administrator copies now, and what D3 Constellation recognises when one is pasted —
 * BND-T-23.2), `/invite?token=<token>`, and `/join/<token>` (every link sent before).
 */
export function invitationToken(pathname: string, search: string = ""): string | null {
  for (const prefix of ["/invite/", "/join/"]) {
    if (pathname.startsWith(prefix) && pathname.length > prefix.length) {
      return decodeURIComponent(pathname.slice(prefix.length));
    }
  }
  if (pathname === "/invite" || pathname === "/invite/") {
    return new URLSearchParams(search).get("token");
  }
  return null;
}
