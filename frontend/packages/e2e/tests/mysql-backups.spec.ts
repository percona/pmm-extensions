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

// ── Mock stubs ────────────────────────────────────────────────────────────────

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
  name: 'mysql_backups',
  display_name: 'MySQL Backups',
  description: 'Run XtraBackup, Mydumper, and Binlog backups against MySQL hosts.',
  forms: [
    {
      title: 'Task',
      fields: [
        { type: 'string', name: 'task_name', label: 'Task Name', required: true },
        { type: 'host', name: 'hostname', label: 'Execution Host', required: true },
        {
          type: 'service',
          name: 'service_id',
          label: 'Database Host',
          required: true,
          service_types: ['mysql'],
        },
        {
          type: 'choice',
          name: 'backup_type',
          label: 'Backup Type',
          required: true,
          choices: [
            { label: 'Mydumper', value: 'M' },
            { label: 'XtraBackup', value: 'X' },
            { label: 'Binlog', value: 'B' },
          ],
        },
      ],
    },
    {
      title: 'Upload',
      fields: [
        {
          type: 'multi_choice',
          name: 'upload',
          label: 'Upload providers',
          required: true,
          choices: [
            { label: 'Rsync', value: 'RSYNC' },
            { label: 'S3', value: 'S3' },
            { label: 'Google Cloud Storage', value: 'GSUTIL' },
          ],
        },
        {
          type: 'string',
          name: 's3_bucket',
          label: 'S3 bucket',
          forbidden: [{ when: { not: { contains: { upload: 'S3' } } } }],
        },
        {
          type: 'bool',
          name: 'skip_s3_safety_check',
          label: 'Skip S3 safety check',
          forbidden: [{ when: { not: { contains: { upload: 'S3' } } } }],
        },
      ],
    },
  ],
  capabilities: {
    chaining: true,
    alert_on_fail: true,
    scheduling: true,
    stats: false,
  },
  list_view: {
    columns: [
      { key: 'name', label: 'Name', sortable: true },
      { key: 'status', label: 'Status', format: 'status' },
      { key: 'backup_type', label: 'Type', format: 'chip' },
    ],
  },
};

const tasks: Array<{
  name: string;
  backup_type: string;
  status: string | null;
  data: object;
}> = [];

interface MockOverrides {
  /** Force the schema endpoint to fail with this status. */
  schemaStatus?: number;
  /** Force the POST create endpoint to fail with this status. */
  createStatus?: number;
  /** Delay the POST create endpoint response in ms (for race-condition tests). */
  createDelayMs?: number;
  /** Capture every POST body sent to the create endpoint. */
  capturePosts?: Array<Record<string, unknown>>;
}

