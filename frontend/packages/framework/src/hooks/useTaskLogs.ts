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

import { EventStreamContentType, fetchEventSource } from '@microsoft/fetch-event-source';
import { emitUnauthorized, getToken, refreshAccessToken } from '@pmm-extensions/api';
import { useEffect, useRef, useState } from 'react';

export type LogType = 'stdout' | 'stderr';

export type StepText = Record<LogType, string>;

/**
 * Statuses the SSE `finish` event can carry.
 *
 * Mirrors the backend's terminal `TaskHistoryStatusEnum` members, and must stay
 * exhaustive: `StatusBadge` indexes a `Record` keyed on this union, so a status
 * the backend emits but this union omits reaches the badge as an unmapped key.
 */
export type FinishStatus = 'success' | 'failed' | 'stopped' | 'lost' | 'stale' | 'unlaunchable';

export interface StreamError {
  code?: number;
  detail: unknown;
}

export type StreamStatus = 'idle' | 'connecting' | 'streaming' | 'finished' | 'error';

export interface TaskLogsState {
  textByStep: Record<string, StepText>;
  stepOrder: string[];
  streamStatus: StreamStatus;
  finishStatus?: FinishStatus;
  error?: StreamError;
}

interface IncomingLog {
  msg: string;
  step: string;
  type: LogType;
  offset: number;
}

// Thrown from onopen after a successful token refresh so onerror can return 0
// (immediate retry) with the new token.
class StreamRetriableAfterRefresh extends Error {}

// Thrown from onopen for terminal open failures so onerror re-throws and stops
// the retry loop permanently. Prevents infinite reconnects after auth failure.
class StreamFatalError extends Error {}

// Thrown from onmessage on a 409 so onerror can schedule a backoff reconnect;
// aborting instead would end fetchEventSource's retry loop.
class StreamNotStartedYet extends Error {}

/**
 * Backoff before each reconnect while the run has not started yet (409).
 *
 * One entry per consecutive retry that receives no log line, so the budget is
 * about 35 s; any accepted log line restarts it.
 */
export const NOT_STARTED_RETRY_DELAYS_MS = [1000, 2000, 4000, 8000, 10000, 10000] as const;

/**
 * SSE-backed log stream for a task history.
 *
 * Single /stream-logs/{id} endpoint covers both running and completed tasks:
 * the server streams historical log lines then emits a `finish` event with
 * terminal status. No REST fallback needed.
 *
 * An `extensions-error` with code 409 means the run is not producing output
 * yet: the stream reconnects with bounded backoff, resuming from the offsets
 * already received. A 410 (data gone) or any other code is never retried.
 *
 * Uses @microsoft/fetch-event-source so the Bearer token can be attached as a
 * header — the browser EventSource API has no headers option and could only
 * carry cookies, which are not in scope for the SPA OAuth session.
 *
 * @param taskHistoryId - Task history to stream logs for.
 * @param tail - When set, request only the last N lines per stream from the server.
 * @param attempt - Changing it starts the stream over, even for the same
 *   history and tail.
 */
