// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { InterviewStoryDraft } from './InterviewStoryDrawer';

const storyService = vi.hoisted(() => {
  class StoryError extends Error {
    constructor(
      public readonly status: number,
      public readonly code: string | null,
      public readonly attemptId: number | null = null,
      public readonly retryAfterMs: number | null = null,
    ) {
      super(code ?? 'interview_story_error');
    }
  }
  return { proposal: vi.fn(), getProposal: vi.fn(), nextAction: vi.fn(), confirm: vi.fn(), create: vi.fn(), createVersion: vi.fn(), candidates: vi.fn(), StoryError };
});
const noteService = vi.hoisted(() => ({ list: vi.fn() }));
const actionService = vi.hoisted(() => ({ decide: vi.fn(), state: vi.fn(), undo: vi.fn(), recover: vi.fn() }));
vi.mock('@/services/interviewStories', () => ({
  createInterviewStoryProposal: storyService.proposal,
  getInterviewStoryProposal: storyService.getProposal,
  createInterviewStoryProductAction: storyService.nextAction,
  confirmInterviewStoryProposal: storyService.confirm,
  createInterviewStory: storyService.create,
  createInterviewStoryVersion: storyService.createVersion,
  listInterviewStorySourceCandidates: storyService.candidates,
  InterviewStoryError: storyService.StoryError,
}));
vi.mock('@/services/notes', () => ({ listNotes: noteService.list }));
vi.mock('@/features/reviewReadiness/service', () => ({
  decideProductAction: actionService.decide,
  getProductActionState: actionService.state,
  undoInterviewStory: actionService.undo,
  recoverRejectionControl: actionService.recover,
}));

const { default: InterviewStoryDrawer, createInterviewStoryDraft } = await import('./InterviewStoryDrawer');

let root: Root | undefined;
let container: HTMLDivElement | undefined;

function setControlValue(control: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement, value: string) {
  const prototype = control instanceof HTMLTextAreaElement
    ? HTMLTextAreaElement.prototype
    : control instanceof HTMLSelectElement
      ? HTMLSelectElement.prototype
      : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(prototype, 'value')?.set?.call(control, value);
  control.dispatchEvent(new Event(control instanceof HTMLSelectElement ? 'change' : 'input', { bubbles: true }));
}

function bindManualEvidence(target: string, source: string) {
  const control = document.body.querySelector(`select[data-testid="manual-evidence-${target}"]`) as HTMLSelectElement;
  expect(control).toBeTruthy();
  act(() => setControlValue(control, source));
}

