import { describe, expect, it } from 'vitest';
import { calendarDays, presentCalendar, calendarKind, visibleEntryCount } from './calendarPresentation';
import type { CalendarEntry } from '@/types/calendar';

const entry = (patch: Partial<CalendarEntry> = {}): CalendarEntry => ({ date: '2026-09-04', type: 'applied', title: '云舟科技', app_id: 1, ...patch });
describe('calendar presentation', () => {
  it('always creates six Monday-start weeks, including leap February', () => {
    for (const month of ['2026-09', '2028-02', '2026-02']) {
      const days = calendarDays(month);
      expect(days).toHaveLength(42);
      expect(new Date(`${days[0]}T12:00:00`).getDay()).toBe(1);
      expect(new Set(days).size).toBe(42);
    }
  });
  it('maps real event kinds and does not turn a review into an upcoming interview', () => {
    expect(calendarKind(entry())).toBe('application');
    expect(calendarKind(entry({ type: 'interview', note_id: 3 }))).toBe('other');
    for (const [event_type, kind] of [['interview', 'interview'], ['written_test', 'written_test'], ['deadline', 'deadline'], ['offer_step', 'offer'], ['custom', 'other']] as const) {
      expect(calendarKind(entry({ event_type }))).toBe(kind);
    }
  });
  it('sorts timed entries before all-day entries, retaining equal-time source order', () => {
    const list = presentCalendar([entry(), entry({ event_id: 2, scheduled_at: '2026-09-04T12:00:00' }), entry({ event_id: 3, scheduled_at: '2026-09-04T08:00:00' }), entry({ event_id: 4, scheduled_at: '2026-09-04T08:00:00' })]);
    expect(list.map((item) => item.source.event_id)).toEqual([3, 4, 2, undefined]);
  });
  it('derives the cell and display time from the same local timestamp', () => {
    for (const value of ['2026-09-04T23:30:00Z', '2026-09-05T07:30:00+08:00', '2026-09-05T07:30:00']) {
      const date = new Date(value);
      const [item] = presentCalendar([entry({ scheduled_at: value })]);
      const expected = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
      expect(item.date).toBe(expected);
      expect(item.time).toBe(`${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`);
    }
  });
  it('keeps overflow visible even in short cells', () => {
    expect(visibleEntryCount(120, 7)).toBe(3);
    expect(visibleEntryCount(94, 7)).toBe(2);
    expect(visibleEntryCount(60, 7)).toBe(0);
  });
  it('uses stable identity when neighbouring records change', () => {
    const target = entry({ event_id: 8 });
    expect(presentCalendar([target])[0].key).toBe(presentCalendar([entry({ event_id: 9 }), target]).find((item) => item.source.event_id === 8)?.key);
  });
  it('does not repeat the backend Offer type suffix in the title', () => {
    expect(presentCalendar([entry({ event_type: 'offer_step', title: '云舟科技 · Offer' })])[0].title).toBe('云舟科技');
  });
});
