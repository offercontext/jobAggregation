import { classifyEventLifecycleV1, type EventLifecycleV1 } from './eventLifecycle';
import type { NormalizedInterviewIndexItem } from './interviewIndexContract';

export type InterviewEventBucket = 'upcoming' | 'completed' | 'cancelled' | 'needs_status_update' | 'unavailable';

export type InterviewEventPrimaryAction =
  | 'prepare'
  | 'enter_preparation'
  | 'record_review'
  | 'view_review'
  | 'update_status'
  | 'none';

export type InterviewEventSecondaryAction = 'view_application' | 'retry';

export interface InterviewEventCardModel {
  readonly applicationId: number;
  readonly eventId: number;
  readonly companyName: string;
  readonly positionName: string;
  readonly scheduledAt: string | null;
  readonly scheduledAtTimestamp: number;
  readonly durationMinutes: number | null;
  readonly lifecycle: EventLifecycleV1;
  readonly bucket: InterviewEventBucket;
  readonly primaryAction: InterviewEventPrimaryAction;
  readonly secondaryAction: InterviewEventSecondaryAction;
  readonly secondaryActions: readonly InterviewEventSecondaryAction[];
  readonly noteId: number | null;
  readonly contractReasons: NormalizedInterviewIndexItem['contractReasons'];
}

function freezeCard(card: InterviewEventCardModel): InterviewEventCardModel {
  Object.freeze(card.secondaryActions);
  return Object.freeze(card);
}

function terminalCard(item: NormalizedInterviewIndexItem, lifecycle: EventLifecycleV1, bucket: 'completed' | 'cancelled'): InterviewEventCardModel {
  const hasReview = item.note_id !== null;
  return freezeCard({
    applicationId: item.application_id ?? 0,
    eventId: item.event_id ?? 0,
    companyName: item.company_name,
    positionName: item.position_name,
    scheduledAt: item.scheduleValid ? item.scheduled_at : null,
    scheduledAtTimestamp: item.scheduleTimestamp ?? Number.POSITIVE_INFINITY,
    durationMinutes: item.durationValid ? item.duration_minutes : null,
    lifecycle,
    bucket,
    primaryAction: bucket === 'completed' ? (hasReview ? 'view_review' : 'record_review') : 'none',
    secondaryAction: 'view_application',
    secondaryActions: ['view_application'],
    noteId: item.note_id,
    contractReasons: item.contractReasons,
  });
}

function unavailableCard(item: NormalizedInterviewIndexItem, lifecycle: EventLifecycleV1): InterviewEventCardModel {
  return freezeCard({
    applicationId: item.application_id ?? 0,
    eventId: item.event_id ?? 0,
    companyName: item.company_name,
    positionName: item.position_name,
    scheduledAt: null,
    scheduledAtTimestamp: Number.POSITIVE_INFINITY,
    durationMinutes: null,
    lifecycle,
    bucket: 'unavailable',
    primaryAction: 'none',
    secondaryAction: 'retry',
    secondaryActions: ['retry', 'view_application'],
    noteId: item.note_id,
    contractReasons: item.contractReasons,
  });
}

