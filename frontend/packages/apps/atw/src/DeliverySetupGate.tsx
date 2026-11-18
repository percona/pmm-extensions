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

import { useMemo, type PropsWithChildren } from 'react';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import CircularProgress from '@mui/material/CircularProgress';
import Link from '@mui/material/Link';
import Stack from '@mui/material/Stack';
import Typography from '@mui/material/Typography';
import { Link as RouterLink } from 'react-router';
import { useSettingsList, type SettingClassGroup, type SettingResponse } from '@pmm-extensions/api';
import { ROUTES } from '@pmm-extensions/shared';
import { useAtwConfig } from './hooks';
import type { AtwConfig } from './types';
import {
  DELIVERY_INPUTS_KEY,
  DELIVERY_PLAN_KEY,
  DELIVERY_SETTING_CLASS,
  Messages,
  PERCONA_SUPPORT_URL,
  SUPPORT_DIAGNOSTICS_DOCS_URL,
} from './DeliverySetupGate.messages';

/**
 * What the stored settings say about diagnostics delivery.
 *
 * Derived the same way as `connectionStatus()` in PMM's ServiceNow Settings tab,
 * with the same three values. This status alone no longer decides the gate, though:
 * {@link DeliverySetupGate} also renders the app whenever
 * `GET /api/apps/atw/config/` reports no `send_disabled_reasons`, even when this
 * status says `not-configured` or `drifted` (SEP-1959). A standalone PMM Extensions
 * deployment can carry working credentials in the baked plan or in a file-supplied
 * value with no stored override, which the PMM side-car image never does.
 * PMM itself dropped its setup gate (`ServiceNowSetupGate`) in PMM-15405: the
 * PMM-embedded app always renders and relies only on `send_disabled_reasons`
 * next to the Send control, so there is no PMM gate to keep in step with this one.
 */
export type DeliveryStatus = 'configured' | 'not-configured' | 'drifted';

interface DeliveryInputs {
  endpoint?: string | null;
  secrets?: Record<string, string>;
}

interface StoredDeliveryInputs {
  endpoint: string;
  secrets: Record<string, string>;
  hasOverride: boolean;
  /** Whether the settings response carries the key at all. */
  isPresent: boolean;
}

/** Locate one setting inside the `ExtensionsSettings` group of a LIST response. */
const findExtensionsSetting = (
  groups: SettingClassGroup[] | undefined,
  key: string,
): SettingResponse | undefined =>
  groups
    ?.find((group) => group.setting_class === DELIVERY_SETTING_CLASS)
    ?.settings.find((setting) => setting.key === key);

const asInputs = (value: unknown): DeliveryInputs => {
  if (!value || typeof value !== 'object') {
    return {};
  }
  const { endpoint, secrets } = value as DeliveryInputs;
  return {
    endpoint: typeof endpoint === 'string' ? endpoint : null,
    secrets:
      secrets && typeof secrets === 'object' && !Array.isArray(secrets)
        ? Object.fromEntries(
            Object.entries(secrets).map(([name, secret]) => [
              name,
              typeof secret === 'string' ? secret : '',
            ]),
          )
        : {},
  };
};

/**
 * The secret names this deployment must supply, read from the baked plan.
 *
 * The plan is the declaration PMM Extensions validates a write against, so it always wins.
 * The stored inputs are a fallback for a PMM Extensions build that does not list the plan
 * at all: their names are only stale if that build also renamed one.
 */
export const declaredSecretNames = (groups: SettingClassGroup[] | undefined): string[] => {
  const planNames = Object.keys(
    asInputs(findExtensionsSetting(groups, DELIVERY_PLAN_KEY)?.value).secrets ?? {},
  );
  if (planNames.length > 0) {
    return planNames;
  }
  return Object.keys(
    asInputs(findExtensionsSetting(groups, DELIVERY_INPUTS_KEY)?.value).secrets ?? {},
  );
};

/**
 * The stored per-deployment inputs. Secrets come back masked once something is
 * stored, so the values here are only ever compared against the empty string —
 * never inspected for content.
 */
export const storedDeliveryInputs = (
  groups: SettingClassGroup[] | undefined,
): StoredDeliveryInputs => {
  const setting = findExtensionsSetting(groups, DELIVERY_INPUTS_KEY);
  const { endpoint, secrets } = asInputs(setting?.value);
  return {
    endpoint: endpoint ?? '',
    secrets: secrets ?? {},
    hasOverride: setting?.has_override ?? false,
    isPresent: setting !== undefined,
  };
};

