// @vitest-environment jsdom
import { act, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Application } from '@/types/application';
import { createCoreTaskSurfaceController } from '@/features/coreTaskSurface/controller';

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const state = vi.hoisted(() => ({
  analyzeJD: vi.fn(),
  saveApplicationJdVersion: vi.fn(),
  jdCurrent: null as unknown,
  jdHistory: [] as unknown[],
  jdDetail: null as unknown,
  materialKit: null as unknown,
  events: [] as unknown[],
  notes: [] as unknown[],
  queryErrors: new Set<string>(),
  queryLoading: new Set<string>(),
  queryFetching: new Set<string>(),
  refetch: vi.fn(),
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
  saveApplicationJdVersion: state.saveApplicationJdVersion,
}));
vi.mock('@tanstack/react-query', () => ({
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
  useQuery: (options: { queryKey?: unknown[] }) => ({
    data: options.queryKey?.[0] === 'events'
      ? state.events
      : options.queryKey?.[0] === 'notes'
        ? state.notes
      : options.queryKey?.[0] === 'application-jd-current'
        ? state.jdCurrent
        : options.queryKey?.[0] === 'application-jd-history'
          ? state.jdHistory
          : options.queryKey?.[0] === 'application-jd-detail'
            ? state.jdDetail
            : options.queryKey?.[0] === 'application-material-kit'
              ? state.materialKit
            : options.queryKey?.[0] === 'opportunity-fit-v2-reviews'
              ? []
              : null,
    isLoading: state.queryLoading.has(String(options.queryKey?.[0] ?? '')),
    isFetching: state.queryFetching.has(String(options.queryKey?.[0] ?? '')),
    isError: state.queryErrors.has(String(options.queryKey?.[0] ?? '')),
    refetch: state.refetch,
  }),
  useMutation: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock('./ApplicationDetail.module.css', () => ({ default: {
  jdHistoryOption: 'jdHistoryOption',
  jdHistoryPreview: 'jdHistoryPreview',
  jdHistoryDetail: 'jdHistoryDetail',
} }));
vi.mock('./PilotAttachmentHandle', () => ({ createPilotAttachmentDragBinding: () => ({}) }));
vi.mock('./ScheduleEventForm', () => ({ default: () => null }));
vi.mock('./ReviewFormDrawer', () => ({
  default: (props: { open?: boolean; note?: unknown }) => props.open
    ? <div data-testid="review-form-drawer">{props.note ? '编辑复盘' : '新建复盘'}</div>
    : null,
}));
vi.mock('./InterviewReviewProposalDrawer', () => ({
  default: (props: { open?: boolean; onClose?: () => void }) => props.open
    ? <div data-testid="review-proposal-drawer">复盘建议<button type="button" onClick={props.onClose}>关闭复盘建议</button></div>
    : null,
}));
vi.mock('./InterviewKnowledgeCaptureDrawer', () => ({
  default: () => null,
  createInterviewKnowledgeCaptureDraft: () => ({}),
}));
vi.mock('./InterviewPreparationProposalDrawer', () => ({ default: () => null }));
vi.mock('./MaterialKitDrawer', () => ({ default: () => null }));
vi.mock('./OpportunityFitReviewDrawer', () => ({ default: () => null }));
vi.mock('./ApplicationOutcomeDrawer', () => ({
  default: (props: { open?: boolean; application?: { company_name?: string } }) => props.open
    ? <div role="dialog">{props.application?.company_name} · 投递事实与结果工作区</div>
    : null,
}));
vi.mock('./NextStepSuggestions', () => ({ default: () => null }));
vi.mock('@ant-design/icons', () => ({
  ArrowLeftOutlined: () => null,
  CalendarOutlined: () => null,
  RobotOutlined: () => null,
  PlusOutlined: () => null,
  AudioOutlined: () => null,
  FileTextOutlined: () => null,
  DatabaseOutlined: () => null,
  MoreOutlined: () => null,
  HistoryOutlined: () => null,
  EditOutlined: () => null,
}));
vi.mock('antd', () => {
  const Form = Object.assign(
    (props: { children: ReactNode; onFinish?: (value: unknown) => void }) => (
      <form onSubmit={(event) => { event.preventDefault(); props.onFinish?.({}); }}>{props.children}</form>
    ),
    { Item: (props: { children: ReactNode }) => <label>{props.children}</label>, useForm: () => [{ resetFields: vi.fn() }] },
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
    Alert: (props: { message?: ReactNode; action?: ReactNode }) => <div role="alert">{props.message}{props.action}</div>,
    Button: ({ children, htmlType: _htmlType, loading: _loading, icon: _icon, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement> & { htmlType?: string; loading?: boolean; icon?: ReactNode }) => (
      <button {...props}>{children}</button>
    ),
    Modal: (props: { open?: boolean; title?: ReactNode; cancelText?: ReactNode; children: ReactNode }) => props.open ? (
      <div role="dialog">
        <h3>{props.title}</h3>
        {props.children}
        {props.cancelText && <button type="button">{props.cancelText}</button>}
      </div>
    ) : null,
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
  company_name: '示例公司',
  position_name: '后端工程师',
  job_url: 'https://external.example/job/7',
  status: 'pending',
  source: 'manual',
  notes: '',
  applied_at: '2026-07-21T00:00:00Z',
  created_at: '2026-07-21T00:00:00Z',
  updated_at: '2026-07-21T00:00:00Z',
};

let root: Root | undefined;
let container: HTMLDivElement | undefined;

beforeEach(() => {
  state.analyzeJD.mockReset();
  state.saveApplicationJdVersion.mockReset();
  state.jdCurrent = null;
  state.jdHistory = [];
  state.jdDetail = null;
  state.materialKit = null;
  state.events = [];
  state.notes = [];
  state.queryErrors.clear();
  state.queryLoading.clear();
  state.queryFetching.clear();
  state.refetch.mockReset();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
});

describe('ApplicationDetail deterministic Pilot JD entry', () => {
  function setSavedMaterials() {
    state.jdCurrent = { current: { id: 11, application_id: 7, version_number: 2, jd_text: '岗位职责：维护 API 服务。', source_url: null } };
    state.materialKit = { id: 21, application_id: 7, resume_id: 91, jd_version_id: 11, status: 'draft', updated_at: '2026-09-07T10:00:00Z' };
  }

  it.each([
    ['pending', '继续准备'],
    ['applied', '查看投递材料'],
    ['written_test', '查看投递材料'],
    ['interview', '查看投递材料'],
  ] as const)('uses stage-appropriate material copy for %s without claiming a submitted resume', (status, action) => {
    setSavedMaterials();
    act(() => root?.render(<ApplicationDetail application={{ ...application, status }} open onClose={vi.fn()} />));
    const panel = container?.querySelector('#application-preparation-panel');
    expect(panel?.textContent).toContain(action);
    expect(panel?.textContent).toContain('关联材料');
    expect(panel?.textContent).not.toContain('已提交简历');
    if (status !== 'pending') expect(panel?.textContent).not.toContain('完成提交前检查');
  });

  it('does not present a legacy submitted marker as verified submission evidence', () => {
    setSavedMaterials();
    state.materialKit = { ...(state.materialKit as object), status: 'submitted' };
    act(() => root?.render(<ApplicationDetail application={application} open onClose={vi.fn()} />));
    const materials = container?.querySelector('[aria-labelledby="application-linked-materials-heading"]');
    expect(materials?.textContent).toContain('旧投递标记，缺少证据快照');
    expect(materials?.textContent).not.toContain('已记录投递');
  });

  it('shows only the resume linked by the scoped material kit and preserves the task owner', () => {
    setSavedMaterials();
    const controller = createCoreTaskSurfaceController();
    const resumes = [
      { id: 90, title: '另一份简历', deleted_at: null },
      { id: 91, title: '岗位专用简历', deleted_at: null },
    ] as never;
    act(() => root?.render(<ApplicationDetail application={{ ...application, status: 'applied' }} open onClose={vi.fn()} resumes={resumes} taskController={controller} />));
    const materials = container?.querySelector('[aria-labelledby="application-linked-materials-heading"]');
    expect(materials?.textContent).toContain('关联简历');
    expect(materials?.textContent).toContain('岗位专用简历');
    expect(materials?.textContent).not.toContain('另一份简历');
    const entry = [...(materials?.querySelectorAll('button') ?? [])].find((button) => button.textContent === '查看投递材料');
    expect(entry).toBeDefined();
    act(() => entry?.click());
    const firstOwner = controller.getState().active;
    expect(firstOwner?.key).toBe('application.material_kit:applicationId=7');
    act(() => entry?.click());
    expect(controller.getState().active?.generation).toBe(firstOwner?.generation);
    expect(state.analyzeJD).not.toHaveBeenCalled();
  });

  it.each([null, '', '   '])('omits the JD source control when its value is %s', (source_url) => {
    setSavedMaterials();
    state.jdCurrent = { current: { ...(state.jdCurrent as { current: object }).current, source_url } };
    act(() => root?.render(<ApplicationDetail application={application} open onClose={vi.fn()} />));
    expect(container?.querySelector('#application-preparation-panel')?.textContent).not.toContain('复制来源');
  });

  it('keeps the real source visible and lets readers expand the original JD', () => {
    setSavedMaterials();
    const jdText = '岗位职责：\n' + Array.from({ length: 24 }, (_, index) => `${index + 1}. 维护服务，核对接口来源。`).join('\n');
    state.jdCurrent = { current: { ...(state.jdCurrent as { current: object }).current, jd_text: jdText, source_url: 'https://example.com/jobs/7' } };
    act(() => root?.render(<ApplicationDetail application={application} open onClose={vi.fn()} />));
    const panel = container?.querySelector('#application-preparation-panel');
    expect(panel?.textContent).toContain('当前 JD · 版本 2');
    expect(panel?.textContent).toContain('https://example.com/jobs/7');
    const expand = [...(panel?.querySelectorAll('button') ?? [])].find((button) => button.textContent === '展开全文');
    expect(expand).toBeDefined();
    expect(expand?.getAttribute('aria-expanded')).toBe('false');
    act(() => expand?.click());
    expect(expand?.getAttribute('aria-expanded')).toBe('true');
    expect(panel?.querySelector('#application-jd-text h5')?.textContent).toBe('岗位职责');
    expect([...panel!.querySelectorAll('#application-jd-text li')].map((item) => item.textContent)).toEqual(jdText.split('\n').slice(1));
  });

  it('does not send users into an empty interview review chooser', () => {
    act(() => root?.render(<ApplicationDetail application={{ ...application, status: 'applied' }} open onClose={vi.fn()} />));
    const panel = container?.querySelector('#application-preparation-panel');
    expect(panel?.textContent).toContain('尚无可复盘的面试');
    expect(panel?.textContent).not.toContain('选择面试并开始复盘');
  });

  it('does not expose another application material kit as linked materials', () => {
    setSavedMaterials();
    state.materialKit = { ...(state.materialKit as object), application_id: 999, resume_id: 91 };
    act(() => root?.render(<ApplicationDetail application={application} open onClose={vi.fn()} resumes={[{ id: 91, title: '其他投递的材料', deleted_at: null }] as never} />));
    const materials = container?.querySelector('[aria-labelledby="application-linked-materials-heading"]');
    expect(materials?.textContent).toContain('材料归属暂不可确认');
    expect(materials?.textContent).not.toContain('其他投递的材料');
  });

  it('keeps material read errors distinct from having no saved material', () => {
    state.queryErrors.add('application-material-kit');
    act(() => root?.render(<ApplicationDetail application={application} open onClose={vi.fn()} />));
    const materials = container?.querySelector('[aria-labelledby="application-linked-materials-heading"]');
    expect(materials?.textContent).toContain('投递材料暂时无法读取');
    expect(materials?.textContent).not.toContain('尚未保存投递材料');
  });

  it('does not ask for another resume while the locked preparation source is still loading', () => {
    const controller = createCoreTaskSurfaceController();
    controller.launch({ ref: { taskId: 'application.interview_prepare', applicationId: 7, eventId: 31 }, source: 'application_task_card' });
    state.events = [{ id: 31, application_id: 7, event_type: 'interview', status: 'todo', scheduled_at: '2027-01-01T00:00:00Z', duration_minutes: 60 }];
    act(() => root?.render(<ApplicationDetail application={{ ...application, status: 'interview' }} open onClose={vi.fn()} taskController={controller} resumesLoading />));
    expect(container?.textContent).toContain('正在核对面试资料');
    expect(container?.textContent).not.toContain('请先在面试准备中心选择一份可用简历');
  });

  it('waits for the scoped event refresh before rejecting an exact review handoff', () => {
    const controller = createCoreTaskSurfaceController();
    controller.launch({ ref: { taskId: 'application.interview_review', applicationId: 7, eventId: 31 }, source: 'application_task_card' });
    state.events = [{ id: 31, application_id: 7, event_type: 'interview', status: 'todo', scheduled_at: '2027-01-01T00:00:00Z', duration_minutes: 60 }];
    state.queryFetching.add('events');
    const draw = () => act(() => root?.render(<ApplicationDetail application={{ ...application, status: 'interview' }} open onClose={vi.fn()} taskController={controller} />));
    draw();
    expect(container?.textContent).toContain('正在核对面试资料');
    expect(container?.textContent).not.toContain('该面试当前不可复盘');
    state.queryFetching.clear();
    state.events = [{ ...(state.events[0] as object), status: 'done' }];
    state.notes = [{ id: 51, application_id: 7, application_event_id: 31 }];
    draw();
    expect(container?.querySelector('[data-testid="review-proposal-drawer"]')).not.toBeNull();
  });

  it('shows an unscheduled audit event without inventing a date or zero-minute appointment', () => {
    state.events = [{ id: 61, application_id: application.id, event_type: 'custom', status: 'done', scheduled_at: '', duration_minutes: 0, notes: '已接受材料修改建议' }];
    act(() => root?.render(<ApplicationDetail application={application} open onClose={vi.fn()} />));
    expect(container?.textContent).not.toContain('Invalid Date');
    expect(container?.textContent).not.toContain('时长 0 分钟');
    expect(container?.textContent).toContain('时间待确认');
  });

  const completedInterview = (id: number) => ({
    id,
    application_id: application.id,
    event_type: 'interview',
    subtype: id === 31 ? '技术面' : '行为面',
    scheduled_at: `2026-08-${id === 31 ? '20' : '21'}T10:00:00Z`,
    duration_minutes: 45,
    status: 'done',
  });

  it('keeps an application-only review intent in an explicit zero-event safe state', async () => {
    const consumed = vi.fn();
    act(() => root?.render(
      <ApplicationDetail
        application={application}
        open
        onClose={vi.fn()}
        pilotInterviewReviewApplicationId={application.id}
        onPilotInterviewReviewFocusConsumed={consumed}
      />,
    ));
    await act(async () => { await Promise.resolve(); });

    expect(container?.querySelector('[role="dialog"]')?.textContent).toContain('选择要复盘的面试');
    expect(container?.textContent).toContain('当前没有可复盘的面试');
    expect(container?.querySelector('[data-core-task-owner]')).toBeNull();
    expect(consumed).toHaveBeenCalledOnce();
  });

  it('requires an explicit click for a singleton review event', async () => {
    state.events = [completedInterview(31)];
    act(() => root?.render(
      <ApplicationDetail
        application={application}
        open
        onClose={vi.fn()}
        pilotInterviewReviewApplicationId={application.id}
        onPilotInterviewReviewFocusConsumed={vi.fn()}
      />,
    ));
    await act(async () => { await Promise.resolve(); });

    const dialog = container?.querySelector('[role="dialog"]');
    expect(dialog?.querySelectorAll('button')).toHaveLength(1);
    expect(container?.querySelector('[data-core-task-owner]')).toBeNull();
    act(() => (dialog?.querySelector('button') as HTMLButtonElement).click());

    expect(container?.querySelector('[data-core-task-key]')?.getAttribute('data-core-task-key'))
      .toBe('application.interview_review:applicationId=7:eventId=31');
    expect(container?.querySelector('[data-testid="review-form-drawer"]')).not.toBeNull();
  });

  it('keeps many review choices event-scoped and never falls back to general review', async () => {
    state.events = [completedInterview(31), completedInterview(32)];
    act(() => root?.render(
      <ApplicationDetail
        application={application}
        open
        onClose={vi.fn()}
        pilotInterviewReviewApplicationId={application.id}
        onPilotInterviewReviewFocusConsumed={vi.fn()}
      />,
    ));
    await act(async () => { await Promise.resolve(); });

    const dialogButtons = [...(container?.querySelectorAll('[role="dialog"] button') ?? [])];
    expect(dialogButtons).toHaveLength(2);
    act(() => (dialogButtons[1] as HTMLButtonElement).click());

    expect(container?.querySelector('[data-core-task-key]')?.getAttribute('data-core-task-key'))
      .toBe('application.interview_review:applicationId=7:eventId=32');
    expect(container?.querySelector('[data-core-task-key]')?.getAttribute('data-core-task-key'))
      .not.toContain('application.general_review');
  });

  it('opens general review only for an explicit null event id and rejects missing ids', () => {
    state.notes = [{ id: 61, application_event_id: null, date: '2026-08-20' }];
    act(() => root?.render(<ApplicationDetail application={application} open onClose={vi.fn()} />));
    const openGeneral = [...(container?.querySelectorAll('button') ?? [])]
      .find((button) => button.textContent === '打开复盘');
    act(() => (openGeneral as HTMLButtonElement).click());
    expect(container?.querySelector('[data-core-task-key]')?.getAttribute('data-core-task-key'))
      .toBe('application.general_review:applicationId=7');

    act(() => root?.unmount());
    state.notes = [{ id: 62, date: '2026-08-21' }];
    root = createRoot(container!);
    act(() => root?.render(<ApplicationDetail application={application} open onClose={vi.fn()} />));
    expect(container?.querySelector('[data-task-id="application.general_review"]')).toBeNull();
    expect(container?.querySelector('[data-core-task-key]')).toBeNull();
  });

  it('keeps failed JD, event, review, and Offer sources visible instead of presenting false empty states', () => {
    state.queryErrors.add('application-jd-current');
    state.queryErrors.add('events');
    state.queryErrors.add('notes');
    const retryOffers = vi.fn();

    act(() => root?.render(
      <ApplicationDetail
        application={application}
        open
        onClose={vi.fn()}
        offersError
        onRetryOffers={retryOffers}
      />,
    ));

    expect(container?.textContent).toContain('岗位资料暂时无法读取');
    expect(container?.textContent).toContain('日程暂时无法读取');
    expect(container?.textContent).toContain('面试复盘暂时无法读取');
    expect(container?.textContent).toContain('部分 Offer 进展暂时无法读取');
    expect(container?.textContent).not.toContain('尚未确认岗位描述');
    expect(container?.textContent).toContain('下一步时间：暂时无法读取');
    expect(container?.textContent).not.toContain('下一步时间：待安排');
    const editJd = [...(container?.querySelectorAll('button') ?? [])]
      .find((button) => button.textContent?.includes('暂不可编辑')) as HTMLButtonElement | undefined;
    expect(editJd?.disabled).toBe(true);
  });

  it('keeps the JD write entry disabled until the current-version source finishes loading', () => {
    state.queryLoading.add('application-jd-current');

    act(() => root?.render(<ApplicationDetail application={application} open onClose={vi.fn()} />));

    const editJd = [...(container?.querySelectorAll('button') ?? [])]
      .find((button) => button.textContent?.includes('读取中')) as HTMLButtonElement | undefined;
    expect(editJd?.disabled).toBe(true);
  });

  it('does not offer a write action while interview events or reviews are loading', () => {
    state.queryLoading.add('events');
    state.queryLoading.add('notes');
    const interviewApplication = { ...application, status: 'interview' } as never;

    act(() => root?.render(<ApplicationDetail application={interviewApplication} open onClose={vi.fn()} />));

    const primary = [...(container?.querySelectorAll('button') ?? [])]
      .find((button) => button.textContent?.includes('等待面试进展加载')) as HTMLButtonElement | undefined;
    expect(primary).not.toBeUndefined();
    expect(primary?.disabled).toBe(true);
    expect(container?.textContent).toContain('下一时间读取中');
    expect(container?.textContent).not.toContain('下一时间待安排');
    expect(container?.querySelector('[data-testid="review-form-drawer"]')).toBeNull();
  });

  it('replaces the interview write action with retries when required queries fail', () => {
    state.queryErrors.add('events');
    state.queryErrors.add('notes');
    const interviewApplication = { ...application, status: 'interview' } as never;

    act(() => root?.render(<ApplicationDetail application={interviewApplication} open onClose={vi.fn()} />));

    const retry = [...(container?.querySelectorAll('button') ?? [])]
      .find((button) => button.textContent?.includes('重试日程和复盘')) as HTMLButtonElement | undefined;
    expect(retry).not.toBeUndefined();
    expect(retry?.disabled).toBe(false);
    act(() => retry?.click());
    expect(state.refetch).toHaveBeenCalledTimes(2);
    expect(container?.querySelector('[data-testid="review-form-drawer"]')).toBeNull();
  });

  it.each(['cancelled', 'deleted', 'soft_deleted'])('does not let %s events drive interview history or preparation', (status) => {
    state.events = [{
      id: 31,
      application_id: application.id,
      event_type: 'interview',
      subtype: '一面',
      tags: [],
      round: 1,
      scheduled_at: '2099-01-01T00:00:00Z',
      duration_minutes: 45,
      location: '线上',
      notes: '',
      status,
      created_at: '2025-12-20T00:00:00Z',
    }];
    state.notes = [{ id: 51, application_event_id: 31 }];
    const interviewApplication = { ...application, status: 'interview' } as never;

    act(() => root?.render(<ApplicationDetail application={interviewApplication} open onClose={vi.fn()} />));

    expect(container?.textContent).toContain('已约面试');
    expect(container?.textContent).not.toContain('面试结束');
    expect(container?.textContent).toContain('该面试已结束或取消');
    expect(container?.textContent).toContain('下一步时间：待安排');
    expect([...((container?.querySelectorAll('button') ?? []))].some((button) => button.textContent?.includes('查看复盘'))).toBe(false);
    expect([...((container?.querySelectorAll('button') ?? []))].some((button) => button.textContent?.includes('面试准备建议'))).toBe(false);
    expect(container?.querySelector('[data-testid="review-proposal-drawer"]')).toBeNull();
  });

  it('opens an existing interview review instead of opening the create form', () => {
    state.events = [{
      id: 31,
      application_id: application.id,
      event_type: 'interview',
      subtype: '一面',
      tags: [],
      round: 1,
      scheduled_at: '2026-01-01T00:00:00Z',
      duration_minutes: 45,
      location: '线上',
      notes: '',
      status: 'done',
      created_at: '2025-12-20T00:00:00Z',
    }];
    state.notes = [{ id: 51, application_event_id: 31, round: '一面' }];
    const interviewApplication = { ...application, status: 'interview' } as never;

    act(() => root?.render(<ApplicationDetail application={interviewApplication} open onClose={vi.fn()} />));
    const primary = [...(container?.querySelectorAll('button') ?? [])]
      .find((button) => button.textContent?.includes('查看本轮复盘'));
    expect(primary).not.toBeUndefined();
    act(() => (primary as HTMLButtonElement).click());

    expect(container?.querySelector('[data-testid="review-proposal-drawer"]')).not.toBeNull();
    expect(container?.querySelector('[data-testid="review-form-drawer"]')).toBeNull();
  });

  it('preserves an exact pending Review owner through the real Drawer close callback', () => {
    state.events = [{
      id: 31, application_id: application.id, event_type: 'interview', subtype: '一面', tags: [], round: 1,
      scheduled_at: '2026-01-01T00:00:00Z', duration_minutes: 45, location: '线上', notes: '', status: 'done',
      created_at: '2025-12-20T00:00:00Z',
    }];
    state.notes = [{ id: 51, application_id: application.id, application_event_id: 31, round: '一面' }];
    const controller = createCoreTaskSurfaceController();
    const request = { ref: { taskId: 'application.interview_review' as const, applicationId: application.id, eventId: 31 }, source: 'application_task_card' as const };
    const first = controller.launch(request);
    if (first.kind !== 'launched') throw new Error('review launch failed');
    controller.markOpen(first.generation);
    const frozenInput = {
      proposal_id: 61, focus_id: 'focus-1', expected_note_revision: 4, expected_candidate_fingerprint: 'a'.repeat(64),
      idempotency_key: '00000000-0000-4000-8000-000000000051', user_note: '冻结正文',
    };
    const draft = {
      ownerKey: `review:${first.generation}:51:61`, ownerGeneration: first.generation, noteId: 51, proposalId: 61,
      applicationId: application.id, selectedFocusId: 'focus-1', userNote: '冻结正文', idempotencyKey: frozenInput.idempotency_key,
      frozenProposalInput: frozenInput, proposalUnknown: true, actionDraft: null,
    };
    act(() => root?.render(<ApplicationDetail
      application={{ ...application, status: 'interview' } as never} open onClose={vi.fn()} taskController={controller}
      reviewReadinessDrafts={{ [draft.ownerKey]: draft }}
    />));
    const closeReview = [...(container?.querySelectorAll('button') ?? [])]
      .find((button) => button.textContent === '关闭复盘建议') as HTMLButtonElement | undefined;
    if (!closeReview) throw new Error('review Drawer close should render');
    act(() => closeReview.click());
    const owner = container?.querySelector('[data-core-task-owner]') as HTMLElement;
    act(() => owner.dispatchEvent(new Event('animationend', { bubbles: true })));
    let reopened: ReturnType<typeof controller.launch> | undefined;
    act(() => { reopened = controller.launch(request); });
    if (!reopened) throw new Error('review relaunch should produce a result');
    if (reopened.kind !== 'launched') throw new Error('review relaunch failed');
    expect(controller.getState().active).toMatchObject({ recoveryGeneration: first.generation, key: 'application.interview_review:applicationId=7:eventId=31' });
    expect(draft.frozenProposalInput).toEqual(frozenInput);
  });

  it('maps event subtype and status enums to user-facing progress copy', () => {
    state.events = [{
      id: 12,
      application_id: application.id,
      event_type: 'written_test',
      subtype: 'assessment',
      tags: [],
      round: 0,
      scheduled_at: '2026-08-20T10:00:00Z',
      duration_minutes: 60,
      location: '',
      notes: '',
      status: 'todo',
      created_at: '2026-08-01T00:00:00Z',
    }];

    act(() => root?.render(<ApplicationDetail application={application} open onClose={vi.fn()} />));

    expect(container?.textContent).toContain('笔试 · 测评');
    expect(container?.textContent).toContain('待处理');
    expect(container?.textContent).not.toContain('assessment');
    expect(container?.textContent).not.toContain('todo');
  });

  it('renders the JD editor labels as Chinese text instead of escape sequences', () => {
    act(() => root?.render(<ApplicationDetail application={application} open onClose={vi.fn()} />));

    const addButton = [...(container?.querySelectorAll('button') ?? [])]
      .find((button) => button.textContent?.includes('\u6dfb\u52a0 JD'));
    expect(addButton).not.toBeUndefined();
    act(() => (addButton as HTMLButtonElement).click());

    const dialog = container?.querySelector('[role="dialog"]');
    expect(dialog?.textContent).toContain('\u6295\u9012\u5c97\u4f4d\u8d44\u6599');
    expect(dialog?.textContent).toContain('\u53d6\u6d88');
    expect(dialog?.textContent).not.toContain('\\u6295');
    expect(dialog?.querySelector('textarea')?.placeholder).toBe('\u7c98\u8d34\u5c97\u4f4d\u63cf\u8ff0');
    expect(dialog?.querySelector('input')?.placeholder).toBe('\u6765\u6e90 URL\uff08\u4ec5\u5c55\u793a\uff0c\u4e0d\u4f1a\u8bbf\u95ee\uff09');
  });

  it('uses a save shortcut without a current JD and never calls the JD save service', () => {
    const onAskPilot = vi.fn();
    act(() => root?.render(<ApplicationDetail application={application} open onClose={vi.fn()} onAskPilot={onAskPilot} />));

    const shortcut = [...(container?.querySelectorAll('button') ?? [])]
      .find((button) => button.textContent?.includes('让 Haru 帮我'));
    expect(shortcut).not.toBeUndefined();
    act(() => (shortcut as HTMLButtonElement).click());

    expect(onAskPilot).toHaveBeenCalledWith(application, { type: 'application_jd_save' });
    expect(state.saveApplicationJdVersion).not.toHaveBeenCalled();
    expect(state.analyzeJD).not.toHaveBeenCalled();
  });

  it('uses an update shortcut when a current JD exists', () => {
    state.jdCurrent = { current: { id: 41, jd_text: '已保存 JD', source_url: null } };
    const onAskPilot = vi.fn();
    act(() => root?.render(<ApplicationDetail application={application} open onClose={vi.fn()} onAskPilot={onAskPilot} />));

    const shortcut = [...(container?.querySelectorAll('button') ?? [])]
      .find((button) => button.textContent?.includes('让 Haru 帮我'));
    expect(shortcut).not.toBeUndefined();
    act(() => (shortcut as HTMLButtonElement).click());

    expect(onAskPilot).toHaveBeenCalledWith(application, { type: 'application_jd_save' });
    expect(state.saveApplicationJdVersion).not.toHaveBeenCalled();
  });

  it('opens the application outcome workspace from the mounted detail view', () => {
    act(() => root?.render(<ApplicationDetail application={application} open onClose={vi.fn()} />));

    const button = [...(container?.querySelectorAll('button') ?? [])]
      .find((candidate) => candidate.textContent?.includes('投递事实与结果'));
    expect(button).not.toBeUndefined();
    act(() => (button as HTMLButtonElement).click());

    expect(container?.querySelector('[role="dialog"]')?.textContent)
      .toContain('示例公司 · 投递事实与结果工作区');
  });

  it('routes the Offer stage primary action to the Offer workspace', () => {
    const onOpenOffers = vi.fn();
    const offerApplication = { ...application, status: 'offer' } as never;
    act(() => root?.render(<ApplicationDetail application={offerApplication} open onClose={vi.fn()} onOpenOffers={onOpenOffers} />));

    const button = [...(container?.querySelectorAll('button') ?? [])]
      .find((candidate) => candidate.textContent?.includes('查看 Offer 与截止时间'));
    expect(button).not.toBeUndefined();
    act(() => (button as HTMLButtonElement).click());

    expect(onOpenOffers).toHaveBeenCalledOnce();
    expect(container?.textContent).not.toContain('投递事实与结果工作区');
  });

  it('keeps a completed interview in the completed stage when its review exists', () => {
    state.events = [{
      id: 31,
      application_id: application.id,
      event_type: 'interview',
      scheduled_at: '2026-01-01T00:00:00Z',
      duration_minutes: 45,
      status: 'done',
    }];
    state.notes = [{ id: 51, application_event_id: 31 }];
    const interviewApplication = { ...application, status: 'interview' } as never;
    act(() => root?.render(<ApplicationDetail application={interviewApplication} open onClose={vi.fn()} />));

    expect(container?.textContent).toContain('面试结束');
    expect(container?.textContent).toContain('查看本轮复盘');
    expect(container?.textContent).not.toContain('准备本轮面试');
  });

  it('uses the resolver primary interview task when completed and upcoming events coexist', () => {
    const now = Date.parse('2026-08-29T10:00:00Z');
    state.events = [
      {
        id: 31,
        application_id: application.id,
        event_type: 'interview',
        scheduled_at: '2026-08-29T08:00:00Z',
        duration_minutes: 45,
        status: 'done',
      },
      {
        id: 32,
        application_id: application.id,
        event_type: 'interview',
        scheduled_at: '2026-08-29T11:00:00Z',
        duration_minutes: 45,
        status: 'todo',
      },
    ];
    const interviewApplication = { ...application, status: 'interview' } as never;

    act(() => root?.render(
      <ApplicationDetail application={interviewApplication} open onClose={vi.fn()} taskNow={now} />,
    ));

    const prepare = [...(container?.querySelectorAll('button') ?? [])]
      .find((button) => button.textContent === '准备本轮面试') as HTMLButtonElement | undefined;
    expect(prepare).not.toBeUndefined();
    expect(container?.textContent).not.toContain('完成面试复盘');
    expect(container?.querySelectorAll('button[type="primary"]')).toHaveLength(1);
    act(() => prepare?.click());
    expect(container?.querySelector('[data-core-task-key]')?.getAttribute('data-core-task-key'))
      .toBe('application.interview_prepare:applicationId=7:eventId=32');
  });

  it('disables lower task cards when a higher-priority source issue blocks execution', () => {
    state.events = [
      {
        id: 31,
        application_id: 99,
        event_type: 'interview',
        scheduled_at: '2026-08-29T11:00:00Z',
        duration_minutes: 45,
        status: 'todo',
      },
      {
        id: 32,
        application_id: application.id,
        event_type: 'interview',
        scheduled_at: '2026-08-29T08:00:00Z',
        duration_minutes: 45,
        status: 'done',
      },
    ];
    state.notes = [{ id: 61, application_event_id: null, date: '2026-08-20' }];
    const interviewApplication = { ...application, status: 'interview' } as never;

    act(() => root?.render(
      <ApplicationDetail
        application={interviewApplication}
        open
        onClose={vi.fn()}
        taskNow={Date.parse('2026-08-29T10:00:00Z')}
      />,
    ));

    const card = container?.querySelector('[data-task-id="application.general_review"]');
    const openReview = card?.querySelector('button') as HTMLButtonElement | null;
    expect(openReview?.disabled).toBe(true);
    const eventReview = [...(container?.querySelectorAll('button') ?? [])]
      .find((button) => button.textContent === '记录复盘') as HTMLButtonElement | undefined;
    expect(eventReview?.disabled).toBe(true);
    act(() => openReview?.click());
    expect(container?.querySelector('[data-core-task-key]')).toBeNull();
  });

  it('supports keyboard tabs and keeps the progress projection read-only', () => {
    state.events = [{
      id: 31,
      event_type: 'interview',
      subtype: '一面',
      scheduled_at: '2026-08-20T10:00:00Z',
      status: 'done',
      location: '线上',
      notes: '完成面试',
    }];
    const linkedOffer = {
      id: 18,
      application_id: application.id,
      company_name: application.company_name,
      position_name: application.position_name,
      status: 'pending' as const,
      base_monthly: 0,
      months_per_year: 12,
      signing_bonus: 0,
      equity: '',
      perks: '',
      deadline: '2026-09-01T00:00:00Z',
      notes: '',
      assessment: '',
      total_cash: 0,
      created_at: '2026-08-20T00:00:00Z',
      updated_at: '2026-08-20T00:00:00Z',
    };
    act(() => root?.render(<ApplicationDetail application={application} offers={[linkedOffer]} open onClose={vi.fn()} />));

    const tabs = [...(container?.querySelectorAll<HTMLButtonElement>('[role="tab"]') ?? [])];
    expect(tabs.map((tab) => tab.textContent)).toEqual(['概览', '准备', '进展']);
    expect(tabs[0]?.getAttribute('aria-selected')).toBe('true');

    act(() => tabs[0]?.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true })));
    expect(tabs[1]?.getAttribute('aria-selected')).toBe('true');
    act(() => tabs[1]?.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true })));
    expect(tabs[2]?.getAttribute('aria-selected')).toBe('true');

    const progressPanel = container?.querySelector<HTMLElement>('[aria-labelledby="application-progress-tab"]');
    expect(progressPanel?.textContent).toContain('进展时间线');
    expect(progressPanel?.textContent).toContain('Offer · 示例公司 · 后端工程师');
    expect(progressPanel?.querySelector('button')).toBeNull();
  });

  it('renders long JD history previews and details in dedicated wrapping containers', () => {
    const longPreview = '职位：高级后端工程师，负责高并发 API 设计、微服务治理与可观测性建设。要求熟悉 Python、FastAPI、PostgreSQL，链接 https://example.com/jobs/backend-platform-observability-with-a-very-long-token';
    state.jdHistory = [{
      id: 41,
      version_number: 2,
      source_kind: 'pilot',
      preview: longPreview,
    }];
    state.jdDetail = { id: 41, jd_text: `${longPreview}\n${'ContinuousEnglishToken'.repeat(12)}` };
    act(() => root?.render(<ApplicationDetail application={application} open onClose={vi.fn()} />));

    const historyButton = [...(container?.querySelectorAll('button') ?? [])]
      .find((button) => button.textContent === '查看历史');
    act(() => historyButton?.click());

    const dialog = [...(container?.querySelectorAll('[role="dialog"]') ?? [])]
      .find((candidate) => candidate.textContent?.includes('岗位资料历史'));
    expect(dialog?.querySelector('.jdHistoryOption')).not.toBeNull();
    expect(dialog?.querySelector('.jdHistoryPreview')?.textContent).toContain('FastAPI');

    const versionButton = dialog?.querySelector<HTMLButtonElement>('.jdHistoryOption');
    act(() => versionButton?.click());
    expect(dialog?.querySelector('.jdHistoryDetail')?.textContent).toContain('ContinuousEnglishToken');
  });
});
