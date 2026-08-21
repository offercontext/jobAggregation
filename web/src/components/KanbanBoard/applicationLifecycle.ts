import dayjs from 'dayjs';
import type { Application, ApplicationInput, ApplicationStatus } from '@/types/application';
import { KANBAN_COLUMNS, STATUS_LABELS } from '@/types/application';
import type { ScheduleEvent } from '@/types/event';
import { EVENT_TYPE_LABELS } from '@/types/event';

export type ApplicationSortBy = 'updated_desc' | 'updated_asc' | 'applied_desc' | 'applied_asc';

export interface ApplicationViewState {
  keyword: string;
  status: ApplicationStatus | 'all';
  sortBy: ApplicationSortBy;
}

export const DEFAULT_APPLICATION_VIEW_STATE: ApplicationViewState = {
  keyword: '',
  status: 'all',
  sortBy: 'updated_desc',
};

export interface ApplicationListFilter {
  keyword: string;
  status: ApplicationStatus | 'all';
  sortBy: ApplicationSortBy;
}

export const PILOT_CONTEXT_DROP_ID = 'pilot-context-drop';

export type KanbanDropDestination =
  | { kind: 'status'; status: ApplicationStatus }
  | { kind: 'pilot' };

export function resolveKanbanDropDestination(dropTargetId: string): KanbanDropDestination | null {
  if (dropTargetId === PILOT_CONTEXT_DROP_ID) return { kind: 'pilot' };
  if (KANBAN_STATUS_SET.has(dropTargetId as ApplicationStatus)) {
    return { kind: 'status', status: dropTargetId as ApplicationStatus };
  }
  return null;
}

export function requiresClosedReason(currentStatus: ApplicationStatus, nextStatus: ApplicationStatus): boolean {
  return currentStatus !== 'closed' && nextStatus === 'closed';
}

export function willRecordFirstStatusTimestamp(app: Application, nextStatus: ApplicationStatus): boolean {
  const fieldByStatus: Record<ApplicationStatus, keyof Application> = {
    pending: 'first_pending_at',
    applied: 'first_applied_at',
    written_test: 'first_written_test_at',
    interview: 'first_interview_at',
    offer: 'first_offer_at',
    closed: 'closed_at',
  };
  return !app[fieldByStatus[nextStatus]];
}

export function buildApplicationStatusPayload(
  app: Application,
  nextStatus: ApplicationStatus,
  closedReason = ''
): Partial<ApplicationInput> {
  return {
    company_name: app.company_name,
    position_name: app.position_name,
    job_url: app.job_url,
    status: nextStatus,
    notes: app.notes,
    ...(nextStatus === 'closed' ? { closed_reason: closedReason.trim() } : {}),
  };
}

export function filterAndSortApplications(apps: Application[], filter: ApplicationListFilter): Application[] {
  const tokens = filter.keyword
    .trim()
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean);

  return apps
    .filter((app) => !app.deleted_at)
    .filter((app) => filter.status === 'all' || app.status === filter.status)
    .filter((app) => {
      if (tokens.length === 0) return true;
      const haystack = [
        app.company_name,
        app.position_name,
        app.job_url,
        app.notes,
        app.source,
        STATUS_LABELS[app.status],
      ]
        .join(' ')
        .toLowerCase();
      return tokens.every((token) => haystack.includes(token));
    })
    .sort((a, b) => compareApplications(a, b, filter.sortBy));
}

export function formatNextApplicationEvent(
  app: Application,
  events: ScheduleEvent[],
  now = dayjs().toISOString()
): string {
  const cursor = dayjs(now);
  const next = events
    .filter((event) => event.application_id === app.id)
    .filter((event) => dayjs(event.scheduled_at).isSame(cursor) || dayjs(event.scheduled_at).isAfter(cursor))
    .sort((a, b) => dayjs(a.scheduled_at).valueOf() - dayjs(b.scheduled_at).valueOf())[0];

  if (!next) return '暂无下一事件';
  return `${EVENT_TYPE_LABELS[next.event_type]} · ${dayjs(next.scheduled_at).format('YYYY-MM-DD HH:mm')}`;
}

function compareApplications(a: Application, b: Application, sortBy: ApplicationSortBy): number {
  if (sortBy === 'updated_asc') return compareDate(a.updated_at, b.updated_at, true, a.id, b.id);
  if (sortBy === 'applied_desc') return compareDate(a.applied_at, b.applied_at, false, a.id, b.id);
  if (sortBy === 'applied_asc') return compareDate(a.applied_at, b.applied_at, true, a.id, b.id);
  return compareDate(a.updated_at, b.updated_at, false, a.id, b.id);
}

const KANBAN_STATUS_SET = new Set(KANBAN_COLUMNS);

function compareDate(aDate: string, bDate: string, asc: boolean, aId: number, bId: number): number {
  const aValue = dayjs(aDate).valueOf();
  const bValue = dayjs(bDate).valueOf();
  const primary = asc ? aValue - bValue : bValue - aValue;
  return primary === 0 ? bId - aId : primary;
}
