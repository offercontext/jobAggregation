/** The closed, read-only lifecycle vocabulary used by all interview surfaces. */
export type EventLifecycleV1 = 'scheduled' | 'in_progress' | 'completed' | 'cancelled' | 'unknown';

const SCHEDULED_STATUSES = new Set(['todo', 'pending', 'scheduled']);
const COMPLETED_STATUSES = new Set(['done', 'completed']);
const CANCELLED_STATUSES = new Set(['cancelled', 'deleted', 'soft_deleted']);

/**
 * Classifies only the source status. In particular, this function deliberately
 * does not inspect dates: a stale `todo` is still scheduled until its source
 * status is changed.
 */
export function classifyEventLifecycleV1(status: unknown): EventLifecycleV1 {
  if (typeof status !== 'string' || status.length === 0) return 'unknown';
  if (SCHEDULED_STATUSES.has(status)) return 'scheduled';
  if (status === 'in_progress') return 'in_progress';
  if (COMPLETED_STATUSES.has(status)) return 'completed';
  if (CANCELLED_STATUSES.has(status)) return 'cancelled';
  return 'unknown';
}

/** Canonical short alias retained for consumers that do not need the version suffix. */
export const classifyEventLifecycle = classifyEventLifecycleV1;

// Re-export the surface vocabulary from the classifier module so consumers can
// depend on one interview-events entry point without introducing another rule.
export type { InterviewEventBucket } from './interviewEventCard';
export type { InterviewContractReason } from './interviewIndexContract';
