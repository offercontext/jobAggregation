import { describe, expect, it } from 'vitest';
import {
  createResumeSelectionLease,
  resolveResumeSelection,
  type ResumeSelectionInput,
} from './resumeSelectionLease';

const resumes = [
  { id: 11, deletedAt: null, isMaster: true },
  { id: 12, deletedAt: null, isMaster: false },
];

const baseInput: ResumeSelectionInput = {
  applicationId: 7,
  eventId: 8,
  resumes,
};

describe('ResumeSelectionLease', () => {
  it('prefers an explicit visible selection for the same application and event', () => {
    const lease = createResumeSelectionLease(3);
    expect(lease.select({ applicationId: 7, eventId: 8, resumeId: 12 })).toBe(true);
    expect(resolveResumeSelection({ ...baseInput, lease })).toMatchObject({
      kind: 'selected',
      resumeId: 12,
      source: 'lease',
    });
  });

  it('uses a verified saved snapshot before applying the one-visible-resume fallback', () => {
    expect(resolveResumeSelection({
      ...baseInput,
      resumes: [{ id: 11, deletedAt: null }, { id: 12, deletedAt: null }],
      savedSnapshot: { applicationId: 7, eventId: 8, resumeId: 12, sourceValid: true },
    })).toMatchObject({ kind: 'selected', resumeId: 12, source: 'snapshot' });

    expect(resolveResumeSelection({
      ...baseInput,
      resumes: [{ id: 11, deletedAt: null }],
      savedSnapshot: { applicationId: 7, eventId: 8, resumeId: 12, sourceValid: false },
    })).toMatchObject({ kind: 'selected', resumeId: 11, source: 'single_visible' });
  });

  it('accepts an application-scoped saved snapshot when no event identity is present', () => {
    expect(resolveResumeSelection({
      ...baseInput,
      savedSnapshot: { applicationId: 7, resumeId: 12, sourceValid: true },
    })).toMatchObject({ kind: 'selected', resumeId: 12, source: 'snapshot' });
    expect(resolveResumeSelection({
      ...baseInput,
      savedSnapshot: { applicationId: 7, eventId: 9, resumeId: 12, sourceValid: true },
    }).kind).toBe('needs_selection');
    expect(resolveResumeSelection({
      ...baseInput,
      savedSnapshot: { applicationId: 7, eventId: 0, resumeId: 12, sourceValid: true },
    }).kind).toBe('needs_selection');
  });

  it('does not reuse a deleted, cross-application, or cross-event selection', () => {
    const lease = createResumeSelectionLease(3);
    lease.select({ applicationId: 7, eventId: 8, resumeId: 12 });
    expect(resolveResumeSelection({ ...baseInput, lease, resumes: [{ id: 12, deletedAt: '2026-08-30T00:00:00Z' }] }).kind).toBe('needs_selection');
    expect(resolveResumeSelection({ ...baseInput, applicationId: 9, lease }).kind).toBe('needs_selection');
    expect(resolveResumeSelection({ ...baseInput, eventId: 10, lease }).kind).toBe('needs_selection');
  });

  it('does not treat a foreign resume row as a visible fallback', () => {
    const result = resolveResumeSelection({
      ...baseInput,
      resumes: [{ id: 11, application_id: 99, event_id: 8 }],
    });
    expect(result).toMatchObject({ kind: 'needs_selection', reason: 'selection_missing' });
  });

  it('asks only once when multiple visible resumes remain unresolved', () => {
    const lease = createResumeSelectionLease(3);
    const first = resolveResumeSelection({ ...baseInput, lease });
    const second = resolveResumeSelection({ ...baseInput, lease });
    expect(first).toMatchObject({ kind: 'needs_selection', shouldAsk: true });
    expect(second).toMatchObject({ kind: 'needs_selection', shouldAsk: false });
  });

  it('revokes selection on close/cancel and rejects a stale generation', () => {
    const lease = createResumeSelectionLease(3);
    expect(lease.select(7, 8, 11)).toBe(true);
    expect(lease.isUsable(3)).toBe(true);
    lease.revoke();
    expect(lease.isUsable(3)).toBe(false);
    expect(lease.getSelection(7, 8)).toBeNull();

    const next = createResumeSelectionLease(4);
    next.select({ applicationId: 7, eventId: 8, resumeId: 11 });
    expect(resolveResumeSelection({ ...baseInput, lease: next, generation: 3 }).kind).toBe('needs_selection');
  });

  it('fails closed when the resume source is omitted or not ready', () => {
    expect(resolveResumeSelection({ ...baseInput, resumes: undefined })).toMatchObject({
      kind: 'unavailable',
      reason: 'source_absent',
    });
    expect(resolveResumeSelection({
      ...baseInput,
      resumeSource: { status: 'unknown', value: null },
      resumes: undefined,
    })).toMatchObject({ kind: 'unavailable', reason: 'source_unknown' });
    expect(resolveResumeSelection({
      ...baseInput,
      resumes: { status: 'ready', value: null },
    })).toMatchObject({ kind: 'unavailable', reason: 'source_unknown' });
  });

  it('fails closed when a source getter throws', () => {
    const hostile = {
      get value(): unknown {
        throw new Error('source getter must not escape');
      },
      status: 'ready' as const,
    };
    expect(resolveResumeSelection({ ...baseInput, resumes: hostile as never })).toMatchObject({
      kind: 'unavailable',
      reason: 'source_unknown',
    });
  });
});
