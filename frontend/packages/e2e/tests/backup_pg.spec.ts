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

import { test, expect, type Page } from '@playwright/test';
import { fulfillEnabledApps, isEnabledAppsPath } from './mockEnabledApps';

const APP_ROUTE = '/backups/postgresql';

const DISPLAY_NAME = 'PostgreSQL Backups';

const MOCK_TASK_NAME = 'pgbackrest-task';
const NEW_TASK_NAME = 'e2e-smoke-pg-backup';
const NEW_TASK_HOSTNAME = 'e2e-pg-host';

const MOCK_TOKEN = { access_token: 'smoke-test-token', expires_in: 3600 };

const MOCK_USER = {
  id: '00000000-0000-0000-0000-000000000001',
  username: 'smoke',
  email: 'smoke@percona.com',
  firstName: 'Smoke',
  lastName: 'Test',
  // Admin: the app pages under test render their create / execute / delete
  // controls only for a session that may mutate.
  isAdmin: true,
};

const MOCK_SCHEMA = {
  name: 'backup_pg',
  display_name: DISPLAY_NAME,
  forms: [
    {
      title: 'Task',
      fields: [
        { type: 'string', name: 'task_name', label: 'Task Name', required: true },
        { type: 'string', name: 'hostname', label: 'Execution Host', required: true },
      ],
    },
  ],
  list_view: {
    columns: [
      { key: 'name', label: 'Name', sortable: true },
      { key: 'status', label: 'Status', format: 'status' },
      { key: 'hostname', label: 'Execution Host' },
      { key: 'created_at', label: 'Created', format: 'relative' },
      { key: 'created_by', label: 'Created By' },
    ],
    default_sort: 'name',
  },
};

type TaskRow = {
  name: string;
  hostname: string;
  status: string | null;
  created_at: string;
  created_by: string;
};

function escapeRegExp(str: string): string {
  return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function buildTaskRow(name: string, hostname: string): TaskRow {
  return {
    name,
    hostname,
    status: null,
    created_at: '2026-05-27T12:00:00Z',
    created_by: 'smoke',
  };
}

function buildCreateResponse(name: string, hostname: string) {
  const row = buildTaskRow(name, hostname);
  return {
    ...row,
    id: 42,
    owner: 'BACKUP_PG',
    backend: 'PROXY',
    data: { meta: { target: hostname } },
    protected: false,
    alert_on_fail: false,
    updated_at: row.created_at,
    last_updated_by: row.created_by,
  };
}

const MOCK_TASK_LIST: TaskRow[] = [buildTaskRow(MOCK_TASK_NAME, 'pg-host')];

function isBenignConsoleError(msg: string, deletedTaskNames: string[]): boolean {
  if (msg.includes(':nth-child')) {
    return true;
  }
  // React Query may re-fetch a just-deleted task's detail before the component
  // unmounts; a 404 for *that specific task's* app endpoint is expected. Any
  // other 404 (wrong route, missing mock, unrelated endpoint) must still fail.
  if (
    msg.includes('404 (Not Found)') &&
    msg.includes('/api/apps/backup_pg/') &&
    deletedTaskNames.some((name) => msg.includes(name))
  ) {
    return true;
  }
  return false;
}

type ApiState = { tasks: TaskRow[]; deletedTaskNames: string[] };

/** Task name segment from ``/api/apps/backup_pg/{task_name}`` (not list/schema/schedule). */
function taskNameFromPath(pathname: string): string | null {
  const prefix = '/api/apps/backup_pg/';
  if (!pathname.startsWith(prefix)) {
    return null;
  }
  const segment = pathname.slice(prefix.length).replace(/\/$/, '');
  if (!segment || segment === 'schema' || segment === 'schedule') {
    return null;
  }
  return decodeURIComponent(segment);
}

async function mockBackupPgApis(page: Page, apiState: ApiState): Promise<void> {
  await page.route('**/api/**', (route) => {
    const req = route.request();
    const { pathname } = new URL(req.url());

    if (!pathname.startsWith('/api/')) {
      return route.continue();
    }

    if (pathname.includes('/oauth/refresh')) {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(MOCK_TOKEN),
      });
    }

    if (pathname.includes('/users/me')) {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(MOCK_USER),
      });
    }

    if (isEnabledAppsPath(pathname)) {
      return fulfillEnabledApps(route);
    }

    if (pathname === '/api/extensions/app-info/') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ footer_text: 'PMM Extensions', version: 'v0.0.0-test' }),
      });
    }

    if (pathname === '/api/apps/backup_pg/schema') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(MOCK_SCHEMA),
      });
    }

    if (
      req.method() === 'POST' &&
      (pathname === '/api/apps/backup_pg/' || pathname === '/api/apps/backup_pg')
    ) {
      const body = req.postDataJSON() as { task_name?: string; hostname?: string };
      if (!body.task_name || !body.hostname) {
        return route.fulfill({
          status: 400,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'Missing required fields: task_name, hostname' }),
        });
      }
      const taskName = body.task_name;
      const hostname = body.hostname;
      const created = buildCreateResponse(taskName, hostname);
      apiState.tasks = [buildTaskRow(taskName, hostname), ...apiState.tasks];
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify(created),
      });
    }

    const taskName = taskNameFromPath(pathname);

    if (req.method() === 'DELETE' && taskName) {
      apiState.tasks = apiState.tasks.filter((t) => t.name !== taskName);
      apiState.deletedTaskNames.push(taskName);
      return route.fulfill({ status: 204, body: '' });
    }

    if (req.method() === 'GET' && taskName) {
      const task = apiState.tasks.find((t) => t.name === taskName);
      if (!task) {
        return route.fulfill({
          status: 404,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'Not found' }),
        });
      }
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(buildCreateResponse(task.name, task.hostname)),
      });
    }

    if (
      req.method() === 'GET' &&
      (pathname === '/api/apps/backup_pg/' || pathname === '/api/apps/backup_pg')
    ) {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(apiState.tasks),
      });
    }

    return route.fulfill({
      status: 404,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'not mocked' }),
    });
  });
}

