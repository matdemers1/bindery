import { Bot, FileText, Pencil, Workflow } from "lucide-react";

import type { FieldSourceRef } from "../../api";

/**
 * Who decided this value (REQ-064).
 *
 * The requirement has been in the register since Phase 3 and passed vacuously
 * until now, because nothing could produce a human-set value. Three sources,
 * three shapes — and the distinction matters most in the direction people
 * forget: a field *you* set looks different so that re-running AI review and
 * finding it unchanged reads as correct rather than broken.
 */
const LOOK = {
  human: { icon: Pencil, label: "you set this", cls: "text-accent" },
  rule: { icon: Workflow, label: "a rule set this", cls: "text-info" },
  ai: { icon: Bot, label: "AI set this", cls: "text-muted" },
  // A fact the file carried — an EXIF capture date — not an inference.
  file: { icon: FileText, label: "from the file", cls: "text-success" },
} as const;

export default function SourceBadge({
  source,
  when,
}: {
  source: FieldSourceRef["source"] | undefined;
  when?: string | null;
}) {
  // No row means nobody has claimed it — every document older than Phase 17.
  // Silence is the honest answer, not a guess at "ai".
  if (!source) return null;
  const look = LOOK[source];
  if (!look) return null;
  const Icon = look.icon;
  return (
    <span
      title={when ? `${look.label} on ${when.slice(0, 10)}` : look.label}
      className={`inline-flex items-center gap-1 text-11 ${look.cls}`}
    >
      <Icon size={11} aria-hidden />
      {look.label}
    </span>
  );
}
