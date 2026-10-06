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

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import type { ReactNode } from 'react';
import { describe, expect, it } from 'vitest';
import { useAppInfo } from '@pmm-extensions/api';

import { server } from '../../tests/msw-server';

const APP_INFO_URL = 'http://localhost/api/extensions/app-info/';

function wrapper({ children }: { children: ReactNode }) {
  // Disable retries so an error case resolves deterministically in one tick.
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe('useAppInfo', () => {
  it('fetches and returns the rendered footer text', async () => {
    server.use(
      http.get(APP_INFO_URL, () =>
        HttpResponse.json({ footer_text: 'Footer v9.9.9', version: 'v0.0.0-test' }),
      ),
    );
    const { result } = renderHook(() => useAppInfo(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual({
      footer_text: 'Footer v9.9.9',
      version: 'v0.0.0-test',
    });
  });

  it('surfaces an error when the request fails', async () => {
    server.use(
      http.get(APP_INFO_URL, () => HttpResponse.json({ detail: 'nope' }, { status: 500 })),
    );
    const { result } = renderHook(() => useAppInfo(), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.data).toBeUndefined();
  });
});
