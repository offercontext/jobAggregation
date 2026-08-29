// @vitest-environment jsdom
import { act, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { writeMaterialKitHandoff } from '@/features/pilot/materialKitHandoff';
import type { Application } from '@/types/application';
import type { Resume } from '@/types/resume';

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const state = vi.hoisted(() => ({
  materialProps: vi.fn(),
  analyzeJD: vi.fn(),
  events: [] as unknown[],
  materialKit: null as unknown,
  fitHistory: [] as unknown[],
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
          : options.queryKey?.[0] === 'application-material-kit'
            ? state.materialKit
            : options.queryKey?.[0] === 'opportunity-fit-v2-reviews'
              ? state.fitHistory
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
  default: (props: {
    onPrepareMaterials: (review: unknown, jdText: string, jdVersionId?: number) => void;
    onOwnerStateChange?: (state: { pending: boolean; resultUnknown: boolean; unsaved: boolean }) => void;
  }) => (
    <>
      <button onClick={() => props.onPrepareMaterials({ source: { resume: { id: 11 } } }, 'Frozen JD text', 1)}>
        prepare
      </button>
      <button onClick={() => props.onOwnerStateChange?.({ pending: true, resultUnknown: false, unsaved: true })}>
        mark fit pending
      </button>
      <button onClick={() => props.onOwnerStateChange?.({ pending: false, resultUnknown: true, unsaved: true })}>
        mark fit unknown
      </button>
    </>
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

const application: Application = {
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
};

const resume: Resume = {
  id: 11,
  name: '主简历',
  file_path: '',
  parsed_data: '',
  parse_status: 'parsed',
  title: '主简历',
  is_master: true,
  parent_resume_id: null,
  source: 'manual',
  source_file_path: '',
  content_json: {},
  deleted_at: null,
  created_at: '2026-07-01T00:00:00Z',
  completion_percent: 100,
  missing_sections: [],
  is_complete: true,
};

let root: Root | undefined;
let container: HTMLDivElement | undefined;

beforeEach(() => {
  state.materialProps.mockReset();
  state.analyzeJD.mockReset();
  state.events = [];
  state.materialKit = null;
  state.fitHistory = [];
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

    const appliedApplication = { ...application, status: 'applied' } as typeof application;
    act(() => root?.render(
      <ApplicationDetail
        application={appliedApplication}
        resumes={[resume]}
        open
        taskNow={Date.parse('2026-07-24T09:00:00Z')}
        onClose={vi.fn()}
      />,
    ));
    act(() => {
      [...(container?.querySelectorAll('button') || [])]
        .find((button) => button.textContent === '打开准备')
        ?.click();
    });

    const materialKit = container?.querySelector('[data-testid="material-kit"]');
    expect(materialKit?.getAttribute('data-jd')).toBe('当前已确认的岗位资料');
    expect(materialKit?.getAttribute('data-jd-version-id')).toBe('41');
  });

  it('passes historical frozen Resume and JD into Material Kit without opening a URL', () => {
    state.jdCurrent = {
      current: {
        id: 41,
        application_id: 7,
        version_number: 1,
        jd_text: '当前岗位资料',
        source_url: null,
        source_kind: 'ui',
        content_sha256: 'a'.repeat(64),
        utf8_byte_length: 10,
        preview: '当前岗位资料',
        created_at: '2026-08-05T00:00:00Z',
      },
    };
    const pendingApplication = { ...application, status: 'pending' } as typeof application;
    act(() => root?.render(
      <ApplicationDetail
        application={pendingApplication}
        resumes={[resume]}
        open
        taskNow={Date.parse('2026-07-24T09:00:00Z')}
        onClose={vi.fn()}
      />,
    ));

    expect(container?.querySelector('a')).toBeNull();
    expect(state.analyzeJD).not.toHaveBeenCalled();

    act(() => {
      [...(container?.querySelectorAll('button') || [])]
        .find((button) => button.textContent === '开始判断')
        ?.click();
    });
    act(() => {
      [...(container?.querySelectorAll('button') || [])]
        .find((button) => button.textContent === 'prepare')
        ?.click();
    });

    const materialKit = container?.querySelector('[data-testid="material-kit"]');
    expect(materialKit?.getAttribute('data-resume-id')).toBe('11');
    expect(materialKit?.getAttribute('data-jd')).toBe('Frozen JD text');
  });

  it('consumes a matching AppShell handoff once and uses frozen values', async () => {
    writeMaterialKitHandoff({
      applicationId: 7,
      source: 'pilot',
      hints: { suggestedResumeId: 12, suggestedJdVersionId: 2 },
    });

    act(() => root?.render(<ApplicationDetail application={application} open onClose={vi.fn()} />));
    await act(async () => {
      await Promise.resolve();
    });

    expect(container?.querySelector('[data-testid="material-kit"]')?.getAttribute('data-resume-id')).toBe('12');
    expect(container?.querySelector('[data-testid="material-kit"]')?.getAttribute('data-jd')).toBeNull();
  });

  it('keeps a consumed handoff open when the current JD query transitions from loading to loaded', async () => {
    writeMaterialKitHandoff({
      applicationId: 7,
      source: 'pilot',
      hints: { suggestedResumeId: 12, suggestedJdVersionId: 2 },
    });
    state.jdCurrent = null;
    state.jdLoading = true;

    act(() => root?.render(<ApplicationDetail application={application} open onClose={vi.fn()} />));
    await act(async () => { await Promise.resolve(); });
    expect(container?.querySelector('[data-testid="material-kit"]')?.getAttribute('data-jd')).toBeNull();

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
    expect(container?.querySelector('[data-testid="material-kit"]')?.getAttribute('data-jd')).toBeNull();
  });

  it('opens Material Kit for an application-only handoff and lets the owner resolve JD', async () => {
    writeMaterialKitHandoff({
      applicationId: 7,
      source: 'deep_link',
      hints: { suggestedResumeId: 12 },
    });

    act(() => root?.render(<ApplicationDetail application={application} open onClose={vi.fn()} />));
    await act(async () => { await Promise.resolve(); });

    expect(container?.querySelector('[data-testid="material-kit"]')).not.toBeNull();
  });

  it('clears consumed material prefill when switching to another Application', async () => {
    writeMaterialKitHandoff({
      applicationId: 7,
      source: 'pilot',
      hints: { suggestedResumeId: 12, suggestedJdVersionId: 2 },
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
      source: 'pilot',
      hints: { suggestedResumeId: 12, suggestedJdVersionId: 2 },
    });

    act(() => root?.render(<ApplicationDetail application={application} open onClose={vi.fn()} />));
    await act(async () => { await Promise.resolve(); });
    expect(container?.querySelector('[data-testid="material-kit"]')).not.toBeNull();

    act(() => {
      (container?.querySelector('[aria-label="close material kit"]') as HTMLButtonElement)?.click();
    });
    act(() => {
      (container?.querySelector('[data-core-task-owner]') as HTMLElement)?.dispatchEvent(new Event('animationend', { bubbles: true }));
    });

    expect(container?.querySelector('[data-testid="material-kit"]')).toBeNull();
    expect(state.materialProps.mock.calls.some(([props]) => (
      props.initialResumeID === 12 && props.initialJdSnapshot === undefined
    ))).toBe(true);
  });

  it('exposes the canonical Application-scoped evaluation task without URL analysis', () => {
    const openPilot = vi.fn();
    state.jdCurrent = {
      current: {
        id: 41,
        application_id: 7,
        version_number: 1,
        jd_text: '当前岗位资料',
        source_url: null,
        source_kind: 'ui',
        content_sha256: 'a'.repeat(64),
        utf8_byte_length: 10,
        preview: '当前岗位资料',
        created_at: '2026-08-05T00:00:00Z',
      },
    };
    act(() => root?.render(
      <ApplicationDetail
        application={application}
        resumes={[resume]}
        open
        onClose={vi.fn()}
        onOpenPilotOpportunityFit={openPilot}
      />,
    ));
    const button = [...(container?.querySelectorAll('button') || [])]
      .find((candidate) => candidate.textContent === '开始判断');
    act(() => button?.click());
    expect(container?.querySelector('[data-core-task-owner]')).not.toBeNull();
    expect(openPilot).not.toHaveBeenCalled();
    expect(state.analyzeJD).not.toHaveBeenCalled();
  });

  it('keeps the shared task surface guarded while the Fit owner is pending or unknown', () => {
    const guards: Array<{ pending: boolean; unsaved: boolean }> = [];
    state.jdCurrent = { current: { id: 41, application_id: 7, jd_text: '岗位资料', source_url: null } };
    act(() => root?.render(
      <ApplicationDetail
        application={application}
        resumes={[resume]}
        open
        onClose={vi.fn()}
        onTaskSurfaceGuardChange={(guard) => guards.push(guard)}
      />,
    ));
    act(() => {
      [...(container?.querySelectorAll('button') || [])]
        .find((button) => button.textContent === '开始判断')
        ?.click();
    });

    act(() => {
      [...(container?.querySelectorAll('button') || [])]
        .find((button) => button.textContent === 'mark fit pending')
        ?.click();
    });
    expect(guards[guards.length - 1]).toEqual({ pending: true, unsaved: true });

    act(() => {
      [...(container?.querySelectorAll('button') || [])]
        .find((button) => button.textContent === 'mark fit unknown')
        ?.click();
    });
    expect(guards[guards.length - 1]).toEqual({ pending: true, unsaved: true });
  });

  it('isolates standalone controller lifecycles across duplicate mounts of one application', () => {
    state.jdCurrent = { current: { id: 41, jd_text: '岗位资料', source_url: null } };
    act(() => root?.render(
      <>
        <ApplicationDetail application={application} resumes={[resume]} open onClose={vi.fn()} />
        <ApplicationDetail application={application} resumes={[resume]} open onClose={vi.fn()} />
      </>,
    ));
    const starts = [...(container?.querySelectorAll('button') ?? [])]
      .filter((button) => button.textContent === '开始判断');
    expect(starts).toHaveLength(2);
    act(() => starts[0]?.click());
    act(() => starts[1]?.click());
    expect(container?.querySelectorAll('[data-core-task-owner]')).toHaveLength(2);

    act(() => (container?.querySelectorAll('button[aria-label="关闭任务"]')[0] as HTMLButtonElement).click());
    act(() => (container?.querySelectorAll('[data-core-task-owner]')[0] as HTMLElement).dispatchEvent(new Event('animationend', { bubbles: true })));
    expect(container?.querySelectorAll('[data-core-task-owner]')).toHaveLength(1);
  });

  it('keeps standalone task identities separate for different applications', () => {
    state.jdCurrent = { current: { id: 41, jd_text: '岗位资料', source_url: null } };
    const otherApplication: Application = { ...application, id: 8, company_name: 'Other Co.' };
    act(() => root?.render(
      <>
        <ApplicationDetail application={application} resumes={[resume]} open onClose={vi.fn()} />
        <ApplicationDetail application={otherApplication} resumes={[resume]} open onClose={vi.fn()} />
      </>,
    ));
    const starts = [...(container?.querySelectorAll('button') ?? [])]
      .filter((button) => button.textContent === '开始判断');
    expect(starts).toHaveLength(2);
    act(() => starts[0]?.click());
    act(() => starts[1]?.click());
    expect([...(container?.querySelectorAll('[data-core-task-key]') ?? [])]
      .map((owner) => owner.getAttribute('data-core-task-key')))
      .toEqual(expect.arrayContaining([
        'application.opportunity_fit:applicationId=7',
        'application.opportunity_fit:applicationId=8',
      ]));
  });

  it('requires an explicit interview choice when Pilot targets multiple interviews', async () => {
    state.events = [
      { id: 31, application_id: 7, event_type: 'interview', subtype: 'technical', status: 'scheduled', duration_minutes: 45, scheduled_at: '2026-07-24T10:00:00Z' },
      { id: 32, application_id: 7, event_type: 'interview', subtype: 'behavioral', status: 'scheduled', duration_minutes: 45, scheduled_at: '2026-07-24T11:00:00Z' },
    ];
    act(() => root?.render(
      <ApplicationDetail
        application={application}
        open
        onClose={vi.fn()}
        taskNow={Date.parse('2026-07-24T09:00:00Z')}
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
      { id: 31, application_id: 7, event_type: 'interview', subtype: 'technical', status: 'scheduled', duration_minutes: 45, scheduled_at: '2026-07-24T10:00:00Z' },
      { id: 32, application_id: 7, event_type: 'interview', subtype: 'behavioral', status: 'scheduled', duration_minutes: 45, scheduled_at: '2026-07-24T11:00:00Z' },
    ];
    act(() => root?.render(
      <ApplicationDetail
        application={application}
        open
        onClose={vi.fn()}
        pilotInterviewPreparationApplicationId={7}
        pilotInterviewPreparationEventId={32}
        taskNow={Date.parse('2026-07-24T09:00:00Z')}
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
