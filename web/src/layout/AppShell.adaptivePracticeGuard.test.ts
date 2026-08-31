import { describe, expect, it } from 'vitest';
import type { AdaptivePracticeOwnerDraft } from '@/types/adaptiveInterviewPractice';
import { isReviewReadinessDraftUnsaved, type ReviewReadinessOwnerDraft } from '@/features/reviewReadiness/contracts';
import {
  adaptivePracticeDraftGuard,
  adaptivePracticeGuardAfterOwnerLaunch,
  adaptivePracticeSubOwnerReplacementDenied,
  authorizeInterviewPreparationPracticeHandoff,
  closeCoreTaskOwnerWithGuard,
  interviewPreparationPracticeHandoffAllowsUnsavedBypass,
  settleRecoveredCoreTaskAfterGuardTransition,
  transactAdaptivePracticeDraftSnapshot,
  transactReviewReadinessDraftSnapshot,
  authorizeReviewReadinessDraftTransaction,
} from './AppShell';
import appShellSource from './AppShell.tsx?raw';
import { createCoreTaskSurfaceController } from '@/features/coreTaskSurface/controller';
import type { TaskLaunchRequest } from '@/features/coreTaskSurface/contracts';

function draft(patch: Partial<AdaptivePracticeOwnerDraft> = {}): AdaptivePracticeOwnerDraft {
  return {
    ownerKey: 'practice:7:91:103:new', ownerGeneration: 7, signalVersionId: 91,
    targetEventId: 103, planId: null, answer: '', reflection: '', assessment: null,
    startInput: null, completionInput: null, resultUnknown: false, pendingOperation: null,
    ...patch,
  };
}

