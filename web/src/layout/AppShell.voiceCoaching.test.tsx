// @vitest-environment jsdom
import { act, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import AppShell from './AppShell';

vi.mock('@tanstack/react-query', () => ({
  useMutation: () => ({ isPending: false, mutate: vi.fn() }),
  useQuery: () => ({ data: [], isError: false, isLoading: false, isFetching: false, error: null }),
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}));
vi.mock('@dnd-kit/core', () => ({
  DndContext: (props: { children: ReactNode }) => <div>{props.children}</div>,
  PointerSensor: class PointerSensor {}, useSensor: () => ({}), useSensors: () => ({}),
}));
vi.mock('antd', () => {
  const Layout = Object.assign((props: Record<string, unknown> & { children?: ReactNode }) => <div>{props.children}</div>, {
    Content: (props: { children?: ReactNode }) => <main>{props.children}</main>,
  });
  return {
    Button: (props: React.ButtonHTMLAttributes<HTMLButtonElement>) => <button {...props} />,
    Layout,
    Spin: () => <div>loading</div>,
    Tabs: () => <div />,
    message: { warning: vi.fn(), success: vi.fn(), error: vi.fn(), info: vi.fn() },
  };
});
vi.mock('@/services/applicationJdVersions', () => ({
  getCurrentApplicationJd: vi.fn().mockResolvedValue({ current: { id: 4, jd_text: '后端工程师 JD' } }),
}));
vi.mock('./Sidebar', () => ({
  default: (props: { onChange: (view: string) => void }) => <nav>
    <button type="button" data-testid="nav-interview" onClick={() => props.onChange('interview')}>面试</button>
    <button type="button" data-testid="nav-pilot" onClick={() => props.onChange('pilot')}>Pilot</button>
  </nav>,
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
vi.mock('@/components/SettingsView', () => ({ default: () => <div /> }));
vi.mock('@/features/dashboard/DashboardView', () => ({ default: () => <div /> }));
vi.mock('@/features/reminders/RemindersView', () => ({ default: () => <div /> }));
vi.mock('@/components/OfferNegotiationDrawer', () => ({ default: () => <div /> }));
vi.mock('@/components/InterviewStoryDrawer', () => ({
  createInterviewStoryDraft: () => ({}), default: () => <div />,
}));
vi.mock('@/features/pilot/PilotAttachmentContext', () => ({
  PilotAttachmentProvider: (props: { children: ReactNode }) => <>{props.children}</>,
  usePilotAttachmentStore: () => ({ addAttachment: vi.fn(), createNewDraftWithAttachment: vi.fn() }),
}));
vi.mock('@/features/pilot/attachmentHandoff', () => ({ retainPilotAttachmentKey: (_current: unknown, next: unknown) => next }));
vi.mock('@/features/pilot/PilotOpportunityFitV2Card', () => ({ default: () => <div /> }));
vi.mock('@/features/pilotMascot/PilotMascot', () => ({ default: () => <div /> }));
vi.mock('@/components/InterviewV01View', () => ({
  default: (props: { onOpenVoiceCoachingGrowth?: () => void }) => (
    <button type="button" data-testid="open-ui-growth" onClick={props.onOpenVoiceCoachingGrowth}>表达成长</button>
  ),
}));

vi.mock('@/components/VoiceCoachingGrowthView', () => ({
  default: (props: { onBack: () => void; onPractice: (input: unknown) => void }) => (
    <section data-testid="voice-growth-view">
      <button type="button" data-testid="back-growth" onClick={props.onBack}>返回</button>
      <button type="button" data-testid="practice-growth" onClick={() => props.onPractice({ focus_kind: 'long_pause_control' })}>再练一次</button>
    </section>
  ),
}));
// Free practice now has one canonical owner: AppShell opens the readiness
// center, which hands a frozen context to InterviewStudio.  Mock both lazy
// boundaries so this test only verifies that composition-root wiring.
vi.mock('@/features/interviewReadiness/InterviewReadinessCenter', () => ({
  default: (props: { fixedMode?: string; onOpenStudio?: (context: unknown) => void }) => (
    <section data-testid="interview-readiness-center" data-readiness-mode={props.fixedMode ?? 'real'}>
      <button
        type="button"
        data-testid="open-quick-studio"
        onClick={() => props.onOpenStudio?.({
          kind: 'quick_practice',
          caseId: 101,
          positionName: '后端工程师',
          jdText: '已确认的岗位资料',
          resumeId: 11,
        })}
      >进入练习工作台</button>
    </section>
  ),
}));
vi.mock('@/features/interviewStudio/InterviewStudio', () => ({
  default: (props: { context: { kind: string; caseId?: number; positionName?: string }; onClose: () => void }) => (
    <section
      data-testid="interview-studio"
      data-context-kind={props.context.kind}
      data-case-id={props.context.caseId ?? 'none'}
    >
      <output data-testid="studio-position">{props.context.positionName ?? 'none'}</output>
      <button type="button" data-testid="close-studio" onClick={props.onClose}>关闭工作台</button>
    </section>
  ),
}));
vi.mock('@/components/ChatPanel', () => ({
  default: (props: { variant?: string; onOpenVoiceCoachingGrowth?: () => void }) => props.variant === 'page' ? (
    <button type="button" data-testid="open-pilot-growth" onClick={props.onOpenVoiceCoachingGrowth}>查看表达成长</button>
  ) : <div />,
}));

let root: Root;
let host: HTMLDivElement;

async function flush(): Promise<void> {
  await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); });
}

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  window.matchMedia = () => ({ matches: false, addEventListener: () => undefined, removeEventListener: () => undefined }) as unknown as MediaQueryList;
  window.scrollTo = vi.fn();
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.clearAllMocks();
});