/**
 * What the stored inputs say about delivery, without asking PMM Extensions a second time.
 *
 * An empty secret is a valid save that leaves delivery unavailable, so it reads
 * as "not configured" rather than as a failure. A declared name with no stored
 * counterpart means the image renamed one after the values were supplied — the
 * value PMM Extensions still holds no longer satisfies the plan.
 *
 * A plan that declares no secrets is judged on the override alone: there is no
 * credential left for the deployment to supply, so a stored override is as
 * configured as it can get.
 */
export const deliveryStatus = (
  declaredNames: string[],
  stored: StoredDeliveryInputs,
): DeliveryStatus => {
  if (!stored.hasOverride) {
    return 'not-configured';
  }
  if (declaredNames.length === 0) {
    return 'configured';
  }
  if (declaredNames.some((name) => stored.secrets[name] === undefined)) {
    return 'drifted';
  }
  return declaredNames.every((name) => stored.secrets[name] !== '')
    ? 'configured'
    : 'not-configured';
};

/**
 * What PMM Extensions currently holds for diagnostics delivery, read once and derived.
 *
 * The settings LIST is admin-gated (it answers 403 to everyone else), so the
 * `error` this returns is a routine outcome for a regular user rather than an
 * exception — see {@link DeliverySetupGate} for how it is handled.
 */
export const useDeliveryConfiguration = () => {
  const { data: groups, isLoading, error } = useSettingsList();

  const declaredNames = useMemo(() => declaredSecretNames(groups), [groups]);
  const stored = useMemo(() => storedDeliveryInputs(groups), [groups]);

  return {
    declaredNames,
    stored,
    status: deliveryStatus(declaredNames, stored),
    isLoading,
    error,
  };
};

/**
 * Whether the backend reports that a send would go through right now.
 *
 * Only an actual empty array counts. A response that carries no
 * `send_disabled_reasons` array at all (an e2e catch-all route answering `[]`,
 * an older backend, a malformed body) says nothing about delivery, so the gate
 * falls back to the settings-derived decision instead of treating it as usable.
 *
 * Note that `useAtwConfig` resolves a failed request to an empty array, so a
 * config read that failed counts as usable here. That is the same fail-open rule
 * the gate already applies to a failed settings read, and the Send action's own
 * 503 check still guards the upload.
 */
const isDeliveryUsable = (config: AtwConfig | undefined): boolean => {
  const reasons: unknown = config?.send_disabled_reasons;
  return Array.isArray(reasons) && reasons.length === 0;
};

/**
 * What an operator sees instead of the app while delivery is unconfigured:
 * what the tool does, who can enable it, and what it will not do unasked.
 */
const SetupPrompt = ({ isAdmin }: { isAdmin: boolean }) => (
  <Stack gap={3} sx={{ flex: 1 }}>
    <Typography variant="h4">{Messages.heading}</Typography>
    <Stack
      alignItems="center"
      justifyContent="center"
      sx={{ flex: 1, minHeight: '60vh' }}
      data-testid="atw-delivery-setup-prompt"
    >
      <Stack alignItems="center" textAlign="center" sx={{ maxWidth: 480, width: '100%', p: 2 }}>
        <Stack gap={1}>
          <Typography variant="h5">{Messages.title}</Typography>
          <Typography variant="body1">{Messages.description}</Typography>
        </Stack>

        {/*
          The call to action slot. PMM Extensions has no guided ServiceNow form to send an
          operator to, so a regular user gets the explanation alone and an admin
          gets the settings page plus the name of the key to fill in.
        */}
        <Stack alignItems="center" gap={1.5} sx={{ my: 4 }}>
          <Typography variant="body1">{Messages.notConfigured}</Typography>
          {isAdmin && (
            <>
              <Typography variant="body2" color="text.secondary">
                {Messages.adminInstructions}
              </Typography>
              <Button
                variant="outlined"
                component={RouterLink}
                to={ROUTES.settings}
                data-testid="atw-delivery-setup-cta"
              >
                {Messages.openSettings}
              </Button>
            </>
          )}
        </Stack>

        <Stack gap={2}>
          <Typography variant="body2">
            {Messages.consentNote}{' '}
            <Link href={SUPPORT_DIAGNOSTICS_DOCS_URL} target="_blank" rel="noopener noreferrer">
              {Messages.howItWorks}
            </Link>
          </Typography>
          <Typography variant="body2">
            {Messages.subscriptionPrompt}
            <br />
            <Link href={PERCONA_SUPPORT_URL} target="_blank" rel="noopener noreferrer">
              {Messages.subscriptionLinkText}
            </Link>
          </Typography>
        </Stack>
      </Stack>
    </Stack>
  </Stack>
);

