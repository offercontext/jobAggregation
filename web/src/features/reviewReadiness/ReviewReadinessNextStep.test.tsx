// @vitest-environment jsdom
import { act, type ComponentType } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReviewReadinessOwnerDraft } from './contracts';

const service = vi.hoisted(() => ({ candidates: vi.fn(), propose: vi.fn(), decide: vi.fn(), state: vi.fn(), recover: vi.fn(), rejection: vi.fn(), undo: vi.fn() }));
vi.mock('./service', () => ({
  getReviewReadinessCandidates: service.candidates,
  proposeReviewReadinessAction: service.propose,
  decideProductAction: service.decide,
  getProductActionState: service.state,
  recoverSignalOwnerAction: service.recover,
  recoverRejectionControl: service.rejection,
  undoReadinessSignal: service.undo,
}));

const { ReviewReadinessNextStep } = await import('./ReviewReadinessNextStep');
const { isReviewReadinessDraftPending, isReviewReadinessDraftUnsaved, isSafeReviewReadinessTerminalDraft, recoverReviewReadinessOwnerDraft, sanitizeReviewReadinessTerminalDraft, selectReviewReadinessOwnerDraft } = await import('./contracts');
const { createCoreTaskSurfaceController } = await import('@/features/coreTaskSurface/controller');
const { authorizeReviewReadinessDraftTransaction, closeCoreTaskOwnerWithGuard } = await import('@/layout/AppShell');
const ControlledReviewReadinessNextStep = ReviewReadinessNextStep as unknown as ComponentType<Record<string, unknown>>;

let root: Root;
let host: HTMLDivElement;

const proposal = {
  id: 11,
  note_id: 7,
  application_event_id: 5,
  proposal_schema_version: 2 as const,
  source_note_revision: 4,
  source_fingerprint: `sha256:${'1'.repeat(64)}`,
  source_status: 'current' as const,
  proposal_hash: `sha256:${'2'.repeat(64)}`,
  created_at: '2026-08-31T00:00:00Z',
  proposal: {
    summary: { text: '复盘', evidence_refs: [] },
    observations: [], clarifications: [], next_questions: [],
    practice_focuses: [{ id: 'focus-1', text: '先给结论', evidence_refs: [{ source: 'interview_note' as const, path: '/difficulty_points' as const, excerpt: '取舍不清楚' }] }],
  },
};

const candidate = {
  application_id: 3,
  event_id: 5,
  note_id: 7,
  proposal_id: 11,
  proposal_schema_version: 2 as const,
  focus_id: 'focus-1',
  statement: '先给结论',
  source_note_revision: 4,
  source_note_fingerprint: `sha256:${'1'.repeat(64)}`,
  source_proposal_hash: `sha256:${'2'.repeat(64)}`,
  candidate_fingerprint: `sha256:${'3'.repeat(64)}`,
  evidence: [{ ordinal: 0, source_path: '/difficulty_points' as const, excerpt: '取舍不清楚', excerpt_sha256: `sha256:${'4'.repeat(64)}`, source_field_sha256: `sha256:${'5'.repeat(64)}` }],
};

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  service.candidates.mockReset().mockResolvedValue({ schema_version: 1, state: 'ready', note_id: 7, proposal_id: 11, candidates: [candidate] });
  service.propose.mockReset().mockResolvedValue({ schema_version: 1, operation_id: '00000000-0000-4000-8000-000000000002', action_call_id: '00000000-0000-4000-8000-000000000003', action_name: 'save_review_readiness_signal', status: 'proposed', created: true, replayed: false, confirmation_token: 'a'.repeat(64) });
  service.decide.mockReset();
  service.state.mockReset();
  service.recover.mockReset();
  service.rejection.mockReset();
  service.undo.mockReset();
  vi.spyOn(globalThis.crypto, 'randomUUID').mockReturnValue('00000000-0000-4000-8000-000000000001');
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => { act(() => root.unmount()); host.remove(); vi.restoreAllMocks(); });