/** Projects one immutable index row. Status is authoritative; time is only for active rows. */
export function projectInterviewEventCard(item: NormalizedInterviewIndexItem, now: number): InterviewEventCardModel {
  const lifecycle = classifyEventLifecycleV1(item.event_status);
  if (item.application_id === null || item.event_id === null) return unavailableCard(item, lifecycle);
  if (item.sourceMismatch) return unavailableCard(item, lifecycle);
  if (lifecycle === 'completed') return terminalCard(item, lifecycle, 'completed');
  if (lifecycle === 'cancelled') return terminalCard(item, lifecycle, 'cancelled');
  if (!Number.isFinite(now)) return unavailableCard(item, lifecycle);
  if (!item.preparation_available) return unavailableCard(item, lifecycle);
  if (lifecycle === 'unknown' || !item.scheduleValid || !item.durationValid || item.scheduled_at_state !== 'present') {
    return unavailableCard(item, lifecycle);
  }

  const startsAt = item.scheduleTimestamp;
  if (startsAt === null) return unavailableCard(item, lifecycle);
  const bucket: InterviewEventBucket = lifecycle === 'scheduled'
    ? startsAt > now ? 'upcoming' : 'needs_status_update'
    : now <= startsAt + (item.duration_minutes ?? 0) * 60_000 ? 'upcoming' : 'needs_status_update';
  const primaryAction: InterviewEventPrimaryAction = bucket === 'upcoming'
    ? lifecycle === 'in_progress' ? 'enter_preparation' : 'prepare'
    : 'update_status';
  return freezeCard({
    applicationId: item.application_id,
    eventId: item.event_id,
    companyName: item.company_name,
    positionName: item.position_name,
    scheduledAt: item.scheduled_at,
    scheduledAtTimestamp: startsAt,
    durationMinutes: item.duration_minutes,
    lifecycle,
    bucket,
    primaryAction,
    secondaryAction: 'view_application',
    secondaryActions: ['view_application'],
    noteId: item.note_id,
    contractReasons: item.contractReasons,
  });
}

/** Stable order for card lists; equal business times are ordered by numeric event identity. */
export function compareInterviewEventCards(left: InterviewEventCardModel, right: InterviewEventCardModel): number {
  const timestampRank = (value: unknown): number => {
    if (typeof value !== 'number') return 3;
    if (Number.isNaN(value)) return 3;
    if (value === Number.NEGATIVE_INFINITY) return 0;
    if (Number.isFinite(value)) return 1;
    return 2;
  };
  const compareNumber = (a: unknown, b: unknown): number => {
    const leftValue = typeof a === 'number' && Number.isFinite(a) ? a : 0;
    const rightValue = typeof b === 'number' && Number.isFinite(b) ? b : 0;
    return leftValue < rightValue ? -1 : leftValue > rightValue ? 1 : 0;
  };
  const leftRank = timestampRank(left.scheduledAtTimestamp);
  const rightRank = timestampRank(right.scheduledAtTimestamp);
  if (leftRank !== rightRank) return leftRank < rightRank ? -1 : 1;
  if (leftRank === 1) {
    const time = compareNumber(left.scheduledAtTimestamp, right.scheduledAtTimestamp);
    if (time !== 0) return time;
  }
  const eventId = compareNumber(left.eventId, right.eventId);
  if (eventId !== 0) return eventId;
  const applicationId = compareNumber(left.applicationId, right.applicationId);
  if (applicationId !== 0) return applicationId;
  const lifecycleOrder: Record<EventLifecycleV1, number> = {
    scheduled: 0,
    in_progress: 1,
    completed: 2,
    cancelled: 3,
    unknown: 4,
  };
  const leftLifecycleOrder = lifecycleOrder[left.lifecycle] ?? lifecycleOrder.unknown;
  const rightLifecycleOrder = lifecycleOrder[right.lifecycle] ?? lifecycleOrder.unknown;
  const lifecycle = leftLifecycleOrder - rightLifecycleOrder;
  if (lifecycle !== 0) return lifecycle < 0 ? -1 : 1;
  const stableString = (a: unknown, b: unknown): number => {
    const leftString = typeof a === 'string' ? a : '';
    const rightString = typeof b === 'string' ? b : '';
    return leftString < rightString ? -1 : leftString > rightString ? 1 : 0;
  };
  for (const [a, b] of [
    [left.bucket, right.bucket],
    [left.companyName, right.companyName],
    [left.positionName, right.positionName],
    [left.scheduledAt, right.scheduledAt],
    [left.primaryAction, right.primaryAction],
    [left.secondaryAction, right.secondaryAction],
  ] as const) {
    const result = stableString(a, b);
    if (result !== 0) return result;
  }
  const duration = compareNumber(left.durationMinutes, right.durationMinutes);
  if (duration !== 0) return duration;
  return 0;
}