async function mockMysqlBackupsRoutes(page: Page, overrides: MockOverrides = {}) {
  await page.route('**/api/**', async (route) => {
    const req = route.request();
    const { pathname } = new URL(req.url());

    if (isEnabledAppsPath(pathname)) {
      return fulfillEnabledApps(route);
    }

    if (pathname.includes('/oauth/refresh')) {
      return route.fulfill({ json: MOCK_TOKEN });
    }
    if (pathname.includes('/users/me')) {
      return route.fulfill({ json: MOCK_USER });
    }
    if (pathname === '/api/apps/mysql_backups/schema') {
      if (overrides.schemaStatus) {
        return route.fulfill({
          status: overrides.schemaStatus,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'schema unavailable (mock)' }),
        });
      }
      return route.fulfill({ json: MOCK_SCHEMA });
    }
    if (pathname === '/api/apps/mysql_backups/' && req.method() === 'GET') {
      return route.fulfill({
        json: { items: tasks, total: tasks.length, offset: 0, limit: 50 },
      });
    }
    if (pathname === '/api/apps/mysql_backups/' && req.method() === 'POST') {
      const auth = req.headers()['authorization'] ?? '';
      if (!auth.toLowerCase().startsWith('bearer ')) {
        return route.fulfill({
          status: 401,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'Bearer required (mock enforcement)' }),
        });
      }
      const body = req.postDataJSON() as Record<string, unknown>;
      overrides.capturePosts?.push(body);
      if (overrides.createDelayMs) {
        await new Promise((r) => setTimeout(r, overrides.createDelayMs));
      }
      if (overrides.createStatus) {
        return route.fulfill({
          status: overrides.createStatus,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'create blew up (mock)' }),
        });
      }
      tasks.push({
        name: body.task_name as string,
        backup_type: body.backup_type as string,
        status: null,
        data: {},
      });
      return route.fulfill({ status: 201, json: tasks[tasks.length - 1] });
    }
    const historyMatch = pathname.match(/^\/api\/apps\/mysql_backups\/[^/]+\/history\/?$/);
    if (historyMatch) {
      return route.fulfill({
        json: {
          items: [{ status: 'SUCCESS', created_at: '2026-05-22T10:00:00Z' }],
          total: 1,
          offset: 0,
          limit: 1,
        },
      });
    }
    if (pathname.endsWith('/extensions/hosts/')) {
      return route.fulfill({
        json: [{ id: 'host1', name: 'host1', address: '127.0.0.1' }],
      });
    }
    if (pathname.endsWith('/extensions/services/')) {
      return route.fulfill({
        json: {
          items: [{ id: 1, name: 'svc1', type: 'mysql' }],
          total: 1,
          offset: 0,
          limit: 200,
        },
      });
    }

    return route.fulfill({
      status: 404,
      contentType: 'application/json',
      body: JSON.stringify({
        detail: `Unmocked API route in mysql-backups e2e: ${req.method()} ${pathname}`,
      }),
    });
  });
}

test.describe('MySQL Backups smoke', () => {
  test.beforeEach(async ({ page }) => {
    // Reset the in-memory task store so each test starts clean.
    tasks.length = 0;
    await mockMysqlBackupsRoutes(page);
  });

  test('loads list page and renders schema-driven app', async ({ page }) => {
    await page.goto('/apps/mysql_backups');
    await expect(page.getByRole('heading', { name: 'MySQL Backups' })).toBeVisible({
      timeout: 30_000,
    });
  });

  for (const [label, value] of [
    ['Mydumper', 'M'],
    ['XtraBackup', 'X'],
    ['Binlog', 'B'],
  ] as const) {
    test(`creates a ${label} (${value}) task and surfaces it in the list view`, async ({
      page,
    }) => {
      await page.goto('/apps/mysql_backups');
      await expect(page.getByRole('heading', { name: 'MySQL Backups' })).toBeVisible({
        timeout: 30_000,
      });

      // Open the create form.
      await page
        .getByRole('button', { name: /^New (MySQL Backups|task)/i })
        .first()
        .click();

      // Fill task_name + backup_type. The schema-driven renderer maps a
      // ChoiceField with `value` "M"/"X"/"B" to its `label` in the option list.
      const taskName = `smoke-${value.toLowerCase()}`;
      await page.getByLabel('Task Name').fill(taskName);

      // Fill required host + service Autocompletes (RHF blocks submit otherwise).
      await page.getByLabel('Execution Host').click();
      await page.getByRole('option', { name: 'host1' }).click();

      await page.getByLabel('Database Host').click();
      await page.getByRole('option', { name: 'svc1 (mysql)' }).click();

      // ChoiceField renders as a radiogroup, not an Autocomplete.
      await page.getByRole('radio', { name: label }).check();

      // percona-ui's SelectInput doesn't link its label to the combobox; locate
      // by MUI's generated id. s3_bucket is gated by Contains("upload", "S3").
      await page.locator('#mui-component-select-upload').click();
      await page.getByRole('option', { name: 'S3' }).click();
      await page.keyboard.press('Escape');
      await page.getByLabel('S3 bucket').fill('test-bucket');

      // Submit.
      await page
        .getByRole('button', { name: /submit|create|save/i })
        .last()
        .click();

      // Verify the new row appears in the list view.
      await expect(page.getByRole('row', { name: new RegExp(taskName) })).toBeVisible({
        timeout: 15_000,
      });
    });
  }
});

