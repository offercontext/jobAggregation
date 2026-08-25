import dayjs, { type ConfigType } from 'dayjs';

interface TodayAction {
  id: string;
}

interface TodayEvent {
  id: number;
  scheduled_at: string;
}

interface CompletedApplication {
  id: number;
  company_name: string;
  position_name: string;
  applied_at?: string;
}

interface CompletedOffer {
  id: number;
  company_name: string;
  position_name: string;
  created_at: string;
}

export function deriveWeeklyCompletedHighlight({
  applications,
  offers,
  now,
}: {
  applications: readonly CompletedApplication[];
  offers: readonly CompletedOffer[];
  now: ConfigType;
}) {
  const current = dayjs(now);
  const weekStart = current.startOf('week');
  const inCurrentWeek = (value?: string) => {
    const timestamp = dayjs(value);
    return timestamp.isValid()
      && (timestamp.isAfter(weekStart) || timestamp.isSame(weekStart))
      && !timestamp.isAfter(current);
  };
  const latestOffer = offers
    .filter((offer) => inCurrentWeek(offer.created_at))
    .sort((left, right) => dayjs(right.created_at).valueOf() - dayjs(left.created_at).valueOf())[0];
  if (latestOffer) {
    return {
      kind: 'offer' as const,
      id: latestOffer.id,
      title: `本周已完成：收到${latestOffer.company_name} Offer`,
      detail: `${latestOffer.position_name} · 已记录 Offer`,
    };
  }

  const latestApplication = applications
    .filter((application) => inCurrentWeek(application.applied_at))
    .sort((left, right) => dayjs(right.applied_at).valueOf() - dayjs(left.applied_at).valueOf())[0];
  return latestApplication ? {
    kind: 'application' as const,
    id: latestApplication.id,
    title: `本周已完成：投递${latestApplication.company_name}`,
    detail: latestApplication.position_name,
  } : null;
}

export function deriveTodayWorkspace<TAction extends TodayAction, TEvent extends TodayEvent>({
  actions,
  events,
  now,
}: {
  actions: readonly TAction[];
  events: readonly TEvent[];
  now: ConfigType;
}) {
  const current = dayjs(now);
  const windowEnd = current.add(7, 'day');
  const upcomingEvents = events
    .filter((event) => {
      const scheduled = dayjs(event.scheduled_at);
      return scheduled.isValid() && scheduled.isAfter(current) && !scheduled.isAfter(windowEnd);
    })
    .sort((left, right) => dayjs(left.scheduled_at).valueOf() - dayjs(right.scheduled_at).valueOf());

  return {
    primaryAction: actions[0] ?? null,
    otherActions: actions.slice(1, 4),
    upcomingEvents,
  };
}
