// @vitest-environment jsdom
import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import PilotOpportunityFitV2Card, { type PilotOpportunityFitV2Draft } from './PilotOpportunityFitV2Card';
import type { OpportunityFitV2StageResponse } from '@/types/opportunityFitReview';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const proposal = {
  schema_version: 2 as const,
  stage: 'triage' as const,
  source: { kind: 'opportunity_fit' as const, contract_version: 'opportunity_fit.v2' as const, snapshot_version: '1' as const },
  summary: { text: '基于冻结资料的摘要', rationale: '可审阅依据', evidence_refs: [{ source: 'jd' as const, path: '/jd/text', excerpt: '岗位要求' }] },
  conditions: [],
  risks: [],
  questions: [],
  next_steps: [],
};

function stage(status: OpportunityFitV2StageResponse['stage_status']): OpportunityFitV2StageResponse {
  return {
    id: 1,
    review_id: 2,
    stage_id: 1,
    application_id: 3,
    resume_id: 4,
    stage: 'triage',
    schema_version: 2,
    stage_status: status,
    parent_triage_stage_id: null,
    idempotency_key: 'triage-key',
    source_fingerprint_sha256: 'source',
    proposal_sha256: 'proposal',
    proposal,
    created_at: '2026-07-27T00:00:00Z',
  };
}

function draft(overrides: Partial<PilotOpportunityFitV2Draft> = {}): PilotOpportunityFitV2Draft {
  return {
    applicationId: 3,
    resumeId: 4,
    jdText: '岗位要求',
    assertionsText: '',
    triageKey: 'triage-key',
    deepKey: null,
    triage: null,
    deep: null,
    historical: false,
    resultUnknown: false,
    error: null,
    ...overrides,
  };
}

let root: Root | undefined;
let container: HTMLDivElement;

function renderCard(initial: PilotOpportunityFitV2Draft) {
  root = createRoot(container);
  const props = {
    draft: initial,
    resumes: [{ id: 4, title: '测试简历' }],
    history: [],
    onChange: vi.fn(),
    onStartTriage: vi.fn(),
    onConfirmTriage: vi.fn(),
    onStartDeepReview: vi.fn(),
    onViewHistory: vi.fn(),
    onStartNew: vi.fn(),
    onPrepareMaterials: vi.fn(),
    onCancel: vi.fn(),
  };
  act(() => root?.render(createElement(PilotOpportunityFitV2Card, props)));
  return props;
}

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
});

afterEach(() => {
  act(() => root?.unmount());
  container.remove();
});

