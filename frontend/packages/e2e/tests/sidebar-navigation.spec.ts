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

/**
 * Sidebar wiring regression net (SEP-1270).
 *
 * The per-app specs (mysql-backups, archives, …) verify each app page in
 * isolation by navigating directly to its URL. They do NOT exercise the sidebar
 * itself, which is how two regressions slipped through review: the MySQL entry
 * pointed at a placeholder route and the Archive entry at /archive (no route →
 * NotFoundPage).
 *
 * This spec clicks the covered sidebar entries the way a user would and asserts
 * (1) the resulting URL, (2) a positive sentinel rendered by the target app, and
 * only then (3) that the 404 NotFoundPage is not showing. The positive sentinel
 * is essential: pages are lazy()/Suspense-loaded, so the URL flips synchronously
 * on click while the chunk is still resolving — asserting 404 *absence* against that
 * still-loading DOM would pass even for a regressed route. Waiting for the
 * target's own element first guarantees the page has actually mounted.
 *
 * Entries not covered by this spec:
 * Alert Templates, Schema Change/Alters, Health & Security Report, Settings.
 */

import { test, expect, type Locator, type Page } from '@playwright/test';
import {
  fulfillConfiguredDelivery,
  fulfillEnabledApps,
  isEnabledAppsPath,
  isSettingsListPath,
} from './mockEnabledApps';

// ── Mocks ───────────────────────────────────────────────────────────────────
// NOTE: this auth+API mock is intentionally close to the one in shell.spec.ts.
// Per _template.spec.ts, the shared base ought to be extracted to
// tests/helpers/mock-apis.ts once a consumer needs it; tracked as a fast-follow
// so this ticket stays scoped to the sidebar fix.

const MOCK_TOKEN = { access_token: 'smoke-test-token', expires_in: 3600 };

const MOCK_USER = {
  id: '00000000-0000-0000-0000-000000000001',
  username: 'smoke',
  email: 'smoke@percona.com',
  firstName: 'Smoke',
  lastName: 'Test',
  // Deliberately non-admin: these specs only navigate and assert readable
  // sentinels, so they double as cross-app smoke coverage of the read-only
  // experience.
  isAdmin: false,
};

// Heading served for schema-driven apps whose display name we don't assert
// individually (the URL already identifies them). Schema-driven pages render
// `schema.display_name` as their h4, so this is a deterministic sentinel.
const GENERIC_APP_HEADING = 'PMM Extensions App';

// Apps whose display-name heading we assert explicitly (keyed by the
// `<name>` in /api/apps/<name>/schema). These are the schema-driven entries
// this ticket is about — including the two that regressed.
const SCHEMA_DISPLAY_NAMES: Record<string, string> = {
  checksums: 'Checksums',
  mysql_backups: 'MySQL Backups',
  archives: 'Archives',
  backup_pg: 'PostgreSQL Backups',
};

async function mockAuthenticatedApis(page: Page): Promise<void> {
  await page.route('**/api/**', (route) => {
    const { pathname } = new URL(route.request().url());

    // Pass through Vite's internal module-serving paths (e.g. /@fs/...)
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

    const schemaMatch = pathname.match(/\/api\/apps\/(.+)\/schema$/);
    if (schemaMatch) {
      const name = schemaMatch[1];
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          name,
          display_name: SCHEMA_DISPLAY_NAMES[name] ?? GENERIC_APP_HEADING,
          capabilities: { chaining: false, alert_on_fail: false, scheduling: false, stats: false },
          forms: [],
          list_view: { columns: [] },
        }),
      });
    }

    if (pathname.endsWith('/extensions/dashboard/')) {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ nodes: 0, tasks: 0, snippets: 0, targets: 0 }),
      });
    }

    if (pathname.includes('/extensions/task-history/')) {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ items: [], total: 0, offset: 0, limit: 5 }),
      });
    }

    // The delivery setup gate wraps every ATW route.
    if (isSettingsListPath(pathname)) {
      return fulfillConfiguredDelivery(route);
    }

    if (isEnabledAppsPath(pathname)) {
      return fulfillEnabledApps(route);
    }

    // Default: empty success for app task lists and anything else
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: '[]',
    });
  });
}

// ── Sidebar map ───────────────────────────────────────────────────────────────
// One entry per covered leaf in shell/src/appNavConfig.ts.
// `label` must match ``display_name`` from ``GET /api/apps/`` (see mockEnabledApps).
// `group`      — collapsible parent that must be expanded before the child shows.
// `urlPattern` — matched against the post-navigation URL (apps may redirect
//                to a default sub-route, e.g. /backups/mongodb → /backups/mongodb/backups).
// `sentinel`   — positive locator the target page renders; asserted before the
//                404 negative check so we never assert against a
//                still-loading DOM.
interface SidebarTarget {
  label: string;
  group?: string;
  urlPattern: RegExp;
  sentinel: (page: Page) => Locator;
}

