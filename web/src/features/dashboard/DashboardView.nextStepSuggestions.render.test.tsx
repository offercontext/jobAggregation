import { describe, expect, it } from 'vitest';
import source from './DashboardView.tsx?raw';

describe('DashboardView task hierarchy', () => {
  it('uses existing deterministic facts but does not compete with a second next-step surface', () => {
    expect(source).toContain('deriveActionHints');
    expect(source).toContain('deriveMissionControl');
    expect(source).toContain('deriveTodayWorkspace');
    expect(source).not.toContain('NextStepSuggestions');
    expect(source).not.toContain('deriveNextStepSuggestions');
  });

  it('keeps unavailable weekly domains explicit', () => {
    expect(source).toContain("kinds.push('interviews')");
    expect(source).toContain("kinds.push('offers')");
    expect(source).toContain("kinds.push('practice')");
    expect(source).toContain("kinds.push('materials')");
  });
});
