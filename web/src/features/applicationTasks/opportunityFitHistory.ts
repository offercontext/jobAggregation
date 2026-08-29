import type {
  OpportunityFitRecommendation,
  OpportunityFitReviewSummary,
  OpportunityFitV2SessionSummary,
} from '@/types/opportunityFitReview';

/**
 * The only details that may cross the history boundary. Identity, source
 * kind, schema/version, hashes, tokens and raw provider data stay in the
 * owner and are deliberately absent from this model.
 */
export type OpportunityFitHistoryResultState = 'ready' | 'result_unknown' | 'source_changed';

export interface SafeOpportunityFitDetails {
  readonly recommendation: OpportunityFitRecommendation | 'unknown';
  readonly hasDeepReview: boolean;
  readonly evidenceSummary: string;
  readonly resultState: OpportunityFitHistoryResultState;
}

export type OpportunityFitHistorySourceState = 'current' | 'source_changed' | 'unavailable';

export interface OpportunityFitHistoryItem {
  /** React-only identity. It must never be copied into user-visible text. */
  readonly internalKey: string;
  readonly createdAt: string;
  readonly summary: string;
  readonly sourceState: OpportunityFitHistorySourceState;
  readonly details: SafeOpportunityFitDetails;
}

export type OpportunityFitHistoryRead<T> =
  | { readonly status: 'ready'; readonly value: readonly T[] }
  | { readonly status: 'loading' | 'error' | 'absent'; readonly reason?: string };

export interface OpportunityFitHistorySources {
  readonly v1: OpportunityFitHistoryRead<unknown> | readonly unknown[] | null | undefined;
  readonly v2: OpportunityFitHistoryRead<unknown> | readonly unknown[] | null | undefined;
  /** Optional current source fingerprint used to label frozen historical rows. */
  readonly currentSourceFingerprint?: string;
  readonly applicationId?: number;
}

export interface OpportunityFitHistoryProjection {
  readonly items: readonly OpportunityFitHistoryItem[];
  /** True when one or more source envelopes were not available. */
  readonly partial: boolean;
  readonly unavailableSources: readonly ('v1' | 'v2')[];
  readonly invalidRecordCount: number;
}

const SOURCE_ORDINAL: Readonly<Record<'v1' | 'v2', number>> = Object.freeze({ v1: 1, v2: 2 });
const MAX_SAFE_TEXT_LENGTH = 8_000;

function isRecord(value: unknown): value is Record<string, unknown> {
  try {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
  } catch {
    return false;
  }
}

function hasOwn(value: Record<string, unknown>, key: string): boolean {
  try {
    return Object.prototype.hasOwnProperty.call(value, key);
  } catch {
    return false;
  }
}

function safePositiveInteger(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0 ? value : null;
}

function safeText(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  return trimmed.length > 0 && trimmed.length <= MAX_SAFE_TEXT_LENGTH ? trimmed : null;
}

/**
 * Parse only the finite RFC3339 business subset accepted by the API. Native
 * Date.parse accepts calendar rollover (for example 02-30), so calendar
 * components are checked before asking the runtime for the UTC instant.
 */
export function normalizeOpportunityFitHistoryDate(value: unknown): string | null {
  const text = safeText(value);
  if (!text) return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})$/.exec(text);
  if (!match) return null;

  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);
  if (month < 1 || month > 12 || hour > 23 || minute > 59 || second > 59) return null;
  if (match[7] && !/^\d+$/.test(match[7])) return null;

  const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const daysInMonth = [31, leapYear ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1];
  if (day < 1 || day > daysInMonth) return null;

  const offset = match[8];
  if (offset !== 'Z') {
    const offsetHour = Number(offset.slice(1, 3));
    const offsetMinute = Number(offset.slice(4, 6));
    if (offsetHour > 23 || offsetMinute > 59) return null;
  }
  const timestamp = Date.parse(text);
  return Number.isFinite(timestamp) ? new Date(timestamp).toISOString() : null;
}

function safeDate(value: unknown): string | null {
  return normalizeOpportunityFitHistoryDate(value);
}

