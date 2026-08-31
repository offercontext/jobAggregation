// @vitest-environment jsdom
import { act, useState } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { InterviewPreparationDraft } from './InterviewPreparationProposalDrawer';

const service = vi.hoisted(() => {
  class InterviewPreparationProposalError extends Error {
    constructor(public status: number, public code: string | null, message = '') {
      super(message);
    }
  }
  return { create: vi.fn(), list: vi.fn(), InterviewPreparationProposalError };
});
const readinessService = vi.hoisted(() => ({ advisory: vi.fn() }));
vi.mock('@/services/interviewPreparationProposals', () => ({
  createInterviewPreparationProposal: service.create,
  listInterviewPreparationProposals: service.list,
  InterviewPreparationProposalError: service.InterviewPreparationProposalError,
}));
vi.mock('@/features/reviewReadiness/service', () => ({ getEventReadinessFeedback: readinessService.advisory }));

const { default: InterviewPreparationProposalDrawer } = await import('./InterviewPreparationProposalDrawer');

declare global { var IS_REACT_ACT_ENVIRONMENT: boolean | undefined; }
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const context = {
  applicationId: 7,
  eventId: 11,
  resumeId: 13,
  jdText: 'Build reliable services.',
  jdVersionId: 1,
  knowledgeSelections: [],
  userAssertions: ['I led a migration.'],
};

let root: Root | undefined;
let container: HTMLDivElement | undefined;

beforeEach(() => {
  service.create.mockReset();
  service.list.mockReset();
  service.list.mockResolvedValue([]);
  readinessService.advisory.mockReset().mockRejectedValue(new Error('unavailable'));
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  vi.spyOn(globalThis.crypto, 'randomUUID').mockReturnValue('00000000-0000-0000-0000-000000000001');
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
  vi.restoreAllMocks();
});

