import { useEffect, useRef, useState } from 'react';
import type {
  ProductActionDecision,
  ProductActionDecisionRequest,
  ProductActionDecisionResponse,
  ProductActionCompensationResponse,
  ProductActionOwnerDraft,
  ProductActionProposalResponse,
  ProductActionRecoveryResponse,
  ProductActionUndoRequest,
} from './contracts';
import { decideProductAction, getProductActionState } from './service';
import styles from './reviewReadiness.module.css';

interface ProductActionConfirmationProps {
  draft: ProductActionOwnerDraft;
  editedPayload?: Record<string, unknown>;
  onDraftChange: (
    draft: ProductActionOwnerDraft,
    context?: { readonly undoRequest: ProductActionUndoRequest },
  ) => boolean | void;
  onTerminal?: (response: ProductActionDecisionResponse) => void;
  onDecision?: (request: ProductActionDecisionRequest) => Promise<ProductActionDecisionResponse>;
  onRecoverControl?: () => Promise<ProductActionRecoveryResponse>;
  onUndo?: (request: ProductActionUndoRequest) => Promise<ProductActionCompensationResponse>;
  onRestart?: () => Promise<void>;
  approveLabel?: string;
  modifyLabel?: string;
  rejectLabel?: string;
  committedLabel?: string;
  rejectedLabel?: string;
  failedLabel?: string;
}

export function productActionDraftFromProposal(
  ownerKey: string,
  proposal: ProductActionProposalResponse,
  originalPayload: Record<string, unknown>,
  expectedActionName?: ProductActionOwnerDraft['actionName'],
): ProductActionOwnerDraft {
  if (
    (expectedActionName !== undefined && proposal.action_name !== expectedActionName)
    ||
    proposal.status !== 'proposed'
    || !proposal.operation_id
    || !proposal.action_call_id
    || !proposal.confirmation_token
  ) {
    throw new Error(expectedActionName !== undefined && proposal.action_name !== expectedActionName
      ? 'product_action_proposal_owner_mismatch'
      : 'product_action_proposal_is_not_owner_controllable');
  }
  return {
    ownerKey,
    operationId: proposal.operation_id,
    actionCallId: proposal.action_call_id,
    actionName: proposal.action_name,
    confirmationToken: proposal.confirmation_token,
    allowedDecisions: ['approve', 'modify', 'reject'],
    status: 'proposed',
    result: null,
    originalPayload: { ...originalPayload },
    pendingDecision: null,
    resultUnknown: false,
    undoStatus: null,
    undoReplayed: false,
    undoRequest: null,
    undoResultUnknown: false,
  };
}

function responseOwnsDraft(
  draft: ProductActionOwnerDraft,
  response: { operation_id: string; action_name: ProductActionOwnerDraft['actionName'] },
): boolean {
  return response.operation_id === draft.operationId && response.action_name === draft.actionName;
}

function exactUndoRequest(draft: ProductActionOwnerDraft, request: ProductActionUndoRequest | null | undefined): request is ProductActionUndoRequest {
  return Boolean(request
    && request.ownerKey === draft.ownerKey
    && typeof request.originOwnerKey === 'string'
    && request.originOwnerKey.length > 0
    && request.parentOperationId === draft.operationId
    && request.actionName === draft.actionName);
}

function terminalDraft(
  draft: ProductActionOwnerDraft,
  response: Pick<ProductActionDecisionResponse, 'status' | 'result'>,
): ProductActionOwnerDraft {
  return {
    ...draft,
    confirmationToken: null,
    status: response.status,
    result: { ...response.result },
    pendingDecision: null,
    resultUnknown: false,
  };
}

function payloadChanged(
  original: Record<string, unknown>,
  edited: Record<string, unknown> | undefined,
): boolean {
  return edited !== undefined && JSON.stringify(original) !== JSON.stringify(edited);
}

