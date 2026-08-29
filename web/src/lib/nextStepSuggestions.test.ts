import { describe, expect, expectTypeOf, it } from 'vitest';
import {
  deriveNextStepSuggestions,
  deriveMaterialKitFact,
  type NextStepFacts,
  type ResumeFact,
  type InterviewReviewHistoryDestination,
  type InterviewEventSelectionDestination,
  type OpportunityFitHistoryDestination,
} from './nextStepSuggestions';
import type { Application } from '@/types/application';
import type { ScheduleEvent } from '@/types/event';

const now = new Date('2026-07-30T09:00:00+08:00');

function makeApplication(id = 1): Application {
  return {
    id,
    company_name: 'Company ' + id,
    position_name: 'Role ' + id,
    job_url: '',
    status: 'interview',
    source: '',
    notes: '',
    applied_at: '2026-07-01',
    created_at: '2026-07-01T09:00:00+08:00',
    updated_at: '2026-07-20T09:00:00+08:00',
  };
}

function makeResume(id = 1, version = 'v1'): ResumeFact {
  return {
    id,
    name: 'Resume ' + id,
    title: 'Resume ' + id,
    file_path: '',
    parsed_data: '',
    parse_status: 'parsed',
    is_master: id === 1,
    parent_resume_id: null,
    source: 'manual',
    source_file_path: '',
    content_json: { raw_text: 'resume ' + version },
    deleted_at: null,
    created_at: '2026-07-01T09:00:00+08:00',
    completion_percent: 100,
    missing_sections: [],
    is_complete: true,
    version,
  };
}

function makeInterviewEvent(
  id: number,
  scheduledAt: string,
  durationMinutes = 60,
  overrides: Partial<ScheduleEvent> = {},
): ScheduleEvent {
  return {
    id,
    application_id: 1,
    event_type: 'interview',
    subtype: 'technical',
    tags: [],
    round: id,
    scheduled_at: scheduledAt,
    duration_minutes: durationMinutes,
    location: '',
    notes: '',
    remind_at: null,
    status: 'scheduled',
    created_at: '2026-07-' + String(id).padStart(2, '0') + 'T09:00:00+08:00',
    ...overrides,
  };
}

function makeFacts(overrides: Partial<NextStepFacts> = {}): NextStepFacts {
  return {
    application: { status: 'known', value: makeApplication() },
    availableResumes: { status: 'known', value: [makeResume()] },
    events: { status: 'known', value: [] },
    offers: { status: 'known', value: [] },
    confirmedKnowledge: { status: 'known', value: [] },
    practiceStats: {
      status: 'known',
      value: { total: 0, new: 0, practicing: 0, mastered: 0, due: 0, today_reviews: 0, streak_days: 0 },
    },
    jd: { status: 'unknown', reason: 'not_supported' },
    fitReview: { status: 'unknown', reason: 'not_loaded' },
    materialKit: { status: 'unknown', reason: 'not_loaded' },
    interviewPreparationHistory: { status: 'unknown', reason: 'not_loaded' },
    mockInterviewHistory: { status: 'unknown', reason: 'not_loaded' },
    ...overrides,
  };
}

describe('next-step suggestion destination types', () => {
  it('exports the shared pure derivation function', () => {
    expect(deriveNextStepSuggestions).toBeTypeOf('function');
  });

  it('keeps incomplete Material Kit coverage unknown and marks complete coverage known', () => {
    expect(deriveMaterialKitFact({ applicationId: 1, status: 'loading', complete: false }).status).toBe('unknown');
    expect(deriveMaterialKitFact({ applicationId: 1, status: 'error', complete: false }).status).toBe('unknown');
    expect(deriveMaterialKitFact({
      applicationId: 1,
      status: 'success',
      complete: false,
      kits: [{ application_id: 1 }],
    }).status).toBe('unknown');
    expect(deriveMaterialKitFact({
      applicationId: 1,
      status: 'success',
      complete: true,
      kits: [{ application_id: 1 }],
    }).status).toBe('known');
    expect(deriveMaterialKitFact({
      applicationId: 2,
      status: 'success',
      complete: true,
      kits: [{ application_id: 1 }],
    })).toEqual({ status: 'known', value: null });
  });

  it('requires complete history and selection contexts at compile time', () => {
    const validHistory: InterviewReviewHistoryDestination = {
      kind: 'interview_review_history',
      applicationId: 1,
      eventId: 2,
      reviewId: 3,
    };
    expectTypeOf(validHistory).toMatchTypeOf<InterviewReviewHistoryDestination>();
    const validOpportunityHistory: OpportunityFitHistoryDestination = {
      kind: 'opportunity_fit_history',
      applicationId: 1,
      reviewId: 4,
    };
    expectTypeOf(validOpportunityHistory).toMatchTypeOf<OpportunityFitHistoryDestination>();

    // @ts-expect-error review history cannot omit reviewId
    const incompleteHistory: InterviewReviewHistoryDestination = {
      kind: 'interview_review_history',
      applicationId: 1,
      eventId: 2,
    };
    void incompleteHistory;

    const guessedSelection: InterviewEventSelectionDestination = {
      kind: 'interview_event_selection',
      applicationId: 1,
      // @ts-expect-error a multi-event selection cannot guess an eventId
      eventId: 2,
    };
    void guessedSelection;
  });

  it('keeps the destination union discriminated', () => {
    expectTypeOf<InterviewReviewHistoryDestination['reviewId']>().toEqualTypeOf<number>();
    expectTypeOf<InterviewEventSelectionDestination>().not.toHaveProperty('eventId');
    expect(true).toBe(true);
  });
});