describe('InterviewPreparationProposalDrawer interaction', () => {
  function deferred<T>() {
    let resolve!: (value: T) => void;
    let reject!: (reason?: unknown) => void;
    const promise = new Promise<T>((resolvePromise, rejectPromise) => {
      resolve = resolvePromise;
      reject = rejectPromise;
    });
    return { promise, resolve, reject };
  }

  function proposalResult() {
    return {
      id: 20,
      application_id: 7,
      event_id: 11,
      resume_id: 13,
      attempt_status: 'ready',
      proposal_status: 'safe_empty',
      source_status: 'current',
      source_states: { jd: 'not_checked' },
      proposal: {
        preparation_directions: [],
        story_prompts: [],
        review_points: [],
        interviewer_questions: [],
        items_to_clarify: [],
      },
    };
  }

  it('records a pending attempt before the generation request settles', async () => {
    const request = deferred<ReturnType<typeof proposalResult>>();
    service.create.mockReturnValue(request.promise);
    const attemptChanges: Array<{ key: string; result_unknown: boolean } | null> = [];

    act(() => root?.render(
      <InterviewPreparationProposalDrawer
        open
        context={context}
        onClose={() => {}}
        onAttemptStateChange={(state) => attemptChanges.push(state)}
      />,
    ));
    await act(async () => {
      container?.querySelector<HTMLButtonElement>('[data-testid="interview-preparation-generate"]')?.click();
      await Promise.resolve();
    });

    expect(attemptChanges[0]).toEqual({ key: '00000000-0000-0000-0000-000000000001', result_unknown: false });
    expect(service.create).toHaveBeenCalledTimes(1);
    expect(Object.prototype.hasOwnProperty.call(service.create.mock.calls[0]?.[0], 'readiness_feedback_version_ids')).toBe(false);
    await act(async () => {
      request.resolve(proposalResult());
      await request.promise;
    });
  });

  it('sends explicit empty V2 selection only after the advisory contract loads', async () => {
    readinessService.advisory.mockResolvedValue({ schema_version: 1, application_id: 7, event_id: 11, items: [] });
    service.create.mockResolvedValue(proposalResult());
    act(() => root?.render(<InterviewPreparationProposalDrawer open context={context} onClose={() => {}} />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); });
    await act(async () => {
      container?.querySelector<HTMLButtonElement>('[data-testid="interview-preparation-generate"]')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(service.create.mock.calls[0]?.[0]).toEqual(expect.objectContaining({
      readiness_feedback_version_ids: [],
    }));
  });

  it('discloses every selected readiness-feedback field sent to the AI', async () => {
    readinessService.advisory.mockResolvedValue({
      schema_version: 1,
      application_id: 7,
      event_id: 11,
      items: [{
        signalId: 4,
        versionId: 91,
        practiceSourceFingerprint: `sha256:${'a'.repeat(64)}`,
        practiceTargetFingerprint: `sha256:${'b'.repeat(64)}`,
        state: 'available',
        practiceState: 'not_started',
        selected: false,
        title: '准备重点',
        sourceLabel: '第 1 轮面试复盘',
      }],
    });
    service.create.mockResolvedValue(proposalResult());
    act(() => root?.render(<InterviewPreparationProposalDrawer open context={context} onClose={() => {}} />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); });
    act(() => container?.querySelector<HTMLInputElement>('[aria-label="已确认的复盘准备重点"] input[type="checkbox"]')?.click());

    expect(container?.textContent).toContain('所选复盘准备重点');
    expect(container?.textContent).toContain('用户备注');
    expect(container?.textContent).toContain('证据片段');
    expect(container?.textContent).toContain('来源轮次/类型');

    await act(async () => {
      container?.querySelector<HTMLButtonElement>('[data-testid="interview-preparation-generate"]')?.click();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('所选复盘准备重点'));
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('用户备注'));
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('证据片段'));
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('来源轮次/类型'));
    expect(service.create).toHaveBeenCalledWith(expect.objectContaining({
      readiness_feedback_version_ids: [91],
    }));
  });

  it('keeps the original V1 omission when the advisory contract arrives during an unknown request', async () => {
    const advisory = deferred<{ schema_version: 1; application_id: number; event_id: number; items: [] }>();
    const firstRequest = deferred<ReturnType<typeof proposalResult>>();
    readinessService.advisory.mockReturnValue(advisory.promise);
    service.create.mockReturnValueOnce(firstRequest.promise).mockResolvedValueOnce(proposalResult());

    function Harness() {
      const [draft, setDraft] = useState<InterviewPreparationDraft | null>(null);
      const [attempt, setAttempt] = useState<{ key: string; result_unknown: boolean } | null>(null);
      return <InterviewPreparationProposalDrawer
        open
        context={context}
        draft={draft ?? undefined}
        attemptState={attempt ?? undefined}
        onDraftChange={setDraft}
        onAttemptStateChange={setAttempt}
        onClose={() => {}}
      />;
    }

    act(() => root?.render(<Harness />));
    await act(async () => {
      container?.querySelector<HTMLButtonElement>('[data-testid="interview-preparation-generate"]')?.click();
      await Promise.resolve();
    });
    expect(Object.prototype.hasOwnProperty.call(service.create.mock.calls[0]?.[0], 'readiness_feedback_version_ids')).toBe(false);

    await act(async () => {
      advisory.resolve({ schema_version: 1, application_id: 7, event_id: 11, items: [] });
      await advisory.promise;
      firstRequest.reject(new Error('network'));
      try { await firstRequest.promise; } catch { /* expected unknown result */ }
      await Promise.resolve();
    });
    expect(container?.textContent).toContain('使用原尝试重试');

    await act(async () => {
      container?.querySelector<HTMLButtonElement>('[data-testid="interview-preparation-generate"]')?.click();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(service.create).toHaveBeenCalledTimes(2);
    expect(Object.prototype.hasOwnProperty.call(service.create.mock.calls[1]?.[0], 'readiness_feedback_version_ids')).toBe(false);
    expect(service.create.mock.calls[1]?.[0].idempotency_key).toBe(service.create.mock.calls[0]?.[0].idempotency_key);
  });

  it('hands practice off with the exact Signal Version and target Event only', async () => {
    readinessService.advisory.mockResolvedValue({
      schema_version: 1, application_id: 7, event_id: 11,
      items: [{
        signalId: 4, versionId: 91,
        practiceSourceFingerprint: `sha256:${'a'.repeat(64)}`,
        practiceTargetFingerprint: `sha256:${'b'.repeat(64)}`,
        state: 'available', practiceState: 'not_started', selected: false,
        title: '准备重点', sourceLabel: '第 1 轮面试复盘',
      }],
    });
    const openPractice = vi.fn();
    act(() => root?.render(<InterviewPreparationProposalDrawer open context={context} onClose={() => {}} onOpenPractice={openPractice} />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); });
    act(() => [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent === '用这个重点练习')?.click());
    expect(openPractice).toHaveBeenCalledWith({ ownerGeneration: 1, signalVersionId: 91, targetEventId: 11 });
  });

  it('marks a busy request unknown when the drawer closes and ignores its late success', async () => {
    const request = deferred<ReturnType<typeof proposalResult>>();
    service.create.mockReturnValue(request.promise);
    const attemptChanges: Array<{ key: string; result_unknown: boolean } | null> = [];
    const draftChanges: Array<InterviewPreparationDraft | null> = [];
    const onClose = vi.fn();

    act(() => root?.render(
      <InterviewPreparationProposalDrawer
        open
        context={context}
        onClose={onClose}
        onAttemptStateChange={(state) => attemptChanges.push(state)}
        onDraftChange={(draft) => draftChanges.push(draft)}
      />,
    ));
    await act(async () => {
      container?.querySelector<HTMLButtonElement>('[data-testid="interview-preparation-generate"]')?.click();
      await Promise.resolve();
    });
    const key = attemptChanges[0]?.key;
    await act(async () => {
      [...(container?.querySelectorAll<HTMLButtonElement>('button') ?? [])]
        .find((button) => button.textContent === '关闭')
        ?.click();
      await Promise.resolve();
    });

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(attemptChanges).toEqual([
      { key, result_unknown: false },
      { key, result_unknown: true },
    ]);
    expect(draftChanges[draftChanges.length - 1]?.attemptState).toEqual({ key, result_unknown: true });
    request.resolve(proposalResult());
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(attemptChanges).toEqual([
      { key, result_unknown: false },
      { key, result_unknown: true },
    ]);
  });

  it('keeps generations isolated when an unmounted request resolves after a retry', async () => {
    const first = deferred<ReturnType<typeof proposalResult>>();
    const second = deferred<ReturnType<typeof proposalResult>>();
    service.create.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const attemptChanges: Array<{ key: string; result_unknown: boolean } | null> = [];
    const onClose = vi.fn();

    const props = {
      open: true,
      context,
      onClose,
      onAttemptStateChange: (state: { key: string; result_unknown: boolean } | null) => attemptChanges.push(state),
    };
    act(() => root?.render(<InterviewPreparationProposalDrawer {...props} />));
    await act(async () => {
      container?.querySelector<HTMLButtonElement>('[data-testid="interview-preparation-generate"]')?.click();
      await Promise.resolve();
    });
    const firstKey = attemptChanges[0]?.key;
    act(() => root?.unmount());

    root = createRoot(container!);
    act(() => root?.render(
      <InterviewPreparationProposalDrawer
        {...props}
        attemptState={{ key: firstKey!, result_unknown: true }}
        draft={{
          attemptState: { key: firstKey!, result_unknown: true },
          resumeId: context.resumeId,
          jdText: context.jdText,
          jdVersionId: context.jdVersionId,
          assertionsText: context.userAssertions.join('\n'),
          knowledgeSelections: [],
        }}
      />,
    ));
    await act(async () => {
      container?.querySelector<HTMLButtonElement>('[data-testid="interview-preparation-generate"]')?.click();
      await Promise.resolve();
    });
    expect(attemptChanges).toEqual([
      { key: firstKey, result_unknown: false },
      { key: firstKey, result_unknown: true },
      { key: firstKey, result_unknown: false },
    ]);

    second.resolve(proposalResult());
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(attemptChanges[attemptChanges.length - 1]).toBeNull();

    first.resolve(proposalResult());
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(attemptChanges[attemptChanges.length - 1]).toBeNull();
  });

  it('does not let an unmounted rejection clear a replacement generation', async () => {
    const first = deferred<ReturnType<typeof proposalResult>>();
    const second = deferred<ReturnType<typeof proposalResult>>();
    service.create.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const attemptChanges: Array<{ key: string; result_unknown: boolean } | null> = [];

    const props = {
      open: true,
      context,
      onClose: () => {},
      onAttemptStateChange: (state: { key: string; result_unknown: boolean } | null) => attemptChanges.push(state),
    };
    act(() => root?.render(<InterviewPreparationProposalDrawer {...props} />));
    await act(async () => {
      container?.querySelector<HTMLButtonElement>('[data-testid="interview-preparation-generate"]')?.click();
      await Promise.resolve();
    });
    const firstKey = attemptChanges[0]?.key;
    act(() => root?.unmount());

    root = createRoot(container!);
    act(() => root?.render(
      <InterviewPreparationProposalDrawer
        {...props}
        attemptState={{ key: firstKey!, result_unknown: true }}
      />,
    ));
    await act(async () => {
      container?.querySelector<HTMLButtonElement>('[data-testid="interview-preparation-generate"]')?.click();
      await Promise.resolve();
    });
    second.resolve(proposalResult());
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(attemptChanges[attemptChanges.length - 1]).toBeNull();

    first.reject(new Error('late rejection'));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(attemptChanges[attemptChanges.length - 1]).toBeNull();
  });

  it('requires explicit confirmation and displays a safe empty result in Chinese', async () => {
    service.create.mockResolvedValue({
      id: 20,
      application_id: 7,
      event_id: 11,
      resume_id: 13,
      attempt_status: 'ready',
      proposal_status: 'safe_empty',
      source_status: 'current',
      source_states: { jd: 'not_checked' },
      proposal: {
        preparation_directions: [],
        story_prompts: [],
        review_points: [],
        interviewer_questions: [],
        items_to_clarify: [],
      },
    });

    act(() => root?.render(<InterviewPreparationProposalDrawer open context={context} onClose={() => {}} />));
    expect(container?.querySelector('[data-testid="interview-preparation-source-panel"]')).not.toBeNull();
    expect(container?.querySelector('[data-testid="interview-preparation-resume-select"]')?.className).toContain('nativeControl');
    expect(container?.querySelector('[data-testid="interview-preparation-generate"]')?.className).toContain('nativeButtonPrimary');
    const generate = () => [...(container?.querySelectorAll('button') || [])]
      .find((button) => button.textContent === '生成面试准备建议') as HTMLButtonElement;

    expect(container?.textContent).toContain('仅 JD、所选简历和已确认 Knowledge Evidence 会发送给 AI');
    await act(async () => {
      generate().click();
      await Promise.resolve();
    });
    expect(service.create).toHaveBeenCalledWith(expect.objectContaining({
      application_id: 7,
      event_id: 11,
      resume_id: 13,
      jd_version_id: 1,
      user_assertions: ['I led a migration.'],
      idempotency_key: '00000000-0000-0000-0000-000000000001',
    }));
    expect(container?.textContent).toContain('暂无可验证的面试准备建议');
  });

  it('does not claim current sources before JD and resume are selected', async () => {
    const emptyContext = { ...context, resumeId: 0, jdText: '' };
    act(() => root?.render(<InterviewPreparationProposalDrawer open context={emptyContext} onClose={() => {}} />));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(container?.textContent).not.toContain('当前使用来源');
  });

  it('maps a provider error without exposing the original message', async () => {
    service.create.mockRejectedValue(new service.InterviewPreparationProposalError(502, 'interview_preparation_provider_error', 'API key secret'));
    act(() => root?.render(<InterviewPreparationProposalDrawer open context={context} onClose={() => {}} />));
    await act(async () => {
      [...(container?.querySelectorAll('button') || [])]
        .find((button) => button.textContent === '生成面试准备建议')
        ?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
      await Promise.resolve();
    });
    expect(container?.textContent).toContain('AI 服务暂不可用，请稍后重试');
    expect(container?.textContent).not.toContain('API key secret');
  });

  it('keeps the same attempt after a Provider 502 and remount', async () => {
    service.create.mockRejectedValueOnce(new service.InterviewPreparationProposalError(502, 'interview_preparation_provider_error'));
    const attemptChanges: Array<{ key: string; result_unknown: boolean } | null> = [];
    const draftChanges: unknown[] = [];
    const props = {
      open: true,
      context,
      onClose: () => {},
      onDraftChange: (draft: unknown) => draftChanges.push(draft),
      onAttemptStateChange: (state: { key: string; result_unknown: boolean } | null) => attemptChanges.push(state),
    };
    act(() => root?.render(<InterviewPreparationProposalDrawer {...props} />));
    await act(async () => {
      container?.querySelectorAll('button')[0]?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
      await Promise.resolve();
    });
    const unknownAttempt = attemptChanges.find((state) => state?.result_unknown);
    expect(unknownAttempt).toEqual({ key: expect.any(String), result_unknown: true });
    expect(draftChanges).not.toContain(null);
    act(() => root?.render(<InterviewPreparationProposalDrawer {...props} attemptState={unknownAttempt!} />));
    expect(container?.textContent).toContain('上次请求结果待确认，请使用原尝试重试');

    act(() => root?.unmount());
    service.create.mockResolvedValueOnce({
      id: 21,
      application_id: 7,
      event_id: 11,
      resume_id: 13,
      attempt_status: 'ready',
      proposal_status: 'safe_empty',
      source_status: 'current',
      source_states: { jd: 'not_checked' },
      proposal: {
        preparation_directions: [], story_prompts: [], review_points: [],
        interviewer_questions: [], items_to_clarify: [],
      },
    });
    const reusedDraft = {
      attemptState: unknownAttempt!,
      resumeId: 13,
      jdText: context.jdText,
      jdVersionId: context.jdVersionId,
      assertionsText: context.userAssertions.join('\n'),
      knowledgeSelections: [],
    };
    root = createRoot(container!);
    act(() => root?.render(
      <InterviewPreparationProposalDrawer
        {...props}
        attemptState={unknownAttempt!}
        draft={reusedDraft}
      />,
    ));
    await act(async () => {
      container?.querySelectorAll('button')[0]?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
      await Promise.resolve();
    });
    expect(service.create).toHaveBeenLastCalledWith(expect.objectContaining({
      idempotency_key: unknownAttempt!.key,
    }));
  });

  it('keeps inputs frozen when retry returns 202 generating and remounts with the same key', async () => {
    service.create.mockResolvedValue({
      attempt_status: 'generating',
      application_id: 7,
      event_id: 11,
      idempotency_key: 'unknown-attempt-0001',
      generation_revision: 1,
      retry_after_ms: 1000,
    });
    const attemptState = { key: 'unknown-attempt-0001', result_unknown: true };
    const attemptChanges: Array<{ key: string; result_unknown: boolean } | null> = [];
    const props = {
      open: true,
      context,
      attemptState,
      draft: {
        attemptState,
        resumeId: 13,
        jdText: context.jdText,
        jdVersionId: context.jdVersionId,
        assertionsText: context.userAssertions.join('\n'),
        knowledgeSelections: [],
      },
      onClose: () => {},
      onAttemptStateChange: (state: { key: string; result_unknown: boolean } | null) => attemptChanges.push(state),
    };
    act(() => root?.render(<InterviewPreparationProposalDrawer {...props} />));
    await act(async () => {
      container?.querySelectorAll('button')[0]?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
      await Promise.resolve();
    });
    expect(attemptChanges[attemptChanges.length - 1]).toEqual(attemptState);
    expect(Array.from(container?.querySelectorAll('select, textarea, input[type="checkbox"]') ?? [])
      .every((control) => (control as HTMLInputElement).disabled)).toBe(true);

    act(() => root?.unmount());
    root = createRoot(container!);
    act(() => root?.render(<InterviewPreparationProposalDrawer {...props} />));
    await act(async () => {
      container?.querySelectorAll('button')[0]?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
      await Promise.resolve();
    });
    expect(service.create).toHaveBeenLastCalledWith(expect.objectContaining({
      idempotency_key: attemptState.key,
    }));
    expect(Array.from(container?.querySelectorAll('select, textarea, input[type="checkbox"]') ?? [])
      .every((control) => (control as HTMLInputElement).disabled)).toBe(true);
  });

  it('clears the draft and attempt key after a definite validation failure', async () => {
    service.create.mockRejectedValue(new service.InterviewPreparationProposalError(422, 'interview_preparation_inputs_invalid'));
    const draftChanges: unknown[] = [];
    const attemptChanges: unknown[] = [];
    act(() => root?.render(
      <InterviewPreparationProposalDrawer
        open
        context={context}
        onClose={() => {}}
        onDraftChange={(draft) => draftChanges.push(draft)}
        onAttemptStateChange={(state) => attemptChanges.push(state)}
      />,
    ));
    await act(async () => {
      [...(container?.querySelectorAll('button') || [])]
        [0]
        ?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(attemptChanges).toContain(null);
    expect(draftChanges).toContain(null);
  });
});
