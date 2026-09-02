// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { OfferNegotiationBlock, OfferNegotiationProposal, OfferNegotiationSnapshot } from '@/types/offer';
import OfferSnapshotSummary from './OfferSnapshotSummary';
import NegotiationBriefForm from './NegotiationBriefForm';
import NegotiationProposalCard from './NegotiationProposalCard';
import NegotiationHistoryList from './NegotiationHistoryList';
import { OFFER_STATUS_LABELS } from '@/types/offer';

const snapshot: OfferNegotiationSnapshot = {
  snapshot_version: 1,
  offer_snapshot: {
    company_name: '星云数据',
    position_name: '后端工程师',
    status: 'pending',
    base_monthly: 28000,
    months_per_year: 12,
    signing_bonus: 0,
    equity: null,
    perks: '补充医疗、弹性办公',
    deadline: '2026-08-15',
    notes: '筱哲｜一线业务平台方向',
    dimensions: [{ path_id: 'dimension_001', label: '通勤', value_text: '地铁 35 分钟' }],
  },
  user_brief: { goal: '确认薪资结构', concerns: '远程安排', scenario: '电话沟通' },
};

const block: OfferNegotiationBlock = {
  id: 'goal-1',
  text: '确认薪资构成和沟通时间',
  rationale: '基于 Offer 固定事实准备沟通。',
  evidence_refs: [{ source: 'offer_snapshot', path: '/offer_snapshot/base_monthly', excerpt: '28000' }],
};

const proposal: OfferNegotiationProposal = {
  id: 12,
  offer_id: 7,
  application_id: null,
  attempt_status: 'ready',
  proposal_status: 'normal',
  proposal: {
    proposal_status: 'normal',
    communication_goals: [block],
    clarification_questions: [],
    talking_points: [],
    preparation_checks: [],
  },
  source_fingerprint: 'source',
  input_snapshot: snapshot,
  source_changed: false,
  source_states: {},
  proposal_hash: 'proposal',
  brief: {
    id: 3,
    proposal_id: 12,
    offer_id: 7,
    application_id: null,
    selected_blocks: ['goal-1'],
    edited_content: { blocks: [block], edits: { 'goal-1': '用户编辑后的表达' }, proposal_hash: 'proposal' },
    content_hash: 'brief',
    confirmed_at: '2026-08-01T08:00:00Z',
  },
};

