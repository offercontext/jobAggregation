// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import dayjs from 'dayjs';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Application } from '@/types/application';
import type { EvidenceBundlePreview, EvidenceBundleSummary } from '@/types/evidenceBundle';
import type { MaterialKitViewModel } from '@/types/materialKit';
import drawerSource from './MaterialKitDrawer.tsx?raw';

const queryState = vi.hoisted(() => ({
  historyError: null as unknown,
  historyFetching: false,
  historyRefetch: vi.fn(),
  history: [] as EvidenceBundleSummary[] | undefined,
  kit: null as MaterialKitViewModel | null,
  previewError: null as unknown,
  previewFetching: false,
  preview: undefined as EvidenceBundlePreview | undefined,
  previewRefetch: vi.fn(),
  previewUpdatedAt: 1,
  queryClient: {
    invalidateQueries: vi.fn(),
    setQueryData: vi.fn(),
  },
}));

const evidenceService = vi.hoisted(() => ({
  confirmEvidenceBundle: vi.fn(),
  getEvidenceBundle: vi.fn(),
  getEvidenceBundlePreview: vi.fn(),
  listEvidenceBundles: vi.fn(),
}));

const proposalService = vi.hoisted(() => ({
  createMaterialRevisionProposal: vi.fn(),
}));

const materialKitService = vi.hoisted(() => ({
  generateApplicationMaterialKit: vi.fn(),
  getApplicationMaterialKit: vi.fn(),
  updateMaterialKit: vi.fn(),
}));

vi.mock('@tanstack/react-query', () => ({
  useMutation: (options: any) => ({
    isPending: false,
    mutate: (variables: unknown) => {
      void Promise.resolve(options.mutationFn(variables))
        .then((result) => options.onSuccess?.(result, variables))
        .catch((error) => options.onError?.(error, variables));
    },
  }),
  useQuery: (options: any) => {
    const key = options.queryKey?.[0];
    const dataByKey: Record<string, unknown> = {
      'application-evidence-bundle-preview': queryState.preview,
      'application-evidence-bundles': queryState.history,
      'application-material-kit': queryState.kit,
      resumes: [{ id: 11, name: 'Backend Resume' }],
    };
    const data = dataByKey[key];
    const error = key === 'application-evidence-bundle-preview'
      ? queryState.previewError
      : key === 'application-evidence-bundles'
        ? queryState.historyError
        : null;
    return {
      data,
      error,
      isError: Boolean(error),
      dataUpdatedAt: key === 'application-evidence-bundle-preview' ? queryState.previewUpdatedAt : 0,
      isFetching: key === 'application-evidence-bundle-preview'
        ? queryState.previewFetching
        : key === 'application-evidence-bundles' && queryState.historyFetching,
      isSuccess: data !== undefined && !error,
      refetch: key === 'application-evidence-bundle-preview'
        ? queryState.previewRefetch
        : key === 'application-evidence-bundles'
          ? queryState.historyRefetch
          : vi.fn(),
    };
  },
  useQueryClient: () => queryState.queryClient,
}));

