import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Card, Checkbox, Empty, Input, Spin, Tag, Tooltip } from 'antd';
import type { Offer, OfferComparisonDimension } from '@/types/offer';
import {
  clearOfferComparisonValue,
  createOfferComparisonDimension,
  listOfferComparisonDimensions,
  listOfferComparisonValues,
  saveOfferComparisonValue,
  updateOfferComparisonDimension,
} from '@/services/offers';
import styles from './OfferComparisonDimensionPanel.module.css';
import { listOfferBindingState } from './offerWorkspaceModel';

interface Props {
  offers: Offer[];
  onSelectionChange?: (dimensionIds: number[]) => void;
  onChanged?: () => void;
  selectedDimensionIds?: number[];
}

export default function OfferComparisonDimensionPanel({ offers, onSelectionChange, onChanged, selectedDimensionIds }: Props) {
  const [dimensions, setDimensions] = useState<OfferComparisonDimension[]>([]);
  const [localSelectedIds, setSelectedIds] = useState<number[]>([]);
  const selectedIds = selectedDimensionIds ?? localSelectedIds;
  const loadGeneration = useRef(0);
  const [draftLabel, setDraftLabel] = useState('');
  const [draftValues, setDraftValues] = useState<Record<string, string>>({});
  const [loadState, setLoadState] = useState<'loading' | 'ready' | 'error'>('loading');

  const reload = async () => {
    const generation = ++loadGeneration.current;
    setLoadState('loading');
    try {
      const [nextDimensions, valueLists] = await Promise.all([
        listOfferComparisonDimensions(true),
        Promise.all(offers.map((offer) => listOfferComparisonValues(offer.id))),
      ]);
      if (generation !== loadGeneration.current) return;
      setDimensions(nextDimensions);
      const nextValues: Record<string, string> = {};
      valueLists.flat().forEach((value) => {
        nextValues[`${value.offer_id}:${value.dimension_id}`] = value.value_text ?? '';
      });
      setDraftValues(nextValues);
      const nextSelected = selectedIds.filter((id) => nextDimensions.some((dimension) => dimension.id === id && dimension.archived_at === null));
      setSelectedIds(nextSelected);
      if (nextSelected.length !== selectedIds.length) onSelectionChange?.(nextSelected);
      setLoadState('ready');
    } catch {
      if (generation !== loadGeneration.current) return;
      setDimensions([]);
      setDraftValues({});
      setLoadState('error');
    }
  };

  useEffect(() => {
    void reload();
    return () => { loadGeneration.current += 1; };
    // The panel intentionally refreshes only when the Offer set changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [offers.map((offer) => offer.id).join(',')]);

  const selectDimension = (id: number, checked: boolean) => {
    const next = checked
      ? [...selectedIds, id].sort((left, right) => left - right)
      : selectedIds.filter((item) => item !== id);
    setSelectedIds(next);
    onSelectionChange?.(next);
  };

  const createDimension = async () => {
    if (!draftLabel.trim()) return;
    const created = await createOfferComparisonDimension(draftLabel);
    setDimensions((current) => [...current, created].sort((left, right) => left.id - right.id));
    setDraftLabel('');
  };

  const archiveDimension = async (id: number) => {
    const updated = await updateOfferComparisonDimension(id, { archived: true });
    setDimensions((current) => current.map((dimension) => dimension.id === id ? updated : dimension));
    const next = selectedIds.filter((item) => item !== id);
    setSelectedIds(next);
    onSelectionChange?.(next);
  };

  const saveValue = async (offerId: number, dimensionId: number) => {
    if (!offers.some((offer) => offer.id === offerId && listOfferBindingState(offer) === 'bound')) return;
    const value = draftValues[`${offerId}:${dimensionId}`] ?? '';
    if (!value.trim()) return;
    const generation = loadGeneration.current;
    const saved = await saveOfferComparisonValue(offerId, dimensionId, value);
    if (generation !== loadGeneration.current) return;
    setDraftValues((current) => ({ ...current, [`${saved.offer_id}:${saved.dimension_id}`]: saved.value_text ?? '' }));
    onChanged?.();
  };

  const clearValue = async (offerId: number, dimensionId: number) => {
    if (!offers.some((offer) => offer.id === offerId && listOfferBindingState(offer) === 'bound')) return;
    const generation = loadGeneration.current;
    await clearOfferComparisonValue(offerId, dimensionId);
    if (generation !== loadGeneration.current) return;
    setDraftValues((current) => ({ ...current, [`${offerId}:${dimensionId}`]: '' }));
    onChanged?.();
  };

  if (loadState === 'loading') {
    return <section className={styles.panel} aria-label="自定义比较维度"><div className={styles.loading} role="status"><Spin size="small" /> 正在加载比较维度</div></section>;
  }

  if (loadState === 'error') {
    return <section className={styles.panel} aria-label="自定义比较维度"><Alert message="比较维度暂时无法加载" type="error" action={<Button size="small" onClick={() => void reload()}>重试</Button>} /></section>;
  }

  return (
    <section className={styles.panel} aria-label="自定义比较维度">
      <div className={styles.panelHeader}>
        <div>
          <h3 className={styles.title}>管理比较维度</h3>
          <p className={styles.help}>最多选择 8 个比较维度。空白值表示尚未填写，不代表负面判断。</p>
        </div>
        <Tag color="blue">已选择 {selectedIds.length}/8</Tag>
      </div>
      <div className={styles.createRow}>
        <Input
          aria-label="新比较维度"
          placeholder="新比较维度"
          value={draftLabel}
          onChange={(event) => setDraftLabel(event.target.value)}
        />
        <Button data-action="create-dimension" onClick={() => void createDimension()}>新增维度</Button>
      </div>
      {dimensions.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有自定义比较维度" /> : dimensions.map((dimension) => {
        const active = dimension.archived_at === null;
        const selected = selectedIds.includes(dimension.id);
        const selectionBlocked = !selected && selectedIds.length >= 8;
        return (
          <Card key={dimension.id} className={styles.dimensionCard} data-testid="comparison-dimension-card" data-dimension-id={dimension.id} size="small">
            <div className={styles.dimensionHeader}>
              <label>
              {active && (
                <Checkbox checked={selected} disabled={selectionBlocked} onChange={(event) => selectDimension(dimension.id, event.target.checked)}>
                  {dimension.label}
                </Checkbox>
              )}
              {!active && <><span>{dimension.label}</span><Tag className={styles.archiveTag}>已归档，仅历史可读</Tag></>}
              </label>
              {active ? <Tag color="green">可用于比较</Tag> : null}
            </div>
            <div className={styles.offerValues}>
              {offers.map((offer) => {
                const key = `${offer.id}:${dimension.id}`;
                return (
                  <div key={key} className={styles.offerValue}>
                    <label htmlFor={`dimension-${dimension.id}-offer-${offer.id}`}>{offer.company_name}</label>
                    {active && listOfferBindingState(offer) === 'bound' ? (
                      <>
                        <Input
                          id={`dimension-${dimension.id}-offer-${offer.id}`}
                          data-offer-id={offer.id}
                          data-dimension-id={dimension.id}
                          aria-label={`${offer.company_name}-${dimension.label}`}
                          value={draftValues[key] ?? ''}
                          placeholder="尚未填写"
                          onChange={(event) => setDraftValues((current) => ({ ...current, [key]: event.target.value }))}
                        />
                        <div className={styles.valueActions}>
                          <Button size="small" data-action="save-value" data-offer-id={offer.id} onClick={() => void saveValue(offer.id, dimension.id)}>保存</Button>
                          <Tooltip title="恢复为尚未填写，不会写入‘未知’等伪事实"><Button size="small" data-action="clear-value" data-offer-id={offer.id} onClick={() => void clearValue(offer.id, dimension.id)}>清除值</Button></Tooltip>
                        </div>
                      </>
                    ) : (
                      <span data-testid="archived-dimension-value" className={styles.archivedValue}>{draftValues[key] || '尚未填写'}{active ? ' · 历史未绑定，仅可查看' : ''}</span>
                    )}
                  </div>
                );
              })}
              {active && <Button type="link" danger data-action="archive-dimension" data-dimension-id={dimension.id} onClick={() => void archiveDimension(dimension.id)}>归档维度</Button>}
            </div>
          </Card>
        );
      })}
    </section>
  );
}
