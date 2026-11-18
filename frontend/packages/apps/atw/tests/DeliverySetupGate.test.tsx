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
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import type { ReactNode } from 'react';
import {
  ApiError,
  apiClient,
  REDACTED_SECRET,
  useSettingsList,
  type SettingClassGroup,
} from '@pmm-extensions/api';
import { ROUTES } from '@pmm-extensions/shared';
import { DeliverySetupGate } from '../src/DeliverySetupGate';
import { Messages } from '../src/DeliverySetupGate.messages';
import { useAtwConfig } from '../src/hooks';
import type * as AtwHooks from '../src/hooks';

vi.mock('@pmm-extensions/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@pmm-extensions/api')>()),
  useSettingsList: vi.fn(),
}));

vi.mock('../src/hooks', async (importOriginal) => ({
  ...(await importOriginal<typeof AtwHooks>()),
  useAtwConfig: vi.fn(),
}));

const settingsList = vi.mocked(useSettingsList);
const atwConfig = vi.mocked(useAtwConfig);
const { useAtwConfig: realUseAtwConfig } = await vi.importActual<typeof AtwHooks>('../src/hooks');

const setting = (key: string, value: unknown, hasOverride = false) =>
  ({
    key,
    value,
    has_override: hasOverride,
    setting_class: 'ExtensionsSettings',
    type: 'object',
  }) as unknown as SettingClassGroup['settings'][number];

/**
 * A LIST response whose `ExtensionsSettings` group declares `declared` as the required
 * secret names and stores `storedSecrets` against them. Omitting `storedSecrets`
 * models a deployment that never supplied an override at all.
 */
const extensionsGroups = (
  declared: string[],
  storedSecrets?: Record<string, string>,
): SettingClassGroup[] =>
  [
    {
      setting_class: 'ExtensionsSettings',
      is_app_owned: false,
      settings: [
        setting('DIAGNOSTICS_DELIVERY', {
          secrets: Object.fromEntries(declared.map((name) => [name, REDACTED_SECRET])),
        }),
        setting(
          'DIAGNOSTICS_DELIVERY_INPUTS',
          { endpoint: '', secrets: storedSecrets ?? {} },
          storedSecrets !== undefined,
        ),
      ],
    },
  ] as unknown as SettingClassGroup[];

const mockList = (overrides: Partial<ReturnType<typeof useSettingsList>> = {}) => {
  settingsList.mockReturnValue({
    data: undefined,
    isLoading: false,
    error: null,
    ...overrides,
  } as ReturnType<typeof useSettingsList>);
};

/** What `GET /api/apps/atw/config/` answers while a send is unavailable. */
const UNUSABLE_REASONS = ['Diagnostics delivery is not configured.'];

type AtwConfigData = ReturnType<typeof useAtwConfig>['data'];

const mockConfig = (overrides: Partial<ReturnType<typeof useAtwConfig>> = {}) => {
  atwConfig.mockReturnValue({
    data: { send_disabled_reasons: UNUSABLE_REASONS, case_search_available: false },
    isPending: false,
    isFetching: false,
    ...overrides,
  } as ReturnType<typeof useAtwConfig>);
};

/** The backend's answer when the merged delivery plan would send. */
const mockUsableConfig = () =>
  mockConfig({ data: { send_disabled_reasons: [], case_search_available: false } });

const newQueryClient = () =>
  new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });

