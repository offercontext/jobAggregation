// @vitest-environment jsdom
import { act, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { writeMaterialKitHandoff } from '@/features/pilot/materialKitHandoff';

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const state = vi.hoisted(() => ({
  materialProps: vi.fn(),
  analyzeJD: vi.fn(),
  events: [] as unknown[],
  jdCurrent: null as unknown,
  jdLoading: false,
  jdHistory: [] as unknown[],
  jdDetail: null as unknown,
}));

vi.mock('@/services/ai', () => ({ analyzeJD: state.analyzeJD }));
vi.mock('@/services/notes', () => ({
  listNotesByApp: vi.fn().mockResolvedValue([]),
  createNote: vi.fn(),
  deleteNote: vi.fn(),
  updateNote: vi.fn(),
}));
vi.mock('@/services/events', () => ({ listEvents: vi.fn().mockResolvedValue([]) }));
vi.mock('@/services/applicationJdVersions', () => ({
  getCurrentApplicationJd: vi.fn(),
  getApplicationJdVersion: vi.fn(),
  listApplicationJdVersions: vi.fn(),
  saveApplicationJdVersion: vi.fn(),
}));
vi.mock('@tanstack/react-query', () => ({
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
  useQuery: (options: { queryKey?: unknown[] }) => ({
    data: options.queryKey?.[0] === 'events'
      ? state.events
      : options.queryKey?.[0] === 'application-jd-current'
        ? state.jdCurrent
        : options.queryKey?.[0] === 'application-jd-history'
          ? state.jdHistory
          : options.queryKey?.[0] === 'application-jd-detail'
            ? state.jdDetail
            : [],
    isLoading: options.queryKey?.[0] === 'application-jd-current' && state.jdLoading,
  }),
  useMutation: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock('./ApplicationDetail.module.css', () => ({ default: {} }));
vi.mock('./PilotAttachmentHandle', () => ({ createPilotAttachmentDragBinding: () => ({}) }));
vi.mock('./ScheduleEventForm', () => ({ default: () => null }));
vi.mock('./ReviewFormDrawer', () => ({ default: () => null }));
vi.mock('./MaterialKitDrawer', () => ({
  default: (props: { initialResumeID?: number; initialJdSnapshot?: string; initialJdVersionID?: number; onClose?: () => void }) => {
    state.materialProps(props);
    return (
      <div data-testid="material-kit" data-resume-id={props.initialResumeID} data-jd={props.initialJdSnapshot} data-jd-version-id={props.initialJdVersionID}>
        <button type="button" aria-label="close material kit" onClick={props.onClose}>close</button>
      </div>
    );
  },
}));
vi.mock('./OpportunityFitReviewDrawer', () => ({
  default: (props: { onPrepareMaterials: (review: unknown, jdText: string, jdVersionId?: number) => void }) => (
    <button onClick={() => props.onPrepareMaterials({ source: { resume: { id: 11 } } }, 'Frozen JD text', 1)}>
      prepare
    </button>
  ),
}));
vi.mock('@ant-design/icons', () => ({
  ArrowLeftOutlined: () => null,
  CalendarOutlined: () => null,
  RobotOutlined: () => null,
  PlusOutlined: () => null,
  AudioOutlined: () => null,
  DatabaseOutlined: () => null,
  FileTextOutlined: () => null,
  MoreOutlined: () => null,
}));
vi.mock('antd', () => {
  const Form = Object.assign(
    (props: { children: ReactNode; onFinish?: (value: unknown) => void }) => <form onSubmit={(event) => { event.preventDefault(); props.onFinish?.({}); }}>{props.children}</form>,
    {
      Item: (props: { children: ReactNode }) => <label>{props.children}</label>,
      useForm: () => [{ resetFields: vi.fn() }],
    },
  );
  const Input = Object.assign(
    (props: React.InputHTMLAttributes<HTMLInputElement>) => <input {...props} />,
    { TextArea: (props: React.TextareaHTMLAttributes<HTMLTextAreaElement>) => <textarea {...props} /> },
  );
  const Typography = {
    Title: (props: { children: ReactNode }) => <h2>{props.children}</h2>,
    Paragraph: (props: { children: ReactNode }) => <p>{props.children}</p>,
    Text: (props: { children: ReactNode }) => <span>{props.children}</span>,
  };
  return {
    Dropdown: (props: { children: ReactNode; menu?: { items?: Array<{ key: string; label: ReactNode; onClick?: () => void }> } }) => (
      <>{props.children}{props.menu?.items?.map((item) => <button key={item.key} onClick={item.onClick}>{item.label}</button>)}</>
    ),
    Button: ({ children, htmlType: _htmlType, loading: _loading, icon: _icon, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement> & { htmlType?: string; loading?: boolean; icon?: ReactNode }) => (
      <button {...props}>{children}</button>
    ),
    Modal: (props: { open?: boolean; title?: ReactNode; children: ReactNode; footer?: ReactNode; onCancel?: () => void }) => (
      props.open ? <div role="dialog"><h3>{props.title}</h3>{props.children}</div> : null
    ),
    Divider: () => <hr />,
    Empty: (props: { description?: ReactNode }) => <div>{props.description}</div>,
    Form,
    Input,
    Popconfirm: (props: { children: ReactNode }) => <>{props.children}</>,
    Select: () => <select />,
    Space: (props: { children: ReactNode }) => <div>{props.children}</div>,
    Spin: () => <span>loading</span>,
    Tag: (props: { children: ReactNode }) => <span>{props.children}</span>,
    Timeline: () => null,
    Typography,
    message: { success: vi.fn(), error: vi.fn() },
  };
});

const { default: ApplicationDetail } = await import('./ApplicationDetail');

const application = {
  id: 7,
  company_name: 'Example Co.',
  position_name: 'Backend Engineer',
  job_url: 'https://external.example/job/7',
  status: 'pending',
  source: 'manual',
  notes: '',
  applied_at: '2026-07-21T00:00:00Z',
  created_at: '2026-07-21T00:00:00Z',
  updated_at: '2026-07-21T00:00:00Z',
} as never;

let root: Root | undefined;
let container: HTMLDivElement | undefined;

beforeEach(() => {
  state.materialProps.mockReset();
  state.analyzeJD.mockReset();
  state.events = [];
  state.jdCurrent = null;
  state.jdLoading = false;
  state.jdHistory = [];
  state.jdDetail = null;
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
});

describe('ApplicationDetail opportunity fit handoff', () => {
  it('renders the current source tag only for the mounted application context', () => {
    act(() => root?.render(<ApplicationDetail application={application} open onClose={vi.fn()} />));

    expect(container?.textContent).toContain('当前使用来源');
    expect(container?.textContent).toContain('当前投递');
    expect(state.analyzeJD).not.toHaveBeenCalled();
  });

  it('mounts the known JD as read-only context without turning the source URL into a link', () => {
    state.jdCurrent = {
      current: {
        id: 41,
        application_id: 7,
        version_number: 1,
        jd_text: '筱哲案例公司的后端岗位描述',
        source_url: 'https://example.invalid/jd/41',
        source_kind: 'ui',
        content_sha256: 'a'.repeat(64),
        utf8_byte_length: 30,
        preview: '筱哲案例公司的后端岗位描述',
        created_at: '2026-08-05T00:00:00Z',
      },
    };

    act(() => root?.render(<ApplicationDetail application={application} open onClose={vi.fn()} />));

    expect(container?.textContent).toContain('筱哲案例公司的后端岗位描述');
    expect(container?.textContent).toContain('https://example.invalid/jd/41');
    expect(container?.querySelector('a')).toBeNull();
    expect(state.analyzeJD).not.toHaveBeenCalled();
  });

  it('passes the current saved JD into Material Kit from the direct application action', () => {
    state.jdCurrent = {
      current: {
        id: 41,
        application_id: 7,
        version_number: 1,
        jd_text: '当前已确认的岗位资料',
        source_url: null,
        source_kind: 'ui',
        content_sha256: 'a'.repeat(64),
        utf8_byte_length: 30,
        preview: '当前已确认的岗位资料',
        created_at: '2026-08-05T00:00:00Z',
      },
    };

    act(() => root?.render(<ApplicationDetail application={application} open onClose={vi.fn()} />));
    act(() => {
      [...(container?.querySelectorAll('button') || [])]
        .find((button) => button.textContent === '打开投递材料')
        ?.click();
    });

    const materialKit = container?.querySelector('[data-testid="material-kit"]');
    expect(materialKit?.getAttribute('data-jd')).toBe('当前已确认的岗位资料');
    expect(materialKit?.getAttribute('data-jd-version-id')).toBe('41');
  });

  it('passes historical frozen Resume and JD into Material Kit without opening a URL', () => {
    act(() => root?.render(<ApplicationDetail application={application} open onClose={vi.fn()} />));

    expect(container?.querySelector('a')).toBeNull();
    expect(state.analyzeJD).not.toHaveBeenCalled();

    act(() => {
      [...(container?.querySelectorAll('button') || [])]
        .find((button) => button.textContent === '岗位决策漏斗')
        ?.click();
    });
    act(() => container?.querySelector('button')?.click());

    const materialKit = container?.querySelector('[data-testid="material-kit"]');
    expect(materialKit?.getAttribute('data-resume-id')).toBe('11');
    expect(materialKit?.getAttribute('data-jd')).toBe('Frozen JD text');
  });

  it('consumes a matching AppShell handoff once and uses frozen values', async () => {
    writeMaterialKitHandoff({
      applicationId: 7,
      resumeId: 12,
      jdText: 'Frozen Pilot JD',
      jdVersionId: 2,
    });

    act(() => root?.render(<ApplicationDetail application={application} open onClose={vi.fn()} />));
    await act(async () => {
      await Promise.resolve();
    });

    expect(container?.querySelector('[data-testid="material-kit"]')?.getAttribute('data-resume-id')).toBe('12');
    expect(container?.querySelector('[data-testid="material-kit"]')?.getAttribute('data-jd')).toBe('Frozen Pilot JD');
  });

  it('keeps a consumed handoff open when the current JD query transitions from loading to loaded', async () => {
    writeMaterialKitHandoff({
      applicationId: 7,
      resumeId: 12,
      jdText: 'Frozen Pilot JD',
      jdVersionId: 2,
    });
    state.jdCurrent = null;
    state.jdLoading = true;

    act(() => root?.render(<ApplicationDetail application={application} open onClose={vi.fn()} />));
    await act(async () => { await Promise.resolve(); });
    expect(container?.querySelector('[data-testid="material-kit"]')?.getAttribute('data-jd')).toBe('Frozen Pilot JD');

    state.jdLoading = false;
    state.jdCurrent = {
      current: {
        id: 3,
        application_id: 7,
        version_number: 2,
        jd_text: 'New current JD',
        source_url: null,
        source_kind: 'ui',
        content_sha256: 'b'.repeat(64),
        utf8_byte_length: 16,
        preview: 'New current JD',
        created_at: '2026-08-06T00:00:00Z',
      },
    };
    act(() => root?.render(<ApplicationDetail application={application} open onClose={vi.fn()} />));
    expect(container?.querySelector('[data-testid="material-kit"]')?.getAttribute('data-jd')).toBe('Frozen Pilot JD');
  });

  it('does not open Material Kit for a legacy handoff without a JD version', async () => {
    writeMaterialKitHandoff({
      applicationId: 7,
      resumeId: 12,
      jdText: 'Legacy JD',
    } as never);

    act(() => root?.render(<ApplicationDetail application={application} open onClose={vi.fn()} />));
    await act(async () => { await Promise.resolve(); });

    expect(container?.querySelector('[data-testid="material-kit"]')).toBeNull();
  });

  it('clears consumed material prefill when switching to another Application', async () => {
    writeMaterialKitHandoff({
      applicationId: 7,
      resumeId: 12,
      jdText: 'Frozen Pilot JD',
      jdVersionId: 2,
    });
    const otherApplication = Object.assign({}, application, { id: 8 }) as typeof application;

    act(() => root?.render(<ApplicationDetail application={application} open onClose={vi.fn()} />));
    await act(async () => { await Promise.resolve(); });
    expect(container?.querySelector('[data-testid="material-kit"]')).not.toBeNull();

    act(() => root?.render(<ApplicationDetail application={otherApplication} open onClose={vi.fn()} />));
    await act(async () => { await Promise.resolve(); });
    expect(container?.querySelector('[data-testid="material-kit"]')).toBeNull();
  });

  it('clears the consumed material prefill immediately when Material Kit closes', async () => {
    writeMaterialKitHandoff({
      applicationId: 7,
      resumeId: 12,
      jdText: 'Frozen Pilot JD',
      jdVersionId: 2,
    });

    act(() => root?.render(<ApplicationDetail application={application} open onClose={vi.fn()} />));
    await act(async () => { await Promise.resolve(); });
    expect(container?.querySelector('[data-testid="material-kit"]')).not.toBeNull();

    act(() => {
      (container?.querySelector('[aria-label="close material kit"]') as HTMLButtonElement)?.click();
    });

    expect(container?.querySelector('[data-testid="material-kit"]')).toBeNull();
    expect(state.materialProps.mock.calls[state.materialProps.mock.calls.length - 1]?.[0]).toMatchObject({
      initialResumeID: 12,
      initialJdSnapshot: 'Frozen Pilot JD',
    });
  });

  it('exposes the Application-scoped Pilot evaluation entry without URL analysis', () => {
    const openPilot = vi.fn();
    act(() => root?.render(<ApplicationDetail application={application} open onClose={vi.fn()} onOpenPilotOpportunityFit={openPilot} />));
    const button = [...(container?.querySelectorAll('button') || [])]
      .find((candidate) => candidate.textContent === '评估岗位匹配');
    act(() => button?.click());
    expect(openPilot).toHaveBeenCalledWith(application);
    expect(state.analyzeJD).not.toHaveBeenCalled();
  });

  it('requires an explicit interview choice when Pilot targets multiple interviews', async () => {
    state.events = [
      { id: 31, event_type: 'interview', subtype: 'technical', scheduled_at: '2026-07-24T10:00:00Z' },
      { id: 32, event_type: 'interview', subtype: 'behavioral', scheduled_at: '2026-07-25T10:00:00Z' },
    ];
    act(() => root?.render(
      <ApplicationDetail
        application={application}
        open
        onClose={vi.fn()}
        pilotInterviewPreparationApplicationId={7}
        onPilotInterviewPreparationFocusConsumed={vi.fn()}
      />,
    ));
    await act(async () => { await Promise.resolve(); });

    expect(container?.textContent).toContain('选择要准备的面试');
    expect(container?.textContent).toContain('technical');
    expect(container?.textContent).toContain('behavioral');
    const dialogButtons = [...(container?.querySelectorAll('[role="dialog"] button') || [])];
    expect(dialogButtons).toHaveLength(2);
    act(() => (dialogButtons[1] as HTMLButtonElement).click());
    expect(container?.textContent).toContain('面试准备建议');
  });
  it('opens the explicitly requested interview preparation event without showing a choice dialog', async () => {
    state.events = [
      { id: 31, event_type: 'interview', subtype: 'technical', scheduled_at: '2026-07-24T10:00:00Z' },
      { id: 32, event_type: 'interview', subtype: 'behavioral', scheduled_at: '2026-07-25T10:00:00Z' },
    ];
    act(() => root?.render(
      <ApplicationDetail
        application={application}
        open
        onClose={vi.fn()}
        pilotInterviewPreparationApplicationId={7}
        pilotInterviewPreparationEventId={32}
        onPilotInterviewPreparationFocusConsumed={vi.fn()}
      />,
    ));
    await act(async () => { await Promise.resolve(); });

    expect(container?.querySelector('[role="dialog"]')).toBeNull();
    expect(container?.textContent).toContain('面试准备建议');
  });

  it('mounts next-step navigation with exact context and performs no write', () => {
    const onNavigate = vi.fn();
    const onSetDisposition = vi.fn();
    act(() => root?.render(
      <ApplicationDetail
        application={application}
        open
        onClose={vi.fn()}
        nextStepSuggestions={{
          candidates: [{
            id: 'prepare-event',
            stateKey: 'prepare-event-v1',
            title: 'Prepare event',
            reason: 'Use the selected interview context.',
            destination: { kind: 'interview_event', applicationId: 7, eventId: 32 },
            sources: [],
          }],
          sourceRisks: [],
        }}
        onSetDisposition={onSetDisposition}
        onNextStepNavigate={onNavigate}
      />,
    ));

    act(() => (container?.querySelector('article button') as HTMLButtonElement)?.click());

    expect(onNavigate).toHaveBeenCalledWith({ kind: 'interview_event', applicationId: 7, eventId: 32 });
    expect(onSetDisposition).not.toHaveBeenCalled();
    expect(state.analyzeJD).not.toHaveBeenCalled();
  });
});
