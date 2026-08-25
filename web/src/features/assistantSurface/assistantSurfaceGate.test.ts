import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { join, relative } from 'node:path';
import { describe, expect, it } from 'vitest';

const BASELINE = '7c36957176445c5213a31b013251fbbce8d610db';
const ALLOWLIST = [
  '.gitattributes',
  'web/src/features/assistantSurface/**',
  'web/src/features/pilotMascot/**',
  'web/src/layout/AppShell.tsx',
  'web/src/layout/AppShell*.test.*',
  'web/src/components/ChatPanel/index.tsx',
  'web/src/components/SettingsView.tsx',
  'web/src/components/SettingsView*.test.*',
  'docs/superpowers/specs/2026-08-23-haru-surface-completion-design.md',
  'docs/superpowers/plans/2026-08-23-haru-surface-completion.md',
  'docs/reports/2026-08-23-haru-surface-completion-verification.md',
] as const;
const CANONICAL_ALLOWLIST_JSON = JSON.stringify(ALLOWLIST);
const ALLOWLIST_SHA256 = 'b1698f9b89b23effcb6adc604c5d4457a26a36c6d2207b4bbe49c70cae290eb8';
const CONTROLLER_PATH = 'web/src/features/assistantSurface/usePilotConversationController.ts';
const DOC_PATHS = ALLOWLIST.filter((path) => path.startsWith('docs/'));

type SourceEntry = {
  relativePath: string;
  text: string;
};

function repoRoot(): string {
  return execFileSync('git', ['rev-parse', '--show-toplevel'], {
    encoding: 'utf8',
  }).trim();
}

function gitLines(root: string, args: string[]): string[] {
  const output = execFileSync('git', args, {
    cwd: root,
    encoding: 'utf8',
  }).trim();
  return output ? output.split(/\r?\n/).map((line) => line.trim()).filter(Boolean) : [];
}

function isProductionSource(relativePath: string): boolean {
  return /\.(?:ts|tsx)$/.test(relativePath) && !/(?:\.test|\.spec)\.[^.]+$/.test(relativePath);
}

function readProductionSources(root: string, sourceRoot: string): SourceEntry[] {
  const absoluteRoot = join(root, sourceRoot);
  const entries: SourceEntry[] = [];

  const visit = (directory: string): void => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const absolutePath = join(directory, entry.name);
      if (entry.isDirectory()) {
        visit(absolutePath);
        continue;
      }
      const relativePath = normalize(relative(root, absolutePath));
      if (isProductionSource(relativePath)) {
        entries.push({
          relativePath,
          text: readFileSync(absolutePath, 'utf8'),
        });
      }
    }
  };

  visit(absoluteRoot);
  return entries;
}

function normalize(path: string): string {
  return path.split('\\').join('/');
}

function isAllowed(relativePath: string): boolean {
  return (
    relativePath === '.gitattributes'
    || relativePath.startsWith('web/src/features/assistantSurface/')
    || relativePath.startsWith('web/src/features/pilotMascot/')
    || relativePath === 'web/src/layout/AppShell.tsx'
    || /^web\/src\/layout\/AppShell.*\.test\.[^/]+$/.test(relativePath)
    || relativePath === 'web/src/components/ChatPanel/index.tsx'
    || relativePath === 'web/src/components/SettingsView.tsx'
    || /^web\/src\/components\/SettingsView.*\.test\.[^/]+$/.test(relativePath)
    || DOC_PATHS.includes(relativePath as (typeof DOC_PATHS)[number])
  );
}

function changedPaths(root: string): string[] {
  const paths = new Set<string>();
  const diffArgs = (prefix: string[]): string[] => [
    ...prefix,
    '--name-only',
    '--no-renames',
  ];

  for (const path of gitLines(root, diffArgs(['diff', `${BASELINE}..HEAD`]))) paths.add(normalize(path));
  for (const path of gitLines(root, diffArgs(['diff']))) paths.add(normalize(path));
  for (const path of gitLines(root, diffArgs(['diff', '--cached']))) paths.add(normalize(path));
  for (const path of gitLines(root, ['ls-files', '--others', '--exclude-standard'])) paths.add(normalize(path));

  for (const path of DOC_PATHS) {
    if (existsSync(join(root, path))) paths.add(path);
  }

  return [...paths].sort();
}

function assertBaselineIsAncestor(root: string): void {
  expect(gitLines(root, ['rev-parse', BASELINE])).toEqual([BASELINE]);
  expect(() => execFileSync('git', ['merge-base', '--is-ancestor', BASELINE, 'HEAD'], {
    cwd: root,
    stdio: 'ignore',
  })).not.toThrow();
}

