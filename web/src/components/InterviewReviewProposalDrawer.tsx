import { useEffect, useMemo, useRef, useState } from 'react';
import { Button, Card, Empty, List, Space, Spin, Tag, Typography } from 'antd';
import type { InterviewNote } from '@/types/note';
import type {
  InterviewReviewEvidenceRef,
  InterviewReviewProposal,
} from '@/types/interviewReviewProposal';
import {
  createInterviewReviewProposal,
  getInterviewReviewProposal,
  InterviewReviewProposalError,
  listInterviewReviewProposals,
} from '@/services/interviewReviewProposals';
import { ReviewReadinessNextStep } from '@/features/reviewReadiness/ReviewReadinessNextStep';
import { isReviewReadinessDraftPending, isReviewReadinessDraftUnsaved, selectReviewReadinessOwnerDraft, type ProductActionUndoRequest, type ReviewReadinessOwnerDraft } from '@/features/reviewReadiness/contracts';
import styles from './InterviewReviewProposalDrawer.module.css';

const { Paragraph, Text, Title } = Typography;

const EVIDENCE_LABELS: Record<InterviewReviewEvidenceRef['path'], string> = {
  '/questions': '复盘问题',
  '/self_reflection': '自我反思',
  '/difficulty_points': '困难点',
  '/mood': '情绪记录',
};

interface Props {
  open: boolean;
  note: InterviewNote;
  applicationId?: number;
  eventID?: number | null;
  onClose: () => void;
  attemptState?: InterviewReviewProposalAttemptState | null;
  onAttemptStateChange?: (state: InterviewReviewProposalAttemptState | null) => void;
  onOpenStory?: (noteId: number, focusId: string) => void;
  ownerGeneration?: number;
  recoveryOwnerGeneration?: number | null;
  readinessDrafts?: Readonly<Record<string, ReviewReadinessOwnerDraft>>;
  onReadinessDraftChange?: (
    key: string,
    draft: ReviewReadinessOwnerDraft | null,
    retireOwnerKey?: string,
    undoRequest?: ProductActionUndoRequest,
  ) => boolean | void;
}

export interface InterviewReviewProposalAttemptState {
  key: string;
  result_unknown: boolean;
  event_id: number | null;
}

function newAttemptKey() {
  return crypto.randomUUID?.() ?? `interview-review-${Date.now()}`;
}

function safeErrorMessage(error: unknown): string {
  const typed = error instanceof InterviewReviewProposalError ? error : null;
  switch (typed?.code) {
    case 'interview_review_provider_error':
      return 'AI 服务暂不可用，请稍后重试。';
    case 'interview_review_unverifiable':
      return 'AI 建议未通过证据校验，原复盘未受影响，请重试。';
    case 'interview_review_source_conflict':
      return '复盘来源已变化，请重新核对后再生成。';
    case 'interview_review_event_required':
      return '请先绑定有效的面试事件。';
    case 'interview_review_not_found':
      return '面试复盘已不可见，请重新打开投递。';
    default:
      return '复盘建议暂时不可用，请稍后重试。';
  }
}

function EvidenceRefs({ refs }: { refs: InterviewReviewEvidenceRef[] }) {
  if (refs.length === 0) return null;
  return (
    <div className={styles.evidence}>
      {refs.map((ref) => (
        <div key={`${ref.path}:${ref.excerpt}`}>
          <Tag>{EVIDENCE_LABELS[ref.path]}</Tag>
          <Text type="secondary">{ref.path}</Text>
          <Paragraph className={styles.excerpt}>“{ref.excerpt}”</Paragraph>
        </div>
      ))}
    </div>
  );
}

