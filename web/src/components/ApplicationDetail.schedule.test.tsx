// @vitest-environment jsdom
import { act, forwardRef, type ReactNode, type Ref } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Application } from '@/types/application';

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const state = vi.hoisted(() => ({
  refetch: vi.fn(),
}));

vi.mock('@/services/ai', () => ({ analyzeJD: vi.fn() }));
vi.mock('@/services/notes', () => ({
  listNotesByApp: vi.fn().mockResolvedValue([]),
  createNote: vi.fn(),
  deleteNote: vi.fn(),
  updateNote: vi.fn(),
}));
vi.mock('@/services/events', () => ({
  listEvents: vi.fn().mockResolvedValue([]),
}));
vi.mock('@/services/applicationJdVersions', () => ({
  getCurrentApplicationJd: vi.fn(),
  getApplicationJdVersion: vi.fn(),
  listApplicationJdVersions: vi.fn(),
  saveApplicationJdVersion: vi.fn(),
}));
vi.mock('@/services/materialKits', () => ({ getApplicationMaterialKit: vi.fn().mockResolvedValue(null) }));
vi.mock('@/services/opportunityFitReviews', () => ({ listOpportunityFitV2Reviews: vi.fn().mockResolvedValue([]) }));
vi.mock('@tanstack/react-query', () => ({
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
  useQuery: (options: { queryKey?: unknown[] }) => ({
    data: options.queryKey?.[0] === 'events'
      ? []
      : options.queryKey?.[0] === 'notes'
        ? []
        : options.queryKey?.[0] === 'application-jd-current'
          ? null
          : options.queryKey?.[0] === 'application-jd-history'
            ? []
            : options.queryKey?.[0] === 'application-jd-detail'
              ? null
              : options.queryKey?.[0] === 'opportunity-fit-v2-reviews'
                ? []
                : null,
    isLoading: false,
    isError: false,
    refetch: state.refetch,
  }),
  useMutation: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock('./ApplicationDetail.module.css', () => ({
  default: new Proxy({}, { get: (_target, property) => String(property) }),
}));
vi.mock('./PilotAttachmentHandle', () => ({ createPilotAttachmentDragBinding: () => ({}) }));
vi.mock('./ScheduleEventForm', () => ({
  default: (props: {
    open?: boolean;
    headingRef?: Ref<HTMLHeadingElement>;
    onClose?: () => void;
    onSuccess?: () => void;
  }) => props.open ? (
    <section data-testid="schedule-event-form" aria-label="新建日程">
      <h2 ref={props.headingRef} tabIndex={-1}>新建日程</h2>
      <button type="button" data-testid="schedule-cancel" onClick={props.onClose}>取消</button>
      <button
        type="button"
        data-testid="schedule-success"
        onClick={() => {
          props.onSuccess?.();
          props.onClose?.();
        }}
      >
        模拟创建成功
      </button>
    </section>
  ) : null,
}));
vi.mock('./ReviewFormDrawer', () => ({ default: () => null }));
vi.mock('./InterviewReviewProposalDrawer', () => ({ default: () => null }));
vi.mock('./InterviewKnowledgeCaptureDrawer', () => ({
  default: () => null,
  createInterviewKnowledgeCaptureDraft: () => ({}),
}));
vi.mock('./InterviewPreparationProposalDrawer', () => ({ default: () => null }));
vi.mock('./MaterialKitDrawer', () => ({ default: () => null }));
vi.mock('./OpportunityFitReviewDrawer', () => ({ default: () => null }));
vi.mock('./ApplicationOutcomeDrawer', () => ({ default: () => null }));
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
  const Button = forwardRef<HTMLButtonElement, React.ButtonHTMLAttributes<HTMLButtonElement> & { htmlType?: string; loading?: boolean; icon?: ReactNode }>(
    ({ children, htmlType: _htmlType, loading: _loading, icon: _icon, ...props }, ref) => (
      <button ref={ref} {...props}>{children}</button>
    ),
  );
  return {
    Dropdown: (props: { children: ReactNode; menu?: { items?: Array<{ key: string; label: ReactNode; onClick?: () => void }> } }) => (
      <>{props.children}{props.menu?.items?.map((item) => (
        <button key={item.key} type="button" data-testid={`dropdown-item-${item.key}`} onClick={item.onClick}>{item.label}</button>
      ))}</>
    ),
    Alert: (props: { message?: ReactNode; action?: ReactNode }) => <div role="alert">{props.message}{props.action}</div>,
    Button,
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
    message: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
  };
});

const { default: ApplicationDetail } = await import('./ApplicationDetail');

const application: Application = {
  id: 7,
  company_name: '示例公司',
  position_name: '后端工程师',
  job_url: 'https://external.example/job/7',
  status: 'applied',
  source: 'manual',
  notes: '',
  applied_at: '2026-07-21T00:00:00Z',
  created_at: '2026-07-21T00:00:00Z',
  updated_at: '2026-07-21T00:00:00Z',
};

let root: Root | undefined;
let container: HTMLDivElement | undefined;
let content: HTMLDivElement | undefined;
let scrollIntoView: ReturnType<typeof vi.fn>;

function renderDetail() {
  content = document.createElement('div');
  content.className = 'op-app-content';
  content.tabIndex = -1;
  container = document.createElement('div');
  content.appendChild(container);
  document.body.appendChild(content);
  root = createRoot(container);
  act(() => root?.render(<ApplicationDetail application={application} open onClose={vi.fn()} />));
}

function button(testId: string): HTMLButtonElement {
  const target = document.querySelector<HTMLButtonElement>(`[data-testid="${testId}"]`);
  if (!target) throw new Error(`missing ${testId}`);
  return target;
}

beforeEach(() => {
  state.refetch.mockReset();
  scrollIntoView = vi.fn();
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
    configurable: true,
    value: scrollIntoView,
  });
  window.scrollTo = vi.fn();
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: query === '(prefers-reduced-motion: reduce)',
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  });
});