vi.mock('antd', () => {
  const Form = Object.assign(
    (props: any) => <div>{props.children}</div>,
    { Item: (props: any) => <label>{props.label}{props.children}</label> },
  );
  const Input = Object.assign(
    ({ bordered: _bordered, ...props }: any) => <input {...props} value={props.value ?? ''} />,
    { TextArea: ({ bordered: _bordered, ...props }: any) => <textarea {...props} value={props.value ?? ''} /> },
  );
  const Typography = {
    Paragraph: (props: any) => <p>{props.children}</p>,
    Text: (props: any) => <span>{props.children}</span>,
    Title: (props: any) => <h2>{props.children}</h2>,
  };

  return {
    Alert: (props: any) => <div role="alert">{props.message}</div>,
    App: { useApp: () => ({ message: { error: vi.fn(), success: vi.fn() } }) },
    Button: ({ children, icon: _icon, loading, ...props }: any) => (
      <button type="button" {...props} disabled={Boolean(props.disabled || loading)}>{children}</button>
    ),
    Checkbox: (props: any) => <input type="checkbox" {...props} />,
    Empty: (props: any) => <div>{props.description}</div>,
    Form,
    Input,
    Modal: (props: any) => props.open ? (
      <section role="dialog" aria-label={props.title}>
        <h2>{props.title}</h2>
        {props.children}
        {props.footer}
      </section>
    ) : null,
    Progress: () => <div />,
    Select: ({ options = [], value, onChange, loading: _loading, showSearch: _showSearch, optionFilterProp: _optionFilterProp, ...props }: any) => (
      <select
        {...props}
        value={value ?? ''}
        onChange={(event) => onChange?.(options.find((option: any) => String(option.value) === event.target.value)?.value)}
      >
        {options.map((option: any) => <option key={option.value} value={option.value}>{option.label}</option>)}
      </select>
    ),
    Space: (props: any) => <div>{props.children}</div>,
    Spin: (props: any) => <>{props.children}</>,
    Tag: (props: any) => <span>{props.children}</span>,
    Typography,
  };
});

vi.mock('@ant-design/icons', () => ({
  ArrowLeftOutlined: () => null,
  CopyOutlined: () => null,
  ReloadOutlined: () => null,
  SaveOutlined: () => null,
}));

vi.mock('@/services/evidenceBundles', () => evidenceService);
vi.mock('@/services/materialRevisionProposals', () => proposalService);
vi.mock('@/services/materialKits', () => materialKitService);
vi.mock('@/services/resumes', () => ({ listResumes: vi.fn() }));

const { default: MaterialKitDrawer } = await import('./MaterialKitDrawer');

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean | undefined;
}

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const application = {
  id: 7,
  company_name: 'Example Co.',
  position_name: 'Backend Engineer',
  notes: 'Build services',
} as Application;

const switchedApplication = {
  id: 8,
  company_name: 'Other Co.',
  position_name: 'Platform Engineer',
  notes: 'Build platforms',
} as Application;

const readyPreview: EvidenceBundlePreview = {
  application_id: 7,
  ready: true,
  issues: [],
  bundle_sha256: 'a'.repeat(64),
  sources: {
    application: {
      id: 7,
      company_name: 'Example Co.',
      position_name: 'Backend Engineer',
      job_url: 'https://example.com/jobs/7',
      source: 'manual',
    },
    jd: { sha256: 'b'.repeat(64), characters: 1200 },
    resume: { id: 11, title: 'Backend Resume', sha256: 'c'.repeat(64) },
    material_kit: { id: 5, sha256: 'd'.repeat(64) },
  },
};

function materialKit(status: MaterialKitViewModel['status'] = 'ready'): MaterialKitViewModel {
  return {
    id: 5,
    application_id: 7,
    resume_id: 11,
    jd_version_id: 1,
    jd_snapshot: 'Build services',
    status,
    created_at: '2026-07-14T09:00:00.000Z',
    updated_at: '2026-07-14T09:00:00.000Z',
    content: {
      resume_advice: { summary: '', highlights: [], rewrite_bullets: [], gaps: [], notes: '' },
      messages: [],
      checklist: [],
    },
  };
}

let container: HTMLDivElement | undefined;
let root: Root | undefined;

type SurfaceProps = {
  pendingState?: 'none' | 'pending' | 'unknown' | 'result_unknown';
  resultUnknown?: boolean;
  sourceConflict?: boolean;
};

function render(nextApplication: Application = application, surfaceProps: SurfaceProps = {}) {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root?.render(
    <MaterialKitDrawer
      application={nextApplication}
      open
      onClose={vi.fn()}
      initialJdSnapshot="Build services"
      initialJdVersionID={1}
      {...surfaceProps}
    />,
  ));
  return container;
}

