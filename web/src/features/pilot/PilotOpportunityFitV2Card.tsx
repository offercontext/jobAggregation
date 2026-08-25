import { useMemo, useState } from 'react';
import type {
  CreateOpportunityFitV2Input,
  OpportunityFitV2EvidenceRef,
  OpportunityFitV2Proposal,
  OpportunityFitV2SessionSummary,
  OpportunityFitReview,
  OpportunityFitReviewSummary,
  OpportunityFitV2Draft,
} from '@/types/opportunityFitReview';
import type { ScheduleEvent } from '@/types/event';
import { OPPORTUNITY_FIT_COPY, opportunityFitEvidenceLabel } from '@/components/opportunityFitCopy';

export type PilotOpportunityFitV2Draft = OpportunityFitV2Draft;

interface Props {
  draft: PilotOpportunityFitV2Draft;
  resumes: Array<{ id: number; title?: string; name?: string }>;
  history: OpportunityFitV2SessionSummary[];
  legacyHistory?: OpportunityFitReviewSummary[];
  legacyReview?: OpportunityFitReview | null;
  historyLoading?: boolean;
  legacyHistoryLoading?: boolean;
  triageLoading?: boolean;
  deepLoading?: boolean;
  onChange: (patch: Partial<PilotOpportunityFitV2Draft>) => void;
  onStartTriage: (input: CreateOpportunityFitV2Input) => void;
  onConfirmTriage: () => void;
  onStartDeepReview: () => void;
  onViewHistory: (reviewId: number) => void;
  onViewLegacyHistory?: (reviewId: number) => void;
  onStartNew: () => void;
  restartDisabled?: boolean;
  historyDisabled?: boolean;
  onPrepareMaterials?: (resumeId: number, jdText: string, jdVersionId: number) => void;
  onOpenInterviewReview?: (applicationId: number) => void;
  onOpenInterviewPreparation?: (applicationId: number) => void;
  interviewEvents?: ScheduleEvent[];
  onOpenMockInterview?: (applicationId: number, eventId: number) => void;
  onCancel: () => void;
}

function newKey(): string {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `opportunity-fit-v2-${Date.now()}`;
}

function EvidenceRefs({ refs }: { refs: OpportunityFitV2EvidenceRef[] }) {
  return refs.length > 0 ? (
    <ul>
      {refs.map((ref, index) => (
        <li key={`${ref.source}:${ref.path}:${index}`}>
          <span>{opportunityFitEvidenceLabel(ref.source)}</span>{' · '}
          <code>{ref.path}</code>{' · '}
          <q>{ref.excerpt}</q>
        </li>
      ))}
    </ul>
  ) : <p>暂无可用证据引用</p>;
}

function ProposalSection({
  title,
  items,
}: {
  title: string;
  items: Array<{ id?: string; text: string; rationale: string; evidence_refs: OpportunityFitV2EvidenceRef[] }>;
}) {
  return (
    <section>
      <h4>{title}</h4>
      {items.length === 0 ? <p>暂无可验证内容</p> : null}
      {items.map((item, index) => (
        <article key={item.id ?? `${title}-${index}`}>
          <p>{item.text}</p>
          <p>{item.rationale}</p>
          <EvidenceRefs refs={item.evidence_refs} />
        </article>
      ))}
    </section>
  );
}

function ProposalView({ proposal }: { proposal: OpportunityFitV2Proposal }) {
  return (
    <div>
      <p>{proposal.summary.text}</p>
      <EvidenceRefs refs={proposal.summary.evidence_refs} />
      <ProposalSection title="条件" items={proposal.conditions} />
      <ProposalSection title="风险" items={proposal.risks} />
      <ProposalSection title="下一步" items={proposal.next_steps} />
      <section>
        <h4>待确认问题</h4>
        {proposal.questions.length === 0 ? <p>暂无待确认问题</p> : null}
        {proposal.questions.map((item) => (
          <article key={item.question_id}>
            <p>{item.text}</p>
            <EvidenceRefs refs={item.evidence_refs} />
          </article>
        ))}
      </section>
    </div>
  );
}