describe('deriveNextStepSuggestions', () => {
  it('does not infer a missing Resume from an unknown collection', () => {
    const result = deriveNextStepSuggestions(
      makeFacts({ availableResumes: { status: 'unknown', reason: 'not_loaded' } }),
      'detail',
      now,
    );

    expect(result.candidates.some((candidate) => candidate.id === 'choose_resume')).toBe(false);
  });

  it('suggests choosing a Resume without a current-source label when the known collection is empty', () => {
    const result = deriveNextStepSuggestions(makeFacts({ availableResumes: { status: 'known', value: [] } }), 'detail', now);
    const candidate = result.candidates.find((item) => item.id === 'choose_resume');

    expect(candidate).toEqual(expect.objectContaining({ destination: { kind: 'application_detail', applicationId: 1 } }));
    expect(candidate?.sources.some((source) => source.label === '当前使用来源')).toBe(false);
    expect(candidate?.sources.some((source) => source.label === '已检查简历库')).toBe(true);
  });

  it('uses one neutral detail action when the workbench lacks enough known facts', () => {
    const result = deriveNextStepSuggestions(makeFacts(), 'workbench', now);

    expect(result.candidates).toHaveLength(1);
    expect(result.candidates[0]).toEqual(expect.objectContaining({
      title: '查看投递详情以确认下一步',
      reason: '查看投递详情以确认下一步',
      destination: { kind: 'application_detail', applicationId: 1 },
    }));
  });

  it('does not create a missing-JD or confirmation action for an unsupported JD fact', () => {
    const result = deriveNextStepSuggestions(makeFacts(), 'detail', now);

    expect(result.candidates.some((candidate) => /JD|岗位信息/.test(candidate.title + candidate.reason))).toBe(false);
  });

  it('uses an event destination for one valid future event', () => {
    const event = makeInterviewEvent(7, '2026-08-01T10:00:00+08:00');
    const result = deriveNextStepSuggestions(makeFacts({ events: { status: 'known', value: [event] } }), 'detail', now);

    expect(result.candidates[0]?.destination).toEqual({
      kind: 'interview_event',
      applicationId: 1,
      eventId: 7,
    });
  });

  it('requires event selection when multiple valid future events exist', () => {
    const result = deriveNextStepSuggestions(makeFacts({
      events: {
        status: 'known',
        value: [
          makeInterviewEvent(7, '2026-08-01T10:00:00+08:00'),
          makeInterviewEvent(8, '2026-08-02T10:00:00+08:00'),
        ],
      },
    }), 'detail', now);

    expect(result.candidates[0]?.destination).toEqual({
      kind: 'interview_event_selection',
      applicationId: 1,
    });
  });

  it('uses review history only when exactly one ended review is known', () => {
    const event = makeInterviewEvent(7, '2026-07-29T10:00:00+08:00', 60, { status: 'done' });
    const result = deriveNextStepSuggestions(makeFacts({
      events: { status: 'known', value: [event] },
      interviewPreparationHistory: { status: 'known', value: [] },
      mockInterviewHistory: { status: 'known', value: [] },
      fitReview: { status: 'known', value: { reviewCountByEvent: { 7: 1 }, reviewIdByEvent: { 7: 19 } } },
    }), 'detail', now);

    expect(result.candidates[0]?.destination).toEqual({
      kind: 'interview_review_history',
      applicationId: 1,
      eventId: 7,
      reviewId: 19,
    });
  });

  it('uses lifecycle completion for a future done event and never ends a past todo event', () => {
    const futureDone = deriveNextStepSuggestions(makeFacts({
      events: { status: 'known', value: [makeInterviewEvent(7, '2026-08-30T10:00:00+08:00', 60, { status: 'done' })] },
      interviewPreparationHistory: { status: 'known', value: [] },
      mockInterviewHistory: { status: 'known', value: [] },
      fitReview: { status: 'known', value: null },
    }), 'detail', now);
    expect(futureDone.candidates.some((candidate) => candidate.id === 'review_interview')).toBe(true);

    const pastTodo = deriveNextStepSuggestions(makeFacts({
      events: { status: 'known', value: [makeInterviewEvent(8, '2026-07-29T10:00:00+08:00', 60, { status: 'todo' })] },
    }), 'detail', now);
    expect(pastTodo.candidates.some((candidate) => candidate.id === 'review_interview')).toBe(false);
  });

  it('keeps an in-progress event actionable at its exact scheduled end', () => {
    const eventStart = '2026-07-30T08:00:00+08:00';
    const atEnd = deriveNextStepSuggestions(makeFacts({
      events: { status: 'known', value: [makeInterviewEvent(9, eventStart, 60, { status: 'in_progress' })] },
    }), 'detail', new Date('2026-07-30T09:00:00+08:00'));
    expect(atEnd.candidates.some((candidate) => candidate.id === 'prepare_interview')).toBe(true);
  });

  it('excludes events with invalid date or duration from interview destinations', () => {
    const result = deriveNextStepSuggestions(makeFacts({
      events: {
        status: 'known',
        value: [
          makeInterviewEvent(7, 'not-a-date'),
          makeInterviewEvent(8, '2026-08-01T10:00:00+08:00', 0),
          makeInterviewEvent(9, '2026-08-02T10:00:00+08:00', 30.5),
        ],
      },
    }), 'detail', now);

    expect(result.candidates.some((candidate) => candidate.destination.kind.startsWith('interview_'))).toBe(false);
  });

  it('treats calendar-invalid dates as unknown and keeps explicit source risks separate', () => {
    const result = deriveNextStepSuggestions(makeFacts({
      events: {
        status: 'known',
        value: [makeInterviewEvent(7, '2026-02-30T10:00:00+08:00')],
      },
      sourceRisks: [{
        id: 'jd-risk',
        stateKey: 'jd-risk-v1',
        title: '来源状态需要确认',
        reason: '当前输入尚未形成冻结来源。',
        sources: [{ label: '岗位描述', status: 'changed' }],
      }],
    }), 'detail', now);

    expect(result.candidates.some((candidate) => candidate.destination.kind.startsWith('interview_'))).toBe(false);
    expect(result.sourceRisks.map((risk) => risk.id)).toEqual(['jd-risk']);
  });

  it('changes the stateKey when the Resume version changes without changing its ID', () => {
    const before = deriveNextStepSuggestions(
      makeFacts({
        availableResumes: { status: 'known', value: [makeResume(3, 'v1')] },
        fitReview: { status: 'known', value: null },
      }),
      'detail',
      now,
    );
    const after = deriveNextStepSuggestions(
      makeFacts({
        availableResumes: { status: 'known', value: [makeResume(3, 'v2')] },
        fitReview: { status: 'known', value: null },
      }),
      'detail',
      now,
    );

    expect(after.candidates[0]?.stateKey).not.toBe(before.candidates[0]?.stateKey);
    expect(after.candidates[0]?.stateKey).not.toContain('resume v2');
  });

  it('changes the stateKey when the unique historical review changes', () => {
    const before = deriveNextStepSuggestions(makeFacts({
      events: { status: 'known', value: [makeInterviewEvent(7, '2026-07-29T10:00:00+08:00', 60, { status: 'done' })] },
      fitReview: { status: 'known', value: { reviewCountByEvent: { 7: 1 }, reviewIdByEvent: { 7: 19 } } },
    }), 'detail', now);
    const after = deriveNextStepSuggestions(makeFacts({
      events: { status: 'known', value: [makeInterviewEvent(7, '2026-07-29T10:00:00+08:00', 60, { status: 'done' })] },
      fitReview: { status: 'known', value: { reviewCountByEvent: { 7: 1 }, reviewIdByEvent: { 7: 20 } } },
    }), 'detail', now);

    expect(before.candidates[0]?.destination).toEqual(expect.objectContaining({ reviewId: 19 }));
    expect(after.candidates[0]?.destination).toEqual(expect.objectContaining({ reviewId: 20 }));
    expect(after.candidates[0]?.stateKey).not.toBe(before.candidates[0]?.stateKey);
  });
});
