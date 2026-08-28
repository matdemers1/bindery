import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

/**
 * One header shape for every screen.
 *
 * The headers had drifted — `text-lg font-semibold` on some, `text-xl
 * font-semibold tracking-tight` on others, differing margins — which is the
 * kind of inconsistency nobody consciously notices and everybody feels as the
 * app looking unfinished.
 *
 * The icon repeats the one in the sidebar, so a screen confirms where you are
 * rather than making you re-read a word you just clicked.
 */
export default function PageHeader({
  icon: Icon,
  title,
  children,
  actions,
}: {
  icon: LucideIcon;
  title: string;
  children?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="mb-6 flex items-start justify-between gap-6">
      <div className="min-w-0">
        <h1 className="flex items-center gap-2.5 text-xl font-semibold tracking-tight">
          <Icon size={19} className="shrink-0 text-accent" />
          {title}
        </h1>
        {children && (
          <div className="mt-1.5 max-w-2xl text-sm text-muted">{children}</div>
        )}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </header>
  );
}
