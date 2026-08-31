// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ProductActionOwnerDraft } from './contracts';

const service = vi.hoisted(() => ({ decide: vi.fn(), state: vi.fn() }));
vi.mock('./service', () => ({ decideProductAction: service.decide, getProductActionState: service.state }));

const { ProductActionConfirmation, productActionDraftFromProposal } = await import('./ProductActionConfirmation');

let root: Root;
let host: HTMLDivElement;

const proposal = {
  schema_version: 1 as const,
  operation_id: '00000000-0000-4000-8000-000000000002',
  action_call_id: '00000000-0000-4000-8000-000000000003',
  action_name: 'save_review_readiness_signal' as const,
  status: 'proposed' as const,
  created: true,
  replayed: false,
  confirmation_token: 'a'.repeat(64),
};

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  service.decide.mockReset();
  service.state.mockReset();
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

describe('ProductActionConfirmation', () => {
  it.each(['false', 'throw'] as const)('does not send a decision when frozen token-bound persistence returns %s', async (failure) => {
    const decide = vi.fn();
    const persist = vi.fn(() => {
      if (failure === 'throw') throw new Error('storage failed');
      return false;
    });
    const draft = productActionDraftFromProposal('review:7:11:focus-1', proposal, { user_note: '' });
    act(() => root.render(<ProductActionConfirmation draft={draft} onDraftChange={persist} onDecision={decide} />));
    await act(async () => {
      [...host.querySelectorAll('button')].find((button) => button.textContent === '确认保存')?.click();
      await Promise.resolve();
    });
    expect(persist).toHaveBeenCalledWith(expect.objectContaining({
      confirmationToken: expect.any(String), pendingDecision: { decision: 'approve' }, resultUnknown: true,
    }));
    expect(decide).not.toHaveBeenCalled();
  });
  it('converges duplicate approve clicks and clears the token on terminal success', async () => {
    let resolve!: (value: unknown) => void;
    service.decide.mockReturnValue(new Promise((done) => { resolve = done; }));
    let draft = productActionDraftFromProposal('review:7:11:focus-1', proposal, { user_note: '' });
    const render = () => root.render(<ProductActionConfirmation draft={draft} onDraftChange={(next) => { draft = next; render(); }} />);
    act(render);
    const approve = [...host.querySelectorAll('button')].find((button) => button.textContent === '确认保存')!;
    act(() => { approve.click(); approve.click(); });
    expect(service.decide).toHaveBeenCalledTimes(1);
    await act(async () => {
      resolve({ schema_version: 1, operation_id: proposal.operation_id, action_name: proposal.action_name, status: 'committed', result: { signal_id: 8, signal_version_id: 9, signal_revision: 1 }, replayed: false, direct_commit: true });
      await Promise.resolve();
    });
    expect(draft.status).toBe('committed');
    expect(draft.confirmationToken).toBeNull();
    expect(host.textContent).toContain('已保存为下次准备重点');
  });

  it('fails closed when proposal, decision, state, or recovered control has a foreign primary-action identity', async () => {
    expect(() => productActionDraftFromProposal(
      'review:7:11:focus-1',
      { ...proposal, action_name: 'confirm_interview_story' },
      { user_note: '' },
      'save_review_readiness_signal',
    )).toThrow('product_action_proposal_owner_mismatch');

    let draft = productActionDraftFromProposal(
      'review:7:11:focus-1', proposal, { user_note: '' }, 'save_review_readiness_signal',
    );
    const decision = vi.fn().mockResolvedValue({
      schema_version: 1, operation_id: 'foreign-operation', action_name: 'save_review_readiness_signal',
      status: 'committed', result: { signal_id: 999 }, replayed: false, direct_commit: false,
    });
    const render = (recover?: () => Promise<never>) => root.render(<ProductActionConfirmation
      draft={draft}
      onDraftChange={(next) => { draft = next; render(recover); }}
      onDecision={decision}
      onRecoverControl={recover}
    />);
    act(() => render());
    await act(async () => {
      [...host.querySelectorAll('button')].find((button) => button.textContent === '确认保存')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(draft).toMatchObject({ status: 'proposed', resultUnknown: true, result: null });
    expect(JSON.stringify(draft)).not.toContain('999');

    service.state.mockResolvedValueOnce({
      schema_version: 1, operation_id: proposal.operation_id, action_name: 'confirm_interview_story',
      status: 'committed', result: { signal_id: 998 },
    });
    await act(async () => {
      [...host.querySelectorAll('button')].find((button) => button.textContent === '确认操作结果')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(draft).toMatchObject({ status: 'proposed', resultUnknown: true, result: null });
    expect(JSON.stringify(draft)).not.toContain('998');

    service.state.mockResolvedValueOnce({
      schema_version: 1, operation_id: proposal.operation_id, action_name: proposal.action_name,
      status: 'proposed',
    });
    const recover = vi.fn().mockResolvedValue({
      schema_version: 1, operation_id: 'foreign-operation', action_call_id: 'foreign-call',
      action_name: proposal.action_name, status: 'proposed', confirmation_token: 'z'.repeat(64),
      allowed_decisions: ['approve'], rejection_only: false, live_source_state: 'current',
    });
    act(() => render(recover));
    await act(async () => {
      [...host.querySelectorAll('button')].find((button) => button.textContent === '确认操作结果')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(draft.operationId).toBe(proposal.operation_id);
    expect(draft.confirmationToken).toBe(proposal.confirmation_token);
    expect(draft.actionCallId).toBe(proposal.action_call_id);
    expect(JSON.stringify(draft)).not.toContain('foreign-call');
  });

  it('binds a Story decision to confirm_interview_story and never installs a Signal result', async () => {
    const storyProposal = { ...proposal, action_name: 'confirm_interview_story' as const };
    let draft = productActionDraftFromProposal(
      'story:33:3:1', storyProposal, { content: {} }, 'confirm_interview_story',
    );
    const decision = vi.fn().mockResolvedValue({
      schema_version: 1, operation_id: storyProposal.operation_id, action_name: 'save_review_readiness_signal',
      status: 'committed', result: { signal_id: 999 }, replayed: false, direct_commit: false,
    });
    const render = () => root.render(<ProductActionConfirmation
      draft={draft}
      onDraftChange={(next) => { draft = next; render(); }}
      onDecision={decision}
    />);
    act(render);
    await act(async () => {
      [...host.querySelectorAll('button')].find((button) => button.textContent === '确认保存')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(draft).toMatchObject({ actionName: 'confirm_interview_story', status: 'proposed', resultUnknown: true, result: null });
    expect(JSON.stringify(draft)).not.toContain('999');
  });

  it('keeps the original token and effective payload for unknown modify replay', async () => {
    service.decide.mockRejectedValueOnce(new Error('network')).mockResolvedValueOnce({ schema_version: 1, operation_id: proposal.operation_id, action_name: proposal.action_name, status: 'committed', result: { signal_id: 8, signal_version_id: 9, signal_revision: 1 }, replayed: true, direct_commit: false });
    let draft = productActionDraftFromProposal('review:7:11:focus-1', proposal, { user_note: '' });
    const render = () => root.render(<ProductActionConfirmation draft={draft} editedPayload={{ user_note: '下次先给结论。' }} onDraftChange={(next) => { draft = next; render(); }} />);
    act(render);
    await act(async () => { [...host.querySelectorAll('button')].find((button) => button.textContent === '保存修改')?.click(); await Promise.resolve(); });
    expect(draft.resultUnknown).toBe(true);
    expect(draft.confirmationToken).toBe('a'.repeat(64));
    expect(draft.pendingDecision).toEqual({ decision: 'modify', edited_payload: { user_note: '下次先给结论。' } });
    await act(async () => { [...host.querySelectorAll('button')].find((button) => button.textContent === '使用原操作重试')?.click(); await Promise.resolve(); });
    expect(service.decide.mock.calls[0]![1]).toEqual(service.decide.mock.calls[1]![1]);
  });

  it('persists the exact pending decision before a close and replays it after remount', async () => {
    service.decide.mockReturnValueOnce(new Promise(() => undefined)).mockResolvedValueOnce({
      schema_version: 1, operation_id: proposal.operation_id, action_name: proposal.action_name,
      status: 'rejected', result: { reason: 'user_rejected' }, replayed: true, direct_commit: false,
    });
    let draft = productActionDraftFromProposal('review:7:11:focus-1', proposal, { user_note: '' });
    const render = () => root.render(<ProductActionConfirmation draft={draft} onDraftChange={(next) => { draft = next; render(); }} />);
    act(render);
    act(() => [...host.querySelectorAll('button')].find((button) => button.textContent === '暂不保存')?.click());
    expect(draft).toMatchObject({ resultUnknown: true, pendingDecision: { decision: 'reject' } });
    const first = service.decide.mock.calls[0]![1];
    act(() => root.unmount());
    root = createRoot(host);
    act(render);
    await act(async () => {
      [...host.querySelectorAll('button')].find((button) => button.textContent === '使用原操作重试')?.click();
      await Promise.resolve();
    });
    expect(service.decide.mock.calls[1]![1]).toEqual(first);
  });

  it('supports reject and owner-scoped Undo without a second modal', async () => {
    service.decide.mockResolvedValue({ schema_version: 1, operation_id: proposal.operation_id, action_name: proposal.action_name, status: 'rejected', result: { reason: 'user_rejected' }, replayed: false, direct_commit: false });
    const undo = vi.fn().mockResolvedValue(undefined);
    let draft = productActionDraftFromProposal('review:7:11:focus-1', proposal, { user_note: '' });
    const render = () => root.render(<ProductActionConfirmation draft={draft} onDraftChange={(next) => { draft = next; render(); }} onUndo={undo} />);
    act(render);
    await act(async () => { [...host.querySelectorAll('button')].find((button) => button.textContent === '暂不保存')?.click(); await Promise.resolve(); });
    expect(draft.status).toBe('rejected');
    expect(host.querySelector('[role="dialog"]')).toBeNull();
  });

  it('treats typed failed Undo as deterministic terminal and never sends a second request', async () => {
    let draft: ProductActionOwnerDraft = {
      ...productActionDraftFromProposal('review:7:11:focus-1', proposal, { user_note: '' }),
      confirmationToken: null,
      allowedDecisions: [],
      status: 'committed' as const,
      result: { signal_id: 8, signal_version_id: 9 },
    };
    const undo = vi.fn().mockResolvedValue({
      schema_version: 1, operation_id: 'undo-operation', compensation_kind: 'undo:save_review_readiness_signal',
      status: 'failed', result: { reason: 'dependent_practice_exists' }, replayed: false,
    });
    const render = () => root.render(<ProductActionConfirmation
      draft={draft}
      onDraftChange={(next) => { draft = next; render(); }}
      onUndo={undo}
    />);
    act(render);

    await act(async () => {
      [...host.querySelectorAll('button')].find((button) => button.textContent === '撤销本次保存')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(host.textContent).toContain('撤销未完成');
    expect(host.textContent).not.toContain('已撤销本次保存');
    expect([...host.querySelectorAll('button')].find((button) => button.textContent === '撤销本次保存')).toBeUndefined();
    expect(draft).toMatchObject({ undoStatus: 'failed', undoReplayed: false, undoRequest: null, undoResultUnknown: false });
    act(render);
    expect(undo).toHaveBeenCalledTimes(1);
    expect(host.textContent).not.toContain('已撤销本次保存');
  });

  it.each(['false', 'throw'] as const)('persists exact Undo ownership before transport and aborts when persistence returns %s', async (failure) => {
    const draft: ProductActionOwnerDraft = {
      ...productActionDraftFromProposal('review:7:11:focus-1', proposal, { user_note: '' }),
      confirmationToken: null, allowedDecisions: [], status: 'committed', result: { signal_id: 8, signal_version_id: 9 },
    };
    const undo = vi.fn();
    const persist = vi.fn(() => {
      if (failure === 'throw') throw new Error('storage failed');
      return false;
    });
    act(() => root.render(<ProductActionConfirmation draft={draft} onDraftChange={persist} onUndo={undo} />));
    await act(async () => {
      [...host.querySelectorAll('button')].find((button) => button.textContent === '撤销本次保存')?.click();
      await Promise.resolve();
    });
    expect(persist).toHaveBeenCalledWith(expect.objectContaining({
      undoRequest: {
        ownerKey: draft.ownerKey, originOwnerKey: draft.ownerKey, parentOperationId: draft.operationId, actionName: draft.actionName,
      },
      undoResultUnknown: false,
    }), expect.objectContaining({ undoRequest: expect.objectContaining({ originOwnerKey: draft.ownerKey }) }));
    expect(undo).not.toHaveBeenCalled();
  });

  it('does not duplicate a persisted pending Undo after remount and only replays an unknown exact request', async () => {
    let rejectTransport!: (error: Error) => void;
    let draft: ProductActionOwnerDraft = {
      ...productActionDraftFromProposal('review:7:11:focus-1', proposal, { user_note: '' }),
      confirmationToken: null, allowedDecisions: [], status: 'committed', result: { signal_id: 8, signal_version_id: 9 },
    };
    const undo = vi.fn()
      .mockReturnValueOnce(new Promise((_, reject) => { rejectTransport = reject; }))
      .mockResolvedValueOnce({
        schema_version: 1, operation_id: 'undo-operation', compensation_kind: 'undo:save_review_readiness_signal',
        status: 'committed', result: {}, replayed: true,
      });
    const render = () => root.render(<ProductActionConfirmation
      draft={draft}
      onDraftChange={(next) => { draft = next; render(); return true; }}
      onUndo={undo}
    />);
    act(render);
    act(() => [...host.querySelectorAll('button')].find((button) => button.textContent === '撤销本次保存')?.click());
    expect(draft).toMatchObject({ undoRequest: { ownerKey: draft.ownerKey, parentOperationId: draft.operationId }, undoResultUnknown: false });
    act(() => root.unmount());
    root = createRoot(host);
    act(render);
    expect([...host.querySelectorAll('button')].find((button) => button.textContent === '撤销本次保存')).toBeUndefined();
    expect(undo).toHaveBeenCalledTimes(1);

    await act(async () => { rejectTransport(new Error('response lost')); await Promise.resolve(); });
    expect(draft.undoResultUnknown).toBe(true);
    const replay = [...host.querySelectorAll('button')].find((button) => button.textContent === '使用原撤销操作重试');
    expect(replay).toBeTruthy();
    await act(async () => { replay?.click(); await Promise.resolve(); await Promise.resolve(); });
    expect(undo).toHaveBeenCalledTimes(2);
    expect(undo.mock.calls[0]?.[0]).toEqual(undo.mock.calls[1]?.[0]);
    expect(draft).toMatchObject({ undoStatus: 'committed', undoRequest: null, undoResultUnknown: false });
  });

  it('keeps an exact unknown reconciliation state when terminal Undo settlement persistence returns false', async () => {
    let draft: ProductActionOwnerDraft = {
      ...productActionDraftFromProposal('review:7:11:focus-1', proposal, { user_note: '' }),
      confirmationToken: null, allowedDecisions: [], status: 'committed', result: { signal_id: 8, signal_version_id: 9 },
    };
    const undo = vi.fn().mockResolvedValue({
      schema_version: 1, operation_id: 'undo-operation', compensation_kind: 'undo:save_review_readiness_signal',
      status: 'committed', result: {}, replayed: false,
    });
    const render = () => root.render(<ProductActionConfirmation
      draft={draft}
      onDraftChange={(next) => {
        if (next.undoStatus === 'committed') return false;
        draft = next;
        render();
        return true;
      }}
      onUndo={undo}
    />);
    act(render);
    await act(async () => {
      [...host.querySelectorAll('button')].find((button) => button.textContent === '撤销本次保存')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(draft).toMatchObject({ undoStatus: null, undoRequest: expect.any(Object), undoResultUnknown: true });
    expect(host.textContent).not.toContain('已撤销本次保存');
    expect(host.textContent).toContain('使用原撤销操作重试');
    expect(undo).toHaveBeenCalledTimes(1);
  });

  it('never transports or consumes an Undo request from another exact owner', async () => {
    const draft: ProductActionOwnerDraft = {
      ...productActionDraftFromProposal('review:7:11:focus-1', proposal, { user_note: '' }),
      confirmationToken: null, allowedDecisions: [], status: 'committed', result: { signal_id: 8, signal_version_id: 9 },
      undoRequest: { ownerKey: 'review:foreign', originOwnerKey: 'review:foreign', parentOperationId: proposal.operation_id, actionName: proposal.action_name },
      undoResultUnknown: true,
    };
    const undo = vi.fn();
    act(() => root.render(<ProductActionConfirmation draft={draft} onDraftChange={() => true} onUndo={undo} />));
    expect([...host.querySelectorAll('button')].find((button) => button.textContent === '使用原撤销操作重试')).toBeUndefined();
    expect(undo).not.toHaveBeenCalled();
  });

  it('keeps exact Undo retry only for transport unknown and validates the action-specific compensation kind', async () => {
    let draft: ProductActionOwnerDraft = {
      ...productActionDraftFromProposal('review:7:11:focus-1', proposal, { user_note: '' }),
      confirmationToken: null, allowedDecisions: [], status: 'committed', result: { signal_id: 8, signal_version_id: 9 },
    };
    const undo = vi.fn()
      .mockRejectedValueOnce(new Error('transport unknown'))
      .mockResolvedValueOnce({
        schema_version: 1, operation_id: 'wrong-kind', compensation_kind: 'undo:confirm_interview_story',
        status: 'committed', result: {}, replayed: false,
      })
      .mockResolvedValueOnce({
        schema_version: 1, operation_id: 'signal-undo', compensation_kind: 'undo:save_review_readiness_signal',
        status: 'committed', result: { signal_id: 8 }, replayed: true,
      });
    const render = () => root.render(<ProductActionConfirmation draft={draft} onDraftChange={(next) => { draft = next; render(); }} onUndo={undo} />);
    act(render);
    const clickUndo = async () => {
      [...host.querySelectorAll('button')].find((button) => ['撤销本次保存', '使用原撤销操作重试'].includes(button.textContent ?? ''))?.click();
      await Promise.resolve(); await Promise.resolve();
    };
    await act(clickUndo);
    expect(host.textContent).toContain('撤销结果待确认');
    expect([...host.querySelectorAll('button')].find((button) => button.textContent === '使用原撤销操作重试')).toBeTruthy();
    await act(clickUndo);
    expect(host.textContent).not.toContain('已撤销本次保存');
    expect([...host.querySelectorAll('button')].find((button) => button.textContent === '使用原撤销操作重试')).toBeTruthy();
    expect(draft.undoStatus).toBeNull();
    await act(clickUndo);
    expect(host.textContent).toContain('已撤销本次保存');
    expect(undo).toHaveBeenCalledTimes(3);
  });
});
