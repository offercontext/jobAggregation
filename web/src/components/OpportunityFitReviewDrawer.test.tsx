// @vitest-environment jsdom
import { act, useEffect, useState, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const state = vi.hoisted(() => ({
  create: vi.fn(),
  deep: vi.fn(),
  confirm: vi.fn(),
  deepV2: vi.fn(),
  get: vi.fn(),
  list: vi.fn(),
  getV2: vi.fn(),
  sourceConflict: vi.fn(),
  listV2: vi.fn(),
  history: [] as Array<{ id: number; recommendation: string; created_at: string }>,
  drawerClose: null as (() => void) | null,
}));

vi.mock('@/services/resumes', () => ({
  listResumes: vi.fn().mockResolvedValue([{ id: 11, name: 'Backend Resume', title: 'Backend Resume' }]),
}));
vi.mock('@/services/opportunityFitReviews', () => ({
  createOpportunityFitReview: state.create,
  createOpportunityFitV2Triage: state.create,
  confirmOpportunityFitV2Triage: state.confirm,
  createOpportunityFitDeepReview: state.deep,
  createOpportunityFitV2DeepReview: state.deepV2,
  getOpportunityFitReview: state.get,
  listOpportunityFitReviews: state.list,
  getOpportunityFitV2Review: state.getV2,
  findOpportunityFitV2SourceConflictStage: state.sourceConflict,
  listOpportunityFitV2Reviews: state.listV2,
}));
vi.mock('@/services/applicationJdVersions', () => ({
  getApplicationJdVersion: vi.fn().mockResolvedValue({ id: 1, application_id: 7, jd_text: 'Frozen JD text' }),
}));
vi.mock('@tanstack/react-query', () => ({
  useQuery: (options: { enabled?: boolean; queryKey?: unknown[]; queryFn: () => unknown }) => {
    const [queryState, setQueryState] = useState<{ data?: unknown; error?: unknown }>({});
    const queryKey = JSON.stringify(options.queryKey || []);
    useEffect(() => {
      if (options.enabled === false) return undefined;
      let active = true;
      void Promise.resolve(options.queryFn()).then(
        (data) => active && setQueryState({ data }),
        (error) => active && setQueryState({ error }),
      );
      return () => { active = false; };
    }, [options.enabled, queryKey]);
    return {
      data: queryState.data,
      error: queryState.error,
      isFetching: queryState.data === undefined && queryState.error === undefined,
    };
  },
  useMutation: (options: { mutationFn: (variables?: unknown) => unknown; onSuccess?: (data: unknown) => void; onError?: (error: unknown, variables?: any) => void }) => {
    const [isPending, setIsPending] = useState(false);
    return {
      isPending,
      mutate: (variables?: unknown) => {
        setIsPending(true);
        void Promise.resolve(options.mutationFn(variables)).then(
          (data) => {
            setIsPending(false);
            options.onSuccess?.(data);
          },
          (error) => {
            setIsPending(false);
            options.onError?.(error, variables);
          },
        );
      },
    };
  },
}));
vi.mock('antd', () => {
  const Form = Object.assign(
    (props: { children: ReactNode }) => <div>{props.children}</div>,
    { Item: (props: { label?: ReactNode; children: ReactNode }) => <label>{props.label}{props.children}</label> },
  );
  const Input = Object.assign(
    (props: React.TextareaHTMLAttributes<HTMLTextAreaElement>) => <textarea {...props} />,
    { TextArea: (props: React.TextareaHTMLAttributes<HTMLTextAreaElement>) => <textarea {...props} /> },
  );
  const Typography = {
    Paragraph: (props: { children: ReactNode }) => <p>{props.children}</p>,
    Text: (props: { children: ReactNode }) => <span>{props.children}</span>,
    Title: (props: { children: ReactNode }) => <h2>{props.children}</h2>,
  };
  return {
    Alert: (props: { message: ReactNode }) => <div role="alert">{props.message}</div>,
    Button: ({ loading: _loading, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement> & { loading?: boolean }) => (
      <button {...props}>{props.children}</button>
    ),
    Card: (props: { title?: ReactNode; children: ReactNode }) => <section><h3>{props.title}</h3>{props.children}</section>,
    Divider: () => <hr />,
    Drawer: (props: { open: boolean; title: ReactNode; children: ReactNode; onClose?: () => void }) => {
      state.drawerClose = props.onClose ?? null;
      return props.open ? <div role="dialog"><h1>{props.title}</h1>{props.children}</div> : null;
    },
    Form,
    Input,
    Select: (props: { value?: unknown; disabled?: boolean; onChange?: (value: unknown) => void; options?: Array<{ value: unknown; label: string }> }) => (
      <select disabled={props.disabled} value={String(props.value ?? '')} onChange={(event) => props.onChange?.(Number(event.target.value))}>
        <option value="">select</option>
        {(props.options || []).map((option) => <option key={String(option.value)} value={String(option.value)}>{option.label}</option>)}
      </select>
    ),
    Space: (props: { children: ReactNode }) => <div>{props.children}</div>,
    Spin: () => <span>loading</span>,
    Tag: (props: { children: ReactNode }) => <span>{props.children}</span>,
    Typography,
  };
});

const {
  default: OpportunityFitReviewDrawer,
  createOpportunityFitOwnerStore,
} = await import('./OpportunityFitReviewDrawer');

const application = { id: 7, company_name: 'Example Co.', position_name: 'Backend Engineer' } as never;

function historyV1(id: number, createdAt = '2026-07-21T00:00:00Z') {
  return {
    schema_version: 1,
    id,
    application_id: 7,
    resume_id: 11,
    status: 'triage_complete',
    summary: { text: `Historical V1 ${id}` },
    recommendation: 'advance',
    source_fingerprint_sha256: 'history-source',
    created_at: createdAt,
  };
}

function historyV2(id: number, createdAt = '2026-07-21T00:00:00Z') {
  return {
    id,
    review_id: id,
    application_id: 7,
    schema_version: 2,
    status: 'active',
    created_at: createdAt,
    latest_stage: {
      id: id * 10,
      stage_id: id * 10,
      review_id: id,
      application_id: 7,
      schema_version: 2,
      stage: 'triage',
      stage_status: 'ready',
      created_at: createdAt,
      source_fingerprint_sha256: 'history-source',
      proposal: {
        schema_version: 2,
        stage: 'triage',
        summary: { text: `Historical V2 ${id}` },
      },
    },
  };
}

let root: Root | undefined;
let container: HTMLDivElement | undefined;

async function render(
  onPrepareMaterials?: (review: unknown, jdText: string) => void,
  currentJdText = '',
  draft?: Record<string, unknown>,
  onDraftChange?: (patch: Record<string, unknown>) => void,
  ownerOptions: {
    application?: unknown;
    ownerStore?: ReturnType<typeof createOpportunityFitOwnerStore>;
    onOwnerStateChange?: (state: { pending: boolean; resultUnknown: boolean; unsaved: boolean }) => void;
    onOwnerProjectionChange?: (projection: unknown) => void;
    onClose?: () => void;
  } = {},
) {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(
      <OpportunityFitReviewDrawer
        application={(ownerOptions.application ?? application) as never}
        open
        currentJdText={currentJdText}
        jdVersionId={1}
        draft={draft as never}
        onDraftChange={onDraftChange as never}
        ownerStore={ownerOptions.ownerStore}
        onOwnerStateChange={ownerOptions.onOwnerStateChange}
        onOwnerProjectionChange={ownerOptions.onOwnerProjectionChange}
        onClose={ownerOptions.onClose ?? vi.fn()}
        onPrepareMaterials={onPrepareMaterials}
      />,
    );
  });
  return container;
}

