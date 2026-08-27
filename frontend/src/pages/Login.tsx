import { useState, type FormEvent } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { useAuth } from "../lib/auth";
import { errorMessage } from "../lib/api";
import { Button, ErrorNote, Field, Spinner } from "../components/ui";

/**
 * The sign-in screen. It is a real route rather than a gate around the tree, so
 * that a session expiring mid-crop lands here with the page it interrupted in
 * `location.state.from` and returns there afterwards.
 */
export default function Login() {
  const { me, loading, login } = useAuth();
  const location = useLocation();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const from = (location.state as { from?: string } | null)?.from ?? "/";

  if (loading) {
    return (
      <div className="min-h-screen grid place-items-center">
        <Spinner label="Checking your session" />
      </div>
    );
  }
  if (me) return <Navigate to={from} replace />;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await login(username.trim(), password);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <div className="min-h-screen grid place-items-center px-6">
      <form onSubmit={submit} className="w-full max-w-sm flex flex-col gap-5">
        <div className="flex items-center gap-3 justify-center mb-2">
          <div className="w-10 h-10 bg-accent grid place-items-center rounded" aria-hidden="true">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none">
              <path
                d="M12 2.5l8.2 4.75v9.5L12 21.5 3.8 16.75v-9.5z"
                stroke="white"
                strokeWidth="2"
                strokeLinejoin="round"
              />
            </svg>
          </div>
          <div>
            <div className="font-sans text-xl text-near-black leading-none">Bienenblech</div>
            <div className="text-[11px] font-mono uppercase tracking-[0.18em] text-gray-tertiary mt-1">
              polygon labeling for YOLO-seg
            </div>
          </div>
        </div>

        <div className="flex flex-col gap-3">
          <Field
            label="Username"
            value={username}
            onChange={setUsername}
            autoFocus
            autoComplete="username"
          />
          <Field
            label="Password"
            value={password}
            onChange={setPassword}
            type="password"
            autoComplete="current-password"
          />
        </div>

        {error ? <ErrorNote>{error}</ErrorNote> : null}

        <Button type="submit" disabled={busy || !username.trim() || !password} className="w-full">
          {busy ? "Signing in" : "Sign in"}
        </Button>
      </form>
    </div>
  );
}