function rerender(nextApplication: Application, surfaceProps: SurfaceProps = {}) {
  act(() => root?.render(
    <MaterialKitDrawer
      application={nextApplication}
      open
      onClose={vi.fn()}
      initialJdSnapshot="Build services"
      initialJdVersionID={1}
      {...surfaceProps}
    />,
  ));
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

function buttonByText(view: HTMLDivElement, text: string) {
  return [...view.querySelectorAll('button')].find((item) => item.textContent?.includes(text)) as HTMLButtonElement | undefined;
}

function clickByText(view: HTMLDivElement, text: string) {
  const button = buttonByText(view, text);
  expect(button, `expected button ${text}`).toBeInstanceOf(HTMLButtonElement);
  act(() => button?.click());
  return button as HTMLButtonElement;
}

function setProposalAssertions(view: HTMLDivElement, value: string) {
  const input = view.querySelector<HTMLTextAreaElement>(
    'textarea[placeholder="每行填写一条候选人事实"]',
  );
  expect(input).toBeInstanceOf(HTMLTextAreaElement);
  act(() => {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
    setter?.call(input, value);
    input?.dispatchEvent(new Event('input', { bubbles: true }));
    input?.dispatchEvent(new Event('change', { bubbles: true }));
  });
}

function formatLocalDateTime(date: Date) {
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

beforeEach(() => {
  queryState.kit = materialKit();
  queryState.preview = readyPreview;
  queryState.history = [];
  queryState.previewError = null;
  queryState.previewFetching = false;
  queryState.previewUpdatedAt = 1;
  queryState.historyError = null;
  queryState.historyFetching = false;
  queryState.previewRefetch.mockReset();
  queryState.historyRefetch.mockReset();
  queryState.queryClient.invalidateQueries.mockReset();
  queryState.queryClient.setQueryData.mockReset();
  evidenceService.confirmEvidenceBundle.mockReset();
  evidenceService.getEvidenceBundle.mockReset();
  evidenceService.getEvidenceBundlePreview.mockReset();
  evidenceService.listEvidenceBundles.mockReset();
  proposalService.createMaterialRevisionProposal.mockReset();
  proposalService.createMaterialRevisionProposal.mockResolvedValue(null);
  materialKitService.generateApplicationMaterialKit.mockReset();
  materialKitService.getApplicationMaterialKit.mockReset();
  materialKitService.updateMaterialKit.mockReset();
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'e2ddc6c1-2a4d-4bd6-8969-7c0bc29cc771') });
});

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
  root = undefined;
  container = undefined;
  vi.unstubAllGlobals();
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe('MaterialKitDrawer evidence confirmation', () => {
  it('keeps hashes and internal identifiers out of the user-facing material surface', () => {
    expect(drawerSource).toContain('本次投递记录');
    expect(drawerSource).toContain('选择本次实际使用的简历版本');
    expect(drawerSource).not.toContain('选择最匹配的简历版本');
    expect(drawerSource).not.toContain('投递材料包</Typography.Title>');
    expect(drawerSource).not.toContain('{entry.bundle_sha256}');
    expect(drawerSource).not.toContain('{evidenceDetail.bundle_sha256}');
    expect(drawerSource).not.toContain('JSON.stringify(evidenceDetail.snapshot');
  });

  it('uses a neutral error when material kit generation returns a general 409', async () => {
    materialKitService.generateApplicationMaterialKit.mockRejectedValueOnce({ response: { status: 409 } });
    const view = render();
    await flush();

    clickByText(view, '生成材料包');
    await flush();

    expect(view.textContent).toContain('操作未完成，请稍后重试');
    expect(view.textContent).not.toContain('提交材料已变化，请重新核对');
  });

  it('sends user-supplied candidate assertions with a generated proposal', async () => {
    const view = render();
    await flush();

    setProposalAssertions(view, 'I led the migration.\nI shipped the API.');

    clickByText(view, '生成基于证据的简历提案');
    await flush();

    expect(proposalService.createMaterialRevisionProposal).toHaveBeenCalledWith(7, {
      instructions: '',
      user_assertions: ['I led the migration.', 'I shipped the API.'],
    });
  });

  it('blocks proposal generation when there are more than ten assertions', async () => {
    const view = render();
    await flush();
    setProposalAssertions(view, Array.from({ length: 11 }, (_, index) => `Fact ${index + 1}`).join('\n'));

    expect(view.textContent).toContain('最多填写 10 条非空断言');
    expect(buttonByText(view, '生成基于证据的简历提案')?.disabled).toBe(true);
    expect(proposalService.createMaterialRevisionProposal).not.toHaveBeenCalled();
  });

  it('blocks proposal generation when an assertion exceeds 500 characters', async () => {
    const view = render();
    await flush();
    setProposalAssertions(view, 'x'.repeat(501));

    expect(view.textContent).toContain('每条断言不得超过 500 个字符');
    expect(buttonByText(view, '生成基于证据的简历提案')?.disabled).toBe(true);
    expect(proposalService.createMaterialRevisionProposal).not.toHaveBeenCalled();
  });

  it('shows a product error when the proposal cannot be verified', async () => {
    proposalService.createMaterialRevisionProposal.mockRejectedValue({
      response: {
        data: {
          error_code: 'material_proposal_unverifiable',
          error: 'AI returned a proposal that could not be verified. Please retry.',
        },
      },
    });
    const view = render();
    await flush();

    setProposalAssertions(view, 'I led the migration.');
    clickByText(view, '生成基于证据的简历提案');
    await flush();

    expect(view.textContent).toContain(
      'AI 输出未通过证据校验，已保护原简历且未创建草稿，请重试',
    );
    expect(view.textContent).not.toContain('AI returned a proposal that could not be verified. Please retry.');
    expect(view.textContent).not.toContain('Request failed with status code 502');
    expect(buttonByText(view, '生成基于证据的简历提案')?.disabled).toBe(false);
  });

  it('shows safe preview issue labels without exposing unknown server diagnostics', async () => {
    queryState.preview = {
      application_id: 7,
      ready: false,
      issues: ['缺少已选择的简历', '服务端返回的校验问题'],
      sources: {},
    };
    const view = render();
    await flush();

    clickByText(view, '确认已投递');

    expect(view.textContent).toContain('用户确认，非平台回执');
    expect(view.textContent).toContain('缺少已选择的简历，请先选择简历');
    expect(view.textContent).toContain('材料证据尚未准备完成，请补充必要来源');
    expect(view.textContent).not.toContain('服务端返回的校验问题');
    expect([...view.querySelectorAll('button')].find((item) => item.textContent?.includes('确认投递'))?.disabled).toBe(true);
  });

  it('shows preview loading separately from unready evidence', async () => {
    queryState.preview = undefined;
    queryState.previewFetching = true;
    const view = render();
    await flush();

    clickByText(view, '确认已投递');

    const modal = view.querySelector<HTMLElement>('[role="dialog"]');
    const confirmButton = [...view.querySelectorAll('button')].find((item) => item.textContent?.includes('确认投递'));
    expect(modal?.textContent).toContain('正在加载材料证据，请稍候');
    expect(modal?.textContent).not.toContain('材料证据尚未准备完成');
    expect(confirmButton?.disabled).toBe(true);
  });

  it('projects owner pending, result-unknown, and source-conflict states into the drawer', async () => {
    const view = render(application, { pendingState: 'pending' });
    await flush();
    expect(view.querySelector('[data-testid="material-kit-surface-state"]')?.getAttribute('data-state')).toBe('waiting_confirmation');

    rerender(application, { resultUnknown: true });
    await flush();
    expect(view.querySelector('[data-testid="material-kit-surface-state"]')?.getAttribute('data-state')).toBe('result_unknown');

    rerender(application, { sourceConflict: true });
    await flush();
    expect(view.querySelector('[data-testid="material-kit-surface-state"]')?.getAttribute('data-state')).toBe('source_conflict');
    expect(view.querySelector('[data-material-primary="true"]')?.textContent).toContain('确认处理结果');
  });

  it('labels a ready evidence preview as pending until confirmation', async () => {
    const view = render();
    await flush();

    clickByText(view, '确认已投递');

    const modal = view.querySelector<HTMLElement>('[role="dialog"]');
    expect(modal?.textContent).toContain('待确认的证据预览');
    expect(modal?.textContent).not.toContain('已确认投递证据快照');
  });

  it('keeps a legacy submitted kit readable and strictly read-only', async () => {
    queryState.kit = materialKit('submitted');
    const view = render();
    await flush();

    expect(view.textContent).toContain('旧投递标记，缺少证据快照');
    expect(view.textContent).not.toContain('状态：已投递');
    expect(view.querySelector('option[value="submitted"]')).toBeNull();
    expect(buttonByText(view, '确认已投递')).toBeUndefined();
    expect(view.querySelectorAll('input:not([type="hidden"]):not(:disabled), textarea:not(:disabled), select:not(:disabled)')).toHaveLength(0);
    expect(evidenceService.confirmEvidenceBundle).not.toHaveBeenCalled();
  });

  it('defaults to local civil time and confirms its ISO instant with one idempotency key per modal opening', async () => {
    vi.useFakeTimers();
    const now = new Date('2026-07-14T08:30:45.000Z');
    const expectedLocalDateTime = formatLocalDateTime(now);
    vi.setSystemTime(now);
    evidenceService.confirmEvidenceBundle.mockResolvedValue({ id: 2 });
    const view = render();
    await flush();

    clickByText(view, '确认已投递');
    const submittedAt = view.querySelector<HTMLInputElement>('input[type="datetime-local"]');
    expect(submittedAt).toBeInstanceOf(HTMLInputElement);
    expect(submittedAt?.value).toBe(expectedLocalDateTime);
    clickByText(view, '确认投递');
    await flush();

    expect(evidenceService.confirmEvidenceBundle).toHaveBeenCalledWith(7, expect.objectContaining({
      expected_bundle_sha256: 'a'.repeat(64),
      idempotency_key: 'e2ddc6c1-2a4d-4bd6-8969-7c0bc29cc771',
      submitted_at: new Date(expectedLocalDateTime).toISOString(),
    }));
    expect(globalThis.crypto.randomUUID).toHaveBeenCalledTimes(1);
  });

  it('keeps the modal open after a 409 and gates a new confirmation until the refreshed preview is ready', async () => {
    let resolvePreviewRefetch: ((result: { data: EvidenceBundlePreview; isError: false; isSuccess: true }) => void) | undefined;
    queryState.previewRefetch.mockReturnValueOnce(new Promise<{ data: EvidenceBundlePreview; isError: false; isSuccess: true }>((resolve) => {
      resolvePreviewRefetch = resolve;
    }));
    evidenceService.confirmEvidenceBundle.mockRejectedValueOnce({ response: { status: 409 } });
    evidenceService.confirmEvidenceBundle.mockResolvedValueOnce({ id: 2 });
    const view = render();
    await flush();

    clickByText(view, '确认已投递');
    clickByText(view, '确认投递');
    await flush();

    expect(evidenceService.confirmEvidenceBundle).toHaveBeenCalledTimes(1);
    expect(queryState.previewRefetch).toHaveBeenCalledTimes(1);
    expect(view.querySelector('[role="dialog"]')).not.toBeNull();
    expect(view.textContent).toContain('提交材料已变化，请重新核对');
    const confirmButton = [...view.querySelectorAll('button')].find((item) => item.textContent?.includes('确认投递'));
    expect(confirmButton?.disabled).toBe(true);

    queryState.preview = { ...readyPreview, bundle_sha256: 'e'.repeat(64) };
    await act(async () => resolvePreviewRefetch?.({ data: queryState.preview!, isError: false, isSuccess: true }));
    await flush();
    expect(confirmButton?.disabled).toBe(false);
    clickByText(view, '确认投递');
    await flush();
    expect(evidenceService.confirmEvidenceBundle).toHaveBeenCalledTimes(2);
    expect(evidenceService.confirmEvidenceBundle).toHaveBeenLastCalledWith(7, expect.objectContaining({
      expected_bundle_sha256: 'e'.repeat(64),
    }));
  });

  it('ignores a delayed 409 from the previous application after the drawer switches context', async () => {
    let rejectConfirmation: ((error: unknown) => void) | undefined;
    evidenceService.confirmEvidenceBundle.mockReturnValueOnce(new Promise((_resolve, reject) => {
      rejectConfirmation = reject;
    }));
    const view = render();
    await flush();

    clickByText(view, '确认已投递');
    clickByText(view, '确认投递');
    rerender(switchedApplication);
    await flush();

    act(() => rejectConfirmation?.({ response: { status: 409 } }));
    await flush();

    expect(view.querySelector('[role="dialog"]')).toBeNull();
    expect(queryState.previewRefetch).not.toHaveBeenCalled();
  });

  it('keeps stale evidence hidden and confirmation gated when the 409 preview refresh fails', async () => {
    queryState.previewRefetch.mockResolvedValueOnce({
      data: readyPreview,
      isError: true,
      isSuccess: false,
    });
    evidenceService.confirmEvidenceBundle.mockRejectedValueOnce({ response: { status: 409 } });
    const view = render();
    await flush();

    clickByText(view, '确认已投递');
    clickByText(view, '确认投递');
    await flush();

    const modal = view.querySelector<HTMLElement>('[role="dialog"]');
    const confirmButton = [...view.querySelectorAll('button')].find((item) => item.textContent?.includes('确认投递'));
    expect(modal?.textContent).toContain('材料证据刷新失败，请重试刷新后再确认');
    expect(modal?.textContent).not.toContain('Backend Resume');
    expect(modal?.textContent).not.toContain('a'.repeat(64));
    expect(confirmButton?.disabled).toBe(true);
    expect(evidenceService.confirmEvidenceBundle).toHaveBeenCalledTimes(1);
  });

  it('shows an initial preview-load error with a manual refresh instead of allowing confirmation', async () => {
    queryState.previewError = new Error('Preview unavailable');
    queryState.previewRefetch.mockReturnValueOnce(new Promise(() => undefined));
    const view = render();
    await flush();

    clickByText(view, '确认已投递');

    const modal = view.querySelector<HTMLElement>('[role="dialog"]');
    const confirmButton = [...view.querySelectorAll('button')].find((item) => item.textContent?.includes('确认投递'));
    expect(modal?.textContent).toContain('材料证据加载失败，请刷新后再确认');
    expect(confirmButton?.disabled).toBe(true);
    clickByText(view, '重新刷新证据');
    expect(queryState.previewRefetch).toHaveBeenCalledTimes(1);
  });

  it('shows a history-load error with a retry instead of an empty-history claim', async () => {
    queryState.historyError = new Error('History unavailable');
    const view = render();
    await flush();

    const history = view.querySelector<HTMLElement>('[data-testid="evidence-history"]');
    expect(history?.textContent).toContain('本次投递记录加载失败');
    expect(history?.textContent).not.toContain('尚无本次投递记录');
    clickByText(view, '重新加载历史');
    expect(queryState.historyRefetch).toHaveBeenCalledTimes(1);
  });

  it('shows initial history loading instead of claiming no evidence exists', async () => {
    queryState.history = undefined;
    queryState.historyFetching = true;
    const view = render();
    await flush();

    const history = view.querySelector<HTMLElement>('[data-testid="evidence-history"]');
    expect(history?.textContent).toContain('正在加载本次投递记录，请稍候');
    expect(history?.textContent).not.toContain('尚无本次投递记录');
  });

  it('recovers confirmation only after a later current ready preview succeeds', async () => {
    queryState.previewError = new Error('Preview unavailable');
    const view = render();
    await flush();
    clickByText(view, '确认已投递');

    queryState.previewError = null;
    queryState.preview = { ...readyPreview, bundle_sha256: 'f'.repeat(64) };
    queryState.previewUpdatedAt = 2;
    rerender(application);
    await flush();

    const modal = view.querySelector<HTMLElement>('[role="dialog"]');
    const confirmButton = [...view.querySelectorAll('button')].find((item) => item.textContent?.includes('确认投递'));
    expect(modal?.textContent).not.toContain('f'.repeat(64));
    expect(confirmButton?.disabled).toBe(false);
  });

  it('invalidates all actual event consumers after confirmation succeeds', async () => {
    queryState.history = [{
      id: 1,
      application_id: 7,
      sequence: 3,
      submitted_at: '2026-07-14T09:00:00.000Z',
      confirmed_at: '2026-07-14T09:01:00.000Z',
      confirmation_kind: 'user_asserted',
      bundle_sha256: 'a'.repeat(64),
      created_at: '2026-07-14T09:01:00.000Z',
    }];
    evidenceService.confirmEvidenceBundle.mockResolvedValue({ id: 2 });
    const view = render();
    await flush();

    const history = view.querySelector<HTMLElement>('[data-testid="evidence-history"]');
    expect(history?.textContent).toContain('第 3 次');
    expect(history?.textContent).toContain('投递（本地）');
    expect(history?.textContent).not.toContain('a'.repeat(64));
    expect(history?.textContent).not.toContain('2026-07-14T09:00:00.000Z');
    expect(history?.querySelector('input, select, textarea')).toBeNull();

    clickByText(view, '确认已投递');
    clickByText(view, '确认投递');
    await flush();

    expect(queryState.queryClient.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['application-evidence-bundle-preview', 7] });
    expect(queryState.queryClient.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['application-evidence-bundles', 7] });
    expect(queryState.queryClient.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['events'] });
    expect(queryState.queryClient.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['events', 7] });
    expect(queryState.queryClient.invalidateQueries).not.toHaveBeenCalledWith({ queryKey: ['application-events', 7] });
    expect(queryState.queryClient.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['applications'] });
  });

  it('summarizes confirmations and opens an immutable evidence detail', async () => {
    queryState.history = [
      {
        id: 2,
        application_id: 7,
        sequence: 2,
        submitted_at: '2026-07-14T09:00:00.000Z',
        confirmed_at: '2026-07-14T09:02:00.000Z',
        confirmation_kind: 'user_asserted',
        bundle_sha256: 'b'.repeat(64),
        created_at: '2026-07-14T09:02:00.000Z',
      },
      {
        id: 1,
        application_id: 7,
        sequence: 1,
        submitted_at: '2026-07-13T09:00:00.000Z',
        confirmed_at: '2026-07-13T09:02:00.000Z',
        confirmation_kind: 'user_asserted',
        bundle_sha256: 'a'.repeat(64),
        created_at: '2026-07-13T09:02:00.000Z',
      },
    ];
    evidenceService.getEvidenceBundle.mockResolvedValue({
      ...queryState.history[0],
      snapshot: { jd: { text: 'Immutable job description' } },
    });
    const view = render();
    await flush();

    const history = view.querySelector<HTMLElement>('[data-testid="evidence-history"]');
    expect(history?.textContent).toContain('已确认投递 2 次');
    expect(history?.textContent).toContain(`最近确认（本地）：${dayjs(queryState.history[0].confirmed_at).format('YYYY-MM-DD HH:mm')}`);
    expect(history?.textContent).toContain('第 2 次');
    expect(history?.textContent).toContain('用户确认');
    expect(history?.textContent).not.toContain('b'.repeat(64));

    clickByText(view, '查看详情');
    await flush();

    expect(evidenceService.getEvidenceBundle).toHaveBeenCalledWith(7, 2);
    const detailModal = view.querySelector<HTMLElement>('[role="dialog"]');
    expect(detailModal?.textContent).toContain('用户确认');
    expect(detailModal?.textContent).not.toContain('Immutable job description');
    expect(detailModal?.querySelector('input, textarea, select')).toBeNull();
  });
});
