// @vitest-environment jsdom
import { act, useState } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import OfferNegotiationDrawer, { type OfferNegotiationDraft } from './OfferNegotiationDrawer';
import type { Offer, OfferNegotiationProposal, OfferNegotiationPreview } from '@/types/offer';
import { OfferNegotiationError } from '@/services/offers';

const service = vi.hoisted(() => ({
  create: vi.fn(),
  list: vi.fn(async (): Promise<any[]> => []),
  dimensions: vi.fn(async (): Promise<any[]> => []),
  values: vi.fn(async () => []),
  confirm: vi.fn(),
  preview: vi.fn(),
  get: vi.fn(),
}));

vi.mock('@/services/offers', () => ({
  createOfferNegotiationProposal: service.create,
  listOfferNegotiationProposals: service.list,
  listOfferComparisonDimensions: service.dimensions,
  listOfferComparisonValues: service.values,
  confirmOfferNegotiationProposal: service.confirm,
  previewOfferNegotiation: service.preview,
  getOfferNegotiationProposal: service.get,
  OfferNegotiationError: class OfferNegotiationError extends Error {
    constructor(public status: number, public code: string | null) { super(code ?? 'error'); }
  },
}));

const offer: Offer = {
  id: 7, application_id: 42, company_name: 'Company', position_name: 'Engineer', status: 'pending',
  base_monthly: 28000, months_per_year: 12, signing_bonus: 0, equity: '', perks: '',
  deadline: '', notes: '', assessment: '', total_cash: 336000,
  created_at: '2026-08-01T00:00:00Z', updated_at: '2026-08-01T00:00:00Z',
};

const proposal = (): OfferNegotiationProposal => ({
  id: 3, offer_id: 7, application_id: 42, attempt_status: 'ready', proposal_status: 'normal',
  source_fingerprint: 'fingerprint', source_changed: false, source_states: { offer: 'current' }, proposal_hash: 'hash',
  proposal: {
    proposal_status: 'normal',
    communication_goals: [{ id: 'goal-1', text: 'Goal', rationale: 'Offer', evidence_refs: [{ source: 'offer_snapshot', path: '/offer_snapshot/company_name', excerpt: 'Company' }] }],
    clarification_questions: [], talking_points: [], preparation_checks: [],
  },
  input_snapshot: {
    snapshot_version: 1,
    offer_snapshot: {
      company_name: 'Company', position_name: 'Engineer', status: 'pending',
      base_monthly: 28000, months_per_year: 12, signing_bonus: 0,
      equity: null, perks: null, deadline: null, notes: null,
      dimensions: [{ path_id: 'dimension_001', label: '通勤', value_text: '地铁 35 分钟' }],
    },
    user_brief: { goal: 'Goal', concerns: 'Concern', scenario: 'Call' },
  },
});

const preview = (): OfferNegotiationPreview => ({
  source_fingerprint: 'fingerprint',
  snapshot: proposal().input_snapshot,
});

function changeValue(control: HTMLInputElement | HTMLTextAreaElement, value: string) {
  const prototype = control instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(prototype, 'value')?.set?.call(control, value);
  control.dispatchEvent(new Event('input', { bubbles: true }));
  control.dispatchEvent(new Event('change', { bubbles: true }));
}

