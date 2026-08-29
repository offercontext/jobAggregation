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
  readonly scheduledAtState: 'present' | 'absent';
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
  readonly status: 'draft' | 'ready' | 'submitted';
  readonly deleted?: boolean;
  readonly stale?: boolean;
  readonly sourceMismatch?: boolean;
  readonly updatedAt?: number | string | null;
}

export interface ApplicationTaskOffer {
  readonly id: number;
  readonly applicationId: number;
  readonly status: 'pending' | 'negotiating' | 'accepted' | 'declined' | 'expired';
  readonly deadline?: number | string | null;
  readonly deleted?: boolean;
  readonly stale?: boolean;
  readonly sourceMismatch?: boolean;
}

export interface ApplicationTaskJd { readonly id?: number; readonly versionId?: number }
export interface ApplicationTaskResume { readonly id?: number; readonly selected?: boolean; readonly deleted?: boolean }
export interface ApplicationTaskFit { readonly reviewId?: number; readonly status?: string }
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
  readonly taskId: CoreTaskId;
  readonly ref: CoreTaskRef;
  readonly availability: TaskAvailability;
  readonly reason: ApplicationTaskReason;
  readonly reasonCode: ApplicationTaskReason;
  readonly primary: boolean;
  readonly executable: boolean;
  readonly businessTime: number | null;
}

export interface ApplicationTaskIssue {
  readonly availability: Exclude<TaskAvailability, 'ready' | 'waiting_confirmation' | 'result_unknown'>;
  readonly reason: ApplicationTaskReason;
  readonly priority: number;
  readonly identity: number | null;
}

export interface ApplicationTaskResolution {
  readonly tasks: readonly ApplicationTaskModel[];
  readonly issues: readonly ApplicationTaskIssue[];
  readonly primaryTask: ApplicationTaskModel | null;
  readonly primary: ApplicationTaskModel | null;
  readonly hasExecutableTask: boolean;
  readonly hasLoading: boolean;
  readonly hasUnavailable: boolean;
}

type RuntimeSource = { state: 'ready' | 'loading' | 'error' | 'absent'; value: unknown; malformed?: boolean };
type InternalTask = ApplicationTaskModel & { readonly priority: number; readonly identity: number | null };
type InternalIssue = ApplicationTaskIssue;

