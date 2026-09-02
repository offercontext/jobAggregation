// @vitest-environment jsdom
import { act, StrictMode, useState } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { createCoreTaskSurfaceController, type CoreTaskLaunchResult } from './controller';
import { CoreTaskSurfaceHost } from './CoreTaskSurfaceHost';

declare global { var IS_REACT_ACT_ENVIRONMENT: boolean | undefined; }
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const roots: Root[] = [];
afterEach(() => {
  for (const root of roots.splice(0)) act(() => root.unmount());
  vi.unstubAllGlobals();
  document.body.replaceChildren();
});

const launchRequest = (applicationId: number, source: 'application_header' | 'pilot' = 'application_header', hints?: { suggestedResumeId?: number }) => ({
  ref: { taskId: 'application.material_kit' as const, applicationId },
  source,
  focus: source === 'pilot' ? 'current' as const : 'overview' as const,
  hints,
});

describe('CoreTaskSurfaceHost', () => {
  it('reveals a newly opened owner once in StrictMode, keeps focus, and restores the source', () => {
    const originalScrollIntoView = HTMLElement.prototype.scrollIntoView;
    const scrollIntoView = vi.fn();
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
      configurable: true,
      value: scrollIntoView,
    });
    try {
      const controller = createCoreTaskSurfaceController();
      const host = document.createElement('div');
      const source = document.createElement('button');
      source.textContent = '准备谈薪';
      document.body.append(source, host);
      source.focus();
      const root = createRoot(host);
      roots.push(root);
      act(() => root.render(<StrictMode><CoreTaskSurfaceHost controller={controller} sourceElement={source} revealOnOpen><Draft /></CoreTaskSurfaceHost></StrictMode>));
      const opened = requireLaunch(launch(controller, 7));
      act(() => root.render(<StrictMode><CoreTaskSurfaceHost controller={controller} sourceElement={source} revealOnOpen><Draft /></CoreTaskSurfaceHost></StrictMode>));

      expect(scrollIntoView).toHaveBeenCalledTimes(1);
      expect(scrollIntoView).toHaveBeenCalledWith({ behavior: 'smooth', block: 'start' });
      expect(document.activeElement).toBe(host.querySelector('[data-core-task-owner]'));

      act(() => { controller.launch(launchRequest(7, 'pilot')); });
      expect(controller.getState().generation).toBe(opened.generation);
      expect(scrollIntoView).toHaveBeenCalledTimes(1);

      act(() => controller.close(opened.generation));
      act(() => controller.markClosed(opened.generation));
      expect(document.activeElement).toBe(source);
    } finally {
      if (originalScrollIntoView) {
        Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
          configurable: true,
          value: originalScrollIntoView,
        });
      } else {
        delete (HTMLElement.prototype as { scrollIntoView?: unknown }).scrollIntoView;
      }
    }
  });

  it('reveals without motion when reduced motion is requested', () => {
    vi.stubGlobal('matchMedia', vi.fn(() => ({
      matches: true,
      media: '(prefers-reduced-motion: reduce)',
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })));
    const originalScrollIntoView = HTMLElement.prototype.scrollIntoView;
    const scrollIntoView = vi.fn();
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
      configurable: true,
      value: scrollIntoView,
    });
    try {
      const controller = createCoreTaskSurfaceController();
      const host = document.createElement('div');
      document.body.append(host);
      const root = createRoot(host);
      roots.push(root);
      act(() => root.render(<CoreTaskSurfaceHost controller={controller} revealOnOpen><Draft /></CoreTaskSurfaceHost>));
      launch(controller, 7);
      act(() => root.render(<CoreTaskSurfaceHost controller={controller} revealOnOpen><Draft /></CoreTaskSurfaceHost>));

      expect(scrollIntoView).toHaveBeenCalledOnce();
      expect(scrollIntoView).toHaveBeenCalledWith({ behavior: 'auto', block: 'start' });
      expect(document.activeElement).toBe(host.querySelector('[data-core-task-owner]'));
    } finally {
      if (originalScrollIntoView) {
        Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
          configurable: true,
          value: originalScrollIntoView,
        });
      } else {
        delete (HTMLElement.prototype as { scrollIntoView?: unknown }).scrollIntoView;
      }
    }
  });

  it('completes enter/exit animations, unloads once, and restores source focus', () => {
    const controller = createCoreTaskSurfaceController();
    const host = document.createElement('div');
    const source = document.createElement('button');
    source.textContent = '打开';
    document.body.append(source, host);
    source.focus();
    const root = createRoot(host);
    roots.push(root);
    act(() => root.render(<CoreTaskSurfaceHost controller={controller} sourceElement={source}><Draft /></CoreTaskSurfaceHost>));
    const opened = requireLaunch(launch(controller, 7));
    expect(controller.getState().phase).toBe('opening');
    act(() => root.render(<CoreTaskSurfaceHost controller={controller} sourceElement={source}><Draft /></CoreTaskSurfaceHost>));
    const owner = host.querySelector('[data-core-task-owner]') as HTMLElement;
    expect(owner.className).toContain('opening');
    act(() => owner.dispatchEvent(new Event('animationend', { bubbles: true })));
    expect(controller.getState().phase).toBe('open');
    expect(host.querySelector('[data-core-task-owner]')).toBe(owner);

    act(() => (host.querySelector('button[aria-label="关闭任务"]') as HTMLButtonElement).click());
    expect(controller.getState().phase).toBe('closing');
    expect(host.querySelector('[data-core-task-owner]')).toBe(owner);
    expect(owner.className).toContain('closing');
    act(() => owner.dispatchEvent(new Event('animationend', { bubbles: true })));
    expect(controller.getState()).toMatchObject({ phase: 'closed', generation: opened.generation, active: null });
    expect(host.querySelector('[data-core-task-owner]')).toBeNull();
    expect(document.activeElement).toBe(source);
  });

  it('uses Escape, and duplicate launch focuses the same owner without resetting a real draft', () => {
    const controller = createCoreTaskSurfaceController();
    const host = document.createElement('div');
    const source = document.createElement('button');
    document.body.append(source, host);
    const root = createRoot(host);
    roots.push(root);
    act(() => root.render(<StrictMode><CoreTaskSurfaceHost controller={controller} sourceElement={source}><Draft /></CoreTaskSurfaceHost></StrictMode>));
    const first = requireLaunch(launch(controller, 7));
    act(() => root.render(<StrictMode><CoreTaskSurfaceHost controller={controller} sourceElement={source}><Draft /></CoreTaskSurfaceHost></StrictMode>));
    const owner = host.querySelector('[data-core-task-owner]') as HTMLElement;
    const input = host.querySelector('input') as HTMLInputElement;
    input.value = '真实输入';
    source.focus();
    const duplicate = launch(controller, 7, 'pilot', { suggestedResumeId: 99 });
    expect(duplicate).toMatchObject({ kind: 'focused_existing', generation: first.generation });
    expect(host.querySelector('[data-core-task-owner]')).toBe(owner);
    expect((host.querySelector('input') as HTMLInputElement).value).toBe('真实输入');
    expect(document.activeElement).toBe(owner);

    act(() => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
    expect(controller.getState().phase).toBe('closing');
  });

  it.each([
    ['button', (host: HTMLElement) => (host.querySelector('button[aria-label="关闭任务"]') as HTMLButtonElement).click()],
    ['Escape', () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))],
  ])('uses the exact guard-aware close authority for the real %s close path', (_label, closeOwner) => {
    const controller = createCoreTaskSurfaceController();
    const host = document.createElement('div');
    document.body.append(host);
    const root = createRoot(host);
    roots.push(root);
    const request = {
      ref: { taskId: 'interview.free_practice' as const },
      source: 'application_task_card' as const,
      childOwnerIdentity: '91:103',
    };
    act(() => root.render(<CoreTaskSurfaceHost controller={controller} closeGuard={() => ({ pending: true, unsaved: true })} />));
    let firstResult: ReturnType<typeof controller.launch> | undefined;
    act(() => { firstResult = controller.launch(request); });
    const first = requireLaunch(firstResult);
    act(() => root.render(<CoreTaskSurfaceHost controller={controller} closeGuard={() => ({ pending: true, unsaved: true })} />));
    const owner = host.querySelector('[data-core-task-owner]') as HTMLElement;
    act(() => owner.dispatchEvent(new Event('animationend', { bubbles: true })));
    act(() => closeOwner(host));
    expect(controller.getState().phase).toBe('closing');
    act(() => owner.dispatchEvent(new Event('animationend', { bubbles: true })));

    let reopened: ReturnType<typeof controller.launch> | undefined;
    act(() => { reopened = controller.launch(request); });
    if (!reopened) throw new Error('relaunch should produce a result');
    if (reopened.kind !== 'launched') throw new Error('relaunch should succeed');
    expect(controller.getState().active).toMatchObject({
      recoveryGeneration: first.generation,
      childOwnerIdentity: '91:103',
    });
  });

  it('finishes opening and closing deterministically when reduced motion is requested', () => {
    vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: true, media: '', onchange: null, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() })));
    const controller = createCoreTaskSurfaceController();
    const host = document.createElement('div');
    document.body.append(host);
    const root = createRoot(host);
    roots.push(root);
    act(() => root.render(<CoreTaskSurfaceHost controller={controller} />));
    const opened = requireLaunch(launch(controller, 3));
    act(() => root.render(<CoreTaskSurfaceHost controller={controller} />));
    expect(controller.getState().phase).toBe('open');
    act(() => controller.close(opened.generation));
    expect(controller.getState().phase).toBe('closed');
    expect(host.querySelector('[data-core-task-owner]')).toBeNull();
  });

  it('reacts to a runtime reduced-motion change and cleans the media listener', () => {
    let matches = false;
    let change: ((event: MediaQueryListEvent) => void) | undefined;
    const remove = vi.fn();
    const media = {
      get matches() { return matches; },
      media: '(prefers-reduced-motion: reduce)',
      onchange: null,
      addEventListener: vi.fn((_type: string, listener: (event: MediaQueryListEvent) => void) => { change = listener; }),
      removeEventListener: remove,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    } as unknown as MediaQueryList;
    vi.stubGlobal('matchMedia', vi.fn(() => media));
    const controller = createCoreTaskSurfaceController();
    const host = document.createElement('div');
    document.body.append(host);
    const root = createRoot(host);
    roots.push(root);
    act(() => root.render(<CoreTaskSurfaceHost controller={controller} />));
    const opened = requireLaunch(launch(controller, 5));
    act(() => root.render(<CoreTaskSurfaceHost controller={controller} />));
    expect(controller.getState().phase).toBe('opening');
    matches = true;
    act(() => change?.({ matches: true } as MediaQueryListEvent));
    expect(controller.getState().phase).toBe('open');
    matches = false;
    act(() => change?.({ matches: false } as MediaQueryListEvent));
    act(() => controller.close(opened.generation));
    expect(controller.getState().phase).toBe('closing');
    matches = true;
    act(() => change?.({ matches: true } as MediaQueryListEvent));
    expect(controller.getState().phase).toBe('closed');
    act(() => root.unmount());
    expect(remove).toHaveBeenCalled();
  });

  it('captures focus and focuses an already-open owner when mounted, then restores it', () => {
    const controller = createCoreTaskSurfaceController();
    const source = document.createElement('button');
    const host = document.createElement('div');
    document.body.append(source, host);
    source.focus();
    const opened = requireLaunch(launch(controller, 6));
    act(() => controller.markOpen(opened.generation));
    const root = createRoot(host);
    roots.push(root);
    act(() => root.render(<StrictMode><CoreTaskSurfaceHost controller={controller} sourceElement={source} /></StrictMode>));
    const owner = host.querySelector('[data-core-task-owner]') as HTMLElement;
    expect(document.activeElement).toBe(owner);
    act(() => controller.close(opened.generation));
    act(() => controller.markClosed(opened.generation));
    expect(document.activeElement).toBe(source);
  });

  it('restores focus after active unmount without StrictMode probe cleanup stealing focus', async () => {
    const controller = createCoreTaskSurfaceController();
    const source = document.createElement('button');
    const host = document.createElement('div');
    document.body.append(source, host);
    source.focus();
    launch(controller, 9);
    const root = createRoot(host);
    roots.push(root);
    act(() => root.render(<StrictMode><CoreTaskSurfaceHost controller={controller} sourceElement={source} /></StrictMode>));
    expect(document.activeElement).toBe(host.querySelector('[data-core-task-owner]'));
    act(() => root.unmount());
    await act(async () => { await Promise.resolve(); });
    expect(document.activeElement).toBe(source);
  });

  it('fails safely when matchMedia throws', () => {
    vi.stubGlobal('matchMedia', vi.fn(() => { throw new Error('unsupported'); }));
    const controller = createCoreTaskSurfaceController();
    const host = document.createElement('div');
    document.body.append(host);
    const root = createRoot(host);
    roots.push(root);
    act(() => root.render(<CoreTaskSurfaceHost controller={controller} />));
    const opened = requireLaunch(launch(controller, 4));
    act(() => root.render(<CoreTaskSurfaceHost controller={controller} />));
    expect(controller.getState().phase).toBe('opening');
    act(() => controller.markOpen(opened.generation));
  });
});

function launch(controller: ReturnType<typeof createCoreTaskSurfaceController>, applicationId: number, source: 'application_header' | 'pilot' = 'application_header', hints?: { suggestedResumeId?: number }) {
  let result: ReturnType<typeof controller.launch> | undefined;
  act(() => { result = controller.launch(launchRequest(applicationId, source, hints)); });
  return result;
}

function requireLaunch(result: CoreTaskLaunchResult | undefined) {
  if (!result || result.kind !== 'launched') throw new Error('launch should succeed');
  return result;
}

function Draft() {
  const [value] = useState('initial');
  return <input aria-label="草稿" defaultValue={value} />;
}
