import { classifyEventLifecycleV1, type EventLifecycleV1 } from '@/features/interviewEvents/eventLifecycle';
import type { ApplicationStatus } from '@/types/application';
import type { CoreTaskId, CoreTaskRef } from '@/features/coreTaskSurface/contracts';

export type TaskAvailability = 'ready' | 'loading' | 'blocked' | 'waiting_confirmation' | 'result_unknown' | 'unavailable';

/**
 * Read-only input for the application task projector.  The deliberately
 * structural shape lets API adapters add JSON fields without making this
 * pure module depend on a service or repository contract.
 */
export interface FrozenApplicationTaskSnapshot {
  readonly application?: unknown;
  readonly jd?: unknown;
  readonly events?: unknown;
  readonly offers?: unknown;
  readonly materialKit?: unknown;
  readonly material_kit?: unknown;
  readonly reviews?: unknown;
  readonly fitReview?: unknown;
  readonly pending?: unknown;
  readonly resultUnknown?: unknown;
  readonly resume?: unknown;
  readonly [key: string]: unknown;
}

export type ApplicationTaskReason =
  | 'pending_confirmation'
  | 'result_unknown'
  | 'interview_preparation_available'
  | 'interview_review_missing'
  | 'interview_review_available'
  | 'material_kit_incomplete'
  | 'offer_review_pending'
  | 'opportunity_fit'
  | 'material_kit_missing'
  | 'record_outcome'
  | 'general_review'
  | 'source_loading'
  | 'source_error'
  | 'source_absent'
  | 'source_mismatch'
  | 'application_deleted'
  | 'entity_deleted'
  | 'identity_mismatch'
  | 'foreign_pending';