function isRecord(value: unknown): value is Record<string, unknown> { return typeof value === 'object' && value !== null && !Array.isArray(value); }
function safeId(value: unknown): number | null { return typeof value === 'number' && Number.isSafeInteger(value) && value > 0 ? value : null; }
function isLifecycle(value: unknown): value is EventLifecycleV1 { return value === 'scheduled' || value === 'in_progress' || value === 'completed' || value === 'cancelled' || value === 'unknown'; }
function isBucket(value: unknown): boolean { return value === 'upcoming' || value === 'completed' || value === 'cancelled' || value === 'needs_status_update' || value === 'unavailable'; }
function isPrimaryAction(value: unknown): boolean { return value === 'prepare' || value === 'enter_preparation' || value === 'record_review' || value === 'view_review' || value === 'update_status' || value === 'none'; }
function finiteTime(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string') { const parsed = Date.parse(value); return Number.isFinite(parsed) ? parsed : null; }
  return null;
}
function readSource(value: unknown): RuntimeSource {
  if (!isRecord(value)) return { state: 'error', value: undefined, malformed: true };
  const status = value.status;
  const hasValue = Object.prototype.hasOwnProperty.call(value, 'value');
  if (status === 'ready') return hasValue ? { state: 'ready', value: value.value } : { state: 'error', value: undefined, malformed: true };
  if (status === 'loading') return hasValue ? { state: 'error', value: undefined, malformed: true } : { state: 'loading', value: undefined };
  if (status === 'error') return hasValue ? { state: 'error', value: undefined, malformed: true } : { state: 'error', value: undefined };
  if (status === 'absent') return hasValue ? { state: 'error', value: undefined, malformed: true } : { state: 'absent', value: undefined };
  return { state: 'error', value: undefined, malformed: true };
}
function stableJson(value: unknown): string {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(stableJson).sort().join(',') + ']';
  return '{' + Object.keys(value as object).sort().map((key) => JSON.stringify(key) + ':' + stableJson((value as Record<string, unknown>)[key])).join(',') + '}';
}
function makeTask(taskId: CoreTaskId, ref: CoreTaskRef, availability: TaskAvailability, reason: ApplicationTaskReason, priority: number, businessTime: number | null = null, identity: number | null = null): InternalTask {
  return { taskId, ref, availability, reason, reasonCode: reason, primary: false, executable: availability === 'ready' || availability === 'waiting_confirmation' || availability === 'result_unknown', businessTime, priority, identity };
}
function makeIssue(availability: InternalIssue['availability'], reason: ApplicationTaskReason, priority: number, identity: number | null = null): InternalIssue {
  return Object.freeze({ availability, reason, priority, identity });
}
function taskKey(task: InternalTask): string { return [task.ref.taskId, task.ref.applicationId, task.ref.eventId, task.ref.resumeId, task.ref.storyId, task.ref.sourceId].join(':'); }
function compareTask(left: InternalTask, right: InternalTask): number {
  if (left.businessTime === null && right.businessTime !== null) return 1;
  if (left.businessTime !== null && right.businessTime === null) return -1;
  if (left.businessTime !== null && right.businessTime !== null && left.businessTime !== right.businessTime) return left.businessTime - right.businessTime;
  return (left.identity ?? Number.MAX_SAFE_INTEGER) - (right.identity ?? Number.MAX_SAFE_INTEGER) || left.taskId.localeCompare(right.taskId);
}
function parseRef(value: unknown): CoreTaskRef | null { const parsed = parseCoreTaskRef(value); return parsed.ok ? parsed.ref : null; }
function pendingRef(value: ApplicationTaskPending | null): CoreTaskRef | null {
  if (!value || !isRecord(value)) return null;
  const hasOwn = (key: string): boolean => Object.prototype.hasOwnProperty.call(value, key);
  if (hasOwn('ref')) {
    const direct = parseRef(value.ref);
    if (!direct) return null;
    if (hasOwn('taskId') && (typeof value.taskId !== 'string' || value.taskId !== direct.taskId)) return null;
    if (hasOwn('applicationId') && safeId(value.applicationId) !== direct.applicationId) return null;
    if (hasOwn('eventId') && safeId(value.eventId) !== direct.eventId) return null;
    return direct;
  }
  const candidate: Record<string, unknown> = { taskId: value.taskId };
  if (value.applicationId !== undefined) candidate.applicationId = value.applicationId;
  if (value.eventId !== undefined) candidate.eventId = value.eventId;
  return parseRef(candidate);
}
function sameRef(left: CoreTaskRef, right: CoreTaskRef): boolean {
  return left.taskId === right.taskId && left.applicationId === right.applicationId && left.eventId === right.eventId
    && left.resumeId === right.resumeId && left.storyId === right.storyId && left.sourceId === right.sourceId;
}
function dependencyAvailability(required: readonly RuntimeSource[]): TaskAvailability {
  if (required.some((source) => source.state === 'loading')) return 'loading';
  if (required.some((source) => source.state === 'error' || source.malformed)) return 'unavailable';
  if (required.some((source) => source.state === 'absent' || (source.state === 'ready' && source.value === null))) return 'blocked';
  return 'ready';
}

