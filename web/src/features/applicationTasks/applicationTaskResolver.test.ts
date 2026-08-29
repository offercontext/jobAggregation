import { describe, expect, it } from 'vitest';
import { resolveApplicationTasks, type FrozenApplicationTaskSnapshot } from './applicationTaskResolver';

const NOW = Date.parse('2026-08-29T10:00:00Z');

function snapshot(overrides: Record<string, unknown> = {}): FrozenApplicationTaskSnapshot {
  return Object.freeze({
    application: { id: 7, status: 'interview' },
    jd: { status: 'ready', value: { id: 11 } },
    events: { status: 'ready', value: [] },
    offers: { status: 'ready', value: [] },
    materialKit: { status: 'ready', value: null },
    reviews: { status: 'ready', value: [] },
    pending: { status: 'ready', value: null },
    ...overrides,
  });
}

describe('resolveApplicationTasks', () => {
  it('exports the closed deterministic resolver and freezes its output', () => {
    const result = resolveApplicationTasks(snapshot(), NOW);
    expect(result.primaryTask).toBeNull();
    expect(Object.isFrozen(result)).toBe(true);
    expect(Object.isFrozen(result.tasks)).toBe(true);
  });

  it('gives a current application pending confirmation precedence over events', () => {
    const result = resolveApplicationTasks(snapshot({
      pending: { status: 'ready', value: { applicationId: 7, taskId: 'application.material_kit' } },
      events: { status: 'ready', value: [{ id: 3, application_id: 7, status: 'todo', scheduled_at: '2026-08-29T11:00:00Z', duration_minutes: 60, preparation_available: true }] },
    }), NOW);
    expect(result.primaryTask?.ref).toEqual({ taskId: 'application.material_kit', applicationId: 7 });
    expect(result.primaryTask?.availability).toBe('waiting_confirmation');
    expect(result.tasks.filter((task) => task.primary)).toHaveLength(1);
  });

  it('does not rebind a foreign pending action', () => {
    const result = resolveApplicationTasks(snapshot({
      pending: { status: 'ready', value: { applicationId: 99, taskId: 'application.material_kit' } },
    }), NOW);
    expect(result.primaryTask?.ref).not.toEqual({ taskId: 'application.material_kit', applicationId: 7 });
    expect(result.tasks.some((task) => task.reason === 'pending_confirmation' && task.primary)).toBe(false);
  });

  it('uses lifecycle status rather than the clock for future completed events', () => {
    const result = resolveApplicationTasks(snapshot({
      events: { status: 'ready', value: [{ id: 3, application_id: 7, status: 'done', scheduled_at: '2026-08-30T11:00:00Z', duration_minutes: 60 }] },
    }), NOW);
    expect(result.primaryTask?.ref).toEqual({ taskId: 'application.interview_review', applicationId: 7, eventId: 3 });
  });

  it('is stable under shuffled source arrays and does not mutate input', () => {
    const events = [
      { id: 8, application_id: 7, status: 'todo', scheduled_at: '2026-08-29T12:00:00Z', duration_minutes: 30, preparation_available: true },
      { id: 3, application_id: 7, status: 'todo', scheduled_at: '2026-08-29T12:00:00Z', duration_minutes: 30, preparation_available: true },
    ];
    const first = snapshot({ events: { status: 'ready', value: events } });
    const before = JSON.stringify(first);
    const second = snapshot({ events: { status: 'ready', value: [...events].reverse() } });
    const a = resolveApplicationTasks(first, NOW);
    const b = resolveApplicationTasks(second, NOW);
    expect(a).toEqual(b);
    expect(JSON.stringify(first)).toBe(before);
  });

  it('keeps loading and error sources non-executable', () => {
    const result = resolveApplicationTasks(snapshot({ jd: { status: 'loading' }, materialKit: { status: 'error' } }), NOW);
    expect(result.tasks.some((task) => task.primary)).toBe(false);
    expect(result.tasks.every((task) => task.availability !== 'ready' || !task.executable)).toBe(true);
  });
});
