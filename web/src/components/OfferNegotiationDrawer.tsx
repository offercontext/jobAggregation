import { useEffect, useMemo, useRef, useState } from 'react';
import { Button } from 'antd';
import {
  confirmOfferNegotiationProposal,
  createOfferNegotiationProposal,
  previewOfferNegotiation,
  getOfferNegotiationProposal,
  listOfferNegotiationProposals,
  listOfferComparisonDimensions,
  listOfferComparisonValues,
  OfferNegotiationError,
} from '@/services/offers';
import { OFFER_STATUS_LABELS, type Offer, type OfferComparisonDimension, type OfferNegotiationBlock, type OfferNegotiationProposal, type OfferNegotiationPending, type OfferNegotiationPreview, type OfferNegotiationSnapshot } from '@/types/offer';
import { ConfirmationPanel } from './ui/ConfirmationPanel';
import NegotiationBriefForm, { type NegotiationBriefValue } from './offer-negotiation/NegotiationBriefForm';
import NegotiationHistoryList from './offer-negotiation/NegotiationHistoryList';
import NegotiationProposalCard from './offer-negotiation/NegotiationProposalCard';
import OfferSnapshotSummary from './offer-negotiation/OfferSnapshotSummary';
import styles from './OfferNegotiationDrawer.module.css';
import { listOfferBindingState } from './offerWorkspaceModel';

interface Props {
  open: boolean;
  offer: Offer;
  dimensionIds?: number[];
  onClose: () => void;
  draft?: OfferNegotiationDraft;
  onDraftChange?: (draft: OfferNegotiationDraft | null) => void;
  entrypoint?: 'ui' | 'pilot';
}

export interface OfferNegotiationDraft {
  attemptKey: string;
  confirmationKey: string;
  goal: string;
  concerns: string;
  scenario: string;
  resultUnknown: boolean;
  pendingOperation: PendingOperation;
  proposalId: number | null;
  selectedBlocks: string[];
  edits: Record<string, string>;
  dimensionIds: number[];
  sourceFingerprint: string | null;
  previewSnapshot: OfferNegotiationSnapshot | null;
  previewInputKey: string | null;
}

type BlockField = 'communication_goals' | 'clarification_questions' | 'talking_points' | 'preparation_checks';
type PendingOperation = 'generate' | 'confirm' | null;

const SECTIONS: Array<[BlockField, string]> = [
  ['communication_goals', '沟通目标'],
  ['clarification_questions', '待澄清问题'],
  ['talking_points', '建议表达'],
  ['preparation_checks', '沟通前检查项'],
];

function newKey(prefix: string): string {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `${prefix}-${Date.now()}`;
}

function safeError(error: unknown): string {
  if (!(error instanceof OfferNegotiationError)) return '谈薪准备暂时不可用，请稍后重试。';
  if (error.code === 'offer_negotiation_provider_error') return 'AI 服务暂不可用，请使用原尝试重试。';
  if (error.code === 'offer_negotiation_unverifiable') return 'AI 建议未通过证据校验，请重新开始。';
  if (error.code === 'offer_negotiation_source_changed') return 'Offer 来源已变化，请查看历史记录。';
  if (error.status === 422) return '谈薪准备输入无法验证，请检查后重试。';
  if (error.status === 409) return '本次谈薪准备已发生冲突，请重新开始。';
  return '谈薪准备暂时不可用，请稍后重试。';
}

function isPending(value: OfferNegotiationProposal | OfferNegotiationPending): value is OfferNegotiationPending {
  return value.attempt_status === 'generating' || value.attempt_status === 'provider_unknown';
}

export default function OfferNegotiationDrawer(props: Props) {
  if (!props.open) return null;
  if (listOfferBindingState(props.offer) === 'unbound') {
    return (
      <section className={styles.workspace} aria-label="谈薪准备不可用">
        <header className={styles.header}>
          <div>
            <h2>无法为该 Offer 准备谈薪</h2>
            <p role="alert">历史未绑定 Offer 仅支持只读查看，不能进入谈薪准备。</p>
          </div>
          <Button onClick={props.onClose}>关闭</Button>
        </header>
      </section>
    );
  }
  return <BoundOfferNegotiationDrawer {...props} />;
}

