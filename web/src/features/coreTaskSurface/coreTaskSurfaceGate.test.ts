import { execFileSync } from 'node:child_process';
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { join, relative } from 'node:path';
import ts from 'typescript';
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
const CANONICAL_LAUNCH_NAMES = new Set([
  'launchCoreTask',
  'launchCoreTaskSurface',
  'openCoreTask',
  'openCoreTaskSurface',
  'openTaskSurface',
]);

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

type SourceArtifact = {
  path: string;
  text: string;
  sourceFile: ts.SourceFile;
};

type AuditSources = {
  productionFiles: Map<string, string>;
  registrySource: SourceArtifact | null;
  contractsSource: SourceArtifact | null;
  controllerSource: SourceArtifact | null;
  allowDeletedEntrypointCutover: boolean;
};

const baselineSourceCache = new Map<string, SourceArtifact | null>();

function repositoryRoot(): string {
  return execFileSync('git', ['rev-parse', '--show-toplevel'], {
    encoding: 'utf8',
  }).trim();
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function parseSource(path: string, text: string): SourceArtifact {
  const scriptKind = /\.tsx$/i.test(path) ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  return {
    path,
    text,
    sourceFile: ts.createSourceFile(path, text, ts.ScriptTarget.Latest, true, scriptKind),
  };
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

function artifactFromText(path: string, text: string): SourceArtifact {
  return parseSource(path, text);
}

function artifactFromProduction(
  productionFiles: Map<string, string>,
  path: string,
): SourceArtifact | null {
  const text = productionFiles.get(path);
  return text === undefined ? null : parseSource(path, text);
}

function readSourceArtifact(root: string, paths: string[]): SourceArtifact | null {
  for (const relativePath of paths) {
    const absolutePath = join(root, relativePath);
    if (!existsSync(absolutePath)) continue;
    try {
      return parseSource(relativePath, readFileSync(absolutePath, 'utf8'));
    } catch {
      return null;
    }
  }
  return null;
}

function readBaselineFile(root: string, file: string): SourceArtifact | null {
  const cacheKey = `${root}:${file}`;
  if (baselineSourceCache.has(cacheKey)) return baselineSourceCache.get(cacheKey) ?? null;
  let artifact: SourceArtifact | null = null;
  try {
    const text = execFileSync('git', ['show', `${BASELINE}:${file}`], {
      cwd: root,
      encoding: 'utf8',
    });
    artifact = parseSource(file, text);
  } catch {
    artifact = null;
  }
  baselineSourceCache.set(cacheKey, artifact);
  return artifact;
}

function hasExportModifier(node: ts.Node): boolean {
  if (!ts.canHaveModifiers(node)) return false;
  return ts.getModifiers(node)?.some((modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword) ?? false;
}

type FunctionLikeWithBody =
  | ts.FunctionDeclaration
  | ts.FunctionExpression
  | ts.ArrowFunction
  | ts.MethodDeclaration
  | ts.GetAccessorDeclaration
  | ts.SetAccessorDeclaration;

function isFunctionLikeWithBody(node: ts.Node): node is FunctionLikeWithBody {
  return ts.isFunctionDeclaration(node)
    || ts.isFunctionExpression(node)
    || ts.isArrowFunction(node)
    || ts.isMethodDeclaration(node)
    || ts.isGetAccessorDeclaration(node)
    || ts.isSetAccessorDeclaration(node);
}

function hasImplementedBody(node: FunctionLikeWithBody): boolean {
  if (!node.body) return false;
  return !ts.isBlock(node.body) || node.body.statements.length > 0;
}

function functionLikeName(node: FunctionLikeWithBody): string | null {
  if (!node.name || !ts.isIdentifier(node.name)) return null;
  return node.name.text;
}

function variableFunctionLike(
  declaration: ts.VariableDeclaration,
): FunctionLikeWithBody | null {
  if (!declaration.initializer || !isFunctionLikeWithBody(declaration.initializer)) return null;
  return declaration.initializer;
}

function findExportedFunction(
  sourceFile: ts.SourceFile,
  name: string,
): FunctionLikeWithBody | null {
  for (const statement of sourceFile.statements) {
    if (
      isFunctionLikeWithBody(statement)
      && hasExportModifier(statement)
      && functionLikeName(statement) === name
      && hasImplementedBody(statement)
    ) {
      return statement;
    }
    if (!ts.isVariableStatement(statement) || !hasExportModifier(statement)) continue;
    for (const declaration of statement.declarationList.declarations) {
      if (!ts.isIdentifier(declaration.name) || declaration.name.text !== name) continue;
      const functionLike = variableFunctionLike(declaration);
      if (functionLike && hasImplementedBody(functionLike)) return functionLike;
    }
  }
  return null;
}

function findFunctionByLeaf(sourceFile: ts.SourceFile, name: string): FunctionLikeWithBody | null {
  let found: FunctionLikeWithBody | null = null;
  const visit = (node: ts.Node): void => {
    if (found) return;
    if (
      isFunctionLikeWithBody(node)
      && functionLikeName(node) === name
      && hasImplementedBody(node)
    ) {
      found = node;
      return;
    }
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.name.text === name) {
      const functionLike = variableFunctionLike(node);
      if (functionLike && hasImplementedBody(functionLike)) {
        found = functionLike;
        return;
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return found;
}

function calleeName(expression: ts.Expression): string | null {
  if (ts.isIdentifier(expression)) return expression.text;
  if (ts.isPropertyAccessExpression(expression)) return expression.name.text;
  return null;
}

function callsCanonicalLauncher(functionLike: FunctionLikeWithBody): boolean {
  if (!functionLike.body) return false;
  let found = false;
  const visit = (node: ts.Node): void => {
    if (found) return;
    if (ts.isCallExpression(node) && CANONICAL_LAUNCH_NAMES.has(calleeName(node.expression) ?? '')) {
      found = true;
      return;
    }
    ts.forEachChild(node, visit);
  };
  ts.forEachChild(functionLike.body, visit);
  return found;
}

function findExportedTypeAlias(sourceFile: ts.SourceFile, name: string): ts.TypeAliasDeclaration | null {
  return sourceFile.statements.find(
    (statement): statement is ts.TypeAliasDeclaration => (
      ts.isTypeAliasDeclaration(statement)
      && statement.name.text === name
      && hasExportModifier(statement)
    ),
  ) ?? null;
}

function unwrapType(node: ts.TypeNode): ts.TypeNode {
  let current = node;
  while (ts.isParenthesizedTypeNode(current)) current = current.type;
  return current;
}

function coreTaskIdUnion(sourceFile: ts.SourceFile): string[] | null {
  const alias = findExportedTypeAlias(sourceFile, 'CoreTaskId');
  if (!alias) return null;
  const type = unwrapType(alias.type);
  if (!ts.isUnionTypeNode(type)) return null;
  const values: string[] = [];
  for (const member of type.types) {
    if (!ts.isLiteralTypeNode(member) || !ts.isStringLiteral(member.literal)) return null;
    values.push(member.literal.text);
  }
  return values;
}

function exactCoreTaskIdSet(values: string[] | null): boolean {
  if (!values || values.length !== CORE_TASK_IDS.length) return false;
  const actual = new Set(values);
  return actual.size === CORE_TASK_IDS.length
    && CORE_TASK_IDS.every((taskId) => actual.has(taskId));
}

function hasCoreTaskContracts(artifact: SourceArtifact | null): boolean {
  if (!artifact || !exactCoreTaskIdSet(coreTaskIdUnion(artifact.sourceFile))) return false;
  return Boolean(
    findExportedFunction(artifact.sourceFile, 'parseCoreTaskRef')
    && findExportedFunction(artifact.sourceFile, 'coreTaskCanonicalKey'),
  );
}

function findExportedConstInitializer(
  sourceFile: ts.SourceFile,
  name: string,
): ts.Expression | null {
  for (const statement of sourceFile.statements) {
    if (!ts.isVariableStatement(statement) || !hasExportModifier(statement)) continue;
    if ((statement.declarationList.flags & ts.NodeFlags.Const) === 0) continue;
    for (const declaration of statement.declarationList.declarations) {
      if (ts.isIdentifier(declaration.name) && declaration.name.text === name) {
        return declaration.initializer ?? null;
      }
    }
  }
  return null;
}

function unwrapExpression(expression: ts.Expression): ts.Expression {
  let current = expression;
  for (;;) {
    if (ts.isParenthesizedExpression(current)) {
      current = current.expression;
      continue;
    }
    if (ts.isAsExpression(current) || ts.isTypeAssertionExpression(current) || ts.isSatisfiesExpression(current)) {
      current = current.expression;
      continue;
    }
    if (ts.isNonNullExpression(current)) {
      current = current.expression;
      continue;
    }
    if (
      ts.isCallExpression(current)
      && ts.isPropertyAccessExpression(current.expression)
      && ts.isIdentifier(current.expression.expression)
      && current.expression.expression.text === 'Object'
      && current.expression.name.text === 'freeze'
      && current.arguments.length === 1
    ) {
      current = current.arguments[0];
      continue;
    }
    return current;
  }
}

function propertyNameText(name: ts.PropertyName): string | null {
  if (ts.isIdentifier(name) || ts.isStringLiteral(name) || ts.isNoSubstitutionTemplateLiteral(name)) {
    return name.text;
  }
  return null;
}

function objectProperties(object: ts.ObjectLiteralExpression): Map<string, ts.PropertyAssignment> | null {
  const properties = new Map<string, ts.PropertyAssignment>();
  for (const property of object.properties) {
    if (!ts.isPropertyAssignment(property)) return null;
    const name = propertyNameText(property.name);
    if (!name || properties.has(name)) return null;
    properties.set(name, property);
  }
  return properties;
}

function objectLiteralFromExpression(expression: ts.Expression): ts.ObjectLiteralExpression | null {
  const unwrapped = unwrapExpression(expression);
  return ts.isObjectLiteralExpression(unwrapped) ? unwrapped : null;
}

function literalOwnerId(value: ts.Expression): string | null {
  const unwrapped = unwrapExpression(value);
  if (!ts.isStringLiteral(unwrapped) || !unwrapped.text.trim()) return null;
  return unwrapped.text;
}

function hasCanonicalRegistry(artifact: SourceArtifact | null): boolean {
  if (!artifact) return false;
  const initializer = findExportedConstInitializer(artifact.sourceFile, 'CORE_TASK_REGISTRY');
  if (!initializer) return false;
  const registry = objectLiteralFromExpression(initializer);
  if (!registry) return false;
  const properties = objectProperties(registry);
  if (!properties || properties.size !== CORE_TASK_IDS.length) return false;
  const keys = [...properties.keys()];
  const expected = new Set<string>(CORE_TASK_IDS);
  if (keys.some((key) => !expected.has(key))) return false;

  const ownerIds = new Set<string>();
  for (const taskId of CORE_TASK_IDS) {
    const property = properties.get(taskId);
    if (!property) return false;
    const ownerRecord = objectLiteralFromExpression(property.initializer);
    if (!ownerRecord) return false;
    const ownerProperties = objectProperties(ownerRecord);
    const ownerProperty = ownerProperties?.get('ownerId');
    const ownerId = ownerProperty ? literalOwnerId(ownerProperty.initializer) : null;
    if (!ownerId || ownerIds.has(ownerId)) return false;
    ownerIds.add(ownerId);
  }
  return ownerIds.size === CORE_TASK_IDS.length;
}

function exportedStateHasFields(sourceFile: ts.SourceFile): boolean {
  for (const statement of sourceFile.statements) {
    if (!hasExportModifier(statement)) continue;
    let members: ts.NodeArray<ts.TypeElement> | undefined;
    let declarationName: string | null = null;
    if (ts.isInterfaceDeclaration(statement)) {
      declarationName = statement.name.text;
      members = statement.members;
    } else if (ts.isTypeAliasDeclaration(statement)) {
      declarationName = statement.name.text;
      const type = unwrapType(statement.type);
      if (ts.isTypeLiteralNode(type)) members = type.members;
    }
    if (!declarationName || !/^CoreTask.*State$/.test(declarationName) || !members) continue;
    const names = new Set(
      members.flatMap((member) => {
        if (!ts.isPropertySignature(member) || !member.name) return [];
        const name = propertyNameText(member.name);
        return name ? [name] : [];
      }),
    );
    if (['phase', 'generation', 'active'].every((name) => names.has(name))) return true;
  }
  return false;
}

function hasCoreTaskController(artifact: SourceArtifact | null): boolean {
  return Boolean(
    artifact
    && findExportedFunction(artifact.sourceFile, 'createCoreTaskSurfaceController')
    && exportedStateHasFields(artifact.sourceFile),
  );
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

  const parts = entry.qualified_symbol.split('.');
  const leaf = parts[parts.length - 1] ?? '';
  const baseline = readBaselineFile(root, entry.file);
  if (!baseline || !findFunctionByLeaf(baseline.sourceFile, leaf)) return false;

  const current = artifactFromProduction(sources.productionFiles, entry.file);
  if (!current) return sources.allowDeletedEntrypointCutover;
  const currentFunction = findFunctionByLeaf(current.sourceFile, leaf);
  if (!currentFunction) return sources.allowDeletedEntrypointCutover;
  if (entry.category !== 'core_task') return true;
  return callsCanonicalLauncher(currentFunction);
}

function hasIdentifier(sourceFile: ts.SourceFile, name: string): boolean {
  let found = false;
  const visit = (node: ts.Node): void => {
    if (found) return;
    if (ts.isIdentifier(node) && node.text === name) {
      found = true;
      return;
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return found;
}

function hasExportedFunctionMatching(
  sourceFile: ts.SourceFile,
  pattern: RegExp,
): boolean {
  for (const statement of sourceFile.statements) {
    if (
      isFunctionLikeWithBody(statement)
      && hasExportModifier(statement)
      && hasImplementedBody(statement)
      && pattern.test(functionLikeName(statement) ?? '')
    ) return true;
    if (!ts.isVariableStatement(statement) || !hasExportModifier(statement)) continue;
    for (const declaration of statement.declarationList.declarations) {
      if (!ts.isIdentifier(declaration.name) || !pattern.test(declaration.name.text)) continue;
      const functionLike = variableFunctionLike(declaration);
      if (functionLike && hasImplementedBody(functionLike)) return true;
    }
  }
  return false;
}

function hasCentralEventClassifier(productionFiles: Map<string, string>): boolean {
  for (const [path, source] of productionFiles) {
    if (!path.includes('/features/interviewEvents/')) continue;
    if (!/(?:eventLifecycle|lifecycle|classifier)/i.test(path)) continue;
    const artifact = parseSource(path, source);
    if (!hasIdentifier(artifact.sourceFile, 'EventLifecycleV1')) continue;
    if (!hasExportedFunctionMatching(artifact.sourceFile, /^(?:classify|project|resolve).*Event/i)) continue;
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
    const artifact = artifactFromProduction(productionFiles, path);
    return Boolean(artifact && localMarkers.some((marker) => hasIdentifier(artifact.sourceFile, marker)));
  });
}

function hasCentralMaterialMapper(productionFiles: Map<string, string>): boolean {
  for (const [path, source] of productionFiles) {
    if (!path.includes('/features/materialSurfaces/')) continue;
    if (!/(?:mapper|classifier|projector|lineage)/i.test(path)) continue;
    const artifact = parseSource(path, source);
    if (!hasExportedFunctionMatching(artifact.sourceFile, /^(?:classify|project|resolve|map).*(?:Material|Source|Resume)/i)) continue;
    if (!['MaterialSource', 'ResumeLineageV1', 'materialSource', 'origin_kind', 'source_kind']
      .some((name) => hasIdentifier(artifact.sourceFile, name))) continue;
    return true;
  }
  return false;
}

function sourceHasVisibleText(sourceFile: ts.SourceFile, needle: string): boolean {
  let found = false;
  const visit = (node: ts.Node): void => {
    if (found) return;
    if (
      (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node))
      && node.text.includes(needle)
    ) {
      found = true;
      return;
    }
    if (ts.isJsxText(node) && node.getText(sourceFile).includes(needle)) {
      found = true;
      return;
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return found;
}

function auditManifest(
  root: string,
  entries: Entrypoint[],
  visibleCopy: VisibleCopy[],
  sources: AuditSources,
): string[] {
  const violations: string[] = [];
  if (!hasCanonicalRegistry(sources.registrySource)) violations.push('registry:missing-owner');
  if (!hasCoreTaskContracts(sources.contractsSource)) violations.push('contracts:missing-core-task-id');
  if (!hasCoreTaskController(sources.controllerSource)) violations.push('controller:missing-owner');

  const entrypointAuditResults = entries.map((entry) => entrypointHasAudit(root, entry, sources));
  if (entrypointAuditResults.some((audited) => !audited)) violations.push('entrypoint:unclassified');

  const centralEventClassifier = hasCentralEventClassifier(sources.productionFiles);
  if (!centralEventClassifier || hasLocalEventClassifier(sources.productionFiles)) {
    violations.push('event:local-classifier');
  }

  const productionArtifacts = [...sources.productionFiles.entries()].map(([path, source]) => parseSource(path, source));
  const copyAuditResults = visibleCopy.map((item) => (
    !item.lexeme || productionArtifacts.some((artifact) => sourceHasVisibleText(artifact.sourceFile, item.lexeme))
  ));
  if (copyAuditResults.some((forbidden) => forbidden)) violations.push('copy:forbidden-lexeme');

  if (!hasCentralMaterialMapper(sources.productionFiles)) violations.push('materials:missing-source-mapper');
  return [...new Set(violations)];
}

function realAuditSources(root: string): AuditSources {
  return {
    productionFiles: collectProductionFiles(root),
    registrySource: readSourceArtifact(root, ['web/src/features/coreTaskSurface/registry.ts']),
    contractsSource: readSourceArtifact(root, ['web/src/features/coreTaskSurface/contracts.ts']),
    controllerSource: readSourceArtifact(root, [
      'web/src/features/coreTaskSurface/controller.ts',
      'web/src/features/coreTaskSurface/controller.tsx',
    ]),
    allowDeletedEntrypointCutover: true,
  };
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

  it('rejects comment/string pseudo surfaces and unrelated fixture files', () => {
    const root = repositoryRoot();
    const { assets } = readAssets(root);
    const entries = entrypointsFromAsset(assets.get(ASSET_NAMES[0]));
    const visibleCopy = visibleCopyFromAsset(assets.get(ASSET_NAMES[2]));
    const fakeText = [
      ...CORE_TASK_IDS,
      ...entries.map((entry) => entry.qualified_symbol),
      'core_task',
      'navigation_only',
      'record_management',
      'parseCoreTaskRef',
      'coreTaskCanonicalKey',
      'createCoreTaskSurfaceController',
      'phase generation active',
    ].join(' ');
    const violations = auditManifest(root, entries, visibleCopy, {
      registrySource: artifactFromText(
        'web/src/features/coreTaskSurface/registry.ts',
        `/* ${fakeText} */ export const CORE_TASK_REGISTRY = {};`,
      ),
      contractsSource: artifactFromText(
        'web/src/features/coreTaskSurface/contracts.ts',
        `/* ${fakeText} */\nexport const fake = '${fakeText}';\ndeclare function parseCoreTaskRef(): unknown;\nexport function parseCoreTaskRef() {}\nexport function coreTaskCanonicalKey() {}`,
      ),
      controllerSource: artifactFromText(
        'web/src/features/coreTaskSurface/controller.ts',
        `/* ${fakeText} */\nexport interface FakeState { phase: string; generation: number; active: boolean }\nexport declare function createCoreTaskSurfaceController(): unknown;\nexport function createCoreTaskSurfaceController() {}`,
      ),
      productionFiles: new Map([
        ['web/src/features/coreTaskSurface/unrelated.ts', `/* ${fakeText} */`],
        ['web/src/features/interviewEvents/unrelated.ts', `const fake = '${fakeText}';`],
        ['web/src/features/materialSurfaces/unrelated.ts', `const fake = '${fakeText}';`],
        ['web/src/unrelated.ts', 'const oldCopy = "旧版评估";'],
      ]),
      allowDeletedEntrypointCutover: false,
    });
    expect(violations).toEqual(expect.arrayContaining([
      'registry:missing-owner',
      'contracts:missing-core-task-id',
      'controller:missing-owner',
      'entrypoint:unclassified',
      'event:local-classifier',
      'copy:forbidden-lexeme',
      'materials:missing-source-mapper',
    ]));
  });

  it('requires the canonical AST surfaces, audited entrypoints, classifier, and copy migration', () => {
    const root = repositoryRoot();
    const { assets, violations: assetViolations } = readAssets(root);
    const violations = auditManifest(
      root,
      entrypointsFromAsset(assets.get(ASSET_NAMES[0])),
      visibleCopyFromAsset(assets.get(ASSET_NAMES[2])),
      realAuditSources(root),
    );
    // Intentional RED at the captured baseline.  The implementation batch may
    // turn this into PASS only after every named canonical surface exists.
    expect([...assetViolations, ...violations]).toEqual([]);
  });
});
