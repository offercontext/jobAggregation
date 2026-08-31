import axios from 'axios';
import { createApiClient } from '@/services/http';
import type {
  EventReadinessFeedbackResponse,
  InterviewStoryCompensationResponse,
  ProductActionCompensationKind,
  ProductActionCompensationResponse,
  ProductActionDecisionRequest,
  ProductActionDecisionResponse,
  ProductActionName,
  ProductActionProposalResponse,
  ProductActionRecoveryResponse,
  ProductActionStateResponse,
  ProposeReadinessActionRequest,
  ReadinessFeedbackItem,
  ReadinessPracticeFocus,
  ReviewReadinessSignalCompensationResponse,
  ReviewReadinessCandidate,
  ReviewReadinessCandidatesResponse,
} from './contracts';

const http = createApiClient({ baseURL: '/api', timeout: 15000 });

export class ReviewReadinessServiceError extends Error {
  constructor(public readonly code: string, public readonly status = 0) {
    super(code);
    this.name = 'ReviewReadinessServiceError';
  }
}

function invalidResponse(): never {
  throw new ReviewReadinessServiceError('review_readiness_invalid_response');
}

function safeError(error: unknown): ReviewReadinessServiceError {
  if (error instanceof ReviewReadinessServiceError) return error;
  const response = axios.isAxiosError(error)
    ? error.response
    : (error as { response?: { status?: number; data?: unknown } } | null)?.response;
  const data = response?.data as { error_code?: unknown } | undefined;
  return new ReviewReadinessServiceError(
    typeof data?.error_code === 'string' ? data.error_code : 'review_readiness_unavailable',
    response?.status ?? 0,
  );
}

function object(value: unknown): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) invalidResponse();
  return value as Record<string, unknown>;
}

function exact(value: unknown, required: readonly string[], optional: readonly string[] = []): Record<string, unknown> {
  const record = object(value);
  const keys = Object.keys(record).sort();
  const allowed = [...required, ...optional];
  if (required.some((key) => !(key in record)) || keys.some((key) => !allowed.includes(key))) invalidResponse();
  return record;
}

function literal<T extends string | number>(value: unknown, allowed: readonly T[]): T {
  if (!allowed.includes(value as T)) invalidResponse();
  return value as T;
}

function string(value: unknown): string {
  if (typeof value !== 'string') invalidResponse();
  return value;
}

function positiveInt(value: unknown): number {
  if (!Number.isSafeInteger(value) || Number(value) < 1) invalidResponse();
  return Number(value);
}

function nonNegativeInt(value: unknown): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) invalidResponse();
  return value;
}

function bool(value: unknown): boolean {
  if (typeof value !== 'boolean') invalidResponse();
  return value;
}

function parseEvidence(value: unknown) {
  const item = exact(value, ['ordinal', 'source_path', 'excerpt', 'excerpt_sha256', 'source_field_sha256']);
  return {
    ordinal: nonNegativeInt(item.ordinal),
    source_path: literal(item.source_path, ['/questions', '/self_reflection', '/difficulty_points', '/mood'] as const),
    excerpt: string(item.excerpt),
    excerpt_sha256: string(item.excerpt_sha256),
    source_field_sha256: string(item.source_field_sha256),
  };
}

function parseCandidate(value: unknown): ReviewReadinessCandidate {
  const item = exact(value, [
    'application_id', 'event_id', 'note_id', 'proposal_id', 'proposal_schema_version',
    'focus_id', 'statement', 'source_note_revision', 'source_note_fingerprint',
    'source_proposal_hash', 'candidate_fingerprint', 'evidence',
  ]);
  if (!Array.isArray(item.evidence)) invalidResponse();
  return {
    application_id: positiveInt(item.application_id),
    event_id: positiveInt(item.event_id),
    note_id: positiveInt(item.note_id),
    proposal_id: positiveInt(item.proposal_id),
    proposal_schema_version: literal(item.proposal_schema_version, [2] as const),
    focus_id: string(item.focus_id),
    statement: string(item.statement),
    source_note_revision: positiveInt(item.source_note_revision),
    source_note_fingerprint: string(item.source_note_fingerprint),
    source_proposal_hash: string(item.source_proposal_hash),
    candidate_fingerprint: string(item.candidate_fingerprint),
    evidence: item.evidence.map(parseEvidence),
  };
}