test.describe('PostgreSQL backup_pg app smoke', () => {
  let apiState: ApiState;
  let consoleErrors: string[];

  test.beforeEach(async ({ page }) => {
    apiState = { tasks: [...MOCK_TASK_LIST], deletedTaskNames: [] };
    consoleErrors = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') {
        // Append the source URL: a "Failed to load resource" console error
        // carries the failed request URL in its location, not its text, and
        // the benign-404 gate below needs the URL to scope the carve-out.
        const url = msg.location()?.url ?? '';
        consoleErrors.push(url ? `${msg.text()} ${url}` : msg.text());
      }
    });
    await mockBackupPgApis(page, apiState);
  });

  test.afterEach(async () => {
    const criticalErrors = consoleErrors.filter(
      (msg) => !isBenignConsoleError(msg, apiState.deletedTaskNames),
    );
    expect(criticalErrors).toEqual([]);
  });

  test('list page mounts with task rows', async ({ page }) => {
    await page.goto(APP_ROUTE);

    await expect(page.getByRole('heading', { name: DISPLAY_NAME })).toBeVisible({
      timeout: 30_000,
    });
    await expect(page.getByRole('button', { name: /new postgresql backups/i })).toBeVisible();
    await expect(page.getByText(MOCK_TASK_NAME)).toBeVisible({ timeout: 15_000 });
  });

  test('creates a backup task', async ({ page }) => {
    await page.goto(`${APP_ROUTE}/new`);

    await expect(page.getByRole('heading', { name: /new postgresql backups/i })).toBeVisible({
      timeout: 30_000,
    });

    await page.getByRole('textbox', { name: /task name/i }).fill(NEW_TASK_NAME);
    await page.getByRole('textbox', { name: /execution host/i }).fill(NEW_TASK_HOSTNAME);
    await page.getByRole('button', { name: /create postgresql backups/i }).click();

    await expect(page).toHaveURL(/\/backups\/postgresql\/?$/);
    await expect(page.getByRole('heading', { name: DISPLAY_NAME })).toBeVisible({
      timeout: 15_000,
    });
    await expect(page.getByText(NEW_TASK_NAME)).toBeVisible();
    await expect(page.getByText(NEW_TASK_HOSTNAME)).toBeVisible();
    expect(apiState.tasks.some((t) => t.name === NEW_TASK_NAME)).toBe(true);
  });

  test('opens detail page from list', async ({ page }) => {
    await page.goto(APP_ROUTE);

    await expect(page.getByRole('heading', { name: DISPLAY_NAME })).toBeVisible({
      timeout: 30_000,
    });
    await expect(page.getByText(MOCK_TASK_NAME)).toBeVisible({ timeout: 15_000 });

    await page.getByText(MOCK_TASK_NAME).click();

    await expect(page).toHaveURL(
      new RegExp(escapeRegExp(`/backups/postgresql/task/${MOCK_TASK_NAME}`)),
    );
    await expect(page.getByTestId('plugin-task-delete')).toBeVisible({ timeout: 15_000 });
  });

  test('deletes a backup task', async ({ page }) => {
    const deleteRequests: string[] = [];
    page.on('request', (req) => {
      if (req.method() === 'DELETE') {
        deleteRequests.push(new URL(req.url()).pathname);
      }
    });

    await page.goto(APP_ROUTE);

    await expect(page.getByRole('heading', { name: DISPLAY_NAME })).toBeVisible({
      timeout: 30_000,
    });
    await expect(page.getByText(MOCK_TASK_NAME)).toBeVisible({ timeout: 15_000 });

    await page.getByText(MOCK_TASK_NAME).click();
    await expect(page).toHaveURL(
      new RegExp(escapeRegExp(`/backups/postgresql/task/${MOCK_TASK_NAME}`)),
    );

    await page.getByTestId('plugin-task-delete').click();

    const dialog = page.getByRole('dialog');
    await expect(dialog.getByText(/delete postgresql backups task/i)).toBeVisible();
    await dialog.getByRole('button', { name: 'Delete' }).click();

    await expect(page).toHaveURL(/\/backups\/postgresql\/?$/);
    await expect(page.getByText(MOCK_TASK_NAME)).toHaveCount(0);
    await expect
      .poll(() => deleteRequests, { timeout: 5_000 })
      .toContain(`/api/apps/backup_pg/${MOCK_TASK_NAME}`);
    expect(apiState.tasks.some((t) => t.name === MOCK_TASK_NAME)).toBe(false);
  });
});
