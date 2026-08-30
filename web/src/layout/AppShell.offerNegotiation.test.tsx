// @vitest-environment jsdom
import { act, useEffect, useRef } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import AppShell from './AppShell';
import appShellSource from './AppShell.tsx?raw';
import pilotCardSource from '@/features/pilot/PilotOpportunityFitV2Card.tsx?raw';

const offer = {
  id: 42,
  application_id: 7,
  company_name: '星云数据',
  position_name: '后端工程师',
  status: 'pending',
  base_monthly: 28000,
  months_per_year: 12,
  signing_bonus: 0,
  equity: '',
  perks: '',
  deadline: '',
  notes: '',
  assessment: '',
  total_cash: 336000,
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
} as const;

const runtime = vi.hoisted(() => ({
  projection: null as { status: string; summary: string | null } | null,
  openOwner: vi.fn(),
  assistant: {
    surface: 'none' as const,
    openHaru: vi.fn(),
    openPilot: vi.fn(),
    closeSurface: vi.fn(),
    reportReplyLifecycle: vi.fn(),
    conversationRequest: null,
    consumeConversationRequest: vi.fn(),
  },
  pilot: {
    pending: false,
    loading: false,
    activeRequestRef: { current: null },
    activePendingRef: { current: null },
    confirmPhase: 'idle' as const,
    followingContext: null,
    setFollowingContext: vi.fn(),
  },
}));

const baseDraft = (goal: string) => ({
  attemptKey: `${goal}-attempt`, confirmationKey: `${goal}-confirm`, goal,
  concerns: `${goal}-顾虑`, scenario: `${goal}-场景`, resultUnknown: false,
  pendingOperation: null, proposalId: null, selectedBlocks: [], edits: {},
  dimensionIds: [], sourceFingerprint: null, previewSnapshot: null, previewInputKey: null,
});

vi.mock('@tanstack/react-query', () => ({
  useMutation: () => ({ isPending: false, mutate: vi.fn() }),
  useQuery: (options: { queryKey?: unknown[] }) => {
    const key = String(options.queryKey?.[0] ?? '');
    const data: Record<string, unknown> = {
      applications: [
        { id: 7, company_name: '星云数据', position_name: '后端工程师', applied_at: '2026-08-01T00:00:00Z' },
        { id: 8, company_name: '另一家公司', position_name: '平台工程师', applied_at: '2026-08-02T00:00:00Z' },
      ],
      events: [],
      offers: [offer],
      resumes: [{ id: 11, title: '简历' }],
      knowledge: [],
      questions: undefined,
      'application-jd-current': { current: { id: 1, application_id: 7, jd_text: 'JD text' } },
    };
    return { data: data[key], isError: false, isLoading: false, isFetching: false, error: null };
  },
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}));

vi.mock('@/services/opportunityFitReviews', () => ({
  createOpportunityFitV2Triage: vi.fn(),
  confirmOpportunityFitV2Triage: vi.fn(),
  createOpportunityFitV2DeepReview: vi.fn(),
  getOpportunityFitV2Review: vi.fn(),
  findOpportunityFitV2SourceConflictStage: vi.fn(),
  listOpportunityFitV2Reviews: vi.fn().mockResolvedValue([]),
  listOpportunityFitReviews: vi.fn().mockResolvedValue([]),
  getOpportunityFitReview: vi.fn(),
  createOpportunityFitReview: vi.fn(),
  createOpportunityFitDeepReview: vi.fn(),
}));

vi.mock('@dnd-kit/core', () => ({
  DndContext: (props: any) => <div>{props.children}</div>,
  PointerSensor: class PointerSensor {},
  useSensor: () => ({}),
  useSensors: () => ({}),
}));

vi.mock('antd', () => {
  const Layout = Object.assign((props: any) => <div {...props}>{props.children}</div>, {
    Content: (props: any) => <main {...props}>{props.children}</main>,
  });
  const Typography = {
    Paragraph: (props: any) => <p>{props.children}</p>,
    Text: (props: any) => <span>{props.children}</span>,
    Title: (props: any) => <h2>{props.children}</h2>,
  };
  return {
    Button: (props: any) => <button {...props} type="button" onClick={props.onClick}>{props.children}</button>,
    Layout,
    Spin: () => <div>loading</div>,
    Tabs: () => <div />,
    Typography,
    message: { warning: vi.fn(), success: vi.fn(), error: vi.fn() },
  };
});