function safeRequiredString(value: Record<string, unknown>, key: string): boolean {
  try {
    return hasOwn(value, key) && safeText(value[key]) !== null;
  } catch {
    return false;
  }
}

function safeRecommendation(value: unknown): OpportunityFitRecommendation | 'unknown' {
  return value === 'advance' || value === 'hold' || value === 'decline' ? value : 'unknown';
}

function sourceState(
  value: Record<string, unknown>,
  currentSourceFingerprint: string | undefined,
): OpportunityFitHistorySourceState {
  try {
    if (value.source_changed === true || value.source_state === 'source_changed' || value.stage_status === 'source_conflict') {
      return 'source_changed';
    }
    const fingerprint = safeText(value.source_fingerprint_sha256);
    return currentSourceFingerprint && fingerprint && fingerprint !== currentSourceFingerprint
      ? 'source_changed'
      : 'current';
  } catch {
    return 'unavailable';
  }
}

function v1Item(
  value: unknown,
  currentSourceFingerprint: string | undefined,
): OpportunityFitHistoryItem | null {
  if (!isRecord(value)) return null;
  try {
    if (value.schema_version !== 1) return null;
    const id = safePositiveInteger(value.id);
    const applicationId = safePositiveInteger(value.application_id);
    const createdAt = safeDate(value.created_at);
    const summary = isRecord(value.summary) ? safeText(value.summary.text) : null;
    const recommendation = safeRecommendation(value.recommendation);
    const status = value.status;
    if (
      !id
      || !applicationId
      || !createdAt
      || !summary
      || recommendation === 'unknown'
      || (status !== 'triage_complete' && status !== 'deep_reviewed')
      || !safeRequiredString(value, 'source_fingerprint_sha256')
    ) return null;
    const source = sourceState(value, currentSourceFingerprint);
    if (source === 'unavailable') return null;
    return Object.freeze({
      internalKey: `v1:${applicationId}:${id}`,
      createdAt,
      summary,
      sourceState: source,
      details: Object.freeze({
        recommendation,
        hasDeepReview: status === 'deep_reviewed',
        evidenceSummary: summary,
        resultState: 'ready' as const,
      }),
    });
  } catch {
    return null;
  }
}

const V2_STAGE_STATUSES = ['generating', 'provider_unknown', 'ready', 'confirmed', 'source_conflict'] as const;
type V2StageStatus = (typeof V2_STAGE_STATUSES)[number];

function isV2StageStatus(value: unknown): value is V2StageStatus {
  return V2_STAGE_STATUSES.includes(value as V2StageStatus);
}

function v2Item(
  value: unknown,
  currentSourceFingerprint: string | undefined,
): OpportunityFitHistoryItem | null {
  if (!isRecord(value)) return null;
  try {
    if (value.schema_version !== 2 || value.status !== 'active') return null;
    const rootId = safePositiveInteger(value.id);
    const reviewId = safePositiveInteger(value.review_id);
    const applicationId = safePositiveInteger(value.application_id);
    const createdAt = safeDate(value.created_at);
    // The API exposes both names for the root identity. They must agree.
    if (!rootId || !reviewId || rootId !== reviewId || !applicationId || !createdAt) return null;

    const latestStage = isRecord(value.latest_stage) ? value.latest_stage : null;
    if (!latestStage) return null;
    const stageRowId = safePositiveInteger(latestStage.id);
    const stageId = safePositiveInteger(latestStage.stage_id);
    const stageCreatedAt = safeDate(latestStage.created_at);
    const stageStatus = latestStage.stage_status;
    if (
      !stageRowId
      || !stageId
      || stageRowId !== stageId
      || latestStage.review_id !== reviewId
      || latestStage.application_id !== applicationId
      || latestStage.schema_version !== 2
      || (latestStage.stage !== 'triage' && latestStage.stage !== 'deep_review')
      || !stageCreatedAt
      || !isV2StageStatus(stageStatus)
      || !safeRequiredString(latestStage, 'source_fingerprint_sha256')
    ) return null;

    const resultState: OpportunityFitHistoryResultState = stageStatus === 'source_conflict'
      ? 'source_changed'
      : stageStatus === 'generating' || stageStatus === 'provider_unknown'
        ? 'result_unknown'
        : 'ready';
    // Pending and source-conflict stages never expose a proposal, even if a
    // malformed or stale response happens to include one. Their state is
    // projected through fixed safe copy; ready/confirmed rows require a
    // validated proposal summary.
    let proposalSummary: string | null = null;
    if (resultState === 'ready') {
      const proposalValue = hasOwn(latestStage, 'proposal') ? latestStage.proposal : undefined;
      if (!isRecord(proposalValue)) return null;
      const proposalSummaryRow = isRecord(proposalValue.summary) ? proposalValue.summary : null;
      proposalSummary = proposalSummaryRow ? safeText(proposalSummaryRow.text) : null;
      if (proposalValue.schema_version !== 2 || proposalValue.stage !== latestStage.stage || !proposalSummary) return null;
    } else if (resultState === 'source_changed') proposalSummary = '资料已更新，结果待确认';
    else proposalSummary = '结果待确认';
    const summary = proposalSummary;
    const source = sourceState(latestStage, currentSourceFingerprint);
    if (source === 'unavailable') return null;
    return Object.freeze({
      internalKey: `v2:${applicationId}:${reviewId}`,
      createdAt: stageCreatedAt,
      summary,
      sourceState: resultState === 'source_changed' ? 'source_changed' : source,
      details: Object.freeze({
        recommendation: 'unknown' as const,
        hasDeepReview: latestStage.stage === 'deep_review',
        evidenceSummary: summary,
        resultState,
      }),
    });
  } catch {
    return null;
  }
}

