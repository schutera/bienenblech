/**
 * The label taxonomy: create, rename, recolor, archive.
 *
 * Two rules are stated on the page rather than assumed, because both surprise
 * people: a class is archived, never deleted, and its yolo_index is reserved
 * forever. data.yaml is keyed by yolo_index including archived classes, so a
 * model trained on last month's export still matches this month's indices.
 */

import { useEffect, useState, type FormEvent } from "react";
import type { LabelClass } from "../lib/types";
import {
  archiveClass,
  createClass,
  errorMessage,
  listClasses,
  restoreClass,
  updateClass,
} from "../lib/api";
import { useAuth } from "../lib/auth";
import {
  Button,
  Card,
  ConfirmDialog,
  EmptyState,
  ErrorNote,
  Field,
  Pill,
  SectionLabel,
  Spinner,
  Toggle,
} from "../components/ui";

/* Defaults offered to a new class. These are DATA — the colour a class is drawn
   in — not theme colours, which is why they are literals here and nowhere else.
   Chosen to stay apart from each other over a warm, brownish sheet. */
const PALETTE = [
  "#d5513a",
  "#3f6b50",
  "#3b6ea5",
  "#e58a3c",
  "#7b4ea3",
  "#2f9c95",
  "#b4523f",
  "#8a7a1f",
  "#c2456e",
];

