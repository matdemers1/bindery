import { useCallback, useId, useState } from "react";
import { Check, Plus, Undo2, X } from "lucide-react";

import {
  api,
  type DocumentDetail,
  type DocumentEdit,
  type TaxonomyOption,
} from "../../api";
import { useLiveQuery } from "../../live/LiveProvider";
import MetadataPanel from "../media/MetadataPanel";
import SourceBadge from "./SourceBadge";
import { Button, Alert } from "@d3cloud/ui";

/**
 * Correcting a document (REQ-188 to REQ-190).
 *
 * Two rules shape this form, and both are about not deciding things on the
 * person's behalf:
 *
 * **Only what changed is sent.** A field left alone is omitted from the
 * payload, not sent as its current value — sending everything would claim
 * every field for the person the first time they fixed one, and freeze the
 * whole document against AI review for good.
 *
 * **Clearing is explicit.** Emptying a field sends `null`, which the server
 * treats as "remove this" — distinct from omitting it. Without that there is
 * no way to delete a wrong date, only to replace it with another wrong one.
 *
 * New taxonomy is created by an explicit action, never by typing a name that
 * happens not to match (invariant 6). The counts beside each option are there
 * so a near-duplicate is obvious while choosing.
 */
export default function EditPanel({
  detail,
  onSaved,
  onCancel,
}: {
  detail: DocumentDetail;
  onSaved: (message: string) => void;
  onCancel?: () => void;
}) {
  const { document: doc } = detail;
  const sources = new Map(detail.field_sources.map((row) => [row.field_name, row]));

  const [title, setTitle] = useState(doc.title ?? "");
  const [summary, setSummary] = useState(doc.summary ?? "");
  const [documentDate, setDocumentDate] = useState(doc.document_date ?? "");
  const [correspondentId, setCorrespondentId] = useState(doc.correspondent_id ?? "");
  const [typeId, setTypeId] = useState(doc.document_type_id ?? "");
  const [newCorrespondent, setNewCorrespondent] = useState("");
  const [newType, setNewType] = useState("");

  const [tagIds, setTagIds] = useState<string[]>(detail.tags.map((t) => t.id));
  const [newTags, setNewTags] = useState<string[]>([]);
  const [tagDraft, setTagDraft] = useState("");
  const [highlight, setHighlight] = useState(0);
  // Adding and removing a chip changed the form and said nothing. Its own
  // region rather than a shared one, so it can never overwrite the count of
  // suggestions mid-announcement.
  const [chipNote, setChipNote] = useState("");
  const suggestionsId = useId();

  const [correspondents, setCorrespondents] = useState<{ id: string; name: string }[]>([]);
  const [types, setTypes] = useState<TaxonomyOption[]>([]);
  const [tagOptions, setTagOptions] = useState<TaxonomyOption[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadOptions = useCallback(async () => {
    const [people, kinds, tags] = await Promise.all([
      api.correspondents(),
      api.documentTypeOptions(),
      api.tagOptions(),
    ]);
    setCorrespondents(people.map((p) => ({ id: p.id, name: p.name })));
    setTypes(kinds);
    setTagOptions(tags);
  }, []);

  // No topics: these are the pickers' contents, read once when the form opens.
  // A correspondent appearing while you are mid-edit is not worth a refetch,
  // and the field you are typing in is the one it would fight with.
  useLiveQuery([], loadOptions);

  /** Omitted means "leave it alone"; null means "clear it". */
  function changed(): DocumentEdit {
    const edit: DocumentEdit = {};
    const trimmedTitle = title.trim();
    if (trimmedTitle !== (doc.title ?? "")) edit.title = trimmedTitle || null;
    const trimmedSummary = summary.trim();
    if (trimmedSummary !== (doc.summary ?? "")) edit.summary = trimmedSummary || null;
    if (documentDate !== (doc.document_date ?? "")) {
      edit.document_date = documentDate || null;
    }
    if (newCorrespondent.trim()) {
      edit.create_correspondent = newCorrespondent.trim();
    } else if (correspondentId !== (doc.correspondent_id ?? "")) {
      edit.correspondent_id = correspondentId || null;
    }
    if (newType.trim()) {
      edit.create_document_type = newType.trim();
    } else if (typeId !== (doc.document_type_id ?? "")) {
      edit.document_type_id = typeId || null;
    }

    const before = new Set(detail.tags.map((t) => t.id));
    const added = tagIds.filter((id) => !before.has(id));
    const removed = [...before].filter((id) => !tagIds.includes(id));
    if (added.length) edit.add_tag_ids = added;
    if (removed.length) edit.remove_tag_ids = removed;
    if (newTags.length) edit.create_tags = newTags;
    return edit;
  }

  async function save() {
    const edit = changed();
    if (Object.keys(edit).length === 0) {
      onSaved("Nothing changed.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await api.editDocument(doc.id, edit);
      const parts = [
        result.changed.length ? `${result.changed.join(", ")} updated` : "",
        result.tags_added.length ? `added ${result.tags_added.join(", ")}` : "",
        result.tags_removed.length ? `removed ${result.tags_removed.join(", ")}` : "",
      ].filter(Boolean);
      onSaved(parts.join(" · ") || "Saved.");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  async function undo() {
    setBusy(true);
    setError(null);
    try {
      await api.undo(doc.id);
      onSaved("Reverted.");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  const known = new Map(tagOptions.map((t) => [t.id, t]));
  const draft = tagDraft.trim();
  const matches = draft
    ? tagOptions.filter(
        (t) =>
          t.name.toLowerCase().includes(draft.toLowerCase()) && !tagIds.includes(t.id),
      )
    : [];
  const exact = tagOptions.some((t) => t.name.toLowerCase() === draft.toLowerCase());
  const offered = matches.slice(0, 6);
  const active = offered.length ? Math.min(highlight, offered.length - 1) : -1;

  function addExisting(tag: TaxonomyOption) {
    setTagIds((ids) => [...ids, tag.id]);
    setTagDraft("");
    setHighlight(0);
    setChipNote(`${tag.name} added`);
  }

  // The suggestion list is the safeguard against a near-duplicate (invariant
  // 6), and it was sighted-only: it appeared with no role, no announcement and
  // no way in but Tab into a group whose existence was never mentioned.
  const suggestionNote = !draft
    ? ""
    : offered.length
      ? `${offered.length} matching tag${offered.length === 1 ? "" : "s"}`
      : exact
        ? ""
        : `No tag matches ${draft}`;

  return (
    <div className="space-y-4 rounded-xl border border-edge bg-surface p-4">
      <Field label="Title" source={sources.get("title")}>
        {(id) => (
          <input
            id={id}
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="Untitled"
            className="w-full rounded-lg border border-field bg-ink/40 px-3 py-2 text-sm outline-none focus:border-accent"
          />
        )}
      </Field>

      <Field label="Date" source={sources.get("document_date")}>
        {(id) => (
          <input
            id={id}
            type="date"
            value={documentDate}
            onChange={(event) => setDocumentDate(event.target.value)}
            className="rounded-lg border border-field bg-ink/40 px-3 py-2 text-sm outline-none focus:border-accent"
          />
        )}
      </Field>

      <Field label="From" source={sources.get("correspondent_id")}>
        {(id) => (
          <div className="flex flex-wrap items-center gap-2">
            <select
              id={id}
              value={correspondentId}
              disabled={!!newCorrespondent.trim()}
              onChange={(event) => setCorrespondentId(event.target.value)}
              className="min-w-40 rounded-lg border border-field bg-ink/40 px-3 py-2 text-sm outline-none focus:border-accent disabled:opacity-40"
            >
              <option value="">— none —</option>
              {correspondents.map((person) => (
                <option key={person.id} value={person.id}>
                  {person.name}
                </option>
              ))}
            </select>
            <input
              aria-label="Create a new correspondent"
              value={newCorrespondent}
              onChange={(event) => setNewCorrespondent(event.target.value)}
              placeholder="or create a new one…"
              className="min-w-40 flex-1 rounded-lg border border-field bg-ink/40 px-3 py-2 text-sm outline-none focus:border-accent"
            />
          </div>
        )}
      </Field>

      <Field label="Type" source={sources.get("document_type_id")}>
        {(id) => (
          <div className="flex flex-wrap items-center gap-2">
            <select
              id={id}
              value={typeId}
              disabled={!!newType.trim()}
              onChange={(event) => setTypeId(event.target.value)}
              className="min-w-40 rounded-lg border border-field bg-ink/40 px-3 py-2 text-sm outline-none focus:border-accent disabled:opacity-40"
            >
              <option value="">— none —</option>
              {types.map((kind) => (
                <option key={kind.id} value={kind.id}>
                  {kind.name} ({kind.document_count})
                </option>
              ))}
            </select>
            <input
              aria-label="Create a new document type"
              value={newType}
              onChange={(event) => setNewType(event.target.value)}
              placeholder="or create a new one…"
              className="min-w-40 flex-1 rounded-lg border border-field bg-ink/40 px-3 py-2 text-sm outline-none focus:border-accent"
            />
          </div>
        )}
      </Field>

      <fieldset>
        <legend className="text-xs text-muted">Tags</legend>
        <div className="mt-1 flex flex-wrap gap-1.5">
          {tagIds.map((id) => (
            <Chip
              key={id}
              label={known.get(id)?.name ?? detail.tags.find((t) => t.id === id)?.name ?? "tag"}
              onRemove={(label) => {
                setTagIds((ids) => ids.filter((each) => each !== id));
                setChipNote(`${label} removed`);
              }}
            />
          ))}
          {newTags.map((name) => (
            <Chip
              key={`new:${name}`}
              label={name}
              isNew
              onRemove={(label) => {
                setNewTags((names) => names.filter((each) => each !== name));
                setChipNote(`${label} removed`);
              }}
            />
          ))}
        </div>
        <input
          aria-label="Find a tag, or type a new name"
          role="combobox"
          aria-expanded={offered.length > 0}
          aria-autocomplete="list"
          aria-controls={suggestionsId}
          aria-activedescendant={
            active >= 0 ? `${suggestionsId}-${offered[active].id}` : undefined
          }
          value={tagDraft}
          onChange={(event) => {
            setTagDraft(event.target.value);
            setHighlight(0);
          }}
          onKeyDown={(event) => {
            if (offered.length === 0) return;
            if (event.key === "ArrowDown") {
              event.preventDefault();
              setHighlight(Math.min(active + 1, offered.length - 1));
            } else if (event.key === "ArrowUp") {
              event.preventDefault();
              setHighlight(Math.max(active - 1, 0));
            } else if (event.key === "Enter" && active >= 0) {
              // Only ever an existing tag. Creating stays the explicit button
              // below, because a name that did not match must not become a new
              // one by reflex (invariant 6).
              event.preventDefault();
              addExisting(offered[active]);
            }
          }}
          placeholder="Find a tag, or type a new name…"
          className="mt-2 w-full rounded-lg border border-field bg-ink/40 px-3 py-2 text-sm outline-none focus:border-accent"
        />
        <p role="status" aria-live="polite" className="sr-only">
          {suggestionNote}
        </p>
        <p role="status" aria-live="polite" className="sr-only">
          {chipNote}
        </p>
        {draft && (
          <div className="mt-1 flex flex-wrap items-center gap-1.5">
            {offered.length > 0 && (
              <ul id={suggestionsId} role="listbox" className="flex flex-wrap gap-1.5">
                {offered.map((tag, index) => (
                  <li
                    key={tag.id}
                    id={`${suggestionsId}-${tag.id}`}
                    role="option"
                    aria-selected={index === active}
                    onMouseEnter={() => setHighlight(index)}
                    onClick={() => addExisting(tag)}
                    className={`cursor-pointer rounded-full border px-2.5 py-1 text-xs ${
                      index === active ? "border-accent/60" : "border-edge"
                    }`}
                  >
                    {tag.name} <span className="text-muted">({tag.document_count})</span>
                  </li>
                ))}
              </ul>
            )}
            {/* Creating is its own action, never a fallback for a name that
                did not match something (invariant 6). Outside the listbox for
                the same reason: it is not one of the things you matched. */}
            {!exact && (
              <button
                type="button"
                onClick={() => {
                  setNewTags((names) => [...new Set([...names, draft])]);
                  setTagDraft("");
                  setHighlight(0);
                  setChipNote(`${draft} added as a new tag`);
                }}
                className="flex items-center gap-1 rounded-full border border-accent/60 bg-accent/10 px-2.5 py-1 text-xs text-accent"
              >
                <Plus size={11} /> Create “{draft}”
              </button>
            )}
          </div>
        )}
      </fieldset>

      <Field label="Summary" source={sources.get("summary")}>
        {(id) => (
          <textarea
            id={id}
            value={summary}
            onChange={(event) => setSummary(event.target.value)}
            rows={3}
            className="w-full rounded-lg border border-field bg-ink/40 px-3 py-2 text-sm outline-none focus:border-accent"
          />
        )}
      </Field>

      {error && (
        <Alert tone="danger" dynamic>
          {error}
        </Alert>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <Button variant="primary" onClick={() => void save()} disabled={busy}
          icon={<Check size={14} />}
        >
          {busy ? "Saving…" : "Save"}
        </Button>
        {onCancel && (
          <Button onClick={onCancel}>
            Cancel
          </Button>
        )}
        <button
          type="button"
          onClick={() => void undo()}
          disabled={busy}
          title="Walk back the last change to this document"
          className="ml-auto flex items-center gap-1.5 text-xs text-muted underline underline-offset-2 hover:text-fg disabled:opacity-40"
        >
          <Undo2 size={12} /> Undo last change
        </button>
      </div>

      {detail.media && (
        <div className="rounded-lg border border-edge bg-ink/30 p-3">
          <p className="mb-2 text-xs text-muted">What the file says about itself</p>
          <MetadataPanel media={detail.media} />
        </div>
      )}

      <p className="text-xs text-muted">
        What you set here is yours. AI review will keep improving the fields you
        have not touched and will leave the ones you have.
      </p>
    </div>
  );
}

/**
 * The label has to be a real `<label htmlFor>`. This form is the only way to
 * correct a wrong title, date, correspondent or type, and a `<span>` sitting
 * beside a control names nothing to a screen reader or to voice control.
 *
 * `children` is a function so the generated id reaches the control the label
 * points at. The two rows that hold a picker and a "create a new one" box give
 * the id to the picker and name the box themselves.
 */
function Field({
  label,
  source,
  children,
}: {
  label: string;
  source?: { source: "ai" | "rule" | "human" | "file"; set_at: string | null };
  children: (id: string) => React.ReactNode;
}) {
  const id = useId();
  return (
    <div>
      <div className="flex items-baseline justify-between gap-2">
        <label htmlFor={id} className="text-xs text-muted">
          {label}
        </label>
        <SourceBadge source={source?.source} when={source?.set_at} />
      </div>
      <div className="mt-1">{children(id)}</div>
    </div>
  );
}

function Chip({
  label,
  onRemove,
  isNew = false,
}: {
  label: string;
  onRemove: (label: string) => void;
  isNew?: boolean;
}) {
  return (
    <span
      className={`flex items-center gap-1 rounded-full px-2.5 py-1 text-xs ${
        isNew ? "border border-accent/60 bg-accent/10 text-accent" : "border border-edge"
      }`}
    >
      {label}
      <button type="button" onClick={() => onRemove(label)} aria-label={`Remove ${label}`}>
        <X size={11} />
      </button>
    </span>
  );
}
