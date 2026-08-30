import { mapMaterialLabel, type MaterialLabelKey } from './materialLabels';

/**
 * The material split is intentionally driven by typed fields.  This type is
 * broad because the web API currently returns a few generations of the
 * source/capture envelope; the classifier narrows those envelopes without
 * using titles, body text, or paths as a signal.
 */
export interface MaterialRecord {
  readonly id?: number | string | null;
  readonly [key: string]: unknown;
}

// Runtime boundaries are deliberately unknown: hostile/partial API payloads
// are part of the projector's contract and must become unavailable rather
// than being hidden by a compile-time row shape.
type MaterialInputRecord = unknown;

export type MaterialClassification =
  | { readonly kind: 'experience_story' }
  | { readonly kind: 'confirmed_capture' }
  | { readonly kind: 'captured_unavailable'; readonly reason: 'capture_relation_invalid' }
  | { readonly kind: 'external_reference' }
  | { readonly kind: 'unclassified'; readonly reason: 'unsupported_source_kind' | 'missing_source_kind' | 'not_material_record' };

export type MaterialProjectionState = 'loading' | 'error' | 'empty' | 'partial' | 'ready' | 'unavailable';
export type MaterialSourceState = 'loading' | 'ready' | 'error' | 'absent' | 'unknown' | 'empty' | 'partial';

export interface MaterialSourceEnvelope<T = readonly MaterialInputRecord[]> {
  readonly status: MaterialSourceState;
  readonly value?: T | null;
  readonly error?: unknown;
}

export interface MaterialProjectionInput {
  /** A combined input is convenient for callers that already have one cache. */
  readonly records?: readonly MaterialInputRecord[] | MaterialSourceEnvelope<readonly MaterialInputRecord[]> | null;
  /** Composed inputs keep Story and capture loaders independently observable. */
  readonly stories?: readonly MaterialInputRecord[] | MaterialSourceEnvelope<readonly MaterialInputRecord[]> | null;
  readonly captures?: readonly MaterialInputRecord[] | MaterialSourceEnvelope<readonly MaterialInputRecord[]> | null;
  readonly sources?: readonly MaterialInputRecord[] | MaterialSourceEnvelope<readonly MaterialInputRecord[]> | null;
  readonly knowledgeSources?: readonly MaterialInputRecord[] | MaterialSourceEnvelope<readonly MaterialInputRecord[]> | null;
  readonly confirmedCaptures?: readonly MaterialInputRecord[] | MaterialSourceEnvelope<readonly MaterialInputRecord[]> | null;
  readonly recordsState?: MaterialSourceState;
  readonly storiesState?: MaterialSourceState;
  readonly capturesState?: MaterialSourceState;
  readonly sourcesState?: MaterialSourceState;
  readonly knowledgeSourcesState?: MaterialSourceState;
  readonly confirmedCapturesState?: MaterialSourceState;
  readonly state?: MaterialSourceState;
}

export interface MaterialProjectionItem {
  readonly internalKey: string;
  readonly kind: 'experience_story' | 'confirmed_capture' | 'external_reference';
  readonly label: string;
  readonly title: string;
  readonly summary: string;
  readonly sourceState: 'current' | 'source_changed' | 'unavailable';
  /** Kept for event handlers; views must not render this identity. */
  readonly record: MaterialRecord;
}

export interface MaterialProjectionIssue {
  readonly kind: 'captured_unavailable' | 'unclassified' | 'source_unavailable';
  readonly reason: string;
}

export interface ExperienceMaterialProjection {
  readonly items: readonly MaterialProjectionItem[];
  readonly state: MaterialProjectionState;
  readonly hasUnavailable: boolean;
  readonly unavailable: readonly MaterialProjectionIssue[];
}

export interface ExternalReferenceProjection {
  readonly items: readonly MaterialProjectionItem[];
  readonly state: MaterialProjectionState;
  readonly hasUnavailable: boolean;
  readonly unavailable: readonly MaterialProjectionIssue[];
}

const CAPTURE_ORIGIN = 'confirmed_interview_capture';
const CAPTURE_SOURCE_KIND = 'captured_interview_note';
const CAPTURE_SCHEMA_VERSION = 'interview-note-capture-v1';
const EXTERNAL_SOURCE_KINDS = new Set(['markdown', 'text', 'bundle']);
const RAW_RECORD_KINDS = new Set([
  'interview_note',
  'raw_interview_note',
  'proposal',
  'interview_proposal',
  'pending',
  'interview_pending',
  'preview',
]);
const STORY_SOURCE_KINDS = new Set(['resume_version', 'interview_note', 'mock_turn', 'user_assertion']);

