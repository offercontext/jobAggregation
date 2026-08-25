import { useEffect, useMemo, useRef, useState } from 'react';
import { Checkbox, Input, Select } from 'antd';
import type { Application } from '@/types/application';
import type { ScheduleEvent } from '@/types/event';
import type { Resume } from '@/types/resume';
import { getCurrentApplicationJd } from '@/services/applicationJdVersions';
import { createInterviewPracticeCase } from '@/services/interviewPracticeCases';
import {
  buildQuickPracticeReadiness,
  buildRealInterviewReadiness,
  type QuickPracticeDraft,
  type ReadinessItem,
} from './interviewReadinessModel';
import styles from './InterviewReadinessCenter.module.css';

const { TextArea } = Input;

export interface RealInterviewStudioContext {
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
  positionName: string;
  jdText: string;
  resumeId: number;
}

interface Props {
  applications?: Application[];
  events?: ScheduleEvent[];
  resumes?: Resume[];
  /** The same readiness surface serves real events and free practice. */
  initialMode?: 'real' | 'quick';
  /** When embedded in a task tab, prevent switching into a second formal entry. */
  fixedMode?: 'real' | 'quick';
  actionEmphasis?: 'primary' | 'secondary';
  onOpenApplication?: (applicationId: number) => void;
  onOpenPreparation?: (applicationId: number, eventId: number) => void;
  onOpenStudio?: (context: RealInterviewStudioContext | QuickPracticeStudioContext) => void;
}

const STATUS_COPY: Record<ReadinessItem['status'], string> = {
  ready: '已就绪',
  needs_input: '需要补充',
  source_changed: '原资料已更新，本次结果仍使用旧版',
  unknown: '暂时未知',
  unavailable: '暂时不可用',
};

function makeKey(prefix: string): string {
  return `${prefix}-${typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : Date.now()}`;
}

function Checklist({ items, onAction }: { items: ReadinessItem[]; onAction?: (item: ReadinessItem) => void }) {
  return (
    <div className={styles.checklist} aria-label="开始前检查">
      {items.map((item) => (
        <div className={styles.checkItem} key={item.key} data-status={item.status}>
          <span className={styles.checkIcon} aria-hidden="true">{item.status === 'ready' ? '✓' : '·'}</span>
          <div className={styles.checkCopy}>
            <div className={styles.checkHeading}>
              <strong>{item.label}</strong>
              <span className={styles.status}>{STATUS_COPY[item.status]}</span>
            </div>
            <p>{item.detail}</p>
          </div>
          {item.actionLabel && onAction ? (
            <button type="button" className={styles.inlineAction} onClick={() => onAction(item)}>{item.actionLabel}</button>
          ) : null}
        </div>
      ))}
    </div>
  );
}

