import { describe, expect, it } from 'vitest';
import appShellSource from './AppShell.tsx?raw';
import dashboardSource from '@/features/dashboard/DashboardView.tsx?raw';

describe('next-step suggestions AppShell integration contract', () => {
  it('mounts the shared suggestion component without adding per-application history queries', () => {
    expect(appShellSource).toContain('deriveNextStepSuggestions');
    expect(appShellSource).toContain('suggestionSessionStates');
    expect(appShellSource).toContain('nextStepSuggestions={selectedNextStepSuggestions ?? undefined}');
    expect(dashboardSource).not.toContain('NextStepSuggestions');
    expect(appShellSource).not.toContain('listOpportunityFitV2Reviews(application.id)');
    expect(appShellSource).not.toContain('listMockInterviewHistory(application.id)');
  });

  it('keeps AppShell unknown facts explicit and reuses Dashboard material coverage', () => {
    expect(appShellSource).toContain("reason: 'not_loaded'");
    expect(appShellSource).toContain("reason: 'not_supported'");
    expect(dashboardSource).toContain('materialKitsQ');
    expect(dashboardSource).toContain('hasPartialMaterialKitCoverage');
  });

  it('keys session updates by applicationId and suggestionId', () => {
    expect(appShellSource).toContain('applicationId + suggestionId');
    expect(appShellSource).toContain('onSetDisposition');
    expect(appShellSource).toContain('stateKey');
  });

  it('does not persist snooze or ignore state', () => {
    expect(appShellSource).not.toContain('localStorage.setItem');
    expect(appShellSource).not.toContain('saveSuggestion');
  });

  it('prunes a stale state entry instead of only ignoring it while rendering', () => {
    expect(appShellSource).toContain('delete next[key]');
    expect(appShellSource).toContain('existing.stateKey === selectedNextStepCandidate.stateKey');
    expect(dashboardSource).not.toContain('onPruneDisposition');
  });

  it('does not drop review destination context when no typed adapter exists', () => {
    expect(appShellSource).toContain('interview_review_history');
    expect(appShellSource).toContain('opportunity_fit_history');
    expect(appShellSource).toContain('isNextStepNavigationAvailable');
    expect(appShellSource).not.toContain('openPilotInterviewReview(destination.applicationId)');
  });
});