type UnknownRecord = Record<string, unknown>;

function recordOf(value: unknown): UnknownRecord | null {
  try {
    return typeof value === 'object' && value !== null && !Array.isArray(value)
      ? value as UnknownRecord
      : null;
  } catch {
    return null;
  }
}

function read(record: UnknownRecord, ...names: string[]): unknown {
  for (const name of names) {
    try {
      if (name in record) return record[name];
    } catch {
      return undefined;
    }
  }
  return undefined;
}

function text(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function positiveId(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0 ? value : null;
}

function hasOwn(record: UnknownRecord, ...names: string[]): boolean {
  try {
    return names.some((name) => Object.prototype.hasOwnProperty.call(record, name));
  } catch {
    return false;
  }
}

function captureMetadata(record: UnknownRecord): UnknownRecord | null {
  const value = read(
    record,
    'capture_metadata',
    'captureMetadata',
    'captured_source_metadata',
    'capturedSourceMetadata',
  );
  const explicit = recordOf(value);
  if (explicit) return explicit;

  // Some older confirmed-note responses flatten the metadata relation.  Treat
  // this as metadata only when a typed capture field is present; arbitrary
  // source metadata must never make an external document a capture.
  const metadataNames = [
    'origin_note_id',
    'originNoteId',
    'application_event_id',
    'applicationEventId',
    'note_fingerprint',
    'noteFingerprint',
    'capture_schema_version',
    'captureSchemaVersion',
  ];
  return hasOwn(record, ...metadataNames)
    ? record
    : null;
}

function hasCaptureMetadataMarker(record: UnknownRecord): boolean {
  return hasOwn(record, 'capture_metadata', 'captureMetadata', 'captured_source_metadata', 'capturedSourceMetadata')
    || captureMetadata(record) !== null;
}

function hasCaptureSignal(record: UnknownRecord): boolean {
  const origin = text(read(record, 'origin_kind', 'originKind'));
  const sourceKind = text(read(record, 'source_kind', 'sourceKind'));
  return origin === CAPTURE_ORIGIN
    || sourceKind === CAPTURE_SOURCE_KIND
    || hasCaptureMetadataMarker(record);
}

function hasUnsupportedOriginMarker(record: UnknownRecord): boolean {
  if (!hasOwn(record, 'origin_kind', 'originKind')) return false;
  const origin = read(record, 'origin_kind', 'originKind');
  // Source serializers may include an explicit nullable relation column.
  // Null is still "no origin"; any other non-capture value is an unknown
  // typed origin and must not fall through to external-reference handling.
  return origin !== null && origin !== undefined;
}

function readMetadataId(metadata: UnknownRecord, ...names: string[]): number | null {
  return positiveId(read(metadata, ...names));
}

function completeCaptureRelation(record: UnknownRecord, metadata: UnknownRecord | null): boolean {
  // A capture is only complete when the typed metadata relation is present.
  // In particular, an origin marker plus a source/version id is not enough:
  // an orphaned or partially serialized capture must remain unavailable.
  if (!metadata) return false;
  const source = sourceKind(record);
  const sourceIdPresent = hasOwn(record, 'source_id', 'sourceId');
  const explicitSourceId = positiveId(read(record, 'source_id', 'sourceId'));
  const sourceId = sourceIdPresent
    ? explicitSourceId
    : source === CAPTURE_SOURCE_KIND
      ? positiveId(read(record, 'id'))
      : null;
  if (!sourceId) return false;
  const originNoteId = readMetadataId(metadata, 'origin_note_id', 'originNoteId', 'note_id', 'noteId');
  const rawApplicationEventId = read(metadata, 'application_event_id', 'applicationEventId');
  const applicationEventId = rawApplicationEventId === null
    ? null
    : positiveId(rawApplicationEventId);
  const fingerprint = text(read(metadata, 'note_fingerprint', 'noteFingerprint'));
  const schemaVersion = text(read(metadata, 'capture_schema_version', 'captureSchemaVersion'));
  if (!originNoteId || rawApplicationEventId === undefined || (rawApplicationEventId !== null && !applicationEventId)
    || !fingerprint || schemaVersion !== CAPTURE_SCHEMA_VERSION) return false;
  const metadataSourceId = read(metadata, 'source_id', 'sourceId');
  if (metadataSourceId !== undefined && positiveId(metadataSourceId) !== sourceId) return false;
  const metadataVersionId = read(metadata, 'version_id', 'versionId');
  if (metadataVersionId !== undefined && !positiveId(metadataVersionId)) return false;

  const relatedNote = recordOf(read(record, 'confirmed_note', 'confirmedNote', 'origin_note', 'originNote', 'note'));
  if (hasOwn(record, 'confirmed_note', 'confirmedNote', 'origin_note', 'originNote', 'note') && !relatedNote) return false;
  if (source === CAPTURE_SOURCE_KIND && !relatedNote) return false;
  if (relatedNote) {
    const relatedId = positiveId(read(relatedNote, 'id', 'note_id', 'noteId'));
    if (!relatedId || relatedId !== originNoteId) return false;
  }
  const relatedEvent = recordOf(read(record, 'application_event', 'applicationEvent', 'event'));
  const hasRelatedEventField = hasOwn(record, 'application_event', 'applicationEvent', 'event');
  const rawRelatedEvent = hasRelatedEventField
    ? read(record, 'application_event', 'applicationEvent', 'event')
    : undefined;
  if (hasRelatedEventField && rawRelatedEvent !== null && !relatedEvent) return false;
  if (hasRelatedEventField && rawRelatedEvent === null && applicationEventId !== null) return false;
  if (relatedEvent) {
    const relatedEventId = positiveId(read(relatedEvent, 'id', 'event_id', 'eventId'));
    if (applicationEventId === null || !relatedEventId || relatedEventId !== applicationEventId) return false;
  }
  return true;
}

function isStory(record: UnknownRecord): boolean {
  const kind = text(read(record, 'kind', 'record_kind', 'recordKind', 'type'))?.toLowerCase() ?? null;
  if (kind !== null && kind !== 'interview_story' && kind !== 'story') return false;
  const id = positiveId(read(record, 'id'));
  const title = text(read(record, 'title'));
  const status = text(read(record, 'status'));
  const revision = positiveId(read(record, 'story_revision', 'storyRevision'));
  const currentVersion = read(record, 'current_version_id', 'currentVersionId');
  const versionValid = currentVersion === null || positiveId(currentVersion) !== null;
  const versionNumber = read(record, 'version_number', 'versionNumber');
  const versionNumberValid = versionNumber === null || positiveId(versionNumber) !== null;
  const sourceStates = read(record, 'source_states', 'sourceStates');
  let sourceStatesValid = false;
  let sourceStatesIsArray = false;
  try {
    sourceStatesIsArray = Array.isArray(sourceStates);
  } catch {
    sourceStatesIsArray = false;
  }
  if (sourceStatesIsArray) {
    const sourceStateItems = sourceStates as unknown[];
    try {
      sourceStatesValid = true;
      for (let index = 0; index < sourceStateItems.length; index += 1) {
        if (!(index in sourceStateItems)) {
          sourceStatesValid = false;
          break;
        }
        const state = recordOf(sourceStateItems[index]);
        const stateName = text(state ? read(state, 'state') : undefined);
        const stateSourceKind = text(state ? read(state, 'source_kind', 'sourceKind') : undefined);
        const stableId = text(state ? read(state, 'source_stable_id', 'sourceStableId') : undefined);
        const sourceVersion = text(state ? read(state, 'source_version_or_snapshot', 'sourceVersionOrSnapshot') : undefined);
        if (!state || !stateName || !['current', 'changed', 'missing', 'frozen_user_assertion', 'error', 'deleted', 'unknown'].includes(stateName)
          || !stateSourceKind || !STORY_SOURCE_KINDS.has(stateSourceKind) || !stableId || !sourceVersion) {
          sourceStatesValid = false;
          break;
        }
      }
    } catch {
      sourceStatesValid = false;
    }
  }
  return id !== null
    && title !== null
    && (status === 'active' || status === 'archived')
    && revision !== null
    && versionValid
    && versionNumberValid
    && sourceStatesValid
    && !hasCaptureSignal(record);
}

function isExplicitStoryKind(record: UnknownRecord): boolean {
  const kind = text(read(record, 'kind', 'record_kind', 'recordKind', 'type'))?.toLowerCase() ?? null;
  return kind === 'interview_story' || kind === 'story';
}

function isRawRecord(record: UnknownRecord): boolean {
  const kind = text(read(record, 'kind', 'record_kind', 'recordKind', 'type'))?.toLowerCase();
  return Boolean(kind && RAW_RECORD_KINDS.has(kind));
}

function sourceKind(record: UnknownRecord): string | null {
  return text(read(record, 'source_kind', 'sourceKind'))?.toLowerCase() ?? null;
}

function recordKind(record: UnknownRecord): string | null {
  return text(read(record, 'kind', 'record_kind', 'recordKind', 'type'))?.toLowerCase() ?? null;
}

/**
 * Classify one record using origin/source truth.  The result is deliberately
 * small so consumers cannot accidentally turn internal relation data into UI
 * copy.  Capture signals are evaluated before external source kinds.
 */
export function classifyMaterialRecord(input: unknown): MaterialClassification {
  const record = recordOf(input);
  if (!record) return { kind: 'unclassified', reason: 'not_material_record' };

  try {
    const origin = text(read(record, 'origin_kind', 'originKind'));
    const source = sourceKind(record);
    if (origin === CAPTURE_ORIGIN || source === CAPTURE_SOURCE_KIND || hasCaptureMetadataMarker(record)) {
      return completeCaptureRelation(record, captureMetadata(record))
        ? { kind: 'confirmed_capture' }
        : { kind: 'captured_unavailable', reason: 'capture_relation_invalid' };
    }

    if (hasUnsupportedOriginMarker(record)) return { kind: 'unclassified', reason: 'unsupported_source_kind' };

    if (isStory(record)) return { kind: 'experience_story' };
    if (isExplicitStoryKind(record)) return { kind: 'unclassified', reason: 'not_material_record' };
    if (isRawRecord(record)) return { kind: 'unclassified', reason: 'not_material_record' };
    if (source && EXTERNAL_SOURCE_KINDS.has(source)) {
      const kind = recordKind(record);
      return kind === null || kind === 'knowledge_source' || kind === 'source'
        ? { kind: 'external_reference' }
        : { kind: 'unclassified', reason: 'unsupported_source_kind' };
    }
    return {
      kind: 'unclassified',
      reason: source ? 'unsupported_source_kind' : 'missing_source_kind',
    };
  } catch {
    return { kind: 'unclassified', reason: 'not_material_record' };
  }
}

interface ParsedSource {
  readonly records: readonly unknown[];
  readonly status: MaterialSourceState | null;
  readonly invalid: boolean;
  readonly reason: string;
}

function sourceState(value: unknown): MaterialSourceState | null {
  const status = text(value);
  if (status === 'loading' || status === 'ready' || status === 'error' || status === 'absent'
    || status === 'unknown' || status === 'empty' || status === 'partial') return status;
  return null;
}

function sourceValues(values: unknown[]): { records: readonly unknown[]; invalid: boolean; nonEmpty: boolean } {
  try {
    let invalid = false;
    const records: unknown[] = [];
    const length = values.length;
    for (let index = 0; index < length; index += 1) {
      if (!(index in values)) {
        invalid = true;
        continue;
      }
      const value = values[index];
      if (!recordOf(value)) {
        invalid = true;
        continue;
      }
      records.push(value);
    }
    return { records, invalid, nonEmpty: length > 0 };
  } catch {
    return { records: [], invalid: true, nonEmpty: false };
  }
}

function parseSource(
  value: unknown,
  explicitStatus: unknown,
  explicitStatusProvided: boolean,
  valueProvided: boolean,
): ParsedSource {
  const explicit = explicitStatusProvided ? sourceState(explicitStatus) : null;
  const invalidExplicit = explicitStatusProvided && explicit === null;

  if (value === undefined) {
    const statusAllowsNoValue = explicit === 'loading' || explicit === 'error' || explicit === 'absent'
      || explicit === 'unknown' || explicit === 'empty';
    const valueMissing = valueProvided
      || (explicit !== null && !statusAllowsNoValue)
      || (valueProvided && explicit === null);
    return {
      records: [],
      status: explicit,
      invalid: invalidExplicit || valueMissing,
      reason: invalidExplicit ? 'source_status_invalid' : valueMissing ? 'source_value_missing' : 'source_absent',
    };
  }

  let valueIsArray = false;
  try {
    valueIsArray = Array.isArray(value);
  } catch {
    return { records: [], status: explicit, invalid: true, reason: 'source_envelope_invalid' };
  }
  if (valueIsArray) {
    const parsed = sourceValues(value as unknown[]);
    const valueStatusConflict = parsed.nonEmpty
      && explicit !== null
      && explicit !== 'ready'
      && explicit !== 'partial';
    return {
      records: valueStatusConflict ? [] : parsed.records,
      status: explicit,
      invalid: invalidExplicit || parsed.invalid || valueStatusConflict,
      reason: invalidExplicit
        ? 'source_status_invalid'
        : valueStatusConflict ? 'source_value_status_conflict' : parsed.invalid ? 'record_invalid' : '',
    };
  }

  const envelope = recordOf(value);
  if (!envelope) {
    return { records: [], status: explicit, invalid: true, reason: 'source_envelope_invalid' };
  }

  const envelopeHasStatus = hasOwn(envelope, 'status');
  const envelopeStatus = sourceState(read(envelope, 'status'));
  const status = explicit ?? envelopeStatus;
  const statusConflict = explicitStatusProvided
    && envelopeHasStatus
    && explicit !== null
    && envelopeStatus !== null
    && explicit !== envelopeStatus;
  const invalidEnvelopeStatus = (!explicitStatusProvided && !envelopeHasStatus)
    || (envelopeHasStatus && envelopeStatus === null)
    || invalidExplicit
    || statusConflict;
  const hasValue = hasOwn(envelope, 'value');
  const nested = read(envelope, 'value');
  const statusAllowsNoValue = status === 'loading' || status === 'error' || status === 'absent'
    || status === 'unknown' || status === 'empty';
  if (!hasValue) {
    return {
      records: [],
      status,
      invalid: invalidEnvelopeStatus || !statusAllowsNoValue,
      reason: invalidEnvelopeStatus ? 'source_status_invalid' : !statusAllowsNoValue ? 'source_value_missing' : '',
    };
  }
  if (nested === undefined || nested === null) {
    return {
      records: [],
      status,
      invalid: invalidEnvelopeStatus || !statusAllowsNoValue,
      reason: invalidEnvelopeStatus
        ? 'source_status_invalid'
        : !statusAllowsNoValue ? 'source_value_invalid' : '',
    };
  }
  let nestedIsArray = false;
  try {
    nestedIsArray = Array.isArray(nested);
  } catch {
    return { records: [], status, invalid: true, reason: 'source_value_invalid' };
  }
  if (!nestedIsArray) {
    return { records: [], status, invalid: true, reason: 'source_value_invalid' };
  }
  const parsed = sourceValues(nested as unknown[]);
  const valueStatusConflict = parsed.nonEmpty
    && status !== null
    && status !== 'ready'
    && status !== 'partial';
  if (statusConflict || valueStatusConflict) {
    return {
      records: [],
      status,
      invalid: true,
      reason: statusConflict ? 'source_status_conflict' : 'source_value_status_conflict',
    };
  }
  return {
    records: parsed.records,
    status,
    invalid: invalidEnvelopeStatus || parsed.invalid,
    reason: invalidEnvelopeStatus ? 'source_status_invalid' : parsed.invalid ? 'record_invalid' : '',
  };
}

function explicitStatus(input: MaterialProjectionInput, name: keyof MaterialProjectionInput): {
  readonly provided: boolean;
  readonly value: unknown;
} {
  const source = input as unknown as UnknownRecord;
  const provided = hasOwn(source, name);
  return { provided, value: provided ? read(source, name) : undefined };
}

function projectionStatus(statuses: readonly (MaterialSourceState | null)[], itemCount: number): MaterialProjectionState {
  const known = statuses.filter((status): status is MaterialSourceState => status !== null);
  if (known.length === 0) return itemCount > 0 ? 'ready' : 'empty';
  if (known.every((status) => status === 'loading')) return 'loading';
  if (known.every((status) => status === 'error' || status === 'absent' || status === 'unknown')) return 'error';
  if (known.some((status) => status === 'partial')) return 'partial';
  if (known.some((status) => status === 'loading' || status === 'error' || status === 'absent' || status === 'unknown')) return 'partial';
  if (itemCount === 0 && known.every((status) => status === 'empty' || status === 'ready')) return 'empty';
  return itemCount > 0 ? 'ready' : 'empty';
}

function safeTitle(record: UnknownRecord, fallback: string): string {
  const value = text(read(record, 'title', 'display_title', 'displayTitle', 'name')) ?? fallback;
  return Array.from(value).slice(0, 160).join('');
}

function safeSummary(record: UnknownRecord, fallback: string): string {
  const content = read(record, 'summary', 'excerpt', 'description');
  const value = text(content) ?? fallback;
  return Array.from(value).slice(0, 360).join('');
}

function safeCloneAndFreeze<T>(value: T, seen = new WeakMap<object, unknown>()): T {
  if (value === null || typeof value !== 'object') return value;
  const source = value as object;
  const prior = seen.get(source);
  if (prior !== undefined) return prior as T;

  let array = false;
  try {
    array = Array.isArray(value);
  } catch {
    return Object.freeze(Object.create(null)) as T;
  }
  const clone = (array ? [] : Object.create(null)) as Record<PropertyKey, unknown>;
  seen.set(source, clone);
  let keys: PropertyKey[];
  try {
    keys = Reflect.ownKeys(source);
  } catch {
    return Object.freeze(clone) as T;
  }
  for (const key of keys) {
    if (array && key === 'length') continue;
    try {
      const descriptor = Object.getOwnPropertyDescriptor(source, key);
      if (!descriptor || !('value' in descriptor)) continue;
      clone[key] = safeCloneAndFreeze(descriptor.value, seen);
    } catch {
      // Accessors and hostile descriptors are not copied into the read model.
    }
  }
  return Object.freeze(clone) as T;
}

function stableSerialize(value: unknown, seen = new WeakSet<object>()): string {
  if (value === null) return 'null';
  switch (typeof value) {
    case 'undefined': return 'undefined';
    case 'string': return JSON.stringify(value);
    case 'number': return Number.isFinite(value) ? String(value) : `[${String(value)}]`;
    case 'boolean': return String(value);
    case 'bigint': return `${String(value)}n`;
    case 'symbol': return String(value);
    case 'function': return '[function]';
    default: break;
  }
  if (seen.has(value)) return '[circular]';
  seen.add(value);
  if (Array.isArray(value)) {
    let length = 0;
    try { length = value.length; } catch { return '[unreadable-array]'; }
    const output = [];
    for (let index = 0; index < length; index += 1) {
      output.push(index in value ? stableSerialize(value[index], seen) : '[hole]');
    }
    return `[${output.join(',')}]`;
  }
  const object = recordOf(value);
  if (!object) return '[object]';
  let keys: string[];
  try { keys = Object.keys(object).sort(); } catch { return '[unreadable-object]'; }
  return `{${keys.map((key) => {
    try { return `${JSON.stringify(key)}:${stableSerialize(object[key], seen)}`; } catch { return `${JSON.stringify(key)}:[unreadable]`; }
  }).join(',')}}`;
}

function itemIdentity(record: UnknownRecord, classification: MaterialClassification): string | null {
  if (classification.kind === 'unclassified') return null;
  const id = positiveId(read(record, 'id', 'note_id', 'noteId', 'story_id', 'storyId'));
  const sourceIdPresent = hasOwn(record, 'source_id', 'sourceId');
  const sourceId = positiveId(read(record, 'source_id', 'sourceId'));
  if (sourceIdPresent && sourceId === null) return null;
  const typedSource = sourceKind(record);
  const captureLike = classification.kind === 'confirmed_capture'
    || classification.kind === 'captured_unavailable'
    || text(read(record, 'origin_kind', 'originKind')) === CAPTURE_ORIGIN
    || typedSource === CAPTURE_SOURCE_KIND;
  if (captureLike && sourceId) return `source:${sourceId}`;
  if (typedSource && EXTERNAL_SOURCE_KINDS.has(typedSource) && id) {
    return `source:${sourceId ?? id}`;
  }
  if (id) return `${classification.kind}:${id}`;
  return null;
}

function sourceStateFor(record: UnknownRecord, classification: MaterialClassification): MaterialProjectionItem['sourceState'] {
  if (classification.kind === 'captured_unavailable') return 'unavailable';
  const sourceStatus = text(read(record, 'source_status', 'sourceStatus'));
  if (sourceStatus === 'missing' || sourceStatus === 'error' || sourceStatus === 'deleted' || sourceStatus === 'unknown') return 'unavailable';
  if (sourceStatus === 'source_changed' || sourceStatus === 'changed') return 'source_changed';
  if (sourceStatus !== null && sourceStatus !== 'current' && sourceStatus !== 'frozen') return 'unavailable';
  const sourceStates = read(record, 'source_states', 'sourceStates');
  let sourceStatesIsArray = false;
  try {
    sourceStatesIsArray = Array.isArray(sourceStates);
  } catch {
    return 'unavailable';
  }
  if (hasOwn(record, 'source_states', 'sourceStates') && !sourceStatesIsArray) return 'unavailable';
  if (sourceStatesIsArray) {
    const sourceStateItems = sourceStates as unknown[];
    try {
      let changed = false;
      for (let index = 0; index < sourceStateItems.length; index += 1) {
        if (!(index in sourceStateItems)) return 'unavailable';
        const item = recordOf(sourceStateItems[index]);
        const state = text(item ? read(item, 'state') : undefined);
        if (!item || state === null) return 'unavailable';
        if (state === 'missing' || state === 'error' || state === 'deleted' || state === 'unknown'
          || (state !== 'current' && state !== 'changed' && state !== 'frozen_user_assertion')) return 'unavailable';
        if (state === 'changed') changed = true;
      }
      if (changed) return 'source_changed';
    } catch {
      return 'unavailable';
    }
  }
  return 'current';
}

function rank(classification: MaterialClassification): number {
  switch (classification.kind) {
    case 'captured_unavailable': return 5;
    case 'confirmed_capture': return 4;
    case 'experience_story': return 3;
    case 'external_reference': return 2;
    case 'unclassified': return 1;
  }
}

function sourceStateRank(record: UnknownRecord, classification: MaterialClassification): number {
  switch (sourceStateFor(record, classification)) {
    case 'unavailable': return 3;
    case 'source_changed': return 2;
    case 'current': return 1;
  }
}

function canonicalRecordKey(record: UnknownRecord): string {
  try {
    return stableSerialize(record);
  } catch {
    return '[unreadable-record]';
  }
}

interface CanonicalCandidate {
  readonly record: UnknownRecord;
  readonly classification: MaterialClassification;
  readonly canonicalKey: string;
}

function compareCanonicalCandidates(left: CanonicalCandidate, right: CanonicalCandidate): number {
  const rankDifference = rank(left.classification) - rank(right.classification);
  if (rankDifference !== 0) return rankDifference;
  const sourceRankDifference = sourceStateRank(left.record, left.classification)
    - sourceStateRank(right.record, right.classification);
  if (sourceRankDifference !== 0) return sourceRankDifference;
  // Lexicographic selection makes equal identities independent of API row
  // order.  The key is never rendered or sent back to the API.
  if (left.canonicalKey < right.canonicalKey) return 1;
  if (left.canonicalKey > right.canonicalKey) return -1;
  return 0;
}

function collectRecords(input: MaterialProjectionInput): {
  records: readonly unknown[];
  statuses: readonly (MaterialSourceState | null)[];
  issues: readonly MaterialProjectionIssue[];
} {
  const records: unknown[] = [];
  const statuses: Array<MaterialSourceState | null> = [];
  const issues: MaterialProjectionIssue[] = [];
  const inputRecord = recordOf(input);
  try {
    if (!inputRecord) throw new Error('invalid material projection input');
    Reflect.ownKeys(inputRecord);
  } catch {
    return {
      records,
      statuses,
      issues: [Object.freeze({ kind: 'source_unavailable', reason: 'source_input_invalid' })],
    };
  }
  const collect = (
    value: unknown,
    statusName: keyof MaterialProjectionInput,
    valueProvided: boolean,
  ) => {
    try {
      const explicit = explicitStatus(input, statusName);
      const parsed = parseSource(value, explicit.value, explicit.provided, valueProvided);
      for (let index = 0; index < parsed.records.length; index += 1) {
        records.push(parsed.records[index]);
      }
      if (parsed.status !== null) statuses.push(parsed.status);
      if (parsed.invalid) {
        issues.push(Object.freeze({
          kind: 'source_unavailable',
          reason: parsed.reason || 'source_invalid',
        }));
      }
    } catch {
      issues.push(Object.freeze({ kind: 'source_unavailable', reason: 'source_invalid' }));
    }
  };
  const combined = read(inputRecord, 'records');
  if (hasOwn(inputRecord, 'records')) {
    collect(combined, 'recordsState', true);
  } else if (hasOwn(inputRecord, 'recordsState')) {
    collect(undefined, 'recordsState', false);
  } else if (hasOwn(inputRecord, 'state')) {
    collect(undefined, 'state', false);
  }
  for (const [value, statusName] of [
    [read(inputRecord, 'stories'), 'storiesState'],
    [hasOwn(inputRecord, 'captures') ? read(inputRecord, 'captures') : read(inputRecord, 'confirmedCaptures'), hasOwn(inputRecord, 'captures') ? 'capturesState' : 'confirmedCapturesState'],
    [hasOwn(inputRecord, 'sources') ? read(inputRecord, 'sources') : read(inputRecord, 'knowledgeSources'), hasOwn(inputRecord, 'sources') ? 'sourcesState' : 'knowledgeSourcesState'],
  ] as const) {
    if (value !== undefined) {
      collect(value, statusName, true);
    } else {
      const explicit = explicitStatus(input, statusName);
      if (explicit.provided) collect(undefined, statusName, false);
    }
  }
  return { records, statuses, issues };
}

function project(input: MaterialProjectionInput, wanted: 'experience' | 'external'): ExperienceMaterialProjection | ExternalReferenceProjection {
  const { records, statuses, issues: collectedIssues } = collectRecords(input);
  const byIdentity = new Map<string, CanonicalCandidate>();
  const issues: MaterialProjectionIssue[] = [...collectedIssues];

  for (const candidate of records) {
    const record = recordOf(candidate);
    if (!record) continue;
    const classification = classifyMaterialRecord(record);
    const identity = itemIdentity(record, classification);
    if (!identity) {
      issues.push(Object.freeze({ kind: classification.kind === 'captured_unavailable' ? 'captured_unavailable' : 'unclassified', reason: 'identity_missing' }));
      continue;
    }
    const next: CanonicalCandidate = { record, classification, canonicalKey: canonicalRecordKey(record) };
    const previous = byIdentity.get(identity);
    if (!previous || compareCanonicalCandidates(next, previous) > 0) {
      byIdentity.set(identity, next);
    }
  }

  const items: MaterialProjectionItem[] = [];
  for (const { record, classification } of byIdentity.values()) {
    if (classification.kind === 'captured_unavailable' || classification.kind === 'unclassified') {
      issues.push(Object.freeze({ kind: classification.kind, reason: classification.reason }));
      continue;
    }
    const matches = wanted === 'experience'
      ? classification.kind === 'experience_story' || classification.kind === 'confirmed_capture'
      : classification.kind === 'external_reference';
    if (!matches) continue;
    const labelKey: MaterialLabelKey = classification.kind;
    const title = safeTitle(record, classification.kind === 'experience_story' ? '经历故事' : classification.kind === 'confirmed_capture' ? '已确认面试片段' : '参考资料');
    const summary = safeSummary(record, title);
    const itemSourceState = sourceStateFor(record, classification);
    if (itemSourceState === 'unavailable') {
      issues.push(Object.freeze({ kind: 'source_unavailable', reason: 'source_state_unavailable' }));
    }
    items.push(Object.freeze({
      internalKey: itemIdentity(record, classification) ?? `invalid:${items.length}`,
      kind: classification.kind,
      label: mapMaterialLabel(labelKey),
      title,
      summary,
      sourceState: itemSourceState,
      record: safeCloneAndFreeze(record),
    }));
  }

  items.sort((left, right) => {
    const keyOrder = left.internalKey.localeCompare(right.internalKey);
    if (keyOrder !== 0) return keyOrder;
    const titleOrder = left.title.localeCompare(right.title);
    if (titleOrder !== 0) return titleOrder;
    return left.summary.localeCompare(right.summary);
  });
  const baseState = projectionStatus(statuses, items.length);
  const hasProjectionIssue = issues.length > 0;
  const unavailableItems = items.filter((item) => item.sourceState === 'unavailable').length;
  const projectionState: MaterialProjectionState = hasProjectionIssue || unavailableItems > 0
    ? items.length === 0 || unavailableItems === items.length ? 'unavailable' : 'partial'
    : baseState;
  const hasUnavailable = hasProjectionIssue
    || projectionState === 'error'
    || projectionState === 'partial'
    || projectionState === 'unavailable'
    || items.some((item) => item.sourceState === 'unavailable');
  return Object.freeze({
    items: Object.freeze(items),
    state: projectionState,
    hasUnavailable,
    unavailable: Object.freeze(issues),
  });
}

export function projectExperienceMaterials(input: MaterialProjectionInput): ExperienceMaterialProjection {
  return project(input, 'experience') as ExperienceMaterialProjection;
}

export function projectExternalReferences(input: MaterialProjectionInput): ExternalReferenceProjection {
  return project(input, 'external') as ExternalReferenceProjection;
}
