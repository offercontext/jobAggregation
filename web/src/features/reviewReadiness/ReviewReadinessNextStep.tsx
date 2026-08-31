import { useEffect, useMemo, useRef, useState } from 'react';
import type { InterviewReviewProposal } from '@/types/interviewReviewProposal';
import { committedSignalIdentity, recoverReviewReadinessOwnerDraft, sanitizeReviewReadinessTerminalDraft, type ProductActionOwnerDraft, type ProductActionProposalResponse, type ProductActionUndoRequest, type ReviewReadinessCandidate, type ReviewReadinessOwnerDraft } from './contracts';
import { ProductActionConfirmation, productActionDraftFromProposal } from './ProductActionConfirmation';
import { getReviewReadinessCandidates, proposeReviewReadinessAction, recoverRejectionControl, recoverSignalOwnerAction, undoReadinessSignal } from './service';
import styles from './reviewReadiness.module.css';

interface ReviewReadinessNextStepProps {
  noteId: number;
  proposal: InterviewReviewProposal;
  applicationId?: number;
  ownerBlocked?: boolean;
  ownerGeneration?: number;
  recoveryOwnerGeneration?: number | null;
  draft?: ReviewReadinessOwnerDraft | null;
  onDraftChange?: (
    draft: ReviewReadinessOwnerDraft | null,
    transaction?: { readonly ownerKey: string; readonly retireOwnerKey?: string; readonly undoRequest?: ProductActionUndoRequest },
  ) => boolean | void;
  onOpenStory?: (noteId: number, focusId: string) => void;
  onSignalCommitted?: (identity: { applicationId: number; signalId: number; versionId: number }) => void;
}

function stateMessage(state: string): string {
  if (state === 'already_confirmed') return '这个准备重点已经保存，可在目标面试的准备页查看。';
  if (state === 'legacy_requires_regeneration') return '旧版复盘建议需要重新生成后才能保存准备重点。';
  if (state === 'source_changed') return '复盘来源已变化，请重新生成建议。';
  if (state === 'source_missing') return '复盘来源已不可见，不能创建新的准备重点。';
  if (state === 'not_eligible') return '当前复盘没有可安全保存的准备重点。';
  return '准备重点暂时不可用，请稍后重试。';
}

function terminalDraft(
  ownerKey: string,
  response: ProductActionProposalResponse,
  originalPayload: Record<string, unknown>,
): ProductActionOwnerDraft | null {
  if (
    response.action_name !== 'save_review_readiness_signal'
    || !response.operation_id
    || !['committed', 'rejected', 'failed', 'already_confirmed'].includes(response.status)
  ) return null;
  return {
    ownerKey,
    operationId: response.operation_id,
    actionCallId: response.action_call_id ?? '',
    actionName: response.action_name,
    confirmationToken: null,
    allowedDecisions: [],
    status: response.status === 'already_confirmed' ? 'committed' : response.status,
    result: response.result ?? null,
    originalPayload,
    pendingDecision: null,
    resultUnknown: false,
    undoStatus: null,
    undoReplayed: false,
    undoRequest: null,
    undoResultUnknown: false,
  };
}

function safeV2Proposal(proposal: InterviewReviewProposal, ownerBlocked: boolean): boolean {
  return !ownerBlocked
    && proposal.proposal_schema_version === 2
    && proposal.source_status === 'current'
    && proposal.source_note_revision !== null
    && proposal.note_id !== null;
}

function structurallyRecoverableV2Proposal(proposal: InterviewReviewProposal, ownerBlocked: boolean): boolean {
  return !ownerBlocked
    && proposal.proposal_schema_version === 2
    && proposal.source_note_revision !== null
    && proposal.note_id !== null;
}

function hasRejectionOnlyControl(draft: ProductActionOwnerDraft): boolean {
  return draft.confirmationToken !== null
    && draft.allowedDecisions.length === 1
    && draft.allowedDecisions[0] === 'reject';
}

function emptyOwnerDraft(ownerKey: string, ownerGeneration: number, noteId: number, proposalId: number): ReviewReadinessOwnerDraft {
  return {
    ownerKey,
    ownerGeneration,
    noteId,
    proposalId,
    applicationId: null,
    selectedFocusId: null,
    userNote: '',
    idempotencyKey: null,
    frozenProposalInput: null,
    proposalUnknown: false,
    actionDraft: null,
  };
}

