import { useEffect, useMemo, useRef, useState, type RefCallback } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { DeleteOutlined, EditOutlined, LeftOutlined, PlusOutlined, RightOutlined, CheckCircleOutlined, EnvironmentOutlined } from '@ant-design/icons';
import { Button, Spin, Empty, Tag, Popconfirm, Tooltip, message, Drawer, Select } from 'antd';
import dayjs from 'dayjs';
import type { Application } from '@/types/application';
import type { CalendarEntry } from '@/types/calendar';
import ScheduleEventForm from '@/components/ScheduleEventForm';
import { deleteEvent, getEvent, updateEvent } from '@/services/events';
import { getCalendar } from '@/services/calendar';
import type { ScheduleEvent } from '@/types/event';
import type { EvidenceTarget } from '@/components/ChatPanel/model';
import { calendarDays, calendarLocalEventDate, calendarEntryKey, presentCalendar, CALENDAR_KINDS, visibleEntryCount, type CalendarKind, type PresentedCalendarEntry } from './calendarPresentation';
import styles from './CalendarView.module.css';

const WEEKDAYS = ['一', '二', '三', '四', '五', '六', '日'];
const EMPTY_ENTRIES: CalendarEntry[] = [];
interface CalendarViewProps {
  onOpenDetail: (app: Application) => void;
  applications: Application[];
  focusEvent?: Extract<EvidenceTarget, { kind: 'event' }>;
  onEvidenceFocusConsumed?: () => void;
  haruHostRef?: RefCallback<HTMLDivElement>;
}

