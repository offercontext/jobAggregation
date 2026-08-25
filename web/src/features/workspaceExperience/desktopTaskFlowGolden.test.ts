import { describe, expect, it } from 'vitest';
import { DESKTOP_TASK_FLOW_GOLDEN } from './desktopTaskFlowGolden';
import navigation from '@/layout/navigation.ts?raw';
import appShell from '@/layout/AppShell.tsx?raw';

describe('desktop task flow golden', () => {
  it('pins the project baseline and desktop matrix', () => {
    expect(DESKTOP_TASK_FLOW_GOLDEN.baseline).toBe('0c10e05e256eb757d5f89a8b009dcea193f2fc78');
    expect(DESKTOP_TASK_FLOW_GOLDEN.desktopWidths).toEqual([768, 1024, 1280, 1440]);
  });

  it('keeps the task-first navigation and canonical application views', () => {
    for (const label of [
      ...DESKTOP_TASK_FLOW_GOLDEN.navigationGroups,
      ...DESKTOP_TASK_FLOW_GOLDEN.primaryNavigation,
      ...DESKTOP_TASK_FLOW_GOLDEN.resourceNavigation,
      ...DESKTOP_TASK_FLOW_GOLDEN.resourceTabs,
    ]) expect(navigation).toContain(`label: '${label}'`);
    expect(navigation).toContain("{ view: 'board', label: '看板' }");
    expect(navigation).toContain("{ view: 'applications-list', label: '列表' }");
    expect(navigation).toContain("calendar: 'today'");
    expect(navigation).toContain("offers: 'offers'");
  });

  it('keeps one assistant owner while TopBar actions only signal existing flows', () => {
    expect((appShell.match(/<AssistantSurfaceProvider>/g) ?? [])).toHaveLength(1);
    expect((appShell.match(/usePilotConversationController\(\)/g) ?? [])).toHaveLength(1);
    for (const label of DESKTOP_TASK_FLOW_GOLDEN.topBarActions.filter((label) => label !== '录入 Offer')) {
      expect(appShell).toContain(`label: '${label}'`);
    }
    expect(appShell).toContain("? '添加投递' : '录入 Offer'");
    expect(appShell).not.toContain('new EventSource');
    expect(appShell).not.toContain('streamChat(');
  });
});
