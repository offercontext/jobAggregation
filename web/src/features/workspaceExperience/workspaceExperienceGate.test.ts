import { describe, expect, it } from 'vitest';

async function read(relativePath: string): Promise<string> {
  const fsModule = 'node:fs';
  const { readFileSync } = (await import(fsModule)) as { readFileSync: (path: URL, encoding: string) => string };
  return readFileSync(new URL(relativePath, import.meta.url), 'utf8');
}

describe('desktop workspace external gate', () => {
  it('pins the worktree, allowlist and its sha-256', async () => {
    const manifest = JSON.parse(await read('../../../workspace-experience-gate.json')) as {
      baseline: string;
      worktreeLocator: string;
      allowlist: string[];
      allowlistSha256: string;
    };
    expect(manifest.baseline).toBe('2f6e895e02b86f33052a2e507e9b0404bb82f4b5');
    expect(manifest.worktreeLocator).toBe('D:\\Users\\yuqi.chen\\offerpilot\\.worktrees\\refactor-20260821-assistant-surface-shell');
    expect(manifest.allowlist).toEqual(['web/**', 'docs/superpowers/specs/**', 'docs/superpowers/plans/**', 'docs/reports/**']);
    expect(manifest.allowlistSha256).toMatch(/^[a-f0-9]{64}$/);
  });

  it('keeps assistant ownership and transport centralized', async () => {
    const appShell = await read('../../layout/AppShell.tsx');
    const provider = await read('../assistantSurface/AssistantSurfaceProvider.tsx');
    expect((appShell.match(/<AssistantSurfaceProvider>/g) ?? [])).toHaveLength(1);
    expect(provider).toContain('usePilotConversationController');
    expect(appShell).not.toContain('JournalRun');
    expect(appShell).not.toContain('PendingAction');
    expect(appShell).not.toContain('chatOpen');
    expect(appShell).not.toContain('pilotDrawerOpen');
    expect(appShell).not.toContain('new EventSource');
  });

  it('keeps four business destinations, a weak settings entry and compatible view mappings', async () => {
    const navigation = await read('../../layout/navigation.ts');
    const sidebar = await read('../../layout/Sidebar.tsx');
    expect(navigation).toContain("{ key: 'today', label: '今日'");
    expect(navigation).toContain("{ key: 'applications', label: '投递'");
    expect(navigation).toContain("{ key: 'interview', label: '面试'");
    expect(navigation).toContain("{ key: 'resources', label: '资料'");
    expect(navigation).not.toContain("{ key: 'pilot', label:");
    expect(sidebar).toContain("MODULE_NAV.filter((item) => item.key !== 'settings')");
    expect(sidebar).toContain('data-navigation-tier="utility"');
    for (const view of ['dashboard', 'reminders', 'board', 'applications-list', 'calendar', 'offers', 'interview', 'questions', 'resumes', 'reviews', 'knowledge', 'pilot', 'settings']) {
      expect(
        navigation.includes(`${view}:`) || navigation.includes(`'${view}':`),
        `${view} must retain an explicit module mapping`,
      ).toBe(true);
    }
  });

  it('keeps changed business pages outside transport and pending protocols', async () => {
    const sources = await Promise.all([
      read('../../features/dashboard/DashboardView.tsx'),
      read('../../components/ApplicationDetail.tsx'),
      read('../../components/InterviewV01View.tsx'),
      read('../../components/ResumeLibraryView.tsx'),
      read('../../components/KnowledgeSourcesView.tsx'),
      read('../../components/OfferCenterView.tsx'),
      read('../../components/SettingsView.tsx'),
    ]);
    const joined = sources.join('\n');
    expect(joined).not.toContain('new EventSource');
    expect(joined).not.toContain('JournalRun');
    expect(joined).not.toContain('WriteOperationLedger');
    expect(joined).not.toContain('pending_action');
  });

  it('keeps board and list on the same application view state', async () => {
    const appShell = await read('../../layout/AppShell.tsx');
    expect(appShell).toContain('const [applicationViewState, setApplicationViewState]');
    expect(appShell).toMatch(/<KanbanBoard[\s\S]*?viewState=\{applicationViewState\}/);
    expect(appShell).toMatch(/<ApplicationListView[\s\S]*?viewState=\{applicationViewState\}[\s\S]*?onViewStateChange=\{setApplicationViewState\}/);
    expect(appShell).not.toContain('setAISettingsOpen');
  });

  it('does not add a second design system or mobile product dependency', async () => {
    const packageJson = JSON.parse(await read('../../../package.json')) as { dependencies: Record<string, string> };
    expect(packageJson.dependencies).not.toHaveProperty('tailwindcss');
    expect(packageJson.dependencies).not.toHaveProperty('react-native');
    expect(packageJson.dependencies).not.toHaveProperty('motion');
    expect(packageJson.dependencies).not.toHaveProperty('framer-motion');
  });
});
