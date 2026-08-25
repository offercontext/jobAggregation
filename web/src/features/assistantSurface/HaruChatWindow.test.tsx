// @vitest-environment jsdom
import { act, useEffect } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  AssistantSurfaceProvider,
  useAssistantSurface,
  usePilotConversationController,
} from './AssistantSurfaceProvider';
import HaruChatWindow from './HaruChatWindow';

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | undefined;
let host: HTMLDivElement | undefined;

function Harness({ stop, onExpand }: { stop: () => void; onExpand?: () => void }) {
  const controller = usePilotConversationController();
  const surface = useAssistantSurface();
  useEffect(() => {
    controller.setTurns([
      { role: 'user', content: '帮我看看下一步' },
      { role: 'assistant', content: '先准备项目案例。' },
    ]);
    controller.setPending({
      tool_name: 'update_application',
      human: '更新投递',
      confirmation_token: 'token',
      args: {},
    });
    controller.setFollowingContext({
      view: 'applications-list',
      label: '投递列表',
      entity: { kind: 'application', id: '9', label: '星河科技 · 前端工程师' },
    });
    controller.setAttachments([{ kind: 'resume', id: '3', label: '产品简历' }]);
    controller.activeRequestRef.current = {
      kind: 'chat',
      conversationId: 7,
      controller: { abort: stop } as unknown as AbortController,
    };
    controller.setLoading(true);
    surface.reportTaskState('running');
    surface.openHaru();
  }, []);
  return <HaruChatWindow returnFocusRef={{ current: null }} onExpand={onExpand} />;
}

function ContextHarness({
  returnFocusRef = { current: null },
  onExpand,
  anchorRect,
}: {
  returnFocusRef?: React.ComponentProps<typeof HaruChatWindow>['returnFocusRef'];
  onExpand?: React.ComponentProps<typeof HaruChatWindow>['onExpand'];
  anchorRect?: React.ComponentProps<typeof HaruChatWindow>['anchorRect'];
}) {
  const controller = usePilotConversationController();
  const surface = useAssistantSurface();
  useEffect(() => {
    controller.setConversationId(7);
    controller.setTurns([{ role: 'assistant', content: '保留这条消息' }]);
    controller.pinConversationContext(7, {
      view: 'applications-list',
      label: '腾讯投递',
      entity: { kind: 'application', id: '7', label: '腾讯 · 后端开发工程师' },
    });
    controller.setFollowingContext({
      view: 'applications-list',
      label: '美团投递',
      entity: { kind: 'application', id: '8', label: '美团 · 后端开发工程师' },
    });
    surface.openHaru();
  }, []);
  return (
    <HaruChatWindow
      returnFocusRef={returnFocusRef}
      onExpand={onExpand}
      anchorRect={anchorRect}
    />
  );
}

function FirstUseHarness() {
  const surface = useAssistantSurface();
  useEffect(() => surface.openHaru(), []);
  return <HaruChatWindow returnFocusRef={{ current: null }} />;
}

