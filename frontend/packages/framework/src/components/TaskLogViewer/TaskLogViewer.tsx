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

import DownloadIcon from '@mui/icons-material/Download';
import Badge from '@mui/material/Badge';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import FormControl from '@mui/material/FormControl';
import FormControlLabel from '@mui/material/FormControlLabel';
import IconButton from '@mui/material/IconButton';
import MenuItem from '@mui/material/MenuItem';
import Paper from '@mui/material/Paper';
import Select from '@mui/material/Select';
import Stack from '@mui/material/Stack';
import Switch from '@mui/material/Switch';
import Tab from '@mui/material/Tab';
import Tabs from '@mui/material/Tabs';
import Tooltip from '@mui/material/Tooltip';
import Typography from '@mui/material/Typography';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useExecutionEvents } from '../../hooks/useExecutionEvents';
import { useLogDownload } from '../../hooks/useLogDownload';
import {
  useTaskLogs,
  type FinishStatus,
  type LogType,
  type StepText,
} from '../../hooks/useTaskLogs';
import { ExecutionEventsPanel } from './ExecutionEventsPanel';
import { LogOutputPane } from './LogOutputPane';
import { LogStepTabs } from './LogStepTabs';
import { StatusBadge, type BadgeStatus } from './StatusBadge';
import { StreamErrorBlock } from './StreamErrorBlock';

type TopTab = 'stdout' | 'stderr' | 'events';

export const LOG_TAIL_LINE_OPTIONS = [
  { label: '100', value: '100' },
  { label: '1000', value: '1000' },
  { label: '5000', value: '5000' },
  { label: 'All', value: 'all' },
] as const;

export type LogTailLineChoice = (typeof LOG_TAIL_LINE_OPTIONS)[number]['value'];

const NUMERIC_LOG_TAIL_OPTIONS = LOG_TAIL_LINE_OPTIONS.map((option) => Number(option.value)).filter(
  (value) => Number.isFinite(value),
);

/**
 * Smallest numeric cap on offer. A proven-complete log at or below this size
 * looks identical under every option, so the select has nothing left to do.
 * Derived from the options list so changing the list moves the threshold.
 *
 * Falls back to 0 when the list holds no numeric option: `Math.min()` of an
 * empty list is Infinity, which would hide the select for every finished task.
 */
const SMALLEST_LOG_TAIL_OPTION =
  NUMERIC_LOG_TAIL_OPTIONS.length > 0 ? Math.min(...NUMERIC_LOG_TAIL_OPTIONS) : 0;

const LOG_TAIL_STORAGE_KEY = 'extensions.taskLogViewer.tail';

const DEFAULT_LOG_TAIL_CHOICE = '1000' satisfies LogTailLineChoice;

function readStoredLogTailChoice(): LogTailLineChoice {
  if (globalThis.localStorage === undefined) {
    return DEFAULT_LOG_TAIL_CHOICE;
  }
  const stored = globalThis.localStorage.getItem(LOG_TAIL_STORAGE_KEY);
  if (stored === 'all') {
    return 'all';
  }
  if (stored === '100' || stored === '1000' || stored === '5000') {
    return stored;
  }
  return DEFAULT_LOG_TAIL_CHOICE;
}

function logTailChoiceToParam(choice: LogTailLineChoice): number | undefined {
  return choice === 'all' ? undefined : Number(choice);
}

export interface TaskLogViewerProps {
  taskHistoryId: number | string;
  taskStatus?: string;
  height?: number | string;
}

/**
 * The terminal statuses a `finish` frame is supposed to carry. The frame is
 * parsed without validation, so a live log is only treated as complete when
 * its status is one of these. Keyed on the union so a new member cannot be
 * added without deciding it here.
 */
const TERMINAL_FINISH_STATUS: Record<FinishStatus, true> = {
  success: true,
  failed: true,
  stopped: true,
  lost: true,
  stale: true,
  unlaunchable: true,
};

/** Where the uncapped stream a running history opened stands. */
interface LiveLog {
  historyId: TaskLogViewerProps['taskHistoryId'];
  state: 'open' | 'complete' | 'ended';
}

function isRunningStatus(status?: string): boolean {
  return (status ?? '').toLowerCase() === 'running';
}

/**
 * Line count, saturating at `limit + 1`. Callers only need to know whether a
 * pane is over the threshold, so a large log stops being scanned as soon as it
 * provably is — no full pass over megabytes of "All lines" output.
 */
