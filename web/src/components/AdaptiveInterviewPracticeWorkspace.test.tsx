// @vitest-environment jsdom
import { act, type ComponentType } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AdaptivePracticeOwnerDraft } from '@/types/adaptiveInterviewPractice';

const service = vi.hoisted(() => ({ plans: vi.fn(), start: vi.fn(), complete: vi.fn(), focus: vi.fn() }));

vi.mock('@/services/adaptiveInterviewPractice', () => ({
  listAdaptivePracticePlans: service.plans,
  startAdaptivePracticeV2: service.start,
  completeAdaptivePractice: service.complete,
  AdaptivePracticeError: class extends Error {
    code?: string;
    constructor(code?: string) { super(code ?? 'unknown'); this.code = code; }
  },
}));
vi.mock('@/features/reviewReadiness/service', () => ({ getReadinessPracticeFocus: service.focus }));

const { default: AdaptiveInterviewPracticeWorkspace } = await import('./AdaptiveInterviewPracticeWorkspace');
const { createCoreTaskSurfaceController } = await import('@/features/coreTaskSurface/controller');
const ControlledAdaptiveInterviewPracticeWorkspace = AdaptiveInterviewPracticeWorkspace as unknown as ComponentType<Record<string, unknown>>;

let root: Root | undefined;
let container: HTMLDivElement | undefined;
const ownerFocus = { ownerGeneration: 7, signalVersionId: 91, targetEventId: 103 };
const exactFocus = {
  schema_version: 1 as const,
  signalId: 51,
  versionId: 91,
  practiceSourceFingerprint: `sha256:${'a'.repeat(64)}`,
  practiceTargetFingerprint: `sha256:${'b'.repeat(64)}`,
  state: 'available' as const,
  practiceState: 'not_started' as const,
  selected: false,
  title: '拆解卡住的关键一步',
  sourceLabel: '第 1 轮面试复盘',
  targetEventId: 103,
};
const recommendation = {
  proposal_id: 4, focus_id: 'focus-1', application_id: 2, application_event_id: 3,
  interview_note_id: 5, company_name: '云栖智能', position_name: '后端工程师',
  drill_kind: 'difficulty_breakdown' as const, title: '拆解卡住的关键一步',
  observation: '影响范围追问时，回答节奏被打断。', reason: '这个问题在复盘中被明确记录为卡点。',
  prompt: '写出当时卡住的具体节点，并用三步说明下一次如何推进。',
  source_path: '/difficulty_points', source_excerpt: '被追问影响范围时卡住了。',
  source_fingerprint: exactFocus.practiceSourceFingerprint,
};
const plan = {
  id: 8, ...recommendation,
  origin_contract: 'confirmed_readiness_signal_v1' as const,
  target_application_event_id: 103,
  readiness_signal_version_id: 91,
  target_fingerprint: exactFocus.practiceTargetFingerprint,
  practice_state: 'in_progress' as const,
  status: 'in_progress' as const,
  revision: 1,
  response_text: '', reflection_text: '', self_assessment: '',
  source_status: null,
  created_at: '2026-08-12T10:00:00Z', completed_at: null,
};
const legacyPlan = {
  ...plan,
  id: 17,
  title: '历史 V1 练习',
  origin_contract: 'legacy_review_focus_v1' as const,
  target_application_event_id: null,
  readiness_signal_version_id: null,
  target_fingerprint: null,
};

function setTextArea(control: HTMLTextAreaElement, value: string) {
  Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set?.call(control, value);
  control.dispatchEvent(new Event('input', { bubbles: true }));
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  Object.defineProperty(window, 'matchMedia', { configurable: true, value: () => ({ matches: false, addListener: () => undefined, removeListener: () => undefined }) });
  const nativeGetComputedStyle = window.getComputedStyle.bind(window);
  vi.spyOn(window, 'getComputedStyle').mockImplementation((element) => nativeGetComputedStyle(element));
  service.focus.mockReset().mockResolvedValue(exactFocus);
  service.plans.mockReset().mockResolvedValue([]);
  service.start.mockReset().mockResolvedValue(plan);
  service.complete.mockReset().mockResolvedValue({ ...plan, status: 'completed', practice_state: 'completed', revision: 2, self_assessment: 'clearer' });
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => { act(() => root?.unmount()); container?.remove(); vi.restoreAllMocks(); });