export interface ApplicationTaskModel {
  readonly taskId: CoreTaskId;
  readonly ref: CoreTaskRef;
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

type SourceState = 'ready' | 'loading' | 'error' | 'absent';
type SourceView = { state: SourceState; value: unknown; reason: ApplicationTaskReason | null };

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function sourceView(value: unknown): SourceView {
  if (value === undefined) return { state: 'absent', value: undefined, reason: 'source_absent' };
  const wrapped = record(value);
  if (!wrapped || !Object.prototype.hasOwnProperty.call(wrapped, 'status') && !Object.prototype.hasOwnProperty.call(wrapped, 'state')) {
    return { state: 'ready', value, reason: null };
  }
  const status = wrapped.status ?? wrapped.state;
  const hasPayload = Object.prototype.hasOwnProperty.call(wrapped, 'value')
    || Object.prototype.hasOwnProperty.call(wrapped, 'data')
    || Object.prototype.hasOwnProperty.call(wrapped, 'items');
  if (status === 'pending' && !hasPayload) return { state: 'ready', value, reason: null };
  if (status === 'unknown' && Object.prototype.hasOwnProperty.call(wrapped, 'reason')) {
    return { state: 'loading', value: undefined, reason: 'source_loading' };
  }
  if (status === 'ready' || status === 'success' || status === 'known' || status === 'loaded') {
    return { state: 'ready', value: wrapped.value ?? wrapped.data ?? wrapped.items ?? null, reason: null };
  }
  if (status === 'loading' || status === 'pending') return { state: 'loading', value: undefined, reason: 'source_loading' };
  if (status === 'error' || status === 'failed') return { state: 'error', value: undefined, reason: 'source_error' };
  if (status === 'absent' || status === 'missing' || status === 'not_found') return { state: 'absent', value: null, reason: 'source_absent' };
  if (status === 'unavailable' || status === 'mismatch' || status === 'stale') return { state: 'error', value: undefined, reason: 'source_mismatch' };
  // Domain rows also commonly have a `status` field (for example a draft
  // Material Kit or an Application status).  Without a wrapper payload those
  // are already-loaded records, not failed source envelopes.
  if (!hasPayload) {
    return { state: 'ready', value, reason: null };
  }
  return { state: 'error', value: undefined, reason: 'source_error' };
}

function asId(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0 ? value : null;
}

function identity(value: Record<string, unknown>, ...keys: string[]): number | null {
  for (const key of keys) {
    const id = asId(value[key]);
    if (id !== null) return id;
  }
  return null;
}

function arrayValue(value: unknown): readonly unknown[] {
  return Array.isArray(value) ? value : [];
}

function finiteTime(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string') {
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function eventLifecycle(value: Record<string, unknown>): EventLifecycleV1 {
  // The lifecycle field is accepted from Task 4's card projection. Raw event
  // rows still go through the one canonical classifier; no local alias set is
  // maintained here.
  const projected = value.lifecycle;
  if (projected === 'scheduled' || projected === 'in_progress' || projected === 'completed' || projected === 'cancelled' || projected === 'unknown') {
    return projected;
  }
  return classifyEventLifecycleV1(value.event_status ?? value.status);
}

function freezeTask(task: ApplicationTaskModel): ApplicationTaskModel {
  return Object.freeze({ ...task, ref: Object.freeze({ ...task.ref }) });
}

function compareTasks(left: ApplicationTaskModel, right: ApplicationTaskModel): number {
  const time = (left.businessTime === null ? 1 : 0) - (right.businessTime === null ? 1 : 0);
  if (time !== 0) return time;
  if (left.businessTime !== null && right.businessTime !== null && left.businessTime !== right.businessTime) {
    return left.businessTime - right.businessTime;
  }
  const leftIdentity = left.ref.eventId ?? left.ref.applicationId ?? left.ref.offerId ?? left.ref.resumeId ?? left.ref.storyId ?? left.ref.sourceId ?? Number.MAX_SAFE_INTEGER;
  const rightIdentity = right.ref.eventId ?? right.ref.applicationId ?? right.ref.offerId ?? right.ref.resumeId ?? right.ref.storyId ?? right.ref.sourceId ?? Number.MAX_SAFE_INTEGER;
  return leftIdentity - rightIdentity || left.taskId.localeCompare(right.taskId);
}

function uniqueTaskKey(task: ApplicationTaskModel): string {
  const ref = task.ref;
  return [ref.taskId, ref.applicationId, ref.eventId, ref.resumeId, ref.storyId, ref.sourceId].join(':');
}

function appStatus(value: Record<string, unknown>): ApplicationStatus | null {
  const status = value.status;
  return status === 'pending' || status === 'applied' || status === 'written_test' || status === 'interview' || status === 'offer' || status === 'closed'
    ? status
    : null;
}

function taskFor(
  taskId: CoreTaskId,
  ref: CoreTaskRef,
  availability: TaskAvailability,
  reason: ApplicationTaskReason,
  priority: number,
  businessTime: number | null = null,
): ApplicationTaskModel & { readonly _priority: number } {
  return {
    taskId,
    ref,
    availability,
    reason,
    reasonCode: reason,
    primary: false,
    executable: availability === 'ready' || availability === 'waiting_confirmation' || availability === 'result_unknown',
    businessTime,
    _priority: priority,
  };
}

/** Pure, deterministic application task resolution. */
export function resolveApplicationTasks(snapshot: FrozenApplicationTaskSnapshot, now: number): ApplicationTaskResolution {
  if (!Number.isFinite(now)) throw new RangeError('now must be finite');
  const appSource = sourceView(snapshot.application);
  const app = record(appSource.value);
  const appId = app ? identity(app, 'id', 'applicationId', 'application_id') : null;
  const appDeleted = Boolean(app?.deleted_at ?? app?.deleted ?? app?.is_deleted);
  const appStale = Boolean(app?.stale ?? app?.source_mismatch ?? app?.identity_mismatch);
  const tasks: Array<ApplicationTaskModel & { readonly _priority: number }> = [];
  const add = (task: ApplicationTaskModel & { readonly _priority: number }) => {
    if (appId === null) return;
    if (tasks.some((item) => uniqueTaskKey(item) === uniqueTaskKey(task))) return;
    tasks.push(task);
  };

  if (appId === null || appDeleted || appStale) {
    if (appId !== null) add(taskFor('application.general_review', { taskId: 'application.general_review', applicationId: appId }, 'unavailable', appDeleted ? 'application_deleted' : 'source_mismatch', 99));
  } else {
    const jdSource = sourceView(snapshot.jd);
    const resumeSource = sourceView(snapshot.resume);
    const pendingSource = sourceView(snapshot.pending ?? snapshot.pendingAction);
    const pending = record(pendingSource.value);
    const pendingAppId = pending ? identity(pending, 'applicationId', 'application_id') : null;
    const pendingStatus = pending?.status ?? pending?.attempt_status;
    const resultUnknownSource = sourceView(snapshot.resultUnknown ?? snapshot.result_unknown);
    const resultUnknown = record(resultUnknownSource.value);
    const resultAppId = resultUnknown ? identity(resultUnknown, 'applicationId', 'application_id') : null;
    const pendingTaskId = pending?.taskId ?? pending?.task_id;
    const isTaskId = (value: unknown): value is CoreTaskId => typeof value === 'string' && value.startsWith('application.') && value !== 'application.interview_prepare' || value === 'application.interview_prepare';
    if (pendingSource.state === 'ready' && pending && pendingAppId === appId && pendingStatus !== 'result_unknown' && pendingStatus !== 'unknown' && pendingStatus !== 'provider_unknown') {
      const taskId = isTaskId(pendingTaskId) ? pendingTaskId : 'application.general_review';
      add(taskFor(taskId, { taskId, applicationId: appId }, 'waiting_confirmation', 'pending_confirmation', 1));
    } else if (pendingSource.state === 'ready' && pending && pendingAppId === appId && (pendingStatus === 'result_unknown' || pendingStatus === 'provider_unknown' || pendingStatus === 'unknown')) {
      const taskId = isTaskId(pendingTaskId) ? pendingTaskId : 'application.general_review';
      add(taskFor(taskId, { taskId, applicationId: appId }, 'result_unknown', 'result_unknown', 1));
    } else if (resultUnknownSource.state === 'ready' && resultUnknown && resultAppId === appId) {
      const taskId = isTaskId(resultUnknown.taskId) ? resultUnknown.taskId : 'application.general_review';
      add(taskFor(taskId, { taskId, applicationId: appId }, 'result_unknown', 'result_unknown', 1));
    } else if (pendingSource.state === 'ready' && pending && pendingAppId !== null && pendingAppId !== appId) {
      add(taskFor('application.general_review', { taskId: 'application.general_review', applicationId: appId }, 'unavailable', 'foreign_pending', 99));
    }

    const eventsSource = sourceView(snapshot.events);
    const events = eventsSource.state === 'ready' ? arrayValue(eventsSource.value) : [];
    const reviewsSource = sourceView(snapshot.reviews ?? snapshot.fitReview);
    const reviews = reviewsSource.state === 'ready' ? arrayValue(reviewsSource.value) : [];
    const reviewedEventIds = new Set<number>();
    let hasGeneralReview = false;
    for (const item of reviews) {
      const review = record(item);
      if (!review) continue;
      const reviewAppId = identity(review, 'applicationId', 'application_id');
      if (reviewAppId !== appId) continue;
      const eventId = identity(review, 'eventId', 'event_id', 'application_event_id');
      if (eventId === null) hasGeneralReview = true;
      else reviewedEventIds.add(eventId);
    }
    const validEvents: Array<{ item: Record<string, unknown>; id: number; lifecycle: EventLifecycleV1; start: number | null; end: number | null }> = [];
    for (const raw of events) {
      const event = record(raw);
      if (!event) continue;
      const id = identity(event, 'eventId', 'event_id', 'id');
      const owner = identity(event, 'applicationId', 'application_id');
      if (id === null || owner !== appId) continue;
      if (event.deleted_at || event.deleted || event.source_mismatch || event.sourceMismatch || event.identity_mismatch) {
        const taskId: CoreTaskId = eventLifecycle(event) === 'completed' ? 'application.interview_review' : 'application.interview_prepare';
        add(taskFor(taskId, { taskId, applicationId: appId, eventId: id }, 'unavailable', event.source_mismatch || event.sourceMismatch || event.identity_mismatch ? 'source_mismatch' : 'entity_deleted', 99));
        continue;
      }
      const lifecycle = eventLifecycle(event);
      if (lifecycle === 'cancelled' || lifecycle === 'unknown') continue;
      const start = event.scheduled_at_state === 'absent'
        ? null
        : finiteTime(event.scheduledAtTimestamp ?? event.scheduled_at ?? event.scheduledAt);
      const duration = typeof event.duration_minutes === 'number' && Number.isInteger(event.duration_minutes) && event.duration_minutes > 0 && event.duration_minutes <= 10080 ? event.duration_minutes : null;
      validEvents.push({ item: event, id, lifecycle, start, end: start !== null && duration !== null ? start + duration * 60_000 : null });
    }
    for (const event of validEvents) {
      const preparationAvailable = event.item.preparation_available === true || event.item.preparationAvailable === true || event.item.primaryAction === 'prepare' || event.item.primaryAction === 'enter_preparation';
      const within24h = event.start !== null
        && ((event.start >= now && event.start - now <= 24 * 60 * 60_000)
          || (event.lifecycle === 'in_progress' && event.end !== null && event.end >= now && now - event.start <= 24 * 60 * 60_000));
      if ((event.lifecycle === 'scheduled' || event.lifecycle === 'in_progress') && preparationAvailable && within24h) {
        const availability: TaskAvailability = jdSource.state === 'loading' || resumeSource.state === 'loading'
          ? 'loading'
          : jdSource.state === 'error' || resumeSource.state === 'error'
            ? 'unavailable'
            : jdSource.state === 'absent' ? 'blocked' : 'ready';
        add(taskFor('application.interview_prepare', { taskId: 'application.interview_prepare', applicationId: appId, eventId: event.id }, availability, availability === 'loading' ? 'source_loading' : availability === 'unavailable' ? 'source_error' : 'interview_preparation_available', 2, event.start));
      } else if (event.lifecycle === 'completed') {
        add(taskFor('application.interview_review', { taskId: 'application.interview_review', applicationId: appId, eventId: event.id }, 'ready', reviewedEventIds.has(event.id) ? 'interview_review_available' : 'interview_review_missing', reviewedEventIds.has(event.id) ? 7 : 3, event.start));
      }
    }
    const materialSource = sourceView(snapshot.materialKit ?? snapshot.material_kit);
    const material = record(materialSource.value);
    const materialComplete = material?.complete === true || material?.is_complete === true || material?.status === 'complete' || material?.status === 'submitted' || material?.status === 'ready';
    if (materialSource.state === 'ready' && material && !materialComplete) {
      add(taskFor('application.material_kit', { taskId: 'application.material_kit', applicationId: appId }, 'ready', 'material_kit_incomplete', 4, finiteTime(material.updated_at)));
    }
    const offersSource = sourceView(snapshot.offers);
    let hasOfferMismatch = false;
    if (offersSource.state === 'ready') {
      const offers = arrayValue(offersSource.value).map(record).filter((value): value is Record<string, unknown> => value !== null).filter((offer) => {
        const owner = identity(offer, 'applicationId', 'application_id');
        const unresolved = ['pending', 'negotiating'].includes(String(offer.status));
        if (unresolved && owner !== null && owner !== appId) hasOfferMismatch = true;
        return (owner === null || owner === appId) && unresolved;
      });
      if (offers.length > 0) {
        const deadlines = offers.map((offer) => finiteTime(offer.deadline)).filter((value): value is number => value !== null);
        add(taskFor('application.offer_review', { taskId: 'application.offer_review', applicationId: appId }, 'ready', 'offer_review_pending', 5, deadlines.length > 0 ? Math.min(...deadlines) : null));
      }
      else if (hasOfferMismatch) add(taskFor('application.offer_review', { taskId: 'application.offer_review', applicationId: appId }, 'unavailable', 'source_mismatch', 5));
    }
    const status = appStatus(app as Record<string, unknown>);
    const stageEvent = validEvents.find((event) => {
      const preparationAvailable = event.item.preparation_available === true || event.item.preparationAvailable === true || event.item.primaryAction === 'prepare' || event.item.primaryAction === 'enter_preparation';
      const within24h = event.start !== null
        && ((event.start >= now && event.start - now <= 24 * 60 * 60_000)
          || (event.lifecycle === 'in_progress' && event.end !== null && event.end >= now && now - event.start <= 24 * 60 * 60_000));
      return preparationAvailable && within24h;
    });
    const hasEventForStage = stageEvent !== undefined;
    const stageTask: { id: CoreTaskId; reason: ApplicationTaskReason } | null = status === 'pending'
      ? { id: 'application.opportunity_fit', reason: 'opportunity_fit' }
      : status === 'applied' || status === 'written_test'
        ? { id: 'application.material_kit', reason: 'material_kit_missing' }
        : status === 'interview'
        ? (hasEventForStage ? { id: 'application.interview_prepare', reason: 'interview_preparation_available' } : null)
          : status === 'offer'
            ? { id: 'application.offer_review', reason: 'offer_review_pending' }
            : status === 'closed'
              ? { id: 'application.record_outcome', reason: 'record_outcome' }
              : null;
    if (stageTask) {
      const stageSourceLoading = (stageTask.id === 'application.interview_prepare' && eventsSource.state === 'loading')
        || (stageTask.id === 'application.offer_review' && offersSource.state === 'loading')
        || (stageTask.id === 'application.material_kit' && (materialSource.state === 'loading' || resumeSource.state === 'loading'));
      const stageSourceError = (stageTask.id === 'application.interview_prepare' && eventsSource.state === 'error')
        || (stageTask.id === 'application.offer_review' && (offersSource.state === 'error' || hasOfferMismatch))
        || (stageTask.id === 'application.material_kit' && (materialSource.state === 'error' || resumeSource.state === 'error'));
      const availability: TaskAvailability = stageSourceLoading || jdSource.state === 'loading' ? 'loading' : stageSourceError || jdSource.state === 'error' ? 'unavailable' : jdSource.state === 'absent' ? 'blocked' : 'ready';
      const stageRef: CoreTaskRef = stageTask.id === 'application.interview_prepare' && stageEvent
        ? { taskId: stageTask.id, applicationId: appId, eventId: stageEvent.id }
        : { taskId: stageTask.id, applicationId: appId };
      const materialAlreadyComplete = materialSource.state === 'ready' && material !== null && materialComplete;
      if (!(stageTask.id === 'application.material_kit' && materialAlreadyComplete)) {
        add(taskFor(stageTask.id, stageRef, availability, availability === 'loading' ? 'source_loading' : availability === 'unavailable' ? (jdSource.reason ?? 'source_error') : stageTask.reason, 6));
      }
    }
    if (hasGeneralReview) add(taskFor('application.general_review', { taskId: 'application.general_review', applicationId: appId }, 'ready', 'general_review', 7));
  }

  const unique = new Map<string, ApplicationTaskModel & { readonly _priority: number }>();
  for (const task of tasks) {
    const existing = unique.get(uniqueTaskKey(task));
    if (!existing || task._priority < existing._priority) unique.set(uniqueTaskKey(task), task);
  }
  const ordered = [...unique.values()].sort((a, b) => a._priority - b._priority || compareTasks(a, b));
  const firstExecutable = ordered.find((task) => task.executable && task.availability !== 'loading' && task.availability !== 'unavailable' && task.availability !== 'blocked') ?? null;
  const frozenTasks = ordered.map((task) => freezeTask({
    taskId: task.taskId,
    ref: task.ref,
    availability: task.availability,
    reason: task.reason,
    reasonCode: task.reasonCode,
    primary: task === firstExecutable,
    executable: task.executable,
    businessTime: task.businessTime,
  }));
  const primaryTask = frozenTasks.find((task) => task.primary) ?? null;
  return Object.freeze({
    tasks: Object.freeze(frozenTasks),
    primaryTask,
    primary: primaryTask,
    hasExecutableTask: firstExecutable !== null,
    hasLoading: frozenTasks.some((task) => task.availability === 'loading'),
    hasUnavailable: frozenTasks.some((task) => task.availability === 'unavailable'),
  });
}
