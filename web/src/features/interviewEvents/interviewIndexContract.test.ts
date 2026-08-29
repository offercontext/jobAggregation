import { describe, expect, it } from 'vitest';

import {
  normalizeInterviewIndexItem,
  validateInterviewIndexSource,
  type NormalizedInterviewIndexItem,
} from './interviewIndexContract';

const validRaw = (overrides: Record<string, unknown> = {}) => ({
  application_id: 7,
  event_id: 9,
  company_name: 'Acme',
  position_name: 'Engineer',
  scheduled_at: '2026-08-29T10:00:00Z',
  scheduled_at_state: 'present',
  event_status: 'todo',
  duration_minutes: 60,
  note_id: null,
  note_source_status: null,
  has_review_proposal: false,
  review_summary: null,
  has_confirmed_knowledge: false,
  preparation_available: true,
  ...overrides,
});

describe('normalizeInterviewIndexItem', () => {
  it('normalizes the complete trusted read contract without changing the input', () => {
    const input = validRaw();
    const normalized = normalizeInterviewIndexItem(input);

    expect(normalized).toMatchObject({
      application_id: 7,
      event_id: 9,
      event_status: 'todo',
      duration_minutes: 60,
      scheduled_at_state: 'present',
      contractReasons: [],
    });
    expect(input).toEqual(validRaw());
  });

  it('fails closed when schedule state is missing', () => {
    const input = validRaw();
    const { scheduled_at_state: _scheduledAtState, ...withoutState } = input;
    expect(normalizeInterviewIndexItem(withoutState).contractReasons).toContain('contract_field_missing');
  });

  it.each([
    [{ scheduled_at_state: undefined }, 'contract_field_invalid'],
    [{ scheduled_at_state: null }, 'contract_field_invalid'],
    [{ scheduled_at_state: 'future' }, 'contract_field_invalid'],
    [{ scheduled_at_state: 1 }, 'contract_field_invalid'],
  ] as const)('fails closed for schedule state %s', (patch, reason) => {
    const normalized = normalizeInterviewIndexItem(validRaw(patch));
    expect(normalized.contractReasons).toContain(reason);
  });

  it('treats absent state as authoritative and ignores legacy sentinel schedules', () => {
    const normalized = normalizeInterviewIndexItem(validRaw({
      scheduled_at: '0001-01-01T00:00:00',
      scheduled_at_state: 'absent',
    }));
    expect(normalized).toMatchObject({ scheduled_at_state: 'absent', scheduleTimestamp: null });
    expect(normalized.contractReasons).toContain('schedule_absent');
    expect(normalized.contractReasons).not.toContain('schedule_invalid');
  });

  it.each(['done', 'cancelled'] as const)('records schedule_absent for terminal status %s as raw contract truth', (event_status) => {
    const normalized = normalizeInterviewIndexItem(validRaw({
      event_status,
      scheduled_at: '0001-01-01T00:00:00',
      scheduled_at_state: 'absent',
    }));
    expect(normalized.contractReasons).toContain('schedule_absent');
  });

  it.each([
    '',
    '0001-01-01T00:00:00Z',
    '2026-02-30T10:00:00Z',
    '2026-08-29T10:00:60Z',
    '2026-08-29 10:00:00Z',
    'not-a-date',
  ])('rejects an invalid present schedule: %s', (scheduled_at) => {
    const normalized = normalizeInterviewIndexItem(validRaw({ scheduled_at }));
    expect(normalized.contractReasons).toContain('schedule_invalid');
    expect(normalized.scheduleTimestamp).toBeNull();
  });

  it('accepts RFC3339 schedules with offsets and fractional seconds', () => {
    const normalized = normalizeInterviewIndexItem(validRaw({ scheduled_at: '2026-08-29T18:00:00.125+08:00' }));
    expect(normalized.contractReasons).not.toContain('schedule_invalid');
    expect(normalized.scheduleTimestamp).toBe(Date.parse('2026-08-29T18:00:00.125+08:00'));
  });

  it.each([
    undefined,
    null,
    true,
    1.5,
    0,
    -1,
    10081,
  ])('rejects duration %s', (duration_minutes) => {
    const normalized = normalizeInterviewIndexItem(validRaw({ duration_minutes }));
    expect(normalized.contractReasons).toContain('duration_invalid');
  });

  it.each([1, 10080])('accepts duration boundary %s', (duration_minutes) => {
    expect(normalizeInterviewIndexItem(validRaw({ duration_minutes })).contractReasons).not.toContain('duration_invalid');
  });

  it.each([undefined, null, '', 'future', 'DONE'])('marks unknown status %s unavailable', (event_status) => {
    const normalized = normalizeInterviewIndexItem(validRaw({ event_status }));
    expect(normalized.contractReasons).toContain('status_unknown');
  });

  it('reports source identity and status mismatches through pure preflight', () => {
    const item = validRaw();
    expect(validateInterviewIndexSource(item, { id: 9, application_id: 7, status: 'todo' })).toEqual({ ok: true });
    expect(validateInterviewIndexSource(item, { id: 10, application_id: 7, status: 'todo' })).toEqual({ ok: false, reason: 'source_mismatch' });
    expect(validateInterviewIndexSource(item, { id: 9, application_id: 8, status: 'todo' })).toEqual({ ok: false, reason: 'source_mismatch' });
    expect(validateInterviewIndexSource(item, { id: 9, application_id: 7, status: 'done' })).toEqual({ ok: false, reason: 'source_mismatch' });
    expect(normalizeInterviewIndexItem(item, { id: 9, application_id: 7, status: 'done' }).contractReasons).toContain('source_mismatch');
  });

  it('does not hide conflicting or invalid canonical source ids behind the event_id alias', () => {
    expect(validateInterviewIndexSource(validRaw(), { id: 9, event_id: 10, application_id: 7, status: 'todo' })).toEqual({ ok: false, reason: 'source_mismatch' });
    expect(validateInterviewIndexSource(validRaw(), { id: 'bad', event_id: 9, application_id: 7, status: 'todo' })).toEqual({ ok: false, reason: 'source_mismatch' });
    const hostile = Object.defineProperty({ event_id: 9, application_id: 7, status: 'todo' }, 'id', {
      get: () => { throw new Error('id unavailable'); },
    });
    expect(validateInterviewIndexSource(validRaw(), hostile)).toEqual({ ok: false, reason: 'source_mismatch' });
  });

  it('reads relevant getters once and never throws for hostile input', () => {
    const reads = new Map<string, number>();
    const input = new Proxy(validRaw(), {
      get(target, property, receiver) {
        const key = String(property);
        reads.set(key, (reads.get(key) ?? 0) + 1);
        return Reflect.get(target, property, receiver);
      },
    });
    expect(() => normalizeInterviewIndexItem(input)).not.toThrow();
    for (const key of ['application_id', 'event_id', 'event_status', 'duration_minutes', 'scheduled_at_state', 'scheduled_at']) {
      expect(reads.get(key)).toBe(1);
    }

    const { proxy, revoke } = Proxy.revocable(validRaw(), {});
    revoke();
    expect(() => normalizeInterviewIndexItem(proxy)).not.toThrow();
    expect(normalizeInterviewIndexItem(proxy).contractReasons.length).toBeGreaterThan(0);
  });

  it('keeps normalized data safe when ids and legacy values are malformed', () => {
    const normalized: NormalizedInterviewIndexItem = normalizeInterviewIndexItem(validRaw({
      application_id: '7', event_id: Number.POSITIVE_INFINITY, note_id: false,
      has_review_proposal: 'yes', has_confirmed_knowledge: 1, preparation_available: null,
      review_summary: 1, note_source_status: 'invalid', company_name: null,
    }));
    expect(normalized.application_id).toBeNull();
    expect(normalized.event_id).toBeNull();
    expect(normalized.note_id).toBeNull();
    expect(normalized.has_review_proposal).toBe(false);
    expect(normalized.has_confirmed_knowledge).toBe(false);
    expect(normalized.preparation_available).toBe(false);
    expect(normalized.review_summary).toBeNull();
  });
});