describe('OfferNegotiationDrawer', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  beforeEach(() => {
    service.create.mockReset();
    service.confirm.mockReset();
    service.preview.mockReset();
    service.list.mockClear();
    service.dimensions.mockClear();
    service.values.mockClear();
    service.get.mockClear();
    service.preview.mockResolvedValue(preview());
    service.list.mockResolvedValue([]);
    window.matchMedia = () => ({
      matches: false,
      addListener: () => undefined,
      removeListener: () => undefined,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    }) as unknown as MediaQueryList;
    vi.stubGlobal('confirm', vi.fn(() => true));
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
  });

  afterEach(() => {
    act(() => root?.unmount());
    host?.remove();
    vi.unstubAllGlobals();
  });

  it('requires the three user brief fields before generation', async () => {
    await act(async () => { root?.render(<OfferNegotiationDrawer open offer={offer} onClose={vi.fn()} />); });
    const button = host?.querySelector<HTMLButtonElement>('[data-testid="offer-negotiation-generate"]');
    expect(button?.disabled).toBe(true);
    expect(service.create).not.toHaveBeenCalled();
  });

  it('shows the first actionable step before the Offer facts', async () => {
    await act(async () => { root?.render(<OfferNegotiationDrawer open offer={offer} onClose={vi.fn()} />); });

    const nextStep = host?.querySelector('[data-testid="offer-negotiation-next-step"]');
    const facts = host?.querySelector('[data-testid="offer-negotiation-input-facts"]');
    const goal = host?.querySelector<HTMLInputElement>('#negotiation-goal');
    const concerns = host?.querySelector<HTMLTextAreaElement>('#negotiation-concerns');
    const scenario = host?.querySelector<HTMLInputElement>('#negotiation-scenario');
    const button = host?.querySelector<HTMLButtonElement>('[data-testid="offer-negotiation-generate"]');

    expect(nextStep?.textContent).toContain('第 1 步，共 3 步');
    expect(nextStep?.textContent).toContain('先填写这次谈薪的目标');
    expect(nextStep?.compareDocumentPosition(facts as Node) ?? 0).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(goal?.placeholder).toContain('固定月薪');
    expect(concerns?.placeholder).toContain('试用期');
    expect(scenario?.placeholder).toContain('HR 电话');
    expect(button?.textContent).toContain('下一步：检查输入');
  });

  it('hands the current Offer and editable brief to Pilot without starting any service call', async () => {
    const onOpenPilotChat = vi.fn();
    await act(async () => {
      root?.render(
        <OfferNegotiationDrawer
          open
          offer={offer}
          onClose={vi.fn()}
          onOpenPilotChat={onOpenPilotChat}
        />,
      );
    });
    const inputs = host?.querySelectorAll('input') ?? [];
    const textareas = host?.querySelectorAll('textarea') ?? [];
    await act(async () => {
      changeValue(inputs[0] as HTMLInputElement, '希望固定月薪多 2K');
      changeValue(textareas[0] as HTMLTextAreaElement, '担心对方取消 Offer');
      changeValue(inputs[1] as HTMLInputElement, 'HR 电话沟通');
    });
    service.preview.mockClear();
    service.create.mockClear();
    service.confirm.mockClear();

    const handoff = host?.querySelector<HTMLButtonElement>('[data-testid="offer-negotiation-open-pilot"]');
    expect(handoff?.textContent).toContain('和 Pilot 深聊这份 Offer');
    expect(host?.textContent).toContain('消息由你决定是否发送');
    await act(async () => handoff?.click());

    expect(onOpenPilotChat).toHaveBeenCalledTimes(1);
    expect(onOpenPilotChat).toHaveBeenCalledWith(offer, {
      goal: '希望固定月薪多 2K',
      concerns: '担心对方取消 Offer',
      scenario: 'HR 电话沟通',
    });
    expect(service.preview).not.toHaveBeenCalled();
    expect(service.create).not.toHaveBeenCalled();
    expect(service.confirm).not.toHaveBeenCalled();
  });

  it('does not offer a second Pilot handoff when the task already came from Pilot', async () => {
    await act(async () => {
      root?.render(
        <OfferNegotiationDrawer
          open
          offer={offer}
          entrypoint="pilot"
          onClose={vi.fn()}
          onOpenPilotChat={vi.fn()}
        />,
      );
    });

    expect(host?.querySelector('[data-testid="offer-negotiation-open-pilot"]')).toBeNull();
  });

  it('rejects a directly supplied historical unbound Offer before any negotiation read or write', async () => {
    await act(async () => {
      root?.render(<OfferNegotiationDrawer open offer={{ ...offer, application_id: undefined }} onClose={vi.fn()} />);
    });

    expect(host?.textContent).toContain('历史未绑定 Offer 仅支持只读查看');
    expect(host?.querySelector('[data-testid="offer-negotiation-generate"]')).toBeNull();
    expect(service.list).not.toHaveBeenCalled();
    expect(service.dimensions).not.toHaveBeenCalled();
    expect(service.values).not.toHaveBeenCalled();
    expect(service.preview).not.toHaveBeenCalled();
    expect(service.create).not.toHaveBeenCalled();
    expect(service.confirm).not.toHaveBeenCalled();
  });

  it('does not loop when the parent stores each draft update', async () => {
    let renderCount = 0;
    function Wrapper() {
      const [draft, setDraft] = useState<OfferNegotiationDraft | null>(null);
      renderCount += 1;
      return <OfferNegotiationDrawer open offer={offer} draft={draft ?? undefined} onClose={vi.fn()} onDraftChange={setDraft} />;
    }
    await act(async () => { root?.render(<Wrapper />); });
    expect(renderCount).toBeLessThan(6);
  });

  it('restores a valid preview draft to the source confirmation step', async () => {
    const restoredDraft: OfferNegotiationDraft = {
      attemptKey: 'attempt-key',
      confirmationKey: 'confirmation-key',
      goal: 'Goal',
      concerns: 'Concern',
      scenario: 'Call',
      resultUnknown: false,
      pendingOperation: null,
      proposalId: null,
      selectedBlocks: [],
      edits: {},
      dimensionIds: [],
      sourceFingerprint: 'fingerprint',
      previewSnapshot: preview().snapshot,
      previewInputKey: JSON.stringify({ dimension_ids: [], goal: 'Goal', concerns: 'Concern', scenario: 'Call' }),
    };
    await act(async () => {
      root?.render(<OfferNegotiationDrawer open offer={offer} draft={restoredDraft} onClose={vi.fn()} />);
    });

    expect(host?.querySelector('[data-action="confirm-generate"]')).not.toBeNull();
  });

  it('requires a non-blank concerns field before generation', async () => {
    await act(async () => { root?.render(<OfferNegotiationDrawer open offer={offer} onClose={vi.fn()} />); });
    const inputs = host?.querySelectorAll('input') ?? [];
    const textareas = host?.querySelectorAll('textarea') ?? [];
    await act(async () => {
      changeValue(inputs[0] as HTMLInputElement, 'Goal');
      changeValue(textareas[0] as HTMLTextAreaElement, ' \t');
      changeValue(inputs[1] as HTMLInputElement, 'Call');
    });
    expect(host?.querySelector<HTMLButtonElement>('[data-testid="offer-negotiation-generate"]')?.disabled).toBe(true);
    expect(service.create).not.toHaveBeenCalled();
  });

  it('uses the frozen dimension ids when creating a proposal', async () => {
    service.create.mockResolvedValue(proposal());
    service.dimensions.mockResolvedValueOnce([
      { id: 9, label: '成长空间', archived_at: null },
      { id: 3, label: '通勤', archived_at: null },
    ]);
    await act(async () => { root?.render(<OfferNegotiationDrawer open offer={offer} dimensionIds={[9, 3]} onClose={vi.fn()} />); });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    const inputs = host?.querySelectorAll('input') ?? [];
    const textareas = host?.querySelectorAll('textarea') ?? [];
    await act(async () => {
      changeValue(inputs[0] as HTMLInputElement, 'Goal');
      changeValue(textareas[0] as HTMLTextAreaElement, 'Concern');
      changeValue(inputs[1] as HTMLInputElement, 'Call');
      host?.querySelector<HTMLButtonElement>('[data-testid="offer-negotiation-generate"]')?.click();
    });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    await act(async () => { host?.querySelector<HTMLButtonElement>('[data-testid="offer-negotiation-generate"]')?.click(); });
    expect(service.create.mock.calls[0][1].dimension_ids).toEqual([3, 9]);
  });

  it('blocks generation when selected dimension facts cannot be loaded', async () => {
    service.dimensions.mockRejectedValueOnce(new Error('dimension read failed'));
    await act(async () => { root?.render(<OfferNegotiationDrawer open offer={offer} dimensionIds={[3]} onClose={vi.fn()} />); });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    const inputs = host?.querySelectorAll('input') ?? [];
    const textareas = host?.querySelectorAll('textarea') ?? [];
    await act(async () => {
      changeValue(inputs[0] as HTMLInputElement, 'Goal');
      changeValue(textareas[0] as HTMLTextAreaElement, 'Concern');
      changeValue(inputs[1] as HTMLInputElement, 'Call');
    });
    expect(host?.querySelector<HTMLButtonElement>('[data-testid="offer-negotiation-generate"]')?.disabled).toBe(true);
    expect(service.create).not.toHaveBeenCalled();
  });

  it('clears the persisted draft when preview rejects the request before an Attempt exists', async () => {
    service.preview.mockRejectedValueOnce(new OfferNegotiationError(422, 'offer_negotiation_invalid_request'));
    const onDraftChange = vi.fn();
    await act(async () => { root?.render(<OfferNegotiationDrawer open offer={offer} onClose={vi.fn()} onDraftChange={onDraftChange} />); });
    const inputs = host?.querySelectorAll('input') ?? [];
    const textareas = host?.querySelectorAll('textarea') ?? [];
    await act(async () => {
      changeValue(inputs[0] as HTMLInputElement, 'Goal');
      changeValue(textareas[0] as HTMLTextAreaElement, 'Concern');
      changeValue(inputs[1] as HTMLInputElement, 'Call');
      host?.querySelector<HTMLButtonElement>('[data-testid="offer-negotiation-generate"]')?.click();
    });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });

    expect(service.create).not.toHaveBeenCalled();
    const latestDraftUpdate = onDraftChange.mock.calls[onDraftChange.mock.calls.length - 1];
    expect(latestDraftUpdate?.[0]).toBeNull();
  });

  it('shows the complete frozen Offer facts before generation', async () => {
    await act(async () => { root?.render(<OfferNegotiationDrawer open offer={{ ...offer, equity: '期权', perks: '补充医疗', deadline: '周五', notes: '用户备注' }} onClose={vi.fn()} />); });
    const showSources = Array.from(host?.querySelectorAll('[role="button"]') ?? []).find(
      (button) => button.textContent?.includes('查看完整来源'),
    );
    await act(async () => { showSources?.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    expect(host?.querySelector('[data-testid="offer-negotiation-input-facts"]')?.textContent).toContain('期权');
    expect(host?.querySelector('[data-testid="offer-negotiation-input-facts"]')?.textContent).toContain('用户备注');
  });

  it('renders history from the proposal snapshot rather than the current Offer', async () => {
    service.list.mockResolvedValue([proposal()]);
    await act(async () => {
      root?.render(
        <OfferNegotiationDrawer
          open
          offer={{ ...offer, company_name: 'Current company', equity: 'Current equity', perks: 'Current perks', deadline: 'Current deadline', notes: 'Current notes' }}
          onClose={vi.fn()}
          onOpenPilotChat={vi.fn()}
        />,
      );
    });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    await act(async () => { host?.querySelector<HTMLButtonElement>('section[aria-label="历史谈薪准备"] button')?.click(); });
    expect(host?.querySelector('[data-testid="offer-negotiation-open-pilot"]')).toBeNull();
    const facts = host?.querySelector('[data-testid="offer-negotiation-input-facts"]')?.textContent ?? '';
    expect(host?.querySelector('h2')?.textContent).toContain('Company');
    expect(host?.querySelector('h2')?.textContent).not.toContain('Current company');
    expect(facts).toContain('Company');
    expect(facts).not.toContain('Current company');
    expect(facts).not.toContain('Current equity');
    expect(facts).not.toContain('Current perks');
    expect(facts).not.toContain('Current deadline');
    expect(facts).not.toContain('Current notes');
    expect(facts).toContain('待处理');
    expect(facts).not.toContain('pending');
    expect(facts).toContain('Goal');
    expect(facts).toContain('Concern');
    expect(facts).toContain('Call');
    expect(facts).toContain('通勤');
    expect(facts).toContain('地铁 35 分钟');
  });

  it('keeps history records read-only while switching between them', async () => {
    const confirmed = {
      ...proposal(),
      id: 1,
      brief: {
        selected_blocks: ['goal-1'],
        edited_content: {
          blocks: proposal().proposal.communication_goals,
          edits: { 'goal-1': '上一条记录的编辑内容' },
          proposal_hash: 'hash',
        },
        content_hash: 'brief-hash',
        confirmed_at: '2026-08-01T00:00:00Z',
      },
    };
    service.list.mockResolvedValue([confirmed, { ...proposal(), id: 2 }]);
    await act(async () => { root?.render(<OfferNegotiationDrawer open offer={offer} onClose={vi.fn()} />); });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });

    const historyButtons = host?.querySelectorAll('section[aria-label="历史谈薪准备"] button') ?? [];
    await act(async () => { (historyButtons[0] as HTMLButtonElement).click(); });
    expect(host?.textContent).toContain('上一条记录的编辑内容');
    await act(async () => { (historyButtons[1] as HTMLButtonElement).click(); });

    expect(host?.querySelector('[data-testid="offer-negotiation-confirm"]')).toBeNull();
    expect(host?.querySelector<HTMLInputElement>('article input[type="checkbox"]')?.disabled).toBe(true);
    expect(host?.textContent).not.toContain('上一条记录的编辑内容');
  });

  it('keeps the active unknown-result draft isolated while viewing history', async () => {
    service.list.mockResolvedValue([{ ...proposal(), id: 3 }]);
    const onDraftChange = vi.fn();
    const activeDraft: OfferNegotiationDraft = {
      attemptKey: 'active-attempt-key',
      confirmationKey: 'active-confirmation-key',
      goal: '当前目标',
      concerns: '当前顾虑',
      scenario: '当前场景',
      resultUnknown: true,
      pendingOperation: 'generate',
      proposalId: 99,
      selectedBlocks: [],
      edits: {},
      dimensionIds: [],
      sourceFingerprint: 'active-fingerprint',
      previewSnapshot: preview().snapshot,
      previewInputKey: JSON.stringify({ dimension_ids: [], goal: '当前目标', concerns: '当前顾虑', scenario: '当前场景' }),
    };
    await act(async () => {
      root?.render(<OfferNegotiationDrawer open offer={offer} draft={activeDraft} onClose={vi.fn()} onDraftChange={onDraftChange} />);
    });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    await act(async () => {
      host?.querySelector<HTMLButtonElement>('section[aria-label="历史谈薪准备"] button')?.click();
    });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });

    const latestCall = onDraftChange.mock.calls[onDraftChange.mock.calls.length - 1];
    const latestDraft = latestCall?.[0] as OfferNegotiationDraft;
    expect(Array.from(host?.querySelectorAll('button') ?? []).some((button) => button.textContent?.includes('使用原尝试重试'))).toBe(false);
    expect(latestDraft.resultUnknown).toBe(true);
    expect(latestDraft.pendingOperation).toBe('generate');
    expect(latestDraft.proposalId).toBe(99);
    expect(latestDraft.attemptKey).toBe('active-attempt-key');
  });

  it('generates an editable evidence-backed draft and confirms selected blocks', async () => {
    service.create.mockResolvedValue(proposal());
    service.confirm.mockResolvedValue({
      id: 8,
      proposal_id: 3,
      offer_id: 7,
      selected_blocks: ['goal-1'],
      edited_content: { blocks: proposal().proposal.communication_goals, edits: { 'goal-1': '用户最终编辑的表达' }, proposal_hash: 'hash' },
      content_hash: 'brief-hash',
      confirmed_at: '2026-08-01T00:00:00Z',
    });
    await act(async () => { root?.render(<OfferNegotiationDrawer open offer={offer} onClose={vi.fn()} />); });
    const inputs = host?.querySelectorAll('input') ?? [];
    const textareas = host?.querySelectorAll('textarea') ?? [];
    await act(async () => {
      changeValue(inputs[0] as HTMLInputElement, 'Goal');
      changeValue(textareas[0] as HTMLTextAreaElement, 'Concern');
      changeValue(inputs[1] as HTMLInputElement, 'Call');
    });
    await act(async () => { host?.querySelector<HTMLButtonElement>('[data-testid="offer-negotiation-generate"]')?.click(); });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    await act(async () => { host?.querySelector<HTMLButtonElement>('[data-testid="offer-negotiation-generate"]')?.click(); });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(host?.textContent).toContain('可以这样说');
    expect(host?.textContent).toContain('需要问清楚');
    expect(host?.textContent).toContain('沟通前核对');
    expect(host?.textContent).toContain('本次沟通目标');
    expect(host?.textContent).toContain('先从“本次沟通目标”开始');
    expect(host?.textContent).toContain('请选择至少一项建议后才能保存。');
    const checkbox = host?.querySelector('article input[type="checkbox"]') as HTMLInputElement;
    await act(async () => { checkbox.click(); });
    expect(host?.querySelector<HTMLButtonElement>('[data-testid="offer-negotiation-confirm"]')?.textContent).toContain('1');
    await act(async () => { host?.querySelector<HTMLButtonElement>('[data-testid="offer-negotiation-confirm"]')?.click(); });
    expect(host?.querySelector('[aria-label="确认保存谈薪准备"]')).not.toBeNull();
    await act(async () => { host?.querySelector<HTMLButtonElement>('[data-action="confirm-save"]')?.click(); });
    expect(service.confirm).toHaveBeenCalledTimes(1);
    expect(service.confirm.mock.calls[0][0]).toBe(3);
    expect(service.confirm.mock.calls[0][1].selected_blocks).toEqual(['goal-1']);
    expect(service.create.mock.calls[0][2]).toBe('ui');
    expect(host?.textContent).toContain('用户最终编辑的表达');
  });

  it('uses the product confirmation panel without changing request order', async () => {
    const nativeConfirm = window.confirm;
    service.create.mockResolvedValue(proposal());
    await act(async () => { root?.render(<OfferNegotiationDrawer open offer={offer} onClose={vi.fn()} />); });
    const inputs = host?.querySelectorAll('input') ?? [];
    const textareas = host?.querySelectorAll('textarea') ?? [];
    await act(async () => {
      changeValue(inputs[0] as HTMLInputElement, '确认薪资结构');
      changeValue(textareas[0] as HTMLTextAreaElement, '远程安排');
      changeValue(inputs[1] as HTMLInputElement, '电话沟通');
      host?.querySelector<HTMLButtonElement>('[data-testid="offer-negotiation-generate"]')?.click();
    });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(service.preview).toHaveBeenCalledTimes(1);
    expect(service.create).not.toHaveBeenCalled();
    expect(host?.querySelector('[aria-label="确认本次 AI 输入"]')).not.toBeNull();
    expect(window.confirm).toBe(nativeConfirm);

    await act(async () => { host?.querySelector<HTMLButtonElement>('[data-action="confirm-generate"]')?.click(); });
    expect(service.create).toHaveBeenCalledTimes(1);
    expect(window.confirm).toBe(nativeConfirm);
  });

  it('marks Pilot-generated requests without changing the API payload', async () => {
    service.create.mockResolvedValue(proposal());
    await act(async () => {
      root?.render(<OfferNegotiationDrawer open offer={offer} entrypoint="pilot" onClose={vi.fn()} />);
    });
    const inputs = host?.querySelectorAll('input') ?? [];
    const textareas = host?.querySelectorAll('textarea') ?? [];
    await act(async () => {
      changeValue(inputs[0] as HTMLInputElement, 'Goal');
      changeValue(textareas[0] as HTMLTextAreaElement, 'Concern');
      changeValue(inputs[1] as HTMLInputElement, 'Call');
      host?.querySelector<HTMLButtonElement>('[data-testid="offer-negotiation-generate"]')?.click();
    });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    await act(async () => { host?.querySelector<HTMLButtonElement>('[data-testid="offer-negotiation-generate"]')?.click(); });
    expect(service.create.mock.calls[0][2]).toBe('pilot');
  });

  it.each([
    ['network timeout', new OfferNegotiationError(0, null), '请求可能仍在后台处理，请使用原尝试重试；输入已冻结。'],
    ['provider error', new OfferNegotiationError(502, 'offer_negotiation_provider_error'), 'AI 服务暂不可用，请使用原尝试重试。'],
    ['bare 5xx', new OfferNegotiationError(502, null), '谈薪准备暂时不可用，请稍后重试。'],
  ])('keeps %s input frozen and exposes retry', async (_label, error, expectedMessage) => {
    service.create.mockRejectedValueOnce(error);
    await act(async () => { root?.render(<OfferNegotiationDrawer open offer={offer} onClose={vi.fn()} />); });
    const inputs = host?.querySelectorAll('input') ?? [];
    const textareas = host?.querySelectorAll('textarea') ?? [];
    await act(async () => {
      changeValue(inputs[0] as HTMLInputElement, 'Goal');
      changeValue(textareas[0] as HTMLTextAreaElement, 'Concern');
      changeValue(inputs[1] as HTMLInputElement, 'Call');
    });
    await act(async () => { host?.querySelector<HTMLButtonElement>('[data-testid="offer-negotiation-generate"]')?.click(); });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    await act(async () => { host?.querySelector<HTMLButtonElement>('[data-testid="offer-negotiation-generate"]')?.click(); });
    expect(host?.querySelector('fieldset')?.hasAttribute('disabled')).toBe(true);
    expect(service.create).toHaveBeenCalledTimes(1);
    expect(host?.textContent).toContain(expectedMessage);
    expect(host?.textContent).toContain('使用原尝试重试');
  });

  it('explains that an interrupted save may still finish before offering the original retry', async () => {
    service.create.mockResolvedValue(proposal());
    service.confirm.mockRejectedValueOnce(new OfferNegotiationError(0, null));
    await act(async () => { root?.render(<OfferNegotiationDrawer open offer={offer} onClose={vi.fn()} />); });
    const inputs = host?.querySelectorAll('input') ?? [];
    const textareas = host?.querySelectorAll('textarea') ?? [];
    await act(async () => {
      changeValue(inputs[0] as HTMLInputElement, 'Goal');
      changeValue(textareas[0] as HTMLTextAreaElement, 'Concern');
      changeValue(inputs[1] as HTMLInputElement, 'Call');
      host?.querySelector<HTMLButtonElement>('[data-testid="offer-negotiation-generate"]')?.click();
    });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    await act(async () => { host?.querySelector<HTMLButtonElement>('[data-action="confirm-generate"]')?.click(); });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    await act(async () => { host?.querySelector<HTMLInputElement>('article input[type="checkbox"]')?.click(); });
    await act(async () => { host?.querySelector<HTMLButtonElement>('[data-testid="offer-negotiation-confirm"]')?.click(); });
    await act(async () => { host?.querySelector<HTMLButtonElement>('[data-action="confirm-save"]')?.click(); });

    expect(host?.textContent).toContain('请求可能仍在后台处理，请使用原尝试重试；输入已冻结。');
    expect(host?.textContent).toContain('使用原尝试重试');
    expect(service.confirm).toHaveBeenCalledTimes(1);
  });
});
