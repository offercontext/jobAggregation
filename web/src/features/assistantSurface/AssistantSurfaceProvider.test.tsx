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
});
