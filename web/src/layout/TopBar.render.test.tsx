// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import TopBar from './TopBar';

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe('TopBar detail presentation', () => {
  it('keeps search and settings reachable without the greeting in compact mode', () => {
    const onSearch = vi.fn();
    const onSettings = vi.fn();
    act(() => root.render(<TopBar compact onSearch={onSearch} onOpenSettings={onSettings} />));
    expect(container.textContent).not.toMatch(/早上好|下午好|晚上好|夜深了/);
    const search = [...container.querySelectorAll('button')].find((button) => button.textContent?.includes('快速打开'));
    act(() => search?.click());
    act(() => container.querySelector<HTMLButtonElement>('button[aria-label="设置"]')?.click());
    expect(onSearch).toHaveBeenCalledOnce();
    expect(onSettings).toHaveBeenCalledOnce();
  });

  it('keeps the existing greeting outside the compact detail mode', () => {
    act(() => root.render(<TopBar onSearch={vi.fn()} onOpenSettings={vi.fn()} />));
    expect(container.textContent).toMatch(/早上好|下午好|晚上好|夜深了/);
  });
});
