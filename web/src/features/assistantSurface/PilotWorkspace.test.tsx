// @vitest-environment jsdom
import { act, useEffect } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  AssistantSurfaceProvider,
  useAssistantSurface,
  usePilotConversationController,
} from './AssistantSurfaceProvider';
import PilotWorkspace from './PilotWorkspace';

vi.mock('@/components/ChatPanel', () => ({
  default: (props: {
    controllerActive?: boolean;
    onboardingFocusToken?: number;
    onReplyLifecycle?: (event: { status: 'success' | 'error'; conversationId: number; background: boolean }) => void;
  }) => (
    <div
      data-testid="shared-pilot-chat"
      data-controller-active={String(props.controllerActive)}
      data-onboarding-focus-token={props.onboardingFocusToken}
    >
      保留共享对话
      <button
        type="button"
        data-testid="mock-reply-complete"
        onClick={() => props.onReplyLifecycle?.({ status: 'success', conversationId: 42, background: false })}
      >complete</button>
    </div>
  ),
}));

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root;
let host: HTMLDivElement;

function Harness() {
  const controller = usePilotConversationController();
  useEffect(() => {
    controller.setConversationId(7);
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
  }, []);
  return <PilotWorkspace onClose={vi.fn()} />;
}

function LifecycleHarness({
  foreground,
  onReplyLifecycle,
}: {
  foreground: boolean;
  onReplyLifecycle: (event: { status: 'success' | 'error'; conversationId: number; background: boolean }) => void;
}) {
  const surface = useAssistantSurface();
  useEffect(() => {
    if (foreground) surface.openHaru();
    else surface.closeSurface();
  }, [foreground, surface.closeSurface, surface.openHaru]);
  return (
    <PilotWorkspace
      pageActive={false}
      open={false}
      controllerActive
      onboardingFocusToken={9}
      onReplyLifecycle={onReplyLifecycle}
      onClose={vi.fn()}
    />
  );
}

describe('PilotWorkspace context switch', () => {
  beforeEach(() => {
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
  });

  afterEach(() => {
    act(() => root.unmount());
    host.remove();
  });

  it('shows the pinned conversation and following page before an explicit switch', async () => {
    await act(async () => root.render(
      <AssistantSurfaceProvider><Harness /></AssistantSurfaceProvider>,
    ));

    expect(host.textContent).toContain('当前会话');
    expect(host.textContent).toContain('腾讯 · 后端开发工程师');
    expect(host.textContent).toContain('当前页面');
    expect(host.textContent).toContain('美团 · 后端开发工程师');
    expect(host.textContent).toContain('保持原上下文');

    const switchButton = [...host.querySelectorAll<HTMLButtonElement>('button')]
      .find((button) => button.textContent?.includes('切换到当前页面'));
    act(() => switchButton?.click());

    expect(host.textContent).not.toContain('切换到当前页面');
    expect(host.querySelector('[data-testid="shared-pilot-chat"]')).not.toBeNull();
  });

  it('keeps the controller warm while deriving background lifecycle from the visible Surface', async () => {
    const onReplyLifecycle = vi.fn();
    await act(async () => root.render(
      <AssistantSurfaceProvider>
        <LifecycleHarness foreground={false} onReplyLifecycle={onReplyLifecycle} />
      </AssistantSurfaceProvider>,
    ));

    const chat = host.querySelector('[data-testid="shared-pilot-chat"]');
    expect(chat?.getAttribute('data-controller-active')).toBe('true');
    expect(chat?.getAttribute('data-onboarding-focus-token')).toBeNull();
    act(() => host.querySelector<HTMLButtonElement>('[data-testid="mock-reply-complete"]')?.click());
    expect(onReplyLifecycle).toHaveBeenLastCalledWith({
      status: 'success',
      conversationId: 42,
      background: true,
    });

    onReplyLifecycle.mockClear();
    await act(async () => root.render(
      <AssistantSurfaceProvider>
        <LifecycleHarness foreground onReplyLifecycle={onReplyLifecycle} />
      </AssistantSurfaceProvider>,
    ));
    act(() => host.querySelector<HTMLButtonElement>('[data-testid="mock-reply-complete"]')?.click());
    expect(onReplyLifecycle).toHaveBeenLastCalledWith({
      status: 'success',
      conversationId: 42,
      background: false,
    });
  });
});
