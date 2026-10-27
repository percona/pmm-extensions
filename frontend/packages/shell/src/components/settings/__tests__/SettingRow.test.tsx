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

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { describe, expect, it, vi } from 'vitest';

import { server } from '../../../../tests/msw-server';
import SettingRow from '../SettingRow';
import { makeSetting, makeWrapper } from './fixtures';

const PATCH_EXTENSIONS = 'http://localhost/api/extensions/admin/settings/ExtensionsSettings';
const CROSS_FIELD_MSG = 'POLL_INTERVAL, TASK_TIMEOUT: POLL_INTERVAL must not exceed TASK_TIMEOUT';
// The backend reports a broken rule once per field it spans, so each field's
// row can claim the message.
const CROSS_FIELD_DETAIL = ['POLL_INTERVAL', 'TASK_TIMEOUT'].map((field) => ({
  loc: ['body', field],
  msg: CROSS_FIELD_MSG,
  type: 'cross_field',
  ctx: { fields: ['POLL_INTERVAL', 'TASK_TIMEOUT'] },
}));

function renderRow(setting = makeSetting()) {
  return render(<SettingRow setting={setting} />, { wrapper: makeWrapper() });
}

describe('SettingRow', () => {
  it('saves an edited hot value via PATCH', async () => {
    const body = vi.fn();
    server.use(
      http.patch(PATCH_EXTENSIONS, async ({ request }) => {
        body(await request.json());
        return HttpResponse.json([]);
      }),
    );
    renderRow(makeSetting({ key: 'SYNC_REFRESH_TIME', type: 'int', value: 5 }));

    const input = screen.getByLabelText('SYNC_REFRESH_TIME');
    await userEvent.clear(input);
    await userEvent.type(input, '10');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(body).toHaveBeenCalledWith({ SYNC_REFRESH_TIME: 10 }));
  });

  it('clears the typed secret from the input after a successful save', async () => {
    server.use(http.patch(PATCH_EXTENSIONS, () => HttpResponse.json([])));
    renderRow(
      makeSetting({ key: 'API_SECRET', type: 'SecretStr', is_secret: true, value: '**********' }),
    );

    const input = screen.getByLabelText('API_SECRET');
    await userEvent.type(input, 'new-secret');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(input).toHaveValue(''));
  });

  it('surfaces a 422 message inline next to the field', async () => {
    server.use(
      http.patch(PATCH_EXTENSIONS, () =>
        HttpResponse.json(
          {
            detail: [{ loc: ['body', 'SYNC_REFRESH_TIME'], msg: 'Input should be greater than 0' }],
          },
          { status: 422 },
        ),
      ),
    );
    renderRow(makeSetting({ key: 'SYNC_REFRESH_TIME', type: 'int', value: 5 }));

    const input = screen.getByLabelText('SYNC_REFRESH_TIME');
    await userEvent.clear(input);
    await userEvent.type(input, '0');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));

    expect(await screen.findByText('Input should be greater than 0')).toBeInTheDocument();
  });

  it('surfaces a cross-field refusal inline next to the edited field', async () => {
    server.use(
      http.patch(PATCH_EXTENSIONS, () =>
        HttpResponse.json({ detail: CROSS_FIELD_DETAIL }, { status: 422 }),
      ),
    );
    renderRow(makeSetting({ key: 'TASK_TIMEOUT', type: 'int', value: 180 }));

    const input = screen.getByLabelText('TASK_TIMEOUT');
    await userEvent.clear(input);
    await userEvent.type(input, '1');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));

    expect(await screen.findByText(CROSS_FIELD_MSG)).toBeInTheDocument();
  });

  it('shows why a reset was refused instead of a bare status', async () => {
    server.use(
      http.delete(`${PATCH_EXTENSIONS}/TASK_TIMEOUT`, () =>
        HttpResponse.json({ detail: CROSS_FIELD_DETAIL }, { status: 422 }),
      ),
    );
    renderRow(makeSetting({ key: 'TASK_TIMEOUT', type: 'int', value: 30, has_override: true }));

    await userEvent.click(screen.getByRole('button', { name: 'Reset to default' }));

    expect(await screen.findByText(CROSS_FIELD_MSG)).toBeInTheDocument();
  });

  it('renders no edit affordance for NOT_OVERRIDABLE fields', () => {
    renderRow(makeSetting({ key: 'STATIC_DIR', reload: 'not_overridable' }));
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument();
    expect(screen.getByText(/Read-only/)).toBeInTheDocument();
  });

  it('shows Reset only when an override is present and DELETEs it', async () => {
    const deleted = vi.fn();
    server.use(
      http.delete(
        'http://localhost/api/extensions/admin/settings/ExtensionsSettings/SYNC_REFRESH_TIME',
        () => {
          deleted();
          return new HttpResponse(null, { status: 204 });
        },
      ),
    );
    renderRow(makeSetting({ key: 'SYNC_REFRESH_TIME', type: 'int', value: 5, has_override: true }));

    await userEvent.click(screen.getByRole('button', { name: 'Reset to default' }));
    await waitFor(() => expect(deleted).toHaveBeenCalled());
  });

  it('shows Reset for not_overridable rows when an override is present', async () => {
    const deleted = vi.fn();
    server.use(
      http.delete(
        'http://localhost/api/extensions/admin/settings/ExtensionsSettings/STATIC_DIR',
        () => {
          deleted();
          return new HttpResponse(null, { status: 204 });
        },
      ),
    );
    renderRow(
      makeSetting({
        key: 'STATIC_DIR',
        reload: 'not_overridable',
        has_override: true,
      }),
    );

    await userEvent.click(screen.getByRole('button', { name: 'Reset to default' }));
    await waitFor(() => expect(deleted).toHaveBeenCalled());
  });

  it('hides Reset when no override is present', () => {
    renderRow(
      makeSetting({ key: 'SYNC_REFRESH_TIME', type: 'int', value: 5, has_override: false }),
    );
    expect(screen.queryByRole('button', { name: 'Reset to default' })).not.toBeInTheDocument();
  });

  it('renders a disabled explanation and no Save for a not-applicable setting', () => {
    renderRow(
      makeSetting({
        key: 'AMBIENT_SESSION_SSO_ENABLED',
        type: 'bool',
        value: false,
        is_applicable: false,
      }),
    );
    expect(screen.getByText(/Not applicable with the current auth provider/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument();
  });

  it('renders an editable control for an applicable hot bool', () => {
    renderRow(
      makeSetting({
        key: 'AMBIENT_SESSION_SSO_ENABLED',
        type: 'bool',
        value: false,
        is_applicable: true,
      }),
    );
    expect(screen.queryByText(/Not applicable/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument();
  });

  it('shows a read-only note (not a duplicated value) for complex fields', () => {
    renderRow(
      makeSetting({
        key: 'NESTED_CFG',
        reload: 'not_overridable',
        is_complex: true,
        value: { a: 1 },
      }),
    );
    expect(screen.getByText(/nested submodel set via settings\.yaml/)).toBeInTheDocument();
    // The value appears once (Current column), not duplicated in the edit column.
    expect(screen.getAllByText('{"a":1}')).toHaveLength(1);
  });

  it('renders the enum option label in the Current column instead of the wire value', () => {
    renderRow(
      makeSetting({
        key: 'LOGGING',
        type: 'LogLevel',
        value: 30,
        options: [
          { label: 'WARNING', value: 30 },
          { label: 'DEBUG', value: 10 },
        ],
      }),
    );
    expect(screen.getByTestId('setting-value-LOGGING')).toHaveTextContent('WARNING');
    expect(screen.queryByText('30')).not.toBeInTheDocument();
  });

  it('truncates long values behind a "View more" modal that pretty-prints JSON', async () => {
    const value = { items: Array.from({ length: 40 }, (_, i) => `entry-${i}`) };
    renderRow(makeSetting({ key: 'BIG_LIST', reload: 'not_overridable', value }));

    const viewMore = screen.getByRole('button', { name: 'View more…' });
    await userEvent.click(viewMore);

    const dialog = await screen.findByRole('dialog');
    // Pretty-printed: indented across multiple lines inside the <pre>.
    expect(dialog.querySelector('pre')?.textContent).toContain('  "items"');
    expect(dialog.querySelector('pre')?.textContent).toContain('entry-39');
  });
});
