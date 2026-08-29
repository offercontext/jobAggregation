import { execFileSync } from 'node:child_process';
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const BASELINE = '93fb0063118761f2c76e71e4209000feee0f755b';
const ASSET_NAMES = [
  'core_task_entrypoints_93fb006.json',
  'core_task_request_counts_93fb006.json',
  'core_task_visible_copy_93fb006.json',
  'interview_index_api_93fb006.json',
] as const;

type Asset = {
  schema_version: number;
  source_baseline: string;
  items: unknown[];
};

function repositoryRoot(): string {
  return execFileSync('git', ['rev-parse', '--show-toplevel'], {
    encoding: 'utf8',
  }).trim();
}

function readAssets(root: string): { assets: Map<string, Asset>; violations: string[] } {
  const assets = new Map<string, Asset>();
  const violations: string[] = [];
  for (const name of ASSET_NAMES) {
    const path = join(root, 'tests', 'fixtures', 'core_task_surface', name);
    try {
      const parsed: unknown = JSON.parse(readFileSync(path, 'utf8'));
      if (
        typeof parsed !== 'object'
        || parsed === null
        || !Array.isArray((parsed as { items?: unknown }).items)
        || (parsed as { schema_version?: unknown }).schema_version !== 1
        || (parsed as { source_baseline?: unknown }).source_baseline !== BASELINE
      ) {
        violations.push(`assets:invalid-envelope:${name}`);
        continue;
      }
      assets.set(name, parsed as Asset);
    } catch {
      violations.push(`assets:unreadable:${name}`);
    }
  }
  return { assets, violations };
}

function hasTypeScriptFile(path: string): boolean {
  return existsSync(path)
    && readdirSync(path, { withFileTypes: true }).some(
      (entry) => entry.isFile() && /\.tsx?$/.test(entry.name),
    );
}

function missingSurfaceViolations(root: string): string[] {
  const violations: string[] = [];
  const surfaceRoot = join(root, 'web', 'src', 'features', 'coreTaskSurface');

  // Keep registry first so an unimplemented baseline fails with a stable,
  // actionable name rather than an incidental filesystem or import error.
  if (!existsSync(join(surfaceRoot, 'registry.ts'))) violations.push('registry:missing-owner');
  if (!existsSync(join(surfaceRoot, 'contracts.ts'))) violations.push('contracts:missing-core-task-id');
  if (!existsSync(join(surfaceRoot, 'controller.ts')) && !existsSync(join(surfaceRoot, 'controller.tsx'))) {
    violations.push('controller:missing-owner');
  }
  if (!hasTypeScriptFile(join(root, 'web', 'src', 'features', 'interviewEvents'))) {
    violations.push('interview:missing-event-classifier');
  }
  if (!hasTypeScriptFile(join(root, 'web', 'src', 'features', 'materialSurfaces'))) {
    violations.push('materials:missing-source-mapper');
  }
  return violations;
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

  it('requires the canonical registry, owner, event classifier, and materials mapper', () => {
    const root = repositoryRoot();
    const { violations: assetViolations } = readAssets(root);
    const violations = [...assetViolations, ...missingSurfaceViolations(root)];
    // Intentional RED at the captured baseline.  The implementation batch may
    // turn this into PASS only after every named canonical surface exists.
    expect(violations).toEqual([]);
  });
});
