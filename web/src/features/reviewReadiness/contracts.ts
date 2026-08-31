export type ProductActionName = 'save_review_readiness_signal' | 'confirm_interview_story';
export type ProductActionDecision = 'approve' | 'modify' | 'reject';
export type ProductActionTerminalStatus = 'rejected' | 'committed' | 'failed';
export type ProductActionStatus = 'proposed' | ProductActionTerminalStatus | 'already_confirmed';

export interface ReadinessEvidence {
  ordinal: number;
  source_path: '/questions' | '/self_reflection' | '/difficulty_points' | '/mood';
  excerpt: string;
  excerpt_sha256: string;
  source_field_sha256: string;
}

export interface ReviewReadinessCandidate {
  application_id: number;
  event_id: number;
  note_id: number;
  proposal_id: number;
  proposal_schema_version: 2;
  focus_id: string;
  statement: string;
  source_note_revision: number;
  source_note_fingerprint: string;
  source_proposal_hash: string;
  candidate_fingerprint: string;
  evidence: ReadinessEvidence[];
}

export type ReviewReadinessCandidateState =
  | 'ready'
  | 'already_confirmed'
  | 'legacy_requires_regeneration'
  | 'source_changed'
  | 'source_missing'
  | 'not_eligible'
  | 'unavailable';

export interface ReviewReadinessCandidatesResponse {
  schema_version: 1;
  state: ReviewReadinessCandidateState;
  note_id: number;
  proposal_id: number;
  candidates: ReviewReadinessCandidate[];
}

export interface ProposeReadinessActionRequest {
  proposal_id: number;
  focus_id: string;
  expected_note_revision: number;
  expected_candidate_fingerprint: string;
  idempotency_key: string;
  user_note: string;
}

export interface ProductActionProposalResponse {
  schema_version: 1;
  operation_id: string | null;
  action_call_id: string | null;
  action_name: ProductActionName;
  status: ProductActionStatus;
  created: boolean;
  replayed: boolean;
  confirmation_token?: string;
  result?: Record<string, unknown>;
}

export type ProductActionDecisionRequest =
  | { confirmation_token: string; decision: 'approve' | 'reject' }
  | { confirmation_token: string; decision: 'modify'; edited_payload: Record<string, unknown> };

export interface ProductActionDecisionResponse {
  schema_version: 1;
  operation_id: string;
  action_name: ProductActionName;
  status: ProductActionTerminalStatus;
  result: Record<string, unknown>;
  replayed: boolean;
  direct_commit: boolean;
}

export interface ProductActionStateResponse {
  schema_version: 1;
  operation_id: string;
  action_name: ProductActionName;
  status: 'proposed' | ProductActionTerminalStatus;
  result?: Record<string, unknown>;
}

export interface ProductActionRecoveryResponse {
  schema_version: 1;
  operation_id: string;
  action_call_id: string;
  action_name: ProductActionName;
  status: 'proposed';
  confirmation_token: string;
  allowed_decisions: ProductActionDecision[];
  rejection_only: boolean;
  live_source_state: 'current' | 'not_observed';
}

export type ProductActionCompensationKind = 'undo:save_review_readiness_signal' | 'undo:confirm_interview_story';

export interface ProductActionCompensationResponse<Kind extends ProductActionCompensationKind = ProductActionCompensationKind> {
  schema_version: 1;
  operation_id: string;
  compensation_kind: Kind;
  status: 'committed' | 'failed';
  result: Record<string, unknown>;
  replayed: boolean;
}

export type ReviewReadinessSignalCompensationResponse = ProductActionCompensationResponse<'undo:save_review_readiness_signal'>;
export type InterviewStoryCompensationResponse = ProductActionCompensationResponse<'undo:confirm_interview_story'>;

export type ReadinessAdvisoryState = 'available' | 'practiced' | 'stale_source' | 'retracted' | 'unavailable';
export type ReadinessPracticeState = 'not_started' | 'in_progress' | 'completed' | 'legacy_only';

export interface ReadinessFeedbackItem {
  signalId: number;
  versionId: number;
  practiceSourceFingerprint: string;
  practiceTargetFingerprint: string;
  state: ReadinessAdvisoryState;
  practiceState: ReadinessPracticeState;
  selected: boolean;
  title: string;
  sourceLabel: string;
}