describe('ReviewReadinessNextStep', () => {
  it('constructs safe terminal state from exact owner/action whitelists and rejects runtime canaries', () => {
    const tainted = {
      ownerKey: 'review:4:7:11', ownerGeneration: 4, noteId: 7, proposalId: 11, applicationId: 3,
      selectedFocusId: 'focus-1', userNote: 'sensitive note', idempotencyKey: 'sensitive-uuid',
      frozenProposalInput: { proposal_id: 11, focus_id: 'focus-1', expected_note_revision: 4, expected_candidate_fingerprint: 'fingerprint', idempotency_key: 'sensitive-uuid', user_note: 'sensitive note' },
      proposalUnknown: false, topLevelCanary: 'must-not-survive',
      actionDraft: {
        ownerKey: 'review:4:7:11:focus-1', operationId: 'safe-operation', actionCallId: 'sensitive-call', actionName: 'save_review_readiness_signal' as const,
        confirmationToken: 'sensitive-token', allowedDecisions: ['approve'] as const, status: 'committed' as const,
        result: { signal_id: 8, signal_version_id: 9, private_result: 'must-not-survive' },
        originalPayload: { user_note: 'sensitive note' }, pendingDecision: null, resultUnknown: false,
        undoStatus: 'failed' as const, undoReplayed: true, undoRequest: null, undoResultUnknown: false,
        actionLevelCanary: 'must-not-survive',
      },
    } as unknown as ReviewReadinessOwnerDraft;
    const sanitized = sanitizeReviewReadinessTerminalDraft(tainted);
    expect(Object.keys(sanitized).sort()).toEqual([
      'actionDraft', 'applicationId', 'frozenProposalInput', 'idempotencyKey', 'noteId', 'ownerGeneration',
      'ownerKey', 'proposalId', 'proposalUnknown', 'selectedFocusId', 'userNote',
    ].sort());
    expect(Object.keys(sanitized.actionDraft ?? {}).sort()).toEqual([
      'actionCallId', 'actionName', 'allowedDecisions', 'confirmationToken', 'operationId', 'originalPayload',
      'ownerKey', 'pendingDecision', 'result', 'resultUnknown', 'status', 'undoReplayed', 'undoRequest',
      'undoResultUnknown', 'undoStatus',
    ].sort());
    expect(sanitized).toMatchObject({
      selectedFocusId: null, userNote: '', idempotencyKey: null, frozenProposalInput: null,
      actionDraft: {
        ownerKey: 'review:4:7:11:terminal', operationId: 'safe-operation', actionCallId: '',
        result: { signal_id: 8, signal_version_id: 9 }, originalPayload: {}, undoStatus: 'failed', undoReplayed: true,
        undoRequest: null, undoResultUnknown: false,
      },
    });
    expect(JSON.stringify(sanitized)).not.toContain('Canary');
    expect(JSON.stringify(sanitized)).not.toContain('sensitive');
    expect(JSON.stringify(sanitized)).not.toContain('private_result');
    expect(isSafeReviewReadinessTerminalDraft(sanitized)).toBe(true);
    const taintedTop = { ...sanitized, topLevelCanary: true } as unknown as ReviewReadinessOwnerDraft;
    const taintedAction = { ...sanitized, actionDraft: { ...sanitized.actionDraft!, actionLevelCanary: true } } as unknown as ReviewReadinessOwnerDraft;
    expect(isSafeReviewReadinessTerminalDraft(taintedTop)).toBe(false);
    expect(isSafeReviewReadinessTerminalDraft(taintedAction)).toBe(false);
    expect(selectReviewReadinessOwnerDraft({ [taintedTop.ownerKey]: taintedTop }, {
      ownerGeneration: 5, recoveryOwnerGeneration: null, noteId: 7, proposalId: 11, applicationId: 3,
    })).toBeNull();
    expect(selectReviewReadinessOwnerDraft({ [taintedAction.ownerKey]: taintedAction }, {
      ownerGeneration: 5, recoveryOwnerGeneration: null, noteId: 7, proposalId: 11, applicationId: 3,
    })).toBeNull();
    const foreignUndo = sanitizeReviewReadinessTerminalDraft({
      ...tainted,
      actionDraft: {
        ...tainted.actionDraft!, undoStatus: null,
        undoRequest: { ownerKey: 'review:foreign:terminal', originOwnerKey: 'review:foreign:terminal', parentOperationId: 'safe-operation', actionName: 'save_review_readiness_signal' },
        undoResultUnknown: true,
      },
    });
    expect(foreignUndo.actionDraft).toMatchObject({ undoRequest: null, undoResultUnknown: false });

    const malformedOrigin = 'review:not-a-generation:7:11:focus-1';
    const malformedPending = sanitizeReviewReadinessTerminalDraft({
      ...tainted,
      actionDraft: {
        ...tainted.actionDraft!,
        ownerKey: 'review:4:7:11:terminal',
        undoStatus: null,
        undoRequest: {
          ownerKey: 'review:4:7:11:terminal', originOwnerKey: malformedOrigin,
          parentOperationId: 'safe-operation', actionName: 'save_review_readiness_signal',
        },
        undoResultUnknown: true,
      },
    });
    const malformedTerminal = sanitizeReviewReadinessTerminalDraft({
      ...malformedPending,
      actionDraft: {
        ...malformedPending.actionDraft!, undoStatus: 'committed', undoRequest: null, undoResultUnknown: false,
      },
    });
    expect(authorizeReviewReadinessDraftTransaction(
      { [malformedPending.ownerKey]: malformedPending }, malformedTerminal.ownerKey, malformedTerminal,
      undefined, null, {
        ownerKey: 'review:4:7:11:terminal', originOwnerKey: malformedOrigin,
        parentOperationId: 'safe-operation', actionName: 'save_review_readiness_signal',
      },
    )).toBeNull();
  });

  it('preserves only an exact unknown Undo owner through guarded close and recovery generation migration', () => {
    const controller = createCoreTaskSurfaceController();
    const request = { ref: { taskId: 'application.interview_review' as const, applicationId: 3, eventId: 5 }, source: 'application_task_card' as const };
    const first = controller.launch(request);
    if (first.kind !== 'launched') throw new Error('review launch should succeed');
    const oldOwnerKey = `review:${first.generation}:7:11`;
    const old = sanitizeReviewReadinessTerminalDraft({
      ownerKey: oldOwnerKey, ownerGeneration: first.generation, noteId: 7, proposalId: 11, applicationId: 3,
      selectedFocusId: 'focus-1', userNote: '', idempotencyKey: null, frozenProposalInput: null, proposalUnknown: false,
      actionDraft: {
        ownerKey: `${oldOwnerKey}:focus-1`, operationId: 'signal-operation', actionCallId: '',
        actionName: 'save_review_readiness_signal', confirmationToken: null, allowedDecisions: [], status: 'committed',
        result: { signal_id: 8, signal_version_id: 9 }, originalPayload: {}, pendingDecision: null, resultUnknown: false,
        undoStatus: null, undoReplayed: false,
        undoRequest: { ownerKey: `${oldOwnerKey}:focus-1`, originOwnerKey: `${oldOwnerKey}:focus-1`, parentOperationId: 'signal-operation', actionName: 'save_review_readiness_signal' },
        undoResultUnknown: true,
      },
    });
    expect(isSafeReviewReadinessTerminalDraft(old)).toBe(true);
    expect(isReviewReadinessDraftPending(old)).toBe(true);
    closeCoreTaskOwnerWithGuard(controller, { pending: true, unsaved: false });
    const reopened = controller.launch(request);
    if (reopened.kind !== 'launched') throw new Error('review relaunch should succeed');
    const recoveryGeneration = controller.getState().active?.recoveryGeneration ?? null;
    expect(recoveryGeneration).toBe(first.generation);
    const selected = selectReviewReadinessOwnerDraft({ [oldOwnerKey]: old }, {
      ownerGeneration: reopened.generation, recoveryOwnerGeneration: recoveryGeneration,
      noteId: 7, proposalId: 11, applicationId: 3,
    });
    const recovered = recoverReviewReadinessOwnerDraft(selected, {
      ownerGeneration: reopened.generation, recoveryOwnerGeneration: recoveryGeneration,
      noteId: 7, proposalId: 11, applicationId: 3,
    });
    const newOwnerKey = `review:${reopened.generation}:7:11`;
    expect(recovered).toMatchObject({
      ownerKey: newOwnerKey,
      actionDraft: {
        ownerKey: `${newOwnerKey}:terminal`, operationId: 'signal-operation', undoResultUnknown: true,
        undoRequest: { ownerKey: `${newOwnerKey}:terminal`, originOwnerKey: `${oldOwnerKey}:focus-1`, parentOperationId: 'signal-operation', actionName: 'save_review_readiness_signal' },
      },
    });
    expect(isSafeReviewReadinessTerminalDraft(recovered!)).toBe(true);
    expect(selectReviewReadinessOwnerDraft({ [oldOwnerKey]: old }, {
      ownerGeneration: reopened.generation, recoveryOwnerGeneration: recoveryGeneration,
      noteId: 7, proposalId: 11, applicationId: 4,
    })).toBeNull();
  });

  it('requires the late unknown embedded request to exactly match its trusted g1 context before routing to g2', () => {
    const controller = createCoreTaskSurfaceController();
    const request = { ref: { taskId: 'application.interview_review' as const, applicationId: 3, eventId: 5 }, source: 'application_task_card' as const };
    const first = controller.launch(request);
    if (first.kind !== 'launched') throw new Error('g1 should launch');
    controller.close(first.generation, 'preserve');
    controller.markClosed(first.generation);
    const second = controller.launch(request);
    if (second.kind !== 'launched') throw new Error('g2 should launch');
    expect(controller.getState().active?.recoveryGeneration).toBe(first.generation);

    const originOwnerKey = `review:${first.generation}:7:11:terminal`;
    const firstOwnerKey = `review:${first.generation}:7:11`;
    const secondOwnerKey = `review:${second.generation}:7:11`;
    const trustedRequest = {
      ownerKey: `${firstOwnerKey}:terminal`, originOwnerKey,
      parentOperationId: 'signal-operation', actionName: 'save_review_readiness_signal' as const,
    };
    const current = sanitizeReviewReadinessTerminalDraft({
      ownerKey: secondOwnerKey, ownerGeneration: second.generation, noteId: 7, proposalId: 11, applicationId: 3,
      selectedFocusId: null, userNote: '', idempotencyKey: null, frozenProposalInput: null, proposalUnknown: false,
      actionDraft: {
        ownerKey: `${secondOwnerKey}:terminal`, operationId: 'signal-operation', actionCallId: '',
        actionName: 'save_review_readiness_signal', confirmationToken: null, allowedDecisions: [], status: 'committed',
        result: { signal_id: 8, signal_version_id: 9 }, originalPayload: {}, pendingDecision: null, resultUnknown: false,
        undoStatus: null, undoReplayed: false,
        undoRequest: {
          ownerKey: `${secondOwnerKey}:terminal`, originOwnerKey,
          parentOperationId: 'signal-operation', actionName: 'save_review_readiness_signal',
        },
        undoResultUnknown: true,
      },
    });
    const incoming = sanitizeReviewReadinessTerminalDraft({
      ...current,
      ownerKey: firstOwnerKey,
      ownerGeneration: first.generation,
      actionDraft: {
        ...current.actionDraft!,
        ownerKey: `${firstOwnerKey}:terminal`,
        undoRequest: trustedRequest,
      },
    });
    const accepted = authorizeReviewReadinessDraftTransaction(
      { [secondOwnerKey]: current }, firstOwnerKey, incoming, undefined,
      controller.getState().active, trustedRequest,
    );
    expect(accepted?.snapshot[secondOwnerKey]?.actionDraft).toMatchObject({
      ownerKey: `${secondOwnerKey}:terminal`, undoRequest: {
        ownerKey: `${secondOwnerKey}:terminal`, originOwnerKey,
        parentOperationId: 'signal-operation', actionName: 'save_review_readiness_signal',
      },
      undoResultUnknown: true,
    });
    expect(accepted?.settledGeneration).toBeNull();
    expect(controller.getState().active?.recoveryGeneration).toBe(first.generation);

    const mismatches = [
      { ownerKey: `${firstOwnerKey}:foreign` },
      { originOwnerKey: `review:${first.generation}:8:11:terminal` },
      { parentOperationId: 'foreign-operation' },
      { actionName: 'confirm_interview_story' as const },
    ];
    for (const mismatch of mismatches) {
      const foreignEmbedded = {
        ...incoming,
        actionDraft: {
          ...incoming.actionDraft!,
          undoRequest: { ...trustedRequest, ...mismatch },
        },
      } as ReviewReadinessOwnerDraft;
      expect(authorizeReviewReadinessDraftTransaction(
        { [secondOwnerKey]: current }, firstOwnerKey, foreignEmbedded, undefined,
        controller.getState().active, trustedRequest,
      )).toBeNull();
      expect(current.actionDraft?.undoRequest?.originOwnerKey).toBe(originOwnerKey);
      expect(controller.getState().active?.recoveryGeneration).toBe(first.generation);
    }
  });

  it.each(
    (['committed', 'failed', 'throw'] as const).flatMap((outcome) => (
      ['closed', 'foreign_active'] as const
    ).map((window) => ({ outcome, window }))),
  )(
    'routes a late Signal Undo $outcome settlement through a $window window into A g3 without a duplicate request',
    async ({ outcome, window }) => {
      const controller = createCoreTaskSurfaceController();
      const launchRequest = { ref: { taskId: 'application.interview_review' as const, applicationId: 3, eventId: 5 }, source: 'application_task_card' as const };
      const first = controller.launch(launchRequest);
      if (first.kind !== 'launched') throw new Error('review launch should succeed');
      const firstOwnerKey = `review:${first.generation}:7:11`;
      let snapshot: Record<string, ReviewReadinessOwnerDraft> = {
        [firstOwnerKey]: sanitizeReviewReadinessTerminalDraft({
          ownerKey: firstOwnerKey, ownerGeneration: first.generation, noteId: 7, proposalId: 11, applicationId: 3,
          selectedFocusId: 'focus-1', userNote: '', idempotencyKey: null, frozenProposalInput: null, proposalUnknown: false,
          actionDraft: {
            ownerKey: `${firstOwnerKey}:focus-1`, operationId: 'signal-operation', actionCallId: '',
            actionName: 'save_review_readiness_signal', confirmationToken: null, allowedDecisions: [], status: 'committed',
            result: { signal_id: 8, signal_version_id: 9 }, originalPayload: {}, pendingDecision: null, resultUnknown: false,
          },
        }),
      };
      let currentGeneration = first.generation;
      let settle!: (value: unknown) => void;
      let reject!: (error: Error) => void;
      service.undo.mockReturnValueOnce(new Promise((resolve, rejectPromise) => { settle = resolve; reject = rejectPromise; }));
      const render = () => {
        const active = controller.getState().active;
        const selected = selectReviewReadinessOwnerDraft(snapshot, {
          ownerGeneration: currentGeneration, recoveryOwnerGeneration: active?.recoveryGeneration,
          noteId: 7, proposalId: 11, applicationId: 3,
        });
        root.render(<ReviewReadinessNextStep
          noteId={7} proposal={proposal} applicationId={3} ownerGeneration={currentGeneration}
          recoveryOwnerGeneration={active?.recoveryGeneration} draft={selected}
          onDraftChange={(next, transaction) => {
            const ownerKey = transaction?.ownerKey ?? next?.ownerKey;
            if (!ownerKey) return false;
            const authorized = authorizeReviewReadinessDraftTransaction(
              snapshot, ownerKey, next, transaction?.retireOwnerKey, controller.getState().active, transaction?.undoRequest,
            );
            if (!authorized) return false;
            snapshot = authorized.snapshot;
            if (authorized.settledGeneration !== null) controller.settleRecovery(authorized.settledGeneration);
            render();
            return true;
          }}
        />);
      };

      act(render);
      await act(async () => { await Promise.resolve(); await Promise.resolve(); });
      act(() => [...host.querySelectorAll('button')].find((button) => button.textContent === '撤销本次保存')?.click());
      expect(service.undo).toHaveBeenCalledTimes(1);
      expect(snapshot[firstOwnerKey]?.actionDraft).toMatchObject({ undoRequest: expect.any(Object), undoResultUnknown: false });

      closeCoreTaskOwnerWithGuard(controller, { pending: true, unsaved: false });
      const reopened = controller.launch(launchRequest);
      if (reopened.kind !== 'launched') throw new Error('review relaunch should succeed');
      currentGeneration = reopened.generation;
      act(render);
      await act(async () => { await Promise.resolve(); await Promise.resolve(); });
      const currentOwnerKey = `review:${reopened.generation}:7:11`;
      expect(snapshot[firstOwnerKey]).toBeUndefined();
      expect(snapshot[currentOwnerKey]?.actionDraft).toMatchObject({ undoRequest: expect.any(Object), undoResultUnknown: false });
      expect(service.undo).toHaveBeenCalledTimes(1);

      closeCoreTaskOwnerWithGuard(controller, { pending: true, unsaved: false });
      let foreignOwnerKey: string | null = null;
      let foreignDraft: ReviewReadinessOwnerDraft | null = null;
      if (window === 'foreign_active') {
        const foreign = controller.launch({
          ref: { taskId: 'application.interview_review', applicationId: 4, eventId: 6 },
          source: 'application_task_card',
        });
        if (foreign.kind !== 'launched') throw new Error('foreign review launch should succeed');
        foreignOwnerKey = `review:${foreign.generation}:8:12`;
        foreignDraft = {
          ownerKey: foreignOwnerKey, ownerGeneration: foreign.generation, noteId: 8, proposalId: 12, applicationId: 4,
          selectedFocusId: 'foreign-focus', userNote: 'B 的未保存输入', idempotencyKey: null,
          frozenProposalInput: null, proposalUnknown: false, actionDraft: null,
        };
        snapshot = { ...snapshot, [foreignOwnerKey]: foreignDraft };
      }

      await act(async () => {
        if (outcome === 'throw') reject(new Error('response lost'));
        else settle({
          schema_version: 1, operation_id: 'undo-operation', compensation_kind: 'undo:save_review_readiness_signal',
          status: outcome, result: outcome === 'failed' ? { reason: 'dependent_practice_exists' } : {}, replayed: false,
        });
        await Promise.resolve(); await Promise.resolve();
      });
      expect(snapshot[firstOwnerKey]).toBeUndefined();
      if (outcome === 'throw') {
        expect(snapshot[currentOwnerKey]?.actionDraft).toMatchObject({
          undoStatus: null, undoRequest: expect.any(Object), undoResultUnknown: true,
        });
      } else {
        expect(snapshot[currentOwnerKey]?.actionDraft).toMatchObject({
          undoStatus: outcome, undoRequest: null, undoResultUnknown: false,
        });
      }
      expect(service.undo).toHaveBeenCalledTimes(1);
      if (foreignOwnerKey && foreignDraft) expect(snapshot[foreignOwnerKey]).toEqual(foreignDraft);

      const foreignActive = controller.getState().active;
      if (foreignActive) {
        controller.close(foreignActive.generation, 'ordinary');
        controller.markClosed(foreignActive.generation);
      }
      if (outcome !== 'throw') {
        for (let index = 0; index < 70; index += 1) {
          const ordinary = controller.launch({
            ref: { taskId: 'application.interview_review', applicationId: 100 + index, eventId: 200 + index },
            source: 'application_task_card',
          });
          if (ordinary.kind !== 'launched') throw new Error('ordinary owner should launch');
          controller.close(ordinary.generation, 'discard');
          controller.markClosed(ordinary.generation);
        }
      }
      const third = controller.launch(launchRequest);
      if (third.kind !== 'launched') throw new Error('third review launch should succeed');
      expect(controller.getState().active?.recoveryGeneration).toBe(outcome === 'throw' ? first.generation : null);
      currentGeneration = third.generation;
      act(render);
      await act(async () => { await Promise.resolve(); await Promise.resolve(); });
      const thirdOwnerKey = `review:${third.generation}:7:11`;
      expect(snapshot[currentOwnerKey]).toBeUndefined();
      if (outcome === 'throw') {
        expect(snapshot[thirdOwnerKey]?.actionDraft).toMatchObject({
          undoStatus: null, undoRequest: expect.any(Object), undoResultUnknown: true,
        });
        expect(host.textContent).toContain('使用原撤销操作重试');
      } else {
        expect(snapshot[thirdOwnerKey]?.actionDraft).toMatchObject({
          undoStatus: outcome, undoRequest: null, undoResultUnknown: false,
        });
      }
      expect(service.undo).toHaveBeenCalledTimes(1);
      if (outcome === 'throw') {
        service.undo.mockResolvedValueOnce({
          schema_version: 1, operation_id: 'undo-operation', compensation_kind: 'undo:save_review_readiness_signal',
          status: 'committed', result: {}, replayed: true,
        });
        await act(async () => {
          [...host.querySelectorAll('button')].find((button) => button.textContent === '使用原撤销操作重试')?.click();
          await Promise.resolve(); await Promise.resolve();
        });
        expect(service.undo).toHaveBeenCalledTimes(2);
        expect(snapshot[thirdOwnerKey]?.actionDraft).toMatchObject({
          undoStatus: 'committed', undoRequest: null, undoResultUnknown: false,
        });
        closeCoreTaskOwnerWithGuard(controller, { pending: false, unsaved: false });
        for (let index = 0; index < 70; index += 1) {
          const ordinary = controller.launch({
            ref: { taskId: 'application.interview_review', applicationId: 300 + index, eventId: 400 + index },
            source: 'application_task_card',
          });
          if (ordinary.kind !== 'launched') throw new Error('ordinary owner should launch');
          controller.close(ordinary.generation, 'discard');
          controller.markClosed(ordinary.generation);
        }
        const fourth = controller.launch(launchRequest);
        if (fourth.kind !== 'launched') throw new Error('fourth review launch should succeed');
        expect(controller.getState().active?.recoveryGeneration).toBeNull();
      }
    },
  );
  it('settles replacement guards for every real terminal status including already-confirmed projection', () => {
    for (const status of ['committed', 'rejected', 'failed'] as const) {
      const terminal = {
        ownerKey: 'review:9:7:11', ownerGeneration: 9, noteId: 7, proposalId: 11,
        applicationId: 3, selectedFocusId: 'focus-1', userNote: '历史备注', idempotencyKey: 'key',
        frozenProposalInput: { proposal_id: 11, focus_id: 'focus-1', expected_note_revision: 4, expected_candidate_fingerprint: 'fingerprint', idempotency_key: 'key', user_note: '历史备注' },
        proposalUnknown: false,
        actionDraft: { ownerKey: 'action', operationId: 'op', actionCallId: 'call', actionName: 'save_review_readiness_signal' as const, confirmationToken: null, allowedDecisions: [], status, result: {}, originalPayload: {}, pendingDecision: null, resultUnknown: false },
      };
      expect(isReviewReadinessDraftPending(terminal)).toBe(false);
      expect(isReviewReadinessDraftUnsaved(terminal)).toBe(false);
    }
  });

  it('shows one primary and a secondary Story opener only for a safe V2 focus', async () => {
    const openStory = vi.fn();
    act(() => root.render(<ReviewReadinessNextStep noteId={7} proposal={proposal} onOpenStory={openStory} />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(service.candidates).toHaveBeenCalledWith(7, 11);
    act(() => host.querySelector<HTMLInputElement>('input[type="radio"]')?.click());
    const primary = [...host.querySelectorAll('button')].filter((button) => button.textContent === '保存为下次准备重点');
    expect(primary).toHaveLength(1);
    expect(host.textContent).toContain('整理为经历素材');
    act(() => [...host.querySelectorAll('button')].find((button) => button.textContent === '整理为经历素材')?.click());
    expect(openStory).toHaveBeenCalledWith(7, 'focus-1');
  });

  it('converges duplicate primary clicks on one Product Action and exact request', async () => {
    let resolve!: (value: unknown) => void;
    service.propose.mockReturnValue(new Promise((done) => { resolve = done; }));
    act(() => root.render(<ReviewReadinessNextStep noteId={7} proposal={proposal} />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    act(() => host.querySelector<HTMLInputElement>('input[type="radio"]')?.click());
    const primary = [...host.querySelectorAll('button')].find((button) => button.textContent === '保存为下次准备重点')!;
    act(() => { primary.click(); primary.click(); });
    expect(service.propose).toHaveBeenCalledTimes(1);
    expect(service.propose).toHaveBeenCalledWith(7, {
      proposal_id: 11,
      focus_id: 'focus-1',
      expected_note_revision: 4,
      expected_candidate_fingerprint: `sha256:${'3'.repeat(64)}`,
      idempotency_key: '00000000-0000-4000-8000-000000000001',
      user_note: '',
    });
    await act(async () => {
      resolve({ schema_version: 1, operation_id: '00000000-0000-4000-8000-000000000002', action_call_id: '00000000-0000-4000-8000-000000000003', action_name: 'save_review_readiness_signal', status: 'proposed', created: true, replayed: false, confirmation_token: 'a'.repeat(64) });
      await Promise.resolve();
    });
    expect(host.textContent).toContain('确认本次保存');
  });

  it('rejects a foreign action identity from the Review proposal entry without installing its operation', async () => {
    service.propose.mockResolvedValueOnce({
      schema_version: 1, operation_id: 'foreign-story-operation', action_call_id: 'foreign-story-call',
      action_name: 'confirm_interview_story', status: 'proposed', created: true, replayed: false,
      confirmation_token: 'z'.repeat(64),
    });
    let persisted: ReviewReadinessOwnerDraft | null = null;
    const render = () => root.render(<ReviewReadinessNextStep
      noteId={7} proposal={proposal} applicationId={3} ownerGeneration={9} draft={persisted}
      onDraftChange={(next) => { persisted = next; render(); return true; }}
    />);
    act(render);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    act(() => host.querySelector<HTMLInputElement>('input[type="radio"]')?.click());
    await act(async () => {
      [...host.querySelectorAll('button')].find((button) => button.textContent === '保存为下次准备重点')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(persisted).toMatchObject({ proposalUnknown: true, actionDraft: null });
    expect(JSON.stringify(persisted)).not.toContain('foreign-story-operation');
    expect(JSON.stringify(persisted)).not.toContain('foreign-story-call');
  });

  it('does not query or render execution controls for V1, changed or blocked owners', async () => {
    const cases = [
      { ...proposal, proposal_schema_version: 1 as const },
      { ...proposal, source_status: 'source_changed' as const },
    ];
    for (const item of cases) {
      act(() => root.render(<ReviewReadinessNextStep noteId={7} proposal={item} />));
      await act(async () => { await Promise.resolve(); });
      expect(host.textContent).not.toContain('保存为下次准备重点');
    }
    expect(service.candidates).not.toHaveBeenCalled();
  });

  it('persists an unknown Review proposal across remount and replays the exact owner-generation request', async () => {
    service.propose.mockRejectedValueOnce(new Error('network')).mockResolvedValueOnce({
      schema_version: 1, operation_id: '00000000-0000-4000-8000-000000000002', action_call_id: '00000000-0000-4000-8000-000000000003',
      action_name: 'save_review_readiness_signal', status: 'proposed', created: true, replayed: false, confirmation_token: 'a'.repeat(64),
    });
    let persisted: unknown = null;
    const render = () => root.render(<ControlledReviewReadinessNextStep
      noteId={7} proposal={proposal} ownerGeneration={9} draft={persisted}
      onDraftChange={(next: unknown) => { persisted = next; render(); }}
    />);
    act(render);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    act(() => host.querySelector<HTMLInputElement>('input[type="radio"]')?.click());
    const note = host.querySelector<HTMLTextAreaElement>('textarea')!;
    act(() => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set?.call(note, '保留这条备注');
      note.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await act(async () => {
      [...host.querySelectorAll('button')].find((button) => button.textContent === '保存为下次准备重点')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    const first = service.propose.mock.calls[0]?.[1];
    expect(persisted).toMatchObject({ ownerGeneration: 9, proposalUnknown: true, idempotencyKey: first.idempotency_key });

    act(() => root.unmount());
    root = createRoot(host);
    act(render);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    await act(async () => {
      [...host.querySelectorAll('button')].find((button) => button.textContent === '使用原操作重试')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(service.propose).toHaveBeenCalledTimes(2);
    expect(service.propose.mock.calls[1]?.[1]).toEqual(first);
  });

  it('recovers the exact token-bound Review decision after a real controller close and relaunch', async () => {
    const controller = createCoreTaskSurfaceController();
    const first = controller.launch({
      ref: { taskId: 'application.interview_review', applicationId: 3, eventId: 5 },
      source: 'application_task_card',
    });
    if (first.kind !== 'launched') throw new Error('review launch should succeed');
    controller.markOpen(first.generation);
    controller.close(first.generation, 'preserve');
    controller.markClosed(first.generation);
    const reopened = controller.launch({
      ref: { taskId: 'application.interview_review', applicationId: 3, eventId: 5 },
      source: 'application_task_card',
    });
    if (reopened.kind !== 'launched') throw new Error('review relaunch should succeed');
    const recoveryGeneration = controller.getState().active?.recoveryGeneration;
    const originalDecision = { decision: 'modify' as const, edited_payload: { user_note: '保留的冻结备注' } };
    const oldDraft = {
      ownerKey: `review:${first.generation}:7:11`, ownerGeneration: first.generation, noteId: 7, proposalId: 11,
      applicationId: 3, selectedFocusId: 'focus-1', userNote: '保留的冻结备注', idempotencyKey: '00000000-0000-4000-8000-000000000001',
      frozenProposalInput: { proposal_id: 11, focus_id: 'focus-1', expected_note_revision: 4, expected_candidate_fingerprint: candidate.candidate_fingerprint, idempotency_key: '00000000-0000-4000-8000-000000000001', user_note: '保留的冻结备注' },
      proposalUnknown: false,
      actionDraft: {
        ownerKey: `review:${first.generation}:7:11:focus-1`, operationId: 'operation-original', actionCallId: 'call-original', actionName: 'save_review_readiness_signal' as const,
        confirmationToken: 'token-original', allowedDecisions: ['approve', 'modify', 'reject'] as Array<'approve' | 'modify' | 'reject'>, status: 'proposed' as const, result: null,
        originalPayload: { user_note: '' }, pendingDecision: originalDecision, resultUnknown: true,
      },
    };
    service.decide.mockResolvedValue({
      schema_version: 1, operation_id: 'operation-original', action_name: 'save_review_readiness_signal',
      status: 'committed', result: { signal_id: 4, signal_version_id: 5 }, replayed: true, direct_commit: false,
    });
    let drafts: Record<string, ReviewReadinessOwnerDraft> = { [oldDraft.ownerKey]: oldDraft };
    const transact = vi.fn((next: ReviewReadinessOwnerDraft | null, transaction?: { ownerKey: string; retireOwnerKey?: string }) => {
      if (!transaction) return false;
      const updated = { ...drafts };
      if (transaction.retireOwnerKey) delete updated[transaction.retireOwnerKey];
      if (next) updated[transaction.ownerKey] = next;
      else delete updated[transaction.ownerKey];
      drafts = updated;
      return true;
    });
    vi.mocked(globalThis.crypto.randomUUID).mockClear();
    act(() => root.render(<ReviewReadinessNextStep
      noteId={7} proposal={proposal} applicationId={3} ownerGeneration={reopened.generation}
      recoveryOwnerGeneration={recoveryGeneration}
      draft={oldDraft}
      onDraftChange={transact}
    />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    await act(async () => {
      [...host.querySelectorAll('button')].find((button) => button.textContent === '使用原操作重试')?.click();
      await Promise.resolve(); await Promise.resolve();
    });

    expect(reopened.generation).toBe(first.generation + 1);
    expect(service.decide).toHaveBeenCalledWith('operation-original', {
      confirmation_token: 'token-original', ...originalDecision,
    });
    expect(globalThis.crypto.randomUUID).not.toHaveBeenCalled();
    expect(transact).toHaveBeenCalledWith(expect.objectContaining({
      ownerKey: `review:${reopened.generation}:7:11`, ownerGeneration: reopened.generation,
    }), { ownerKey: `review:${reopened.generation}:7:11`, retireOwnerKey: oldDraft.ownerKey });
    expect(Object.keys(drafts)).toEqual([`review:${reopened.generation}:7:11`]);
    expect(drafts[`review:${reopened.generation}:7:11`]).toMatchObject({
      actionDraft: {
        operationId: 'operation-original', actionCallId: '', confirmationToken: null,
        status: 'committed', result: { signal_id: 4, signal_version_id: 5 }, originalPayload: {},
      },
    });
    expect(JSON.stringify(drafts)).not.toContain('token-original');
    expect(JSON.stringify(drafts)).not.toContain('00000000-0000-4000-8000-000000000001');
    expect(JSON.stringify(drafts)).not.toContain('保留的冻结备注');
  });

  it('keeps the exact old Review unknown draft when atomic migration installation throws', async () => {
    const oldDraft = {
      ownerKey: 'review:4:7:11', ownerGeneration: 4, noteId: 7, proposalId: 11, applicationId: 3,
      selectedFocusId: 'focus-1', userNote: '不可丢正文', idempotencyKey: 'uuid-old',
      frozenProposalInput: { proposal_id: 11, focus_id: 'focus-1', expected_note_revision: 4, expected_candidate_fingerprint: candidate.candidate_fingerprint, idempotency_key: 'uuid-old', user_note: '不可丢正文' },
      proposalUnknown: true, actionDraft: null,
    };
    let drafts: Record<string, ReviewReadinessOwnerDraft> = { [oldDraft.ownerKey]: oldDraft };
    const install = vi.fn(() => { throw new Error('storage aborted'); });
    act(() => root.render(<ControlledReviewReadinessNextStep
      noteId={7} proposal={proposal} ownerGeneration={5} recoveryOwnerGeneration={4}
      draft={oldDraft} onDraftChange={install}
    />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(install).toHaveBeenCalledWith(expect.objectContaining({ ownerKey: 'review:5:7:11' }), {
      ownerKey: 'review:5:7:11', retireOwnerKey: 'review:4:7:11',
    });
    expect(drafts).toEqual({ 'review:4:7:11': oldDraft });
    await act(async () => {
      [...host.querySelectorAll('button')].find((button) => button.textContent === '使用原操作重试')?.click();
      await Promise.resolve();
    });
    expect(service.propose).not.toHaveBeenCalled();
    expect(drafts).toEqual({ 'review:4:7:11': oldDraft });
    const retryInstall = (next: ReviewReadinessOwnerDraft | null, transaction?: { ownerKey: string; retireOwnerKey?: string }) => {
      if (!next || !transaction) return false;
      const updated = { ...drafts, [transaction.ownerKey]: next };
      if (transaction.retireOwnerKey) delete updated[transaction.retireOwnerKey];
      drafts = updated;
      return true;
    };
    act(() => root.render(<ControlledReviewReadinessNextStep
      noteId={7} proposal={proposal} ownerGeneration={5} recoveryOwnerGeneration={4}
      draft={oldDraft} onDraftChange={retryInstall}
    />));
    await act(async () => { await Promise.resolve(); });
    expect(drafts['review:4:7:11']).toBeUndefined();
    expect(drafts['review:5:7:11']).toMatchObject({ idempotencyKey: 'uuid-old', userNote: '不可丢正文' });
  });

  it('recovers the exact unknown Review proposal UUID and frozen body after a real close and relaunch', async () => {
    const controller = createCoreTaskSurfaceController();
    const first = controller.launch({ ref: { taskId: 'application.interview_review', applicationId: 3, eventId: 5 }, source: 'application_task_card' });
    if (first.kind !== 'launched') throw new Error('review launch should succeed');
    controller.close(first.generation, 'preserve');
    controller.markClosed(first.generation);
    const reopened = controller.launch({ ref: { taskId: 'application.interview_review', applicationId: 3, eventId: 5 }, source: 'application_task_card' });
    if (reopened.kind !== 'launched') throw new Error('review relaunch should succeed');
    const originalInput = {
      proposal_id: 11, focus_id: 'focus-1', expected_note_revision: 4,
      expected_candidate_fingerprint: candidate.candidate_fingerprint,
      idempotency_key: '00000000-0000-4000-8000-000000000077', user_note: '冻结的备注',
    };
    const oldDraft = {
      ownerKey: `review:${first.generation}:7:11`, ownerGeneration: first.generation, noteId: 7, proposalId: 11,
      applicationId: 3, selectedFocusId: 'focus-1', userNote: '冻结的备注', idempotencyKey: originalInput.idempotency_key,
      frozenProposalInput: originalInput, proposalUnknown: true, actionDraft: null,
    };
    act(() => root.render(<ReviewReadinessNextStep
      noteId={7} proposal={proposal} ownerGeneration={reopened.generation}
      recoveryOwnerGeneration={controller.getState().active?.recoveryGeneration}
      draft={oldDraft}
    />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    vi.mocked(globalThis.crypto.randomUUID).mockClear();
    await act(async () => {
      [...host.querySelectorAll('button')].find((button) => button.textContent === '使用原操作重试')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(service.propose).toHaveBeenCalledWith(7, originalInput);
    expect(globalThis.crypto.randomUUID).not.toHaveBeenCalled();
  });

  it('recovers exact unsaved Review focus and user_note after close and relaunch', async () => {
    const controller = createCoreTaskSurfaceController();
    const request = { ref: { taskId: 'application.interview_review' as const, applicationId: 3, eventId: 5 }, source: 'application_task_card' as const };
    const first = controller.launch(request);
    if (first.kind !== 'launched') throw new Error('review launch should succeed');
    controller.close(first.generation, 'preserve'); controller.markClosed(first.generation);
    const reopened = controller.launch(request);
    if (reopened.kind !== 'launched') throw new Error('review relaunch should succeed');
    const oldDraft = {
      ownerKey: `review:${first.generation}:7:11`, ownerGeneration: first.generation, noteId: 7, proposalId: 11,
      applicationId: 3, selectedFocusId: 'focus-1', userNote: '尚未提交的精确备注', idempotencyKey: null,
      frozenProposalInput: null, proposalUnknown: false, actionDraft: null,
    };
    const persisted: unknown[] = [];
    act(() => root.render(<ReviewReadinessNextStep
      noteId={7} proposal={proposal} ownerGeneration={reopened.generation}
      recoveryOwnerGeneration={controller.getState().active?.recoveryGeneration}
      draft={oldDraft} onDraftChange={(next) => { persisted.push(next); }}
    />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(host.querySelector<HTMLInputElement>('input[type="radio"]')?.checked).toBe(true);
    expect(host.querySelector<HTMLTextAreaElement>('textarea')?.value).toBe('尚未提交的精确备注');
    expect(persisted).toContainEqual(expect.objectContaining({
      ownerGeneration: reopened.generation, selectedFocusId: 'focus-1', userNote: '尚未提交的精确备注',
    }));
  });

  it('does not recover a settled Review draft or a draft from a different exact owner', async () => {
    const controller = createCoreTaskSurfaceController();
    const first = controller.launch({ ref: { taskId: 'application.interview_review', applicationId: 3, eventId: 5 }, source: 'application_task_card' });
    if (first.kind !== 'launched') throw new Error('review launch should succeed');
    controller.close(first.generation);
    controller.markClosed(first.generation);
    const reopened = controller.launch({ ref: { taskId: 'application.interview_review', applicationId: 3, eventId: 5 }, source: 'application_task_card' });
    if (reopened.kind !== 'launched') throw new Error('review relaunch should succeed');
    const settled = {
      ownerKey: `review:${first.generation}:7:11`, ownerGeneration: first.generation, noteId: 7, proposalId: 11,
      applicationId: 3, selectedFocusId: 'focus-1', userNote: '', idempotencyKey: 'old', frozenProposalInput: null, proposalUnknown: false,
      actionDraft: { ownerKey: 'old', operationId: 'settled-operation', actionCallId: 'settled-call', actionName: 'save_review_readiness_signal' as const, confirmationToken: null, allowedDecisions: [], status: 'committed' as const, result: {}, originalPayload: {}, pendingDecision: null, resultUnknown: false },
    };
    act(() => root.render(<ReviewReadinessNextStep
      noteId={7} proposal={proposal} ownerGeneration={reopened.generation}
      recoveryOwnerGeneration={controller.getState().active?.recoveryGeneration}
      draft={settled}
    />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(host.textContent).not.toContain('撤销本次保存');
    expect(host.textContent).not.toContain('使用原操作重试');

    controller.close(reopened.generation);
    controller.markClosed(reopened.generation);
    const different = controller.launch({ ref: { taskId: 'application.interview_review', applicationId: 3, eventId: 6 }, source: 'application_task_card' });
    if (different.kind !== 'launched') throw new Error('different review launch should succeed');
    expect(controller.getState().active?.recoveryGeneration).toBeNull();
  });

  it('keeps canonical Review user_note editable after proposal so modify sends the exact effective payload', async () => {
    service.decide.mockResolvedValue({
      schema_version: 1, operation_id: '00000000-0000-4000-8000-000000000002', action_name: 'save_review_readiness_signal',
      status: 'committed', result: { signal_id: 4, signal_version_id: 5 }, replayed: false, direct_commit: false,
    });
    let persisted: unknown = null;
    const render = () => root.render(<ControlledReviewReadinessNextStep
      noteId={7} proposal={proposal} ownerGeneration={9} draft={persisted}
      onDraftChange={(next: unknown) => { persisted = next; render(); }}
    />);
    act(render);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    act(() => host.querySelector<HTMLInputElement>('input[type="radio"]')?.click());
    await act(async () => {
      [...host.querySelectorAll('button')].find((button) => button.textContent === '保存为下次准备重点')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    const editable = host.querySelector<HTMLTextAreaElement>('textarea');
    expect(editable).not.toBeNull();
    act(() => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set?.call(editable, '修改后的备注');
      editable?.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await act(async () => { await Promise.resolve(); });
    await act(async () => {
      [...host.querySelectorAll('button')].find((button) => button.textContent === '保存修改')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(service.decide).toHaveBeenCalledWith('00000000-0000-4000-8000-000000000002', {
      confirmation_token: 'a'.repeat(64), decision: 'modify', edited_payload: { user_note: '修改后的备注' },
    });
  });

  it('falls back to application-bound rejection-only recovery without inventing terminal state', async () => {
    service.decide.mockRejectedValueOnce(new Error('network')).mockResolvedValueOnce({
      schema_version: 1, operation_id: '00000000-0000-4000-8000-000000000002', action_name: 'save_review_readiness_signal',
      status: 'rejected', result: {}, replayed: false, direct_commit: false,
    });
    service.state.mockResolvedValue({ schema_version: 1, operation_id: '00000000-0000-4000-8000-000000000002', action_name: 'save_review_readiness_signal', status: 'proposed' });
    service.recover.mockRejectedValue(new Error('source invalid'));
    service.rejection.mockResolvedValue({
      schema_version: 1, operation_id: '00000000-0000-4000-8000-000000000002', action_call_id: '00000000-0000-4000-8000-000000000003',
      action_name: 'save_review_readiness_signal', status: 'proposed', confirmation_token: 'b'.repeat(64), allowed_decisions: ['reject'], rejection_only: true, live_source_state: 'not_observed',
    });
    let persisted: unknown = null;
    const render = () => root.render(<ControlledReviewReadinessNextStep
      noteId={7} proposal={proposal} ownerGeneration={9} draft={persisted}
      onDraftChange={(next: unknown) => { persisted = next; render(); }}
    />);
    act(render);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    act(() => host.querySelector<HTMLInputElement>('input[type="radio"]')?.click());
    await act(async () => {
      [...host.querySelectorAll('button')].find((button) => button.textContent === '保存为下次准备重点')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    await act(async () => {
      [...host.querySelectorAll('button')].find((button) => button.textContent === '确认保存')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    await act(async () => {
      [...host.querySelectorAll('button')].find((button) => button.textContent === '确认操作结果')?.click();
      await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
    });
    expect(service.recover).toHaveBeenCalled();
    expect(service.rejection).toHaveBeenCalledWith(3, '00000000-0000-4000-8000-000000000002');
    const approve = [...host.querySelectorAll('button')].find((button) => button.textContent === '确认保存');
    const reject = [...host.querySelectorAll('button')].find((button) => button.textContent === '暂不保存') as HTMLButtonElement;
    expect(approve).toBeUndefined();
    expect(reject.disabled).toBe(false);
    await act(async () => { reject.click(); await Promise.resolve(); await Promise.resolve(); });
    expect(service.decide).toHaveBeenLastCalledWith('00000000-0000-4000-8000-000000000002', { confirmation_token: 'b'.repeat(64), decision: 'reject' });
  });

  it('recovers only rejection after source drift, close and history reopen without proposing again', async () => {
    const controller = createCoreTaskSurfaceController();
    const first = controller.launch({ ref: { taskId: 'application.interview_review', applicationId: 3, eventId: 5 }, source: 'application_task_card' });
    if (first.kind !== 'launched') throw new Error('review launch should succeed');
    let persisted: unknown = null;
    const renderCurrent = () => root.render(<ControlledReviewReadinessNextStep
      noteId={7} proposal={proposal} ownerGeneration={first.generation} draft={persisted}
      onDraftChange={(next: unknown) => { persisted = next; renderCurrent(); }}
    />);
    act(renderCurrent);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    act(() => host.querySelector<HTMLInputElement>('input[type="radio"]')?.click());
    await act(async () => {
      [...host.querySelectorAll('button')].find((button) => button.textContent === '保存为下次准备重点')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(persisted).toMatchObject({ actionDraft: { status: 'proposed', operationId: '00000000-0000-4000-8000-000000000002' } });

    controller.close(first.generation, 'preserve'); controller.markClosed(first.generation);
    const reopened = controller.launch({ ref: { taskId: 'application.interview_review', applicationId: 3, eventId: 5 }, source: 'application_task_card' });
    if (reopened.kind !== 'launched') throw new Error('review relaunch should succeed');
    service.candidates.mockClear();
    service.propose.mockClear();
    service.recover.mockClear();
    service.state.mockClear();
    service.rejection.mockResolvedValue({
      schema_version: 1, operation_id: '00000000-0000-4000-8000-000000000002', action_call_id: '00000000-0000-4000-8000-000000000003',
      action_name: 'save_review_readiness_signal', status: 'proposed', confirmation_token: 'b'.repeat(64),
      allowed_decisions: ['reject'], rejection_only: true, live_source_state: 'not_observed',
    });
    service.decide.mockResolvedValue({
      schema_version: 1, operation_id: '00000000-0000-4000-8000-000000000002', action_name: 'save_review_readiness_signal',
      status: 'rejected', result: {}, replayed: false, direct_commit: false,
    });
    const changed = { ...proposal, source_status: 'source_changed' as const };
    const renderChanged = () => root.render(<ControlledReviewReadinessNextStep
      noteId={7} proposal={changed} ownerGeneration={reopened.generation}
      recoveryOwnerGeneration={controller.getState().active?.recoveryGeneration} draft={persisted}
      onDraftChange={(next: unknown) => { persisted = next; renderChanged(); }}
    />);
    act(renderChanged);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); });

    expect(service.candidates).not.toHaveBeenCalled();
    expect(service.propose).not.toHaveBeenCalled();
    expect(service.recover).not.toHaveBeenCalled();
    expect(service.state).not.toHaveBeenCalled();
    expect(service.rejection).toHaveBeenCalledWith(3, '00000000-0000-4000-8000-000000000002');
    expect(host.textContent).not.toContain('确认保存');
    expect(host.textContent).not.toContain('保存修改');
    expect(host.textContent).not.toContain('保存为下次准备重点');
    const reject = [...host.querySelectorAll('button')].find((button) => button.textContent === '暂不保存');
    expect(reject).toBeTruthy();
    await act(async () => { reject?.click(); await Promise.resolve(); await Promise.resolve(); });
    expect(service.decide).toHaveBeenCalledTimes(1);
    expect(service.decide).toHaveBeenCalledWith('00000000-0000-4000-8000-000000000002', {
      confirmation_token: 'b'.repeat(64), decision: 'reject',
    });
  });

  it('downgrades a proposed action to rejection-only when the candidate source is now missing', async () => {
    service.candidates.mockResolvedValue({ schema_version: 1, state: 'source_missing', note_id: 7, proposal_id: 11, candidates: [] });
    service.rejection.mockResolvedValue({
      schema_version: 1, operation_id: 'operation-missing', action_call_id: 'call-missing',
      action_name: 'save_review_readiness_signal', status: 'proposed', confirmation_token: 'c'.repeat(64),
      allowed_decisions: ['reject'], rejection_only: true, live_source_state: 'not_observed',
    });
    const proposedDraft = {
      ownerKey: 'review:9:7:11', ownerGeneration: 9, noteId: 7, proposalId: 11,
      applicationId: 3, selectedFocusId: 'focus-1', userNote: '', idempotencyKey: 'key', frozenProposalInput: null, proposalUnknown: false,
      actionDraft: {
        ownerKey: 'review:9:7:11:focus-1', operationId: 'operation-missing', actionCallId: 'call-original', actionName: 'save_review_readiness_signal' as const,
        confirmationToken: 'unsafe-old-token', allowedDecisions: ['approve', 'modify', 'reject'] as Array<'approve' | 'modify' | 'reject'>,
        status: 'proposed' as const, result: null, originalPayload: { user_note: '' }, pendingDecision: null, resultUnknown: false,
      },
    };
    act(() => root.render(<ReviewReadinessNextStep noteId={7} proposal={proposal} ownerGeneration={9} draft={proposedDraft} />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); });
    expect(service.rejection).toHaveBeenCalledWith(3, 'operation-missing');
    expect(host.textContent).not.toContain('确认保存');
    expect(host.textContent).not.toContain('保存修改');
    expect([...host.querySelectorAll('button')].find((button) => button.textContent === '暂不保存')).toBeTruthy();
  });

  it('settles a rejected terminal draft and lets the same owner edit before a new proposal', async () => {
    service.decide.mockResolvedValue({
      schema_version: 1, operation_id: '00000000-0000-4000-8000-000000000002', action_name: 'save_review_readiness_signal',
      status: 'rejected', result: { reason: 'user_rejected' }, replayed: false, direct_commit: false,
    });
    let persisted: unknown = null;
    const render = () => root.render(<ControlledReviewReadinessNextStep
      noteId={7} proposal={proposal} ownerGeneration={9} draft={persisted}
      onDraftChange={(next: unknown) => { persisted = next; render(); }}
    />);
    act(render);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    act(() => host.querySelector<HTMLInputElement>('input[type="radio"]')?.click());
    await act(async () => {
      [...host.querySelectorAll('button')].find((button) => button.textContent === '保存为下次准备重点')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    await act(async () => {
      [...host.querySelectorAll('button')].find((button) => button.textContent === '暂不保存')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(persisted).toMatchObject({ actionDraft: { status: 'rejected' } });
    expect(host.textContent).toContain('再次保存');
    await act(async () => {
      [...host.querySelectorAll('button')].find((button) => button.textContent === '再次保存')?.click();
      await Promise.resolve();
    });
    expect(persisted).toMatchObject({ actionDraft: null, frozenProposalInput: null, idempotencyKey: null, proposalUnknown: false });
    act(() => host.querySelector<HTMLInputElement>('input[type="radio"]')?.click());
    expect(host.querySelector<HTMLTextAreaElement>('textarea')?.disabled).toBe(false);
    expect(host.textContent).toContain('保存为下次准备重点');
  });

  it('persists a late proposal response only to its captured owner and never projects it into a replacement owner', async () => {
    let resolveOld!: (value: unknown) => void;
    service.propose.mockReturnValueOnce(new Promise((resolve) => { resolveOld = resolve; }));
    const persisted: unknown[] = [];
    act(() => root.render(<ReviewReadinessNextStep noteId={7} proposal={proposal} ownerGeneration={9} onDraftChange={(next) => { persisted.push(next); }} />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    act(() => host.querySelector<HTMLInputElement>('input[type="radio"]')?.click());
    act(() => [...host.querySelectorAll('button')].find((button) => button.textContent === '保存为下次准备重点')?.click());
    const replacement = { ...proposal, id: 12, proposal_hash: `sha256:${'6'.repeat(64)}` };
    act(() => root.render(<ReviewReadinessNextStep noteId={7} proposal={replacement} ownerGeneration={10} onDraftChange={(next) => { persisted.push(next); }} />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    await act(async () => {
      resolveOld({ schema_version: 1, operation_id: '00000000-0000-4000-8000-000000000002', action_call_id: '00000000-0000-4000-8000-000000000003', action_name: 'save_review_readiness_signal', status: 'proposed', created: true, replayed: false, confirmation_token: 'a'.repeat(64) });
      await Promise.resolve(); await Promise.resolve();
    });
    expect(persisted.some((value) => (value as { ownerKey?: string; actionDraft?: unknown }).ownerKey === 'review:9:7:11' && Boolean((value as { actionDraft?: unknown }).actionDraft))).toBe(true);
    expect(host.textContent).not.toContain('确认本次保存');
    expect(host.querySelector('[aria-label="复盘后的下一步"]')).not.toBeNull();
  });

  it.each(['false', 'throw'] as const)('does not propose when exact frozen Review persistence returns %s', async (failure) => {
    const preselected: ReviewReadinessOwnerDraft = {
      ownerKey: 'review:9:7:11', ownerGeneration: 9, noteId: 7, proposalId: 11, applicationId: 3,
      selectedFocusId: 'focus-1', userNote: 'must persist first', idempotencyKey: null, frozenProposalInput: null,
      proposalUnknown: false, actionDraft: null,
    };
    const persist = vi.fn(() => {
      if (failure === 'throw') throw new Error('storage failed');
      return false;
    });
    act(() => root.render(<ReviewReadinessNextStep
      noteId={7} proposal={proposal} ownerGeneration={9} draft={preselected} onDraftChange={persist}
    />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    await act(async () => {
      [...host.querySelectorAll('button')].find((button) => button.textContent === '保存为下次准备重点')?.click();
      await Promise.resolve();
    });
    expect(persist).toHaveBeenCalledWith(expect.objectContaining({
      idempotencyKey: '00000000-0000-4000-8000-000000000001',
      frozenProposalInput: expect.objectContaining({ user_note: 'must persist first' }),
    }), { ownerKey: 'review:9:7:11' });
    expect(service.propose).not.toHaveBeenCalled();
  });

  it.each(['approve', 'modify'] as const)('keeps only an owner-safe terminal after %s, migrates it across CoreTask close/reopen, and preserves Undo', async (decision) => {
    service.decide.mockResolvedValue({
      schema_version: 1, operation_id: '00000000-0000-4000-8000-000000000002', action_name: 'save_review_readiness_signal',
      status: 'committed', result: { signal_id: 4, signal_version_id: 5, private_projection: 'must-not-persist' }, replayed: false, direct_commit: false,
    });
    service.undo.mockResolvedValue({
      schema_version: 1, operation_id: '00000000-0000-4000-8000-000000000004', compensation_kind: 'undo:save_review_readiness_signal',
      status: 'committed', result: { signal_id: 4, retracted_version_id: 6 }, replayed: false,
    });
    const controller = createCoreTaskSurfaceController();
    const request = { ref: { taskId: 'application.interview_review' as const, applicationId: 3, eventId: 5 }, source: 'application_task_card' as const };
    const first = controller.launch(request);
    if (first.kind !== 'launched') throw new Error('review launch should succeed');
    let snapshot: Record<string, ReviewReadinessOwnerDraft> = {};

    const renderOwner = (generation: number) => {
      const selected = selectReviewReadinessOwnerDraft(snapshot, {
        ownerGeneration: generation, recoveryOwnerGeneration: controller.getState().active?.recoveryGeneration,
        noteId: 7, proposalId: 11, applicationId: 3,
      });
      root.render(<ReviewReadinessNextStep
        noteId={7} proposal={proposal} applicationId={3} ownerGeneration={generation}
        recoveryOwnerGeneration={controller.getState().active?.recoveryGeneration} draft={selected}
        onDraftChange={(next, transaction) => {
          const ownerKey = transaction?.ownerKey ?? next?.ownerKey;
          if (!ownerKey) return false;
          const authorized = authorizeReviewReadinessDraftTransaction(
            snapshot, ownerKey, next, transaction?.retireOwnerKey, controller.getState().active, transaction?.undoRequest,
          );
          if (!authorized) return false;
          snapshot = authorized.snapshot;
          if (authorized.settledGeneration !== null) controller.settleRecovery(authorized.settledGeneration);
          renderOwner(generation);
          return true;
        }}
      />);
    };

    act(() => renderOwner(first.generation));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    act(() => host.querySelector<HTMLInputElement>('input[type="radio"]')?.click());
    await act(async () => {
      [...host.querySelectorAll('button')].find((button) => button.textContent === '保存为下次准备重点')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    if (decision === 'modify') {
      const note = host.querySelector<HTMLTextAreaElement>('textarea');
      act(() => {
        Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set?.call(note, '只应存在于冻结请求');
        note?.dispatchEvent(new Event('input', { bubbles: true }));
      });
    }
    await act(async () => {
      [...host.querySelectorAll('button')].find((button) => button.textContent === (decision === 'modify' ? '保存修改' : '确认保存'))?.click();
      await Promise.resolve(); await Promise.resolve();
    });

    expect(Object.keys(snapshot)).toEqual([`review:${first.generation}:7:11`]);
    const safeTerminal = snapshot[`review:${first.generation}:7:11`];
    expect(safeTerminal).toMatchObject({
      userNote: '', idempotencyKey: null, frozenProposalInput: null, proposalUnknown: false,
      actionDraft: {
        operationId: '00000000-0000-4000-8000-000000000002', actionCallId: '', confirmationToken: null,
        allowedDecisions: [], status: 'committed', result: { signal_id: 4, signal_version_id: 5 },
        originalPayload: {}, pendingDecision: null, resultUnknown: false,
      },
    });
    expect(JSON.stringify(safeTerminal)).not.toContain('a'.repeat(64));
    expect(JSON.stringify(safeTerminal)).not.toContain('00000000-0000-4000-8000-000000000001');
    expect(JSON.stringify(safeTerminal)).not.toContain('只应存在于冻结请求');
    expect(JSON.stringify(safeTerminal)).not.toContain('private_projection');

    controller.close(first.generation);
    controller.markClosed(first.generation);
    const reopened = controller.launch(request);
    if (reopened.kind !== 'launched') throw new Error('review relaunch should succeed');
    expect(controller.getState().active?.recoveryGeneration).toBeNull();
    act(() => renderOwner(reopened.generation));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    expect(Object.keys(snapshot)).toEqual([`review:${reopened.generation}:7:11`]);
    expect(host.textContent).toContain('撤销本次保存');
    expect(selectReviewReadinessOwnerDraft(snapshot, {
      ownerGeneration: reopened.generation, recoveryOwnerGeneration: null,
      noteId: 7, proposalId: 12, applicationId: 3,
    })).toBeNull();
    await act(async () => {
      [...host.querySelectorAll('button')].find((button) => button.textContent === '撤销本次保存')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(service.undo).toHaveBeenCalledWith(3, 4, '00000000-0000-4000-8000-000000000002');
    expect(snapshot[`review:${reopened.generation}:7:11`]?.actionDraft).toMatchObject({ undoStatus: 'committed' });
    expect(host.textContent).toContain('已撤销本次保存');
  });

  it('propagates replayed failed Signal Undo as deterministic terminal without a second transport', async () => {
    let persisted: ReviewReadinessOwnerDraft = {
      ownerKey: 'review:9:7:11', ownerGeneration: 9, noteId: 7, proposalId: 11,
      applicationId: 3, selectedFocusId: null, userNote: '', idempotencyKey: null, frozenProposalInput: null, proposalUnknown: false,
      actionDraft: {
        ownerKey: 'review:9:7:11:terminal', operationId: 'signal-operation', actionCallId: '', actionName: 'save_review_readiness_signal',
        confirmationToken: null, allowedDecisions: [], status: 'committed', result: { signal_id: 4, signal_version_id: 5 },
        originalPayload: {}, pendingDecision: null, resultUnknown: false, undoStatus: null, undoReplayed: false,
        undoRequest: null, undoResultUnknown: false,
      },
    };
    service.undo.mockResolvedValue({
      schema_version: 1, operation_id: 'signal-undo', compensation_kind: 'undo:save_review_readiness_signal',
      status: 'failed', result: { reason: 'dependent_practice_exists' }, replayed: true,
    });
    const render = () => root.render(<ReviewReadinessNextStep
      noteId={7} proposal={proposal} applicationId={3} ownerGeneration={9} draft={persisted}
      onDraftChange={(next) => {
        if (next) persisted = next;
        render();
        return true;
      }}
    />);
    act(render);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    await act(async () => {
      [...host.querySelectorAll('button')].find((button) => button.textContent === '撤销本次保存')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(service.undo).toHaveBeenCalledWith(3, 4, 'signal-operation');
    expect(persisted.actionDraft).toMatchObject({ undoStatus: 'failed', undoReplayed: true });
    expect(host.textContent).toContain('撤销未完成');
    expect(host.textContent).not.toContain('已撤销本次保存');
    expect([...host.querySelectorAll('button')].find((button) => button.textContent === '撤销本次保存')).toBeUndefined();
    act(render);
    expect(service.undo).toHaveBeenCalledTimes(1);
  });
});