// ── Unhappy-path coverage ─────────────────────────────────────────────────────
//
// These tests lock in the SchemaDrivenApp contract under failure conditions:
// backend 5xx on schema/create, validation gating, double-submit guard, and the
// forbidden-gate field-strip behaviour from the contains-tightening commit.

async function openCreateFormAndFillRequired(page: Page, taskName: string) {
  await page.goto('/apps/mysql_backups');
  await expect(page.getByRole('heading', { name: 'MySQL Backups' })).toBeVisible({
    timeout: 30_000,
  });
  await page
    .getByRole('button', { name: /^New (MySQL Backups|task)/i })
    .first()
    .click();

  await page.getByLabel('Task Name').fill(taskName);
  await page.getByLabel('Execution Host').click();
  await page.getByRole('option', { name: 'host1' }).click();
  await page.getByLabel('Database Host').click();
  await page.getByRole('option', { name: 'svc1 (mysql)' }).click();
  await page.getByRole('radio', { name: 'Mydumper' }).check();
  await page.locator('#mui-component-select-upload').click();
  await page.getByRole('option', { name: 'S3' }).click();
  await page.keyboard.press('Escape');
}

// ── Section-visibility schema used only by the section-gate suite ────────────
//
// Extends MOCK_SCHEMA with the three mode sections that carry ``forbidden``
// gates mirroring the real ``mysql_backups_schema``. Each section has one
// representative field so the test can assert presence/absence without
// knowing every field the real app exposes.
const MOCK_SCHEMA_WITH_SECTION_GATES = {
  ...MOCK_SCHEMA,
  forms: [
    ...MOCK_SCHEMA.forms,
    {
      title: 'Mydumper',
      forbidden: [{ when: { not_equals: { backup_type: 'M' } } }],
      fields: [{ type: 'integer', name: 'mydumper_threads', label: 'Mydumper threads' }],
    },
    {
      title: 'XtraBackup',
      forbidden: [{ when: { not_equals: { backup_type: 'X' } } }],
      fields: [{ type: 'integer', name: 'xtrabackup_parallel', label: 'XtraBackup parallel' }],
    },
    {
      title: 'Binlog',
      forbidden: [{ when: { not_equals: { backup_type: 'B' } } }],
      fields: [{ type: 'string', name: 'binlog_start_position', label: 'Binlog start position' }],
    },
  ],
};

// ── Section-visibility gate smoke tests ───────────────────────────────────────
//
// Guards the ``useConditionalSections`` / ``useUnregisterHiddenSections`` +
// ``SectionRenderer`` contract:
// mode sections appear/disappear based on ``backup_type`` and stale child
// values are not included in the submit payload.