export function useTaskLogs(
  taskHistoryId: number | string | undefined,
  tail?: number,
  attempt = 0,
): TaskLogsState {
  const [textByStep, setTextByStep] = useState<Record<string, StepText>>({});
  const [stepOrder, setStepOrder] = useState<string[]>([]);
  const [streamStatus, setStreamStatus] = useState<StreamStatus>('idle');
  const [finishStatus, setFinishStatus] = useState<FinishStatus | undefined>();
  const [error, setError] = useState<StreamError | undefined>();

  const offsetsRef = useRef<Record<string, number>>({});
  // Stable ref so onclose can read current streamStatus without re-registering.
  const streamStatusRef = useRef<StreamStatus>('idle');

  useEffect(() => {
    if (taskHistoryId === undefined || taskHistoryId === null || taskHistoryId === '') {
      return;
    }

    // Reset state on id, tail or attempt change
    offsetsRef.current = {};
    setTextByStep({});
    setStepOrder([]);
    setFinishStatus(undefined);
    setError(undefined);
    streamStatusRef.current = 'connecting';
    setStreamStatus('connecting');

    const ctrl = new AbortController();
    let currentToken = getToken() ?? '';
    let refreshAttempted = false;
    // Guards all state setters: set to true in cleanup so stale callbacks
    // from an aborted stream cannot write into a subsequent stream's state.
    let disposed = false;
    // Set at every intentional terminal site so onclose can distinguish a clean
    // shutdown from an unexpected connection drop.
    let terminatedCleanly = false;
    let notStartedAttempts = 0;

    const baseUrl = `/stream-logs/${encodeURIComponent(String(taskHistoryId))}`;
    const buildUrl = () => {
      const params = Object.entries(offsetsRef.current).map(
        ([key, offset]) => `${encodeURIComponent(`${key}_offset`)}=${offset}`,
      );
      if (tail !== undefined && tail > 0) {
        params.unshift(`tail=${encodeURIComponent(String(tail))}`);
      }
      return params.length ? `${baseUrl}?${params.join('&')}` : baseUrl;
    };

    fetchEventSource(baseUrl, {
      signal: ctrl.signal,
      openWhenHidden: true,

      // Custom fetch so we can inject a fresh token on every (re)connect,
      // including after a 401-triggered refresh, and resume a reconnect from
      // the offsets already received (the library re-sends its fixed input).
      fetch: (_input, init) => {
        // Preserve existing headers (fetchEventSource passes a plain object,
        // but guard against Headers/string[][] just in case).
        const headers = new Headers(init?.headers as HeadersInit | undefined);
        // Only attach the header when a token is available; an empty Bearer
        // value could confuse backend Bearer-detection logic.
        if (currentToken) {
          headers.set('Authorization', `Bearer ${currentToken}`);
        }
        return globalThis.fetch(buildUrl(), { ...init, headers });
      },

      onopen: async (response) => {
        const contentType = response.headers.get('content-type') ?? '';
        if (response.ok && contentType.includes(EventStreamContentType)) {
          // Reset so that future reconnects (e.g. after a network blip that
          // expires the token) are still allowed to attempt a refresh.
          refreshAttempted = false;
          if (!disposed) {
            streamStatusRef.current = 'streaming';
            setStreamStatus('streaming');
          }
          return;
        }
        // Auth-failure detection: HTML redirect (SPA fallback on expired session),
        // or 401 after refresh fails. Mirror the apiClient behaviour by calling
        // emitUnauthorized() so the shell can clear auth and redirect to login.
        let isAuthFailure = false;
        if (response.ok) {
          // Status 200 but wrong content-type — backend redirected to the HTML
          // login page (SPA fallback).
          isAuthFailure = true;
        } else if (response.status === 401 && !refreshAttempted) {
          refreshAttempted = true;
          const newToken = await refreshAccessToken();
          if (newToken) {
            currentToken = newToken;
            throw new StreamRetriableAfterRefresh();
          }
          isAuthFailure = true;
        } else if (response.status === 401) {
          isAuthFailure = true;
        }
        if (isAuthFailure) {
          emitUnauthorized();
        }
        // Build a content-type-aware message so "Stream open failed with status 200"
        // never appears when the SPA fallback serves index.html.
        const message = response.ok
          ? `Stream endpoint returned non-SSE content: ${contentType || 'unknown'}`
          : `Stream open failed with status ${response.status}`;
        // Set error state, abort, then throw fatal sentinel so onerror re-throws
        // it and permanently stops the retry loop.
        if (!disposed) {
          setError({ detail: { message } });
          streamStatusRef.current = 'error';
          setStreamStatus('error');
        }
        terminatedCleanly = true;
        ctrl.abort();
        throw new StreamFatalError(message);
      },

      onmessage: (ev) => {
        if (disposed) {
          return;
        }
        if (ev.data === '') {
          return; // keepalive comment line
        }

        if (ev.event === 'finish') {
          try {
            const data = JSON.parse(ev.data) as { status?: FinishStatus };
            if (data.status) {
              setFinishStatus(data.status);
            }
          } catch {
            // ignore malformed finish payload
          }
          streamStatusRef.current = 'finished';
          setStreamStatus('finished');
          terminatedCleanly = true;
          ctrl.abort();
          return;
        }

        if (ev.event === 'extensions-error') {
          let payload: StreamError;
          try {
            const parsed = JSON.parse(ev.data) as { code?: number; detail?: unknown };
            payload = { code: parsed.code, detail: parsed.detail ?? ev.data };
          } catch {
            payload = { detail: String(ev.data || 'Unknown stream error') };
          }
          if (payload.code === 409 && notStartedAttempts < NOT_STARTED_RETRY_DELAYS_MS.length) {
            streamStatusRef.current = 'connecting';
            setStreamStatus('connecting');
            throw new StreamNotStartedYet();
          }
          setError(payload);
          streamStatusRef.current = 'error';
          setStreamStatus('error');
          terminatedCleanly = true;
          ctrl.abort();
          return;
        }

        // Default message event — log line
        let payload: IncomingLog;
        try {
          payload = JSON.parse(ev.data) as IncomingLog;
        } catch {
          return;
        }
        const { msg, step, type, offset } = payload;
        // `step: ''` is a valid stepless line, not a malformed payload:
        // `useExecutionEvents` buckets it under the same empty key and the
        // viewer labels that bucket "General". Rejecting it would drop output.
        if (
          typeof msg !== 'string' ||
          typeof step !== 'string' ||
          !type ||
          typeof offset !== 'number'
        ) {
          return;
        }
        const key = `${step}_${type}`;
        const previousOffset = offsetsRef.current[key];
        if (previousOffset !== undefined && offset <= previousOffset) {
          return;
        }
        offsetsRef.current[key] = offset;
        notStartedAttempts = 0;

        setTextByStep((prev) => {
          const existing = prev[step] ?? { stdout: '', stderr: '' };
          return {
            ...prev,
            [step]: { ...existing, [type]: existing[type] + msg },
          };
        });
        setStepOrder((prev) => (prev.includes(step) ? prev : [...prev, step]));
      },

      onerror: (err): number | undefined => {
        if (err instanceof StreamRetriableAfterRefresh) {
          // Immediate retry: token was just refreshed, no need to back off.
          return 0;
        }
        if (err instanceof StreamFatalError) {
          // State already set in onopen; re-throw to stop the retry loop.
          throw err;
        }
        if (err instanceof StreamNotStartedYet) {
          const delay = NOT_STARTED_RETRY_DELAYS_MS[notStartedAttempts];
          notStartedAttempts += 1;
          return delay;
        }
        // Transient network blip — stay in 'connecting' and let fetchEventSource
        // retry with default 1 000 ms backoff.
        if (!disposed) {
          streamStatusRef.current = 'connecting';
          setStreamStatus('connecting');
        }
        return undefined;
      },

      onclose: () => {
        if (disposed || terminatedCleanly) {
          return;
        }
        // Server closed the connection without a finish/extensions-error frame.
        setError({ detail: { message: 'Task log stream connection closed.' } });
        streamStatusRef.current = 'error';
        setStreamStatus('error');
      },
    }).catch(() => {
      // fetchEventSource rejects when AbortController fires — expected on
      // unmount / id change. StreamFatalError re-thrown from onerror also
      // lands here; state is already set so we can ignore it.
    });

    return () => {
      disposed = true;
      ctrl.abort();
    };
  }, [taskHistoryId, tail, attempt]);

  return { textByStep, stepOrder, streamStatus, finishStatus, error };
}
