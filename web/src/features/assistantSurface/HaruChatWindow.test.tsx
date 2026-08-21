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

function Harness({ stop }: { stop: () => void }) {
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
    await act(async () => root?.render(
      <AssistantSurfaceProvider><Harness stop={vi.fn()} /></AssistantSurfaceProvider>,
    ));
    expect(host!.querySelector('[role="dialog"]')?.textContent).toContain('先准备项目案例。');
    expect(host!.textContent).toContain('星河科技 · 前端工程师 · 1 个附件');
    expect(host!.textContent).toContain('有一项操作等你确认');

    act(() => host!.querySelector<HTMLButtonElement>('[data-testid="haru-open-pending"]')?.click());
    expect(host!.querySelector('[role="dialog"]')).toBeNull();
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
});