export function resolveApplicationTasks(snapshot: FrozenApplicationTaskSnapshot, now: number): ApplicationTaskResolution {
  if (!Number.isFinite(now)) throw new RangeError('now must be finite');
  const sources = {
    application: readSource(snapshot?.application), jd: readSource(snapshot?.jd), events: readSource(snapshot?.events), offers: readSource(snapshot?.offers),
    materialKit: readSource(snapshot?.materialKit), reviews: readSource(snapshot?.reviews), fit: readSource(snapshot?.fit), resume: readSource(snapshot?.resume),
    pending: readSource(snapshot?.pending), resultUnknown: readSource(snapshot?.resultUnknown),
  };
  const issues: InternalIssue[] = [];
  const tasks: InternalTask[] = [];
  const addIssue = (issue: InternalIssue): void => { if (!issues.some((item) => item.reason === issue.reason && item.priority === issue.priority && item.identity === issue.identity)) issues.push(issue); };
  const addTask = (task: InternalTask): void => { if (!tasks.some((item) => taskKey(item) === taskKey(task))) tasks.push(task); };
  const eventsMalformed = sources.events.state === 'ready' && !Array.isArray(sources.events.value);
  const offersMalformed = sources.offers.state === 'ready' && !Array.isArray(sources.offers.value);
  const reviewsMalformed = sources.reviews.state === 'ready' && !Array.isArray(sources.reviews.value);
  const jdMalformed = sources.jd.state === 'ready' && sources.jd.value !== null && !isRecord(sources.jd.value);
  const resumeMalformed = sources.resume.state === 'ready' && sources.resume.value !== null && !isRecord(sources.resume.value);
  const fitMalformed = sources.fit.state === 'ready' && sources.fit.value !== null && !isRecord(sources.fit.value);
  if (jdMalformed) sources.jd.malformed = true;
  if (resumeMalformed) sources.resume.malformed = true;
  if (fitMalformed) sources.fit.malformed = true;
  const materialMalformed = sources.materialKit.state === 'ready' && sources.materialKit.value !== null && !isRecord(sources.materialKit.value);
  const app = isRecord(sources.application.value) ? sources.application.value : null;
  const appId = safeId(app?.id);

  if (sources.application.state !== 'ready' || app === null || appId === null) {
    addIssue(makeIssue(sources.application.state === 'loading' ? 'loading' : 'unavailable', sources.application.state === 'loading' ? 'source_loading' : sources.application.state === 'absent' ? 'source_absent' : 'source_error', 1));
  } else if (!['pending', 'applied', 'written_test', 'interview', 'offer', 'closed'].includes(String(app.status))) {
    addIssue(makeIssue('unavailable', 'source_error', 1));
  } else if (app.deleted || app.deletedAt || app.stale || app.sourceMismatch) {
    addIssue(makeIssue('unavailable', app.deleted || app.deletedAt ? 'application_deleted' : 'source_mismatch', 1));
  } else {
    const status = app.status as ApplicationStatus;
    const pending = sources.pending.state === 'ready' && isRecord(sources.pending.value) ? sources.pending.value as ApplicationTaskPending : null;
    const unknown = sources.resultUnknown.state === 'ready' && isRecord(sources.resultUnknown.value) ? sources.resultUnknown.value as ApplicationTaskPending : null;
    const pendingSourceReady = sources.pending.state === 'ready' && (pending !== null || sources.pending.value === null);
    const resultSourceReady = sources.resultUnknown.state === 'ready' && (unknown !== null || sources.resultUnknown.value === null);
    const sourceIssue = (source: RuntimeSource): InternalIssue => makeIssue(
      source.state === 'loading' ? 'loading' : 'unavailable',
      source.state === 'loading' ? 'source_loading' : source.state === 'absent' ? 'source_absent' : 'source_error', 1,
    );
    const pendingCandidate = pendingSourceReady && pending ? pendingRef(pending) : null;
    const unknownCandidate = resultSourceReady && unknown ? pendingRef(unknown) : null;
    const pendingValid = pending === null || (pendingCandidate !== null && pendingCandidate.applicationId === appId);
    const unknownValid = unknown === null || (unknownCandidate !== null && unknownCandidate.applicationId === appId);
    if (!pendingSourceReady || !resultSourceReady) {
      if (!pendingSourceReady) addIssue(sourceIssue(sources.pending));
      if (!resultSourceReady) addIssue(sourceIssue(sources.resultUnknown));
    } else if (!pendingValid || !unknownValid) {
      addIssue(makeIssue('unavailable', 'pending_identity_invalid', 1, (pendingCandidate ?? unknownCandidate)?.eventId ?? (pendingCandidate ?? unknownCandidate)?.applicationId));
    } else if (pendingCandidate && unknownCandidate && !sameRef(pendingCandidate, unknownCandidate)) {
      addIssue(makeIssue('unavailable', 'pending_identity_invalid', 1, pendingCandidate.eventId ?? pendingCandidate.applicationId));
    } else if (pendingCandidate || unknownCandidate) {
      const ref = pendingCandidate ?? unknownCandidate;
      if (ref) {
        const availability: TaskAvailability = pendingCandidate ? 'waiting_confirmation' : 'result_unknown';
        addTask(makeTask(ref.taskId, ref, availability, availability === 'waiting_confirmation' ? 'pending_confirmation' : 'result_unknown', 1, null, ref.eventId ?? ref.applicationId));
      }
    }

    const reviews = sources.reviews.state === 'ready' && Array.isArray(sources.reviews.value) ? sources.reviews.value : [];
    const reviewedEvents = new Set<number>();
    let hasGeneralReview = false;
    for (const raw of reviews) {
      if (!isRecord(raw) || !Object.prototype.hasOwnProperty.call(raw, 'applicationId') || !Object.prototype.hasOwnProperty.call(raw, 'eventId')) {
        addIssue(makeIssue('unavailable', 'event_contract_invalid', 3)); continue;
      }
      const owner = safeId(raw.applicationId);
      if (owner !== appId || raw.deleted || raw.stale || raw.sourceMismatch) { addIssue(makeIssue('unavailable', 'source_mismatch', 3, safeId(raw.eventId))); continue; }
      if (raw.eventId === null) hasGeneralReview = true;
      else if (safeId(raw.eventId) !== null) reviewedEvents.add(safeId(raw.eventId) as number);
      else addIssue(makeIssue('unavailable', 'event_contract_invalid', 3));
    }
    const events = sources.events.state === 'ready' && Array.isArray(sources.events.value) ? sources.events.value : [];
    const eventById = new Map<number, ApplicationTaskEvent>();
    const conflictIds = new Set<number>();
    for (const raw of events) {
      if (!isRecord(raw) || !Object.prototype.hasOwnProperty.call(raw, 'applicationId') || !Object.prototype.hasOwnProperty.call(raw, 'eventId')) { addIssue(makeIssue('unavailable', 'event_contract_invalid', 2)); continue; }
      const id = safeId(raw.eventId); const owner = safeId(raw.applicationId);
      if (id === null || owner !== appId) { addIssue(makeIssue('unavailable', 'source_mismatch', 2, id)); continue; }
      if (!isLifecycle(raw.lifecycle) || !isBucket(raw.bucket) || !isPrimaryAction(raw.primaryAction)) { addIssue(makeIssue('unavailable', 'event_contract_invalid', 2, id)); continue; }
      const current = eventById.get(id);
      if (current && stableJson(current) !== stableJson(raw)) { conflictIds.add(id); eventById.delete(id); addIssue(makeIssue('unavailable', 'duplicate_identity_conflict', 2, id)); }
      else if (!current && !conflictIds.has(id)) eventById.set(id, raw as unknown as ApplicationTaskEvent);
    }
    for (const event of eventById.values()) {
      const lifecycle = event.lifecycle;
      if (lifecycle === 'unknown') { addIssue(makeIssue('unavailable', 'event_contract_invalid', 2, event.eventId)); continue; }
      if (event.sourceMismatch || event.deleted || event.stale) { addIssue(makeIssue('unavailable', event.deleted ? 'entity_deleted' : 'source_mismatch', 2, event.eventId)); continue; }
      const bucketConsistent = lifecycle === 'completed'
        ? event.bucket === 'completed'
        : lifecycle === 'cancelled'
          ? event.bucket === 'cancelled' && event.primaryAction === 'none'
          : event.bucket === 'upcoming' || event.bucket === 'needs_status_update' || event.bucket === 'unavailable';
      const actionConsistent = lifecycle === 'completed'
        ? event.primaryAction === 'record_review' || event.primaryAction === 'view_review'
        : lifecycle === 'cancelled'
          ? event.primaryAction === 'none'
          : event.bucket === 'needs_status_update'
            ? event.primaryAction === 'update_status'
            : event.bucket === 'unavailable'
              ? event.primaryAction === 'none'
              : event.primaryAction === 'prepare' || event.primaryAction === 'enter_preparation';
      if (!bucketConsistent || !actionConsistent) { addIssue(makeIssue('unavailable', 'event_contract_invalid', 2, event.eventId)); continue; }
      if (lifecycle === 'cancelled') continue;
      const durationValid = typeof event.durationMinutes === 'number' && Number.isInteger(event.durationMinutes) && event.durationMinutes >= 1 && event.durationMinutes <= 10080;
      const timeValid = typeof event.scheduledAtTimestamp === 'number' && Number.isFinite(event.scheduledAtTimestamp) && event.scheduledAtState === 'present';
      if (lifecycle === 'completed') {
        const availability: TaskAvailability = reviewsMalformed || sources.reviews.state === 'error' ? 'unavailable' : sources.reviews.state === 'loading' ? 'loading' : sources.reviews.state === 'absent' ? 'unavailable' : 'ready';
        const reason: ApplicationTaskReason = availability === 'loading' ? 'source_loading' : availability === 'unavailable' && sources.reviews.state === 'absent' ? 'source_absent' : availability === 'unavailable' ? 'source_error' : reviewedEvents.has(event.eventId) ? 'interview_review_available' : 'interview_review_missing';
        const priority = reason === 'interview_review_missing' ? 3 : 7;
        addTask(makeTask('application.interview_review', { taskId: 'application.interview_review', applicationId: appId, eventId: event.eventId }, availability, reason, priority, timeValid ? event.scheduledAtTimestamp : null, event.eventId));
        if (availability !== 'ready') addIssue(makeIssue(availability, reason, 3, event.eventId));
      } else if (lifecycle === 'scheduled' || lifecycle === 'in_progress') {
        const actionAllowed = event.primaryAction === 'prepare' || event.primaryAction === 'enter_preparation';
        if (!durationValid || !timeValid) {
          addTask(makeTask('application.interview_prepare', { taskId: 'application.interview_prepare', applicationId: appId, eventId: event.eventId }, 'unavailable', 'event_contract_invalid', 99, null, event.eventId));
          addIssue(makeIssue('unavailable', 'event_contract_invalid', 2, event.eventId));
          continue;
        }
        if (event.bucket === 'needs_status_update') { addIssue(makeIssue('unavailable', 'event_status_needs_update', 2, event.eventId)); continue; }
        const end = durationValid && timeValid ? event.scheduledAtTimestamp + event.durationMinutes * 60_000 : null;
        const inWindow = lifecycle === 'in_progress' ? end !== null && now <= end : timeValid && event.scheduledAtTimestamp > now && event.scheduledAtTimestamp - now <= 24 * 60 * 60_000;
        if (event.bucket === 'upcoming' && actionAllowed && durationValid && timeValid && inWindow) addTask(makeTask('application.interview_prepare', { taskId: 'application.interview_prepare', applicationId: appId, eventId: event.eventId }, 'ready', 'interview_preparation_available', 2, event.scheduledAtTimestamp, event.eventId));
        else if (event.bucket === 'upcoming' || event.bucket === 'unavailable') { addTask(makeTask('application.interview_prepare', { taskId: 'application.interview_prepare', applicationId: appId, eventId: event.eventId }, 'unavailable', 'event_contract_invalid', 99, null, event.eventId)); addIssue(makeIssue('unavailable', 'event_contract_invalid', 2, event.eventId)); }
      }
    }
    if ((eventsMalformed || sources.events.state !== 'ready') && status === 'interview') addIssue(makeIssue(sources.events.state === 'loading' ? 'loading' : 'unavailable', sources.events.state === 'loading' ? 'source_loading' : sources.events.state === 'absent' ? 'source_absent' : 'source_error', 2));
    if ((sources.reviews.state !== 'ready' || reviewsMalformed) && events.some((item) => isRecord(item) && item.lifecycle === 'completed')) addIssue(makeIssue(sources.reviews.state === 'loading' ? 'loading' : 'unavailable', sources.reviews.state === 'loading' ? 'source_loading' : sources.reviews.state === 'absent' ? 'source_absent' : 'source_error', 3));

    const material = sources.materialKit.state === 'ready' && isRecord(sources.materialKit.value) ? sources.materialKit.value : null;
    if (materialMalformed && (status === 'applied' || status === 'written_test' || status === 'interview')) addIssue(makeIssue('unavailable', 'source_error', 4, appId));
    if (material) {
      const owner = safeId(material.applicationId);
      if (owner !== appId || material.deleted || material.stale || material.sourceMismatch) { addIssue(makeIssue('unavailable', material.sourceMismatch || owner !== appId ? 'source_mismatch' : 'entity_deleted', 4, appId)); addTask(makeTask('application.material_kit', { taskId: 'application.material_kit', applicationId: appId }, 'unavailable', material.sourceMismatch || owner !== appId ? 'source_mismatch' : 'entity_deleted', 99, null, appId)); }
      else if (material.status === 'draft' || material.status === 'ready') addTask(makeTask('application.material_kit', { taskId: 'application.material_kit', applicationId: appId }, 'ready', 'material_kit_incomplete', 4, finiteTime(material.updatedAt), appId));
      else if (material.status !== 'submitted') { addIssue(makeIssue('unavailable', 'source_mismatch', 4, appId)); addTask(makeTask('application.material_kit', { taskId: 'application.material_kit', applicationId: appId }, 'unavailable', 'source_mismatch', 99, null, appId)); }
    } else if (sources.materialKit.state !== 'ready' && (status === 'applied' || status === 'written_test' || status === 'interview')) addIssue(makeIssue(sources.materialKit.state === 'loading' ? 'loading' : 'unavailable', sources.materialKit.state === 'loading' ? 'source_loading' : sources.materialKit.state === 'absent' ? 'source_absent' : 'source_error', 4));

    let offerInvalid = offersMalformed;
    const offers = sources.offers.state === 'ready' && Array.isArray(sources.offers.value) ? sources.offers.value : [];
    const unresolved: Array<{ id: number; deadline: number | null }> = [];
    for (const raw of offers) {
      if (!isRecord(raw) || safeId(raw.id) === null || safeId(raw.applicationId) !== appId || raw.deleted || raw.stale || raw.sourceMismatch || !['pending', 'negotiating', 'accepted', 'declined', 'expired'].includes(String(raw.status))) { offerInvalid = true; continue; }
      if (raw.status === 'pending' || raw.status === 'negotiating') unresolved.push({ id: safeId(raw.id) as number, deadline: finiteTime(raw.deadline) });
    }
    if (sources.offers.state !== 'ready' && status === 'offer') { addIssue(makeIssue(sources.offers.state === 'loading' ? 'loading' : 'unavailable', sources.offers.state === 'loading' ? 'source_loading' : sources.offers.state === 'absent' ? 'source_absent' : 'source_error', 5)); }
    else if (offerInvalid) { addIssue(makeIssue('unavailable', 'source_mismatch', 5)); addTask(makeTask('application.offer_review', { taskId: 'application.offer_review', applicationId: appId }, 'unavailable', 'source_mismatch', 99, null, appId)); }
    else if (unresolved.length > 0) {
      const ordered = [...unresolved].sort((a, b) => {
        if (a.deadline === null && b.deadline === null) return a.id - b.id;
        if (a.deadline === null) return 1;
        if (b.deadline === null) return -1;
        return a.deadline - b.deadline || a.id - b.id;
      });
      addTask(makeTask('application.offer_review', { taskId: 'application.offer_review', applicationId: appId }, 'ready', 'offer_review_pending', 5, ordered[0].deadline, ordered[0].id));
    }
    else if (status === 'offer' && sources.offers.state === 'ready') addTask(makeTask('application.offer_review', { taskId: 'application.offer_review', applicationId: appId }, 'ready', 'offer_review_pending', 6, null, appId));

    if (status === 'pending') {
      const availability = dependencyAvailability([sources.jd, sources.resume]);
      if (sources.fit.state === 'ready' && sources.fit.value === null) addTask(makeTask('application.opportunity_fit', { taskId: 'application.opportunity_fit', applicationId: appId }, availability, availability === 'ready' ? 'opportunity_fit' : availability === 'loading' ? 'source_loading' : availability === 'blocked' ? 'source_absent' : 'source_error', 6, null, appId));
      else if (sources.fit.state !== 'ready' || fitMalformed) addIssue(makeIssue(sources.fit.state === 'loading' ? 'loading' : 'unavailable', sources.fit.state === 'loading' ? 'source_loading' : sources.fit.state === 'absent' ? 'source_absent' : 'source_error', 6));
    }
    if (status === 'applied' || status === 'written_test') {
      const availability = dependencyAvailability([sources.jd, sources.resume]);
      if (!material && sources.materialKit.state === 'ready' && !materialMalformed) addTask(makeTask('application.material_kit', { taskId: 'application.material_kit', applicationId: appId }, availability, availability === 'ready' ? 'material_kit_missing' : availability === 'loading' ? 'source_loading' : availability === 'blocked' ? 'source_absent' : 'source_error', 6, null, appId));
    }
    if (status === 'closed') addTask(makeTask('application.record_outcome', { taskId: 'application.record_outcome', applicationId: appId }, 'ready', 'record_outcome', 6, null, appId));
    if (hasGeneralReview && sources.reviews.state === 'ready') addTask(makeTask('application.general_review', { taskId: 'application.general_review', applicationId: appId }, 'ready', 'general_review', 7, null, appId));
  }
  const deduped = new Map<string, InternalTask>();
  for (const task of tasks) { const existing = deduped.get(taskKey(task)); if (!existing || task.priority < existing.priority) deduped.set(taskKey(task), task); }
  const ordered = [...deduped.values()].sort((a, b) => a.priority - b.priority || compareTask(a, b));
  const first = ordered.find((task) => task.executable) ?? null;
  const blockerPriority = [...issues.map((issue) => issue.priority), ...ordered.filter((task) => !task.executable).map((task) => task.priority)].reduce((min, value) => Math.min(min, value), Number.POSITIVE_INFINITY);
  const primaryInternal = first && blockerPriority > first.priority ? first : null;
  const frozenTasks = ordered.map((task) => Object.freeze({ taskId: task.taskId, ref: Object.freeze({ ...task.ref }), availability: task.availability, reason: task.reason, reasonCode: task.reasonCode, primary: task === primaryInternal, executable: task.executable, businessTime: task.businessTime }));
  const frozenIssues = Object.freeze([...issues].sort((a, b) => a.priority - b.priority || (a.identity ?? Number.MAX_SAFE_INTEGER) - (b.identity ?? Number.MAX_SAFE_INTEGER) || a.reason.localeCompare(b.reason)));
  const primaryTask = frozenTasks.find((task) => task.primary) ?? null;
  return Object.freeze({
    tasks: Object.freeze(frozenTasks),
    issues: frozenIssues,
    primaryTask,
    primary: primaryTask,
    hasExecutableTask: primaryTask !== null,
    hasLoading: frozenIssues.some((issue) => issue.availability === 'loading') || frozenTasks.some((task) => task.availability === 'loading'),
    hasUnavailable: frozenIssues.some((issue) => issue.availability !== 'loading') || frozenTasks.some((task) => task.availability === 'blocked' || task.availability === 'unavailable'),
  });
}
