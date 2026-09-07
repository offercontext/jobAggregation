import { useEffect, useMemo, useRef, useState, type CSSProperties, type MutableRefObject } from 'react';
import { Alert, Button, Input, Tag } from 'antd';
import {
  AudioOutlined,
  DeleteOutlined,
  PauseOutlined,
  PlayCircleOutlined,
  SoundOutlined,
} from '@ant-design/icons';
import {
  createLocalSpeechRecognition,
  detectVoiceInterviewCapabilities,
  ensureLocalSpeechLanguage,
  queryLocalSpeechLanguage,
  type LocalSpeechLanguageState,
  type SpeechRecognitionConstructorLike,
  type SpeechRecognitionLike,
} from './voiceInterviewCapability';
import { decodeAudioBlob, downmixAndResample } from './audioDecoder';
import OfflineWhisperModelCard from './OfflineWhisperModelCard';
import {
  offlineWhisperController,
  useOfflineWhisperState,
} from './offlineWhisperController';
import type { OfflineWhisperController } from './offlineWhisperTypes';
import { createVoiceCaptureRuntime, type VoiceCaptureRuntime, type VoiceCaptureFrame } from './voiceCaptureRuntime';
import { createVoiceSessionController, type VoiceSessionController, type VoiceSessionState } from './voiceSessionController';
import { buildVoiceDeliverySummary, type VoiceDeliverySummary } from './voiceDeliverySummary';
import VoiceDeliverySummaryCard from './VoiceDeliverySummaryCard';
import styles from './VoiceAnswerComposer.module.css';

export type VoiceAnswerActivity = 'idle' | 'preparing_voice' | 'speaking' | 'waiting_for_speech' | 'listening' | 'speech_paused' | 'transcribing' | 'reviewing_voice' | 'success' | 'error';
type AnswerMode = 'text' | 'voice';
const RECORDING_SAFETY_LIMIT_MS = 299_000;

interface MediaRecorderLike {
  state: string;
  ondataavailable: ((event: { data: Blob }) => void) | null;
  onstop: (() => void) | null;
  start(): void;
  pause(): void;
  resume(): void;
  stop(): void;
}

interface SpeechSynthesisLike {
  speak(utterance: SpeechUtteranceLike): void;
  cancel(): void;
  pause(): void;
  resume(): void;
  paused: boolean;
}

interface SpeechUtteranceLike {
  text: string;
  lang?: string;
  rate?: number;
  onend?: () => void;
  onerror?: () => void;
}

export interface VoiceAnswerBrowser {
  getUserMedia: (constraints: MediaStreamConstraints) => Promise<MediaStream>;
  createMediaRecorder?: (stream: MediaStream) => MediaRecorderLike;
  createObjectURL: (blob: Blob) => string;
  revokeObjectURL: (url: string) => void;
  speechSynthesis: SpeechSynthesisLike;
  speechSynthesisSupported?: boolean;
  createUtterance: (text: string) => SpeechUtteranceLike;
  SpeechRecognition?: SpeechRecognitionConstructorLike;
  now: () => number;
}

export type VoiceAnswerComposerCommand = {
  id: number;
  type: 'preflight' | 'read_question' | 'start_recording' | 'stop_recording' | 'start_transcription' | 'pause_capture' | 'resume_capture' | 'cleanup';
};

export type VoiceAnswerComposerEvent =
  | { type: 'preflight_succeeded'; commandId: number }
  | { type: 'preflight_failed'; commandId: number; message: string }
  | { type: 'question_read_finished'; commandId: number }
  | { type: 'recording_started'; commandId: number }
  | { type: 'recording_stopped'; commandId: number }
  | { type: 'speech_detected'; commandId: number }
  | { type: 'silence_detected'; commandId: number }
  | { type: 'page_hidden'; commandId: number }
  | { type: 'review_available'; commandId: number }
  | { type: 'recording_reset'; commandId: number }
  | { type: 'error'; commandId: number; message: string };

interface Props {
  question: string;
  disabled: boolean;
  textValue?: string;
  onTextChange?: (value: string) => void;
  submitRevision: number;
  onConfirmTranscript: (text: string) => void;
  onVoiceReviewConfirmed?: (text: string, summary: VoiceDeliverySummary) => void;
  onAnswerModeChange?: (mode: AnswerMode) => void;
  onDirtyChange?: (dirty: boolean) => void;
  onActivityChange?: (activity: VoiceAnswerActivity) => void;
  browser?: VoiceAnswerBrowser;
  offlineController?: OfflineWhisperController;
  decodeAudio?: (blob: Blob) => Promise<Float32Array>;
  createCaptureRuntime?: typeof createVoiceCaptureRuntime;
  cleanupRef?: MutableRefObject<(() => void) | null>;
  continuous?: boolean;
  compact?: boolean;
  continuousCommand?: VoiceAnswerComposerCommand;
  onContinuousEvent?: (event: VoiceAnswerComposerEvent) => void;
}

function defaultBrowser(): VoiceAnswerBrowser {
  const target = globalThis as typeof globalThis & {
    webkitSpeechRecognition?: SpeechRecognitionConstructorLike;
    SpeechRecognition?: SpeechRecognitionConstructorLike;
  };
  const unavailableSpeechSynthesis: SpeechSynthesisLike = {
    speak: () => undefined,
    cancel: () => undefined,
    pause: () => undefined,
    resume: () => undefined,
    paused: false,
  };
  const speechSynthesis = typeof window !== 'undefined' && window.speechSynthesis
    ? window.speechSynthesis as unknown as SpeechSynthesisLike
    : unavailableSpeechSynthesis;
  return {
    getUserMedia: (constraints) => navigator.mediaDevices?.getUserMedia
      ? navigator.mediaDevices.getUserMedia(constraints)
      : Promise.reject(new DOMException('Microphone unavailable', 'NotSupportedError')),
    createMediaRecorder: typeof MediaRecorder === 'function'
      ? (stream) => new MediaRecorder(stream) as unknown as MediaRecorderLike
      : undefined,
    createObjectURL: (blob) => URL.createObjectURL(blob),
    revokeObjectURL: (url) => URL.revokeObjectURL(url),
    speechSynthesis,
    speechSynthesisSupported: typeof window !== 'undefined' && Boolean(window.speechSynthesis),
    createUtterance: (text) => typeof SpeechSynthesisUtterance === 'function'
      ? new SpeechSynthesisUtterance(text) as unknown as SpeechUtteranceLike
      : { text },
    SpeechRecognition: target.SpeechRecognition ?? target.webkitSpeechRecognition,
    now: () => Date.now(),
  };
}