export default function CalendarView({ onOpenDetail, applications, focusEvent, onEvidenceFocusConsumed, haruHostRef }: CalendarViewProps) {
  const queryClient = useQueryClient();
  const [currentMonth, setCurrentMonth] = useState(() => dayjs().startOf('month'));
  const [selectedDate, setSelectedDate] = useState(() => dayjs().format('YYYY-MM-DD'));
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [focusedEventId, setFocusedEventId] = useState<number | null>(null);
  const [kind, setKind] = useState<CalendarKind | 'all'>('all');
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [narrow, setNarrow] = useState(() => typeof window !== 'undefined' && window.innerWidth < 1280);
  const [formOpen, setFormOpen] = useState(false);
  const [editingEvent, setEditingEvent] = useState<ScheduleEvent | null>(null);
  const [cellHeight, setCellHeight] = useState(120);
  const gridRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLElement | null>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const editRequestToken = useRef(0);
  const focusedEvidenceTarget = useRef<typeof focusEvent>();
  const consumedEvidenceTarget = useRef<typeof focusEvent>();
  const monthKey = currentMonth.format('YYYY-MM');
  const { data: rawEntries, isLoading, isError, isFetching, refetch } = useQuery({
    queryKey: ['calendar', monthKey, 'six-weeks-local'],
    queryFn: async () => {
      // The unchanged API partitions by UTC month. Adjacent partitions cover
      // six-week spillover and local-time events across a month boundary.
      const partitions = await Promise.all([-1, 0, 1].map((offset) => getCalendar(currentMonth.add(offset, 'month').format('YYYY-MM'))));
      const seen = new Set<string>();
      return partitions.flatMap((part) => part ?? []).filter((entry) => {
        const key = calendarEntryKey(entry);
        if (seen.has(key)) return false;
        seen.add(key); return true;
      });
    },
  });
  const entries = useMemo(() => presentCalendar(rawEntries ?? EMPTY_ENTRIES), [rawEntries]);
  const grid = useMemo(() => calendarDays(monthKey), [monthKey]);
  const filtered = useMemo(() => entries.filter((entry) => kind === 'all' || entry.kind === kind), [entries, kind]);
  const byDate = useMemo(() => {
    const result = new Map<string, PresentedCalendarEntry[]>();
    for (const entry of filtered) result.set(entry.date, [...(result.get(entry.date) ?? []), entry]);
    return result;
  }, [filtered]);
  const selectedEntries = byDate.get(selectedDate) ?? [];
  const selected = selectedEntries.find((entry) => entry.source.event_id === focusedEventId)
    ?? selectedEntries.find((entry) => entry.key === selectedKey) ?? selectedEntries[0];
  const eventId = selected?.source.event_id;
  const detail = useQuery({
    queryKey: ['events', 'calendar-detail', eventId, selected?.source.app_id],
    enabled: Boolean(eventId) && !isError && !isFetching,
    queryFn: async () => {
      const event = await getEvent(eventId!);
      if (!event || event.id !== eventId || event.application_id !== selected?.source.app_id) throw new Error('日程身份不匹配');
      return event;
    }, retry: false,
  });
  const selectedDetail = eventId && detail.data?.id === eventId && detail.data?.application_id === selected?.source.app_id ? detail.data : undefined;
  const cancelPendingEdit = () => { editRequestToken.current += 1; };
  useEffect(() => {
    const resize = () => setNarrow(window.innerWidth < 1280);
    window.addEventListener('resize', resize);
    return () => { window.removeEventListener('resize', resize); editRequestToken.current += 1; };
  }, []);
  useEffect(() => {
    if (!gridRef.current || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(([entry]) => setCellHeight(entry.contentRect.height / 6));
    observer.observe(gridRef.current); return () => observer.disconnect();
  }, [isLoading, isError]);
  useEffect(() => {
    if (!focusEvent) { focusedEvidenceTarget.current = undefined; consumedEvidenceTarget.current = undefined; return; }
    const date = calendarLocalEventDate(focusEvent.scheduledAt);
    if (!date) { setFocusedEventId(null); message.warning('引用的记录已不存在'); onEvidenceFocusConsumed?.(); return; }
    if (focusedEvidenceTarget.current !== focusEvent) {
      focusedEvidenceTarget.current = focusEvent; consumedEvidenceTarget.current = undefined;
      cancelPendingEdit(); setFormOpen(false); setEditingEvent(null);
      setCurrentMonth(dayjs(date).startOf('month')); setSelectedDate(date); setFocusedEventId(focusEvent.id); setKind('all'); setDrawerOpen(true);
    }
    if (isLoading || isError || isFetching || monthKey !== dayjs(date).format('YYYY-MM') || consumedEvidenceTarget.current === focusEvent) return;
    consumedEvidenceTarget.current = focusEvent;
    if (!entries.some((entry) => entry.source.event_id === focusEvent.id && entry.date === date)) { message.warning('引用的记录已不存在'); setFocusedEventId(null); }
    onEvidenceFocusConsumed?.();
  }, [focusEvent, entries, monthKey, isLoading, isError, isFetching, onEvidenceFocusConsumed]);
  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['calendar'] });
    void queryClient.invalidateQueries({ queryKey: ['events'] });
    void queryClient.invalidateQueries({ queryKey: ['interviews'] });
  };
  const deleteMutation = useMutation({ mutationFn: deleteEvent, onSuccess: () => { cancelPendingEdit(); invalidate(); message.success('日程已删除'); }, onError: () => message.error('删除日程失败') });
  const completeMutation = useMutation({
    mutationFn: async (target: { id: number; appId: number }) => {
      const latest = await getEvent(target.id);
      if (latest.id !== target.id || latest.application_id !== target.appId) throw new Error('日程身份不匹配');
      if (['done', 'completed', 'cancelled', 'deleted', 'soft_deleted'].includes(latest.status)) throw new Error('日程状态已变化');
      return updateEvent(latest.id, { application_id: latest.application_id, event_type: latest.event_type, subtype: latest.subtype, tags: latest.tags, round: latest.round, scheduled_at: latest.scheduled_at, duration_minutes: latest.duration_minutes, location: latest.location, notes: latest.notes, remind_at: latest.remind_at, status: 'done' });
    },
    onSuccess: () => { invalidate(); message.success('日程已标记完成'); }, onError: () => message.error('更新日程失败，请刷新后重试'),
  });
  const editMutation = useMutation({
    mutationFn: async ({ id, appId }: { id: number; appId: number; token: number }) => { const event = await getEvent(id); if (event.id !== id || event.application_id !== appId) throw new Error('日程身份不匹配'); return event; },
    onSuccess: (event, input) => { if (input.token !== editRequestToken.current) return; setEditingEvent(event); setFormOpen(true); },
    onError: (_error, input) => { if (input.token === editRequestToken.current) message.error('获取日程失败'); },
  });
  const selectDate = (date: string, entry?: PresentedCalendarEntry) => {
    cancelPendingEdit(); setSelectedDate(date); setSelectedKey(entry?.key ?? null); setFocusedEventId(null); setDrawerOpen(true);
  };
  const create = () => { cancelPendingEdit(); setEditingEvent(null); setFormOpen(true); };
  const closeDrawer = () => { setDrawerOpen(false); triggerRef.current?.focus({ preventScroll: true }); };
  const text = (entry: PresentedCalendarEntry) => `${entry.time ? `${entry.time} ` : ''}${entry.label} · ${entry.title}${entry.source.subtitle ? ` · ${entry.source.subtitle}` : ''}`;
  const busy = deleteMutation.isPending || completeMutation.isPending || editMutation.isPending;
  const sourceReady = !isError && !isFetching && !detail.isError && !detail.isFetching && selectedDetail;
  const terminal = selectedDetail && ['done', 'completed', 'cancelled', 'deleted', 'soft_deleted'].includes(selectedDetail.status);
  const dateLabel = `${dayjs(selectedDate).format('M 月 D 日')}（周${WEEKDAYS[(dayjs(selectedDate).day() + 6) % 7]}）`;
  const detailContent = (
    <section className={styles.dayDetail} aria-label="日期详情" data-selected-date={selectedDate}>
      <h2 className={styles.detailTitle}>{dateLabel}</h2>
      {isLoading ? <div role="status"><Spin />正在加载日程</div> : isError ? <div role="alert">加载日程失败<Button onClick={() => void refetch()}>重试</Button></div> : !selected ? (
        <div className={styles.emptyDay}><Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当天暂无安排" /><Button icon={<PlusOutlined />} onClick={create}>在这一天新建日程</Button></div>
      ) : (
        <>
          <article data-selected-event={eventId} className={focusedEventId === eventId ? styles.entryItemFocused : undefined}>
            <div className={styles.detailMeta}><span data-kind={selected.kind} className={styles.kindBadge}>{selected.label}</span><span>{selected.time || '全天'}{selected.time && selected.source.duration_minutes ? `–${dayjs(selected.source.scheduled_at).add(selected.source.duration_minutes, 'minute').format('HH:mm')}` : ''}</span></div>
            <h3 className={styles.eventTitle}>{selected.label} · {selected.title}</h3>
            {selected.source.subtitle && <p className={styles.secondary}>{selected.source.subtitle}</p>}
            {(selectedDetail?.location || selected.source.location) && <p className={styles.location}><EnvironmentOutlined />{selectedDetail?.location || selected.source.location}</p>}
            {selectedDetail?.notes && <section className={styles.detailSection}><h4>备注</h4><p>{selectedDetail.notes}</p></section>}
            {selectedDetail?.status && <section className={styles.detailSection}><h4>状态</h4><Tag>{({ todo: '待处理', done: '已完成', completed: '已完成', cancelled: '已取消', in_progress: '进行中' } as Record<string, string>)[selectedDetail.status] ?? '其他状态'}</Tag></section>}
            {detail.isLoading && eventId && <p role="status" className={styles.secondary}>正在加载详情</p>}
            {detail.isError && eventId && <p role="alert">日程详情暂不可用 <Button size="small" onClick={() => void detail.refetch()}>重试详情</Button></p>}
            <div className={styles.detailActions}>
              {applications.some((app) => app.id === selected.source.app_id) && <Button onClick={() => { const app = applications.find((app) => app.id === selected.source.app_id); if (app) onOpenDetail(app); }}>查看岗位</Button>}
              {selected.source.editable && sourceReady && eventId && <>
                <Button icon={<EditOutlined />} disabled={busy} loading={editMutation.isPending} onClick={() => { const token = ++editRequestToken.current; editMutation.mutate({ id: eventId, appId: selected.source.app_id, token }); }}>调整时间</Button>
                {!terminal && <Popconfirm title="将这项日程标记为已完成？" onConfirm={() => completeMutation.mutate({ id: eventId, appId: selected.source.app_id })} okText="标记完成" cancelText="取消"><Button icon={<CheckCircleOutlined />} disabled={busy}>标记完成</Button></Popconfirm>}
                <Popconfirm title="删除日程" description="确定删除这个日程吗？" okText="删除" cancelText="取消" onConfirm={() => deleteMutation.mutate(eventId)}><Button danger icon={<DeleteOutlined />} disabled={busy}>删除日程</Button></Popconfirm>
              </>}
            </div>
          </article>
          <section className={styles.otherEvents}><h4>当天其他安排</h4>{selectedEntries.length === 1 ? <p className={styles.secondary}>暂无其他安排</p> : selectedEntries.filter((entry) => entry.key !== selected.key).map((entry) => <button key={entry.key} className={styles.otherEvent} onClick={() => selectDate(selectedDate, entry)}>{text(entry)}</button>)}</section>
        </>
      )}
    </section>
  );
  return (
    <div className={styles.workspace} data-calendar-workspace>
      <section className={styles.panel} aria-label="月历">
        <div className={styles.toolbar}>
          <div className={styles.monthControls}>
            <Button shape="circle" aria-label="上一个月" icon={<LeftOutlined />} onClick={() => setCurrentMonth((month) => month.subtract(1, 'month'))} />
            <h2 className={styles.monthLabel}>{currentMonth.format('YYYY 年 M 月')}</h2>
            <Button shape="circle" aria-label="下一个月" icon={<RightOutlined />} onClick={() => setCurrentMonth((month) => month.add(1, 'month'))} />
            <Button onClick={() => { setCurrentMonth(dayjs().startOf('month')); selectDate(dayjs().format('YYYY-MM-DD')); }}>今天</Button>
          </div>
          <div className={styles.toolbarActions}>
            <Select aria-label="日程类型" value={kind} onChange={(value: CalendarKind | 'all') => { cancelPendingEdit(); setKind(value); setSelectedKey(null); setFocusedEventId(null); }} options={[{ value: 'all', label: '全部类型' }, ...Object.entries(CALENDAR_KINDS).map(([value, label]) => ({ value, label }))]} />
            <Button type="primary" icon={<PlusOutlined />} onClick={create}>新建日程</Button>
          </div>
        </div>
        <div className={styles.weekHeader}>{WEEKDAYS.map((day) => <div key={day}>{day}</div>)}</div>
        {isLoading ? <div role="status" className={styles.queryState}><Spin />正在加载日程</div> : isError ? <div role="alert" className={styles.queryState}>加载日程失败<Button onClick={() => void refetch()}>重试</Button></div> : (
          <div className={styles.grid} ref={gridRef}>
            {grid.map((date) => {
              const dayEntries = byDate.get(date) ?? [];
              const count = visibleEntryCount(cellHeight, dayEntries.length);
              const today = date === dayjs().format('YYYY-MM-DD');
              return <div key={date} data-calendar-date={date} className={`${styles.cell} ${date.slice(0, 7) !== monthKey ? styles.cellMuted : ''} ${selectedDate === date ? styles.cellSelected : ''}`}>
                <button data-date-select={date} className={styles.dateButton} aria-label={`${date}${today ? ' 今天' : ''}，${dayEntries.length} 项安排`} aria-pressed={selectedDate === date} onClick={(event) => { triggerRef.current = event.currentTarget; selectDate(date); }}><span className={today ? styles.today : ''}>{dayjs(date).date()}</span></button>
                <div className={styles.entries}>{dayEntries.slice(0, count).map((entry) => <Tooltip title={text(entry)} key={entry.key}><button data-calendar-event={entry.source.event_id} data-kind={entry.kind} className={styles.entryChip} onClick={(event) => { triggerRef.current = event.currentTarget; selectDate(date, entry); }}><span>{entry.time} {entry.label}</span><span className={styles.chipTitle}> · {entry.title}{entry.source.subtitle ? ` · ${entry.source.subtitle}` : ''}</span></button></Tooltip>)}
                  {dayEntries.length > count && <button className={styles.moreCount} onClick={(event) => { triggerRef.current = event.currentTarget; selectDate(date); }}>另有 {dayEntries.length - count} 项</button>}
                </div>
              </div>;
            })}
          </div>
        )}
      </section>
      <aside className={styles.rightRail} aria-label="日历侧栏">
        {!narrow && detailContent}
        <div ref={haruHostRef} className={styles.haruHost} data-calendar-haru-host />
      </aside>
      <Drawer title="当天安排" open={narrow && drawerOpen && !formOpen} width={360} onClose={closeDrawer} afterOpenChange={(open) => { if (!open && !formOpen) triggerRef.current?.focus({ preventScroll: true }); }} destroyOnClose>{narrow ? detailContent : null}</Drawer>
      <Drawer title={editingEvent ? '编辑日程' : '新建日程'} open={formOpen} width={620} onClose={() => { cancelPendingEdit(); setFormOpen(false); setEditingEvent(null); }} afterOpenChange={(open) => { if (open) headingRef.current?.focus({ preventScroll: true }); }} destroyOnClose>
        {formOpen && <ScheduleEventForm open applications={applications} event={editingEvent ?? undefined} initialDate={selectedDate} headingRef={headingRef} onClose={() => { cancelPendingEdit(); setFormOpen(false); setEditingEvent(null); }} />}
      </Drawer>
    </div>
  );
}
