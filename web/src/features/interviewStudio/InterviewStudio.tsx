import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Tag } from 'antd';
import { ArrowLeftOutlined, CheckCircleOutlined, FileTextOutlined, MenuOutlined, SendOutlined } from '@ant-design/icons';
import VoiceAnswerComposer, { type VoiceAnswerActivity, type VoiceAnswerComposerCommand, type VoiceAnswerComposerEvent } from '@/features/mockInterviewVoice/VoiceAnswerComposer';
import {
  createContinuousVoiceSessionController,
  type ContinuousVoiceCommand,
  type ContinuousVoiceSessionController,
  type ContinuousVoiceState,
} from '@/features/mockInterviewVoice/continuousVoiceSessionController';
import type { VoiceDeliverySummary } from '@/features/mockInterviewVoice/voiceDeliverySummary';
import {
  finishInterviewStudio,
  generateInterviewStudioQuestion,
  discardInterviewStudioAttempt,
  startInterviewStudioAttempt,
  submitInterviewStudioAnswer,
  type InterviewStudioContext as InterviewApiContext,
} from '@/services/mockInterviews';
import { saveInterviewStudioVoiceCoachingSnapshot } from '@/services/voiceCoaching';
import type { MockInterviewProposalResponse, MockInterviewTurn } from '@/types/mockInterview';
import { resolveErrorRecovery, type RecoveryDecision } from '@/lib/recoveryPolicy/recoveryPolicy';
import type { RecoveryAttemptRetention, RecoveryUserAction } from '@/lib/recoveryPolicy/generatedRecoveryPolicy';
import {
  createStudioState,
  reduceStudioState,
  shouldGenerateNextQuestion,
  type StudioState,
} from './interviewStudioController';
import { buildEvidenceEntries, evidenceKey, type StudioEvidenceEntry } from './evidenceLocator';
import styles from './InterviewStudio.module.css';
import ContinuousVoiceModePanel from './ContinuousVoiceModePanel';

export interface InterviewStudioContext {
  kind: 'application_event';
  applicationId: number;
  eventId: number;
  resumeId: number;
  jdVersionId: number;
  jdText: string;
  companyName?: string;
  positionName?: string;
}

export interface QuickPracticeStudioContext {
  kind: 'quick_practice';
  caseId: number;
  resumeId: number;
  positionName: string;
  jdText: string;
}

type Props = {
  context: InterviewStudioContext | QuickPracticeStudioContext;
  onClose: () => void;
  onActivityChange?: (activity: VoiceAnswerActivity) => void;
  onToggleHaru?: () => void;
  onEvidenceVisibilityChange?: (open: boolean) => void;
};

type TimelineEntry = MockInterviewTurn & { confirmed?: boolean };
type VoiceReview = {
  turnNo: number;
  summary: VoiceDeliverySummary;
  saveState: 'idle' | 'saving' | 'saved' | 'unknown' | 'conflict' | 'invalid';
  idempotencyKey: string;
  saveError?: string;
};
type VoiceReviewRecovery = { attemptKey: string; attemptId: number | null; voiceReview: VoiceReview };
type StudioBusinessRecovery = { attemptKey: string; attemptId: number; state: StudioState; timeline: TimelineEntry[] };
type StudioStartRecovery = { attemptKey: string; questionKey: string; message: string };
type TerminalDisposition = 'terminal_no_retry' | 'restart_new_attempt' | 'reload_source';
type StudioTerminalFailure = {
  message: string;
  attemptId: number | null;
  disposition: TerminalDisposition;
  userAction: RecoveryUserAction;
  attemptRetention: RecoveryAttemptRetention;
};
type WorkspaceTab = 'answer' | 'evidence';

const CONTINUOUS_VOICE_PREFERENCE_KEY = 'offerpilot:interview-studio:continuous-voice-preference';

function continuousActivity(status: ContinuousVoiceState['status']): VoiceAnswerActivity | undefined {
  if (status === 'reading_question') return 'speaking';
  if (status === 'waiting_for_speech') return 'waiting_for_speech';
  if (status === 'listening') return 'listening';
  if (status === 'end_candidate') return 'speech_paused';
  if (status === 'transcribing') return 'transcribing';
  if (status === 'reviewing_transcript') return 'reviewing_voice';
  if (status === 'fallback_standard') return 'idle';
  if (status === 'submitting_confirmed_answer' || status === 'generating_next_question') return 'preparing_voice';
  return undefined;
}

function key(prefix: string): string {
  return `${prefix}-${typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : Date.now()}`;
}

function toServiceContext(context: Props['context']): InterviewApiContext {
  return context.kind === 'quick_practice'
    ? { kind: 'quick_practice', caseId: context.caseId }
    : { kind: 'application_event', applicationId: context.applicationId, eventId: context.eventId };
}

function isTurnResponse(value: Awaited<ReturnType<typeof startInterviewStudioAttempt>>): value is Extract<Awaited<ReturnType<typeof startInterviewStudioAttempt>>, { turn: MockInterviewTurn }> {
  return 'turn' in value;
}

function isBlockingFailure(decision: RecoveryDecision): decision is RecoveryDecision & { disposition: TerminalDisposition } {
  return decision.disposition === 'terminal_no_retry'
    || decision.disposition === 'restart_new_attempt'
    || decision.disposition === 'reload_source';
}

function terminalFailureOf(decision: RecoveryDecision, fallbackAttemptId: number | null): StudioTerminalFailure {
  return {
    message: decision.message,
    attemptId: decision.attemptId ?? fallbackAttemptId,
    disposition: decision.disposition as TerminalDisposition,
    userAction: decision.userAction,
    attemptRetention: decision.attemptRetention,
  };
}

function questionLabel(turn: TimelineEntry): string {
  if (turn.question_kind === 'follow_up') return '追问';
  return '新话题';
}

function previewText(value: string, length = 180): string {
  const trimmed = value.trim();
  return trimmed.length > length ? `${trimmed.slice(0, length)}…` : trimmed;
}

function voiceRecoveryStorageKey(context: Props['context']): string {
  return context.kind === 'quick_practice'
    ? `offerpilot:interview-studio:voice-recovery:quick:${context.caseId}`
    : `offerpilot:interview-studio:voice-recovery:real:${context.applicationId}:${context.eventId}`;
}

function studioRecoveryStorageKey(context: Props['context']): string {
  return context.kind === 'quick_practice'
    ? `offerpilot:interview-studio:business-recovery:quick:${context.caseId}`
    : `offerpilot:interview-studio:business-recovery:real:${context.applicationId}:${context.eventId}`;
}

function studioStartRecoveryStorageKey(context: Props['context']): string {
  return context.kind === 'quick_practice'
    ? `offerpilot:interview-studio:start-recovery:quick:${context.caseId}`
    : `offerpilot:interview-studio:start-recovery:real:${context.applicationId}:${context.eventId}`;
}

