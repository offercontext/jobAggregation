import { createElement, useEffect, useState } from 'react';
import { Switch } from 'antd';
import type { Offer } from '@/types/offer';
import { OFFER_STATUS_LABELS, OFFER_STATUS_COLORS } from '@/types/offer';
import type { Capability } from './capabilities';
import { remainingEvidence, selectEvidence, type EvidenceItem, type EvidenceTarget } from './model';
import EvidenceList from './EvidenceList';
import PilotOfferSelectionCard from './PilotOfferSelectionCard';
import { listOfferBindingState } from '../offerWorkspaceModel';
import styles from './ChatPanel.module.css';

interface Props {
  floating?: boolean;
  isNego: boolean;
  offer: Offer | null;
  capabilities: Capability[];
  evidence: EvidenceItem[];
  autoApprove: boolean;
  hasKey: boolean;
  degraded: boolean;
  disabled: boolean;
  onCapability: (cap: Capability) => void;
  onToggleAutoApprove: (v: boolean) => void;
  onOpenSettings?: () => void;
  onOpenEvidence?: (target: EvidenceTarget) => void;
  onPrepareOfferNegotiation?: (offer: Offer) => void;
  onOpenInterviewStoryLibrary?: () => void;
  offers?: Offer[];
  contextKey?: string;
}

function formatTotal(total: number): string {
  if (total >= 10000) return (total / 10000).toFixed(1);
  return String(total);
}

