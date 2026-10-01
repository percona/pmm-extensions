/**
 * Copyright (C) 2026 Percona LLC
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
 * GNU Affero General Public License for more details.
 *
 * You should have received a copy of the GNU Affero General Public License
 * along with this program. If not, see <https://www.gnu.org/licenses/>.
 */

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import {
  AuthContext,
  postLogin,
  postLogout,
  postSession,
  fetchCurrentUser,
  refreshAccessToken,
  setOnRefreshed,
  setOnUnauthorized,
  setTokenProvider,
  useAuth,
  type AuthSession,
  type SPAOAuthTokenResponse,
  type User,
} from '@pmm-extensions/api';
import { useSilentRefresh } from '../hooks/useSilentRefresh';

// ── Context ─────────────────────────────────────────────────────────────
// The context and its ``useAuth`` reader live in ``@pmm-extensions/api`` so the framework
// and app packages can read the session without depending on the shell; this
// module keeps ownership of the session and token state that fills it.

// ── Provider ────────────────────────────────────────────────────────────
// Access token lives only in React state. The refresh token lives in an
// `HttpOnly` cookie set by the backend — JS never sees it and never
// persists anything to localStorage/sessionStorage.
export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  // Access token lives only in React state. A ref mirrors the latest value
  // so the token provider callback (injected into the API client) always
  // reads the current token without stale closures.
  const [accessToken, setAccessToken] = useState<string | null>(null);
  const [expiresIn, setExpiresIn] = useState<number | null>(null);
  const tokenRef = useRef<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [ready, setReady] = useState(false);

  const isAuthenticated = !!accessToken;
  const isAdmin = user?.isAdmin ?? false;

  const applyTokens = useCallback((tokens: SPAOAuthTokenResponse) => {
    tokenRef.current = tokens.access_token;
    setAccessToken(tokens.access_token);
    setExpiresIn(tokens.expires_in);
  }, []);

  const clearAuth = useCallback(() => {
    tokenRef.current = null;
    setAccessToken(null);
    setExpiresIn(null);
    setUser(null);
  }, []);

  // ── Login ─────────────────────────────────────────────────────────────
  const login = useCallback(
    async (username: string, password: string) => {
      setLoading(true);
      try {
        const tokens = await postLogin(username, password);
        applyTokens(tokens);
        const profile = await fetchCurrentUser();
        setUser(profile);
      } finally {
        setLoading(false);
      }
    },
    [applyTokens],
  );

  // ── Logout ────────────────────────────────────────────────────────────
  // Always clear local state, even if the backend call fails (network
  // error, expired token). The refresh cookie is best-effort cleared
  // server-side; a stuck cookie would be re-validated on next use.
  const logout = useCallback(async () => {
    try {
      await postLogout();
    } catch {
      // swallow — we clear local state unconditionally below
    } finally {
      clearAuth();
    }
  }, [clearAuth]);

  // ── Silent refresh timer ──────────────────────────────────────────────
  // Success updates state via the ``setOnRefreshed`` callback below — the
  // hook only triggers the shared single-flight refresh so the timer and
  // the 401 retry interceptor cannot double-rotate the refresh cookie.
  useSilentRefresh({
    accessToken,
    expiresIn,
    onFailure: clearAuth,
  });

  // ── Inject callbacks into API client ──────────────────────────────────
  useEffect(() => {
    setTokenProvider(() => tokenRef.current);
    setOnRefreshed((token, ttl) => {
      tokenRef.current = token;
      setAccessToken(token);
      setExpiresIn(ttl);
    });
    setOnUnauthorized(() => {
      clearAuth();
      if (!window.location.pathname.startsWith('/login')) {
        window.location.href = `/login?next=${encodeURIComponent(window.location.pathname)}`;
      }
    });
    return () => {
      setTokenProvider(() => null);
      setOnRefreshed(() => {});
      setOnUnauthorized(() => {});
    };
  }, [clearAuth]);

  // ── Session bootstrap (runs once on mount) ────────────────────────────
  // Access token is memory-only so it's lost on reload. Attempt a silent
  // refresh through the shared coordinator (same path as the 401 retry
  // interceptor) so a page-load concurrent with an in-flight refresh does
  // not rotate the cookie twice. If the cookie is absent or invalid the
  // call resolves to null and the user lands on /login.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    refreshAccessToken()
      .then(async (token) => {
        if (cancelled) {
          return;
        }
        if (!token) {
          // No PMM Extensions session — try ambient Grafana SSO (PMM auto-login) once
          // before giving up. The backend is authoritative: it returns 401
          // when ambient SSO is disabled or the provider isn't Grafana, so
          // this is a no-op there. Any failure resolves to a null fallback.
          const ambient = await postSession().catch(() => null);
          if (cancelled) {
            return;
          }
          if (!ambient) {
            clearAuth();
            return;
          }
          applyTokens(ambient);
        }
        // ``setOnRefreshed`` (refresh path) or ``applyTokens`` (ambient path)
        // has synced tokenRef + accessToken state, so the profile fetch will
        // carry the new Bearer token.
        const profile = await fetchCurrentUser();
        if (cancelled) {
          return;
        }
        setUser(profile);
      })
      .catch(() => {
        if (!cancelled) {
          clearAuth();
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
          setReady(true);
        }
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Context value ─────────────────────────────────────────────────────
  const value = useMemo<AuthSession>(
    () => ({
      user,
      accessToken,
      isAuthenticated,
      isAdmin,
      loading,
      ready,
      login,
      logout,
    }),
    [user, accessToken, isAuthenticated, isAdmin, loading, ready, login, logout],
  );

  return <AuthContext value={value}>{children}</AuthContext>;
}

// ── Re-exports ──────────────────────────────────────────────────────────
// Shell-local imports keep pointing at this module; the implementation is
// ``@pmm-extensions/api``'s, so the framework and app packages read the same context.
export { useAuth };
export type { AuthSession, User };
