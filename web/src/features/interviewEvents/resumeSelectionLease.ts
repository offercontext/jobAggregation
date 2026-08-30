export interface ResumeSelectionCandidate {
  readonly id: number;
  readonly applicationId?: number | null;
  readonly application_id?: number | null;
  readonly eventId?: number | null;
  readonly event_id?: number | null;
  readonly deleted?: boolean;
  readonly deletedAt?: string | null;
  readonly deleted_at?: string | null;
  readonly hidden?: boolean;
  readonly visible?: boolean;
  readonly isMaster?: boolean;
  readonly is_master?: boolean;
}

export interface ResumeSelectionSource<T> {
  readonly status: 'loading' | 'ready' | 'error' | 'absent' | 'unknown';
  readonly value?: T | null;
}

export interface SavedResumeSnapshot {
  readonly applicationId?: number | null;
  readonly application_id?: number | null;
  readonly eventId?: number | null;
  readonly event_id?: number | null;
  readonly resumeId?: number | null;
  readonly resume_id?: number | null;
  readonly sourceValid?: boolean;
  readonly verified?: boolean;
  readonly deleted?: boolean;
  readonly deletedAt?: string | null;
  readonly deleted_at?: string | null;
}

export interface ResumeSelectionInput {
  readonly applicationId: number;
  readonly eventId: number;
  readonly generation?: number;
  readonly resumes?: readonly ResumeSelectionCandidate[] | ResumeSelectionSource<readonly ResumeSelectionCandidate[]>;
  /** Alias used by source adapters that keep the query envelope named explicitly. */
  readonly resumeSource?: readonly ResumeSelectionCandidate[] | ResumeSelectionSource<readonly ResumeSelectionCandidate[]>;
  readonly lease?: ResumeSelectionLease | null;
  readonly selectedResumeId?: number | null;
  readonly currentResumeId?: number | null;
  readonly savedSnapshot?: SavedResumeSnapshot | null;
  readonly savedResumeId?: number | null;
  readonly savedSnapshotResumeId?: number | null;
  readonly savedSnapshotApplicationId?: number | null;
  readonly savedSnapshotEventId?: number | null;
  readonly savedSnapshotSourceValid?: boolean;
  readonly asked?: boolean;
  readonly askOnce?: boolean;
}

export type ResumeSelectionKind = 'selected' | 'needs_selection' | 'unavailable';
export type ResumeSelectionSourceKind = 'lease' | 'current' | 'snapshot' | 'single_visible' | 'none' | 'unavailable';

export interface ResumeSelectionResult {
  readonly kind: ResumeSelectionKind;
  readonly status: ResumeSelectionKind;
  readonly source: ResumeSelectionSourceKind;
  readonly resumeId: number | null;
  readonly selection: ResumeSelectionCandidate | null;
  readonly shouldAsk: boolean;
  readonly reason: 'source_loading' | 'source_error' | 'source_absent' | 'source_unknown' | 'context_invalid' | 'resume_deleted' | 'selection_missing' | null;
}

export interface ResumeSelectionLeaseSelection {
  readonly applicationId: number;
  readonly eventId: number;
  readonly resumeId: number;
}

export interface ResumeSelectionLease {
  readonly generation: number;
  readonly revoked: boolean;
  select(selection: ResumeSelectionLeaseSelection): boolean;
  select(applicationId: number, eventId: number, resumeId: number): boolean;
  setSelection(selection: ResumeSelectionLeaseSelection): boolean;
  getSelection(applicationId: number, eventId: number): number | null;
  clearSelection(applicationId: number, eventId: number): boolean;
  hasAsked(applicationId: number, eventId: number): boolean;
  markAsked(applicationId: number, eventId: number): boolean;
  isUsable(generation?: number): boolean;
  revoke(): void;
  cancel(): void;
  release(): void;
  resolve(input: Omit<ResumeSelectionInput, 'lease'>): ResumeSelectionResult;
}

interface StoredSelection extends ResumeSelectionLeaseSelection {}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function validId(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0;
}

function pairKey(applicationId: number, eventId: number): string {
  return `${applicationId}:${eventId}`;
}

type ResumeSourceValue = NonNullable<ResumeSelectionInput['resumes']>;

function sourceStatus(value: ResumeSourceValue | null | undefined): ResumeSelectionSource<readonly ResumeSelectionCandidate[]>['status'] | 'direct' {
  if (value === undefined || value === null) return 'absent';
  if (isRecord(value) && typeof value.status === 'string') {
    return value.status === 'loading' || value.status === 'ready' || value.status === 'error'
      || value.status === 'absent' || value.status === 'unknown'
      ? value.status
      : 'unknown';
  }
  return 'direct';
}

function sourceRows(value: ResumeSourceValue | null | undefined): readonly ResumeSelectionCandidate[] | null {
  if (Array.isArray(value)) return value;
  if (isRecord(value) && Array.isArray(value.value)) return value.value as readonly ResumeSelectionCandidate[];
  return null;
}