function countLinesUpTo(text: string, limit: number): number {
  if (text === '') {
    return 0;
  }
  let lines = 0;
  for (let index = 0; index < text.length; index += 1) {
    if (text[index] === '\n') {
      lines += 1;
      if (lines > limit) {
        return lines;
      }
    }
  }
  // A trailing fragment without its newline is still a line on screen.
  return text.endsWith('\n') ? lines : lines + 1;
}

/**
 * Largest line count across every step and both streams, saturating at
 * `limit + 1`. The line-cap decision uses this rather than the visible pane so
 * the control does not appear and disappear as the user moves between step or
 * stream tabs.
 */
function maxPaneLineCountUpTo(textByStep: Record<string, StepText>, limit: number): number {
  let max = 0;
  for (const pane of Object.values(textByStep)) {
    max = Math.max(max, countLinesUpTo(pane.stdout, limit), countLinesUpTo(pane.stderr, limit));
    if (max > limit) {
      return max;
    }
  }
  return max;
}

function resolveBadgeStatus(
  finishStatus: ReturnType<typeof useTaskLogs>['finishStatus'],
  error: ReturnType<typeof useTaskLogs>['error'],
): BadgeStatus | undefined {
  if (error) {
    return error.code === 410 ? 'executor-gone' : 'stream-error';
  }
  return finishStatus;
}