describe('AdaptiveInterviewPracticeWorkspace', () => {
  it.each(['false', 'throw'] as const)('does not start when exact frozen Practice persistence returns %s', async (failure) => {
    const persist = vi.fn((_key: string, draft: AdaptivePracticeOwnerDraft | null) => {
      if (draft?.pendingOperation !== 'start') return true;
      if (failure === 'throw') throw new Error('storage failed');
      return false;
    });
    act(() => root?.render(<ControlledAdaptiveInterviewPracticeWorkspace
      focus={ownerFocus} ownerGeneration={7} drafts={{}} onDraftChange={persist}
    />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    act(() => [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent?.includes('确认这组重点与目标'))?.click());
    await act(async () => {
      [...document.body.querySelectorAll('button')].find((button) => button.textContent === '确认开始')?.click();
      await Promise.resolve();
    });
    expect(persist).toHaveBeenCalledWith('practice:7:91:103:new', expect.objectContaining({
      pendingOperation: 'start', startInput: expect.objectContaining({ idempotency_key: expect.any(String) }),
    }), undefined);
    expect(service.start).not.toHaveBeenCalled();
  });

  it.each(['false', 'throw'] as const)('does not complete when exact frozen Practice persistence returns %s', async (failure) => {
    service.plans.mockResolvedValue([plan]);
    const key = `practice:7:91:103:plan:${plan.id}`;
    const existing: AdaptivePracticeOwnerDraft = {
      ownerKey: key, ownerGeneration: 7, signalVersionId: 91, targetEventId: 103, planId: plan.id,
      answer: 'persist before complete', reflection: 'reflection', assessment: 'clearer',
      startInput: null, completionInput: null, resultUnknown: false, pendingOperation: null,
    };
    const persist = vi.fn((_key: string, draft: AdaptivePracticeOwnerDraft | null) => {
      if (draft?.pendingOperation !== 'complete') return true;
      if (failure === 'throw') throw new Error('storage failed');
      return false;
    });
    act(() => root?.render(<ControlledAdaptiveInterviewPracticeWorkspace
      focus={ownerFocus} ownerGeneration={7} drafts={{ [key]: existing }} onDraftChange={persist}
    />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    await act(async () => {
      [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent === '完成本次练习')?.click();
      await Promise.resolve();
    });
    expect(persist).toHaveBeenCalledWith(key, expect.objectContaining({
      pendingOperation: 'complete', completionInput: expect.objectContaining({ response_text: 'persist before complete' }),
    }), undefined);
    expect(service.complete).not.toHaveBeenCalled();
  });

  it('requires an exact Signal Version and target Event, then starts with a bare UUID', async () => {
    act(() => root?.render(<AdaptiveInterviewPracticeWorkspace focus={ownerFocus} />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(service.focus).toHaveBeenCalledWith(91, 103);
    expect(container?.textContent).toContain('拆解卡住的关键一步');
    expect(service.start).not.toHaveBeenCalled();
    act(() => [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent?.includes('确认这组重点与目标'))?.click());
    expect(document.body.textContent).toContain('目标面试 #103');
    await act(async () => {
      const confirm = [...document.body.querySelectorAll('button')].find((button) => button.textContent === '确认开始');
      confirm?.click();
      confirm?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(service.start).toHaveBeenCalledWith({
      readiness_signal_version_id: 91,
      target_application_event_id: 103,
      expected_source_fingerprint: exactFocus.practiceSourceFingerprint,
      expected_target_fingerprint: exactFocus.practiceTargetFingerprint,
      idempotency_key: expect.stringMatching(/^[0-9a-f-]{36}$/),
    });
    expect(service.start).toHaveBeenCalledTimes(1);
    expect(service.start.mock.calls[0][0].idempotency_key).not.toContain('adaptive-practice');
  });

  it('completes a V2 plan with a bare UUID and semantic self assessment', async () => {
    service.plans.mockResolvedValue([plan]);
    act(() => root?.render(<AdaptiveInterviewPracticeWorkspace focus={ownerFocus} />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    const response = container?.querySelector('textarea[aria-label="练习回答"]') as HTMLTextAreaElement;
    const reflection = container?.querySelector('textarea[aria-label="练习复盘"]') as HTMLTextAreaElement;
    act(() => { setTextArea(response, '先说明影响范围，再讲定位和恢复结果。'); setTextArea(reflection, '下一次先给结论。'); });
    act(() => [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent?.includes('更清楚了'))?.click());
    await act(async () => {
      [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent === '完成本次练习')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(service.complete).toHaveBeenCalledWith(8, expect.objectContaining({
      response_text: '先说明影响范围，再讲定位和恢复结果。',
      reflection_text: '下一次先给结论。', self_assessment: 'clearer',
      idempotency_key: expect.stringMatching(/^[0-9a-f-]{36}$/),
    }));
    expect(service.complete.mock.calls[0][1].idempotency_key).not.toContain('adaptive-practice');
  });

  it('converges an unknown completion with the same key and frozen answer', async () => {
    service.plans.mockResolvedValue([plan]);
    service.complete.mockRejectedValueOnce(new Error('network')).mockResolvedValueOnce({ ...plan, status: 'completed', practice_state: 'completed', revision: 2 });
    act(() => root?.render(<AdaptiveInterviewPracticeWorkspace focus={ownerFocus} />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    const response = container?.querySelector('textarea[aria-label="练习回答"]') as HTMLTextAreaElement;
    act(() => setTextArea(response, '保留的原回答'));
    act(() => [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent?.includes('更清楚了'))?.click());
    await act(async () => {
      [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent === '完成本次练习')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    const first = service.complete.mock.calls[0][1];
    expect((container?.querySelector('textarea[aria-label="练习回答"]') as HTMLTextAreaElement).disabled).toBe(true);
    await act(async () => {
      [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent === '使用原操作重试')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(service.complete.mock.calls[1][1]).toEqual(first);
  });

  it('fails closed without an explicit target and does not query a recommendation list', async () => {
    act(() => root?.render(<AdaptiveInterviewPracticeWorkspace />));
    await act(async () => { await Promise.resolve(); });
    expect(container?.textContent).toContain('明确的目标面试');
    expect(service.focus).not.toHaveBeenCalled();
    expect(service.start).not.toHaveBeenCalled();
  });

  it('keeps a legacy in-progress plan readable and completable without a V2 focus', async () => {
    service.plans.mockResolvedValue([legacyPlan]);
    act(() => root?.render(<AdaptiveInterviewPracticeWorkspace />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(container?.textContent).toContain('历史 V1 练习');
    const answer = container?.querySelector<HTMLTextAreaElement>('textarea[aria-label="练习回答"]');
    expect(answer).not.toBeNull();
    act(() => setTextArea(answer!, '继续完成旧练习'));
    act(() => [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent?.includes('更清楚了'))?.click());
    await act(async () => {
      [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent === '完成本次练习')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(service.complete.mock.calls[0]?.[1].idempotency_key).toMatch(/^adaptive-practice-complete-/);
    expect(service.focus).not.toHaveBeenCalled();
  });

  it('shows and completes only the exact V2 pair when unrelated in-progress plans also exist', async () => {
    const secondExactPlan = { ...plan, id: 9, title: '同一精确目标的另一组练习' };
    service.plans.mockResolvedValue([plan, secondExactPlan, legacyPlan]);
    act(() => root?.render(<AdaptiveInterviewPracticeWorkspace focus={ownerFocus} />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(container?.textContent).not.toContain('历史 V1 练习');
    expect(container?.textContent).not.toContain('继续历史练习');

    act(() => [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent === '继续这组练习')?.click());
    expect(container?.textContent).toContain('同一精确目标的另一组练习');

    const response = container?.querySelector('textarea[aria-label="练习回答"]') as HTMLTextAreaElement;
    act(() => setTextArea(response, '只完成精确目标练习'));
    act(() => [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent?.includes('更清楚了'))?.click());
    await act(async () => {
      [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent === '完成本次练习')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(service.complete).toHaveBeenCalledTimes(1);
    expect(service.complete).toHaveBeenCalledWith(9, expect.objectContaining({ response_text: '只完成精确目标练习' }));
  });

  it('never activates an unrelated in-progress plan when an explicit V2 pair has no exact plan', async () => {
    service.plans.mockResolvedValue([legacyPlan]);
    act(() => root?.render(<AdaptiveInterviewPracticeWorkspace focus={ownerFocus} />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    expect(container?.textContent).not.toContain('历史 V1 练习');
    expect(container?.textContent).not.toContain('继续历史练习');
    expect(container?.querySelector('textarea[aria-label="练习回答"]')).toBeNull();
    expect(container?.textContent).not.toContain('练习进行中');
    expect(container?.textContent).toContain('确认这组重点与目标');
  });

  it('persists an unknown V2 start across remount with the exact same bare key and body', async () => {
    service.start.mockRejectedValueOnce(new Error('network')).mockResolvedValueOnce(plan);
    let drafts: Record<string, unknown> = {};
    const render = () => root?.render(<ControlledAdaptiveInterviewPracticeWorkspace
      focus={ownerFocus} ownerGeneration={7} drafts={drafts}
      onDraftChange={(key: string, draft: unknown) => { drafts = draft ? { ...drafts, [key]: draft } : drafts; render(); }}
    />);
    act(render);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    act(() => [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent?.includes('确认这组重点与目标'))?.click());
    await act(async () => {
      [...document.body.querySelectorAll('button')].find((button) => button.textContent === '确认开始')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    const first = service.start.mock.calls[0]?.[0];
    expect(Object.values(drafts)[0]).toMatchObject({ resultUnknown: true, pendingOperation: 'start', startInput: first });
    act(() => root?.unmount());
    root = createRoot(container!);
    act(render);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    await act(async () => {
      [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent === '使用原操作重试')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(service.start.mock.calls[1]?.[0]).toEqual(first);
  });

  it('recovers the exact unknown V2 start after a real controller close and relaunch', async () => {
    const controller = createCoreTaskSurfaceController();
    const first = controller.launch({ ref: { taskId: 'interview.free_practice' }, source: 'application_task_card', focus: 'source', childOwnerIdentity: '91:103' });
    if (first.kind !== 'launched') throw new Error('practice launch should succeed');
    controller.markOpen(first.generation);
    controller.close(first.generation, 'preserve');
    controller.markClosed(first.generation);
    const ordinary = controller.launch({ ref: { taskId: 'interview.free_practice' }, source: 'deep_link', focus: 'current', childOwnerIdentity: 'three-mode' });
    if (ordinary.kind !== 'launched') throw new Error('ordinary practice should launch');
    expect(controller.getState().active?.recoveryGeneration).toBeNull();
    controller.close(ordinary.generation);
    controller.markClosed(ordinary.generation);
    const other = controller.launch({ ref: { taskId: 'interview.free_practice' }, source: 'application_task_card', focus: 'source', childOwnerIdentity: '92:104' });
    if (other.kind !== 'launched') throw new Error('other exact practice should launch');
    expect(controller.getState().active?.recoveryGeneration).toBeNull();
    controller.close(other.generation);
    controller.markClosed(other.generation);
    const reopened = controller.launch({ ref: { taskId: 'interview.free_practice' }, source: 'application_task_card', focus: 'source', childOwnerIdentity: '91:103' });
    if (reopened.kind !== 'launched') throw new Error('practice relaunch should succeed');
    const originalInput = {
      readiness_signal_version_id: 91,
      target_application_event_id: 103,
      expected_source_fingerprint: exactFocus.practiceSourceFingerprint,
      expected_target_fingerprint: exactFocus.practiceTargetFingerprint,
      idempotency_key: '00000000-0000-4000-8000-000000000099',
    };
    let drafts: Record<string, AdaptivePracticeOwnerDraft> = {
      [`practice:${first.generation}:91:103:new`]: {
        ownerKey: `practice:${first.generation}:91:103:new`, ownerGeneration: first.generation,
        signalVersionId: 91, targetEventId: 103, planId: null, answer: '', reflection: '', assessment: null,
        startInput: originalInput, completionInput: null, resultUnknown: true, pendingOperation: 'start',
      },
    };
    const render = () => root?.render(<ControlledAdaptiveInterviewPracticeWorkspace
      focus={{ ownerGeneration: reopened.generation, signalVersionId: 91, targetEventId: 103 }}
      ownerGeneration={reopened.generation}
      recoveryOwnerGeneration={controller.getState().active?.recoveryGeneration}
      drafts={drafts}
      onDraftChange={(key: string, draft: AdaptivePracticeOwnerDraft | null, retireOwnerKey?: string) => {
        const next = { ...drafts };
        if (retireOwnerKey) delete next[retireOwnerKey];
        if (draft) next[key] = draft; else delete next[key];
        drafts = next;
        render();
        return true;
      }}
    />);
    vi.spyOn(globalThis.crypto, 'randomUUID').mockReturnValue('00000000-0000-4000-8000-000000000100');
    act(render);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    await act(async () => {
      [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent === '使用原操作重试')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(reopened.generation).toBe(first.generation + 3);
    expect(controller.getState().active?.recoveryGeneration).toBe(first.generation);
    expect(service.start).toHaveBeenCalledWith(originalInput);
    expect(globalThis.crypto.randomUUID).not.toHaveBeenCalled();
    expect(drafts[`practice:${reopened.generation}:91:103:new`]).toBeUndefined();
    expect(drafts[`practice:${first.generation}:91:103:new`]).toBeUndefined();
    expect(drafts).toEqual({});
    expect(JSON.stringify(drafts)).not.toContain(originalInput.idempotency_key);
    expect(JSON.stringify(drafts)).not.toContain(originalInput.expected_source_fingerprint);
  });

  it('keeps the exact old Practice unknown draft when atomic migration installation throws', async () => {
    const oldKey = 'practice:4:91:103:new';
    const oldDraft: AdaptivePracticeOwnerDraft = {
      ownerKey: oldKey, ownerGeneration: 4, signalVersionId: 91, targetEventId: 103, planId: null,
      answer: '', reflection: '', assessment: null,
      startInput: { readiness_signal_version_id: 91, target_application_event_id: 103, expected_source_fingerprint: 'source', expected_target_fingerprint: 'target', idempotency_key: 'uuid-old' },
      completionInput: null, resultUnknown: true, pendingOperation: 'start',
    };
    let drafts: Record<string, AdaptivePracticeOwnerDraft> = { [oldKey]: oldDraft };
    const install = vi.fn(() => { throw new Error('storage aborted'); });
    act(() => root?.render(<ControlledAdaptiveInterviewPracticeWorkspace
      focus={{ ownerGeneration: 5, signalVersionId: 91, targetEventId: 103 }} ownerGeneration={5}
      recoveryOwnerGeneration={4} drafts={drafts} onDraftChange={install}
    />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(install).toHaveBeenCalledWith('practice:5:91:103:new', expect.objectContaining({ ownerGeneration: 5 }), oldKey);
    expect(drafts).toEqual({ [oldKey]: oldDraft });
    await act(async () => {
      [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent === '使用原操作重试')?.click();
      await Promise.resolve();
    });
    expect(service.start).not.toHaveBeenCalled();
    expect(drafts).toEqual({ [oldKey]: oldDraft });
    const retryInstall = (key: string, draft: AdaptivePracticeOwnerDraft | null, retireOwnerKey?: string) => {
      if (!draft) return false;
      const next = { ...drafts, [key]: draft };
      if (retireOwnerKey) delete next[retireOwnerKey];
      drafts = next;
      return true;
    };
    act(() => root?.render(<ControlledAdaptiveInterviewPracticeWorkspace
      focus={{ ownerGeneration: 5, signalVersionId: 91, targetEventId: 103 }} ownerGeneration={5}
      recoveryOwnerGeneration={4} drafts={{ [oldKey]: oldDraft }} onDraftChange={retryInstall}
    />));
    await act(async () => { await Promise.resolve(); });
    expect(drafts[oldKey]).toBeUndefined();
    expect(drafts['practice:5:91:103:new']).toMatchObject({ startInput: oldDraft.startInput });
  });

  it('does not recover a pending practice draft for a different exact Signal/Event owner', async () => {
    const controller = createCoreTaskSurfaceController();
    const first = controller.launch({ ref: { taskId: 'interview.free_practice' }, source: 'application_task_card' });
    if (first.kind !== 'launched') throw new Error('practice launch should succeed');
    controller.close(first.generation, 'preserve');
    controller.markClosed(first.generation);
    const reopened = controller.launch({ ref: { taskId: 'interview.free_practice' }, source: 'application_task_card' });
    if (reopened.kind !== 'launched') throw new Error('practice relaunch should succeed');
    const oldDraft = {
      ownerKey: `practice:${first.generation}:91:103:new`, ownerGeneration: first.generation,
      signalVersionId: 91, targetEventId: 103, planId: null, answer: '', reflection: '', assessment: null,
      startInput: { readiness_signal_version_id: 91, target_application_event_id: 103, expected_source_fingerprint: 'source', expected_target_fingerprint: 'target', idempotency_key: 'old-key' },
      completionInput: null, resultUnknown: true, pendingOperation: 'start' as const,
    };
    act(() => root?.render(<AdaptiveInterviewPracticeWorkspace
      focus={{ ownerGeneration: reopened.generation, signalVersionId: 92, targetEventId: 104 }}
      ownerGeneration={reopened.generation}
      recoveryOwnerGeneration={controller.getState().active?.recoveryGeneration}
      drafts={{ [oldDraft.ownerKey]: oldDraft }}
    />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(container?.textContent).not.toContain('使用原操作重试');
    expect(service.start).not.toHaveBeenCalled();
  });

  it('recovers exact unsaved Practice answer and reflection after close and relaunch', async () => {
    service.plans.mockResolvedValue([plan]);
    const controller = createCoreTaskSurfaceController();
    const request = { ref: { taskId: 'interview.free_practice' as const }, source: 'application_task_card' as const, childOwnerIdentity: '91:103' };
    const first = controller.launch(request);
    if (first.kind !== 'launched') throw new Error('practice launch should succeed');
    controller.close(first.generation, 'preserve'); controller.markClosed(first.generation);
    const reopened = controller.launch(request);
    if (reopened.kind !== 'launched') throw new Error('practice relaunch should succeed');
    const oldKey = `practice:${first.generation}:91:103:plan:${plan.id}`;
    let drafts: Record<string, AdaptivePracticeOwnerDraft> = {
      [oldKey]: {
        ownerKey: oldKey, ownerGeneration: first.generation, signalVersionId: 91, targetEventId: 103,
        planId: plan.id, answer: '尚未提交的回答', reflection: '尚未提交的复盘', assessment: 'clearer',
        startInput: null, completionInput: null, resultUnknown: false, pendingOperation: null,
      },
    };
    const render = () => root?.render(<ControlledAdaptiveInterviewPracticeWorkspace
      focus={{ ownerGeneration: reopened.generation, signalVersionId: 91, targetEventId: 103 }}
      ownerGeneration={reopened.generation} recoveryOwnerGeneration={controller.getState().active?.recoveryGeneration}
      drafts={drafts} onDraftChange={(key: string, draft: AdaptivePracticeOwnerDraft | null, retireOwnerKey?: string) => {
        const next = { ...drafts };
        if (retireOwnerKey) delete next[retireOwnerKey];
        if (draft) next[key] = draft; else delete next[key];
        drafts = next; render(); return true;
      }}
    />);
    act(render);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(container?.querySelector<HTMLTextAreaElement>('textarea[aria-label="练习回答"]')?.value).toBe('尚未提交的回答');
    expect(container?.querySelector<HTMLTextAreaElement>('textarea[aria-label="练习复盘"]')?.value).toBe('尚未提交的复盘');
    expect(drafts[`practice:${reopened.generation}:91:103:plan:${plan.id}`]).toMatchObject({
      answer: '尚未提交的回答', reflection: '尚未提交的复盘', assessment: 'clearer',
    });
    expect(drafts[oldKey]).toBeUndefined();
    expect(Object.keys(drafts)).toHaveLength(1);
  });

  it('does not recover a settled practice draft from the exact previous generation', async () => {
    const controller = createCoreTaskSurfaceController();
    const first = controller.launch({ ref: { taskId: 'interview.free_practice' }, source: 'application_task_card' });
    if (first.kind !== 'launched') throw new Error('practice launch should succeed');
    controller.close(first.generation);
    controller.markClosed(first.generation);
    const reopened = controller.launch({ ref: { taskId: 'interview.free_practice' }, source: 'application_task_card' });
    if (reopened.kind !== 'launched') throw new Error('practice relaunch should succeed');
    const settled = {
      ownerKey: `practice:${first.generation}:91:103:new`, ownerGeneration: first.generation,
      signalVersionId: 91, targetEventId: 103, planId: null, answer: '', reflection: '', assessment: null,
      startInput: { readiness_signal_version_id: 91, target_application_event_id: 103, expected_source_fingerprint: 'source', expected_target_fingerprint: 'target', idempotency_key: 'settled-key' },
      completionInput: null, resultUnknown: false, pendingOperation: null,
    };
    act(() => root?.render(<AdaptiveInterviewPracticeWorkspace
      focus={{ ownerGeneration: reopened.generation, signalVersionId: 91, targetEventId: 103 }}
      ownerGeneration={reopened.generation}
      recoveryOwnerGeneration={controller.getState().active?.recoveryGeneration}
      drafts={{ [settled.ownerKey]: settled }}
    />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(container?.textContent).not.toContain('使用原操作重试');
    expect(container?.textContent).toContain('确认这组重点与目标');
  });

  it('persists unknown completion answer and exact request across remount by owner pair and plan', async () => {
    service.plans.mockResolvedValue([plan]);
    service.complete.mockRejectedValueOnce(new Error('network')).mockResolvedValueOnce({ ...plan, status: 'completed', practice_state: 'completed' });
    let drafts: Record<string, unknown> = {};
    const render = () => root?.render(<ControlledAdaptiveInterviewPracticeWorkspace
      focus={ownerFocus} ownerGeneration={7} drafts={drafts}
      onDraftChange={(key: string, draft: unknown) => { drafts = draft ? { ...drafts, [key]: draft } : drafts; render(); }}
    />);
    act(render);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    const response = container?.querySelector<HTMLTextAreaElement>('textarea[aria-label="练习回答"]')!;
    act(() => setTextArea(response, '必须保留的回答'));
    act(() => [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent?.includes('更清楚了'))?.click());
    await act(async () => {
      [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent === '完成本次练习')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    const first = service.complete.mock.calls[0]?.[1];
    expect(Object.values(drafts).some((draft) => (draft as { completionInput?: unknown }).completionInput)).toBe(true);
    act(() => root?.unmount());
    root = createRoot(container!);
    act(render);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(container?.querySelector<HTMLTextAreaElement>('textarea[aria-label="练习回答"]')?.value).toBe('必须保留的回答');
    await act(async () => {
      [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent === '使用原操作重试')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(service.complete.mock.calls[1]?.[1]).toEqual(first);
  });

  it('ignores a late load from an older owner generation and exact pair', async () => {
    let resolveOld!: (value: typeof exactFocus) => void;
    service.focus
      .mockReturnValueOnce(new Promise((resolve) => { resolveOld = resolve; }))
      .mockResolvedValueOnce({ ...exactFocus, versionId: 92, targetEventId: 104, title: '新 owner 的重点' });
    act(() => root?.render(<AdaptiveInterviewPracticeWorkspace focus={ownerFocus} />));
    act(() => root?.render(<AdaptiveInterviewPracticeWorkspace focus={{ ownerGeneration: 8, signalVersionId: 92, targetEventId: 104 }} />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(container?.textContent).toContain('新 owner 的重点');
    await act(async () => { resolveOld(exactFocus); await Promise.resolve(); await Promise.resolve(); });
    expect(container?.textContent).toContain('新 owner 的重点');
    expect(container?.querySelector('[data-practice-owner-scope="7:91:103"]')).toBeNull();
  });

  it('does not let a late start response mutate a replacement owner scope', async () => {
    let resolveStart!: (value: typeof plan) => void;
    service.start.mockReturnValueOnce(new Promise((resolve) => { resolveStart = resolve; }));
    service.focus
      .mockResolvedValueOnce(exactFocus)
      .mockResolvedValueOnce({ ...exactFocus, versionId: 92, targetEventId: 104, title: '替换后的重点' });
    act(() => root?.render(<AdaptiveInterviewPracticeWorkspace focus={ownerFocus} />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    act(() => [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent?.includes('确认这组重点与目标'))?.click());
    act(() => [...document.body.querySelectorAll('button')].find((button) => button.textContent === '确认开始')?.click());
    act(() => root?.render(<AdaptiveInterviewPracticeWorkspace focus={{ ownerGeneration: 8, signalVersionId: 92, targetEventId: 104 }} />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    await act(async () => { resolveStart(plan); await Promise.resolve(); await Promise.resolve(); });
    expect(container?.textContent).toContain('替换后的重点');
    expect(container?.textContent).not.toContain('精确目标练习');
  });
});