function renderWithProviders(ui: ReactNode, queryClient = newQueryClient()) {
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const renderGate = (isAdmin = false, queryClient?: QueryClient) =>
  renderWithProviders(
    <DeliverySetupGate isAdmin={isAdmin}>
      <div data-testid="atw-app" />
    </DeliverySetupGate>,
    queryClient,
  );

describe('DeliverySetupGate', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // The cases below predate the config read and pin the settings-derived
    // decision, which is what the gate falls back to whenever the backend
    // reports a reason the send is unavailable.
    mockConfig();
  });

  it('renders the app once delivery is configured', () => {
    mockList({ data: extensionsGroups(['sn_api_key'], { sn_api_key: 'secret' }) });
    renderGate();

    expect(screen.getByTestId('atw-app')).toBeInTheDocument();
    expect(screen.queryByTestId('atw-delivery-setup-prompt')).not.toBeInTheDocument();
  });

  it('prompts for setup when nothing is stored', () => {
    mockList({ data: extensionsGroups(['sn_api_key']) });
    renderGate();

    expect(screen.getByTestId('atw-delivery-setup-prompt')).toBeInTheDocument();
    expect(screen.getByText(Messages.title)).toBeInTheDocument();
    expect(screen.queryByTestId('atw-app')).not.toBeInTheDocument();
  });

  it('prompts for setup when a declared secret was stored empty', () => {
    mockList({ data: extensionsGroups(['sn_api_key'], { sn_api_key: '' }) });
    renderGate();

    expect(screen.getByTestId('atw-delivery-setup-prompt')).toBeInTheDocument();
  });

  it('renders the app when the plan declares no secrets and an override is stored', () => {
    // Nothing is left for the deployment to supply, so the override alone is as
    // configured as it can get — the endpoint would otherwise read as missing.
    mockList({ data: extensionsGroups([], {}) });
    renderGate();

    expect(screen.getByTestId('atw-app')).toBeInTheDocument();
  });

  it('prompts for setup when the stored values no longer match the plan', () => {
    mockList({ data: extensionsGroups(['sn_token'], { sn_api_key: 'secret' }) });
    renderGate();

    expect(screen.getByTestId('atw-delivery-setup-prompt')).toBeInTheDocument();
  });

  it('renders the app when the settings carry no delivery inputs key', () => {
    // Nothing for an admin to fill in, so the prompt would name a remedy
    // nobody in this deployment can carry out.
    mockList({
      data: [
        {
          setting_class: 'ExtensionsSettings',
          is_app_owned: false,
          settings: [setting('DIAGNOSTICS_DELIVERY', { secrets: {} })],
        },
      ] as unknown as SettingClassGroup[],
    });
    renderGate(true);

    expect(screen.getByTestId('atw-app')).toBeInTheDocument();
    expect(screen.queryByTestId('atw-delivery-setup-prompt')).not.toBeInTheDocument();
  });

  it('renders the app when the settings read failed', () => {
    mockList({ error: new ApiError({ kind: 'network', message: 'down' }) });
    renderGate();

    expect(screen.getByTestId('atw-app')).toBeInTheDocument();
  });

  it('renders the app when the admin-only settings read was forbidden', () => {
    mockList({
      error: new ApiError({ kind: 'http', status: 403, message: 'Forbidden' }),
    });
    renderGate();

    expect(screen.getByTestId('atw-app')).toBeInTheDocument();
    expect(screen.queryByTestId('atw-delivery-setup-prompt')).not.toBeInTheDocument();
  });

  it('waits while the settings are loading', () => {
    mockList({ isLoading: true });
    renderGate();

    expect(screen.getByLabelText(Messages.loading)).toBeInTheDocument();
    expect(screen.queryByTestId('atw-app')).not.toBeInTheDocument();
  });

  it('tells a non-admin to ask an administrator, with no call to action', () => {
    mockList({ data: extensionsGroups(['sn_api_key']) });
    renderGate(false);

    expect(screen.getByText(Messages.notConfigured)).toBeInTheDocument();
    expect(screen.queryByTestId('atw-delivery-setup-cta')).not.toBeInTheDocument();
    expect(screen.queryByText(Messages.adminInstructions)).not.toBeInTheDocument();
  });

  it('points an admin at the settings page and names the key to fill in', () => {
    mockList({ data: extensionsGroups(['sn_api_key']) });
    renderGate(true);

    expect(screen.getByText(Messages.notConfigured)).toBeInTheDocument();
    expect(screen.getByText(Messages.adminInstructions)).toBeInTheDocument();
    expect(screen.getByTestId('atw-delivery-setup-cta')).toHaveAttribute('href', ROUTES.settings);
  });

  describe('when the backend reports delivery usable', () => {
    beforeEach(() => {
      mockUsableConfig();
    });

    it('renders the app for baked credentials with no override stored', () => {
      // The standalone settings.yaml case: the plan's own secret is filled in, so
      // the backend resolves the baked plan even though nothing is stored.
      mockList({ data: extensionsGroups(['api_key']) });
      renderGate(true);

      expect(screen.getByTestId('atw-app')).toBeInTheDocument();
      expect(screen.queryByTestId('atw-delivery-setup-prompt')).not.toBeInTheDocument();
    });

    it('renders the app for a plan that declares no secrets with no override stored', () => {
      mockList({ data: extensionsGroups([]) });
      renderGate(true);

      expect(screen.getByTestId('atw-app')).toBeInTheDocument();
      expect(screen.queryByTestId('atw-delivery-setup-prompt')).not.toBeInTheDocument();
    });

    it('renders the app for filled-in inputs that are not a stored override', () => {
      // Inputs supplied through settings.yaml or the environment carry values,
      // but `has_override` reports a stored database override only.
      mockList({
        data: [
          {
            setting_class: 'ExtensionsSettings',
            is_app_owned: false,
            settings: [
              setting('DIAGNOSTICS_DELIVERY', { secrets: { api_key: REDACTED_SECRET } }),
              setting(
                'DIAGNOSTICS_DELIVERY_INPUTS',
                { endpoint: 'https://intake.example.com', secrets: { api_key: REDACTED_SECRET } },
                false,
              ),
            ],
          },
        ] as unknown as SettingClassGroup[],
      });
      renderGate(true);

      expect(screen.getByTestId('atw-app')).toBeInTheDocument();
      expect(screen.queryByTestId('atw-delivery-setup-prompt')).not.toBeInTheDocument();
    });

    it('renders the app when the settings say the stored values drifted', () => {
      mockList({ data: extensionsGroups(['sn_token'], { sn_api_key: 'secret' }) });
      renderGate(true);

      expect(screen.getByTestId('atw-app')).toBeInTheDocument();
    });

    it('renders the app without waiting for the settings read', () => {
      mockList({ isLoading: true });
      renderGate(true);

      expect(screen.getByTestId('atw-app')).toBeInTheDocument();
      expect(screen.queryByLabelText(Messages.loading)).not.toBeInTheDocument();
    });
  });

  describe('when a cached "usable" answer is being refreshed on mount', () => {
    // TanStack Query keeps serving the cached answer during the mount refresh,
    // which may still come back with reasons.
    beforeEach(() => {
      mockConfig({
        data: { send_disabled_reasons: [], case_search_available: false },
        isFetching: true,
        isFetchedAfterMount: false,
      });
    });

    it('waits instead of flashing the app when the settings would prompt', () => {
      mockList({ data: extensionsGroups(['api_key']) });
      renderGate(true);

      expect(screen.getByLabelText(Messages.loading)).toBeInTheDocument();
      expect(screen.queryByTestId('atw-app')).not.toBeInTheDocument();
      expect(screen.queryByTestId('atw-delivery-setup-prompt')).not.toBeInTheDocument();
    });

    it('waits while the settings are loading', () => {
      mockList({ isLoading: true });
      renderGate(true);

      expect(screen.getByLabelText(Messages.loading)).toBeInTheDocument();
      expect(screen.queryByTestId('atw-app')).not.toBeInTheDocument();
    });

    it('renders the app without waiting when the settings already render it', () => {
      mockList({ data: extensionsGroups(['sn_api_key'], { sn_api_key: 'secret' }) });
      renderGate();

      expect(screen.getByTestId('atw-app')).toBeInTheDocument();
      expect(screen.queryByLabelText(Messages.loading)).not.toBeInTheDocument();
    });

    it('keeps the app on screen during a later background refetch', () => {
      // Once the mount refresh has landed, a refetch must not unmount the app.
      mockConfig({
        data: { send_disabled_reasons: [], case_search_available: false },
        isFetching: true,
        isFetchedAfterMount: true,
      });
      mockList({ data: extensionsGroups(['api_key']) });
      renderGate(true);

      expect(screen.getByTestId('atw-app')).toBeInTheDocument();
      expect(screen.queryByLabelText(Messages.loading)).not.toBeInTheDocument();
    });
  });

  describe('when the backend gives no usable answer', () => {
    it('falls back to the settings when the config carries no reasons array', () => {
      // An e2e catch-all route answers `[]`, which has no `send_disabled_reasons`.
      mockConfig({ data: [] as unknown as AtwConfigData });
      mockList({ data: extensionsGroups(['api_key']) });
      renderGate(true);

      expect(screen.getByTestId('atw-delivery-setup-prompt')).toBeInTheDocument();
      expect(screen.queryByTestId('atw-app')).not.toBeInTheDocument();
    });

    it('falls back to the settings when the reasons field is not an array', () => {
      mockConfig({ data: { case_search_available: false } as unknown as AtwConfigData });
      mockList({ data: extensionsGroups(['api_key']) });
      renderGate(true);

      expect(screen.getByTestId('atw-delivery-setup-prompt')).toBeInTheDocument();
    });

    it('still renders a settings-configured deployment when the config carries no array', () => {
      mockConfig({ data: [] as unknown as AtwConfigData });
      mockList({ data: extensionsGroups(['sn_api_key'], { sn_api_key: 'secret' }) });
      renderGate();

      expect(screen.getByTestId('atw-app')).toBeInTheDocument();
    });

    it('waits instead of flashing the prompt while the config request is in flight', () => {
      mockConfig({ data: undefined, isPending: true, isFetching: true });
      mockList({ data: extensionsGroups(['api_key']) });
      renderGate(true);

      expect(screen.getByLabelText(Messages.loading)).toBeInTheDocument();
      expect(screen.queryByTestId('atw-delivery-setup-prompt')).not.toBeInTheDocument();
      expect(screen.queryByTestId('atw-app')).not.toBeInTheDocument();
    });

    it('waits instead of showing the prompt while a cached answer is being refreshed', () => {
      mockConfig({ isFetching: true });
      mockList({ data: extensionsGroups(['api_key']) });
      renderGate(true);

      expect(screen.getByLabelText(Messages.loading)).toBeInTheDocument();
      expect(screen.queryByTestId('atw-delivery-setup-prompt')).not.toBeInTheDocument();
    });

    it('does not wait on the config request when the settings already render the app', () => {
      mockConfig({ data: undefined, isPending: true, isFetching: true });
      mockList({ data: extensionsGroups(['sn_api_key'], { sn_api_key: 'secret' }) });
      renderGate();

      expect(screen.getByTestId('atw-app')).toBeInTheDocument();
      expect(screen.queryByLabelText(Messages.loading)).not.toBeInTheDocument();
    });
  });

  describe('through the real config hook', () => {
    // These go through `useAtwConfig` itself, so they pin which config outcomes
    // reach the gate as "usable" rather than relying on a hand-built answer.
    beforeEach(() => {
      atwConfig.mockImplementation(realUseAtwConfig);
      mockList({ data: extensionsGroups(['api_key']) });
    });

    afterEach(() => {
      vi.restoreAllMocks();
    });

    it('prompts when a successful response carries no reasons array', async () => {
      vi.spyOn(apiClient, 'get').mockResolvedValue({ data: [] });
      renderGate(true);

      expect(await screen.findByTestId('atw-delivery-setup-prompt')).toBeInTheDocument();
      expect(screen.queryByTestId('atw-app')).not.toBeInTheDocument();
    });

    it('replaces a cached "unavailable" answer when it mounts again', async () => {
      // An admin saw the prompt, fixed delivery in Settings and came back: the
      // gate must not leave the old reasons cached for the Send action to reuse.
      const queryClient = newQueryClient();
      queryClient.setQueryData(['atw', 'config'], {
        send_disabled_reasons: UNUSABLE_REASONS,
        case_search_available: false,
      });
      const get = vi
        .spyOn(apiClient, 'get')
        .mockResolvedValue({ data: { send_disabled_reasons: [], case_search_available: false } });
      renderGate(true, queryClient);

      expect(screen.queryByTestId('atw-delivery-setup-prompt')).not.toBeInTheDocument();
      expect(await screen.findByTestId('atw-app')).toBeInTheDocument();
      expect(get).toHaveBeenCalledTimes(1);
      expect(queryClient.getQueryData(['atw', 'config'])).toEqual({
        send_disabled_reasons: [],
        case_search_available: false,
      });
    });

    it('never shows the app when a cached "usable" answer refreshes to reasons', async () => {
      // The reverse of the case above: delivery was usable when the answer was
      // cached and has since stopped being usable.
      const queryClient = newQueryClient();
      queryClient.setQueryData(['atw', 'config'], {
        send_disabled_reasons: [],
        case_search_available: false,
      });
      let resolveGet: (value: unknown) => void = () => {};
      const get = vi.spyOn(apiClient, 'get').mockReturnValue(
        new Promise((resolve) => {
          resolveGet = resolve;
        }),
      );
      renderGate(true, queryClient);

      expect(screen.getByLabelText(Messages.loading)).toBeInTheDocument();
      expect(screen.queryByTestId('atw-app')).not.toBeInTheDocument();

      resolveGet({
        data: { send_disabled_reasons: UNUSABLE_REASONS, case_search_available: false },
      });

      expect(await screen.findByTestId('atw-delivery-setup-prompt')).toBeInTheDocument();
      expect(screen.queryByTestId('atw-app')).not.toBeInTheDocument();
      expect(get).toHaveBeenCalledTimes(1);
    });

    it('renders the app once a cached "usable" answer is confirmed on mount', async () => {
      const queryClient = newQueryClient();
      queryClient.setQueryData(['atw', 'config'], {
        send_disabled_reasons: [],
        case_search_available: false,
      });
      vi.spyOn(apiClient, 'get').mockResolvedValue({
        data: { send_disabled_reasons: [], case_search_available: false },
      });
      renderGate(true, queryClient);

      expect(await screen.findByTestId('atw-app')).toBeInTheDocument();
      expect(screen.queryByTestId('atw-delivery-setup-prompt')).not.toBeInTheDocument();
    });

    it('renders the app when the config request fails (fail-open)', async () => {
      // `useAtwConfig` maps any request error to an empty reasons array, so a
      // failed config read opens the app the same way a failed settings read
      // does. The Send action's 503 check still guards the upload.
      vi.spyOn(apiClient, 'get').mockRejectedValue(
        new ApiError({ kind: 'http', status: 500, message: 'boom' }),
      );
      renderGate(true);

      expect(await screen.findByTestId('atw-app')).toBeInTheDocument();
      await waitFor(() =>
        expect(screen.queryByTestId('atw-delivery-setup-prompt')).not.toBeInTheDocument(),
      );
    });
  });
});
