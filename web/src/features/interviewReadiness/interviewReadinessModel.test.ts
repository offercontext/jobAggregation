import { describe, expect, it } from 'vitest';
import {
  buildQuickPracticeReadiness,
  buildRealInterviewReadiness,
  validateQuickPracticeDraft,
} from './interviewReadinessModel';

describe('interview readiness model', () => {
  it('fails closed for malformed locked real identities', () => {
    const result = buildRealInterviewReadiness({
      application: { id: 0 },
      jd: { status: 'ready' },
      resume: { id: Number.NaN },
      event: { id: -1 },
    });

    expect(result.ready).toBe(false);
    expect(result.items
      .filter((item) => item.key === 'application' || item.key === 'resume' || item.key === 'event')
      .every((item) => item.status === 'needs_input')).toBe(true);
  });

  it('keeps unknown real sources from being marked ready', () => {
    const result = buildRealInterviewReadiness({
      application: null,
      jd: { status: 'unknown' },
      resume: null,
      event: null,
    });

    expect(result.ready).toBe(false);
    expect(result.items.find((item) => item.key === 'jd')?.status).toBe('unknown');
  });

  it('requires explicit JD confirmation for quick practice', () => {
    const result = buildQuickPracticeReadiness({
      positionName: '后端工程师',
      jdText: '负责 Python 服务。',
      jdConfirmed: false,
      resumeId: 3,
    });

    expect(result.ready).toBe(false);
    expect(result.items.find((item) => item.key === 'jd')?.status).toBe('needs_input');
  });

  it('rejects blank or overlong quick-practice drafts', () => {
    expect(validateQuickPracticeDraft({ positionName: ' ', jdText: 'JD', jdConfirmed: true, resumeId: 1 })).toEqual({
      ok: false,
      field: 'positionName',
    });
    expect(validateQuickPracticeDraft({ positionName: '工程师', jdText: 'JD', jdConfirmed: true, resumeId: undefined })).toEqual({
      ok: false,
      field: 'resumeId',
    });
    expect(validateQuickPracticeDraft({ positionName: '工程师', jdText: 'JD', jdConfirmed: false, resumeId: 1 })).toEqual({
      ok: false,
      field: 'jdConfirmed',
    });
    expect(validateQuickPracticeDraft({ positionName: '工程师', jdText: 'JD', jdConfirmed: true, resumeId: 0 })).toEqual({
      ok: false,
      field: 'resumeId',
    });
    expect(validateQuickPracticeDraft({ positionName: '工程师', jdText: 'JD', jdConfirmed: true, resumeId: 1.5 })).toEqual({
      ok: false,
      field: 'resumeId',
    });
    expect(buildQuickPracticeReadiness({ positionName: '工程师', jdText: 'JD', jdConfirmed: true, resumeId: Number.NaN }).items.find((item) => item.key === 'resume')).toMatchObject({
      status: 'needs_input',
    });
  });
});
