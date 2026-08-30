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
  const Layout = Object.assign((props: any) => <div {...props}>{props.children}</div>, {
    Content: (props: any) => <main {...props}>{props.children}</main>,
  });
  return {
    Button: (props: any) => <button type="button" {...props}>{props.children}</button>,
    Layout, Spin: () => <div>loading</div>, Tabs: () => <div />,
    message: { warning: vi.fn(), success: vi.fn(), error: vi.fn() },
  };
});
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
vi.mock('@/features/dashboard/DashboardView', () => ({ default: () => <div /> }));
vi.mock('@/features/reminders/RemindersView', () => ({ default: () => <div /> }));
vi.mock('@/components/SettingsView', () => ({ default: () => <div /> }));
vi.mock('@/features/pilot/PilotAttachmentContext', () => ({
  PilotAttachmentProvider: (props: { children: React.ReactNode }) => <>{props.children}</>,
  usePilotAttachmentStore: () => ({ addAttachment: vi.fn(), createNewDraftWithAttachment: vi.fn() }),
}));
vi.mock('@/features/pilot/attachmentHandoff', () => ({ retainPilotAttachmentKey: (_current: unknown, next: unknown) => next }));
vi.mock('@/features/pilot/PilotOpportunityFitV2Card', () => ({ default: () => <div /> }));
vi.mock('@/components/OfferNegotiationDrawer', () => ({ default: () => <div /> }));
vi.mock('@/components/InterviewV01View', () => ({
  default: (props: { onOpenStoryLibrary: (noteId?: number) => void }) => (
    <button type="button" data-testid="open-ui-story" onClick={() => props.onOpenStoryLibrary(7)}>整理为故事</button>
  ),
}));
// AppShell's reviews/interview-story surface is the canonical
// ExperienceMaterialsView wrapper.  Keep the test focused on the AppShell
// handoff while replacing that lazy boundary, rather than mocking the
// retired library entrypoint directly.
vi.mock('@/components/ExperienceMaterialsView', () => ({
  default: (props: { onOpenDraft: (input: { entrypoint: 'ui'; reviewNoteId: number }) => void }) => (
    <button type="button" data-testid="library-open-ui-story" onClick={() => props.onOpenDraft({ entrypoint: 'ui', reviewNoteId: 7 })}>新建故事</button>
  ),
}));
vi.mock('@/components/InterviewStoryDrawer', () => ({
  createInterviewStoryDraft: (entrypoint: 'ui' | 'pilot', reviewNoteId?: number, revision?: any) => ({
    entrypoint, reviewNoteId, targetStoryId: revision?.targetStoryId ?? null,
    expectedCurrentVersionId: revision?.expectedCurrentVersionId ?? null,
    expectedStoryRevision: revision?.expectedStoryRevision ?? null,
    selections: [], assertions: [], idempotencyKey: `${entrypoint}-${reviewNoteId ?? 'new'}-key`,
    attemptId: null, proposal: null, editedContent: null, resultUnknown: false,
    pendingOperation: null, confirmationToken: null, error: null,
  }),
  default: (props: any) => (
      <section data-testid="story-drawer">
        <output data-testid="story-entrypoint">{props.draft.entrypoint}</output>
        <output data-testid="story-key">{props.draft.idempotencyKey}</output>
        <output data-testid="story-unknown">{String(props.draft.resultUnknown)}</output>
        <button type="button" data-testid="mark-story-unknown" onClick={() => props.onDraftChange({ ...props.draft, resultUnknown: true, pendingOperation: 'generate' })}>mark</button>
        <button type="button" data-testid="close-story" onClick={props.onClose}>close</button>
      </section>
    ),
}));
vi.mock('@/components/ChatPanel', () => ({
  default: (props: { onOpenInterviewStoryLibrary?: () => void }) => (
    <button type="button" data-testid="open-pilot-story" onClick={() => props.onOpenInterviewStoryLibrary?.()}>准备故事</button>
  ),
}));

let root: Root | null = null;
let host: HTMLDivElement | null = null;

async function flush() {
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
}

describe('AppShell Interview Story draft isolation', () => {
  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    window.matchMedia = () => ({ matches: false, addEventListener: () => undefined, removeEventListener: () => undefined }) as unknown as MediaQueryList;
    window.scrollTo = vi.fn();
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

  it('retains each UI/Pilot unknown Story attempt in its own scope after close and re-entry', async () => {
    await act(async () => root?.render(<AppShell />));
    await flush();

    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="nav-interview"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="open-ui-story"]')?.click());
    await flush();
    const uiKey = host?.querySelector('[data-testid="story-key"]')?.textContent;
    expect(host?.querySelector('[data-testid="story-entrypoint"]')?.textContent).toBe('ui');
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="mark-story-unknown"]')?.click());
    await flush();
    expect(host?.querySelector('[data-testid="story-unknown"]')?.textContent).toBe('true');
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="close-story"]')?.click());

    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="nav-pilot"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="open-pilot-story"]')?.click());
    await flush();
    const pilotKey = host?.querySelector('[data-testid="story-key"]')?.textContent;
    expect(host?.querySelector('[data-testid="story-entrypoint"]')?.textContent).toBe('pilot');
    expect(pilotKey).not.toBe(uiKey);
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="mark-story-unknown"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="close-story"]')?.click());

    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="nav-interview"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="library-open-ui-story"]')?.click());
    await flush();
    expect(host?.querySelector('[data-testid="story-entrypoint"]')?.textContent).toBe('ui');
    expect(host?.querySelector('[data-testid="story-key"]')?.textContent).toBe(uiKey);
    expect(host?.querySelector('[data-testid="story-unknown"]')?.textContent).toBe('true');
  });
});
