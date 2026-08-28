import { Workflow } from "lucide-react";

import PageHeader from "../../components/PageHeader";
import { useCallback, useEffect, useState } from "react";

import { ApiError, api, type Library, type RuleDryRun, type RuleRecord } from "../../api";

/**
 * The rules editor (T-3.13, REQ-061).
 *
 * A rule is created disabled and cannot be enabled until it has been dry-run,
 * because a deterministic override you have not seen the effects of is not more
 * predictable than the classifier — it is less. The dry-run runs the same
 * evaluation the pipeline runs, with writes off.
 */
const FIELDS = ["correspondent", "document_type", "known_form", "title", "text", "tag"];
const OPERATORS = ["equals", "contains", "starts_with"];

export default function RulesPage({ libraries }: { libraries: Library[] }) {
  const [rules, setRules] = useState<RuleRecord[]>([]);
  const [dryRuns, setDryRuns] = useState<Record<string, RuleDryRun>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(() => api.rules().then(setRules).catch(() => {}), []);
  useEffect(() => {
    void load();
  }, [load]);

  async function dryRun(rule: RuleRecord) {
    setBusy(rule.id);
    try {
      const result = await api.dryRunRule(rule.id);
      setDryRuns((current) => ({ ...current, [rule.id]: result }));
      setNotice(
        result.matched === 0
          ? `“${rule.name}” matches nothing in the archive right now.`
          : `“${rule.name}” would affect ${result.matched} of ${result.examined} documents.`,
      );
    } finally {
      setBusy(null);
    }
  }

  async function toggle(rule: RuleRecord) {
    if (!rule.enabled && !dryRuns[rule.id]) {
      setNotice("Dry-run it first — you should see what it would do before it does it.");
      return;
    }
    setBusy(rule.id);
    try {
      await api.setRuleEnabled(rule.id, !rule.enabled);
      await load();
      setNotice(rule.enabled ? `“${rule.name}” disabled.` : `“${rule.name}” is now active.`);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="mx-auto max-w-4xl">
      <PageHeader icon={Workflow} title="Rules">
        Deterministic overrides. A rule you wrote outranks the classifier, and its
        effects are recorded as yours rather than the model&apos;s.
      </PageHeader>

      {notice && <p className="mb-4 text-sm text-accent">{notice}</p>}

      <NewRuleForm
        libraries={libraries}
        onCreated={async (name) => {
          await load();
          setNotice(`“${name}” created, disabled. Dry-run it to see what it would do.`);
        }}
        onError={setNotice}
      />

      {rules.length === 0 ? (
        <p className="mt-8 rounded-lg border border-edge p-6 text-center text-sm text-muted">
          No rules. The classifier is unassisted.
        </p>
      ) : (
        <ul className="mt-8 space-y-3">
          {rules.map((rule) => (
            <li key={rule.id} className="rounded-lg border border-edge bg-surface p-4">
              <div className="flex flex-wrap items-baseline justify-between gap-3">
                <div className="min-w-0">
                  <p className="flex items-center gap-2">
                    <span className="truncate font-medium">{rule.name}</span>
                    <span
                      className={`rounded-full border px-2 py-0.5 text-xs ${
                        rule.enabled
                          ? "border-accent/50 text-accent"
                          : "border-edge text-muted"
                      }`}
                    >
                      {rule.enabled ? "active" : "disabled"}
                    </span>
                  </p>
                  <p className="mt-1 font-mono text-xs text-muted">
                    {(rule.conditions.all ?? [])
                      .map((c) => `${c.field} ${c.operator} “${c.value}”`)
                      .join(" and ")}
                    {" → "}
                    {Object.entries(rule.actions)
                      .map(([key, value]) => `${key}: ${JSON.stringify(value)}`)
                      .join(", ")}
                  </p>
                </div>
                <div className="flex shrink-0 gap-2">
                  <button
                    onClick={() => dryRun(rule)}
                    disabled={busy === rule.id}
                    className="rounded-md border border-edge px-3 py-1.5 text-sm disabled:opacity-40"
                  >
                    Dry run
                  </button>
                  <button
                    onClick={() => toggle(rule)}
                    disabled={busy === rule.id}
                    className={`rounded-md px-3 py-1.5 text-sm disabled:opacity-40 ${
                      rule.enabled
                        ? "border border-edge"
                        : "bg-accent font-medium text-ink"
                    }`}
                  >
                    {rule.enabled ? "Disable" : "Enable"}
                  </button>
                </div>
              </div>

              {dryRuns[rule.id] && <DryRunPreview result={dryRuns[rule.id]} />}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function DryRunPreview({ result }: { result: RuleDryRun }) {
  return (
    <div className="mt-3 rounded-md border border-edge bg-ink p-3">
      <p className="mb-2 text-xs text-muted">
        Would affect {result.matched} of {result.examined} documents
        {result.truncated && " (only the most recent were examined)"}. Nothing has
        been written.
      </p>
      {result.matches.length > 0 && (
        <ul className="max-h-40 space-y-1 overflow-y-auto text-sm">
          {result.matches.slice(0, 20).map((match) => (
            <li key={match.document_id} className="truncate text-neutral-300">
              {match.title ?? "(untitled)"}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function NewRuleForm({
  libraries,
  onCreated,
  onError,
}: {
  libraries: Library[];
  onCreated: (name: string) => void;
  onError: (message: string) => void;
}) {
  const [name, setName] = useState("");
  const [field, setField] = useState(FIELDS[0]);
  const [operator, setOperator] = useState(OPERATORS[0]);
  const [value, setValue] = useState("");
  const [tags, setTags] = useState("");

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const library = libraries[0];
    if (!library) return;
    try {
      await api.createRule({
        library_id: library.id,
        name: name.trim(),
        conditions: { all: [{ field, operator, value: value.trim() }] },
        actions: {
          add_tags: tags
            .split(",")
            .map((tag) => tag.trim())
            .filter(Boolean),
        },
      });
      onCreated(name.trim());
      setName("");
      setValue("");
      setTags("");
    } catch (error) {
      onError(error instanceof ApiError ? error.message : "Could not create the rule.");
    }
  }

  return (
    <form
      onSubmit={submit}
      className="rounded-lg border border-edge bg-surface p-4"
    >
      <p className="mb-3 text-xs tracking-wide text-muted uppercase">New rule</p>
      <div className="grid gap-2 sm:grid-cols-2">
        <input
          required
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="Name — e.g. GEICO is vehicle insurance"
          className="rounded-md border border-edge bg-ink px-3 py-1.5 text-sm outline-none focus:border-accent sm:col-span-2"
        />
        <select
          value={field}
          onChange={(event) => setField(event.target.value)}
          className="rounded-md border border-edge bg-ink px-3 py-1.5 text-sm"
        >
          {FIELDS.map((option) => (
            <option key={option} value={option}>
              {option.replace(/_/g, " ")}
            </option>
          ))}
        </select>
        <select
          value={operator}
          onChange={(event) => setOperator(event.target.value)}
          className="rounded-md border border-edge bg-ink px-3 py-1.5 text-sm"
        >
          {OPERATORS.map((option) => (
            <option key={option} value={option}>
              {option.replace(/_/g, " ")}
            </option>
          ))}
        </select>
        <input
          required
          value={value}
          onChange={(event) => setValue(event.target.value)}
          placeholder="Value — e.g. GEICO"
          className="rounded-md border border-edge bg-ink px-3 py-1.5 text-sm outline-none focus:border-accent"
        />
        <input
          required
          value={tags}
          onChange={(event) => setTags(event.target.value)}
          placeholder="Add tags, comma separated"
          className="rounded-md border border-edge bg-ink px-3 py-1.5 text-sm outline-none focus:border-accent"
        />
      </div>
      <button
        type="submit"
        className="mt-3 rounded-md border border-edge px-3 py-1.5 text-sm hover:border-accent/60"
      >
        Create — disabled until you dry-run it
      </button>
    </form>
  );
}