export default function InterviewReviewProposalDrawer({
  open,
  note,
  applicationId,
  eventID,
  onClose,
  attemptState,
  onAttemptStateChange,
  onOpenStory,
  ownerGeneration = 0,
  recoveryOwnerGeneration,
  readinessDrafts,
  onReadinessDraftChange,
}: Props) {
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const [history, setHistory] = useState<InterviewReviewProposal[]>([]);
  const [selected, setSelected] = useState<InterviewReviewProposal | null>(null);
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState('');
  const activeAttemptKey = useRef<string | null>(null);
  const requestGeneration = useRef(0);
  const attemptStateChangeRef = useRef(onAttemptStateChange);
  const currentEventIDRef = useRef<number | null>(null);
  const currentEventID = eventID ?? note.application_event_id ?? null;
  const ownerScope = `${ownerGeneration}:${note.id}`;
  const ownerScopeRef = useRef(ownerScope);
  ownerScopeRef.current = ownerScope;

  useEffect(() => {
    if (!open) return;
    returnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const frame = window.requestAnimationFrame(() => headingRef.current?.focus());
    return () => {
      window.cancelAnimationFrame(frame);
      const target = returnFocusRef.current;
      if (target?.isConnected) target.focus();
    };
  }, [open, note.id]);
  attemptStateChangeRef.current = onAttemptStateChange;
  currentEventIDRef.current = currentEventID;
  const resultUnknown = attemptState?.result_unknown ?? false;
  const hasChangedSource = selected?.source_status === 'source_changed';
  const generationLabel = hasChangedSource ? '重新生成复盘建议' : '生成复盘建议';

  useEffect(() => {
    const eventIdAtScopeStart = currentEventID;
    return () => {
      const key = activeAttemptKey.current;
      if (!key) return;
      activeAttemptKey.current = null;
      attemptStateChangeRef.current?.({ key, result_unknown: true, event_id: eventIdAtScopeStart });
    };
  }, [ownerScope]);

  useEffect(() => {
    if (!open || !attemptState || attemptState.event_id === currentEventID) return;
    onAttemptStateChange?.(null);
  }, [open, currentEventID, attemptState?.event_id]);

  useEffect(() => {
    if (!open) return;
    const request = requestGeneration.current + 1;
    requestGeneration.current = request;
    const requestScope = ownerScope;
    setSelected(null);
    setHistory([]);
    setError('');
    setGenerating(false);
    setLoading(true);
    listInterviewReviewProposals(note.id)
      .then((items) => {
        if (ownerScopeRef.current === requestScope && requestGeneration.current === request) setHistory(items);
      })
      .catch((cause: unknown) => {
        if (ownerScopeRef.current === requestScope && requestGeneration.current === request) setError(safeErrorMessage(cause));
      })
      .finally(() => {
        if (ownerScopeRef.current === requestScope && requestGeneration.current === request) setLoading(false);
      });
    return () => { requestGeneration.current += 1; };
  }, [open, note.id, ownerGeneration, ownerScope]);

  const selectedProposal = useMemo(() => selected, [selected]);
  const readinessDraftsRef = useRef<Readonly<Record<string, ReviewReadinessOwnerDraft>>>(readinessDrafts ?? {});
  readinessDraftsRef.current = readinessDrafts ?? readinessDraftsRef.current;
  const blockingOwnerDrafts = Object.values(readinessDraftsRef.current).filter((draft) => (
    (draft.ownerGeneration === ownerGeneration
      || (draft.ownerGeneration === recoveryOwnerGeneration && isReviewReadinessDraftPending(draft)))
    && draft.noteId === note.id
    && (isReviewReadinessDraftPending(draft) || isReviewReadinessDraftUnsaved(draft))
  ));
  const blockingProposalIDs = new Set(blockingOwnerDrafts.map((draft) => draft.proposalId));
  const selectedProposalOwnsBlockingAction = selectedProposal !== null
    && blockingProposalIDs.has(selectedProposal.id);
  const resumableProposalID = selectedProposal && blockingProposalIDs.has(selectedProposal.id)
    ? selectedProposal.id
    : blockingProposalIDs.size === 1
      ? [...blockingProposalIDs][0]
      : null;
  const historyTargetBlocked = (proposalID: number) => generating
    || resultUnknown
    || (blockingProposalIDs.size > 0 && proposalID !== resumableProposalID);

  async function openHistory(proposalID: number) {
    const latestBlockingProposalIDs = new Set(Object.values(readinessDraftsRef.current)
      .filter((draft) => (
        (draft.ownerGeneration === ownerGeneration
          || (draft.ownerGeneration === recoveryOwnerGeneration && isReviewReadinessDraftPending(draft)))
        && draft.noteId === note.id
        && (isReviewReadinessDraftPending(draft) || isReviewReadinessDraftUnsaved(draft))
      ))
      .map((draft) => draft.proposalId));
    const latestResumableProposalID = selectedProposal && latestBlockingProposalIDs.has(selectedProposal.id)
      ? selectedProposal.id
      : latestBlockingProposalIDs.size === 1
        ? [...latestBlockingProposalIDs][0]
        : null;
    if (generating || resultUnknown || (latestBlockingProposalIDs.size > 0 && proposalID !== latestResumableProposalID)) return;
    const request = requestGeneration.current + 1;
    requestGeneration.current = request;
    const requestScope = ownerScope;
    setLoading(true);
    setError('');
    try {
      const proposal = await getInterviewReviewProposal(note.id, proposalID);
      if (ownerScopeRef.current !== requestScope || requestGeneration.current !== request) return;
      setSelected(proposal);
    } catch (cause) {
      if (ownerScopeRef.current === requestScope && requestGeneration.current === request) setError(safeErrorMessage(cause));
    } finally {
      if (ownerScopeRef.current === requestScope && requestGeneration.current === request) setLoading(false);
    }
  }

  async function handleGenerate() {
    if (!currentEventID) {
      setError('请先绑定有效的面试事件。');
      return;
    }
    const key = hasChangedSource ? newAttemptKey() : (attemptState?.key ?? newAttemptKey());
    if (!window.confirm('本次复盘内容与面试事件信息将发送给当前配置的 AI 服务。是否继续？')) return;
    onAttemptStateChange?.({ key, result_unknown: false, event_id: currentEventID });
    activeAttemptKey.current = key;
    const requestScope = ownerScope;
    setGenerating(true);
    setError('');
    try {
      const proposal = await createInterviewReviewProposal(note.id, key);
      if (ownerScopeRef.current !== requestScope) return;
      setHistory((items) => [proposal, ...items.filter((item) => item.id !== proposal.id)]);
      setSelected(proposal);
      onAttemptStateChange?.(null);
    } catch (cause) {
      if (ownerScopeRef.current !== requestScope) return;
      const safe = cause instanceof InterviewReviewProposalError ? cause : null;
      setError(safeErrorMessage(cause));
      const resultUnknown = !safe?.code || safe.code === 'interview_review_provider_error';
      if (!resultUnknown) onAttemptStateChange?.(null);
      else {
        onAttemptStateChange?.({ key, result_unknown: true, event_id: currentEventID });
      }
    } finally {
      if (activeAttemptKey.current === key) activeAttemptKey.current = null;
      if (ownerScopeRef.current === requestScope) setGenerating(false);
    }
  }

  function handleClose() {
    const key = activeAttemptKey.current ?? attemptState?.key;
    if (generating && key) {
      onAttemptStateChange?.({ key, result_unknown: true, event_id: currentEventID });
    }
    onClose();
  }

  if (!open) return null;

  return (
    <section className={styles.drawer} aria-label="面试复盘建议" aria-busy={loading || generating}>
      <div className={styles.header}>
        <div>
          <Button type="link" onClick={handleClose}>返回复盘</Button>
          <h2 ref={headingRef} tabIndex={-1} className={styles.title}>面试复盘建议</h2>
        </div>
        <Button onClick={handleClose}>关闭</Button>
      </div>

      <Card title="用户记录" size="small">
        <Paragraph><Text strong>面试问题：</Text>{note.questions || '未记录'}</Paragraph>
        <Paragraph><Text strong>自我反思：</Text>{note.self_reflection || '未记录'}</Paragraph>
        <Paragraph><Text strong>困难点：</Text>{note.difficulty_points || '未记录'}</Paragraph>
        <Paragraph><Text strong>情绪记录：</Text>{note.mood || '未记录'}</Paragraph>
      </Card>

      {error && <Paragraph type="danger" role="alert">{error}</Paragraph>}
      {resultUnknown && <Tag color="orange">结果待确认，请使用原尝试重试</Tag>}

      <Card title="历史建议" size="small" className={styles.history}>
        {loading ? <Spin /> : history.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有复盘建议" />
        ) : (
          <List
            dataSource={history}
            renderItem={(item) => (
              <List.Item>
                <Button type="link" disabled={historyTargetBlocked(item.id)} onClick={() => void openHistory(item.id)}>
                  {new Date(item.created_at).toLocaleString()} {item.source_status === 'source_changed' ? '（来源已变化）' : ''}
                </Button>
              </List.Item>
            )}
          />
        )}
      </Card>

      {selectedProposal && (
        <Card title="AI 建议" size="small">
          {hasChangedSource && <Tag color="orange">来源已变化，请重新生成</Tag>}
          <Paragraph>{selectedProposal.proposal.summary.text}</Paragraph>
          <EvidenceRefs refs={selectedProposal.proposal.summary.evidence_refs} />
          <Title level={5}>已观察到的表现</Title>
          {selectedProposal.proposal.observations.map((item) => (
            <div key={item.id} className={styles.item}>
              <Paragraph>{item.text}</Paragraph>
              <EvidenceRefs refs={item.evidence_refs} />
            </div>
          ))}
          <Title level={5}>练习重点</Title>
          {selectedProposal.proposal.practice_focuses.map((item) => (
            <div key={item.id} className={styles.item}>
              <Paragraph>{item.text}</Paragraph>
              <EvidenceRefs refs={item.evidence_refs} />
            </div>
          ))}
          <Title level={5}>待澄清问题</Title>
          {[...selectedProposal.proposal.clarifications, ...selectedProposal.proposal.next_questions].map((item) => (
            <div key={item.id} className={styles.item}>
              <Paragraph>{item.question}</Paragraph>
              <EvidenceRefs refs={item.evidence_refs} />
            </div>
          ))}
          {!selectedProposal.proposal.observations.length && !selectedProposal.proposal.practice_focuses.length && (
            <Text type="secondary">当前没有可安全验证的表现或练习重点，请先补充待澄清问题。</Text>
          )}
        </Card>
      )}

      {selectedProposal ? (
        <ReviewReadinessNextStep
          key={`review:${ownerGeneration}:${note.id}:${selectedProposal.id}`}
          noteId={note.id}
          proposal={selectedProposal}
          applicationId={applicationId ?? note.application_id}
          ownerGeneration={ownerGeneration}
          recoveryOwnerGeneration={recoveryOwnerGeneration}
          draft={applicationId ?? note.application_id
            ? selectReviewReadinessOwnerDraft(readinessDraftsRef.current, {
              ownerGeneration,
              recoveryOwnerGeneration,
              noteId: note.id,
              proposalId: selectedProposal.id,
              applicationId: (applicationId ?? note.application_id)!,
            })
            : undefined}
          onDraftChange={(next, transaction) => {
            const nextOwnerKey = transaction?.ownerKey ?? next?.ownerKey;
            if (!nextOwnerKey) return false;
            try {
              if (onReadinessDraftChange?.(nextOwnerKey, next, transaction?.retireOwnerKey, transaction?.undoRequest) === false) return false;
            } catch {
              return false;
            }
            const snapshot = { ...readinessDraftsRef.current };
            if (transaction?.retireOwnerKey) delete snapshot[transaction.retireOwnerKey];
            if (next) snapshot[nextOwnerKey] = next;
            else delete snapshot[nextOwnerKey];
            readinessDraftsRef.current = snapshot;
            return true;
          }}
          ownerBlocked={generating || resultUnknown}
          onOpenStory={onOpenStory}
        />
      ) : null}

      <Space>
        {(!selectedProposal || (hasChangedSource && !selectedProposalOwnsBlockingAction)) && (
          <Button type="primary" disabled={!currentEventID} loading={generating} onClick={() => void handleGenerate()}>
            {generationLabel}
          </Button>
        )}
        {!currentEventID && <Text type="secondary">请先绑定有效的面试事件。</Text>}
      </Space>
    </section>
  );
}