describe('HaruChatWindow', () => {
  beforeEach(() => {
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
  });

  afterEach(() => {
    act(() => root?.unmount());
    host?.remove();
  });

  it('shows shared messages and routes Pending to the full Pilot workspace', async () => {
    const onExpand = vi.fn();
    await act(async () => root?.render(
      <AssistantSurfaceProvider><Harness stop={vi.fn()} onExpand={onExpand} /></AssistantSurfaceProvider>,
    ));
    expect(host!.querySelector('[role="dialog"]')?.textContent).toContain('先准备项目案例。');
    expect(host!.textContent).toContain('星河科技 · 前端工程师 · 1 个附件');
    expect(host!.textContent).toContain('这一步会修改「星河科技 · 前端工程师」的内容');
    expect(host!.textContent).toContain('查看修改内容');

    act(() => host!.querySelector<HTMLButtonElement>('[data-testid="haru-open-pending"]')?.click());
    expect(host!.querySelector('[role="dialog"]')).toBeNull();
    expect(onExpand).toHaveBeenCalledTimes(1);
  });

  it('stops the single active request only when explicitly requested', async () => {
    const stop = vi.fn();
    await act(async () => root?.render(
      <AssistantSurfaceProvider><Harness stop={stop} /></AssistantSurfaceProvider>,
    ));
    act(() => host!.querySelector<HTMLButtonElement>('[aria-label="停止生成"]')?.click());
    act(() => host!.querySelector<HTMLButtonElement>('[aria-label="停止生成"]')?.click());
    expect(stop).toHaveBeenCalledTimes(1);
    expect(host!.querySelector('[data-task-state="running"]')).toBeNull();
    expect(host!.querySelector('[data-task-state="waiting_confirmation"]')?.textContent).toBe('等待确认');
  });

  it('shows an explicit conversation-to-page context switch without clearing messages', async () => {
    await act(async () => root?.render(
      <AssistantSurfaceProvider><ContextHarness /></AssistantSurfaceProvider>,
    ));
    expect(host!.textContent).toContain('当前会话');
    expect(host!.textContent).toContain('腾讯 · 后端开发工程师');
    expect(host!.textContent).toContain('当前页面');
    expect(host!.textContent).toContain('美团 · 后端开发工程师');
    expect(host!.textContent).toContain('保持原上下文');

    const switchButton = [...host!.querySelectorAll<HTMLButtonElement>('button')]
      .find((button) => button.textContent?.includes('切换到当前页面'));
    expect(switchButton).toBeDefined();
    act(() => switchButton!.click());
    expect(host!.textContent).not.toContain('切换到当前页面');
    expect(host!.textContent).toContain('保留这条消息');
  });

  it('positions from the real Haru anchor and recomputes within the viewport', async () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1024 });
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 900 });
    await act(async () => root?.render(
      <AssistantSurfaceProvider>
        <ContextHarness anchorRect={{ left: 884, top: 650, right: 1000, bottom: 824 }} />
      </AssistantSurfaceProvider>,
    ));
    const dialog = host!.querySelector<HTMLElement>('[role="dialog"]');
    expect(dialog?.style.left).toBe('480px');
    expect(dialog?.style.top).toBe('204px');
    expect(dialog?.getAttribute('data-expand-direction')).toBe('left-up');

    await act(async () => root?.render(
      <AssistantSurfaceProvider>
        <ContextHarness anchorRect={{ left: 24, top: 24, right: 140, bottom: 198 }} />
      </AssistantSurfaceProvider>,
    ));
    expect(dialog?.style.left).toBe('152px');
    expect(dialog?.style.top).toBe('210px');
  });

  it('returns focus after one Escape close and expands without sending a message', async () => {
    const trigger = document.createElement('button');
    document.body.appendChild(trigger);
    const focus = vi.spyOn(trigger, 'focus');
    const onExpand = vi.fn();
    await act(async () => root?.render(
      <AssistantSurfaceProvider>
        <ContextHarness returnFocusRef={{ current: trigger }} onExpand={onExpand} />
      </AssistantSurfaceProvider>,
    ));

    const expand = host!.querySelector<HTMLButtonElement>('[aria-label="展开到 Pilot 工作区"]');
    act(() => expand?.click());
    expect(onExpand).toHaveBeenCalledTimes(1);

    await act(async () => root?.render(
      <AssistantSurfaceProvider key="escape">
        <ContextHarness returnFocusRef={{ current: trigger }} />
      </AssistantSurfaceProvider>,
    ));
    const dialog = host!.querySelector<HTMLElement>('[role="dialog"]');
    act(() => dialog?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
    await act(async () => { await new Promise((resolve) => window.setTimeout(resolve, 0)); });
    expect(host!.querySelector('[role="dialog"]')).toBeNull();
    expect(focus).toHaveBeenCalledTimes(1);
    trigger.remove();
  });

  it('focuses the modeless dialog when Pending disables the composer', async () => {
    await act(async () => root?.render(
      <AssistantSurfaceProvider><Harness stop={vi.fn()} /></AssistantSurfaceProvider>,
    ));
    expect(document.activeElement).toBe(host!.querySelector('[role="dialog"]'));
  });

  it('explains the relationship between Haru and Pilot on first use', async () => {
    await act(async () => root?.render(
      <AssistantSurfaceProvider><FirstUseHarness /></AssistantSurfaceProvider>,
    ));
    expect(host!.textContent).toContain('Haru 是 Pilot 的轻量窗口');
    expect(host!.textContent).toContain('对话不会丢失');
  });
});