export default function InterviewReadinessCenter({
  applications = [],
  events = [],
  resumes = [],
  initialMode = 'real',
  fixedMode,
  actionEmphasis = 'primary',
  onOpenApplication,
  onOpenPreparation,
  onOpenStudio,
}: Props) {
  const [mode, setMode] = useState<'real' | 'quick'>(fixedMode ?? initialMode);
  const [applicationId, setApplicationId] = useState<number | null>(null);
  const [eventId, setEventId] = useState<number | null>(null);
  const [resumeId, setResumeId] = useState<number | undefined>();
  const [jdState, setJdState] = useState<{ status: 'ready' | 'missing' | 'unknown'; id?: number; text?: string }>({ status: 'missing' });
  const [quickDraft, setQuickDraft] = useState<QuickPracticeDraft>({ positionName: '', jdText: '', jdConfirmed: false, resumeId: undefined });
  const [quickError, setQuickError] = useState<string | null>(null);
  const [creatingCase, setCreatingCase] = useState(false);
  const quickCaseKeyRef = useRef<string | null>(null);
  const quickCaseFingerprintRef = useRef<string | null>(null);
  const latestQuickDraftRef = useRef(quickDraft);
  latestQuickDraftRef.current = quickDraft;

  const visibleApplications = useMemo(
    () => applications.filter((item) => !item.deleted_at && item.status !== 'closed'),
    [applications],
  );
  const interviewEvents = useMemo(
    () => events.filter((event) => event.event_type === 'interview' && Boolean(event.scheduled_at) && event.status !== 'cancelled' && (applicationId === null || event.application_id === applicationId)),
    [applicationId, events],
  );
  const selectedApplication = visibleApplications.find((item) => item.id === applicationId) ?? null;
  const selectedEvent = interviewEvents.find((item) => item.id === eventId) ?? null;
  const selectedResume = resumes.find((item) => item.id === resumeId && !item.deleted_at) ?? null;

  useEffect(() => {
    if (applicationId !== null && !visibleApplications.some((item) => item.id === applicationId)) {
      setApplicationId(null);
    }
  }, [applicationId, visibleApplications]);

  useEffect(() => {
    if (eventId !== null && !interviewEvents.some((item) => item.id === eventId)) {
      setEventId(null);
    }
  }, [eventId, interviewEvents]);

  useEffect(() => {
    if (resumeId !== undefined && !resumes.some((item) => item.id === resumeId && !item.deleted_at)) {
      setResumeId(undefined);
    }
  }, [resumeId, resumes]);

  useEffect(() => {
    setEventId(null);
    setJdState({ status: applicationId === null ? 'missing' : 'unknown' });
  }, [applicationId]);

  useEffect(() => {
    if (!applicationId) {
      setJdState({ status: 'missing' });
      return;
    }
    let active = true;
    setJdState({ status: 'unknown' });
    void getCurrentApplicationJd(applicationId).then((result) => {
      if (!active) return;
      setJdState(result.current ? { status: 'ready', id: result.current.id, text: result.current.jd_text } : { status: 'missing' });
    }).catch(() => {
      if (active) setJdState({ status: 'unknown' });
    });
    return () => { active = false; };
  }, [applicationId]);

  useEffect(() => {
    if (eventId !== null && !interviewEvents.some((item) => item.id === eventId)) setEventId(null);
  }, [eventId, interviewEvents]);

  const realReadiness = buildRealInterviewReadiness({
    application: selectedApplication ? { id: selectedApplication.id } : null,
    jd: jdState,
    resume: selectedResume ? { id: selectedResume.id } : null,
    event: selectedEvent ? { id: selectedEvent.id } : null,
  });
  const quickReadiness = buildQuickPracticeReadiness(quickDraft);
  const readiness = mode === 'real' ? realReadiness : quickReadiness;

  const focusControl = (id: string) => {
    const target = document.getElementById(id);
    target?.focus();
    target?.querySelector<HTMLElement>('.ant-select-selector')?.focus();
  };

  const quickDraftFingerprint = (draft: QuickPracticeDraft) => JSON.stringify({
    positionName: draft.positionName.trim(),
    jdText: draft.jdText,
    jdConfirmed: draft.jdConfirmed,
    resumeId: draft.resumeId ?? null,
  });

  const startQuickPractice = async () => {
    const draft = quickDraft;
    const resumeId = draft.resumeId;
    if (!quickReadiness.ready || !resumeId) return;
    const draftFingerprint = quickDraftFingerprint(draft);
    if (quickCaseFingerprintRef.current !== draftFingerprint) {
      quickCaseKeyRef.current = null;
      quickCaseFingerprintRef.current = draftFingerprint;
    }
    const idempotencyKey = quickCaseKeyRef.current ?? makeKey('quick-case');
    quickCaseKeyRef.current = idempotencyKey;
    setQuickError(null);
    setCreatingCase(true);
    try {
      const practiceCase = await createInterviewPracticeCase({
        idempotencyKey,
        positionName: draft.positionName.trim(),
        jdText: draft.jdText,
        resumeId,
      });
      if (draftFingerprint !== quickDraftFingerprint(latestQuickDraftRef.current)) {
        quickCaseKeyRef.current = null;
        setQuickError('练习资料已改变，已取消本次冻结；请确认后重新开始。');
        return;
      }
      quickCaseKeyRef.current = null;
      onOpenStudio?.({
        kind: 'quick_practice',
        caseId: practiceCase.id,
        positionName: practiceCase.position_name_snapshot,
        jdText: practiceCase.jd_text_snapshot,
        resumeId: practiceCase.resume_id,
      });
    } catch (error) {
      const code = (error as { response?: { data?: { error_code?: string } } })?.response?.data?.error_code;
      setQuickError(code === 'interview_practice_case_idempotency_conflict' ? '快速练习档案内容已变化，请重新确认后再试。' : '快速练习档案结果待确认，输入已冻结；请使用原 key 重试。');
    } finally {
      setCreatingCase(false);
    }
  };

  const actionFor = (item: ReadinessItem) => {
    if (item.key === 'application') focusControl('readiness-application');
    if (item.key === 'resume') focusControl(mode === 'quick' ? 'quick-readiness-resume' : 'readiness-resume');
    if (item.key === 'event') focusControl('readiness-event');
    if (item.key === 'jd') {
      if (mode === 'quick') focusControl('quick-readiness-jd');
      else if (selectedApplication && selectedEvent) onOpenPreparation?.(selectedApplication.id, selectedEvent.id);
      else if (selectedApplication) onOpenApplication?.(selectedApplication.id);
      else focusControl('readiness-application');
    }
    if (item.key === 'preparation' && mode === 'real') {
      if (selectedApplication && selectedEvent) onOpenPreparation?.(selectedApplication.id, selectedEvent.id);
      else focusControl('readiness-application');
    }
  };

  return (
    <section className={styles.surface} data-testid="interview-readiness-center" data-readiness-mode={mode} aria-labelledby="readiness-title">
      <div className={styles.hero}>
        <div>
          <span className={styles.kicker}>面试准备</span>
          <h1 id="readiness-title">面试准备中心</h1>
          <p>先把要带进面试的证据准备好，再进入一间只属于这次练习的工作台。</p>
        </div>
        <div className={styles.heroNote}><span className={styles.liveDot} /> 证据门控已开启</div>
      </div>

      {!fixedMode ? <div className={styles.modeGrid} role="tablist" aria-label="练习模式">
        <button type="button" role="tab" aria-selected={mode === 'real'} className={styles.modeCard} data-active={mode === 'real'} onClick={() => setMode('real')}>
          <span className={styles.modeNumber}>01</span>
          <span className={styles.modeTitle}>围绕真实投递练习</span>
          <span className={styles.modeDescription}>绑定当前投递、已确认 JD、简历与已排期面试。</span>
          <span className={styles.modeMeta}>适合面试前的真实准备</span>
        </button>
        <button type="button" role="tab" aria-selected={mode === 'quick'} className={styles.modeCard} data-active={mode === 'quick'} onClick={() => setMode('quick')}>
          <span className={styles.modeNumber}>02</span>
          <span className={styles.modeTitle}>快速练习</span>
          <span className={styles.modeDescription}>只针对一个岗位开始，不创建虚假的投递或日程。</span>
          <span className={styles.modeMeta}>适合临时热身与探索岗位</span>
        </button>
      </div> : null}

      <div className={styles.workspaceGrid}>
        <div className={styles.prepPanel}>
          <div className={styles.panelHeader}>
            <div><span className={styles.eyebrow}>准备检查</span><h2>开始前检查</h2></div>
            <span className={styles.roundBadge}>{readiness.ready ? '可以出发' : '还差一点'}</span>
          </div>
          {mode === 'real' ? (
            <>
              <div className={styles.controls}>
                <label>选择投递<Select id="readiness-application" value={applicationId ?? undefined} placeholder="请选择投递" allowClear onChange={(value) => setApplicationId(value ?? null)} options={visibleApplications.map((item) => ({ value: item.id, label: `${item.company_name} · ${item.position_name}` }))} /></label>
                <label>选择面试事件<Select id="readiness-event" value={eventId ?? undefined} placeholder="请选择已排期面试" allowClear onChange={(value) => setEventId(value ?? null)} options={interviewEvents.map((item) => ({ value: item.id, label: `${item.round ? `第 ${item.round} 轮 · ` : ''}${new Date(item.scheduled_at).toLocaleString()}` }))} /></label>
                <label>选择简历<Select id="readiness-resume" value={resumeId} placeholder="请选择已保存简历" allowClear onChange={(value) => setResumeId(value)} options={resumes.filter((item) => !item.deleted_at).map((item) => ({ value: item.id, label: item.title || item.name || `简历 ${item.id}` }))} /></label>
              </div>
              <div className={styles.readOnlyJd} aria-live="polite">
                <span>当前 JD（只读）</span>
                <p>{jdState.status === 'ready' ? jdState.text : jdState.status === 'unknown' ? '正在读取当前已确认版本…' : '尚未找到当前已确认的岗位资料版本。'}</p>
              </div>
            </>
          ) : (
            <div className={styles.controls}>
              <label>岗位名称<Input value={quickDraft.positionName} maxLength={200} placeholder="例如：后端工程师" onChange={(event) => setQuickDraft((current) => ({ ...current, positionName: event.target.value }))} /></label>
              <label>粘贴 JD<TextArea id="quick-readiness-jd" value={quickDraft.jdText} rows={5} placeholder="粘贴你已核对的岗位描述原文，不抓取 URL。" onChange={(event) => setQuickDraft((current) => ({ ...current, jdText: event.target.value }))} /></label>
              <label className={styles.confirmLabel}><Checkbox checked={quickDraft.jdConfirmed} onChange={(event) => setQuickDraft((current) => ({ ...current, jdConfirmed: event.target.checked }))}>已核对，本次按此岗位资料练习</Checkbox></label>
              <label>选择简历<Select id="quick-readiness-resume" value={quickDraft.resumeId} placeholder="请选择已保存简历" allowClear onChange={(value) => setQuickDraft((current) => ({ ...current, resumeId: value }))} options={resumes.filter((item) => !item.deleted_at).map((item) => ({ value: item.id, label: item.title || item.name || `简历 ${item.id}` }))} /></label>
            </div>
          )}
          <Checklist items={readiness.items} onAction={actionFor} />
          {quickError ? <div className={styles.error} role="alert">{quickError}</div> : null}
          <button
            type="button"
            className={actionEmphasis === 'primary' ? styles.primaryAction : styles.secondaryAction}
            disabled={!readiness.ready || creatingCase}
            onClick={() => {
              if (mode === 'quick') void startQuickPractice();
              else if (selectedApplication && selectedEvent && selectedResume && jdState.id && jdState.text) onOpenStudio?.({ kind: 'application_event', applicationId: selectedApplication.id, eventId: selectedEvent.id, resumeId: selectedResume.id, jdVersionId: jdState.id, jdText: jdState.text, companyName: selectedApplication.company_name, positionName: selectedApplication.position_name });
            }}
          >{creatingCase ? '正在冻结练习资料…' : '进入模拟面试'}<span aria-hidden="true">↗</span></button>
          <p className={styles.privacyNote}>进入后仍会保留人工确认、原 key 恢复和来源状态说明。未确认的语音转写只在浏览器处理。</p>
        </div>

        <aside className={styles.sidePanel} aria-label="面试说明">
          <div className={styles.sideBlock}><span className={styles.eyebrow}>本次输入</span><h3>只带入已确认来源</h3><p>冻结的 JD、明确选择的简历、已排期面试，以及本次练习中已经确认的回答。</p></div>
          <div className={styles.sideBlock}><span className={styles.eyebrow}>仅在本地</span><h3>什么不会离开浏览器</h3><p>原始音频、临时转写、VAD 帧和 Haru 的位置只在本地处理或保存。</p></div>
          <div className={styles.sideBlock}><span className={styles.eyebrow}>练习结束后</span><h3>保留清晰的来源边界</h3><p>你可以查看确认过的回答与复盘入口。快速练习会标记为“快速练习”，不会出现在投递或日历里。</p></div>
        </aside>
      </div>
    </section>
  );
}
