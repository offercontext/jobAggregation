// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import AppShell from './AppShell';

vi.mock('@tanstack/react-query', () => ({
  useMutation: () => ({ isPending: false, mutate: vi.fn() }),
  useQuery: () => ({ data: [], isError: false, isLoading: false, isFetching: false, error: null }),
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}));
vi.mock('@dnd-kit/core', () => ({
  DndContext: (props: { children: React.ReactNode }) => <div>{props.children}</div>,
  PointerSensor: class PointerSensor {}, useSensor: () => ({}), useSensors: () => ({}),
}));
vi.mock('antd', () => {
  const Layout = Object.assign(({ hasSider: _hasSider, ...props }: any) => <div {...props}>{props.children}</div>, {
    Content: (props: any) => <main {...props}>{props.children}</main>,
  });
  return {
    Button: (props: any) => <button type="button" {...props}>{props.children}</button>,
    Layout, Spin: () => <div>loading</div>, Tabs: () => <div />,
    message: { warning: vi.fn(), success: vi.fn(), error: vi.fn() },
  };
});
vi.mock('./Sidebar', () => ({
  default: (props: { onChange: (view: string) => void }) => (
    <nav>
      <button type="button" data-testid="nav-settings" onClick={() => props.onChange('settings')}>settings</button>
      <button type="button" data-testid="nav-offers" onClick={() => props.onChange('offers')}>offers</button>
      <button type="button" data-testid="nav-pilot" onClick={() => props.onChange('pilot')}>pilot</button>
    </nav>
  ),
}));
vi.mock('./TopBar', () => ({ default: () => <div /> }));
vi.mock('./CommandPalette', () => ({ default: () => <div /> }));
vi.mock('@/components/AddApplicationForm', () => ({ default: () => <div /> }));
vi.mock('@/components/ResumeUploadModal', () => ({ default: () => <div /> }));
vi.mock('@/components/AISettingsDrawer', () => ({ default: () => <div /> }));
vi.mock('@/components/ApplicationDetail', () => ({ default: () => <div /> }));
vi.mock('@/components/KanbanBoard', () => ({ default: () => <div /> }));
vi.mock('@/components/ApplicationListView', () => ({ default: () => <div /> }));
vi.mock('@/components/CalendarView', () => ({ default: () => <div /> }));
vi.mock('@/components/KnowledgeSourcesView', () => ({ default: () => <div /> }));
vi.mock('@/components/QuestionBankView', () => ({ default: () => <div /> }));
vi.mock('@/components/OfferCenterView', () => ({
  default: (props: { onCoach: (offer: { id: number }) => void }) => (
    <button type="button" data-testid="coach-offer" onClick={() => props.onCoach({ id: 99 })}>coach</button>
  ),
}));
vi.mock('@/components/ResumeLibraryView', () => ({ default: () => <div /> }));
vi.mock('@/features/dashboard/DashboardView', () => ({ default: () => <div /> }));
vi.mock('@/features/reminders/RemindersView', () => ({ default: () => <div /> }));
vi.mock('@/components/MockInterviewDrawer', () => ({ default: () => <div /> }));
vi.mock('@/components/OfferNegotiationDrawer', () => ({ default: () => <div /> }));
vi.mock('@/components/InterviewV01View', () => ({ default: () => <div /> }));
vi.mock('@/components/InterviewStoryLibraryView', () => ({ default: () => <div /> }));
vi.mock('@/components/InterviewStoryDrawer', () => ({
  createInterviewStoryDraft: () => ({}), default: () => <div />,
}));
vi.mock('@/features/pilot/PilotAttachmentContext', () => ({
  PilotAttachmentProvider: (props: { children: React.ReactNode }) => <>{props.children}</>,
  usePilotAttachmentStore: () => ({ addAttachment: vi.fn(), createNewDraftWithAttachment: vi.fn() }),
}));
vi.mock('@/features/pilot/attachmentHandoff', () => ({ retainPilotAttachmentKey: (_current: unknown, next: unknown) => next }));
vi.mock('@/features/pilot/PilotOpportunityFitCard', () => ({ default: () => <div /> }));
vi.mock('@/features/pilot/PilotOpportunityFitV2Card', () => ({ default: () => <div /> }));
vi.mock('@/features/pilotMascot/PilotMascot', () => ({
  default: (props: {
    onTogglePilot: () => void;
    onHide: () => void;
    onZoomChange: (zoom: number) => void;
    panelOpen: boolean;
    placement?: string;
    notification?: { status: string; conversationId?: number } | null;
    zoom: number;
    animationLevel?: string;
    positionResetToken?: number;
  }) => (
    <section
      data-testid="pilot-mascot"
      data-panel-open={String(props.panelOpen)}
      data-placement={props.placement}
      data-notification={props.notification?.status}
      data-zoom={props.zoom}
      data-animation-level={props.animationLevel}
      data-position-reset-token={props.positionResetToken}
    >
      <button type="button" data-testid="toggle-mascot-pilot" onClick={props.onTogglePilot}>toggle</button>
      <button type="button" data-testid="hide-mascot" onClick={props.onHide}>hide</button>
      <button type="button" data-testid="zoom-mascot" onClick={() => props.onZoomChange(1.2)}>zoom</button>
    </section>
  ),
}));
vi.mock('@/components/ChatPanel', async () => {
  const assistantSurface = await vi.importActual<typeof import('@/features/assistantSurface/AssistantSurfaceProvider')>(
    '@/features/assistantSurface/AssistantSurfaceProvider',
  );
  return { default: (props: {
    variant?: string;
    open?: boolean;
    onClose?: () => void;
    onExpand?: () => void;
    onActivityChange?: (activity: string) => void;
    onReplyLifecycle?: (event: { status: 'success'; conversationId: number; background: boolean }) => void;
    conversationRequest?: { requestKey: number; conversationId: number };
    onboardingFocusToken?: number;
    controllerActive?: boolean;
    offerId?: number;
  }) => {
    const controller = assistantSurface.usePilotConversationController();
    return (
      <section
        data-testid={props.variant === 'rail' ? 'pilot-rail-chat' : props.variant === 'page' ? 'pilot-page-chat' : 'pilot-drawer-chat'}
        data-open={String(props.open)}
        data-conversation-request={props.conversationRequest?.conversationId}
        data-onboarding-focus-token={props.onboardingFocusToken}
        data-controller-active={String(props.controllerActive)}
        data-offer-id={props.offerId}
      >
        {props.variant !== 'rail' ? <button type="button" data-testid="close-pilot" onClick={props.onClose}>close</button> : null}
        {props.variant === 'rail' ? <button type="button" data-testid="expand-pilot-rail" onClick={props.onExpand}>expand</button> : null}
        <button
          type="button"
          data-testid="begin-active-request"
          onClick={() => controller.beginActiveRequest('chat', 99)}
        >begin</button>
        <button
          type="button"
          data-testid="finish-active-request"
          onClick={() => {
            const request = controller.activeRequestRef.current;
            if (request) controller.finishActiveRequest(request);
          }}
        >finish</button>
        <button
          type="button"
          data-testid="hydrate-pending"
          onClick={() => {
            controller.activePendingRef.current = {
              tool_name: 'update_application',
              human: '恢复的待确认更新',
              confirmation_token: 'hydrated-pending-token',
              args: {},
            };
          }}
        >hydrate pending</button>
        <button
          type="button"
          data-testid="clear-hydrated-pending"
          onClick={() => { controller.activePendingRef.current = null; }}
        >clear pending</button>
        <button
          type="button"
          data-testid="set-pending"
          onClick={() => controller.setPending({
            tool_name: 'update_application',
            human: '更新投递',
            confirmation_token: 'pending-token',
            args: {},
          })}
        >pending</button>
        {controller.pending ? (
          <div role="group" aria-label="AI 修改提议">
            <button type="button" data-testid="pending-action">confirm</button>
          </div>
        ) : null}
        <button
          type="button"
          data-testid={`complete-background-${props.variant}`}
          onClick={() => {
            props.onActivityChange?.('thinking');
            props.onClose?.();
            props.onReplyLifecycle?.({ status: 'success', conversationId: 418, background: true });
            props.onActivityChange?.('idle');
          }}
        >complete</button>
      </section>
    );
  } };
});
vi.mock('@/components/SettingsView', () => ({
  default: (props: {
    onPilotMascotVisibleChange: (visible: boolean) => void;
    onPilotMascotAnimationLevelChange: (level: 'minimal') => void;
    onPilotMascotResetPosition: () => void;
    systemReducedMotion: boolean;
  }) => (
    <section data-system-reduced-motion={String(props.systemReducedMotion)}>
      <button type="button" data-testid="restore-mascot" onClick={() => props.onPilotMascotVisibleChange(true)}>restore</button>
      <button type="button" data-testid="minimal-animation" onClick={() => props.onPilotMascotAnimationLevelChange('minimal')}>minimal</button>
      <button type="button" data-testid="reset-position" onClick={props.onPilotMascotResetPosition}>reset</button>
    </section>
  ),
}));

