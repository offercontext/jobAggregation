import { describe, expect, it } from 'vitest';
import { deriveTodayWorkspace } from './todayWorkspace';

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
});