describe('AppShell canonical adaptive-practice sub-owner guard', () => {
  it('allows only the exact active Preparation generation and event to hand off its preserved draft to readiness practice', () => {
    let pending = false;
    let transition: ReturnType<typeof authorizeInterviewPreparationPracticeHandoff> = null;
    const controller = createCoreTaskSurfaceController({
      hasPending: () => pending,
      hasUnsavedChanges: (active) => !interviewPreparationPracticeHandoffAllowsUnsavedBypass(active, transition),
    });
    const preparation = controller.launch({
      ref: { taskId: 'application.interview_prepare', applicationId: 7, eventId: 103 },
      source: 'application_task_card',
    });
    if (preparation.kind !== 'launched') throw new Error('Preparation should launch');
    const preservedDrafts = { '7:103': { marker: 'preserve-me' } };
    const readinessRequest: TaskLaunchRequest = {
      ref: { taskId: 'interview.free_practice' }, source: 'application_task_card', focus: 'source',
      childOwnerIdentity: '91:103',
    };

    expect(authorizeInterviewPreparationPracticeHandoff(controller.getState().active, {
      ownerGeneration: preparation.generation + 1, signalVersionId: 91, targetEventId: 103,
    })).toBeNull();
    expect(authorizeInterviewPreparationPracticeHandoff(controller.getState().active, {
      ownerGeneration: preparation.generation, signalVersionId: 91, targetEventId: 104,
    })).toBeNull();
    expect(controller.launch(readinessRequest)).toMatchObject({ kind: 'replacement_denied' });

    transition = authorizeInterviewPreparationPracticeHandoff(controller.getState().active, {
      ownerGeneration: preparation.generation, signalVersionId: 91, targetEventId: 103,
    });
    pending = true;
    expect(controller.launch(readinessRequest)).toMatchObject({ kind: 'replacement_denied' });
    pending = false;
    expect(controller.launch(readinessRequest)).toMatchObject({
      kind: 'launched', generation: preparation.generation + 1,
    });
    transition = null;
    expect(preservedDrafts).toEqual({ '7:103': { marker: 'preserve-me' } });
  });

  it('wires the exact Preparation-to-practice authorization around only the readiness launch and always clears it', () => {
    const start = appShellSource.indexOf('const openReadinessPractice');
    const end = appShellSource.indexOf('const openInterviewEventEditor', start);
    const source = appShellSource.slice(start, end);
    expect(appShellSource).toContain('const preparationToPracticeTransitionRef = useRef');
    expect(appShellSource).toContain('interviewPreparationPracticeHandoffAllowsUnsavedBypass(active, preparationToPracticeTransitionRef.current)');
    expect(source).toContain('authorizeInterviewPreparationPracticeHandoff(active, focus)');
    expect(source).toMatch(/try\s*\{[\s\S]*launchAdaptivePracticeOwner\(focus/);
    expect(source).toMatch(/finally\s*\{[\s\S]*preparationToPracticeTransitionRef\.current = null/);
    expect(source).not.toContain('setInterviewPreparationDrafts');
    expect(source).not.toContain('setInterviewPreparationSelection(null)');
  });

  it('denies focused-to-three-mode and exact-pair replacement for immediate pending, unknown or unsaved drafts', () => {
    const currentFocus = { ownerGeneration: 7, signalVersionId: 91, targetEventId: 103 };
    for (const guarded of [
      draft({ pendingOperation: 'start', startInput: { readiness_signal_version_id: 91, target_application_event_id: 103, expected_source_fingerprint: 'source', expected_target_fingerprint: 'target', idempotency_key: 'key' } }),
      draft({ resultUnknown: true }),
      draft({ answer: '未保存的回答', planId: 8 }),
    ]) {
      expect(adaptivePracticeSubOwnerReplacementDenied({ currentFocus, nextFocus: undefined, drafts: { [guarded.ownerKey]: guarded }, ownerGeneration: 7, surfaceGuard: { pending: false, unsaved: false } })).toBe(true);
      expect(adaptivePracticeSubOwnerReplacementDenied({ currentFocus, nextFocus: { ownerGeneration: 7, signalVersionId: 92, targetEventId: 104 }, drafts: { [guarded.ownerKey]: guarded }, ownerGeneration: 7, surfaceGuard: { pending: false, unsaved: false } })).toBe(true);
    }
  });

  it('allows same identity and a settled new scope while both canonical openers share the guard', () => {
    const focus = { ownerGeneration: 7, signalVersionId: 91, targetEventId: 103 };
    expect(adaptivePracticeDraftGuard({}, 7)).toEqual({ pending: false, unsaved: false });
    expect(adaptivePracticeSubOwnerReplacementDenied({ currentFocus: focus, nextFocus: focus, drafts: { x: draft({ resultUnknown: true }) }, ownerGeneration: 7, surfaceGuard: { pending: true, unsaved: true } })).toBe(false);
    expect(adaptivePracticeSubOwnerReplacementDenied({ currentFocus: focus, nextFocus: undefined, drafts: {}, ownerGeneration: 7, surfaceGuard: { pending: false, unsaved: false } })).toBe(false);
    expect(appShellSource).toMatch(/const openFreePractice[\s\S]*launchAdaptivePracticeOwner\(undefined/);
    expect(appShellSource).toMatch(/const openReadinessPractice[\s\S]*launchAdaptivePracticeOwner\(focus/);
  });

  it('retains the focused owner guard on same-identity duplicate launches, including three-mode owners', () => {
    const guardedDraft = draft({ resultUnknown: true, pendingOperation: 'complete' });
    expect(adaptivePracticeGuardAfterOwnerLaunch({
      launchKind: 'focused_existing', drafts: { [guardedDraft.ownerKey]: guardedDraft },
      ownerGeneration: 7, surfaceGuard: { pending: false, unsaved: false },
    })).toEqual({ pending: true, unsaved: false });
    expect(adaptivePracticeGuardAfterOwnerLaunch({
      launchKind: 'focused_existing', drafts: {}, ownerGeneration: 7,
      surfaceGuard: { pending: true, unsaved: true },
    })).toEqual({ pending: true, unsaved: true });
    expect(adaptivePracticeGuardAfterOwnerLaunch({
      launchKind: 'launched', drafts: { [guardedDraft.ownerKey]: guardedDraft },
      ownerGeneration: 8, surfaceGuard: { pending: true, unsaved: true },
    })).toEqual({ pending: false, unsaved: false });
    expect(appShellSource).not.toMatch(/setAdaptivePracticeFocus\(nextFocus\);\s*taskSurfaceGuardRef\.current = \{ pending: false, unsaved: false \}/);
  });

  it('restores the original exact-child guard after closed ordinary and other-exact round trips', () => {
    const controller = createCoreTaskSurfaceController();
    const launch = (identity: string) => controller.launch({
      ref: { taskId: 'interview.free_practice' }, source: 'application_task_card', focus: 'source',
      childOwnerIdentity: identity,
    } as TaskLaunchRequest);
    const original = launch('91:103');
    if (original.kind !== 'launched') throw new Error('original launch failed');
    const unknown = draft({
      ownerGeneration: original.generation,
      ownerKey: `practice:${original.generation}:91:103:new`,
      resultUnknown: true,
      pendingOperation: 'start',
      startInput: {
        readiness_signal_version_id: 91, target_application_event_id: 103,
        expected_source_fingerprint: 'source-91', expected_target_fingerprint: 'event-103',
        idempotency_key: 'uuid-original',
      },
    });
    controller.close(original.generation, 'preserve'); controller.markClosed(original.generation);
    const ordinary = launch('three-mode');
    if (ordinary.kind !== 'launched') throw new Error('ordinary launch failed');
    controller.close(ordinary.generation); controller.markClosed(ordinary.generation);
    const other = launch('92:104');
    if (other.kind !== 'launched') throw new Error('other launch failed');
    controller.close(other.generation); controller.markClosed(other.generation);
    const returned = launch('91:103');
    if (returned.kind !== 'launched') throw new Error('return launch failed');

    expect(controller.getState().active?.recoveryGeneration).toBe(original.generation);
    expect(adaptivePracticeGuardAfterOwnerLaunch({
      launchKind: returned.kind,
      drafts: { [unknown.ownerKey]: unknown },
      ownerGeneration: returned.generation,
      recoveryOwnerGeneration: controller.getState().active?.recoveryGeneration,
      nextFocus: { ownerGeneration: returned.generation, signalVersionId: 91, targetEventId: 103 },
      surfaceGuard: { pending: false, unsaved: false },
    })).toEqual({ pending: true, unsaved: true });
    expect(appShellSource).toContain('childOwnerIdentity: adaptivePracticeOwnerIdentity(nextFocus)');
  });

  it('explicitly settles a recovered controller certificate only after its owner guard clears', () => {
    const controller = createCoreTaskSurfaceController();
    const launch = () => controller.launch({
      ref: { taskId: 'interview.free_practice' }, source: 'application_task_card', focus: 'source',
      childOwnerIdentity: '91:103',
    });
    const original = launch();
    if (original.kind !== 'launched') throw new Error('original launch failed');
    controller.close(original.generation, 'preserve'); controller.markClosed(original.generation);
    const recovered = launch();
    if (recovered.kind !== 'launched') throw new Error('recovery launch failed');
    expect(controller.getState().active?.recoveryGeneration).toBe(original.generation);

    settleRecoveredCoreTaskAfterGuardTransition(controller, { pending: true, unsaved: true }, { pending: false, unsaved: true });
    expect(controller.getState().active?.recoveryGeneration).toBe(original.generation);
    settleRecoveredCoreTaskAfterGuardTransition(controller, { pending: false, unsaved: true }, { pending: false, unsaved: false });
    controller.close(recovered.generation); controller.markClosed(recovered.generation);
    const afterSettlement = launch();
    if (afterSettlement.kind !== 'launched') throw new Error('settled owner relaunch failed');
    expect(controller.getState().active?.recoveryGeneration).toBeNull();
  });

  it('settles an unsaved-only recovery certificate only when the exact draft is saved or discarded', () => {
    const controller = createCoreTaskSurfaceController();
    const request = { ref: { taskId: 'interview.free_practice' as const }, source: 'application_task_card' as const, childOwnerIdentity: '91:103' };
    const original = controller.launch(request);
    if (original.kind !== 'launched') throw new Error('original launch failed');
    closeCoreTaskOwnerWithGuard(controller, { pending: false, unsaved: true });
    const recovered = controller.launch(request);
    if (recovered.kind !== 'launched') throw new Error('recovery launch failed');
    expect(controller.getState().active?.recoveryGeneration).toBe(original.generation);

    settleRecoveredCoreTaskAfterGuardTransition(controller, { pending: false, unsaved: true }, { pending: false, unsaved: false });
    closeCoreTaskOwnerWithGuard(controller, { pending: false, unsaved: false });
    const afterSettlement = controller.launch(request);
    if (afterSettlement.kind !== 'launched') throw new Error('settled owner relaunch failed');
    expect(controller.getState().active?.recoveryGeneration).toBeNull();
  });

  it('keeps many unsaved-only owner lifecycles bounded after explicit draft settlement', () => {
    const controller = createCoreTaskSurfaceController();
    for (let index = 0; index < 70; index += 1) {
      const request = {
        ref: { taskId: 'interview.free_practice' as const }, source: 'application_task_card' as const,
        childOwnerIdentity: `signal:${index}:event:${index}`,
      };
      const original = controller.launch(request);
      if (original.kind !== 'launched') throw new Error(`owner ${index} should launch`);
      closeCoreTaskOwnerWithGuard(controller, { pending: false, unsaved: true });
      const recovered = controller.launch(request);
      if (recovered.kind !== 'launched') throw new Error(`owner ${index} should recover`);
      expect(controller.getState().active?.recoveryGeneration).toBe(original.generation);
      settleRecoveredCoreTaskAfterGuardTransition(controller, { pending: false, unsaved: true }, { pending: false, unsaved: false });
      closeCoreTaskOwnerWithGuard(controller, { pending: false, unsaved: false });
      const settled = controller.launch(request);
      if (settled.kind !== 'launched') throw new Error(`owner ${index} should relaunch settled`);
      expect(controller.getState().active?.recoveryGeneration).toBeNull();
      closeCoreTaskOwnerWithGuard(controller, { pending: false, unsaved: false });
    }
  });

  it('atomically replaces and clears exact Review recovery lineages without cross-owner deletion', () => {
    const old = {
      ownerKey: 'review:1:7:11', ownerGeneration: 1, noteId: 7, proposalId: 11, applicationId: 3,
      selectedFocusId: 'focus-1', userNote: 'sensitive body', idempotencyKey: 'uuid-1',
      frozenProposalInput: { proposal_id: 11, focus_id: 'focus-1', expected_note_revision: 4, expected_candidate_fingerprint: 'fingerprint', idempotency_key: 'uuid-1', user_note: 'sensitive body' },
      proposalUnknown: true, actionDraft: null,
    };
    const other = {
      ...old, ownerKey: 'review:1:8:12', noteId: 8, proposalId: 12, userNote: 'other-owner',
      idempotencyKey: 'uuid-other', frozenProposalInput: null,
    };
    let snapshot: Record<string, ReviewReadinessOwnerDraft> = { [old.ownerKey]: old, [other.ownerKey]: other };
    let previous: ReviewReadinessOwnerDraft = old;
    for (let generation = 2; generation <= 12; generation += 1) {
      const next = { ...previous, ownerKey: `review:${generation}:7:11`, ownerGeneration: generation };
      const installed = transactReviewReadinessDraftSnapshot(snapshot, next.ownerKey, next, previous.ownerKey);
      if (!installed) throw new Error('exact Review migration should install');
      snapshot = installed;
      expect(Object.keys(snapshot).filter((key) => key.endsWith(':7:11'))).toHaveLength(1);
      previous = next;
    }
    const crossOwner = { ...previous, ownerKey: 'review:13:8:12', ownerGeneration: 13, noteId: 8, proposalId: 12 };
    expect(transactReviewReadinessDraftSnapshot(snapshot, crossOwner.ownerKey, crossOwner, previous.ownerKey)).toBeNull();
    expect(snapshot[other.ownerKey]).toBe(other);
    const cleared = transactReviewReadinessDraftSnapshot(snapshot, previous.ownerKey, null);
    if (!cleared) throw new Error('Review terminal cleanup should succeed');
    expect(cleared).toEqual({ [other.ownerKey]: other });
    expect(JSON.stringify(cleared)).not.toContain('uuid-1');
    expect(JSON.stringify(cleared)).not.toContain('sensitive body');
  });

  it('routes late A proposed and terminal updates to A without changing active B guard or certificate', () => {
    const controller = createCoreTaskSurfaceController();
    const launch = (eventId: number) => controller.launch({
      ref: { taskId: 'application.interview_review', applicationId: 3, eventId }, source: 'application_task_card',
    });
    const a = launch(5);
    if (a.kind !== 'launched') throw new Error('A should launch');
    const aDraft: ReviewReadinessOwnerDraft = {
      ownerKey: `review:${a.generation}:7:11`, ownerGeneration: a.generation, noteId: 7, proposalId: 11, applicationId: 3,
      selectedFocusId: 'focus-a', userNote: 'A frozen body', idempotencyKey: 'uuid-a', frozenProposalInput: null,
      proposalUnknown: true, actionDraft: null,
    };
    controller.close(a.generation, 'preserve'); controller.markClosed(a.generation);
    const b = launch(6);
    if (b.kind !== 'launched') throw new Error('B should launch');
    const bDraft: ReviewReadinessOwnerDraft = {
      ownerKey: `review:${b.generation}:8:12`, ownerGeneration: b.generation, noteId: 8, proposalId: 12, applicationId: 3,
      selectedFocusId: 'focus-b', userNote: 'B unsaved', idempotencyKey: null, frozenProposalInput: null,
      proposalUnknown: false, actionDraft: null,
    };
    let snapshot: Record<string, ReviewReadinessOwnerDraft> = { [aDraft.ownerKey]: aDraft, [bDraft.ownerKey]: bDraft };
    const lateProposed: ReviewReadinessOwnerDraft = {
      ...aDraft, proposalUnknown: false,
      actionDraft: {
        ownerKey: `${aDraft.ownerKey}:focus-a`, operationId: 'operation-a', actionCallId: 'call-a', actionName: 'save_review_readiness_signal',
        confirmationToken: 'token-a', allowedDecisions: ['approve', 'modify', 'reject'], status: 'proposed', result: null,
        originalPayload: { user_note: 'A frozen body' }, pendingDecision: null, resultUnknown: false,
      },
    };
    const proposed = authorizeReviewReadinessDraftTransaction(snapshot, lateProposed.ownerKey, lateProposed, undefined, controller.getState().active);
    if (!proposed) throw new Error('late A proposed update should route');
    snapshot = proposed.snapshot;
    expect(proposed.affectsCurrentOwner).toBe(false);
    expect(snapshot[bDraft.ownerKey]).toBe(bDraft);
    expect(isReviewReadinessDraftUnsaved(snapshot[bDraft.ownerKey])).toBe(true);
    expect(snapshot[aDraft.ownerKey]).toMatchObject({ actionDraft: { operationId: 'operation-a', confirmationToken: 'token-a' } });

    const terminal = authorizeReviewReadinessDraftTransaction(snapshot, aDraft.ownerKey, null, undefined, controller.getState().active);
    if (!terminal) throw new Error('late A terminal cleanup should route');
    snapshot = terminal.snapshot;
    expect(terminal.affectsCurrentOwner).toBe(false);
    expect(terminal.settledGeneration).toBe(a.generation);
    if (terminal.settledGeneration === null) throw new Error('late A terminal must identify A generation');
    controller.settleRecovery(terminal.settledGeneration);
    expect(snapshot[aDraft.ownerKey]).toBeUndefined();
    expect(snapshot[bDraft.ownerKey]).toBe(bDraft);
    expect(isReviewReadinessDraftUnsaved(snapshot[bDraft.ownerKey])).toBe(true);

    controller.close(b.generation, 'preserve'); controller.markClosed(b.generation);
    const reopenedB = launch(6);
    if (reopenedB.kind !== 'launched') throw new Error('B should reopen');
    expect(controller.getState().active?.recoveryGeneration).toBe(b.generation);
    controller.close(reopenedB.generation, 'preserve'); controller.markClosed(reopenedB.generation);
    const reopenedA = launch(5);
    if (reopenedA.kind !== 'launched') throw new Error('A should reopen terminal');
    expect(controller.getState().active?.recoveryGeneration).toBeNull();
  });

  it('atomically replaces and clears exact Practice recovery lineages with bounded history', () => {
    let previous = draft({ ownerKey: 'practice:1:91:103:plan:8', ownerGeneration: 1, planId: 8, answer: 'sensitive answer', reflection: 'sensitive reflection' });
    const other = draft({ ownerKey: 'practice:1:92:104:plan:9', ownerGeneration: 1, signalVersionId: 92, targetEventId: 104, planId: 9, answer: 'other-owner' });
    let snapshot = { [previous.ownerKey]: previous, [other.ownerKey]: other };
    for (let generation = 2; generation <= 12; generation += 1) {
      const next = { ...previous, ownerKey: `practice:${generation}:91:103:plan:8`, ownerGeneration: generation };
      const installed = transactAdaptivePracticeDraftSnapshot(snapshot, next.ownerKey, next, previous.ownerKey);
      if (!installed) throw new Error('exact Practice migration should install');
      snapshot = installed;
      expect(Object.keys(snapshot).filter((key) => key.includes(':91:103:'))).toHaveLength(1);
      previous = next;
    }
    const wrongChild = { ...previous, ownerKey: 'practice:13:92:104:plan:8', ownerGeneration: 13, signalVersionId: 92, targetEventId: 104 };
    expect(transactAdaptivePracticeDraftSnapshot(snapshot, wrongChild.ownerKey, wrongChild, previous.ownerKey)).toBeNull();
    const cleared = transactAdaptivePracticeDraftSnapshot(snapshot, previous.ownerKey, null);
    if (!cleared) throw new Error('Practice terminal cleanup should succeed');
    expect(cleared).toEqual({ [other.ownerKey]: other });
    expect(adaptivePracticeDraftGuard(cleared, previous.ownerGeneration)).toEqual({ pending: false, unsaved: false });
    expect(JSON.stringify(cleared)).not.toContain('sensitive answer');
  });

  it('keeps direct, chooser and application-detail closes certificate-free when their guard is clear', () => {
    const controller = createCoreTaskSurfaceController();
    const ordinary = controller.launch({
      ref: { taskId: 'application.material_kit', applicationId: 71 }, source: 'application_header', focus: 'current',
    });
    if (ordinary.kind !== 'launched') throw new Error('ordinary owner should launch');
    closeCoreTaskOwnerWithGuard(controller, { pending: false, unsaved: false });
    const reopened = controller.launch({
      ref: { taskId: 'application.material_kit', applicationId: 71 }, source: 'application_header', focus: 'current',
    });
    if (reopened.kind !== 'launched') throw new Error('ordinary owner should relaunch');
    expect(controller.getState().active?.recoveryGeneration).toBeNull();

    expect(appShellSource).toMatch(/const closeCoreTaskSurface[\s\S]*closeCoreTaskOwnerWithGuard\(coreTaskController, guardBeforeClose\)/);
    expect(appShellSource).toMatch(/const openApplicationDetail[\s\S]*closeCoreTaskOwnerWithGuard\(coreTaskController, \{ pending: false, unsaved: false \}\)/);
    expect(appShellSource).toMatch(/trustedEventId === undefined[\s\S]*closeCoreTaskOwnerWithGuard\(coreTaskController, \{ pending: false, unsaved: false \}\)/);
  });
});
