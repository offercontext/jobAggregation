import { classifyEventLifecycleV1, type EventLifecycleV1 } from './eventLifecycle';

export type InterviewContractReason =
  | 'schedule_absent'
  | 'schedule_invalid'
  | 'contract_field_missing'
  | 'contract_field_invalid'
  | 'duration_invalid'
  | 'status_unknown'
  | 'source_mismatch';

export interface NormalizedInterviewIndexItem {
  readonly application_id: number | null;
  readonly event_id: number | null;
  readonly company_name: string;
  readonly position_name: string;
  readonly scheduled_at: string;
  readonly note_id: number | null;
  readonly note_source_status: 'current' | 'source_changed' | null;
  readonly has_review_proposal: boolean;
  readonly review_summary: string | null;
  readonly has_confirmed_knowledge: boolean;
  readonly preparation_available: boolean;
  readonly event_status: string;
  readonly duration_minutes: number | null;
  readonly scheduled_at_state: 'present' | 'absent' | undefined;
  readonly lifecycle: EventLifecycleV1;
  readonly scheduleTimestamp: number | null;
  readonly scheduleValid: boolean;
  readonly durationValid: boolean;
  readonly contractReasons: readonly InterviewContractReason[];
  /** The first stable reason, useful to callers that render one safe message. */
  readonly reason: InterviewContractReason | null;
  readonly sourceMismatch: boolean;
}

export type InterviewIndexSourcePreflight =
  | { readonly ok: true }
  | { readonly ok: false; readonly reason: 'source_mismatch' };

interface ReadField {
  readonly present: boolean;
  readonly value: unknown;
}

function isRecord(value: unknown): value is Record<PropertyKey, unknown> {
  try {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
  } catch {
    return false;
  }
}

function readField(input: Record<PropertyKey, unknown>, key: PropertyKey): ReadField {
  try {
    const present = Object.prototype.hasOwnProperty.call(input, key);
    if (!present) return { present: false, value: undefined };
    return { present: true, value: input[key] };
  } catch {
    return { present: false, value: undefined };
  }
}

function isSafePositiveInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0;
}

function safeString(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function safeNullableString(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

function safeNullableId(value: unknown): number | null {
  return isSafePositiveInteger(value) ? value : null;
}

function safeBoolean(value: unknown): boolean {
  return typeof value === 'boolean' ? value : false;
}

function maxDayOfMonth(year: number, month: number): number {
  if (month === 2) {
    const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
    return leap ? 29 : 28;
  }
  return [4, 6, 9, 11].includes(month) ? 30 : 31;
}

/** Strict RFC3339 parser; Date.parse alone accepts non-RFC strings and normalizes invalid dates. */
function parseRfc3339(value: unknown): number | null {
  if (typeof value !== 'string') return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})$/.exec(value);
  if (!match) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hours = Number(match[4]);
  const minutes = Number(match[5]);
  const seconds = Number(match[6]);
  const zone = match[7];
  if (year === 0 || year === 1 || month < 1 || month > 12 || day < 1 || day > maxDayOfMonth(year, month)) return null;
  if (hours > 23 || minutes > 59 || seconds > 59) return null;
  if (zone !== 'Z') {
    const offsetHours = Number(zone.slice(1, 3));
    const offsetMinutes = Number(zone.slice(4, 6));
    if (offsetHours > 23 || offsetMinutes > 59) return null;
  }
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : null;
}

function sourceValues(source: unknown): { id: number | null; applicationId: number | null; status: unknown; readable: boolean } {
  if (!isRecord(source)) return { id: null, applicationId: null, status: undefined, readable: false };
  const idField = readField(source, 'id');
  const eventIdField = readField(source, 'event_id');
  const applicationField = readField(source, 'application_id');
  const statusField = readField(source, 'status');
  const id = isSafePositiveInteger(idField.value) ? idField.value : isSafePositiveInteger(eventIdField.value) ? eventIdField.value : null;
  return {
    id,
    applicationId: isSafePositiveInteger(applicationField.value) ? applicationField.value : null,
    status: statusField.value,
    readable: idField.present || eventIdField.present,
  };
}

/**
 * Compares the index row to an already loaded ApplicationEvent. This is an
 * explicit, pure preflight only; it never fetches, mutates, or retries data.
 */
