import dayjs from 'dayjs';
import { Button } from 'antd';
import type { ScheduleEvent } from '@/types/event';
import { EVENT_TYPE_LABELS } from '@/types/event';
import styles from '../dashboard.module.css';

export default function UpcomingSchedule({
  events,
  onOpenCalendar,
}: {
  events: ScheduleEvent[];
  onOpenCalendar?: () => void;
}) {
  const upcoming = events
    .filter((e) => e.scheduled_at && dayjs(e.scheduled_at).isAfter(dayjs()))
    .sort((a, b) => dayjs(a.scheduled_at).valueOf() - dayjs(b.scheduled_at).valueOf())
    .slice(0, 5);

  return (
    <div className={styles.card}>
      <div className={styles.sectionHeaderLine}>
        <div className={styles.cardTitle}>近期日程</div>
        {onOpenCalendar ? <Button type="link" onClick={onOpenCalendar}>打开日历</Button> : null}
      </div>
      {upcoming.length === 0 ? (
        <div className={styles.empty}>暂无安排</div>
      ) : (
        upcoming.map((e) => {
          const d = dayjs(e.scheduled_at);
          return (
            <div key={e.id} className={styles.schedItem}>
              <div className={styles.schedDate}>
                <div className={styles.schedMon}>{d.format('M月')}</div>
                <div className={`${styles.schedDay} op-tnum`}>{d.format('DD')}</div>
              </div>
              <div className={styles.schedText}>
                {e.company_name ?? '安排'} {EVENT_TYPE_LABELS[e.event_type]} · {d.format('HH:mm')}
              </div>
            </div>
          );
        })
      )}
    </div>
  );
}
