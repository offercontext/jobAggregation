import { useEffect, useRef, useState } from 'react';
import { Checkbox, Input, Select } from 'antd';
import type { Resume } from '@/types/resume';
import { getCurrentApplicationJd } from '@/services/applicationJdVersions';
import { createInterviewPracticeCase } from '@/services/interviewPracticeCases';
import type { TaskLaunchRequest } from '@/features/coreTaskSurface/contracts';
import { formatResumeLineage, resumeDisplayTitle } from '@/features/materialSurfaces/materialLabels';
import { resolveResumeLineage } from '@/features/materialSurfaces/resumeLineage';
import {
  createResumeSelectionLease,
  resolveResumeSelection,
  type ResumeSelectionCandidate,
  type ResumeSelectionLease,
  type ResumeSelectionSource,
  type SavedResumeSnapshot,
} from '@/features/interviewEvents/resumeSelectionLease';
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

export interface LockedInterviewEvent {
  readonly applicationId: number;
  readonly eventId: number;
  readonly companyName?: string;
  readonly positionName?: string;
}

export interface ReadinessJdSource {
  readonly status: 'ready' | 'missing' | 'source_changed' | 'unknown' | 'unavailable';
  readonly id?: number;
  readonly text?: string;
}

type ResumeRows = readonly (ResumeSelectionCandidate & Partial<Resume>)[];
type ResumeInput = ResumeRows | ResumeSelectionSource<ResumeRows>;
type TaskLauncher = (request: TaskLaunchRequest) => unknown;

interface Props {
  /** Real preparation accepts one already trusted event only. */
  lockedEvent?: LockedInterviewEvent | null;
  /** Explicit aliases for hosts that keep the locked identity in separate fields. */
  applicationId?: number | null;
  eventId?: number | null;
  resumes?: ResumeInput;
  resumeSelectionLease?: ResumeSelectionLease | null;
  generation?: number;
  selectedResumeId?: number | null;
  savedSnapshot?: SavedResumeSnapshot | null;
  jdSource?: ReadinessJdSource;
  initialMode?: 'real' | 'quick';
  fixedMode?: 'real' | 'quick';
  actionEmphasis?: 'primary' | 'secondary';
  onResumeSelected?: (resumeId: number) => void;
  onLaunchTask?: TaskLauncher;
  onOpenTask?: TaskLauncher;
  /** Kept as an exact canonical adapter for an older host. */
  onOpenPreparation?: (applicationId: number, eventId: number) => void;
  onOpenStudio?: (context: RealInterviewStudioContext | QuickPracticeStudioContext) => void;
}

const STATUS_COPY: Record<ReadinessItem['status'], string> = {
  ready: '已就绪',
  needs_input: '需要补充',
  source_changed: '来源已更新，已有结果仍保留',
  unknown: '暂时未知',
  unavailable: '暂时不可用',
};

function isValidId(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0;
}

function normalizeJdSource(value: ReadinessJdSource | undefined): ReadinessJdSource {
  try {
    if (!value || typeof value !== 'object') return { status: 'unknown' };
    const status = value.status;
    if (status !== 'ready' && status !== 'missing' && status !== 'source_changed' && status !== 'unknown' && status !== 'unavailable') {
      return { status: 'unavailable' };
    }
    if (status === 'ready' && (!isValidId(value.id) || typeof value.text !== 'string' || value.text.trim().length === 0)) {
      return { status: 'unavailable' };
    }
    return {
      status,
      ...(isValidId(value.id) ? { id: value.id } : {}),
      ...(typeof value.text === 'string' ? { text: value.text } : {}),
    };
  } catch {
    return { status: 'unavailable' };
  }
}

function makeKey(prefix: string): string {
  return `${prefix}-${typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : Date.now()}`;
}

function sourceRows(value: ResumeInput | undefined): ResumeRows | undefined {
  try {
    if (Array.isArray(value)) return value;
    if (value && typeof value === 'object') {
      const source = value as ResumeSelectionSource<ResumeRows>;
      return source.status === 'ready' && Array.isArray(source.value) ? source.value : undefined;
    }
    return undefined;
  } catch {
    return undefined;
  }
}