export default function Classes() {
  const { isAdmin } = useAuth();
  const [classes, setClasses] = useState<LabelClass[] | null>(null);
  const [showArchived, setShowArchived] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [color, setColor] = useState(PALETTE[0]);
  const [description, setDescription] = useState("");
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [target, setTarget] = useState<LabelClass | null>(null);
  const [archiving, setArchiving] = useState(false);

  async function refresh(includeArchived = showArchived) {
    try {
      const cl = await listClasses(includeArchived);
      setClasses(cl);
      // Offer the next unused palette colour for the following class.
      setColor(PALETTE[cl.length % PALETTE.length]);
    } catch (e) {
      setError(errorMessage(e));
      setClasses([]);
    }
  }

  useEffect(() => {
    void refresh(showArchived);
    // `refresh` is stable enough for this page: it reads only the argument, and
    // adding it to the deps would re-fetch on every render.
  }, [showArchived]);

  async function add(e: FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setAdding(true);
    setError(null);
    try {
      await createClass({
        name: name.trim(),
        color,
        ...(description.trim() ? { description: description.trim() } : {}),
      });
      setName("");
      setDescription("");
      await refresh();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setAdding(false);
    }
  }

  async function restore(c: LabelClass) {
    setError(null);
    try {
      await restoreClass(c.class_id);
      await refresh();
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  async function confirmArchive() {
    if (!target) return;
    setArchiving(true);
    setError(null);
    try {
      await archiveClass(target.class_id);
      setTarget(null);
      await refresh();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setArchiving(false);
    }
  }

  const active = (classes ?? []).filter((c) => !c.archived);

  return (
    <div className="flex flex-col gap-6 max-w-3xl">
      <div>
        <SectionLabel>Taxonomy</SectionLabel>
        <h1 className="font-display text-3xl font-light text-near-black leading-tight mt-1">
          Label classes
        </h1>
        <p className="text-sm text-gray-mid mt-2">
          Every polygon belongs to exactly one class. The first nine active
          classes are what the digit keys 1 to 9 select on the labeling screen, so
          the order here is the order of those shortcuts.
        </p>
      </div>

      {error ? <ErrorNote onDismiss={() => setError(null)}>{error}</ErrorNote> : null}

      <Card className="p-5">
        <SectionLabel>Add a class</SectionLabel>
        <form onSubmit={add} className="mt-3 flex flex-col gap-4">
          <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto] items-end">
            <Field
              label="Name"
              value={name}
              onChange={setName}
              placeholder="bee"
              hint="The name becomes the class id and cannot change afterwards; the display name can."
            />
            <label className="flex flex-col gap-1.5">
              <span className="text-[12px] text-gray-tertiary">Colour</span>
              <input
                type="color"
                aria-label="Class colour"
                value={color}
                onChange={(e) => setColor(e.target.value)}
                className="w-14 h-10 rounded-xl border border-border bg-surface p-1 cursor-pointer"
              />
            </label>
          </div>
          <Field
            label="Description (optional)"
            value={description}
            onChange={setDescription}
            placeholder="What counts as this class, and what does not"
          />
          <div>
            <Button type="submit" disabled={adding || !name.trim()}>
              {adding ? "Adding" : "Add class"}
            </Button>
          </div>
        </form>
      </Card>

      <Card className="p-5">
        <SectionLabel>Archiving keeps the index</SectionLabel>
        <p className="text-[13px] text-gray-mid mt-2">
          Archiving a class hides it from the labeling screen but keeps everything
          else: its polygons stay, and its yolo_index stays reserved forever.
          data.yaml is keyed by that index, archived classes included, so a model
          trained on an older export keeps matching this one. Indices are never
          renumbered and never reused. An archive done by mistake is not fatal:
          an admin can restore the class, and its polygons were never touched.
        </p>
        <p className="text-[13px] text-gray-mid mt-2">
          Anyone can add a class. Renaming, recoloring, archiving and restoring
          one are admin-only, because those change how every existing polygon of
          that class is read.
        </p>
      </Card>

      <div className="flex items-center justify-between gap-4">
        <SectionLabel>
          {active.length} active class{active.length === 1 ? "" : "es"}
        </SectionLabel>
        <Toggle checked={showArchived} onChange={setShowArchived} label="Show archived" />
      </div>

      {classes === null ? (
        <Spinner label="Loading classes" />
      ) : classes.length === 0 ? (
        <EmptyState
          title="No classes yet"
          body="Add the first class above. A polygon cannot be drawn until one exists."
        />
      ) : (
        <div className="flex flex-col gap-2">
          {classes.map((c, i) => (
            <ClassRow
              key={c.class_id}
              c={c}
              shortcut={!c.archived && active.indexOf(c) >= 0 && active.indexOf(c) < 9 ? active.indexOf(c) + 1 : null}
              editing={editing === c.class_id}
              onEdit={isAdmin ? () => setEditing(editing === c.class_id ? null : c.class_id) : null}
              onArchive={isAdmin ? () => setTarget(c) : null}
              onRestore={isAdmin ? () => void restore(c) : null}
              onSaved={async () => {
                setEditing(null);
                await refresh();
              }}
              onError={setError}
              delay={i * 20}
            />
          ))}
        </div>
      )}

      <ConfirmDialog
        open={target !== null}
        title="Archive this class?"
        confirmLabel="Archive"
        busy={archiving}
        body={
          target ? (
            <>
              <p>
                <span className="text-near-black">{target.name}</span> stops being offered
                on the labeling screen. Its {target.n_masks} polygon
                {target.n_masks === 1 ? "" : "s"} stay where they are, and yolo_index{" "}
                {target.yolo_index} stays reserved so old exports keep matching.
              </p>
              <p className="mt-2">An admin can restore it from this page afterwards.</p>
            </>
          ) : null
        }
        onCancel={() => setTarget(null)}
        onConfirm={() => void confirmArchive()}
      />
    </div>
  );
}

function ClassRow({
  c,
  shortcut,
  editing,
  onEdit,
  onArchive,
  onRestore,
  onSaved,
  onError,
  delay,
}: {
  c: LabelClass;
  shortcut: number | null;
  editing: boolean;
  onEdit: (() => void) | null;
  onArchive: (() => void) | null;
  onRestore: (() => void) | null;
  onSaved: () => Promise<void>;
  onError: (m: string) => void;
  delay: number;
}) {
  const [name, setName] = useState(c.name);
  const [color, setColor] = useState(c.color);
  const [description, setDescription] = useState(c.description ?? "");
  const [saving, setSaving] = useState(false);

  // The row is keyed by class_id and survives a refresh, so the fields have to
  // be re-seeded whenever the editor closes — otherwise an abandoned edit sits
  // there waiting to be saved by accident the next time it is opened.
  useEffect(() => {
    if (editing) return;
    setName(c.name);
    setColor(c.color);
    setDescription(c.description ?? "");
  }, [editing, c.name, c.color, c.description]);

  async function save() {
    setSaving(true);
    try {
      await updateClass(c.class_id, {
        name: name.trim(),
        color,
        description: description.trim(),
      });
      await onSaved();
    } catch (e) {
      onError(errorMessage(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card className="p-4" delay={delay}>
      <div className="flex items-center gap-3 flex-wrap">
        {/* A class's own colour is data, so this swatch is the one legitimate
            literal colour in the app's chrome. */}
        <span
          className="w-4 h-4 rounded-sm border border-border shrink-0"
          style={{ backgroundColor: c.color }}
          aria-hidden="true"
        />
        <span className={"text-sm " + (c.archived ? "text-gray-tertiary line-through" : "text-near-black")}>
          {c.name}
        </span>
        {shortcut ? <Pill mono>key {shortcut}</Pill> : null}
        {c.archived ? <Pill tone="warn">archived</Pill> : null}
        <span className="font-mono text-[11px] text-gray-tertiary tabular-nums">
          index {c.yolo_index}
        </span>
        <span className="font-mono text-[11px] text-gray-tertiary tabular-nums">
          {c.n_masks.toLocaleString()} polygons
        </span>
        {/* Renaming, recoloring, archiving and restoring are admin-only on the
            server, so a null handler means the control is not rendered at all —
            an annotator is never offered a button that answers 403. Adding a
            class is open to everyone and its form is not gated. */}
        <div className="ml-auto flex items-center gap-2">
          {onEdit ? (
            <button
              type="button"
              onClick={onEdit}
              className="text-[13px] text-gray-mid hover:text-accent-deep transition-colors px-2"
            >
              {editing ? "Close" : "Edit"}
            </button>
          ) : null}
          {!c.archived && onArchive ? (
            <button
              type="button"
              onClick={onArchive}
              className="text-[13px] text-gray-tertiary hover:text-danger transition-colors px-2"
            >
              Archive
            </button>
          ) : null}
          {c.archived && onRestore ? (
            <button
              type="button"
              onClick={onRestore}
              className="text-[13px] text-accent hover:text-accent-deep transition-colors px-2"
            >
              Restore
            </button>
          ) : null}
        </div>
      </div>

      {c.description && !editing ? (
        <p className="text-[12px] text-gray-mid mt-2">{c.description}</p>
      ) : null}

      {editing ? (
        <div className="mt-4 pt-4 border-t border-border flex flex-col gap-3">
          <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto] items-end">
            <Field label="Display name" value={name} onChange={setName} />
            <label className="flex flex-col gap-1.5">
              <span className="text-[12px] text-gray-tertiary">Colour</span>
              <input
                type="color"
                aria-label={`Colour for ${c.name}`}
                value={color}
                onChange={(e) => setColor(e.target.value)}
                className="w-14 h-10 rounded-xl border border-border bg-surface p-1 cursor-pointer"
              />
            </label>
          </div>
          <Field
            label="Description"
            value={description}
            onChange={setDescription}
            hint="Shown to annotators as the definition of this class."
          />
          <div className="flex items-center gap-3">
            <Button onClick={() => void save()} disabled={saving || !name.trim()}>
              {saving ? "Saving" : "Save"}
            </Button>
            <span className="font-mono text-[11px] text-gray-tertiary">
              class id {c.class_id} — fixed
            </span>
          </div>
        </div>
      ) : null}
    </Card>
  );
}