export default function ContextPanel({
  floating,
  isNego,
  offer,
  capabilities,
  evidence,
  autoApprove,
  hasKey,
  degraded,
  disabled,
  onCapability,
  onToggleAutoApprove,
  onOpenSettings,
  onOpenEvidence,
  onPrepareOfferNegotiation,
  onOpenInterviewStoryLibrary,
  offers = [],
  contextKey = '',
}: Props) {
  const [offerPickerOpen, setOfferPickerOpen] = useState(false);
  const [selectedOffer, setSelectedOffer] = useState<Offer | null>(null);
  useEffect(() => {
    setSelectedOffer(null);
    setOfferPickerOpen(false);
  }, [contextKey, isNego, offer?.id]);
  const evidenceSelection = selectEvidence(evidence, 5);
  const activeOffer = offer ?? selectedOffer;
  const activeOfferIsBound = activeOffer ? listOfferBindingState(activeOffer) === 'bound' : false;
  const eligibleOffers = offers.filter((candidate) => listOfferBindingState(candidate) === 'bound');

  return (
    <aside className={`${styles.context} ${floating ? styles.contextFloating : ''}`}>
      {activeOffer && (isNego || selectedOffer !== null) && (
        <div>
          <div className={styles.panelLabel}>{offer ? '当前绑定 Offer' : '已选择 Offer'}</div>
          <div className={styles.offerCard}>
            <div className={styles.offerCompany}>{activeOffer.company_name}</div>
            <div className={styles.offerRole}>{activeOffer.position_name}</div>
            <div className={styles.offerTotal}>
              {formatTotal(activeOffer.total_cash)}
              <span className={styles.offerUnit}>w 总包/年</span>
            </div>
            <span
              className={styles.offerStatus}
              style={{
                color: OFFER_STATUS_COLORS[activeOffer.status],
                background: `${OFFER_STATUS_COLORS[activeOffer.status]}1f`,
              }}
            >
              {OFFER_STATUS_LABELS[activeOffer.status]}
            </span>
          </div>
          {onPrepareOfferNegotiation && activeOfferIsBound ? (
            <>
              <button
                type="button"
                className={styles.capItem}
                data-testid="pilot-prepare-offer-negotiation"
                onClick={() => onPrepareOfferNegotiation(activeOffer)}
              >
                准备谈薪
              </button>
              {!offer && selectedOffer && (
                <button
                  type="button"
                  className={styles.capItem}
                  data-action="change-offer-negotiation"
                  onClick={() => {
                    setSelectedOffer(null);
                    setOfferPickerOpen(true);
                  }}
                >
                  更换 Offer
                </button>
              )}
            </>
          ) : activeOffer ? (
            <div role="note" className={styles.evidenceEmpty}>历史未绑定 Offer 仅支持只读查看，不能进入谈薪准备。</div>
          ) : null}
        </div>
      )}
      {!activeOffer && onPrepareOfferNegotiation && eligibleOffers.length > 0 && (
        <div>
          <div className={styles.panelLabel}>谈薪准备</div>
          {!offerPickerOpen && (
            <button
              type="button"
              className={styles.capItem}
              data-testid="pilot-choose-offer-negotiation"
              onClick={() => setOfferPickerOpen(true)}
            >
              选择 Offer 后准备谈薪
            </button>
          )}
          {offerPickerOpen && (
            <PilotOfferSelectionCard
              offers={eligibleOffers}
              onCancel={() => setOfferPickerOpen(false)}
              onContinue={(selected) => {
                setSelectedOffer(selected);
                setOfferPickerOpen(false);
              }}
            />
          )}
        </div>
      )}
      {!activeOffer && onPrepareOfferNegotiation && offers.length > 0 && eligibleOffers.length === 0 ? (
        <div>
          <div className={styles.panelLabel}>谈薪准备</div>
          <div role="note" className={styles.evidenceEmpty}>暂无可用于谈薪准备的已绑定 Offer。</div>
        </div>
      ) : null}

      {onOpenInterviewStoryLibrary ? (
        <div>
          <div className={styles.panelLabel}>面试故事</div>
          <button
            type="button"
            className={styles.capItem}
            data-testid="pilot-open-interview-story-library"
            data-story-audit="pilot-entry"
            onClick={onOpenInterviewStoryLibrary}
          >
            整理面试故事
          </button>
        </div>
      ) : null}

      <div>
        <div className={styles.panelLabel}>当前参考依据</div>
        {evidenceSelection.visible.length ? (
          <EvidenceList
            items={evidenceSelection.visible}
            similar={evidenceSelection.similar}
            remaining={remainingEvidence(evidence, evidenceSelection.visible)}
            remainingCount={evidenceSelection.remainingCount}
            clamped
            onOpenEvidence={onOpenEvidence}
          />
        ) : (
          <div className={styles.evidenceEmpty}>
            暂无参考依据。你可以直接提问，或选择一个能力，让领航员读取本地求职数据。
          </div>
        )}
      </div>

      <div>
        <div className={styles.panelLabel}>{isNego ? '谈薪教练能力' : '常用能力'}</div>
        <div className={styles.capList}>
          {capabilities.map((cap) => {
            return (
              <button
                key={cap.id}
                type="button"
                className={styles.capItem}
                disabled={disabled}
                onClick={() => onCapability(cap)}
                title={cap.hint}
              >
                <span className={styles.capIcon} aria-hidden="true">
                  {createElement(cap.icon)}
                </span>
                {cap.label}
              </button>
            );
          })}
        </div>
      </div>

      <div>
        <div className={styles.panelLabel}>设置</div>
        <label className={styles.setting}>
          <span>本地写入自动确认</span>
          <Switch checked={autoApprove} onChange={onToggleAutoApprove} />
        </label>
      </div>

      {!hasKey && (
        <div className={`${styles.notice} ${styles.noticeWarn}`}>
          尚未配置 API key，请先完成设置。
          {onOpenSettings && (
            <button type="button" className={styles.noticeAction} onClick={onOpenSettings}>
              打开设置
            </button>
          )}
        </div>
      )}
      {degraded && (
        <div className={styles.notice}>
          当前模型不支持工具调用，已切换为只读摘要模式，AI 无法修改你的数据。
        </div>
      )}
      <div className={styles.notice}>
        领航员可读取并修改你的投递、日程、复盘与 Offer。删除等高风险写入始终需要你手动确认。
      </div>
    </aside>
  );
}
