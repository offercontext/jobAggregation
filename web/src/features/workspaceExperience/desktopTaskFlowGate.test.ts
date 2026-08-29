import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const BASELINE = '0c10e05e256eb757d5f89a8b009dcea193f2fc78';
const PROJECT_END = '78195000bd94fdfd6fe8508033e0a8687fde3323';
const ALLOWED_DOCUMENTS = [
  'docs/superpowers/specs/2026-08-25-desktop-task-flow-simplification-design.md',
  'docs/superpowers/plans/2026-08-25-desktop-task-flow-simplification.md',
  'docs/superpowers/reports/2026-08-25-desktop-task-flow-simplification-verification.md',
] as const;

const FORBIDDEN_PATHS = [
  'web/src/components/ChatPanel/capabilities.ts',
  'web/src/components/ChatPanel/ProposalCard.tsx',
  'web/src/components/ChatPanel/ProcessTimeline.tsx',
  'web/src/components/ChatPanel/model.ts',
  'web/src/services/chat.ts',
  'web/src/types/chat.ts',
  'web/src/features/assistantSurface/AssistantSurfaceProvider.tsx',
  'web/src/features/assistantSurface/usePilotConversationController.ts',
] as const;

function repoRoot(): string {
  return execFileSync('git', ['rev-parse', '--show-toplevel'], { encoding: 'utf8' }).trim();
}

function gitLines(root: string, args: string[]): string[] {
  const output = execFileSync('git', args, { cwd: root, encoding: 'utf8' }).trim();
  return output ? output.split(/\r?\n/).map((line) => line.trim()).filter(Boolean) : [];
}

function normalize(path: string): string {
  return path.split('\\').join('/');
}

function changedPaths(root: string): string[] {
  // This is a historical project-scope gate. Validate the exact independently
  // reviewed desktop range without claiming ownership over later upstream merges.
  return gitLines(root, ['diff', '--name-only', '--no-renames', `${BASELINE}..${PROJECT_END}`, '--'])
    .map(normalize)
    .sort();
}

function currentWorktreePaths(root: string): string[] {
  const tracked = gitLines(root, ['diff', '--name-only', '--no-renames', 'HEAD', '--']);
  const untracked = gitLines(root, ['ls-files', '--others', '--exclude-standard']);
  return [...new Set([...tracked, ...untracked].map(normalize))].sort();
}

function isAllowedPath(path: string): boolean {
  return path === 'web' || path.startsWith('web/') || ALLOWED_DOCUMENTS.includes(path as (typeof ALLOWED_DOCUMENTS)[number]);
}

function read(root: string, relativePath: string): string {
  return readFileSync(join(root, relativePath), 'utf8');
}

function readHexToken(css: string, token: string): string {
  const value = css.match(new RegExp(`--${token}:\\s*(#[0-9a-f]{6})`, 'i'))?.[1];
  if (!value) throw new Error(`Missing six-digit color token --${token}`);
  return value;
}

function contrastRatio(foreground: string, background: string): number {
  const luminance = (hex: string): number => {
    const channels = [1, 3, 5].map((offset) => Number.parseInt(hex.slice(offset, offset + 2), 16) / 255);
    const [red, green, blue] = channels.map((channel) => (
      channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4
    ));
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
  };
  const foregroundLuminance = luminance(foreground);
  const backgroundLuminance = luminance(background);
  return (Math.max(foregroundLuminance, backgroundLuminance) + 0.05)
    / (Math.min(foregroundLuminance, backgroundLuminance) + 0.05);
}

function isChangedProductionSource(path: string): boolean {
  return path.startsWith('web/src/')
    && /\.(?:ts|tsx)$/.test(path)
    && !/(?:\.test|\.spec)\.[^.]+$/.test(path);
}