function isVisibleResume(value: unknown): value is ResumeRows[number] {
  try {
    if (!value || typeof value !== 'object') return false;
    const candidate = value as ResumeSelectionCandidate & Partial<Resume> & {
      removed?: boolean;
      removedAt?: string | null;
      removed_at?: string | null;
      status?: string;
    };
    return isValidId(candidate.id)
      && candidate.deleted_at == null
      && candidate.deletedAt == null
      && candidate.deleted !== true
      && candidate.hidden !== true
      && candidate.visible !== false
      && candidate.removed !== true
      && candidate.removedAt == null
      && candidate.removed_at == null
      && candidate.status !== 'removed';
  } catch {
    return false;
  }
}

function visibleResumeRows(value: ResumeInput | undefined): ResumeRows {
  try {
    const rows = sourceRows(value) ?? [];
    const visible: Array<ResumeRows[number]> = [];
    const length = rows.length;
    for (let index = 0; index < length; index += 1) {
      try {
        const resume = rows[index];
        if (isVisibleResume(resume)) {
          visible.push(resume);
        }
      } catch {
        // A malformed row must not make the entire source renderable.
      }
    }
    return visible;
  } catch {
    return [];
  }
}

function hasUniqueVisibleResume(rows: ResumeRows, resumeId: unknown): resumeId is number {
  if (!isValidId(resumeId)) return false;
  try {
    let matches = 0;
    for (let index = 0; index < rows.length; index += 1) {
      try {
        const resume = rows[index];
        if (isVisibleResume(resume) && resume.id === resumeId) matches += 1;
      } catch {
        return false;
      }
    }
    return matches === 1;
  } catch {
    return false;
  }
}

function resumeLabel(resume: ResumeSelectionCandidate, rows: ResumeRows): string {
  try {
    const candidate = resume as ResumeSelectionCandidate & Partial<Resume>;
    const lineage = resolveResumeLineage(rows as readonly Resume[], resume.id);
    return `${resumeDisplayTitle(candidate as Resume)} · ${formatResumeLineage(lineage)}`;
  } catch {
    return '未命名简历 · 关系待确认';
  }
}