describe('AppShell voice coaching navigation', () => {
  it('opens the same read-only growth view from Interview and Pilot, then hands off through the canonical readiness owner', async () => {
    await act(async () => root.render(<AppShell />));
    await flush();

    act(() => host.querySelector<HTMLButtonElement>('[data-testid="nav-interview"]')?.click());
    await flush();
    act(() => host.querySelector<HTMLButtonElement>('[data-testid="open-ui-growth"]')?.click());
    await flush();
    expect(host.querySelector('[data-testid="voice-growth-view"]')).not.toBeNull();
    act(() => host.querySelector<HTMLButtonElement>('[data-testid="practice-growth"]')?.click());
    await flush();
    expect(host.querySelector('[data-testid="interview-readiness-center"]')?.getAttribute('data-readiness-mode')).toBe('quick');
    act(() => host.querySelector<HTMLButtonElement>('[data-testid="open-quick-studio"]')?.click());
    await flush();
    expect(host.querySelector('[data-testid="interview-studio"]')?.getAttribute('data-context-kind')).toBe('quick_practice');
    expect(host.querySelector('[data-testid="interview-studio"]')?.getAttribute('data-case-id')).toBe('101');
    act(() => host.querySelector<HTMLButtonElement>('[data-testid="close-studio"]')?.click());
    await flush();
    expect(host.querySelector('[data-testid="interview-studio"]')).toBeNull();

    act(() => host.querySelector<HTMLButtonElement>('[data-testid="nav-pilot"]')?.click());
    await flush();
    act(() => host.querySelector<HTMLButtonElement>('[data-testid="open-pilot-growth"]')?.click());
    await flush();
    expect(host.querySelector('[data-testid="voice-growth-view"]')).not.toBeNull();
    act(() => host.querySelector<HTMLButtonElement>('[data-testid="practice-growth"]')?.click());
    await flush();
    expect(host.querySelector('[data-testid="interview-readiness-center"]')?.getAttribute('data-readiness-mode')).toBe('quick');
  });

  it('keeps the composition root write-free while the canonical studio owns close', async () => {
    await act(async () => root.render(<AppShell />));
    await flush();
    act(() => host.querySelector<HTMLButtonElement>('[data-testid="nav-interview"]')?.click());
    await flush();
    act(() => host.querySelector<HTMLButtonElement>('[data-testid="open-ui-growth"]')?.click());
    await flush();
    act(() => host.querySelector<HTMLButtonElement>('[data-testid="practice-growth"]')?.click());
    await flush();
    expect(host.querySelector('[data-testid="interview-readiness-center"]')).not.toBeNull();
    expect(host.querySelector('[data-testid="interview-studio"]')).toBeNull();
    act(() => host.querySelector<HTMLButtonElement>('[data-testid="open-quick-studio"]')?.click());
    await flush();
    act(() => host.querySelector<HTMLButtonElement>('[data-testid="close-studio"]')?.click());
    await flush();
    expect(host.querySelector('[data-testid="interview-studio"]')).toBeNull();
  });

  it('reopens the same canonical preparation route after the studio closes', async () => {
    await act(async () => root.render(<AppShell />));
    await flush();
    act(() => host.querySelector<HTMLButtonElement>('[data-testid="nav-interview"]')?.click());
    await flush();
    act(() => host.querySelector<HTMLButtonElement>('[data-testid="open-ui-growth"]')?.click());
    await flush();
    act(() => host.querySelector<HTMLButtonElement>('[data-testid="practice-growth"]')?.click());
    await flush();
    act(() => host.querySelector<HTMLButtonElement>('[data-testid="open-quick-studio"]')?.click());
    await flush();
    expect(host.querySelector('[data-testid="interview-studio"]')?.getAttribute('data-case-id')).toBe('101');
    act(() => host.querySelector<HTMLButtonElement>('[data-testid="close-studio"]')?.click());
    await flush();

    act(() => host.querySelector<HTMLButtonElement>('[data-testid="nav-interview"]')?.click());
    await flush();
    act(() => host.querySelector<HTMLButtonElement>('[data-testid="open-ui-growth"]')?.click());
    await flush();
    act(() => host.querySelector<HTMLButtonElement>('[data-testid="practice-growth"]')?.click());
    await flush();
    expect(host.querySelector('[data-testid="interview-readiness-center"]')?.getAttribute('data-readiness-mode')).toBe('quick');
  });
});
