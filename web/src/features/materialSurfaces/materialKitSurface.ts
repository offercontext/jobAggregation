import type { MaterialKitStatus } from '@/types/materialKit';
import { coreTaskCanonicalKey } from '@/features/coreTaskSurface/contracts';
import { MATERIAL_FLOW_COPY } from '@/components/materialFlowCopy';

export type MaterialKitSurfaceState =
  | 'loading'
  | 'missing_jd'
  | 'missing_resume'
  | 'not_generated'
  | 'dirty_draft'
  | 'waiting_confirmation'
  | 'result_unknown'
  | 'source_conflict'
  | 'ready_unsubmitted'
  | 'submitted'
  | 'unavailable';

export type MaterialKitSurfaceSourceStatus = 'loading' | 'ready' | 'error' | 'absent' | 'unknown';

export interface MaterialKitSurfaceSource<T> {
  readonly status: MaterialKitSurfaceSourceStatus;
  readonly value?: T | null;
  readonly error?: unknown;
}

export interface MaterialKitSurfaceResume {
  readonly id: number;
  readonly deletedAt?: string | null;
  readonly deleted_at?: string | null;
  readonly deleted?: boolean;
  readonly isMaster?: boolean;
  readonly is_master?: boolean;
}

export interface MaterialKitSurfaceJd {
  readonly id?: number;
  readonly text?: string;
  readonly jdText?: string;
  readonly jd_text?: string;
  readonly deletedAt?: string | null;
  readonly deleted_at?: string | null;
}

export interface MaterialKitSurfaceKit {
  readonly status?: MaterialKitStatus | string;
  readonly applicationId?: number;
  readonly application_id?: number;
  readonly resumeId?: number;
  readonly resume_id?: number;
  readonly deleted?: boolean;
  readonly deletedAt?: string | null;
  readonly deleted_at?: string | null;
}

type SourceOrValue<T> = MaterialKitSurfaceSource<T> | T | null | undefined;

export interface MaterialKitSurfaceInput {
  readonly applicationId: number;
  /** A direct state is accepted for callers that already own source projection. */
  readonly state?: MaterialKitSurfaceState;
  /** Top-level source state is useful while several source queries are joining. */
  readonly sourceState?: MaterialKitSurfaceSourceStatus;
  readonly jd?: SourceOrValue<MaterialKitSurfaceJd> | string;
  readonly jdSource?: SourceOrValue<MaterialKitSurfaceJd> | string;
  readonly resumes?: SourceOrValue<readonly MaterialKitSurfaceResume[]>;
  readonly resumeSource?: SourceOrValue<readonly MaterialKitSurfaceResume[]>;
  readonly materialKit?: SourceOrValue<MaterialKitSurfaceKit>;
  readonly materialKitSource?: SourceOrValue<MaterialKitSurfaceKit>;
  readonly kit?: SourceOrValue<MaterialKitSurfaceKit>;
  readonly selectedResumeId?: number | null;
  readonly resumeId?: number | null;
  readonly resumeSelected?: boolean;
  readonly draftDirty?: boolean;
  readonly dirty?: boolean;
  readonly pending?: boolean;
  readonly resultUnknown?: boolean;
  readonly pendingState?: 'none' | 'pending' | 'unknown' | 'result_unknown';
  readonly sourceConflict?: boolean;
  /** Hints are intentionally ignored by the canonical task key. */
  readonly suggestedResumeId?: number | null;
}

export type MaterialKitSurfaceActionId =
  | 'none'
  | 'open_jd'
  | 'select_resume'
  | 'generate'
  | 'save'
  | 'confirm'
  | 'resolve'
  | 'record_submission'
  | 'view_submission';

export interface MaterialKitSurfaceAction {
  readonly id: MaterialKitSurfaceActionId;
  readonly label: string;
  readonly primary: boolean;
  readonly enabled: boolean;
  readonly navigationOnly: boolean;
}

export interface MaterialKitSurfaceModel {
  readonly key: string;
  readonly applicationId: number;
  readonly state: MaterialKitSurfaceState;
  readonly primaryAction: MaterialKitSurfaceAction;
  readonly actions: readonly MaterialKitSurfaceAction[];
  readonly hasExecutableAction: boolean;
  readonly reason: string | null;
}