afterEach(() => {
  act(() => root?.unmount());
  content?.remove();
  root = undefined;
  container = undefined;
  content = undefined;
});

describe('ApplicationDetail schedule task surface', () => {
  it('replaces the detail workspace, resets the real app scroller, and focuses the form heading', () => {
    renderDetail();
    content!.scrollTop = 240;
    button('application-stage-action').focus();

    act(() => button('application-stage-action').click());

    expect(document.querySelector('[data-testid="schedule-event-form"]')).not.toBeNull();
    expect(document.querySelector('[role="tablist"]')).toBeNull();
    expect(content?.scrollTop).toBe(0);
    expect(document.activeElement).toBe(document.querySelector('[data-testid="schedule-event-form"] h2'));
    expect(window.scrollTo).not.toHaveBeenCalled();
  });

  it('restores the prior tab, app scroller position, and trigger focus on cancel for each entry point', () => {
    renderDetail();
    const progressTab = [...document.querySelectorAll<HTMLButtonElement>('[role="tab"]')]
      .find((candidate) => candidate.textContent === '进展');
    act(() => progressTab?.click());
    content!.scrollTop = 327;

    for (const { triggerId, expectedFocusId } of [
      { triggerId: 'application-stage-action', expectedFocusId: 'application-stage-action' },
      { triggerId: 'dropdown-item-schedule', expectedFocusId: 'application-more-actions' },
      { triggerId: 'application-schedule-create', expectedFocusId: 'application-schedule-create' },
    ]) {
      const trigger = button(triggerId);
      trigger.focus();
      act(() => trigger.click());
      expect(document.querySelector('[data-testid="schedule-event-form"]')).not.toBeNull();
      act(() => button('schedule-cancel').click());
      expect(document.querySelector('[data-testid="schedule-event-form"]')).toBeNull();
      expect(document.querySelector<HTMLButtonElement>('[role="tab"][aria-selected="true"]')?.textContent).toBe('进展');
      expect(content?.scrollTop).toBe(327);
      expect(document.activeElement).toBe(button(expectedFocusId));
    }
  });

  it('returns successful creation to the schedule section and uses auto scrolling for reduced motion', () => {
    renderDetail();
    content!.scrollTop = 111;
    act(() => button('application-stage-action').click());

    act(() => button('schedule-success').click());

    expect(document.querySelector<HTMLButtonElement>('[role="tab"][aria-selected="true"]')?.textContent).toBe('准备');
    expect(document.querySelector('[aria-labelledby="application-schedule-heading"]')).not.toBeNull();
    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: 'auto', block: 'start' });
  });
});
