import { describe, expect, it } from 'vitest';

import {
  CORE_TASK_ENTRYPOINT_CATEGORIES,
  CORE_TASK_IDS,
  coreTaskCanonicalKey,
  parseCoreTaskRef,
  type CoreTaskRef,
  type TaskLaunchRequest,
} from './contracts';

const expectedTaskIds = [
  'application.opportunity_fit',
  'application.material_kit',
  'application.interview_prepare',
  'application.interview_review',
  'application.general_review',
  'application.offer_review',
  'application.record_outcome',
  'interview.free_practice',
  'materials.resume',
  'materials.story',
  'materials.reference',
] as const;

const expectedEntrypointCategories = [
  'core_task',
  'navigation_only',
  'record_management',
] as const;

describe('CoreTask contracts', () => {
  it('exposes the closed task and entrypoint sets exactly', () => {
    expect(CORE_TASK_IDS).toEqual(expectedTaskIds);
    expect(new Set(CORE_TASK_IDS).size).toBe(expectedTaskIds.length);
    expect(CORE_TASK_ENTRYPOINT_CATEGORIES).toEqual(expectedEntrypointCategories);
  });

  it.each([
    [
      { taskId: 'application.opportunity_fit', applicationId: 7 },
      'application.opportunity_fit:applicationId=7',
    ],
    [
      { taskId: 'application.material_kit', applicationId: 8 },
      'application.material_kit:applicationId=8',
    ],
    [
      { taskId: 'application.interview_prepare', applicationId: 7, eventId: 9 },
      'application.interview_prepare:applicationId=7:eventId=9',
    ],
    [
      { taskId: 'application.interview_review', applicationId: 7, eventId: 9 },
      'application.interview_review:applicationId=7:eventId=9',
    ],
    [
      { taskId: 'application.general_review', applicationId: 10 },
      'application.general_review:applicationId=10',
    ],
    [
      { taskId: 'application.offer_review', applicationId: 11 },
      'application.offer_review:applicationId=11',
    ],
    [
      { taskId: 'application.record_outcome', applicationId: 12 },
      'application.record_outcome:applicationId=12',
    ],
    [
      { taskId: 'interview.free_practice' },
      'interview.free_practice',
    ],
    [
      { taskId: 'materials.resume', resumeId: 13 },
      'materials.resume:resumeId=13',
    ],
    [
      { taskId: 'materials.story', storyId: 14 },
      'materials.story:storyId=14',
    ],
    [
      { taskId: 'materials.reference', sourceId: 15 },
      'materials.reference:sourceId=15',
    ],
  ] as const)('accepts the exact identity for %s', (input, key) => {
    const parsed = parseCoreTaskRef(input);
    expect(parsed).toEqual({ ok: true, ref: input, key });
    expect(coreTaskCanonicalKey(input)).toBe(key);
  });

  it('does not mutate a frozen valid reference', () => {
    const ref = Object.freeze({
      taskId: 'application.interview_review' as const,
      applicationId: 7,
      eventId: 9,
    });

    expect(() => parseCoreTaskRef(ref)).not.toThrow();
    expect(parseCoreTaskRef(ref)).toEqual({
      ok: true,
      ref,
      key: 'application.interview_review:applicationId=7:eventId=9',
    });
  });

  it.each([
    [{ taskId: 'application.material_kit' }, 'applicationId is required'],
    [{ taskId: 'application.interview_prepare', applicationId: 7 }, 'eventId is required'],
    [{ taskId: 'materials.resume' }, 'resumeId is required'],
    [{ taskId: 'materials.story' }, 'storyId is required'],
    [{ taskId: 'materials.reference' }, 'sourceId is required'],
  ] as const)('rejects a missing required identity: %s (%s)', (input, _description) => {
    expect(parseCoreTaskRef(input)).toEqual({ ok: false, reason: 'invalid_task_identity' });
  });

  it.each([
    { taskId: 'application.material_kit', applicationId: 0 },
    { taskId: 'application.material_kit', applicationId: -1 },
    { taskId: 'application.material_kit', applicationId: 1.5 },
    { taskId: 'application.material_kit', applicationId: Number.NaN },
    { taskId: 'application.material_kit', applicationId: Number.POSITIVE_INFINITY },
    { taskId: 'application.material_kit', applicationId: Number.MAX_SAFE_INTEGER + 1 },
    { taskId: 'application.material_kit', applicationId: '7' },
    { taskId: 'application.material_kit', applicationId: null },
    { taskId: 'application.interview_prepare', applicationId: 7, eventId: 0 },
    { taskId: 'application.interview_prepare', applicationId: 7, eventId: 2.25 },
    { taskId: 'application.interview_prepare', applicationId: 7, eventId: Number.MAX_SAFE_INTEGER + 1 },
    { taskId: 'materials.resume', resumeId: true },
    { taskId: 'materials.story', storyId: 0 },
    { taskId: 'materials.reference', sourceId: 1n },
  ] as const)('rejects an unsafe identity: %s', (input) => {
    expect(parseCoreTaskRef(input)).toEqual({ ok: false, reason: 'invalid_task_identity' });
  });

  it.each([
    { taskId: 'application.opportunity_fit', applicationId: 7, eventId: 9 },
    { taskId: 'application.material_kit', applicationId: 7, resumeId: 2 },
    { taskId: 'application.interview_prepare', applicationId: 7, eventId: 9, offerId: 3 },
    { taskId: 'application.offer_review', applicationId: 7, offerId: 3 },
    { taskId: 'application.general_review', applicationId: 7, foo: 1 },
    { taskId: 'interview.free_practice', applicationId: 7 },
    { taskId: 'materials.resume', resumeId: 7, sourceId: 8 },
    { taskId: 'materials.story', storyId: 7, applicationId: 8 },
    { taskId: 'materials.reference', sourceId: 7, storyId: 8 },
  ] as const)('rejects extra identity fields: %s', (input) => {
    expect(parseCoreTaskRef(input)).toEqual({ ok: false, reason: 'invalid_task_identity' });
  });

  it('rejects unknown tasks and non-object input without throwing', () => {
    const unknownInputs: unknown[] = [
      { taskId: 'application.not_registered', applicationId: 7 },
      { taskId: 42 },
      { taskId: Symbol('unknown') },
      {},
      null,
      undefined,
      42,
      'application.material_kit',
      true,
      [],
      () => undefined,
    ];

    for (const input of unknownInputs) {
      expect(() => parseCoreTaskRef(input)).not.toThrow();
      expect(parseCoreTaskRef(input)).toEqual({ ok: false, reason: 'unknown_task' });
    }
  });

  it('fails safely when reading a hostile unknown input', () => {
    const hostile = Object.defineProperty({}, 'taskId', {
      get: () => {
        throw new Error('taskId unavailable');
      },
    });

    expect(() => parseCoreTaskRef(hostile)).not.toThrow();
    expect(parseCoreTaskRef(hostile)).toEqual({ ok: false, reason: 'unknown_task' });
  });

  it('fails safely for a revoked proxy without throwing', () => {
    const { proxy, revoke } = Proxy.revocable({}, {});
    revoke();

    expect(() => parseCoreTaskRef(proxy)).not.toThrow();
    expect(parseCoreTaskRef(proxy)).toEqual({ ok: false, reason: 'unknown_task' });
  });

  it('reads every required identity getter once and reuses the validated values', () => {
    let applicationReads = 0;
    let eventReads = 0;
    const input = {
      taskId: 'application.interview_prepare' as const,
      get applicationId() {
        applicationReads += 1;
        return applicationReads === 1 ? 7 : Number.NaN;
      },
      get eventId() {
        eventReads += 1;
        return eventReads === 1 ? 9 : Number.NaN;
      },
    };

    expect(parseCoreTaskRef(input)).toEqual({
      ok: true,
      ref: { taskId: 'application.interview_prepare', applicationId: 7, eventId: 9 },
      key: 'application.interview_prepare:applicationId=7:eventId=9',
    });
    expect(applicationReads).toBe(1);
    expect(eventReads).toBe(1);
  });

  it('keeps source, focus, and hints out of the canonical identity key', () => {
    const first: TaskLaunchRequest = {
      ref: { taskId: 'application.material_kit', applicationId: 7 },
      source: 'application_header',
      focus: 'overview',
      hints: { suggestedResumeId: 3, suggestedOfferId: 4, suggestedEventId: 5 },
    };
    const second: TaskLaunchRequest = {
      ref: { taskId: 'application.material_kit', applicationId: 7 },
      source: 'pilot',
      focus: 'current',
      hints: { suggestedResumeId: 99 },
    };

    expect(coreTaskCanonicalKey(first.ref)).toBe('application.material_kit:applicationId=7');
    expect(coreTaskCanonicalKey(second.ref)).toBe(coreTaskCanonicalKey(first.ref));
    expect(parseCoreTaskRef(first.ref)).toEqual(parseCoreTaskRef(second.ref));
  });

  it('uses only the ordered required identity fields in canonical keys', () => {
    const ref: CoreTaskRef = {
      taskId: 'application.interview_review',
      applicationId: 7,
      eventId: 9,
    };

    expect(coreTaskCanonicalKey(ref)).toBe(
      'application.interview_review:applicationId=7:eventId=9',
    );
  });
});
