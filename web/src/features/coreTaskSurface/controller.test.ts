import { describe, expect, it, vi } from 'vitest';

import type { TaskLaunchRequest } from './contracts';
import { createCoreTaskSurfaceController, requestCoreTaskClose } from './controller';
import controllerSource from './controller.ts?raw';

const request = (applicationId: number, source: TaskLaunchRequest['source'] = 'application_header', hints?: TaskLaunchRequest['hints']): TaskLaunchRequest => ({
  ref: { taskId: 'application.material_kit', applicationId },
  source,
  focus: source === 'pilot' ? 'current' : 'overview',
  hints,
});

describe('CoreTaskSurfaceController', () => {
  it('owns generation-safe lifecycle transitions', () => {
    const controller = createCoreTaskSurfaceController();
    const notifications: string[] = [];
    controller.subscribe(() => notifications.push(controller.getState().phase));

    const opened = controller.launch(request(7));
    if (opened.kind !== 'launched') throw new Error('launch should succeed');
    expect(opened.kind).toBe('launched');
    expect(controller.getState().phase).toBe('opening');
    expect(controller.getState().generation).toBe(1);
    controller.markOpen(opened.generation);
    expect(controller.getState().phase).toBe('open');
    controller.close(opened.generation);
    expect(controller.getState().phase).toBe('closing');
    controller.markClosed(opened.generation);
    expect(controller.getState()).toMatchObject({ phase: 'closed', generation: 1, active: null });
    expect(notifications).toEqual(['opening', 'open', 'closing', 'closed']);
  });

  it('certifies only an exact close-to-relaunch owner as recovery lineage', () => {
    const controller = createCoreTaskSurfaceController();
    const first = controller.launch(request(7));
    if (first.kind !== 'launched') throw new Error('launch should succeed');
    controller.markOpen(first.generation);
    controller.close(first.generation, 'preserve');
    controller.markClosed(first.generation);

    const reopened = controller.launch(request(7));
    if (reopened.kind !== 'launched') throw new Error('relaunch should succeed');
    expect(reopened.generation).toBe(first.generation + 1);
    expect(controller.getState().active).toMatchObject({
      generation: reopened.generation,
      recoveryGeneration: first.generation,
      key: 'application.material_kit:applicationId=7',
    });

    controller.markOpen(reopened.generation);
    controller.close(reopened.generation, 'preserve');
    controller.markClosed(reopened.generation);
    const differentOwner = controller.launch(request(8));
    if (differentOwner.kind !== 'launched') throw new Error('different owner launch should succeed');
    expect(controller.getState().active).toMatchObject({
      generation: differentOwner.generation,
      recoveryGeneration: null,
      key: 'application.material_kit:applicationId=8',
    });
  });

  it('retains a closed child-owner recovery certificate across unrelated child launches', () => {
    const controller = createCoreTaskSurfaceController();
    const freePractice = (childOwnerIdentity: string): TaskLaunchRequest => ({
      ref: { taskId: 'interview.free_practice' },
      source: 'application_task_card',
      focus: 'source',
      childOwnerIdentity,
    } as TaskLaunchRequest);

    const exact = controller.launch(freePractice('91:103'));
    if (exact.kind !== 'launched') throw new Error('exact child should launch');
    controller.close(exact.generation, 'preserve');
    controller.markClosed(exact.generation);

    const ordinary = controller.launch(freePractice('three-mode'));
    if (ordinary.kind !== 'launched') throw new Error('ordinary child should launch');
    expect(controller.getState().active?.recoveryGeneration).toBeNull();
    controller.close(ordinary.generation);
    controller.markClosed(ordinary.generation);

    const otherExact = controller.launch(freePractice('92:104'));
    if (otherExact.kind !== 'launched') throw new Error('other exact child should launch');
    expect(controller.getState().active?.recoveryGeneration).toBeNull();
    controller.close(otherExact.generation);
    controller.markClosed(otherExact.generation);

    const returned = controller.launch(freePractice('91:103'));
    if (returned.kind !== 'launched') throw new Error('original exact child should relaunch');
    expect(controller.getState().active).toMatchObject({
      generation: returned.generation,
      recoveryGeneration: exact.generation,
      childOwnerIdentity: '91:103',
    });
  });

  it('never evicts an unsettled child-owner certificate after more than 64 unrelated closes', () => {
    const controller = createCoreTaskSurfaceController();
    const freePractice = (childOwnerIdentity: string): TaskLaunchRequest => ({
      ref: { taskId: 'interview.free_practice' },
      source: 'application_task_card',
      focus: 'source',
      childOwnerIdentity,
    });
    const close = (generation: number, recovery: 'discard' | 'preserve' = 'discard') => {
      controller.close(generation, recovery);
      controller.markClosed(generation);
    };

    const original = controller.launch(freePractice('91:103'));
    if (original.kind !== 'launched') throw new Error('original child should launch');
    close(original.generation, 'preserve');

    for (let index = 0; index < 70; index += 1) {
      const unrelated = controller.launch(freePractice(`other:${index}`));
      if (unrelated.kind !== 'launched') throw new Error(`unrelated child ${index} should launch`);
      expect(controller.getState().active?.recoveryGeneration).toBeNull();
      close(unrelated.generation);
    }

    const returned = controller.launch(freePractice('91:103'));
    if (returned.kind !== 'launched') throw new Error('original child should relaunch');
    expect(controller.getState().active).toMatchObject({
      childOwnerIdentity: '91:103',
      recoveryGeneration: original.generation,
    });
  });

  it('keeps an existing unknown certificate across an ordinary close until explicit cleanup', () => {
    const controller = createCoreTaskSurfaceController();
    const exact = (): TaskLaunchRequest => ({
      ref: { taskId: 'interview.free_practice' }, source: 'application_task_card', focus: 'source',
      childOwnerIdentity: '91:103',
    });
    const original = controller.launch(exact());
    if (original.kind !== 'launched') throw new Error('original child should launch');
    controller.close(original.generation, 'preserve'); controller.markClosed(original.generation);
    const recovered = controller.launch(exact());
    if (recovered.kind !== 'launched') throw new Error('unknown child should recover');
    controller.close(recovered.generation); controller.markClosed(recovered.generation);
    const returned = controller.launch(exact());
    if (returned.kind !== 'launched') throw new Error('unknown child should return');
    expect(controller.getState().active?.recoveryGeneration).toBe(original.generation);
  });

  it('revokes an existing certificate when a guard-cleared close explicitly discards recovery', () => {
    const controller = createCoreTaskSurfaceController();
    const exact = (): TaskLaunchRequest => ({
      ref: { taskId: 'interview.free_practice' }, source: 'application_task_card', focus: 'source',
      childOwnerIdentity: '91:103',
    });
    const original = controller.launch(exact());
    if (original.kind !== 'launched') throw new Error('original child should launch');
    controller.close(original.generation, 'preserve'); controller.markClosed(original.generation);
    const recovered = controller.launch(exact());
    if (recovered.kind !== 'launched') throw new Error('unknown child should recover');
    controller.close(recovered.generation, 'discard'); controller.markClosed(recovered.generation);
    const returned = controller.launch(exact());
    if (returned.kind !== 'launched') throw new Error('discarded child should return');
    expect(controller.getState().active?.recoveryGeneration).toBeNull();
  });

  it('does not retain certificates for many ordinary application and child-owner closes', () => {
    const controller = createCoreTaskSurfaceController();
    const close = (generation: number) => {
      controller.close(generation);
      controller.markClosed(generation);
    };

    for (let applicationId = 1; applicationId <= 70; applicationId += 1) {
      const opened = controller.launch(request(applicationId));
      if (opened.kind !== 'launched') throw new Error(`application ${applicationId} should launch`);
      close(opened.generation);
    }
    for (let index = 0; index < 70; index += 1) {
      const opened = controller.launch({
        ref: { taskId: 'interview.free_practice' }, source: 'application_task_card', focus: 'source',
        childOwnerIdentity: `ordinary:${index}`,
      });
      if (opened.kind !== 'launched') throw new Error(`ordinary child ${index} should launch`);
      close(opened.generation);
    }

    for (const reopen of [
      request(1),
      request(70),
      { ref: { taskId: 'interview.free_practice' }, source: 'application_task_card', focus: 'source', childOwnerIdentity: 'ordinary:0' } as const,
      { ref: { taskId: 'interview.free_practice' }, source: 'application_task_card', focus: 'source', childOwnerIdentity: 'ordinary:69' } as const,
    ]) {
      const opened = controller.launch(reopen);
      if (opened.kind !== 'launched') throw new Error('ordinary owner should relaunch');
      expect(controller.getState().active?.recoveryGeneration).toBeNull();
      close(opened.generation);
    }
  });

  it('deletes a pending recovery certificate only after explicit settlement or revocation', () => {
    const controller = createCoreTaskSurfaceController();
    const close = (generation: number, recovery: 'discard' | 'preserve' = 'discard') => {
      controller.close(generation, recovery);
      controller.markClosed(generation);
    };

    const settled = controller.launch(request(71));
    if (settled.kind !== 'launched') throw new Error('settled owner should launch');
    close(settled.generation, 'preserve');
    controller.settleRecovery(settled.generation);
    const afterSettlement = controller.launch(request(71));
    if (afterSettlement.kind !== 'launched') throw new Error('settled owner should relaunch');
    expect(controller.getState().active?.recoveryGeneration).toBeNull();
    close(afterSettlement.generation);

    controller.revokeRecovery(afterSettlement.generation);
    const afterRevocation = controller.launch(request(71));
    if (afterRevocation.kind !== 'launched') throw new Error('revoked owner should relaunch');
    expect(controller.getState().active?.recoveryGeneration).toBeNull();
  });

  it('indexes recovery cleanup by exact generation instead of scanning unrelated certificates', () => {
    const dismissBody = controllerSource.match(/const dismissRecovery\s*=\s*\([^)]*\)\s*=>\s*\{([\s\S]*?)\n\s*\};/)?.[1] ?? '';
    expect(dismissBody).toContain('recoveryCertificateKeyByGeneration.get(generation)');
    expect(dismissBody).not.toMatch(/for\s*\(|\.forEach\s*\(/);
  });

  it('tracks an in-place child focus change before issuing its close certificate', () => {
    const controller = createCoreTaskSurfaceController();
    const launch = (childOwnerIdentity: string) => controller.launch({
      ref: { taskId: 'interview.free_practice' }, source: 'application_task_card', focus: 'source', childOwnerIdentity,
    });
    const first = launch('91:103');
    if (first.kind !== 'launched') throw new Error('first child should launch');
    const focused = launch('92:104');
    expect(focused).toMatchObject({ kind: 'focused_existing', generation: first.generation });
    expect(controller.getState().active?.childOwnerIdentity).toBe('92:104');
    controller.close(first.generation);
    controller.markClosed(first.generation);
    const oldChild = launch('91:103');
    if (oldChild.kind !== 'launched') throw new Error('old child should relaunch');
    expect(controller.getState().active?.recoveryGeneration).toBeNull();
  });

  it('rejects a stale close authority after the exact child owner changes in place', () => {
    const controller = createCoreTaskSurfaceController();
    const first = controller.launch({
      ref: { taskId: 'interview.free_practice' }, source: 'application_task_card', childOwnerIdentity: '91:103',
    });
    if (first.kind !== 'launched') throw new Error('first child should launch');
    const stale = controller.getState().active!;
    const focused = controller.launch({
      ref: { taskId: 'interview.free_practice' }, source: 'application_task_card', childOwnerIdentity: '92:104',
    });
    expect(focused.kind).toBe('focused_existing');
    expect(requestCoreTaskClose(controller, stale, { pending: true, unsaved: true })).toBe(false);
    expect(controller.getState()).toMatchObject({ phase: 'opening', active: { childOwnerIdentity: '92:104' } });
  });

  it('deduplicates by canonical key and ignores source, focus and hints', () => {
    const controller = createCoreTaskSurfaceController();
    const first = controller.launch(request(7));
    if (first.kind !== 'launched') throw new Error('launch should succeed');
    const before = controller.getState();
    const duplicate = controller.launch(request(7, 'pilot', { suggestedResumeId: 99 }));
    expect(duplicate).toMatchObject({ kind: 'focused_existing', generation: first.generation });
    expect(controller.getState()).toBe(before);
  });

  it('guards replacement before changing state and ignores stale callbacks', () => {
    const guard = vi.fn(() => false);
    const controller = createCoreTaskSurfaceController({ canReplace: guard });
    const first = controller.launch(request(7));
    if (first.kind !== 'launched') throw new Error('launch should succeed');
    controller.markOpen(first.generation);
    const snapshot = controller.getState();
    expect(controller.launch(request(8))).toMatchObject({ kind: 'replacement_denied', generation: first.generation });
    expect(controller.getState()).toBe(snapshot);
    expect(guard).toHaveBeenCalledTimes(1);

    const approved = createCoreTaskSurfaceController({ canReplace: () => true });
    const a = approved.launch(request(1));
    if (a.kind !== 'launched') throw new Error('launch should succeed');
    approved.markOpen(a.generation);
    const b = approved.launch(request(2));
    if (b.kind !== 'launched') throw new Error('launch should succeed');
    expect(b).toMatchObject({ kind: 'launched', generation: 2 });
    approved.markOpen(a.generation);
    approved.close(a.generation);
    approved.markClosed(a.generation);
    expect(approved.getState()).toMatchObject({ phase: 'opening', generation: 2, active: { ref: { applicationId: 2 } } });
  });

  it('fails closed for invalid identities and unavailable owners without side effects', () => {
    const controller = createCoreTaskSurfaceController({ registry: {} });
    const listener = vi.fn();
    controller.subscribe(listener);
    expect(controller.launch({ ref: { taskId: 'application.material_kit', applicationId: 0 }, source: 'deep_link' })).toMatchObject({ kind: 'invalid', reason: 'invalid_task_identity' });
    expect(controller.launch(request(7))).toMatchObject({ kind: 'unavailable', reason: 'task_owner_unavailable' });
    expect(listener).not.toHaveBeenCalled();
  });

  it('uses a stable external-store snapshot and cleans listeners', () => {
    const controller = createCoreTaskSurfaceController();
    expect(controller.getState()).toBe(controller.getState());
    const listener = vi.fn();
    const unsubscribe = controller.subscribe(listener);
    unsubscribe();
    controller.launch(request(1));
    expect(listener).not.toHaveBeenCalled();
    controller.markOpen(999);
    expect(controller.getState().phase).toBe('opening');
  });

  it('isolates observer exceptions and reports reentrant replacement as superseded', () => {
    const observed: string[] = [];
    let reentrant: ReturnType<Controller['launch']> | undefined;
    let reentered = false;
    const controller = controllerLaunch({
      canReplace: () => true,
      onFocus: () => { throw new Error('focus observer'); },
    });
    controller.subscribe(() => {
      if (!reentered) {
        reentered = true;
        reentrant = controller.launch(request(8));
      }
      throw new Error('state observer');
    });
    controller.subscribe(() => observed.push(controller.getState().active?.key ?? 'closed'));
    const outer = controller.launch(request(7));
    expect(outer).toMatchObject({ kind: 'superseded', generation: 2 });
    expect(reentrant).toMatchObject({ kind: 'launched', generation: 2 });
    expect(controller.getState().active?.ref.applicationId).toBe(8);
    expect(observed).toEqual(['application.material_kit:applicationId=8', 'application.material_kit:applicationId=8']);

    const focusObserved: string[] = [];
    controller.subscribeFocus(() => { throw new Error('focus listener'); });
    controller.subscribeFocus((active) => focusObserved.push(active.key));
    controller.launch(request(8, 'pilot'));
    expect(focusObserved).toEqual(['application.material_kit:applicationId=8']);
  });

  it('checks pending/unsaved protection before custom replacement guard', () => {
    const guard = vi.fn(() => true);
    const controller = controllerLaunch({ hasPending: () => true, replacementGuard: guard });
    controller.launch(request(1));
    const result = controller.launch(request(2));
    expect(result).toMatchObject({ kind: 'replacement_denied' });
    expect(guard).not.toHaveBeenCalled();
  });

  it('fails closed when the request ref is a revoked Proxy', () => {
    const controller = controllerLaunch();
    const revoked = Proxy.revocable({ ref: { taskId: 'application.material_kit', applicationId: 7 }, source: 'deep_link' }, {});
    revoked.revoke();
    expect(() => controller.launch(revoked.proxy as never)).not.toThrow();
    expect(controller.getState()).toMatchObject({ phase: 'closed', generation: 0, active: null });
  });
});

type Controller = ReturnType<typeof createCoreTaskSurfaceController>;
function controllerLaunch(options: Parameters<typeof createCoreTaskSurfaceController>[0] = {}): Controller {
  return createCoreTaskSurfaceController(options);
}