function formatElapsed(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  const remaining = seconds % 60;
  return `${String(minutes).padStart(2, '0')}:${String(remaining).padStart(2, '0')}`;
}

export default function VoiceAnswerComposer({
  question,
  disabled,
  textValue = '',
  onTextChange,
  submitRevision,
  onConfirmTranscript,
  onVoiceReviewConfirmed,
  onAnswerModeChange,
  onDirtyChange,
  onActivityChange,
  browser: suppliedBrowser,
  offlineController = offlineWhisperController,
  decodeAudio = decodeAudioBlob,
  createCaptureRuntime = createVoiceCaptureRuntime,
  cleanupRef,
  continuous = false,
  compact = false,
  continuousCommand,
  onContinuousEvent,
}: Props) {
  const browser = useMemo(() => suppliedBrowser ?? defaultBrowser(), [suppliedBrowser]);
  const capabilities = useMemo(() => detectVoiceInterviewCapabilities({
    MediaRecorder: browser.createMediaRecorder,
    speechSynthesis: browser.speechSynthesisSupported === false ? undefined : browser.speechSynthesis,
    SpeechRecognition: browser.SpeechRecognition,
  }), [browser]);
  const [mode, setMode] = useState<AnswerMode>('text');
  const [narrationState, setNarrationState] = useState<'idle' | 'speaking' | 'paused'>('idle');
  const [recordingState, setRecordingState] = useState<'idle' | 'recording' | 'paused' | 'ready'>('idle');
  const [elapsed, setElapsed] = useState(0);
  const [transcript, setTranscript] = useState('');
  const [interimTranscript, setInterimTranscript] = useState('');
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const [localLanguageState, setLocalLanguageState] = useState<LocalSpeechLanguageState>('unavailable');
  const [error, setError] = useState<string | null>(null);
  const [transcriptionStatus, setTranscriptionStatus] = useState<'idle' | 'decoding' | 'transcribing' | 'success' | 'error'>('idle');
  const [sessionState, setSessionState] = useState<VoiceSessionState>({ status: 'idle' });
  const [temporaryTranscript, setTemporaryTranscript] = useState('');
  const [batchOnly, setBatchOnly] = useState(false);
  const [waveLevel, setWaveLevel] = useState(0);
  const [deliverySummary, setDeliverySummary] = useState<VoiceDeliverySummary>();
  const [reviewReady, setReviewReady] = useState(false);
  const offlineModelState = useOfflineWhisperState(offlineController);
  const recorderRef = useRef<MediaRecorderLike>();
  const streamRef = useRef<MediaStream>();
  const recognitionRef = useRef<SpeechRecognitionLike>();
  const chunksRef = useRef<Blob[]>([]);
  const audioBlobRef = useRef<Blob>();
  const transcriptRef = useRef('');
  const transcriptionGenerationRef = useRef(0);
  const recordingGenerationRef = useRef(0);
  const recordingStartPendingRef = useRef(false);
  const cancelledRecordingStartGenerationRef = useRef<number>();
  const pendingPreflightCommandRef = useRef<number>();
  const transcriptionStarterRef = useRef<(() => void) | undefined>();
  const activeTranscriptionRef = useRef(false);
  const recognitionSettleRef = useRef<(() => void) | undefined>();
  const intervalRef = useRef<number>();
  const recordingSafetyTimeoutRef = useRef<number>();
  const activityTimeoutRef = useRef<number>();
  const latestAudioUrlRef = useRef<string | null>(null);
  const disposedRef = useRef(false);
  const narrationGenerationRef = useRef(0);
  const activeNarrationCommandRef = useRef<number>();
  const captureRuntimeRef = useRef<VoiceCaptureRuntime>();
  const sessionControllerRef = useRef<VoiceSessionController>();
  const captureOriginRef = useRef<number>();
  const captureLastRawEndRef = useRef<number>();
  const captureExcludedMsRef = useRef(0);
  const captureResumePendingRef = useRef(false);
  const recordingStartedAtRef = useRef(0);
  const recordingEndedAtRef = useRef(0);
  const recordingPausedAtRef = useRef<number>();
  const recordingPausedTotalRef = useRef(0);
  const recordingPausedRef = useRef(false);
  const voicedRangesRef = useRef<ReadonlyArray<readonly [number, number]>>([]);
  const audioElementRef = useRef<HTMLAudioElement>(null);
  const transcriptPanelRef = useRef<HTMLDivElement>(null);
  const continuousRef = useRef(continuous);
  const lastContinuousCommandRef = useRef(0);
  const activeContinuousCommandRef = useRef<number>();
  const latestContinuousStopRef = useRef<number>();
  const transcriptTextAreaRef = useMemo(() => ({
    get current() { return transcriptPanelRef.current?.querySelector('textarea') ?? null; },
  }), []);

  const emitActivity = (activity: VoiceAnswerActivity) => onActivityChange?.(activity);

  continuousRef.current = continuous;

  const emitContinuousEvent = (event: VoiceAnswerComposerEvent) => {
    if (continuousRef.current && !disposedRef.current && event.commandId === lastContinuousCommandRef.current) {
      onContinuousEvent?.(event);
    }
  };

  const clearTimer = () => {
    if (intervalRef.current !== undefined) window.clearInterval(intervalRef.current);
    intervalRef.current = undefined;
  };

  const clearActivityTimeout = () => {
    if (activityTimeoutRef.current !== undefined) window.clearTimeout(activityTimeoutRef.current);
    activityTimeoutRef.current = undefined;
  };

  const cancelNarration = () => {
    narrationGenerationRef.current += 1;
    activeNarrationCommandRef.current = undefined;
    browser.speechSynthesis.cancel();
    setNarrationState('idle');
  };

  const stopStream = () => {
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = undefined;
  };

  const clearAudio = () => {
    const currentUrl = latestAudioUrlRef.current;
    if (currentUrl) browser.revokeObjectURL(currentUrl);
    latestAudioUrlRef.current = null;
    setAudioUrl(null);
    audioBlobRef.current = undefined;
    chunksRef.current = [];
  };

  const clearRecordingSafetyTimeout = () => {
    if (recordingSafetyTimeoutRef.current !== undefined) window.clearTimeout(recordingSafetyTimeoutRef.current);
    recordingSafetyTimeoutRef.current = undefined;
  };

  const disposeVoiceAnalysis = () => {
    const runtime = captureRuntimeRef.current;
    captureRuntimeRef.current = undefined;
    if (runtime) void runtime.dispose();
    sessionControllerRef.current?.dispose();
    sessionControllerRef.current = undefined;
    captureOriginRef.current = undefined;
  };

  const resetVoiceDraft = () => {
    const continuousCommandId = activeContinuousCommandRef.current;
    const shouldRestartContinuousCapture = continuousRef.current
      && continuousCommandId !== undefined
      && sessionState.status !== 'idle';
    recordingGenerationRef.current += 1;
    cancelledRecordingStartGenerationRef.current = recordingGenerationRef.current;
    pendingPreflightCommandRef.current = undefined;
    activeContinuousCommandRef.current = undefined;
    latestContinuousStopRef.current = undefined;
    recordingStartPendingRef.current = false;
    clearTimer();
    clearRecordingSafetyTimeout();
    try { recorderRef.current?.state !== 'inactive' && recorderRef.current?.stop(); } catch { /* cleanup only */ }
    recorderRef.current = undefined;
    stopStream();
    recognitionRef.current?.abort();
    recognitionRef.current = undefined;
    clearAudio();
    disposeVoiceAnalysis();
    setRecordingState('idle');
    setElapsed(0);
    setTranscript('');
    transcriptRef.current = '';
    setInterimTranscript('');
    setTranscriptionStatus('idle');
    setSessionState({ status: 'idle' });
    setTemporaryTranscript('');
    setBatchOnly(false);
    setWaveLevel(0);
    setDeliverySummary(undefined);
    setReviewReady(false);
    captureOriginRef.current = undefined;
    captureLastRawEndRef.current = undefined;
    captureExcludedMsRef.current = 0;
    captureResumePendingRef.current = false;
    recordingPausedAtRef.current = undefined;
    recordingPausedTotalRef.current = 0;
    recordingPausedRef.current = false;
    transcriptionStarterRef.current = undefined;
    transcriptionGenerationRef.current += 1;
    if (activeTranscriptionRef.current) offlineController.cancel();
    activeTranscriptionRef.current = false;
    setError(null);
    clearActivityTimeout();
    cancelNarration();
    onDirtyChange?.(false);
    emitActivity('idle');
    if (shouldRestartContinuousCapture) {
      emitContinuousEvent({ type: 'recording_reset', commandId: continuousCommandId });
    }
  };

  if (cleanupRef) cleanupRef.current = resetVoiceDraft;

  useEffect(() => () => {
    if (cleanupRef) cleanupRef.current = null;
  }, [cleanupRef]);

  useEffect(() => {
    if (mode !== 'voice') return;
    let active = true;
    void queryLocalSpeechLanguage(browser.SpeechRecognition, 'zh-CN').then((state) => {
      if (active) setLocalLanguageState(state);
    });
    return () => { active = false; };
  }, [browser, mode]);

  useEffect(() => {
    if (submitRevision > 0) resetVoiceDraft();
    // submitRevision is a monotonic success signal; browser callbacks are stable for one mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [submitRevision]);

  useEffect(() => {
    disposedRef.current = false;
    return () => {
      disposedRef.current = true;
      recordingGenerationRef.current += 1;
      narrationGenerationRef.current += 1;
      activeNarrationCommandRef.current = undefined;
      pendingPreflightCommandRef.current = undefined;
      transcriptionStarterRef.current = undefined;
      clearTimer();
      clearRecordingSafetyTimeout();
      clearActivityTimeout();
      browser.speechSynthesis.cancel();
      try { recorderRef.current?.state !== 'inactive' && recorderRef.current?.stop(); } catch { /* cleanup only */ }
      recognitionRef.current?.abort();
      disposeVoiceAnalysis();
      stopStream();
      const currentUrl = latestAudioUrlRef.current;
      if (currentUrl) browser.revokeObjectURL(currentUrl);
      onActivityChange?.('idle');
      transcriptionGenerationRef.current += 1;
      if (activeTranscriptionRef.current) offlineController.cancel();
      activeTranscriptionRef.current = false;
    };
  }, [browser, onActivityChange]);

  useEffect(() => {
    onDirtyChange?.(Boolean(audioUrl || transcript.trim() || recordingState !== 'idle'));
  }, [audioUrl, onDirtyChange, recordingState, transcript]);

  const readQuestion = (commandId?: number) => {
    if (disabled || recordingState === 'recording' || recordingState === 'paused') return;
    const narrationGeneration = ++narrationGenerationRef.current;
    activeNarrationCommandRef.current = commandId;
    if (!capabilities.speechSynthesis) {
      activeNarrationCommandRef.current = undefined;
      if (commandId !== undefined) emitContinuousEvent({ type: 'question_read_finished', commandId });
      return;
    }
    browser.speechSynthesis.cancel();
    const utterance = browser.createUtterance(question);
    utterance.lang = 'zh-CN';
    utterance.rate = 0.94;
    utterance.onend = () => {
      if (disposedRef.current || narrationGeneration !== narrationGenerationRef.current) return;
      activeNarrationCommandRef.current = undefined;
      setNarrationState('idle');
      emitActivity('idle');
      if (commandId !== undefined) emitContinuousEvent({ type: 'question_read_finished', commandId });
    };
    utterance.onerror = () => {
      if (disposedRef.current || narrationGeneration !== narrationGenerationRef.current) return;
      activeNarrationCommandRef.current = undefined;
      setNarrationState('idle');
      setError('当前浏览器无法朗读题目，你仍可阅读题目并继续回答。');
      if (commandId !== undefined) emitContinuousEvent({ type: 'question_read_finished', commandId });
      emitActivity('error');
    };
    setNarrationState('speaking');
    emitActivity('speaking');
    browser.speechSynthesis.speak(utterance);
  };

  const pauseQuestion = () => {
    browser.speechSynthesis.pause();
    setNarrationState('paused');
    emitActivity('idle');
  };

  const resumeQuestion = () => {
    browser.speechSynthesis.resume();
    setNarrationState('speaking');
    emitActivity('speaking');
  };

  const startRecognition = (recordingGeneration = recordingGenerationRef.current) => {
    if (localLanguageState !== 'available' || !browser.SpeechRecognition) return;
    const recognition = createLocalSpeechRecognition(browser.SpeechRecognition, 'zh-CN');
    recognition.onresult = (event) => {
      if (recordingGeneration !== recordingGenerationRef.current || recognitionRef.current !== recognition) return;
      let finalText = '';
      let interimText = '';
      for (let index = event.resultIndex; index < event.results.length; index += 1) {
        const result = event.results[index];
        if (result.isFinal) finalText += result[0].transcript;
        else interimText += result[0].transcript;
      }
      if (finalText.trim()) setTranscript((current) => {
        const next = `${current}${current ? ' ' : ''}${finalText.trim()}`;
        transcriptRef.current = next;
        return next;
      });
      setInterimTranscript(interimText.trim());
    };
    recognition.onerror = () => {
      if (recordingGeneration !== recordingGenerationRef.current || recognitionRef.current !== recognition) return;
      setLocalLanguageState('unavailable');
      setError('本机转写没有完成，录音仍在，可试听后手工整理文字。');
    };
    recognition.onend = () => {
      if (recordingGeneration !== recordingGenerationRef.current || recognitionRef.current !== recognition) return;
      recognitionSettleRef.current?.();
    };
    recognitionRef.current = recognition;
    try { recognition.start(); } catch { setLocalLanguageState('unavailable'); }
  };

  const runOfflineTranscription = async (blob: Blob): Promise<'ready' | 'error' | 'stale'> => {
    if (offlineController.getState().status !== 'ready') {
      setTranscriptionStatus('idle');
      emitActivity('idle');
      return 'ready';
    }
    const generation = ++transcriptionGenerationRef.current;
    activeTranscriptionRef.current = true;
    setError(null);
    setTranscriptionStatus('decoding');
    emitActivity('transcribing');
    try {
      const pcm = await decodeAudio(blob);
      if (generation !== transcriptionGenerationRef.current || disposedRef.current) return 'stale';
      setTranscriptionStatus('transcribing');
      const result = await offlineController.transcribe(pcm);
      if (generation !== transcriptionGenerationRef.current || disposedRef.current) return 'stale';
      transcriptRef.current = result.text;
      setTranscript(result.text);
      setInterimTranscript('');
      setTranscriptionStatus('success');
      activeTranscriptionRef.current = false;
      return 'ready';
    } catch (transcriptionError) {
      if (generation !== transcriptionGenerationRef.current || disposedRef.current) return 'stale';
      setTranscriptionStatus('error');
      activeTranscriptionRef.current = false;
      setError(transcriptionError instanceof Error
        ? `${transcriptionError.message}。录音仍在，可重新转写或手工整理文字。`
        : '离线转写失败。录音仍在，可重新转写或手工整理文字。');
      emitActivity('error');
      return 'error';
    }
  };

  const applySessionState = (state: VoiceSessionState) => {
    setSessionState(state);
    const commandId = activeContinuousCommandRef.current;
    if (commandId !== undefined && state.status === 'recording' && state.voicedMs > 0) {
      emitContinuousEvent({ type: 'speech_detected', commandId });
    }
    if (commandId !== undefined && state.status === 'speech_paused') {
      emitContinuousEvent({ type: 'silence_detected', commandId });
    }
    if (state.status === 'waiting_for_speech') emitActivity('waiting_for_speech');
    else if (state.status === 'speech_paused') emitActivity('speech_paused');
    else if (state.status === 'recording') emitActivity('listening');
    else if (state.status === 'transcribing') emitActivity('transcribing');
    else if (state.status === 'error') emitActivity('error');
    else if (state.status === 'finalizing') {
      if (commandId !== undefined) emitContinuousEvent({ type: 'recording_stopped', commandId });
      if (recorderRef.current?.state !== 'inactive') recorderRef.current?.stop();
    }
  };

  const startVoiceAnalysis = async (stream: MediaStream) => {
    const generation = ++transcriptionGenerationRef.current;
    const recordingGeneration = recordingGenerationRef.current;
    let sessionSampleRate = 16_000;
    let receivedFrame = false;
    const session = createVoiceSessionController({
      now: browser.now,
      transcribe: async (pcm) => {
        if (offlineController.getState().status !== 'ready') return '';
        const modelPcm = sessionSampleRate === 16_000
          ? pcm
          : downmixAndResample([pcm], sessionSampleRate, 16_000);
        return (await offlineController.transcribe(modelPcm)).text;
      },
      cancelTranscription: () => offlineController.cancel(),
      onState: applySessionState,
      onInterimTranscript: setTemporaryTranscript,
    });
    session.start(generation, 16_000);
    sessionControllerRef.current = session;
    try {
      const runtime = await createCaptureRuntime(stream, (frame: VoiceCaptureFrame) => {
        if (disposedRef.current || recordingGeneration !== recordingGenerationRef.current || sessionControllerRef.current !== session) return;
        captureOriginRef.current ??= frame.atMs;
        if (captureResumePendingRef.current && captureLastRawEndRef.current !== undefined) {
          captureExcludedMsRef.current += Math.max(0, frame.atMs - captureLastRawEndRef.current);
          captureResumePendingRef.current = false;
        }
        const relativeAt = Math.max(0, frame.atMs - captureOriginRef.current - captureExcludedMsRef.current);
        captureLastRawEndRef.current = Math.max(
          captureLastRawEndRef.current ?? frame.atMs,
          frame.atMs + frame.durationMs,
        );
        if (!receivedFrame && frame.sampleRate !== sessionSampleRate) {
          sessionSampleRate = frame.sampleRate;
          session.start(generation, sessionSampleRate);
        }
        receivedFrame = true;
        setWaveLevel(Math.min(1, Math.max(frame.rms * 9, frame.peak * 4)));
        if (frame.pcm) session.acceptFrame(frame.pcm, relativeAt);
        else session.acceptLevels({ ...frame, atMs: relativeAt });
      });
      if (disposedRef.current || recordingGeneration !== recordingGenerationRef.current || sessionControllerRef.current !== session) {
        await runtime.dispose();
        return;
      }
      captureRuntimeRef.current = runtime;
      if (recordingPausedRef.current) runtime.pause();
      setBatchOnly(runtime.batchOnly);
    } catch {
      if (disposedRef.current || recordingGeneration !== recordingGenerationRef.current || sessionControllerRef.current !== session) return;
      setBatchOnly(true);
      setSessionState({ status: 'recording', elapsedMs: 0, voicedMs: 0, transcriptionMode: 'batch' });
    }
  };

  const finishManualPause = () => {
    const pausedAt = recordingPausedAtRef.current;
    if (pausedAt === undefined) return;
    recordingPausedTotalRef.current += Math.max(0, browser.now() - pausedAt);
    recordingPausedAtRef.current = undefined;
  };

  const cancelPendingRecordingStart = () => {
    if (!recordingStartPendingRef.current) return false;
    recordingGenerationRef.current += 1;
    cancelledRecordingStartGenerationRef.current = recordingGenerationRef.current;
    recordingStartPendingRef.current = false;
    setRecordingState('idle');
    emitActivity('idle');
    return true;
  };

  const requestRecorderStop = () => {
    const cancelledPendingStart = cancelPendingRecordingStart();
    finishManualPause();
    if (recorderRef.current?.state !== 'inactive') recorderRef.current?.stop();
    return cancelledPendingStart;
  };

  const startRecording = async (): Promise<boolean> => {
    if (!capabilities.recorder || disabled || recordingStartPendingRef.current) return false;
    recordingStartPendingRef.current = true;
    browser.speechSynthesis.cancel();
    setNarrationState('idle');
    setError(null);
    clearAudio();
    setTranscript('');
    transcriptRef.current = '';
    setInterimTranscript('');
    setTemporaryTranscript('');
    setDeliverySummary(undefined);
    setReviewReady(false);
    setTranscriptionStatus('idle');
    transcriptionStarterRef.current = undefined;
    const recordingGeneration = ++recordingGenerationRef.current;
    cancelledRecordingStartGenerationRef.current = undefined;
    emitActivity('preparing_voice');
    try {
      const stream = await browser.getUserMedia({ audio: true });
      if (disposedRef.current || recordingGeneration !== recordingGenerationRef.current) {
        stream.getTracks().forEach((track) => track.stop());
        return false;
      }
      recordingStartPendingRef.current = false;
      streamRef.current = stream;
      const recorder = browser.createMediaRecorder!(stream);
      recorderRef.current = recorder;
      const recordingChunks: Blob[] = [];
      chunksRef.current = recordingChunks;
      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) recordingChunks.push(event.data);
      };
      recorder.onstop = () => {
        const isCurrentRecorder = recorderRef.current === recorder
          && recordingGeneration === recordingGenerationRef.current
          && !disposedRef.current;
        if (!isCurrentRecorder) {
          return;
        }
        recorderRef.current = undefined;
        const blob = new Blob(recordingChunks, { type: recordingChunks[0]?.type || 'audio/webm' });
        audioBlobRef.current = blob;
        const url = browser.createObjectURL(blob);
        latestAudioUrlRef.current = url;
        setAudioUrl(url);
        setRecordingState('ready');
        // A recorded blob is already safe to review manually. Offline transcription
        // may continue or be unavailable, but neither case should block an explicit
        // user-edited transcript from reaching the confirmation gate.
        setReviewReady(true);
        recordingEndedAtRef.current = browser.now();
        clearTimer();
        clearRecordingSafetyTimeout();
        const runtime = captureRuntimeRef.current;
        captureRuntimeRef.current = undefined;
        if (runtime) void runtime.dispose();
        const session = sessionControllerRef.current;
        const sessionFinished = session?.finish() ?? Promise.resolve();
        if (session) {
          voicedRangesRef.current = session.getVoicedRanges();
        }
        stopStream();
        let transcriptionStarted = false;
        const startTranscription = () => {
          if (transcriptionStarted) return;
          transcriptionStarted = true;
          void sessionFinished.then(async () => {
            if (recordingGeneration !== recordingGenerationRef.current) return;
            const result = await runOfflineTranscription(blob);
            if (recordingGeneration !== recordingGenerationRef.current || result === 'stale') return;
            setReviewReady(true);
            if (result === 'ready') emitActivity('reviewing_voice');
            if (continuousCommandId !== undefined) {
              emitContinuousEvent({ type: 'review_available', commandId: continuousCommandId });
            }
          });
        };
        transcriptionStarterRef.current = startTranscription;
        const continuousCommandId = activeContinuousCommandRef.current;
        if (continuousCommandId !== undefined) {
          emitContinuousEvent({ type: 'recording_stopped', commandId: continuousCommandId });
        }
        const recognition = recognitionRef.current;
        if (recognition) {
          let settled = false;
          const continueAfterRecognition = () => {
            if (settled) return;
            settled = true;
            recognitionSettleRef.current = undefined;
            recognition.onresult = null;
            recognition.onerror = null;
            recognition.onend = null;
            if (recognitionRef.current === recognition) recognitionRef.current = undefined;
            startTranscription();
          };
          recognitionSettleRef.current = continueAfterRecognition;
          recognition.stop();
          window.setTimeout(continueAfterRecognition, 350);
        } else {
          startTranscription();
        }
      };
      recorder.start();
      recordingStartedAtRef.current = browser.now();
      recordingEndedAtRef.current = 0;
      recordingPausedAtRef.current = undefined;
      recordingPausedTotalRef.current = 0;
      recordingPausedRef.current = false;
      captureOriginRef.current = undefined;
      captureLastRawEndRef.current = undefined;
      captureExcludedMsRef.current = 0;
      captureResumePendingRef.current = false;
      voicedRangesRef.current = [];
      clearRecordingSafetyTimeout();
      recordingSafetyTimeoutRef.current = window.setTimeout(requestRecorderStop, RECORDING_SAFETY_LIMIT_MS);
      void startVoiceAnalysis(stream);
      startRecognition(recordingGeneration);
      setElapsed(0);
      setRecordingState('recording');
      emitActivity('listening');
      if (activeContinuousCommandRef.current !== undefined) {
        emitContinuousEvent({ type: 'recording_started', commandId: activeContinuousCommandRef.current });
      }
      intervalRef.current = window.setInterval(() => setElapsed((value) => value + 1), 1000);
    } catch {
      if (recordingGeneration !== recordingGenerationRef.current || disposedRef.current) return false;
      stopStream();
      setRecordingState('idle');
      setError('未获得麦克风权限。文字回答仍可使用，也可以在浏览器设置中稍后允许。');
      emitActivity('error');
    } finally {
      if (recordingGeneration === recordingGenerationRef.current) recordingStartPendingRef.current = false;
    }
    return recordingGeneration === recordingGenerationRef.current && recorderRef.current?.state !== 'inactive';
  };

  const pauseRecording = () => {
    recordingPausedRef.current = true;
    if (recordingPausedAtRef.current === undefined) recordingPausedAtRef.current = browser.now();
    captureResumePendingRef.current = true;
    recorderRef.current?.pause();
    recognitionRef.current?.stop();
    clearTimer();
    captureRuntimeRef.current?.pause();
    sessionControllerRef.current?.pause();
    setRecordingState('paused');
    emitActivity('idle');
  };

  const resumeRecording = () => {
    recordingPausedRef.current = false;
    finishManualPause();
    recorderRef.current?.resume();
    startRecognition();
    captureRuntimeRef.current?.resume();
    sessionControllerRef.current?.resume();
    intervalRef.current = window.setInterval(() => setElapsed((value) => value + 1), 1000);
    setRecordingState('recording');
    emitActivity('listening');
  };

  const stopRecording = requestRecorderStop;

  useEffect(() => {
    const pauseWhenHidden = () => {
      if (document.hidden && (
        recordingState === 'recording'
        || recordingStartPendingRef.current
        || pendingPreflightCommandRef.current !== undefined
        || activeNarrationCommandRef.current !== undefined
      )) {
        const commandId = recordingState === 'recording' || recordingStartPendingRef.current || pendingPreflightCommandRef.current !== undefined
          ? activeContinuousCommandRef.current
          : activeNarrationCommandRef.current;
        if (activeNarrationCommandRef.current !== undefined) cancelNarration();
        if (recordingState === 'recording') pauseRecording();
        else cancelPendingRecordingStart();
        if (commandId !== undefined) emitContinuousEvent({ type: 'page_hidden', commandId });
      }
    };
    document.addEventListener('visibilitychange', pauseWhenHidden);
    return () => document.removeEventListener('visibilitychange', pauseWhenHidden);
  }, [recordingState]);

  useEffect(() => {
    const command = continuousCommand;
    if (!continuous || !command || command.id <= lastContinuousCommandRef.current) return;
    lastContinuousCommandRef.current = command.id;
    if (command.type === 'preflight') {
      if (disabled) {
        emitContinuousEvent({ type: 'preflight_failed', commandId: command.id, message: '当前回答阶段不可开启连续语音。' });
        return;
      }
      changeMode('voice');
      activeContinuousCommandRef.current = command.id;
      pendingPreflightCommandRef.current = command.id;
      if (!capabilities.recorder) {
        pendingPreflightCommandRef.current = undefined;
        emitContinuousEvent({ type: 'preflight_failed', commandId: command.id, message: '当前浏览器不支持本地录音。' });
        return;
      }
      void browser.getUserMedia({ audio: true }).then((stream) => {
        if (disposedRef.current || pendingPreflightCommandRef.current !== command.id || lastContinuousCommandRef.current !== command.id) {
          stream.getTracks().forEach((track) => track.stop());
          return;
        }
        pendingPreflightCommandRef.current = undefined;
        stream.getTracks().forEach((track) => track.stop());
        emitContinuousEvent({ type: 'preflight_succeeded', commandId: command.id });
      }).catch(() => {
        if (pendingPreflightCommandRef.current !== command.id) return;
        pendingPreflightCommandRef.current = undefined;
        emitContinuousEvent({ type: 'preflight_failed', commandId: command.id, message: '未获得麦克风权限，已保留标准模式。' });
      });
      return;
    }
    if (command.type === 'read_question') {
      changeMode('voice');
      readQuestion(command.id);
      return;
    }
    if (command.type === 'start_recording') {
      changeMode('voice');
      activeContinuousCommandRef.current = command.id;
      void startRecording().then((started) => {
        if (cancelledRecordingStartGenerationRef.current !== undefined) return;
        if (!started) emitContinuousEvent({ type: 'error', commandId: command.id, message: '连续录音未能启动，已保留手动回答。' });
      });
      return;
    }
    if (command.type === 'stop_recording') {
      latestContinuousStopRef.current = command.id;
      activeContinuousCommandRef.current = command.id;
      const cancelledPendingStart = requestRecorderStop();
      if (cancelledPendingStart) emitContinuousEvent({ type: 'review_available', commandId: command.id });
      return;
    }
    if (command.type === 'start_transcription') {
      activeContinuousCommandRef.current = command.id;
      transcriptionStarterRef.current?.();
      return;
    }
    if (command.type === 'pause_capture') {
      resetVoiceDraft();
      changeMode('text');
      return;
    }
    if (command.type === 'cleanup') {
      resetVoiceDraft();
      changeMode('text');
    }
  }, [browser, capabilities.recorder, continuous, continuousCommand]);

  const installLanguage = async () => {
    if (!browser.SpeechRecognition) return;
    setLocalLanguageState('downloading');
    const installed = await ensureLocalSpeechLanguage(browser.SpeechRecognition, 'zh-CN');
    setLocalLanguageState(installed ? 'available' : 'unavailable');
    if (!installed) setError('中文本机语言包下载失败。录音与手工输入仍可使用。');
  };

  const confirmTranscript = () => {
    const confirmed = transcript.trim();
    if (!confirmed || disabled || recordingState !== 'ready' || !reviewReady) return;
    const startedAtMs = recordingStartedAtRef.current;
    const endedAtMs = recordingEndedAtRef.current || browser.now();
    const summary = buildVoiceDeliverySummary({
      startedAtMs: 0,
      endedAtMs: Math.max(0, endedAtMs - startedAtMs - recordingPausedTotalRef.current),
      voicedRanges: voicedRangesRef.current,
      transcript: confirmed,
    });
    setDeliverySummary(summary);
    onConfirmTranscript(confirmed);
    onVoiceReviewConfirmed?.(confirmed, summary);
    emitActivity('success');
  };

  const cancelTranscription = () => {
    transcriptionGenerationRef.current += 1;
    offlineController.cancel();
    activeTranscriptionRef.current = false;
    setTranscriptionStatus('idle');
    setError(null);
    const canReviewManually = recordingState === 'ready' && Boolean(audioBlobRef.current);
    setReviewReady(canReviewManually);
    emitActivity(canReviewManually ? 'reviewing_voice' : 'idle');
    const commandId = activeContinuousCommandRef.current ?? activeNarrationCommandRef.current;
    if (commandId !== undefined) emitContinuousEvent({ type: 'review_available', commandId });
  };

  const retryOfflineTranscription = async () => {
    const blob = audioBlobRef.current;
    if (!blob) return;
    const result = await runOfflineTranscription(blob);
    if (result === 'stale') return;
    setReviewReady(true);
    if (result === 'ready') emitActivity('reviewing_voice');
  };

  const localLabel = localLanguageState === 'available'
    ? '本机转写已就绪'
    : localLanguageState === 'downloadable'
      ? '可下载中文本机语言包'
      : localLanguageState === 'downloading'
        ? '正在下载本机语言包'
      : '本机转写不可用';

  const changeMode = (nextMode: AnswerMode) => {
    if (nextMode !== mode) {
      cancelNarration();
      onAnswerModeChange?.(nextMode);
    }
    setMode(nextMode);
    if (nextMode === 'text') emitActivity('idle');
  };

  const sessionLabel = recordingState === 'paused'
    ? '录音已暂停'
    : sessionState.status === 'waiting_for_speech'
      ? '等待你开口'
      : sessionState.status === 'speech_paused'
        ? '检测到停顿'
        : recordingState === 'recording'
          ? '正在聆听你的回答'
          : audioUrl
            ? '回答录音已就绪'
            : '准备好后开始录音';

  return (
    <section className={`${styles.composer} ${compact ? styles.compact : ''}`} aria-label="回答方式" data-testid="voice-answer-composer">
      <div className={styles.modeHeader}>
        <div>
          <span className={styles.eyebrow}>回答区</span>
          <h4>选择你的回答方式</h4>
        </div>
        <div className={styles.modeSwitch} role="group" aria-label="回答方式">
          {(['text', 'voice'] as AnswerMode[]).map((value) => (
            <button
              key={value}
              type="button"
              aria-pressed={mode === value}
              disabled={disabled || recordingState === 'recording' || recordingState === 'paused'}
              onClick={() => changeMode(value)}
            >
              {value === 'text' ? '文字回答' : '语音回答'}
            </button>
          ))}
        </div>
      </div>

      <div className={styles.readingBar}>
        <div>
          <SoundOutlined aria-hidden />
          <span>由系统音色朗读当前题目</span>
        </div>
        <div className={styles.readingActions}>
          {narrationState === 'idle' ? (
            <Button icon={<PlayCircleOutlined />} onClick={() => readQuestion()} disabled={disabled || !capabilities.speechSynthesis || recordingState === 'recording' || recordingState === 'paused'}>
              朗读题目
            </Button>
          ) : null}
          {narrationState === 'speaking' ? (
            <Button icon={<PauseOutlined />} onClick={pauseQuestion} disabled={disabled}>暂停朗读</Button>
          ) : null}
          {narrationState === 'paused' ? (
            <Button icon={<PlayCircleOutlined />} onClick={resumeQuestion} disabled={disabled}>继续朗读</Button>
          ) : null}
          {narrationState !== 'idle' ? (
            <Button onClick={() => readQuestion()} disabled={disabled}>重新朗读</Button>
          ) : null}
        </div>
      </div>

      {error ? <Alert showIcon type="warning" message={error} /> : null}

      {mode === 'voice' ? (
        <div className={styles.voiceStage}>
          <div className={styles.voiceStatus} data-state={recordingState} data-testid="voice-live-status" role="status" aria-live="polite">
            <div className={styles.micOrb}><AudioOutlined /></div>
            <div className={styles.voiceCopy}>
              <strong>{sessionLabel}</strong>
              <span>{sessionState.status === 'speech_paused' ? '可以继续，也可以完成回答。' : audioUrl ? '先试听，再核对下方文字；系统不会保存原始录音。' : '录音仅保存在当前页面，未确认文字不会进入模拟面试。'}</span>
            </div>
            <span className={styles.timer}>{formatElapsed(elapsed)}</span>
          </div>

          {recordingState === 'recording' || recordingState === 'paused' ? (
            <div className={styles.waveform} role="img" aria-label={sessionLabel} style={{ '--voice-level': waveLevel } as CSSProperties}>
              {Array.from({ length: 24 }, (_, index) => <span key={index} style={{ '--bar-index': index } as CSSProperties} />)}
            </div>
          ) : null}

          {batchOnly && recordingState !== 'idle' ? <Alert type="info" showIcon message="当前设备使用录完后批量转写，录音与确认流程不受影响。" /> : null}
          {temporaryTranscript ? (
            <div className={styles.temporaryTranscript} aria-live="polite">
              <span>临时字幕 · 仅供当前页面参考</span>
              <p>{temporaryTranscript}</p>
            </div>
          ) : null}

          <div className={styles.controls}>
            {recordingState === 'idle' ? (
              <Button type="primary" icon={<AudioOutlined />} onClick={() => void startRecording()} disabled={disabled || !capabilities.recorder}>开始录音</Button>
            ) : null}
            {recordingState === 'recording' ? (
              <>
                <Button icon={<PauseOutlined />} onClick={pauseRecording} disabled={disabled}>暂停</Button>
                <Button type="primary" onClick={stopRecording} disabled={disabled}>完成录音</Button>
              </>
            ) : null}
            {recordingState === 'paused' ? (
              <>
                <Button icon={<PlayCircleOutlined />} onClick={resumeRecording} disabled={disabled}>继续</Button>
                <Button type="primary" onClick={stopRecording} disabled={disabled}>完成录音</Button>
              </>
            ) : null}
            {audioUrl ? (
              <Button icon={<DeleteOutlined />} onClick={resetVoiceDraft} disabled={disabled}>重录</Button>
            ) : null}
          </div>

          {audioUrl ? <audio ref={audioElementRef} className={styles.audio} src={audioUrl} controls preload="metadata" /> : null}

          {(!compact || Boolean(audioUrl) || recordingState === 'ready') ? <div ref={transcriptPanelRef} className={styles.transcriptPanel}>
            <div className={styles.transcriptHeader}>
              <div>
                <span className={styles.eyebrow}>核对回答文字</span>
                <h4>核对回答文字</h4>
              </div>
              <Tag color={localLanguageState === 'available' ? 'green' : 'default'}>{localLabel}</Tag>
            </div>
            {localLanguageState === 'downloadable' ? (
              <Button onClick={() => void installLanguage()} disabled={disabled}>下载中文本机语言包</Button>
            ) : null}
            <OfflineWhisperModelCard controller={offlineController} compact onActivityChange={emitActivity} />
            {transcriptionStatus === 'decoding' || transcriptionStatus === 'transcribing' ? (
              <div className={styles.offlineTranscriptionStatus} role="status" aria-live="polite">
                <div>
                  <strong>{transcriptionStatus === 'decoding' ? '正在整理录音格式' : '正在本地整理语音'}</strong>
                  <span>{offlineModelState.status === 'transcribing' && offlineModelState.backend === 'wasm' ? '兼容模式' : 'GPU 加速优先'}</span>
                </div>
                <Button onClick={cancelTranscription}>取消转写</Button>
              </div>
            ) : null}
            {audioUrl && transcriptionStatus !== 'decoding' && transcriptionStatus !== 'transcribing' && !transcript.trim() && offlineModelState.status === 'ready' ? (
              <Button onClick={() => void retryOfflineTranscription()} disabled={disabled}>
                使用离线模型转写
              </Button>
            ) : null}
            <Input.TextArea
              aria-label="确认后的回答文字"
              value={`${transcript}${interimTranscript ? `${transcript ? ' ' : ''}${interimTranscript}` : ''}`}
              onChange={(event) => {
                transcriptRef.current = event.target.value;
                setTranscript(event.target.value);
                setInterimTranscript('');
              }}
              disabled={disabled}
              placeholder="本机或离线转写不可用时，可试听录音后在这里手工整理回答。"
              autoSize={{ minRows: compact ? 3 : 5, maxRows: compact ? 6 : 10 }}
            />
            <div className={styles.confirmRow}>
              <span>只有确认后的文字才会交给模拟面试。</span>
              <Button type="primary" onClick={confirmTranscript} disabled={disabled || recordingState !== 'ready' || !reviewReady || !transcript.trim()}>
                确认使用这段文字
              </Button>
            </div>
          </div> : null}
          {deliverySummary ? (
            <VoiceDeliverySummaryCard summary={deliverySummary} transcriptRef={transcriptTextAreaRef} audioRef={audioElementRef} />
          ) : null}
        </div>
      ) : (
        <div className={styles.textPanel}>
          <div className={styles.textHint}>直接输入回答；语音能力不会改变现有文本提交流程。</div>
          <Input.TextArea
            aria-label="回答"
            value={textValue}
            onChange={(event) => onTextChange?.(event.target.value)}
            disabled={disabled}
            placeholder="输入你的回答"
            autoSize={{ minRows: compact ? 3 : 5, maxRows: compact ? 6 : 10 }}
          />
        </div>
      )}
    </section>
  );
}
