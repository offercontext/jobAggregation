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
    scheduledAtState: 'present' as const,
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

  it.each(['loading', 'error', 'absent'] as const)('applied material stage follows JD/resume dependency %s', (status) => {
    const result = resolveApplicationTasks(base({
      application: ready({ id: 7, status: 'applied' }),
      jd: ready({ id: 11 }),
      resume: status === 'loading' ? { status } : status === 'error' ? { status } : { status },
      materialKit: ready(null),
    }), NOW);
    expect(result.primaryTask).toBeNull();
    expect(result.tasks.find((task) => task.taskId === 'application.material_kit')?.availability).toBe(status === 'loading' ? 'loading' : status === 'error' ? 'unavailable' : 'blocked');
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

  it('keeps an upcoming preparation ahead of a completed-event review', () => {
    const result = resolveApplicationTasks(base({
      events: ready([
        event({ eventId: 31, lifecycle: 'completed', bucket: 'completed', primaryAction: 'record_review', scheduledAtTimestamp: NOW - 2 * 60 * 60_000 }),
        event({ eventId: 32, scheduledAtTimestamp: NOW + 60 * 60_000 }),
      ]),
    }), NOW);

    expect(result.primaryTask?.ref).toEqual({
      taskId: 'application.interview_prepare',
      applicationId: 7,
      eventId: 32,
    });
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

  it('accepts a valid result-unknown ref without requiring event reads', () => {
    const result = resolveApplicationTasks(base({
      events: { status: 'loading' },
      resultUnknown: ready({ ref: { taskId: 'application.interview_review', applicationId: 7, eventId: 3 } }),
    }), NOW);
    expect(result.primaryTask?.availability).toBe('result_unknown');
  });

  it.each(['loading', 'error', 'absent'] as const)('does not let result unknown win when Pending is %s', (status) => {
    const result = resolveApplicationTasks(base({
      pending: status === 'loading' ? { status } : status === 'error' ? { status } : { status },
      resultUnknown: ready({ ref: { taskId: 'application.interview_review', applicationId: 7, eventId: 3 } }),
    }), NOW);
    expect(result.primaryTask).toBeNull();
    expect(result.tasks.some((task) => task.reason === 'result_unknown')).toBe(false);
    expect(result.issues.some((issue) => issue.reason === `source_${status === 'absent' ? 'absent' : status}`)).toBe(true);
    expect(status === 'loading' ? result.hasLoading : result.hasUnavailable).toBe(true);
  });

  it('does not let Pending win while result unknown source is unresolved', () => {
    const result = resolveApplicationTasks(base({
      pending: ready({ ref: { taskId: 'application.material_kit', applicationId: 7 } }),
      resultUnknown: { status: 'loading' },
    }), NOW);
    expect(result.primaryTask).toBeNull();
    expect(result.issues.some((issue) => issue.reason === 'source_loading')).toBe(true);
    expect(result.hasLoading).toBe(true);
  });

  it('fails closed when ready Pending and result unknown refs disagree', () => {
    const result = resolveApplicationTasks(base({
      pending: ready({ ref: { taskId: 'application.material_kit', applicationId: 7 } }),
      resultUnknown: ready({ ref: { taskId: 'application.interview_review', applicationId: 7, eventId: 3 } }),
    }), NOW);
    expect(result.primaryTask).toBeNull();
    expect(result.tasks.filter((task) => task.availability === 'waiting_confirmation' || task.availability === 'result_unknown')).toHaveLength(0);
    expect(result.issues.some((issue) => issue.reason === 'pending_identity_invalid' && issue.priority === 1)).toBe(true);
    expect(result.hasUnavailable).toBe(true);
  });

  it('rejects outer Pending identity that conflicts with its nested ref', () => {
    const result = resolveApplicationTasks(base({
      pending: ready({
        ref: { taskId: 'application.material_kit', applicationId: 7 },
        taskId: 'application.offer_review',
        applicationId: 7,
      }),
    }), NOW);
    expect(result.primaryTask).toBeNull();
    expect(result.tasks.some((task) => task.reason === 'pending_confirmation')).toBe(false);
    expect(result.issues.some((issue) => issue.reason === 'pending_identity_invalid')).toBe(true);
    expect(result.hasUnavailable).toBe(true);
  });

  it.each(['loading', 'error', 'absent'] as const)('blocks lower work when Pending is %s', (status) => {
    const result = resolveApplicationTasks(base({
      pending: status === 'loading' ? { status } : status === 'error' ? { status } : { status },
      events: ready([event()]),
    }), NOW);
    expect(result.primaryTask).toBeNull();
    expect(result.issues.some((issue) => issue.reason.startsWith('source_'))).toBe(true);
    expect(result.tasks.find((task) => task.taskId === 'application.interview_prepare')?.executable).toBe(false);
  });

  it('uses an event review only for a safe positive exact event ID', () => {
    const result = resolveApplicationTasks(base({
      events: ready([event({ lifecycle: 'completed', bucket: 'completed', primaryAction: 'view_review' })]),
      reviews: ready([{ applicationId: 7, eventId: 3, reviewId: 20 }]),
    }), NOW);
    expect(result.primaryTask?.reason).toBe('interview_review_available');
    const invalid = resolveApplicationTasks(base({
      events: ready([event({ lifecycle: 'completed', bucket: 'completed', primaryAction: 'view_review' })]),
      reviews: ready([{ applicationId: 7, eventId: 0 } as never, { applicationId: 7, eventId: '3' } as never]),
    }), NOW);
    expect(invalid.issues.filter((issue) => issue.reason === 'event_contract_invalid')).toHaveLength(1);
    expect(invalid.issues.find((issue) => issue.reason === 'event_contract_invalid')?.priority).toBe(3);
  });

  it.each(['loading', 'error', 'absent'] as const)('does not call a completed event reviewed when reviews are %s', (status) => {
    const result = resolveApplicationTasks(base({
      events: ready([event({ lifecycle: 'completed', bucket: 'completed', primaryAction: 'view_review' })]),
      reviews: status === 'loading' ? { status } : status === 'error' ? { status } : { status },
    }), NOW);
    expect(result.primaryTask).toBeNull();
    expect(result.tasks.find((task) => task.taskId === 'application.interview_review')?.reason).toBe(`source_${status === 'absent' ? 'absent' : status}`);
  });

  it('keeps application-level null-event review separate', () => {
    const result = resolveApplicationTasks(base({ reviews: ready([{ applicationId: 7, eventId: null, reviewId: 10 }]) }), NOW);
    expect(result.tasks.some((task) => task.taskId === 'application.general_review')).toBe(true);
    expect(result.tasks.some((task) => task.ref.eventId !== undefined && task.taskId === 'application.general_review')).toBe(false);
  });

  it('requires the canonical upcoming/action contract and 24 hour window', () => {
    const invalid = resolveApplicationTasks(base({ events: ready([
      event({ durationMinutes: 0 }), event({ eventId: 4, scheduledAtTimestamp: Number.NaN }),
      event({ eventId: 5, primaryAction: 'none' }), event({ eventId: 6, bucket: 'unavailable' }),
      event({ eventId: 7, scheduledAtTimestamp: NOW + 25 * 60 * 60_000 }),
    ]) }), NOW);
    expect(invalid.tasks.some((task) => task.primary)).toBe(false);
    expect(invalid.issues.length).toBeGreaterThan(0);
  });

  it('validates active event schedule before classifying needs-status-update', () => {
    const result = resolveApplicationTasks(base({ events: ready([event({
      lifecycle: 'in_progress',
      bucket: 'needs_status_update',
      primaryAction: 'update_status',
      scheduledAtState: 'absent',
    })]) }), NOW);
    expect(result.issues.some((issue) => issue.reason === 'event_contract_invalid')).toBe(true);
    expect(result.issues.some((issue) => issue.reason === 'event_status_needs_update')).toBe(false);
  });

  it('fails closed for lifecycle/card bucket conflicts and deleted cancelled cards', () => {
    const result = resolveApplicationTasks(base({ events: ready([
      event({ lifecycle: 'scheduled', bucket: 'completed' }),
      event({ eventId: 4, lifecycle: 'cancelled', bucket: 'cancelled', deleted: true }),
    ]) }), NOW);
    expect(result.issues.some((issue) => issue.reason === 'event_contract_invalid')).toBe(true);
    expect(result.issues.some((issue) => issue.reason === 'entity_deleted')).toBe(true);
  });

  it('keeps in-progress preparation available at exact end', () => {
    const result = resolveApplicationTasks(base({ events: ready([event({ lifecycle: 'in_progress', scheduledAtTimestamp: NOW - 60 * 60_000 })]) }), NOW);
    expect(result.primaryTask?.taskId).toBe('application.interview_prepare');
  });

  it.each(['draft', 'ready', 'submitted'] as const)('classifies material kit %s explicitly', (status) => {
    const result = resolveApplicationTasks(base({ application: ready({ id: 7, status: 'applied' }), materialKit: ready({ applicationId: 7, status }) }), NOW);
    expect(result.tasks.some((task) => task.taskId === 'application.material_kit')).toBe(status !== 'submitted');
  });

  it('fails closed for deleted or foreign material kits', () => {
    for (const kit of [{ applicationId: 8, status: 'draft' as const }, { applicationId: 7, status: 'draft' as const, deleted: true }]) {
      const result = resolveApplicationTasks(base({ application: ready({ id: 7, status: 'applied' }), materialKit: ready(kit) }), NOW);
      expect(result.tasks.find((task) => task.taskId === 'application.material_kit')?.availability).toBe('unavailable');
    }
  });

  it('uses zero, one, and many offers through the same application owner', () => {
    for (const offers of [[], [{ id: 2, applicationId: 7, status: 'pending' as const }], [{ id: 3, applicationId: 7, status: 'negotiating' as const, deadline: null }, { id: 2, applicationId: 7, status: 'pending' as const, deadline: '2026-09-01T00:00:00Z' }]]) {
      const result = resolveApplicationTasks(base({ application: ready({ id: 7, status: 'offer' }), offers: ready(offers) }), NOW);
      expect(result.tasks.some((task) => task.taskId === 'application.offer_review')).toBe(true);
      expect(result.tasks.find((task) => task.taskId === 'application.offer_review')?.ref).not.toHaveProperty('offerId');
    }
  });

  it('fails the whole offer owner for a mixed foreign/malformed source', () => {
    const result = resolveApplicationTasks(base({ application: ready({ id: 7, status: 'offer' }), offers: ready([
      { id: 2, applicationId: 7, status: 'pending' }, { id: 3, applicationId: 8, status: 'pending' },
    ]) }), NOW);
    expect(result.tasks.find((task) => task.taskId === 'application.offer_review')?.availability).toBe('unavailable');
  });

  it('keeps every returned task and issue deeply immutable', () => {
    const result = resolveApplicationTasks(base({ events: ready([event()]) }), NOW);
    expect(Object.isFrozen(result.issues)).toBe(true);
    expect(Object.isFrozen(result.tasks[0]?.ref)).toBe(true);
  });
});
