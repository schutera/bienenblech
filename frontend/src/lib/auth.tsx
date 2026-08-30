/**
 * Session state for the whole app.
 *
 * The provider never gates its children itself — the route tree does, via
 * <RequireAuth>. That split exists because /login has to be a real route: the
 * 401 handler in api.ts fires from anywhere (an expired cookie surfacing on a
 * mask POST halfway through a crop), and bouncing to a URL is the only way the
 * user can get back to where they were afterwards.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { Navigate, useLocation } from "react-router-dom";
import type { Me } from "./types";
import {
  login as apiLogin,
  logout as apiLogout,
  me as apiMe,
  setUnauthorizedHandler,
} from "./api";
import { Card, Spinner } from "../components/ui";

type AuthValue = {
  me: Me | null;
  loading: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  isAdmin: boolean;
  ageEnabled: boolean;
};

const Ctx = createContext<AuthValue | null>(null);

export function useAuth(): AuthValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth must be used inside <AuthProvider>");
  return v;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    // Registered before the probe so the probe's own 401 is harmless: it just
    // sets me to null, which is what the catch below does anyway.
    setUnauthorizedHandler(() => {
      if (alive) setMe(null);
    });
    apiMe()
      .then((u) => {
        if (alive) setMe(u);
      })
      .catch(() => {
        if (alive) setMe(null);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
      setUnauthorizedHandler(null);
    };
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    setMe(await apiLogin(username, password));
  }, []);

  const logout = useCallback(async () => {
    try {
      await apiLogout();
    } finally {
      // Whatever the server said, this browser is done with the session.
      setMe(null);
    }
  }, []);

  const value = useMemo<AuthValue>(
    () => ({ me, loading, login, logout, isAdmin: me?.role === "admin",
             ageEnabled: me?.age_enabled === true }),
    [me, loading, login, logout],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

/** Route wrapper: signed-in users through, everyone else to /login. */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { me, loading } = useAuth();
  const location = useLocation();
  if (loading) {
    return (
      <div className="py-20 grid place-items-center">
        <Spinner label="Checking your session" />
      </div>
    );
  }
  if (!me) return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  return <>{children}</>;
}

/**
 * Route wrapper for admin-only pages. It renders a panel instead of redirecting
 * on purpose: a poweruser who follows a link to /admin should be told the page
 * exists and is not theirs, not silently teleported home wondering whether the
 * click registered.
 */
export function RequireAdmin({ children }: { children: ReactNode }) {
  const { isAdmin } = useAuth();
  if (isAdmin) return <>{children}</>;
  return (
    <Card className="p-6 max-w-xl">
      <h1 className="font-display text-2xl font-light text-near-black leading-tight">
        Admin only
      </h1>
      <p className="text-[13px] text-gray-mid mt-2">
        This page manages accounts, frame deletion, backup and export. Your
        poweruser role can label crops, upload frames and add classes; an admin
        can change it.
      </p>
    </Card>
  );
}