const SURFACE_STATES: ReadonlySet<MaterialKitSurfaceState> = new Set([
  'loading',
  'missing_jd',
  'missing_resume',
  'not_generated',
  'dirty_draft',
  'waiting_confirmation',
  'result_unknown',
  'source_conflict',
  'ready_unsubmitted',
  'submitted',
  'unavailable',
]);

const LABELS: Record<MaterialKitSurfaceActionId, string> = {
  none: MATERIAL_FLOW_COPY.surface.loading,
  open_jd: MATERIAL_FLOW_COPY.surface.missingJd,
  select_resume: MATERIAL_FLOW_COPY.surface.missingResume,
  generate: MATERIAL_FLOW_COPY.surface.notGenerated,
  save: MATERIAL_FLOW_COPY.surface.dirtyDraft,
  confirm: MATERIAL_FLOW_COPY.surface.waitingConfirmation,
  resolve: MATERIAL_FLOW_COPY.surface.resultUnknown,
  record_submission: MATERIAL_FLOW_COPY.surface.readyUnsubmitted,
  view_submission: MATERIAL_FLOW_COPY.surface.submitted,
};

const NAVIGATION_ONLY = new Set<MaterialKitSurfaceActionId>(['open_jd', 'select_resume', 'view_submission']);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function isValidId(value: unknown): value is number {
  return typeof value === 'number'
    && Number.isSafeInteger(value)
    && value > 0;
}

function sourceStatus(value: unknown): MaterialKitSurfaceSourceStatus | null {
  if (!isRecord(value) || typeof value.status !== 'string') return null;
  if (value.status === 'loading' || value.status === 'ready' || value.status === 'error'
    || value.status === 'absent' || value.status === 'unknown') return value.status;
  return 'value' in value ? 'unknown' : null;
}

function sourceValue<T>(value: SourceOrValue<T>): T | null | undefined {
  if (
    isRecord(value)
    && 'status' in value
    && ('value' in value || value.status === 'loading' || value.status === 'error' || value.status === 'absent' || value.status === 'unknown')
  ) return value.value as T | null | undefined;
  return value as T | null | undefined;
}

function sourceIsBlocked(value: unknown): boolean {
  const status = sourceStatus(value);
  return status === 'loading' || status === 'error' || status === 'unknown';
}

function sourceIsAbsent(value: unknown): boolean {
  return sourceStatus(value) === 'absent';
}

function visibleResume(resume: MaterialKitSurfaceResume): boolean {
  return isValidId(resume.id)
    && resume.deleted !== true
    && resume.deletedAt == null
    && resume.deleted_at == null;
}

function normalizedJd(value: SourceOrValue<MaterialKitSurfaceJd> | string): MaterialKitSurfaceJd | null {
  const resolved = sourceValue(value);
  if (typeof resolved === 'string') return resolved.trim() ? { text: resolved } : null;
  if (!isRecord(resolved)) return null;
  const text = typeof resolved.text === 'string'
    ? resolved.text
    : typeof resolved.jdText === 'string'
      ? resolved.jdText
      : typeof resolved.jd_text === 'string'
        ? resolved.jd_text
        : '';
  const id = resolved.id;
  if (resolved.deleted === true || resolved.deletedAt != null || resolved.deleted_at != null) return null;
  return { id: isValidId(id) ? id : undefined, text };
}

function kitStatus(value: unknown): MaterialKitStatus | null {
  const resolved = sourceValue(value as SourceOrValue<MaterialKitSurfaceKit>);
  if (!isRecord(resolved)) return null;
  const status = resolved.status;
  return status === 'draft' || status === 'ready' || status === 'submitted' ? status : null;
}

function kitBelongsToApplication(value: unknown, applicationId: number): boolean {
  if (!isRecord(value)) return false;
  const applicationValues = [value.applicationId, value.application_id]
    .filter((candidate) => candidate !== undefined && candidate !== null);
  return applicationValues.every((candidate) => isValidId(candidate) && candidate === applicationId)
    && (applicationValues.length < 2 || applicationValues[0] === applicationValues[1]);
}