export interface EventReadinessFeedbackResponse {
  schema_version: 1;
  application_id: number;
  event_id: number;
  items: ReadinessFeedbackItem[];
}

export interface ReadinessPracticeFocus extends ReadinessFeedbackItem {
  schema_version: 1;
  targetEventId: number;
}

export interface ReadinessPracticeLaunch {
  ownerGeneration: number;
  signalVersionId: number;
  targetEventId: number;
}

export interface ProductActionOwnerDraft {
  ownerKey: string;
  operationId: string;
  actionCallId: string;
  actionName: ProductActionName;
  confirmationToken: string | null;
  allowedDecisions: ProductActionDecision[];
  status: 'proposed' | ProductActionTerminalStatus;
  result: Record<string, unknown> | null;
  originalPayload: Record<string, unknown>;
  pendingDecision: Omit<ProductActionDecisionRequest, 'confirmation_token'> | null;
  resultUnknown: boolean;
  undoStatus?: ProductActionCompensationResponse['status'] | null;
  undoReplayed?: boolean;
  undoRequest?: ProductActionUndoRequest | null;
  undoResultUnknown?: boolean;
}

export interface ProductActionUndoRequest {
  ownerKey: string;
  originOwnerKey: string;
  parentOperationId: string;
  actionName: ProductActionName;
}

export interface ReviewReadinessOwnerDraft {
  ownerKey: string;
  ownerGeneration: number;
  noteId: number;
  proposalId: number;
  applicationId: number | null;
  selectedFocusId: string | null;
  userNote: string;
  idempotencyKey: string | null;
  frozenProposalInput: ProposeReadinessActionRequest | null;
  proposalUnknown: boolean;
  actionDraft: ProductActionOwnerDraft | null;
}

export function isReviewReadinessDraftPending(draft: ReviewReadinessOwnerDraft): boolean {
  return draft.proposalUnknown
    || draft.actionDraft?.status === 'proposed'
    || Boolean(draft.actionDraft?.resultUnknown)
    || Boolean(draft.actionDraft?.undoRequest)
    || Boolean(draft.actionDraft?.undoResultUnknown);
}

export function isReviewReadinessDraftUnsaved(draft: ReviewReadinessOwnerDraft): boolean {
  if (draft.actionDraft && draft.actionDraft.status !== 'proposed') return false;
  return Boolean(draft.selectedFocusId || draft.userNote || draft.frozenProposalInput);
}

export function reviewReadinessOwnerKey(draft: Pick<ReviewReadinessOwnerDraft, 'ownerGeneration' | 'noteId' | 'proposalId'>): string {
  return `review:${draft.ownerGeneration}:${draft.noteId}:${draft.proposalId}`;
}

const SAFE_TERMINAL_DRAFT_KEYS = [
  'actionDraft', 'applicationId', 'frozenProposalInput', 'idempotencyKey', 'noteId',
  'ownerGeneration', 'ownerKey', 'proposalId', 'proposalUnknown', 'selectedFocusId', 'userNote',
] as const;
const SAFE_TERMINAL_ACTION_KEYS = [
  'actionCallId', 'actionName', 'allowedDecisions', 'confirmationToken', 'operationId',
  'originalPayload', 'ownerKey', 'pendingDecision', 'result', 'resultUnknown', 'status',
  'undoReplayed', 'undoRequest', 'undoResultUnknown', 'undoStatus',
] as const;
const SAFE_UNDO_REQUEST_KEYS = ['actionName', 'originOwnerKey', 'ownerKey', 'parentOperationId'] as const;

function hasExactKeys(value: object, expected: readonly string[]): boolean {
  const actual = Object.keys(value).sort();
  const sortedExpected = [...expected].sort();
  return actual.length === sortedExpected.length
    && actual.every((key, index) => key === sortedExpected[index]);
}