type ReadRowsResult = { state: 'ready' | 'unavailable'; rows: readonly unknown[] };

function readRows(source: OpportunityFitHistorySources['v1']): ReadRowsResult {
  try {
    if (Array.isArray(source)) return { state: 'ready', rows: source };
    if (isRecord(source) && source.status === 'ready' && Array.isArray(source.value)) {
      return { state: 'ready', rows: source.value };
    }
  } catch {
    return { state: 'unavailable', rows: [] };
  }
  // loading/error/absent/unknown are deliberately not represented as []:
  // callers must distinguish an unavailable route from no history.
  return { state: 'unavailable', rows: [] };
}

function compareHistoryItems(left: OpportunityFitHistoryItem, right: OpportunityFitHistoryItem): number {
  const leftTime = Date.parse(left.createdAt);
  const rightTime = Date.parse(right.createdAt);
  if (leftTime < rightTime) return 1;
  if (leftTime > rightTime) return -1;
  const leftParts = left.internalKey.split(':');
  const rightParts = right.internalKey.split(':');
  const leftSource = SOURCE_ORDINAL[leftParts[0] as 'v1' | 'v2'] ?? 0;
  const rightSource = SOURCE_ORDINAL[rightParts[0] as 'v1' | 'v2'] ?? 0;
  if (leftSource < rightSource) return 1;
  if (leftSource > rightSource) return -1;
  const leftId = Number(leftParts[2]);
  const rightId = Number(rightParts[2]);
  if (leftId < rightId) return 1;
  if (leftId > rightId) return -1;
  return left.internalKey < right.internalKey ? -1 : left.internalKey > right.internalKey ? 1 : 0;
}

const EMPTY_PROJECTION: OpportunityFitHistoryProjection = Object.freeze({
  items: Object.freeze([]) as readonly OpportunityFitHistoryItem[],
  partial: true,
  unavailableSources: Object.freeze(['v1', 'v2'] as const),
  invalidRecordCount: 1,
});

/**
 * Converts both API histories into a frozen, safe presentation model. Invalid
 * rows are omitted and a failed source is reported separately, so an error is
 * never mistaken for an empty history.
 */