export function validateInterviewIndexSource(index: unknown, currentEvent: unknown): InterviewIndexSourcePreflight {
  if (!isRecord(index) || !isRecord(currentEvent)) return { ok: false, reason: 'source_mismatch' };
  const application = readField(index, 'application_id');
  const event = readField(index, 'event_id');
  const status = readField(index, 'event_status');
  const source = sourceValues(currentEvent);
  if (!isSafePositiveInteger(application.value) || !isSafePositiveInteger(event.value) || !source.readable || source.id === null || source.applicationId === null || typeof status.value !== 'string') {
    return { ok: false, reason: 'source_mismatch' };
  }
  return application.value === source.applicationId && event.value === source.id && status.value === source.status
    ? { ok: true }
    : { ok: false, reason: 'source_mismatch' };
}

function sourceMismatchFromSnapshot(
  application: ReadField,
  event: ReadField,
  status: ReadField,
  currentEvent: unknown,
): boolean {
  if (currentEvent === undefined) return false;
  if (!isSafePositiveInteger(application.value) || !isSafePositiveInteger(event.value) || typeof status.value !== 'string') return true;
  const source = sourceValues(currentEvent);
  return !source.readable || source.id === null || source.applicationId === null
    || source.id !== event.value || source.applicationId !== application.value || source.status !== status.value;
}

function appendReason(reasons: InterviewContractReason[], reason: InterviewContractReason): void {
  if (!reasons.includes(reason)) reasons.push(reason);
}

/** Normalizes an untrusted API boundary into a safe immutable view model. */
export function normalizeInterviewIndexItem(input: unknown, currentEvent?: unknown): NormalizedInterviewIndexItem {
  const empty = !isRecord(input) ? {} : input;
  const application = readField(empty, 'application_id');
  const event = readField(empty, 'event_id');
  const company = readField(empty, 'company_name');
  const position = readField(empty, 'position_name');
  const scheduled = readField(empty, 'scheduled_at');
  const note = readField(empty, 'note_id');
  const noteSource = readField(empty, 'note_source_status');
  const reviewProposal = readField(empty, 'has_review_proposal');
  const reviewSummary = readField(empty, 'review_summary');
  const confirmedKnowledge = readField(empty, 'has_confirmed_knowledge');
  const preparation = readField(empty, 'preparation_available');
  const status = readField(empty, 'event_status');
  const duration = readField(empty, 'duration_minutes');
  const scheduleState = readField(empty, 'scheduled_at_state');

  const eventStatus = safeString(status.value);
  const lifecycle = classifyEventLifecycleV1(eventStatus);
  const state = scheduleState.value === 'present' || scheduleState.value === 'absent' ? scheduleState.value : undefined;
  const reasons: InterviewContractReason[] = [];

  if (!scheduleState.present) appendReason(reasons, 'contract_field_missing');
  else if (state === undefined) appendReason(reasons, 'contract_field_invalid');

  const scheduleTimestamp = state === 'present' ? parseRfc3339(scheduled.value) : null;
  const scheduleValid = state === 'present' && scheduleTimestamp !== null;
  if (state === 'present' && !scheduleValid) appendReason(reasons, 'schedule_invalid');
  if (state === 'absent') appendReason(reasons, 'schedule_absent');

  const durationValue = typeof duration.value === 'number' && Number.isFinite(duration.value) ? duration.value : null;
  const durationValid = durationValue !== null && Number.isInteger(durationValue) && durationValue >= 1 && durationValue <= 10080;
  if (!durationValid) appendReason(reasons, 'duration_invalid');
  if (!status.present || lifecycle === 'unknown') appendReason(reasons, 'status_unknown');

  const sourceMismatch = sourceMismatchFromSnapshot(application, event, status, currentEvent);
  if (sourceMismatch) appendReason(reasons, 'source_mismatch');

  const normalized: NormalizedInterviewIndexItem = {
    application_id: safeNullableId(application.value),
    event_id: safeNullableId(event.value),
    company_name: safeString(company.value),
    position_name: safeString(position.value),
    scheduled_at: safeString(scheduled.value),
    note_id: safeNullableId(note.value),
    note_source_status: noteSource.value === 'current' || noteSource.value === 'source_changed' ? noteSource.value : null,
    has_review_proposal: safeBoolean(reviewProposal.value),
    review_summary: safeNullableString(reviewSummary.value),
    has_confirmed_knowledge: safeBoolean(confirmedKnowledge.value),
    preparation_available: safeBoolean(preparation.value),
    event_status: eventStatus,
    duration_minutes: durationValue,
    scheduled_at_state: state,
    lifecycle,
    scheduleTimestamp,
    scheduleValid,
    durationValid,
    contractReasons: Object.freeze(reasons),
    reason: reasons[0] ?? null,
    sourceMismatch,
  };
  return Object.freeze(normalized);
}

export const preflightInterviewIndexSource = validateInterviewIndexSource;
