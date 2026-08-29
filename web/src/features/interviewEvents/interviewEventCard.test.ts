import { describe, expect, it } from 'vitest';

import { normalizeInterviewIndexItem } from './interviewIndexContract';
import { compareInterviewEventCards, projectInterviewEventCard, type InterviewEventCardModel } from './interviewEventCard';

const NOW = Date.parse('2026-08-29T10:00:00Z');
const raw = (overrides: Record<string, unknown> = {}) => ({
  application_id: 7, event_id: 9, company_name: 'Acme', position_name: 'Engineer',
  scheduled_at: '2026-08-29T10:30:00Z', scheduled_at_state: 'present', event_status: 'todo', duration_minutes: 60,
  note_id: null, note_source_status: null, has_review_proposal: false, review_summary: null,
  has_confirmed_knowledge: false, preparation_available: true, ...overrides,
});

const project = (overrides: Record<string, unknown> = {}, now = NOW) =>
  projectInterviewEventCard(normalizeInterviewIndexItem(raw(overrides)), now);

describe('projectInterviewEventCard', () => {
  it('projects future active events to upcoming with preparation actions', () => {
    expect(project()).toMatchObject({ lifecycle: 'scheduled', bucket: 'upcoming', primaryAction: 'prepare', secondaryAction: 'view_application' });
    expect(project({ event_status: 'in_progress' })).toMatchObject({ lifecycle: 'in_progress', bucket: 'upcoming', primaryAction: 'enter_preparation' });
  });

  it.each([
    ['done', 'completed', 'record_review'], ['completed', 'completed', 'record_review'],
    ['cancelled', 'cancelled', 'none'], ['deleted', 'cancelled', 'none'],
  ] as const)('status %s wins over future time', (event_status, bucket, primaryAction) => {
    expect(project({ event_status })).toMatchObject({ bucket, primaryAction, secondaryAction: 'view_application' });
  });

  it.each(['done', 'cancelled'] as const)('keeps terminal status for invalid now: %s', (event_status) => {
    expect(project({ event_status }, Number.NaN)).toMatchObject({
      bucket: event_status === 'done' ? 'completed' : 'cancelled',
      primaryAction: event_status === 'done' ? 'record_review' : 'none',
    });
    expect(project({ event_status }, Number.POSITIVE_INFINITY)).toMatchObject({
      bucket: event_status === 'done' ? 'completed' : 'cancelled',
    });
    expect(project({ event_status, scheduled_at_state: 'absent' }, Number.NEGATIVE_INFINITY)).toMatchObject({
      bucket: event_status === 'done' ? 'completed' : 'cancelled',
    });
  });

  it('keeps terminal status with an absent schedule while preserving the contract reason', () => {
    const item = normalizeInterviewIndexItem(raw({ event_status: 'done', scheduled_at_state: 'absent' }));
    expect(item.contractReasons).toContain('schedule_absent');
    expect(projectInterviewEventCard(item, NOW)).toMatchObject({ bucket: 'completed', primaryAction: 'record_review' });
  });

  it('chooses review action when a completed event has a note', () => {
    expect(project({ event_status: 'done', note_id: 20 })).toMatchObject({ bucket: 'completed', primaryAction: 'view_review' });
  });

  it('does not treat a stale source identity as a completed card', () => {
    const item = normalizeInterviewIndexItem(raw({ event_status: 'done' }), { id: 10, application_id: 7, status: 'done' });
    expect(projectInterviewEventCard(item, NOW)).toMatchObject({ bucket: 'unavailable', primaryAction: 'none' });
  });

  it.each([
    ['todo', 'needs_status_update', 'update_status'],
    ['in_progress', 'needs_status_update', 'update_status'],
  ] as const)('projects past/current active event %s as status update', (event_status, bucket, primaryAction) => {
    expect(project({ event_status, scheduled_at: '2026-08-29T08:00:00Z' })).toMatchObject({ bucket, primaryAction });
  });

  it('keeps an in-progress event upcoming through the inclusive end boundary', () => {
    expect(project({ event_status: 'in_progress', scheduled_at: '2026-08-29T09:00:00Z', duration_minutes: 60 }, NOW)).toMatchObject({ bucket: 'upcoming' });
    expect(project({ event_status: 'in_progress', scheduled_at: '2026-08-29T09:00:00Z', duration_minutes: 60 }, NOW + 1)).toMatchObject({ bucket: 'needs_status_update' });
  });

  it.each([
    { scheduled_at_state: 'absent' }, { scheduled_at: '', scheduled_at_state: 'present' }, { duration_minutes: 0 },
    { event_status: 'mystery' },
  ])('returns unavailable for contract failures', (overrides) => {
    expect(project(overrides)).toMatchObject({ bucket: 'unavailable', primaryAction: 'none', secondaryAction: 'retry' });
  });

  it('returns unavailable for a non-finite explicit now', () => {
    expect(project({}, Number.NaN)).toMatchObject({ bucket: 'unavailable', primaryAction: 'none' });
    expect(project({}, Number.POSITIVE_INFINITY)).toMatchObject({ bucket: 'unavailable', primaryAction: 'none' });
  });

  it('does not mutate normalized input', () => {
    const item = normalizeInterviewIndexItem(raw());
    const before = JSON.stringify(item);
    expect(projectInterviewEventCard(item, NOW)).toBeDefined();
    expect(JSON.stringify(item)).toBe(before);
  });

  it('freezes the complete projector output and nested secondary actions', () => {
    const card = project();
    expect(Object.isFrozen(card)).toBe(true);
    expect(Object.isFrozen(card.secondaryActions)).toBe(true);
    const before = [...card.secondaryActions];
    try {
      (card.secondaryActions as unknown as string[]).push('retry');
    } catch {
      // Frozen mutation is allowed to throw in strict mode; either way it must not stick.
    }
    expect(card.secondaryActions).toEqual(before);
  });

  it('fails closed when an active row contradicts its preparation hint', () => {
    expect(project({ preparation_available: false })).toMatchObject({ bucket: 'unavailable', primaryAction: 'none' });
    expect(project({ event_status: 'in_progress', preparation_available: false })).toMatchObject({ bucket: 'unavailable', primaryAction: 'none' });
    expect(project({ event_status: 'done', preparation_available: false })).toMatchObject({ bucket: 'completed', primaryAction: 'record_review' });
  });

  it('has a stable comparator for same-time events independent of API order', () => {
    const first = normalizeInterviewIndexItem(raw({ event_id: 2 }));
    const second = normalizeInterviewIndexItem(raw({ event_id: 1 }));
    const comparator = (a: InterviewEventCardModel, b: InterviewEventCardModel) =>
      a.scheduledAtTimestamp - b.scheduledAtTimestamp || a.eventId - b.eventId;
    const orderA = [projectInterviewEventCard(first, NOW), projectInterviewEventCard(second, NOW)].sort(comparator).map((item) => item.eventId);
    const orderB = [projectInterviewEventCard(second, NOW), projectInterviewEventCard(first, NOW)].sort(comparator).map((item) => item.eventId);
    expect(orderA).toEqual([1, 2]);
    expect(orderB).toEqual(orderA);
  });

  it('exports a total comparator with explicit infinity/NaN ordering and safe identity tie breaks', () => {
    const card = (scheduledAtTimestamp: number, eventId: number, applicationId = 1, lifecycle: InterviewEventCardModel['lifecycle'] = 'scheduled') => ({
      ...project(), scheduledAtTimestamp, eventId, applicationId, lifecycle,
    });
    const negativeInfinity = card(Number.NEGATIVE_INFINITY, 99);
    const finite = card(0, 99);
    const positiveInfinity = card(Number.POSITIVE_INFINITY, 99);
    const nan = card(Number.NaN, 99);
    expect([nan, positiveInfinity, finite, negativeInfinity].sort(compareInterviewEventCards).map((item) => item.scheduledAtTimestamp)).toEqual([
      Number.NEGATIVE_INFINITY, 0, Number.POSITIVE_INFINITY, Number.NaN,
    ]);

    const sameTimeHigherEvent = card(0, 2, 1);
    const sameTimeLowerEvent = card(0, 1, 99);
    expect(compareInterviewEventCards(sameTimeLowerEvent, sameTimeHigherEvent)).toBeLessThan(0);
    const sameEventLowerApplication = card(0, 1, 1);
    const sameEventHigherApplication = card(0, 1, 2);
    expect(compareInterviewEventCards(sameEventLowerApplication, sameEventHigherApplication)).toBeLessThan(0);

    const invalidId = card(0, Number.NaN, Number.POSITIVE_INFINITY);
    expect(() => compareInterviewEventCards(invalidId, sameEventLowerApplication)).not.toThrow();
    expect(Number.isNaN(compareInterviewEventCards(invalidId, sameEventLowerApplication))).toBe(false);
  });

  it('keeps the exported comparator transitive for a three-card chain', () => {
    const card = (scheduledAtTimestamp: number, eventId: number) => ({ ...project(), scheduledAtTimestamp, eventId });
    const a = card(Number.NEGATIVE_INFINITY, 100);
    const b = card(0, Number.NaN);
    const c = card(Number.POSITIVE_INFINITY, 1);
    expect(compareInterviewEventCards(a, b)).toBeLessThan(0);
    expect(compareInterviewEventCards(b, c)).toBeLessThan(0);
    expect(compareInterviewEventCards(a, c)).toBeLessThan(0);
  });
});