function readVoiceReviewRecovery(storageKey: string): VoiceReviewRecovery | null {
  if (typeof window === 'undefined') return null;
  try {
    const raw = window.sessionStorage.getItem(storageKey);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<VoiceReviewRecovery>;
    return parsed && typeof parsed.attemptKey === 'string' && parsed.voiceReview
      ? parsed as VoiceReviewRecovery
      : null;
  } catch {
    return null;
  }
}

function readStudioBusinessRecovery(storageKey: string): StudioBusinessRecovery | null {
  if (typeof window === 'undefined') return null;
  try {
    const parsed = JSON.parse(window.sessionStorage.getItem(storageKey) ?? 'null') as Partial<StudioBusinessRecovery> | null;
    return parsed
      && typeof parsed.attemptKey === 'string'
      && typeof parsed.attemptId === 'number'
      && parsed.state?.phase === 'result_unknown'
      && Array.isArray(parsed.timeline)
      ? parsed as StudioBusinessRecovery
      : null;
  } catch {
    return null;
  }
}

function readStudioStartRecovery(storageKey: string): StudioStartRecovery | null {
  if (typeof window === 'undefined') return null;
  try {
    const parsed = JSON.parse(window.sessionStorage.getItem(storageKey) ?? 'null') as Partial<StudioStartRecovery> | null;
    return parsed
      && typeof parsed.attemptKey === 'string'
      && typeof parsed.questionKey === 'string'
      && typeof parsed.message === 'string'
      ? parsed as StudioStartRecovery
      : null;
  } catch {
    return null;
  }
}

