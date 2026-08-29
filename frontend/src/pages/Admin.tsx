/**
 * Admin: accounts, class curation, backup, export.
 *
 * The things only an admin can do, on one page because they are all "custody
 * of the data" concerns. Every refusal here is server-enforced (the last admin
 * cannot be deleted, an image with masks needs ?force) and the server's own
 * sentence is what gets shown — the UI does not guess at rules it does not own.
 *
 * Roles, amending SPEC section 2: the non-admin role is `poweruser`, not the
 * SPEC's original name — same rights plus uploading frames, which is why
 * POST /api/images is open to every signed-in account while frame deletion
 * stays admin-only. Still exactly two roles; a boot migration renames the old
 * rows, so this page never sees the old value.
 */

import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import type { AgeStats, LabelClass, Role } from "../lib/types";
import {
  ageExportUrl,
  ageStats,
  archiveClass,
  backupStatus,
  createUser,
  deleteUser,
  errorMessage,
  exportYoloUrl,
  listClasses,
  listUsers,
  restoreClass,
  runBackup,
  setPassword,
  stats,
  updateClass,
  type BackupRun,
  type BackupStatus,
  type UserRow,
} from "../lib/api";
import { useAuth } from "../lib/auth";
import {
  Button,
  Card,
  ConfirmDialog,
  ErrorNote,
  Field,
  Pill,
  SectionLabel,
  Select,
  Spinner,
  Toggle,
} from "../components/ui";

const ROLE_BLURB: Record<Role, string> = {
  admin: "Everything: accounts, frame deletion, class curation, export, backup.",
  poweruser: "Label crops, add classes, upload frames, read everything else.",
};