vi.mock('./Sidebar', () => ({
  default: (props: any) => (
    <nav>
      <button type="button" data-testid="nav-offers" onClick={() => props.onChange('offers')}>Offers</button>
      <button type="button" data-testid="nav-pilot" onClick={() => props.onChange('pilot')}>Pilot</button>
    </nav>
  ),
}));
vi.mock('./TopBar', () => ({ default: () => <div /> }));
vi.mock('./CommandPalette', () => ({ default: () => <div /> }));
vi.mock('@/components/AddApplicationForm', () => ({ default: () => <div /> }));
vi.mock('@/components/ResumeUploadModal', () => ({ default: () => <div /> }));
vi.mock('@/components/AISettingsDrawer', () => ({ default: () => <div /> }));

vi.mock('@/components/ApplicationDetail', () => ({
  default: (props: any) => {
    const isPilotNegotiation = props.offerNegotiationEntryPoint === 'pilot';
    const offerDraft = props.offerNegotiationDrafts?.[offer.id];
    const overlayRef = useRef<HTMLElement | null>(null);
    const openerRef = useRef<HTMLElement | null>(document.activeElement as HTMLElement | null);
    useEffect(() => {
      if (props.application?.id !== 7 || isPilotNegotiation) return;
      props.onOpportunityFitProjectionChange?.({
        applicationId: props.application.id,
        status: runtime.projection?.status ?? 'ready',
        summary: runtime.projection?.summary ?? '来自唯一岗位判断 owner 的安全摘要',
        history: [],
        historyState: 'ready',
      });
    }, [isPilotNegotiation, props.application?.id]);
    useEffect(() => {
      if (!isPilotNegotiation) return;
      overlayRef.current?.querySelector<HTMLElement>('button')?.focus();
      const onKeyDown = (event: KeyboardEvent) => {
        if (event.key === 'Escape') {
          event.preventDefault();
          props.onClose?.();
          openerRef.current?.focus();
          return;
        }
        if (event.key !== 'Tab' || !overlayRef.current || !overlayRef.current.contains(document.activeElement)) return;
        const focusable = Array.from(overlayRef.current.querySelectorAll<HTMLElement>('button, [href], input, textarea, select, [tabindex]:not([tabindex="-1"])'))
          .filter((element) => !element.hasAttribute('disabled'));
        if (focusable.length === 0) return;
        const current = document.activeElement;
        const next = event.shiftKey
          ? (current === focusable[0] ? focusable[focusable.length - 1] : focusable[focusable.indexOf(current as HTMLElement) - 1])
          : (current === focusable[focusable.length - 1] ? focusable[0] : focusable[focusable.indexOf(current as HTMLElement) + 1]);
        event.preventDefault();
        next?.focus();
      };
      document.addEventListener('keydown', onKeyDown);
      return () => document.removeEventListener('keydown', onKeyDown);
    }, [isPilotNegotiation, props.onClose]);
    return (
      <section data-testid="application-detail-harness">
        <button type="button" data-testid="open-opportunity-fit" onClick={() => props.onOpenPilotOpportunityFit?.(props.application)}>
          打开岗位判断
        </button>
        {!isPilotNegotiation ? (
          <>
            <button
              type="button"
              data-testid="emit-hostile-fit-projection"
              onClick={() => props.onOpportunityFitProjectionChange?.({
                applicationId: 7,
                status: 'ready',
                summary: '恶意投影不应进入 Pilot',
                history: [{
                  internalKey: 'v2:7:1',
                  createdAt: '2026-02-30T09:00:00Z',
                  summary: '恶意历史',
                  sourceState: 'current',
                }],
                historyState: 'ready',
              })}
            >发送异常岗位判断投影</button>
            <output data-testid="ui-draft-goal">{offerDraft?.goal ?? ''}</output>
            <button type="button" data-testid="edit-ui-offer" onClick={() => props.onOfferNegotiationDraftChange?.(offer.id, baseDraft('UI 目标'))}>
              编辑 UI 谈薪准备
            </button>
          </>
        ) : (
          <section
            data-testid="offer-negotiation-overlay"
            ref={overlayRef}
            style={{ position: 'fixed' }}
            aria-label={`为 ${offer.company_name} 准备谈薪`}
          >
            <section data-testid="offer-negotiation-drawer-harness">
              <output data-testid="pilot-draft-goal">{offerDraft?.goal ?? ''}</output>
              <button type="button" data-testid="save-pilot-draft" onClick={() => props.onOfferNegotiationDraftChange?.(offer.id, baseDraft('Pilot 目标'))}>
                保存 Pilot 草稿
              </button>
              <button type="button" data-testid="close-pilot-drawer" onClick={() => { props.onClose?.(); openerRef.current?.focus(); }}>关闭</button>
            </section>
          </section>
        )}
      </section>
    );
  },
}));

