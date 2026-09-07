// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import CalendarView from './CalendarView';
import { getCalendar } from '@/services/calendar';
import dayjs from 'dayjs';
import { getEvent } from '@/services/events';
import type { ScheduleEvent } from '@/types/event';

vi.mock('@/services/calendar', () => ({ getCalendar: vi.fn() }));
vi.mock('@/services/events', () => ({ getEvent: vi.fn().mockResolvedValue(null), deleteEvent: vi.fn(), updateEvent: vi.fn() }));
vi.mock('./ScheduleEventForm', () => ({ default: ({ initialDate }: { initialDate?: string }) => <div data-form-date={initialDate}>日期表单</div> }));
(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: Root;
let host: HTMLDivElement;
let client: QueryClient;
beforeEach(() => {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1440 });
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({ matches: false, media: query, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn() }));
  host = document.createElement('div'); document.body.append(host); root = createRoot(host);
  vi.mocked(getCalendar).mockResolvedValue([]);
});
afterEach(() => { act(() => root.unmount()); client.clear(); host.remove(); vi.clearAllMocks(); });
async function mount(focusEvent?: { kind: 'event'; id: number; scheduledAt: string }) {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => { root.render(<QueryClientProvider client={client}><CalendarView applications={[]} onOpenDetail={vi.fn()} focusEvent={focusEvent} /></QueryClientProvider>); });
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
}
it('keeps 42 cells when selecting an empty day and passes that day into creation', async () => {
  await mount();
  const date = dayjs().date(12).format('YYYY-MM-DD');
  expect(host.querySelectorAll('[data-calendar-date]')).toHaveLength(42);
  act(() => host.querySelector<HTMLButtonElement>(`[data-date-select="${date}"]`)?.click());
  expect(host.querySelectorAll('[data-calendar-date]')).toHaveLength(42);
  expect(document.body.textContent).toContain('当天暂无安排');
  const create = Array.from(document.querySelectorAll('button')).find((button) => button.textContent?.includes('在这一天新建日程'));
  expect(create).toBeDefined(); act(() => create?.click());
  expect(document.querySelector('[data-form-date]')?.getAttribute('data-form-date')).toBe(date);
});
it('focuses cross-month evidence on the same local date as its calendar chip', async () => {
  const scheduledAt = '2026-09-30T16:30:00Z';
  vi.mocked(getCalendar).mockResolvedValue([{ date: '2026-09-30', type: 'interview', event_id: 9, title: '云舟科技', app_id: 3, scheduled_at: scheduledAt }]);
  await mount({ kind: 'event', id: 9, scheduledAt });
  expect(host.querySelector('[data-selected-date]')?.getAttribute('data-selected-date')).toBe(dayjs(scheduledAt).format('YYYY-MM-DD'));
  expect(host.querySelector('[data-selected-event="9"]')).not.toBeNull();
});
it('fences a late edit response after the user selects another date', async () => {
  const date = dayjs().date(12).format('YYYY-MM-DD');
  const event = { id: 5, application_id: 3, event_type: 'interview', scheduled_at: `${date}T14:00:00`, status: 'todo' } as ScheduleEvent;
  vi.mocked(getCalendar).mockResolvedValue([{ date, type: 'interview', event_type: 'interview', event_id: 5, title: '云舟科技', app_id: 3, scheduled_at: event.scheduled_at, editable: true }]);
  vi.mocked(getEvent).mockResolvedValue(event);
  await mount();
  await act(async () => host.querySelector<HTMLButtonElement>('[data-calendar-event="5"]')?.click());
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
  let resolveEdit!: (value: ScheduleEvent) => void;
  vi.mocked(getEvent).mockReturnValueOnce(new Promise((resolve) => { resolveEdit = resolve; }));
  const edit = Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('调整时间'));
  expect(edit).toBeDefined(); await act(async () => edit?.click());
  act(() => host.querySelector<HTMLButtonElement>(`[data-date-select="${dayjs().date(13).format('YYYY-MM-DD')}"]`)?.click());
  await act(async () => { resolveEdit(event); await new Promise((resolve) => setTimeout(resolve, 50)); });
  expect(document.querySelector('[data-form-date]')).toBeNull();
  expect(host.querySelector('[data-selected-date]')?.getAttribute('data-selected-date')).toBe(dayjs().date(13).format('YYYY-MM-DD'));
});
it('selects the exact chip while keeping the month visible', async () => {
  const date = dayjs().date(12).format('YYYY-MM-DD');
  vi.mocked(getCalendar).mockResolvedValue([{ date, type: 'interview', event_type: 'interview', event_id: 5, title: '云舟科技 · 面试', app_id: 3, scheduled_at: `${date}T14:00:00`, editable: true }]);
  await mount();
  const chip = host.querySelector<HTMLButtonElement>('[data-calendar-event="5"]');
  expect(chip).not.toBeNull(); act(() => chip?.click());
  expect(host.querySelectorAll('[data-calendar-date]')).toHaveLength(42);
  expect(document.querySelector('[data-selected-event="5"]')).not.toBeNull();
});