test.describe('MySQL Backups – section-visibility gates', () => {
  test.beforeEach(async ({ page }) => {
    tasks.length = 0;
    await mockMysqlBackupsRoutes(page, {});
    // Override schema to include mode sections with forbidden gates.
    await page.route('**/api/apps/mysql_backups/schema', (route) =>
      route.fulfill({ json: MOCK_SCHEMA_WITH_SECTION_GATES }),
    );
  });

  test('no mode section visible before backup_type is selected', async ({ page }) => {
    await page.goto('/apps/mysql_backups');
    await expect(page.getByRole('heading', { name: 'MySQL Backups' })).toBeVisible({
      timeout: 30_000,
    });
    await page
      .getByRole('button', { name: /^New (MySQL Backups|task)/i })
      .first()
      .click();

    // None of the three mode-section headings should be present yet.
    await expect(page.getByRole('heading', { name: 'Mydumper' })).toHaveCount(0);
    await expect(page.getByRole('heading', { name: 'XtraBackup' })).toHaveCount(0);
    await expect(page.getByRole('heading', { name: 'Binlog' })).toHaveCount(0);

    await page.screenshot({
      path: 'test-results/screenshots/section-gates-no-type-selected.png',
    });
  });

  // Each tuple: [radio label, backup_type value, own field label, sib-A field label, sib-B field label]
  for (const [label, value, ownField, sibAField, sibBField] of [
    ['Mydumper', 'M', 'Mydumper threads', 'XtraBackup parallel', 'Binlog start position'],
    ['XtraBackup', 'X', 'XtraBackup parallel', 'Mydumper threads', 'Binlog start position'],
    ['Binlog', 'B', 'Binlog start position', 'Mydumper threads', 'XtraBackup parallel'],
  ] as const) {
    test(`selecting ${label} shows its section and hides the others`, async ({ page }) => {
      await page.goto('/apps/mysql_backups');
      await expect(page.getByRole('heading', { name: 'MySQL Backups' })).toBeVisible({
        timeout: 30_000,
      });
      await page
        .getByRole('button', { name: /^New (MySQL Backups|task)/i })
        .first()
        .click();

      await page.getByRole('radio', { name: label }).check();

      // Verify via the section's representative child field.
      await expect(page.getByLabel(ownField)).toBeVisible({ timeout: 5_000 });
      // Sibling section fields must not be present.
      await expect(page.getByLabel(sibAField)).toHaveCount(0);
      await expect(page.getByLabel(sibBField)).toHaveCount(0);

      await page.screenshot({
        path: `test-results/screenshots/section-gates-${value.toLowerCase()}-selected.png`,
      });
    });
  }

  test('mode switch strips stale section fields from the submit payload', async ({ page }) => {
    const posts: Array<Record<string, unknown>> = [];
    // Capture POSTs by routing the create endpoint specifically; beforeEach already
    // wired up the schema override and base routes.
    await page.route('**/api/apps/mysql_backups/', async (route) => {
      const req = route.request();
      if (req.method() === 'POST') {
        const body = req.postDataJSON() as Record<string, unknown>;
        posts.push(body);
        tasks.push({
          name: body.task_name as string,
          backup_type: body.backup_type as string,
          status: null,
          data: {},
        });
        return route.fulfill({ status: 201, json: tasks[tasks.length - 1] });
      }
      return route.fulfill({
        json: { items: tasks, total: tasks.length, offset: 0, limit: 50 },
      });
    });

    await page.goto('/apps/mysql_backups');
    await expect(page.getByRole('heading', { name: 'MySQL Backups' })).toBeVisible({
      timeout: 30_000,
    });
    await page
      .getByRole('button', { name: /^New (MySQL Backups|task)/i })
      .first()
      .click();

    await page.getByLabel('Task Name').fill('mode-switch-payload');
    await page.getByLabel('Execution Host').click();
    await page.getByRole('option', { name: 'host1' }).click();
    await page.getByLabel('Database Host').click();
    await page.getByRole('option', { name: 'svc1 (mysql)' }).click();

    // 1. Select Binlog and fill its field.
    await page.getByRole('radio', { name: 'Binlog' }).check();
    await expect(page.getByLabel('Binlog start position')).toBeVisible({ timeout: 5_000 });
    await page.getByLabel('Binlog start position').fill('mysql-bin.000001:4');

    // 2. Switch to Mydumper — Binlog section (and its field) must disappear.
    await page.getByRole('radio', { name: 'Mydumper' }).check();
    await expect(page.getByLabel('Binlog start position')).toHaveCount(0, { timeout: 5_000 });

    await page.screenshot({
      path: 'test-results/screenshots/section-gates-after-mode-switch.png',
    });

    // 3. Fill upload and submit.
    await page.locator('#mui-component-select-upload').click();
    await page.getByRole('option', { name: 'S3' }).click();
    await page.keyboard.press('Escape');
    await page.getByLabel('S3 bucket').fill('test-bucket');

    await page
      .getByRole('button', { name: /submit|create|save/i })
      .last()
      .click();

    await expect(page.getByRole('row', { name: /mode-switch-payload/ })).toBeVisible({
      timeout: 15_000,
    });

    // Binlog field must not appear in the payload after switching away.
    expect(posts).toHaveLength(1);
    expect(posts[0]).not.toHaveProperty('binlog_start_position');
    expect(posts[0]).toMatchObject({ backup_type: 'M' });

    await page.screenshot({
      path: 'test-results/screenshots/section-gates-submit-success.png',
    });
  });
});

