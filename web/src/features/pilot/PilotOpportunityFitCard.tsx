import { useMemo, useState } from 'react';
import type {
  OpportunityFitEvidenceRef,
  OpportunityFitReview,
  OpportunityFitReviewSummary,
} from '@/types/opportunityFitReview';
import {
  isValidOpportunityFitReview,
  normalizeOpportunityFitAssertions,
  type OpportunityFitDraftAction,
  type OpportunityFitDraftErrorDisposition,
  type OpportunityFitResumeEvidenceProof,
  type OpportunityFitDraftState,
} from './opportunityFitDraft';
import {
  OPPORTUNITY_FIT_COPY,
  opportunityFitEvidenceLabel,
  opportunityFitGapKindLabel,
  opportunityFitRecommendedPathLabel,
  opportunityFitStatusLabel,
} from '@/components/opportunityFitCopy';

export interface PilotOpportunityFitResumeOption {
  id: number;
  title?: string;
  name?: string;
}

export interface PilotOpportunityFitMaterialHandoff {
  applicationId: number;
  resumeId: number;
  jdText: string;
  jdVersionId?: number;
}

interface Props {
  draft: OpportunityFitDraftState;
  dispatch: (action: OpportunityFitDraftAction) => void;
  resumes: PilotOpportunityFitResumeOption[];
  resumeEvidenceProof?: OpportunityFitResumeEvidenceProof | null;
  onStartTriage: (draft: OpportunityFitDraftState, triageAttemptKey: string | null) => void;
  onRetryTriage: (draft: OpportunityFitDraftState, triageAttemptKey: string | null) => void;
  onStartDeepReview: (draft: OpportunityFitDraftState, review: OpportunityFitReview) => void;
  onPrepareMaterials: (handoff: PilotOpportunityFitMaterialHandoff) => void;
  onOpenInterviewReview?: (applicationId: number) => void;
  onOpenInterviewPreparation?: (applicationId: number) => void;
  onCancel: () => void;
  triageFailureDisposition?: OpportunityFitDraftErrorDisposition;
  historicalReview?: boolean;
  historicalReviews?: OpportunityFitReviewSummary[];
  onViewHistoricalReview?: (reviewId: number) => void;
  onStartNewReview?: () => void;
  isHistoryLoading?: boolean;
  isTriageLoading?: boolean;
  isDeepReviewLoading?: boolean;
}

type Confirmation = 'triage' | 'deep_review' | 'prepare_materials' | null;

function isRenderableReview(
  value: OpportunityFitReview | null,
  applicationId: number,
  resumeEvidenceProof: OpportunityFitResumeEvidenceProof | null | undefined,
  historicalReview: boolean,
): value is OpportunityFitReview {
  return value !== null
    && value.application_id === applicationId
    && isValidOpportunityFitReview(value, {
      resumeEvidenceProof: resumeEvidenceProof ?? undefined,
      requireResumeEvidenceProof: !historicalReview,
    });
}

function EvidenceRefs({ refs }: { refs: OpportunityFitEvidenceRef[] }) {
  if (refs.length === 0) return <p className="pilot-opportunity-fit__muted">{OPPORTUNITY_FIT_COPY.drawer.noDirectEvidence}</p>;
  return (
    <ul aria-label={OPPORTUNITY_FIT_COPY.drawer.evidenceSources}>
      {refs.map((ref) => (
        <li key={`${ref.source}:${ref.path}:${ref.excerpt}`}>
          <span>{opportunityFitEvidenceLabel(ref.source)}</span>{' · '}
          <code>{ref.path}</code>{' · '}
          <q>{ref.excerpt}</q>
        </li>
      ))}
    </ul>
  );
}

function ReviewItem({ title, statement, refs }: { title?: string; statement: string; refs: OpportunityFitEvidenceRef[] }) {
  return (
    <article>
      {title ? <h4>{title}</h4> : null}
      <p>{statement}</p>
      <EvidenceRefs refs={refs} />
    </article>
  );
}

