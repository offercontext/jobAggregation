// @vitest-environment jsdom
import { act, useState } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  createInitialOpportunityFitDraft,
  opportunityFitDraftReducer,
  type OpportunityFitDraftAction,
  type OpportunityFitDraftState,
} from './opportunityFitDraft';
import PilotOpportunityFitCard from './PilotOpportunityFitCard';
import type { OpportunityFitReview, OpportunityFitReviewSummary } from '@/types/opportunityFitReview';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const review = {
  schema_version: 1,
  id: 17,
  application_id: 7,
  resume_id: 11,
  status: 'triage_complete',
  recommendation: 'advance',
  summary: { text: '安全摘要', evidence_refs: [{ source: 'resume', path: '/summary', excerpt: '原文证据' }] },
  source_fingerprint_sha256: 'source',
  triage_sha256: 'triage',
  deep_review_sha256: null,
  created_at: '2026-07-22T00:00:00Z',
  deep_reviewed_at: null,
  source: {
    application: { id: 7, company_name: 'Example Co.', position_name: 'Engineer' },
    resume: { id: 11, title: '原始简历', sha256: 'resume' },
    jd: { source_label: '用户粘贴 JD', sha256: 'jd', text: '原始 JD 文本' },
    candidate_assertions: [{ index: 0, text: '用户事实' }],
  },
  triage: {
    summary: { text: '岗位摘要', evidence_refs: [{ source: 'jd', path: '/text', excerpt: 'JD 证据' }] },
    recommendation: 'advance',
    hard_constraints: [{ id: 'constraint-1', requirement: '需要 TypeScript', status: 'met', explanation: '已满足', evidence_refs: [{ source: 'resume', path: '/skills/0', excerpt: 'TypeScript' }] }],
    fit_signals: [{ id: 'fit-1', statement: '有相关经验', evidence_refs: [{ source: 'resume', path: '/experience/0', excerpt: '相关经历' }] }],
    gaps: [{ id: 'gap-1', requirement: '需要云平台经验', kind: 'preferred', candidate_status: 'unknown', evidence_refs: [{ source: 'jd', path: '/text', excerpt: '云平台' }] }],
    deadline: { status: 'stated', text: '本周五', evidence_refs: [{ source: 'jd', path: '/text', excerpt: '本周五' }] },
    next_questions: ['是否可以远程办公？'],
  },
  deep_review: null,
} satisfies OpportunityFitReview;

const deepReview = {
  ...review,
  status: 'deep_reviewed',
  deep_reviewed_at: '2026-07-22T00:01:00Z',
  deep_review_sha256: 'deep',
  deep_review: {
    strengths: [{ id: 'strength-1', statement: '优势内容', evidence_refs: [{ source: 'resume', path: '/experience/0', excerpt: '经历证据' }] }],
    gaps_to_address: [{ id: 'gap-2', statement: '待补足内容', evidence_refs: [{ source: 'resume', path: '/experience/0', excerpt: '经历证据' }] }],
    questions_to_clarify: [{ id: 'question-1', statement: '需要澄清的问题', evidence_refs: [{ source: 'user_assertion', path: '/user_assertions/0/text', excerpt: '用户事实' }] }],
    recommended_path: 'prepare_materials',
    next_actions: [{ id: 'action-1', label: '准备材料', kind: 'open_material_kit' }],
  },
} satisfies OpportunityFitReview;

let root: Root | undefined;
let container: HTMLDivElement | undefined;

const validReview: OpportunityFitReview = {
  ...review,
  summary: {
    text: review.summary.text,
    evidence_refs: [{ source: 'jd', path: '/text', excerpt: review.source.jd.text }],
  },
  triage: {
    ...review.triage,
    summary: {
      text: review.triage.summary.text,
      evidence_refs: [{ source: 'jd', path: '/text', excerpt: review.source.jd.text }],
    },
    hard_constraints: [{
      ...review.triage.hard_constraints[0],
      evidence_refs: [
        { source: 'jd', path: '/text', excerpt: review.source.jd.text },
        { source: 'resume', path: '/skills/0', excerpt: 'TypeScript' },
      ],
    }],
    fit_signals: [{
      ...review.triage.fit_signals[0],
      evidence_refs: [{ source: 'resume', path: '/experience/1', excerpt: '相关经历' }],
    }],
    gaps: [{
      ...review.triage.gaps[0],
      evidence_refs: [{ source: 'jd', path: '/text', excerpt: review.source.jd.text }],
    }],
    deadline: {
      ...review.triage.deadline,
      evidence_refs: [{ source: 'jd', path: '/text', excerpt: review.source.jd.text }],
    },
  },
};