test.describe('MySQL Backups – unhappy paths', () => {
  test.beforeEach(() => {
    tasks.length = 0;
  });

  test('schema-fetch 503 renders an error state instead of a blank page', async ({ page }) => {
    await mockMysqlBackupsRoutes(page, { schemaStatus: 503 });
    await page.goto('/apps/mysql_backups');

    // SchemaDrivenApp surfaces "Failed to load app schema" on fetch failure.
    await expect(page.getByText(/Failed to load app schema/i)).toBeVisible({
      timeout: 30_000,
    });
    // List heading must not appear — the page should not silently render an empty UI.
    await expect(page.getByRole('heading', { name: 'MySQL Backups' })).toHaveCount(0);
  });

  test('POST 500 keeps form state, shows error, leaves list empty', async ({ page }) => {
    await mockMysqlBackupsRoutes(page, { createStatus: 500 });
    await openCreateFormAndFillRequired(page, 'will-fail');
    await page.getByLabel('S3 bucket').fill('test-bucket');

    await page
      .getByRole('button', { name: /submit|create|save/i })
      .last()
      .click();

    // AppCreatePage surfaces error via notistack snackbar.
    await expect(page.getByText(/create blew up|failed to create/i)).toBeVisible({
      timeout: 15_000,
    });

    // Form state survives: Task Name is still filled, S3 bucket still filled,
    // user did not get bounced back to the list.
    await expect(page.getByLabel('Task Name')).toHaveValue('will-fail');
    await expect(page.getByLabel('S3 bucket')).toHaveValue('test-bucket');
  });

  test('validation blocks empty submit – no POST fires', async ({ page }) => {
    const posts: Array<Record<string, unknown>> = [];
    await mockMysqlBackupsRoutes(page, { capturePosts: posts });
    await page.goto('/apps/mysql_backups');
    await expect(page.getByRole('heading', { name: 'MySQL Backups' })).toBeVisible({
      timeout: 30_000,
    });
    await page
      .getByRole('button', { name: /^New (MySQL Backups|task)/i })
      .first()
      .click();

    // Submit with nothing filled in.
    await page
      .getByRole('button', { name: /submit|create|save/i })
      .last()
      .click();

    // RHF should keep us on the form (still see Task Name input), and the
    // "Fix the highlighted fields" alert appears.
    await expect(page.getByLabel('Task Name')).toBeVisible();
    await expect(page.getByText(/Fix the highlighted fields/i)).toBeVisible({
      timeout: 5_000,
    });
    // No request must have been sent.
    expect(posts).toHaveLength(0);
  });

  test('double-submit produces exactly one POST', async ({ page }) => {
    const posts: Array<Record<string, unknown>> = [];
    await mockMysqlBackupsRoutes(page, { createDelayMs: 500, capturePosts: posts });
    await openCreateFormAndFillRequired(page, 'double-click');
    await page.getByLabel('S3 bucket').fill('test-bucket');

    const submit = page.getByRole('button', { name: /submit|create|save/i }).last();
    // First click triggers the in-flight mutation; SchemaFormRenderer disables
    // the Submit button via `loading={create.isPending}`. The second click
    // hits the disabled button and is a no-op.
    await submit.click();
    await submit.click({ force: true }).catch(() => {});

    // Wait for the deferred response.
    await expect(page.getByRole('row', { name: /double-click/ })).toBeVisible({
      timeout: 15_000,
    });
    expect(posts).toHaveLength(1);
  });

  test('forbidden-gated field is stripped from the POST payload', async ({ page }) => {
    const posts: Array<Record<string, unknown>> = [];
    await mockMysqlBackupsRoutes(page, { capturePosts: posts });
    await openCreateFormAndFillRequired(page, 'no-s3-bucket');

    // Fill the S3 bucket while S3 is selected …
    await page.getByLabel('S3 bucket').fill('should-not-ship');

    // … then unselect S3 so the `forbidden: not contains(upload, S3)` gate
    // fires and useConditionalField unregisters the field.
    await page.locator('#mui-component-select-upload').click();
    await page.getByRole('option', { name: 'S3' }).click();
    // Switch to a non-S3 provider so `upload` stays a valid non-empty list.
    await page.getByRole('option', { name: 'Rsync' }).click();
    await page.keyboard.press('Escape');

    // The bucket input itself must be gone from the DOM.
    await expect(page.getByLabel('S3 bucket')).toHaveCount(0);

    await page
      .getByRole('button', { name: /submit|create|save/i })
      .last()
      .click();

    await expect(page.getByRole('row', { name: /no-s3-bucket/ })).toBeVisible({
      timeout: 15_000,
    });
    expect(posts).toHaveLength(1);
    expect(posts[0]).not.toHaveProperty('s3_bucket');
  });

  // ── skip_s3_safety_check visibility gate ──────────────────────────────────
  //
  // SEP-1061 introduced ``forbidden=_upload_excludes(_UPLOAD_S3)`` on the
  // ``skip_s3_safety_check`` BoolField. Combined with the ``_field_is_present``
  // tweak (False treated as absent), the gate hides the checkbox when ``S3``
  // is not in ``upload`` and unregisters its value from the submit payload.
  test('skip_s3_safety_check checkbox hides without S3 and shows with S3', async ({ page }) => {
    await mockMysqlBackupsRoutes(page);
    await openCreateFormAndFillRequired(page, 'skip-s3-visibility');

    // openCreateFormAndFillRequired selects S3 → checkbox visible.
    await expect(page.getByLabel('Skip S3 safety check')).toBeVisible();

    // Drop S3, add Rsync so upload stays non-empty → checkbox disappears.
    await page.locator('#mui-component-select-upload').click();
    await page.getByRole('option', { name: 'S3' }).click();
    await page.getByRole('option', { name: 'Rsync' }).click();
    await page.keyboard.press('Escape');

    await expect(page.getByLabel('Skip S3 safety check')).toHaveCount(0);
  });

  test('skip_s3_safety_check ships when toggled on with S3 selected', async ({ page }) => {
    const posts: Array<Record<string, unknown>> = [];
    await mockMysqlBackupsRoutes(page, { capturePosts: posts });
    await openCreateFormAndFillRequired(page, 'skip-s3-true');
    await page.getByLabel('S3 bucket').fill('bkt');
    await page.getByLabel('Skip S3 safety check').check();

    await page
      .getByRole('button', { name: /submit|create|save/i })
      .last()
      .click();

    await expect(page.getByRole('row', { name: /skip-s3-true/ })).toBeVisible({
      timeout: 15_000,
    });
    expect(posts).toHaveLength(1);
    expect(posts[0]).toMatchObject({ skip_s3_safety_check: true });
  });

  test('skip_s3_safety_check is dropped from the payload when S3 unselected after toggle', async ({
    page,
  }) => {
    const posts: Array<Record<string, unknown>> = [];
    await mockMysqlBackupsRoutes(page, { capturePosts: posts });
    await openCreateFormAndFillRequired(page, 'skip-s3-stripped');
    await page.getByLabel('S3 bucket').fill('bkt');
    await page.getByLabel('Skip S3 safety check').check();

    // Drop S3, switch to Rsync so the gate fires and RHF unregisters both
    // the bucket and the bool.
    await page.locator('#mui-component-select-upload').click();
    await page.getByRole('option', { name: 'S3' }).click();
    await page.getByRole('option', { name: 'Rsync' }).click();
    await page.keyboard.press('Escape');

    await page
      .getByRole('button', { name: /submit|create|save/i })
      .last()
      .click();

    await expect(page.getByRole('row', { name: /skip-s3-stripped/ })).toBeVisible({
      timeout: 15_000,
    });
    expect(posts).toHaveLength(1);
    expect(posts[0]).not.toHaveProperty('skip_s3_safety_check');
  });
});