const LoadingSpinner = () => (
  <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'center', p: 4 }}>
    <CircularProgress aria-label={Messages.loading} />
  </Box>
);

interface DeliverySetupGateProps {
  /** Whether the current user has admin privileges. Controls the call to action. */
  isAdmin?: boolean;
}

/**
 * Holds the Support diagnostics app until diagnostics delivery is configured.
 *
 * The backend's verdict wins when it says delivery is usable:
 * `GET /api/apps/atw/config/` merges the inputs onto the plan and validates the
 * result, which the masked settings values cannot reproduce. A deployment whose
 * credentials come from the baked plan or the settings file, with no stored
 * override, is usable even though the settings read alone says `not-configured`.
 * Any other config answer leaves the decision to the settings below; while the
 * config request is still in flight (including the refetch every mount starts)
 * and the settings would show the prompt, the gate keeps its spinner so a usable
 * deployment never flashes the setup screen. The same holds the other way: a
 * cached "usable" answer waits for that mount refetch whenever the settings
 * would prompt, so a deployment that stopped being usable never flashes the app.
 * A refetch TanStack Query paused because the browser is offline counts as still
 * in flight: it starts the moment the connection returns, and would otherwise
 * swap whatever the gate showed meanwhile.
 *
 * Anything the app can do ends in an upload to a ServiceNow case, so an
 * unconfigured deployment has nothing to offer but a dead end — the prompt
 * replaces the app rather than sitting beside it.
 *
 * A settings read that failed does *not* produce the prompt: the app renders
 * and reports its own failures, which is better than telling an operator with a
 * working connection to go configure one. That rule also covers the 403 the
 * admin-gated settings API returns to every regular user — "cannot tell" must
 * never become a permanent setup screen for people who could not clear it. A
 * build whose settings carry no `DIAGNOSTICS_DELIVERY_INPUTS` key at all is the
 * same case for a different reason: there is no key for anyone to fill in, so
 * the prompt would name a remedy nobody in this deployment can carry out.
 * `drifted` does gate: PMM Extensions holds values the current delivery plan no longer
 * accepts, so delivery is as broken as if nothing were stored.
 *
 * Note for the PMM Extensions → PMM sync: PMM has no equivalent of this gate. It
 * removed its own `ServiceNowSetupGate` in PMM-15405, so the PMM-embedded app
 * renders its routes directly and relies only on `send_disabled_reasons` to
 * disable the Send control and name what is missing. Do not port this gate (or
 * its copy in `DeliverySetupGate.messages.ts`) to PMM.
 */
export function DeliverySetupGate({
  isAdmin = false,
  children,
}: PropsWithChildren<DeliverySetupGateProps>) {
  const { status, stored, isLoading, error } = useDeliveryConfiguration();
  // Refetched on every mount: the gate is what an admin comes back to after
  // fixing delivery in Settings, and its answer is the one the Send action reads.
  const {
    data: config,
    isPending: isConfigPending,
    isFetching: isConfigFetching,
    isPaused: isConfigPaused,
    isFetchedAfterMount: isConfigFetchedAfterMount,
  } = useAtwConfig({ refreshOnMount: true });
  // An offline browser pauses the refresh with `isFetching: false`; it is still
  // unfinished and resumes on reconnect, so it holds the gate the same way.
  const isConfigRefreshing = isConfigFetching || isConfigPaused;

  // TanStack Query keeps serving the cached answer while the mount refresh runs,
  // so a cached "usable" is only trusted once that refresh has landed (or when no
  // refresh is running). Otherwise a cache that says usable, refreshed to "not
  // usable", would render the app and then swap it for the setup prompt. A later
  // background refetch does not reset `isFetchedAfterMount`, so it never unmounts
  // an app that is already on screen.
  if (isDeliveryUsable(config) && (isConfigFetchedAfterMount || !isConfigRefreshing)) {
    return <>{children}</>;
  }

  if (isLoading) {
    return <LoadingSpinner />;
  }

  if (error || !stored.isPresent || status === 'configured') {
    return <>{children}</>;
  }

  if (isConfigPending || isConfigRefreshing) {
    return <LoadingSpinner />;
  }

  return <SetupPrompt isAdmin={isAdmin} />;
}
