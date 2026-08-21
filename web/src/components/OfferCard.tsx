import { Card, Tag, Button, Checkbox, Space, Typography } from 'antd';
import { MessageOutlined, EyeOutlined } from '@ant-design/icons';
import type { Offer } from '@/types/offer';
import { OFFER_STATUS_LABELS, OFFER_STATUS_COLORS } from '@/types/offer';
import { createPilotAttachmentDragBinding } from './PilotAttachmentHandle';
import styles from './OfferCard.module.css';

const { Text } = Typography;

interface Props {
  offer: Offer;
  selectable?: boolean;
  selected: boolean;
  onToggleSelect: (id: number) => void;
  onCoach: (offer: Offer) => void;
  onNegotiation?: (offer: Offer) => void;
  onView: (offer: Offer) => void;
  onAttachToPilot?: (attachment: import('@/types/chat').PilotContextAttachment) => void;
}

function formatWan(n: number): string {
  return (n / 10000).toFixed(1) + '万';
}

export default function OfferCard({ offer, selectable = true, selected, onToggleSelect, onCoach, onNegotiation, onView, onAttachToPilot }: Props) {
  const offerDragBinding = onAttachToPilot
    ? createPilotAttachmentDragBinding({
        kind: 'offer',
        id: String(offer.id),
        label: `${offer.company_name} · ${offer.position_name}`,
      })
    : undefined;

  return (
    <Card
      size="small"
      className={styles.card}
      style={{ borderColor: OFFER_STATUS_COLORS[offer.status] }}
      {...offerDragBinding}
      title={
        <Space className={styles.heading}>
          {selectable ? <Checkbox aria-label={`选择 Offer：${offer.company_name}｜${offer.position_name}`} checked={selected} onChange={() => onToggleSelect(offer.id)} /> : null}
          <Text strong>{offer.company_name}</Text>
        </Space>
      }
      extra={<Tag color={OFFER_STATUS_COLORS[offer.status]}>{OFFER_STATUS_LABELS[offer.status]}</Tag>}
    >
      <div className={styles.position}>{offer.position_name}</div>
      <div className={styles.salary}>
        {offer.base_monthly > 0 ? `${offer.base_monthly / 1000}K` : '月薪待确认'}
        {offer.months_per_year > 0 ? ` × ${offer.months_per_year} 薪` : ' · 年薪月数待确认'}
      </div>
      <div className={styles.facts}>
        签字费 {offer.signing_bonus == null ? '尚未填写' : formatWan(offer.signing_bonus)}
        {offer.equity ? ` · 期权 ${offer.equity}` : ''}
        <br />
        {offer.total_cash > 0 ? `年总包约 ${formatWan(offer.total_cash)}` : '年总包待确认'}
        {offer.deadline ? ` · 截止 ${offer.deadline}` : ''}
        {offer.application_id ? ` · 关联投递 #${offer.application_id}` : ' · 无关联投递'}
      </div>
      <div className={styles.actions}>
        {onNegotiation && (
          <Button type="primary" data-action="start-negotiation" onClick={() => onNegotiation(offer)}>
            开始谈薪准备
          </Button>
        )}
        <Button data-action="open-negotiation-coach" icon={<MessageOutlined />} onClick={() => onCoach(offer)}>
          谈薪教练
        </Button>
        <Button data-action="view-offer" icon={<EyeOutlined />} onClick={() => onView(offer)}>
          详情
        </Button>
      </div>
    </Card>
  );
}
