import { execFileSync } from 'node:child_process';
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { join, relative } from 'node:path';
import { describe, expect, it } from 'vitest';

const BASELINE = '93fb0063118761f2c76e71e4209000feee0f755b';
const ASSET_NAMES = [
  'core_task_entrypoints_93fb006.json',
  'core_task_request_counts_93fb006.json',
  'core_task_visible_copy_93fb006.json',
  'interview_index_api_93fb006.json',
] as const;
const CORE_TASK_IDS = [
  'application.opportunity_fit',
  'application.material_kit',
  'application.interview_prepare',
  'application.interview_review',
  'application.general_review',
  'application.offer_review',
  'application.record_outcome',
  'interview.free_practice',
  'materials.resume',
  'materials.story',
  'materials.reference',
] as const;
const ENTRYPOINT_CATEGORIES = new Set(['core_task', 'navigation_only', 'record_management']);

type Asset = {
  schema_version: number;
  source_baseline: string;
  items: unknown[];
};

type Entrypoint = {
  file: string;
  qualified_symbol: string;
  category: string;
  task_id: string | null;
};

type VisibleCopy = {
  file: string;
  lexeme: string;
  replacement: string;
};

type AuditSources = {
  productionFiles: Map<string, string>;
  registrySource: string | null;
  contractsSource: string | null;
  controllerSource: string | null;
};

function repositoryRoot(): string {
  return execFileSync('git', ['rev-parse', '--show-toplevel'], {
    encoding: 'utf8',
  }).trim();
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function readAssets(root: string): { assets: Map<string, Asset>; violations: string[] } {
  const assets = new Map<string, Asset>();
  const violations: string[] = [];
  for (const name of ASSET_NAMES) {
    const path = join(root, 'tests', 'fixtures', 'core_task_surface', name);
    try {
      const parsed: unknown = JSON.parse(readFileSync(path, 'utf8'));
      if (
        !isRecord(parsed)
        || !Array.isArray(parsed.items)
        || parsed.schema_version !== 1
        || parsed.source_baseline !== BASELINE
      ) {
        violations.push(`assets:invalid-envelope:${name}`);
        continue;
      }
      assets.set(name, parsed as unknown as Asset);
    } catch {
      violations.push(`assets:unreadable:${name}`);
    }
  }
  return { assets, violations };
}

function entrypointsFromAsset(asset: Asset | undefined): Entrypoint[] {
  return (asset?.items ?? []).flatMap((value) => {
    if (!isRecord(value)) return [];
    return [{
      file: typeof value.file === 'string' ? value.file : '',
      qualified_symbol: typeof value.qualified_symbol === 'string' ? value.qualified_symbol : '',
      category: typeof value.category === 'string' ? value.category : '',
      task_id: typeof value.task_id === 'string' || value.task_id === null ? value.task_id : null,
    }];
  });
}

function visibleCopyFromAsset(asset: Asset | undefined): VisibleCopy[] {
  return (asset?.items ?? []).flatMap((value) => {
    if (!isRecord(value)) return [];
    return [{
      file: typeof value.file === 'string' ? value.file : '',
      lexeme: typeof value.lexeme === 'string' ? value.lexeme : '',
      replacement: typeof value.replacement === 'string' ? value.replacement : '',
    }];
  });
}

function collectProductionFiles(root: string): Map<string, string> {
  const result = new Map<string, string>();
  const sourceRoot = join(root, 'web', 'src');
  const visit = (directory: string): void => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) {
        visit(path);
        continue;
      }
      if (!/\.tsx?$/.test(entry.name) || /\.test\.tsx?$/.test(entry.name)) continue;
      try {
        const relativePath = relative(root, path).replace(/\\/g, '/');
        result.set(relativePath, readFileSync(path, 'utf8'));
      } catch {
        // A deleted/temporarily unreadable source is audited as absent below.
      }
    }
  };
  if (existsSync(sourceRoot)) visit(sourceRoot);
  return result;
}

function readBaselineFile(root: string, file: string): string | null {
  try {
    return execFileSync('git', ['show', `${BASELINE}:${file}`], {
      cwd: root,
      encoding: 'utf8',
    });
  } catch {
    return null;
  }
}

function symbolLeaf(qualifiedSymbol: string): string {
  return qualifiedSymbol.split('.').at(-1) ?? '';
}

function containsSymbol(source: string | null, symbol: string): boolean {
  if (!source || !symbol) return false;
  return new RegExp(`\\b${symbol.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\$&')}\\b`).test(source);
}

function registryHasCanonicalOwner(source: string | null, entries: Entrypoint[]): boolean {
  if (!source || !/(?:CoreTaskRegistryV1|CORE_TASK_REGISTRY|coreTaskRegistry)/.test(source)) {
    return false;
  }
  return CORE_TASK_IDS.every((taskId) => source.includes(taskId))
    && entries.some((entry) => entry.category === 'navigation_only' && source.includes(entry.category))
    && entries.some((entry) => entry.category === 'record_management' && source.includes(entry.category));
}

