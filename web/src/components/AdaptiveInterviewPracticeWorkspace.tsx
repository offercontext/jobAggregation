import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Empty, Input, Modal, Skeleton, Tag, message } from 'antd';
import { CheckCircleOutlined, ClockCircleOutlined, CompassOutlined } from '@ant-design/icons';
import {
  completeAdaptivePractice,
  AdaptivePracticeError,
  listAdaptivePracticePlans,
  startAdaptivePracticeV2,
} from '@/services/adaptiveInterviewPractice';
import { ReadinessFeedbackAdvisory } from '@/features/reviewReadiness/ReadinessFeedbackAdvisory';
import { getReadinessPracticeFocus } from '@/features/reviewReadiness/service';
import type { ReadinessPracticeFocus } from '@/features/reviewReadiness/contracts';
import type {
  AdaptivePracticeAssessment,
  AdaptivePracticeCompleteInput,
  AdaptivePracticeFocus,
  AdaptivePracticeOwnerDraft,
  AdaptivePracticePlan,
  AdaptivePracticeV2StartInput,
} from '@/types/adaptiveInterviewPractice';
import styles from './AdaptiveInterviewPracticeWorkspace.module.css';

const ASSESSMENTS: Array<{ value: AdaptivePracticeAssessment; label: string; detail: string }> = [
  { value: 'needs_work', label: '还需要练', detail: '关键步骤还不够顺畅' },
  { value: 'clearer', label: '更清楚了', detail: '结构已经明显改善' },
  { value: 'confident', label: '可以复用', detail: '能稳定用于下一次回答' },
];

export interface AdaptiveInterviewPracticeWorkspaceProps {
  focus?: AdaptivePracticeFocus;
  ownerGeneration?: number;
  recoveryOwnerGeneration?: number | null;
  drafts?: Readonly<Record<string, AdaptivePracticeOwnerDraft>>;
  onDraftChange?: (
    key: string,
    draft: AdaptivePracticeOwnerDraft | null,
    retireOwnerKey?: string,
  ) => boolean | void;
  onGuardChange?: (guard: { pending: boolean; unsaved: boolean }) => void;
}

function canonicalV2Key(): string {
  if (typeof crypto === 'undefined' || !('randomUUID' in crypto)) throw new Error('canonical_uuid_unavailable');
  return crypto.randomUUID();
}

function completionKeyFor(plan: AdaptivePracticePlan): string {
  const value = canonicalV2Key();
  return plan.origin_contract === 'confirmed_readiness_signal_v1'
    ? value
    : `adaptive-practice-complete-${value}`;
}

function sameExactPair(plan: AdaptivePracticePlan, focus: AdaptivePracticeFocus): boolean {
  return plan.origin_contract === 'confirmed_readiness_signal_v1'
    && plan.readiness_signal_version_id === focus.signalVersionId
    && plan.target_application_event_id === focus.targetEventId;
}

function startOwnerKey(generation: number, focus: AdaptivePracticeFocus): string {
  return `practice:${generation}:${focus.signalVersionId}:${focus.targetEventId}:new`;
}

function planOwnerKey(generation: number, plan: AdaptivePracticePlan): string {
  return plan.origin_contract === 'confirmed_readiness_signal_v1'
    ? `practice:${generation}:${plan.readiness_signal_version_id}:${plan.target_application_event_id}:plan:${plan.id}`
    : `practice:${generation}:legacy:plan:${plan.id}`;
}

function emptyDraft(key: string, generation: number, plan?: AdaptivePracticePlan, focus?: AdaptivePracticeFocus): AdaptivePracticeOwnerDraft {
  return {
    ownerKey: key,
    ownerGeneration: generation,
    signalVersionId: plan?.readiness_signal_version_id ?? focus?.signalVersionId ?? null,
    targetEventId: plan?.target_application_event_id ?? focus?.targetEventId ?? null,
    planId: plan?.id ?? null,
    answer: '',
    reflection: '',
    assessment: null,
    startInput: null,
    completionInput: null,
    resultUnknown: false,
    pendingOperation: null,
  };
}