function BoundOfferNegotiationDrawer({ open, offer, dimensionIds = [], onClose, draft, onDraftChange, entrypoint = 'ui' }: Props) {
  const [goal, setGoal] = useState(draft?.goal ?? '');
  const [concerns, setConcerns] = useState(draft?.concerns ?? '');
  const [scenario, setScenario] = useState(draft?.scenario ?? '');
  const [attemptKey, setAttemptKey] = useState(() => draft?.attemptKey ?? newKey('offer-negotiation'));
  const [confirmationKey, setConfirmationKey] = useState(() => draft?.confirmationKey ?? newKey('offer-negotiation-confirm'));
  const [proposal, setProposal] = useState<OfferNegotiationProposal | null>(null);
  const [historyProposal, setHistoryProposal] = useState<OfferNegotiationProposal | null>(null);
  const [history, setHistory] = useState<OfferNegotiationProposal[]>([]);
  const [selectedBlocks, setSelectedBlocks] = useState<string[]>(draft?.selectedBlocks ?? []);
  const [edits, setEdits] = useState<Record<string, string>>(draft?.edits ?? {});
  const [frozenDimensionIds] = useState<number[]>(() => [...(draft?.dimensionIds ?? dimensionIds)]);
  const [frozenPreview, setFrozenPreview] = useState<OfferNegotiationPreview | null>(() => (
    draft?.sourceFingerprint && draft.previewSnapshot
      ? { source_fingerprint: draft.sourceFingerprint, snapshot: draft.previewSnapshot }
      : null
  ));
  const [previewInputKey, setPreviewInputKey] = useState<string | null>(draft?.previewInputKey ?? null);
  const [dimensionFacts, setDimensionFacts] = useState<Array<OfferComparisonDimension & { value_text: string | null }>>([]);
  const [dimensionFactsState, setDimensionFactsState] = useState<'loading' | 'ready' | 'error'>('loading');
  const [busy, setBusy] = useState(false);
  const [resultUnknown, setResultUnknown] = useState(draft?.resultUnknown ?? false);
  const [error, setError] = useState<string | null>(null);
  const [pendingOperation, setPendingOperation] = useState<PendingOperation>(draft?.pendingOperation ?? null);
  const [showGenerateConfirmation, setShowGenerateConfirmation] = useState(() => Boolean(
    draft?.sourceFingerprint && draft.previewSnapshot && draft.previewInputKey,
  ));
  const [showSaveConfirmation, setShowSaveConfirmation] = useState(false);
  const suppressDraftPersistence = useRef(false);
  const lastPersistedDraft = useRef<string | null>(null);

  useEffect(() => {
    if (!open) return;
    void Promise.all([
      listOfferNegotiationProposals(offer.id),
      draft?.proposalId ? getOfferNegotiationProposal(draft.proposalId) : Promise.resolve(null),
    ]).then(([items, restored]) => {
      setHistory(items);
      if (restored && 'proposal' in restored && restored.proposal) setProposal(restored);
    }).catch(() => setHistory([]));
  }, [draft?.proposalId, offer.id, open]);

  useEffect(() => {
    if (!open) setHistoryProposal(null);
  }, [offer.id, open]);

  useEffect(() => {
    if (!open) return;
    setDimensionFactsState('loading');
    void Promise.all([listOfferComparisonDimensions(true), listOfferComparisonValues(offer.id)])
      .then(([dimensions, values]) => {
        const valuesById = new Map(values.map((value) => [value.dimension_id, value.value_text]));
        setDimensionFacts(dimensions.map((dimension) => ({
          ...dimension,
          value_text: valuesById.get(dimension.id) ?? null,
        })));
        setDimensionFactsState('ready');
      })
      .catch(() => {
        setDimensionFacts([]);
        setDimensionFactsState('error');
      });
  }, [offer.id, open]);

  useEffect(() => {
    if (!open || !onDraftChange) return;
    if (suppressDraftPersistence.current) {
      suppressDraftPersistence.current = false;
      return;
    }
    const nextDraft: OfferNegotiationDraft = {
      attemptKey,
      confirmationKey,
      goal,
      concerns,
      scenario,
      resultUnknown,
      pendingOperation,
      proposalId: proposal?.id ?? draft?.proposalId ?? null,
      selectedBlocks,
      edits,
      dimensionIds: frozenDimensionIds,
      sourceFingerprint: frozenPreview?.source_fingerprint ?? null,
      previewSnapshot: frozenPreview?.snapshot ?? null,
      previewInputKey,
    };
    const serialized = JSON.stringify(nextDraft);
    if (serialized === lastPersistedDraft.current) return;
    lastPersistedDraft.current = serialized;
    onDraftChange(nextDraft);
  }, [attemptKey, confirmationKey, concerns, edits, frozenDimensionIds, frozenPreview, goal, onDraftChange, open, pendingOperation, previewInputKey, proposal, resultUnknown, scenario, selectedBlocks]);

  const displayedProposal = historyProposal ?? proposal;
  const isHistoryView = Boolean(historyProposal);
  const blocks = useMemo(() => {
    if (!displayedProposal) return [];
    return SECTIONS.flatMap(([field]) => displayedProposal.proposal[field]);
  }, [displayedProposal]);

  if (!open) return null;
  const frozen = resultUnknown || busy;
  const hasBrief = Boolean(displayedProposal?.brief);
  const currentHasBrief = Boolean(proposal?.brief);
  const dimensionFactsReady = frozenDimensionIds.length === 0 || (
    dimensionFactsState === 'ready'
    && frozenDimensionIds.every((id) => dimensionFacts.some((dimension) => dimension.id === id))
  );

  const currentInputKey = JSON.stringify({
    dimension_ids: [...frozenDimensionIds].sort((a, b) => a - b),
    goal,
    concerns,
    scenario,
  });
  const activePreview = frozenPreview && previewInputKey === currentInputKey ? frozenPreview : null;
  const frozenOffer = displayedProposal?.input_snapshot.offer_snapshot ?? activePreview?.snapshot.offer_snapshot;
  const displayedOffer = frozenOffer ?? offer;
  const visibleDimensions = displayedProposal?.input_snapshot.offer_snapshot.dimensions
    ?? activePreview?.snapshot.offer_snapshot.dimensions
    ?? frozenDimensionIds.map((id) => {
      const dimension = dimensionFacts.find((item) => item.id === id);
      return {
        path_id: `dimension_${id}`,
        label: dimension?.label ?? `维度 ${id}`,
        value_text: dimension?.value_text ?? null,
      };
    });
  const visibleBrief = displayedProposal?.input_snapshot.user_brief ?? activePreview?.snapshot.user_brief;
  const displayedSelectedBlocks = isHistoryView ? (displayedProposal?.brief?.selected_blocks ?? []) : selectedBlocks;
  const displayedEdits = isHistoryView ? (displayedProposal?.brief?.edited_content.edits ?? {}) : edits;
  const snapshotOffer: OfferNegotiationSnapshot['offer_snapshot'] = {
    company_name: displayedOffer.company_name,
    position_name: displayedOffer.position_name,
    status: displayedOffer.status,
    base_monthly: displayedOffer.base_monthly ?? null,
    months_per_year: displayedOffer.months_per_year ?? null,
    signing_bonus: displayedOffer.signing_bonus ?? null,
    equity: displayedOffer.equity || null,
    perks: displayedOffer.perks || null,
    deadline: displayedOffer.deadline || null,
    notes: displayedOffer.notes || null,
    dimensions: visibleDimensions,
  };
  const sourceState = displayedProposal?.source_changed ? 'changed' : (displayedProposal || activePreview ? 'frozen' : 'current');

  const briefValue: NegotiationBriefValue = { goal, concerns, scenario };
  const briefValid = Boolean(goal.trim() && concerns.trim() && scenario.trim());
  const workflowStep = displayedProposal ? 3 : (activePreview ? 2 : 1);
  const workflowMessage = workflowStep === 1
    ? {
        title: '先填写这次谈薪的目标',
        detail: '补充你想争取的结果、当前顾虑和沟通场景，填完后再核对发送给 AI 的内容。',
      }
    : workflowStep === 2
      ? {
          title: '核对这次准备内容',
          detail: '确认 Offer 事实和你的输入无误后，再生成谈薪准备草稿。',
        }
      : hasBrief
        ? {
            title: '谈薪准备已保存',
            detail: '下方是本次实际保存的内容，你可以随时回来查看。',
          }
        : {
            title: '选择你要带走的建议',
            detail: '勾选需要的内容并按需编辑，保存前还会再次确认。',
          };

  const previewGeneration = async () => {
    if (frozen || !briefValid || !dimensionFactsReady) return;
    setError(null);
    setBusy(true);
    try {
      const preview = await previewOfferNegotiation(offer.id, {
        dimension_ids: [...frozenDimensionIds].sort((a, b) => a - b),
        goal,
        concerns,
        scenario,
      });
      setFrozenPreview(preview);
      setPreviewInputKey(currentInputKey);
      setShowGenerateConfirmation(true);
    } catch (caught) {
      if (caught instanceof OfferNegotiationError && caught.status === 422) {
        setAttemptKey(newKey('offer-negotiation'));
        setConfirmationKey(newKey('offer-negotiation-confirm'));
        setResultUnknown(false);
        setPendingOperation(null);
        setFrozenPreview(null);
        setPreviewInputKey(null);
        setShowGenerateConfirmation(false);
        lastPersistedDraft.current = null;
        suppressDraftPersistence.current = true;
        onDraftChange?.(null);
      }
      setError(safeError(caught));
    } finally {
      setBusy(false);
    }
  };

  const submitGeneration = async (fromRetry = false) => {
    if (!frozenPreview || (!fromRetry && frozen)) return;
    setShowGenerateConfirmation(false);
    setError(null);
    setBusy(true);
    try {
      const result = await createOfferNegotiationProposal(offer.id, {
        idempotency_key: attemptKey,
        dimension_ids: [...frozenDimensionIds].sort((a, b) => a - b),
        goal,
        concerns,
        scenario,
        source_fingerprint: frozenPreview.source_fingerprint,
      }, entrypoint);
      if (isPending(result)) {
        setResultUnknown(true);
        setPendingOperation('generate');
        setError('上次请求结果待确认，请使用原尝试重试；输入已冻结。');
      } else {
        setProposal(result);
        setSelectedBlocks([]);
        setEdits({});
        setResultUnknown(false);
        setPendingOperation(null);
        setAttemptKey(newKey('offer-negotiation'));
        setConfirmationKey(newKey('offer-negotiation-confirm'));
        setFrozenPreview(null);
        setPreviewInputKey(null);
        suppressDraftPersistence.current = true;
        onDraftChange?.(null);
      }
    } catch (caught) {
      const known = caught instanceof OfferNegotiationError;
      if (
        known &&
        (
          caught.code === 'offer_negotiation_provider_error' ||
          caught.status === 0 ||
          (caught.status >= 500 && caught.status <= 599 && caught.code == null)
        )
      ) {
        setResultUnknown(true);
        setPendingOperation('generate');
      } else {
        setResultUnknown(false);
        setPendingOperation(null);
        setAttemptKey(newKey('offer-negotiation'));
        setFrozenPreview(null);
        setPreviewInputKey(null);
      }
      setError(safeError(caught));
    } finally {
      setBusy(false);
    }
  };

  const confirm = async (fromRetry = false) => {
    if (!proposal || (frozen && !fromRetry) || currentHasBrief || selectedBlocks.length === 0) return;
    setShowSaveConfirmation(false);
    setBusy(true);
    setError(null);
    try {
      const brief = await confirmOfferNegotiationProposal(proposal.id, {
        confirmation_key: confirmationKey,
        selected_blocks: selectedBlocks,
        edited_content: edits,
      }, entrypoint);
      setProposal((current) => current ? { ...current, brief } : current);
      setResultUnknown(false);
      setPendingOperation(null);
      suppressDraftPersistence.current = true;
      onDraftChange?.(null);
    } catch (caught) {
      if (caught instanceof OfferNegotiationError && caught.code === 'offer_negotiation_source_changed') {
        setProposal((current) => current ? { ...current, source_changed: true } : current);
      } else if (caught instanceof OfferNegotiationError && (
        caught.code === 'offer_negotiation_provider_error'
        || caught.status === 0
        || (caught.status >= 500 && caught.code == null)
      )) {
        setResultUnknown(true);
        setPendingOperation('confirm');
      }
      setError(safeError(caught));
    } finally {
      setBusy(false);
    }
  };

  const selectHistory = (item: OfferNegotiationProposal) => {
    setHistoryProposal(item);
  };

  const retry = () => {
    setError(null);
    setResultUnknown(false);
    if (pendingOperation === 'confirm') void confirm(true);
    else void submitGeneration(true);
  };

  const clearPreview = () => {
    if (busy) return;
    setFrozenPreview(null);
    setPreviewInputKey(null);
    setShowGenerateConfirmation(false);
    setError(null);
  };

  return (
    <section aria-label="谈薪准备" data-testid="offer-negotiation-drawer" className={styles.workspace}>
      <header className={styles.header}>
        <h2>为 {displayedOffer.company_name} 准备谈薪</h2>
        <p className={styles.boundary}>只整理事实、问题和表达草稿，不替你决定接受或拒绝 Offer。</p>
        <Button type="text" onClick={onClose}>关闭</Button>
      </header>
      {displayedProposal && (
        <p role="status">{displayedProposal.source_changed ? '来源已变化，以下仅供历史查看。' : '已冻结 Offer 来源，可审阅引用。'}</p>
      )}
      {isHistoryView && <Button type="link" onClick={() => setHistoryProposal(null)}>返回当前谈薪准备</Button>}
      {error && <p role="alert">{error}</p>}
      {resultUnknown && !isHistoryView && (
        <button type="button" onClick={retry} disabled={busy}>使用原尝试重试</button>
      )}
      {!isHistoryView && (
        <section className={styles.workflowGuide} data-testid="offer-negotiation-next-step" aria-label="谈薪准备进度">
          <div className={styles.workflowEyebrow}>第 {workflowStep} 步，共 3 步</div>
          <ol className={styles.workflowSteps} aria-label="谈薪准备步骤">
            <li data-active={workflowStep === 1}>定目标</li>
            <li data-active={workflowStep === 2}>核对并生成</li>
            <li data-active={workflowStep === 3}>选择并保存</li>
          </ol>
          <h3>{workflowMessage.title}</h3>
          <p>{workflowMessage.detail}</p>
        </section>
      )}
      {!displayedProposal && (
        <fieldset className={styles.briefFieldset} disabled={frozen || Boolean(showGenerateConfirmation)}>
          <NegotiationBriefForm
            value={briefValue}
            disabled={frozen || Boolean(showGenerateConfirmation)}
            errors={{
              goal: goal.trim() ? undefined : '请填写本次沟通目标。',
              concerns: concerns.trim() ? undefined : '请填写本次顾虑。',
              scenario: scenario.trim() ? undefined : '请填写沟通场景。',
            }}
            onChange={(next) => {
              setGoal(next.goal);
              setConcerns(next.concerns);
              setScenario(next.scenario);
            }}
          />
          {!activePreview && (
            <Button
              type="primary"
              data-testid="offer-negotiation-generate"
              onClick={() => void previewGeneration()}
              disabled={frozen || !dimensionFactsReady || !briefValid}
            >
              下一步：检查输入
            </Button>
          )}
          {dimensionFactsState === 'loading' && frozenDimensionIds.length > 0 && (
            <p className={styles.fieldHint} role="status">正在读取比较维度，读取完成后才能继续。</p>
          )}
        </fieldset>
      )}
      <section aria-label="AI input facts" data-testid="offer-negotiation-input-facts" className={styles.factsSection}>
        <h3>本次将使用的 Offer 事实</h3>
        <p>{displayedProposal?.input_snapshot || activePreview ? '以下为本次冻结输入' : '当前 Offer 事实，尚未冻结'}</p>
        <OfferSnapshotSummary offer={snapshotOffer} brief={visibleBrief} sourceState={sourceState} />
        <dl>
          <div><dt>公司</dt><dd>{snapshotOffer.company_name}</dd></div>
          <div><dt>职位</dt><dd>{snapshotOffer.position_name}</dd></div>
          <div><dt>状态</dt><dd>{OFFER_STATUS_LABELS[snapshotOffer.status]}</dd></div>
          <div><dt>月薪</dt><dd>{snapshotOffer.base_monthly ?? '尚未填写'}</dd></div>
          <div><dt>计薪月数</dt><dd>{snapshotOffer.months_per_year ?? '尚未填写'}</dd></div>
          <div><dt>签字费</dt><dd>{snapshotOffer.signing_bonus ?? '尚未填写'}</dd></div>
          <div><dt>股权</dt><dd>{snapshotOffer.equity || '尚未填写'}</dd></div>
          <div><dt>福利</dt><dd>{snapshotOffer.perks || '尚未填写'}</dd></div>
          <div><dt>截止时间</dt><dd>{snapshotOffer.deadline || '尚未填写'}</dd></div>
          <div><dt>备注</dt><dd>{snapshotOffer.notes || '尚未填写'}</dd></div>
          {visibleBrief && (
            <>
              <div><dt>本次目标</dt><dd>{visibleBrief.goal}</dd></div>
              <div><dt>本次顾虑</dt><dd>{visibleBrief.concerns}</dd></div>
              <div><dt>沟通场景</dt><dd>{visibleBrief.scenario}</dd></div>
            </>
          )}
        </dl>
        {visibleDimensions.length > 0 && (
          <ul>
            {visibleDimensions.map((dimension) => (
              <li key={dimension.path_id}>{dimension.label}：{dimension.value_text?.trim() ? dimension.value_text : '尚未填写'}</li>
            ))}
          </ul>
        )}
      </section>
      {!displayedProposal && activePreview && showGenerateConfirmation && (
        <ConfirmationPanel
          title="确认本次 AI 输入"
          description="核对冻结的 Offer 事实与本次填写内容后，才会发送给 AI。"
          sources={[{ state: 'frozen', detail: '本次 Offer 与用户输入快照' }]}
        >
          <Button onClick={clearPreview} disabled={busy}>返回修改</Button>
          <Button
            type="primary"
            data-testid="offer-negotiation-generate"
            data-action="confirm-generate"
            onClick={() => void submitGeneration()}
            disabled={busy}
          >
            确认生成谈薪准备草稿
          </Button>
        </ConfirmationPanel>
      )}
      {dimensionFactsState === 'loading' && <p role="status">正在读取本次谈薪准备所需的 Offer 事实……</p>}
      {dimensionFactsState === 'error' && <p role="alert">Offer 自定义维度暂时无法读取，请稍后重试。</p>}
      {displayedProposal && (
        <div aria-label="谈薪准备草稿">
          {displayedProposal.proposal_status === 'safe_empty' ? (
            <p>目前没有可验证、可给出的谈薪准备建议。</p>
          ) : SECTIONS.map(([field, label]) => (
            <section key={field} className={styles.proposalSection}>
              <h3>{label}</h3>
              {displayedProposal.proposal[field].map((block: OfferNegotiationBlock) => {
                const selected = displayedSelectedBlocks.includes(block.id);
                return (
                  <NegotiationProposalCard
                    key={block.id}
                    block={block}
                    selected={selected}
                    editedText={displayedEdits[block.id] ?? block.text}
                    disabled={frozen || isHistoryView || hasBrief || displayedProposal.source_changed}
                    onToggle={() => setSelectedBlocks((current) => selected ? current.filter((id) => id !== block.id) : [...current, block.id])}
                    onEdit={(text) => setEdits((current) => ({ ...current, [block.id]: text }))}
                  />
                );
              })}
            </section>
          ))}
          {displayedProposal.proposal_status !== 'safe_empty' && !isHistoryView && !hasBrief && !displayedProposal.source_changed && (
            <>
              {selectedBlocks.length === 0 && <p className={styles.selectionHint} role="status">请选择至少一项建议后才能保存。</p>}
              <Button type="primary" data-testid="offer-negotiation-confirm" onClick={() => setShowSaveConfirmation(true)} disabled={frozen || selectedBlocks.length === 0}>确认保存谈薪准备（已选 {selectedBlocks.length} 项）</Button>
              {showSaveConfirmation && (
                <ConfirmationPanel
                  title="确认保存谈薪准备"
                  description="仅保存你选中并编辑后的内容；不会修改 Offer 或自动执行其他操作。"
                  sources={[{ state: 'frozen', detail: '本次谈薪准备 Proposal' }]}
                >
                  <Button onClick={() => setShowSaveConfirmation(false)} disabled={busy}>返回修改</Button>
                  <Button type="primary" data-action="confirm-save" onClick={() => void confirm(true)} disabled={busy}>确认保存</Button>
                </ConfirmationPanel>
              )}
            </>
          )}
          {hasBrief && (
            <section aria-label="saved negotiation brief">
              <p>已确认保存，以下是本次实际保存的内容：</p>
              {(displayedProposal.brief?.edited_content.blocks ?? [])
                .filter((block) => displayedProposal.brief?.selected_blocks.includes(block.id))
                .map((block) => (
                  <p key={block.id}>{displayedProposal.brief?.edited_content.edits?.[block.id] ?? block.text}</p>
                ))}
            </section>
          )}
        </div>
      )}
      {history.length > 0 && (
        <section aria-label="历史谈薪准备">
          <h3>历史记录</h3>
          <NegotiationHistoryList items={history} selectedId={historyProposal?.id ?? proposal?.id ?? null} onSelect={selectHistory} />
        </section>
      )}
      {blocks.length === 0 && displayedProposal?.proposal_status === 'safe_empty' && <p>未生成可验证内容，未保存任何谈薪准备记录。</p>}
    </section>
  );
}