// ── multi_choice POST body regression (SEP-1293) ──────────────────────────────
//
// Guards the full data flow: schema → render → user interaction → POST body.
// If the `multi_choice` discriminator breaks again the control won't render,
// the user can't select a value, required validation blocks submit, and these
// tests fail at the explicit `posts[0].upload` assertion — making the root
// cause obvious rather than surfacing as a "row not visible" timeout.

test.describe('MySQL Backups — multi_choice POST body (SEP-1293 regression)', () => {
  test.beforeEach(() => {
    tasks.length = 0;
  });

  test('multi_choice: single selection reaches the POST body', async ({ page }) => {
    const posts: Array<Record<string, unknown>> = [];
    await mockMysqlBackupsRoutes(page, { capturePosts: posts });
    await openCreateFormAndFillRequired(page, 'mc-single');
    await page.getByLabel('S3 bucket').fill('my-bucket');

    await page
      .getByRole('button', { name: /submit|create|save/i })
      .last()
      .click();

    await expect(page.getByRole('row', { name: /mc-single/ })).toBeVisible({
      timeout: 15_000,
    });
    expect(posts).toHaveLength(1);
    expect(posts[0].upload).toEqual(['S3']);
    expect(posts[0]).toHaveProperty('s3_bucket', 'my-bucket');
  });

  test('multi_choice: multiple selections all reach the POST body', async ({ page }) => {
    const posts: Array<Record<string, unknown>> = [];
    await mockMysqlBackupsRoutes(page, { capturePosts: posts });
    // openCreateFormAndFillRequired already selects S3; add Rsync on top.
    await openCreateFormAndFillRequired(page, 'mc-multi');
    await page.locator('#mui-component-select-upload').click();
    await page.getByRole('option', { name: 'Rsync' }).click();
    await page.keyboard.press('Escape');
    await page.getByLabel('S3 bucket').fill('my-bucket');

    await page
      .getByRole('button', { name: /submit|create|save/i })
      .last()
      .click();

    await expect(page.getByRole('row', { name: /mc-multi/ })).toBeVisible({
      timeout: 15_000,
    });
    expect(posts).toHaveLength(1);
    expect(posts[0].upload).toContain('S3');
    expect(posts[0].upload).toContain('RSYNC');
  });
});
// ── Encryption-format POST body ───────────────────────────────────────────────
//
// The encryption format is the only signal for which encryption a backup runs,
// so a break anywhere on schema → render → interaction → POST body downgrades an
// encrypted backup to plaintext without an error. Four choices puts the control
// over ``RADIO_THRESHOLD``, so it renders as a select rather than radios.
const MOCK_SCHEMA_WITH_ENCRYPTION = {
  ...MOCK_SCHEMA,
  forms: [
    ...MOCK_SCHEMA.forms,
    {
      title: 'Encryption',
      fields: [
        {
          type: 'choice',
          name: 'encryption_format',
          label: 'Encryption format',
          required: true,
          choices: [
            { label: 'None', value: 'none' },
            { label: 'GPG', value: 'gpg' },
            { label: 'AES-256', value: 'aes256' },
            { label: 'AES-256 + GPG', value: 'dual' },
          ],
        },
        {
          type: 'string',
          name: 'encryption_recipient',
          label: 'GPG recipient',
        },
      ],
    },
  ],
};

