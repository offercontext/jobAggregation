import { describe, expect, it } from 'vitest';
import { resolveApplicationTasks, type FrozenApplicationTaskSnapshot, type TaskSource } from './applicationTaskResolver';

const NOW = Date.parse('2026-08-29T10:00:00Z');
const ready = <T>(value: T): TaskSource<T> => ({ status: 'ready', value });

function base(overrides: Partial<FrozenApplicationTaskSnapshot> = {}): FrozenApplicationTaskSnapshot {
  return {
    application: ready({ id: 7, status: 'interview' }),
    jd: ready({ id: 11 }),
    events: ready([]),
    offers: ready([]),
    materialKit: ready(null),
    reviews: ready([]),
    fit: ready(null),
    resume: ready(null),
    pending: ready(null),
    resultUnknown: ready(null),
    ...overrides,
  };
}

function event(overrides: Record<string, unknown> = {}) {
  return {
    applicationId: 7,
    eventId: 3,
    lifecycle: 'scheduled' as const,
    bucket: 'upcoming' as const,
    primaryAction: 'prepare' as const,
    scheduledAtTimestamp: NOW + 60 * 60_000,
    durationMinutes: 60,
    sourceMismatch: false,
    deleted: false,
    ...overrides,
  };
}

describe('resolveApplicationTasks', () => {
  it.each([
    ['pending', 'application.opportunity_fit'],
    ['applied', 'application.material_kit'],
    ['written_test', 'application.material_kit'],
    ['offer', 'application.offer_review'],
    ['closed', 'application.record_outcome'],
  ] as const)('covers %s stage fallback', (status, taskId) => {
    const result = resolveApplicationTasks(base({
      application: ready({ id: 7, status }),
      resume: ready({ id: 12 }),
    }), NOW);
    expect(result.primaryTask?.taskId).toBe(taskId);
  });

  it('does not fabricate an interview ref when no event exists', () => {
    const result = resolveApplicationTasks(base({ application: ready({ id: 7, status: 'interview' }) }), NOW);
    expect(result.tasks.some((task) => task.taskId === 'application.interview_prepare' && task.ref?.eventId === undefined)).toBe(false);
  });

  it.each(['loading', 'error', 'absent'] as const)('keeps a non-ready JD from becoming executable (%s)', (status) => {
    const result = resolveApplicationTasks(base({
      application: ready({ id: 7, status: 'pending' }),
      jd: status === 'loading' ? { status } : status === 'error' ? { status, reason: 'read_failed' } : { status },
      resume: ready({ id: 12 }),
    }), NOW);
    expect(result.primaryTask).toBeNull();
    expect(result.tasks.some((task) => task.taskId === 'application.opportunity_fit')).toBe(true);
  });

  it('exports a closed resolver and freezes nested output', () => {
    const result = resolveApplicationTasks(base(), NOW);
    expect(result.primaryTask).toBeNull();
    expect(Object.isFrozen(result)).toBe(true);
    expect(Object.isFrozen(result.tasks)).toBe(true);
  });

  it('gives a scoped pending confirmation precedence over an event', () => {
    const result = resolveApplicationTasks(base({
      pending: ready({ ref: { taskId: 'application.material_kit', applicationId: 7 } }),
      events: ready([event()]),
    }), NOW);
    expect(result.primaryTask?.ref).toEqual({ taskId: 'application.material_kit', applicationId: 7 });
    expect(result.primaryTask?.availability).toBe('waiting_confirmation');
  });

  it('rejects foreign and malformed pending identities without rebinding', () => {
    const foreign = resolveApplicationTasks(base({ pending: ready({ ref: { taskId: 'application.material_kit', applicationId: 99 } }) }), NOW);
    expect(foreign.tasks.some((task) => task.reason === 'pending_confirmation')).toBe(false);
    const malformed = resolveApplicationTasks(base({ pending: ready({ ref: { taskId: 'application.foo', applicationId: 7 } }) }), NOW);
    expect(malformed.primaryTask).toBeNull();
    expect(malformed.hasUnavailable).toBe(true);
  });

  it('uses lifecycle and card projection for prepare/review, not clock inference', () => {
    const completed = resolveApplicationTasks(base({
      events: ready([event({ lifecycle: 'completed', bucket: 'completed', primaryAction: 'view_review', scheduledAtTimestamp: NOW + 7 * 24 * 60 * 60_000 })]),
    }), NOW);
    expect(completed.primaryTask?.ref).toEqual({ taskId: 'application.interview_review', applicationId: 7, eventId: 3 });
    const pastTodo = resolveApplicationTasks(base({
      events: ready([event({ scheduledAtTimestamp: NOW - 60 * 60_000, bucket: 'needs_status_update' })]),
    }), NOW);
    expect(pastTodo.tasks.some((task) => task.taskId === 'application.interview_review')).toBe(false);
  });

  it('does not treat loading/error/absent sources as empty known data', () => {
    const loading = resolveApplicationTasks(base({ application: { status: 'loading' }, events: { status: 'loading' } }), NOW);
    expect(loading.primaryTask).toBeNull();
    expect(loading.hasLoading).toBe(true);
    const error = resolveApplicationTasks(base({ application: { status: 'error' }, events: { status: 'error' } }), NOW);
    expect(error.primaryTask).toBeNull();
    expect(error.hasUnavailable).toBe(true);
  });

  it('keeps unfinished material kits and unresolved offers ahead of stage fallbacks', () => {
    const material = resolveApplicationTasks(base({
      application: ready({ id: 7, status: 'applied' }),
      materialKit: ready({ applicationId: 7, status: 'ready' }),
    }), NOW);
    expect(material.primaryTask?.reason).toBe('material_kit_incomplete');
    const offer = resolveApplicationTasks(base({
      application: ready({ id: 7, status: 'offer' }),
      offers: ready([{ id: 9, applicationId: 7, status: 'negotiating', deadline: '2026-09-01T00:00:00Z' }]),
    }), NOW);
    expect(offer.primaryTask?.ref).toEqual({ taskId: 'application.offer_review', applicationId: 7 });
    expect(offer.primaryTask?.ref).not.toHaveProperty('offerId');
  });

  it('keeps submitted material read-only and rejects foreign offers', () => {
    const submitted = resolveApplicationTasks(base({
      application: ready({ id: 7, status: 'applied' }),
      materialKit: ready({ applicationId: 7, status: 'submitted' }),
      resume: ready({ id: 12 }),
    }), NOW);
    expect(submitted.tasks.some((task) => task.taskId === 'application.material_kit')).toBe(false);
    const foreign = resolveApplicationTasks(base({
      application: ready({ id: 7, status: 'offer' }),
      offers: ready([{ id: 4, applicationId: 99, status: 'pending' }]),
    }), NOW);
    expect(foreign.primaryTask).toBeNull();
    expect(foreign.tasks.find((task) => task.taskId === 'application.offer_review')?.availability).toBe('unavailable');
  });

  it('is stable under shuffled inputs, deduplicates refs, and leaves input unchanged', () => {
    const events = [event({ eventId: 8, scheduledAtTimestamp: NOW + 2 * 60 * 60_000 }), event({ eventId: 3 })];
    const input = base({ events: ready(events) });
    const before = JSON.stringify(input);
    const a = resolveApplicationTasks(input, NOW);
    const b = resolveApplicationTasks(base({ events: ready([...events].reverse()) }), NOW);
    expect(a).toEqual(b);
    expect(a.tasks.filter((task) => task.primary)).toHaveLength(1);
    expect(JSON.stringify(input)).toBe(before);
  });
});