beforeEach(() => {
  state.create.mockReset();
  state.deep.mockReset();
  state.confirm.mockReset();
  state.deepV2.mockReset();
  state.get.mockReset();
  state.list.mockReset();
  state.getV2.mockReset();
  state.sourceConflict.mockReset();
  state.listV2.mockReset();
  state.history = [];
  state.drawerClose = null;
  state.list.mockResolvedValue([]);
  state.listV2.mockResolvedValue([]);
  state.create.mockResolvedValue({
    id: 8,
    recommendation: 'hold',
    source: {
      resume: { id: 11, title: 'Backend Resume', sha256: 'resume' },
      jd: { source_label: '用户粘贴 JD', sha256: 'jd', text: 'JD text' },
      candidate_assertions: [],
    },
    triage: {
      summary: { text: 'safe', evidence_refs: [] },
      recommendation: 'hold',
      hard_constraints: [],
      fit_signals: [],
      gaps: [],
      deadline: { status: 'not_stated', text: '', evidence_refs: [] },
      next_questions: ['clarify'],
    },
    deep_review: null,
  });
  state.get.mockResolvedValue({
    id: 8,
    recommendation: 'advance',
    source: {
      resume: { id: 11, title: 'Backend Resume', sha256: 'resume' },
      jd: { source_label: 'Frozen JD', sha256: 'jd', text: 'Frozen JD text' },
      candidate_assertions: [],
    },
    triage: {
      summary: { text: 'safe', evidence_refs: [] },
      recommendation: 'advance',
      hard_constraints: [],
      fit_signals: [],
      gaps: [],
      deadline: { status: 'not_stated', text: '', evidence_refs: [] },
      next_questions: [],
    },
    deep_review: {
      strengths: [],
      gaps_to_address: [],
      questions_to_clarify: [],
      recommended_path: 'prepare_materials',
      next_actions: [],
    },
  });
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'd4b4b5e8-0a3a-4a3e-8e4d-6bc7a04d36b0') });
});

afterEach(async () => {
  await act(async () => {
    root?.unmount();
  });
  container?.remove();
  vi.unstubAllGlobals();
  state.drawerClose = null;
});

function setValue(element: HTMLTextAreaElement | HTMLSelectElement, value: string) {
  const prototype = element instanceof HTMLSelectElement
    ? HTMLSelectElement.prototype
    : HTMLTextAreaElement.prototype;
  Object.getOwnPropertyDescriptor(prototype, 'value')?.set?.call(element, value);
  element.dispatchEvent(new Event('input', { bubbles: true }));
  element.dispatchEvent(new Event('change', { bubbles: true }));
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

async function waitFor(assertion: () => void, attempts = 5) {
  let lastError: unknown;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      assertion();
      return;
    } catch (error) {
      lastError = error;
      await flush();
    }
  }
  throw lastError;
}

function getByRole(view: HTMLDivElement, role: string, name?: string): HTMLElement {
  const selector = role === 'button' ? 'button,[role="button"]' : `[role="${role}"]`;
  const element = [...view.querySelectorAll<HTMLElement>(selector)].find((candidate) => (
    !name || candidate.textContent?.includes(name)
  ));
  if (!element) throw new Error(`Expected ${role}${name ? ` named ${name}` : ''}`);
  return element;
}

function getByLabelText(view: HTMLDivElement, label: string): HTMLTextAreaElement | HTMLSelectElement {
  const labelElement = [...view.querySelectorAll('label')].find((candidate) => candidate.textContent?.includes(label));
  const control = labelElement?.querySelector('textarea,select');
  if (!(control instanceof HTMLTextAreaElement) && !(control instanceof HTMLSelectElement)) {
    throw new Error(`Expected control labelled ${label}`);
  }
  return control;
}

async function click(element: HTMLElement) {
  await act(async () => {
    element.click();
  });
}

