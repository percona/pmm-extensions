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

import { useEffect, useState } from 'react';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import Grid from '@mui/material/Grid';
import IconButton from '@mui/material/IconButton';
import Stack from '@mui/material/Stack';
import Tooltip from '@mui/material/Tooltip';
import Typography from '@mui/material/Typography';
import HelpOutlineIcon from '@mui/icons-material/HelpOutline';
import {
  settingErrorMessage,
  usePatchSetting,
  useResetSetting,
  type SettingResponse,
} from '@pmm-extensions/api';

import { useNotification } from '../../contexts/notification';
import SettingEditField from './SettingEditField';
import SettingValueDisplay from './SettingValueDisplay';
import {
  type EditValue,
  isEditable,
  isSaveable,
  toInitialEditValue,
  toPatchValue,
} from './settingField';

export interface SettingRowProps {
  setting: SettingResponse;
}

/** One configuration field: value, reload indicator, help, edit + reset. */
export default function SettingRow({ setting }: SettingRowProps) {
  const { showSuccess, showError } = useNotification();
  const patch = usePatchSetting();
  const reset = useResetSetting();

  const [edit, setEdit] = useState<EditValue>(() => toInitialEditValue(setting));

  // Resync the edit control whenever the server value changes (e.g. after a
  // successful save/reset refetch). Keying on key+value keeps a freshly typed
  // secret from lingering once the backend has accepted it.
  useEffect(() => {
    setEdit(toInitialEditValue(setting));
    patch.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [setting.key, setting.setting_class, JSON.stringify(setting.value)]);

  const editable = isEditable(setting);
  const applicable = setting.is_applicable !== false;
  const fieldError = settingErrorMessage(patch.error, setting.key);
  const canSave = isSaveable(setting, edit) && !patch.isPending;
  const showReset = setting.has_override;

  const handleChange = (value: EditValue) => {
    setEdit(value);
    if (patch.error) {
      patch.reset();
    }
  };

  const handleSave = () => {
    patch.mutate(
      {
        settingClass: setting.setting_class,
        key: setting.key,
        value: toPatchValue(setting, edit),
      },
      {
        onSuccess: () => {
          // Drop the typed plaintext for secrets: the refetch returns the same
          // redacted literal, so the resync effect won't fire and the real
          // value would otherwise linger in the input and React state.
          if (setting.is_secret) {
            setEdit('');
          }
          showSuccess(`Saved ${setting.key}`);
        },
        onError: (err) => {
          // 422s render inline next to the field; anything else is a toast.
          if (!settingErrorMessage(err, setting.key)) {
            showError(err.message || `Failed to save ${setting.key}`);
          }
        },
      },
    );
  };

  const handleReset = () => {
    reset.mutate(
      { settingClass: setting.setting_class, key: setting.key },
      {
        onSuccess: () => showSuccess(`Reset ${setting.key} to default`),
        // A refused reset has no inline field to land on, so its 422 reason
        // goes in the toast rather than the bare status.
        onError: (err) =>
          showError(
            settingErrorMessage(err, setting.key) ||
              err.message ||
              `Failed to reset ${setting.key}`,
          ),
      },
    );
  };

  return (
    <Grid
      container
      spacing={2}
      alignItems="flex-start"
      sx={{ py: 1.5, borderBottom: 1, borderColor: 'divider', opacity: applicable ? 1 : 0.6 }}
      data-testid={`setting-row-${setting.key}`}
    >
      {/* Key + reload indicator + help */}
      <Grid size={{ xs: 12, md: 4 }} sx={{ minWidth: 0 }}>
        <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap">
          <Typography variant="body2" sx={{ fontFamily: 'monospace', fontWeight: 600 }}>
            {setting.key}
          </Typography>
          {setting.reload === 'hot' ? (
            <Tooltip title="Live-editable — takes effect immediately">
              <Chip size="small" color="success" label="Hot" />
            </Tooltip>
          ) : (
            <Tooltip title="Read-only — requires a settings.yaml edit and restart">
              <Chip size="small" variant="outlined" label="Not overridable" />
            </Tooltip>
          )}
          {setting.description ? (
            <Tooltip title={setting.description}>
              <IconButton size="small" aria-label={`Help for ${setting.key}`}>
                <HelpOutlineIcon fontSize="inherit" />
              </IconButton>
            </Tooltip>
          ) : null}
        </Stack>
        <Typography variant="caption" color="text.secondary">
          {setting.type}
        </Typography>
      </Grid>

      {/* Current value */}
      <Grid size={{ xs: 12, md: 3 }} sx={{ minWidth: 0 }}>
        <Typography variant="caption" color="text.secondary" display="block">
          Current
        </Typography>
        <SettingValueDisplay
          value={setting.value}
          settingKey={setting.key}
          options={setting.options}
        />
      </Grid>

      {/* Edit control + actions */}
      <Grid size={{ xs: 12, md: 5 }} sx={{ minWidth: 0 }}>
        {!applicable ? (
          // Inert under the current runtime state (e.g. a non-Grafana auth
          // provider). Show it disabled with the reason rather than hiding it,
          // so the admin sees the toggle exists and learns why it is inert.
          <Typography variant="caption" color="text.disabled">
            Not applicable with the current auth provider
          </Typography>
        ) : editable ? (
          <Stack spacing={1}>
            <SettingEditField
              setting={setting}
              value={edit}
              onChange={handleChange}
              disabled={patch.isPending}
              error={fieldError}
            />
            <Stack direction="row" spacing={1}>
              <Button size="small" variant="contained" disabled={!canSave} onClick={handleSave}>
                Save
              </Button>
              {showReset ? (
                <Button
                  size="small"
                  variant="outlined"
                  disabled={reset.isPending}
                  onClick={handleReset}
                >
                  Reset to default
                </Button>
              ) : null}
            </Stack>
          </Stack>
        ) : setting.is_complex ? (
          // A non-overridable nested submodel: its editable leaves (if any) are
          // expanded into their own rows by the backend, so anything still
          // flagged complex here cannot be overridden. Value is shown in the
          // Current column, so don't duplicate it — just explain the state.
          <Typography variant="caption" color="text.secondary">
            Read-only (nested submodel set via settings.yaml)
          </Typography>
        ) : (
          // NOT_OVERRIDABLE scalars get no edit affordance; value shown above.
          <Typography variant="caption" color="text.secondary">
            Read-only (set via settings.yaml)
          </Typography>
        )}
      </Grid>

      {!editable && showReset ? (
        <Grid size={12}>
          <Stack direction="row" spacing={1} alignItems="center">
            <Chip size="small" variant="outlined" label="override present" />
            <Box>
              <Button
                size="small"
                variant="outlined"
                disabled={reset.isPending}
                onClick={handleReset}
              >
                Reset to default
              </Button>
            </Box>
          </Stack>
        </Grid>
      ) : null}
    </Grid>
  );
}
