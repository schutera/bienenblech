/**
 * Admin: accounts, backup, export.
 *
 * The three things only an admin can do, on one page because they are all
 * "custody of the data" concerns. Every refusal here is server-enforced (the
 * last admin cannot be deleted, an image with masks needs ?force) and the
 * server's own sentence is what gets shown — the UI does not guess at rules it
 * does not own.
 */

import { useEffect, useState, type FormEvent } from "react";
import type { Role } from "../lib/types";
import {
  backupStatus,
  createUser,
  deleteUser,
  errorMessage,
  exportYoloUrl,
  listUsers,
  runBackup,
  setPassword,
  stats,
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
} from "../components/ui";

const ROLE_BLURB: Record<Role, string> = {
  admin: "Everything: accounts, uploads, deletions, classes, export, backup.",
  annotator: "Label crops, add classes, read everything else.",
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
          Accounts, backup and export
        </h1>
      </div>
      <Users />
      <Backup />
      <Export />
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
  const [role, setRole] = useState<Role>("annotator");
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
      setRole("annotator");
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
              { value: "annotator", label: "annotator" },
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
              immediately. The polygons they drew stay, credited to their username.
              The server refuses this if they are the last admin.
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
              The backup zips the database, flat CSV exports of the masks, classes
              and crops, and every stored frame — labels without pixels are worth
              nothing. It runs on a schedule and can be run by hand here.
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
              ? "Skipped: the store was busy, so nothing was written and nothing is wrong."
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
          Downloads a zip laid out for YOLO11-seg training: data.yaml, the crop
          images, and one polygon label file per crop. Only crops marked done are
          included — an open crop would teach the model that its unlabeled bees are
          background, so it is left out entirely. A crop marked empty ships with an
          empty label file, which is a real negative example.
        </p>
        <p className="text-[13px] text-gray-mid mt-2">
          The train/val split is deterministic and grouped by frame, never by crop:
          two tiles of the same frame on opposite sides of the split would leak, and
          the val metric would be a lie. The same seed gives the same split.
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
                : "No crop has been marked done yet, so there is nothing to export."}
            </span>
          ) : null}
        </div>
      </Card>
    </section>
  );
}