describe('Haru Desktop Surface Completion gate', () => {
  it('pins the baseline, exact allowlist, and canonical allowlist hash', () => {
    const root = repoRoot();
    assertBaselineIsAncestor(root);
    expect(JSON.stringify(ALLOWLIST)).toBe(
      '[".gitattributes","web/src/features/assistantSurface/**","web/src/features/pilotMascot/**","web/src/layout/AppShell.tsx","web/src/layout/AppShell*.test.*","web/src/components/ChatPanel/index.tsx","web/src/components/SettingsView.tsx","web/src/components/SettingsView*.test.*","docs/superpowers/specs/2026-08-23-haru-surface-completion-design.md","docs/superpowers/plans/2026-08-23-haru-surface-completion.md","docs/reports/2026-08-23-haru-surface-completion-verification.md"]',
    );
    expect(createHash('sha256').update(CANONICAL_ALLOWLIST_JSON).digest('hex')).toBe(ALLOWLIST_SHA256);
  });

  it('rejects every committed, staged, unstaged, and untracked path outside the allowlist', () => {
    const root = repoRoot();
    const disallowed = changedPaths(root).filter((path) => !isAllowed(path));
    expect(disallowed, 'every changed path must match the handoff allowlist').toEqual([]);
  });

  it('keeps Chat transport ownership in the controller', () => {
    const root = repoRoot();
    const sources = [
      ...readProductionSources(root, 'web/src/features/assistantSurface'),
      ...readProductionSources(root, 'web/src/features/pilotMascot'),
    ];
    const serviceImportFiles = sources
      .filter(({ text }) => /from\s+['"]@\/services\/chat['"]/.test(text))
      .map(({ relativePath }) => relativePath);
    expect(serviceImportFiles).toEqual([CONTROLLER_PATH]);

    const controller = sources.find(({ relativePath }) => relativePath === CONTROLLER_PATH);
    expect(controller).toBeDefined();
    expect(controller?.text).toMatch(/streamChat\s+as\s+streamChatService/);
    expect(controller?.text).toMatch(/streamConfirmAction\s+as\s+streamConfirmActionService/);

    const nonController = sources.filter(({ relativePath }) => relativePath !== CONTROLLER_PATH);
    const transportPatterns = [
      /from\s+['"]@\/services\/chat['"]/,
      /\bstream(?:Chat|ConfirmAction)\b/,
      /\bnew\s+EventSource\b/,
      /\bfetch\s*\(/,
      /\bReadableStream\b/,
      /\.getReader\s*\(/,
    ];
    for (const source of nonController) {
      for (const pattern of transportPatterns) {
        expect(source.text, `${source.relativePath} must not own Chat transport`).not.toMatch(pattern);
      }
    }
  });

  it('keeps Journal, AgentRun, Ledger, ToolMessage, and Haru variant out of the surface', () => {
    const root = repoRoot();
    const surfaceSources = [
      ...readProductionSources(root, 'web/src/features/assistantSurface'),
      ...readProductionSources(root, 'web/src/features/pilotMascot'),
    ];
    const appShellPath = join(root, 'web/src/layout/AppShell.tsx');
    const settingsPath = join(root, 'web/src/components/SettingsView.tsx');
    const boundarySources = [
      ...surfaceSources,
      { relativePath: 'web/src/layout/AppShell.tsx', text: readFileSync(appShellPath, 'utf8') },
      { relativePath: 'web/src/components/SettingsView.tsx', text: readFileSync(settingsPath, 'utf8') },
    ];
    const forbiddenPatterns = [
      /\bJournal(?:Run)?\b/,
      /\bAgentRun\b/,
      /\b(?:WriteOperationLedger|OperationLedger|Ledger)\b/,
      /\bToolMessage\b/,
      /variant\s*(?:[:=])\s*(?:["']haru["']|\{\s*["']haru["']\s*\})/,
      /\brun[_-]?id\b/,
      /\brunId\b/,
    ];
    for (const source of boundarySources.filter(({ relativePath }) => relativePath !== CONTROLLER_PATH)) {
      for (const pattern of forbiddenPatterns) {
        expect(source.text, `${source.relativePath} must stay outside runtime internals`).not.toMatch(pattern);
      }
    }

    const allWebSources = readProductionSources(root, 'web/src');
    for (const source of allWebSources) {
      expect(source.text, `${source.relativePath} must not introduce variant=haru`).not.toMatch(
        /variant\s*(?:[:=])\s*(?:["']haru["']|\{\s*["']haru["']\s*\})/,
      );
    }
  });

  it('has one controller construction owner and no duplicate AppShell request/lifecycle state', () => {
    const root = repoRoot();
    const surfaceSources = [
      ...readProductionSources(root, 'web/src/features/assistantSurface'),
      ...readProductionSources(root, 'web/src/features/pilotMascot'),
    ];
    const constructionSites = surfaceSources
      .filter(({ text }) => /=\s*usePilotConversationControllerState\s*\(\)/.test(text))
      .map(({ relativePath }) => relativePath);
    expect(constructionSites).toEqual(['web/src/features/assistantSurface/AssistantSurfaceProvider.tsx']);

    const appShellPath = join(root, 'web/src/layout/AppShell.tsx');
    const appShell = readFileSync(appShellPath, 'utf8');
    expect((appShell.match(/<AssistantSurfaceProvider>/g) ?? [])).toHaveLength(1);
    for (const duplicateState of [
      /\bPilotMascotNotification\b/,
      /\bpilotMascotNotification\b/,
      /\bPilotConversationRequest\b/,
      /\bpilotConversationRequest\b/,
      /\bPilotReplyLifecycleEvent\b/,
      /\bhandlePilotReplyLifecycle\b/,
    ]) {
      expect(appShell, 'AppShell must consume surface state rather than own a duplicate').not.toMatch(duplicateState);
    }
  });

  it('does not add mobile runtime dependencies', () => {
    const root = repoRoot();
    const packageJson = JSON.parse(readFileSync(join(root, 'web/package.json'), 'utf8')) as {
      dependencies?: Record<string, string>;
      devDependencies?: Record<string, string>;
    };
    const dependencyNames = [
      ...Object.keys(packageJson.dependencies ?? {}),
      ...Object.keys(packageJson.devDependencies ?? {}),
    ];
    expect(dependencyNames.filter((name) => (
      name === 'react-native'
      || name === 'react-native-web'
      || name.startsWith('@capacitor/')
    ))).toEqual([]);
  });
});
