import { Button, Card, Empty, Radio, Space } from 'antd';
import { useState } from 'react';
import type { Offer } from '@/types/offer';
import { listOfferBindingState } from '../offerWorkspaceModel';
import styles from './ChatPanel.module.css';

interface Props {
  offers: Offer[];
  disabled?: boolean;
  onContinue: (offer: Offer) => void;
  onCancel: () => void;
}

function offerLabel(offer: Offer): string {
  return `${offer.company_name}｜${offer.position_name}`;
}

export default function PilotOfferSelectionCard({ offers, disabled = false, onContinue, onCancel }: Props) {
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const eligibleOffers = offers.filter((offer) => listOfferBindingState(offer) === 'bound');
  const selectedOffer = eligibleOffers.find((offer) => offer.id === selectedId) ?? null;

  return (
    <Card size="small" className={styles.pilotOfferSelectionCard}>
      <div className={styles.panelLabel}>选择要准备谈薪的 Offer</div>
      {eligibleOffers.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无可用于谈薪准备的已绑定 Offer" />
      ) : (
        <>
          <Radio.Group
            aria-label="选择 Offer"
            value={selectedId ?? undefined}
            onChange={(event) => setSelectedId(Number(event.target.value))}
            disabled={disabled}
          >
            <Space direction="vertical" className={styles.pilotOfferOptions}>
              {eligibleOffers.map((offer) => (
                <Radio key={offer.id} value={offer.id}>
                  {offerLabel(offer)}
                </Radio>
              ))}
            </Space>
          </Radio.Group>
          {selectedOffer && (
            <div className={styles.pilotOfferAnswer} role="status">
              <strong>已选择 Offer</strong>
              <span>{offerLabel(selectedOffer)}</span>
            </div>
          )}
          <div className={styles.pilotOfferActions}>
            <Button onClick={onCancel} disabled={disabled}>取消</Button>
            <Button
              type="primary"
              data-action="continue-offer-negotiation"
              onClick={() => selectedOffer && onContinue(selectedOffer)}
              disabled={disabled || !selectedOffer}
            >
              继续准备谈薪
            </Button>
          </div>
        </>
      )}
    </Card>
  );
}