export function isSafeReviewReadinessTerminalDraft(draft: ReviewReadinessOwnerDraft): boolean {
  const action = draft.actionDraft;
  if (
    !hasExactKeys(draft, SAFE_TERMINAL_DRAFT_KEYS)
    || draft.ownerKey !== reviewReadinessOwnerKey(draft)
    || !Number.isSafeInteger(draft.applicationId)
    || Number(draft.applicationId) <= 0
    || draft.selectedFocusId !== null
    || draft.userNote !== ''
    || draft.idempotencyKey !== null
    || draft.frozenProposalInput !== null
    || draft.proposalUnknown
    || !action
    || !hasExactKeys(action, SAFE_TERMINAL_ACTION_KEYS)
    || action.ownerKey !== `${draft.ownerKey}:terminal`
    || action.actionName !== 'save_review_readiness_signal'
    || action.confirmationToken !== null
    || action.allowedDecisions.length !== 0
    || action.actionCallId !== ''
    || Object.keys(action.originalPayload).length !== 0
    || action.pendingDecision !== null
    || action.resultUnknown
    || !['committed', 'rejected', 'failed'].includes(action.status)
    || !action.operationId
  ) return false;
  const undoRequest = action.undoRequest ?? null;
  if (undoRequest && (
    !hasExactKeys(undoRequest, SAFE_UNDO_REQUEST_KEYS)
    || action.status !== 'committed'
    || undoRequest.ownerKey !== action.ownerKey
    || typeof undoRequest.originOwnerKey !== 'string'
    || undoRequest.originOwnerKey.length === 0
    || undoRequest.parentOperationId !== action.operationId
    || undoRequest.actionName !== action.actionName
    || action.undoStatus != null
  )) return false;
  if (action.undoResultUnknown && !undoRequest) return false;
  if (action.undoStatus != null && (undoRequest !== null || action.undoResultUnknown)) return false;
  const resultKeys = Object.keys(action.result ?? {});
  if (resultKeys.some((key) => key !== 'signal_id' && key !== 'signal_version_id')) return false;
  return action.status !== 'committed' || committedSignalIdentity(action.result) !== null;
}

export function sanitizeReviewReadinessTerminalDraft(draft: ReviewReadinessOwnerDraft): ReviewReadinessOwnerDraft {
  const action = draft.actionDraft;
  const identity = action?.status === 'committed' ? committedSignalIdentity(action.result) : null;
  const preserveUndoRequest = Boolean(
    action?.status === 'committed'
    && action.undoStatus == null
    && action.undoRequest
    && action.undoRequest.ownerKey === action.ownerKey
    && action.undoRequest.parentOperationId === action.operationId
    && action.undoRequest.actionName === action.actionName,
  );
  return {
    ownerKey: draft.ownerKey,
    ownerGeneration: draft.ownerGeneration,
    noteId: draft.noteId,
    proposalId: draft.proposalId,
    applicationId: draft.applicationId,
    selectedFocusId: null,
    userNote: '',
    idempotencyKey: null,
    frozenProposalInput: null,
    proposalUnknown: false,
    actionDraft: action ? {
      ownerKey: `${draft.ownerKey}:terminal`,
      operationId: action.operationId,
      actionCallId: '',
      actionName: 'save_review_readiness_signal',
      confirmationToken: null,
      allowedDecisions: [],
      status: action.status,
      result: identity ? { signal_id: identity.signalId, signal_version_id: identity.versionId } : {},
      originalPayload: {},
      pendingDecision: null,
      resultUnknown: false,
      undoStatus: action.undoStatus ?? null,
      undoReplayed: action.undoReplayed ?? false,
      undoRequest: preserveUndoRequest && action.undoRequest
        ? {
            ownerKey: `${draft.ownerKey}:terminal`,
            originOwnerKey: action.undoRequest.originOwnerKey,
            parentOperationId: action.operationId,
            actionName: 'save_review_readiness_signal',
          }
        : null,
      undoResultUnknown: preserveUndoRequest ? action.undoResultUnknown ?? false : false,
    } : null,
  };
}

