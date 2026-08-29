/** The complete set of task surfaces supported by the desktop application. */
export type CoreTaskId =
  | 'application.opportunity_fit'
  | 'application.material_kit'
  | 'application.interview_prepare'
  | 'application.interview_review'
  | 'application.general_review'
  | 'application.offer_review'
  | 'application.record_outcome'
  | 'interview.free_practice'
  | 'materials.resume'
  | 'materials.story'
  | 'materials.reference';

export const CORE_TASK_IDS = [
  'application.opportunity_fit',
  'application.material_kit',
  'application.interview_prepare',
  'application.interview_review',
  'application.general_review',
  'application.offer_review',
  'application.record_outcome',
  'interview.free_practice',
  'materials.resume',
  'materials.story',
  'materials.reference',
] as const satisfies readonly CoreTaskId[];

export type CoreTaskEntrypointCategory = 'core_task' | 'navigation_only' | 'record_management';

export const CORE_TASK_ENTRYPOINT_CATEGORIES = [
  'core_task',
  'navigation_only',
  'record_management',
] as const satisfies readonly CoreTaskEntrypointCategory[];

export type TaskLaunchSource =
  | 'application_header'
  | 'application_task_card'
  | 'interview_event_card'
  | 'materials_library'
  | 'haru'
  | 'pilot'
  | 'deep_link'
  | 'command_palette';

export type TaskLaunchFocus = 'overview' | 'current' | 'history' | 'source';

export interface TaskLaunchHints {
  readonly suggestedResumeId?: number;
  readonly suggestedOfferId?: number;
  readonly suggestedEventId?: number;
}

export interface CoreTaskRef {
  readonly taskId: CoreTaskId;
  readonly applicationId?: number;
  readonly eventId?: number;
  readonly offerId?: number;
  readonly resumeId?: number;
  readonly storyId?: number;
  readonly sourceId?: number;
}

export interface TaskLaunchRequest {
  readonly ref: CoreTaskRef;
  readonly source: TaskLaunchSource;
  readonly focus?: TaskLaunchFocus;
  readonly hints?: Readonly<TaskLaunchHints>;
}

export type CoreTaskParseFailureReason = 'unknown_task' | 'invalid_task_identity';

export type CoreTaskParseResult =
  | { readonly ok: true; readonly ref: CoreTaskRef; readonly key: string }
  | { readonly ok: false; readonly reason: CoreTaskParseFailureReason };

type CoreTaskIdentityField =
  | 'applicationId'
  | 'eventId'
  | 'resumeId'
  | 'storyId'
  | 'sourceId';

const CORE_TASK_ID_SET = new Set<string>(CORE_TASK_IDS);

const REQUIRED_IDENTITY_FIELDS: Readonly<Record<CoreTaskId, readonly CoreTaskIdentityField[]>> = Object.freeze({
  'application.opportunity_fit': ['applicationId'],
  'application.material_kit': ['applicationId'],
  'application.interview_prepare': ['applicationId', 'eventId'],
  'application.interview_review': ['applicationId', 'eventId'],
  'application.general_review': ['applicationId'],
  'application.offer_review': ['applicationId'],
  'application.record_outcome': ['applicationId'],
  'interview.free_practice': [],
  'materials.resume': ['resumeId'],
  'materials.story': ['storyId'],
  'materials.reference': ['sourceId'],
});

const UNKNOWN_TASK_RESULT: CoreTaskParseResult = Object.freeze({
  ok: false,
  reason: 'unknown_task',
});

const INVALID_IDENTITY_RESULT: CoreTaskParseResult = Object.freeze({
  ok: false,
  reason: 'invalid_task_identity',
});

function isRecord(value: unknown): value is Record<PropertyKey, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function hasOwn(value: object, key: PropertyKey): boolean {
  return Object.prototype.hasOwnProperty.call(value, key);
}

function isSafePositiveInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0;
}

function isCoreTaskId(value: unknown): value is CoreTaskId {
  return typeof value === 'string' && CORE_TASK_ID_SET.has(value);
}

function canonicalKeyForRef(ref: CoreTaskRef, fields: readonly CoreTaskIdentityField[]): string {
  return [
    ref.taskId,
    ...fields.map((field) => `${field}=${ref[field]}`),
  ].join(':');
}

/**
 * Parses an untrusted launch identity without reading or changing application state.
 * Invalid or hostile values fail closed and never escape as an exception.
 */
export function parseCoreTaskRef(input: unknown): CoreTaskParseResult {
  if (!isRecord(input)) return UNKNOWN_TASK_RESULT;

  let taskId: unknown;
  try {
    if (!hasOwn(input, 'taskId')) return UNKNOWN_TASK_RESULT;
    taskId = input.taskId;
  } catch {
    return UNKNOWN_TASK_RESULT;
  }

  if (!isCoreTaskId(taskId)) return UNKNOWN_TASK_RESULT;

  try {

    const requiredFields = REQUIRED_IDENTITY_FIELDS[taskId];
    const allowedFields = new Set<PropertyKey>(['taskId', ...requiredFields]);
    const ownKeys = Reflect.ownKeys(input);
    if (ownKeys.length !== allowedFields.size || ownKeys.some((key) => !allowedFields.has(key))) {
      return INVALID_IDENTITY_RESULT;
    }

    for (const field of requiredFields) {
      if (!hasOwn(input, field) || !isSafePositiveInteger(input[field])) {
        return INVALID_IDENTITY_RESULT;
      }
    }

    const ref = {
      taskId,
      ...Object.fromEntries(requiredFields.map((field) => [field, input[field]])),
    } as CoreTaskRef;
    const key = canonicalKeyForRef(ref, requiredFields);
    return { ok: true, ref: Object.freeze(ref), key };
  } catch {
    return INVALID_IDENTITY_RESULT;
  }
}

/**
 * Returns the stable key for a valid CoreTaskRef. The total runtime behavior also
 * keeps malformed values from throwing when a caller crosses the trust boundary.
 */
export function coreTaskCanonicalKey(ref: CoreTaskRef): string {
  const parsed = parseCoreTaskRef(ref);
  if (!parsed.ok) return `invalid:${parsed.reason}`;
  return parsed.key;
}
