import type { PasswordStrength } from "@d3cloud/ui";

/**
 * How strong a chosen password looks, for the meter under new-password fields.
 *
 * Advisory. The server's `validate_password` is the rule: at least 12
 * characters, not on the common-passwords list, not containing the address.
 * This mirrors the two parts that can be judged without the list, so the meter
 * never says "Strong" about something the server will refuse — and says why
 * when it will. Beyond the minimum it rewards length and separate words, which
 * is the advice the form gives: four unrelated words beat one clever word.
 */
export function judgePassword(password: string, email?: string | null): PasswordStrength | null {
  // Nothing typed, nothing to judge: an empty meter reads as a verdict.
  if (!password) return null;
  if (password.length < 12) {
    return { score: 0, label: `${12 - password.length} more` };
  }
  const local = (email ?? "").split("@")[0]?.trim().toLowerCase();
  if (local && password.toLowerCase().includes(local)) {
    return { score: 1, label: "Has your email" };
  }
  const distinct = new Set(password.toLowerCase()).size;
  if (distinct < 5) return { score: 1, label: "Too repetitive" };

  const words = password.trim().split(/[\s\-_.]+/).filter((w) => w.length >= 3).length;
  let score = 2;
  if (password.length >= 16) score += 1;
  if (words >= 3 || password.length >= 24) score += 1;
  const labels = ["", "", "Fair", "Good", "Strong"] as const;
  const clamped = Math.min(4, score) as 2 | 3 | 4;
  return { score: clamped, label: labels[clamped] };
}