function makeAction(id: MaterialKitSurfaceActionId, enabled: boolean): MaterialKitSurfaceAction {
  return Object.freeze({
    id,
    label: LABELS[id],
    primary: id !== 'none',
    enabled,
    navigationOnly: NAVIGATION_ONLY.has(id),
  });
}

function freezeModel(model: MaterialKitSurfaceModel): MaterialKitSurfaceModel {
  Object.freeze(model.primaryAction);
  Object.freeze(model.actions);
  return Object.freeze(model);
}

function explicitState(input: MaterialKitSurfaceInput): MaterialKitSurfaceState | null {
  if (input.state && SURFACE_STATES.has(input.state)) return input.state;
  if (input.sourceState && input.sourceState !== 'ready') {
    return input.sourceState === 'loading' ? 'loading' : 'unavailable';
  }
  if (input.pendingState === 'pending' || input.pending === true) return 'waiting_confirmation';
  if (input.pendingState === 'unknown' || input.pendingState === 'result_unknown' || input.resultUnknown === true) return 'result_unknown';
  if (input.sourceConflict === true) return 'source_conflict';
  return null;
}

function getSource<T>(first: SourceOrValue<T>, second: SourceOrValue<T>): SourceOrValue<T> {
  return first !== undefined ? first : second;
}

function unavailableModel(applicationId: number, reason: string): MaterialKitSurfaceModel {
  const safeApplicationId = isValidId(applicationId) ? applicationId : 0;
  const primaryAction = makeAction('none', false);
  return freezeModel({
    key: coreTaskCanonicalKey({ taskId: 'application.material_kit', applicationId: safeApplicationId }),
    applicationId: safeApplicationId,
    state: 'unavailable',
    primaryAction,
    actions: Object.freeze([primaryAction]),
    hasExecutableAction: false,
    reason,
  });
}

/**
 * Project Material Kit facts into one bounded user action. The projector never
 * invokes a service and deliberately omits source identities from the key.
 */
