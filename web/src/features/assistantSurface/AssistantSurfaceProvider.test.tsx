// @vitest-environment jsdom
import { act, useEffect } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  AssistantSurfaceProvider,
  useAssistantSurface,
  usePilotConversationController,
} from './AssistantSurfaceProvider';

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | undefined;
let host: HTMLDivElement | undefined;

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
});

describe('AssistantSurfaceProvider', () => {
  it('gives Haru and Pilot the same conversation controller instance', () => {
    const seen: unknown[] = [];

    function Consumer() {
      const controller = usePilotConversationController();
      useEffect(() => { seen.push(controller); }, [controller]);
      return null;
    }

    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root?.render(
      <AssistantSurfaceProvider>
        <Consumer />
        <Consumer />
      </AssistantSurfaceProvider>,
    ));

    expect(seen).toHaveLength(2);
    expect(seen[0]).toBe(seen[1]);
  });

  it('changes presentation without replacing the controller', () => {
    const controllers: unknown[] = [];

    function Consumer() {
      const controller = usePilotConversationController();
      const surface = useAssistantSurface();
      controllers.push(controller);
      return <button type="button" onClick={surface.openPilot}>{surface.surface}</button>;
    }

    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root?.render(<AssistantSurfaceProvider><Consumer /></AssistantSurfaceProvider>));
    act(() => host?.querySelector('button')?.click());

    expect(host.querySelector('button')?.textContent).toBe('pilot_workspace');
    expect(controllers[controllers.length - 1]).toBe(controllers[0]);
  });

  it('leases at most one active request across presentation changes', () => {
    let controller: ReturnType<typeof usePilotConversationController> | undefined;

    function Consumer() {
      controller = usePilotConversationController();
      return null;
    }

    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root?.render(<AssistantSurfaceProvider><Consumer /></AssistantSurfaceProvider>));

    let first: ReturnType<NonNullable<typeof controller>['beginActiveRequest']> | undefined;
    let duplicate: ReturnType<NonNullable<typeof controller>['beginActiveRequest']> | undefined;
    act(() => {
      first = controller?.beginActiveRequest('chat');
      duplicate = controller?.beginActiveRequest('chat');
    });
    expect(first).not.toBeNull();
    expect(duplicate).toBeNull();
    act(() => controller?.stopActiveRequest({ silent: true }));
    expect(controller?.activeRequestRef.current).toBeNull();
  });

  it('pins a conversation context and restores it when that conversation becomes active again', () => {
    let controller: ReturnType<typeof usePilotConversationController> | undefined;
    function Consumer() {
      controller = usePilotConversationController();
      return null;
    }

    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root?.render(<AssistantSurfaceProvider><Consumer /></AssistantSurfaceProvider>));

    const pinned = { view: 'applications-list' as const, label: '投递列表' };
    act(() => controller?.pinConversationContext(7, pinned));
    expect(controller?.pinnedContext).toEqual(pinned);
    act(() => controller?.activateConversationContext(undefined));
    expect(controller?.pinnedContext).toBeUndefined();
    act(() => controller?.activateConversationContext(7));
    expect(controller?.pinnedContext).toEqual(pinned);
  });

  it('hydrates a persisted conversation identity before comparing it with the current page', () => {
    let controller: ReturnType<typeof usePilotConversationController> | undefined;
    function Consumer() {
      controller = usePilotConversationController();
      return null;
    }

    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root?.render(<AssistantSurfaceProvider><Consumer /></AssistantSurfaceProvider>));

    act(() => {
      controller?.setConversations([{
        id: 7,
        title: '腾讯投递',
        context_type: 'application',
        context_ref: '42',
        context_label: '腾讯 · 后端开发工程师',
        created_at: '2026-08-23T00:00:00Z',
        updated_at: '2026-08-23T00:00:00Z',
      }]);
    });
    act(() => {
      controller?.activateConversationContext(7);
      controller?.setFollowingContext({
        view: 'applications-list',
        label: '美团投递',
        entity: { kind: 'application', id: '43', label: '美团 · 后端开发工程师' },
      });
    });

    expect(controller?.pinnedContext).toMatchObject({
      view: 'applications-list',
      entity: { kind: 'application', id: '42' },
    });
    expect(controller?.contextChangeNotice).toMatchObject({
      currentConversationLabel: '腾讯 · 后端开发工程师',
      currentPageLabel: '美团 · 后端开发工程师',
    });
  });

  it('releases a ChatPanel action binding without leaving a stale closure behind', async () => {
    let controller: ReturnType<typeof usePilotConversationController> | undefined;
    function Consumer() {
      controller = usePilotConversationController();
      return null;
    }

    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root?.render(<AssistantSurfaceProvider><Consumer /></AssistantSurfaceProvider>));

    const owner = {};
    const sendMessage = vi.fn(async () => 'sent' as const);
    act(() => controller?.bindActions(owner, {
      sendMessage,
      selectConversation: async () => undefined,
      startNewChat: () => true,
      retryLastMessage: () => undefined,
      clearLastFailure: () => undefined,
      handleConfirm: async () => undefined,
      retryConfirmAction: () => undefined,
      refreshConfirmationStatus: async () => undefined,
      clearActiveContext: async () => undefined,
    }));
    await expect(controller?.sendMessage('hello')).resolves.toBe('sent');
    act(() => controller?.releaseActions(owner));
    await expect(controller?.sendMessage('hello')).resolves.toBe('ignored');
    expect(sendMessage).toHaveBeenCalledTimes(1);
  });

  it('owns one background completion notice per request and opens its original conversation', () => {
    let surface: ReturnType<typeof useAssistantSurface> | undefined;
    function Consumer() {
      surface = useAssistantSurface();
      return null;
    }

    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root?.render(<AssistantSurfaceProvider><Consumer /></AssistantSurfaceProvider>));

    act(() => surface?.reportTaskState('running', 42));
    act(() => surface?.reportReplyLifecycle({
      status: 'success',
      conversationId: 42,
      background: true,
    }));
    expect(surface?.completionNotice).toEqual({ status: 'completed', conversationId: 42 });

    act(() => surface?.dismissNotice());
    act(() => surface?.reportReplyLifecycle({
      status: 'success',
      conversationId: 42,
      background: true,
    }));
    expect(surface?.completionNotice).toBeNull();

    act(() => surface?.reportTaskState('running', 42));
    act(() => surface?.reportReplyLifecycle({
      status: 'success',
      conversationId: 42,
      background: true,
    }));
    act(() => surface?.openCompletionNotice());
    expect(surface?.surface).toBe('haru_chat');
    expect(surface?.conversationRequest?.conversationId).toBe(42);
    expect(surface?.completionNotice).toBeNull();
  });

  it('clears non-lifecycle work while preserving a terminal reply lifecycle', () => {
    let surface: ReturnType<typeof useAssistantSurface> | undefined;
    let controller: ReturnType<typeof usePilotConversationController> | undefined;
    function Consumer() {
      surface = useAssistantSurface();
      controller = usePilotConversationController();
      return null;
    }

    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root?.render(<AssistantSurfaceProvider><Consumer /></AssistantSurfaceProvider>));

    let confirmationRequest: ReturnType<NonNullable<typeof controller>['beginActiveRequest']> = null;
    act(() => { confirmationRequest = controller!.beginActiveRequest('confirmation', 42); });
    expect(surface?.taskState).toBe('running');
    act(() => { controller!.finishActiveRequest(confirmationRequest!); });
    expect(surface?.taskState).toBe('idle');

    let chatRequest: ReturnType<NonNullable<typeof controller>['beginActiveRequest']> = null;
    act(() => { chatRequest = controller!.beginActiveRequest('chat', 42); });
    act(() => surface?.reportReplyLifecycle({
      status: 'success',
      conversationId: 42,
      background: true,
    }));
    act(() => { controller!.finishActiveRequest(chatRequest!); });
    expect(surface?.taskState).toBe('idle');
    expect(surface?.completionNotice).toEqual({ status: 'completed', conversationId: 42 });
  });

  it('notifies once when confirmation work finishes in the background', async () => {
    let surface: ReturnType<typeof useAssistantSurface> | undefined;
    let controller: ReturnType<typeof usePilotConversationController> | undefined;
    function Consumer() {
      surface = useAssistantSurface();
      controller = usePilotConversationController();
      return null;
    }

    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => root?.render(<AssistantSurfaceProvider><Consumer /></AssistantSurfaceProvider>));

    let request: ReturnType<NonNullable<typeof controller>['beginActiveRequest']> = null;
    await act(async () => {
      controller?.setConversationId(42);
      request = controller?.beginActiveRequest('confirmation', 42) ?? null;
      controller?.setConfirmPhase('success');
      if (request) controller?.finishActiveRequest(request);
      await Promise.resolve();
    });

    expect(surface?.taskState).toBe('idle');
    expect(surface?.completionNotice).toEqual({ status: 'completed', conversationId: 42 });

    await act(async () => {
      surface?.dismissNotice();
      controller?.setConfirmPhase('success');
      await Promise.resolve();
    });
    expect(surface?.completionNotice).toBeNull();

    await act(async () => {
      surface?.openPilot();
      controller?.setConfirmPhase('saving');
      request = controller?.beginActiveRequest('confirmation', 42) ?? null;
      await Promise.resolve();
    });
    await act(async () => {
      controller?.setConfirmPhase('success');
      if (request) controller?.finishActiveRequest(request);
      await Promise.resolve();
    });
    expect(surface?.taskState).toBe('idle');
    expect(surface?.completionNotice).toBeNull();
  });

  it('reports a background confirmation or undo failure through the shared lifecycle', async () => {
    let surface: ReturnType<typeof useAssistantSurface> | undefined;
    let controller: ReturnType<typeof usePilotConversationController> | undefined;
    function Consumer() {
      surface = useAssistantSurface();
      controller = usePilotConversationController();
      return null;
    }

    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root?.render(<AssistantSurfaceProvider><Consumer /></AssistantSurfaceProvider>));

    let request: ReturnType<NonNullable<typeof controller>['beginActiveRequest']> = null;
    await act(async () => {
      controller?.setConversationId(73);
      request = controller?.beginActiveRequest('confirmation', 73) ?? null;
      controller?.setConfirmPhase('error');
      if (request) controller?.finishActiveRequest(request);
      await Promise.resolve();
    });

    expect(surface?.taskState).toBe('idle');
    expect(surface?.completionNotice).toEqual({ status: 'failed', conversationId: 73 });
  });

  it('reports confirmation and undo terminal states before returning to idle', () => {
    let controller: ReturnType<typeof usePilotConversationController> | undefined;
    function Consumer() {
      controller = usePilotConversationController();
      return null;
    }

    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root?.render(<AssistantSurfaceProvider><Consumer /></AssistantSurfaceProvider>));

    const completedSequence: string[] = [];
    act(() => {
      controller?.bindTaskStateReporter((state) => completedSequence.push(state));
      const request = controller?.beginActiveRequest('confirmation', 42);
      controller?.setConfirmPhase('success');
      if (request) controller?.finishActiveRequest(request);
    });
    expect(completedSequence).toEqual(['running', 'completed', 'idle']);

    const failedSequence: string[] = [];
    act(() => {
      controller?.bindTaskStateReporter((state) => failedSequence.push(state));
      const request = controller?.beginActiveRequest('undo', 73);
      controller?.setConfirmPhase('error');
      if (request) controller?.finishActiveRequest(request);
    });
    expect(failedSequence).toEqual(['running', 'failed', 'idle']);
  });

  it('compares closed context identity instead of labels and switches without clearing content', () => {
    let controller: ReturnType<typeof usePilotConversationController> | undefined;
    function Consumer() {
      controller = usePilotConversationController();
      return null;
    }

    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root?.render(<AssistantSurfaceProvider><Consumer /></AssistantSurfaceProvider>));

    const pinned = {
      view: 'applications-list' as const,
      label: '腾讯旧标签',
      entity: { kind: 'application' as const, id: '7', label: '相同显示文案' },
    };
    act(() => {
      controller?.setConversationId(99);
      controller?.setTurns([{ role: 'assistant', content: '保留消息' }]);
      controller?.setAttachments([{ kind: 'resume', id: '3', label: '保留附件' }]);
      controller?.pinConversationContext(99, pinned);
      controller?.setFollowingContext({ ...pinned, label: '腾讯新标签' });
    });
    expect(controller?.contextChangeNotice).toBeNull();

    act(() => controller?.setFollowingContext({
      ...pinned,
      entity: { ...pinned.entity, id: '8' },
    }));
    expect(controller?.contextChangeNotice).toMatchObject({
      currentConversationLabel: '相同显示文案',
      currentPageLabel: '相同显示文案',
    });

    let switched = false;
    act(() => { switched = controller?.switchToFollowingContext() ?? false; });
    expect(switched).toBe(true);
    expect(controller?.pinnedContext?.entity?.id).toBe('8');
    expect(controller?.turns).toEqual([{ role: 'assistant', content: '保留消息' }]);
    expect(controller?.attachments).toEqual([{ kind: 'resume', id: '3', label: '保留附件' }]);
    expect(controller?.contextChangeNotice).toBeNull();
  });

  it('does not replace pinned or frozen request context while running or waiting for confirmation', () => {
    let controller: ReturnType<typeof usePilotConversationController> | undefined;
    function Consumer() {
      controller = usePilotConversationController();
      return null;
    }

    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root?.render(<AssistantSurfaceProvider><Consumer /></AssistantSurfaceProvider>));

    const original = {
      view: 'applications-list' as const,
      label: '腾讯',
      entity: { kind: 'application' as const, id: '7', label: '腾讯' },
    };
    const following = {
      view: 'applications-list' as const,
      label: '美团',
      entity: { kind: 'application' as const, id: '8', label: '美团' },
    };
    act(() => {
      controller?.setConversationId(99);
      controller?.pinConversationContext(99, original);
      controller?.setFollowingContext(following);
      controller?.setRequestContextSnapshot(original);
      controller?.setLoading(true);
    });
    expect(controller?.switchToFollowingContext()).toBe(false);
    expect(controller?.pinnedContext).toEqual(original);
    expect(controller?.requestContextSnapshot).toEqual(original);

    act(() => {
      controller?.setLoading(false);
      controller?.setPending({
        tool_name: 'update_application',
        human: '更新投递',
        confirmation_token: 'token',
        args: {},
      });
    });
    expect(controller?.switchToFollowingContext()).toBe(false);
    expect(controller?.pinnedContext).toEqual(original);
    expect(controller?.requestContextSnapshot).toEqual(original);
  });
});