export function TaskLogViewer({ taskHistoryId, taskStatus, height = 480 }: TaskLogViewerProps) {
  const running = isRunningStatus(taskStatus);
  const [logTailChoice, setLogTailChoice] = useState<LogTailLineChoice>(readStoredLogTailChoice);
  // The uncapped stream a running history opened. Re-fetching it capped once
  // the polled status turns terminal only blanks the pane and loses the scroll
  // position, and that status can arrive before the stream's own `finish`, so
  // the stream is kept until it ends. Ending with a terminal `finish` means it
  // already holds the whole log and is kept for good; a stream cut short, or
  // whose `finish` is non-terminal, is reloaded once the run is over. Under
  // the "All" cap that reload has the same history and tail as the live
  // stream, so it is requested explicitly rather than through the tail.
  const [liveLog, setLiveLog] = useState<LiveLog | null>(null);
  const liveLogState =
    liveLog !== null && liveLog.historyId === taskHistoryId ? liveLog.state : undefined;
  const keepLiveLog = liveLogState === 'open' || liveLogState === 'complete';
  const reloadLiveLog = liveLogState === 'ended' && !running;
  const tailLines = logTailChoiceToParam(logTailChoice);
  const effectiveTailLines = running || keepLiveLog ? undefined : tailLines;
  const { textByStep, stepOrder, streamStatus, finishStatus, error, resumed } = useTaskLogs(
    taskHistoryId,
    effectiveTailLines,
    reloadLiveLog ? 1 : 0,
  );

  const liveLogHistoryIdRef = useRef(taskHistoryId);
  useEffect(() => {
    // On the render that switches histories, the stream state still belongs
    // to the previous stream: useTaskLogs only resets it in this same commit.
    if (liveLogHistoryIdRef.current !== taskHistoryId) {
      liveLogHistoryIdRef.current = taskHistoryId;
      setLiveLog(running ? { historyId: taskHistoryId, state: 'open' } : null);
      return;
    }
    // Only the stream opened while running is tracked, plus any other stream
    // that resumed: its offsets may be live ones the persisted log does not
    // share (no status is polled, or the run started after the viewer opened),
    // so it is reloaded too. Each settles once: the reload that follows an
    // `ended` stream must not be taken for it.
    if (streamStatus === 'finished' || streamStatus === 'error') {
      if (liveLogState === undefined && resumed && streamStatus === 'finished') {
        setLiveLog({ historyId: taskHistoryId, state: 'ended' });
        return;
      }
      if (liveLogState !== 'open') {
        return;
      }
      const complete =
        streamStatus === 'finished' &&
        !resumed &&
        finishStatus !== undefined &&
        Object.prototype.hasOwnProperty.call(TERMINAL_FINISH_STATUS, finishStatus);
      setLiveLog({ historyId: taskHistoryId, state: complete ? 'complete' : 'ended' });
    } else if (running && liveLogState === undefined) {
      setLiveLog({ historyId: taskHistoryId, state: 'open' });
    }
  }, [running, streamStatus, finishStatus, resumed, taskHistoryId, liveLogState]);
  const { eventsByStep, stepOrder: eventStepOrder } = useExecutionEvents(taskHistoryId, running);

  const [topTab, setTopTab] = useState<TopTab>('stdout');
  const [activeStep, setActiveStep] = useState<string | undefined>();
  const [wrap, setWrap] = useState(false);

  const [unreadTypes, setUnreadTypes] = useState<Set<LogType>>(new Set());
  const [unreadSteps, setUnreadSteps] = useState<Set<string>>(new Set());

  const prevLogSizesRef = useRef<Record<string, number>>({});

  // Reset view state when switching to a different task history
  useEffect(() => {
    setActiveStep(undefined);
    setUnreadTypes(new Set());
    setUnreadSteps(new Set());
    prevLogSizesRef.current = {};
  }, [taskHistoryId]);

  useEffect(() => {
    if (!activeStep && stepOrder.length > 0) {
      setActiveStep(stepOrder[0]);
    }
  }, [stepOrder, activeStep]);

  // Track unread notifications based on accumulated text growth
  useEffect(() => {
    for (const step of stepOrder) {
      const pane = textByStep[step];
      if (!pane) {
        continue;
      }
      (['stdout', 'stderr'] as const).forEach((type) => {
        const key = `${step}_${type}`;
        const size = pane[type].length;
        const prev = prevLogSizesRef.current[key] ?? 0;
        if (size > prev) {
          if (topTab !== type) {
            setUnreadTypes((prevSet) => {
              if (prevSet.has(type)) {
                return prevSet;
              }
              const next = new Set(prevSet);
              next.add(type);
              return next;
            });
          }
          if (activeStep !== step) {
            setUnreadSteps((prevSet) => {
              if (prevSet.has(step)) {
                return prevSet;
              }
              const next = new Set(prevSet);
              next.add(step);
              return next;
            });
          }
        }
        prevLogSizesRef.current[key] = size;
      });
    }
  }, [textByStep, stepOrder, topTab, activeStep]);

  const handleTopTab = (value: TopTab) => {
    setTopTab(value);
    // Execution events deliberately carry no unread indicator: they arrive over
    // SSE on every pushed event, and badging them pulled attention away from
    // stdout and stderr, which are the reason the console is open.
    if (value === 'events') {
      return;
    }
    setUnreadTypes((prev) => {
      if (!prev.has(value)) {
        return prev;
      }
      const next = new Set(prev);
      next.delete(value);
      return next;
    });
  };

  const handleStepSelect = (step: string) => {
    setActiveStep(step);
    setUnreadSteps((prev) => {
      if (!prev.has(step)) {
        return prev;
      }
      const next = new Set(prev);
      next.delete(step);
      return next;
    });
  };

  const stepsForTabs = useMemo(
    () => (topTab === 'events' ? eventStepOrder : stepOrder),
    [topTab, eventStepOrder, stepOrder],
  );

  const currentPaneText = useMemo(() => {
    if (topTab === 'events' || !activeStep) {
      return '';
    }
    const pane = textByStep[activeStep];
    if (!pane) {
      return '';
    }
    return pane[topTab] ?? '';
  }, [textByStep, activeStep, topTab]);

  const download = useLogDownload();
  const handleDownload = () => {
    if (topTab === 'events') {
      return;
    }
    const filename = `task-${taskHistoryId}-${activeStep ?? 'step'}-${topTab}.log`;
    download(filename, currentPaneText);
  };

  const handleLogTailChange = (choice: LogTailLineChoice) => {
    // Only a cap has to clear the live-log record to be fetched again. "All"
    // keeps it tracked, so a stream still open after the run ends is reloaded
    // if it ends cut short.
    if (logTailChoiceToParam(choice) !== undefined) {
      setLiveLog(null);
    }
    setLogTailChoice(choice);
    if (globalThis.localStorage !== undefined) {
      globalThis.localStorage.setItem(LOG_TAIL_STORAGE_KEY, choice);
    }
  };

  const badgeStatus = resolveBadgeStatus(finishStatus, error);

  // Hide the line cap once a finished history has streamed a log that is
  // provably complete and short enough that every option would show the same
  // thing. Gated on the terminal stream status so the control does not flicker
  // while lines are still arriving.
  const showLogTailSelect = useMemo(() => {
    if (running || streamStatus !== 'finished') {
      return true;
    }
    // Saturated at one over the threshold: any pane above it keeps the select
    // regardless of the requested cap, so the exact count no longer matters.
    const maxLines = maxPaneLineCountUpTo(textByStep, SMALLEST_LOG_TAIL_OPTION);
    if (maxLines > SMALLEST_LOG_TAIL_OPTION) {
      return true;
    }
    // A pane sitting exactly at the requested cap may have been trimmed
    // server-side, so only a count strictly below the cap proves completeness.
    return effectiveTailLines !== undefined && maxLines >= effectiveTailLines;
  }, [running, streamStatus, textByStep, effectiveTailLines]);

  return (
    <Paper variant="outlined" sx={{ display: 'flex', flexDirection: 'column' }}>
      <Stack
        direction="row"
        alignItems="center"
        sx={{ px: 1, pt: 1, borderBottom: 1, borderColor: 'divider' }}
      >
        <Tabs
          // The events view is not one of these tabs, so hand MUI `false`
          // rather than an out-of-range value: no tab reads as active and no
          // out-of-range warning is logged.
          value={topTab === 'events' ? false : topTab}
          onChange={(_, v: LogType) => handleTopTab(v)}
          sx={{ minHeight: 40 }}
        >
          <Tab
            value="stdout"
            // MUI gives every tab tabIndex -1 when no tab is selected, which
            // would strand keyboard users outside the strip while the events
            // view is open. Keep one entry point; arrow keys move from there.
            {...(topTab === 'events' ? { tabIndex: 0 } : {})}
            label={
              <Badge color="primary" variant="dot" invisible={!unreadTypes.has('stdout')}>
                <span>stdout</span>
              </Badge>
            }
          />
          <Tab
            value="stderr"
            label={
              <Badge color="primary" variant="dot" invisible={!unreadTypes.has('stderr')}>
                <span>stderr</span>
              </Badge>
            }
          />
        </Tabs>
        {/* Subordinate to the primary tabs, but still one click away. */}
        <Button
          size="small"
          color="inherit"
          onClick={() => handleTopTab('events')}
          // Not a toggle: a second click is a no-op and the way back is a
          // primary tab, so mark it as the current view rather than pressed.
          aria-current={topTab === 'events' ? 'true' : undefined}
          sx={{
            ml: 1,
            textTransform: 'none',
            color: topTab === 'events' ? 'text.primary' : 'text.secondary',
            bgcolor: topTab === 'events' ? 'action.selected' : 'transparent',
          }}
        >
          Execution events
        </Button>
        <Box sx={{ flex: 1 }} />
        <Stack direction="row" alignItems="center" spacing={1} sx={{ pr: 1 }}>
          {badgeStatus && <StatusBadge status={badgeStatus} />}
          {showLogTailSelect && (
            <Tooltip
              title={
                running
                  ? 'Line cap applies to finished task logs only'
                  : 'Limit how many lines are loaded from the server'
              }
            >
              <FormControl size="small" sx={{ minWidth: 96 }} disabled={running}>
                <Select
                  value={logTailChoice}
                  onChange={(event) => handleLogTailChange(event.target.value as LogTailLineChoice)}
                  aria-label="Log lines to show"
                  disabled={running}
                  renderValue={(value) => (
                    <Typography variant="body2" component="span">
                      {value === 'all' ? 'All lines' : `Last ${value}`}
                    </Typography>
                  )}
                  sx={{
                    '& .MuiSelect-select': { py: 0.75, display: 'flex', alignItems: 'center' },
                  }}
                >
                  {LOG_TAIL_LINE_OPTIONS.map((option) => (
                    <MenuItem key={option.value} value={option.value}>
                      {option.label === 'All' ? 'All lines' : `Last ${option.label}`}
                    </MenuItem>
                  ))}
                </Select>
              </FormControl>
            </Tooltip>
          )}
          <FormControlLabel
            control={
              <Switch size="small" checked={wrap} onChange={(_, checked) => setWrap(checked)} />
            }
            label="Wrap"
            slotProps={{ typography: { variant: 'body2' } }}
          />
          <Tooltip title="Download log">
            <span>
              <IconButton
                size="small"
                onClick={handleDownload}
                disabled={topTab === 'events' || !currentPaneText}
                aria-label="Download log"
              >
                <DownloadIcon fontSize="small" />
              </IconButton>
            </span>
          </Tooltip>
        </Stack>
      </Stack>

      {error && (
        <Box sx={{ p: 1 }}>
          <StreamErrorBlock error={error} />
        </Box>
      )}

      <Box sx={{ flex: 1, minHeight: 0 }}>
        {topTab === 'events' ? (
          <ExecutionEventsPanel
            eventsByStep={eventsByStep}
            activeStep={activeStep}
            height={height}
          />
        ) : (
          <LogOutputPane text={currentPaneText} wrap={wrap} height={height} />
        )}
      </Box>

      <Box sx={{ borderTop: 1, borderColor: 'divider', px: 1 }}>
        <LogStepTabs
          steps={stepsForTabs}
          activeStep={activeStep}
          unreadSteps={unreadSteps}
          onSelect={handleStepSelect}
        />
      </Box>
    </Paper>
  );
}