const validDeepReview: OpportunityFitReview = {
  ...validReview,
  status: 'deep_reviewed',
  deep_reviewed_at: '2026-07-22T00:01:00Z',
  deep_review_sha256: 'deep',
  deep_review: deepReview.deep_review,
};

const defaultResumeEvidenceProof = {
  resumeId: 11,
  sha256: 'resume',
  contentJson: {
  skills: ['TypeScript'],
  experience: ['经历证据', '相关经历'],
  },
};

function Harness({
  initial = createInitialOpportunityFitDraft(7, 'pilot:7'),
  deepLoading = false,
  historyLoading = false,
  historicalReviews = [],
  onViewHistoricalReview,
  resumeEvidenceProof: proof = defaultResumeEvidenceProof,
}: {
  initial?: OpportunityFitDraftState;
  deepLoading?: boolean;
  historyLoading?: boolean;
  historicalReviews?: OpportunityFitReviewSummary[];
  onViewHistoricalReview?: (reviewId: number) => void;
  resumeEvidenceProof?: typeof defaultResumeEvidenceProof | null;
}) {
  const [draft, dispatch] = useState(initial);
  const reduce = (action: OpportunityFitDraftAction) => dispatch((current) => opportunityFitDraftReducer(current, action));
  return (
    <PilotOpportunityFitCard
      draft={draft}
      dispatch={reduce}
      resumes={[{ id: 11, title: '原始简历' }]}
      resumeEvidenceProof={proof}
      onStartTriage={triage}
      onRetryTriage={retry}
      onStartDeepReview={deep}
      onPrepareMaterials={prepare}
      onCancel={cancel}
      triageFailureDisposition={triageDisposition}
      isHistoryLoading={historyLoading}
      historicalReviews={historicalReviews}
      onViewHistoricalReview={onViewHistoricalReview}
      historicalReview={initial.reviewSource === 'historical'}
      isDeepReviewLoading={deepLoading}
    />
  );
}

const triage = vi.fn();
const retry = vi.fn();
const deep = vi.fn();
const prepare = vi.fn();
const cancel = vi.fn();
let triageDisposition: 'unknown' | 'definite_no_write' | undefined;

beforeEach(() => {
  triage.mockReset();
  retry.mockReset();
  deep.mockReset();
  prepare.mockReset();
  cancel.mockReset();
  triageDisposition = undefined;
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root?.unmount());
  container?.remove();
});

async function render(initial?: OpportunityFitDraftState, proof: typeof defaultResumeEvidenceProof | null = defaultResumeEvidenceProof) {
  await act(async () => {
    root?.unmount();
    root = createRoot(container!);
    root.render(<Harness initial={initial} resumeEvidenceProof={proof} />);
  });
  return container!;
}

function getByRole(view: HTMLElement, role: string, name: string): HTMLElement {
  const selector = role === 'button' ? 'button,[role="button"]' : `[role="${role}"]`;
  const found = [...view.querySelectorAll<HTMLElement>(selector)].find((item) => (
    (item.getAttribute('aria-label') ?? item.textContent?.trim() ?? '') === name
  ));
  if (!found) throw new Error(`missing ${role} ${name}`);
  return found;
}

function button(view: HTMLElement, name: string): HTMLButtonElement {
  const found = getByRole(view, 'button', name);
  if (!(found instanceof HTMLButtonElement)) throw new Error(`missing button ${name}`);
  return found;
}

function dialogButton(view: HTMLElement, name: string): HTMLButtonElement {
  const dialog = view.querySelector('[role="dialog"]');
  if (!(dialog instanceof HTMLElement)) throw new Error('missing confirmation dialog');
  return button(dialog, name);
}

function labeled(view: HTMLElement, text: string): HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement {
  const label = [...view.querySelectorAll('label')].find((item) => item.textContent?.includes(text));
  const control = label?.querySelector('input,textarea,select');
  if (!(control instanceof HTMLInputElement || control instanceof HTMLTextAreaElement || control instanceof HTMLSelectElement)) {
    throw new Error(`missing field ${text}`);
  }
  return control;
}

async function change(control: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement, value: string) {
  const prototype = control instanceof HTMLSelectElement ? HTMLSelectElement.prototype : control instanceof HTMLInputElement ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype;
  Object.getOwnPropertyDescriptor(prototype, 'value')?.set?.call(control, value);
  await act(async () => control.dispatchEvent(new Event('input', { bubbles: true })));
  await act(async () => control.dispatchEvent(new Event('change', { bubbles: true })));
}

async function click(view: HTMLElement, name: string) {
  await act(async () => button(view, name).click());
}

