import { useEffect, useState, type CSSProperties, type ReactNode } from 'react';
import { ArrowLeftOutlined, BankOutlined, CalendarOutlined, EditOutlined, MessageOutlined, SlidersOutlined, WalletOutlined, InfoCircleOutlined } from '@ant-design/icons';
import { Alert, Button, Checkbox, Drawer, Empty, Switch } from 'antd';
import type { Offer, OfferComparisonRead } from '@/types/offer';
import { OFFER_STATUS_LABELS } from '@/types/offer';
import { readOfferComparison } from '@/services/offers';
import { listOfferBindingState } from './offerWorkspaceModel';
import { annualSalaryEstimate, comparisonInsights, formatCash, formatWan, isSameKnownRow, numberFact, textFact, type ComparisonFact } from './offerComparisonModel';
import styles from './OfferCompareDrawer.module.css';

interface Props {
  open: boolean; onClose: () => void; offers: Offer[]; dimensionIds?: number[];
  onCoach?: (offer: Offer) => void; onNegotiation?: (offer: Offer) => void;
  onEdit?: (offer: Offer) => void; onAdjustOffers?: () => void;
  dimensionSettings?: ReactNode; refreshKey?: number;
}
interface FactRow { key: string; label: string; values: ComparisonFact[] }
const NO_DIMENSIONS: number[] = [];
const FIELDS = [['monthly', '月薪与计薪月数'], ['annual', '年薪估算'], ['signing', '签字费'], ['first-year', '首年现金估算'], ['equity', '期权'], ['perks', '福利'], ['deadline', '回复截止']];

