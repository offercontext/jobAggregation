import dayjs from 'dayjs';
import type { CalendarEntry } from '@/types/calendar';
import { eventFocusDate } from '@/lib/pilotEvidenceFocus';

export const CALENDAR_KINDS = { application: '投递', interview: '面试', written_test: '笔试', deadline: '截止', offer: 'Offer', other: '其他' } as const;
export type CalendarKind = keyof typeof CALENDAR_KINDS;
// Keep the existing strict timestamp validation, but the calendar's date
// identity is local throughout: chip, detail, and incoming evidence focus.
export function calendarLocalEventDate(value: string): string | undefined {
  return eventFocusDate(value) ? dayjs(value).format('YYYY-MM-DD') : undefined;
}
export function calendarEntryKey(source: CalendarEntry): string {
  if (source.event_id) return `event:${source.event_id}`;
  if (source.note_id) return `note:${source.note_id}`;
  return `${source.type}:application:${source.app_id}:${source.date}`;
}
export interface PresentedCalendarEntry {
  key: string;
  kind: CalendarKind;
  label: string;
  title: string;
  date: string;
  time: string;
  source: CalendarEntry;
}
export function calendarDays(month: string): string[] {
  const start = dayjs(`${month}-01`);
  const monday = start.subtract((start.day() + 6) % 7, 'day');
  return Array.from({ length: 42 }, (_, i) => monday.add(i, 'day').format('YYYY-MM-DD'));
}
export function calendarKind(entry: CalendarEntry): CalendarKind {
  if (entry.note_id && !entry.event_id) return 'other';
  const type = entry.event_type ?? entry.type;
  if (type === 'applied') return 'application';
  if (type === 'offer_step') return 'offer';
  if (type === 'interview' || type === 'written_test' || type === 'deadline') return type;
  return 'other';
}
export function presentCalendar(entries: CalendarEntry[]): PresentedCalendarEntry[] {
  return entries.map((source, index) => {
    const localDate = source.scheduled_at ? calendarLocalEventDate(source.scheduled_at) : undefined;
    const timestamp = localDate ? dayjs(source.scheduled_at) : null;
    const timed = timestamp?.isValid() ? timestamp : null;
    const kind = calendarKind(source);
    const label = source.note_id && !source.event_id ? '复盘' : CALENDAR_KINDS[kind];
    const title = source.event_type ? source.title.replace(/ · (面试|笔试|Offer(?: 进展)?|截止|自定义)$/, '') : source.title;
    return {
      key: calendarEntryKey(source),
      kind, label, title, source,
      date: localDate ?? source.date,
      time: timed ? timed.format('HH:mm') : '',
      sort: timed ? timed.valueOf() : Number.POSITIVE_INFINITY,
      index,
    };
  }).sort((a, b) => a.date.localeCompare(b.date) || (a.sort === b.sort ? a.index - b.index : a.sort - b.sort));
}
export function visibleEntryCount(cellHeight: number, total: number): number {
  const room = Math.max(0, cellHeight - 28);
  const allFit = Math.floor(room / 23);
  return total <= Math.min(3, allFit) ? total : Math.max(0, Math.min(3, Math.floor((room - 18) / 23)));
}