vi.mock('@/features/dashboard/DashboardView', () => ({
  default: (props: any) => (
    <section data-testid="dashboard-harness">
      <button type="button" data-testid="open-application-detail" onClick={() => props.onOpenDetailById?.(7)}>查看投递</button>
      <button type="button" data-testid="open-application-detail-b" onClick={() => props.onOpenDetailById?.(8)}>查看另一份投递</button>
    </section>
  ),
}));
vi.mock('@/components/OfferCenterView', () => ({
  default: (props: any) => <section data-testid="offer-center-harness"><button type="button" data-testid="open-ui-offer" onClick={() => props.onOpenNegotiation?.(offer)}>打开 UI 谈薪准备</button></section>,
}));
vi.mock('@/features/assistantSurface/AssistantSurfaceProvider', () => ({
  AssistantSurfaceProvider: (props: any) => <>{props.children}</>,
  useAssistantSurface: () => runtime.assistant,
  usePilotConversationController: () => runtime.pilot,
}));
vi.mock('@/features/assistantSurface/PilotWorkspace', () => ({
  default: (props: any) => (
    <section data-testid="pilot-workspace">
      <button type="button" data-testid="open-pilot-offer" onClick={() => props.onPrepareOfferNegotiation?.(offer)}>打开 Pilot 谈薪准备</button>
    </section>
  ),
}));
vi.mock('@/features/pilot/PilotAttachmentContext', () => ({
  PilotAttachmentProvider: (props: any) => <>{props.children}</>,
  usePilotAttachmentStore: () => ({ addAttachment: vi.fn(), createNewDraftWithAttachment: vi.fn() }),
}));
vi.mock('@/features/pilot/attachmentHandoff', () => ({ retainPilotAttachmentKey: (_current: unknown, next: unknown) => next }));
vi.mock('@/features/pilot/PilotOpportunityFitV2Card', () => ({
  default: (props: any) => (
    <section data-testid="pilot-opportunity-fit-v2-card" data-status={props.status} data-summary={props.summary ?? ''}>
      <button type="button" data-testid="open-pilot-owner" onClick={props.onOpenTask}>打开岗位判断</button>
    </section>
  ),
}));

vi.mock('@/components/KanbanBoard', () => ({ default: () => <div /> }));
vi.mock('@/components/ApplicationListView', () => ({ default: () => <div /> }));
vi.mock('@/components/CalendarView', () => ({ default: () => <div /> }));
vi.mock('@/components/KnowledgeSourcesView', () => ({ default: () => <div /> }));
vi.mock('@/components/QuestionBankView', () => ({ default: () => <div /> }));
vi.mock('@/components/InterviewV01View', () => ({ default: () => <div /> }));
vi.mock('@/components/ResumeLibraryView', () => ({ default: () => <div /> }));
vi.mock('@/features/reminders/RemindersView', () => ({ default: () => <div /> }));
vi.mock('@/components/SettingsView', () => ({ default: () => <div /> }));
vi.mock('@/components/MockInterviewDrawer', () => ({ default: () => <div /> }));
vi.mock('@/components/InterviewStoryLibraryView', () => ({ default: () => <div /> }));
vi.mock('@/components/InterviewStoryDrawer', () => ({ default: () => <div /> }));
vi.mock('@/features/pilotMascot/PilotMascot', () => ({ default: () => <div /> }));
vi.mock('@/features/assistantSurface/HaruDock', () => ({ default: () => <div /> }));

declare global { var IS_REACT_ACT_ENVIRONMENT: boolean | undefined; }
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLDivElement | null = null;

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  window.history.replaceState(null, '', '/');
  window.matchMedia = () => ({ matches: false, addEventListener: () => undefined, removeEventListener: () => undefined }) as unknown as MediaQueryList;
  window.scrollTo = vi.fn();
  runtime.projection = null;
  runtime.openOwner.mockReset();
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  vi.clearAllMocks();
});