test.describe('MySQL Backups — encryption_format POST body', () => {
  const posts: Array<Record<string, unknown>> = [];

  test.beforeEach(async ({ page }) => {
    tasks.length = 0;
    posts.length = 0;
    await mockMysqlBackupsRoutes(page, { capturePosts: posts });
    await page.route('**/api/apps/mysql_backups/schema', (route) =>
      route.fulfill({ json: MOCK_SCHEMA_WITH_ENCRYPTION }),
    );
  });

  test('the selected encryption format reaches the POST body', async ({ page }) => {
    await openCreateFormAndFillRequired(page, 'enc-gpg');
    await page.getByLabel('S3 bucket').fill('my-bucket');
    await page.locator('#mui-component-select-encryption_format').click();
    await page.getByRole('option', { name: 'GPG', exact: true }).click();
    await page.keyboard.press('Escape');
    await page.getByLabel('GPG recipient').fill('ops@example.com');

    await page
      .getByRole('button', { name: /submit|create|save/i })
      .last()
      .click();

    await expect(page.getByRole('row', { name: /enc-gpg/ })).toBeVisible({
      timeout: 15_000,
    });
    expect(posts).toHaveLength(1);
    expect(posts[0]).toHaveProperty('encryption_format', 'gpg');
    expect(posts[0]).toHaveProperty('encryption_recipient', 'ops@example.com');
  });
});