function Checklist({ items, onAction }: { items: readonly ReadinessItem[]; onAction?: (item: ReadinessItem) => void }) {
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

function LockedRealSurface({
  event,
  resumes,
  resumeSelectionLease,
  generation,
  selectedResumeId,
  savedSnapshot,
  jdSource,
  onResumeSelected,
  onLaunchTask,
  onOpenTask,
  onOpenPreparation,
}: {
  event: LockedInterviewEvent | null;
  resumes?: ResumeInput;
  resumeSelectionLease?: ResumeSelectionLease | null;
  generation?: number;
  selectedResumeId?: number | null;
  savedSnapshot?: SavedResumeSnapshot | null;
  jdSource?: ReadinessJdSource;
  onResumeSelected?: (resumeId: number) => void;
  onLaunchTask?: TaskLauncher;
  onOpenTask?: TaskLauncher;
  onOpenPreparation?: (applicationId: number, eventId: number) => void;
}) {
  const internalLeaseRef = useRef<ResumeSelectionLease>();
  if (!internalLeaseRef.current || internalLeaseRef.current.generation !== (generation ?? 0)) {
    internalLeaseRef.current = createResumeSelectionLease(generation ?? 0);
  }
  const lease = resumeSelectionLease ?? internalLeaseRef.current;
  const validContext = Boolean(event && isValidId(event.applicationId) && isValidId(event.eventId));
  const resumeSource = resumes;
  const selection = validContext && event
    ? resolveResumeSelection({
      applicationId: event.applicationId,
      eventId: event.eventId,
      generation,
      resumes: resumeSource,
      lease,
      selectedResumeId,
      savedSnapshot,
    })
    : null;
  const resolvedResume = selection?.kind === 'selected' ? selection.selection : null;
  const [localResumeId, setLocalResumeId] = useState<number | undefined>(
    selectedResumeId ?? resolvedResume?.id ?? undefined,
  );
  const selectedResume = resolvedResume?.id === localResumeId ? resolvedResume : null;
  const jdContextKey = `${validContext && event ? `${event.applicationId}:${event.eventId}` : 'invalid'}:${jdSource ? 'provided' : 'application'}`;
  const [resolvedJdState, setResolvedJdState] = useState<{ key: string; value: ReadinessJdSource }>(() => ({
    key: jdContextKey,
    value: normalizeJdSource(jdSource),
  }));

  useEffect(() => {
    setLocalResumeId(resolvedResume?.id);
  }, [event?.applicationId, event?.eventId, resolvedResume?.id]);

  useEffect(() => {
    if (!event || !validContext || selection?.kind !== 'needs_selection' || !selection.shouldAsk) return;
    lease.markAsked(event.applicationId, event.eventId);
  }, [event, lease, selection?.kind, selection?.shouldAsk, validContext]);

  useEffect(() => {
    if (jdSource) {
      return;
    }
    if (!event || !validContext) {
      setResolvedJdState({ key: jdContextKey, value: { status: 'unavailable' } });
      return;
    }
    let active = true;
    setResolvedJdState({ key: jdContextKey, value: { status: 'unknown' } });
    void getCurrentApplicationJd(event.applicationId).then((result) => {
      if (!active) return;
      setResolvedJdState({
        key: jdContextKey,
        value: normalizeJdSource(result.current
          ? { status: 'ready', id: result.current.id, text: result.current.jd_text }
          : { status: 'missing' }),
      });
    }).catch(() => {
      if (active) setResolvedJdState({ key: jdContextKey, value: { status: 'unavailable' } });
    });
    return () => { active = false; };
  }, [event?.applicationId, event?.eventId, jdContextKey, jdSource, validContext]);

  if (!event || !validContext) {
    return (
      <div className={styles.prepPanel} data-testid="locked-real-preparation-unavailable">
        <div className={styles.panelHeader}><div><span className={styles.eyebrow}>准备检查</span><h2>选择一场具体面试</h2></div></div>
        <p className={styles.privacyNote}>请从面试事件卡进入准备，当前入口没有可验证的面试身份。</p>
      </div>
    );
  }

  const jd = jdSource
    ? normalizeJdSource(jdSource)
    : resolvedJdState.key === jdContextKey
      ? resolvedJdState.value
      : { status: 'unknown' as const };
  const readiness = buildRealInterviewReadiness({
    application: { id: event.applicationId },
    jd: { status: jd.status },
    resume: selectedResume ? { id: selectedResume.id } : null,
    event: { id: event.eventId },
  });
  const sourceUnavailable = selection?.kind === 'unavailable';
  const displayedItems = sourceUnavailable
    ? readiness.items.map((item) => item.key === 'resume'
      ? { ...item, status: 'unavailable' as const, detail: '简历来源暂时无法确认。' }
      : item)
    : readiness.items;
  const ready = readiness.ready && !sourceUnavailable && jd.status === 'ready' && isValidId(jd.id) && typeof jd.text === 'string' && jd.text.length > 0;
  const launch = onLaunchTask ?? onOpenTask;
  const visibleResumes = visibleResumeRows(resumeSource);
  const selectResume = (value: number | undefined) => {
    if (!isValidId(value)) {
      lease.clearSelection(event.applicationId, event.eventId);
      setLocalResumeId(undefined);
      return;
    }
    if (!lease.select({ applicationId: event.applicationId, eventId: event.eventId, resumeId: value })) return;
    setLocalResumeId(value);
    onResumeSelected?.(value);
  };

  return (
    <div className={styles.prepPanel} data-testid="locked-real-preparation">
      <div className={styles.panelHeader}>
        <div><span className={styles.eyebrow}>面试准备</span><h2>{event.companyName || '本次面试'}{event.positionName ? ` · ${event.positionName}` : ''}</h2></div>
        <span className={styles.roundBadge}>事件已锁定</span>
      </div>
      <div className={styles.readOnlyJd} aria-live="polite">
        <span>当前 JD（只读）</span>
        <p>{jd.status === 'ready' ? jd.text : jd.status === 'unknown' ? '正在读取当前已确认版本…' : jd.status === 'source_changed' ? '岗位资料版本已变化，请先确认最新版本。' : jd.status === 'unavailable' ? '岗位资料暂时无法读取。' : '尚未找到当前已确认的岗位资料版本。'}</p>
      </div>
      <div className={styles.controls}>
        <label>选择简历
          <Select
            id="locked-readiness-resume"
            value={localResumeId}
            placeholder="请选择已保存简历"
            allowClear
            disabled={sourceUnavailable}
            onChange={(value) => selectResume(value)}
            options={visibleResumes.map((resume) => ({ value: resume.id, label: resumeLabel(resume, visibleResumes) }))}
          />
        </label>
      </div>
      <Checklist items={displayedItems} />
      {selection?.reason === 'source_loading' ? <div className={styles.error} role="status">简历列表正在加载，确认后才能开始。</div> : null}
      {selection?.reason === 'source_error' || selection?.reason === 'source_unknown' ? <div className={styles.error} role="alert">简历状态暂时无法确认，请重试后再开始。</div> : null}
      {selection?.reason === 'source_absent' ? <div className={styles.error} role="status">简历列表尚未加载。</div> : null}
      <button
        type="button"
        className={styles.secondaryAction}
        disabled={!ready}
        onClick={() => {
          if (!ready || !selectedResume || !isValidId(jd.id) || typeof jd.text !== 'string') return;
          const request: TaskLaunchRequest = {
            ref: { taskId: 'application.interview_prepare', applicationId: event.applicationId, eventId: event.eventId },
            source: 'interview_event_card',
            focus: 'current',
            hints: { suggestedResumeId: selectedResume.id },
          };
          if (launch) launch(request);
          else onOpenPreparation?.(event.applicationId, event.eventId);
        }}
      >开始准备<span aria-hidden="true">↗</span></button>
    </div>
  );
}

export default function InterviewReadinessCenter({
  lockedEvent,
  applicationId,
  eventId,
  resumes,
  resumeSelectionLease,
  generation,
  selectedResumeId,
  savedSnapshot,
  jdSource,
  initialMode = 'real',
  fixedMode,
  actionEmphasis = 'primary',
  onResumeSelected,
  onLaunchTask,
  onOpenTask,
  onOpenPreparation,
  onOpenStudio,
}: Props) {
  const [mode, setMode] = useState<'real' | 'quick'>(fixedMode ?? initialMode);
  const [quickDraft, setQuickDraft] = useState<QuickPracticeDraft>({ positionName: '', jdText: '', jdConfirmed: false, resumeId: undefined });
  const [quickError, setQuickError] = useState<string | null>(null);
  const [creatingCase, setCreatingCase] = useState(false);
  const quickCaseKeyRef = useRef<string | null>(null);
  const quickCaseFingerprintRef = useRef<string | null>(null);
  const latestQuickDraftRef = useRef(quickDraft);
  latestQuickDraftRef.current = quickDraft;

  const quickReadiness = buildQuickPracticeReadiness(quickDraft);
  const effectiveLockedEvent: LockedInterviewEvent | null = lockedEvent ?? (
    isValidId(applicationId) && isValidId(eventId) ? { applicationId, eventId } : null
  );
  const quickResumeRows = visibleResumeRows(resumes);
  const quickResumeSelectionValid = hasUniqueVisibleResume(quickResumeRows, quickDraft.resumeId);

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
    if (!quickReadiness.ready || !hasUniqueVisibleResume(quickResumeRows, resumeId)) {
      setQuickError('当前简历已不可用，请重新选择后再开始快速练习。');
      return;
    }
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
      setQuickError(code === 'interview_practice_case_idempotency_conflict' ? '快速练习档案内容已变化，请重新确认后再试。' : '快速练习档案结果待确认，输入已冻结；请使用原尝试恢复。');
    } finally {
      setCreatingCase(false);
    }
  };

  const realMode = mode === 'real';
  return (
    <section className={styles.surface} data-testid="interview-readiness-center" data-readiness-mode={mode} aria-labelledby="readiness-title">
      <div className={styles.hero}>
        <div>
          <span className={styles.kicker}>面试准备</span>
          <h1 id="readiness-title">面试准备中心</h1>
          <p>{realMode ? '围绕已锁定的面试事件准备，不需要重新挑选上下文。' : '先把要带进练习的证据准备好，再进入一间只属于这次练习的工作台。'}</p>
        </div>
        <div className={styles.heroNote}><span className={styles.liveDot} /> 准备信息已确认</div>
      </div>

      {!fixedMode ? <div className={styles.modeGrid} role="tablist" aria-label="练习模式">
        <button type="button" role="tab" aria-selected={mode === 'real'} className={styles.modeCard} data-active={mode === 'real'} onClick={() => setMode('real')}>
          <span className={styles.modeNumber}>01</span>
          <span className={styles.modeTitle}>围绕真实投递练习</span>
          <span className={styles.modeDescription}>只接受从具体面试事件进入的准备上下文。</span>
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
        {realMode ? (
          <LockedRealSurface
            event={effectiveLockedEvent}
            resumes={resumes}
            resumeSelectionLease={resumeSelectionLease}
            generation={generation}
            selectedResumeId={selectedResumeId}
            savedSnapshot={savedSnapshot}
            jdSource={jdSource}
            onResumeSelected={onResumeSelected}
            onLaunchTask={onLaunchTask}
            onOpenTask={onOpenTask}
            onOpenPreparation={onOpenPreparation}
          />
        ) : (
          <div className={styles.prepPanel} data-testid="quick-practice-panel">
            <div className={styles.panelHeader}>
              <div><span className={styles.eyebrow}>准备检查</span><h2>开始前检查</h2></div>
              <span className={styles.roundBadge}>{quickReadiness.ready && quickResumeSelectionValid ? '可以出发' : '还差一点'}</span>
            </div>
            <div className={styles.controls}>
              <label>岗位名称<Input value={quickDraft.positionName} maxLength={200} placeholder="例如：后端工程师" onChange={(event) => setQuickDraft((current) => ({ ...current, positionName: event.target.value }))} /></label>
              <label>粘贴 JD<TextArea id="quick-readiness-jd" value={quickDraft.jdText} rows={5} placeholder="粘贴你已核对的岗位描述原文，不抓取 URL。" onChange={(event) => setQuickDraft((current) => ({ ...current, jdText: event.target.value }))} /></label>
              <label className={styles.confirmLabel}><Checkbox checked={quickDraft.jdConfirmed} onChange={(event) => setQuickDraft((current) => ({ ...current, jdConfirmed: event.target.checked }))}>已核对，本次按此岗位资料练习</Checkbox></label>
              <label>选择简历<Select id="quick-readiness-resume" value={quickDraft.resumeId} placeholder="请选择已保存简历" allowClear onChange={(value) => setQuickDraft((current) => ({ ...current, resumeId: value }))} options={quickResumeRows.map((resume) => ({ value: resume.id, label: resumeLabel(resume, quickResumeRows) }))} /></label>
            </div>
            <Checklist items={quickReadiness.items} onAction={(item) => item.key === 'resume' ? focusControl('quick-readiness-resume') : item.key === 'jd' ? focusControl('quick-readiness-jd') : undefined} />
            {quickError ? <div className={styles.error} role="alert">{quickError}</div> : null}
            <button type="button" className={actionEmphasis === 'primary' ? styles.primaryAction : styles.secondaryAction} disabled={!quickReadiness.ready || !quickResumeSelectionValid || creatingCase} onClick={() => void startQuickPractice()}>
              {creatingCase ? '正在冻结练习资料…' : '进入快速练习'}<span aria-hidden="true">↗</span>
            </button>
            <p className={styles.privacyNote}>进入后仍会保留人工确认、原尝试恢复和来源状态说明。未确认的语音转写只在浏览器处理。</p>
          </div>
        )}

        <aside className={styles.sidePanel} aria-label="面试说明">
          <div className={styles.sideBlock}><span className={styles.eyebrow}>本次输入</span><h3>{realMode ? '只带入已确认来源' : '只发送已确认资料'}</h3><p>{realMode ? '当前 JD、明确选择的简历和已锁定的面试事件属于本次准备。' : '快速练习只发送冻结 JD、明确选择的简历和本次确认的回答。'}</p></div>
          <div className={styles.sideBlock}><span className={styles.eyebrow}>仅在本地</span><h3>什么不会离开浏览器</h3><p>原始音频、临时转写、语音活动片段和 Haru 的位置只在本地处理或保存。</p></div>
          <div className={styles.sideBlock}><span className={styles.eyebrow}>练习结束后</span><h3>保留清晰的来源边界</h3><p>你可以查看确认过的回答与复盘入口。快速练习不会出现在投递或日历里。</p></div>
        </aside>
      </div>
    </section>
  );
}
