// @vitest-environment jsdom
import React, { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ContinuousVoiceModePanel from './ContinuousVoiceModePanel';
import InterviewStudio from './InterviewStudio';

const serviceSpies = vi.hoisted(() => ({
  start: vi.fn(),
  answer: vi.fn(),
  nextQuestion: vi.fn(),
  finish: vi.fn(),
  discard: vi.fn(),
  saveVoiceReview: vi.fn(),
}));

vi.mock('@/services/mockInterviews', () => ({
  startInterviewStudioAttempt: serviceSpies.start,
  submitInterviewStudioAnswer: serviceSpies.answer,
  generateInterviewStudioQuestion: serviceSpies.nextQuestion,
  finishInterviewStudio: serviceSpies.finish,
  discardInterviewStudioAttempt: serviceSpies.discard,
}));

vi.mock('@/services/voiceCoaching', () => ({
  saveInterviewStudioVoiceCoachingSnapshot: serviceSpies.saveVoiceReview,
}));

vi.mock('@/features/mockInterviewVoice/VoiceAnswerComposer', () => ({
  default: (props: {
    continuousCommand?: { id: number; type: string };
    onContinuousEvent?: (event: { type: string; commandId: number; message?: string }) => void;
    onVoiceReviewConfirmed?: (text: string, summary: unknown) => void;
  }) => {
    React.useEffect(() => {
      const command = props.continuousCommand;
      if (!command) return;
      if (command.type === 'preflight') props.onContinuousEvent?.({ type: 'preflight_succeeded', commandId: command.id });
      if (command.type === 'read_question') props.onContinuousEvent?.({ type: 'question_read_finished', commandId: command.id });
      if (command.type === 'start_recording') props.onContinuousEvent?.({ type: 'recording_started', commandId: command.id });
      if (command.type === 'stop_recording') {
        props.onContinuousEvent?.({ type: 'recording_stopped', commandId: command.id });
        props.onContinuousEvent?.({ type: 'review_available', commandId: command.id });
      }
    }, [props.continuousCommand, props.onContinuousEvent]);
    return <button type="button" onClick={() => props.onVoiceReviewConfirmed?.('我先定位日志，再完成回滚。', {
      totalDurationMs: 1000,
      voicedDurationMs: 800,
      pauseCount: 1,
      longestPauseMs: 200,
      speechRateCpm: 120,
      fillerOccurrences: [],
    } as never)}>确认录音文字</button>;
  },
}));

let root: Root | undefined;
let host: HTMLDivElement | undefined;

async function renderPanel(props: Partial<React.ComponentProps<typeof ContinuousVoiceModePanel>> = {}) {
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(<ContinuousVoiceModePanel status="disabled" onEnable={vi.fn()} {...props} />);
  });
}

function button(label: string): HTMLButtonElement {
  const match = Array.from(host!.querySelectorAll<HTMLButtonElement>('button'))
    .find((item) => item.textContent?.includes(label));
  if (!match) throw new Error(`missing button: ${label}`);
  return match;
}

afterEach(async () => {
  if (root) await act(async () => { root!.unmount(); });
  host?.remove();
  root = undefined;
  host = undefined;
  window.sessionStorage.clear();
});

describe('ContinuousVoiceModePanel', () => {
  beforeEach(() => {
    globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  });

  it('requires an explicit user click before enabling continuous voice', async () => {
    const onEnable = vi.fn();
    await renderPanel({ onEnable });

    expect(host!.textContent).toContain('连续语音模式');
    expect(onEnable).not.toHaveBeenCalled();
    await act(async () => { button('开启连续语音模式').click(); });
    expect(onEnable).toHaveBeenCalledTimes(1);
  });

  it('exposes countdown cancellation and fallback actions as text controls', async () => {
    const onCancelCountdown = vi.fn();
    const onDisable = vi.fn();
    await renderPanel({ status: 'end_candidate', countdownSeconds: 3, onCancelCountdown, onDisable });

    expect(host!.textContent).toContain('3 秒后停止录音');
    await act(async () => { button('继续补充').click(); });
    expect(onCancelCountdown).toHaveBeenCalledTimes(1);
    await act(async () => { button('切换标准模式').click(); });
    expect(onDisable).toHaveBeenCalledTimes(1);
  });

  it('allows leaving a pending preflight before the microphone promise resolves', async () => {
    const onDisable = vi.fn();
    await renderPanel({ status: 'preflight', onDisable });

    await act(async () => { button('切换标准模式').click(); });
    expect(onDisable).toHaveBeenCalledTimes(1);
  });
});

