import { useEffect, useState } from 'react';
import { ArrowLeftOutlined } from '@ant-design/icons';
import { Button, Empty, Table } from 'antd';
import type { Offer, OfferComparisonRead } from '@/types/offer';
import { OFFER_STATUS_LABELS } from '@/types/offer';
import { readOfferComparison } from '@/services/offers';
import { listOfferBindingState } from './offerWorkspaceModel';
import styles from './OfferCompareDrawer.module.css';

interface Props {
  open: boolean;
  onClose: () => void;
  offers: Offer[];
  dimensionIds?: number[];
  onCoach?: (offer: Offer) => void;
  onNegotiation?: (offer: Offer) => void;
}

interface Row {
  key: string;
  field: string;
  [companyKey: string]: React.ReactNode;
}

function formatWan(value: number): string {
  return `${(value / 10000).toFixed(1)} 万元`;
}

function missingValue(): React.ReactElement {
  return <span className={styles.missing} data-missing="true">尚未填写</span>;
}

function displayValue(value: string | number | null | undefined): React.ReactNode {
  if (typeof value === 'number') return value;
  if (typeof value === 'string' && value.trim()) return value;
  return missingValue();
}

export default function OfferCompareDrawer({
  open,
  onClose,
  offers,
  dimensionIds = [],
  onCoach,
  onNegotiation,
}: Props) {
  const [comparison, setComparison] = useState<OfferComparisonRead | null>(null);

  useEffect(() => {
    if (!open || offers.length < 2) {
      setComparison(null);
      return;
    }
    let current = true;
    void readOfferComparison(offers.map((offer) => offer.id), dimensionIds)
      .then((payload) => { if (current) setComparison(payload); })
      .catch(() => { if (current) setComparison(null); });
    return () => { current = false; };
  }, [open, offers, dimensionIds]);

  if (!open) return null;
  const displayedOffers = comparison?.offers ?? offers;
  const columns = [
    { title: '比较事实', dataIndex: 'field', key: 'field', fixed: 'left' as const, width: 160 },
    ...displayedOffers.map((offer) => ({
      title: (
        <div className={styles.offerHeader} data-testid={`offer-comparison-header-${offer.id}`}>
          <strong>{offer.company_name}</strong>
          <span>{offer.position_name}</span>
          <b>{offer.base_monthly / 1000}K × {offer.months_per_year}</b>
        </div>
      ),
      dataIndex: `c${offer.id}`,
      key: `c${offer.id}`,
    })),
  ];

  const fieldRow = (field: string, value: (offer: Offer) => React.ReactNode): Row => {
    const row: Row = { key: field, field };
    displayedOffers.forEach((offer) => { row[`c${offer.id}`] = value(offer); });
    return row;
  };

  const fixedData: Row[] = [
    fieldRow('职位', (offer) => displayValue(offer.position_name)),
    fieldRow('状态', (offer) => OFFER_STATUS_LABELS[offer.status]),
    fieldRow('月薪与月数', (offer) => `${offer.base_monthly / 1000}K × ${offer.months_per_year}`),
    fieldRow('签字费', (offer) => offer.signing_bonus == null ? missingValue() : formatWan(offer.signing_bonus)),
    fieldRow('年总包事实', (offer) => formatWan(offer.total_cash)),
    fieldRow('期权', (offer) => displayValue(offer.equity)),
    fieldRow('福利', (offer) => displayValue(offer.perks)),
    fieldRow('截止时间', (offer) => displayValue(offer.deadline)),
  ];
  const customDimensionData: Row[] = comparison?.dimensions.map((dimension) => fieldRow(dimension.label, (offer) => {
    const cell = dimension.values.find((value) => value.offer_id === offer.id);
    return displayValue(cell?.value_text);
  })) ?? [];

  return (
    <section className={styles.workspace} aria-label="Offer 横向对比" data-selected-dimension-ids={dimensionIds.join(',')}>
      <div className={styles.header}>
        <Button type="link" icon={<ArrowLeftOutlined />} onClick={onClose}>
          返回 Offer 中心
        </Button>
        <h2>Offer 横向对比</h2>
      </div>
      {displayedOffers.length === 0 ? (
        <Empty description="请选择至少两个 Offer" />
      ) : (
        <>
          <div data-section="fixed-facts">
            <h3 className={styles.sectionTitle}>固定薪酬事实</h3>
            <Table columns={columns} dataSource={fixedData} pagination={false} scroll={{ x: true }} size="small" bordered />
          </div>
          {customDimensionData.length > 0 && (
            <div data-section="custom-dimensions" className={styles.customSection}>
              <h3 className={styles.sectionTitle}>自定义比较维度</h3>
              <p className={styles.muted}>仅并排展示用户记录的固定事实与文字维度。</p>
              <Table columns={columns} dataSource={customDimensionData} pagination={false} scroll={{ x: true }} size="small" bordered />
            </div>
          )}
          {(onNegotiation || onCoach) && (
            <div style={{ display: 'flex', gap: 8, marginTop: 16, flexWrap: 'wrap' }}>
              {displayedOffers.filter((offer) => listOfferBindingState(offer) === 'bound').map((offer) => (
                <Button key={offer.id} data-action="start-negotiation" data-offer-id={offer.id} onClick={() => (onNegotiation ?? onCoach)?.(offer)}>
                  为 {offer.company_name} 准备谈薪
                </Button>
              ))}
            </div>
          )}
        </>
      )}
    </section>
  );
}
