import { describe, expect, it } from "vitest";

import { ssoMessage } from "./messages";

describe("what the sign-in screen says about a refused SSO attempt", () => {
  it("says nothing when the person simply arrived", () => {
    expect(ssoMessage("")).toBeNull();
    expect(ssoMessage("?next=/archive")).toBeNull();
  });

  it("turns each code into words, and never repeats the URL back", () => {
    expect(ssoMessage("?sso=expired")).toMatch(/took too long/);
    expect(ssoMessage("?sso=refused")).toMatch(/ask whoever runs it/);
    expect(ssoMessage("?sso=unavailable")).toMatch(/still sign in with a password/);
    expect(ssoMessage("?sso=too-many-attempts&retry_after=42")).toBe(
      "Too many attempts. Try again in 42 seconds.",
    );
  });

  it("does not print a code it does not recognise", () => {
    // The query string is attacker-chosen; this screen owns its own wording.
    const message = ssoMessage("?sso=<script>alert(1)</script>");
    expect(message).toBe("That sign-in did not complete. Try again.");
  });
});
