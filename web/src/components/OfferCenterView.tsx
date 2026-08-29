import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Alert, Row, Col, Button, Space, Spin, Empty, Typography, message } from 'antd';
import { PlusOutlined, SwapOutlined } from '@ant-design/icons';
import type { Application } from '@/types/application';
import type { Offer } from '@/types/offer';
import { listOffers } from '@/services/offers';
import OfferCard from '@/components/OfferCard';
import AddOfferForm from '@/components/AddOfferForm';
import OfferCompareDrawer from '@/components/OfferCompareDrawer';
import OfferComparisonDimensionPanel from '@/components/OfferComparisonDimensionPanel';
import { findEvidenceFocusRecord } from '@/lib/pilotEvidenceFocus';
import { getOfferWorkspaceMode, listMissingOfferFacts, listOfferBindingState } from './offerWorkspaceModel';

interface Props {
  applications: Application[];
  onCoach: (offer: Offer) => void;
  onAddApplication?: () => void;
  /** Opens the owning canonical application detail for a bound Offer. */
  onOpenApplication?: (applicationId: number) => void;
  /**
   * A numeric token opens the create flow when it increases. A callback keeps
   * compatibility with hosts that generate the token inside the click handler.
   */
  createRequestToken?: number | (() => string | number | void);
  onAttachToPilot?: (attachment: import('@/types/chat').PilotContextAttachment) => void;
  focusOfferId?: number;
  onEvidenceFocusConsumed?: () => void;
  /** The composition root owns the sole negotiation task surface. */
  onOpenNegotiation?: (offer: Offer) => void;
}