function recoverableDraftIdentity(draft: AdaptivePracticeOwnerDraft): { signalVersionId: number | null; targetEventId: number | null } {
  return {
    signalVersionId: draft.startInput?.readiness_signal_version_id ?? draft.signalVersionId,
    targetEventId: draft.startInput?.target_application_event_id ?? draft.targetEventId,
  };
}

export function recoverAdaptivePracticeDrafts(
  drafts: Readonly<Record<string, AdaptivePracticeOwnerDraft>>,
  ownerGeneration: number,
  recoveryOwnerGeneration: number | null | undefined,
  focus: AdaptivePracticeFocus | undefined,
): {
  snapshot: Readonly<Record<string, AdaptivePracticeOwnerDraft>>;
  recovered: AdaptivePracticeOwnerDraft[];
  migrations: Array<{ fromOwnerKey: string; draft: AdaptivePracticeOwnerDraft }>;
} {
  if (recoveryOwnerGeneration == null) return { snapshot: drafts, recovered: [], migrations: [] };
  const snapshot: Record<string, AdaptivePracticeOwnerDraft> = { ...drafts };
  const recovered: AdaptivePracticeOwnerDraft[] = [];
  const migrations: Array<{ fromOwnerKey: string; draft: AdaptivePracticeOwnerDraft }> = [];
  for (const draft of Object.values(drafts)) {
    const hasPendingRecovery = draft.resultUnknown || draft.pendingOperation !== null;
    const hasUnsavedPractice = draft.planId !== null && Boolean(draft.answer || draft.reflection || draft.assessment);
    if (
      draft.ownerGeneration !== recoveryOwnerGeneration
      || (!hasPendingRecovery && !hasUnsavedPractice)
    ) continue;
    const identity = recoverableDraftIdentity(draft);
    if (focus) {
      if (identity.signalVersionId !== focus.signalVersionId || identity.targetEventId !== focus.targetEventId) continue;
    } else if (identity.signalVersionId !== null || identity.targetEventId !== null) continue;
    if (draft.pendingOperation === 'start' && !draft.startInput) continue;
    if (draft.pendingOperation === 'complete' && !draft.completionInput) continue;
    const oldKey = draft.planId === null
      ? focus ? `practice:${recoveryOwnerGeneration}:${identity.signalVersionId}:${identity.targetEventId}:new` : null
      : identity.signalVersionId === null && identity.targetEventId === null
        ? `practice:${recoveryOwnerGeneration}:legacy:plan:${draft.planId}`
        : `practice:${recoveryOwnerGeneration}:${identity.signalVersionId}:${identity.targetEventId}:plan:${draft.planId}`;
    if (oldKey === null || draft.ownerKey !== oldKey) continue;
    const newKey = draft.planId === null
      ? `practice:${ownerGeneration}:${identity.signalVersionId}:${identity.targetEventId}:new`
      : identity.signalVersionId === null && identity.targetEventId === null
        ? `practice:${ownerGeneration}:legacy:plan:${draft.planId}`
        : `practice:${ownerGeneration}:${identity.signalVersionId}:${identity.targetEventId}:plan:${draft.planId}`;
    if (snapshot[newKey]) continue;
    const next = { ...draft, ownerKey: newKey, ownerGeneration, ...identity };
    snapshot[newKey] = next;
    recovered.push(next);
    migrations.push({ fromOwnerKey: oldKey, draft: next });
  }
  return recovered.length ? { snapshot, recovered, migrations } : { snapshot: drafts, recovered, migrations };
}

