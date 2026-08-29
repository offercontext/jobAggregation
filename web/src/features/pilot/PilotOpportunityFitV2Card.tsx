import {
  normalizeOpportunityFitHistoryDate,
  type OpportunityFitHistoryItem,
} from '@/features/applicationTasks/opportunityFitHistory';

export type PilotOpportunityFitProjectionStatus =
  | 'idle'
  | 'pending'
  | 'result_unknown'
  | 'ready'
  | 'source_conflict'
  | 'unavailable';

export interface PilotOpportunityFitV2CardProps {
  /** A bounded, user-safe status supplied by the canonical task owner. */
  readonly status: PilotOpportunityFitProjectionStatus;
  /** A result excerpt; this is never an internal record or transport value. */
  readonly summary?: string | null;
  /** Already-adapted history; IDs and source kinds remain inside the adapter. */
  readonly history?: readonly Pick<OpportunityFitHistoryItem, 'internalKey' | 'createdAt' | 'summary' | 'sourceState'>[];
  readonly historyState?: 'ready' | 'loading' | 'error' | 'absent';
  /** The only interaction this projection may perform. */
  readonly onOpenTask: () => void;
}

const STATUS_COPY: Readonly<Record<PilotOpportunityFitProjectionStatus, string>> = Object.freeze({
  idle: '开始',
  pending: '等待确认',
  result_unknown: '结果待确认',
  ready: '查看结果',
  source_conflict: '结果待确认',
  unavailable: '暂时不可用',
});

function sourceStateCopy(state: OpportunityFitHistoryItem['sourceState']): string {
  if (state === 'source_changed') return '原资料已更新，本次结果仍使用旧版';
  if (state === 'unavailable') return '历史记录暂时不可用';
  return '当前来源';
}

function displayDate(value: string): string {
  const canonical = normalizeOpportunityFitHistoryDate(value);
  return canonical ? new Date(canonical).toLocaleString() : '时间暂不可用';
}

/**
 * Read-only Pilot projection. All evaluation inputs, mutations, retries and
 * history navigation live in OpportunityFitReviewDrawer.
 */
export default function PilotOpportunityFitV2Card({
  status,
  summary,
  history = [],
  historyState = 'ready',
  onOpenTask,
}: PilotOpportunityFitV2CardProps) {
  const safeStatus = Object.prototype.hasOwnProperty.call(STATUS_COPY, status) ? status : 'unavailable';
  return (
    <section aria-labelledby="pilot-opportunity-fit-v2-title">
      <header>
        <h2 id="pilot-opportunity-fit-v2-title">岗位判断</h2>
        <p>查看带依据的岗位判断；需要开始或继续时，请打开岗位判断工作区。</p>
      </header>

      <section aria-label="当前判断">
        <p role="status">{STATUS_COPY[safeStatus]}</p>
        {summary ? <p>{summary}</p> : null}
        <button type="button" onClick={onOpenTask}>打开岗位判断</button>
      </section>

      <section aria-label="历史记录">
        <h3>历史记录</h3>
        {historyState === 'loading' ? <p role="status">历史记录加载中</p> : null}
        {historyState === 'error' ? <p role="alert">部分历史暂时不可用</p> : null}
        {historyState === 'absent' ? <p role="status">暂时没有可查看的历史记录</p> : null}
        {history.length === 0 && historyState === 'ready' ? <p>暂无历史记录</p> : null}
        {history.map((item) => (
          <article key={item.internalKey}>
            <p>{item.summary}</p>
            <p>{sourceStateCopy(item.sourceState)} · {displayDate(item.createdAt)}</p>
          </article>
        ))}
      </section>
    </section>
  );
}
