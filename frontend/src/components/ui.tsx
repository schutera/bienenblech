/**
 * The shared primitives, in cownting's visual language.
 *
 * Everything here takes its colour from the `@theme` tokens in index.css and
 * never from a literal — the one exception in the whole app is a label class's
 * own hex, which is data rather than theming and is rendered as a swatch by the
 * pages that show classes.
 */

import {
  useEffect,
  type CSSProperties,
  type ReactNode,
} from "react";

/* The shared text-input skin. Width is deliberately not baked in — callers
   append `w-full` where the field should fill its column. */
export const INPUT_CLS =
  "bg-bg border border-border rounded-xl px-3 h-10 box-border text-sm text-text " +
  "focus:outline-none focus:border-accent transition-colors";

/* Soft, rounded card on white. */
export function Card({
  children,
  className,
  accent,
  delay,
}: {
  children: ReactNode;
  className?: string;
  accent?: string;
  delay?: number;
}) {
  const style: CSSProperties = {
    animationDelay: `${delay ?? 0}ms`,
    ...(accent ? { borderTop: `3px solid ${accent}` } : {}),
  };
  return (
    <div
      className={
        "bg-surface border border-border rounded-2xl shadow-[0_1px_2px_rgba(43,42,38,0.04),0_8px_24px_-12px_rgba(43,42,38,0.10)] animate-fade-slide-in" +
        (className ? " " + className : "")
      }
      style={style}
    >
      {children}
    </div>
  );
}

/* Small, gently-tracked section label. Sentence case — friendlier than uppercase. */
export function SectionLabel({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <span
      className={
        "text-[12px] font-medium tracking-[0.02em] text-gray-tertiary" +
        (className ? " " + className : "")
      }
    >
      {children}
    </span>
  );
}

/* A keycap badge. The Label screen binds 1..9 to the first nine classes, and the
   same binding has to look the same on the class row and in the shortcut hint. */
export function Kbd({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <kbd
      className={
        "inline-grid place-items-center min-w-6 h-6 px-1.5 align-middle rounded-md " +
        "border border-border border-b-2 bg-surface-sunk " +
        "font-mono text-[11px] leading-none text-near-black" +
        (className ? " " + className : "")
      }
    >
      {children}
    </kbd>
  );
}

type ButtonVariant = "primary" | "ghost" | "danger";

function buttonClass(variant: ButtonVariant, size: "md" | "lg"): string {
  const pad = size === "lg" ? "text-base px-7 py-3.5" : "text-sm px-5 py-2.5";
  if (variant === "primary") {
    return `bg-accent text-white font-medium ${pad} rounded-full hover:opacity-90 active:scale-95 transition-all duration-150`;
  }
  if (variant === "danger") {
    return `bg-danger text-white font-medium ${pad} rounded-full hover:opacity-90 active:scale-95 transition-all duration-150`;
  }
  return `border border-border text-text ${pad} rounded-full hover:border-accent hover:text-accent-deep transition-colors duration-150`;
}

/**
 * The one button. Pass `href` to render an anchor with the same skin — used for
 * the YOLO export, which is a plain browser download rather than a fetch so the
 * zip streams straight to disk instead of through memory.
 */
export function Button({
  children,
  onClick,
  variant = "primary",
  size = "md",
  disabled,
  className,
  type = "button",
  href,
  download,
  title,
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: ButtonVariant;
  size?: "md" | "lg";
  disabled?: boolean;
  className?: string;
  type?: "button" | "submit";
  href?: string;
  download?: boolean;
  title?: string;
}) {
  const cls =
    buttonClass(variant, size) +
    (disabled ? " opacity-50 pointer-events-none" : "") +
    (className ? " " + className : "");

  if (href !== undefined) {
    return (
      <a href={href} download={download} title={title} className={"inline-block " + cls}>
        {children}
      </a>
    );
  }
  return (
    <button type={type} onClick={onClick} disabled={disabled} title={title} className={cls}>
      {children}
    </button>
  );
}

/* A square, quiet button for a single glyph or icon. `label` is required: these
   have no visible text, so without it they are invisible to a screen reader. */