describe('AppShell canonical opportunity-fit owner', () => {
  it('opens the bounded Pilot projection through the same Application task ref', async () => {
    await act(async () => root?.render(<AppShell />));
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="open-application-detail"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="open-opportunity-fit"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="nav-pilot"]')?.click());
    await flush();

    const card = host?.querySelector('[data-testid="pilot-opportunity-fit-v2-card"]');
    expect(card?.getAttribute('data-status')).toBe('ready');
    expect(card?.getAttribute('data-summary')).toBe('来自唯一岗位判断 owner 的安全摘要');
    expect(card?.querySelector('textarea,input,select')).toBeNull();
    expect(card?.querySelectorAll('button')).toHaveLength(1);
  });

  it('keeps AppShell free of the former Pilot mutation and history callbacks', () => {
    expect(appShellSource).not.toContain('pilotV2Draft');
    expect(appShellSource).not.toContain('onStartTriage');
    expect(appShellSource).not.toContain('onConfirmTriage');
    expect(appShellSource).not.toContain('listOpportunityFitV2Reviews');
    expect(appShellSource).toContain('createOpportunityFitOwnerStore');
    expect(pilotCardSource).not.toMatch(/onChange|onStartTriage|onConfirmTriage|onStartDeepReview|onViewHistory|onStartNew/);
    expect(pilotCardSource).not.toMatch(/\btextarea\b|\binput\b|\bselect\b/);
  });

  it('rejects a non-canonical history date from a child projection', async () => {
    await act(async () => root?.render(<AppShell />));
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="open-application-detail"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="open-opportunity-fit"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="emit-hostile-fit-projection"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="nav-pilot"]')?.click());
    await flush();

    const card = host?.querySelector('[data-testid="pilot-opportunity-fit-v2-card"]');
    expect(card?.getAttribute('data-summary')).toBe('来自唯一岗位判断 owner 的安全摘要');
    expect(card?.textContent).not.toContain('恶意投影不应进入 Pilot');
  });
});

describe('AppShell Offer negotiation draft isolation', () => {
  it('keeps UI and Pilot drafts isolated for the same Offer', async () => {
    await act(async () => root?.render(<AppShell />));
    await flush();

    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="nav-offers"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="open-ui-offer"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="edit-ui-offer"]')?.click());
    await flush();
    expect(host?.querySelector('[data-testid="ui-draft-goal"]')?.textContent).toBe('UI 目标');

    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="nav-pilot"]')?.click());
    await flush();
    const pilotOpenButton = host?.querySelector<HTMLButtonElement>('[data-testid="open-pilot-offer"]');
    pilotOpenButton?.focus();
    act(() => pilotOpenButton?.click());
    await flush();
    expect(host?.querySelector('[data-testid="pilot-draft-goal"]')?.textContent).toBe('');
    const overlay = host?.querySelector<HTMLElement>('[data-testid="offer-negotiation-overlay"]');
    expect(overlay).not.toBeNull();
    expect(overlay?.style.position).toBe('fixed');
    expect(overlay?.getAttribute('aria-label')).toBe(`为 ${offer.company_name} 准备谈薪`);
    expect(overlay?.contains(document.activeElement)).toBe(true);
    const focusable = Array.from(overlay?.querySelectorAll<HTMLElement>('button, [href], input, textarea, select, [tabindex]:not([tabindex="-1"])') ?? [])
      .filter((element) => !element.hasAttribute('disabled'));
    expect(focusable.length).toBeGreaterThan(1);
    focusable[focusable.length - 1]?.focus();
    act(() => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true })));
    expect(document.activeElement).toBe(focusable[0]);
    act(() => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
    await flush();
    expect(host?.querySelector('[data-testid="offer-negotiation-overlay"]')).toBeNull();
    expect(document.activeElement).toBe(pilotOpenButton);

    act(() => pilotOpenButton?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="save-pilot-draft"]')?.click());
    await flush();
    expect(host?.querySelector('[data-testid="pilot-draft-goal"]')?.textContent).toBe('Pilot 目标');

    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="close-pilot-drawer"]')?.click());
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="nav-offers"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="open-ui-offer"]')?.click());
    await flush();
    expect(host?.querySelector('[data-testid="ui-draft-goal"]')?.textContent).toBe('UI 目标');
  });
});