// ── Required Task-section field vs. a collapsed section ───────────────────────
//
// These serve the *renderer*, not mysql_backups: the schema below is written
// here rather than fetched, so reverting the app's own field declaration leaves
// them passing. What they pin is the client half of a required field's
// mechanism — that a required string in a non-collapsed section renders with the
// HTML required attribute, that RHF suppresses the POST outright rather than
// letting a 422 round-trip, and that the typed value arrives under the right
// body key. Which section mysql_backups actually declares, and that the field is
// required there, is pinned server-side against the live derived schema by
// ``test_schema_pins_section_collapse_posture``; that is the test a revert
// fails. Section order here is Task, Upload, General rather than the app's own
// order — only the collapse posture is mirrored.
const MOCK_SCHEMA_WITH_COLLAPSED_GENERAL = {
  ...MOCK_SCHEMA,
  forms: [
    {
      ...MOCK_SCHEMA.forms[0],
      fields: [
        ...MOCK_SCHEMA.forms[0].fields,
        { type: 'string', name: 'backup_dir', label: 'Backup directory', required: true },
      ],
    },
    ...MOCK_SCHEMA.forms.slice(1),
    {
      title: 'General',
      collapsible: true,
      collapsed_by_default: true,
      fields: [{ type: 'string', name: 'logging_dir', label: 'Logging directory' }],
    },
  ],
};

test.describe('MySQL Backups — required backup directory on the create form', () => {
  const posts: Array<Record<string, unknown>> = [];

  test.beforeEach(async ({ page }) => {
    tasks.length = 0;
    posts.length = 0;
    await mockMysqlBackupsRoutes(page, { capturePosts: posts });
    await page.route('**/api/apps/mysql_backups/schema', (route) =>
      route.fulfill({ json: MOCK_SCHEMA_WITH_COLLAPSED_GENERAL }),
    );
  });

  test('the backup directory is visible when the form opens, unlike a collapsed field', async ({
    page,
  }) => {
    await page.goto('/apps/mysql_backups');
    await expect(page.getByRole('heading', { name: 'MySQL Backups' })).toBeVisible({
      timeout: 30_000,
    });
    await page
      .getByRole('button', { name: /^New (MySQL Backups|task)/i })
      .first()
      .click();

    const backupDir = page.getByLabel(/Backup directory/);
    await expect(backupDir).toBeVisible();
    await expect(backupDir).toHaveAttribute('required', '');

    // A collapsed section unmounts its children, so the General field is absent
    // rather than merely hidden. Expanding it has to bring the field back —
    // without that half, this assertion would also pass for a field the schema
    // never declared, which is the case it is meant to exclude.
    const loggingDir = page.getByLabel(/Logging directory/);
    await expect(loggingDir).toHaveCount(0);
    await page.getByRole('button', { name: 'General' }).click();
    await expect(loggingDir).toBeVisible();

    await page.screenshot({
      path: 'test-results/screenshots/backup-dir-visible-on-open.png',
    });
  });

  test('submitting without a backup directory is refused and fires no POST', async ({ page }) => {
    await openCreateFormAndFillRequired(page, 'needs-dir');
    await page.getByLabel('S3 bucket').fill('my-bucket');

    await page
      .getByRole('button', { name: /submit|create|save/i })
      .last()
      .click();

    await expect(page.getByText(/Backup directory is required/i)).toBeVisible();
    expect(posts).toHaveLength(0);
  });

  test('a filled backup directory reaches the POST body', async ({ page }) => {
    await openCreateFormAndFillRequired(page, 'has-dir');
    await page.getByLabel('S3 bucket').fill('my-bucket');
    await page.getByLabel(/Backup directory/).fill('/backups');

    await page
      .getByRole('button', { name: /submit|create|save/i })
      .last()
      .click();

    await expect(page.getByRole('row', { name: /has-dir/ })).toBeVisible({
      timeout: 15_000,
    });
    expect(posts).toHaveLength(1);
    expect(posts[0]).toHaveProperty('backup_dir', '/backups');
  });
});