function parseCandidates(value: unknown): ReviewReadinessCandidatesResponse {
  const response = exact(value, ['schema_version', 'state', 'note_id', 'proposal_id', 'candidates']);
  if (!Array.isArray(response.candidates)) invalidResponse();
  return {
    schema_version: literal(response.schema_version, [1] as const),
    state: literal(response.state, ['ready', 'already_confirmed', 'legacy_requires_regeneration', 'source_changed', 'source_missing', 'not_eligible', 'unavailable'] as const),
    note_id: positiveInt(response.note_id),
    proposal_id: positiveInt(response.proposal_id),
    candidates: response.candidates.map(parseCandidate),
  };
}

function parseProposal(value: unknown): ProductActionProposalResponse {
  const response = exact(value, ['schema_version', 'operation_id', 'action_call_id', 'action_name', 'status', 'created', 'replayed'], ['confirmation_token', 'result']);
  const operationId = response.operation_id === null ? null : string(response.operation_id);
  const actionCallId = response.action_call_id === null ? null : string(response.action_call_id);
  const status = literal(response.status, ['proposed', 'rejected', 'committed', 'failed', 'already_confirmed'] as const);
  if (status === 'proposed') {
    if (!operationId || !actionCallId || typeof response.confirmation_token !== 'string' || response.confirmation_token.length === 0) invalidResponse();
  } else if (response.confirmation_token !== undefined) invalidResponse();
  return {
    schema_version: literal(response.schema_version, [1] as const),
    operation_id: operationId,
    action_call_id: actionCallId,
    action_name: literal(response.action_name, ['save_review_readiness_signal', 'confirm_interview_story'] as const),
    status,
    created: bool(response.created),
    replayed: bool(response.replayed),
    ...(response.confirmation_token === undefined ? {} : { confirmation_token: string(response.confirmation_token) }),
    ...(response.result === undefined ? {} : { result: object(response.result) }),
  };
}

function parseDecision(value: unknown): ProductActionDecisionResponse {
  const response = exact(value, ['schema_version', 'operation_id', 'action_name', 'status', 'result', 'replayed', 'direct_commit']);
  return {
    schema_version: literal(response.schema_version, [1] as const),
    operation_id: string(response.operation_id),
    action_name: literal(response.action_name, ['save_review_readiness_signal', 'confirm_interview_story'] as const),
    status: literal(response.status, ['rejected', 'committed', 'failed'] as const),
    result: object(response.result),
    replayed: bool(response.replayed),
    direct_commit: bool(response.direct_commit),
  };
}

function parseState(value: unknown): ProductActionStateResponse {
  const response = exact(value, ['schema_version', 'operation_id', 'action_name', 'status'], ['result']);
  return {
    schema_version: literal(response.schema_version, [1] as const),
    operation_id: string(response.operation_id),
    action_name: literal(response.action_name, ['save_review_readiness_signal', 'confirm_interview_story'] as const),
    status: literal(response.status, ['proposed', 'rejected', 'committed', 'failed'] as const),
    ...(response.result === undefined ? {} : { result: object(response.result) }),
  };
}

function parseRecovery(value: unknown): ProductActionRecoveryResponse {
  const response = exact(value, ['schema_version', 'operation_id', 'action_call_id', 'action_name', 'status', 'confirmation_token', 'allowed_decisions', 'rejection_only', 'live_source_state']);
  if (!Array.isArray(response.allowed_decisions)) invalidResponse();
  return {
    schema_version: literal(response.schema_version, [1] as const),
    operation_id: string(response.operation_id),
    action_call_id: string(response.action_call_id),
    action_name: literal(response.action_name, ['save_review_readiness_signal', 'confirm_interview_story'] as const),
    status: literal(response.status, ['proposed'] as const),
    confirmation_token: string(response.confirmation_token),
    allowed_decisions: response.allowed_decisions.map((item) => literal(item, ['approve', 'modify', 'reject'] as const)),
    rejection_only: bool(response.rejection_only),
    live_source_state: literal(response.live_source_state, ['current', 'not_observed'] as const),
  };
}

function parseFeedbackItem(value: unknown): ReadinessFeedbackItem {
  const item = exact(value, ['signalId', 'versionId', 'practiceSourceFingerprint', 'practiceTargetFingerprint', 'state', 'practiceState', 'selected', 'title', 'sourceLabel']);
  return {
    signalId: positiveInt(item.signalId),
    versionId: positiveInt(item.versionId),
    practiceSourceFingerprint: string(item.practiceSourceFingerprint),
    practiceTargetFingerprint: string(item.practiceTargetFingerprint),
    state: literal(item.state, ['available', 'practiced', 'stale_source', 'retracted', 'unavailable'] as const),
    practiceState: literal(item.practiceState, ['not_started', 'in_progress', 'completed', 'legacy_only'] as const),
    selected: bool(item.selected),
    title: string(item.title),
    sourceLabel: string(item.sourceLabel),
  };
}