function withServerStoryAction(
  draft: InterviewStoryDraft,
  proposal: NonNullable<InterviewStoryDraft['proposal']>,
  token = 'server-story-token',
): InterviewStoryDraft {
  const content = draft.editedContent ?? ('content' in proposal ? {
    title: proposal.content.title.text,
    blocks: proposal.content.blocks.map(({ kind, text, fact_mode }) => ({ kind, text, fact_mode })),
    capability_labels: [], applicable_questions: [], fact_gap_codes: [],
  } : draft.manualContent);
  return {
    ...draft,
    attemptGenerationRevision: 3,
    productActionGeneration: 1,
    serverConfirmationToken: token,
    productAction: {
      ownerKey: `story:${draft.attemptId}:3:1`, operationId: 'story-operation-1', actionCallId: 'story-call-1',
      actionName: 'confirm_interview_story', confirmationToken: token,
      allowedDecisions: ['approve', 'modify', 'reject'], status: 'proposed', result: null,
      originalPayload: {
        content,
        evidence_links: 'evidence_links' in proposal ? proposal.evidence_links.map((link) => ({
          target_kind: link.target_kind, target_id: link.target_id, source_kind: link.source_kind,
          source_id: link.source_stable_id, source_path: link.source_path, excerpt: link.excerpt,
        })) : [],
        expected_current_version_id: draft.expectedCurrentVersionId,
        expected_story_revision: draft.expectedStoryRevision,
      },
      pendingDecision: null, resultUnknown: false,
    },
  };
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: () => ({ matches: false, addListener: () => undefined, removeListener: () => undefined }),
  });
  const nativeGetComputedStyle = window.getComputedStyle.bind(window);
  vi.spyOn(window, 'getComputedStyle').mockImplementation((element) => nativeGetComputedStyle(element));
  storyService.proposal.mockReset();
  storyService.getProposal.mockReset();
  storyService.nextAction.mockReset();
  storyService.confirm.mockReset();
  storyService.create.mockReset();
  storyService.createVersion.mockReset();
  storyService.candidates.mockReset();
  noteService.list.mockReset();
  actionService.decide.mockReset();
  actionService.state.mockReset();
  actionService.undo.mockReset();
  actionService.recover.mockReset();
  storyService.candidates.mockResolvedValue({
    resumes: [{ id: 2, label: '筱哲的后端简历', leaves: [{ path: '/content_json/projects/0/detail', preview: '定位缓存击穿' }] }],
    interview_notes: [{ id: 4, label: '星云数据 · 后端工程师', leaves: [{ path: '/questions', preview: '如何排查延迟？' }] }],
    mock_turns: [{ attempt_id: 7, turn_no: 1, label: '模拟面试 #7 · 第 1 题', leaves: [{ path: '/turns/001/answer', preview: '我分段定位了延迟' }] }],
  });
  noteService.list.mockResolvedValue([{ id: 4, company: '星云数据', position: '后端工程师', questions: '如何排查延迟？', self_reflection: '', difficulty_points: '', mood: '', round: '', date: '', revision: 1, created_at: '' }]);
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('InterviewStoryDrawer', () => {
  it('requires selecting an original source and an explicit AI confirmation before proposal generation', async () => {
    const changes: unknown[] = [];
    let current = createInterviewStoryDraft('ui');
    const render = () => root?.render(<InterviewStoryDrawer open draft={current} onDraftChange={(draft) => {
      changes.push(draft);
      if (draft) {
        current = draft;
        render();
      }
    }} onClose={() => {}} />);
    act(render);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(document.body.textContent).toContain('先选择原始证据');
    expect(storyService.proposal).not.toHaveBeenCalled();

    act(() => [...document.body.querySelectorAll('button')].find((button) => button.textContent === '打开来源选择器')?.click());
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    const select = document.body.querySelector('input[type="checkbox"]') as HTMLInputElement;
    act(() => select?.click());
    const preview = [...document.body.querySelectorAll('button')].find((button) => button.textContent === '使用 AI 整理');
    act(() => preview?.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    expect(document.body.textContent).toContain('生成建议前请确认来源');
    expect(storyService.proposal).not.toHaveBeenCalled();
    expect(changes.length).toBeGreaterThan(0);
  });

  it('renders Resume, saved-review, and completed Mock sources only after the user opens the picker', async () => {
    let current = createInterviewStoryDraft('ui');
    const render = () => root?.render(<InterviewStoryDrawer open draft={current} onDraftChange={(draft) => {
      if (draft) {
        current = draft;
        render();
      }
    }} onClose={() => {}} />);
    act(render);
    expect(storyService.candidates).not.toHaveBeenCalled();

    act(() => [...document.body.querySelectorAll('button')].find((button) => button.textContent === '打开来源选择器')?.click());
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    expect(storyService.candidates).toHaveBeenCalledWith(undefined);
    expect(document.body.textContent).toContain('/content_json/projects/0/detail');
    expect(document.body.textContent).toContain('/questions');
    expect(document.body.textContent).toContain('/turns/001/answer');
  });

  it('creates a fresh idempotency key whenever the selected source input changes', async () => {
    let current = createInterviewStoryDraft('ui');
    const firstKey = current.idempotencyKey;
    const render = () => root?.render(<InterviewStoryDrawer open draft={current} onDraftChange={(draft) => {
      if (draft) {
        current = draft;
        render();
      }
    }} onClose={() => {}} />);
    act(render);
    act(() => [...document.body.querySelectorAll('button')].find((button) => button.textContent === '打开来源选择器')?.click());
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    act(() => (document.body.querySelector('input[type="checkbox"]') as HTMLInputElement).click());
    const selectedKey = current.idempotencyKey;
    expect(selectedKey).not.toBe(firstKey);

    act(() => (document.body.querySelector('input[type="checkbox"]') as HTMLInputElement).click());
    expect(current.idempotencyKey).not.toBe(selectedKey);
  });

  it('rotates the key and clears the pending Story state when a user assertion changes', async () => {
    let current = createInterviewStoryDraft('ui');
    const render = () => root?.render(<InterviewStoryDrawer open draft={current} onDraftChange={(draft) => {
      if (draft) {
        current = draft;
        render();
      }
    }} onClose={() => {}} />);
    act(render);
    const firstKey = current.idempotencyKey;
    current = {
      ...current,
      attemptId: 88,
      proposal: {
        proposal_status: 'safe_empty',
        content: { title: { id: 'title', text: '' }, blocks: [], capability_labels: [], applicable_questions: [], fact_gap_codes: [] },
        evidence_links: [],
      },
    };
    act(render);

    const assertion = document.body.querySelector('input[aria-label="用户明确原始陈述"]') as HTMLInputElement;
    act(() => setControlValue(assertion, '我本人负责了缓存排查。'));
    await act(async () => { await Promise.resolve(); });
    const addAssertion = assertion.parentElement?.querySelector('button') as HTMLButtonElement;
    expect(addAssertion).toBeTruthy();
    expect(addAssertion.disabled).toBe(false);
    act(() => addAssertion.click());

    expect(current.assertions).toEqual(['我本人负责了缓存排查。']);
    expect(current.idempotencyKey).not.toBe(firstKey);
    expect(current.attemptId).toBeNull();
    expect(current.proposal).toBeNull();
  });

  it('replays an unknown proposal with the same frozen input and idempotency key', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-08-12T00:00:00Z'));
    let current = createInterviewStoryDraft('ui');
    const render = () => root?.render(<InterviewStoryDrawer open draft={current} onDraftChange={(draft) => {
      if (draft) {
        current = draft;
        render();
      }
    }} onClose={() => {}} />);
    storyService.proposal
      .mockRejectedValueOnce(new storyService.StoryError(0, null))
      .mockResolvedValueOnce({
        id: 18,
        attempt_status: 'ready',
        generation_revision: 1,
        source_fingerprint: 'fingerprint',
        proposal: {
          proposal_status: 'normal',
          content: {
            title: { id: 'title', text: '排查延迟' },
            blocks: [{ id: 'situation_001', kind: 'situation', text: '服务延迟', fact_mode: 'evidence_backed' }],
            capability_labels: [],
            applicable_questions: [],
            fact_gap_codes: [],
          },
          evidence_links: [{
            target_kind: 'title', target_id: 'title', source_kind: 'interview_note',
            source_stable_id: '4', source_version_or_snapshot: '2026-08-10T00:00:00+00:00',
            source_path: '/questions', excerpt: '如何排查延迟', text_location: '',
          }, {
            target_kind: 'block', target_id: 'situation_001', source_kind: 'interview_note',
            source_stable_id: '4', source_version_or_snapshot: '2026-08-10T00:00:00+00:00',
            source_path: '/questions', excerpt: '如何排查延迟', text_location: '',
          }],
        },
      });

    act(render);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    act(() => [...document.body.querySelectorAll('button')].find((button) => button.textContent === '打开来源选择器')?.click());
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    act(() => (document.body.querySelector('input[type="checkbox"]') as HTMLInputElement).click());
    act(() => [...document.body.querySelectorAll('button')].find((button) => button.textContent === '使用 AI 整理')?.click());
    const checkboxes = [...document.body.querySelectorAll('input[type="checkbox"]')];
    const confirmation = checkboxes[checkboxes.length - 1] as HTMLInputElement;
    act(() => confirmation.click());
    await act(async () => {
      [...document.body.querySelectorAll('button')].find((button) => button.textContent === '生成故事建议')?.click();
      await Promise.resolve();
      await Promise.resolve();
    });
    const initialPayload = storyService.proposal.mock.calls[0]?.[0];
    expect(current.resultUnknown).toBe(true);
    expect(current.pendingOperation).toBe('generate');
    expect(current.attemptId).toBeNull();
    expect(document.body.textContent).toContain('使用原尝试重试');
    const earlyRetry = [...document.body.querySelectorAll('button')].find((button) => button.textContent === '使用原尝试重试') as HTMLButtonElement;
    expect(earlyRetry.disabled).toBe(true);
    act(() => earlyRetry.click());
    expect(storyService.proposal).toHaveBeenCalledTimes(1);

    act(() => root?.render(null));
    act(render);

    expect((([...document.body.querySelectorAll('button')].find((button) => button.textContent === '使用原尝试重试')) as HTMLButtonElement).disabled).toBe(true);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_250);
    });

    await act(async () => {
      [...document.body.querySelectorAll('button')].find((button) => button.textContent === '使用原尝试重试')?.click();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(storyService.proposal).toHaveBeenCalledTimes(2);
    expect(storyService.proposal.mock.calls[1]?.[0]).toEqual(initialPayload);
    expect(current.resultUnknown).toBe(false);
    expect(current.pendingOperation).toBeNull();
    vi.useRealTimers();
  });

  it.each([
    { label: 'the coded error Attempt', existingAttemptId: null, errorAttemptId: 73, expectedAttemptId: 73 },
    { label: 'the persisted draft Attempt', existingAttemptId: 52, errorAttemptId: null, expectedAttemptId: 52 },
  ])('preserves $label and the frozen proposal identity after a coded operation-result-unknown response', async ({
    existingAttemptId,
    errorAttemptId,
    expectedAttemptId,
  }) => {
    const frozenInput = {
      target_story_id: null,
      expected_current_version_id: null,
      expected_story_revision: null,
      selections: [{ source_kind: 'interview_note' as const, source_id: 4, path: '/questions' }],
      assertions: ['I personally owned this work.'],
      idempotency_key: 'story-unknown-operation-key',
    };
    let current: InterviewStoryDraft = {
      ...createInterviewStoryDraft('ui'),
      idempotencyKey: frozenInput.idempotency_key,
      attemptId: existingAttemptId,
      proposalInput: frozenInput,
      resultUnknown: true,
      retryAvailableAt: 0,
      pendingOperation: 'generate',
      error: 'AI 结果待确认，请使用原尝试重试。',
    };
    const render = () => root?.render(<InterviewStoryDrawer open draft={current} onDraftChange={(draft) => {
      if (draft) {
        current = draft;
        render();
      }
    }} onClose={() => {}} />);
    storyService.proposal
      .mockRejectedValueOnce(new storyService.StoryError(503, 'operation_result_unknown', errorAttemptId, 0))
      .mockResolvedValueOnce({
        id: expectedAttemptId,
        attempt_status: 'ready',
        generation_revision: 1,
        source_fingerprint: 'fingerprint',
        proposal: {
          proposal_status: 'normal',
          content: {
            title: { id: 'title', text: '排查延迟' },
            blocks: [],
            capability_labels: [],
            applicable_questions: [],
            fact_gap_codes: [],
          },
          evidence_links: [],
        },
      });

    act(render);
    await act(async () => {
      [...document.body.querySelectorAll('button')].find((button) => button.textContent === '使用原尝试重试')?.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(current.resultUnknown).toBe(true);
    expect(current.attemptId).toBe(expectedAttemptId);
    expect(current.proposalInput).toEqual(frozenInput);
    expect(current.idempotencyKey).toBe(frozenInput.idempotency_key);

    await act(async () => {
      [...document.body.querySelectorAll('button')].find((button) => button.textContent === '使用原尝试重试')?.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(storyService.proposal).toHaveBeenCalledTimes(2);
    expect(storyService.proposal.mock.calls[0]?.[0]).toEqual(frozenInput);
    expect(storyService.proposal.mock.calls[1]?.[0]).toEqual(frozenInput);
    expect(current.attemptId).toBe(expectedAttemptId);
    expect(current.resultUnknown).toBe(false);
  });

  it('replays an unknown confirmation with the original token and selected content', async () => {
    const proposal = {
      proposal_status: 'normal' as const,
      content: {
        title: { id: 'title' as const, text: '排查延迟' },
        blocks: [{ id: 'situation_001', kind: 'situation' as const, text: '服务延迟', fact_mode: 'evidence_backed' as const }],
        capability_labels: [], applicable_questions: [], fact_gap_codes: [],
      },
      evidence_links: [{
        target_kind: 'title' as const, target_id: 'title', source_kind: 'interview_note' as const,
        source_stable_id: '4', source_version_or_snapshot: 'snapshot', source_path: '/questions', excerpt: '如何排查延迟',
      }, {
        target_kind: 'block' as const, target_id: 'situation_001', source_kind: 'interview_note' as const,
        source_stable_id: '4', source_version_or_snapshot: 'snapshot', source_path: '/questions', excerpt: '如何排查延迟',
      }],
    };
    let current: InterviewStoryDraft = withServerStoryAction({ ...createInterviewStoryDraft('pilot'), attemptId: 33, proposal, editedContent: {
      title: '我编辑后的故事标题', blocks: proposal.content.blocks, capability_labels: [], applicable_questions: [], fact_gap_codes: [],
    } }, proposal);
    const render = () => root?.render(<InterviewStoryDrawer open draft={current} onDraftChange={(draft) => {
      if (draft) {
        current = draft;
        render();
      }
    }} onClose={() => {}} />);
    storyService.confirm
      .mockRejectedValueOnce(new storyService.StoryError(502, 'story_provider_error'))
      .mockResolvedValueOnce({ story_id: 8, version_id: 12, created: true });
    act(render);
    await act(async () => {
      [...document.body.querySelectorAll('button')].find((button) => button.textContent === '确认保存这个故事版本')?.click();
      await Promise.resolve();
      await Promise.resolve();
    });
    const initialToken = current.serverConfirmationToken;
    const initialPayload = storyService.confirm.mock.calls[0]?.[1];
    expect(initialToken).toBeTruthy();
    expect(current.productAction?.pendingDecision?.decision).toBe('approve');
    expect(current.productAction?.resultUnknown).toBe(true);
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    const retry = [...document.body.querySelectorAll('button')].find((button) => button.textContent === '使用原操作重试') as HTMLButtonElement;
    expect(retry).toBeTruthy();
    expect(retry.disabled).toBe(false);
    await act(async () => {
      retry.click();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(storyService.confirm).toHaveBeenCalledTimes(2);
    expect(storyService.confirm.mock.calls[1]?.[1]).toEqual(initialPayload);
  });

  it('preserves authored content and assertions while requiring a fresh Attempt after a canonical Story CAS failure', async () => {
    const proposal = {
      proposal_status: 'normal' as const,
      content: {
        title: { id: 'title' as const, text: '鎺掓煡寤惰繜' },
        blocks: [{ id: 'situation_001', kind: 'situation' as const, text: '鏈嶅姟寤惰繜', fact_mode: 'evidence_backed' as const }],
        capability_labels: [], applicable_questions: [], fact_gap_codes: [],
      },
      evidence_links: [{
        target_kind: 'title' as const, target_id: 'title', source_kind: 'interview_note' as const,
        source_stable_id: '4', source_version_or_snapshot: 'snapshot', source_path: '/questions', excerpt: '濡備綍鎺掓煡寤惰繜',
      }, {
        target_kind: 'block' as const, target_id: 'situation_001', source_kind: 'interview_note' as const,
        source_stable_id: '4', source_version_or_snapshot: 'snapshot', source_path: '/questions', excerpt: '濡備綍鎺掓煡寤惰繜',
      }],
    };
    const authoredTitle = 'Edited incident story';
    let current: InterviewStoryDraft = withServerStoryAction({
      ...createInterviewStoryDraft('ui'),
      attemptId: 33,
      proposal,
      selections: [{ source_kind: 'interview_note', source_id: 4, path: '/questions' }],
      assertions: ['I personally owned this work.'],
      editedContent: { title: authoredTitle, blocks: proposal.content.blocks, capability_labels: [], applicable_questions: [], fact_gap_codes: [] },
    }, proposal);
    const originalKey = current.idempotencyKey;
    const render = () => root?.render(<InterviewStoryDrawer open draft={current} onDraftChange={(draft) => {
      if (draft) {
        current = draft;
        render();
      }
    }} onClose={() => {}} />);
    storyService.confirm.mockRejectedValueOnce(new storyService.StoryError(409, 'product_action_story_write_conflict'));
    actionService.state.mockResolvedValueOnce({
      schema_version: 1,
      operation_id: 'story-operation-1',
      action_name: 'confirm_interview_story',
      status: 'failed',
      result: {
        schema_version: 1,
        action_name: 'confirm_interview_story',
        outcome: 'failed',
        code: 'product_action_story_write_conflict',
      },
    });

    act(render);
    await act(async () => {
      [...document.body.querySelectorAll('button')].find((button) => button.textContent === '确认保存这个故事版本')?.click();
      await Promise.resolve();
      await Promise.resolve();
    });
    await act(async () => {
      [...document.body.querySelectorAll('button')].find((button) => button.textContent === '确认操作结果')?.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(current.idempotencyKey).not.toBe(originalKey);
    expect(current.selections).toEqual([]);
    expect(current.attemptId).toBeNull();
    expect(current.proposal).toBeNull();
    expect(current.resultUnknown).toBe(false);
    expect(current.assertions).toEqual(['I personally owned this work.']);
    expect(current.manualContent.title).toBe(authoredTitle);
  });

  it('creates an explicit N+1 server confirmation only after Story rejection', async () => {
    const proposal = {
      proposal_status: 'normal' as const,
      content: {
        title: { id: 'title' as const, text: '排查延迟' },
        blocks: [{ id: 'situation_001', kind: 'situation' as const, text: '服务延迟', fact_mode: 'evidence_backed' as const }],
        capability_labels: [], applicable_questions: [], fact_gap_codes: [],
      },
      evidence_links: [],
    };
    let current = withServerStoryAction({
      ...createInterviewStoryDraft('ui'), attemptId: 33, proposal,
      editedContent: { title: '排查延迟', blocks: proposal.content.blocks, capability_labels: [], applicable_questions: [], fact_gap_codes: [] },
    }, proposal);
    const render = () => root?.render(<InterviewStoryDrawer open draft={current} onDraftChange={(draft) => {
      if (draft) { current = draft; render(); }
    }} onClose={() => {}} />);
    actionService.decide.mockResolvedValue({
      schema_version: 1, operation_id: 'story-operation-1', action_name: 'confirm_interview_story',
      status: 'rejected', result: {}, replayed: false, direct_commit: false,
    });
    storyService.nextAction.mockResolvedValue({
      schema_version: 1, contract: 'story_product_action_proposal_response_v1',
      operation_id: 'story-operation-2', action_call_id: 'story-call-2', product_action_generation: 2,
      status: 'proposed', proposal_created: true, confirmation_token: 'server-story-token-2',
    });
    act(render);
    await act(async () => {
      [...document.body.querySelectorAll('button')].find((button) => button.textContent === '暂不保存这个故事')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(current.productAction?.status).toBe('rejected');
    expect(current.serverConfirmationToken).toBeNull();
    expect(storyService.nextAction).not.toHaveBeenCalled();
    await act(async () => {
      [...document.body.querySelectorAll('button')].find((button) => button.textContent === '再次保存')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(storyService.nextAction).toHaveBeenCalledWith(33, {
      expected_generation_revision: 3,
      expected_product_action_generation: 1,
    });
    expect(current.productActionGeneration).toBe(2);
    expect(current.serverConfirmationToken).toBe('server-story-token-2');
  });

  it('recovers a lost N+1 response as a replayed proposed control without getting stuck', async () => {
    const proposal = {
      proposal_status: 'normal' as const,
      content: {
        title: { id: 'title' as const, text: '排查延迟' },
        blocks: [{ id: 'situation_001', kind: 'situation' as const, text: '服务延迟', fact_mode: 'evidence_backed' as const }],
        capability_labels: [], applicable_questions: [], fact_gap_codes: [],
      },
      evidence_links: [],
    };
    const proposed = withServerStoryAction({ ...createInterviewStoryDraft('ui'), attemptId: 33, proposal }, proposal);
    let current: InterviewStoryDraft = {
      ...proposed,
      serverConfirmationToken: null,
      productAction: proposed.productAction ? { ...proposed.productAction, confirmationToken: null, status: 'rejected', result: {} } : null,
    };
    const render = () => root?.render(<InterviewStoryDrawer open draft={current} onDraftChange={(draft) => {
      if (draft) { current = draft; render(); }
    }} onClose={() => {}} />);
    storyService.nextAction
      .mockRejectedValueOnce(new Error('response lost'))
      .mockResolvedValueOnce({
        schema_version: 1, contract: 'story_product_action_proposal_response_v1',
        operation_id: 'story-operation-2', action_call_id: 'story-call-2', product_action_generation: 2,
        status: 'proposed', proposal_created: false, confirmation_token: 'server-story-token-2',
      });
    act(render);
    const restart = () => [...document.body.querySelectorAll('button')].find((button) => button.textContent === '再次保存');
    await act(async () => { restart()?.click(); await Promise.resolve(); await Promise.resolve(); });
    expect(current.productAction?.status).toBe('rejected');
    expect(document.body.textContent).toContain('再次保存');
    await act(async () => { restart()?.click(); await Promise.resolve(); await Promise.resolve(); });
    expect(storyService.nextAction).toHaveBeenCalledTimes(2);
    expect(storyService.nextAction).toHaveBeenNthCalledWith(2, 33, {
      expected_generation_revision: 3,
      expected_product_action_generation: 1,
    });
    expect(current.productAction).toMatchObject({
      operationId: 'story-operation-2', status: 'proposed', confirmationToken: 'server-story-token-2',
    });
    expect(current.serverConfirmationToken).toBe('server-story-token-2');
  });

  it('installs a terminal N+1 replay with no token and preserves its safe Undo result', async () => {
    const proposal = {
      proposal_status: 'normal' as const,
      content: { title: { id: 'title' as const, text: '排查延迟' }, blocks: [], capability_labels: [], applicable_questions: [], fact_gap_codes: [] },
      evidence_links: [],
    };
    const proposed = withServerStoryAction({ ...createInterviewStoryDraft('ui'), attemptId: 33, proposal }, proposal);
    let current: InterviewStoryDraft = {
      ...proposed,
      serverConfirmationToken: null,
      productAction: proposed.productAction ? { ...proposed.productAction, confirmationToken: null, status: 'rejected', result: {} } : null,
    };
    const render = () => root?.render(<InterviewStoryDrawer open draft={current} onDraftChange={(draft) => {
      if (draft) { current = draft; render(); }
    }} onClose={() => {}} />);
    storyService.nextAction.mockResolvedValue({
      schema_version: 1, contract: 'story_product_action_proposal_response_v1',
      operation_id: 'story-operation-2', action_call_id: 'story-call-2', product_action_generation: 2,
      status: 'committed', proposal_created: false, terminal_result: { story_id: 8, version_id: 12 },
    });
    act(render);
    await act(async () => {
      [...document.body.querySelectorAll('button')].find((button) => button.textContent === '再次保存')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(current.productAction).toMatchObject({
      operationId: 'story-operation-2', status: 'committed', confirmationToken: null,
      result: { story_id: 8, version_id: 12 },
    });
    expect(current.serverConfirmationToken).toBeNull();
    expect(document.body.textContent).toContain('撤销本次保存');
  });

  it('does not invent terminal failed state for a coded pre-executor Story decision failure', async () => {
    const proposal = {
      proposal_status: 'normal' as const,
      content: { title: { id: 'title' as const, text: '排查延迟' }, blocks: [], capability_labels: [], applicable_questions: [], fact_gap_codes: [] },
      evidence_links: [],
    };
    let current = withServerStoryAction({ ...createInterviewStoryDraft('ui'), attemptId: 33, proposal }, proposal);
    const render = () => root?.render(<InterviewStoryDrawer open draft={current} onDraftChange={(draft) => {
      if (draft) { current = draft; render(); }
    }} onClose={() => {}} />);
    storyService.confirm.mockRejectedValue(new storyService.StoryError(409, 'story_source_conflict'));
    act(render);
    await act(async () => {
      [...document.body.querySelectorAll('button')].find((button) => button.textContent === '确认保存这个故事版本')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(current.productAction).toMatchObject({
      operationId: 'story-operation-1', status: 'proposed', confirmationToken: 'server-story-token', resultUnknown: true,
    });
    expect(current.serverConfirmationToken).toBe('server-story-token');
    expect(document.body.textContent).toContain('确认操作结果');
  });

  it('falls back to application-bound rejection-only Story recovery when the source owner is invalid', async () => {
    const proposal = {
      proposal_status: 'normal' as const,
      content: { title: { id: 'title' as const, text: '排查延迟' }, blocks: [], capability_labels: [], applicable_questions: [], fact_gap_codes: [] },
      evidence_links: [],
    };
    let current = withServerStoryAction({ ...createInterviewStoryDraft('ui', 4, { applicationId: 6 }), attemptId: 33, proposal }, proposal);
    const render = () => root?.render(<InterviewStoryDrawer open draft={current} onDraftChange={(draft) => {
      if (draft) { current = draft; render(); }
    }} onClose={() => {}} />);
    storyService.confirm.mockRejectedValue(new storyService.StoryError(409, 'story_source_conflict'));
    actionService.state.mockResolvedValue({ schema_version: 1, operation_id: 'story-operation-1', action_name: 'confirm_interview_story', status: 'proposed' });
    storyService.getProposal.mockRejectedValue(new storyService.StoryError(409, 'story_source_conflict'));
    actionService.recover.mockResolvedValue({
      schema_version: 1, operation_id: 'story-operation-1', action_call_id: 'rejection-call',
      action_name: 'confirm_interview_story', status: 'proposed', confirmation_token: 'rejection-token',
      allowed_decisions: ['reject'], rejection_only: true, live_source_state: 'not_observed',
    });
    act(render);
    await act(async () => {
      [...document.body.querySelectorAll('button')].find((button) => button.textContent === '确认保存这个故事版本')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    await act(async () => {
      [...document.body.querySelectorAll('button')].find((button) => button.textContent === '确认操作结果')?.click();
      await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
    });
    expect(actionService.recover).toHaveBeenCalledWith(6, 'story-operation-1');
    expect([...document.body.querySelectorAll('button')].find((button) => button.textContent === '确认保存这个故事版本')).toBeUndefined();
    expect((([...document.body.querySelectorAll('button')].find((button) => button.textContent === '暂不保存这个故事')) as HTMLButtonElement).disabled).toBe(false);
  });

  it('keeps Story Undo local to the committed owner and treats failed compensation as deterministic terminal', async () => {
    const proposal = {
      proposal_status: 'normal' as const,
      content: { title: { id: 'title' as const, text: '排查延迟' }, blocks: [], capability_labels: [], applicable_questions: [], fact_gap_codes: [] },
      evidence_links: [],
    };
    const proposed = withServerStoryAction({ ...createInterviewStoryDraft('ui'), attemptId: 33, proposal }, proposal);
    let current: InterviewStoryDraft = {
      ...proposed,
      serverConfirmationToken: null,
      productAction: proposed.productAction ? {
        ...proposed.productAction,
        confirmationToken: null,
        status: 'committed',
        result: { story_id: 8, version_id: 12 },
      } : null,
    };
    const render = () => root?.render(<InterviewStoryDrawer open draft={current} onDraftChange={(draft) => {
      if (draft) { current = draft; render(); }
    }} onClose={() => {}} />);
    actionService.undo.mockResolvedValue({
      schema_version: 1, operation_id: 'story-undo-operation', compensation_kind: 'undo:confirm_interview_story',
      status: 'failed', result: { reason: 'story_has_dependents' }, replayed: true,
    });
    act(render);
    await act(async () => {
      [...document.body.querySelectorAll('button')].find((button) => button.textContent === '撤销本次保存')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(actionService.undo).toHaveBeenCalledWith(8, 'story-operation-1');
    expect(document.body.textContent).toContain('撤销未完成');
    expect(document.body.textContent).not.toContain('已撤销本次保存');
    expect([...document.body.querySelectorAll('button')].find((button) => button.textContent === '撤销本次保存')).toBeUndefined();
    expect(current.productAction).toMatchObject({ undoStatus: 'failed', undoReplayed: true });
    act(render);
    expect(actionService.undo).toHaveBeenCalledTimes(1);
  });

  it('persists the exact Story Undo before transport and replays only its unknown request after remount', async () => {
    const proposal = {
      proposal_status: 'normal' as const,
      content: { title: { id: 'title' as const, text: '排查延迟' }, blocks: [], capability_labels: [], applicable_questions: [], fact_gap_codes: [] },
      evidence_links: [],
    };
    const proposed = withServerStoryAction({ ...createInterviewStoryDraft('ui'), attemptId: 33, proposal }, proposal);
    let current: InterviewStoryDraft = {
      ...proposed,
      serverConfirmationToken: null,
      productAction: proposed.productAction ? {
        ...proposed.productAction, confirmationToken: null, status: 'committed', result: { story_id: 8, version_id: 12 },
      } : null,
    };
    let allowPersistence = false;
    const render = () => root?.render(<InterviewStoryDrawer open draft={current} onDraftChange={(next) => {
      if (!allowPersistence) return false;
      if (next) { current = next; render(); }
      return true;
    }} onClose={() => {}} />);
    actionService.undo
      .mockRejectedValueOnce(new Error('response lost'))
      .mockResolvedValueOnce({
        schema_version: 1, operation_id: 'story-undo-operation', compensation_kind: 'undo:confirm_interview_story',
        status: 'committed', result: {}, replayed: true,
      });
    act(render);
    await act(async () => {
      [...document.body.querySelectorAll('button')].find((button) => button.textContent === '撤销本次保存')?.click();
      await Promise.resolve();
    });
    expect(actionService.undo).not.toHaveBeenCalled();

    allowPersistence = true;
    await act(async () => {
      [...document.body.querySelectorAll('button')].find((button) => button.textContent === '撤销本次保存')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(current.productAction).toMatchObject({
      undoRequest: {
        ownerKey: 'story:33:3:1', parentOperationId: 'story-operation-1', actionName: 'confirm_interview_story',
      },
      undoResultUnknown: true,
    });
    act(() => root?.unmount());
    root = createRoot(container!);
    act(render);
    expect(actionService.undo).toHaveBeenCalledTimes(1);
    await act(async () => {
      [...document.body.querySelectorAll('button')].find((button) => button.textContent === '使用原撤销操作重试')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(actionService.undo).toHaveBeenCalledTimes(2);
    expect(actionService.undo.mock.calls[0]).toEqual(actionService.undo.mock.calls[1]);
    expect(current.productAction).toMatchObject({ undoStatus: 'committed', undoRequest: null, undoResultUnknown: false });
  });

  it('routes a late Story Undo settlement into the remounted exact draft without duplicating transport', async () => {
    const proposal = {
      proposal_status: 'normal' as const,
      content: { title: { id: 'title' as const, text: '排查延迟' }, blocks: [], capability_labels: [], applicable_questions: [], fact_gap_codes: [] },
      evidence_links: [],
    };
    const proposed = withServerStoryAction({ ...createInterviewStoryDraft('ui'), attemptId: 33, proposal }, proposal);
    let current: InterviewStoryDraft = {
      ...proposed,
      serverConfirmationToken: null,
      productAction: proposed.productAction ? {
        ...proposed.productAction, confirmationToken: null, status: 'committed', result: { story_id: 8, version_id: 12 },
      } : null,
    };
    let settle!: (value: unknown) => void;
    actionService.undo.mockReturnValueOnce(new Promise((resolve) => { settle = resolve; }));
    const render = () => root?.render(<InterviewStoryDrawer open draft={current} onDraftChange={(next) => {
      if (next) { current = next; render(); }
      return true;
    }} onClose={() => {}} />);
    act(render);
    act(() => [...document.body.querySelectorAll('button')].find((button) => button.textContent === '撤销本次保存')?.click());
    expect(current.productAction).toMatchObject({ undoRequest: expect.any(Object), undoResultUnknown: false });
    act(() => root?.unmount());
    root = createRoot(container!);
    act(render);
    expect(actionService.undo).toHaveBeenCalledTimes(1);
    await act(async () => {
      settle({
        schema_version: 1, operation_id: 'story-undo-operation', compensation_kind: 'undo:confirm_interview_story',
        status: 'committed', result: {}, replayed: false,
      });
      await Promise.resolve(); await Promise.resolve();
    });
    expect(current.productAction).toMatchObject({ undoStatus: 'committed', undoRequest: null, undoResultUnknown: false });
    expect(actionService.undo).toHaveBeenCalledTimes(1);
    expect(document.body.textContent).toContain('已撤销本次保存');
  });

  it('freezes Story source, edit, and replacement controls while exact Undo is pending', async () => {
    const proposal = {
      proposal_status: 'normal' as const,
      content: {
        title: { id: 'title' as const, text: '排查延迟' },
        blocks: [{ id: 'situation' as const, kind: 'situation' as const, text: '我定位了缓存问题', fact_mode: 'evidence_backed' as const }],
        capability_labels: [], applicable_questions: [], fact_gap_codes: [],
      },
      evidence_links: [],
    };
    const proposed = withServerStoryAction({ ...createInterviewStoryDraft('ui'), attemptId: 33, proposal }, proposal);
    let current: InterviewStoryDraft = {
      ...proposed,
      serverConfirmationToken: null,
      productAction: proposed.productAction ? {
        ...proposed.productAction,
        confirmationToken: null,
        status: 'committed',
        result: { story_id: 8, version_id: 12 },
      } : null,
    };
    let settle!: (value: unknown) => void;
    actionService.undo.mockReturnValueOnce(new Promise((resolve) => { settle = resolve; }));
    const render = () => root?.render(<InterviewStoryDrawer open draft={current} onDraftChange={(next) => {
      if (next) { current = next; render(); }
      return true;
    }} onClose={() => {}} />);
    act(render);
    act(() => [...document.body.querySelectorAll('button')].find((button) => button.textContent === '撤销本次保存')?.click());

    expect(current.productAction).toMatchObject({
      ownerKey: 'story:33:3:1',
      operationId: 'story-operation-1',
      undoRequest: {
        ownerKey: 'story:33:3:1',
        originOwnerKey: 'story:33:3:1',
        parentOperationId: 'story-operation-1',
        actionName: 'confirm_interview_story',
      },
    });
    const sourceButton = [...document.body.querySelectorAll('button')]
      .find((button) => button.textContent === '打开来源选择器') as HTMLButtonElement;
    const assertionInput = document.body.querySelector('[aria-label="用户明确原始陈述"]') as HTMLInputElement;
    const storyBlock = document.body.querySelector('[aria-label="故事区块 1"]') as HTMLTextAreaElement;
    expect(sourceButton.disabled).toBe(true);
    expect(assertionInput.disabled).toBe(true);
    expect(storyBlock.disabled).toBe(true);

    act(() => {
      sourceButton.click();
      setControlValue(assertionInput, '旧回调不得创建新输入');
      setControlValue(storyBlock, '旧回调不得覆盖故事正文');
    });
    expect(current.attemptId).toBe(33);
    expect(current.assertions).toEqual([]);
    expect(current.productAction).toMatchObject({ ownerKey: 'story:33:3:1', operationId: 'story-operation-1' });

    await act(async () => {
      settle({
        schema_version: 1, operation_id: 'story-undo-operation', compensation_kind: 'undo:confirm_interview_story',
        status: 'committed', result: {}, replayed: false,
      });
      await Promise.resolve(); await Promise.resolve();
    });
    expect(current.productAction).toMatchObject({ undoStatus: 'committed', undoRequest: null });
  });

  it('allows an explicit source-backed manual save without calling the proposal endpoint', async () => {
    let current = createInterviewStoryDraft('ui');
    const close = vi.fn();
    const render = () => root?.render(<InterviewStoryDrawer open draft={current} onDraftChange={(draft) => {
      if (draft) {
        current = draft;
        render();
      }
    }} onClose={close} />);
    storyService.create.mockResolvedValue({ id: 12 });
    act(render);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    act(() => [...document.body.querySelectorAll('button')].find((button) => button.textContent === '打开来源选择器')?.click());
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    act(() => (document.body.querySelector('input[type="checkbox"]') as HTMLInputElement).click());
    act(() => [...document.body.querySelectorAll('button')].find((button) => button.textContent === '手动编写并保存')?.click());
    const title = document.body.querySelector('input[aria-label="手动故事标题"]') as HTMLInputElement;
    const situation = document.body.querySelector('textarea[aria-label="手动故事情境"]') as HTMLTextAreaElement;
    act(() => setControlValue(title, '一次延迟排查'));
    await act(async () => { await Promise.resolve(); });
    act(() => setControlValue(situation, '我先确认指标，再定位缓存问题。'));
    await act(async () => { await Promise.resolve(); });
    bindManualEvidence('title-title', 'selection:resume_version:2:/content_json/projects/0/detail');
    bindManualEvidence('block-situation_001', 'selection:resume_version:2:/content_json/projects/0/detail');
    await act(async () => {
      [...document.body.querySelectorAll('button')].find((button) => button.textContent === '确认手动保存故事版本')?.click();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(storyService.create).toHaveBeenCalledTimes(1);
    expect(storyService.proposal).not.toHaveBeenCalled();
    expect(close).toHaveBeenCalledTimes(1);
  });

  it('renders the complete manual STAR editor and records the fixed result gap when result is blank', async () => {
    let current = createInterviewStoryDraft('ui');
    const render = () => root?.render(<InterviewStoryDrawer open draft={current} onDraftChange={(draft) => {
      if (draft) {
        current = draft;
        render();
      }
    }} onClose={() => {}} />);
    storyService.create.mockResolvedValue({ id: 12 });
    act(render);
    act(() => [...document.body.querySelectorAll('button')].find((button) => button.textContent === '打开来源选择器')?.click());
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    act(() => (document.body.querySelector('input[type="checkbox"]') as HTMLInputElement).click());
    act(() => [...document.body.querySelectorAll('button')].find((button) => button.textContent === '手动编写并保存')?.click());
    for (const label of ['手动故事情境', '手动故事任务', '手动故事行动', '手动故事结果', '手动故事复盘', '手动能力标签', '手动适用问题']) {
      expect(document.body.querySelector(`[aria-label="${label}"]`)).toBeTruthy();
    }
    expect(document.body.textContent).toContain('尚未填写结果');

    const title = document.body.querySelector('input[aria-label="手动故事标题"]') as HTMLInputElement;
    const situation = document.body.querySelector('textarea[aria-label="手动故事情境"]') as HTMLTextAreaElement;
    act(() => setControlValue(title, '一次延迟排查'));
    await act(async () => { await Promise.resolve(); });
    act(() => setControlValue(situation, '我确认了指标并定位问题。'));
    await act(async () => { await Promise.resolve(); });
    bindManualEvidence('title-title', 'selection:resume_version:2:/content_json/projects/0/detail');
    bindManualEvidence('block-situation_001', 'selection:resume_version:2:/content_json/projects/0/detail');
    await act(async () => {
      [...document.body.querySelectorAll('button')].find((button) => button.textContent === '确认手动保存故事版本')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(storyService.create.mock.calls[0]?.[0].content.fact_gap_codes).toEqual(['missing_result']);
  });

  it('replays an unknown manual save with the same idempotency key and frozen payload', async () => {
    let current = createInterviewStoryDraft('ui');
    const render = () => root?.render(<InterviewStoryDrawer open draft={current} onDraftChange={(draft) => {
      if (draft) {
        current = draft;
        render();
      }
    }} onClose={() => {}} />);
    storyService.create
      .mockRejectedValueOnce(new storyService.StoryError(502, 'story_provider_error'))
      .mockResolvedValueOnce({ id: 12 });
    act(render);
    act(() => [...document.body.querySelectorAll('button')].find((button) => button.textContent === '打开来源选择器')?.click());
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    act(() => (document.body.querySelector('input[type="checkbox"]') as HTMLInputElement).click());
    act(() => [...document.body.querySelectorAll('button')].find((button) => button.textContent === '手动编写并保存')?.click());
    const title = document.body.querySelector('input[aria-label="手动故事标题"]') as HTMLInputElement;
    const situation = document.body.querySelector('textarea[aria-label="手动故事情境"]') as HTMLTextAreaElement;
    act(() => setControlValue(title, '一次延迟排查'));
    await act(async () => { await Promise.resolve(); });
    act(() => setControlValue(situation, '我确认了指标。'));
    await act(async () => { await Promise.resolve(); });
    bindManualEvidence('title-title', 'selection:resume_version:2:/content_json/projects/0/detail');
    bindManualEvidence('block-situation_001', 'selection:resume_version:2:/content_json/projects/0/detail');
    await act(async () => {
      [...document.body.querySelectorAll('button')].find((button) => button.textContent === '确认手动保存故事版本')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    const initialPayload = storyService.create.mock.calls[0]?.[0];
    expect(current.pendingOperation).toBe('manual');
    expect(current.resultUnknown).toBe(true);
    act(() => root?.render(null));
    act(render);
    await act(async () => {
      [...document.body.querySelectorAll('button')].find((button) => button.textContent === '使用原尝试重试')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(storyService.create).toHaveBeenCalledTimes(2);
    expect(storyService.create.mock.calls[1]?.[0]).toEqual(initialPayload);
  });

  it('allows a user assertion as the only explicitly selected Story source', async () => {
    const draft = {
      ...createInterviewStoryDraft('ui'),
      assertions: ['我本人负责了这次线上延迟排查。'],
    };

    act(() => root?.render(<InterviewStoryDrawer open draft={draft} onDraftChange={() => {}} onClose={() => {}} />));

    expect([...document.body.querySelectorAll('button')].some((button) => button.textContent === '使用 AI 整理')).toBe(true);
    expect([...document.body.querySelectorAll('button')].some((button) => button.textContent === '手动编写并保存')).toBe(true);
  });

  it('does not expose the UI-only manual save route from the Pilot drawer', () => {
    const draft = {
      ...createInterviewStoryDraft('pilot'),
      assertions: ['我本人负责了这次线上延迟排查。'],
    };

    act(() => root?.render(<InterviewStoryDrawer open draft={draft} onDraftChange={() => {}} onClose={() => {}} />));

    expect([...document.body.querySelectorAll('button')].some((button) => button.textContent === '使用 AI 整理')).toBe(true);
    expect([...document.body.querySelectorAll('button')].some((button) => button.textContent === '手动编写并保存')).toBe(false);
  });

  it('saves a manually authored evidence-backed Story from an explicitly bound user assertion', async () => {
    let current: InterviewStoryDraft = {
      ...createInterviewStoryDraft('ui'),
      assertions: ['我本人负责了这次线上延迟排查。'],
      manualContent: {
        ...createInterviewStoryDraft('ui').manualContent,
        title: '一次延迟排查',
        blocks: [{ kind: 'situation', text: '我本人负责了这次线上延迟排查。', fact_mode: 'evidence_backed' }],
      },
    };
    const render = () => root?.render(<InterviewStoryDrawer open draft={current} onDraftChange={(draft) => {
      if (draft) {
        current = draft;
        render();
      }
    }} onClose={() => {}} />);
    storyService.create.mockResolvedValue({ id: 12 });
    act(render);
    act(() => [...document.body.querySelectorAll('button')].find((button) => button.textContent === '手动编写并保存')?.click());
    bindManualEvidence('title-title', 'assertion:1');
    bindManualEvidence('block-situation_001', 'assertion:1');
    await act(async () => {
      [...document.body.querySelectorAll('button')].find((button) => button.textContent === '确认手动保存故事版本')?.click();
      await Promise.resolve(); await Promise.resolve();
    });

    expect(storyService.create).toHaveBeenCalledWith(expect.objectContaining({
      evidence_links: expect.arrayContaining([
        expect.objectContaining({ source_kind: 'user_assertion', source_id: 'assertion_001' }),
      ]),
    }));
  });

  it('requires an explicit evidence binding for every manual target and never copies the first source', async () => {
    let current = createInterviewStoryDraft('ui');
    const render = () => root?.render(<InterviewStoryDrawer open draft={current} onDraftChange={(draft) => {
      if (draft) {
        current = draft;
        render();
      }
    }} onClose={() => {}} />);
    storyService.create.mockResolvedValue({ id: 12 });
    act(render);
    act(() => [...document.body.querySelectorAll('button')].find((button) => button.textContent === '打开来源选择器')?.click());
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    const boxes = [...document.body.querySelectorAll('input[type="checkbox"]')] as HTMLInputElement[];
    act(() => boxes[0]?.click());
    act(() => boxes[1]?.click());
    act(() => [...document.body.querySelectorAll('button')].find((button) => button.textContent === '手动编写并保存')?.click());
    const title = document.body.querySelector('input[aria-label="手动故事标题"]') as HTMLInputElement;
    const situation = document.body.querySelector('textarea[aria-label="手动故事情境"]') as HTMLTextAreaElement;
    act(() => setControlValue(title, '一次延迟排查'));
    await act(async () => { await Promise.resolve(); });
    act(() => setControlValue(situation, '我先确认指标，再定位缓存问题。'));
    await act(async () => { await Promise.resolve(); });
    await act(async () => {
      [...document.body.querySelectorAll('button')].find((button) => button.textContent === '确认手动保存故事版本')?.click();
      await Promise.resolve(); await Promise.resolve();
    });
    expect(storyService.create).not.toHaveBeenCalled();

    bindManualEvidence('title-title', 'selection:resume_version:2:/content_json/projects/0/detail');
    bindManualEvidence('block-situation_001', 'selection:interview_note:4:/questions');
    await act(async () => {
      [...document.body.querySelectorAll('button')].find((button) => button.textContent === '确认手动保存故事版本')?.click();
      await Promise.resolve(); await Promise.resolve();
    });

    expect(storyService.create).toHaveBeenCalledTimes(1);
    const links = storyService.create.mock.calls[0]?.[0].evidence_links;
    expect(links).toEqual(expect.arrayContaining([
      expect.objectContaining({ target_id: 'title', source_kind: 'resume_version', source_path: '/content_json/projects/0/detail' }),
      expect.objectContaining({ target_id: 'situation_001', source_kind: 'interview_note', source_path: '/questions' }),
    ]));
  });
});
