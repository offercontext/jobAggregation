// @vitest-environment jsdom
import React, { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import VoiceAnswerComposer, { type VoiceAnswerBrowser } from './VoiceAnswerComposer';
import type { SpeechRecognitionLike } from './voiceInterviewCapability';
import type { OfflineModelState, OfflineWhisperController } from './offlineWhisperTypes';
import type { VoiceCaptureFrame } from './voiceCaptureRuntime';

let root: Root | undefined;
let host: HTMLDivElement | undefined;

function click(label: string): void {
  const button = Array.from(host!.querySelectorAll<HTMLElement>('button,[role="radio"]'))
    .find((item) => item.textContent?.includes(label));
  if (!button) throw new Error(`missing button: ${label}`);
  act(() => { button.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
}

function changeTextarea(textarea: HTMLTextAreaElement, value: string): void {
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLTextAreaElement.prototype,
    'value',
  )?.set;
  if (!setter) throw new Error('missing textarea value setter');
  act(() => {
    setter.call(textarea, value);
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

function browserFixture(options: { local?: boolean; localState?: 'available' | 'downloadable' | 'downloading' | 'unavailable' } = {}) {
  const track = { stop: vi.fn() };
  const stream = { getTracks: () => [track] } as unknown as MediaStream;
  const recorder = {
    state: 'inactive',
    ondataavailable: null as ((event: { data: Blob }) => void) | null,
    onstop: null as (() => void) | null,
    start: vi.fn(function start(this: typeof recorder) { this.state = 'recording'; }),
    pause: vi.fn(function pause(this: typeof recorder) { this.state = 'paused'; }),
    resume: vi.fn(function resume(this: typeof recorder) { this.state = 'recording'; }),
    stop: vi.fn(function stop(this: typeof recorder) {
      this.state = 'inactive';
      this.ondataavailable?.({ data: new Blob(['voice'], { type: 'audio/webm' }) });
      this.onstop?.();
    }),
  };
  const recognition: SpeechRecognitionLike = {
    processLocally: false,
    lang: '',
    continuous: true,
    interimResults: false,
    onresult: null,
    onerror: null,
    onend: null,
    start: vi.fn(),
    stop: vi.fn(),
    abort: vi.fn(),
  };
  const SpeechRecognition = Object.assign(vi.fn(() => recognition), {
    available: vi.fn().mockResolvedValue(options.localState ?? (options.local ? 'available' : 'unavailable')),
    install: vi.fn().mockResolvedValue(true),
  });
  const utterances: Array<{ text: string; onend?: () => void; onerror?: () => void }> = [];
  const speechSynthesis = {
    speak: vi.fn((utterance: { text: string; onend?: () => void; onerror?: () => void }) => utterances.push(utterance)),
    cancel: vi.fn(),
    pause: vi.fn(function pause(this: { paused: boolean }) { this.paused = true; }),
    resume: vi.fn(function resume(this: { paused: boolean }) { this.paused = false; }),
    paused: false,
  };
  const browser: VoiceAnswerBrowser = {
    getUserMedia: vi.fn().mockResolvedValue(stream),
    createMediaRecorder: vi.fn(() => recorder),
    createObjectURL: vi.fn(() => 'blob:voice'),
    revokeObjectURL: vi.fn(),
    speechSynthesis,
    createUtterance: vi.fn((text) => ({ text })),
    SpeechRecognition: SpeechRecognition as never,
    now: vi.fn(() => 10_000),
  };
  return { browser, recorder, recognition, track, utterances, SpeechRecognition };
}

function offlineControllerFixture(state: OfflineModelState = {
  status: 'ready',
  modelVersion: 'test',
  cachedBytes: 100,
  backend: 'webgpu',
}): OfflineWhisperController {
  return {
    getState: () => state,
    subscribe: () => () => undefined,
    check: vi.fn(async () => undefined),
    prepare: vi.fn(async () => 'webgpu' as const),
    transcribe: vi.fn(async () => ({ text: '我先定位日志，再完成回滚。', backend: 'webgpu' as const })),
    cancel: vi.fn(),
    remove: vi.fn(async () => undefined),
    dispose: vi.fn(),
  };
}

async function renderComposer(overrides: Partial<React.ComponentProps<typeof VoiceAnswerComposer>> = {}, options = {}) {
  const fixture = browserFixture(options);
  const props: React.ComponentProps<typeof VoiceAnswerComposer> = {
    question: '请介绍一次你解决线上故障的经历。',
    disabled: false,
    submitRevision: 0,
    onConfirmTranscript: vi.fn(),
    onDirtyChange: vi.fn(),
    onActivityChange: vi.fn(),
    browser: fixture.browser,
    ...overrides,
  };
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => { root!.render(<VoiceAnswerComposer {...props} />); });
  return { ...fixture, props, rerender: async (next: Partial<typeof props>) => {
    await act(async () => { root!.render(<VoiceAnswerComposer {...props} {...next} />); });
  } };
}

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  vi.useFakeTimers();
});

afterEach(async () => {
  if (root) await act(async () => { root!.unmount(); });
  host?.remove();
  root = undefined;
  host = undefined;
  Object.defineProperty(document, 'hidden', { configurable: true, value: false });
  vi.useRealTimers();
});

describe('VoiceAnswerComposer', () => {
  it('keeps text mode free of native voice probes across rerenders', async () => {
    const { browser, SpeechRecognition, rerender } = await renderComposer();

    expect(SpeechRecognition.available).not.toHaveBeenCalled();
    expect(SpeechRecognition.install).not.toHaveBeenCalled();
    expect(browser.getUserMedia).not.toHaveBeenCalled();

    await rerender({ question: '请说明一次你如何推进跨团队协作。' });

    expect(SpeechRecognition.available).not.toHaveBeenCalled();
    expect(SpeechRecognition.install).not.toHaveBeenCalled();
    expect(browser.getUserMedia).not.toHaveBeenCalled();
  });

  it('probes local speech once for each transition into voice mode', async () => {
    const { SpeechRecognition } = await renderComposer();

    await act(async () => { click('语音回答'); await Promise.resolve(); });
    expect(SpeechRecognition.available).toHaveBeenCalledTimes(1);

    await act(async () => { click('语音回答'); await Promise.resolve(); });
    expect(SpeechRecognition.available).toHaveBeenCalledTimes(1);

    click('文字回答');
    await act(async () => { click('语音回答'); await Promise.resolve(); });
    expect(SpeechRecognition.available).toHaveBeenCalledTimes(2);
  });

  it('discards a local speech probe that completes after returning to text mode', async () => {
    const fixture = browserFixture();
    let resolveFirst!: (state: unknown) => void;
    let resolveSecond!: (state: unknown) => void;
    vi.mocked(fixture.SpeechRecognition.available).mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve; }))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveSecond = resolve; }));
    await renderComposer({ browser: fixture.browser });

    click('语音回答');
    click('文字回答');
    await act(async () => {
      resolveFirst('downloadable');
      await Promise.resolve();
    });

    click('语音回答');
    await act(async () => { click('开始录音'); await Promise.resolve(); });
    await act(async () => { click('完成录音'); await vi.runAllTimersAsync(); });

    expect(host!.textContent).toContain('本机转写不可用');
    expect(host!.textContent).not.toContain('下载中文本机语言包');

    await act(async () => {
      resolveSecond('downloadable');
      await Promise.resolve();
    });

    expect(host!.textContent).toContain('下载中文本机语言包');
  });

  it('ignores a speech probe that completes after unmount', async () => {
    const fixture = browserFixture();
    let resolveProbe!: (state: unknown) => void;
    vi.mocked(fixture.SpeechRecognition.available).mockImplementationOnce(
      () => new Promise((resolve) => { resolveProbe = resolve; }),
    );
    const { props } = await renderComposer({ browser: fixture.browser });
    click('语音回答');
    expect(fixture.SpeechRecognition.available).toHaveBeenCalledTimes(1);
    await act(async () => { root!.unmount(); });
    root = undefined;
    vi.mocked(props.onActivityChange!).mockClear();
    vi.mocked(props.onDirtyChange!).mockClear();

    await act(async () => {
      resolveProbe('downloadable');
      await Promise.resolve();
    });

    expect(host!.childElementCount).toBe(0);
    expect(props.onActivityChange).not.toHaveBeenCalled();
    expect(props.onDirtyChange).not.toHaveBeenCalled();
    expect(fixture.SpeechRecognition.install).not.toHaveBeenCalled();
    expect(fixture.browser.getUserMedia).not.toHaveBeenCalled();
  });

  it('reads the question aloud and cancels speech on unmount', async () => {
    const { browser, props } = await renderComposer();
    click('朗读题目');

    expect(browser.speechSynthesis.speak).toHaveBeenCalledTimes(1);
    expect(props.onActivityChange).toHaveBeenCalledWith('speaking');
    await act(async () => { root!.unmount(); root = undefined; });
    expect(browser.speechSynthesis.cancel).toHaveBeenCalled();
  });

  it('runs controlled read/start/stop without auto-confirming the transcript', async () => {
    const onContinuousEvent = vi.fn();
    const rendered = await renderComposer({
      continuous: true,
      onContinuousEvent,
      continuousCommand: { id: 1, type: 'read_question' },
    });
    await rendered.rerender({ continuousCommand: { id: 2, type: 'read_question' } });

    await act(async () => { rendered.utterances[rendered.utterances.length - 1]?.onend?.(); });
    await rendered.rerender({ continuousCommand: { id: 3, type: 'start_recording' } });
    expect(rendered.browser.getUserMedia).toHaveBeenCalledTimes(1);

    await rendered.rerender({ continuousCommand: { id: 4, type: 'stop_recording' } });
    await act(async () => { await vi.runAllTimersAsync(); });

    expect(rendered.props.onConfirmTranscript).not.toHaveBeenCalled();
    expect(onContinuousEvent).toHaveBeenCalledWith(expect.objectContaining({ type: 'review_available', commandId: 4 }));
  });

  it('fences a pending continuous preflight after cleanup', async () => {
    const fixture = browserFixture();
    const stream = { getTracks: () => [fixture.track] } as unknown as MediaStream;
    let release!: (stream: MediaStream) => void;
    fixture.browser.getUserMedia = vi.fn(() => new Promise<MediaStream>((resolve) => { release = resolve; }));
    const onContinuousEvent = vi.fn();
    const rendered = await renderComposer({
      browser: fixture.browser,
      continuous: true,
      onContinuousEvent,
      continuousCommand: { id: 1, type: 'preflight' },
    });

    await rendered.rerender({ continuousCommand: { id: 2, type: 'cleanup' } });
    await act(async () => {
      release(stream);
      await Promise.resolve();
    });

    expect(onContinuousEvent).not.toHaveBeenCalledWith(expect.objectContaining({ type: 'preflight_succeeded' }));
  });

  it('moves a cancelled pending continuous capture to manual transcript review', async () => {
    const fixture = browserFixture();
    let release!: (stream: MediaStream) => void;
    fixture.browser.getUserMedia = vi.fn(() => new Promise<MediaStream>((resolve) => { release = resolve; }));
    const onContinuousEvent = vi.fn();
    const rendered = await renderComposer({
      browser: fixture.browser,
      continuous: true,
      onContinuousEvent,
      continuousCommand: { id: 1, type: 'start_recording' },
    });

    await rendered.rerender({ continuousCommand: { id: 2, type: 'stop_recording' } });
    expect(onContinuousEvent).toHaveBeenCalledWith({ type: 'review_available', commandId: 2 });
    release({ getTracks: () => [fixture.track] } as unknown as MediaStream);
  });

  it('pauses continuous narration when the page becomes hidden', async () => {
    const onContinuousEvent = vi.fn();
    const rendered = await renderComposer({
      continuous: true,
      onContinuousEvent,
      continuousCommand: { id: 1, type: 'read_question' },
    });

    Object.defineProperty(document, 'hidden', { configurable: true, value: true });
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')); });
    rendered.utterances[rendered.utterances.length - 1]?.onend?.();

    expect(rendered.browser.speechSynthesis.cancel).toHaveBeenCalled();
    expect(onContinuousEvent).toHaveBeenCalledWith(expect.objectContaining({ type: 'page_hidden', commandId: 1 }));
    expect(onContinuousEvent).not.toHaveBeenCalledWith(expect.objectContaining({ type: 'question_read_finished', commandId: 1 }));
    Object.defineProperty(document, 'hidden', { configurable: true, value: false });
  });

  it('reports page hiding in continuous mode and does not auto-resume capture', async () => {
    const onContinuousEvent = vi.fn();
    const rendered = await renderComposer({
      continuous: true,
      onContinuousEvent,
      continuousCommand: { id: 1, type: 'start_recording' },
    });
    await act(async () => { await Promise.resolve(); });
    Object.defineProperty(document, 'hidden', { configurable: true, value: true });
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')); });

    expect(rendered.recorder.pause).toHaveBeenCalled();
    expect(onContinuousEvent).toHaveBeenCalledWith(expect.objectContaining({ type: 'page_hidden' }));
  });

  it('pauses, resumes and restarts question narration', async () => {
    const { browser } = await renderComposer();
    click('朗读题目');
    click('暂停朗读');
    expect(browser.speechSynthesis.pause).toHaveBeenCalledTimes(1);

    click('继续朗读');
    expect(browser.speechSynthesis.resume).toHaveBeenCalledTimes(1);

    click('重新朗读');
    expect(browser.speechSynthesis.cancel).toHaveBeenCalledTimes(2);
    expect(browser.speechSynthesis.speak).toHaveBeenCalledTimes(2);
  });

  it('records, pauses, resumes, stops and previews an answer without confirming it', async () => {
    const { browser, recorder, props } = await renderComposer();
    click('语音回答');
    await act(async () => { click('开始录音'); });
    click('暂停');
    click('继续');
    await act(async () => { click('完成录音'); });

    expect(browser.getUserMedia).toHaveBeenCalledWith({ audio: true });
    expect(recorder.pause).toHaveBeenCalled();
    expect(recorder.resume).toHaveBeenCalled();
    expect(host!.querySelector('audio')?.getAttribute('src')).toBe('blob:voice');
    expect(props.onConfirmTranscript).not.toHaveBeenCalled();
  });

  it('uses local-only recognition and confirms editable transcript explicitly', async () => {
    const { recognition, props } = await renderComposer({}, { local: true });
    click('语音回答');
    await act(async () => { await Promise.resolve(); });
    await act(async () => { click('开始录音'); });
    expect(recognition.processLocally).toBe(true);
    expect(recognition.start).toHaveBeenCalled();

    await act(async () => {
      recognition.onresult?.({ resultIndex: 0, results: [{ isFinal: true, 0: { transcript: '我先定位日志。' } }] });
    });
    const textarea = host!.querySelector('textarea[aria-label="确认后的回答文字"]') as HTMLTextAreaElement;
    expect(textarea.value).toContain('我先定位日志');
    changeTextarea(textarea, '我先定位日志，再完成回滚。');
    const confirm = Array.from(host!.querySelectorAll<HTMLButtonElement>('button'))
      .find((item) => item.textContent?.includes('确认使用这段文字'))!;
    expect(confirm.disabled).toBe(true);
    click('完成录音');
    await act(async () => { await vi.runAllTimersAsync(); });
    click('确认使用这段文字');

    expect(props.onConfirmTranscript).toHaveBeenCalledWith('我先定位日志，再完成回滚。');
  });

  it('keeps manual transcription available when local recognition is unavailable', async () => {
    const { props } = await renderComposer();
    click('语音回答');
    await act(async () => { click('开始录音'); });
    await act(async () => { click('完成录音'); });

    expect(host!.textContent).toContain('本机转写不可用');
    const textarea = host!.querySelector('textarea[aria-label="确认后的回答文字"]') as HTMLTextAreaElement;
    changeTextarea(textarea, '这是我核对后的手工文字。');
    click('确认使用这段文字');
    expect(props.onConfirmTranscript).toHaveBeenCalledWith('这是我核对后的手工文字。');
  });

  it('runs offline Whisper after recording and still requires explicit confirmation', async () => {
    const offlineController = offlineControllerFixture();
    const decodeAudio = vi.fn(async () => new Float32Array([0.1, 0.2]));
    const { props } = await renderComposer({ offlineController, decodeAudio });
    click('语音回答');
    await act(async () => { click('开始录音'); });
    await act(async () => {
      click('完成录音');
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(decodeAudio).toHaveBeenCalledOnce();
    expect(offlineController.transcribe).toHaveBeenCalledOnce();
    const textarea = host!.querySelector('textarea[aria-label="确认后的回答文字"]') as HTMLTextAreaElement;
    expect(textarea.value).toBe('我先定位日志，再完成回滚。');
    expect(props.onConfirmTranscript).not.toHaveBeenCalled();
    click('确认使用这段文字');
    expect(props.onConfirmTranscript).toHaveBeenCalledWith('我先定位日志，再完成回滚。');
  });

  it('uses the complete recording for final offline transcription even when native recognition has interim text', async () => {
    const offlineController = offlineControllerFixture();
    const decodeAudio = vi.fn(async () => new Float32Array([0.1]));
    const { recognition } = await renderComposer({ offlineController, decodeAudio }, { local: true });
    click('语音回答');
    await act(async () => { await Promise.resolve(); });
    await act(async () => { click('开始录音'); });
    expect(recognition.start).toHaveBeenCalledOnce();
    expect(recognition.onresult).not.toBeNull();
    await act(async () => {
      recognition.onresult?.({ resultIndex: 0, results: [{ isFinal: true, 0: { transcript: '原生本地文字' } }] });
      click('完成录音');
      await vi.runAllTimersAsync();
    });
    expect(decodeAudio).toHaveBeenCalledOnce();
    expect(offlineController.transcribe).toHaveBeenCalledOnce();
  });

  it('fences a late microphone permission result after synchronous cleanup', async () => {
    const fixture = browserFixture();
    let resolveStream!: (stream: MediaStream) => void;
    fixture.browser.getUserMedia = vi.fn(() => new Promise<MediaStream>((resolve) => { resolveStream = resolve; }));
    const cleanupRef: { current: (() => void) | null } = { current: null };
    await renderComposer({ browser: fixture.browser, cleanupRef });
    click('语音回答');
    act(() => { click('开始录音'); });

    act(() => { cleanupRef.current?.(); });
    await act(async () => {
      resolveStream({ getTracks: () => [fixture.track] } as unknown as MediaStream);
      await Promise.resolve();
    });

    expect(fixture.track.stop).toHaveBeenCalledOnce();
    expect(fixture.browser.createMediaRecorder).not.toHaveBeenCalled();
  });

  it('keeps manual confirmation available after cancelling final transcription', async () => {
    let resolveTranscription!: (value: { text: string; backend: 'webgpu' }) => void;
    const offlineController = offlineControllerFixture();
    vi.mocked(offlineController.transcribe).mockImplementation(() => new Promise((resolve) => { resolveTranscription = resolve; }));
    const { props } = await renderComposer({
      offlineController,
      decodeAudio: vi.fn(async () => new Float32Array([0.1])),
    });
    click('语音回答');
    await act(async () => { click('开始录音'); });
    await act(async () => { click('完成录音'); await Promise.resolve(); await Promise.resolve(); });
    click('取消转写');

    const textarea = host!.querySelector('textarea[aria-label="确认后的回答文字"]') as HTMLTextAreaElement;
    changeTextarea(textarea, '这是我试听后手工核对的回答。');
    click('确认使用这段文字');

    expect(props.onConfirmTranscript).toHaveBeenCalledWith('这是我试听后手工核对的回答。');
    resolveTranscription({ text: '迟到结果', backend: 'webgpu' });
  });

  it('returns Haru to review state after a successful offline transcription retry', async () => {
    const offlineController = offlineControllerFixture();
    vi.mocked(offlineController.transcribe)
      .mockRejectedValueOnce(new Error('temporary failure'))
      .mockResolvedValueOnce({ text: '重试后的本地文字', backend: 'webgpu' });
    const { props } = await renderComposer({
      offlineController,
      decodeAudio: vi.fn(async () => new Float32Array([0.1])),
    });
    click('语音回答');
    await act(async () => { click('开始录音'); });
    await act(async () => { click('完成录音'); await Promise.resolve(); await Promise.resolve(); });
    expect(props.onActivityChange).toHaveBeenCalledWith('error');

    await act(async () => { click('使用离线模型转写'); await Promise.resolve(); await Promise.resolve(); });

    expect(props.onActivityChange).toHaveBeenLastCalledWith('reviewing_voice');
  });

  it('downloads an optional browser-managed local language pack only after consent', async () => {
    const { SpeechRecognition } = await renderComposer({}, { localState: 'downloadable' });
    click('语音回答');
    await act(async () => { await Promise.resolve(); });
    click('下载中文本机语言包');
    await act(async () => { await Promise.resolve(); });

    expect(SpeechRecognition.install).toHaveBeenCalledWith({ langs: ['zh-CN'] });
    expect(host!.textContent).toContain('本机转写已就绪');
  });

  it('freezes every answer control while the existing workflow is pending', async () => {
    await renderComposer({ disabled: true });
    const controls = [...host!.querySelectorAll<HTMLButtonElement | HTMLTextAreaElement>('button, textarea')];
    expect(controls.length).toBeGreaterThan(0);
    expect(controls.every((control) => control.disabled)).toBe(true);
  });

  it('never persists audio or invokes a network request', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('unexpected network'));
    const storageSpy = vi.spyOn(Storage.prototype, 'setItem');
    await renderComposer();
    click('语音回答');
    await act(async () => { click('开始录音'); });
    await act(async () => { click('完成录音'); });

    expect(fetchSpy).not.toHaveBeenCalled();
    expect(storageSpy).not.toHaveBeenCalled();
    fetchSpy.mockRestore();
    storageSpy.mockRestore();
  });

  it('discards old audio and transcript before re-recording', async () => {
    const { browser } = await renderComposer();
    click('语音回答');
    await act(async () => { click('开始录音'); });
    await act(async () => { click('完成录音'); });
    const textarea = host!.querySelector('textarea[aria-label="确认后的回答文字"]') as HTMLTextAreaElement;
    changeTextarea(textarea, '旧文字');
    await act(async () => { click('重录'); await Promise.resolve(); });

    expect(browser.revokeObjectURL).toHaveBeenCalledWith('blob:voice');
    expect(host!.querySelector('audio')).toBeNull();
    expect((host!.querySelector('textarea[aria-label="确认后的回答文字"]') as HTMLTextAreaElement | null)?.value ?? '').toBe('');
  });

  it('falls back safely after microphone permission denial', async () => {
    const fixture = browserFixture();
    fixture.browser.getUserMedia = vi.fn().mockRejectedValue(new DOMException('denied', 'NotAllowedError'));
    await renderComposer({ browser: fixture.browser });
    click('语音回答');
    await act(async () => { click('开始录音'); });

    expect(host!.textContent).toContain('未获得麦克风权限');
    expect(host!.textContent).toContain('文字回答仍可使用');
  });

  it('cleans tracks, object URLs and local recognition on unmount', async () => {
    const { browser, recognition, track } = await renderComposer({}, { local: true });
    click('语音回答');
    await act(async () => { await Promise.resolve(); });
    await act(async () => { click('开始录音'); });
    await act(async () => { click('完成录音'); });
    await act(async () => { root!.unmount(); root = undefined; });

    expect(track.stop).toHaveBeenCalled();
    expect(recognition.abort).toHaveBeenCalled();
    expect(browser.revokeObjectURL).toHaveBeenCalledWith('blob:voice');
  });

  it('does not create a late object URL when unmounted during recording', async () => {
    const { browser, recorder, track } = await renderComposer();
    click('语音回答');
    await act(async () => { click('开始录音'); });
    await act(async () => { root!.unmount(); root = undefined; });

    expect(recorder.stop).toHaveBeenCalledTimes(1);
    expect(browser.createObjectURL).not.toHaveBeenCalled();
    expect(track.stop).toHaveBeenCalled();
  });

  it('clears audio only after a successful answer submission revision', async () => {
    const { browser, rerender } = await renderComposer();
    click('语音回答');
    await act(async () => { click('开始录音'); });
    await act(async () => { click('完成录音'); });
    await rerender({ submitRevision: 1 });

    expect(browser.revokeObjectURL).toHaveBeenCalledWith('blob:voice');
    expect(host!.querySelector('audio')).toBeNull();
  });

  it('fences late native recognition results after re-recording starts', async () => {
    const { recognition } = await renderComposer({}, { local: true });
    click('语音回答');
    await act(async () => { await Promise.resolve(); });
    await act(async () => { click('开始录音'); });
    expect(recognition.start).toHaveBeenCalledOnce();
    expect(recognition.onresult).not.toBeNull();
    const lateResult = recognition.onresult;
    await act(async () => { click('完成录音'); await vi.runAllTimersAsync(); });
    await act(async () => { click('重录'); });
    await act(async () => { click('开始录音'); });
    expect(recognition.start).toHaveBeenCalledTimes(2);

    await act(async () => {
      lateResult?.({ resultIndex: 0, results: [{ isFinal: true, 0: { transcript: '旧一轮迟到文字' } }] });
    });

    const textarea = host!.querySelector('textarea[aria-label="确认后的回答文字"]') as HTMLTextAreaElement;
    expect(textarea.value).not.toContain('旧一轮迟到文字');
  });

  it('shows local interim coaching, long-pause guidance and a review only after explicit confirmation', async () => {
    let frameCallback: ((frame: VoiceCaptureFrame) => void) | undefined;
    const captureRuntime = { batchOnly: false, pause: vi.fn(), resume: vi.fn(), dispose: vi.fn(async () => undefined) };
    const createCaptureRuntime = vi.fn(async (_stream, onFrame: (frame: VoiceCaptureFrame) => void) => {
      frameCallback = onFrame;
      return captureRuntime;
    });
    const offlineController = offlineControllerFixture();
    const onVoiceReviewConfirmed = vi.fn();
    let now = 10_000;
    const fixture = browserFixture();
    fixture.browser.now = vi.fn(() => now);
    const { props } = await renderComposer({
      browser: fixture.browser,
      createCaptureRuntime,
      offlineController,
      decodeAudio: vi.fn(async () => new Float32Array([0.1, 0.2])),
      onVoiceReviewConfirmed,
    });

    click('语音回答');
    await act(async () => { click('开始录音'); await Promise.resolve(); });
    await act(async () => {
      frameCallback?.({ pcm: new Float32Array(12_800), sampleRate: 16_000, atMs: 0, durationMs: 800, rms: 0, peak: 0 });
      frameCallback?.({ pcm: new Float32Array(320_000).fill(0.08), sampleRate: 16_000, atMs: 800, durationMs: 20_000, rms: 0.08, peak: 0.08 });
      for (let turn = 0; turn < 6; turn += 1) await Promise.resolve();
    });
    expect(host!.textContent).toContain('临时字幕 · 仅供当前页面参考');
    await act(async () => {
      frameCallback?.({ pcm: new Float32Array(48_000), sampleRate: 16_000, atMs: 20_800, durationMs: 3_000, rms: 0, peak: 0 });
    });
    expect(host!.textContent).toContain('检测到停顿');
    expect(host!.textContent).toContain('可以继续，也可以完成回答');
    expect(props.onConfirmTranscript).not.toHaveBeenCalled();

    await act(async () => {
      frameCallback?.({ pcm: new Float32Array(80_000).fill(0.08), sampleRate: 16_000, atMs: 23_800, durationMs: 5_000, rms: 0.08, peak: 0.08 });
    });

    now = 82_000;
    await act(async () => { click('完成录音'); await vi.runAllTimersAsync(); });
    expect(captureRuntime.dispose).toHaveBeenCalledOnce();
    expect(props.onActivityChange).toHaveBeenCalledWith('reviewing_voice');
    expect(props.onActivityChange).not.toHaveBeenCalledWith('success');
    const textarea = host!.querySelector('textarea[aria-label="确认后的回答文字"]') as HTMLTextAreaElement;
    changeTextarea(textarea, '嗯我先定位日志，然后完成回滚。');
    click('确认使用这段文字');
    expect(props.onConfirmTranscript).toHaveBeenCalledOnce();
    expect(onVoiceReviewConfirmed).toHaveBeenCalledWith(
      '嗯我先定位日志，然后完成回滚。',
      expect.objectContaining({
        totalDurationMs: 72_000,
        longestPauseMs: 3_000,
        source: 'local_audio_and_confirmed_transcript',
      }),
    );
    expect(props.onActivityChange).toHaveBeenLastCalledWith('success');
    expect(host!.textContent).toContain('表达节奏复盘');
    expect(host!.textContent).toContain('01:12');
    const audio = host!.querySelector('audio') as HTMLAudioElement;
    audio.play = vi.fn(async () => undefined);
    click('最长停顿');
    expect(audio.currentTime).toBeGreaterThanOrEqual(20);
    expect(audio.currentTime).toBeLessThan(30);
  });

  it('announces live VAD state changes to assistive technology', async () => {
    await renderComposer();
    click('语音回答');
    const status = host!.querySelector('[data-testid="voice-live-status"]');
    expect(status?.getAttribute('role')).toBe('status');
    expect(status?.getAttribute('aria-live')).toBe('polite');
  });

  it('stops at the five-minute boundary without confirming and keeps batch fallback explicit', async () => {
    let frameCallback: ((frame: VoiceCaptureFrame) => void) | undefined;
    const createCaptureRuntime = vi.fn(async (_stream, onFrame: (frame: VoiceCaptureFrame) => void) => {
      frameCallback = onFrame;
      return { batchOnly: true, pause: vi.fn(), resume: vi.fn(), dispose: vi.fn(async () => undefined) };
    });
    const { props, recorder } = await renderComposer({ createCaptureRuntime });
    click('语音回答');
    await act(async () => { click('开始录音'); await Promise.resolve(); });
    expect(host!.textContent).toContain('录完后批量转写');
    await act(async () => {
      frameCallback?.({ sampleRate: 16_000, atMs: 0, durationMs: 300_000, rms: 0.08, peak: 0.08 });
      await Promise.resolve();
    });
    expect(recorder.stop).toHaveBeenCalledOnce();
    expect(props.onConfirmTranscript).not.toHaveBeenCalled();
  });

  it('starts the five-minute safety cap with MediaRecorder before the first analysis frame arrives', async () => {
    const { recorder, props } = await renderComposer({
      createCaptureRuntime: vi.fn(async () => ({
        batchOnly: false,
        pause: vi.fn(),
        resume: vi.fn(),
        dispose: vi.fn(async () => undefined),
      })),
    });
    click('语音回答');
    await act(async () => { click('开始录音'); await Promise.resolve(); });

    await act(async () => { await vi.advanceTimersByTimeAsync(299_000); });

    expect(recorder.stop).toHaveBeenCalledOnce();
    expect(props.onConfirmTranscript).not.toHaveBeenCalled();
  });

  it('removes a manual pause from delivery metrics and audio pause navigation', async () => {
    let frameCallback: ((frame: VoiceCaptureFrame) => void) | undefined;
    let now = 0;
    const fixture = browserFixture();
    fixture.browser.now = vi.fn(() => now);
    const { props } = await renderComposer({
      browser: fixture.browser,
      offlineController: offlineControllerFixture(),
      decodeAudio: vi.fn(async () => new Float32Array([0.1])),
      createCaptureRuntime: vi.fn(async (_stream, onFrame: (frame: VoiceCaptureFrame) => void) => {
        frameCallback = onFrame;
        return { batchOnly: false, pause: vi.fn(), resume: vi.fn(), dispose: vi.fn(async () => undefined) };
      }),
    });
    click('语音回答');
    await act(async () => { click('开始录音'); await Promise.resolve(); });
    await act(async () => {
      frameCallback?.({ pcm: new Float32Array(12_800), sampleRate: 16_000, atMs: 0, durationMs: 800, rms: 0, peak: 0 });
      frameCallback?.({ pcm: new Float32Array(147_200).fill(0.08), sampleRate: 16_000, atMs: 800, durationMs: 9_200, rms: 0.08, peak: 0.08 });
    });
    now = 10_000;
    click('暂停');
    now = 40_000;
    click('继续');
    await act(async () => {
      frameCallback?.({ pcm: new Float32Array(80_000).fill(0.08), sampleRate: 16_000, atMs: 40_000, durationMs: 5_000, rms: 0.08, peak: 0.08 });
      frameCallback?.({ pcm: new Float32Array(48_000), sampleRate: 16_000, atMs: 45_000, durationMs: 3_000, rms: 0, peak: 0 });
      frameCallback?.({ pcm: new Float32Array(32_000).fill(0.08), sampleRate: 16_000, atMs: 48_000, durationMs: 2_000, rms: 0.08, peak: 0.08 });
    });
    now = 50_000;
    await act(async () => { click('完成录音'); await vi.runAllTimersAsync(); });
    const textarea = host!.querySelector('textarea[aria-label="确认后的回答文字"]') as HTMLTextAreaElement;
    changeTextarea(textarea, '嗯，我先定位日志，再说明稳定性与成本之间的取舍。');
    click('确认使用这段文字');

    expect(props.onConfirmTranscript).toHaveBeenCalledOnce();
    expect(host!.textContent).toContain('3.0 秒');
    const audio = host!.querySelector('audio') as HTMLAudioElement;
    audio.play = vi.fn(async () => undefined);
    click('最长停顿');
    expect(audio.currentTime).toBeGreaterThanOrEqual(14);
    expect(audio.currentTime).toBeLessThan(17);
  });

  it('pauses local capture when the page becomes hidden', async () => {
    const captureRuntime = { batchOnly: false, pause: vi.fn(), resume: vi.fn(), dispose: vi.fn(async () => undefined) };
    const { recorder } = await renderComposer({
      createCaptureRuntime: vi.fn(async () => captureRuntime),
    });
    click('语音回答');
    await act(async () => { click('开始录音'); await Promise.resolve(); });

    Object.defineProperty(document, 'hidden', { configurable: true, value: true });
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')); });

    expect(recorder.pause).toHaveBeenCalledOnce();
    expect(captureRuntime.pause).toHaveBeenCalledOnce();
    expect(host!.textContent).toContain('录音已暂停');
    Object.defineProperty(document, 'hidden', { configurable: true, value: false });
  });

  it('applies a pending pause after deferred capture runtime initialization', async () => {
    const captureRuntime = { batchOnly: false, pause: vi.fn(), resume: vi.fn(), dispose: vi.fn(async () => undefined) };
    let resolveRuntime!: (runtime: typeof captureRuntime) => void;
    const { recorder } = await renderComposer({
      createCaptureRuntime: vi.fn(() => new Promise<typeof captureRuntime>((resolve) => { resolveRuntime = resolve; })),
    });
    click('语音回答');
    await act(async () => { click('开始录音'); await Promise.resolve(); });
    click('暂停');
    expect(recorder.pause).toHaveBeenCalledOnce();

    await act(async () => { resolveRuntime(captureRuntime); await Promise.resolve(); });

    expect(captureRuntime.pause).toHaveBeenCalledOnce();
  });

  it('resamples worklet PCM to the offline model sample rate', async () => {
    let frameCallback: ((frame: VoiceCaptureFrame) => void) | undefined;
    const offlineController = offlineControllerFixture();
    await renderComposer({
      offlineController,
      createCaptureRuntime: vi.fn(async (_stream, onFrame: (frame: VoiceCaptureFrame) => void) => {
        frameCallback = onFrame;
        return { batchOnly: false, pause: vi.fn(), resume: vi.fn(), dispose: vi.fn(async () => undefined) };
      }),
    });
    click('语音回答');
    await act(async () => { click('开始录音'); await Promise.resolve(); });

    await act(async () => {
      frameCallback?.({
        pcm: new Float32Array(48_000 * 20).fill(0.08),
        sampleRate: 48_000,
        atMs: 0,
        durationMs: 20_000,
        rms: 0.08,
        peak: 0.08,
      });
      for (let turn = 0; turn < 6; turn += 1) await Promise.resolve();
    });

    expect(offlineController.transcribe).toHaveBeenCalled();
    const pcm = vi.mocked(offlineController.transcribe).mock.calls[0][0];
    expect(pcm.length).toBe(320_000);
  });
});
