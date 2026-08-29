import type { EventLifecycleV1 } from '@/features/interviewEvents/eventLifecycle';
import { parseCoreTaskRef, type CoreTaskId, type CoreTaskRef } from '@/features/coreTaskSurface/contracts';
import type { ApplicationStatus } from '@/types/application';

export type TaskAvailability = 'ready' | 'loading' | 'blocked' | 'waiting_confirmation' | 'result_unknown' | 'unavailable';

export type TaskSource<T> =
  | { readonly status: 'ready'; readonly value: T }
  | { readonly status: 'loading' }
  | { readonly status: 'error'; readonly reason?: string }
  | { readonly status: 'absent' };

export interface ApplicationTaskApplication {
  readonly id: number;
  readonly status: ApplicationStatus;
  readonly deleted?: boolean;
  readonly deletedAt?: string | null;
  readonly stale?: boolean;
  readonly sourceMismatch?: boolean;
}

export interface ApplicationTaskEvent {
  readonly applicationId: number;
  readonly eventId: number;
  readonly lifecycle: EventLifecycleV1;
  readonly bucket: 'upcoming' | 'completed' | 'cancelled' | 'needs_status_update' | 'unavailable';
  readonly primaryAction: 'prepare' | 'enter_preparation' | 'record_review' | 'view_review' | 'update_status' | 'none';
  readonly scheduledAtTimestamp: number | null;
  readonly durationMinutes: number | null;
  readonly scheduledAtState?: 'present' | 'absent';
  readonly sourceMismatch?: boolean;
  readonly deleted?: boolean;
  readonly stale?: boolean;
}

export interface ApplicationTaskReview {
  readonly applicationId: number;
  readonly eventId: number | null;
  readonly reviewId?: number;
  readonly deleted?: boolean;
  readonly stale?: boolean;
  readonly sourceMismatch?: boolean;
}

export interface ApplicationTaskMaterialKit {
  readonly applicationId: number;
  readonly status: 'draft' | 'ready' | 'submitted' | string;
  readonly deleted?: boolean;
  readonly stale?: boolean;
  readonly sourceMismatch?: boolean;
  readonly updatedAt?: number | string | null;
}

export interface ApplicationTaskOffer {
  readonly id: number;
  readonly applicationId: number;
  readonly status: 'pending' | 'negotiating' | 'accepted' | 'declined' | 'expired' | string;
  readonly deadline?: number | string | null;
  readonly deleted?: boolean;
  readonly stale?: boolean;
  readonly sourceMismatch?: boolean;
}

export interface ApplicationTaskJd {
  readonly id?: number;
  readonly versionId?: number;
}

export interface ApplicationTaskResume {
  readonly id?: number;
  readonly selected?: boolean;
  readonly deleted?: boolean;
}

export interface ApplicationTaskFit {
  readonly reviewId?: number;
  readonly status?: string;
}

export interface ApplicationTaskPending {
  readonly ref?: unknown;
  readonly taskId?: unknown;
  readonly applicationId?: unknown;
  readonly eventId?: unknown;
}

export interface FrozenApplicationTaskSnapshot {
  readonly application: TaskSource<ApplicationTaskApplication>;
  readonly jd: TaskSource<ApplicationTaskJd | null>;
  readonly events: TaskSource<readonly ApplicationTaskEvent[]>;
  readonly offers: TaskSource<readonly ApplicationTaskOffer[]>;
  readonly materialKit: TaskSource<ApplicationTaskMaterialKit | null>;
  readonly reviews: TaskSource<readonly ApplicationTaskReview[]>;
  readonly fit: TaskSource<ApplicationTaskFit | null>;
  readonly resume: TaskSource<ApplicationTaskResume | null>;
  readonly pending: TaskSource<ApplicationTaskPending | null>;
  readonly resultUnknown: TaskSource<ApplicationTaskPending | null>;
}

export type ApplicationTaskReason =
  | 'pending_confirmation' | 'result_unknown' | 'pending_identity_invalid'
  | 'interview_preparation_available' | 'interview_review_missing' | 'interview_review_available'
  | 'material_kit_incomplete' | 'offer_review_pending' | 'opportunity_fit' | 'material_kit_missing'
  | 'record_outcome' | 'general_review' | 'source_loading' | 'source_error' | 'source_absent'
  | 'source_mismatch' | 'application_deleted' | 'entity_deleted' | 'foreign_pending'
  | 'duplicate_identity_conflict' | 'event_contract_invalid' | 'event_status_needs_update';

