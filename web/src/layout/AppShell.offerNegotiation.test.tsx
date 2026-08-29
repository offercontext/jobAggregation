// @vitest-environment jsdom
import { act, useEffect, useRef, useState } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import AppShell from './AppShell';

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

const opportunityFitState = vi.hoisted(() => ({
  createTriage: vi.fn(),
  confirmTriage: vi.fn(),
  createDeep: vi.fn(),
  getReview: vi.fn(),
  getLegacyReview: vi.fn(),
  listReviews: vi.fn(),
  legacyHistory: [] as Array<{ id: number }>,
  sourceConflict: vi.fn(),
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
      events: [], offers: [offer], resumes: [{ id: 11, title: '简历' }], knowledge: [], questions: undefined,
      'application-jd-current': { current: { id: 1, application_id: 7, jd_text: 'JD text' } },
      'opportunity-fit-v1-reviews': opportunityFitState.legacyHistory,
      'opportunity-fit-v2-reviews': [
        { review_id: 201, stage_count: 1 },
        { review_id: 202, stage_count: 1 },
      ],
    };
    return { data: data[key], isError: false, isLoading: false, isFetching: false, error: null };
  },
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}));

vi.mock('@/services/opportunityFitReviews', () => ({
  createOpportunityFitV2Triage: opportunityFitState.createTriage,
  confirmOpportunityFitV2Triage: opportunityFitState.confirmTriage,
  createOpportunityFitV2DeepReview: opportunityFitState.createDeep,
  getOpportunityFitV2Review: opportunityFitState.getReview,
  findOpportunityFitV2SourceConflictStage: opportunityFitState.sourceConflict,
  listOpportunityFitV2Reviews: opportunityFitState.listReviews,
  listOpportunityFitReviews: vi.fn().mockResolvedValue([]),
  getOpportunityFitReview: opportunityFitState.getLegacyReview,
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
          打开岗位评估
        </button>
        {!isPilotNegotiation ? (
          <>
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
vi.mock('@/components/KanbanBoard', () => ({ default: () => <div /> }));
vi.mock('@/components/ApplicationListView', () => ({ default: () => <div /> }));
vi.mock('@/components/CalendarView', () => ({ default: () => <div /> }));
vi.mock('@/components/KnowledgeSourcesView', () => ({ default: () => <div /> }));
vi.mock('@/components/QuestionBankView', () => ({ default: () => <div /> }));
vi.mock('@/components/InterviewV01View', () => ({ default: () => <div /> }));
vi.mock('@/components/ResumeLibraryView', () => ({ default: () => <div /> }));
vi.mock('@/features/reminders/RemindersView', () => ({ default: () => <div /> }));
vi.mock('@/components/SettingsView', () => ({ default: () => <div /> }));
vi.mock('@/features/dashboard/DashboardView', () => ({
  default: (props: any) => (
    <section data-testid="dashboard-harness">
      <button type="button" data-testid="open-application-detail" onClick={() => props.onOpenDetailById?.(7)}>
        查看投递
      </button>
      <button type="button" data-testid="open-application-detail-b" onClick={() => props.onOpenDetailById?.(8)}>
        查看另一份投递
      </button>
    </section>
  ),
}));
vi.mock('@/features/pilot/PilotAttachmentContext', () => ({
  PilotAttachmentProvider: (props: any) => <>{props.children}</>,
  usePilotAttachmentStore: () => ({ addAttachment: vi.fn(), createNewDraftWithAttachment: vi.fn() }),
}));
vi.mock('@/features/pilot/attachmentHandoff', () => ({ retainPilotAttachmentKey: (_current: unknown, next: unknown) => next }));
vi.mock('@/features/pilot/PilotOpportunityFitCard', () => ({ default: () => <div /> }));

vi.mock('@/features/pilot/PilotOpportunityFitV2Card', () => ({
  default: (props: any) => {
    const [confirmation, setConfirmation] = useState<'triage' | 'deep' | null>(null);
    const draft = props.draft ?? {};
    const assertions = String(draft.assertionsText ?? '').split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
    const input = {
      schema_version: 2,
      resume_id: draft.resumeId ?? 0,
      jd_version_id: draft.jdVersionId ?? 0,
      jd_source_label: '用户粘贴 JD',
      candidate_assertions: assertions,
      idempotency_key: draft.triageKey ?? 'pilot-triage-key',
    };
    const triageStatus = draft.triage?.stage_status;
    const deepStatus = draft.deep?.stage_status;
    const historyDisabled = Boolean(props.historyDisabled || props.triageLoading || props.deepLoading || draft.resultUnknown);
    return (
      <section aria-label="岗位评估">
        <select
          value={draft.resumeId ?? ''}
          onChange={(event) => props.onChange?.({ resumeId: Number(event.target.value) || undefined })}
          disabled={Boolean(draft.triageKey) || props.triageLoading || props.deepLoading || draft.resultUnknown}
        >
          <option value="">请选择简历</option>
          {(props.resumes ?? []).map((resume: any) => <option key={resume.id} value={resume.id}>{resume.title ?? resume.name}</option>)}
        </select>
        <textarea value={draft.jdText ?? ''} readOnly disabled={Boolean(draft.triageKey) || props.triageLoading || props.deepLoading || draft.resultUnknown} />
        <aside aria-label="历史岗位评估">
          {(props.legacyHistory ?? []).map((item: any) => (
            <div key={`legacy-${item.id}`}>
              <span>旧版评估 #{item.id}</span>
              {props.onViewLegacyHistory ? <button type="button" disabled={historyDisabled} onClick={() => props.onViewLegacyHistory(item.id)}>查看</button> : null}
            </div>
          ))}
          {(props.history ?? []).map((item: any) => (
            <div key={item.review_id}>
              <span>评估 #{item.review_id}</span>
              <button type="button" disabled={historyDisabled} onClick={() => props.onViewHistory(item.review_id)}>查看</button>
            </div>
          ))}
        </aside>
        {props.legacyReview ? <p>{props.legacyReview.summary?.text}</p> : null}
        {draft.error ? <p role="status">{draft.error}</p> : null}
        {draft.triage?.proposal?.summary?.text ? <p>{draft.triage.proposal.summary.text}</p> : null}
        {!draft.triage && !props.triageLoading && !draft.error ? (
          <button type="button" onClick={() => setConfirmation('triage')}>快速判断</button>
        ) : null}
        {draft.error && draft.triageKey && !draft.triage ? (
          <button type="button" onClick={() => props.onStartTriage?.(input)}>使用原尝试重试快速判断</button>
        ) : null}
        {triageStatus === 'ready' ? (
          <button type="button" onClick={() => props.onConfirmTriage?.()}>
            {draft.resultUnknown ? '使用原尝试重试快速判断' : '确认快速判断'}
          </button>
        ) : null}
        {triageStatus === 'confirmed' && !draft.deep ? (
          <button type="button" disabled={props.deepLoading} onClick={() => setConfirmation('deep')}>深入分析</button>
        ) : null}
        {deepStatus === 'ready' && draft.deep?.proposal?.summary?.text ? <p>{draft.deep.proposal.summary.text}</p> : null}
        {(
          draft.historical
          || triageStatus === 'confirmed'
          || deepStatus === 'ready'
          || triageStatus === 'source_conflict'
          || deepStatus === 'source_conflict'
        ) ? (
          <button type="button" aria-label="重新开始岗位评估" disabled={props.restartDisabled} onClick={() => props.onStartNew?.()}>开始新的岗位评估</button>
        ) : null}
        <button type="button" onClick={props.onCancel}>取消流程</button>
        {confirmation ? (
          <div role="dialog" aria-modal="true">
            <button type="button" onClick={() => setConfirmation(null)}>取消</button>
            <button type="button" onClick={() => {
              const next = confirmation;
              setConfirmation(null);
              if (next === 'triage') props.onStartTriage?.(input);
              else props.onStartDeepReview?.();
            }}>确认</button>
          </div>
        ) : null}
      </section>
    );
  },
}));

vi.mock('@/components/OfferCenterView', () => ({
  default: (props: any) => (
    <section data-testid="offer-center-harness">
      <button type="button" data-testid="open-ui-offer" onClick={() => props.onOpenNegotiation?.(offer)}>打开 UI 谈薪准备</button>
    </section>
  ),
}));

vi.mock('@/components/ChatPanel', () => ({
  default: (props: any) => (
    <section data-testid={`chat-${props.variant ?? 'drawer'}`}>
      <button type="button" data-testid="open-pilot-offer" onClick={() => props.onPrepareOfferNegotiation?.(offer)}>打开 Pilot 谈薪准备</button>
    </section>
  ),
}));

vi.mock('@/components/MockInterviewDrawer', () => ({ default: () => <div /> }));
vi.mock('@/components/OfferNegotiationDrawer', () => ({
  default: (props: any) => (
    <section data-testid="offer-negotiation-drawer-harness">
      <output data-testid="pilot-draft-goal">{props.draft?.goal ?? ''}</output>
      <button type="button" data-testid="save-pilot-draft" onClick={() => props.onDraftChange?.(baseDraft('Pilot 目标'))}>保存 Pilot 草稿</button>
      <button type="button" data-testid="close-pilot-drawer" onClick={props.onClose}>关闭</button>
    </section>
  ),
}));

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

describe('AppShell mounted Opportunity Fit confirmation recovery', () => {
  beforeEach(() => {
    window.history.replaceState(null, '', '/');
    opportunityFitState.createTriage.mockReset();
    opportunityFitState.confirmTriage.mockReset();
    opportunityFitState.createDeep.mockReset();
    opportunityFitState.getReview.mockReset();
    opportunityFitState.getLegacyReview.mockReset();
    opportunityFitState.listReviews.mockReset();
    opportunityFitState.legacyHistory = [];
    opportunityFitState.createTriage.mockResolvedValue({
      stage_id: 101,
      review_id: 201,
      resume_id: 11,
      jd_version_id: 1,
      stage: 'triage',
      schema_version: 2,
      stage_status: 'ready',
      parent_triage_stage_id: null,
      idempotency_key: 'pilot-triage-key',
      source_fingerprint_sha256: 'frozen-source',
      confirmation_token: 'triage-confirmation-token',
      proposal: {
        schema_version: 2,
        stage: 'triage',
        source: { kind: 'opportunity_fit', contract_version: 'opportunity_fit.v2', snapshot_version: '1' },
        summary: { text: 'Triage summary', rationale: 'evidence', evidence_refs: [] },
        conditions: [],
        risks: [],
        questions: [],
        next_steps: [],
      },
    });
    opportunityFitState.confirmTriage.mockRejectedValue({
      response: { data: { error_code: 'opportunity_fit_triage_confirmation_consumed' } },
    });
    opportunityFitState.getReview.mockResolvedValue({
      stages: [{
        stage_id: 101,
        review_id: 201,
        resume_id: 11,
        jd_version_id: 1,
        stage: 'triage',
        schema_version: 2,
        stage_status: 'confirmed',
        parent_triage_stage_id: null,
        idempotency_key: 'pilot-triage-key',
        source_fingerprint_sha256: 'frozen-source',
        confirmation_token: 'triage-confirmation-token',
        proposal: {
          schema_version: 2,
          stage: 'triage',
          source: { kind: 'opportunity_fit', contract_version: 'opportunity_fit.v2', snapshot_version: '1' },
          summary: { text: 'Confirmed summary', rationale: 'evidence', evidence_refs: [] },
          conditions: [],
          risks: [],
          questions: [],
          next_steps: [],
        },
      }],
    });
    opportunityFitState.listReviews.mockResolvedValue([]);
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

  it('recovers a consumed Pilot Triage confirmation from the mounted AppShell', async () => {
    await act(async () => root?.render(<AppShell />));
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="open-application-detail"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="open-opportunity-fit"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="nav-pilot"]')?.click());
    await flush();
    const resumeSelect = host?.querySelector<HTMLSelectElement>('select');
    if (!resumeSelect) throw new Error('Pilot resume selector was not mounted');
    resumeSelect.value = '11';
    act(() => resumeSelect.dispatchEvent(new Event('change', { bubbles: true })));
    await flush();
    const startButton = Array.from(host?.querySelectorAll<HTMLButtonElement>('button') ?? [])
      .find((button) => button.textContent?.includes('快速判断'));
    if (!startButton) throw new Error('Pilot Triage start button was not mounted');
    act(() => startButton.click());
    await flush();
    const confirmation = host?.querySelector<HTMLElement>('[role="dialog"]');
    const confirmButton = confirmation?.querySelectorAll<HTMLButtonElement>('button')[1];
    if (!confirmButton) throw new Error('Pilot Triage confirmation button was not mounted');
    act(() => confirmButton.click());
    await flush();
    expect(opportunityFitState.createTriage).toHaveBeenCalledTimes(1);
    const triageButton = Array.from(host?.querySelectorAll<HTMLButtonElement>('button') ?? [])
      .find((button) => button.textContent?.includes('快速判断'));
    if (!triageButton) throw new Error('Pilot Triage confirmation button was not mounted');
    act(() => triageButton.click());
    await flush();
    await flush();
    expect(opportunityFitState.confirmTriage).toHaveBeenCalledTimes(1);
    expect(opportunityFitState.getReview).toHaveBeenCalledWith(7, 201);
    const restartButton = host?.querySelector<HTMLButtonElement>('[aria-label="重新开始岗位评估"]');
    if (!restartButton) throw new Error('Pilot restart button was not mounted');
    act(() => restartButton.click());
    await flush();
    expect(host?.querySelector<HTMLTextAreaElement>('textarea')?.value).toBe('JD text');
    expect(host?.querySelector<HTMLSelectElement>('select')?.value).toBe('');
  });

  it('keeps the original confirmation attempt after repeated unknown retries', async () => {
    opportunityFitState.confirmTriage.mockRejectedValue({
      response: { status: 503, data: { error_code: 'opportunity_fit_provider_error' } },
    });
    opportunityFitState.getReview.mockRejectedValue(new Error('temporary status read failure'));

    await act(async () => root?.render(<AppShell />));
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="open-application-detail"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="open-opportunity-fit"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="nav-pilot"]')?.click());
    await flush();
    const resumeSelect = host?.querySelector<HTMLSelectElement>('select');
    if (!resumeSelect) throw new Error('Pilot resume selector was not mounted');
    resumeSelect.value = '11';
    act(() => resumeSelect.dispatchEvent(new Event('change', { bubbles: true })));
    await flush();

    const startButton = Array.from(host?.querySelectorAll<HTMLButtonElement>('button') ?? [])
      .find((button) => button.textContent?.includes('快速判断'));
    if (!startButton) throw new Error('Pilot Triage start button was not mounted');
    act(() => startButton.click());
    await flush();
    const confirmation = host?.querySelector<HTMLElement>('[role="dialog"]');
    const confirmButton = confirmation?.querySelectorAll<HTMLButtonElement>('button')[1];
    if (!confirmButton) throw new Error('Pilot Triage confirmation button was not mounted');
    act(() => confirmButton.click());
    await flush();

    const firstRetry = Array.from(host?.querySelectorAll<HTMLButtonElement>('button') ?? [])
      .find((button) => button.textContent?.includes('快速判断'));
    if (!firstRetry) throw new Error('First Triage retry button was not mounted');
    act(() => firstRetry.click());
    await flush();
    const secondRetry = Array.from(host?.querySelectorAll<HTMLButtonElement>('button') ?? [])
      .find((button) => button.textContent?.includes('快速判断'));
    if (!secondRetry) throw new Error('Second Triage retry button was not mounted');
    act(() => secondRetry.click());
    await flush();

    expect(opportunityFitState.confirmTriage).toHaveBeenCalledTimes(2);
    expect(opportunityFitState.confirmTriage.mock.calls[0]).toEqual(opportunityFitState.confirmTriage.mock.calls[1]);
    expect(host?.textContent).toContain('Triage summary');
    expect(host?.querySelector<HTMLSelectElement>('select')?.disabled).toBe(true);
  });

  it('disables restarting while a Deep Review request is in flight', async () => {
    let resolveDeep: ((value: unknown) => void) | undefined;
    opportunityFitState.confirmTriage.mockResolvedValue({
      stage_id: 101,
      review_id: 201,
      resume_id: 11,
      jd_version_id: 1,
      stage: 'triage',
      schema_version: 2,
      stage_status: 'confirmed',
      parent_triage_stage_id: null,
      idempotency_key: 'pilot-triage-key',
      source_fingerprint_sha256: 'frozen-source',
      confirmation_token: null,
      proposal: { summary: { text: 'Confirmed summary', rationale: 'evidence', evidence_refs: [] }, conditions: [], risks: [], questions: [], next_steps: [] },
    });
    opportunityFitState.createDeep.mockImplementation(() => new Promise((resolve) => {
      resolveDeep = resolve;
    }));

    await act(async () => root?.render(<AppShell />));
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="open-application-detail"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="open-opportunity-fit"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="nav-pilot"]')?.click());
    await flush();
    const resumeSelect = host?.querySelector<HTMLSelectElement>('select');
    if (!resumeSelect) throw new Error('Pilot resume selector was not mounted');
    resumeSelect.value = '11';
    act(() => resumeSelect.dispatchEvent(new Event('change', { bubbles: true })));
    await flush();

    const startButton = Array.from(host?.querySelectorAll<HTMLButtonElement>('button') ?? [])
      .find((button) => button.textContent?.includes('快速判断'));
    if (!startButton) throw new Error('Pilot Triage start button was not mounted');
    act(() => startButton.click());
    await flush();
    const triageConfirmation = host?.querySelector<HTMLElement>('[role="dialog"]');
    const triageConfirmButton = triageConfirmation?.querySelectorAll<HTMLButtonElement>('button')[1];
    if (!triageConfirmButton) throw new Error('Pilot Triage confirmation button was not mounted');
    act(() => triageConfirmButton.click());
    await flush();
    const confirmButton = Array.from(host?.querySelectorAll<HTMLButtonElement>('button') ?? [])
      .find((button) => button.textContent?.includes('确认快速判断'));
    if (!confirmButton) throw new Error('Pilot Triage confirm button was not mounted');
    act(() => confirmButton.click());
    await flush();

    const deepButton = Array.from(host?.querySelectorAll<HTMLButtonElement>('button') ?? [])
      .find((button) => button.textContent?.includes('深入分析'));
    if (!deepButton) throw new Error('Pilot Deep Review button was not mounted');
    act(() => deepButton.click());
    await flush();
    const deepConfirmation = host?.querySelector<HTMLElement>('[role="dialog"]');
    const deepConfirmButton = deepConfirmation?.querySelectorAll<HTMLButtonElement>('button')[1];
    if (!deepConfirmButton) throw new Error('Pilot Deep Review confirmation button was not mounted');
    act(() => deepConfirmButton.click());
    await flush();

    const restartButton = host?.querySelector<HTMLButtonElement>('[aria-label="重新开始岗位评估"]');
    if (!restartButton) throw new Error('Pilot restart button was not mounted');
    expect(restartButton.disabled).toBe(true);
    resolveDeep?.({
      stage_id: 102,
      review_id: 201,
      resume_id: 11,
      jd_version_id: 1,
      stage: 'deep_review',
      schema_version: 2,
      stage_status: 'ready',
      parent_triage_stage_id: 101,
      idempotency_key: 'pilot-deep-key',
      source_fingerprint_sha256: 'frozen-source',
      confirmation_token: null,
      proposal: null,
    });
    await flush();
  });

  it('disables Pilot history entries while a Triage request is in flight', async () => {
    let resolveTriage: ((value: unknown) => void) | undefined;
    opportunityFitState.createTriage.mockImplementation(() => new Promise((resolve) => {
      resolveTriage = resolve;
    }));

    await act(async () => root?.render(<AppShell />));
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="open-application-detail"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="open-opportunity-fit"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="nav-pilot"]')?.click());
    await flush();

    const resumeSelect = host?.querySelector<HTMLSelectElement>('select');
    if (!resumeSelect) throw new Error('Pilot resume selector was not mounted');
    resumeSelect.value = '11';
    act(() => resumeSelect.dispatchEvent(new Event('change', { bubbles: true })));
    await flush();

    const startButton = Array.from(host?.querySelectorAll<HTMLButtonElement>('button') ?? [])
      .find((button) => button.textContent?.includes('快速判断'));
    if (!startButton) throw new Error('Pilot Triage start button was not mounted');
    act(() => startButton.click());
    await flush();
    const confirmation = host?.querySelector<HTMLElement>('[role="dialog"]');
    const confirmButton = confirmation?.querySelectorAll<HTMLButtonElement>('button')[1];
    if (!confirmButton) throw new Error('Pilot Triage confirmation button was not mounted');
    act(() => confirmButton.click());
    await flush();
    expect(opportunityFitState.createTriage).toHaveBeenCalledTimes(1);

    const historyButtons = Array.from(host?.querySelectorAll<HTMLButtonElement>('button') ?? [])
      .filter((button) => button.textContent?.includes('\u67e5\u770b'));
    expect(historyButtons.length).toBeGreaterThanOrEqual(2);
    historyButtons.forEach((button) => {
      expect(button.disabled).toBe(true);
      act(() => button.click());
    });
    expect(opportunityFitState.getReview).not.toHaveBeenCalled();

    resolveTriage?.({
      stage_id: 301,
      review_id: 201,
      resume_id: 11,
      jd_version_id: 1,
      stage: 'triage',
      schema_version: 2,
      stage_status: 'ready',
      parent_triage_stage_id: null,
      idempotency_key: 'pilot-triage-key',
      source_fingerprint_sha256: 'frozen-source',
      confirmation_token: 'triage-confirmation-token',
      proposal: { summary: { text: 'Triage summary', rationale: 'evidence', evidence_refs: [] }, conditions: [], risks: [], questions: [], next_steps: [] },
    });
    await flush();
  });

  it('drops a late request from application A after switching to application B', async () => {
    let resolveDeep: ((value: unknown) => void) | undefined;
    opportunityFitState.confirmTriage.mockResolvedValue({
      stage_id: 101,
      review_id: 201,
      resume_id: 11,
      jd_version_id: 1,
      stage: 'triage',
      schema_version: 2,
      stage_status: 'confirmed',
      parent_triage_stage_id: null,
      idempotency_key: 'pilot-triage-key',
      source_fingerprint_sha256: 'frozen-source',
      confirmation_token: null,
      proposal: { summary: { text: 'Confirmed summary', rationale: 'evidence', evidence_refs: [] }, conditions: [], risks: [], questions: [], next_steps: [] },
    });
    opportunityFitState.createDeep.mockImplementation(() => new Promise((resolve) => {
      resolveDeep = resolve;
    }));

    await act(async () => root?.render(<AppShell />));
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="open-application-detail"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="open-opportunity-fit"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="nav-pilot"]')?.click());
    await flush();
    const resumeSelect = host?.querySelector<HTMLSelectElement>('select');
    if (!resumeSelect) throw new Error('Pilot resume selector was not mounted');
    resumeSelect.value = '11';
    act(() => resumeSelect.dispatchEvent(new Event('change', { bubbles: true })));
    await flush();
    const startButton = Array.from(host?.querySelectorAll<HTMLButtonElement>('button') ?? [])
      .find((button) => button.textContent?.includes('快速判断'));
    if (!startButton) throw new Error('Pilot Triage start button was not mounted');
    act(() => startButton.click());
    await flush();
    const triageConfirmation = host?.querySelector<HTMLElement>('[role="dialog"]');
    const triageConfirmButton = triageConfirmation?.querySelectorAll<HTMLButtonElement>('button')[1];
    if (!triageConfirmButton) throw new Error('Pilot Triage confirmation button was not mounted');
    act(() => triageConfirmButton.click());
    await flush();
    const triageCommitButton = Array.from(host?.querySelectorAll<HTMLButtonElement>('button') ?? [])
      .find((button) => button.textContent?.includes('确认快速判断'));
    if (!triageCommitButton) throw new Error('Pilot Triage confirm button was not mounted');
    act(() => triageCommitButton.click());
    await flush();

    const deepButton = Array.from(host?.querySelectorAll<HTMLButtonElement>('button') ?? [])
      .find((button) => button.textContent?.includes('深入分析'));
    if (!deepButton) throw new Error('Pilot Deep Review button was not mounted');
    act(() => deepButton.click());
    await flush();
    const deepConfirmation = host?.querySelector<HTMLElement>('[role="dialog"]');
    const deepConfirmButton = deepConfirmation?.querySelectorAll<HTMLButtonElement>('button')[1];
    if (!deepConfirmButton) throw new Error('Pilot Deep Review confirmation button was not mounted');
    act(() => deepConfirmButton.click());
    await flush();
    expect(opportunityFitState.createDeep).toHaveBeenCalledTimes(1);

    const cancelButton = Array.from(host?.querySelectorAll<HTMLButtonElement>('button') ?? [])
      .find((button) => button.textContent?.includes('取消流程'));
    if (!cancelButton) throw new Error('Pilot cancel button was not mounted');
    act(() => cancelButton.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="open-application-detail-b"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="open-opportunity-fit"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="nav-pilot"]')?.click());
    await flush();

    resolveDeep?.({
      stage_id: 102,
      review_id: 201,
      resume_id: 11,
      jd_version_id: 1,
      stage: 'deep_review',
      schema_version: 2,
      stage_status: 'ready',
      parent_triage_stage_id: 101,
      idempotency_key: 'pilot-deep-key',
      source_fingerprint_sha256: 'frozen-source',
      confirmation_token: null,
      proposal: { summary: { text: 'A late deep result', rationale: 'evidence', evidence_refs: [] }, conditions: [], risks: [], questions: [], next_steps: [] },
    });
    await flush();

    expect(host?.textContent).not.toContain('A late deep result');
    expect(host?.querySelector('select')).not.toBeNull();
  });

  it('drops a late Pilot history response after starting a new review', async () => {
    let resolveHistory: ((value: unknown) => void) | undefined;
    opportunityFitState.confirmTriage.mockResolvedValue({
      stage_id: 101,
      review_id: 201,
      resume_id: 11,
      jd_version_id: 1,
      stage: 'triage',
      schema_version: 2,
      stage_status: 'confirmed',
      parent_triage_stage_id: null,
      idempotency_key: 'pilot-triage-key',
      source_fingerprint_sha256: 'frozen-source',
      confirmation_token: null,
      proposal: { summary: { text: 'Current triage', rationale: 'evidence', evidence_refs: [] }, conditions: [], risks: [], questions: [], next_steps: [] },
    });
    opportunityFitState.listReviews.mockResolvedValue([{ review_id: 202, stage_count: 1 }]);
    opportunityFitState.getReview.mockImplementation(() => new Promise((resolve) => {
      resolveHistory = resolve;
    }));

    await act(async () => root?.render(<AppShell />));
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="open-application-detail"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="open-opportunity-fit"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="nav-pilot"]')?.click());
    await flush();
    const resumeSelect = host?.querySelector<HTMLSelectElement>('select');
    if (!resumeSelect) throw new Error('Pilot resume selector was not mounted');
    resumeSelect.value = '11';
    act(() => resumeSelect.dispatchEvent(new Event('change', { bubbles: true })));
    await flush();
    const startButton = Array.from(host?.querySelectorAll<HTMLButtonElement>('button') ?? [])
      .find((button) => button.textContent?.includes('快速判断'));
    if (!startButton) throw new Error('Pilot Triage start button was not mounted');
    act(() => startButton.click());
    await flush();
    const triageConfirmation = host?.querySelector<HTMLElement>('[role="dialog"]');
    const triageConfirmButton = triageConfirmation?.querySelectorAll<HTMLButtonElement>('button')[1];
    if (!triageConfirmButton) throw new Error('Pilot Triage confirmation button was not mounted');
    act(() => triageConfirmButton.click());
    await flush();
    const triageCommitButton = Array.from(host?.querySelectorAll<HTMLButtonElement>('button') ?? [])
      .find((button) => button.textContent?.includes('确认快速判断'));
    if (!triageCommitButton) throw new Error('Pilot Triage confirm button was not mounted');
    act(() => triageCommitButton.click());
    await flush();

    const historyButton = Array.from(host?.querySelectorAll<HTMLButtonElement>('button') ?? [])
      .find((button) => button.textContent === '查看');
    if (!historyButton) throw new Error('Pilot history button was not mounted');
    act(() => historyButton.click());
    await flush();
    const restartButton = host?.querySelector<HTMLButtonElement>('[aria-label="重新开始岗位评估"]');
    if (!restartButton) throw new Error('Pilot restart button was not mounted');
    act(() => restartButton.click());
    await flush();

    resolveHistory?.({
      stages: [{
        stage_id: 2021,
        review_id: 202,
        resume_id: 11,
        jd_version_id: 1,
        stage: 'triage',
        schema_version: 2,
        stage_status: 'ready',
        parent_triage_stage_id: null,
        idempotency_key: 'history-key',
        source_fingerprint_sha256: 'history-frozen-source',
        confirmation_token: null,
        proposal: { summary: { text: 'Late historical result', rationale: 'evidence', evidence_refs: [] }, conditions: [], risks: [], questions: [], next_steps: [] },
      }],
    });
    await flush();

    expect(host?.textContent).not.toContain('Late historical result');
    expect(host?.querySelector('select')).not.toBeNull();
  });

  it('keeps the latest Pilot history selection when an earlier response arrives late', async () => {
    const resolveHistory = new Map<number, (value: unknown) => void>();
    opportunityFitState.listReviews.mockResolvedValue([
      { review_id: 201, stage_count: 1 },
      { review_id: 202, stage_count: 1 },
    ]);
    opportunityFitState.getReview.mockImplementation((_applicationId: number, reviewId: number) => (
      new Promise((resolve) => { resolveHistory.set(reviewId, resolve); })
    ));

    await act(async () => root?.render(<AppShell />));
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="open-application-detail"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="open-opportunity-fit"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="nav-pilot"]')?.click());
    await flush();

    const historyButtons = Array.from(host?.querySelectorAll<HTMLButtonElement>('button') ?? [])
      .filter((button) => button.textContent === '查看');
    expect(historyButtons).toHaveLength(2);
    await act(async () => {
      historyButtons[0].click();
      historyButtons[1].click();
      await Promise.resolve();
    });
    expect(opportunityFitState.getReview).toHaveBeenCalledTimes(2);

    resolveHistory.get(202)?.({
      stages: [{
        stage_id: 2021,
        review_id: 202,
        resume_id: 11,
        jd_version_id: 1,
        stage: 'triage',
        schema_version: 2,
        stage_status: 'ready',
        parent_triage_stage_id: null,
        idempotency_key: 'history-b-key',
        source_fingerprint_sha256: 'history-b-source',
        confirmation_token: null,
        proposal: { summary: { text: 'History B', rationale: 'evidence', evidence_refs: [] }, conditions: [], risks: [], questions: [], next_steps: [] },
      }],
    });
    await flush();
    expect(host?.textContent).toContain('History B');

    resolveHistory.get(201)?.({
      stages: [{
        stage_id: 2011,
        review_id: 201,
        resume_id: 11,
        jd_version_id: 1,
        stage: 'triage',
        schema_version: 2,
        stage_status: 'ready',
        parent_triage_stage_id: null,
        idempotency_key: 'history-a-key',
        source_fingerprint_sha256: 'history-a-source',
        confirmation_token: null,
        proposal: { summary: { text: 'History A late', rationale: 'evidence', evidence_refs: [] }, conditions: [], risks: [], questions: [], next_steps: [] },
      }],
    });
    await flush();

    expect(host?.textContent).toContain('History B');
    expect(host?.textContent).not.toContain('History A late');
  });

  it('clears the legacy detail when a v2 history selection succeeds', async () => {
    opportunityFitState.legacyHistory = [{ id: 91 }];
    opportunityFitState.getLegacyReview.mockResolvedValue({
      summary: { text: 'Legacy history detail' },
      recommendation: 'advance',
    });
    opportunityFitState.getReview.mockResolvedValue({
      stages: [{
        stage_id: 201,
        review_id: 201,
        resume_id: 11,
        jd_version_id: 1,
        stage: 'triage',
        schema_version: 2,
        stage_status: 'ready',
        parent_triage_stage_id: null,
        idempotency_key: 'v2-history-key',
        source_fingerprint_sha256: 'v2-history-source',
        confirmation_token: null,
        proposal: { summary: { text: 'V2 history detail', rationale: 'evidence', evidence_refs: [] }, conditions: [], risks: [], questions: [], next_steps: [] },
      }],
    });

    await act(async () => root?.render(<AppShell />));
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="open-application-detail"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="open-opportunity-fit"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="nav-pilot"]')?.click());
    await flush();

    const historyButtons = Array.from(host?.querySelectorAll<HTMLButtonElement>('button') ?? [])
      .filter((button) => button.textContent === '\u67e5\u770b');
    expect(historyButtons.length).toBeGreaterThanOrEqual(3);
    act(() => historyButtons[0].click());
    await flush();
    expect(host?.textContent).toContain('Legacy history detail');

    const refreshedHistoryButtons = Array.from(host?.querySelectorAll<HTMLButtonElement>('button') ?? [])
      .filter((button) => button.textContent === '\u67e5\u770b');
    act(() => refreshedHistoryButtons[1].click());
    await flush();
    expect(host?.textContent).toContain('V2 history detail');
    expect(host?.textContent).not.toContain('Legacy history detail');
  });

  it('restores a persisted Pilot Triage source conflict after a JD race', async () => {
    opportunityFitState.createTriage.mockRejectedValue({
      response: { status: 409, data: { error_code: 'application_jd_source_conflict' } },
    });
    opportunityFitState.sourceConflict.mockResolvedValue({ status: 'found', stage: {
      stage_id: 101,
      review_id: 201,
      resume_id: 11,
      jd_version_id: 1,
      stage: 'triage',
      schema_version: 2,
      stage_status: 'source_conflict',
      parent_triage_stage_id: null,
      idempotency_key: 'pilot-triage-key',
      source_fingerprint_sha256: 'frozen-source',
      confirmation_token: null,
      proposal: null,
    }});

    await act(async () => root?.render(<AppShell />));
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="open-application-detail"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="open-opportunity-fit"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="nav-pilot"]')?.click());
    await flush();
    const resumeSelect = host?.querySelector<HTMLSelectElement>('select');
    if (!resumeSelect) throw new Error('Pilot resume selector was not mounted');
    resumeSelect.value = '11';
    act(() => resumeSelect.dispatchEvent(new Event('change', { bubbles: true })));
    await flush();
    const startButton = Array.from(host?.querySelectorAll<HTMLButtonElement>('button') ?? [])
      .find((button) => button.textContent?.includes('快速判断'));
    if (!startButton) throw new Error('Pilot Triage start button was not mounted');
    act(() => startButton.click());
    await flush();
    const confirmation = host?.querySelector<HTMLElement>('[role="dialog"]');
    const confirmButton = confirmation?.querySelectorAll<HTMLButtonElement>('button')[1];
    if (!confirmButton) throw new Error('Pilot Triage confirmation button was not mounted');
    act(() => confirmButton.click());
    await flush();
    await flush();

    expect(opportunityFitState.sourceConflict).toHaveBeenCalledWith(7, 'triage', expect.any(String), undefined);
    expect(opportunityFitState.sourceConflict.mock.calls[0][2]).toBe(
      opportunityFitState.createTriage.mock.calls[0][1].idempotency_key,
    );
    expect(host?.textContent).toContain('岗位资料版本已变化');
    expect(host?.querySelector('[aria-label="重新开始岗位评估"]')).not.toBeNull();
  });

  it('cleans up the Pilot context when source-conflict recovery reports a missing review', async () => {
    opportunityFitState.createTriage.mockRejectedValue({
      response: { status: 409, data: { error_code: 'application_jd_source_conflict' } },
    });
    opportunityFitState.sourceConflict.mockResolvedValue({ status: 'application_missing' });

    await act(async () => root?.render(<AppShell />));
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="open-application-detail"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="open-opportunity-fit"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="nav-pilot"]')?.click());
    await flush();
    const resumeSelect = host?.querySelector<HTMLSelectElement>('select');
    if (!resumeSelect) throw new Error('Pilot resume selector was not mounted');
    resumeSelect.value = '11';
    act(() => resumeSelect.dispatchEvent(new Event('change', { bubbles: true })));
    await flush();
    const startButton = Array.from(host?.querySelectorAll<HTMLButtonElement>('button') ?? [])
      .find((button) => button.textContent?.includes('快速判断'));
    if (!startButton) throw new Error('Pilot Triage start button was not mounted');
    act(() => startButton.click());
    await flush();
    const confirmation = host?.querySelector<HTMLElement>('[role="dialog"]');
    const confirmButton = confirmation?.querySelectorAll<HTMLButtonElement>('button')[1];
    if (!confirmButton) throw new Error('Pilot Triage confirmation button was not mounted');
    act(() => confirmButton.click());
    await flush();
    await flush();

    expect(host?.querySelector('[data-testid="dashboard-harness"]')).not.toBeNull();
    expect(host?.querySelector('[data-testid="application-detail-harness"]')).toBeNull();
  });

  it('drops a late Pilot history response when a new Triage starts', async () => {
    let resolveHistory: ((value: unknown) => void) | undefined;
    opportunityFitState.getReview.mockImplementationOnce(() => new Promise((resolve) => { resolveHistory = resolve; }));
    opportunityFitState.createTriage.mockResolvedValue({
      stage_id: 301,
      review_id: 301,
      resume_id: 11,
      jd_version_id: 1,
      stage: 'triage',
      schema_version: 2,
      stage_status: 'ready',
      parent_triage_stage_id: null,
      idempotency_key: 'current-triage-key',
      source_fingerprint_sha256: 'current-source',
      confirmation_token: null,
      proposal: { summary: { text: 'Current Triage', rationale: 'evidence', evidence_refs: [] }, conditions: [], risks: [], questions: [], next_steps: [] },
    });

    await act(async () => root?.render(<AppShell />));
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="open-application-detail"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="open-opportunity-fit"]')?.click());
    await flush();
    act(() => host?.querySelector<HTMLButtonElement>('[data-testid="nav-pilot"]')?.click());
    await flush();
    const historyButton = Array.from(host?.querySelectorAll<HTMLButtonElement>('button') ?? [])
      .find((button) => button.textContent === '查看');
    if (!historyButton) throw new Error('Pilot history button was not mounted');
    act(() => historyButton.click());
    await flush();
    const resumeSelect = host?.querySelector<HTMLSelectElement>('select');
    if (!resumeSelect) throw new Error('Pilot resume selector was not mounted');
    resumeSelect.value = '11';
    act(() => resumeSelect.dispatchEvent(new Event('change', { bubbles: true })));
    await flush();
    const triageButton = Array.from(host?.querySelectorAll<HTMLButtonElement>('button') ?? [])
      .find((button) => button.textContent?.includes('快速判断') && !button.textContent?.includes('确认'));
    if (!triageButton) throw new Error('Pilot Triage button was not mounted');
    act(() => triageButton.click());
    await flush();
    const confirmation = host?.querySelector<HTMLElement>('[role="dialog"]');
    const confirmButton = confirmation?.querySelectorAll<HTMLButtonElement>('button')[1];
    if (!confirmButton) throw new Error('Pilot Triage confirmation button was not mounted');
    act(() => confirmButton.click());
    await flush();
    await flush();

    resolveHistory?.({
      stages: [{
        stage_id: 901,
        review_id: 202,
        resume_id: 11,
        jd_version_id: 1,
        stage: 'triage',
        schema_version: 2,
        stage_status: 'ready',
        parent_triage_stage_id: null,
        idempotency_key: 'stale-history-key',
        source_fingerprint_sha256: 'stale-history-source',
        confirmation_token: null,
        proposal: { summary: { text: 'Stale history after Triage', rationale: 'evidence', evidence_refs: [] }, conditions: [], risks: [], questions: [], next_steps: [] },
      }],
    });
    await flush();

    expect(host?.textContent).toContain('Current Triage');
    expect(host?.textContent).not.toContain('Stale history after Triage');
  });
});

describe('AppShell Offer negotiation draft isolation', () => {
  beforeEach(() => {
    window.history.replaceState(null, '', '/');
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
    expect(overlay?.querySelector('[data-testid="offer-negotiation-drawer-harness"]')).not.toBeNull();
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