export default function PilotOpportunityFitV2Card({
  draft,
  resumes,
  history,
  legacyHistory = [],
  legacyReview = null,
  historyLoading = false,
  legacyHistoryLoading = false,
  triageLoading = false,
  deepLoading = false,
  onChange,
  onStartTriage,
  onConfirmTriage,
  onStartDeepReview,
  onViewHistory,
  onViewLegacyHistory,
  onStartNew,
  restartDisabled = false,
  historyDisabled = false,
  onPrepareMaterials,
  onOpenInterviewReview,
  onOpenInterviewPreparation,
  interviewEvents = [],
  onOpenMockInterview,
  onCancel,
}: Props) {
  const [confirmation, setConfirmation] = useState<'triage' | 'deep' | null>(null);
  const assertions = useMemo(
    () => draft.assertionsText.split(/\r?\n/).map((item) => item.trim()).filter(Boolean),
    [draft.assertionsText],
  );
  const inputIsValid = Boolean(draft.resumeId && draft.jdVersionId)
    && assertions.length <= 10
    && assertions.every((item) => item.length <= 500);
  const triageReady = draft.triage?.stage_status === 'ready';
  const triageConfirmed = draft.triage?.stage_status === 'confirmed';
  const triageSourceConflict = draft.triage?.stage_status === 'source_conflict';
  const deepReady = draft.deep?.stage_status === 'ready';
  const deepSourceConflict = draft.deep?.stage_status === 'source_conflict';
  const triagePending = Boolean(draft.triage && ['generating', 'provider_unknown'].includes(draft.triage.stage_status));
  const deepPending = Boolean(draft.deep && ['generating', 'provider_unknown'].includes(draft.deep.stage_status));
  const historyEntryDisabled = historyDisabled
    || triageLoading
    || deepLoading
    || draft.resultUnknown
    || triagePending
    || deepPending;
  const isHistorical = draft.historical || Boolean(legacyReview);
  const input: CreateOpportunityFitV2Input = {
    schema_version: 2,
    resume_id: draft.resumeId ?? 0,
    jd_version_id: draft.jdVersionId ?? 0,
    jd_source_label: '用户粘贴 JD',
    candidate_assertions: assertions,
    idempotency_key: draft.triageKey ?? newKey(),
  };

  return (
    <section aria-labelledby="pilot-opportunity-fit-v2-title">
      <header>
        <h2 id="pilot-opportunity-fit-v2-title">岗位评估</h2>
      <p>AI 仅提供带证据的条件、风险和待确认问题，不替你做投递或 Offer 决定。</p>
    </header>

      {interviewEvents.length > 0 ? (
        <section aria-label="模拟面试入口">
          <h3>模拟面试</h3>
          <p>请选择一场已安排的面试事件开始练习。</p>
          {interviewEvents.map((event) => (
            <button key={event.id} type="button" onClick={() => onOpenMockInterview?.(draft.applicationId, event.id)}>
              开始：第 {event.round || 1} 轮面试
            </button>
          ))}
        </section>
      ) : null}

      <aside aria-label="历史岗位评估">
        <h3>历史评估（只读）</h3>
        {historyLoading || legacyHistoryLoading ? <p role="status">正在加载历史评估</p> : null}
        <fieldset disabled={historyEntryDisabled} style={{ border: 0, padding: 0, margin: 0 }}>
        {legacyHistory.map((item) => (
          <div key={`legacy-${item.id}`}>
            <span>旧版评估 #{item.id} · 只读</span>
            {onViewLegacyHistory ? <button type="button" disabled={historyEntryDisabled} onClick={() => onViewLegacyHistory(item.id)}>查看</button> : null}
          </div>
        ))}
        {history.map((item) => (
          <div key={item.review_id}>
            <span>评估 #{item.review_id} · {item.stage_count} 个阶段</span>
            <button type="button" disabled={historyEntryDisabled} onClick={() => onViewHistory(item.review_id)}>查看</button>
          </div>
        ))}
        </fieldset>
      </aside>

      {legacyReview ? (
        <section aria-label="旧版岗位评估详情">
          <h3>旧版岗位评估（只读历史）</h3>
          <p>{legacyReview.summary.text}</p>
          <p>旧版结论：{legacyReview.recommendation}</p>
          <p>该记录保留原始快照与哈希，不支持继续生成或写入。</p>
          <button type="button" aria-label="重新开始岗位评估" onClick={onStartNew} disabled={restartDisabled}>开始新的岗位评估</button>
        </section>
      ) : null}

      {draft.error ? <p role="alert">{draft.error}</p> : null}
      {triageSourceConflict || deepSourceConflict ? (
        <p role="status">{OPPORTUNITY_FIT_COPY.drawer.sourceChanged}</p>
      ) : null}
      {isHistorical ? (
        <button type="button" aria-label="重新开始岗位评估" onClick={onStartNew} disabled={restartDisabled}>开始新的岗位评估</button>
      ) : (
        <>
          <label>
            选择简历
            <select
              value={draft.resumeId ?? ''}
              onChange={(event) => onChange({ resumeId: event.target.value ? Number(event.target.value) : undefined })}
              disabled={Boolean(draft.triageKey) || triageLoading || deepLoading}
            >
              <option value="">请选择简历</option>
              {resumes.map((resume) => <option key={resume.id} value={resume.id}>{resume.name ?? resume.title ?? `简历 ${resume.id}`}</option>)}
            </select>
          </label>
          <label>
            粘贴 JD
            <textarea
              value={draft.jdText}
              readOnly
              disabled={Boolean(draft.triageKey) || triageLoading || deepLoading}
              placeholder="只粘贴岗位要求文本，不抓取链接"
            />
          </label>
          <label>
            用户断言（不会作为模型事实）
            <textarea
              value={draft.assertionsText}
              onChange={(event) => onChange({ assertionsText: event.target.value })}
              disabled={Boolean(draft.triageKey) || triageLoading || deepLoading}
              placeholder="每行一条补充断言"
            />
          </label>
          {assertions.length > 10 ? <p role="alert">最多填写 10 条非空断言</p> : null}
          {assertions.some((item) => item.length > 500) ? <p role="alert">每条断言最多 500 字</p> : null}
          <p>仅 JD、选定简历和已确认证据会发送给 AI；用户断言仅保存在本次快照中。</p>
          {!draft.triage && !triageLoading ? (
            <button type="button" disabled={!inputIsValid} onClick={() => setConfirmation('triage')}>{OPPORTUNITY_FIT_COPY.drawer.startTriage}</button>
          ) : null}
        </>
      )}

      {triageLoading ? <p role="status">正在等待 AI 返回评估结果</p> : null}
      {draft.error && draft.triageKey && !draft.triage && !isHistorical ? (
        <button type="button" onClick={() => onStartTriage(input)}>使用原尝试重试快速判断</button>
      ) : null}
      {triagePending ? (
        <>
          <p role="status">结果待确认，请使用原尝试重试快速判断；输入已保留。</p>
          <button type="button" onClick={() => onStartTriage(input)}>使用原尝试重试快速判断</button>
        </>
      ) : null}
      {draft.triage?.proposal ? (
        <section>
          <h3>{OPPORTUNITY_FIT_COPY.drawer.triage}（证据化结果）</h3>
          <ProposalView proposal={draft.triage.proposal} />
          {triageReady ? (
            <button type="button" onClick={onConfirmTriage}>
              {draft.resultUnknown ? '使用原尝试重试快速判断' : '确认快速判断'}
            </button>
          ) : null}
        </section>
      ) : null}
      {triageConfirmed && !draft.deep ? (
        <button type="button" disabled={deepLoading} onClick={() => setConfirmation('deep')}>{OPPORTUNITY_FIT_COPY.drawer.startDeepReview}</button>
      ) : null}
      {deepLoading ? <p role="status">正在进行{OPPORTUNITY_FIT_COPY.drawer.deepReview}</p> : null}
      {draft.error && draft.deepKey && !draft.deep && !isHistorical ? (
        <button type="button" onClick={() => onStartDeepReview()}>使用原尝试重试深入分析</button>
      ) : null}
      {deepPending ? (
        <>
          <p role="status">结果待确认，请使用原尝试重试深入分析；输入已保留。</p>
          <button type="button" onClick={() => onStartDeepReview()}>使用原尝试重试深入分析</button>
        </>
      ) : null}
      {deepReady && draft.deep?.proposal ? (
        <section>
          <h3>{OPPORTUNITY_FIT_COPY.drawer.deepReview}（证据化结果）</h3>
          <ProposalView proposal={draft.deep.proposal} />
          {onPrepareMaterials && draft.resumeId && draft.jdVersionId && !isHistorical ? (
            <button type="button" onClick={() => onPrepareMaterials(draft.resumeId!, draft.jdText, draft.jdVersionId!)}>去准备材料</button>
          ) : null}
        </section>
      ) : null}

      {!isHistorical && (
        draft.triage?.stage_status === 'confirmed'
        || draft.deep?.stage_status === 'ready'
        || triageSourceConflict
        || deepSourceConflict
      ) ? (
        <button type="button" aria-label="重新开始岗位评估" onClick={onStartNew} disabled={restartDisabled}>开始新的岗位评估</button>
      ) : null}

      {onOpenInterviewReview ? (
        <button type="button" onClick={() => onOpenInterviewReview(draft.applicationId)}>打开面试复盘</button>
      ) : null}
      {onOpenInterviewPreparation ? (
        <button type="button" onClick={() => onOpenInterviewPreparation(draft.applicationId)}>打开面试准备</button>
      ) : null}

      <button type="button" onClick={onCancel}>取消流程</button>
      {confirmation ? (
        <div role="dialog" aria-modal="true">
          <h3>{confirmation === 'triage' ? '确认发送评估输入' : '确认开始深入分析'}</h3>
          <p>这一步会调用当前配置的 AI 服务；结果仍需你人工确认。</p>
          <button type="button" onClick={() => setConfirmation(null)}>取消</button>
          <button
            type="button"
            onClick={() => {
              setConfirmation(null);
              if (confirmation === 'triage') onStartTriage(input);
              else onStartDeepReview();
            }}
          >确认</button>
        </div>
      ) : null}
    </section>
  );
}
