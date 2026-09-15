import { describe, expect, it } from "vitest";

import { judgePassword } from "./strength";

describe("judgePassword — the meter never promises what the server refuses", () => {
  it("says nothing until something is typed", () => {
    expect(judgePassword("")).toBeNull();
  });

  it("counts down to the server's twelve-character minimum", () => {
    expect(judgePassword("short")).toEqual({ score: 0, label: "7 more" });
    expect(judgePassword("elevenchars")?.score).toBe(0);
  });

  it("marks a password containing the address as weak, as the server refuses it", () => {
    expect(judgePassword("matthew-rules-the-archive", "matthew@example.com")).toEqual({
      score: 1,
      label: "Has your email",
    });
  });

  it("rewards length and separate words, which is the form's advice", () => {
    expect(judgePassword("aaaaaaaaaaaaaaaa")?.label).toBe("Too repetitive");
    expect(judgePassword("Tr0ub4dor&3xyz")?.score).toBe(2);
    expect(judgePassword("quiet harbour lantern")?.score).toBe(4);
    expect(judgePassword("correcthorsebattery")?.score).toBe(3);
  });
});