let root: Root;
let host: HTMLDivElement;

async function flush() {
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
}

describe('AppShell Pilot mascot integration', () => {
  beforeEach(() => {
    window.history.replaceState(null, '', '/');
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    localStorage.clear();
    window.matchMedia = vi.fn().mockReturnValue({
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    });
    window.scrollTo = vi.fn();
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
  });

  afterEach(() => {
    act(() => root.unmount());
    host.remove();
    localStorage.clear();
    vi.clearAllMocks();
  });

  it('hides only Haru, preserves an open Pilot, then restores the default rail and Settings preference', async () => {
    await act(async () => root.render(<AppShell />));
    await flush();
    expect(host.querySelector('[data-testid="pilot-mascot"]')).not.toBeNull();
    expect(host.querySelector('[data-testid="pilot-rail-chat"]')).toBeNull();
    expect(host.querySelector('[data-testid="pilot-drawer-chat"]')?.getAttribute('data-controller-active')).toBe('false');

    act(() => host.querySelector<HTMLButtonElement>('[data-testid="zoom-mascot"]')?.click());
    await flush();
    expect(host.querySelector('[data-testid="pilot-mascot"]')?.getAttribute('data-zoom')).toBe('1.2');
    expect(localStorage.getItem('offerpilot:pilot-mascot-zoom')).toBe('1.2');

    act(() => host.querySelector<HTMLButtonElement>('[data-testid="toggle-mascot-pilot"]')?.click());
    await flush();
    expect(host.querySelector('[data-testid="pilot-drawer-chat"]')).not.toBeNull();
    expect(host.querySelector('[data-testid="pilot-drawer-chat"]')?.getAttribute('data-controller-active')).toBe('true');

    act(() => host.querySelector<HTMLButtonElement>('[data-testid="hide-mascot"]')?.click());
    await flush();
    expect(host.querySelector('[data-testid="pilot-mascot"]')).toBeNull();
    expect(host.querySelector('[data-testid="pilot-rail-chat"]')).not.toBeNull();

    act(() => host.querySelector<HTMLButtonElement>('[data-testid="nav-settings"]')?.click());
    await flush();
    act(() => host.querySelector<HTMLButtonElement>('[data-testid="restore-mascot"]')?.click());
    await flush();
    expect(host.querySelector('[data-testid="pilot-mascot"]')).not.toBeNull();
    expect(host.querySelector('[data-testid="pilot-rail-chat"]')).toBeNull();
    expect(host.querySelector('[data-system-reduced-motion="true"]')).not.toBeNull();

    act(() => host.querySelector<HTMLButtonElement>('[data-testid="minimal-animation"]')?.click());
    await flush();
    expect(localStorage.getItem('offerpilot:pilot-mascot-animation')).toBe('minimal');
    expect(host.querySelector('[data-testid="pilot-mascot"]')?.getAttribute('data-animation-level')).toBe('minimal');

    act(() => host.querySelector<HTMLButtonElement>('[data-testid="reset-position"]')?.click());
    await flush();
    expect(host.querySelector('[data-testid="pilot-mascot"]')?.getAttribute('data-position-reset-token')).toBe('1');
    expect(JSON.parse(localStorage.getItem('offerpilot:pilot-mascot-position') ?? '{}').normal).toEqual({
      xRatio: 0.96,
      yRatio: 0.9,
    });
  });

  it('keeps one contextual controller mounted and opens its exact completed conversation in Haru', async () => {
    await act(async () => root.render(<AppShell />));
    await flush();
    act(() => host.querySelector<HTMLButtonElement>('[data-testid="toggle-mascot-pilot"]')?.click());
    await flush();

    act(() => host.querySelector<HTMLButtonElement>('[data-testid="complete-background-drawer"]')?.click());
    await flush();
    const closedPanel = host.querySelector('[data-testid="pilot-drawer-chat"]');
    expect(closedPanel).not.toBeNull();
    expect(closedPanel?.getAttribute('data-open')).toBe('false');
    expect(closedPanel?.getAttribute('data-controller-active')).toBe('false');
    expect(host.querySelector('[data-testid="pilot-mascot"]')?.getAttribute('data-notification')).toBe('success');

    act(() => host.querySelector<HTMLButtonElement>('[data-testid="toggle-mascot-pilot"]')?.click());
    await flush();
    expect(document.querySelector('[role="dialog"][aria-label="Haru 轻量对话"]')).not.toBeNull();
    expect(host.querySelector('[data-testid="pilot-drawer-chat"]')?.getAttribute('data-conversation-request')).toBe('418');
    expect(host.querySelector('[data-testid="pilot-mascot"]')?.getAttribute('data-notification')).toBeNull();
    const sharedChatOwner = host.querySelector('[data-testid="pilot-drawer-chat"]');
    expect(sharedChatOwner?.getAttribute('data-controller-active')).toBe('true');

    act(() => document.querySelector<HTMLButtonElement>('[aria-label="展开到 Pilot 工作区"]')?.click());
    await flush();
    expect(host.querySelector('[data-testid="pilot-page-chat"]')?.getAttribute('data-conversation-request')).toBe('418');
    expect(host.querySelector('[data-testid="pilot-page-chat"]')?.getAttribute('data-onboarding-focus-token')).toBe('1');
    expect(host.querySelector('[data-testid="pilot-page-chat"]')).toBe(sharedChatOwner);
  });

  it('keeps the Offer conversation owner and request scope while Haru expands to Pilot', async () => {
    await act(async () => root.render(<AppShell />));
    await flush();

    act(() => host.querySelector<HTMLButtonElement>('[data-testid="nav-offers"]')?.click());
    await flush();
    act(() => host.querySelector<HTMLButtonElement>('[data-testid="coach-offer"]')?.click());
    await flush();

    const sharedChatOwner = host.querySelector('[data-testid="pilot-drawer-chat"]');
    expect(sharedChatOwner?.getAttribute('data-offer-id')).toBe('99');
    expect(sharedChatOwner?.getAttribute('data-controller-active')).toBe('true');

    act(() => document.querySelector<HTMLButtonElement>('[aria-label="展开到 Pilot 工作区"]')?.click());
    await flush();
    const pageOwner = host.querySelector('[data-testid="pilot-page-chat"]');
    expect(pageOwner).toBe(sharedChatOwner);
    expect(pageOwner?.getAttribute('data-offer-id')).toBe('99');

    act(() => host.querySelector<HTMLButtonElement>('[data-testid="hydrate-pending"]')?.click());
    act(() => host.querySelector<HTMLButtonElement>('[data-testid="close-pilot"]')?.click());
    await flush();
    expect(pageOwner?.getAttribute('data-offer-id')).toBe('99');
    act(() => host.querySelector<HTMLButtonElement>('[data-testid="clear-hydrated-pending"]')?.click());

    act(() => host.querySelector<HTMLButtonElement>('[data-testid="begin-active-request"]')?.click());
    await flush();
    act(() => host.querySelector<HTMLButtonElement>('[data-testid="close-pilot"]')?.click());
    await flush();
    expect(pageOwner?.getAttribute('data-offer-id')).toBe('99');

    act(() => host.querySelector<HTMLButtonElement>('[data-testid="finish-active-request"]')?.click());
    await flush();
    act(() => host.querySelector<HTMLButtonElement>('[data-testid="close-pilot"]')?.click());
    await flush();
    expect(pageOwner?.getAttribute('data-offer-id')).toBeNull();
  });

  it('focuses the first pending action inside the stable Pilot host', async () => {
    await act(async () => root.render(<AppShell />));
    await flush();
    act(() => host.querySelector<HTMLButtonElement>('[data-testid="nav-pilot"]')?.click());
    await flush();

    act(() => host.querySelector<HTMLButtonElement>('[data-testid="set-pending"]')?.click());
    await act(async () => { await new Promise((resolve) => window.setTimeout(resolve, 0)); });

    expect(document.activeElement).toBe(host.querySelector('[data-testid="pending-action"]'));
  });

  it('hands rail expansion focus to the stable Pilot page owner', async () => {
    await act(async () => root.render(<AppShell />));
    await flush();
    act(() => host.querySelector<HTMLButtonElement>('[data-testid="hide-mascot"]')?.click());
    await flush();

    const sharedChatOwner = host.querySelector('[data-testid="pilot-rail-chat"]');
    act(() => host.querySelector<HTMLButtonElement>('[data-testid="expand-pilot-rail"]')?.click());
    await flush();

    const pageOwner = host.querySelector('[data-testid="pilot-page-chat"]');
    expect(pageOwner).toBe(sharedChatOwner);
    expect(pageOwner?.getAttribute('data-onboarding-focus-token')).toBe('1');
  });

  it('hides Haru on the top-level Pilot page while preserving the old workspace route', async () => {
    await act(async () => root.render(<AppShell />));
    await flush();
    act(() => host.querySelector<HTMLButtonElement>('[data-testid="nav-pilot"]')?.click());
    await flush();

    expect(host.querySelector('[data-testid="pilot-page-chat"]')).not.toBeNull();
    expect(host.querySelector('[data-testid="pilot-mascot"]')).toBeNull();
    expect(host.querySelector('.op-app-main-pilot')).not.toBeNull();
    expect(host.querySelector('.op-app-content-pilot')).not.toBeNull();
    expect(host.querySelector('.op-pilot-page-layout')).not.toBeNull();
  });
});