export default function InterviewStudio({ context, onClose, onActivityChange, onToggleHaru, onEvidenceVisibilityChange }: Props) {
  const studioRef = useRef<HTMLDivElement>(null);
  const questionHeadingRef = useRef<HTMLHeadingElement>(null);
  const mobileWorkspaceTriggerRef = useRef<HTMLButtonElement>(null);
  const answerTabRef = useRef<HTMLButtonElement>(null);
  const onCloseRef = useRef(onClose);
  const closeRequestRef = useRef<() => void>(() => undefined);
  onCloseRef.current = onClose;
  const serviceContext = useMemo(() => toServiceContext(context), [context]);
  const recoveryStorageKey = useMemo(() => voiceRecoveryStorageKey(context), [context]);
  const studioRecoveryKey = useMemo(() => studioRecoveryStorageKey(context), [context]);
  const studioStartRecoveryKey = useMemo(() => studioStartRecoveryStorageKey(context), [context]);
  const recoveryRef = useRef<VoiceReviewRecovery | null | undefined>(undefined);
  if (recoveryRef.current === undefined) recoveryRef.current = readVoiceReviewRecovery(recoveryStorageKey);
  const recovery = recoveryRef.current;
  const businessRecoveryRef = useRef<StudioBusinessRecovery | null | undefined>(undefined);
  if (businessRecoveryRef.current === undefined) businessRecoveryRef.current = readStudioBusinessRecovery(studioRecoveryKey);
  const businessRecovery = businessRecoveryRef.current;
  const startRecoveryRef = useRef<StudioStartRecovery | null | undefined>(undefined);
  if (startRecoveryRef.current === undefined) startRecoveryRef.current = readStudioStartRecovery(studioStartRecoveryKey);
  const startRecovery = startRecoveryRef.current;
  const attemptKeyRef = useRef(businessRecovery?.attemptKey ?? startRecovery?.attemptKey ?? recovery?.attemptKey ?? key('attempt'));
  const initialQuestionKeyRef = useRef(startRecovery?.questionKey ?? key('question'));
  const startRetryTimerRef = useRef<number | null>(null);
  const startCleanupTimerRef = useRef<number | null>(null);
  const startEffectContextRef = useRef<InterviewApiContext | null>(null);
  const startRequestRef = useRef(0);
  const initialQuestionFocusedRef = useRef(false);
  const [state, setState] = useState<StudioState | null>(() => businessRecovery?.state ?? null);
  const [continuousState, setContinuousState] = useState<ContinuousVoiceState>({
    status: 'disabled',
    generation: 0,
    question: '',
    transcript: '',
    countdownSeconds: null,
    error: null,
  });
  const [continuousCommand, setContinuousCommand] = useState<VoiceAnswerComposerCommand>();
  const [attemptId, setAttemptId] = useState<number | null>(() => businessRecovery?.attemptId ?? recovery?.attemptId ?? null);
  const [timeline, setTimeline] = useState<TimelineEntry[]>(() => businessRecovery?.timeline ?? []);
  const [proposal, setProposal] = useState<MockInterviewProposalResponse | null>(null);
  const [voiceSubmitRevision, setVoiceSubmitRevision] = useState(0);
  const [workspaceTab, setWorkspaceTab] = useState<WorkspaceTab>('answer');
  const [mobileWorkspaceOpen, setMobileWorkspaceOpen] = useState(false);
  const [selectedEvidenceKey, setSelectedEvidenceKey] = useState<string | null>(null);
  const [jdExpanded, setJdExpanded] = useState(false);
  const [working, setWorking] = useState(false);
  const [startError, setStartError] = useState<string | null>(() => startRecovery?.message ?? null);
  const [terminalFailure, setTerminalFailure] = useState<StudioTerminalFailure | null>(null);
  const [voiceReview, setVoiceReview] = useState<VoiceReview | null>(() => recovery?.voiceReview ?? null);
  const [voiceDirty, setVoiceDirty] = useState(false);
  const studioStatusRef = useRef<HTMLDivElement>(null);
  const feedbackResultRef = useRef<HTMLElement>(null);
  const mobileWorkspaceOpenRef = useRef(mobileWorkspaceOpen);
  const voiceReviewRef = useRef<VoiceReview | null>(voiceReview);
  const continuousSubmitRef = useRef<(text: string) => void>();
  const continuousGenerateRef = useRef<() => void>();
  const continuousCommandIdRef = useRef(0);
  const continuousCleanupTimerRef = useRef<number | null>(null);
  const studioDisposedRef = useRef(false);
  const businessGenerationRef = useRef(0);
  const isBusinessRequestCurrent = (generation: number) => !studioDisposedRef.current && generation === businessGenerationRef.current;
  const onActivityChangeRef = useRef(onActivityChange);
  onActivityChangeRef.current = onActivityChange;
  voiceReviewRef.current = voiceReview;
  mobileWorkspaceOpenRef.current = mobileWorkspaceOpen;

  const continuousControllerRef = useRef<ContinuousVoiceSessionController>();
  if (!continuousControllerRef.current) {
    continuousControllerRef.current = createContinuousVoiceSessionController({
      onState: (next) => {
        if (studioDisposedRef.current) return;
        setContinuousState(next);
        const activity = continuousActivity(next.status);
        if (activity) onActivityChangeRef.current?.(activity);
      },
      onCommand: (command: ContinuousVoiceCommand) => {
        if (studioDisposedRef.current) return;
        if (command.type === 'submit_answer') {
          continuousSubmitRef.current?.(command.text);
          return;
        }
        if (command.type === 'generate_next_question') {
          continuousGenerateRef.current?.();
          return;
        }
        if (command.type === 'preflight' || command.type === 'read_question' || command.type === 'start_recording'
          || command.type === 'stop_recording' || command.type === 'start_transcription' || command.type === 'pause_capture' || command.type === 'resume_capture'
          || command.type === 'cleanup') {
          setContinuousCommand({ id: ++continuousCommandIdRef.current, type: command.type });
        }
      },
    });
  }
  const continuousController = continuousControllerRef.current;

  useEffect(() => {
    studioDisposedRef.current = false;
    if (continuousCleanupTimerRef.current !== null) {
      window.clearTimeout(continuousCleanupTimerRef.current);
      continuousCleanupTimerRef.current = null;
    }
    return () => {
      continuousCleanupTimerRef.current = window.setTimeout(() => {
        studioDisposedRef.current = true;
        businessGenerationRef.current += 1;
        continuousSubmitRef.current = undefined;
        continuousGenerateRef.current = undefined;
        continuousController.close();
        continuousCleanupTimerRef.current = null;
      }, 0);
    };
    // The deferred cleanup survives React StrictMode's effect probe without creating a second controller.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    try {
      if (voiceReview?.saveState === 'unknown') {
        if (attemptId !== null) {
          window.sessionStorage.setItem(recoveryStorageKey, JSON.stringify({ attemptKey: attemptKeyRef.current, attemptId, voiceReview }));
        }
      } else if (voiceReview?.saveState === 'saved') {
        window.sessionStorage.removeItem(recoveryStorageKey);
      } else if (voiceReview?.saveState === 'conflict' || voiceReview?.saveState === 'invalid') {
        window.sessionStorage.removeItem(recoveryStorageKey);
      }
    } catch {
      // Session storage is only a best-effort recovery aid; the server remains authoritative.
    }
  }, [attemptId, recoveryStorageKey, voiceReview]);

  useEffect(() => {
    if (state?.phase !== 'result_unknown' || attemptId === null) return;
    try {
      window.sessionStorage.setItem(studioRecoveryKey, JSON.stringify({
        attemptKey: attemptKeyRef.current,
        attemptId,
        state,
        timeline,
      } satisfies StudioBusinessRecovery));
    } catch {
      // Session storage is only a best-effort recovery aid; the server remains authoritative.
    }
  }, [attemptId, state, studioRecoveryKey, timeline]);

  const clearStudioBusinessRecovery = () => {
    businessRecoveryRef.current = null;
    try { window.sessionStorage.removeItem(studioRecoveryKey); } catch { /* best effort */ }
  };

  const persistStudioStartRecovery = (message: string) => {
    try {
      window.sessionStorage.setItem(studioStartRecoveryKey, JSON.stringify({
        attemptKey: attemptKeyRef.current,
        questionKey: initialQuestionKeyRef.current,
        message,
      } satisfies StudioStartRecovery));
    } catch {
      // Session storage is only a best-effort recovery aid; the server remains authoritative.
    }
  };

  const clearStudioStartRecovery = () => {
    startRecoveryRef.current = null;
    try { window.sessionStorage.removeItem(studioStartRecoveryKey); } catch { /* best effort */ }
  };

  useEffect(() => {
    const studio = studioRef.current;
    if (!studio) return;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    const siblings = studio.parentElement
      ? Array.from(studio.parentElement.children)
        .filter((element) => element !== studio && !(element as HTMLElement).hasAttribute('data-interview-studio-companion'))
        .map((element) => ({ element: element as HTMLElement, inert: Boolean((element as HTMLElement & { inert?: boolean }).inert) }))
      : [];

    document.body.style.overflow = 'hidden';
    for (const sibling of siblings) (sibling.element as HTMLElement & { inert?: boolean }).inert = true;
    (studio.querySelector<HTMLElement>('.topbar button') ?? studio).focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        if (mobileWorkspaceOpenRef.current) {
          setMobileWorkspaceOpen(false);
          window.requestAnimationFrame(() => mobileWorkspaceTriggerRef.current?.focus());
          return;
        }
        closeRequestRef.current();
        return;
      }
      if (event.key !== 'Tab') return;
      const focusable = Array.from(studio.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])')).filter((element) => !element.hidden);
      if (!focusable.length) {
        event.preventDefault();
        studio.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    studio.addEventListener('keydown', onKeyDown);
    return () => {
      studio.removeEventListener('keydown', onKeyDown);
      for (const sibling of siblings) (sibling.element as HTMLElement & { inert?: boolean }).inert = sibling.inert;
      document.body.style.overflow = previousOverflow;
      previousFocus?.focus();
    };
    // A Studio keeps one focus trap for its lifetime; the callback is read from a ref.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    onEvidenceVisibilityChange?.(workspaceTab === 'evidence');
  }, [onEvidenceVisibilityChange, workspaceTab]);

  useEffect(() => {
    setWorkspaceTab('answer');
    setMobileWorkspaceOpen(false);
  }, [state?.turnNo]);

  useEffect(() => {
    if (initialQuestionFocusedRef.current || !state || !timeline.length) return;
    const frame = window.requestAnimationFrame(() => {
      questionHeadingRef.current?.focus();
      initialQuestionFocusedRef.current = true;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [state, timeline.length]);

  useEffect(() => {
    const needsRecoveryFocus = Boolean(terminalFailure)
      || (state?.phase === 'result_unknown' && Boolean(state.error));
    if (!needsRecoveryFocus) return;
    const frame = window.requestAnimationFrame(() => studioStatusRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [state?.error, state?.phase, terminalFailure]);

  const revealFeedbackResult = useCallback(() => {
    const result = feedbackResultRef.current;
    if (!result) return;
    if (typeof result.scrollIntoView === 'function') result.scrollIntoView({ block: 'start' });
    result.focus({ preventScroll: true });
  }, []);

  useEffect(() => {
    if (!proposal) return;
    const frame = window.requestAnimationFrame(revealFeedbackResult);
    return () => window.cancelAnimationFrame(frame);
  }, [proposal, revealFeedbackResult]);

  const update = (action: Parameters<typeof reduceStudioState>[1]) => {
    setState((current) => current ? reduceStudioState(current, action) : current);
  };

  const handleContinuousEvent = (event: VoiceAnswerComposerEvent) => {
    switch (event.type) {
      case 'preflight_succeeded':
        continuousController.preflightSucceeded();
        break;
      case 'preflight_failed':
        continuousController.preflightFailed(event.message);
        break;
      case 'question_read_finished':
        continuousController.questionReadFinished();
        break;
      case 'speech_detected':
        continuousController.speechDetected();
        break;
      case 'silence_detected':
        continuousController.silenceDetected();
        break;
      case 'page_hidden':
        continuousController.pause();
        break;
      case 'recording_stopped':
        continuousController.recordingStopped();
        break;
      case 'review_available':
        continuousController.transcriptReady('');
        break;
      case 'recording_reset':
        continuousController.recordingReset();
        break;
      case 'error':
        continuousController.fallback(event.message);
        break;
      case 'recording_started':
        break;
    }
  };

  const cancelStartRetry = () => {
    if (startRetryTimerRef.current !== null) {
      window.clearTimeout(startRetryTimerRef.current);
      startRetryTimerRef.current = null;
    }
  };

  const start = async () => {
    cancelStartRetry();
    const requestId = ++startRequestRef.current;
    const businessGeneration = businessGenerationRef.current;
    setStartError(null);
    setWorking(true);
    try {
      const result = await startInterviewStudioAttempt({
        context: serviceContext,
        resumeId: context.kind === 'application_event' ? context.resumeId : undefined,
        jdVersionId: context.kind === 'application_event' ? context.jdVersionId : undefined,
        attemptKey: attemptKeyRef.current,
        questionKey: initialQuestionKeyRef.current,
      });
      if (requestId !== startRequestRef.current || !isBusinessRequestCurrent(businessGeneration)) return;
      if (!isTurnResponse(result)) {
        const message = '第一题结果待确认，输入已冻结。请使用原尝试恢复。';
        persistStudioStartRecovery(message);
        setStartError(message);
        const retryAfterMs = 'retry_after_ms' in result && typeof result.retry_after_ms === 'number'
          ? Math.max(250, Math.min(5000, result.retry_after_ms))
          : 1000;
        if (startRetryTimerRef.current === null) {
          startRetryTimerRef.current = window.setTimeout(() => {
            startRetryTimerRef.current = null;
            void start();
          }, retryAfterMs);
        }
        return;
      }
      if (startRetryTimerRef.current !== null) {
        window.clearTimeout(startRetryTimerRef.current);
        startRetryTimerRef.current = null;
      }
      clearStudioStartRecovery();
      setStartError(null);
      setTerminalFailure(null);
      setAttemptId(result.attempt_id);
      setTimeline([{ ...result.turn, confirmed: false }]);
      setState(createStudioState({ turnNo: result.turn.turn_no, question: result.turn.question }));
    } catch (error) {
      if (requestId !== startRequestRef.current || !isBusinessRequestCurrent(businessGeneration)) return;
      const failure = resolveErrorRecovery(error, 'start');
      if (isBlockingFailure(failure)) {
        clearStudioStartRecovery();
        setStartError(null);
        setTerminalFailure(terminalFailureOf(failure, null));
        return;
      }
      if (failure.disposition === 'edit_input') {
        clearStudioStartRecovery();
        if (!failure.preserveIdempotencyKey) {
          attemptKeyRef.current = key('attempt');
          initialQuestionKeyRef.current = key('question');
        }
        setStartError(failure.message);
        return;
      }
      const message = failure.message;
      persistStudioStartRecovery(message);
      setStartError(message);
    } finally {
      if (requestId === startRequestRef.current && isBusinessRequestCurrent(businessGeneration)) setWorking(false);
    }
  };

  useEffect(() => {
    if (businessRecoveryRef.current || startRecoveryRef.current) return;
    const scheduleStartCleanup = () => {
      startCleanupTimerRef.current = window.setTimeout(() => {
        if (startEffectContextRef.current === serviceContext) {
          startEffectContextRef.current = null;
          startRequestRef.current += 1;
          cancelStartRetry();
        }
        startCleanupTimerRef.current = null;
      }, 0);
    };
    if (startCleanupTimerRef.current !== null) {
      window.clearTimeout(startCleanupTimerRef.current);
      startCleanupTimerRef.current = null;
    }
    if (startEffectContextRef.current === serviceContext) return scheduleStartCleanup;
    startEffectContextRef.current = serviceContext;
    void start();
    return scheduleStartCleanup;
    // A Studio instance owns one frozen context and one attempt key.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serviceContext]);

  const appendConfirmedAnswer = (answer: string) => {
    setTimeline((current) => current.map((turn) => turn.turn_no === state?.turnNo ? { ...turn, answer, confirmed: true } : turn));
  };

  const saveVoiceReview = async (review: VoiceReview, currentAttemptId: number): Promise<boolean> => {
    const businessGeneration = businessGenerationRef.current;
    if (!isBusinessRequestCurrent(businessGeneration)) return false;
    try {
      window.sessionStorage.setItem(recoveryStorageKey, JSON.stringify({
        attemptKey: attemptKeyRef.current,
        attemptId: currentAttemptId,
        voiceReview: { ...review, saveState: 'unknown', saveError: undefined },
      } satisfies VoiceReviewRecovery));
    } catch {
      // Session storage is only a best-effort recovery aid; the server remains authoritative.
    }
    setVoiceReview((current) => current ? { ...current, saveState: 'saving' } : current);
    try {
      await saveInterviewStudioVoiceCoachingSnapshot({
        context: serviceContext,
        attemptId: currentAttemptId,
        turnNo: review.turnNo,
        payload: {
          idempotency_key: review.idempotencyKey,
          total_duration_ms: review.summary.totalDurationMs,
          voiced_duration_ms: review.summary.voicedDurationMs,
          pause_count: review.summary.pauseCount,
          longest_pause_ms: review.summary.longestPauseMs,
          speech_rate_cpm: review.summary.speechRateCpm ?? null,
          filler_occurrences: review.summary.fillerOccurrences.map((item) => ({ text: item.text, count: item.count, transcript_offsets: item.transcriptOffsets })),
          reflection_text: '',
          focus_kind: null,
          origin_snapshot_id: null,
        },
      });
      if (!isBusinessRequestCurrent(businessGeneration)) return false;
      setVoiceReview((current) => current ? { ...current, saveState: 'saved' } : current);
      return true;
    } catch (error) {
      if (!isBusinessRequestCurrent(businessGeneration)) return false;
      const response = (error as { response?: { status?: number } } | null)?.response;
      if (response?.status === 409) {
        setVoiceReview((current) => current ? { ...current, saveState: 'conflict', saveError: '表达复盘已存在不同内容，原保存尝试无法覆盖历史快照。' } : current);
      } else if (response?.status === 422) {
        setVoiceReview((current) => current ? { ...current, saveState: 'invalid', saveError: '表达复盘数据未通过校验，回答已提交，可继续文本面试。' } : current);
      } else {
        setVoiceReview((current) => current ? { ...current, saveState: 'unknown', saveError: undefined } : current);
      }
      return false;
    }
  };

  const retryVoiceReview = async () => {
    if (!voiceReview || !attemptId || voiceReview.saveState !== 'unknown') return;
    await saveVoiceReview(voiceReview, attemptId);
  };

  const requestClose = () => {
    const hasUnconfirmedDraft = Boolean(state?.answer.trim())
      && state?.phase === 'answering'
      && !timeline.some((turn) => turn.turn_no === state?.turnNo && turn.confirmed);
    const hasPendingVoiceSave = Boolean(startError)
      || voiceReview?.saveState === 'unknown'
      || voiceReview?.saveState === 'saving'
      || voiceDirty
      || state?.phase === 'result_unknown'
      || ['preflight', 'reading_question', 'waiting_for_speech', 'listening', 'end_candidate', 'transcribing', 'reviewing_transcript', 'submitting_confirmed_answer', 'generating_next_question', 'paused', 'result_unknown'].includes(continuousState.status);
    if ((hasUnconfirmedDraft || hasPendingVoiceSave) && !window.confirm('当前还有未确认的回答或待恢复的语音复盘，确定退出工作台吗？')) return;
    studioDisposedRef.current = true;
    startRequestRef.current += 1;
    cancelStartRetry();
    businessGenerationRef.current += 1;
    continuousSubmitRef.current = undefined;
    continuousGenerateRef.current = undefined;
    continuousController.close();
    onCloseRef.current();
  };
  closeRequestRef.current = requestClose;

  const generateNextQuestion = async (currentState: StudioState, currentAttemptId: number) => {
    const businessGeneration = businessGenerationRef.current;
    if (!isBusinessRequestCurrent(businessGeneration)) return;
    const questionKey = currentState.questionKey ?? key('question');
    if (continuousState.status === 'result_unknown') continuousController.retryNextQuestion();
    update({ type: 'question_submitting', questionKey });
    setWorking(true);
    try {
      const result = await generateInterviewStudioQuestion({
        context: serviceContext,
        attemptId: currentAttemptId,
        turnNo: currentState.turnNo + 1,
        questionKey,
      });
      if (!isBusinessRequestCurrent(businessGeneration)) return;
      if (!isTurnResponse(result)) {
        continuousController.nextQuestionUnknown('下一题结果待确认，已保留原 question key。');
        update({ type: 'result_unknown', operation: 'question', message: '下一题结果待确认，已保留原 question key。' });
        return;
      }
      clearStudioBusinessRecovery();
      setTimeline((current) => [...current, { ...result.turn, confirmed: false }]);
      update({ type: 'question_succeeded', turnNo: result.turn.turn_no, question: result.turn.question });
      continuousController.nextQuestionReady(result.turn.question);
    } catch (error) {
      if (!isBusinessRequestCurrent(businessGeneration)) return;
      const failure = resolveErrorRecovery(error, 'question');
      if (failure.disposition === 'retry_same_key') {
        update({ type: 'result_unknown', operation: 'question', message: failure.message });
        continuousController.nextQuestionUnknown(failure.message);
      } else if (failure.disposition === 'edit_input') {
        update({
          type: 'edit_input',
          operation: 'question',
          preserveIdempotencyKey: failure.preserveIdempotencyKey,
          message: failure.message,
        });
        continuousController.fallback(failure.message);
      } else {
        clearStudioBusinessRecovery();
        setTerminalFailure(terminalFailureOf(failure, currentAttemptId));
        update({ type: 'contract_failed', message: failure.message });
        continuousController.fallback(failure.message);
      }
    } finally {
      if (isBusinessRequestCurrent(businessGeneration)) setWorking(false);
    }
  };

  continuousGenerateRef.current = () => {
    if (state && attemptId) void generateNextQuestion(state, attemptId);
  };

  const submitAnswer = async (answerOverride?: string) => {
    const answer = (answerOverride ?? state?.answer ?? '').trim();
    const businessGeneration = businessGenerationRef.current;
    if (!state || !attemptId || !answer || working || !isBusinessRequestCurrent(businessGeneration)) return;
    const turnKey = state.turnKey ?? key('turn');
    if (continuousState.status === 'result_unknown') continuousController.retryAnswerSubmission();
    update({ type: 'answer_submitting', turnKey });
    setWorking(true);
    try {
      await submitInterviewStudioAnswer({ context: serviceContext, attemptId, turnNo: state.turnNo, answerText: answer, turnKey });
      if (!isBusinessRequestCurrent(businessGeneration)) return;
      appendConfirmedAnswer(answer);
      const reviewForTurn = voiceReviewRef.current?.turnNo === state.turnNo ? voiceReviewRef.current : null;
      if (reviewForTurn) {
        void saveVoiceReview(reviewForTurn, attemptId);
      }
      clearStudioBusinessRecovery();
      update({ type: 'answer_succeeded' });
      setVoiceSubmitRevision((revision) => revision + 1);
      const confirmedState = { ...state, phase: 'answer_confirmed' as const };
      const hasNextQuestion = shouldGenerateNextQuestion(confirmedState);
      if (continuousState.status !== 'disabled' && continuousState.status !== 'fallback_standard') {
        continuousController.answerSubmissionSucceeded(hasNextQuestion);
        if (!hasNextQuestion) setState((current) => current ? reduceStudioState(current, { type: 'answer_succeeded' }) : current);
      } else if (hasNextQuestion) await generateNextQuestion(confirmedState, attemptId);
      else setState((current) => current ? reduceStudioState(current, { type: 'answer_succeeded' }) : current);
    } catch (error) {
      if (!isBusinessRequestCurrent(businessGeneration)) return;
      const failure = resolveErrorRecovery(error, 'answer');
      if (failure.disposition === 'retry_same_key') {
        update({ type: 'result_unknown', operation: 'answer', message: failure.message });
        continuousController.answerSubmissionUnknown(failure.message);
      } else if (failure.disposition === 'edit_input') {
        update({
          type: 'edit_input',
          operation: 'answer',
          preserveIdempotencyKey: failure.preserveIdempotencyKey,
          message: failure.message,
        });
      } else {
        clearStudioBusinessRecovery();
        setTerminalFailure(terminalFailureOf(failure, attemptId));
        update({ type: 'contract_failed', message: failure.message });
        continuousController.fallback(failure.message);
      }
    } finally {
      if (isBusinessRequestCurrent(businessGeneration)) setWorking(false);
    }
  };

  continuousSubmitRef.current = (answer) => { void submitAnswer(answer); };

  const finish = async () => {
    const businessGeneration = businessGenerationRef.current;
    if (!state || !attemptId || working || state.phase === 'answer_submitting' || state.phase === 'next_question_generating' || !isBusinessRequestCurrent(businessGeneration)) return;
    const feedbackKey = state.feedbackKey ?? key('feedback');
    update({ type: 'feedback_submitting', feedbackKey });
    setWorking(true);
    try {
      const result = await finishInterviewStudio({ context: serviceContext, attemptId, feedbackKey });
      if (!isBusinessRequestCurrent(businessGeneration)) return;
      if (!('proposal' in result)) {
        update({ type: 'result_unknown', operation: 'feedback', message: '复盘结果待确认，已保留原 feedback key。' });
        return;
      }
      clearStudioBusinessRecovery();
      setProposal(result);
      setState((current) => current ? { ...current, pendingOperation: null, resultUnknown: false, phase: 'completed' } : current);
    } catch (error) {
      if (!isBusinessRequestCurrent(businessGeneration)) return;
      const failure = resolveErrorRecovery(error, 'feedback');
      if (failure.disposition === 'retry_same_key') {
        update({ type: 'result_unknown', operation: 'feedback', message: failure.message });
      } else if (failure.disposition === 'edit_input') {
        update({
          type: 'edit_input',
          operation: 'feedback',
          preserveIdempotencyKey: failure.preserveIdempotencyKey,
          message: failure.message,
        });
      } else {
        clearStudioBusinessRecovery();
        setTerminalFailure(terminalFailureOf(failure, attemptId));
        update({ type: 'contract_failed', message: failure.message });
      }
    } finally {
      if (isBusinessRequestCurrent(businessGeneration)) setWorking(false);
    }
  };

  const retry = () => {
    if (startError) {
      void start();
      return;
    }
    if (!state || !attemptId) return;
    if (state.pendingOperation === 'answer') void submitAnswer();
    else if (state.pendingOperation === 'question') void generateNextQuestion(state, attemptId);
    else if (state.pendingOperation === 'feedback') void finish();
    else if (state.pendingOperation === 'start') void start();
  };

  const title = context.kind === 'quick_practice'
    ? `${context.positionName} · 快速练习`
    : `${context.companyName ?? '真实投递'} · ${context.positionName ?? '模拟面试'}`;
  const currentQuestion = state?.question ?? '正在准备第一题…';
  const enableContinuous = () => {
    try { window.localStorage.setItem(CONTINUOUS_VOICE_PREFERENCE_KEY, 'true'); } catch { /* preference is best effort */ }
    continuousController.enable(currentQuestion);
  };

  const restartAfterTerminalFailure = async () => {
    if (!terminalFailure || working) return;
    const failed = terminalFailure;
    const generation = ++businessGenerationRef.current;
    startRequestRef.current += 1;
    cancelStartRetry();
    setWorking(true);
    try {
      if (failed.attemptId !== null && failed.attemptRetention === 'absent') {
        try {
          await discardInterviewStudioAttempt({ context: serviceContext, attemptId: failed.attemptId });
        } catch (error) {
          const status = (error as { response?: { status?: number } } | null)?.response?.status;
          if (status !== 404) throw error;
        }
      }
      if (!isBusinessRequestCurrent(generation)) return;
      clearStudioBusinessRecovery();
      clearStudioStartRecovery();
      try { window.sessionStorage.removeItem(recoveryStorageKey); } catch { /* best effort */ }
      attemptKeyRef.current = key('attempt');
      initialQuestionKeyRef.current = key('question');
      initialQuestionFocusedRef.current = false;
      setAttemptId(null);
      setTimeline([]);
      setState(null);
      setProposal(null);
      setVoiceReview(null);
      setVoiceDirty(false);
      setTerminalFailure(null);
      setStartError(null);
      continuousController.disable();
      await start();
    } catch {
      if (!isBusinessRequestCurrent(generation)) return;
      setTerminalFailure({ ...failed, message: '旧练习清理结果待确认，暂未创建新的练习。请稍后重试。' });
    } finally {
      if (isBusinessRequestCurrent(generation)) setWorking(false);
    }
  };
  const disableContinuous = () => {
    if (working) return;
    businessGenerationRef.current += 1;
    try { window.localStorage.setItem(CONTINUOUS_VOICE_PREFERENCE_KEY, 'false'); } catch { /* preference is best effort */ }
    continuousController.disable();
  };
  const fallbackContinuous = () => {
    continuousController.fallback('连续语音不可用，已保留标准回答。');
  };
  const continuousInProgress = !['disabled', 'fallback_standard', 'closed'].includes(continuousState.status);
  const feedbackGenerating = working && state?.pendingOperation === 'feedback';
  const feedbackIsSafeEmpty = proposal?.proposal_status === 'safe_empty';
  const feedbackItems = proposal
    ? [...proposal.proposal.strengths, ...proposal.proposal.practice_points, ...proposal.proposal.next_practice_steps].slice(0, 4)
    : [];
  const canSubmit = Boolean(state?.answer.trim()) && !working && !state?.resultUnknown && state?.phase !== 'completed' && !continuousInProgress;
  const hasConfirmedAnswer = Boolean(state && timeline.some((turn) => turn.turn_no === state.turnNo && turn.confirmed));
  const currentTimelineTurn = timeline.find((turn) => turn.turn_no === state?.turnNo) ?? timeline[timeline.length - 1];
  const currentEvidence = buildEvidenceEntries(currentTimelineTurn?.basis_refs);
  const contextJdReference = {
    source: 'jd',
    path: '/jd/text',
    excerpt: previewText(context.jdText),
  };

  const focusEvidence = (entry: StudioEvidenceEntry) => {
    setWorkspaceTab('evidence');
    setMobileWorkspaceOpen(true);
    setSelectedEvidenceKey(entry.key);
  };

  const openAnswerWorkspace = () => {
    setWorkspaceTab('answer');
    setMobileWorkspaceOpen(true);
    window.requestAnimationFrame(() => answerTabRef.current?.focus());
  };

  useEffect(() => {
    if (!selectedEvidenceKey || workspaceTab !== 'evidence') return;
    const frame = window.requestAnimationFrame(() => {
      const target = Array.from(document.querySelectorAll<HTMLElement>('[data-evidence-key]'))
        .find((element) => element.dataset.evidenceKey === selectedEvidenceKey);
      if (typeof target?.scrollIntoView === 'function') {
        target.scrollIntoView({ block: 'center', behavior: 'smooth' });
      }
    });
    return () => window.cancelAnimationFrame(frame);
  }, [selectedEvidenceKey, workspaceTab]);

  return (
    <div ref={studioRef} className={styles.studio} data-testid="interview-studio" data-interview-studio role="dialog" tabIndex={-1} aria-modal="true" aria-labelledby="interview-studio-title">
      <header className={styles.topbar}>
        <Button type="text" icon={<ArrowLeftOutlined />} onClick={() => closeRequestRef.current()}>退出工作台</Button>
        <div className={styles.titleBlock}>
          <span className={styles.kicker}>{context.kind === 'quick_practice' ? '快速练习' : '真实投递'}</span>
          <h1 id="interview-studio-title">{title}</h1>
        </div>
        <div className={styles.topActions}>
          <span className={styles.round}>{state ? `第 ${state.turnNo} / ${state.maxTurns} 轮` : '准备中'}</span>
          <Tag color={startError || terminalFailure || state?.phase === 'result_unknown' ? 'orange' : 'green'}>{terminalFailure ? '需要重新开始' : startError || state?.phase === 'result_unknown' ? '结果待确认' : '来源已冻结'}</Tag>
          {onToggleHaru ? <Button type="text" icon={<MenuOutlined />} onClick={onToggleHaru}>显示 Haru</Button> : null}
          <Button
            onClick={() => void finish()}
            loading={feedbackGenerating}
            aria-busy={feedbackGenerating}
            disabled={!attemptId || !state || working || Boolean(proposal) || state?.phase === 'result_unknown' || state?.phase === 'contract_failed' || ['preflight', 'reading_question', 'waiting_for_speech', 'listening', 'end_candidate', 'transcribing', 'reviewing_transcript', 'submitting_confirmed_answer', 'generating_next_question', 'paused', 'result_unknown'].includes(continuousState.status)}
          >
            {feedbackGenerating ? '正在生成复盘…' : '结束并生成复盘'}
          </Button>
        </div>
      </header>

      <main className={styles.main}>
        <section className={`${styles.timeline} ${styles.conversationPane}`} aria-label="面试对话时间线">
          <div className={styles.timelineHeader}><div><span className={styles.kicker}>面试对话</span><h2>保持对话，答案由你确认</h2></div><span className={styles.livePill}><i /> {state?.phase === 'contract_failed' ? '需要重新开始' : state?.phase === 'result_unknown' ? '需要对账' : '本轮进行中'}</span></div>
          {feedbackGenerating ? (
            <div className={`${styles.studioStatus} ${styles.feedbackProgress}`} data-interview-studio-status role="status" aria-live="polite">
              <span className={styles.loader} />
              <div><strong>正在生成复盘，通常需要几十秒</strong><span>请保持当前页面打开；系统只会提交一次，完成后会在这里显示结果。</span></div>
            </div>
          ) : startError ? (
            <div className={styles.studioStatus} data-interview-studio-status>
              <Alert className={styles.alert} type="warning" showIcon message={startError} action={<Button size="small" onClick={retry} disabled={working}>使用原尝试恢复</Button>} />
            </div>
          ) : terminalFailure ? (
            <div ref={studioStatusRef} className={styles.studioStatus} data-interview-studio-status tabIndex={-1}>
              <Alert
                className={styles.alert}
                type="warning"
                showIcon
                message={terminalFailure.message}
                action={terminalFailure.userAction === 'reload_source' ? (
                  <Button size="small" onClick={requestClose} disabled={working}>退出并重新加载</Button>
                ) : terminalFailure.userAction === 'restart_new_attempt' ? (
                  <Button size="small" onClick={() => void restartAfterTerminalFailure()} disabled={working}>重新开始练习</Button>
                ) : (
                  <Button size="small" onClick={requestClose} disabled={working}>关闭</Button>
                )}
              />
            </div>
          ) : state?.phase === 'result_unknown' && state.error ? (
            <div ref={studioStatusRef} className={styles.studioStatus} data-interview-studio-status tabIndex={-1}>
              <Alert className={styles.alert} type="warning" showIcon message={state.error} action={<Button size="small" onClick={retry} disabled={working}>使用原尝试恢复</Button>} />
            </div>
          ) : state?.error ? (
            <div className={styles.studioStatus} data-interview-studio-status>
              <Alert className={styles.alert} type="warning" showIcon message={state.error} />
            </div>
          ) : proposal ? (
            <div className={styles.studioStatus} data-interview-studio-status role="status" aria-live="polite">
              <Alert className={styles.alert} type="success" showIcon message={feedbackIsSafeEmpty ? '复盘已完成，暂无可验证建议' : '复盘已生成'} action={<Button size="small" onClick={revealFeedbackResult}>查看复盘</Button>} />
            </div>
          ) : null}
          <div className={styles.conversationScroll} data-interview-conversation-scroll>
            <div className={styles.turnList} aria-live="polite">
              {timeline.map((turn) => (
                <article key={turn.turn_no} className={`${styles.turn} ${turn.turn_no === state?.turnNo ? styles.activeTurn : ''}`}>
                  <div className={styles.turnMarker}>{String(turn.turn_no).padStart(2, '0')}</div>
                  <div className={styles.turnBody}>
                    <div className={styles.turnMeta}><span>面试官</span>{turn.turn_no > 1 ? <Tag>{questionLabel(turn)}</Tag> : null}<span className={styles.turnState}>{turn.confirmed ? '回答已确认' : turn.turn_no === state?.turnNo ? '等待回答' : ''}</span></div>
                    <h3 ref={turn.turn_no === state?.turnNo ? questionHeadingRef : undefined} tabIndex={turn.turn_no === state?.turnNo ? -1 : undefined} data-interview-studio-question>{turn.question}</h3>
                    {buildEvidenceEntries(turn.basis_refs).length ? (
                      <div className={styles.turnEvidence} aria-label="提问依据" data-interview-studio-evidence-trigger>
                        <span className={styles.evidenceLabel}>提问依据</span>
                        {buildEvidenceEntries(turn.basis_refs).map((entry) => (
                          <button key={entry.key} type="button" className={styles.evidenceLink} onClick={() => focusEvidence(entry)}>{entry.label}</button>
                        ))}
                      </div>
                    ) : null}
                    {turn.question_kind === 'follow_up' && turn.parent_turn_no ? (
                      <button type="button" className={styles.followUpLink} data-interview-studio-follow-up onClick={() => {
                        const entry = buildEvidenceEntries(turn.basis_refs)[0];
                        if (entry) focusEvidence(entry);
                      }}>上一轮回答 → 当前追问</button>
                    ) : null}
                    {turn.turn_no === state?.turnNo && !turn.confirmed ? <span className={styles.questionHint}>当前问题 · 先回答，再由你确认提交</span> : null}
                    {turn.answer && turn.confirmed ? (
                      <div className={styles.candidateMessage} data-interview-speaker="candidate">
                        <span>你</span>
                        <p className={styles.answerBubble}>{turn.answer}</p>
                      </div>
                    ) : null}
                  </div>
                </article>
              ))}
              {!timeline.length ? <div className={styles.loadingTurn}><span className={styles.loader} />正在创建冻结 Attempt…</div> : null}
            </div>
            {state?.phase === 'next_question_generating' ? <div className={styles.generating} role="status" aria-live="polite"><span className={styles.loader} />正在根据已确认回答准备下一题…</div> : null}
            {voiceReview?.saveState === 'unknown' ? <Alert className={styles.alert} type="warning" showIcon message="表达复盘保存结果待确认，原保存尝试已保留。" action={<Button size="small" onClick={() => void retryVoiceReview()} disabled={working}>使用原尝试恢复</Button>} /> : null}
            {voiceReview?.saveError ? <Alert className={styles.alert} type="warning" showIcon message={voiceReview.saveError} /> : null}
            {state?.phase === 'completed' && !proposal ? <div className={styles.completeCard}><CheckCircleOutlined /><div><strong>本轮已完成</strong><span>你可以结束并生成复盘，或退出保留已确认的回答。</span></div></div> : null}
            {proposal ? <section ref={feedbackResultRef} tabIndex={-1} data-interview-feedback-result className={styles.feedbackCard} aria-label="复盘建议"><span className={styles.kicker}>复盘建议</span><h2>{feedbackIsSafeEmpty ? '复盘已完成' : '复盘建议已准备好'}</h2><p>{feedbackIsSafeEmpty ? '本轮没有生成可验证的复盘建议；已确认回答仍然保留。' : '建议只来自本次已确认回答与冻结来源。正式投递和快速练习会保持各自的来源边界。'}</p>{feedbackItems.length ? <ul>{feedbackItems.map((item) => <li key={item.id}>{item.text}</li>)}</ul> : null}</section> : null}
          </div>
          <Button
            ref={mobileWorkspaceTriggerRef}
            className={styles.mobileWorkspaceTrigger}
            type="primary"
            aria-expanded={mobileWorkspaceOpen}
            aria-controls="interview-answer-workspace"
            onClick={openAnswerWorkspace}
          >
            打开回答工作台
          </Button>
        </section>

        <aside id="interview-answer-workspace" className={`${styles.answerWorkspace} ${mobileWorkspaceOpen ? styles.mobileWorkspaceOpen : ''}`} aria-label="回答与依据工作台">
          <div className={styles.workspaceHeader}>
            <div className={styles.workspaceTabs} role="tablist" aria-label="回答工作台视图">
              <button ref={answerTabRef} type="button" role="tab" aria-selected={workspaceTab === 'answer'} aria-controls="interview-answer-panel" className={workspaceTab === 'answer' ? styles.activeWorkspaceTab : ''} onClick={() => setWorkspaceTab('answer')}>回答</button>
              <button type="button" role="tab" aria-selected={workspaceTab === 'evidence'} aria-controls="interview-evidence-panel" className={workspaceTab === 'evidence' ? styles.activeWorkspaceTab : ''} onClick={() => setWorkspaceTab('evidence')}>依据</button>
            </div>
            <Button className={styles.mobileWorkspaceClose} type="text" onClick={() => {
              setMobileWorkspaceOpen(false);
              window.requestAnimationFrame(() => mobileWorkspaceTriggerRef.current?.focus());
            }}>关闭回答工作台</Button>
          </div>

          {workspaceTab === 'answer' ? (
            <div id="interview-answer-panel" role="tabpanel" className={styles.answerWorkspacePanel} aria-label="回答工作台">
              <div className={styles.answerWorkspaceBody}>
                <ContinuousVoiceModePanel
                  status={continuousState.status}
                  countdownSeconds={continuousState.countdownSeconds}
                  error={continuousState.error}
                  disabled={!state || working || Boolean(startError) || Boolean(terminalFailure) || ['result_unknown', 'contract_failed', 'completed', 'next_question_generating', 'answer_submitting', 'feedback_submitting'].includes(state?.phase ?? '')}
                  onEnable={enableContinuous}
                  onDisable={disableContinuous}
                  onSkipReading={() => continuousController.skipReading()}
                  onCancelCountdown={() => continuousController.cancelEndCandidate()}
                  onPause={() => continuousController.pause()}
                  onResume={() => continuousController.resume()}
                  onStop={() => continuousController.manualStop()}
                  onFallback={fallbackContinuous}
                />
                <VoiceAnswerComposer
                  question={currentQuestion}
                  disabled={!state || Boolean(startError) || Boolean(terminalFailure) || working || state?.phase === 'result_unknown' || state?.phase === 'contract_failed' || state?.phase === 'completed' || state?.phase === 'next_question_generating'}
                  textValue={state?.answer ?? ''}
                  onTextChange={(answer) => update({ type: 'draft_changed', answer })}
                  submitRevision={voiceSubmitRevision}
                  onConfirmTranscript={(answer) => {
                    update({ type: 'transcript_ready', answer });
                    update({ type: 'transcript_confirmed' });
                  }}
                  onVoiceReviewConfirmed={(answer, summary) => {
                    update({ type: 'transcript_ready', answer });
                    update({ type: 'transcript_confirmed' });
                    const review = { turnNo: state?.turnNo ?? 1, summary, saveState: 'idle' as const, idempotencyKey: key('voice') };
                    voiceReviewRef.current = review;
                    setVoiceReview(review);
                    if (continuousState.status === 'reviewing_transcript') {
                      continuousController.confirmTranscript(answer);
                      return;
                    }
                  }}
                  onAnswerModeChange={(mode) => update({ type: 'answer_mode', mode })}
                  continuous
                  compact
                  continuousCommand={continuousCommand}
                  onContinuousEvent={handleContinuousEvent}
                  onDirtyChange={setVoiceDirty}
                  onActivityChange={onActivityChange}
                />
              </div>
              <div className={styles.workspaceActions}>
                <span>{state?.answerMode === 'voice' ? '语音必须先核对文字，再进入同一个提交流程。' : '提交后回答会冻结，系统自动准备下一题。'}</span>
                <Button type="primary" size="large" icon={<SendOutlined />} disabled={!canSubmit} onClick={() => void submitAnswer()}>确认并提交回答</Button>
                {hasConfirmedAnswer && state?.phase === 'answering' ? <span className={styles.confirmedHint}>回答已经发送，正在准备下一步…</span> : null}
              </div>
            </div>
          ) : (
            <section id="interview-evidence-panel" role="tabpanel" className={`${styles.evidence} ${styles.evidenceWorkspaceBody}`} aria-label="本轮依据">
              <div className={styles.evidenceHeader}><div><span className={styles.kicker}>本轮来源</span><h2>提问依据</h2></div><Button type="text" onClick={() => setWorkspaceTab('answer')}>返回回答</Button></div>
              <div className={styles.sourceCard} data-evidence-key={evidenceKey(contextJdReference)} data-evidence-active={selectedEvidenceKey === evidenceKey(contextJdReference)} data-evidence-expanded={jdExpanded}>
                <FileTextOutlined />
                <div><strong>JD · 冻结版本</strong><p>{jdExpanded ? context.jdText : previewText(context.jdText)}</p>{context.jdText.length > 180 ? <button type="button" className={styles.expandEvidence} onClick={() => setJdExpanded((expanded) => !expanded)}>{jdExpanded ? '收起全文' : '展开全文'}</button> : null}</div>
              </div>
              <div className={styles.sourceCard}><FileTextOutlined /><div><strong>简历 · 已选快照</strong><p>已使用候选人确认的第 {context.resumeId} 份简历快照；原始内容不会在 Studio 中编辑。</p></div></div>
              <div className={styles.evidenceList} aria-label="当前问题引用"><strong>当前问题引用</strong>{currentEvidence.length ? currentEvidence.map((entry) => (
                <button type="button" key={entry.key} className={styles.evidenceExcerpt} data-evidence-key={entry.key} data-evidence-active={selectedEvidenceKey === entry.key} onClick={() => focusEvidence(entry)}><span>{entry.label}</span><q>{entry.excerpt}</q></button>
              )) : <p className={styles.emptyEvidence}>当前问题的来源正在整理，旧历史仍保持只读。</p>}</div>
              <div className={styles.sourceNote}>快速练习只关联 Practice Case，不会写入投递、日程、Knowledge、Memory、Story 或 Offer。</div>
            </section>
          )}
        </aside>
      </main>
    </div>
  );
}