function hasCoreTaskContracts(source: string | null): boolean {
  if (!source) return false;
  const idDeclaration = source.match(/\b(?:export\s+)?type\s+CoreTaskId\s*=([\s\S]*?);/);
  const idBody = idDeclaration?.[1] ?? '';
  const declaredIds = [...idBody.matchAll(/['"]([^'"]+)['"]/g)].map((match) => match[1]);
  const declaredIdSet = new Set(declaredIds);
  const hasClosedIdDeclaration = Boolean(idDeclaration)
    && declaredIds.length === CORE_TASK_IDS.length
    && declaredIdSet.size === CORE_TASK_IDS.length
    && declaredIds.every((taskId) => CORE_TASK_IDS.includes(taskId as typeof CORE_TASK_IDS[number]))
    && CORE_TASK_IDS.every((taskId) => source.includes(taskId));
  const parserStart = source.search(/\bparseCoreTaskRef\s*(?:=|\(|:)/);
  const parserBody = parserStart >= 0 ? source.slice(parserStart, parserStart + 900) : '';
  const hasParser = parserStart >= 0 && /\bCoreTaskRef\b/.test(parserBody);
  const keyStart = source.search(/\b(?:canonicalTaskKey|canonicalCoreTaskKey|coreTaskRefKey)\s*(?:=|\(|:)/);
  const keyBody = keyStart >= 0 ? source.slice(keyStart, keyStart + 900) : '';
  const hasCanonicalKey = keyStart >= 0
    && /\b(?:taskId|applicationId|eventId|offerId|resumeId|storyId|sourceId)\b/.test(keyBody);
  return hasClosedIdDeclaration && hasParser && hasCanonicalKey;
}

function hasCoreTaskController(source: string | null): boolean {
  if (!source) return false;
  const hasControllerExport = /\bexport\s+(?:function|class|const)\s+(?:CoreTaskSurfaceController|createCoreTaskSurfaceController|useCoreTaskSurfaceController)\b/.test(source);
  const hasOwnerGenerationState = /\b(?:ownerGeneration|owner_generation|generation)(?:Ref)?\b\s*(?::[^=;]+)?=/.test(source)
    && /\b(?:useRef|useState|Map|Set|owner)\b/.test(source);
  return hasControllerExport && hasOwnerGenerationState;
}

function entrypointHasAudit(
  root: string,
  entry: Entrypoint,
  sources: AuditSources,
): boolean {
  if (!entry.file.startsWith('web/src/') || !entry.qualified_symbol) return false;
  if (!ENTRYPOINT_CATEGORIES.has(entry.category)) return false;
  if (entry.category === 'core_task' && !CORE_TASK_IDS.includes(entry.task_id as typeof CORE_TASK_IDS[number])) {
    return false;
  }
  if (entry.category !== 'core_task' && entry.task_id !== null) return false;
  const leaf = symbolLeaf(entry.qualified_symbol);
  const baselineSource = readBaselineFile(root, entry.file);
  if (!containsSymbol(baselineSource, leaf)) return false;

  const currentSource = sources.productionFiles.get(entry.file) ?? null;
  const migratedSymbol = containsSymbol(currentSource, leaf)
    || Boolean(sources.registrySource?.includes(entry.qualified_symbol));
  if (!migratedSymbol || !sources.registrySource) return false;
  if (entry.category === 'core_task') {
    return Boolean(entry.task_id && sources.registrySource.includes(entry.task_id));
  }
  return sources.registrySource.includes(entry.category)
    && (sources.registrySource.includes(entry.qualified_symbol) || sources.registrySource.includes(entry.file));
}

function hasCentralEventClassifier(productionFiles: Map<string, string>): boolean {
  for (const [path, source] of productionFiles) {
    if (!path.includes('/features/interviewEvents/')) continue;
    if (!/(?:eventLifecycle|lifecycle|classifier)/i.test(path)) continue;
    if (!source.includes('EventLifecycleV1')) continue;
    if (!/\b(?:classify|project|resolve)[A-Za-z]*Event[A-Za-z]*\b/.test(source)) continue;
    return true;
  }
  return false;
}

function hasLocalEventClassifier(productionFiles: Map<string, string>): boolean {
  const localPaths = [
    'web/src/components/ApplicationDetail.tsx',
    'web/src/components/InterviewV01View.tsx',
    'web/src/features/interviewReadiness/InterviewReadinessCenter.tsx',
    'web/src/layout/AppShell.tsx',
  ];
  const localMarkers = [
    'ENDED_EVENT_STATUSES',
    'TERMINAL_EVENT_STATUSES',
    'isUpcomingInterview',
    'scheduledTimestamp',
  ];
  return localPaths.some((path) => {
    const source = productionFiles.get(path) ?? '';
    return localMarkers.some((marker) => source.includes(marker));
  });
}

function hasCentralMaterialMapper(productionFiles: Map<string, string>): boolean {
  for (const [path, source] of productionFiles) {
    if (!path.includes('/features/materialSurfaces/')) continue;
    if (!/(?:mapper|classifier|projector|lineage)/i.test(path)) continue;
    if (!/(?:classify|project|resolve)[A-Za-z]*(?:Material|Source|Resume)/.test(source)) continue;
    if (!/(?:origin_kind|source_kind|lineage|MaterialSource)/.test(source)) continue;
    return true;
  }
  return false;
}

function auditManifest(
  root: string,
  entries: Entrypoint[],
  visibleCopy: VisibleCopy[],
  sources: AuditSources,
): string[] {
  const violations: string[] = [];
  if (!registryHasCanonicalOwner(sources.registrySource, entries)) {
    violations.push('registry:missing-owner');
  }
  if (!hasCoreTaskContracts(sources.contractsSource)) {
    violations.push('contracts:missing-core-task-id');
  }
  if (!hasCoreTaskController(sources.controllerSource)) {
    violations.push('controller:missing-owner');
  }
  const entrypointAuditResults = entries.map((entry) => entrypointHasAudit(root, entry, sources));
  if (entrypointAuditResults.some((audited) => !audited)) {
    violations.push('entrypoint:unclassified');
  }

  const centralEventClassifier = hasCentralEventClassifier(sources.productionFiles);
  if (!centralEventClassifier || hasLocalEventClassifier(sources.productionFiles)) {
    violations.push('event:local-classifier');
  }

  const productionText = [...sources.productionFiles.values()].join('\n');
  const copyAuditResults = visibleCopy.map((item) => !item.lexeme || productionText.includes(item.lexeme));
  if (copyAuditResults.some((forbidden) => forbidden)) {
    violations.push('copy:forbidden-lexeme');
  }

  if (!hasCentralMaterialMapper(sources.productionFiles)) {
    violations.push('materials:missing-source-mapper');
  }
  return [...new Set(violations)];
}

describe('core task surface baseline gate', () => {
  it('reads every fixed baseline asset through the shared envelope', () => {
    const root = repositoryRoot();
    const { assets, violations } = readAssets(root);
    expect(violations).toEqual([]);
    expect([...assets.keys()].sort()).toEqual([...ASSET_NAMES].sort());
    for (const asset of assets.values()) {
      expect(asset.schema_version).toBe(1);
      expect(asset.source_baseline).toBe(BASELINE);
      expect(Array.isArray(asset.items)).toBe(true);
    }
    expect(execFileSync('git', ['cat-file', '-e', BASELINE], { cwd: root })).toBeDefined();
  });

  it('does not let unrelated files satisfy the named manifest violations', () => {
    const root = repositoryRoot();
    const { assets } = readAssets(root);
    const entries = entrypointsFromAsset(assets.get(ASSET_NAMES[0]));
    const visibleCopy = visibleCopyFromAsset(assets.get(ASSET_NAMES[2]));
    const violations = auditManifest(root, entries, visibleCopy, {
      registrySource: [
        'CoreTaskRegistryV1',
        ...CORE_TASK_IDS,
        'navigation_only',
        'record_management',
      ].join(' '),
      contractsSource: 'export const unrelatedContracts = true;',
      controllerSource: 'export const unrelatedController = true;',
      productionFiles: new Map([
        ['web/src/features/coreTaskSurface/unrelated.ts', 'export const unrelated = true;'],
        ['web/src/features/interviewEvents/unrelated.ts', 'export const unrelated = true;'],
        ['web/src/features/materialSurfaces/unrelated.ts', 'export const unrelated = true;'],
        ['web/src/unrelated.ts', 'const oldCopy = "旧版评估";'],
      ]),
    });
    expect(violations).not.toContain('registry:missing-owner');
    expect(violations).toEqual(expect.arrayContaining([
      'entrypoint:unclassified',
      'event:local-classifier',
      'copy:forbidden-lexeme',
      'materials:missing-source-mapper',
      'contracts:missing-core-task-id',
      'controller:missing-owner',
    ]));
  });

  it('requires the canonical registry, audited entrypoints, classifier, and copy migration', () => {
    const root = repositoryRoot();
    const { assets, violations: assetViolations } = readAssets(root);
    const violations = auditManifest(
      root,
      entrypointsFromAsset(assets.get(ASSET_NAMES[0])),
      visibleCopyFromAsset(assets.get(ASSET_NAMES[2])),
      {
        registrySource: (() => {
          const path = join(root, 'web', 'src', 'features', 'coreTaskSurface', 'registry.ts');
          return existsSync(path) ? readFileSync(path, 'utf8') : null;
        })(),
        contractsSource: (() => {
          const path = join(root, 'web', 'src', 'features', 'coreTaskSurface', 'contracts.ts');
          return existsSync(path) ? readFileSync(path, 'utf8') : null;
        })(),
        controllerSource: (() => {
          const candidates = ['controller.ts', 'controller.tsx'];
          const path = candidates
            .map((name) => join(root, 'web', 'src', 'features', 'coreTaskSurface', name))
            .find((candidate) => existsSync(candidate));
          return path ? readFileSync(path, 'utf8') : null;
        })(),
        productionFiles: collectProductionFiles(root),
      },
    );
    // Intentional RED at the captured baseline.  The implementation batch may
    // turn this into PASS only after every named canonical surface exists.
    expect([...assetViolations, ...violations]).toEqual([]);
  });
});