const heading = (name: string) => (page: Page) => page.getByRole('heading', { name }).first();

const TARGETS: SidebarTarget[] = [
  { label: 'Task Manager', urlPattern: /\/tasks(\/|$)/, sentinel: heading(GENERIC_APP_HEADING) },
  {
    label: 'Snippet Manager',
    urlPattern: /\/snippets(\/|$)/,
    sentinel: heading('Snippet Manager'),
  },
  {
    label: 'Support diagnostics',
    group: 'Diagnostics',
    urlPattern: /\/atw(\/|$)/,
    sentinel: heading('Support diagnostics'),
  },
  { label: 'Checksums', urlPattern: /\/apps\/checksums(\/|$)/, sentinel: heading('Checksums') },
  {
    label: 'Alert Troubleshooting',
    group: 'Alerts',
    urlPattern: /\/alerts\/troubleshooting(\/|$)/,
    // Empty-state page renders no heading — assert its empty-state copy instead.
    sentinel: (page) => page.getByText(/No alerts found/i),
  },
  {
    label: 'MySQL Backups',
    group: 'Backups',
    urlPattern: /\/apps\/mysql_backups(\/|$)/,
    sentinel: heading('MySQL Backups'),
  },
  {
    label: 'MongoDB Backups',
    group: 'Backups',
    urlPattern: /\/backups\/mongodb(\/|$)/,
    sentinel: heading(GENERIC_APP_HEADING),
  },
  {
    label: 'PostgreSQL Backups',
    group: 'Backups',
    urlPattern: /\/backups\/postgresql(\/|$)/,
    sentinel: heading('PostgreSQL Backups'),
  },
  { label: 'Archives', urlPattern: /\/apps\/archives(\/|$)/, sentinel: heading('Archives') },
  {
    label: 'Dipper Data Collection',
    group: 'Diagnostics',
    urlPattern: /\/dipper(\/|$)/,
    sentinel: heading(GENERIC_APP_HEADING),
  },
];

// NotFoundPage sentinel copy — its presence means the sidebar landed on an
// unrouted destination.
const NOT_FOUND_TEXT = /Page not found/i;

const LAZY_TIMEOUT = 30_000;

test.describe('sidebar navigation wiring', () => {
  test.beforeEach(async ({ page }) => {
    await mockAuthenticatedApis(page);
    await page.goto('/');
    // Wait for the authenticated shell (sidebar) before clicking around.
    await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible({
      timeout: LAZY_TIMEOUT,
    });
  });

  for (const target of TARGETS) {
    test(`sidebar → ${target.group ? `${target.group} / ` : ''}${target.label} mounts its app`, async ({
      page,
    }) => {
      // Expand the parent group so its children render (Collapse uses unmountOnExit).
      if (target.group) {
        const child = page.getByRole('button', { name: target.label });
        if (!(await child.isVisible().catch(() => false))) {
          await page.getByRole('button', { name: target.group }).click();
        }
      }

      await page.getByRole('button', { name: target.label }).click();

      // URL resolves to the target app's route (not a 404 path).
      await expect(page).toHaveURL(target.urlPattern, { timeout: LAZY_TIMEOUT });

      // Positive sentinel: wait until the target page has actually mounted. This
      // must come BEFORE the negative checks below, otherwise they race the
      // lazy chunk and pass against a still-loading DOM.
      await expect(target.sentinel(page)).toBeVisible({ timeout: LAZY_TIMEOUT });

      // Negative sentinel: an unrouted destination renders this, the real ones never do.
      await expect(page.getByText(NOT_FOUND_TEXT)).toHaveCount(0);
    });
  }

  test('sidebar → Dashboard returns home', async ({ page }) => {
    // Leave the dashboard first so the click is a real navigation.
    await page.getByRole('button', { name: 'Task Manager' }).click();
    await expect(page).toHaveURL(/\/tasks(\/|$)/, { timeout: LAZY_TIMEOUT });
    // Wait for the lazy Task Manager page to actually mount before navigating away.
    // The URL flips synchronously on click while the chunk is still resolving, so
    // without this sentinel the Dashboard click can be swallowed mid-load.
    await expect(heading(GENERIC_APP_HEADING)(page)).toBeVisible({ timeout: LAZY_TIMEOUT });

    await page.getByRole('button', { name: 'Dashboard' }).click();
    await expect(page).toHaveURL(/\/$/, { timeout: LAZY_TIMEOUT });
    await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible();
    await expect(page.getByText(NOT_FOUND_TEXT)).toHaveCount(0);
  });
});
