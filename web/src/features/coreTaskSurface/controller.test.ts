import { describe, expect, it, vi } from 'vitest';

import type { TaskLaunchRequest } from './contracts';
import { createCoreTaskSurfaceController } from './controller';

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