function isVisible(candidate: ResumeSelectionCandidate): boolean {
  return validId(candidate.id)
    && candidate.deleted !== true
    && candidate.deletedAt == null
    && candidate.deleted_at == null
    && candidate.hidden !== true
    && candidate.visible !== false;
}

function belongsToContext(candidate: ResumeSelectionCandidate, input: ResumeSelectionInput): boolean {
  const applicationValues = [candidate.applicationId, candidate.application_id]
    .filter((value) => value !== undefined && value !== null);
  if (applicationValues.some((value) => value !== input.applicationId)) return false;
  if (applicationValues.length === 2 && applicationValues[0] !== applicationValues[1]) return false;
  const eventValues = [candidate.eventId, candidate.event_id]
    .filter((value) => value !== undefined && value !== null);
  if (eventValues.some((value) => value !== input.eventId)) return false;
  if (eventValues.length === 2 && eventValues[0] !== eventValues[1]) return false;
  return true;
}

interface OptionalScopedId {
  readonly present: boolean;
  readonly valid: boolean;
  readonly value: number | null;
}

function readOptionalScopedId(record: Record<string, unknown>, camel: string, snake: string): OptionalScopedId {
  const hasCamel = Object.prototype.hasOwnProperty.call(record, camel);
  const hasSnake = Object.prototype.hasOwnProperty.call(record, snake);
  const camelValue = record[camel];
  const snakeValue = record[snake];
  const nonNullCamel = camelValue !== undefined && camelValue !== null;
  const nonNullSnake = snakeValue !== undefined && snakeValue !== null;
  const camelValid = !nonNullCamel || validId(camelValue);
  const snakeValid = !nonNullSnake || validId(snakeValue);
  const camelId = validId(camelValue) ? camelValue : null;
  const snakeId = validId(snakeValue) ? snakeValue : null;
  const contradictory = camelId !== null && snakeId !== null && camelId !== snakeId;
  return {
    present: hasCamel || hasSnake,
    valid: camelValid && snakeValid && !contradictory,
    value: camelId ?? snakeId,
  };
}

function snapshotFromInput(input: ResumeSelectionInput): SavedResumeSnapshot | null {
  if (input.savedSnapshot) return input.savedSnapshot;
  if (input.savedSnapshotResumeId !== undefined || input.savedResumeId !== undefined) {
    return {
      applicationId: input.savedSnapshotApplicationId,
      eventId: input.savedSnapshotEventId,
      resumeId: input.savedSnapshotResumeId ?? input.savedResumeId ?? null,
      sourceValid: input.savedSnapshotSourceValid,
    };
  }
  return null;
}

function snapshotResumeId(snapshot: SavedResumeSnapshot): number | null {
  const result = readOptionalScopedId(snapshot as unknown as Record<string, unknown>, 'resumeId', 'resume_id');
  return result.valid ? result.value : null;
}

function snapshotMatchesContext(snapshot: SavedResumeSnapshot, input: ResumeSelectionInput): boolean {
  const record = snapshot as unknown as Record<string, unknown>;
  const appId = readOptionalScopedId(record, 'applicationId', 'application_id');
  if (!appId.present || !appId.valid || appId.value !== input.applicationId) return false;

  const eventId = readOptionalScopedId(record, 'eventId', 'event_id');
  // Material Kit / Submission snapshots are application-scoped in some API
  // paths. An omitted/null event is therefore valid; an explicit event must
  // remain exact and malformed identity must fail closed.
  if (!eventId.present) return true;
  return eventId.valid && (eventId.value === null || eventId.value === input.eventId);
}

function snapshotIsTrusted(snapshot: SavedResumeSnapshot, input: ResumeSelectionInput): boolean {
  return snapshotMatchesContext(snapshot, input)
    && (snapshot.sourceValid === true || snapshot.verified === true)
    && snapshot.deleted !== true
    && snapshot.deletedAt == null
    && snapshot.deleted_at == null
    && validId(snapshotResumeId(snapshot));
}

function makeResult(
  kind: ResumeSelectionKind,
  source: ResumeSelectionSourceKind,
  resumeId: number | null,
  selection: ResumeSelectionCandidate | null,
  shouldAsk: boolean,
  reason: ResumeSelectionResult['reason'],
): ResumeSelectionResult {
  return Object.freeze({
    kind,
    status: kind,
    source,
    resumeId,
    selection,
    shouldAsk,
    reason,
  });
}

/**
 * Resolve a resume without mutating application state. A selection is trusted
 * only when it is scoped to this application/event and still visible.
 */