async function clickDialog(view: HTMLElement, name: string) {
  await act(async () => dialogButton(view, name).click());
}

describe('PilotOpportunityFitCard', () => {
  it('shows read-only historical summaries and requests details only when viewed', async () => {
    const onViewHistoricalReview = vi.fn();
    const view = await render();
    await act(async () => root?.render(
      <Harness
        historicalReviews={[{
          schema_version: 1,
          id: 42,
          application_id: 7,
          resume_id: 11,
          status: 'triage_complete',
          summary: { text: '历史摘要', evidence_refs: [] },
          recommendation: 'advance',
          source_fingerprint_sha256: 'source',
          triage_sha256: 'triage',
          deep_review_sha256: null,
          created_at: '2026-07-22T00:00:00Z',
          deep_reviewed_at: null,
        }]}
        onViewHistoricalReview={onViewHistoricalReview}
      />,
    ));

    expect(view.textContent).toContain('历史岗位评估');
    expect(view.textContent).toContain('历史摘要');
    await click(view, '查看历史评估');
    expect(onViewHistoricalReview).toHaveBeenCalledWith(42);
    expect(triage).not.toHaveBeenCalled();
  });

  it('lets the user return from a historical review to a fresh form', async () => {
    const historical = { ...validReview, id: 42 };
    const initial = {
      ...createInitialOpportunityFitDraft(7, 'pilot:7'),
      review: historical,
      reviewSource: 'historical' as const,
      phase: 'triage_ready' as const,
      resumeID: 11,
      jdText: '冻结 JD',
    };
    const view = await render(initial);
    await click(view, '开始新的岗位评估');
    expect(view.querySelector('select')?.value).toBe('');
    expect(view.textContent).toContain('开始快速判断');
  });

  it('keeps historical Triage details read-only and does not offer Deep Review', async () => {
    const initial = {
      ...createInitialOpportunityFitDraft(7, 'pilot:7'),
      review: validReview,
      reviewSource: 'historical' as const,
      phase: 'triage_ready' as const,
    };
    const view = await render(initial);
    expect(view.textContent).toContain('开始新的岗位评估');
    expect(view.textContent).not.toContain('开始深入分析');
    expect(view.querySelector('button')?.textContent).not.toContain('开始深入分析');
    expect(deep).not.toHaveBeenCalled();
  });

  it('blocks new triage while historical reviews are loading', async () => {
    const view = await render();
    await act(async () => root?.render(<Harness historyLoading />));
    expect(view.querySelector('[role="status"]')?.textContent).toContain('历史岗位评估');
    expect(button(view, '开始快速判断').disabled).toBe(true);
  });

  it('normalizes assertions and disables triage for invalid input', async () => {
    const view = await render();
    await change(labeled(view, '选择简历'), '11');
    await change(labeled(view, '粘贴 JD'), 'JD');
    await change(labeled(view, '补充断言'), Array.from({ length: 11 }, (_, i) => ` fact ${i} `).join('\n'));
    expect(view.textContent).toContain('最多填写 10 条非空断言');
    expect(button(view, '开始快速判断').disabled).toBe(true);
    expect(triage).not.toHaveBeenCalled();
  });

  it('requires confirmation and does not call triage when cancelled', async () => {
    const view = await render();
    await change(labeled(view, '选择简历'), '11');
    await change(labeled(view, '粘贴 JD'), 'JD');
    await click(view, '开始快速判断');
    expect(view.textContent).toContain('确认将这些内容发送给当前配置的 AI 服务');
    await clickDialog(view, '取消');
    expect(triage).not.toHaveBeenCalled();
  });

  it('sends the controlled draft and attempt key only after confirmation', async () => {
    const view = await render();
    await change(labeled(view, '选择简历'), '11');
    await change(labeled(view, '粘贴 JD'), ' JD ');
    await change(labeled(view, '补充断言'), ' fact one \n\n fact two ');
    await click(view, '开始快速判断');
    await click(view, '确认发送');
    expect(triage).toHaveBeenCalledWith(expect.objectContaining({ resumeID: 11, jdText: 'JD', assertionsText: 'fact one\nfact two' }), null);
  });

  it('submits normalized input without mutating the original draft', async () => {
    const initial = createInitialOpportunityFitDraft(7, 'pilot:7');
    const view = await render(initial);
    await change(view.querySelector('select')!, '11');
    await change(labeled(view, '粘贴 JD'), '  JD  ');
    await change(labeled(view, '补充断言'), '  fact  \n');
    await click(view, '开始快速判断');
    const dialog = view.querySelector('[role="dialog"]');
    const confirmButton = dialog?.querySelectorAll('button')[1];
    await act(async () => (confirmButton as HTMLButtonElement).click());

    expect(triage).toHaveBeenCalledWith(
      expect.objectContaining({ jdText: 'JD', assertionsText: 'fact' }),
      null,
    );
    expect(initial.jdText).toBe('');
    expect(initial.assertionsText).toBe('');
  });

  it('renders evidence-backed triage content without translating dynamic text', async () => {
    const initial = { ...createInitialOpportunityFitDraft(7, 'pilot:7'), review: validReview, phase: 'triage_ready' as const };
    const view = await render(initial);
    expect(view.textContent).toContain('岗位摘要');
    expect(view.textContent).toContain('简历');
    expect(view.textContent).toContain('用户粘贴 JD');
    expect(view.textContent).toContain('原始 JD 文本');
    expect(view.textContent).toContain('/text');
    expect(view.textContent).toContain('岗位描述');
    expect(view.textContent).toContain('岗位约束');
  });

  it('shows unknown failure and retries with the same attempt key', async () => {
    triageDisposition = 'unknown';
    const initial = { ...createInitialOpportunityFitDraft(7, 'pilot:7'), phase: 'triage_loading' as const, triageAttemptKey: 'attempt-1', actionError: '安全错误' };
    const view = await render(initial);
    expect(view.textContent).toContain('结果未知');
    expect(view.querySelector('[role="status"]')).toBeNull();
    await click(view, '使用原尝试重试');
    expect(retry).toHaveBeenCalledWith(expect.objectContaining({ triageAttemptKey: 'attempt-1' }), 'attempt-1');
  });

  it('keeps the retry action reachable after an error returns to confirmation', async () => {
    triageDisposition = 'unknown';
    const initial = {
      ...createInitialOpportunityFitDraft(7, 'pilot:7'),
      phase: 'confirm_triage' as const,
      triageAttemptKey: 'attempt-1',
      actionError: '安全错误',
    };
    const view = await render(initial);
    await click(view, '使用原尝试重试');
    expect(retry).toHaveBeenCalledWith(expect.objectContaining({ triageAttemptKey: 'attempt-1' }), 'attempt-1');
  });

  it('requires a second confirmation before deep review', async () => {
    const initial = { ...createInitialOpportunityFitDraft(7, 'pilot:7'), review: validReview, phase: 'triage_ready' as const };
    const view = await render(initial);
    await click(view, '开始深入分析');
    expect(view.textContent).toContain('确认开始深入分析');
    await clickDialog(view, '取消');
    expect(deep).not.toHaveBeenCalled();
    await click(view, '开始深入分析');
    await click(view, '确认深入分析');
    expect(deep).toHaveBeenCalledWith(expect.anything(), expect.objectContaining({ id: 17 }));
  });

  it('renders deep review and uses the primary prepare button', async () => {
    const initial = { ...createInitialOpportunityFitDraft(7, 'pilot:7'), review: validDeepReview, phase: 'deep_review_ready' as const };
    const view = await render(initial);
    expect(view.textContent).toContain('优势内容');
    expect(view.textContent).toContain('建议准备材料');
    await click(view, '去准备材料');
    expect(prepare).toHaveBeenCalledWith(expect.objectContaining({
      applicationId: 7,
      resumeId: 11,
      jdText: '原始 JD 文本',
    }));
    expect(prepare.mock.calls[0][0]).not.toHaveProperty('review');
    expect(prepare.mock.calls[0][0]).not.toHaveProperty('resumeEvidenceProof');
  });

  it('requires explicit confirmation when deviating from the recommendation', async () => {
    const divergent = { ...validDeepReview, deep_review: { ...validDeepReview.deep_review!, recommended_path: 'clarify_first' as const } };
    const initial = { ...createInitialOpportunityFitDraft(7, 'pilot:7'), review: divergent, phase: 'deep_review_ready' as const };
    const view = await render(initial);
    await click(view, '仍要准备材料');
    expect(view.textContent).toContain('当前建议不是准备材料');
    await clickDialog(view, '取消');
    expect(prepare).not.toHaveBeenCalled();
    await click(view, '仍要准备材料');
    await click(view, '确认仍要准备材料');
    expect(prepare).toHaveBeenCalledWith(expect.objectContaining({ resumeId: 11, jdText: '原始 JD 文本' }));
  });

  it('does not render malformed or empty review payloads', async () => {
    const malformed = { ...createInitialOpportunityFitDraft(7, 'pilot:7'), review: {} as OpportunityFitReview, phase: 'triage_ready' as const };
    const view = await render(malformed);
    expect(view.textContent).toContain('暂无可展示的评估结果');
    expect(view.textContent).not.toContain('undefined');
  });

  it('does not render a deep review with empty or disallowed gap evidence', async () => {
    const malformed = {
      ...validDeepReview,
      deep_review: {
        ...validDeepReview.deep_review!,
        gaps_to_address: [{ ...deepReview.deep_review!.gaps_to_address[0], evidence_refs: [] }],
      },
    } as OpportunityFitReview;
    const view = await render({ ...createInitialOpportunityFitDraft(7, 'pilot:7'), review: malformed, phase: 'deep_review_ready' });
    expect(view.textContent).toContain('暂无可展示的评估结果');
    expect(view.textContent).not.toContain('待补足内容');

    const disallowed = {
      ...validDeepReview,
      deep_review: {
        ...validDeepReview.deep_review!,
        gaps_to_address: [{ ...deepReview.deep_review!.gaps_to_address[0], evidence_refs: [{ source: 'evidence_bundle', path: '/text', excerpt: '岗位证据' }] }],
      },
    } as unknown as OpportunityFitReview;
    const disallowedView = await render({ ...createInitialOpportunityFitDraft(7, 'pilot:7'), review: disallowed, phase: 'deep_review_ready' });
    expect(disallowedView.textContent).toContain('暂无可展示的评估结果');
  });

  it('rejects a 501-character assertion before triage', async () => {
    const view = await render();
    await change(labeled(view, '补充断言'), 'x'.repeat(501));
    expect(view.textContent).toContain('每条断言最多 500 字');
    expect(button(view, '开始快速判断').disabled).toBe(true);
    expect(triage).not.toHaveBeenCalled();
  });

  it('marks deep review loading with a status role', async () => {
    const initial = { ...createInitialOpportunityFitDraft(7, 'pilot:7'), review: validReview, phase: 'deep_review_loading' as const };
    await act(async () => root?.render(<Harness initial={initial} deepLoading />));
    const view = container!;
    expect(view.querySelector('[role="status"]')?.textContent).toContain('深入分析');
  });

  it('safely renders a malformed nested deep review as empty state', async () => {
    const malformed = {
      ...validDeepReview,
      deep_review: {
        ...validDeepReview.deep_review!,
        gaps_to_address: [{ ...deepReview.deep_review!.gaps_to_address[0], evidence_refs: [{}] }],
      },
    } as unknown as OpportunityFitReview;
    const view = await render({ ...createInitialOpportunityFitDraft(7, 'pilot:7'), review: malformed, phase: 'deep_review_ready' });
    expect(view.textContent).toContain('暂无可展示的评估结果');
    expect(view.textContent).not.toContain('undefined');
  });

  it('never displays raw service error text and hands off only frozen source data', async () => {
    const initial = { ...createInitialOpportunityFitDraft(7, 'pilot:7'), review: validDeepReview, phase: 'deep_review_ready' as const, actionError: 'AxiosError: secret backend detail' };
    const view = await render(initial);
    expect(view.textContent).not.toContain('secret backend detail');
    await click(view, '去准备材料');
    expect(prepare).toHaveBeenCalledWith(expect.objectContaining({ applicationId: 7, resumeId: 11, jdText: '原始 JD 文本' }));
  });

  it('fails closed when resume evidence proof is missing or forged', async () => {
    const missingProof = await render({ ...createInitialOpportunityFitDraft(7, 'pilot:7'), review: validReview, phase: 'triage_ready' }, null);
    expect(missingProof.textContent).toContain('暂无可展示的评估结果');
    expect(missingProof.textContent).not.toContain('岗位摘要');

    const forgedReview = {
      ...validDeepReview,
      deep_review: {
        ...validDeepReview.deep_review!,
        gaps_to_address: [{
          ...validDeepReview.deep_review!.gaps_to_address[0],
          evidence_refs: [{ source: 'resume', path: '/skills/0', excerpt: '伪造证据' }],
        }],
      },
    } as OpportunityFitReview;
    const forged = await render({ ...createInitialOpportunityFitDraft(7, 'pilot:7'), review: forgedReview, phase: 'deep_review_ready' }, defaultResumeEvidenceProof);
    expect(forged.textContent).toContain('暂无可展示的评估结果');
    expect(forged.textContent).not.toContain('待补足内容');
    expect(prepare).not.toHaveBeenCalled();
  });

  it('cancels through the controlled callback without creating material state', async () => {
    const view = await render();
    await click(view, '取消流程');
    expect(cancel).toHaveBeenCalledTimes(1);
    expect(prepare).not.toHaveBeenCalled();
  });
});
