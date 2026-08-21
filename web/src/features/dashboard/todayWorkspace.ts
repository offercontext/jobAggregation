import dayjs, { type ConfigType } from 'dayjs';

interface TodayAction {
  id: string;
}

interface TodayEvent {
  id: number;
  scheduled_at: string;
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