export default function OfferCompareDrawer({ open, onClose, offers, dimensionIds = NO_DIMENSIONS, onCoach, onNegotiation, onEdit, onAdjustOffers, dimensionSettings, refreshKey = 0 }: Props) {
  const [read, setRead] = useState<{ key: string; data?: OfferComparisonRead; error?: boolean } | null>(null);
  const [onlyDifferences, setOnlyDifferences] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [hiddenFields, setHiddenFields] = useState<string[]>([]);
  const [retry, setRetry] = useState(0);
  const requestKey = JSON.stringify({ offers, dimensionIds, refreshKey, retry });
  useEffect(() => {
    if (!open || offers.length < 2) return;
    let current = true;
    setRead(null);
    void readOfferComparison(offers.map((offer) => offer.id), dimensionIds)
      .then((data) => { if (current) setRead({ key: requestKey, data }); })
      .catch(() => { if (current) setRead({ key: requestKey, error: true }); });
    return () => { current = false; };
    // Serialized inputs avoid identity loops and invalidate snapshots after edits.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, requestKey]);
  if (!open) return null;
  const currentRead = read?.key === requestKey ? read : null;
  const comparison = currentRead?.data;
  const displayedOffers = offers.map((offer) => comparison?.offers.find((item) => item.id === offer.id) ?? offer);
  const insights = comparisonInsights(displayedOffers);
  const higher = displayedOffers.find((offer) => offer.id === insights.higherSalaryId);
  const earlier = displayedOffers.find((offer) => insights.earliestDeadlineIds.includes(offer.id));
  const row = (key: string, label: string, value: (offer: Offer) => ComparisonFact): FactRow => ({ key, label, values: displayedOffers.map(value) });
  const groups = [
    { label: '薪酬构成', rows: [
      row('monthly', '月薪与计薪月数', (offer) => annualSalaryEstimate(offer) === null ? textFact(null) : textFact(`${offer.base_monthly / 1000}K × ${offer.months_per_year}`)),
      row('annual', '年薪估算', (offer) => numberFact(annualSalaryEstimate(offer), formatCash)),
      row('signing', '签字费（一次性）', (offer) => numberFact(offer.signing_bonus, formatCash)),
      row('first-year', '首年现金估算', (offer) => { const annual = annualSalaryEstimate(offer); return numberFact(annual !== null && Number.isFinite(offer.signing_bonus) && offer.signing_bonus >= 0 ? annual + offer.signing_bonus : null, formatCash); }),
    ] },
    { label: '权益与福利', rows: [row('equity', '期权', (offer) => textFact(offer.equity)), row('perks', '福利', (offer) => textFact(offer.perks))] },
    { label: '回复安排', rows: [row('deadline', '回复截止', (offer) => textFact(offer.deadline))] },
  ];
  const customRows = comparison?.dimensions.map((dimension) => row(`dimension-${dimension.id}`, dimension.label, (offer) => textFact(dimension.values.find((value) => value.offer_id === offer.id)?.value_text))) ?? [];
  const visible = (rows: FactRow[]) => rows.filter((item) => !hiddenFields.includes(item.key) && (!onlyDifferences || !isSameKnownRow(item.values)));
  const hiddenSameCount = [...groups.flatMap((group) => group.rows), ...customRows].filter((item) => !hiddenFields.includes(item.key) && onlyDifferences && isSameKnownRow(item.values)).length;
  const tableHeader = <><colgroup><col style={{ width: 160 }} />{displayedOffers.map((offer) => <col key={offer.id} />)}</colgroup><thead><tr><th scope="col">对比项</th>{displayedOffers.map((offer) => <th key={offer.id} scope="col"><BankOutlined aria-hidden="true" /> {offer.company_name}</th>)}</tr></thead></>;
  const renderRow = (item: FactRow) => <tr key={item.key} data-field={item.key}><th scope="row">{item.label}</th>{item.values.map((fact, index) => <td key={displayedOffers[index].id} className={item.key === 'annual' ? styles.emphasis : undefined}><span className={fact.state === 'missing' ? styles.missing : undefined} data-missing={fact.state === 'missing' ? 'true' : undefined}>{fact.text}</span>{fact.state === 'uncertain' && <span className={styles.deadlineBadge}>待确认</span>}{item.key === 'deadline' && insights.earliestDeadlineIds.includes(displayedOffers[index].id) && <span className={styles.deadlineBadge}>早 {insights.deadlineGap} 天截止{displayedOffers.length > 2 ? '（较最晚）' : ''}</span>}</td>)}</tr>;
  const tableStyle = { minWidth: 160 + displayedOffers.length * 260 };
  return <section className={styles.workspace} aria-label="Offer 横向对比" data-selected-dimension-ids={dimensionIds.join(',')}>
    <Button className={styles.back} type="text" icon={<ArrowLeftOutlined />} onClick={onClose}>返回 Offer 中心</Button>
    <header className={styles.header}><div><h2>Offer 横向对比</h2><p>正在比较 {displayedOffers.length} 份 Offer，看清差异，再决定下一步。</p></div><div className={styles.toolbar}><label className={styles.toggle}><Switch size="small" checked={onlyDifferences} onChange={setOnlyDifferences} aria-label="只看差异" />只看差异</label><Button icon={<SlidersOutlined />} onClick={() => setSettingsOpen(true)}>调整对比项</Button>{onAdjustOffers && <Button type="text" onClick={onAdjustOffers}>调整对比对象</Button>}</div></header>
    {displayedOffers.length < 2 ? <Empty description="请选择至少两份 Offer 进行比较" /> : <>
      {currentRead?.error && <Alert type="warning" showIcon message="对比详情暂时无法更新，当前展示已加载的 Offer 信息；自定义项暂不可用。" action={<Button onClick={() => setRetry((value) => value + 1)}>重试</Button>} />}
      <div className={styles.insights} aria-label="关键差异摘要">
        <div><WalletOutlined aria-hidden="true" /><section><small>年薪估算{displayedOffers.length > 2 ? '最大差额' : '差额'}</small><strong>{insights.salaryGap === null ? '暂无法比较' : insights.salaryGap === 0 ? '当前金额相同' : formatCash(insights.salaryGap)}</strong><p>{higher ? `${higher.company_name}更高` : '仅比较月薪 × 计薪月数，不含签字费'}</p></section></div>
        <div><CalendarOutlined aria-hidden="true" /><section><small>回复时间差</small><strong>{insights.deadlineGap === null ? '暂无法比较' : insights.deadlineGap === 0 ? '同一天截止' : `${insights.deadlineGap} 天`}</strong><p>{earlier ? `${earlier.deadline} · ${insights.earliestDeadlineIds.length > 1 ? `${insights.earliestDeadlineIds.length} 份 Offer` : earlier.company_name}更早` : '按完整的 YYYY-MM-DD 回复日期计算'}</p></section></div>
        <div><InfoCircleOutlined aria-hidden="true" /><section><small>基础信息待补充</small><strong>{insights.missingCount ? `${insights.missingCount} 项未补充` : '已填写完整'}</strong><p>{insights.missingLabels.join('、') || '已录入不代表条款已核实'}{insights.uncertainCount ? ` · ${insights.uncertainCount} 项待确认` : ''}</p></section></div>
      </div>
      <div className={styles.summaries} style={{ '--offer-count': displayedOffers.length } as CSSProperties}>{displayedOffers.map((offer) => {
        const annual = annualSalaryEstimate(offer); const bound = listOfferBindingState(offer) === 'bound';
        return <article className={styles.summary} key={offer.id} aria-label={`${offer.company_name} Offer 摘要`} data-testid={`offer-comparison-header-${offer.id}`}>
          <div className={styles.company}><span className={styles.companyIcon}><BankOutlined aria-hidden="true" /></span><div><h3>{offer.company_name}</h3><p>{offer.position_name || '岗位未补充'}</p></div><span className={styles.status}>{OFFER_STATUS_LABELS[offer.status]}</span></div>
          <div className={styles.salaryLine}><div><small>年薪估算</small><div className={styles.salary}>{annual === null ? '未补充' : <><strong>{formatWan(annual)}</strong><span>万 / 年</span></>}</div></div>{higher?.id === offer.id && insights.salaryGap !== null && <span className={styles.salaryBadge}>↑ 高 {formatCash(insights.salaryGap)} / 年</span>}</div>
          <p className={styles.calculation}>{annual === null ? '补充月薪与计薪月数后计算' : `${offer.base_monthly / 1000}K × ${offer.months_per_year} · 按已填月薪与计薪月数计算`}</p>
          <div className={styles.deadline}><CalendarOutlined aria-hidden="true" /><span>回复截止</span><strong>{offer.deadline || '未补充'}</strong></div>
          <div className={styles.actions}>{bound && (onNegotiation || onCoach) && <Button type="primary" icon={<MessageOutlined />} data-action="start-negotiation" data-offer-id={offer.id} onClick={() => (onNegotiation ?? onCoach)?.(offer)}>准备谈薪</Button>}{onEdit && <Button icon={<EditOutlined />} data-action="edit-offer" data-offer-id={offer.id} onClick={() => onEdit(offer)}>{bound ? '编辑信息' : '查看信息'}</Button>}{!bound && <small>历史未绑定记录，仅可查看</small>}</div>
        </article>;
      })}</div>
      <div data-section="fixed-facts"><div className={styles.tableHeading}><h3>逐项对比</h3><small role="status">{!currentRead ? '正在更新对比详情…' : onlyDifferences ? `已隐藏 ${hiddenSameCount} 项相同内容，未补充和待确认项仍保留` : '只呈现已录入的信息，不替你判断优劣'}</small></div><div className={styles.tableScroll} tabIndex={0} role="region" aria-label="Offer 对比明细，可横向滚动"><table style={tableStyle}>{tableHeader}{groups.map((group) => visible(group.rows).length ? <tbody key={group.label}><tr className={styles.group}><th colSpan={displayedOffers.length + 1} scope="rowgroup">{group.label}</th></tr>{visible(group.rows).map(renderRow)}</tbody> : null)}{groups.every((group) => !visible(group.rows).length) && <tbody><tr><td colSpan={displayedOffers.length + 1}>当前没有可展示的明细。可关闭“只看差异”或调整对比项。</td></tr></tbody>}</table></div><p className={styles.formula}>年薪估算 = 月薪 × 计薪月数；首年现金估算另加一次性签字费。均按已填口径计算，不代表保底承诺，不含期权估值或未录入奖金。</p></div>
      {customRows.length > 0 && <section className={styles.custom} data-section="custom-dimensions"><h3>自定义对比项</h3><div className={styles.tableScroll} tabIndex={0} role="region" aria-label="自定义对比项，可横向滚动"><table style={tableStyle}>{tableHeader}<tbody>{visible(customRows).map(renderRow)}{!visible(customRows).length && <tr><td colSpan={displayedOffers.length + 1}>已填写的自定义项均相同</td></tr>}</tbody></table></div></section>}
    </>}
    <Drawer title="调整对比项" open={settingsOpen} onClose={() => setSettingsOpen(false)} width={520} forceRender styles={{ wrapper: { maxWidth: '100vw' } }}><div className={styles.settings}><h3>显示哪些明细</h3><p>只影响本页展示，不修改 Offer 信息。</p><div className={styles.fields}>{FIELDS.map(([key, label]) => <Checkbox key={key} checked={!hiddenFields.includes(key)} onChange={(event) => setHiddenFields((current) => event.target.checked ? current.filter((item) => item !== key) : [...current, key])}>{label}</Checkbox>)}</div><Button type="link" onClick={() => setHiddenFields([])}>恢复全部明细</Button>{dimensionSettings}</div></Drawer>
  </section>;
}