describe('InterviewStudio continuous voice integration', () => {
  beforeEach(() => {
    globalThis.IS_REACT_ACT_ENVIRONMENT = true;
    serviceSpies.start.mockResolvedValue({
      attempt_id: 41,
      turn: { turn_no: 1, question: '请介绍一次排障经历。', answer: '', question_kind: 'new_topic', basis_refs: [] },
    });
    serviceSpies.answer.mockResolvedValue({ ok: true });
    serviceSpies.nextQuestion.mockResolvedValue({
      attempt_id: 41,
      turn: { turn_no: 2, question: '你如何验证修复？', answer: '', question_kind: 'follow_up', parent_turn_no: 1, basis_refs: [] },
    });
    serviceSpies.finish.mockReset();
    serviceSpies.discard.mockResolvedValue(undefined);
    serviceSpies.saveVoiceReview.mockResolvedValue({ ok: true });
    serviceSpies.start.mockClear();
    serviceSpies.answer.mockClear();
    serviceSpies.nextQuestion.mockClear();
    serviceSpies.discard.mockClear();
    serviceSpies.saveVoiceReview.mockClear();
  });

  it('shows a persistent top-level progress state while feedback is being generated', async () => {
    serviceSpies.finish.mockImplementationOnce(() => new Promise(() => undefined));
    await act(async () => {
      root = createRoot(host = document.createElement('div'));
      document.body.appendChild(host);
      root.render(
        <InterviewStudio
          context={{ kind: 'application_event', applicationId: 7, eventId: 8, resumeId: 9, jdVersionId: 10, jdText: '需要排障与回滚能力。' }}
          onClose={vi.fn()}
        />,
      );
      await Promise.resolve();
    });
    await act(async () => { await Promise.resolve(); });

    await act(async () => { button('结束并生成复盘').click(); await Promise.resolve(); });

    const status = host!.querySelector<HTMLElement>('[data-interview-studio-status]');
    expect(status?.textContent).toContain('正在生成复盘，通常需要几十秒');
    expect(status?.closest('[data-interview-conversation-scroll]')).toBeNull();
    const generatingButton = button('正在生成复盘…');
    expect(generatingButton.getAttribute('aria-busy')).toBe('true');
    generatingButton.click();
    expect(serviceSpies.finish).toHaveBeenCalledTimes(1);
  });

  it('announces completed feedback and moves focus to the result', async () => {
    const originalScrollIntoView = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollIntoView');
    const scrollIntoView = vi.fn();
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
      configurable: true,
      value: scrollIntoView,
    });
    try {
      serviceSpies.finish.mockResolvedValueOnce({
        proposal_id: 91,
        proposal_status: 'normal',
        proposal_hash: 'feedback-hash',
        proposal: {
          schema_version: 'mock-interview-feedback-v1',
          proposal_status: 'normal',
          strengths: [{ id: 'strength-1', text: '定位过程清晰。', evidence_refs: [] }],
          practice_points: [],
          follow_up_questions: [],
          next_practice_steps: [],
        },
      });
      serviceSpies.nextQuestion.mockImplementation(async (input: { turnNo: number }) => ({
        attempt_id: 41,
        turn: { turn_no: input.turnNo, question: `第 ${input.turnNo} 轮追问`, answer: '', question_kind: 'follow_up', parent_turn_no: input.turnNo - 1, basis_refs: [] },
      }));
      await act(async () => {
        root = createRoot(host = document.createElement('div'));
        document.body.appendChild(host);
        root.render(
          <InterviewStudio
            context={{ kind: 'application_event', applicationId: 7, eventId: 8, resumeId: 9, jdVersionId: 10, jdText: '需要排障与回滚能力。' }}
            onClose={vi.fn()}
          />,
        );
        await Promise.resolve();
      });
      await act(async () => { await Promise.resolve(); });
      await act(async () => { button('开启连续语音模式').click(); await Promise.resolve(); });
      for (let round = 0; round < 5; round += 1) {
        await act(async () => { button('结束本轮录音').click(); await Promise.resolve(); });
        await act(async () => { button('确认录音文字').click(); await Promise.resolve(); });
        await act(async () => { await Promise.resolve(); });
      }

      await act(async () => { button('结束并生成复盘').click(); await Promise.resolve(); });
      await act(async () => { await Promise.resolve(); });
      await act(async () => { await new Promise((resolve) => window.requestAnimationFrame(resolve)); });

      const status = host!.querySelector<HTMLElement>('[data-interview-studio-status]');
      const result = host!.querySelector<HTMLElement>('[data-interview-feedback-result]');
      expect(host!.querySelectorAll('[data-interview-speaker="candidate"]')).toHaveLength(5);
      expect(status?.textContent).toContain('复盘已生成');
      expect(status?.closest('[data-interview-conversation-scroll]')).toBeNull();
      expect(result?.textContent).toContain('定位过程清晰');
      expect(scrollIntoView).toHaveBeenCalledWith({ block: 'start' });
      expect(document.activeElement).toBe(result);
      scrollIntoView.mockClear();
      await act(async () => { button('查看复盘').click(); });
      expect(scrollIntoView).toHaveBeenCalledWith({ block: 'start' });
      expect(document.activeElement).toBe(result);
    } finally {
      if (originalScrollIntoView) {
        Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', originalScrollIntoView);
      } else {
        delete (HTMLElement.prototype as { scrollIntoView?: typeof HTMLElement.prototype.scrollIntoView }).scrollIntoView;
      }
    }
  });

  it('explains when completed feedback contains no verifiable suggestions', async () => {
    serviceSpies.finish.mockResolvedValueOnce({
      proposal_id: 92,
      proposal_status: 'safe_empty',
      proposal_hash: 'safe-empty-hash',
      proposal: {
        schema_version: 'mock-interview-feedback-v1',
        proposal_status: 'safe_empty',
        strengths: [],
        practice_points: [],
        follow_up_questions: [],
        next_practice_steps: [],
      },
    });
    await act(async () => {
      root = createRoot(host = document.createElement('div'));
      document.body.appendChild(host);
      root.render(
        <InterviewStudio
          context={{ kind: 'application_event', applicationId: 7, eventId: 8, resumeId: 9, jdVersionId: 10, jdText: '需要排障与回滚能力。' }}
          onClose={vi.fn()}
        />,
      );
      await Promise.resolve();
    });
    await act(async () => { await Promise.resolve(); });

    await act(async () => { button('结束并生成复盘').click(); await Promise.resolve(); });
    await act(async () => { await Promise.resolve(); });

    expect(host!.querySelector('[data-interview-studio-status]')?.textContent).toContain('复盘已完成，暂无可验证建议');
    expect(host!.querySelector('[data-interview-feedback-result]')?.textContent).toContain('本轮没有生成可验证的复盘建议');
  });

  it('surfaces terminal feedback failures above the scroller and focuses recovery', async () => {
    serviceSpies.finish.mockRejectedValueOnce({
      response: { status: 502, data: { error_code: 'mock_interview_unverifiable', attempt_id: 41 } },
    });
    await act(async () => {
      root = createRoot(host = document.createElement('div'));
      document.body.appendChild(host);
      root.render(
        <InterviewStudio
          context={{ kind: 'application_event', applicationId: 7, eventId: 8, resumeId: 9, jdVersionId: 10, jdText: '需要排障与回滚能力。' }}
          onClose={vi.fn()}
        />,
      );
      await Promise.resolve();
    });
    await act(async () => { await Promise.resolve(); });

    await act(async () => { button('结束并生成复盘').click(); await Promise.resolve(); });
    await act(async () => { await Promise.resolve(); });
    await act(async () => { await new Promise((resolve) => window.requestAnimationFrame(resolve)); });

    const status = host!.querySelector<HTMLElement>('[data-interview-studio-status]');
    expect(status?.textContent).toContain('AI 输出未通过证据验证');
    expect(status?.closest('[data-interview-conversation-scroll]')).toBeNull();
    expect(document.activeElement).toBe(status);
    expect(host!.textContent).toContain('重新开始练习');
  });

  it('surfaces unknown feedback results above the scroller with the original-key recovery', async () => {
    serviceSpies.finish.mockRejectedValueOnce({
      response: { status: 502, data: { error_code: 'mock_interview_feedback_result_unknown', attempt_id: 41 } },
    });
    await act(async () => {
      root = createRoot(host = document.createElement('div'));
      document.body.appendChild(host);
      root.render(
        <InterviewStudio
          context={{ kind: 'application_event', applicationId: 7, eventId: 8, resumeId: 9, jdVersionId: 10, jdText: '需要排障与回滚能力。' }}
          onClose={vi.fn()}
        />,
      );
      await Promise.resolve();
    });
    await act(async () => { await Promise.resolve(); });

    await act(async () => { button('结束并生成复盘').click(); await Promise.resolve(); });
    await act(async () => { await Promise.resolve(); });
    await act(async () => { await new Promise((resolve) => window.requestAnimationFrame(resolve)); });

    const status = host!.querySelector<HTMLElement>('[data-interview-studio-status]');
    expect(status?.textContent).toContain('复盘结果待确认，已保留原 feedback key');
    expect(status?.closest('[data-interview-conversation-scroll]')).toBeNull();
    expect(document.activeElement).toBe(status);
    expect(host!.textContent).toContain('使用原尝试恢复');
    expect(serviceSpies.finish).toHaveBeenCalledTimes(1);
  });

  it('treats provider_error as a same-key reconciliation, never a restart', async () => {
    serviceSpies.nextQuestion.mockRejectedValueOnce({
      response: { status: 502, data: { error_code: 'mock_interview_provider_error', attempt_id: 41 } },
    });
    await act(async () => {
      root = createRoot(host = document.createElement('div'));
      document.body.appendChild(host);
      root.render(
        <InterviewStudio
          context={{ kind: 'application_event', applicationId: 7, eventId: 8, resumeId: 9, jdVersionId: 10, jdText: '需要排障与回滚能力。' }}
          onClose={vi.fn()}
        />,
      );
      await Promise.resolve();
    });
    await act(async () => { await Promise.resolve(); });

    await act(async () => { button('开启连续语音模式').click(); await Promise.resolve(); });
    await act(async () => { button('结束本轮录音').click(); await Promise.resolve(); });
    await act(async () => { button('确认录音文字').click(); await Promise.resolve(); });
    await act(async () => { await Promise.resolve(); });

    expect(host!.textContent).toContain('下一题结果待确认，已保留原 question key');
    expect(host!.textContent).toContain('使用原尝试恢复');
    expect(host!.textContent).not.toContain('重新开始练习');
    expect(serviceSpies.discard).not.toHaveBeenCalled();
  });

  it('treats idempotency conflicts as a restart with fresh keys, not a same-key retry', async () => {
    serviceSpies.nextQuestion.mockRejectedValueOnce({
      response: { status: 409, data: { error_code: 'mock_interview_turn_idempotency_conflict', attempt_id: 41 } },
    });
    const context = { kind: 'application_event' as const, applicationId: 7, eventId: 8, resumeId: 9, jdVersionId: 10, jdText: '需要排障与回滚能力。' };
    await act(async () => {
      root = createRoot(host = document.createElement('div'));
      document.body.appendChild(host);
      root.render(<InterviewStudio context={context} onClose={vi.fn()} />);
      await Promise.resolve();
    });
    await act(async () => { await Promise.resolve(); });
    const firstStart = serviceSpies.start.mock.calls[0]?.[0] as { attemptKey: string; questionKey: string };

    await act(async () => { button('开启连续语音模式').click(); await Promise.resolve(); });
    await act(async () => { button('结束本轮录音').click(); await Promise.resolve(); });
    await act(async () => { button('确认录音文字').click(); await Promise.resolve(); });
    await act(async () => { await Promise.resolve(); });

    expect(host!.textContent).not.toContain('使用原尝试恢复');
    expect(host!.textContent).toContain('重新开始练习');
    await act(async () => { button('重新开始练习').click(); await Promise.resolve(); });
    await act(async () => { await Promise.resolve(); });

    expect(serviceSpies.discard).not.toHaveBeenCalled();
    const restarted = serviceSpies.start.mock.calls[1]?.[0] as { attemptKey: string; questionKey: string };
    expect(restarted.attemptKey).not.toBe(firstStart.attemptKey);
    expect(restarted.questionKey).not.toBe(firstStart.questionKey);
  });

  it('restarts a terminally unverifiable question with fresh keys instead of retrying the failed key', async () => {
    serviceSpies.nextQuestion.mockRejectedValueOnce({
      response: {
        status: 502,
        data: { error_code: 'mock_interview_unverifiable', attempt_id: 41 },
      },
    });
    const context = { kind: 'application_event' as const, applicationId: 7, eventId: 8, resumeId: 9, jdVersionId: 10, jdText: '需要排障与回滚能力。' };
    await act(async () => {
      root = createRoot(host = document.createElement('div'));
      document.body.appendChild(host);
      root.render(<InterviewStudio context={context} onClose={vi.fn()} />);
      await Promise.resolve();
    });
    await act(async () => { await Promise.resolve(); });
    const firstStart = serviceSpies.start.mock.calls[0]?.[0] as { attemptKey: string; questionKey: string };

    await act(async () => { button('开启连续语音模式').click(); await Promise.resolve(); });
    await act(async () => { button('结束本轮录音').click(); await Promise.resolve(); });
    await act(async () => { button('确认录音文字').click(); await Promise.resolve(); });
    await act(async () => { await Promise.resolve(); });

    expect(host!.textContent).toContain('AI 输出未通过证据验证');
    expect(host!.textContent).not.toContain('使用原尝试恢复');
    await act(async () => { button('重新开始练习').click(); await Promise.resolve(); });
    await act(async () => { await Promise.resolve(); });

    expect(serviceSpies.discard).not.toHaveBeenCalled();
    expect(serviceSpies.start).toHaveBeenCalledTimes(2);
    const restarted = serviceSpies.start.mock.calls[1]?.[0] as { attemptKey: string; questionKey: string };
    expect(restarted.attemptKey).not.toBe(firstStart.attemptKey);
    expect(restarted.questionKey).not.toBe(firstStart.questionKey);
  });

  it('does not call business services during media stages and submits confirmed text once', async () => {
    const onClose = vi.fn();
    await act(async () => {
      root = createRoot(host = document.createElement('div'));
      document.body.appendChild(host);
      root.render(
        <InterviewStudio
          context={{ kind: 'application_event', applicationId: 7, eventId: 8, resumeId: 9, jdVersionId: 10, jdText: '需要排障与回滚能力。', companyName: '示例公司', positionName: '平台工程师' }}
          onClose={onClose}
        />,
      );
      await Promise.resolve();
    });
    await act(async () => { await Promise.resolve(); });
    expect(serviceSpies.start).toHaveBeenCalledTimes(1);

    await act(async () => { button('开启连续语音模式').click(); await Promise.resolve(); });
    await act(async () => { await Promise.resolve(); });
    expect(serviceSpies.answer).not.toHaveBeenCalled();
    expect(serviceSpies.nextQuestion).not.toHaveBeenCalled();

    await act(async () => { button('结束本轮录音').click(); await Promise.resolve(); });
    await act(async () => { await Promise.resolve(); });
    expect(serviceSpies.answer).not.toHaveBeenCalled();

    await act(async () => { button('确认录音文字').click(); await Promise.resolve(); });
    await act(async () => { await Promise.resolve(); });
    expect(serviceSpies.answer).toHaveBeenCalledTimes(1);
    expect(serviceSpies.answer).toHaveBeenCalledWith(expect.objectContaining({ answerText: '我先定位日志，再完成回滚。' }));
    expect(serviceSpies.saveVoiceReview).toHaveBeenCalledTimes(1);
    expect(serviceSpies.nextQuestion).toHaveBeenCalledTimes(1);
  });

  it('uses a new question key for each successful round', async () => {
    serviceSpies.nextQuestion.mockImplementation(async (input: { turnNo: number }) => ({
      attempt_id: 41,
      turn: { turn_no: input.turnNo, question: `第 ${input.turnNo} 轮追问`, answer: '', question_kind: 'follow_up', parent_turn_no: input.turnNo - 1, basis_refs: [] },
    }));
    await act(async () => {
      root = createRoot(host = document.createElement('div'));
      document.body.appendChild(host);
      root.render(
        <InterviewStudio
          context={{ kind: 'application_event', applicationId: 7, eventId: 8, resumeId: 9, jdVersionId: 10, jdText: '需要排障与回滚能力。' }}
          onClose={vi.fn()}
        />,
      );
      await Promise.resolve();
    });
    await act(async () => { await Promise.resolve(); });
    await act(async () => { button('开启连续语音模式').click(); await Promise.resolve(); });

    for (let round = 0; round < 2; round += 1) {
      await act(async () => { button('结束本轮录音').click(); await Promise.resolve(); });
      await act(async () => { button('确认录音文字').click(); await Promise.resolve(); });
      await act(async () => { await Promise.resolve(); });
    }

    const firstKey = (serviceSpies.nextQuestion.mock.calls[0]?.[0] as { questionKey: string }).questionKey;
    const secondKey = (serviceSpies.nextQuestion.mock.calls[1]?.[0] as { questionKey: string }).questionKey;
    expect(firstKey).toMatch(/^question-/);
    expect(secondKey).toMatch(/^question-/);
    expect(secondKey).not.toBe(firstKey);
  });

  it('switches between answer and evidence without calling business services', async () => {
    serviceSpies.start.mockResolvedValueOnce({
      attempt_id: 41,
      turn: {
        turn_no: 1,
        question: '请介绍一次排障经历。',
        answer: '',
        question_kind: 'new_topic',
        basis_refs: [{ source: 'jd', path: '/jd/text', excerpt: '需要排障与回滚能力。' }],
      },
    });
    await act(async () => {
      root = createRoot(host = document.createElement('div'));
      document.body.appendChild(host);
      root.render(
        <InterviewStudio
          context={{ kind: 'application_event', applicationId: 7, eventId: 8, resumeId: 9, jdVersionId: 10, jdText: '需要排障与回滚能力。', companyName: '示例公司', positionName: '平台工程师' }}
          onClose={vi.fn()}
        />,
      );
      await Promise.resolve();
    });
    await act(async () => { await Promise.resolve(); });

    const tabs = Array.from(host!.querySelectorAll<HTMLElement>('[role="tab"]'));
    expect(tabs.map((tab) => tab.textContent)).toEqual(['回答', '依据']);
    expect(tabs[0]?.getAttribute('aria-selected')).toBe('true');
    expect(host!.querySelector('[aria-label="回答工作台"]')).not.toBeNull();
    const startCalls = serviceSpies.start.mock.calls.length;

    await act(async () => { button('冻结 JD').click(); });
    expect(host!.querySelectorAll<HTMLElement>('[role="tab"]')[1]?.getAttribute('aria-selected')).toBe('true');
    expect(host!.querySelector('[aria-label="本轮依据"]')).not.toBeNull();
    await act(async () => { host!.querySelectorAll<HTMLButtonElement>('[role="tab"]')[0]?.click(); });
    expect(host!.querySelectorAll<HTMLElement>('[role="tab"]')[0]?.getAttribute('aria-selected')).toBe('true');
    expect(serviceSpies.start).toHaveBeenCalledTimes(startCalls);
    expect(serviceSpies.answer).not.toHaveBeenCalled();
    expect(serviceSpies.nextQuestion).not.toHaveBeenCalled();
  });

  it('renders a confirmed answer as a candidate chat message', async () => {
    await act(async () => {
      root = createRoot(host = document.createElement('div'));
      document.body.appendChild(host);
      root.render(
        <InterviewStudio
          context={{ kind: 'application_event', applicationId: 7, eventId: 8, resumeId: 9, jdVersionId: 10, jdText: '需要排障与回滚能力。' }}
          onClose={vi.fn()}
        />,
      );
      await Promise.resolve();
    });
    await act(async () => { await Promise.resolve(); });
    await act(async () => { button('开启连续语音模式').click(); await Promise.resolve(); });
    await act(async () => { button('结束本轮录音').click(); await Promise.resolve(); });
    await act(async () => { button('确认录音文字').click(); await Promise.resolve(); });
    await act(async () => { await Promise.resolve(); });

    const candidateMessage = host!.querySelector('[data-interview-speaker="candidate"]');
    expect(candidateMessage?.textContent).toContain('你');
    expect(candidateMessage?.textContent).toContain('我先定位日志，再完成回滚。');
  });

  it('exposes a mobile answer-workspace trigger', async () => {
    const onClose = vi.fn();
    await act(async () => {
      root = createRoot(host = document.createElement('div'));
      document.body.appendChild(host);
      root.render(
        <InterviewStudio
          context={{ kind: 'application_event', applicationId: 7, eventId: 8, resumeId: 9, jdVersionId: 10, jdText: '需要排障与回滚能力。' }}
          onClose={onClose}
        />,
      );
      await Promise.resolve();
    });
    await act(async () => { await Promise.resolve(); });

    const trigger = button('打开回答工作台');
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    expect(trigger.getAttribute('aria-controls')).toBe('interview-answer-workspace');
    await act(async () => {
      trigger.click();
      await new Promise((resolve) => window.requestAnimationFrame(resolve));
    });
    expect(trigger.getAttribute('aria-expanded')).toBe('true');
    expect(document.activeElement).toBe(host!.querySelectorAll<HTMLButtonElement>('[role="tab"]')[0]);
    await act(async () => {
      host!.querySelector('[data-interview-studio]')?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      await new Promise((resolve) => window.requestAnimationFrame(resolve));
    });
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    expect(document.activeElement).toBe(trigger);
    expect(onClose).not.toHaveBeenCalled();
  });

  it('restores a business result-unknown with the original attempt and turn key', async () => {
    window.sessionStorage.setItem('offerpilot:interview-studio:business-recovery:real:7:8', JSON.stringify({
      attemptKey: 'attempt-original',
      attemptId: 41,
      state: {
        phase: 'result_unknown',
        answerMode: 'text',
        turnNo: 1,
        maxTurns: 5,
        question: 'Describe a debugging incident.',
        answer: 'I located the logs and completed a rollback.',
        turnKey: 'turn-original',
        questionKey: null,
        feedbackKey: null,
        pendingOperation: 'answer',
        resultUnknown: true,
        error: 'Result needs confirmation',
      },
      timeline: [{
        turn_no: 1,
        question: 'Describe a debugging incident.',
        answer: 'I located the logs and completed a rollback.',
        question_kind: 'new_topic',
        basis_refs: [],
        confirmed: false,
      }],
    }));
    const onClose = vi.fn();
    await act(async () => {
      root = createRoot(host = document.createElement('div'));
      document.body.appendChild(host);
      root.render(
        <InterviewStudio
          context={{ kind: 'application_event', applicationId: 7, eventId: 8, resumeId: 9, jdVersionId: 10, jdText: 'Debugging and rollback.', companyName: 'Example', positionName: 'Platform Engineer' }}
          onClose={onClose}
        />,
      );
      await Promise.resolve();
    });
    expect(serviceSpies.start).not.toHaveBeenCalled();
    await act(async () => { button('使用原尝试恢复').click(); await Promise.resolve(); });
    await act(async () => { await Promise.resolve(); });
    expect(serviceSpies.answer).toHaveBeenCalledWith(expect.objectContaining({
      attemptId: 41,
      turnKey: 'turn-original',
      answerText: 'I located the logs and completed a rollback.',
    }));
    expect(window.sessionStorage.getItem('offerpilot:interview-studio:business-recovery:real:7:8')).toBeNull();
  });

  it('persists an initial result-unknown attempt key until the user retries it', async () => {
    serviceSpies.start.mockRejectedValueOnce({
      response: { status: 502, data: { error_code: 'mock_interview_provider_error' } },
    });
    const context = { kind: 'application_event' as const, applicationId: 7, eventId: 8, resumeId: 9, jdVersionId: 10, jdText: 'Debugging and rollback.', companyName: 'Example', positionName: 'Platform Engineer' };
    await act(async () => {
      root = createRoot(host = document.createElement('div'));
      document.body.appendChild(host);
      root.render(<InterviewStudio context={context} onClose={vi.fn()} />);
      await Promise.resolve();
    });
    await act(async () => { await Promise.resolve(); });

    const stored = JSON.parse(window.sessionStorage.getItem('offerpilot:interview-studio:start-recovery:real:7:8') ?? 'null') as { attemptKey: string; questionKey: string };
    expect(stored.attemptKey).toMatch(/^attempt-/);
    expect(stored.questionKey).toMatch(/^question-/);

    await act(async () => {
      root?.unmount();
      host?.remove();
      root = undefined;
      host = undefined;
    });
    serviceSpies.start.mockResolvedValueOnce({
      attempt_id: 41,
      turn: { turn_no: 1, question: '请介绍一次排障经历。', answer: '', question_kind: 'new_topic', basis_refs: [] },
    });
    await act(async () => {
      root = createRoot(host = document.createElement('div'));
      document.body.appendChild(host);
      root.render(<InterviewStudio context={context} onClose={vi.fn()} />);
      await Promise.resolve();
    });
    expect(serviceSpies.start).toHaveBeenCalledTimes(1);
    await act(async () => { button('使用原尝试恢复').click(); await Promise.resolve(); });
    await act(async () => { await Promise.resolve(); });
    expect(serviceSpies.start).toHaveBeenCalledWith(expect.objectContaining({ attemptKey: stored.attemptKey, questionKey: stored.questionKey }));
    expect(window.sessionStorage.getItem('offerpilot:interview-studio:start-recovery:real:7:8')).toBeNull();
  });

  it('does not start two attempts during a React StrictMode effect probe', async () => {
    const context = { kind: 'application_event' as const, applicationId: 7, eventId: 8, resumeId: 9, jdVersionId: 10, jdText: 'Debugging and rollback.', companyName: 'Example', positionName: 'Platform Engineer' };
    await act(async () => {
      root = createRoot(host = document.createElement('div'));
      document.body.appendChild(host);
      root.render(<React.StrictMode><InterviewStudio context={context} onClose={vi.fn()} /></React.StrictMode>);
      await Promise.resolve();
    });
    await act(async () => { await Promise.resolve(); });

    expect(serviceSpies.start).toHaveBeenCalledTimes(1);
  });

  it('cleans the initial retry timer when StrictMode Studio unmounts', async () => {
    vi.useFakeTimers();
    try {
      serviceSpies.start.mockResolvedValueOnce({ status: 'pending', retry_after_ms: 1000 });
      const context = { kind: 'application_event' as const, applicationId: 7, eventId: 8, resumeId: 9, jdVersionId: 10, jdText: 'Debugging and rollback.', companyName: 'Example', positionName: 'Platform Engineer' };
      await act(async () => {
        root = createRoot(host = document.createElement('div'));
        document.body.appendChild(host);
        root.render(<React.StrictMode><InterviewStudio context={context} onClose={vi.fn()} /></React.StrictMode>);
        await Promise.resolve();
      });
      await act(async () => { await Promise.resolve(); });
      expect(serviceSpies.start).toHaveBeenCalledTimes(1);

      await act(async () => {
        root?.unmount();
        host?.remove();
        root = undefined;
        host = undefined;
        await vi.advanceTimersByTimeAsync(1500);
      });
      expect(serviceSpies.start).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('continues to the next question when voice review persistence is still pending', async () => {
    let releaseReview!: (value: unknown) => void;
    serviceSpies.saveVoiceReview.mockImplementationOnce(() => new Promise((resolve) => { releaseReview = resolve; }));
    await act(async () => {
      root = createRoot(host = document.createElement('div'));
      document.body.appendChild(host);
      root.render(
        <InterviewStudio
          context={{ kind: 'application_event', applicationId: 7, eventId: 8, resumeId: 9, jdVersionId: 10, jdText: '需要排障与回滚能力。', companyName: '示例公司', positionName: '平台工程师' }}
          onClose={vi.fn()}
        />,
      );
      await Promise.resolve();
    });
    await act(async () => { await Promise.resolve(); });
    await act(async () => { button('开启连续语音模式').click(); await Promise.resolve(); });
    await act(async () => { button('结束本轮录音').click(); await Promise.resolve(); });
    await act(async () => { button('确认录音文字').click(); await Promise.resolve(); });
    await act(async () => { await Promise.resolve(); });

    expect(serviceSpies.answer).toHaveBeenCalledTimes(1);
    expect(serviceSpies.nextQuestion).toHaveBeenCalledTimes(1);
    expect(window.sessionStorage.getItem('offerpilot:interview-studio:voice-recovery:real:7:8')).not.toBeNull();
    releaseReview({ ok: true });
    await act(async () => { await Promise.resolve(); });
    expect(window.sessionStorage.getItem('offerpilot:interview-studio:voice-recovery:real:7:8')).toBeNull();
  });

  it('keeps deterministic voice review validation failures distinct from unknown results', async () => {
    serviceSpies.saveVoiceReview.mockRejectedValueOnce({ response: { status: 422 } });
    await act(async () => {
      root = createRoot(host = document.createElement('div'));
      document.body.appendChild(host);
      root.render(
        <InterviewStudio
          context={{ kind: 'application_event', applicationId: 7, eventId: 8, resumeId: 9, jdVersionId: 10, jdText: '需要排障与回滚能力。', companyName: '示例公司', positionName: '平台工程师' }}
          onClose={vi.fn()}
        />,
      );
      await Promise.resolve();
    });
    await act(async () => { await Promise.resolve(); });
    await act(async () => { button('开启连续语音模式').click(); await Promise.resolve(); });
    await act(async () => { button('结束本轮录音').click(); await Promise.resolve(); });
    await act(async () => { button('确认录音文字').click(); await Promise.resolve(); });
    await act(async () => { await Promise.resolve(); });

    expect(serviceSpies.nextQuestion).toHaveBeenCalledTimes(1);
    expect(host!.textContent).toContain('表达复盘数据未通过校验');
  });
});
