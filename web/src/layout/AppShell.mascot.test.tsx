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
vi.mock('@/components/OfferCenterView', () => ({ default: () => <div /> }));
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
  }) => (
    <section
      data-testid="pilot-mascot"
      data-panel-open={String(props.panelOpen)}
      data-placement={props.placement}
      data-notification={props.notification?.status}
      data-zoom={props.zoom}
    >
      <button type="button" data-testid="toggle-mascot-pilot" onClick={props.onTogglePilot}>toggle</button>
      <button type="button" data-testid="hide-mascot" onClick={props.onHide}>hide</button>
      <button type="button" data-testid="zoom-mascot" onClick={() => props.onZoomChange(1.2)}>zoom</button>
    </section>
  ),
}));
vi.mock('@/components/ChatPanel', () => ({
  default: (props: {
    variant?: string;
    open?: boolean;
    onClose?: () => void;
    onActivityChange?: (activity: string) => void;
    onReplyLifecycle?: (event: { status: 'success'; conversationId: number; background: boolean }) => void;
    conversationRequest?: { requestKey: number; conversationId: number };
  }) => (
    <section
      data-testid={props.variant === 'rail' ? 'pilot-rail-chat' : props.variant === 'page' ? 'pilot-page-chat' : 'pilot-drawer-chat'}
      data-open={String(props.open)}
      data-conversation-request={props.conversationRequest?.conversationId}
    >
      {props.variant !== 'rail' ? <button type="button" data-testid="close-pilot" onClick={props.onClose}>close</button> : null}
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
  ),
}));
vi.mock('@/components/SettingsView', () => ({
  default: (props: { onPilotMascotVisibleChange: (visible: boolean) => void }) => (
    <button type="button" data-testid="restore-mascot" onClick={() => props.onPilotMascotVisibleChange(true)}>restore</button>
  ),
}));

let root: Root;
let host: HTMLDivElement;

async function flush() {
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
}

describe('AppShell Pilot mascot integration', () => {
  beforeEach(() => {
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

    act(() => host.querySelector<HTMLButtonElement>('[data-testid="zoom-mascot"]')?.click());
    await flush();
    expect(host.querySelector('[data-testid="pilot-mascot"]')?.getAttribute('data-zoom')).toBe('1.2');
    expect(localStorage.getItem('offerpilot:pilot-mascot-zoom')).toBe('1.2');

    act(() => host.querySelector<HTMLButtonElement>('[data-testid="toggle-mascot-pilot"]')?.click());
    await flush();
    expect(host.querySelector('[data-testid="pilot-drawer-chat"]')).not.toBeNull();

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
    expect(host.querySelector('[data-testid="pilot-mascot"]')?.getAttribute('data-notification')).toBe('success');

    act(() => host.querySelector<HTMLButtonElement>('[data-testid="toggle-mascot-pilot"]')?.click());
    await flush();
    expect(document.querySelector('[role="dialog"][aria-label="Haru 轻量对话"]')).not.toBeNull();
    expect(host.querySelector('[data-testid="pilot-drawer-chat"]')?.getAttribute('data-conversation-request')).toBe('418');
    expect(host.querySelector('[data-testid="pilot-mascot"]')?.getAttribute('data-notification')).toBeNull();
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
