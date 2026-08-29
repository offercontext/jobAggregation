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

function terminalCard(item: NormalizedInterviewIndexItem, lifecycle: EventLifecycleV1, bucket: 'completed' | 'cancelled'): InterviewEventCardModel {
  const hasReview = item.note_id !== null;
  return {
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
  };
}

function unavailableCard(item: NormalizedInterviewIndexItem, lifecycle: EventLifecycleV1): InterviewEventCardModel {
  return {
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
  };
}

/** Projects one immutable index row. Status is authoritative; time is only for active rows. */
export function projectInterviewEventCard(item: NormalizedInterviewIndexItem, now: number): InterviewEventCardModel {
  const lifecycle = classifyEventLifecycleV1(item.event_status);
  if (item.application_id === null || item.event_id === null) return unavailableCard(item, lifecycle);
  if (item.sourceMismatch) return unavailableCard(item, lifecycle);
  if (lifecycle === 'completed') return terminalCard(item, lifecycle, 'completed');
  if (lifecycle === 'cancelled') return terminalCard(item, lifecycle, 'cancelled');
  if (!Number.isFinite(now)) return unavailableCard(item, lifecycle);
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
  return {
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
  };
}

/** Stable order for card lists; equal business times are ordered by numeric event identity. */
export function compareInterviewEventCards(left: InterviewEventCardModel, right: InterviewEventCardModel): number {
  return left.scheduledAtTimestamp - right.scheduledAtTimestamp || left.eventId - right.eventId;
}