export interface ApplicationTaskModel {
  readonly taskId: CoreTaskId | null;
  readonly ref: CoreTaskRef | null;
  readonly availability: TaskAvailability;
  readonly reason: ApplicationTaskReason;
  readonly reasonCode: ApplicationTaskReason;
  readonly primary: boolean;
  readonly executable: boolean;
  readonly businessTime: number | null;
}

export interface ApplicationTaskResolution {
  readonly tasks: readonly ApplicationTaskModel[];
  readonly primaryTask: ApplicationTaskModel | null;
  readonly primary: ApplicationTaskModel | null;
  readonly hasExecutableTask: boolean;
  readonly hasLoading: boolean;
  readonly hasUnavailable: boolean;
}

type RuntimeSource = { state: 'ready' | 'loading' | 'error' | 'absent'; value: unknown; malformed?: boolean };
type InternalTask = ApplicationTaskModel & { readonly priority: number; readonly identity: number | null };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function safeId(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0 ? value : null;
}

function isApplicationStatus(value: unknown): value is ApplicationStatus {
  return value === 'pending' || value === 'applied' || value === 'written_test' || value === 'interview' || value === 'offer' || value === 'closed';
}

function finiteTime(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string') {
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function readSource(value: unknown): RuntimeSource {
  if (!isRecord(value)) return { state: 'error', value: undefined, malformed: true };
  const status = value.status;
  const hasValue = Object.prototype.hasOwnProperty.call(value, 'value');
  if (status === 'ready') return hasValue
    ? { state: 'ready', value: value.value }
    : { state: 'error', value: undefined, malformed: true };
  if (status === 'loading') return hasValue ? { state: 'error', value: undefined, malformed: true } : { state: 'loading', value: undefined };
  if (status === 'error') return hasValue ? { state: 'error', value: undefined, malformed: true } : { state: 'error', value: undefined };
  if (status === 'absent') return hasValue ? { state: 'error', value: undefined, malformed: true } : { state: 'absent', value: undefined };
  return { state: 'error', value: undefined, malformed: true };
}

function sourceAvailability(source: RuntimeSource, absent: TaskAvailability = 'blocked'): TaskAvailability {
  if (source.state === 'loading') return 'loading';
  if (source.state === 'error') return 'unavailable';
  if (source.state === 'absent') return absent;
  return 'ready';
}

function eventLifecycle(event: ApplicationTaskEvent): EventLifecycleV1 {
  switch (event.lifecycle) {
    case 'scheduled':
    case 'in_progress':
    case 'completed':
    case 'cancelled':
    case 'unknown':
      return event.lifecycle;
    default:
      return 'unknown';
  }
}

function stableJson(value: unknown): string {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(stableJson).sort().join(',') + ']';
  return '{' + Object.keys(value as object).sort().map((key) => JSON.stringify(key) + ':' + stableJson((value as Record<string, unknown>)[key])).join(',') + '}';
}

function makeTask(taskId: CoreTaskId | null, ref: CoreTaskRef | null, availability: TaskAvailability, reason: ApplicationTaskReason, priority: number, businessTime: number | null = null, identity: number | null = null): InternalTask {
  return {
    taskId,
    ref,
    availability,
    reason,
    reasonCode: reason,
    primary: false,
    executable: taskId !== null && ref !== null && (availability === 'ready' || availability === 'waiting_confirmation' || availability === 'result_unknown'),
    businessTime,
    priority,
    identity,
  };
}

function taskKey(task: InternalTask): string {
  return task.ref === null ? `blocker:${task.reason}` : [task.ref.taskId, task.ref.applicationId, task.ref.eventId, task.ref.resumeId, task.ref.storyId, task.ref.sourceId].join(':');
}

function compareTask(left: InternalTask, right: InternalTask): number {
  const leftMissing = left.businessTime === null ? 1 : 0;
  const rightMissing = right.businessTime === null ? 1 : 0;
  if (leftMissing !== rightMissing) return leftMissing - rightMissing;
  if (left.businessTime !== null && right.businessTime !== null && left.businessTime !== right.businessTime) return left.businessTime - right.businessTime;
  const leftId = left.identity ?? Number.MAX_SAFE_INTEGER;
  const rightId = right.identity ?? Number.MAX_SAFE_INTEGER;
  return leftId - rightId || (left.taskId ?? '').localeCompare(right.taskId ?? '');
}

function parseRef(value: unknown): CoreTaskRef | null {
  if (!isRecord(value)) return null;
  const parsed = parseCoreTaskRef(value);
  return parsed.ok ? parsed.ref : null;
}

function pendingRef(value: ApplicationTaskPending | null): CoreTaskRef | null {
  if (!value) return null;
  const direct = parseRef(value.ref);
  if (direct) return direct;
  const candidate: Record<string, unknown> = { taskId: value.taskId };
  if (value.applicationId !== undefined) candidate.applicationId = value.applicationId;
  if (value.eventId !== undefined) candidate.eventId = value.eventId;
  return parseRef(candidate);
}

/** Resolves immutable, already-loaded facts into one canonical Application task surface. */
export function resolveApplicationTasks(snapshot: FrozenApplicationTaskSnapshot, now: number): ApplicationTaskResolution {
  if (!Number.isFinite(now)) throw new RangeError('now must be finite');
  const sources = {
    application: readSource(snapshot?.application),
    jd: readSource(snapshot?.jd),
    events: readSource(snapshot?.events),
    offers: readSource(snapshot?.offers),
    materialKit: readSource(snapshot?.materialKit),
    reviews: readSource(snapshot?.reviews),
    fit: readSource(snapshot?.fit),
    resume: readSource(snapshot?.resume),
    pending: readSource(snapshot?.pending),
    resultUnknown: readSource(snapshot?.resultUnknown),
  };
  const eventsMalformed = sources.events.state === 'ready' && !Array.isArray(sources.events.value);
  const offersMalformed = sources.offers.state === 'ready' && !Array.isArray(sources.offers.value);
  const reviewsMalformed = sources.reviews.state === 'ready' && !Array.isArray(sources.reviews.value);
  const materialMalformed = sources.materialKit.state === 'ready' && sources.materialKit.value !== null && !isRecord(sources.materialKit.value);
  const hasLoading = Object.values(sources).some((source) => source.state === 'loading');
  const hasUnavailableSource = Object.values(sources).some((source) => source.state === 'error' || source.state === 'absent' || source.malformed)
    || eventsMalformed || offersMalformed || reviewsMalformed || materialMalformed;
  const application = isRecord(sources.application.value) ? sources.application.value : null;
  const appId = safeId(application?.id);
  const tasks: InternalTask[] = [];
  const add = (task: InternalTask): void => {
    if (tasks.some((item) => taskKey(item) === taskKey(task))) return;
    tasks.push(task);
  };
  if (sources.application.state !== 'ready' || appId === null || application === null) {
    const blockerReason: ApplicationTaskReason = sources.application.state === 'loading' ? 'source_loading' : sources.application.state === 'absent' ? 'source_absent' : 'source_error';
    add(makeTask(null, null, sources.application.state === 'loading' ? 'loading' : 'unavailable', blockerReason, 1));
  } else if (!isApplicationStatus(application.status)) {
    add(makeTask(null, null, 'unavailable', 'source_error', 1));
  } else if (application.deleted || application.deletedAt || application.stale || application.sourceMismatch) {
    add(makeTask(null, null, 'unavailable', application.deleted || application.deletedAt ? 'application_deleted' : 'source_mismatch', 1));
  } else {
    const appStatus = application.status;
    const pending = sources.pending.state === 'ready' && isRecord(sources.pending.value) ? sources.pending.value as ApplicationTaskPending : null;
    const unknown = sources.resultUnknown.state === 'ready' && isRecord(sources.resultUnknown.value) ? sources.resultUnknown.value as ApplicationTaskPending : null;
    const pendingValue = pending ?? unknown;
    const pendingKind = pending ? 'waiting_confirmation' : 'result_unknown';
    const pendingSourceBlock = sources.pending.state !== 'ready' || sources.resultUnknown.state !== 'ready';
    if (pendingValue) {
      const ref = pendingRef(pendingValue);
      const refApp = ref?.applicationId ?? null;
      const eventValues = sources.events.state === 'ready' && Array.isArray(sources.events.value) ? sources.events.value : [];
      const eventOwned = ref?.eventId === undefined || eventValues.some((item) => isRecord(item) && safeId(item.eventId) === ref.eventId && safeId(item.applicationId) === appId);
      if (ref && refApp === appId && ref.taskId.startsWith('application.') && eventOwned) {
        add(makeTask(ref.taskId, ref, pendingKind, pendingKind === 'waiting_confirmation' ? 'pending_confirmation' : 'result_unknown', 1, null, ref.eventId ?? ref.applicationId ?? null));
      } else {
        add(makeTask(null, null, 'unavailable', 'pending_identity_invalid', 1));
      }
    } else if (pendingSourceBlock) {
      const sourceState = sources.pending.state !== 'ready' ? sources.pending : sources.resultUnknown;
      add(makeTask(null, null, sourceState.state === 'loading' ? 'loading' : 'unavailable', sourceState.state === 'loading' ? 'source_loading' : sourceState.state === 'absent' ? 'source_absent' : 'source_error', 1));
    }

    const reviews = sources.reviews.state === 'ready' && Array.isArray(sources.reviews.value) ? sources.reviews.value : [];
    const reviewedEvents = new Set<number>();
    let generalReview = false;
    for (const raw of reviews) {
      if (!isRecord(raw) || safeId(raw.applicationId) !== appId || raw.deleted || raw.stale || raw.sourceMismatch) continue;
      const reviewEventId = raw.eventId === null ? null : safeId(raw.eventId);
      if (reviewEventId === null) generalReview = true;
      else reviewedEvents.add(reviewEventId);
    }
    const events = sources.events.state === 'ready' && Array.isArray(sources.events.value) ? sources.events.value : [];
    const eventById = new Map<number, ApplicationTaskEvent>();
    const conflictIds = new Set<number>();
    for (const raw of events) {
      if (!isRecord(raw)) continue;
      const id = safeId(raw.eventId);
      if (id === null || safeId(raw.applicationId) !== appId) continue;
      const current = eventById.get(id);
      if (current && stableJson(current) !== stableJson(raw)) {
        add(makeTask(null, null, 'unavailable', 'duplicate_identity_conflict', 2, null, id));
        conflictIds.add(id);
        eventById.delete(id);
      } else if (!current && !conflictIds.has(id)) {
        eventById.set(id, raw as unknown as ApplicationTaskEvent);
      }
    }
    for (const event of eventById.values()) {
      const lifecycle = eventLifecycle(event);
      if (lifecycle === 'unknown') {
        add(makeTask(null, null, 'unavailable', 'event_contract_invalid', 2, null, event.eventId));
        continue;
      }
      if (lifecycle === 'cancelled') continue;
      if (event.sourceMismatch || event.deleted || event.stale) {
        add(makeTask(lifecycle === 'completed' ? 'application.interview_review' : 'application.interview_prepare', { taskId: lifecycle === 'completed' ? 'application.interview_review' : 'application.interview_prepare', applicationId: appId, eventId: event.eventId }, 'unavailable', event.deleted ? 'entity_deleted' : 'source_mismatch', 99, null, event.eventId));
        continue;
      }
      const durationValid = typeof event.durationMinutes === 'number' && Number.isInteger(event.durationMinutes) && event.durationMinutes >= 1 && event.durationMinutes <= 10080;
      const timeValid = typeof event.scheduledAtTimestamp === 'number' && Number.isFinite(event.scheduledAtTimestamp) && event.scheduledAtState !== 'absent';
      const end = timeValid && durationValid ? event.scheduledAtTimestamp + event.durationMinutes * 60_000 : null;
      if (lifecycle === 'completed') {
        const availability = reviewsMalformed ? 'unavailable' : sources.reviews.state === 'ready' ? 'ready' : sources.reviews.state === 'loading' ? 'loading' : 'unavailable';
        const reason = availability === 'ready' && reviewedEvents.has(event.eventId) ? 'interview_review_available' : 'interview_review_missing';
        add(makeTask('application.interview_review', { taskId: 'application.interview_review', applicationId: appId, eventId: event.eventId }, availability, reason, reason === 'interview_review_missing' ? 3 : 7, timeValid ? event.scheduledAtTimestamp : null, event.eventId));
      } else if (lifecycle === 'scheduled' || lifecycle === 'in_progress') {
        if (event.scheduledAtState !== undefined && event.scheduledAtState !== 'present' && event.scheduledAtState !== 'absent') {
          add(makeTask('application.interview_prepare', { taskId: 'application.interview_prepare', applicationId: appId, eventId: event.eventId }, 'unavailable', 'event_contract_invalid', 99, null, event.eventId));
          continue;
        }
        if (event.bucket === 'needs_status_update') {
          add(makeTask(null, null, 'unavailable', 'event_status_needs_update', 2, null, event.eventId));
          continue;
        }
        const actionAllowed = event.primaryAction === 'prepare' || event.primaryAction === 'enter_preparation';
        const inWindow = lifecycle === 'in_progress'
          ? end !== null && now <= end
          : timeValid && event.scheduledAtTimestamp > now && event.scheduledAtTimestamp - now <= 24 * 60 * 60_000;
        if (event.bucket !== 'upcoming' && event.bucket !== 'unavailable') {
          add(makeTask('application.interview_prepare', { taskId: 'application.interview_prepare', applicationId: appId, eventId: event.eventId }, 'unavailable', 'event_contract_invalid', 99, null, event.eventId));
        } else if (event.bucket === 'upcoming' && actionAllowed && durationValid && timeValid && inWindow) {
          add(makeTask('application.interview_prepare', { taskId: 'application.interview_prepare', applicationId: appId, eventId: event.eventId }, 'ready', 'interview_preparation_available', 2, event.scheduledAtTimestamp, event.eventId));
        } else if (!durationValid || !timeValid || !actionAllowed || event.bucket === 'unavailable') {
          add(makeTask('application.interview_prepare', { taskId: 'application.interview_prepare', applicationId: appId, eventId: event.eventId }, 'unavailable', 'event_contract_invalid', 99, null, event.eventId));
        }
      }
    }

    const material = sources.materialKit.state === 'ready' && isRecord(sources.materialKit.value) ? sources.materialKit.value : null;
    if (material) {
      const materialOwner = safeId(material.applicationId);
      if (materialOwner !== appId || material.deleted || material.stale || material.sourceMismatch) {
        add(makeTask('application.material_kit', { taskId: 'application.material_kit', applicationId: appId }, 'unavailable', material.sourceMismatch || materialOwner !== appId ? 'source_mismatch' : 'entity_deleted', 99));
      } else if (material.status !== 'draft' && material.status !== 'ready' && material.status !== 'submitted') {
        add(makeTask('application.material_kit', { taskId: 'application.material_kit', applicationId: appId }, 'unavailable', 'source_mismatch', 99));
      } else if (material.status !== 'submitted') {
        add(makeTask('application.material_kit', { taskId: 'application.material_kit', applicationId: appId }, 'ready', 'material_kit_incomplete', 4, finiteTime(material.updatedAt), appId));
      }
    } else if (materialMalformed) {
      add(makeTask('application.material_kit', { taskId: 'application.material_kit', applicationId: appId }, 'unavailable', 'source_error', 99));
    }

    let offerOwnerMismatch = false;
    const offers = sources.offers.state === 'ready' && Array.isArray(sources.offers.value) ? sources.offers.value : [];
    const unresolved: Array<{ offer: ApplicationTaskOffer; deadline: number | null }> = [];
    for (const raw of offers) {
      if (!isRecord(raw)) { offerOwnerMismatch = true; continue; }
      const id = safeId(raw.id);
      const owner = safeId(raw.applicationId);
      const invalid = id === null || owner !== appId || raw.deleted || raw.stale || raw.sourceMismatch;
      if (invalid) { offerOwnerMismatch = true; continue; }
      if (raw.status !== 'pending' && raw.status !== 'negotiating' && raw.status !== 'accepted' && raw.status !== 'declined' && raw.status !== 'expired') { offerOwnerMismatch = true; continue; }
      if (raw.status === 'pending' || raw.status === 'negotiating') unresolved.push({ offer: raw as unknown as ApplicationTaskOffer, deadline: finiteTime(raw.deadline) });
    }
    if (unresolved.length > 0) {
      const ordered = [...unresolved].sort((a, b) => (a.deadline === null ? 1 : b.deadline === null ? -1 : a.deadline - b.deadline) || a.offer.id - b.offer.id);
      add(makeTask('application.offer_review', { taskId: 'application.offer_review', applicationId: appId }, 'ready', 'offer_review_pending', 5, ordered[0].deadline, ordered[0].offer.id));
    } else if (offerOwnerMismatch) {
      add(makeTask('application.offer_review', { taskId: 'application.offer_review', applicationId: appId }, 'unavailable', 'source_mismatch', 5));
    }

    const jd = sources.jd;
    const resume = sources.resume;
    const dependencyAvailability = (required: readonly RuntimeSource[]): TaskAvailability => {
      if (required.some((source) => source.state === 'loading')) return 'loading';
      if (required.some((source) => source.state === 'error')) return 'unavailable';
      if (required.some((source) => source.state === 'absent' || (source.state === 'ready' && source.value === null))) return 'blocked';
      return 'ready';
    };
    if (appStatus === 'pending' && sources.fit.state !== 'ready') {
      add(makeTask('application.opportunity_fit', { taskId: 'application.opportunity_fit', applicationId: appId }, sourceAvailability(sources.fit), sources.fit.state === 'loading' ? 'source_loading' : sources.fit.state === 'error' ? 'source_error' : 'source_absent', 6));
    } else if (appStatus === 'pending' && sources.fit.state === 'ready' && sources.fit.value === null) {
      add(makeTask('application.opportunity_fit', { taskId: 'application.opportunity_fit', applicationId: appId }, dependencyAvailability([jd, resume]), 'opportunity_fit', 6));
    }
    const materialExists = material !== null;
    if ((appStatus === 'applied' || appStatus === 'written_test') && !materialExists && sources.materialKit.state === 'ready' && !materialMalformed) {
      add(makeTask('application.material_kit', { taskId: 'application.material_kit', applicationId: appId }, dependencyAvailability([jd, resume]), 'material_kit_missing', 6));
    }
    if (appStatus === 'offer' && (sources.offers.state !== 'ready' || offersMalformed)) {
      add(makeTask('application.offer_review', { taskId: 'application.offer_review', applicationId: appId }, offersMalformed ? 'unavailable' : sourceAvailability(sources.offers, 'unavailable'), offersMalformed ? 'source_error' : sources.offers.state === 'loading' ? 'source_loading' : sources.offers.state === 'error' ? 'source_error' : 'source_absent', 6));
    } else if (appStatus === 'offer' && sources.offers.state === 'ready' && !offersMalformed && !offerOwnerMismatch && unresolved.length === 0) {
      add(makeTask('application.offer_review', { taskId: 'application.offer_review', applicationId: appId }, 'ready', 'offer_review_pending', 6));
    }
    if (appStatus === 'closed') add(makeTask('application.record_outcome', { taskId: 'application.record_outcome', applicationId: appId }, 'ready', 'record_outcome', 6, null, appId));
    if (generalReview && sources.reviews.state === 'ready') add(makeTask('application.general_review', { taskId: 'application.general_review', applicationId: appId }, 'ready', 'general_review', 7, null, appId));
    if ((sources.events.state !== 'ready' || eventsMalformed) && appStatus === 'interview') add(makeTask(null, null, sources.events.state === 'loading' ? 'loading' : 'unavailable', sources.events.state === 'loading' ? 'source_loading' : sources.events.state === 'absent' ? 'source_absent' : 'source_error', 2));
    if ((sources.reviews.state !== 'ready' || reviewsMalformed) && events.some((item) => isRecord(item) && item.lifecycle === 'completed')) add(makeTask(null, null, sources.reviews.state === 'loading' ? 'loading' : 'unavailable', sources.reviews.state === 'loading' ? 'source_loading' : sources.reviews.state === 'absent' ? 'source_absent' : 'source_error', 3));
  }
  const deduped = new Map<string, InternalTask>();
  for (const task of tasks) {
    const key = taskKey(task);
    const existing = deduped.get(key);
    if (!existing || task.priority < existing.priority) deduped.set(key, task);
  }
  const ordered = [...deduped.values()].sort((a, b) => a.priority - b.priority || compareTask(a, b));
  const first = ordered.find((task) => task.executable);
  const blocking = ordered.find((task) => !task.executable);
  const primaryInternal = first && (!blocking || blocking.priority > first.priority) ? first : null;
  const frozenTasks = ordered.map((task) => Object.freeze({
    taskId: task.taskId,
    ref: task.ref === null ? null : Object.freeze({ ...task.ref }),
    availability: task.availability,
    reason: task.reason,
    reasonCode: task.reasonCode,
    primary: task === primaryInternal,
    executable: task.executable,
    businessTime: task.businessTime,
  }));
  const primaryTask = frozenTasks.find((task) => task.primary) ?? null;
  return Object.freeze({
    tasks: Object.freeze(frozenTasks),
    primaryTask,
    primary: primaryTask,
    hasExecutableTask: primaryTask !== null,
    hasLoading,
    hasUnavailable: hasUnavailableSource || frozenTasks.some((task) => task.availability === 'unavailable' || task.availability === 'blocked'),
  });
}
