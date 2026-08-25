// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ConfirmationPanel } from './ConfirmationPanel';

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
});

describe('ConfirmationPanel', () => {
  it('renders sources and delegates the child action without owning a mutation', () => {
    const onConfirm = vi.fn();
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => root?.render(
      <ConfirmationPanel
        title="确认生成面试建议"
        description="仅使用以下已选择内容。"
        sources={[{ state: 'frozen', detail: '已确认岗位描述' }]}
      >
        <button type="button" onClick={onConfirm}>确认并生成</button>
      </ConfirmationPanel>,
    ));

    expect(container.textContent).toContain('确认生成面试建议');
    expect(container.textContent).toContain('已保留当时版本');
    expect(container.textContent).toContain('已确认岗位描述');
    const button = container.querySelector('button');
    expect(button).not.toBeNull();
    act(() => button?.click());
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });
});