describe('PilotOpportunityFitV2Card', () => {
  it('keeps inputs frozen and retries a pending Triage with the original key', () => {
    const props = renderCard(draft({ triage: stage('generating') }));
    const textarea = container.querySelector('textarea');
    expect((textarea as HTMLTextAreaElement).disabled).toBe(true);
    expect(container.textContent).toContain('结果待确认');
    const retry = [...container.querySelectorAll('button')].find((button) => button.textContent?.includes('使用原尝试重试快速判断'));
    expect(retry).toBeTruthy();
    act(() => retry?.click());
    expect(props.onStartTriage).toHaveBeenCalledWith(expect.objectContaining({ idempotency_key: 'triage-key' }));
  });

  it('retains the frozen form for an unknown result and exposes the same-key retry', () => {
    const props = renderCard(draft({ error: 'AI 服务暂不可用', resultUnknown: true }));
    expect((container.querySelector('select') as HTMLSelectElement).disabled).toBe(true);
    const retry = [...container.querySelectorAll('button')].find((button) => button.textContent?.includes('使用原尝试重试快速判断'));
    act(() => retry?.click());
    expect(props.onStartTriage).toHaveBeenCalledWith(expect.objectContaining({ idempotency_key: 'triage-key' }));
  });

  it('keeps historical results read-only and hides material handoff', () => {
    const prepare = vi.fn();
    root = createRoot(container);
    act(() => root?.render(createElement(PilotOpportunityFitV2Card, {
      draft: draft({ historical: true, triage: stage('ready'), deep: stage('ready') }),
      resumes: [{ id: 4, title: '测试简历' }],
      history: [],
      onChange: vi.fn(),
      onStartTriage: vi.fn(),
      onConfirmTriage: vi.fn(),
      onStartDeepReview: vi.fn(),
      onViewHistory: vi.fn(),
      onStartNew: vi.fn(),
      onPrepareMaterials: prepare,
      onCancel: vi.fn(),
    })));
    expect(container.textContent).toContain('开始新的岗位评估');
    expect(container.textContent).not.toContain('去准备材料');
    expect(prepare).not.toHaveBeenCalled();
  });

  it('renders source conflict as a Chinese read-only state with a fresh-start action', () => {
    const props = renderCard(draft({ triage: stage('source_conflict') }));
    expect(container.textContent).toContain('原资料已更新，本次结果仍使用旧版');
    const restart = [...container.querySelectorAll('button')].find((button) => !button.textContent?.includes('鍙栨秷娴佺▼'));
    act(() => restart?.click());
    expect(props.onStartNew).toHaveBeenCalledTimes(1);
  });

  it('disables every history entry while a live operation is pending', () => {
    const onViewHistory = vi.fn();
    const onViewLegacyHistory = vi.fn();
    root = createRoot(container);
    act(() => root?.render(createElement(PilotOpportunityFitV2Card, {
      draft: draft(),
      resumes: [{ id: 4, title: 'test-resume' }],
      history: [{ review_id: 2, stage_count: 1 } as never],
      legacyHistory: [{ id: 9 } as never],
      onChange: vi.fn(),
      onStartTriage: vi.fn(),
      onConfirmTriage: vi.fn(),
      onStartDeepReview: vi.fn(),
      onViewHistory,
      onViewLegacyHistory,
      onStartNew: vi.fn(),
      onCancel: vi.fn(),
      historyDisabled: true,
    })));

    const historyButtons = [...container.querySelectorAll<HTMLButtonElement>('button')]
      .filter((button) => button.textContent === '\u67e5\u770b');
    expect(historyButtons).toHaveLength(2);
    historyButtons.forEach((button) => {
      expect(button.disabled).toBe(true);
      act(() => button.click());
    });
    expect(onViewHistory).not.toHaveBeenCalled();
    expect(onViewLegacyHistory).not.toHaveBeenCalled();
  });

  it.each([
    ['result unknown', draft({ resultUnknown: true })],
    ['generating', draft({ triage: stage('generating') })],
    ['provider unknown', draft({ triage: stage('provider_unknown') })],
    ['deep generating', draft({ triageKey: null, deepKey: 'deep-key', deep: stage('generating') })],
    ['deep provider unknown', draft({ triageKey: null, deepKey: 'deep-key', deep: stage('provider_unknown') })],
  ])('keeps history disabled for a %s persisted attempt', (_label, persistedDraft) => {
    const onViewHistory = vi.fn();
    const onViewLegacyHistory = vi.fn();
    root = createRoot(container);
    act(() => root?.render(createElement(PilotOpportunityFitV2Card, {
      draft: persistedDraft,
      resumes: [{ id: 4, title: 'test-resume' }],
      history: [{ review_id: 2, stage_count: 1 } as never],
      legacyHistory: [{ id: 9 } as never],
      onChange: vi.fn(),
      onStartTriage: vi.fn(),
      onConfirmTriage: vi.fn(),
      onStartDeepReview: vi.fn(),
      onViewHistory,
      onViewLegacyHistory,
      onStartNew: vi.fn(),
      onCancel: vi.fn(),
    })));

    const historyButtons = [...container.querySelectorAll<HTMLButtonElement>('button')]
      .filter((button) => button.textContent === '\u67e5\u770b');
    expect(historyButtons).toHaveLength(2);
    historyButtons.forEach((button) => {
      expect(button.disabled).toBe(true);
      act(() => button.click());
    });
    expect(onViewHistory).not.toHaveBeenCalled();
    expect(onViewLegacyHistory).not.toHaveBeenCalled();
  });
});
