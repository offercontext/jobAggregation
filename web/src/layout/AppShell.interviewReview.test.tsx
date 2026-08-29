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

  it('keeps the entry in Application context and focuses the native review flow', () => {
    expect(pilotCardSource).toContain('onOpenInterviewReview');
    expect(pilotCardSource).toContain('打开面试复盘');
    expect(appShellSource).toContain('pilotInterviewReviewApplicationId');
    expect(appShellSource).toContain('onPilotInterviewReviewFocusConsumed');
    expect(appShellSource).toContain('onOpenInterviewReview');
  });

  it('does not make Pilot call proposal APIs or create cross-domain writes', () => {
    expect(pilotCardSource).not.toContain('createInterviewReviewProposal');
    expect(pilotCardSource).not.toContain('createNote');
    expect(pilotCardSource).not.toContain('createEvent');
    expect(appShellSource).not.toContain('writeInterviewReviewProposal');
  });
});
