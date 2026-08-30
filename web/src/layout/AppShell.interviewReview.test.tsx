import { describe, expect, it } from 'vitest';
import { resolvePilotInterviewReviewIntent } from './AppShell';
import appShellSource from './AppShell.tsx?raw';
import pilotCardSource from '@/features/pilot/PilotOpportunityFitCard.tsx?raw';

describe('Pilot interview review navigation', () => {
  it('keeps application-only intents in a chooser and accepts only an exact event', () => {
    const event = { id: 31, application_id: 7, event_type: 'interview' as const };
    expect(resolvePilotInterviewReviewIntent(7, undefined, [])).toEqual({ kind: 'choose', applicationId: 7 });
    expect(resolvePilotInterviewReviewIntent(7, undefined, [event])).toEqual({ kind: 'choose', applicationId: 7 });
    expect(resolvePilotInterviewReviewIntent(7, undefined, [event, { ...event, id: 32 }])).toEqual({ kind: 'choose', applicationId: 7 });
    expect(resolvePilotInterviewReviewIntent(7, 31, [event])).toEqual({ kind: 'event', applicationId: 7, eventId: 31 });
    expect(resolvePilotInterviewReviewIntent(7, 31, [{ ...event, application_id: 8 }])).toEqual({ kind: 'invalid' });
  });

  it('fails closed when an exact event identity is duplicated or contradicted', () => {
    const event = { id: 31, application_id: 7, event_type: 'interview' as const };
    const foreign = { ...event, application_id: 8 };
    const wrongType = { ...event, event_type: 'deadline' as const };

    for (const events of [
      [event, foreign],
      [foreign, event],
      [event, { ...event }],
      [event, wrongType],
      [wrongType, event],
    ]) {
      expect(resolvePilotInterviewReviewIntent(7, 31, events)).toEqual({ kind: 'invalid' });
    }

    const hostile = new Proxy(event, { get() { throw new Error('hostile event row'); } });
    expect(resolvePilotInterviewReviewIntent(7, 31, [event, hostile])).toEqual({ kind: 'invalid' });
  });

  it('routes exact event cards through the shared controller and application owner', () => {
    expect(appShellSource).toContain('openExactInterviewTask');
    expect(appShellSource).toContain('onOpenTask={openExactInterviewTask}');
    expect(appShellSource).toContain("taskId: 'application.interview_review'");
    expect(appShellSource).not.toContain('onOpenMockInterview');
    expect(appShellSource).not.toContain('openMockInterview');
  });

  it('locks real preparation to the exact event and saved resume before opening the owner', () => {
    expect(appShellSource).toContain('activeInterviewPreparation');
    expect(appShellSource).toContain('fixedMode="real"');
    expect(appShellSource).toContain('generation={activeInterviewPreparation.generation}');
    expect(appShellSource).toContain('suggestedResumeId');
    expect(appShellSource).toContain('interviewPreparationSelection');
    expect(appShellSource).toContain('onLaunchTask={launchTaskFromApplicationDetail}');
  });

  it('does not make Pilot call proposal APIs or create cross-domain writes', () => {
    expect(pilotCardSource).not.toContain('createInterviewReviewProposal');
    expect(pilotCardSource).not.toContain('createNote');
    expect(pilotCardSource).not.toContain('createEvent');
    expect(appShellSource).not.toContain('writeInterviewReviewProposal');
  });

  it('keeps one controller-backed free-practice owner and removes the mock fallback', () => {
    expect(appShellSource).toContain("ref: { taskId: 'interview.free_practice' }");
    expect(appShellSource).toContain('onOpenFreePractice={openFreePractice}');
    expect(appShellSource).toContain("active?.ref.taskId === 'interview.free_practice'");
    expect(appShellSource).not.toContain("@/components/MockInterviewDrawer");
    expect(appShellSource).not.toContain('discardMockInterviewAttempt');
  });
});