describe('Offer negotiation presentation components', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    act(() => root?.unmount());
    host?.remove();
    root = null;
    host = null;
  });

  function mount(element: React.ReactElement) {
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root?.render(element));
    return host;
  }

  it('shows a compact frozen summary and keeps full facts available', () => {
    const rendered = mount(<OfferSnapshotSummary offer={snapshot.offer_snapshot} brief={snapshot.user_brief} sourceState="frozen" />);
    expect(rendered.textContent).toContain('后端工程师');
    expect(rendered.textContent).toContain('28K × 12');
    expect(rendered.textContent).toContain('已保留当时版本');
    expect(rendered.textContent).toContain('查看完整来源');
    expect(rendered.textContent).not.toContain('/offer_snapshot/base_monthly');
  });

  it('keeps a zero signing bonus as a real Offer fact', () => {
    const rendered = mount(<OfferSnapshotSummary offer={{ ...snapshot.offer_snapshot, signing_bonus: 0 }} sourceState="current" />);
    expect(rendered.querySelector('[data-testid="snapshot-signing-bonus"]')?.textContent).toContain('0');
    expect(rendered.querySelector('[data-testid="snapshot-signing-bonus"]')?.textContent).not.toContain('尚未填写');
  });

  it('maps frozen Offer status to the localized status label and separates custom dimensions', () => {
    const rendered = mount(<OfferSnapshotSummary offer={snapshot.offer_snapshot} sourceState="frozen" />);
    expect(rendered.textContent).toContain(OFFER_STATUS_LABELS.pending);
    expect(rendered.querySelector('[data-section="custom-dimensions-summary"]')?.textContent).toContain('通勤');
    expect(rendered.querySelector('[data-section="fixed-facts-summary"]')?.textContent).not.toContain('通勤');
  });

  it('reports field validation next to the controlled field', () => {
    const rendered = mount(
      <NegotiationBriefForm
        value={{ goal: '', concerns: '远程办公安排', scenario: '电话沟通' }}
        disabled={false}
        errors={{ goal: '请填写本次沟通目标' }}
        onChange={vi.fn()}
      />,
    );
    expect(rendered.querySelector('[role="alert"]')?.textContent).toBe('请填写本次沟通目标');
    expect(rendered.querySelector('label[for="negotiation-goal"]')).not.toBeNull();
  });

  it('keeps evidence secondary and reveals human-readable details before the technical path', () => {
    const onToggle = vi.fn();
    const rendered = mount(<NegotiationProposalCard block={block} selected={false} editedText={block.text} disabled={false} onToggle={onToggle} onEdit={vi.fn()} />);
    expect(rendered.textContent).toContain('可直接使用或编辑');
    expect(rendered.textContent).not.toContain('Offer 固定月薪');
    expect(rendered.textContent).not.toContain('/offer_snapshot/base_monthly');
    const evidenceToggle = rendered.querySelector<HTMLButtonElement>('[data-action="toggle-evidence"]');
    expect(evidenceToggle?.getAttribute('aria-expanded')).toBe('false');
    expect(evidenceToggle?.textContent).toContain('查看依据');
    act(() => evidenceToggle?.click());
    expect(evidenceToggle?.getAttribute('aria-expanded')).toBe('true');
    expect(rendered.textContent).toContain('当前固定月薪');
    expect(rendered.textContent).toContain('¥28,000/月');
    expect(rendered.textContent).toContain('来源路径 /offer_snapshot/base_monthly');
    expect(onToggle).not.toHaveBeenCalled();
  });

  it('does not squeeze the proposal header with a duplicate rationale tag', () => {
    const rendered = mount(<NegotiationProposalCard block={block} selected={false} editedText={block.text} disabled={false} onToggle={vi.fn()} onEdit={vi.fn()} />);
    expect(rendered.querySelector('.ant-tag')).toBeNull();
  });

  it('localizes a frozen Offer status in evidence excerpts', () => {
    const rendered = mount(
      <NegotiationProposalCard
        block={{ ...block, evidence_refs: [{ source: 'offer_snapshot', path: '/offer_snapshot/status', excerpt: 'pending' }] }}
        selected={false}
        editedText={block.text}
        disabled={false}
        onToggle={vi.fn()}
        onEdit={vi.fn()}
      />,
    );
    const evidenceToggle = rendered.querySelector<HTMLButtonElement>('[data-action="toggle-evidence"]');
    act(() => evidenceToggle?.click());
    expect(rendered.textContent).toContain('当前 Offer 状态');
    expect(rendered.textContent).toContain('待处理');
    expect(rendered.textContent).not.toContain('pending');
  });

  it('formats salary evidence with product language instead of raw numbers', () => {
    const rendered = mount(
      <NegotiationProposalCard
        block={{
          ...block,
          evidence_refs: [
            { source: 'offer_snapshot', path: '/offer_snapshot/base_monthly', excerpt: '24000' },
            { source: 'offer_snapshot', path: '/offer_snapshot/months_per_year', excerpt: '16' },
            { source: 'offer_snapshot', path: '/offer_snapshot/signing_bonus', excerpt: '0' },
          ],
        }}
        selected={false}
        editedText={block.text}
        disabled={false}
        onToggle={vi.fn()}
        onEdit={vi.fn()}
      />,
    );
    act(() => rendered.querySelector<HTMLButtonElement>('[data-action="toggle-evidence"]')?.click());
    expect(rendered.textContent).toContain('当前固定月薪');
    expect(rendered.textContent).toContain('¥24,000/月');
    expect(rendered.textContent).toContain('计薪月数');
    expect(rendered.textContent).toContain('16 薪');
    expect(rendered.textContent).toContain('签字费');
    expect(rendered.textContent).toContain('¥0');
    expect(rendered.textContent).not.toContain('已验证来源：16');
  });

  it('copies the edited talking point and announces success', async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    const rendered = mount(
      <NegotiationProposalCard
        kind="talking_points"
        block={block}
        selected={false}
        editedText="用户编辑后的表达"
        disabled={false}
        onToggle={vi.fn()}
        onEdit={vi.fn()}
      />,
    );
    const copyButton = rendered.querySelector<HTMLButtonElement>('[aria-label="复制这段表达"]');
    await act(async () => { copyButton?.click(); });
    expect(writeText).toHaveBeenCalledWith('用户编辑后的表达');
    expect(rendered.querySelector('[aria-live="polite"]')?.textContent).toContain('表达已复制');
  });

  it('announces a clipboard failure without changing the proposal', async () => {
    const writeText = vi.fn(async () => { throw new Error('denied'); });
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    const rendered = mount(
      <NegotiationProposalCard
        kind="talking_points"
        block={block}
        selected={false}
        editedText={block.text}
        disabled={false}
        onToggle={vi.fn()}
        onEdit={vi.fn()}
      />,
    );
    await act(async () => { rendered.querySelector<HTMLButtonElement>('[aria-label="复制这段表达"]')?.click(); });
    expect(rendered.querySelector('[aria-live="polite"]')?.textContent).toContain('复制失败');
    expect(rendered.textContent).toContain(block.text);
  });

  it('copies an intentionally empty edit without restoring the generated text', async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    const rendered = mount(
      <NegotiationProposalCard
        kind="talking_points"
        block={block}
        selected
        editedText=""
        disabled={false}
        onToggle={vi.fn()}
        onEdit={vi.fn()}
      />,
    );
    await act(async () => { rendered.querySelector<HTMLButtonElement>('[aria-label="复制这段表达"]')?.click(); });
    expect(writeText).toHaveBeenCalledWith('');
  });

  it('renders user evidence as text without leaking HTML entities or markup', () => {
    const rendered = mount(
      <NegotiationProposalCard
        block={{
          ...block,
          text: '希望多 2K & 保留 Offer <script>🙂',
          evidence_refs: [
            { source: 'user_brief', path: '/user_brief/goal', excerpt: '多 2K & 保留 Offer <script>🙂' },
          ],
        }}
        selected={false}
        editedText=""
        disabled={false}
        onToggle={vi.fn()}
        onEdit={vi.fn()}
      />,
    );
    act(() => rendered.querySelector<HTMLButtonElement>('[data-action="toggle-evidence"]')?.click());
    expect(rendered.textContent).toContain('多 2K & 保留 Offer <script>🙂');
    expect(rendered.innerHTML).not.toContain('<script>🙂</script>');
    expect(rendered.textContent).not.toContain('&#');
  });

  it('visually marks a selected proposal card', () => {
    const rendered = mount(<NegotiationProposalCard block={block} selected editedText={block.text} disabled={false} onToggle={vi.fn()} onEdit={vi.fn()} />);
    expect(rendered.querySelector('article[data-selected="true"]')).not.toBeNull();
  });

  it('shows real confirmation time but never invents a generation time', () => {
    const rendered = mount(<NegotiationHistoryList items={[proposal]} selectedId={null} onSelect={vi.fn()} />);
    expect(rendered.textContent).toContain('记录 #12');
    expect(rendered.textContent).toContain('已确认');
    expect(rendered.textContent).toContain('2026');
    expect(rendered.textContent).not.toContain('生成时间');
  });
});