describe('Desktop Task Flow independent frontend gate', () => {
  it('pins the requested baseline, completed project range, and exact web-plus-three-doc allowlist', () => {
    const root = repoRoot();
    expect(BASELINE).toBe('0c10e05e256eb757d5f89a8b009dcea193f2fc78');
    expect(PROJECT_END).toBe('78195000bd94fdfd6fe8508033e0a8687fde3323');
    expect(gitLines(root, ['rev-parse', BASELINE])).toEqual([BASELINE]);
    expect(gitLines(root, ['rev-parse', PROJECT_END])).toEqual([PROJECT_END]);
    expect(() => execFileSync('git', ['merge-base', '--is-ancestor', BASELINE, 'HEAD'], {
      cwd: root,
      stdio: 'ignore',
    })).not.toThrow();
    expect(() => execFileSync('git', ['merge-base', '--is-ancestor', PROJECT_END, 'HEAD'], {
      cwd: root,
      stdio: 'ignore',
    })).not.toThrow();
    expect(ALLOWED_DOCUMENTS).toEqual([
      'docs/superpowers/specs/2026-08-25-desktop-task-flow-simplification-design.md',
      'docs/superpowers/plans/2026-08-25-desktop-task-flow-simplification.md',
      'docs/superpowers/reports/2026-08-25-desktop-task-flow-simplification-verification.md',
    ]);
  });

  it('rejects paths in the completed desktop project range outside web or the three project documents', () => {
    const root = repoRoot();
    const changed = changedPaths(root);
    const disallowed = changed.filter((path) => !isAllowedPath(path));
    const forbidden = changed.filter((path) => FORBIDDEN_PATHS.includes(path as (typeof FORBIDDEN_PATHS)[number]));

    expect(disallowed, 'the completed desktop project range must stay inside the project allowlist').toEqual([]);
    expect(forbidden, 'forbidden frontend ownership boundaries must remain untouched').toEqual([]);
  });

  it('rejects current worktree and untracked paths outside the desktop project allowlist', () => {
    const disallowed = currentWorktreePaths(repoRoot()).filter((path) => !isAllowedPath(path));
    expect(disallowed).toEqual([]);
  });

  it('keeps the forbidden frontend ownership boundaries explicit', () => {
    expect(FORBIDDEN_PATHS).toEqual([
      'web/src/components/ChatPanel/capabilities.ts',
      'web/src/components/ChatPanel/ProposalCard.tsx',
      'web/src/components/ChatPanel/ProcessTimeline.tsx',
      'web/src/components/ChatPanel/model.ts',
      'web/src/services/chat.ts',
      'web/src/types/chat.ts',
      'web/src/features/assistantSurface/AssistantSurfaceProvider.tsx',
      'web/src/features/assistantSurface/usePilotConversationController.ts',
    ]);
  });

  it('keeps page-aware TopBar actions, detail suppression, route history, focus recovery, and shared Offers wired', () => {
    const root = repoRoot();
    const appShell = read(root, 'web/src/layout/AppShell.tsx');
    const detail = read(root, 'web/src/components/ApplicationDetail.tsx');
    const topBarStart = appShell.indexOf('let topBarPrimaryAction');
    const topBarSource = appShell.slice(topBarStart, appShell.indexOf('return (', topBarStart));

    for (const label of ['添加投递', '开始练习', '上传简历', '添加经历']) {
      expect(topBarSource).toContain(`label: '${label}'`);
    }
    expect(topBarSource).toContain("? '添加投递' : '录入 Offer'");
    expect(topBarSource).toContain('if (!selectedApp) {');
    expect(appShell).toContain('primaryAction={topBarPrimaryAction}');
    expect(topBarSource).toContain('setResumeUploadRequestToken((token) => token + 1)');
    expect(topBarSource).toContain("['dashboard', 'reminders', 'board', 'applications-list']");
    expect(topBarSource).not.toContain("'calendar', 'board'");
    expect(appShell).toContain('uploadRequestToken={resumeUploadRequestToken}');
    expect(appShell).not.toContain("import ResumeUploadModal from '@/components/ResumeUploadModal'");
    expect(appShell).not.toContain('const uploadResumeMut = useMutation');
    for (const page of [
      'web/src/features/dashboard/DashboardView.tsx',
      'web/src/components/ResumeLibraryView.tsx',
      'web/src/components/InterviewStoryLibraryView.tsx',
    ]) {
      expect(read(root, page), `${page} must leave the page-level primary to TopBar`).not.toContain('<Button type="primary"');
    }
    expect((read(root, 'web/src/components/CalendarView.tsx').match(/type="primary"/g) ?? [])).toHaveLength(1);

    expect(appShell).toContain("from './viewRoute'");
    expect(appShell).toContain('readInitialWorkspaceView');
    expect(appShell).toContain('subscribeToWorkspaceView');
    expect(appShell).toContain('pushWorkspaceView(view);');
    expect(appShell).toContain('skipNextHistorySyncRef.current = true;');
    expect(appShell).toContain('const contentRef = useRef<HTMLElement | null>(null);');
    expect(appShell).toContain('contentRef.current?.focus({ preventScroll: true })');
    expect(appShell).toContain("minHeight: '100dvh'");

    const detailStart = appShell.indexOf('<ApplicationDetail');
    const detailEnd = appShell.indexOf('/>', detailStart);
    expect(appShell.slice(detailStart, detailEnd)).toContain('offers={ofrs}');
    expect(detail).toContain('offers?: Offer[]');
  });

  it('keeps one AssistantSurface owner and no AppShell-owned Chat stream', () => {
    const root = repoRoot();
    const appShell = read(root, 'web/src/layout/AppShell.tsx');
    const provider = read(root, 'web/src/features/assistantSurface/AssistantSurfaceProvider.tsx');

    expect((appShell.match(/<AssistantSurfaceProvider>/g) ?? [])).toHaveLength(1);
    expect((appShell.match(/usePilotConversationController\(\)/g) ?? [])).toHaveLength(1);
    expect((provider.match(/usePilotConversationControllerState\(\)/g) ?? [])).toHaveLength(1);
    expect(appShell).not.toContain('new EventSource');
    expect(appShell).not.toContain('streamChat(');
    expect(appShell).not.toContain('sendMessage(');
  });

  it('keeps Haru to Pilot expansion as a no-send/no-stream surface change with its component contract', () => {
    const root = repoRoot();
    const haru = read(root, 'web/src/features/assistantSurface/HaruChatWindow.tsx');
    const haruTest = read(root, 'web/src/features/assistantSurface/HaruChatWindow.test.tsx');
    const expandStart = haru.indexOf('aria-label="展开到 Pilot 工作区"');
    const expandEnd = haru.indexOf('onExpand?.();', expandStart);
    const expansion = haru.slice(expandStart, expandEnd);

    expect(expansion).toContain('surface.openPilot();');
    expect(expansion).not.toContain('sendMessage');
    expect(expansion).not.toContain('streamChat');
    expect(expansion).not.toContain('EventSource');
    expect(haruTest).toContain('expands without sending a message');
    expect(haruTest).toContain('[aria-label="展开到 Pilot 工作区"]');
    expect(haruTest).toContain('onExpand).toHaveBeenCalledTimes(1)');
  });

  it('keeps changed production surfaces away from the Chat transport service', () => {
    const root = repoRoot();
    for (const path of changedPaths(root).filter(isChangedProductionSource)) {
      expect(read(root, path), `${path} must not import Chat transport`).not.toMatch(
        /from\s+['"]@\/services\/chat['"]/
      );
    }
  });

  it('keeps the simplified desktop cards readable in both color themes', () => {
    const root = repoRoot();
    const detailStyles = read(root, 'web/src/components/ApplicationDetail.module.css');
    const onboardingStyles = read(root, 'web/src/features/onboarding/OnboardingChecklist.module.css');
    const mascotStyles = read(root, 'web/src/features/pilotMascot/PilotMascot.module.css');
    const mascot = read(root, 'web/src/features/pilotMascot/PilotMascot.tsx');
    const tokens = read(root, 'web/src/theme/tokens.css');
    const lightTokens = tokens.slice(tokens.indexOf(':root {'), tokens.indexOf(':root[data-theme="dark"]'));
    const darkTokens = tokens.slice(tokens.indexOf(':root[data-theme="dark"]'), tokens.indexOf('\n}', tokens.indexOf(':root[data-theme="dark"]')) + 2);

    expect(detailStyles).not.toContain('--op-surface-subtle');
    expect(detailStyles).not.toContain('--op-text-secondary');
    expect(detailStyles).toContain('background: var(--surface-sunken, #f8fafc);');
    expect(detailStyles).toContain('outline: 2px solid var(--op-primary-strong, #4f46e5);');
    expect(onboardingStyles).not.toContain('background: #f8fafc;');
    expect(onboardingStyles).toContain("var(--op-surface, #fff)");
    expect(onboardingStyles).toContain('color: var(--op-primary-strong, #4f46e5);');
    expect(onboardingStyles).toContain('outline: 3px solid var(--op-primary-strong, #4f46e5);');
    for (const themeTokens of [lightTokens, darkTokens]) {
      const focusColor = readHexToken(themeTokens, 'op-primary-strong');
      const surfaceColor = readHexToken(themeTokens, 'op-surface');
      const sunkenColor = readHexToken(themeTokens, 'surface-sunken');
      expect(contrastRatio(focusColor, surfaceColor), `${focusColor} text on ${surfaceColor}`).toBeGreaterThanOrEqual(4.5);
      expect(contrastRatio(focusColor, surfaceColor), `${focusColor} focus on ${surfaceColor}`).toBeGreaterThanOrEqual(3);
      expect(contrastRatio(focusColor, sunkenColor), `${focusColor} focus on ${sunkenColor}`).toBeGreaterThanOrEqual(3);
    }
    expect(mascotStyles).toContain('@media (min-width: 768px) and (max-width: 900px)');
    expect(mascotStyles).toContain('width: 150px;');
    expect(mascot).toContain('narrowDesktop: { width: 150, height: 238 }');
    expect(mascot).toContain('viewport.width <= 900');
  });
});