export function ReviewReadinessNextStep({
  noteId,
  proposal,
  applicationId,
  ownerBlocked = false,
  ownerGeneration = 0,
  recoveryOwnerGeneration,
  draft,
  onDraftChange,
  onOpenStory,
  onSignalCommitted,
}: ReviewReadinessNextStepProps) {
  const eligible = safeV2Proposal(proposal, ownerBlocked);
  const ownerKey = `review:${ownerGeneration}:${noteId}:${proposal.id}`;
  const recoveredDraft = useMemo(() => recoverReviewReadinessOwnerDraft(draft, {
    ownerGeneration, recoveryOwnerGeneration, noteId, proposalId: proposal.id, applicationId,
  }), [applicationId, draft, noteId, ownerGeneration, proposal.id, recoveryOwnerGeneration]);
  const [candidates, setCandidates] = useState<ReviewReadinessCandidate[] | null>(null);
  const [projectionState, setProjectionState] = useState<string>('');
  const [ownerDraft, setOwnerDraft] = useState<ReviewReadinessOwnerDraft>(() => (
    recoveredDraft
      ? recoveredDraft
      : emptyOwnerDraft(ownerKey, ownerGeneration, noteId, proposal.id)
  ));
  const [loading, setLoading] = useState(false);
  const [proposing, setProposing] = useState(false);
  const [error, setError] = useState('');
  const proposalInFlight = useRef(false);
  const rejectionRecoveryInFlight = useRef<string | null>(null);
  const transactionalParent = useRef(false);
  const recoveryRetireOwnerKey = recoveredDraft && recoveredDraft !== draft ? draft?.ownerKey : undefined;
  const ownerScopeRef = useRef(ownerKey);
  ownerScopeRef.current = ownerKey;
  const readGeneration = useRef(0);
  const previousOwnerKey = useRef(ownerKey);

  useEffect(() => {
    if (!recoveredDraft || recoveredDraft === draft) return;
    try {
      const installed = onDraftChange?.(recoveredDraft, {
        ownerKey: recoveredDraft.ownerKey,
        ...(draft?.ownerKey ? { retireOwnerKey: draft.ownerKey } : {}),
      });
      if (installed === true) transactionalParent.current = true;
    } catch {
      // Atomic parent installation failed. The exact old unknown remains the
      // only persisted recovery authority and can be retried on a later mount.
    }
  }, [draft, onDraftChange, recoveredDraft]);

  function persist(next: ReviewReadinessOwnerDraft): boolean {
    try {
      const result = onDraftChange?.(next, { ownerKey: next.ownerKey });
      if (result === false) return false;
      if (result === true) transactionalParent.current = true;
    } catch { return false; }
    setOwnerDraft(next);
    return true;
  }

  function persistCaptured(next: ReviewReadinessOwnerDraft, requestOwnerKey: string): boolean {
    try {
      const result = onDraftChange?.(next, { ownerKey: next.ownerKey });
      if (result === false) return false;
      if (result === true) transactionalParent.current = true;
    } catch { return false; }
    if (ownerScopeRef.current === requestOwnerKey) setOwnerDraft(next);
    return true;
  }

  function persistTerminal(
    next: ReviewReadinessOwnerDraft,
    requestOwnerKey: string,
    undoRequest?: ProductActionUndoRequest,
  ): boolean {
    const sanitized = sanitizeReviewReadinessTerminalDraft(next);
    try {
      if (onDraftChange?.(sanitized.actionDraft ? sanitized : null, {
        ownerKey: requestOwnerKey,
        ...(recoveryRetireOwnerKey ? { retireOwnerKey: recoveryRetireOwnerKey } : {}),
        ...(undoRequest ? { undoRequest } : {}),
      }) === false) return false;
    } catch {
      // The terminal result is already local and non-sensitive. A parent
      // transaction may retry cleanup without recreating an operation.
      return false;
    }
    if (ownerScopeRef.current === requestOwnerKey) setOwnerDraft(sanitized);
    return true;
  }

  function patchOwner(patch: Partial<ReviewReadinessOwnerDraft>) {
    persist({ ...ownerDraft, ...patch });
  }

  const load = async () => {
    if (!eligible) return;
    const generation = readGeneration.current + 1;
    readGeneration.current = generation;
    setLoading(true);
    setError('');
    try {
      const response = await getReviewReadinessCandidates(noteId, proposal.id);
      if (readGeneration.current !== generation) return;
      setProjectionState(response.state);
      setCandidates(response.candidates);
    } catch {
      if (readGeneration.current === generation) setError('暂时无法读取准备重点，当前复盘和选择均未改变。');
    } finally {
      if (readGeneration.current === generation) setLoading(false);
    }
  };

  useEffect(() => {
    if (previousOwnerKey.current !== ownerKey) {
      previousOwnerKey.current = ownerKey;
      setOwnerDraft(recoveredDraft
        ? recoveredDraft
        : emptyOwnerDraft(ownerKey, ownerGeneration, noteId, proposal.id));
      setProposing(false);
      setError('');
    }
    setCandidates(null);
    setProjectionState('');
    if (eligible) void load();
    return () => { readGeneration.current += 1; };
  }, [eligible, noteId, ownerGeneration, ownerKey, proposal.id, proposal.proposal_hash]);

  useEffect(() => {
    if (!recoveredDraft || recoveredDraft.ownerKey !== ownerKey) return;
    setOwnerDraft((current) => {
      if (
        current.ownerKey !== ownerKey
        || current.actionDraft?.operationId !== recoveredDraft.actionDraft?.operationId
        || current.actionDraft?.actionName !== recoveredDraft.actionDraft?.actionName
      ) return current;
      return current === recoveredDraft ? current : recoveredDraft;
    });
  }, [ownerKey, recoveredDraft]);

  const sourceRequiresRejection = proposal.source_status !== 'current'
    || projectionState === 'source_changed'
    || projectionState === 'source_missing';
  const recoverySourceDraft = ownerDraft.ownerKey === ownerKey ? ownerDraft : recoveredDraft ?? ownerDraft;
  const proposedAction = recoverySourceDraft.actionDraft?.status === 'proposed' ? recoverySourceDraft.actionDraft : null;
  const canRecoverUnsafeProposal = structurallyRecoverableV2Proposal(proposal, ownerBlocked)
    && sourceRequiresRejection
    && proposedAction !== null;
  const sourceVerifiedCurrent = eligible && projectionState === 'ready';
  const visibleActionDraft = proposedAction && !sourceVerifiedCurrent
    ? {
        ...proposedAction,
        confirmationToken: sourceRequiresRejection && hasRejectionOnlyControl(proposedAction)
          ? proposedAction.confirmationToken
          : null,
        allowedDecisions: sourceRequiresRejection && hasRejectionOnlyControl(proposedAction)
          ? ['reject' as const]
          : [],
        pendingDecision: null,
        resultUnknown: false,
      }
    : recoverySourceDraft.actionDraft;

  useEffect(() => {
    if (!canRecoverUnsafeProposal || !proposedAction || !recoverySourceDraft.applicationId) return;
    if (hasRejectionOnlyControl(proposedAction)) return;
    const recoveryKey = `${ownerKey}:${proposedAction.operationId}`;
    if (rejectionRecoveryInFlight.current === recoveryKey) return;
    rejectionRecoveryInFlight.current = recoveryKey;
    setError('');
    void recoverRejectionControl(recoverySourceDraft.applicationId, proposedAction.operationId)
      .then((control) => {
        if (
          ownerScopeRef.current !== ownerKey
          || control.operation_id !== proposedAction.operationId
          || control.action_name !== 'save_review_readiness_signal'
          || !control.rejection_only
          || control.live_source_state !== 'not_observed'
          || control.allowed_decisions.length !== 1
          || control.allowed_decisions[0] !== 'reject'
        ) throw new Error('unsafe_rejection_recovery');
        persistCaptured({
          ...recoverySourceDraft,
          actionDraft: {
            ...proposedAction,
            actionCallId: control.action_call_id,
            confirmationToken: control.confirmation_token,
            allowedDecisions: ['reject'],
            pendingDecision: null,
            resultUnknown: false,
          },
        }, ownerKey);
      })
      .catch(() => {
        if (ownerScopeRef.current === ownerKey) setError('暂时无法恢复安全拒绝操作，请稍后重试。');
      })
      .finally(() => {
        if (rejectionRecoveryInFlight.current === recoveryKey) rejectionRecoveryInFlight.current = null;
      });
  }, [canRecoverUnsafeProposal, ownerKey, proposedAction, recoverySourceDraft]);

  if (!eligible && !canRecoverUnsafeProposal) return null;

  const selected = candidates?.find((candidate) => candidate.focus_id === ownerDraft.selectedFocusId) ?? null;

  async function propose() {
    if (proposalInFlight.current) return;
    const frozen = ownerDraft.proposalUnknown ? ownerDraft.frozenProposalInput : null;
    if (!selected && !frozen) return;
    const idempotencyKey = frozen?.idempotency_key ?? ownerDraft.idempotencyKey ?? crypto.randomUUID();
    const input = frozen ?? {
      proposal_id: selected!.proposal_id,
      focus_id: selected!.focus_id,
      expected_note_revision: selected!.source_note_revision,
      expected_candidate_fingerprint: selected!.candidate_fingerprint,
      idempotency_key: idempotencyKey,
      user_note: ownerDraft.userNote,
    };
    const originalPayload = { user_note: input.user_note };
    const requestOwnerDraft = {
      ...ownerDraft,
      applicationId: selected?.application_id ?? ownerDraft.applicationId,
      selectedFocusId: input.focus_id,
      userNote: input.user_note,
      idempotencyKey,
      frozenProposalInput: input,
      proposalUnknown: false,
    };
    if (!persist(requestOwnerDraft)) {
      setError('无法安全保存本次操作草稿，请保留页面后重试。');
      return;
    }
    const requestOwnerKey = ownerKey;
    proposalInFlight.current = true;
    setProposing(true);
    setError('');
    try {
      const response = await proposeReviewReadinessAction(noteId, input);
      if (response.action_name !== 'save_review_readiness_signal') {
        throw new Error('review_product_action_owner_mismatch');
      }
      if (response.status === 'proposed') {
        persistCaptured({
          ...requestOwnerDraft,
          proposalUnknown: false,
          actionDraft: productActionDraftFromProposal(
            `${ownerKey}:${input.focus_id}`,
            response,
            originalPayload,
            'save_review_readiness_signal',
          ),
        }, requestOwnerKey);
      } else {
        const terminal = terminalDraft(`${ownerKey}:${input.focus_id}`, response, originalPayload);
        persistTerminal({ ...requestOwnerDraft, proposalUnknown: false, actionDraft: terminal }, requestOwnerKey);
        if (ownerScopeRef.current === requestOwnerKey && !terminal && response.status === 'already_confirmed') {
          setProjectionState('already_confirmed');
          setCandidates([]);
        }
      }
    } catch {
      persistCaptured({ ...requestOwnerDraft, proposalUnknown: true }, requestOwnerKey);
      if (ownerScopeRef.current === requestOwnerKey) setError('保存请求结果待确认，请使用原操作重试。');
    } finally {
      proposalInFlight.current = false;
      if (ownerScopeRef.current === requestOwnerKey) setProposing(false);
    }
  }

  function handleActionDraft(
    next: ProductActionOwnerDraft,
    context?: { readonly undoRequest: ProductActionUndoRequest },
  ): boolean {
    const nextOwner = { ...ownerDraft, actionDraft: next };
    const persisted = next.status === 'proposed'
      ? persistCaptured(nextOwner, ownerDraft.ownerKey)
      : persistTerminal(nextOwner, ownerDraft.ownerKey, context?.undoRequest);
    if (!persisted) return false;
    if (next.status !== 'committed' || ownerDraft.actionDraft?.status === 'committed') return true;
    const signalId = next.result?.signal_id;
    const versionId = next.result?.signal_version_id;
    if (Number.isSafeInteger(signalId) && Number(signalId) > 0 && Number.isSafeInteger(versionId) && Number(versionId) > 0 && ownerDraft.applicationId) {
      onSignalCommitted?.({ applicationId: ownerDraft.applicationId, signalId: Number(signalId), versionId: Number(versionId) });
    }
    return true;
  }

  return (
    <section className={styles.nextStep} aria-label="复盘后的下一步" aria-busy={loading || proposing}>
      <h3>复盘后的下一步</h3>
      <p>选择一个有原始来源的练习重点。保存前仍需要你在这里明确确认。</p>
      {loading ? <div className={styles.skeleton} aria-hidden="true" /> : null}
      {error ? <p className={styles.error} role="alert">{error}</p> : null}
      {!loading && projectionState && projectionState !== 'ready' ? <p className={styles.empty}>{stateMessage(projectionState)}</p> : null}
      {!loading && projectionState === 'ready' && candidates ? (
        <div className={styles.focusList} role="radiogroup" aria-label="选择准备重点">
          {candidates.map((candidate) => (
            <label key={candidate.focus_id} className={styles.focusOption}>
              <input
                type="radio"
                name={`readiness-focus-${proposal.id}`}
                checked={ownerDraft.selectedFocusId === candidate.focus_id}
                disabled={Boolean(ownerDraft.actionDraft) || ownerDraft.proposalUnknown}
                onChange={() => {
                  patchOwner({
                    applicationId: candidate.application_id,
                    selectedFocusId: candidate.focus_id,
                    idempotencyKey: null,
                    frozenProposalInput: null,
                  });
                }}
              />
              <span><strong>{candidate.statement}</strong><small>{candidate.evidence[0]?.excerpt ?? '已验证来源'}</small></span>
            </label>
          ))}
        </div>
      ) : null}
      {(selected || visibleActionDraft) ? (
        <>
          <label className={styles.noteField}>
            给自己的备注（可选）
            <textarea
              maxLength={500}
              disabled={ownerDraft.proposalUnknown || proposing || ownerDraft.actionDraft?.resultUnknown || Boolean(ownerDraft.actionDraft && ownerDraft.actionDraft.status !== 'proposed')}
              value={ownerDraft.userNote}
              onChange={(event) => patchOwner({
                userNote: event.target.value,
                ...(ownerDraft.actionDraft ? {} : { idempotencyKey: null, frozenProposalInput: null }),
              })}
            />
          </label>
          {!ownerDraft.actionDraft ? <div className={styles.actions}>
            <button className={styles.primaryButton} type="button" disabled={proposing} onClick={() => void propose()}>{ownerDraft.proposalUnknown ? '使用原操作重试' : '保存为下次准备重点'}</button>
            {onOpenStory && selected ? <button className={styles.secondaryButton} type="button" disabled={proposing || ownerDraft.proposalUnknown} onClick={() => onOpenStory(noteId, selected.focus_id)}>整理为经历素材</button> : null}
          </div> : null}
        </>
      ) : null}
      {visibleActionDraft ? (
        <ProductActionConfirmation
          draft={visibleActionDraft}
          editedPayload={{ user_note: ownerDraft.userNote }}
          onDraftChange={handleActionDraft}
          onRecoverControl={async () => {
            try {
              return await recoverSignalOwnerAction(noteId, visibleActionDraft.operationId);
            } catch {
              if (!ownerDraft.applicationId) throw new Error('review_owner_application_missing');
              return recoverRejectionControl(ownerDraft.applicationId, visibleActionDraft.operationId);
            }
          }}
          onRestart={sourceVerifiedCurrent && visibleActionDraft.status === 'rejected'
            ? async () => {
              persist({
                ...ownerDraft,
                idempotencyKey: null,
                frozenProposalInput: null,
                proposalUnknown: false,
                actionDraft: null,
              });
            }
            : undefined}
          onUndo={visibleActionDraft.status === 'committed' && ownerDraft.applicationId && committedSignalIdentity(visibleActionDraft.result)
            ? async (request) => {
              const identity = committedSignalIdentity(ownerDraft.actionDraft?.result ?? null);
              if (!identity) throw new Error('review_signal_identity_missing');
              if (
                request.ownerKey !== ownerDraft.actionDraft!.ownerKey
                || request.parentOperationId !== ownerDraft.actionDraft!.operationId
                || request.actionName !== 'save_review_readiness_signal'
              ) throw new Error('review_signal_undo_owner_mismatch');
              return undoReadinessSignal(ownerDraft.applicationId!, identity.signalId, request.parentOperationId);
            }
            : undefined}
        />
      ) : null}
      {!ownerDraft.actionDraft ? <div className={styles.liveStatus} role="status" aria-live="polite" aria-atomic="true">
        {loading ? '正在读取准备重点。' : proposing ? '正在创建确认操作。' : ownerDraft.proposalUnknown ? '保存请求结果待确认。' : ''}
      </div> : null}
    </section>
  );
}

export default ReviewReadinessNextStep;