export function IconButton({
  children,
  label,
  onClick,
  disabled,
  tone = "neutral",
  className,
}: {
  children: ReactNode;
  label: string;
  onClick?: () => void;
  disabled?: boolean;
  tone?: "neutral" | "danger";
  className?: string;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      onClick={onClick}
      disabled={disabled}
      className={
        "w-8 h-8 grid place-items-center rounded-lg border border-border bg-surface transition-colors " +
        (tone === "danger"
          ? "text-gray-mid hover:text-danger hover:border-danger "
          : "text-gray-mid hover:text-accent-deep hover:border-accent ") +
        (disabled ? "opacity-40 pointer-events-none " : "") +
        (className ?? "")
      }
    >
      {children}
    </button>
  );
}

/* Labelled text input. `hint` sits under the field and carries the constraint,
   so a refusal is explained before it happens rather than after. */
export function Field({
  label,
  value,
  onChange,
  type = "text",
  placeholder,
  hint,
  disabled,
  autoFocus,
  autoComplete,
  min,
  max,
  step,
  className,
  onKeyDown,
}: {
  label: ReactNode;
  value: string;
  onChange: (v: string) => void;
  type?: string;
  placeholder?: string;
  hint?: ReactNode;
  disabled?: boolean;
  autoFocus?: boolean;
  autoComplete?: string;
  min?: string;
  max?: string;
  step?: string;
  className?: string;
  onKeyDown?: (e: React.KeyboardEvent<HTMLInputElement>) => void;
}) {
  return (
    <label className={"flex flex-col gap-1.5 " + (className ?? "")}>
      <span className="text-[12px] text-gray-tertiary">{label}</span>
      <input
        className={INPUT_CLS + " w-full"}
        value={value}
        type={type}
        placeholder={placeholder}
        disabled={disabled}
        autoFocus={autoFocus}
        autoComplete={autoComplete}
        min={min}
        max={max}
        step={step}
        onKeyDown={onKeyDown}
        onChange={(e) => onChange(e.target.value)}
      />
      {hint ? <span className="text-[11px] text-gray-tertiary">{hint}</span> : null}
    </label>
  );
}

/* Labelled dropdown. */
export function Select<T extends string>({
  label,
  value,
  options,
  onChange,
  hint,
  disabled,
  className,
}: {
  label: ReactNode;
  value: T;
  options: { value: T; label: string }[];
  onChange: (v: T) => void;
  hint?: ReactNode;
  disabled?: boolean;
  className?: string;
}) {
  return (
    <label className={"flex flex-col gap-1.5 " + (className ?? "")}>
      <span className="text-[12px] text-gray-tertiary">{label}</span>
      <select
        className={INPUT_CLS + " w-full"}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value as T)}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
      {hint ? <span className="text-[11px] text-gray-tertiary">{hint}</span> : null}
    </label>
  );
}

/* A switch. State is carried by position AND by the label next to it, never by
   colour alone. */
export function Toggle({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: ReactNode;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={
        "inline-flex items-center gap-2.5 text-sm text-text transition-opacity " +
        (disabled ? "opacity-50 pointer-events-none" : "")
      }
    >
      <span
        className={
          "w-9 h-5 rounded-full border transition-colors relative shrink-0 " +
          (checked ? "bg-accent border-accent" : "bg-surface-sunk border-border")
        }
      >
        <span
          className={
            "absolute top-0.5 w-3.5 h-3.5 rounded-full bg-surface transition-all " +
            (checked ? "left-[18px]" : "left-0.5")
          }
        />
      </span>
      <span>{label}</span>
    </button>
  );
}

/* A small status badge. */
export function Pill({
  children,
  tone = "neutral",
  mono,
  className,
}: {
  children: ReactNode;
  tone?: "neutral" | "accent" | "warn" | "danger";
  mono?: boolean;
  className?: string;
}) {
  const toneCls =
    tone === "accent"
      ? "text-accent-deep border-accent/40 bg-accent-soft"
      : tone === "warn"
        ? "text-warn border-warn/40 bg-warn/10"
        : tone === "danger"
          ? "text-danger border-danger/40 bg-danger/10"
          : "text-gray-mid border-border bg-surface";
  return (
    <span
      className={
        "inline-flex items-center gap-1 text-[11px] px-2 py-0.5 rounded-full border whitespace-nowrap " +
        (mono ? "font-mono " : "") +
        toneCls +
        (className ? " " + className : "")
      }
    >
      {children}
    </span>
  );
}