describe('OpportunityFitReviewDrawer', () => {
  it('renders stable source and action surfaces without changing the input workflow', async () => {
    const rendered = await render();

    expect(rendered.querySelector('[data-testid="opportunity-fit-source-panel"]')).not.toBeNull();
    expect(rendered.querySelector('[data-testid="opportunity-fit-action-group"]')).not.toBeNull();
    expect(state.create).not.toHaveBeenCalled();
  });

  it('publishes pending before mutation and keeps an unknown attempt on close', async () => {
    state.create.mockImplementation(() => new Promise(() => undefined));
    const ownerStore = createOpportunityFitOwnerStore();
    const ownerStates: Array<{ pending: boolean; resultUnknown: boolean; unsaved: boolean }> = [];
    const view = await render(undefined, 'JD text', undefined, undefined, {
      ownerStore,
      onOwnerStateChange: (next) => ownerStates.push(next),
    });
    const select = getByLabelText(view, '用于审阅的简历') as HTMLSelectElement;
    await waitFor(() => expect(select.querySelector('option[value="11"]')).toBeTruthy());
    await act(async () => setValue(select, '11'));
    await waitFor(() => expect(getByRole(view, 'button', '开始快速判断')).toHaveProperty('disabled', false));
    await click(getByRole(view, 'button', '开始快速判断'));
    await waitFor(() => expect(state.create).toHaveBeenCalledTimes(1));
    expect(ownerStates).toContainEqual({ pending: true, resultUnknown: false, unsaved: true });
    expect(ownerStore.getDraft(7).triageKey).toBeTruthy();

    await act(async () => state.drawerClose?.());
    expect(ownerStore.getDraft(7).resultUnknown).toBe(true);
    expect(ownerStates[ownerStates.length - 1]).toEqual({ pending: false, resultUnknown: true, unsaved: true });
  });

  it('drops a pending response after switching applications', async () => {
    let resolveCreate: (() => void) | undefined;
    state.create.mockImplementation((_applicationID, input: { idempotency_key: string }) => new Promise((resolve) => {
      resolveCreate = () => resolve({
        review_id: 31,
        stage_id: 310,
        stage: 'triage',
        stage_status: 'ready',
        confirmation_token: 'confirm-token',
        resume_id: 11,
        jd_version_id: 1,
        idempotency_key: input.idempotency_key,
        proposal: { summary: { text: 'Stale App A result', evidence_refs: [] }, conditions: [], risks: [], questions: [], next_steps: [] },
      });
    }));
    const ownerStore = createOpportunityFitOwnerStore();
    const appB = { id: 8, company_name: 'Other Co.', position_name: 'Platform Engineer' };
    const view = await render(undefined, 'App A JD', undefined, undefined, { ownerStore });
    const select = getByLabelText(view, '用于审阅的简历') as HTMLSelectElement;
    await waitFor(() => expect(select.querySelector('option[value="11"]')).toBeTruthy());
    await act(async () => setValue(select, '11'));
    await waitFor(() => expect(getByRole(view, 'button', '快速判断')).toHaveProperty('disabled', false));
    await click(getByRole(view, 'button', '快速判断'));
    await waitFor(() => expect(state.create).toHaveBeenCalledTimes(1));

    await act(async () => {
      root?.render(
        <OpportunityFitReviewDrawer
          application={appB as never}
          open
          currentJdText="App B JD"
          jdVersionId={1}
          ownerStore={ownerStore}
          onClose={vi.fn()}
        />,
      );
      await Promise.resolve();
    });
    await waitFor(() => expect((getByLabelText(view, '用户粘贴的 JD') as HTMLTextAreaElement).value).toBe('App B JD'));

    resolveCreate?.();
    await flush();

    expect(view.textContent).not.toContain('Stale App A result');
    expect(ownerStore.getDraft(7).resultUnknown).toBe(true);
    expect(ownerStore.getDraft(8).triage).toBeNull();
  });

  it('isolates composition-scoped drafts across applications and Drawer remounts', async () => {
    const ownerStore = createOpportunityFitOwnerStore();
    const appB = { id: 8, company_name: 'Other Co.', position_name: 'Platform Engineer' };
    ownerStore.setDraft(7, {
      ...ownerStore.getDraft(7),
      jdText: 'App A JD',
      jdVersionId: 1,
      assertionsText: 'App A fact',
    });
    const view = await render(undefined, 'App B JD', undefined, undefined, {
      application: appB,
      ownerStore,
    });
    expect((getByLabelText(view, '用户粘贴的 JD') as HTMLTextAreaElement).value).toBe('App B JD');
    expect(ownerStore.getDraft(8).jdText).toBe('App B JD');
    expect(ownerStore.getDraft(7).jdText).toBe('App A JD');

    await act(async () => {
      root?.unmount();
      root = createRoot(container!);
      root.render(
        <OpportunityFitReviewDrawer
          application={application}
          open
          currentJdText="App A current JD"
          jdVersionId={1}
          ownerStore={ownerStore}
          onClose={vi.fn()}
        />,
      );
      await Promise.resolve();
    });
    expect((getByLabelText(view, '用户粘贴的 JD') as HTMLTextAreaElement).value).toBe('App A JD');
  });

  it('reuses the AppShell-owned triage key after a generating response and remount', async () => {
    state.create.mockResolvedValue({
      review_id: 21,
      stage_id: 22,
      stage: 'triage',
      stage_status: 'generating',
      confirmation_token: null,
      resume_id: 11,
      jd_version_id: 1,
      idempotency_key: 'd4b4b5e8-0a3a-4a3e-8e4d-6bc7a04d36b0',
      proposal: undefined,
    });
    let draft: Record<string, unknown> = {
      applicationId: 7,
      resumeId: 11,
      jdText: 'JD text',
      jdVersionId: 1,
      assertionsText: '',
      triageKey: null,
      deepKey: null,
      triage: null,
      deep: null,
      historical: false,
      resultUnknown: false,
      error: null,
    };
    const onDraftChange = vi.fn((patch: Record<string, unknown>) => {
      draft = { ...draft, ...patch };
    });
    const view = await render(undefined, 'JD text', draft, onDraftChange);
    await waitFor(() => expect(getByRole(view, 'button')).toHaveProperty('disabled', false));
    await click(getByRole(view, 'button'));
    await waitFor(() => expect(draft.triageKey).toBe('d4b4b5e8-0a3a-4a3e-8e4d-6bc7a04d36b0'));
    expect(state.create).toHaveBeenCalledTimes(1);

    await act(async () => {
      root?.render(null);
      await Promise.resolve();
      root?.render(
        <OpportunityFitReviewDrawer
          application={application}
          open
          currentJdText="JD text"
          jdVersionId={1}
          draft={draft as never}
          onDraftChange={onDraftChange as never}
          onClose={vi.fn()}
        />,
      );
    });
    expect(view.textContent).toContain('使用原尝试重试');
    expect(state.create).toHaveBeenCalledTimes(1);
  });

  it('keeps the Deep Review key after a provider-unknown response and remount', async () => {
    let rejectDeep: ((error: unknown) => void) | undefined;
    state.deepV2.mockImplementation(() => new Promise((_resolve, reject) => {
      rejectDeep = reject;
    }));
    const providerError = {
      response: { status: 502, data: { error_code: 'opportunity_fit_provider_error' } },
    };
    let draft: Record<string, unknown> = {
      applicationId: 7,
      resumeId: 11,
      jdText: 'JD text',
      jdVersionId: 1,
      assertionsText: '',
      triageKey: 'triage-key-00000001',
      deepKey: null,
      triage: {
        review_id: 21,
        stage_id: 22,
        stage: 'triage',
        stage_status: 'confirmed',
        confirmation_token: null,
        resume_id: 11,
        jd_version_id: 1,
        idempotency_key: 'triage-key-00000001',
        proposal: undefined,
      },
      deep: null,
      historical: false,
      resultUnknown: false,
      error: null,
    };
    const onDraftChange = vi.fn((patch: Record<string, unknown>) => {
      draft = { ...draft, ...patch };
    });
    const view = await render(undefined, 'JD text', draft, onDraftChange);
    await waitFor(() => expect(getByRole(view, 'button')).toHaveProperty('disabled', false));
    await click(getByRole(view, 'button'));
    await waitFor(() => expect(draft.deepKey).toBeTruthy());
    const key = draft.deepKey;
    expect(state.deepV2).toHaveBeenCalledTimes(1);
    expect(state.deepV2.mock.calls[0][2]).toEqual(expect.objectContaining({
      parent_triage_stage_id: 22,
      resume_id: 11,
    }));
    expect(state.deepV2.mock.calls[0][2]).not.toHaveProperty('jd_version_id');
    rejectDeep?.(providerError);
    await waitFor(() => expect(draft.resultUnknown).toBe(true));

    await act(async () => {
      root?.render(null);
      await Promise.resolve();
      root?.render(
        <OpportunityFitReviewDrawer
          application={application}
          open
          currentJdText="JD text"
          jdVersionId={1}
          draft={draft as never}
          onDraftChange={onDraftChange as never}
          onClose={vi.fn()}
        />,
      );
    });
    await click(getByRole(view, 'button'));
    await waitFor(() => expect(state.deepV2).toHaveBeenCalledTimes(2));
    expect(state.deepV2.mock.calls[0][2].idempotency_key).toBe(key);
    expect(state.deepV2.mock.calls[1][2].idempotency_key).toBe(key);
  });

  it('clears an unknown confirmation attempt after a deterministic expiry', async () => {
    state.confirm.mockRejectedValue({
      response: { status: 410, data: { error_code: 'opportunity_fit_triage_confirmation_expired' } },
    });
    let cleared = false;
    const draft = {
      applicationId: 7,
      resumeId: 11,
      jdText: 'JD text',
      jdVersionId: 1,
      assertionsText: '',
      triageKey: 'triage-key-00000001',
      deepKey: null,
      triage: {
        review_id: 21,
        stage_id: 22,
        stage: 'triage',
        stage_status: 'ready',
        confirmation_token: 'confirm-token',
        resume_id: 11,
        jd_version_id: 1,
        idempotency_key: 'triage-key-00000001',
        proposal: {
          schema_version: 2,
          stage: 'triage',
          source: { kind: 'opportunity_fit', contract_version: 'opportunity_fit.v2', snapshot_version: '1' },
          summary: { text: 'summary', rationale: 'evidence', evidence_refs: [] },
          conditions: [],
          risks: [],
          questions: [],
          next_steps: [],
        },
      },
      deep: null,
      historical: false,
      resultUnknown: true,
      error: '操作结果待确认，请使用原尝试重试。',
    };
    const onDraftChange = vi.fn((patch: Record<string, unknown> | null) => {
      if (patch === null) cleared = true;
    });
    const view = await render(undefined, 'JD text', draft, onDraftChange as never);
    await click(getByRole(view, 'button'));
    await waitFor(() => expect(state.confirm).toHaveBeenCalledTimes(1));
    expect(cleared).toBe(true);
    expect(view.querySelector('select')).not.toBeNull();
  });

  it('restores a persisted Triage source conflict instead of dropping the stage', async () => {
    state.create.mockRejectedValue({
      response: { status: 409, data: { error_code: 'application_jd_source_conflict' } },
    });
    state.listV2.mockResolvedValue([{ ...historyV2(21), triage_idempotency_key: 'd4b4b5e8-0a3a-4a3e-8e4d-6bc7a04d36b0' }]);
    state.getV2.mockResolvedValue({
      stages: [{
        review_id: 21,
        stage_id: 22,
        stage: 'triage',
        stage_status: 'source_conflict',
        resume_id: 11,
        jd_version_id: 1,
        idempotency_key: 'd4b4b5e8-0a3a-4a3e-8e4d-6bc7a04d36b0',
        proposal: undefined,
      }],
    });
    state.sourceConflict.mockResolvedValue({ status: 'found', stage: {
      review_id: 21,
      stage_id: 22,
      stage: 'triage',
      stage_status: 'source_conflict',
      resume_id: 11,
      jd_version_id: 1,
      idempotency_key: 'd4b4b5e8-0a3a-4a3e-8e4d-6bc7a04d36b0',
      proposal: undefined,
    }});
    const draft = {
      applicationId: 7,
      resumeId: 11,
      jdText: 'JD text',
      jdVersionId: 1,
      assertionsText: '',
      triageKey: null,
      deepKey: null,
      triage: null,
      deep: null,
      historical: false,
      resultUnknown: false,
      error: null,
    };
    const onDraftChange = vi.fn();
    const view = await render(undefined, 'JD text', draft, onDraftChange as never);
    await waitFor(() => expect(getByRole(view, 'button')).toHaveProperty('disabled', false));
    await click(getByRole(view, 'button', '快速判断'));
    await waitFor(() => expect(state.create).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(view.textContent).toContain('原资料已更新，本次结果仍使用旧版'));
    expect(onDraftChange).toHaveBeenCalledWith(expect.objectContaining({
      triage: expect.objectContaining({ stage_status: 'source_conflict' }),
      triageKey: null,
      resultUnknown: false,
    }));
  });

  it('clears the Drawer draft when source-conflict recovery reports a missing review', async () => {
    state.create.mockRejectedValue({
      response: { status: 409, data: { error_code: 'application_jd_source_conflict' } },
    });
    state.sourceConflict.mockResolvedValue({ status: 'review_missing' });
    let cleared = false;
    const onDraftChange = vi.fn((patch: Record<string, unknown> | null) => {
      if (patch === null) cleared = true;
    });
    const draft = {
      applicationId: 7,
      resumeId: 11,
      jdText: 'JD text',
      jdVersionId: 1,
      assertionsText: '',
      triageKey: null,
      deepKey: null,
      triage: null,
      deep: null,
      historical: false,
      resultUnknown: false,
      error: null,
    };
    const view = await render(undefined, 'JD text', draft, onDraftChange);
    await waitFor(() => expect(getByRole(view, 'button')).toHaveProperty('disabled', false));
    await click(getByRole(view, 'button', '快速判断'));
    await waitFor(() => expect(cleared).toBe(true));
    expect(view.querySelector('select')).not.toBeNull();
    expect(view.textContent).toContain('当前投递或岗位评估已不存在');
  });

  it('keeps the latest Drawer history selection when an earlier response arrives late', async () => {
    state.listV2.mockResolvedValue([historyV2(21), historyV2(22)]);
    const resolveHistory = new Map<number, (value: unknown) => void>();
    state.getV2.mockImplementation((_applicationId: number, reviewId: number) => (
      new Promise((resolve) => { resolveHistory.set(reviewId, resolve); })
    ));
    const view = await render();
    await waitFor(() => {
      expect([...view.querySelectorAll<HTMLButtonElement>('button')].filter((button) => button.textContent === '查看')).toHaveLength(2);
    });
    const historyButtons = [...view.querySelectorAll<HTMLButtonElement>('button')]
      .filter((button) => button.textContent === '查看');
    await act(async () => {
      historyButtons[0].click();
      historyButtons[1].click();
      await Promise.resolve();
    });
    expect(state.getV2).toHaveBeenCalledTimes(2);

    resolveHistory.get(21)?.({
      stages: [{
        review_id: 21,
        stage_id: 210,
        stage: 'triage',
        stage_status: 'ready',
        resume_id: 11,
        jd_version_id: 1,
        idempotency_key: 'history-b-key',
        proposal: {
          summary: { text: 'Drawer history B', evidence_refs: [] },
          conditions: [], risks: [], questions: [], next_steps: [],
        },
      }],
    });
    await flush();
    expect(view.textContent).toContain('Drawer history B');

    resolveHistory.get(22)?.({
      stages: [{
        review_id: 22,
        stage_id: 220,
        stage: 'triage',
        stage_status: 'ready',
        resume_id: 11,
        jd_version_id: 1,
        idempotency_key: 'history-a-key',
        proposal: {
          summary: { text: 'Drawer history A late', evidence_refs: [] },
          conditions: [], risks: [], questions: [], next_steps: [],
        },
      }],
    });
    await flush();

    expect(view.textContent).toContain('Drawer history B');
    expect(view.textContent).not.toContain('Drawer history A late');
  });

  it('disables Drawer history while Triage is in flight', async () => {
    let resolveCreate: ((value: unknown) => void) | undefined;
    state.list.mockResolvedValue([historyV1(8)]);
    state.listV2.mockResolvedValue([historyV2(21)]);
    state.create.mockImplementation(() => new Promise((resolve) => { resolveCreate = resolve; }));
    const view = await render(undefined, 'JD text');
    const select = view.querySelector('select');
    if (!(select instanceof HTMLSelectElement)) throw new Error('Expected resume selector');
    await waitFor(() => expect(select.querySelector('option[value="11"]')).toBeTruthy());
    await act(async () => setValue(select, '11'));
    await waitFor(() => expect(getByRole(view, 'button', '快速判断')).toHaveProperty('disabled', false));
    await click(getByRole(view, 'button', '快速判断'));
    await waitFor(() => expect(state.create).toHaveBeenCalledTimes(1));

    await waitFor(() => {
      const historyButtons = [...view.querySelectorAll<HTMLButtonElement>('button')]
        .filter((button) => button.textContent?.includes('\u67e5\u770b'));
      expect(historyButtons).toHaveLength(2);
      historyButtons.forEach((button) => {
        expect(button.disabled).toBe(true);
        act(() => button.click());
      });
    });
    expect(state.get).not.toHaveBeenCalled();
    expect(state.getV2).not.toHaveBeenCalled();

    resolveCreate?.({
      review_id: 31,
      stage_id: 310,
      stage: 'triage',
      stage_status: 'ready',
      confirmation_token: 'confirm-token',
      resume_id: 11,
      jd_version_id: 1,
      idempotency_key: 'current-triage-key',
      proposal: { summary: { text: 'Current Triage', evidence_refs: [] }, conditions: [], risks: [], questions: [], next_steps: [] },
    });
    await flush();
  });

  it('keeps Drawer history disabled after remounting an unknown attempt', async () => {
    state.list.mockResolvedValue([historyV1(8)]);
    state.listV2.mockResolvedValue([historyV2(21)]);
    const view = await render(undefined, 'JD text', {
      applicationId: 7,
      resumeId: 11,
      jdText: 'JD text',
      jdVersionId: 1,
      assertionsText: '',
      triageKey: 'triage-key-00000001',
      deepKey: null,
      triage: null,
      deep: null,
      historical: false,
      resultUnknown: true,
      error: 'result unknown',
    });

    await waitFor(() => {
      const historyButtons = [...view.querySelectorAll<HTMLButtonElement>('button')]
        .filter((button) => button.textContent?.includes('\u67e5\u770b'));
      expect(historyButtons).toHaveLength(2);
      historyButtons.forEach((button) => {
        expect(button.disabled).toBe(true);
        act(() => button.click());
      });
    });
    expect(state.get).not.toHaveBeenCalled();
    expect(state.getV2).not.toHaveBeenCalled();
  });

  it('keeps Drawer history disabled while a persisted Deep attempt is unresolved', async () => {
    state.list.mockResolvedValue([historyV1(8)]);
    state.listV2.mockResolvedValue([historyV2(21)]);
    const view = await render(undefined, 'JD text', {
      applicationId: 7,
      resumeId: 11,
      jdText: 'JD text',
      jdVersionId: 1,
      assertionsText: '',
      triageKey: null,
      deepKey: 'deep-key-00000001',
      triage: null,
      deep: null,
      historical: false,
      resultUnknown: false,
      error: null,
    });

    await waitFor(() => {
      const historyButtons = [...view.querySelectorAll<HTMLButtonElement>('button')]
        .filter((button) => button.textContent?.includes('\u67e5\u770b'));
      expect(historyButtons).toHaveLength(2);
      historyButtons.forEach((button) => expect(button.disabled).toBe(true));
    });
  });

  it('drops a late Drawer history response when a new Triage starts', async () => {
    let resolveHistory: ((value: unknown) => void) | undefined;
    state.listV2.mockResolvedValue([historyV2(21)]);
    state.getV2.mockImplementationOnce(() => new Promise((resolve) => { resolveHistory = resolve; }));
    state.create.mockImplementation((_applicationID, input: { idempotency_key: string }) => ({
      review_id: 31,
      stage_id: 310,
      stage: 'triage',
      stage_status: 'ready',
      confirmation_token: 'confirm-token',
      resume_id: 11,
      jd_version_id: 1,
      idempotency_key: input.idempotency_key,
      proposal: { summary: { text: 'Current Drawer Triage', evidence_refs: [] }, conditions: [], risks: [], questions: [], next_steps: [] },
    }));
    const view = await render(undefined, 'JD text');
    await waitFor(() => expect([...view.querySelectorAll<HTMLButtonElement>('button')].find((button) => button.textContent === '查看')).toBeTruthy());
    const historyButton = [...view.querySelectorAll<HTMLButtonElement>('button')]
      .find((button) => button.textContent === '查看');
    if (!historyButton) throw new Error('Drawer history button was not mounted');
    await click(historyButton);
    const select = getByLabelText(view, '用于审阅的简历') as HTMLSelectElement;
    await act(async () => setValue(select, '11'));
    await waitFor(() => expect(getByRole(view, 'button', '开始快速判断')).toHaveProperty('disabled', false));
    await click(getByRole(view, 'button', '开始快速判断'));
    await waitFor(() => expect(state.create).toHaveBeenCalledTimes(1));

    resolveHistory?.({
      stages: [{
        review_id: 21,
        stage_id: 210,
        stage: 'triage',
        stage_status: 'ready',
        resume_id: 11,
        jd_version_id: 1,
        idempotency_key: 'stale-history-key',
        proposal: { summary: { text: 'Stale Drawer history after Triage', evidence_refs: [] }, conditions: [], risks: [], questions: [], next_steps: [] },
      }],
    });
    await flush();

    expect(view.textContent).toContain('Current Drawer Triage');
    expect(view.textContent).not.toContain('Stale Drawer history after Triage');
  });

  it('preserves the original Triage key when source-conflict recovery is temporarily unavailable', async () => {
    state.create.mockRejectedValue({
      response: { status: 409, data: { error_code: 'application_jd_source_conflict' } },
    });
    state.sourceConflict.mockRejectedValue(new Error('temporary source-conflict read failure'));
    let draft: Record<string, unknown> = {
      applicationId: 7,
      resumeId: 11,
      jdText: 'JD text',
      jdVersionId: 1,
      assertionsText: '',
      triageKey: null,
      deepKey: null,
      triage: null,
      deep: null,
      historical: false,
      resultUnknown: false,
      error: null,
    };
    const onDraftChange = vi.fn((patch: Record<string, unknown> | null) => {
      if (patch) draft = { ...draft, ...patch };
    });
    const view = await render(undefined, 'JD text', draft, onDraftChange);
    await waitFor(() => expect(getByRole(view, 'button')).toHaveProperty('disabled', false));
    await click(getByRole(view, 'button'));
    await waitFor(() => expect(draft.triageKey).toBeTruthy());

    expect(draft.resultUnknown).toBe(true);
    expect(draft.triageKey).toEqual(expect.any(String));
    expect(draft.error).toEqual(expect.any(String));
  });

  it('restores a persisted Deep Review source conflict instead of dropping the stage', async () => {
    state.deepV2.mockRejectedValue({
      response: { status: 409, data: { error_code: 'application_jd_source_conflict' } },
    });
    state.sourceConflict.mockResolvedValue({ status: 'found', stage: {
      review_id: 21,
      stage_id: 23,
      stage: 'deep_review',
      stage_status: 'source_conflict',
      resume_id: 11,
      jd_version_id: 1,
      idempotency_key: 'deep-key-00000001',
      proposal: undefined,
    }});
    const draft = {
      applicationId: 7,
      resumeId: 11,
      jdText: 'JD text',
      jdVersionId: 1,
      assertionsText: '',
      triageKey: 'triage-key-00000001',
      deepKey: null,
      triage: {
        review_id: 21,
        stage_id: 22,
        stage: 'triage',
        stage_status: 'confirmed',
        confirmation_token: null,
        resume_id: 11,
        jd_version_id: 1,
        idempotency_key: 'triage-key-00000001',
        proposal: undefined,
      },
      deep: null,
      historical: false,
      resultUnknown: false,
      error: null,
    };
    const onDraftChange = vi.fn();
    const view = await render(undefined, 'JD text', draft, onDraftChange as never);
    await click(getByRole(view, 'button', '深入分析'));
    await waitFor(() => expect(state.deepV2).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(state.sourceConflict).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(view.textContent).toContain('原资料已更新，本次结果仍使用旧版'));
    expect(onDraftChange).toHaveBeenCalledWith(expect.objectContaining({
      deep: expect.objectContaining({ stage_status: 'source_conflict' }),
      deepKey: null,
      resultUnknown: false,
    }));
  });

  it('hands off the exact frozen JD and disables handoff after the current version changes', async () => {
    const onPrepareMaterials = vi.fn();
    const draft = {
      applicationId: 7,
      resumeId: 11,
      jdText: 'Frozen JD text',
      jdVersionId: 1,
      assertionsText: '',
      triageKey: 'triage-key-00000001',
      deepKey: 'deep-key-00000001',
      triage: {
        review_id: 21,
        stage_id: 22,
        stage: 'triage',
        stage_status: 'confirmed',
        confirmation_token: null,
        resume_id: 11,
        jd_version_id: 1,
        idempotency_key: 'triage-key-00000001',
        proposal: undefined,
      },
      deep: {
        review_id: 21,
        stage_id: 23,
        stage: 'deep_review',
        stage_status: 'ready',
        confirmation_token: null,
        resume_id: 11,
        jd_version_id: 1,
        idempotency_key: 'deep-key-00000001',
        proposal: undefined,
      },
      historical: false,
      resultUnknown: false,
      error: null,
    };
    const view = await render(onPrepareMaterials, 'Current JD text', draft);
    await waitFor(() => expect(getByRole(view, 'button')).toHaveProperty('disabled', false));
    await click(getByRole(view, 'button'));
    expect(onPrepareMaterials).toHaveBeenCalledWith(11, 'Frozen JD text', 1);

    await act(async () => {
      root?.render(
        <OpportunityFitReviewDrawer
          application={application}
          open
          currentJdText="New current JD text"
          jdVersionId={2}
          draft={draft as never}
          onDraftChange={vi.fn()}
          onClose={vi.fn()}
          onPrepareMaterials={onPrepareMaterials}
        />,
      );
    });
    expect(getByRole(view, 'button')).toHaveProperty('disabled', true);
  });
  it('blocks more than ten assertions before submit', async () => {
    const view = await render();
    await waitFor(() => expect(getByLabelText(view, '本次补充断言（每行一条）')).toBeTruthy());
    const assertions = getByLabelText(view, '本次补充断言（每行一条）') as HTMLTextAreaElement;
    await act(async () => {
      setValue(assertions, Array.from({ length: 11 }, (_, index) => `Fact ${index}`).join('\n'));
    });
    expect(view.textContent).toContain('最多填写 10 条非空断言。');
    expect(getByRole(view, 'button', '开始快速判断')).toHaveProperty('disabled', true);
    expect(state.create).not.toHaveBeenCalled();
  });

  it('submits trimmed assertions as independent input', async () => {
    const view = await render();
    await waitFor(() => expect(getByLabelText(view, '用于审阅的简历')).toBeTruthy());
    const select = getByLabelText(view, '用于审阅的简历') as HTMLSelectElement;
    const jd = getByLabelText(view, '用户粘贴的 JD') as HTMLTextAreaElement;
    const assertions = getByLabelText(view, '本次补充断言（每行一条）') as HTMLTextAreaElement;
    await waitFor(() => expect(select.querySelector('option[value="11"]')).toBeTruthy());
    await act(async () => {
      setValue(select, '11');
      setValue(jd, 'JD text');
      setValue(assertions, ' fact one \n\n fact two ');
    });
    await waitFor(() => expect(getByRole(view, 'button', '开始快速判断')).toHaveProperty('disabled', false));
    await click(getByRole(view, 'button', '开始快速判断'));
    await waitFor(() => expect(state.create).toHaveBeenCalledWith(7, expect.objectContaining({
      resume_id: 11,
      jd_version_id: 1,
      candidate_assertions: ['fact one', 'fact two'],
    })));
  });

  it('keeps an active v2 review when the current JD query refreshes', async () => {
    state.create.mockImplementation((_applicationID, input: { idempotency_key: string }) => ({
      review_id: 21,
      stage_id: 22,
      stage: 'triage',
      stage_status: 'ready',
      confirmation_token: 'confirm-token',
      resume_id: 11,
      jd_version_id: 1,
      idempotency_key: input.idempotency_key,
      proposal: {
        summary: { text: 'Frozen triage result', evidence_refs: [] },
        conditions: [],
        risks: [],
        next_steps: [],
        questions: [],
      },
    }));
    const view = await render(undefined, 'JD version one');
    const select = view.querySelector('select');
    if (!(select instanceof HTMLSelectElement)) throw new Error('Expected resume selector');
    await waitFor(() => expect(select.querySelector('option[value="11"]')).toBeTruthy());
    await act(async () => setValue(select, '11'));
    await waitFor(() => expect(getByRole(view, 'button', '快速判断')).toHaveProperty('disabled', false));
    await click(getByRole(view, 'button', '快速判断'));
    await waitFor(() => expect(view.textContent).toContain('Frozen triage result'));

    await act(async () => {
      root?.render(
        <OpportunityFitReviewDrawer
          application={application}
          open
          currentJdText="JD version two"
          jdVersionId={2}
          onClose={vi.fn()}
        />,
      );
      await Promise.resolve();
    });

    expect(view.textContent).toContain('Frozen triage result');
    expect(view.textContent).not.toContain('当前投递尚未确认岗位资料');
  });

  it('reuses the triage key after a provider-unknown 502 and remount', async () => {
    state.create.mockRejectedValue({
      response: { status: 502, data: { error_code: 'opportunity_fit_provider_error' } },
    });
    let draft: Record<string, unknown> = {
      applicationId: 7,
      resumeId: 11,
      jdText: 'JD text',
      jdVersionId: 1,
      assertionsText: '',
      triageKey: null,
      deepKey: null,
      triage: null,
      deep: null,
      historical: false,
      resultUnknown: false,
      error: null,
    };
    const onDraftChange = vi.fn((patch: Record<string, unknown>) => {
      draft = { ...draft, ...patch };
    });
    const view = await render(undefined, 'JD text', draft, onDraftChange);
    await waitFor(() => expect(getByRole(view, 'button')).toHaveProperty('disabled', false));
    await click(getByRole(view, 'button'));
    await waitFor(() => expect(draft.triageKey).toBeTruthy());
    const key = draft.triageKey;
    await act(async () => {
      root?.render(null);
      await Promise.resolve();
      root?.render(
        <OpportunityFitReviewDrawer
          application={application}
          open
          currentJdText="JD text"
          jdVersionId={1}
          draft={draft as never}
          onDraftChange={onDraftChange as never}
          onClose={vi.fn()}
        />,
      );
    });
    expect(view.querySelector('select')).toHaveProperty('disabled', true);
    await click(getByRole(view, 'button'));
    await waitFor(() => expect(state.create).toHaveBeenCalledTimes(2));
    expect(state.create.mock.calls[0][1].idempotency_key).toBe(key);
    expect(state.create.mock.calls[1][1].idempotency_key).toBe(key);
  });

  it('hands historical review frozen JD and resume to material preparation', async () => {
    state.history = [historyV1(8)];
    state.list.mockResolvedValue(state.history);
    const onPrepareMaterials = vi.fn();
    const view = await render(onPrepareMaterials);

    await waitFor(() => expect(getByRole(view, 'button', '查看')).toBeTruthy());
    await click(getByRole(view, 'button', '查看'));
    await waitFor(() => expect(state.get).toHaveBeenCalledWith(7, 8));
    expect(view.textContent).toContain('Frozen JD text');
    expect(onPrepareMaterials).not.toHaveBeenCalled();
  });

  it.each([
    ['404', { response: { status: 404, data: { error: 'raw history 404' } } }, '请求的岗位评估不存在或不可用，请刷新后重试'],
    ['502', { response: { status: 502, data: { error: 'raw history 502' } } }, 'AI 服务暂不可用，请稍后重试'],
    ['unknown', new Error('raw history error'), '操作失败，请稍后重试'],
  ])('shows safe copy when history list fails with %s', async (_name, error, expected) => {
    state.list.mockRejectedValue(error);
    const view = await render();

    await waitFor(() => expect(getByRole(view, 'alert').textContent).toContain(expected));
    expect(view.textContent).not.toContain('raw history');
  });

  it('shows safe mapped copy instead of raw Opportunity Fit errors', async () => {
    state.create.mockRejectedValue({
      response: {
        status: 502,
        data: {
          error_code: 'opportunity_fit_unverifiable',
          error: 'raw provider text',
        },
      },
    });
    const view = await render();
    await waitFor(() => expect(getByLabelText(view, '用户粘贴的 JD')).toBeTruthy());
    const jd = getByLabelText(view, '用户粘贴的 JD') as HTMLTextAreaElement;
    const select = getByLabelText(view, '用于审阅的简历') as HTMLSelectElement;
    await waitFor(() => expect(select.querySelector('option[value="11"]')).toBeTruthy());
    await act(async () => {
      setValue(select, '11');
      setValue(jd, 'JD text');
    });

    await waitFor(() => expect(getByRole(view, 'button', '开始快速判断')).toHaveProperty('disabled', false));
    await click(getByRole(view, 'button', '开始快速判断'));
    await waitFor(() => expect(view.textContent).toContain('AI 输出未通过证据校验，可重试；原简历已保护，未创建草稿。'));

    expect(view.textContent).toContain('AI 输出未通过证据校验，可重试；原简历已保护，未创建草稿。');
    expect(view.textContent).not.toContain('raw provider text');
  });

  it('renders Chinese labels for Opportunity Fit enum values', async () => {
    state.history = [historyV1(9)];
    state.list.mockResolvedValue(state.history);
    state.get.mockResolvedValue({
      id: 9,
      recommendation: 'advance',
      source: {
        resume: { id: 11, title: 'Frozen Resume', sha256: 'resume' },
        jd: { source_label: 'Frozen JD label', sha256: 'jd', text: 'Frozen JD original text' },
        candidate_assertions: [],
      },
      triage: {
        summary: {
          text: 'Dynamic AI summary',
          evidence_refs: [{ source: 'jd', path: 'requirements.location', excerpt: 'Dynamic evidence excerpt' }],
        },
        recommendation: 'advance',
        hard_constraints: [
          { id: 'constraint-a', requirement: 'Dynamic requirement A', status: 'met', explanation: 'Dynamic explanation A', evidence_refs: [] },
          { id: 'constraint-b', requirement: 'Dynamic requirement B', status: 'unmet', explanation: 'Dynamic explanation B', evidence_refs: [] },
          { id: 'constraint-c', requirement: 'Dynamic requirement C', status: 'unknown', explanation: 'Dynamic explanation C', evidence_refs: [] },
        ],
        fit_signals: [{ id: 'signal-a', statement: 'Dynamic AI statement', evidence_refs: [] }],
        gaps: [
          { id: 'gap-a', requirement: 'Dynamic gap A', kind: 'required', candidate_status: 'unmet', evidence_refs: [] },
          { id: 'gap-b', requirement: 'Dynamic gap B', kind: 'preferred', candidate_status: 'met', evidence_refs: [] },
        ],
        deadline: { status: 'not_stated', text: '', evidence_refs: [] },
        next_questions: [],
      },
      deep_review: {
        strengths: [],
        gaps_to_address: [],
        questions_to_clarify: [],
        recommended_path: 'prepare_materials',
        next_actions: [{ id: 'action-a', label: 'Dynamic next action label', kind: 'open_material_kit' }],
      },
    });
    const view = await render();

    await waitFor(() => expect(getByRole(view, 'button', '查看')).toBeTruthy());
    await click(getByRole(view, 'button', '查看'));
    await waitFor(() => expect(view.textContent).toContain('Frozen Resume'));

    const renderedText = view.textContent || '';
    expect(renderedText).toContain('Frozen Resume');
    expect(renderedText).toContain('Frozen JD label');
    expect(renderedText).toContain('Frozen JD original text');
    expect(renderedText).toContain('岗位描述（仅用于分析方向）');
    expect(renderedText).not.toContain('仅决定分析方向');
    expect(renderedText).toContain('Dynamic AI summary');
    expect(renderedText).toContain('Dynamic AI statement');
    expect(renderedText).toContain('Dynamic explanation A');
    expect(renderedText).toContain('Dynamic evidence excerpt');
    expect(renderedText).toContain('Dynamic next action label');
    expect(renderedText).toContain('建议推进');
    expect(renderedText).toContain('已满足');
    expect(renderedText).toContain('未满足');
    expect(renderedText).toContain('待确认');
    expect(renderedText).toContain('必要条件');
    expect(renderedText).toContain('优先条件');
    expect(renderedText).toContain('建议准备材料');
    expect(renderedText).not.toMatch(/\b(advance|hold|decline|met|unmet|unknown|required|preferred|prepare_materials|clarify_first|do_not_pursue)\b/);
  });

  it('recovers a consumed Triage confirmation from the current server stage', async () => {
    const confirmed = {
      review_id: 21,
      stage_id: 22,
      stage: 'triage' as const,
      stage_status: 'confirmed' as const,
      confirmation_token: null,
      resume_id: 11,
      jd_version_id: 1,
      idempotency_key: 'triage-key-00000001',
      proposal: {
        summary: { text: 'Confirmed triage', evidence_refs: [] },
        conditions: [],
        risks: [],
        next_steps: [],
        questions: [],
      },
    };
    state.confirm.mockRejectedValue({
      response: { status: 409, data: { error_code: 'opportunity_fit_triage_confirmation_consumed' } },
    });
    state.getV2.mockResolvedValue({ stages: [confirmed] });
    const onDraftChange = vi.fn();
    const ownerStates: Array<{ pending: boolean; resultUnknown: boolean; unsaved: boolean }> = [];
    const view = await render(undefined, 'JD text', {
      applicationId: 7,
      resumeId: 11,
      jdText: 'JD text',
      jdVersionId: 1,
      assertionsText: '',
      triageKey: 'triage-key-00000001',
      deepKey: null,
      triage: {
        ...confirmed,
        stage_status: 'ready',
        confirmation_token: 'confirm-token',
      },
      deep: null,
      historical: false,
      resultUnknown: false,
      error: null,
    }, onDraftChange, { onOwnerStateChange: (state) => ownerStates.push(state) });

    await click(getByRole(view, 'button', '快速判断'));
    await waitFor(() => expect(state.getV2).toHaveBeenCalledWith(7, 21));
    expect(onDraftChange).toHaveBeenCalledWith(expect.objectContaining({ triage: confirmed, resultUnknown: false }));
    expect(view.textContent).toContain('Confirmed triage');
    expect(ownerStates[ownerStates.length - 1]).toEqual({ pending: false, resultUnknown: false, unsaved: true });
  });

  it('preserves the Triage confirmation attempt when the response and status lookup are unknown', async () => {
    state.confirm.mockRejectedValue({ response: { status: 503, data: { error_code: 'provider_timeout' } } });
    state.getV2.mockRejectedValue(new Error('status lookup unavailable'));
    const onDraftChange = vi.fn();
    const view = await render(undefined, 'JD text', {
      applicationId: 7,
      resumeId: 11,
      jdText: 'JD text',
      jdVersionId: 1,
      assertionsText: '',
      triageKey: 'triage-key-00000001',
      deepKey: null,
      triage: {
        review_id: 21,
        stage_id: 22,
        stage: 'triage',
        stage_status: 'ready',
        confirmation_token: 'confirm-token',
        resume_id: 11,
        jd_version_id: 1,
        idempotency_key: 'triage-key-00000001',
        proposal: { summary: { text: 'Ready triage', evidence_refs: [] }, conditions: [], risks: [], next_steps: [], questions: [] },
      },
      deep: null,
      historical: false,
      resultUnknown: false,
      error: null,
    }, onDraftChange);

    await click(getByRole(view, 'button', '快速判断'));
    await waitFor(() => expect(onDraftChange).toHaveBeenCalledWith(expect.objectContaining({ resultUnknown: true })));
    expect(view.textContent).toContain('操作结果待确认');
  });

  it('allows a completed v2 review to be explicitly reset into a new input attempt', async () => {
    const onDraftChange = vi.fn();
    const view = await render(undefined, 'JD text', {
      applicationId: 7,
      resumeId: 11,
      jdText: 'JD text',
      jdVersionId: 1,
      assertionsText: '',
      triageKey: 'triage-key-00000001',
      deepKey: null,
      triage: {
        review_id: 21,
        stage_id: 22,
        stage: 'triage',
        stage_status: 'confirmed',
        confirmation_token: null,
        resume_id: 11,
        jd_version_id: 1,
        idempotency_key: 'triage-key-00000001',
        proposal: {
          summary: { text: 'Old result', evidence_refs: [] },
          conditions: [],
          risks: [],
          next_steps: [],
          questions: [],
        },
      },
      deep: null,
      historical: false,
      resultUnknown: false,
      error: null,
    }, onDraftChange);

    await click(getByRole(view, 'button', '重新开始岗位评估'));
    expect(onDraftChange).toHaveBeenCalledWith(null);
    expect(view.textContent).not.toContain('Old result');
    expect(view.querySelector('select')).not.toBeNull();
  });

  it('renders a source conflict as a Chinese read-only state instead of an endless spinner', async () => {
    const view = await render(undefined, 'JD text', {
      applicationId: 7,
      resumeId: 11,
      jdText: 'JD text',
      jdVersionId: 1,
      assertionsText: '',
      triageKey: 'triage-key-00000001',
      deepKey: null,
      triage: {
        review_id: 21,
        stage_id: 22,
        stage: 'triage',
        stage_status: 'source_conflict',
        confirmation_token: null,
        resume_id: 11,
        jd_version_id: 1,
        idempotency_key: 'triage-key-00000001',
        proposal: undefined,
      },
      deep: null,
      historical: false,
      resultUnknown: false,
      error: null,
    });

    expect(view.textContent).toContain('原资料已更新，本次结果仍使用旧版');
    expect(view.textContent).not.toContain('loading');
  });
});