function projectMaterialKitSurfaceUnsafe(input: MaterialKitSurfaceInput): MaterialKitSurfaceModel {
  let applicationId: number;
  try {
    applicationId = input.applicationId;
  } catch {
    applicationId = 0;
  }
  const key = coreTaskCanonicalKey({ taskId: 'application.material_kit', applicationId });
  const invalidApplication = !isValidId(applicationId);
  let state = invalidApplication ? 'unavailable' : explicitState(input);
  let reason: string | null = invalidApplication
    ? 'application_invalid'
    : input.sourceState && input.sourceState !== 'ready' && state
      ? 'source_unavailable'
      : null;

  const jdSource = getSource(input.jd, input.jdSource);
  const resumeSource = getSource(input.resumes, input.resumeSource);
  const kitSource = getSource(input.materialKit, input.materialKitSource ?? input.kit);

  if (!state && (sourceIsBlocked(jdSource) || sourceIsBlocked(resumeSource) || sourceIsBlocked(kitSource))) {
    const statuses = [jdSource, resumeSource, kitSource].map(sourceStatus);
    state = statuses.includes('loading') ? 'loading' : 'unavailable';
    reason = 'source_unavailable';
  }

  const jd = normalizedJd(jdSource as SourceOrValue<MaterialKitSurfaceJd> | string);
  const resumesValue = sourceValue(resumeSource) as readonly MaterialKitSurfaceResume[] | null | undefined;
  const resumeStatus = sourceStatus(resumeSource);
  const resumeSourceReady = resumeStatus === 'ready' || Array.isArray(resumesValue);
  const resumeSourceMalformed = resumeSource !== undefined
    && resumeSource !== null
    && !Array.isArray(resumesValue)
    && (resumeStatus === null || resumeSourceReady);
  const visibleResumes = Array.isArray(resumesValue) ? resumesValue.filter(visibleResume) : [];
  const selectedResumeId = input.selectedResumeId ?? input.resumeId;
  const selectedVisible = isValidId(selectedResumeId)
    && visibleResumes.some((resume) => resume.id === selectedResumeId);
  const hasResumeSource = resumeStatus === 'ready' || Array.isArray(resumesValue);
  const kit = sourceValue(kitSource);
  const status = kitStatus(kitSource);
  const kitApplicationMismatch = kit !== null
    && kit !== undefined
    && status !== null
    && !kitBelongsToApplication(kit, applicationId);

  if (!state && kitApplicationMismatch) {
    state = 'unavailable';
    reason = 'material_kit_mismatch';
  } else if (!state && resumeSourceMalformed) {
    state = 'unavailable';
    reason = 'resume_invalid';
  }

  if (!state && sourceIsAbsent(jdSource)) {
    state = 'missing_jd';
    reason = 'jd_absent';
  } else if (!state && (!jd || !jd.text?.trim() || !isValidId(jd.id))) {
    state = 'missing_jd';
    reason = 'jd_invalid';
  }

  if (!state && sourceIsAbsent(resumeSource)) {
    state = 'missing_resume';
    reason = 'resume_absent';
  } else if (!state && (input.resumeSelected === false || (!selectedVisible && input.resumeSelected !== true && hasResumeSource))) {
    state = 'missing_resume';
    reason = 'resume_unselected';
  } else if (!state && input.resumeSelected !== true && hasResumeSource && visibleResumes.length === 0) {
    state = 'missing_resume';
    reason = 'resume_absent';
  }

  if (!state && (input.draftDirty === true || input.dirty === true)) {
    state = 'dirty_draft';
  }

  const kitStatusValue = sourceStatus(kitSource);
  const kitSourceReady = kitStatusValue === 'ready';
  const kitMalformed = kitSourceReady
    && kit !== null
    && kit !== undefined
    && (!isRecord(kit) || kitStatus(kitSource) === null);
  const directKitMalformed = kitSource !== undefined
    && kitSource !== null
    && kitStatusValue === null
    && (kit === null || kit === undefined || !isRecord(kit) || kitStatus(kitSource) === null);
  if (!state && (kitMalformed || directKitMalformed || kitApplicationMismatch)) {
    state = 'unavailable';
    reason = kitApplicationMismatch ? 'material_kit_mismatch' : 'material_kit_invalid';
  } else if (!state && !kit && kitSource !== undefined && kitSource !== null && kitSourceReady) {
    state = 'not_generated';
    reason = 'material_kit_absent';
  }

  if (!state && status === 'draft') {
    state = 'dirty_draft';
  } else if (!state && status === 'ready') {
    state = 'ready_unsubmitted';
  } else if (!state && status === 'submitted') {
    state = 'submitted';
  }

  if (!state) {
    state = 'not_generated';
    reason = reason ?? 'material_kit_absent';
  }

  let actionId: MaterialKitSurfaceActionId;
  let enabled = !invalidApplication;
  switch (state) {
    case 'loading':
      actionId = 'none';
      enabled = false;
      break;
    case 'missing_jd':
      actionId = 'open_jd';
      break;
    case 'missing_resume':
      actionId = 'select_resume';
      break;
    case 'dirty_draft':
      actionId = 'save';
      break;
    case 'waiting_confirmation':
      actionId = 'confirm';
      break;
    case 'result_unknown':
    case 'source_conflict':
      actionId = 'resolve';
      break;
    case 'ready_unsubmitted':
      actionId = 'record_submission';
      break;
    case 'submitted':
      actionId = 'view_submission';
      break;
    case 'not_generated':
      actionId = 'generate';
      break;
    case 'unavailable':
    default:
      actionId = 'none';
      enabled = false;
      break;
  }

  const primaryAction = makeAction(actionId, enabled);
  return freezeModel({
    key,
    applicationId,
    state,
    primaryAction,
    actions: Object.freeze([primaryAction]),
    hasExecutableAction: enabled && actionId !== 'none',
    reason,
  });
}

/**
 * Project Material Kit facts into one bounded user action. Hostile or malformed
 * source objects fail closed instead of escaping source getter exceptions.
 */
export function projectMaterialKitSurface(input: MaterialKitSurfaceInput): MaterialKitSurfaceModel {
  try {
    return projectMaterialKitSurfaceUnsafe(input);
  } catch {
    let applicationId = 0;
    try {
      if (isValidId(input.applicationId)) applicationId = input.applicationId;
    } catch {
      applicationId = 0;
    }
    return unavailableModel(applicationId, 'source_unavailable');
  }
}
