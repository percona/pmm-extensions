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

// PMM Extensions theme — sourced from @percona/percona-ui.
// SepTheme inherits BaseTheme (Poppins headings, semantic tokens) and layers
// PMM Extensions brand (purple/yellow), AppBar override, and technology palette.

// Example of how to customize the theme further with overrides and/or new tokens (this is for debugging purposes, do not delete):

// import { typographyClasses } from '@mui/material/Typography';
import { sepThemeOptions as sepThemeOptionsOriginal } from '@percona/percona-ui';
import type { PaletteMode, ThemeOptions } from '@mui/material';
import { EXTENSIONS_TABLE_CLASS } from '@pmm-extensions/framework';

import { deepmerge } from '@mui/utils';

// TODO: remove temporary overrides below once percona-ui ships the upstream fix (percona-ui PR #20).
const sepThemeOptions = (mode: PaletteMode): ThemeOptions => {
  const newOptions: ThemeOptions = {
    components: {
      MuiButton: {
        styleOverrides: {
          outlined: ({ theme }) => ({
            ...(theme.palette.mode === 'light' && {
              backgroundColor: theme.palette.background.paper,
            }),
          }),
        },
        variants: [
          { props: { variant: 'contained', color: 'success' }, style: { color: '#fff' } },
          { props: { variant: 'contained', color: 'error' }, style: { color: '#fff' } },
          // Only override warning text color in light mode; dark mode keeps MUI's computed contrastText.
          ...(mode === 'light'
            ? [
                {
                  props: { variant: 'contained' as const, color: 'warning' as const },
                  style: { color: '#fff' },
                },
              ]
            : []),
          { props: { variant: 'contained', color: 'info' }, style: { color: '#fff' } },
        ],
      },
      MuiPaper: {
        styleOverrides: {
          root: ({ theme }) => ({
            ...(theme.palette.mode === 'light' && {
              [`&.${EXTENSIONS_TABLE_CLASS} .MuiToolbar-root`]: {
                backgroundColor: theme.palette.background.paper,
              },
              // Exclude running rows so action.hover highlight still applies.
              [`&.${EXTENSIONS_TABLE_CLASS} .MuiTableRow-root:not([data-running="true"])`]: {
                backgroundColor: theme.palette.background.paper + ' !important',
              },
            }),
          }),
        },
      },
      // This is needed to fix the bg color on Input, Select, Autocomplete, etc. This is on purpose.
      MuiInputBase: {
        styleOverrides: {
          root: ({ theme }) => ({
            ...(theme.palette.mode === 'light' && {
              backgroundColor: theme.palette.background.paper,
            }),
          }),
        },
      },
      MuiChip: {
        styleOverrides: {
          root: ({ theme, ownerState }) => ({
            ...(theme.palette.mode === 'light' &&
              ownerState.variant === 'filled' &&
              ownerState.color === 'default' && {
                // Default filled chip blends into page bg — add visible surface.
                backgroundColor: theme.palette.action.selected,
              }),
          }),
        },
      },
      MuiDrawer: {
        styleOverrides: {
          root: () => ({
            [`& .MuiList-root .MuiCollapse-root .MuiListItemIcon-root`]: {
              minWidth: '33px',
            },
          }),
        },
      },
    },
  };

  return deepmerge<ThemeOptions>(sepThemeOptionsOriginal(mode), newOptions);
};
export { sepThemeOptions };