function parseEventFeedback(value: unknown): EventReadinessFeedbackResponse {
  const response = exact(value, ['schema_version', 'application_id', 'event_id', 'items']);
  if (!Array.isArray(response.items)) invalidResponse();
  return {
    schema_version: literal(response.schema_version, [1] as const),
    application_id: positiveInt(response.application_id),
    event_id: positiveInt(response.event_id),
    items: response.items.map(parseFeedbackItem),
  };
}

function parseFocus(value: unknown): ReadinessPracticeFocus {
  const response = exact(value, ['schema_version', 'signalId', 'versionId', 'targetEventId', 'practiceSourceFingerprint', 'practiceTargetFingerprint', 'state', 'practiceState', 'selected', 'title', 'sourceLabel']);
  const item = parseFeedbackItem(Object.fromEntries(Object.entries(response).filter(([key]) => !['schema_version', 'targetEventId'].includes(key))));
  return { schema_version: literal(response.schema_version, [1] as const), ...item, targetEventId: positiveInt(response.targetEventId) };
}

function parseCompensation<Kind extends ProductActionCompensationKind>(
  value: unknown,
  expectedKind: Kind,
): ProductActionCompensationResponse<Kind> {
  const response = exact(value, ['schema_version', 'operation_id', 'compensation_kind', 'status', 'result', 'replayed']);
  return {
    schema_version: literal(response.schema_version, [1] as const),
    operation_id: string(response.operation_id),
    compensation_kind: literal(response.compensation_kind, [expectedKind] as const),
    status: literal(response.status, ['committed', 'failed'] as const),
    result: object(response.result),
    replayed: bool(response.replayed),
  };
}

async function request<T>(operation: () => Promise<{ data: unknown }>, parse: (value: unknown) => T): Promise<T> {
  try {
    return parse((await operation()).data);
  } catch (error) {
    throw safeError(error);
  }
}

export function getReviewReadinessCandidates(noteId: number, proposalId: number): Promise<ReviewReadinessCandidatesResponse> {
  return request(() => http.get(`/interview-notes/${noteId}/readiness-feedback-candidates`, { params: { proposal_id: proposalId } }), parseCandidates);
}

export function proposeReviewReadinessAction(noteId: number, input: ProposeReadinessActionRequest): Promise<ProductActionProposalResponse> {
  return request(() => http.post(`/interview-notes/${noteId}/readiness-focus-actions`, input), parseProposal);
}

export function decideProductAction(operationId: string, input: ProductActionDecisionRequest): Promise<ProductActionDecisionResponse> {
  return request(() => http.post(`/product-actions/${operationId}/decisions`, input), parseDecision);
}

export function getProductActionState(operationId: string): Promise<ProductActionStateResponse> {
  return request(() => http.get(`/product-actions/${operationId}`), parseState);
}

export function recoverSignalOwnerAction(noteId: number, operationId: string): Promise<ProductActionRecoveryResponse> {
  return request(() => http.get(`/interview-notes/${noteId}/readiness-focus-actions/${operationId}`), parseRecovery);
}

export function recoverRejectionControl(applicationId: number, operationId: string): Promise<ProductActionRecoveryResponse> {
  return request(() => http.get(`/applications/${applicationId}/product-actions/${operationId}/rejection-control`), parseRecovery);
}

export function getEventReadinessFeedback(applicationId: number, eventId: number): Promise<EventReadinessFeedbackResponse> {
  return request(() => http.get(`/applications/${applicationId}/events/${eventId}/readiness-feedback`), parseEventFeedback);
}

export function getReadinessPracticeFocus(versionId: number, targetEventId: number): Promise<ReadinessPracticeFocus> {
  return request(() => http.get(`/interview-practice/focus/${versionId}`, { params: { target_event_id: targetEventId } }), parseFocus);
}

export function undoReadinessSignal(applicationId: number, signalId: number, parentOperationId: string): Promise<ReviewReadinessSignalCompensationResponse> {
  return request(
    () => http.post(`/applications/${applicationId}/readiness-signals/${signalId}/undo`, { parent_operation_id: parentOperationId }),
    (value) => parseCompensation(value, 'undo:save_review_readiness_signal'),
  );
}

export function undoInterviewStory(storyId: number, parentOperationId: string): Promise<InterviewStoryCompensationResponse> {
  return request(
    () => http.post(`/interview-stories/${storyId}/product-action-undo`, { parent_operation_id: parentOperationId }),
    (value) => parseCompensation(value, 'undo:confirm_interview_story'),
  );
}

export function assertProductActionName(value: string): ProductActionName {
  return literal(value, ['save_review_readiness_signal', 'confirm_interview_story'] as const);
}
