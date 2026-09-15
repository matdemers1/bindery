import { Users } from "lucide-react";

import { useCallback, useState } from "react";

import { ApiError, api, type LibraryDetail } from "../../api";
import { useLiveQuery } from "../../live/LiveProvider";
import { Alert, Button, Input, PageHeader, Select } from "@d3cloud/ui";

/**
 * Libraries — who is in the household, and what they may do (T-7.7).
 *
 * The library is the access boundary, so this is the one screen where the
 * boundary is actually drawn. It is deliberately plain: three roles, no
 * per-document sharing, no inheritance. Anything more expressive would be
 * something nobody could reason about at a glance, and "I am fairly sure my
 * partner cannot see my medical records" is not a state anyone should be in.
 *
 * The roles are described in terms of what a person can do, not in terms of
 * permissions, because that is the question being asked.
 */
const ROLE_BLURBS: Record<string, string> = {
  owner: "Can read, change, and decide who else is in this library.",
  contributor: "Can read and change documents, but not membership.",
  reader: "Can read. Cannot change anything.",
};

const ROLES = ["owner", "contributor", "reader"] as const;

export default function LibrariesPage() {
  const [libraries, setLibraries] = useState<LibraryDetail[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [newName, setNewName] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setLibraries(await api.householdLibraries());
    } finally {
      setLoading(false);
    }
  }, []);

  // No topics: libraries change when someone on this screen changes them, and
  // every one of those paths reloads explicitly. Empty is how the sanctioned
  // hook says "once", so the suppression does not have to be repeated here.
  useLiveQuery([], load);

  async function guard(action: () => Promise<unknown>) {
    setError(null);
    try {
      await action();
      await load();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    }
  }

  return (
    <div className="mx-auto max-w-4xl space-y-5">
      <PageHeader icon={<Users size={20} strokeWidth={1.8} />} title="Libraries"
        description={
          <>
          A library is the boundary: someone either has a membership in one and sees
          everything in it, or has no membership and sees nothing in it. There is no
          per-document sharing, because nobody can hold a per-document sharing model in
          their head and still be sure of the answer.
          </>
        }
      />

      {error && (
        <Alert tone="danger" dynamic>
          {error}
        </Alert>
      )}

      {loading ? (
        <p className="text-sm text-muted">Loading…</p>
      ) : (
        <ul className="space-y-4">
          {libraries.map((library) => (
            <LibraryCard
              key={library.id}
              library={library}
              onChange={(email, role) =>
                guard(() => api.setMemberRole(library.id, email, role))
              }
            />
          ))}
        </ul>
      )}

      <section className="rounded-md border border-edge bg-surface p-4">
        <h2 className="text-sm font-semibold">New library</h2>
        <p className="mb-3 mt-1 text-sm text-muted">
          You will own it. Nothing moves into it on its own — a document has to be
          moved deliberately, and the move is audited.
        </p>
        <form
          className="flex flex-wrap gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            if (!newName.trim()) return;
            void guard(() => api.createLibrary(newName.trim(), "personal")).then(() =>
              setNewName(""),
            );
          }}
        >
          <Input
            aria-label="Name for the new library"
            value={newName}
            onChange={(event) => setNewName(event.target.value)}
            placeholder="Library name"
            className="w-64"
          />
          <Button variant="primary" type="submit" disabled={!newName.trim()}>
            Create
          </Button>
        </form>
      </section>
    </div>
  );
}

function LibraryCard({
  library,
  onChange,
}: {
  library: LibraryDetail;
  onChange: (email: string, role: string) => void;
}) {
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("reader");
  const canManage = library.your_role === "owner";

  return (
    <li className="rounded-md border border-edge bg-surface p-4">
      <div className="flex flex-wrap items-baseline gap-2">
        <h2 className="font-semibold">{library.name}</h2>
        <span className="text-xs text-muted">{library.kind}</span>
        <span className="rounded bg-edge px-1.5 py-0.5 text-11 text-fg">
          you are {library.your_role}
        </span>
      </div>
      <p className="mt-1 text-sm text-muted">{ROLE_BLURBS[library.your_role]}</p>

      <ul className="mt-3 divide-y divide-edge border-y border-edge">
        {library.members.map((member) => (
          <li key={member.user_id} className="flex items-center gap-3 py-2 text-sm">
            <span className="flex-1">{member.display_name ?? member.email}</span>
            {canManage ? (
              <Select
                aria-label={`Role for ${member.display_name ?? member.email}`}
                size="sm"
                className="w-36"
                value={member.role}
                onValueChange={(value) => onChange(member.email, value)}
                options={ROLES.map((r) => ({ value: r, label: r }))}
              />
            ) : (
              <span className="text-xs text-muted">{member.role}</span>
            )}
          </li>
        ))}
      </ul>

      {canManage ? (
        <form
          className="mt-3 flex flex-wrap gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            if (!email.trim()) return;
            onChange(email.trim(), role);
            setEmail("");
          }}
        >
          <Input
            aria-label="Email address of the person to add"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            placeholder="Add someone by email"
            className="w-64"
          />
          <Select
            aria-label="Role for the person you are adding"
            className="w-36"
            value={role}
            onValueChange={setRole}
            // Least access first: the choice someone makes without reading
            // should be the safe one.
            options={[...ROLES].reverse().map((r) => ({ value: r, label: r }))}
          />
          <Button type="submit" disabled={!email.trim()}>
            Add
          </Button>
        </form>
      ) : (
        <p className="mt-3 text-xs text-muted">
          Only an owner can change who is in this library.
        </p>
      )}
    </li>
  );
}
