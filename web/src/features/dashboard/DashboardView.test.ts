import { describe, expect, it } from 'vitest';
import source from './DashboardView.tsx?raw';

describe('DashboardView onboarding actions', () => {
  it('forwards typed onboarding actions to the checklist', () => {
    expect(source).toContain('onOnboardingAction: (action: OnboardingAction) => void;');
    expect(source).toContain('onAction={onOnboardingAction}');
  });

  it('renders one task-first desktop hierarchy and collapses analytics by default', () => {
    expect(source).toContain('当前最重要的行动');
    expect(source).toContain('今日其他待办');
    expect(source).toContain('未来 7 天日程');
    expect(source).toContain('本周进度');
    expect(source).toContain('数据分析');
    expect(source).toContain('defaultActiveKey={[]}');
    expect(source).toContain('todayWorkspace.otherActions');
    expect(source).not.toContain('<NextStepSuggestions');
    expect(source).not.toContain('<MissionHeader');
  });

  it('consumes shell-owned workspace data without refetching the same collections', () => {
    expect(source).toContain('applications: Application[];');
    expect(source).toContain('events: ScheduleEvent[];');
    expect(source).toContain('offers: Offer[];');
    expect(source).not.toContain("queryKey: ['applications']");
    expect(source).not.toContain("queryKey: ['events']");
    expect(source).not.toContain("queryKey: ['offers']");
    expect(source).not.toContain("queryKey: ['questions', 'stats']");
  });
});