export function adaptOpportunityFitHistory(sources: OpportunityFitHistorySources): OpportunityFitHistoryProjection {
  let v1: ReadRowsResult;
  let v2: ReadRowsResult;
  let currentSourceFingerprint: string | undefined;
  let applicationId: number | undefined;
  try {
    if (!isRecord(sources)) return EMPTY_PROJECTION;
    const rawApplicationId = sources.applicationId;
    if (rawApplicationId !== undefined) {
      applicationId = safePositiveInteger(rawApplicationId) ?? undefined;
      if (applicationId === undefined) return EMPTY_PROJECTION;
    }
    const rawFingerprint = sources.currentSourceFingerprint;
    if (rawFingerprint !== undefined) {
      currentSourceFingerprint = safeText(rawFingerprint) ?? undefined;
      if (currentSourceFingerprint === undefined) return EMPTY_PROJECTION;
    }
    v1 = readRows(sources.v1);
    v2 = readRows(sources.v2);
  } catch {
    return EMPTY_PROJECTION;
  }

  let invalidRecordCount = 0;
  const candidates: OpportunityFitHistoryItem[] = [];
  const append = (rows: readonly unknown[], source: 'v1' | 'v2'): boolean => {
    let index = 0;
    const sourceCandidates: OpportunityFitHistoryItem[] = [];
    try {
      for (const row of rows) {
        index += 1;
        try {
          const record = isRecord(row) ? row : null;
          if (applicationId !== undefined && (!record || record.application_id !== applicationId)) {
            invalidRecordCount += 1;
            continue;
          }
          const item = source === 'v1'
            ? v1Item(row, currentSourceFingerprint)
            : v2Item(row, currentSourceFingerprint);
          if (!item) invalidRecordCount += 1;
          else sourceCandidates.push(item);
        } catch {
          invalidRecordCount += 1;
        }
      }
      candidates.push(...sourceCandidates);
      return true;
    } catch {
      // A revoked proxy/iterator is a malformed source, not an empty route.
      // Discard rows collected before the failure and surface the route as
      // unavailable so callers never mistake a hostile partial read for a
      // complete history.
      invalidRecordCount += Math.max(1, index);
      return false;
    }
  };
  const v1ReadHealthy = append(v1.rows, 'v1');
  const v2ReadHealthy = append(v2.rows, 'v2');

  const byKey = new Map<string, OpportunityFitHistoryItem[]>();
  for (const item of candidates) {
    const group = byKey.get(item.internalKey) ?? [];
    group.push(item);
    byKey.set(item.internalKey, group);
  }
  const items: OpportunityFitHistoryItem[] = [];
  for (const group of byKey.values()) {
    const first = group[0];
    const conflict = group.some((item) => (
      item.createdAt !== first.createdAt
      || item.summary !== first.summary
      || item.sourceState !== first.sourceState
      || item.details.recommendation !== first.details.recommendation
      || item.details.hasDeepReview !== first.details.hasDeepReview
      || item.details.evidenceSummary !== first.details.evidenceSummary
      || item.details.resultState !== first.details.resultState
    ));
    if (conflict) {
      invalidRecordCount += group.length;
      continue;
    }
    items.push(first);
  }
  items.sort(compareHistoryItems);
  const unavailableSources = [
    ...(v1.state === 'unavailable' || !v1ReadHealthy ? ['v1' as const] : []),
    ...(v2.state === 'unavailable' || !v2ReadHealthy ? ['v2' as const] : []),
  ];
  return Object.freeze({
    items: Object.freeze(items),
    partial: unavailableSources.length > 0,
    unavailableSources: Object.freeze(unavailableSources),
    invalidRecordCount,
  });
}

/** Convenience adapter for callers that only need the immutable timeline. */
export function adaptOpportunityFitHistoryItems(sources: OpportunityFitHistorySources): readonly OpportunityFitHistoryItem[] {
  return adaptOpportunityFitHistory(sources).items;
}

export function isOpportunityFitHistorySource<T>(value: unknown): value is OpportunityFitHistoryRead<T> {
  try {
    return isRecord(value)
      && (value.status === 'ready' || value.status === 'loading' || value.status === 'error' || value.status === 'absent');
  } catch {
    return false;
  }
}

// Keep the imported API types visible to consumers that use the adapter as a
// boundary; runtime validation intentionally does not trust those compile-time
// declarations.
export type OpportunityFitHistoryV1Row = OpportunityFitReviewSummary;
export type OpportunityFitHistoryV2Row = OpportunityFitV2SessionSummary;