export function ProductActionConfirmation({
  draft,
  editedPayload,
  onDraftChange,
  onTerminal,
  onDecision,
  onRecoverControl,
  onUndo,
  onRestart,
  approveLabel = '确认保存',
  modifyLabel = '保存修改',
  rejectLabel = '暂不保存',
  committedLabel = '已保存为下次准备重点。',
  rejectedLabel = '本次未保存，你可以继续修改后创建新一轮确认。',
  failedLabel = '本次保存未完成，请重新检查来源。',
}: ProductActionConfirmationProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [undoStatus, setUndoStatus] = useState<ProductActionCompensationResponse['status'] | null>(draft.undoStatus ?? null);
  const requestInFlight = useRef(false);
  const changed = payloadChanged(draft.originalPayload, editedPayload);

  useEffect(() => {
    setUndoStatus(draft.undoStatus ?? null);
  }, [draft.operationId, draft.undoStatus]);

  async function submit(decision: ProductActionDecision) {
    if (requestInFlight.current || !draft.confirmationToken || draft.status !== 'proposed') return;
    const pending = draft.resultUnknown && draft.pendingDecision
      ? draft.pendingDecision
      : decision === 'modify'
        ? { decision, edited_payload: { ...(editedPayload ?? draft.originalPayload) } } as const
        : { decision } as const;
    if (!draft.allowedDecisions.includes(pending.decision)) return;
    const request = { confirmation_token: draft.confirmationToken, ...pending } as ProductActionDecisionRequest;
    const pendingDraft = { ...draft, pendingDecision: pending, resultUnknown: true };
    // The composition root receives the exact token-bound decision before
    // transport starts, so a visual close/remount can only replay this body.
    try {
      if (onDraftChange(pendingDraft) === false) {
        setError('无法安全保存本次确认操作，请保留页面后重试。');
        return;
      }
    } catch {
      setError('无法安全保存本次确认操作，请保留页面后重试。');
      return;
    }
    requestInFlight.current = true;
    setBusy(true);
    setError('');
    try {
      const response = await (onDecision ? onDecision(request) : decideProductAction(draft.operationId, request));
      if (!responseOwnsDraft(draft, response)) throw new Error('product_action_decision_owner_mismatch');
      const next = terminalDraft(draft, response);
      onDraftChange(next);
      onTerminal?.(response);
    } catch {
      onDraftChange(pendingDraft);
      setError('操作结果待确认，请保留当前页面并使用原操作重试。');
    } finally {
      requestInFlight.current = false;
      setBusy(false);
    }
  }

  async function reconcile() {
    if (requestInFlight.current || draft.status !== 'proposed') return;
    requestInFlight.current = true;
    setBusy(true);
    setError('');
    try {
      const state = await getProductActionState(draft.operationId);
      if (!responseOwnsDraft(draft, state)) throw new Error('product_action_state_owner_mismatch');
      if (state.status !== 'proposed') {
        const response: ProductActionDecisionResponse = {
          schema_version: 1,
          operation_id: state.operation_id,
          action_name: state.action_name,
          status: state.status,
          result: state.result ?? {},
          replayed: true,
          direct_commit: false,
        };
        onDraftChange(terminalDraft(draft, response));
        onTerminal?.(response);
        return;
      }
      if (!onRecoverControl) {
        setError('操作仍在等待确认，请使用原操作重试。');
        return;
      }
      const control = await onRecoverControl();
      if (!responseOwnsDraft(draft, control)) throw new Error('product_action_recovery_owner_mismatch');
      onDraftChange({
        ...draft,
        actionCallId: control.action_call_id,
        confirmationToken: control.confirmation_token,
        allowedDecisions: [...control.allowed_decisions],
        resultUnknown: false,
      });
    } catch {
      setError('暂时无法确认操作结果，请稍后重试。');
    } finally {
      requestInFlight.current = false;
      setBusy(false);
    }
  }

  async function undo() {
    if (!onUndo || requestInFlight.current) return;
    const persistedRequest = draft.undoRequest ?? null;
    if (persistedRequest && (!draft.undoResultUnknown || !exactUndoRequest(draft, persistedRequest))) {
      setError('撤销恢复状态与当前操作不匹配，未发送请求。');
      return;
    }
    const request: ProductActionUndoRequest = persistedRequest ?? {
      ownerKey: draft.ownerKey,
      originOwnerKey: draft.ownerKey,
      parentOperationId: draft.operationId,
      actionName: draft.actionName,
    };
    const pendingDraft = { ...draft, undoRequest: request, undoResultUnknown: false };
    try {
      if (onDraftChange(pendingDraft, { undoRequest: request }) === false) {
        setError('无法安全保存撤销操作，请保留页面后重试。');
        return;
      }
    } catch {
      setError('无法安全保存撤销操作，请保留页面后重试。');
      return;
    }
    requestInFlight.current = true;
    setBusy(true);
    setError('');
    try {
      const response = await onUndo(request);
      const expectedKind = draft.actionName === 'save_review_readiness_signal'
        ? 'undo:save_review_readiness_signal'
        : 'undo:confirm_interview_story';
      if (response.compensation_kind !== expectedKind) {
        throw new Error('product_action_compensation_owner_mismatch');
      }
      const next = {
        ...draft,
        undoStatus: response.status,
        undoReplayed: response.replayed,
        undoRequest: null,
        undoResultUnknown: false,
      };
      if (onDraftChange(next, { undoRequest: request }) === false) throw new Error('product_action_undo_settlement_persistence_failed');
      setUndoStatus(response.status);
      if (response.status === 'failed') {
        setError(response.replayed
          ? '撤销未完成；服务器返回了同一失败结果，原保存仍然有效。'
          : '撤销未完成；服务器已记录失败结果，原保存仍然有效。');
      }
    } catch {
      try {
        onDraftChange({ ...draft, undoRequest: request, undoResultUnknown: true }, { undoRequest: request });
      } catch {
        // The exact pre-transport pending request remains authoritative when
        // the owner store cannot yet install its unknown-result transition.
      }
      setError('撤销结果待确认，请不要重复创建新的操作。');
    } finally {
      requestInFlight.current = false;
      setBusy(false);
    }
  }

  async function restart() {
    if (!onRestart || requestInFlight.current) return;
    requestInFlight.current = true;
    setBusy(true);
    setError('');
    try {
      await onRestart();
    } catch {
      setError('暂时无法创建下一轮确认，请保留当前故事草稿后重试。');
    } finally {
      requestInFlight.current = false;
      setBusy(false);
    }
  }

  const terminalCopy = draft.status === 'committed'
    ? committedLabel
    : draft.status === 'rejected'
      ? rejectedLabel
      : draft.status === 'failed'
        ? failedLabel
        : '';

  return (
    <section className={styles.confirmation} aria-label="产品操作确认" aria-busy={busy}>
      {draft.status === 'proposed' ? (
        <>
          <h4 className={styles.heading}>确认本次保存</h4>
          <p className={styles.supporting}>保存仅在你明确确认后执行；不会发送到聊天，也不会创建第二个确认窗口。</p>
          <div className={styles.actions}>
            {draft.resultUnknown ? (
              <>
                <button className={styles.primaryButton} type="button" disabled={busy} onClick={() => void submit(draft.pendingDecision?.decision ?? 'approve')}>使用原操作重试</button>
                <button className={styles.secondaryButton} type="button" disabled={busy} onClick={() => void reconcile()}>确认操作结果</button>
              </>
            ) : (
              <>
                {!changed && draft.allowedDecisions.includes('approve') ? <button className={styles.primaryButton} type="button" disabled={busy} onClick={() => void submit('approve')}>{approveLabel}</button> : null}
                {changed && draft.allowedDecisions.includes('modify') ? <button className={styles.primaryButton} type="button" disabled={busy} onClick={() => void submit('modify')}>{modifyLabel}</button> : null}
                <button className={styles.secondaryButton} type="button" disabled={busy || !draft.allowedDecisions.includes('reject')} onClick={() => void submit('reject')}>{rejectLabel}</button>
              </>
            )}
          </div>
        </>
      ) : (
        <div className={styles.terminal} data-status={draft.status}>
          <strong>{terminalCopy}</strong>
          {draft.status === 'committed' && onUndo && undoStatus === null && !draft.undoRequest ? <button className={styles.secondaryButton} type="button" disabled={busy} onClick={() => void undo()}>撤销本次保存</button> : null}
          {draft.status === 'committed' && onUndo && undoStatus === null && draft.undoResultUnknown && exactUndoRequest(draft, draft.undoRequest) ? <button className={styles.secondaryButton} type="button" disabled={busy} onClick={() => void undo()}>使用原撤销操作重试</button> : null}
          {draft.status === 'committed' && undoStatus === null && draft.undoRequest && !draft.undoResultUnknown && exactUndoRequest(draft, draft.undoRequest) ? <span>撤销正在处理，请等待原操作返回。</span> : null}
          {draft.status === 'committed' && draft.undoRequest && !exactUndoRequest(draft, draft.undoRequest) ? <span>撤销恢复状态与当前操作不匹配。</span> : null}
          {undoStatus === 'committed' ? <span>已撤销本次保存。</span> : null}
          {undoStatus === 'failed' ? <span>撤销未完成，原保存仍然有效。</span> : null}
          {draft.status === 'rejected' && onRestart ? <button className={styles.primaryButton} type="button" disabled={busy} onClick={() => void restart()}>再次保存</button> : null}
        </div>
      )}
      <div className={styles.liveStatus} role="status" aria-live="polite" aria-atomic="true">
        {busy ? '正在处理，请稍候。' : undoStatus === 'committed' ? '撤销已完成。' : undoStatus === 'failed' ? '撤销未完成，原保存仍然有效。' : draft.resultUnknown ? '操作结果待确认。' : terminalCopy}
      </div>
      {error ? <p className={styles.error} role="alert">{error}</p> : null}
    </section>
  );
}

export default ProductActionConfirmation;