function ConfirmationDialog({
  confirmation,
  onCancel,
  onConfirm,
}: {
  confirmation: Exclude<Confirmation, null>;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const title = confirmation === 'triage'
    ? '确认发送评估内容'
    : confirmation === 'deep_review'
      ? '确认开始深入分析'
      : '确认仍要准备材料';
  const description = confirmation === 'triage'
    ? '确认将这些内容发送给当前配置的 AI 服务。'
    : confirmation === 'deep_review'
      ? `这将使用${OPPORTUNITY_FIT_COPY.drawer.sourceFrozen}的评估来源进行${OPPORTUNITY_FIT_COPY.drawer.deepReview}。`
      : '当前建议不是准备材料，仍要把冻结简历和 JD 交给材料包吗？';
  const confirmLabel = confirmation === 'triage'
    ? '确认发送'
    : confirmation === 'deep_review'
      ? '确认深入分析'
      : '确认仍要准备材料';

  return (
    <div role="dialog" aria-modal="true" aria-labelledby="pilot-opportunity-fit-confirm-title">
      <h2 id="pilot-opportunity-fit-confirm-title">{title}</h2>
      <p>{description}</p>
      <button type="button" onClick={onCancel}>取消</button>
      <button type="button" onClick={onConfirm}>{confirmLabel}</button>
    </div>
  );
}

export default function PilotOpportunityFitCard({
  draft,
  dispatch,
  resumes,
  resumeEvidenceProof,
  onStartTriage,
  onRetryTriage,
  onStartDeepReview,
  onPrepareMaterials,
  onOpenInterviewReview,
  onOpenInterviewPreparation,
  onCancel,
  triageFailureDisposition,
  historicalReview = false,
  historicalReviews = [],
  onViewHistoricalReview,
  onStartNewReview,
  isHistoryLoading = false,
  isTriageLoading = false,
  isDeepReviewLoading = false,
}: Props) {
  const [confirmation, setConfirmation] = useState<Confirmation>(null);
  const assertions = useMemo(() => {
    try {
      return { values: normalizeOpportunityFitAssertions(draft.assertionsText), error: null };
    } catch (error) {
      if (error instanceof Error && error.name === 'OpportunityFitAssertionsNormalizationError') {
        return { values: [], error: error.message.includes('10') ? OPPORTUNITY_FIT_COPY.drawer.assertionsTooMany : OPPORTUNITY_FIT_COPY.drawer.assertionsTooLong };
      }
      return { values: [], error: OPPORTUNITY_FIT_COPY.drawer.assertionsTooLong };
    }
  }, [draft.assertionsText]);
  const normalizedDraft = assertions.error
    ? null
    : {
      ...draft,
      jdText: draft.jdText.trim(),
      assertionsText: assertions.values.join('\n'),
    };
  const review = isRenderableReview(draft.review, draft.applicationId, resumeEvidenceProof, historicalReview)
    ? draft.review
    : null;
  const isTriagePhase = draft.phase === 'collect_input' || draft.phase === 'confirm_triage' || draft.phase === 'triage_loading';
  const canStartTriage = Boolean(draft.resumeID && draft.jdText.trim() && !assertions.error && !isHistoryLoading && !isTriageLoading && draft.phase !== 'triage_loading');
  const failureDisposition = draft.triageFailureDisposition ?? triageFailureDisposition ?? null;
  const isUnknownFailure = failureDisposition === 'unknown' && Boolean(draft.actionError);
  const isDeepReady = Boolean(review?.deep_review) && draft.phase === 'deep_review_ready';

  const resetForNewReview = () => {
    onStartNewReview?.();
    dispatch({ type: 'reset_for_new_review' });
  };

  const submitTriage = () => {
    if (!normalizedDraft) return;
    setConfirmation(null);
    onStartTriage(normalizedDraft, normalizedDraft.triageAttemptKey);
  };

  const submitDeepReview = () => {
    if (!review) return;
    setConfirmation(null);
    onStartDeepReview(draft, review);
  };

  const submitMaterialHandoff = () => {
    if (!review || !review.deep_review || !review.source.jd.text || typeof review.source.resume.id !== 'number') return;
    setConfirmation(null);
    onPrepareMaterials({
      applicationId: draft.applicationId,
      resumeId: review.source.resume.id,
      jdText: review.source.jd.text,
    });
  };

  return (
    <section aria-labelledby="pilot-opportunity-fit-title">
      <header>
        <h2 id="pilot-opportunity-fit-title">岗位评估</h2>
        <p>通过“收集→确认→审阅→交接”逐步完成当前岗位分析。</p>
        {onOpenInterviewReview ? (
          <button type="button" onClick={() => onOpenInterviewReview(draft.applicationId)}>
            打开面试复盘
          </button>
        ) : null}
        {onOpenInterviewPreparation ? (
          <button type="button" onClick={() => onOpenInterviewPreparation(draft.applicationId)}>
            打开面试准备建议
          </button>
        ) : null}
      </header>

      {historicalReviews.length > 0 ? (
        <aside aria-label="历史岗位评估">
          <h3>历史岗位评估</h3>
          <ul>
            {historicalReviews.map((historical) => (
              <li key={historical.id}>
                <p>{historical.summary.text}</p>
                <EvidenceRefs refs={historical.summary.evidence_refs} />
                <button type="button" onClick={() => onViewHistoricalReview?.(historical.id)}>
                  查看历史评估
                </button>
              </li>
            ))}
          </ul>
        </aside>
      ) : null}

      {draft.actionError && isUnknownFailure ? <div role="alert">结果未知：请使用原尝试重试。</div> : null}
      {draft.actionError && !isUnknownFailure ? <div role="alert">{OPPORTUNITY_FIT_COPY.errors.fallback}</div> : null}
      {isHistoryLoading ? <p role="status">正在恢复历史岗位评估…</p> : null}

      {isTriagePhase ? (
        <form onSubmit={(event) => { event.preventDefault(); if (canStartTriage) setConfirmation('triage'); }}>
          <label>
            选择简历
            <select
              aria-label="选择简历"
              value={draft.resumeID ?? ''}
              onChange={(event) => dispatch({ type: 'set_resume', resumeID: event.target.value ? Number(event.target.value) : undefined })}
            >
              <option value="">请选择简历</option>
              {resumes.map((resume) => <option key={resume.id} value={resume.id}>{resume.name || resume.title || `简历 ${resume.id}`}</option>)}
            </select>
          </label>
          <label>
            粘贴 JD
            <textarea aria-label="粘贴 JD" value={draft.jdText} onChange={(event) => dispatch({ type: 'set_jd', jdText: event.target.value })} placeholder="只粘贴岗位要求文本；不会抓取链接。" />
          </label>
          <label>
            补充断言
            <textarea aria-label="补充断言" value={draft.assertionsText} onChange={(event) => dispatch({ type: 'set_assertions', assertionsText: event.target.value })} placeholder="每行一条本次补充的候选人事实" />
          </label>
          <p>最多 10 条，每条最多 500 字。</p>
          {assertions.error ? <p role="alert">{assertions.error}</p> : null}
          <p>这些内容将发送给当前配置的 AI 服务。</p>
          <button type="submit" disabled={!canStartTriage}>{isTriageLoading ? '正在分析…' : OPPORTUNITY_FIT_COPY.drawer.startTriage}</button>
        </form>
      ) : null}

      {draft.phase === 'triage_loading' && !draft.actionError ? <p role="status">正在等待 AI 返回可验证的评估结果…</p> : null}
      {draft.actionError && (draft.phase === 'triage_loading' || draft.phase === 'confirm_triage') ? (
        <button type="button" onClick={() => normalizedDraft && onRetryTriage(normalizedDraft, normalizedDraft.triageAttemptKey)}>
          {isUnknownFailure ? '使用原尝试重试' : '重新尝试'}
        </button>
      ) : null}

      {review && draft.phase !== 'collect_input' && draft.phase !== 'confirm_triage' && draft.phase !== 'triage_loading' ? (
        <div>
          {historicalReview ? <button type="button" onClick={resetForNewReview}>开始新的岗位评估</button> : null}
          <p>{OPPORTUNITY_FIT_COPY.drawer.sourceFrozen} · 人工确认</p>
          <h3>{OPPORTUNITY_FIT_COPY.drawer.triage}</h3>
          <ReviewItem statement={review.triage.summary.text} refs={review.triage.summary.evidence_refs} />

          <h4>岗位约束</h4>
          {review.triage.hard_constraints.map((item) => <ReviewItem key={item.id} title={`${item.requirement} · ${opportunityFitStatusLabel(item.status)}`} statement={item.explanation} refs={item.evidence_refs} />)}
          <h4>候选人匹配信号</h4>
          {review.triage.fit_signals.map((item) => <ReviewItem key={item.id} statement={item.statement} refs={item.evidence_refs} />)}
          <h4>差距与待确认问题</h4>
          {review.triage.gaps.map((item) => <ReviewItem key={item.id} title={`${opportunityFitGapKindLabel(item.kind)} · ${opportunityFitStatusLabel(item.candidate_status)}`} statement={item.requirement} refs={item.evidence_refs} />)}
          {review.triage.next_questions.map((question) => <p key={question}>待确认：{question}</p>)}
          <h4>截止日期</h4>
          <p>{review.triage.deadline.status === 'stated' ? review.triage.deadline.text : OPPORTUNITY_FIT_COPY.drawer.notStated}</p>
          <EvidenceRefs refs={review.triage.deadline.evidence_refs} />

          <h4>{OPPORTUNITY_FIT_COPY.drawer.evidenceSources}</h4>
          <p>{OPPORTUNITY_FIT_COPY.evidence.resume}：{review.source.resume.title}</p>
          <p>{OPPORTUNITY_FIT_COPY.evidence.jd}：{review.source.jd.source_label}</p>
          <p>{review.source.jd.text}</p>
          {review.source.candidate_assertions.length > 0 ? (
            <div>
              <h4>用户断言</h4>
              {review.source.candidate_assertions.map((assertion) => <p key={assertion.index}>{assertion.text}</p>)}
            </div>
          ) : null}

          {!historicalReview && !isDeepReady ? (
            isDeepReviewLoading ? <p role="status">正在进行{OPPORTUNITY_FIT_COPY.drawer.deepReview}…</p> : null
          ) : null}
          {!historicalReview && !isDeepReady ? (
            <button type="button" disabled={isDeepReviewLoading} onClick={() => setConfirmation('deep_review')}>
              {isDeepReviewLoading ? '正在深入分析…' : OPPORTUNITY_FIT_COPY.drawer.startDeepReview}
            </button>
          ) : isDeepReady ? (
            <>
              <h3>{OPPORTUNITY_FIT_COPY.drawer.deepReview}</h3>
              <p>{OPPORTUNITY_FIT_COPY.drawer.recommendedPath}：{opportunityFitRecommendedPathLabel(review.deep_review!.recommended_path)}</p>
              <h4>优势</h4>
              {review.deep_review!.strengths.map((item) => <ReviewItem key={item.id} statement={item.statement} refs={item.evidence_refs} />)}
              <h4>待补足项</h4>
              {review.deep_review!.gaps_to_address.map((item) => <ReviewItem key={item.id} statement={item.statement} refs={item.evidence_refs} />)}
              <h4>待澄清问题</h4>
              {review.deep_review!.questions_to_clarify.map((item) => <ReviewItem key={item.id} statement={item.statement} refs={item.evidence_refs} />)}
              {review.deep_review!.recommended_path === 'prepare_materials' ? (
                <button type="button" disabled={isDeepReviewLoading} onClick={submitMaterialHandoff}>{OPPORTUNITY_FIT_COPY.drawer.prepareMaterials}</button>
              ) : (
                <button type="button" disabled={isDeepReviewLoading} onClick={() => setConfirmation('prepare_materials')}>仍要准备材料</button>
              )}
            </>
          ) : null}
        </div>
      ) : null}

      {!review && draft.phase !== 'collect_input' && draft.phase !== 'confirm_triage' && draft.phase !== 'triage_loading' ? <p>暂无可展示的评估结果</p> : null}
      <button type="button" onClick={onCancel}>取消流程</button>
      {confirmation ? <ConfirmationDialog confirmation={confirmation} onCancel={() => setConfirmation(null)} onConfirm={confirmation === 'triage' ? submitTriage : confirmation === 'deep_review' ? submitDeepReview : submitMaterialHandoff} /> : null}
    </section>
  );
}
