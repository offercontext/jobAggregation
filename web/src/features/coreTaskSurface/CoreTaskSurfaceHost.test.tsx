// @vitest-environment jsdom
import { act, StrictMode, useState } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, describe, expect, it } from 'vitest';

import { createCoreTaskSurfaceController, type CoreTaskLaunchResult } from './controller';
import { CoreTaskSurfaceHost } from './CoreTaskSurfaceHost';

declare global { var IS_REACT_ACT_ENVIRONMENT: boolean | undefined; }
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const roots: Root[] = [];
afterEach(() => { for (const root of roots.splice(0)) act(() => root.unmount()); });

describe('CoreTaskSurfaceHost', () => {
  it('renders one accessible owner and preserves its draft through StrictMode', () => {
    const controller = createCoreTaskSurfaceController();
    const host = document.createElement('div');
    const source = document.createElement('button');
    source.textContent = '打开';
    document.body.append(source, host);
    source.focus();
    const root = createRoot(host);
    roots.push(root);
    act(() => root.render(<StrictMode><CoreTaskSurfaceHost controller={controller}><Draft /></CoreTaskSurfaceHost></StrictMode>));
    let launched: ReturnType<typeof controller.launch> | undefined;
    act(() => { launched = controller.launch({ ref: { taskId: 'application.material_kit', applicationId: 7 }, source: 'application_header' }); });
    const opened = requireLaunch(launched);
    act(() => root.render(<StrictMode><CoreTaskSurfaceHost controller={controller}><Draft /></CoreTaskSurfaceHost></StrictMode>));
    expect(opened.kind).toBe('launched');
    expect(host.querySelectorAll('[data-core-task-owner]')).toHaveLength(1);
    expect(host.querySelectorAll('h2')).toHaveLength(1);
    const input = host.querySelector('input') as HTMLInputElement;
    expect(input.value).toBe('typed draft');
    expect(host.querySelector('[data-core-task-owner]')?.getAttribute('data-core-task-generation')).toBe(String(opened.generation));
    act(() => controller.markOpen(opened.generation));
    act(() => controller.close(opened.generation));
    act(() => controller.markClosed(opened.generation));
    expect(document.activeElement).toBe(source);
    source.remove();
  });

  it('closes with Escape and ignores stale animation completion', () => {
    const controller = createCoreTaskSurfaceController();
    const host = document.createElement('div');
    const root = createRoot(host);
    roots.push(root);
    act(() => root.render(<CoreTaskSurfaceHost controller={controller} />));
    let launched: ReturnType<typeof controller.launch> | undefined;
    act(() => { launched = controller.launch({ ref: { taskId: 'application.material_kit', applicationId: 1 }, source: 'deep_link' }); });
    const opened = requireLaunch(launched);
    act(() => root.render(<CoreTaskSurfaceHost controller={controller} />));
    act(() => controller.markOpen(opened.generation));
    act(() => root.render(<CoreTaskSurfaceHost controller={controller} />));
    act(() => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
    expect(controller.getState().phase).toBe('closing');
    act(() => controller.markClosed(opened.generation - 1));
    expect(controller.getState().phase).toBe('closing');
  });
});

function Draft() {
  const [value] = useState('typed draft');
  return <input aria-label="草稿" defaultValue={value} />;
}

function requireLaunch(result: CoreTaskLaunchResult | undefined) {
  if (!result || result.kind !== 'launched') throw new Error('launch should succeed');
  return result;
}