/* "The box is working" — a spinning ring plus a label. */
export function Spinner({ label, className }: { label?: ReactNode; className?: string }) {
  return (
    <span
      role="status"
      aria-live="polite"
      className={"inline-flex items-center gap-2 text-accent text-sm" + (className ? " " + className : "")}
    >
      <svg className="animate-spin" width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="3" strokeOpacity="0.25" />
        <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
      </svg>
      {label ? <span>{label}</span> : null}
    </span>
  );
}

/**
 * A refusal, shown where it happened. Uses --color-danger rather than --color-warn
 * because those two are deliberately different things: an advisory and a dead end
 * must never look alike.
 */
export function ErrorNote({
  children,
  onDismiss,
  className,
}: {
  children: ReactNode;
  onDismiss?: () => void;
  className?: string;
}) {
  if (!children) return null;
  return (
    <div
      role="alert"
      className={
        "flex items-start gap-3 text-[13px] text-danger bg-danger/10 border border-danger/30 rounded-xl px-3.5 py-2.5" +
        (className ? " " + className : "")
      }
    >
      <span className="flex-1">{children}</span>
      {onDismiss ? (
        <button
          type="button"
          onClick={onDismiss}
          aria-label="Dismiss"
          className="text-danger/70 hover:text-danger leading-none"
        >
          &times;
        </button>
      ) : null}
    </div>
  );
}

/* Nothing here yet — and what to do about it. */
export function EmptyState({
  title,
  body,
  action,
  className,
}: {
  title: ReactNode;
  body?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={
        "border border-dashed border-border rounded-2xl px-6 py-10 text-center" +
        (className ? " " + className : "")
      }
    >
      <p className="font-display text-xl text-near-black leading-snug">{title}</p>
      {body ? <p className="text-[13px] text-gray-mid mt-2 max-w-md mx-auto">{body}</p> : null}
      {action ? <div className="mt-5 flex justify-center">{action}</div> : null}
    </div>
  );
}

/**
 * A modal confirm. Destructive confirms in this app must say what is lost in
 * counted nouns ("24 crops, 137 masks"), never "are you sure" — annotator hours
 * are the only thing on the box that cannot be regenerated.
 */
export function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  danger,
  busy,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: ReactNode;
  body?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onCancel]);

  if (!open) return null;
  return (
    <div
      className="fixed inset-0 z-[100] grid place-items-center px-6 bg-near-black/40"
      role="dialog"
      aria-modal="true"
      onClick={onCancel}
    >
      <div
        className="bg-surface border border-border rounded-2xl p-6 w-full max-w-md shadow-[0_20px_60px_-20px_rgba(43,42,38,0.35)]"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="font-display text-2xl font-light text-near-black leading-tight">{title}</h2>
        {body ? <div className="text-[13px] text-gray-mid mt-2.5">{body}</div> : null}
        <div className="flex items-center justify-end gap-3 mt-6">
          <Button variant="ghost" onClick={onCancel} disabled={busy}>
            {cancelLabel}
          </Button>
          <Button variant={danger ? "danger" : "primary"} onClick={onConfirm} disabled={busy}>
            {busy ? "Working" : confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}

/* A proportion bar. `value` is 0..1 and is clamped, because n_done/n_crops with
   n_crops = 0 is a real state on a frame the tiler has not finished. */
export function ProgressBar({
  value,
  tone = "accent",
  className,
  height = "h-2",
}: {
  value: number;
  tone?: "accent" | "warn";
  className?: string;
  height?: string;
}) {
  const pct = Math.max(0, Math.min(1, Number.isFinite(value) ? value : 0)) * 100;
  return (
    <div
      className={"w-full rounded-full bg-surface-sunk overflow-hidden " + height + (className ? " " + className : "")}
      role="progressbar"
      aria-valuenow={Math.round(pct)}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <div
        className={
          "h-full rounded-full transition-[width] duration-500 ease-out " +
          (tone === "warn" ? "bg-warn" : "bg-accent")
        }
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}