export function selectReviewReadinessOwnerDraft(
  drafts: Readonly<Record<string, ReviewReadinessOwnerDraft>>,
  scope: {
    ownerGeneration: number;
    recoveryOwnerGeneration?: number | null;
    noteId: number;
    proposalId: number;
    applicationId: number;
  },
): ReviewReadinessOwnerDraft | null {
  const currentKey = `review:${scope.ownerGeneration}:${scope.noteId}:${scope.proposalId}`;
  const current = drafts[currentKey];
  if (current?.applicationId === scope.applicationId) return current;
  const recoveryKey = scope.recoveryOwnerGeneration == null
    ? null
    : `review:${scope.recoveryOwnerGeneration}:${scope.noteId}:${scope.proposalId}`;
  const recovery = recoveryKey ? drafts[recoveryKey] : undefined;
  if (recovery?.applicationId === scope.applicationId && (isReviewReadinessDraftPending(recovery) || isReviewReadinessDraftUnsaved(recovery))) {
    return recovery;
  }
  return Object.values(drafts)
    .filter((candidate) => candidate.noteId === scope.noteId
      && candidate.proposalId === scope.proposalId
      && candidate.applicationId === scope.applicationId
      && isSafeReviewReadinessTerminalDraft(candidate))
    .sort((left, right) => right.ownerGeneration - left.ownerGeneration)[0] ?? null;
}

export function recoverReviewReadinessOwnerDraft(
  draft: ReviewReadinessOwnerDraft | null | undefined,
  scope: {
    ownerGeneration: number;
    recoveryOwnerGeneration?: number | null;
    noteId: number;
    proposalId: number;
    applicationId?: number;
  },
): ReviewReadinessOwnerDraft | null {
  const currentOwnerKey = `review:${scope.ownerGeneration}:${scope.noteId}:${scope.proposalId}`;
  if (draft?.ownerKey === currentOwnerKey
    && draft.ownerGeneration === scope.ownerGeneration
    && (scope.applicationId === undefined || draft.applicationId === scope.applicationId)) return draft;
  if (
    draft
    && scope.applicationId !== undefined
    && draft.applicationId === scope.applicationId
    && draft.noteId === scope.noteId
    && draft.proposalId === scope.proposalId
    && isSafeReviewReadinessTerminalDraft(draft)
  ) {
    return {
      ...draft,
      ownerKey: currentOwnerKey,
      ownerGeneration: scope.ownerGeneration,
      actionDraft: draft.actionDraft
        ? {
            ...draft.actionDraft,
            ownerKey: `${currentOwnerKey}:terminal`,
            undoRequest: draft.actionDraft.undoRequest
              ? { ...draft.actionDraft.undoRequest, ownerKey: `${currentOwnerKey}:terminal` }
              : null,
          }
        : null,
    };
  }
  const recoveryGeneration = scope.recoveryOwnerGeneration;
  const recoveryOwnerKey = recoveryGeneration == null
    ? null
    : `review:${recoveryGeneration}:${scope.noteId}:${scope.proposalId}`;
  if (
    !draft
    || recoveryOwnerKey === null
    || draft.ownerGeneration !== recoveryGeneration
    || draft.ownerKey !== recoveryOwnerKey
    || draft.noteId !== scope.noteId
    || draft.proposalId !== scope.proposalId
    || (!isReviewReadinessDraftPending(draft) && !isReviewReadinessDraftUnsaved(draft))
  ) return null;
  if (draft.proposalUnknown && !draft.frozenProposalInput) return null;
  if (draft.actionDraft) {
    const expectedActionOwnerKey = `${recoveryOwnerKey}:${draft.selectedFocusId ?? ''}`;
    if (
      draft.actionDraft.status !== 'proposed'
      || draft.actionDraft.ownerKey !== expectedActionOwnerKey
      || draft.actionDraft.actionName !== 'save_review_readiness_signal'
    ) return null;
  }
  return {
    ...draft,
    ownerKey: currentOwnerKey,
    ownerGeneration: scope.ownerGeneration,
    actionDraft: draft.actionDraft
      ? { ...draft.actionDraft, ownerKey: `${currentOwnerKey}:${draft.selectedFocusId}` }
      : null,
  };
}

export function isSafeReadinessFeedbackItem(item: ReadinessFeedbackItem): boolean {
  return item.state === 'available' || item.state === 'practiced';
}

export function committedSignalIdentity(result: Record<string, unknown> | null): {
  signalId: number;
  versionId: number;
} | null {
  const signalId = result?.signal_id;
  const versionId = result?.signal_version_id;
  return Number.isSafeInteger(signalId) && Number(signalId) > 0
    && Number.isSafeInteger(versionId) && Number(versionId) > 0
    ? { signalId: Number(signalId), versionId: Number(versionId) }
    : null;
}
