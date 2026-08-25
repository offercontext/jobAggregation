import { describe, expect, it } from 'vitest';
import { deriveTodayWorkspace, deriveWeeklyCompletedHighlight } from './todayWorkspace';

const actions = Array.from({ length: 6 }, (_, index) => ({ id: `a-${index}`, title: `行动 ${index}` }));

describe('today workspace model', () => {
  it('chooses exactly one stable primary action and at most three secondary actions', () => {
    const first = deriveTodayWorkspace({ actions, events: [], now: '2026-08-21T09:00:00+08:00' });
    const second = deriveTodayWorkspace({ actions, events: [], now: '2026-08-21T09:00:00+08:00' });
    expect(first.primaryAction?.id).toBe('a-0');
    expect(first.otherActions.map((item) => item.id)).toEqual(['a-1', 'a-2', 'a-3']);
    expect(second).toEqual(first);
  });

  it('keeps only the next seven days and returns an honest empty primary state', () => {
    const model = deriveTodayWorkspace({
      actions: [],
      now: '2026-08-21T09:00:00+08:00',
      events: [
        { id: 1, scheduled_at: '2026-08-20T09:00:00+08:00' },
        { id: 2, scheduled_at: '2026-08-22T09:00:00+08:00' },
        { id: 3, scheduled_at: '2026-08-30T09:00:00+08:00' },
      ],
    });
    expect(model.primaryAction).toBeNull();
    expect(model.otherActions).toEqual([]);
    expect(model.upcomingEvents.map((item) => item.id)).toEqual([2]);
  });

  it('falls back to the most important completed fact from this week', () => {
    expect(deriveWeeklyCompletedHighlight({
      now: '2026-08-21T09:00:00+08:00',
      applications: [{
        id: 1,
        company_name: '星河科技',
        position_name: '前端工程师',
        applied_at: '2026-08-20T10:00:00+08:00',
      }],
      offers: [{
        id: 2,
        company_name: '远山科技',
        position_name: '产品经理',
        created_at: '2026-08-18T10:00:00+08:00',
      }],
    })).toEqual({
      kind: 'offer',
      id: 2,
      title: '本周已完成：收到远山科技 Offer',
      detail: '产品经理 · 已记录 Offer',
    });
  });
});