function resolveResumeSelectionUnsafe(input: ResumeSelectionInput): ResumeSelectionResult {
  if (!validId(input.applicationId) || !validId(input.eventId)) {
    return makeResult('unavailable', 'unavailable', null, null, false, 'context_invalid');
  }

  const resumeSource = input.resumes ?? input.resumeSource;
  const status = sourceStatus(resumeSource);
  if (status !== 'direct' && status !== 'ready') {
    const reason = status === 'loading'
      ? 'source_loading'
      : status === 'error'
        ? 'source_error'
        : status === 'absent'
          ? 'source_absent'
          : 'source_unknown';
    return makeResult('unavailable', 'unavailable', null, null, false, reason);
  }

  const rows = sourceRows(resumeSource);
  if (status === 'ready' && rows === null) {
    return makeResult('unavailable', 'unavailable', null, null, false, 'source_unknown');
  }
  const safeRows = rows ?? [];
  const visible = safeRows.filter((candidate) => isVisible(candidate) && belongsToContext(candidate, input));
  const byId = new Map(visible.map((candidate) => [candidate.id, candidate]));
  const lease = input.lease;
  const leaseGenerationMatches = lease?.isUsable(input.generation) ?? false;
  const leaseResumeId = leaseGenerationMatches ? lease?.getSelection(input.applicationId, input.eventId) ?? null : null;
  if (leaseResumeId !== null) {
    const selected = byId.get(leaseResumeId);
    if (selected) return makeResult('selected', 'lease', selected.id, selected, false, null);
  }

  const explicitResumeId = input.selectedResumeId ?? input.currentResumeId;
  if (validId(explicitResumeId)) {
    const selected = byId.get(explicitResumeId);
    if (selected) return makeResult('selected', 'current', selected.id, selected, false, null);
  }

  const snapshot = snapshotFromInput(input);
  if (snapshot && snapshotIsTrusted(snapshot, input)) {
    const savedId = snapshotResumeId(snapshot);
    const selected = savedId === null ? undefined : byId.get(savedId);
    if (selected) return makeResult('selected', 'snapshot', selected.id, selected, false, null);
  }

  if (visible.length === 1) {
    return makeResult('selected', 'single_visible', visible[0].id, visible[0], false, null);
  }

  const alreadyAsked = input.asked === true
    || (input.askOnce === false)
    || (lease ? lease.hasAsked(input.applicationId, input.eventId) : false);
  const shouldAsk = !alreadyAsked;
  const attemptedResumeId = leaseResumeId ?? explicitResumeId;
  const attemptedWasDeleted = validId(attemptedResumeId)
    && safeRows.some((candidate) => candidate.id === attemptedResumeId && !isVisible(candidate));
  return makeResult('needs_selection', 'none', null, null, shouldAsk, attemptedWasDeleted ? 'resume_deleted' : 'selection_missing');
}

/** Source adapters are external boundaries; a hostile row/getter cannot make
 * selection appear trusted or escape into the task surface. */
export function resolveResumeSelection(input: ResumeSelectionInput): ResumeSelectionResult {
  try {
    return resolveResumeSelectionUnsafe(input);
  } catch {
    return makeResult('unavailable', 'unavailable', null, null, false, 'source_unknown');
  }
}

/**
 * Create a non-persistent, generation-scoped lease. Revoking it clears both
 * selection and ask-once state so a future generation cannot reuse either.
 */
export function createResumeSelectionLease(generation: number): ResumeSelectionLease {
  const safeGeneration = Number.isSafeInteger(generation) && generation >= 0 ? generation : 0;
  let active = true;
  const selections = new Map<string, StoredSelection>();
  const asked = new Set<string>();

  const lease: ResumeSelectionLease = {
    generation: safeGeneration,
    get revoked() {
      return !active;
    },
    select(first: ResumeSelectionLeaseSelection | number, second?: number, third?: number): boolean {
      if (!active) return false;
      const next: ResumeSelectionLeaseSelection = typeof first === 'number'
        ? { applicationId: first, eventId: second ?? 0, resumeId: third ?? 0 }
        : first;
      if (!validId(next.applicationId) || !validId(next.eventId) || !validId(next.resumeId)) return false;
      selections.set(pairKey(next.applicationId, next.eventId), { ...next });
      return true;
    },
    setSelection(next: ResumeSelectionLeaseSelection): boolean {
      return lease.select(next);
    },
    getSelection(applicationId: number, eventId: number): number | null {
      if (!active || !validId(applicationId) || !validId(eventId)) return null;
      const current = selections.get(pairKey(applicationId, eventId));
      return current
        && current.applicationId === applicationId
        && current.eventId === eventId
        ? current.resumeId
        : null;
    },
    clearSelection(applicationId: number, eventId: number): boolean {
      if (!active || !validId(applicationId) || !validId(eventId)) return false;
      return selections.delete(pairKey(applicationId, eventId));
    },
    hasAsked(applicationId: number, eventId: number): boolean {
      return active && asked.has(pairKey(applicationId, eventId));
    },
    markAsked(applicationId: number, eventId: number): boolean {
      if (!active || !validId(applicationId) || !validId(eventId)) return false;
      const key = pairKey(applicationId, eventId);
      const existed = asked.has(key);
      asked.add(key);
      return !existed;
    },
    isUsable(currentGeneration?: number): boolean {
      return active && (currentGeneration === undefined || currentGeneration === safeGeneration);
    },
    revoke(): void {
      active = false;
      selections.clear();
      asked.clear();
    },
    cancel(): void {
      lease.revoke();
    },
    release(): void {
      lease.revoke();
    },
    resolve(input: Omit<ResumeSelectionInput, 'lease'>): ResumeSelectionResult {
      return resolveResumeSelection({ ...input, lease });
    },
  };
  return lease;
}