export default function OfferCenterView({
  applications,
  onCoach,
  onAddApplication,
  onOpenApplication,
  createRequestToken,
  onAttachToPilot,
  focusOfferId,
  onEvidenceFocusConsumed,
  onOpenNegotiation,
}: Props) {
  const [addOpen, setAddOpen] = useState(false);
  const [editing, setEditing] = useState<Offer | null>(null);
  const [compareOpen, setCompareOpen] = useState(false);
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const [selectedDimensionIds, setSelectedDimensionIds] = useState<number[]>([]);
  const [entryRequestToken, setEntryRequestToken] = useState<string | null>(null);
  const lastCreateRequestTokenRef = useRef<number | undefined>(
    typeof createRequestToken === 'number' ? createRequestToken : undefined,
  );
  const openCreateOffer = () => {
    if (applications.length === 0) {
      onAddApplication?.();
      return;
    }
    const token = typeof createRequestToken === 'function' ? createRequestToken() : createRequestToken;
    setEntryRequestToken(token === undefined ? null : String(token));
    setEditing(null);
    setAddOpen(true);
  };
  const openEditOffer = (offer: Offer) => {
    setEntryRequestToken(null);
    setEditing(offer);
    setAddOpen(true);
  };

  useEffect(() => {
    if (typeof createRequestToken !== 'number') return;
    const previous = lastCreateRequestTokenRef.current;
    lastCreateRequestTokenRef.current = createRequestToken;
    if (createRequestToken > 0 && previous !== undefined && createRequestToken > previous) {
      if (applications.length === 0) {
        onAddApplication?.();
        return;
      }
      setEntryRequestToken(String(createRequestToken));
      setEditing(null);
      setAddOpen(true);
    }
  }, [applications.length, createRequestToken, onAddApplication]);
  const handleOpenNegotiation = (offer: Offer) => {
    if (listOfferBindingState(offer) === 'unbound') {
      message.warning('历史未绑定 Offer 仅支持只读查看');
      return;
    }
    setCompareOpen(false);
    onOpenNegotiation?.(offer);
  };

  const { data: offers = [], isLoading, isError, isFetching, refetch } = useQuery({
    queryKey: ['offers'],
    queryFn: () => listOffers(),
  });

  useEffect(() => {
    if (focusOfferId === undefined || isLoading || isError || isFetching) return;
    const offer = findEvidenceFocusRecord(offers, focusOfferId);
    if (offer) {
      setCompareOpen(false);
      setEntryRequestToken(null);
      setEditing(offer);
      setAddOpen(true);
    } else {
      message.warning('引用的记录已不存在');
    }
    onEvidenceFocusConsumed?.();
  }, [focusOfferId, isLoading, isError, isFetching, offers, onEvidenceFocusConsumed]);

  const toggleSelect = (id: number) =>
    setSelectedIds((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  const selectedOffers = selectedIds
    .map((id) => offers.find((offer) => offer.id === id))
    .filter((offer): offer is Offer => Boolean(offer));
  const mode = getOfferWorkspaceMode(offers.length, compareOpen);
  const visibleApplicationIds = new Set(applications.map((application) => application.id));
  const applicationLinkState = (offer: Offer): 'unbound' | 'available' | 'unavailable' => {
    if (listOfferBindingState(offer) === 'unbound') return 'unbound';
    return visibleApplicationIds.has(Number(offer.application_id)) ? 'available' : 'unavailable';
  };

  if (isLoading) {
    return <div role="status" style={{ textAlign: 'center', padding: 48 }}><Spin size="large" /><div>正在加载 Offer</div></div>;
  }
  if (isError) {
    return <div role="alert" style={{ textAlign: 'center', padding: 48 }}><Empty description="加载 Offer 失败"><Button onClick={() => void refetch()}>重试</Button></Empty></div>;
  }
  if (compareOpen) {
    return (
      <div style={{ display: 'grid', gap: 16 }}>
        <OfferComparisonDimensionPanel offers={offers} onSelectionChange={setSelectedDimensionIds} />
        <div data-selected-comparison-dimensions={selectedDimensionIds.join(',')}>
          <OfferCompareDrawer
            open={compareOpen}
            onClose={() => setCompareOpen(false)}
            offers={selectedOffers}
            dimensionIds={selectedDimensionIds}
            onCoach={onCoach}
            onNegotiation={handleOpenNegotiation}
          />
        </div>
      </div>
    );
  }

  return (
    <div style={{ display: 'grid', gap: 16 }} data-offer-workspace-mode={mode}>
      <Row justify="space-between" align="middle">
        <Col>
          <Typography.Title level={2} style={{ margin: 0 }}>Offer</Typography.Title>
          {mode === 'selection' ? <Typography.Text type="secondary">选择至少两份 Offer 进行比较</Typography.Text> : null}
        </Col>
        <Col>
          {mode === 'selection' ? (
            <Space>
              <Button icon={<SwapOutlined />} disabled={selectedIds.length < 2} onClick={() => setCompareOpen(true)}>
                开始比较（已选 {selectedIds.length}）
              </Button>
                <Button icon={<PlusOutlined />} onClick={openCreateOffer}>录入 Offer</Button>
            </Space>
          ) : null}
        </Col>
      </Row>
      {mode === 'entry' ? (
        <Empty
          description={applications.length === 0
            ? 'Offer 必须绑定所属投递，请先添加投递。'
            : '先录入 Offer，再逐步补齐薪酬、截止时间和沟通安排'}
        >
          <Button icon={<PlusOutlined />} onClick={openCreateOffer} disabled={applications.length === 0 && !onAddApplication}>
            {applications.length === 0 ? '先添加投递' : '录入第一份 Offer'}
          </Button>
        </Empty>
      ) : null}
      {mode === 'single' ? (
        <>
          <Alert
            type="info"
            showIcon
            message={`待确认：${listMissingOfferFacts(offers[0]).join('、') || '关键信息已补齐'}`}
            description={`截止时间：${offers[0].deadline || '待确认'} · 下一次沟通：待安排`}
          />
          <Row gutter={[16, 16]}>
            <Col xs={24} lg={12}>
              <OfferCard
                offer={offers[0]}
                selectable={false}
                emphasis="secondary"
                selected={false}
                onToggleSelect={toggleSelect}
                onCoach={onCoach}
                onNegotiation={listOfferBindingState(offers[0]) === 'bound' ? handleOpenNegotiation : undefined}
                onAttachToPilot={onAttachToPilot}
                applicationLinkState={applicationLinkState(offers[0])}
                onOpenApplication={onOpenApplication}
                onView={openEditOffer}
              />
            </Col>
          </Row>
          <Button style={{ justifySelf: 'start' }} icon={<PlusOutlined />} onClick={openCreateOffer}>录入另一份 Offer</Button>
        </>
      ) : null}
      {mode === 'selection' ? (
        <Row gutter={[16, 16]}>
          {offers.map((offer) => (
            <Col key={offer.id} xs={24} sm={12} md={8}>
              <OfferCard
                offer={offer}
                emphasis="secondary"
                selected={selectedIds.includes(offer.id)}
                onToggleSelect={toggleSelect}
                onCoach={onCoach}
                onNegotiation={listOfferBindingState(offer) === 'bound' ? handleOpenNegotiation : undefined}
                onAttachToPilot={onAttachToPilot}
                applicationLinkState={applicationLinkState(offer)}
                onOpenApplication={onOpenApplication}
                onView={openEditOffer}
              />
            </Col>
          ))}
        </Row>
      ) : null}
      <AddOfferForm
        open={addOpen}
        onClose={() => setAddOpen(false)}
        applications={applications}
        editing={editing}
        requestToken={entryRequestToken}
      />
    </div>
  );
}