function fmtDate(iso: string | null): string {
  if (!iso) return "never";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function fmtBytes(n: number | null): string {
  if (n === null || !Number.isFinite(n)) return "-";
  if (n < 1024) return `${n} B`;
  if (n < 1_048_576) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1_048_576).toFixed(1)} MB`;
}

function runTone(status: string): "accent" | "warn" | "danger" | "neutral" {
  if (status === "ok") return "accent";
  if (status === "failed") return "danger";
  if (status === "skipped") return "warn";
  return "neutral";
}

export default function Admin() {
  return (
    <div className="flex flex-col gap-8 max-w-3xl">
      <div>
        <SectionLabel>Administration</SectionLabel>
        <h1 className="font-display text-3xl font-light text-near-black leading-tight mt-1">
          Accounts, classes, backup and export
        </h1>
      </div>
      <Users />
      <Classes />
      <Backup />
      <Export />
      <AgeExport />
    </div>
  );
}

// ------------------------------------------------------------------- accounts

function Users() {
  const { me } = useAuth();
  const [users, setUsers] = useState<UserRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [username, setUsername] = useState("");
  const [password, setPw] = useState("");
  const [role, setRole] = useState<Role>("poweruser");
  const [adding, setAdding] = useState(false);
  const [target, setTarget] = useState<UserRow | null>(null);
  const [deleting, setDeleting] = useState(false);

  async function refresh() {
    try {
      setUsers(await listUsers());
    } catch (e) {
      setError(errorMessage(e));
      setUsers([]);
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  async function add(e: FormEvent) {
    e.preventDefault();
    setAdding(true);
    setError(null);
    try {
      await createUser(username.trim(), password, role);
      setUsername("");
      setPw("");
      setRole("poweruser");
      await refresh();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setAdding(false);
    }
  }

  async function confirmDelete() {
    if (!target) return;
    setDeleting(true);
    setError(null);
    try {
      await deleteUser(target.username);
      setTarget(null);
      await refresh();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setDeleting(false);
    }
  }

  return (
    <section className="flex flex-col gap-4">
      <SectionLabel>Accounts</SectionLabel>
      {error ? <ErrorNote onDismiss={() => setError(null)}>{error}</ErrorNote> : null}

      <Card className="p-5">
        <form onSubmit={add} className="flex flex-col gap-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Username" value={username} onChange={setUsername} autoComplete="off" />
            <Field
              label="Password"
              value={password}
              onChange={setPw}
              type="password"
              autoComplete="new-password"
            />
          </div>
          <Select
            label="Role"
            value={role}
            onChange={setRole}
            options={[
              { value: "poweruser", label: "poweruser" },
              { value: "admin", label: "admin" },
            ]}
            hint={ROLE_BLURB[role]}
            className="max-w-sm"
          />
          <div>
            <Button type="submit" disabled={adding || !username.trim() || !password}>
              {adding ? "Adding" : "Add account"}
            </Button>
          </div>
        </form>
      </Card>

      {users === null ? (
        <Spinner label="Loading accounts" />
      ) : (
        <div className="flex flex-col gap-2">
          {users.map((u) => (
            <UserCard
              key={u.username}
              u={u}
              isMe={u.username === me?.username}
              onDelete={() => setTarget(u)}
              onError={setError}
            />
          ))}
        </div>
      )}

      <ConfirmDialog
        open={target !== null}
        danger
        busy={deleting}
        confirmLabel="Delete the account"
        title="Delete this account?"
        body={
          target ? (
            <p>
              <span className="text-near-black">{target.username}</span> loses access
              immediately; their polygons stay, credited to their username. The
              last admin cannot be deleted.
            </p>
          ) : null
        }
        onCancel={() => setTarget(null)}
        onConfirm={() => void confirmDelete()}
      />
    </section>
  );
}

function UserCard({
  u,
  isMe,
  onDelete,
  onError,
}: {
  u: UserRow;
  isMe: boolean;
  onDelete: () => void;
  onError: (m: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [pw, setPw] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  async function save() {
    if (!pw) return;
    setSaving(true);
    setSaved(false);
    try {
      await setPassword(u.username, pw);
      setPw("");
      setOpen(false);
      setSaved(true);
    } catch (e) {
      onError(errorMessage(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card className="p-4">
      <div className="flex items-center gap-3 flex-wrap">
        <span className="text-sm text-near-black">{u.username}</span>
        <Pill tone={u.role === "admin" ? "accent" : "neutral"} mono>
          {u.role}
        </Pill>
        {isMe ? <span className="text-[11px] text-gray-tertiary">(you)</span> : null}
        {u.created_at ? (
          <span className="text-[11px] text-gray-tertiary">added {fmtDate(u.created_at)}</span>
        ) : null}
        {saved ? <span className="text-[12px] text-accent-deep">Password changed</span> : null}
        <div className="ml-auto flex items-center gap-2">
          <button
            type="button"
            onClick={() => setOpen((o) => !o)}
            className="text-[13px] text-gray-mid hover:text-accent-deep transition-colors px-2"
          >
            {open ? "Close" : "Set password"}
          </button>
          <button
            type="button"
            onClick={onDelete}
            className="text-[13px] text-gray-tertiary hover:text-danger transition-colors px-2"
          >
            Delete
          </button>
        </div>
      </div>
      {open ? (
        <div className="mt-4 pt-4 border-t border-border flex items-end gap-3 flex-wrap">
          <Field
            label="New password"
            value={pw}
            onChange={setPw}
            type="password"
            autoComplete="new-password"
            className="max-w-xs w-full"
          />
          <Button variant="ghost" onClick={() => void save()} disabled={saving || !pw}>
            {saving ? "Saving" : "Set password"}
          </Button>
        </div>
      ) : null}
    </Card>
  );
}

// -------------------------------------------------------------------- classes

/**
 * Class curation, transplanted from the former /classes page when the
 * navigation collapsed (its route redirects home). Two rules are stated on the
 * page rather than assumed, because both surprise people: a class is archived,
 * never deleted, and its yolo_index is reserved forever. data.yaml is keyed by
 * yolo_index including archived classes, so a model trained on last month's
 * export still matches this month's indices.
 *
 * There is deliberately no create form here: creating a class is open to every
 * signed-in account and lives inline on the labeling screen, where the need
 * for a new class actually appears. This section is curation only — the
 * admin-gated verbs (rename, recolor, re-describe, archive, restore).
 */

function Classes() {
  const [classes, setClasses] = useState<LabelClass[] | null>(null);
  const [showArchived, setShowArchived] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [target, setTarget] = useState<LabelClass | null>(null);
  const [archiving, setArchiving] = useState(false);

  async function refresh(includeArchived = showArchived) {
    try {
      setClasses(await listClasses(includeArchived));
    } catch (e) {
      setError(errorMessage(e));
      setClasses([]);
    }
  }

  useEffect(() => {
    void refresh(showArchived);
    // `refresh` is stable enough for this section: it reads only the argument,
    // and adding it to the deps would re-fetch on every render.
  }, [showArchived]);

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
    <section className="flex flex-col gap-4">
      <SectionLabel>Classes</SectionLabel>
      {error ? <ErrorNote onDismiss={() => setError(null)}>{error}</ErrorNote> : null}

      <Card className="p-5">
        <SectionLabel>Archiving keeps the index</SectionLabel>
        <p className="text-[13px] text-gray-mid mt-2">
          Archiving only hides a class from the labeling screen: its polygons
          stay, and its yolo_index stays reserved forever — never renumbered,
          never reused — so data.yaml keeps matching older exports. A mistaken
          archive can be restored here, polygons untouched.
        </p>
        <p className="text-[13px] text-gray-mid mt-2">
          Any signed-in account can add a class, on the labeling screen where the
          need appears. Renaming, recoloring, archiving and restoring are
          admin-only: they change how every existing polygon of that class is read.
          On the labeling screen, keys 1 to 9 pick the first nine active classes
          in this order.
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
        <Card className="p-5">
          <p className="text-[13px] text-gray-mid">
            No classes yet. Add the first on the labeling screen — a polygon
            cannot be drawn without one.
          </p>
        </Card>
      ) : (
        <div className="flex flex-col gap-2">
          {classes.map((c, i) => (
            <ClassRow
              key={c.class_id}
              c={c}
              shortcut={
                !c.archived && active.indexOf(c) >= 0 && active.indexOf(c) < 9
                  ? active.indexOf(c) + 1
                  : null
              }
              editing={editing === c.class_id}
              onEdit={() => setEditing(editing === c.class_id ? null : c.class_id)}
              onArchive={() => setTarget(c)}
              onRestore={() => void restore(c)}
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
                <span className="text-near-black">{target.name}</span> is no longer offered
                on the labeling screen. Its {target.n_masks} polygon
                {target.n_masks === 1 ? "" : "s"} stay, and yolo_index{" "}
                {target.yolo_index} stays reserved so old exports keep matching.
              </p>
              <p className="mt-2">An admin can restore it here later.</p>
            </>
          ) : null
        }
        onCancel={() => setTarget(null)}
        onConfirm={() => void confirmArchive()}
      />
    </section>
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
  onEdit: () => void;
  onArchive: () => void;
  onRestore: () => void;
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
        {/* No per-control gating here: the whole page sits behind RequireAdmin,
            so every verb the server allows an admin is simply offered. */}
        <div className="ml-auto flex items-center gap-2">
          <button
            type="button"
            onClick={onEdit}
            className="text-[13px] text-gray-mid hover:text-accent-deep transition-colors px-2"
          >
            {editing ? "Close" : "Edit"}
          </button>
          {!c.archived ? (
            <button
              type="button"
              onClick={onArchive}
              className="text-[13px] text-gray-tertiary hover:text-danger transition-colors px-2"
            >
              Archive
            </button>
          ) : (
            <button
              type="button"
              onClick={onRestore}
              className="text-[13px] text-accent hover:text-accent-deep transition-colors px-2"
            >
              Restore
            </button>
          )}
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
            hint="Shown on the labeling screen as the class's definition."
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

// --------------------------------------------------------------------- backup

function Backup() {
  const [status, setStatus] = useState<BackupStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [lastResult, setLastResult] = useState<BackupRun | null>(null);

  async function refresh() {
    try {
      setStatus(await backupStatus());
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  async function run() {
    setRunning(true);
    setError(null);
    try {
      setLastResult(await runBackup());
      await refresh();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setRunning(false);
    }
  }

  return (
    <section className="flex flex-col gap-4">
      <SectionLabel>Backup</SectionLabel>
      {error ? <ErrorNote onDismiss={() => setError(null)}>{error}</ErrorNote> : null}

      <Card className="p-5">
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div>
            <p className="text-[13px] text-gray-mid">
              Zips the database, CSV exports of masks, classes and crops, and
              every stored frame — labels without pixels are worth nothing. Runs
              on a schedule, or by hand here.
            </p>
            <div className="flex items-center gap-3 mt-3 flex-wrap text-[13px]">
              <Pill tone={status?.enabled ? "accent" : "warn"}>
                {status?.enabled ? "scheduled" : "schedule off"}
              </Pill>
              <span className="text-gray-mid">
                Last run {fmtDate(status?.last_run?.finished_at ?? status?.last_run?.started_at ?? null)}
              </span>
              {status?.next_due ? (
                <span className="text-gray-tertiary">next due {fmtDate(status.next_due)}</span>
              ) : null}
              {status && status.webhook_configured === false ? (
                <span className="text-gray-tertiary">
                  no webhook configured — zips are kept locally
                </span>
              ) : null}
            </div>
            {status?.error ? (
              <p className="text-[12px] text-warn mt-2">
                The status probe could not read the store: {status.error}. The
                schedule is unaffected.
              </p>
            ) : null}
          </div>
          <Button onClick={() => void run()} disabled={running}>
            {running ? "Running" : "Run backup now"}
          </Button>
        </div>

        {lastResult ? (
          <p className="text-[12px] text-gray-mid mt-3">
            {lastResult.status === "skipped"
              ? "Skipped: the store was busy; nothing was written, nothing is wrong."
              : lastResult.status === "failed"
                ? `Failed: ${lastResult.error ?? "no detail given"}`
                : `Wrote ${fmtBytes(lastResult.bytes)} to ${lastResult.zip_path ?? "the backups directory"}.`}
          </p>
        ) : null}

        {status?.runs?.length ? (
          <div className="mt-4 pt-4 border-t border-border flex flex-col gap-1.5">
            {status.runs.slice(0, 8).map((r) => (
              <div key={r.run_id} className="flex items-center gap-3 flex-wrap text-[12px]">
                <Pill tone={runTone(r.status)}>{r.status}</Pill>
                <span className="text-gray-mid">{fmtDate(r.finished_at ?? r.started_at)}</span>
                <span className="text-gray-tertiary font-mono">{r.trigger}</span>
                <span className="text-gray-tertiary font-mono tabular-nums">{fmtBytes(r.bytes)}</span>
                {r.delivery ? <span className="text-gray-tertiary">{r.delivery}</span> : null}
                {r.error ? <span className="text-danger flex-1 min-w-0 truncate">{r.error}</span> : null}
              </div>
            ))}
          </div>
        ) : null}
      </Card>
    </section>
  );
}

// --------------------------------------------------------------------- export

function Export() {
  const [valFraction, setValFraction] = useState("0.2");
  const [seed, setSeed] = useState("0");
  const [nDone, setNDone] = useState<number | null>(null);

  // The download is a plain browser navigation, so a refusal from the server
  // would land as a raw JSON page rather than in this panel. The one refusal we
  // can predict — an export with no done crop — is therefore checked here first.
  useEffect(() => {
    stats()
      .then((s) => setNDone(s.n_done))
      .catch(() => setNDone(null));
  }, []);

  const val = Number(valFraction);
  const seedNum = Number(seed);
  const valOk = Number.isFinite(val) && val >= 0 && val < 1;
  const seedOk = Number.isFinite(seedNum) && Number.isInteger(seedNum);
  const hasData = nDone === null || nDone > 0;
  const ok = valOk && seedOk && hasData;

  return (
    <section className="flex flex-col gap-4">
      <SectionLabel>YOLO-seg export</SectionLabel>
      <Card className="p-5">
        <p className="text-[13px] text-gray-mid">
          A zip laid out for YOLO11-seg training: data.yaml, the crop images, and
          one polygon label file per crop. Only crops marked done are included —
          an open crop would teach the model its unlabeled instances are
          background. A crop marked empty ships an empty label file: a real
          negative example.
        </p>
        <p className="text-[13px] text-gray-mid mt-2">
          The train/val split is deterministic and grouped by frame, never by
          crop — tiles of one frame on both sides would leak and make the val
          metric a lie.
        </p>

        <div className="grid gap-3 sm:grid-cols-2 mt-4 max-w-md">
          <Field
            label="Validation fraction"
            value={valFraction}
            onChange={setValFraction}
            type="number"
            min="0"
            max="0.9"
            step="0.05"
            hint={valOk ? "Share of frames held out." : "Must be between 0 and 1."}
          />
          <Field
            label="Seed"
            value={seed}
            onChange={setSeed}
            type="number"
            step="1"
            hint={seedOk ? "Same seed, same split." : "Must be a whole number."}
          />
        </div>

        <div className="mt-5 flex items-center gap-4 flex-wrap">
          <Button href={ok ? exportYoloUrl(val, seedNum) : undefined} download disabled={!ok}>
            Download the dataset
          </Button>
          {nDone !== null ? (
            <span className="text-[13px] text-gray-tertiary">
              {nDone > 0
                ? `${nDone.toLocaleString()} crop${nDone === 1 ? "" : "s"} will be included.`
                : "No crops marked done yet — nothing to export."}
            </span>
          ) : null}
        </div>
      </Card>
    </section>
  );
}

// ----------------------------------------------------------------- age export

/**
 * The Age tool's admin surface, kept deliberately thin: upload, reopen and
 * delete live on the Age overview next to the samples they act on. What
 * belongs here is what mirrors this page's other custody concerns — the
 * dataset download, and the count of flagged samples sitting outside it.
 */
function AgeExport() {
  const [s, setS] = useState<AgeStats | null>(null);

  // Same reasoning as the YOLO export above: the download is a plain browser
  // navigation, so the one predictable refusal — nothing annotated yet — is
  // checked here rather than surfacing as a raw JSON page.
  useEffect(() => {
    ageStats()
      .then(setS)
      .catch(() => setS(null));
  }, []);

  const done = s?.done ?? null;
  const ok = done === null || done > 0;

  return (
    <section className="flex flex-col gap-4">
      <SectionLabel>Age export</SectionLabel>
      <Card className="p-5">
        <p className="text-[13px] text-gray-mid">
          A zip of every annotated bee photo plus labels.csv — sample id,
          filename, age in days (28 means 28+), annotator, time. Flagged
          samples are excluded: a flag says the photo cannot be judged, so it
          has no label to ship.
        </p>
        <div className="mt-4 flex items-center gap-4 flex-wrap">
          <Button href={ok ? ageExportUrl() : undefined} download disabled={!ok}>
            Download the age dataset
          </Button>
          {done !== null ? (
            <span className="text-[13px] text-gray-tertiary">
              {done > 0
                ? `${done.toLocaleString()} annotated sample${done === 1 ? "" : "s"} will be included.`
                : "Nothing annotated yet — nothing to export."}
            </span>
          ) : null}
        </div>
        {s && s.flagged > 0 ? (
          <p className="text-[12px] text-gray-mid mt-3 flex items-center gap-2 flex-wrap">
            <Pill tone="warn">{s.flagged} flagged</Pill>
            <span>
              outside the export — review them on the{" "}
              <Link to="/age" className="text-accent hover:text-accent-deep transition-colors">
                Age overview
              </Link>
              .
            </span>
          </p>
        ) : null}
      </Card>
    </section>
  );
}