export default function AdaptiveInterviewPracticeWorkspace({ focus, ownerGeneration, recoveryOwnerGeneration, drafts, onDraftChange, onGuardChange }: AdaptiveInterviewPracticeWorkspaceProps) {
  const generation = ownerGeneration ?? focus?.ownerGeneration ?? 0;
  const scopeKey = focus ? `${generation}:${focus.signalVersionId}:${focus.targetEventId}` : `${generation}:legacy`;
  const scopeIdentityRef = useRef(scopeKey);
  scopeIdentityRef.current = scopeKey;
  const requestGeneration = useRef(0);
  const operationInFlight = useRef(false);
  const activeOperation = useRef<{ key: string; draft: AdaptivePracticeOwnerDraft } | null>(null);
  const mounted = useRef(true);
  const [internalDrafts, setInternalDrafts] = useState<Record<string, AdaptivePracticeOwnerDraft>>({});
  const rawDraftSnapshot = drafts ?? internalDrafts;
  const recovery = useMemo(() => recoverAdaptivePracticeDrafts(
    rawDraftSnapshot, generation, recoveryOwnerGeneration, focus,
  ), [focus?.signalVersionId, focus?.targetEventId, generation, rawDraftSnapshot, recoveryOwnerGeneration]);
  const draftSnapshot = recovery.snapshot;
  const draftsRef = useRef<Readonly<Record<string, AdaptivePracticeOwnerDraft>>>(draftSnapshot);
  draftsRef.current = draftSnapshot;
  const [readiness, setReadiness] = useState<ReadinessPracticeFocus | null>(null);
  const [plans, setPlans] = useState<AdaptivePracticePlan[]>([]);
  const [loading, setLoading] = useState(true);
  const [plansError, setPlansError] = useState(false);
  const [focusError, setFocusError] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [activePlanId, setActivePlanId] = useState<number | null>(null);
  const [answer, setAnswer] = useState('');
  const [reflection, setReflection] = useState('');
  const [assessment, setAssessment] = useState<AdaptivePracticeAssessment | null>(null);
  const [resultUnknown, setResultUnknown] = useState(false);
  const [pendingOperation, setPendingOperation] = useState<'start' | 'complete' | null>(null);
  const [busy, setBusy] = useState(false);

  const persistDraft = useCallback((key: string, draft: AdaptivePracticeOwnerDraft | null, retireOwnerKey?: string): boolean => {
    try {
      if (onDraftChange?.(key, draft, retireOwnerKey) === false) return false;
    } catch {
      return false;
    }
    const next = { ...draftsRef.current };
    if (retireOwnerKey) delete next[retireOwnerKey];
    if (draft) next[key] = draft;
    else delete next[key];
    draftsRef.current = next;
    if (!drafts) setInternalDrafts(next);
    return true;
  }, [drafts, onDraftChange]);

  useEffect(() => {
    for (const migration of recovery.migrations) {
      persistDraft(migration.draft.ownerKey, migration.draft, migration.fromOwnerKey);
    }
  }, [persistDraft, recovery]);

  const active = useMemo(() => plans.find((item) => (
    item.id === activePlanId
    && item.status === 'in_progress'
    && (!focus || sameExactPair(item, focus))
  )) ?? null, [activePlanId, focus?.signalVersionId, focus?.targetEventId, plans]);
  const currentPlanKey = active ? planOwnerKey(generation, active) : null;
  const currentDraft = currentPlanKey ? draftSnapshot[currentPlanKey] : undefined;
  const currentStartKey = focus ? startOwnerKey(generation, focus) : null;
  const startDraft = currentStartKey ? draftSnapshot[currentStartKey] : undefined;

  const load = useCallback(async () => {
    const request = requestGeneration.current + 1;
    requestGeneration.current = request;
    setLoading(true);
    setPlansError(false);
    setFocusError(false);
    const [focusResult, plansResult] = await Promise.allSettled([
      focus ? getReadinessPracticeFocus(focus.signalVersionId, focus.targetEventId) : Promise.resolve(null),
      listAdaptivePracticePlans(),
    ]);
    if (requestGeneration.current !== request) return;
    if (focusResult.status === 'fulfilled') setReadiness(focusResult.value);
    else {
      setReadiness(null);
      setFocusError(true);
    }
    if (plansResult.status === 'rejected') {
      setPlansError(true);
      setPlans([]);
      setActivePlanId(null);
    } else {
      const history = plansResult.value;
      setPlans(history);
      setActivePlanId((current) => {
        if (current && history.some((item) => (
          item.id === current
          && item.status === 'in_progress'
          && (!focus || sameExactPair(item, focus))
        ))) return current;
        if (focus) {
          const exact = history.find((item) => item.status === 'in_progress' && sameExactPair(item, focus));
          return exact?.id ?? null;
        }
        return history.find((item) => item.status === 'in_progress')?.id ?? null;
      });
    }
    setLoading(false);
  }, [focus?.signalVersionId, focus?.targetEventId]);

  useEffect(() => {
    mounted.current = true;
    void load();
    return () => {
      const unsettled = activeOperation.current;
      if (unsettled) {
        persistDraft(unsettled.key, { ...unsettled.draft, resultUnknown: true });
        if (activeOperation.current === unsettled) activeOperation.current = null;
        operationInFlight.current = false;
      }
      mounted.current = false;
      requestGeneration.current += 1;
    };
  }, [load, scopeKey]);

  useEffect(() => {
    if (!active || !currentPlanKey) {
      setAnswer('');
      setReflection('');
      setAssessment(null);
      setResultUnknown(Boolean(startDraft?.resultUnknown));
      setPendingOperation(startDraft?.pendingOperation ?? null);
      return;
    }
    const compatible = currentDraft?.ownerGeneration === generation && currentDraft.planId === active.id
      ? currentDraft
      : emptyDraft(currentPlanKey, generation, active);
    setAnswer(compatible.answer);
    setReflection(compatible.reflection);
    setAssessment(compatible.assessment);
    setResultUnknown(compatible.resultUnknown);
    setPendingOperation(compatible.pendingOperation);
  }, [active?.id, currentPlanKey, currentDraft?.ownerKey, currentDraft?.resultUnknown, generation, startDraft?.ownerKey, startDraft?.resultUnknown]);

  useEffect(() => {
    const relevant = Object.values(draftSnapshot).filter((draft) => draft.ownerGeneration === generation);
    onGuardChange?.({
      pending: relevant.some((draft) => draft.resultUnknown || draft.pendingOperation !== null),
      unsaved: relevant.some((draft) => Boolean(draft.answer || draft.reflection || draft.assessment || draft.startInput || draft.completionInput)),
    });
  }, [draftSnapshot, generation, onGuardChange]);

  const updatePlanDraft = useCallback((patch: Partial<AdaptivePracticeOwnerDraft>) => {
    if (!active || !currentPlanKey) return;
    const base = draftsRef.current[currentPlanKey] ?? emptyDraft(currentPlanKey, generation, active);
    persistDraft(currentPlanKey, { ...base, ...patch, ownerKey: currentPlanKey });
  }, [active, currentPlanKey, generation, persistDraft]);

  const canStart = Boolean(
    focus && readiness?.state === 'available' && readiness.practiceState === 'not_started'
    && !plans.some((item) => item.status === 'in_progress' && sameExactPair(item, focus)),
  );

  const confirmStart = async () => {
    if (!focus || !readiness || !canStart || operationInFlight.current || !currentStartKey) return;
    let input: AdaptivePracticeV2StartInput;
    try {
      input = startDraft?.startInput ?? {
        readiness_signal_version_id: focus.signalVersionId,
        target_application_event_id: focus.targetEventId,
        expected_source_fingerprint: readiness.practiceSourceFingerprint,
        expected_target_fingerprint: readiness.practiceTargetFingerprint,
        idempotency_key: canonicalV2Key(),
      };
    } catch {
      message.error('当前环境无法创建规范练习标识，请刷新后重试。');
      return;
    }
    const base = startDraft ?? emptyDraft(currentStartKey, generation, undefined, focus);
    if (!persistDraft(currentStartKey, { ...base, startInput: input, pendingOperation: 'start', resultUnknown: false })) {
      message.error('无法安全保存本次练习操作，请保留页面后重试。');
      return;
    }
    const operation = { key: currentStartKey, draft: { ...base, startInput: input, pendingOperation: 'start' as const, resultUnknown: false } };
    const requestScope = scopeKey;
    activeOperation.current = operation;
    operationInFlight.current = true;
    setBusy(true);
    setPendingOperation('start');
    try {
      const created = await startAdaptivePracticeV2(input);
      if (!mounted.current || scopeIdentityRef.current !== requestScope) return;
      persistDraft(currentStartKey, null);
      setPlans((current) => [created, ...current.filter((item) => item.id !== created.id)]);
      setActivePlanId(sameExactPair(created, focus) ? created.id : null);
      setConfirming(false);
      setResultUnknown(false);
      setPendingOperation(null);
    } catch (cause) {
      if (!mounted.current || scopeIdentityRef.current !== requestScope) return;
      if (cause instanceof AdaptivePracticeError && cause.code) {
        persistDraft(currentStartKey, null);
        setConfirming(false);
        setResultUnknown(false);
        setPendingOperation(null);
        await load();
      } else {
        persistDraft(currentStartKey, { ...base, startInput: input, pendingOperation: 'start', resultUnknown: true });
        setResultUnknown(true);
        setPendingOperation('start');
      }
      message.error(cause instanceof Error ? cause.message : '开始结果待确认，请使用原操作重试');
    } finally {
      if (activeOperation.current === operation) {
        activeOperation.current = null;
        operationInFlight.current = false;
        if (mounted.current && scopeIdentityRef.current === requestScope) setBusy(false);
      }
    }
  };

  const finish = async () => {
    if (!active || !currentPlanKey || operationInFlight.current) return;
    const existing = draftsRef.current[currentPlanKey];
    if (!existing?.completionInput && (!answer.trim() || !assessment)) return;
    let input: AdaptivePracticeCompleteInput;
    try {
      input = existing?.completionInput ?? {
        expected_revision: active.revision,
        response_text: answer,
        reflection_text: reflection,
        self_assessment: assessment!,
        idempotency_key: completionKeyFor(active),
      };
    } catch {
      message.error('当前环境无法创建规范练习标识，请刷新后重试。');
      return;
    }
    const base = existing ?? emptyDraft(currentPlanKey, generation, active);
    const frozen = { ...base, answer: input.response_text, reflection: input.reflection_text, assessment: input.self_assessment, completionInput: input, pendingOperation: 'complete' as const, resultUnknown: false };
    if (!persistDraft(currentPlanKey, frozen)) {
      message.error('无法安全保存本次练习操作，请保留页面后重试。');
      return;
    }
    const operation = { key: currentPlanKey, draft: frozen };
    const requestScope = scopeKey;
    activeOperation.current = operation;
    operationInFlight.current = true;
    setBusy(true);
    setPendingOperation('complete');
    try {
      const completed = await completeAdaptivePractice(active.id, input);
      if (!mounted.current || scopeIdentityRef.current !== requestScope) return;
      persistDraft(currentPlanKey, null);
      setPlans((current) => [completed, ...current.filter((item) => item.id !== completed.id)]);
      setActivePlanId(null);
      setAnswer('');
      setReflection('');
      setAssessment(null);
      setResultUnknown(false);
      setPendingOperation(null);
      message.success('练习已完成并保存');
    } catch (cause) {
      if (!mounted.current || scopeIdentityRef.current !== requestScope) return;
      if (cause instanceof AdaptivePracticeError && cause.code) {
        persistDraft(currentPlanKey, null);
        setResultUnknown(false);
        setPendingOperation(null);
        await load();
      } else {
        persistDraft(currentPlanKey, { ...frozen, resultUnknown: true });
        setResultUnknown(true);
        setPendingOperation('complete');
      }
      message.error(cause instanceof Error ? cause.message : '完成结果待确认，请使用原操作重试');
    } finally {
      if (activeOperation.current === operation) {
        activeOperation.current = null;
        operationInFlight.current = false;
        if (mounted.current && scopeIdentityRef.current === requestScope) setBusy(false);
      }
    }
  };

  const completedPlans = useMemo(() => plans.filter((item) => item.status === 'completed'), [plans]);
  const otherInProgress = useMemo(() => plans.filter((item) => (
    item.status === 'in_progress'
    && item.id !== activePlanId
    && (!focus || sameExactPair(item, focus))
  )), [activePlanId, focus?.signalVersionId, focus?.targetEventId, plans]);
  const exactItems = useMemo(() => readiness ? [readiness] : undefined, [readiness]);

  if (loading) return <div aria-busy="true"><Skeleton active paragraph={{ rows: 8 }} /></div>;
  if (plansError) return <Alert type="error" showIcon role="alert" message="练习记录暂时无法读取。" action={<Button onClick={() => void load()}>重新读取</Button>} />;

  return (
    <div className={styles.workspace} data-practice-owner-scope={scopeKey} data-practice-draft-scope={`${scopeKey}:${activePlanId ?? 'new'}`} aria-busy={busy}>
      <section className={styles.hero}>
        <div className={styles.heroIcon}><CompassOutlined /></div>
        <div><span className={styles.eyebrow}>{focus ? '已确认复盘重点' : '历史练习'}</span><h2>{focus ? '围绕明确的下一场面试练习' : '继续已创建的复盘练习'}</h2><p>{focus ? '练习只绑定当前 Signal Version 和目标 Event，不会猜测其他面试。' : '没有 V2 重点时仍可读取并完成历史 V1 练习。'}</p></div>
      </section>

      {!focus ? <Alert type="info" showIcon message="请选择一个已确认的复盘重点和明确的目标面试后再创建新练习；历史练习仍可继续。" /> : null}
      {focusError ? <Alert type="error" showIcon role="alert" message="无法确认这个重点与目标面试；历史练习仍可继续。" action={<Button onClick={() => void load()}>重新读取</Button>} /> : null}
      {readiness && focus ? <ReadinessFeedbackAdvisory eventId={focus.targetEventId} initialItems={exactItems} selectedVersionIds={[focus.signalVersionId]} onSelectionChange={() => undefined} frozen /> : null}

      {active ? (
        <section className={styles.activeGrid}>
          <div className={styles.practiceCard}>
            <div className={styles.sectionHeading}><div><span className={styles.eyebrow}>练习进行中</span><h3>{active.title}</h3></div><Tag color="processing" icon={<ClockCircleOutlined />}>进行中</Tag></div>
            <div className={styles.promptBox}><span>本次练习</span><strong>{active.prompt}</strong></div>
            <label className={styles.fieldLabel} htmlFor={`adaptive-answer-${active.id}`}>你的回答</label>
            <Input.TextArea id={`adaptive-answer-${active.id}`} aria-label="练习回答" disabled={resultUnknown} value={answer} onChange={(event) => { const value = event.target.value; setAnswer(value); updatePlanDraft({ answer: value }); }} autoSize={{ minRows: 6, maxRows: 12 }} />
            <label className={styles.fieldLabel} htmlFor={`adaptive-reflection-${active.id}`}>练完后的复盘（可选）</label>
            <Input.TextArea id={`adaptive-reflection-${active.id}`} aria-label="练习复盘" disabled={resultUnknown} value={reflection} onChange={(event) => { const value = event.target.value; setReflection(value); updatePlanDraft({ reflection: value }); }} autoSize={{ minRows: 3, maxRows: 6 }} />
            <div className={styles.assessmentGroup}><span className={styles.fieldLabel}>这次练习后的感受</span><div className={styles.assessmentOptions}>{ASSESSMENTS.map((item) => <Button key={item.value} disabled={resultUnknown} className={assessment === item.value ? styles.assessmentActive : styles.assessment} onClick={() => { setAssessment(item.value); updatePlanDraft({ assessment: item.value }); }}><strong>{item.label}</strong><span>{item.detail}</span></Button>)}</div></div>
            {resultUnknown && pendingOperation === 'complete' ? <Alert type="warning" showIcon message="完成结果待确认，输入已冻结。" action={<Button size="large" onClick={() => void finish()}>使用原操作重试</Button>} /> : <Button type="primary" size="large" disabled={!answer.trim() || !assessment} loading={busy} onClick={() => void finish()}>完成本次练习</Button>}
          </div>
          <aside className={styles.evidencePanel}><span className={styles.eyebrow}>为什么现在练</span><p>{active.reason}</p><span className={styles.evidenceLabel}>冻结复盘来源</span><blockquote>{active.source_excerpt}</blockquote><Tag>{active.origin_contract === 'confirmed_readiness_signal_v1' ? '精确目标练习' : '历史 V1 练习'}</Tag></aside>
        </section>
      ) : null}

      {canStart ? <section className={styles.section}><article className={styles.recommendationCard}><div><h3>{readiness?.title}</h3><p>{readiness?.sourceLabel}</p></div>{startDraft?.resultUnknown && startDraft.pendingOperation === 'start' ? <Alert type="warning" showIcon message="开始结果待确认。" action={<Button onClick={() => void confirmStart()}>使用原操作重试</Button>} /> : <Button type="primary" size="large" onClick={() => setConfirming(true)}>确认这组重点与目标</Button>}</article></section> : !active && focus ? <section className={styles.section}><Empty description="这个重点与目标当前不可创建新练习。" image={Empty.PRESENTED_IMAGE_SIMPLE} /></section> : null}

      {otherInProgress.length > 0 ? <section className={styles.section}><div className={styles.sectionHeading}><div><span className={styles.eyebrow}>其他进行中</span><h3>切换练习</h3></div></div><div className={styles.recommendationGrid}>{otherInProgress.map((item) => <article className={styles.recommendationCard} key={item.id}><div><h4>{item.title}</h4><p>{item.company_name} · {item.position_name}</p></div><Button size="large" onClick={() => setActivePlanId(item.id)}>{item.origin_contract === 'legacy_review_focus_v1' ? '继续历史练习' : '继续这组练习'}</Button></article>)}</div></section> : null}

      <section className={styles.section}><div className={styles.sectionHeading}><div><span className={styles.eyebrow}>完成记录</span><h3>历史练习</h3></div></div><div className={styles.historyList}>{completedPlans.map((item) => <details key={item.id} className={styles.historyCard}><summary><span className={styles.historyIcon}><CheckCircleOutlined /></span><span><strong>{item.title}</strong><small>{item.company_name} · {item.position_name}</small></span><Tag color="success">已完成</Tag></summary><div className={styles.historyBody}><div><span>你的回答</span><p>{item.response_text}</p></div>{item.reflection_text ? <div><span>练后复盘</span><p>{item.reflection_text}</p></div> : null}</div></details>)}</div></section>

      <div className={styles.liveStatus} role="status" aria-live="polite" aria-atomic="true">{busy ? '正在处理练习。' : resultUnknown ? '练习操作结果待确认。' : ''}</div>
      <Modal title="开始前确认" open={confirming} confirmLoading={busy} okText="确认开始" cancelText="暂不开始" onOk={() => void confirmStart()} onCancel={() => { if (!busy) setConfirming(false); }}><p>将使用 <strong>{readiness?.title}</strong>，并绑定目标面试 #{focus?.targetEventId}。</p><Alert type="info" showIcon message="只有确认后才会创建练习记录；不会调用 AI。" /></Modal>
    </div>
  );
}
