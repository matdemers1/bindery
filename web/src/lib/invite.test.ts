import { describe, expect, it } from "vitest";

import { invitationToken } from "./invite";

describe("invitationToken", () => {
  it("reads every shape an invitation link has had", () => {
    expect(invitationToken("/invite/abc_DEF-1")).toBe("abc_DEF-1");
    expect(invitationToken("/invite", "?token=abc")).toBe("abc");
    expect(invitationToken("/join/abc")).toBe("abc");
  });

  it("is nothing elsewhere", () => {
    expect(invitationToken("/search", "?token=abc")).toBeNull();
    expect(invitationToken("/invite/")).toBeNull();
  });
});
