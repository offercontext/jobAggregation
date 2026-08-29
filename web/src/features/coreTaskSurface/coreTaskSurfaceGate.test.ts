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
const ENVELOPE_KEYS = new Set(['schema_version', 'source_baseline', 'items']);
const ENTRYPOINT_KEYS = new Set(['file', 'qualified_symbol', 'category', 'task_id']);
const REQUEST_KEYS = new Set([
  'flow',
  'http_reads',
  'http_mutations',
  'provider_calls',
  'tool_executor_calls',
  'sse_subscriptions',
  'domain_writes',
]);
const VISIBLE_COPY_KEYS = new Set(['file', 'lexeme', 'replacement']);
const INTERVIEW_SCENARIO_KEYS = new Set(['scenario', 'list', 'get']);
const INTERVIEW_LIST_KEYS = new Set(['items', 'next_cursor']);
const INTERVIEW_ITEM_KEYS = new Set([
  'application_id',
  'event_id',
  'company_name',
  'position_name',
  'scheduled_at',
  'note_id',
  'note_source_status',
  'has_review_proposal',
  'review_summary',
  'has_confirmed_knowledge',
  'preparation_available',
]);
const INTERVIEW_NOTE_SOURCE_STATUSES = new Set(['current', 'source_changed']);
const SENTINEL_SCHEDULED_AT = '0001-01-01T00:00:00+00:00';
const RFC3339 = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})$/;
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

type EntrypointCutover = {
  category: string;
  taskId: string | null;
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
  parseDiagnostics: readonly ts.Diagnostic[];
};

type AuditSources = {
  productionFiles: Map<string, string>;
  registrySource: SourceArtifact | null;
  contractsSource: SourceArtifact | null;
  controllerSource: SourceArtifact | null;
  entrypointCutovers: Map<string, EntrypointCutover>;
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

function hasExactKeys(value: Record<string, unknown>, expected: Set<string>): boolean {
  const keys = Object.keys(value);
  return keys.length === expected.size && keys.every((key) => expected.has(key));
}

function parseSource(path: string, text: string): SourceArtifact {
  const scriptKind = /\.tsx$/i.test(path) ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const sourceFile = ts.createSourceFile(path, text, ts.ScriptTarget.Latest, true, scriptKind);
  const parseDiagnostics = (sourceFile as ts.SourceFile & {
    parseDiagnostics?: readonly ts.Diagnostic[];
  }).parseDiagnostics ?? [];
  return {
    path,
    text,
    sourceFile,
    parseDiagnostics,
  };
}

function sourceIsUsable(artifact: SourceArtifact | null): artifact is SourceArtifact {
  return artifact !== null && artifact.parseDiagnostics.length === 0;
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
        || !hasExactKeys(parsed, ENVELOPE_KEYS)
        || typeof parsed.schema_version !== 'number'
        || !Number.isInteger(parsed.schema_version)
        || parsed.schema_version !== 1
        || parsed.source_baseline !== BASELINE
        || typeof parsed.source_baseline !== 'string'
        || !Array.isArray(parsed.items)
        || parsed.items.length === 0
      ) {
        violations.push(`assets:invalid-envelope:${name}`);
        continue;
      }
      assets.set(name, parsed as unknown as Asset);
    } catch {
      violations.push(`assets:unreadable:${name}`);
    }
  }
  violations.push(...assetShapeViolations(assets));
  return { assets, violations };
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0;
}

function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0;
}

function parseEntrypoints(asset: Asset | undefined, name = ASSET_NAMES[0]): {
  values: Entrypoint[];
  violations: string[];
} {
  const values: Entrypoint[] = [];
  const violations: string[] = [];
  const seen = new Set<string>();
  const coreTasks = new Set<string>();
  for (const [index, value] of (asset?.items ?? []).entries()) {
    if (!isRecord(value) || !hasExactKeys(value, ENTRYPOINT_KEYS)) {
      violations.push(`assets:invalid-entrypoint:${name}:${index}`);
      continue;
    }
    const file = value.file;
    const qualifiedSymbol = value.qualified_symbol;
    const category = value.category;
    const taskId = value.task_id;
    if (!isNonEmptyString(file) || !file.startsWith('web/src/')) {
      violations.push(`assets:invalid-entrypoint:${name}:${index}`);
      continue;
    }
    if (!isNonEmptyString(qualifiedSymbol) || typeof category !== 'string') {
      violations.push(`assets:invalid-entrypoint:${name}:${index}`);
      continue;
    }
    if (!ENTRYPOINT_CATEGORIES.has(category)) {
      violations.push(`assets:invalid-entrypoint:${name}:${index}`);
      continue;
    }
    if (category === 'core_task') {
      if (typeof taskId !== 'string' || !new Set<string>(CORE_TASK_IDS).has(taskId)) {
        violations.push(`assets:invalid-entrypoint:${name}:${index}`);
        continue;
      }
      coreTasks.add(taskId);
    } else if (taskId !== null) {
      violations.push(`assets:invalid-entrypoint:${name}:${index}`);
      continue;
    }
    const key = `${file}\u0000${qualifiedSymbol}`;
    if (seen.has(key)) {
      violations.push(`assets:duplicate-entrypoint:${name}:${index}`);
      continue;
    }
    seen.add(key);
    values.push({
      file,
      qualified_symbol: qualifiedSymbol,
      category,
      task_id: taskId,
    });
  }
  if (coreTasks.size !== CORE_TASK_IDS.length || CORE_TASK_IDS.some((taskId) => !coreTasks.has(taskId))) {
    violations.push(`assets:incomplete-core-task-entrypoints:${name}`);
  }
  return { values, violations };
}

function parseRequestCounts(asset: Asset | undefined, name = ASSET_NAMES[1]): string[] {
  const violations: string[] = [];
  const seen = new Set<string>();
  for (const [index, value] of (asset?.items ?? []).entries()) {
    if (!isRecord(value) || !hasExactKeys(value, REQUEST_KEYS)) {
      violations.push(`assets:invalid-request-count:${name}:${index}`);
      continue;
    }
    if (!isNonEmptyString(value.flow)) {
      violations.push(`assets:invalid-request-count:${name}:${index}`);
      continue;
    }
    if (seen.has(value.flow)) {
      violations.push(`assets:duplicate-request-flow:${name}:${index}`);
      continue;
    }
    seen.add(value.flow);
    const countKeys = [...REQUEST_KEYS].filter((key) => key !== 'flow');
    if (countKeys.some((key) => !isNonNegativeInteger(value[key]))) {
      violations.push(`assets:invalid-request-count:${name}:${index}`);
    }
  }
  return violations;
}

function parseVisibleCopy(asset: Asset | undefined, name = ASSET_NAMES[2]): {
  values: VisibleCopy[];
  violations: string[];
} {
  const values: VisibleCopy[] = [];
  const violations: string[] = [];
  const seen = new Set<string>();
  for (const [index, value] of (asset?.items ?? []).entries()) {
    if (!isRecord(value) || !hasExactKeys(value, VISIBLE_COPY_KEYS)) {
      violations.push(`assets:invalid-visible-copy:${name}:${index}`);
      continue;
    }
    const file = value.file;
    const lexeme = value.lexeme;
    const replacement = value.replacement;
    if (!isNonEmptyString(file) || !file.startsWith('web/src/')
      || !isNonEmptyString(lexeme) || !isNonEmptyString(replacement)
      || replacement.includes(lexeme)) {
      violations.push(`assets:invalid-visible-copy:${name}:${index}`);
      continue;
    }
    const key = `${file}\u0000${lexeme}`;
    if (seen.has(key)) {
      violations.push(`assets:duplicate-visible-copy:${name}:${index}`);
      continue;
    }
    seen.add(key);
    values.push({ file, lexeme, replacement });
  }
  return { values, violations };
}

type InterviewItem = {
  application_id: number;
  event_id: number;
  company_name: string;
  position_name: string;
  scheduled_at: string;
  note_id: number | null;
  note_source_status: string | null;
  has_review_proposal: boolean;
  review_summary: string | null;
  has_confirmed_knowledge: boolean;
  preparation_available: boolean;
};

function isRfc3339OrSentinel(value: unknown): value is string {
  if (value === SENTINEL_SCHEDULED_AT) return true;
  if (typeof value !== 'string') return false;
  const match = RFC3339.exec(value);
  if (!match) return false;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);
  if (month < 1 || month > 12 || day < 1 || hour > 23 || minute > 59 || second > 59) return false;
  const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const daysInMonth = [31, leapYear ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (day > daysInMonth[month - 1]!) return false;
  if (match[7] !== 'Z') {
    const offsetHour = Number(match[7]?.slice(1, 3));
    const offsetMinute = Number(match[7]?.slice(4, 6));
    if (offsetHour > 23 || offsetMinute > 59) return false;
  }
  return Number.isFinite(Date.parse(value));
}

function parseInterviewItem(value: unknown): value is InterviewItem {
  if (!isRecord(value) || !hasExactKeys(value, INTERVIEW_ITEM_KEYS)) return false;
  const applicationId = value.application_id;
  const eventId = value.event_id;
  const noteId = value.note_id;
  const noteSourceStatus = value.note_source_status;
  return typeof applicationId === 'number' && Number.isInteger(applicationId) && applicationId > 0
    && typeof eventId === 'number' && Number.isInteger(eventId) && eventId > 0
    && typeof value.company_name === 'string'
    && typeof value.position_name === 'string'
    && isRfc3339OrSentinel(value.scheduled_at)
    && (noteId === null || (typeof noteId === 'number' && Number.isInteger(noteId) && noteId > 0))
    && (noteSourceStatus === null
      || (typeof noteSourceStatus === 'string' && INTERVIEW_NOTE_SOURCE_STATUSES.has(noteSourceStatus)))
    && typeof value.has_review_proposal === 'boolean'
    && (value.review_summary === null || typeof value.review_summary === 'string')
    && typeof value.has_confirmed_knowledge === 'boolean'
    && typeof value.preparation_available === 'boolean';
}

function structurallyEqual(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true;
  if (Array.isArray(left) && Array.isArray(right)) {
    return left.length === right.length && left.every((value, index) => structurallyEqual(value, right[index]));
  }
  if (!isRecord(left) || !isRecord(right) || !hasExactKeys(left, new Set(Object.keys(right)))) return false;
  return Object.keys(left).every((key) => structurallyEqual(left[key], right[key]));
}

function parseInterviewAsset(asset: Asset | undefined, name = ASSET_NAMES[3]): string[] {
  const violations: string[] = [];
  const scenarios = new Set<string>();
  const eventIds = new Set<number>();
  const applicationIds = new Set<number>();
  const noteIds = new Set<number>();
  for (const [index, value] of (asset?.items ?? []).entries()) {
    if (!isRecord(value) || !hasExactKeys(value, INTERVIEW_SCENARIO_KEYS)) {
      violations.push(`assets:invalid-interview-scenario:${name}:${index}`);
      continue;
    }
    if (!isNonEmptyString(value.scenario) || scenarios.has(value.scenario)) {
      violations.push(`assets:invalid-interview-scenario:${name}:${index}`);
      continue;
    }
    scenarios.add(value.scenario);
    const listing = value.list;
    const get = value.get;
    if (!isRecord(listing) || !hasExactKeys(listing, INTERVIEW_LIST_KEYS)
      || !Array.isArray(listing.items) || listing.items.length === 0
      || (listing.next_cursor !== null && typeof listing.next_cursor !== 'string')) {
      violations.push(`assets:invalid-interview-list:${name}:${index}`);
      continue;
    }
    const validListItems = listing.items.every((item) => parseInterviewItem(item));
    if (!validListItems || !parseInterviewItem(get) || listing.items.length !== 1
      || !structurallyEqual(listing.items[0], get)) {
      violations.push(`assets:invalid-interview-payload:${name}:${index}`);
      continue;
    }
    const item = get;
    if (eventIds.has(item.event_id) || applicationIds.has(item.application_id)
      || (item.note_id !== null && noteIds.has(item.note_id))) {
      violations.push(`assets:duplicate-interview-id:${name}:${index}`);
      continue;
    }
    eventIds.add(item.event_id);
    applicationIds.add(item.application_id);
    if (item.note_id !== null) noteIds.add(item.note_id);
  }
  return violations;
}

function assetShapeViolations(assets: Map<string, Asset>): string[] {
  const violations: string[] = [];
  const entries = parseEntrypoints(assets.get(ASSET_NAMES[0]));
  violations.push(...entries.violations);
  violations.push(...parseRequestCounts(assets.get(ASSET_NAMES[1])));
  const visibleCopy = parseVisibleCopy(assets.get(ASSET_NAMES[2]));
  violations.push(...visibleCopy.violations);
  violations.push(...parseInterviewAsset(assets.get(ASSET_NAMES[3])));
  return violations;
}

function entrypointsFromAsset(asset: Asset | undefined): Entrypoint[] {
  return parseEntrypoints(asset).values;
}

function visibleCopyFromAsset(asset: Asset | undefined): VisibleCopy[] {
  return parseVisibleCopy(asset).values;
}

function collectProductionFiles(root: string): Map<string, string> {
  const result = new Map<string, string>();
  const sourceRoot = join(root, 'web', 'src');
  const visit = (directory: string): void => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) {
        if (entry.name.toLowerCase() === '__tests__') continue;
        visit(path);
        continue;
      }
      if (!/\.tsx?$/.test(entry.name)
        || /\.(?:test|spec)\.tsx?$/i.test(entry.name)
        || /\.d\.ts$/i.test(entry.name)) continue;
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
  if (!ts.isBlock(node.body)) return true;
  return node.body.statements.some((statement) => {
    if (!ts.isExpressionStatement(statement)) return true;
    let expression = statement.expression;
    while (ts.isParenthesizedExpression(expression)) expression = expression.expression;
    return !ts.isStringLiteral(expression) && !ts.isNoSubstitutionTemplateLiteral(expression);
  });
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

function findFunctionInStatements(
  statements: readonly ts.Statement[],
  name: string,
): FunctionLikeWithBody | null {
  for (const statement of statements) {
    if (
      isFunctionLikeWithBody(statement)
      && functionLikeName(statement) === name
      && hasImplementedBody(statement)
    ) return statement;
    if (!ts.isVariableStatement(statement)) continue;
    for (const declaration of statement.declarationList.declarations) {
      if (!ts.isIdentifier(declaration.name) || declaration.name.text !== name) continue;
      const functionLike = variableFunctionLike(declaration);
      if (functionLike && hasImplementedBody(functionLike)) return functionLike;
    }
  }
  return null;
}

function findDirectFunctionByName(sourceFile: ts.SourceFile, name: string): FunctionLikeWithBody | null {
  return findFunctionInStatements(sourceFile.statements, name);
}

function findLexicallyNestedFunction(
  parent: FunctionLikeWithBody,
  name: string,
): FunctionLikeWithBody | null {
  if (!parent.body || !ts.isBlock(parent.body)) return null;
  let found: FunctionLikeWithBody | null = null;
  const visit = (node: ts.Node): void => {
    if (found) return;
    if (isFunctionLikeWithBody(node)) {
      if (functionLikeName(node) === name && hasImplementedBody(node)) found = node;
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
  for (const statement of parent.body.statements) visit(statement);
  return found;
}

function containsComponentBinding(parent: FunctionLikeWithBody, name: string): boolean {
  if (!parent.body) return false;
  let found = false;
  const visit = (node: ts.Node): void => {
    if (found) return;
    if (isFunctionLikeWithBody(node)) return;
    if (ts.isJsxOpeningLikeElement(node)) {
      const tagName = node.tagName;
      if (ts.isIdentifier(tagName) && tagName.text === name) {
        found = true;
        return;
      }
    }
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === name) {
      found = true;
      return;
    }
    ts.forEachChild(node, visit);
  };
  ts.forEachChild(parent.body, visit);
  return found;
}

function findFunctionByQualifiedPath(
  sourceFile: ts.SourceFile,
  qualifiedSymbol: string,
): FunctionLikeWithBody | null {
  const parts = qualifiedSymbol.split('.');
  if (parts.some((part) => !part)) return null;
  let current = findDirectFunctionByName(sourceFile, parts[0] ?? '');
  if (!current) return null;
  for (const part of parts.slice(1)) {
    const nested = findLexicallyNestedFunction(current, part);
    if (nested) {
      current = nested;
      continue;
    }
    const composed = findDirectFunctionByName(sourceFile, part);
    if (!composed || !containsComponentBinding(current, part)) return null;
    current = composed;
  }
  return current;
}

type LauncherBinding = {
  kind: 'import' | 'parameter' | 'composition' | 'shadow';
  canonicalName?: string;
};

const CANONICAL_LAUNCH_MODULES = new Set([
  './coreTaskSurface',
  '../coreTaskSurface',
  './features/coreTaskSurface',
  '../features/coreTaskSurface',
  '@/features/coreTaskSurface',
  '../coreTaskSurface/controller',
  './features/coreTaskSurface/controller',
  '../features/coreTaskSurface/controller',
  '@/features/coreTaskSurface/controller',
]);
const CONTROLLED_LAUNCH_PROPERTIES = new Set(['launch']);

function canonicalLauncherName(value: string): string | null {
  if (CANONICAL_LAUNCH_NAMES.has(value)) return value;
  if (CONTROLLED_LAUNCH_PROPERTIES.has(value)) return 'launchCoreTask';
  return null;
}

function bindingNames(pattern: ts.BindingName): string[] {
  if (ts.isIdentifier(pattern)) return [pattern.text];
  const names: string[] = [];
  for (const element of pattern.elements) {
    if (ts.isOmittedExpression(element)) continue;
    names.push(...bindingNames(element.name));
  }
  return names;
}

function importedLauncherBindings(sourceFile: ts.SourceFile): Map<string, LauncherBinding> {
  const bindings = new Map<string, LauncherBinding>();
  for (const statement of sourceFile.statements) {
    if (!ts.isImportDeclaration(statement) || !statement.importClause) continue;
    if (!ts.isStringLiteral(statement.moduleSpecifier)
      || !CANONICAL_LAUNCH_MODULES.has(statement.moduleSpecifier.text)) continue;
    const clause = statement.importClause;
    if (!clause.namedBindings || !ts.isNamedImports(clause.namedBindings)) continue;
    for (const element of clause.namedBindings.elements) {
      const importedName = element.propertyName?.text ?? element.name.text;
      const canonicalName = CANONICAL_LAUNCH_NAMES.has(importedName) ? importedName : undefined;
      bindings.set(element.name.text, { kind: 'import', canonicalName });
    }
  }
  return bindings;
}

function bindingForSourceExpression(
  expression: ts.Expression | undefined,
  bindings: Map<string, LauncherBinding>,
): LauncherBinding {
  if (!expression) return { kind: 'shadow' };
  if (ts.isIdentifier(expression)) return bindings.get(expression.text) ?? { kind: 'shadow' };
  if (ts.isPropertyAccessExpression(expression)) {
    const base = ts.isIdentifier(expression.expression)
      ? bindings.get(expression.expression.text)
      : undefined;
    const canonicalName = canonicalLauncherName(expression.name.text);
    if (base && base.kind !== 'shadow' && base.canonicalName && canonicalName) {
      return { kind: 'composition', canonicalName };
    }
    return { kind: 'shadow' };
  }
  return { kind: 'shadow' };
}

function bindingsForPattern(
  pattern: ts.BindingName,
  initializer: ts.Expression | undefined,
  bindings: Map<string, LauncherBinding>,
): Array<[string, LauncherBinding]> {
  if (ts.isIdentifier(pattern)) {
    const binding = bindingForSourceExpression(initializer, bindings);
    if (CANONICAL_LAUNCH_NAMES.has(pattern.text) && binding.canonicalName) {
      return [[pattern.text, { kind: 'composition', canonicalName: binding.canonicalName }]];
    }
    return [[pattern.text, binding]];
  }
  const result: Array<[string, LauncherBinding]> = [];
  for (const element of pattern.elements) {
    if (ts.isOmittedExpression(element)) continue;
    const propertyName = element.propertyName && propertyNameText(element.propertyName);
    const localNames = bindingNames(element.name);
    const sourceBinding = bindingForSourceExpression(initializer, bindings);
    for (const localName of localNames) {
      if (sourceBinding.kind === 'shadow' || !sourceBinding.canonicalName) {
        result.push([localName, { kind: 'shadow' }]);
      } else if (propertyName && canonicalLauncherName(propertyName)) {
        result.push([localName, {
          kind: 'composition',
          canonicalName: canonicalLauncherName(propertyName) ?? undefined,
        }]);
      } else if (CANONICAL_LAUNCH_NAMES.has(localName)) {
        result.push([localName, { kind: 'composition', canonicalName: localName }]);
      } else {
        result.push([localName, { kind: 'shadow' }]);
      }
    }
  }
  return result;
}

function addFunctionParameters(
  functionLike: FunctionLikeWithBody,
  bindings: Map<string, LauncherBinding>,
): void {
  for (const parameter of functionLike.parameters) {
    for (const name of bindingNames(parameter.name)) {
      bindings.set(name, { kind: 'parameter' });
    }
  }
}

function shadowBindingsForPattern(pattern: ts.BindingName): Array<[string, LauncherBinding]> {
  return bindingNames(pattern).map((name) => [name, { kind: 'shadow' }]);
}

function isLexicalDeclarationList(declarationList: ts.VariableDeclarationList): boolean {
  return (declarationList.flags & (ts.NodeFlags.Let | ts.NodeFlags.Const)) !== 0;
}

function addFunctionVarBindings(
  functionLike: FunctionLikeWithBody,
  bindings: Map<string, LauncherBinding>,
): void {
  if (!functionLike.body || !ts.isBlock(functionLike.body)) return;
  const parameterNames = new Set(functionLike.parameters.flatMap((parameter) => bindingNames(parameter.name)));
  const visit = (node: ts.Node): void => {
    if (isFunctionLikeWithBody(node)) return;
    if (ts.isVariableDeclarationList(node) && !isLexicalDeclarationList(node)) {
      for (const declaration of node.declarations) {
        for (const [name] of shadowBindingsForPattern(declaration.name)) {
          if (!parameterNames.has(name)) bindings.set(name, { kind: 'shadow' });
        }
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(functionLike.body);
}

function addBlockLexicalBindings(
  block: ts.Block,
  bindings: Map<string, LauncherBinding>,
  callPosition: number,
): void {
  for (const statement of block.statements) {
    if (isFunctionLikeWithBody(statement)) {
      const name = functionLikeName(statement);
      if (name) bindings.set(name, { kind: 'shadow' });
      continue;
    }
    if (ts.isClassDeclaration(statement)) {
      if (statement.name) bindings.set(statement.name.text, { kind: 'shadow' });
      continue;
    }
    if (!ts.isVariableStatement(statement) || !isLexicalDeclarationList(statement.declarationList)) continue;
    for (const declaration of statement.declarationList.declarations) {
      const declarationBindings = declaration.getStart() >= callPosition
        ? shadowBindingsForPattern(declaration.name)
        : bindingsForPattern(declaration.name, declaration.initializer, bindings);
      for (const [name, binding] of declarationBindings) bindings.set(name, binding);
    }
  }
}

type LauncherScope =
  | FunctionLikeWithBody
  | ts.Block
  | ts.CatchClause
  | ts.ForStatement
  | ts.ForInStatement
  | ts.ForOfStatement;

function isLauncherScope(node: ts.Node): node is LauncherScope {
  return isFunctionLikeWithBody(node)
    || ts.isBlock(node)
    || ts.isCatchClause(node)
    || ts.isForStatement(node)
    || ts.isForInStatement(node)
    || ts.isForOfStatement(node);
}

function addLoopInitializerBindings(
  loop: ts.ForStatement | ts.ForInStatement | ts.ForOfStatement,
  bindings: Map<string, LauncherBinding>,
): void {
  const initializer = loop.initializer;
  if (!initializer || !ts.isVariableDeclarationList(initializer)) return;
  for (const declaration of initializer.declarations) {
    for (const [name, binding] of shadowBindingsForPattern(declaration.name)) bindings.set(name, binding);
  }
}

function addCatchBindings(
  clause: ts.CatchClause,
  bindings: Map<string, LauncherBinding>,
): void {
  if (!clause.variableDeclaration) return;
  for (const [name, binding] of shadowBindingsForPattern(clause.variableDeclaration.name)) {
    bindings.set(name, binding);
  }
}

function launcherBindingsAtCall(
  sourceFile: ts.SourceFile,
  call: ts.CallExpression,
): Map<string, LauncherBinding> {
  const bindings = importedLauncherBindings(sourceFile);
  const scopes: LauncherScope[] = [];
  let parent: ts.Node | undefined = call.parent;
  while (parent) {
    if (isLauncherScope(parent)) scopes.push(parent);
    parent = parent.parent;
  }
  for (const scope of scopes.reverse()) {
    if (isFunctionLikeWithBody(scope)) {
      addFunctionParameters(scope, bindings);
      addFunctionVarBindings(scope, bindings);
    } else if (ts.isBlock(scope)) {
      addBlockLexicalBindings(scope, bindings, call.getStart());
    } else if (ts.isCatchClause(scope)) {
      addCatchBindings(scope, bindings);
    } else {
      addLoopInitializerBindings(scope, bindings);
    }
  }
  return bindings;
}

function isCanonicalLauncherBinding(binding: LauncherBinding | undefined): boolean {
  return Boolean(
    binding
    && (binding.kind === 'import' || binding.kind === 'composition')
    && binding.canonicalName
    && CANONICAL_LAUNCH_NAMES.has(binding.canonicalName),
  );
}

function canonicalLauncherCall(
  expression: ts.Expression,
  bindings: Map<string, LauncherBinding>,
): boolean {
  if (ts.isIdentifier(expression)) {
    return isCanonicalLauncherBinding(bindings.get(expression.text));
  }
  const propertyName = ts.isPropertyAccessExpression(expression)
    ? canonicalLauncherName(expression.name.text)
    : null;
  if (!ts.isPropertyAccessExpression(expression) || !propertyName) {
    return false;
  }
  if (!ts.isIdentifier(expression.expression)) return false;
  const base = bindings.get(expression.expression.text);
  return isCanonicalLauncherBinding(base);
}

function callsCanonicalLauncher(
  sourceFile: ts.SourceFile,
  functionLike: FunctionLikeWithBody,
): boolean {
  if (!functionLike.body) return false;
  let found = false;
  const visit = (node: ts.Node): void => {
    if (found) return;
    if (isFunctionLikeWithBody(node)) return;
    if (ts.isCallExpression(node)
      && canonicalLauncherCall(
        node.expression,
        launcherBindingsAtCall(sourceFile, node),
      )) {
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
  if (!sourceIsUsable(artifact) || !exactCoreTaskIdSet(coreTaskIdUnion(artifact.sourceFile))) return false;
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

function literalString(value: ts.Expression | undefined): string | null {
  const unwrapped = value ? unwrapExpression(value) : null;
  return unwrapped && ts.isStringLiteral(unwrapped) && unwrapped.text.length > 0
    ? unwrapped.text
    : null;
}

function entrypointCutoversFromSource(
  artifact: SourceArtifact | null,
): Map<string, EntrypointCutover> {
  const cutovers = new Map<string, EntrypointCutover>();
  if (!sourceIsUsable(artifact)) return cutovers;
  const initializer = findExportedConstInitializer(artifact.sourceFile, 'CORE_TASK_ENTRYPOINT_CUTOVERS');
  const object = initializer ? objectLiteralFromExpression(initializer) : null;
  const properties = object ? objectProperties(object) : null;
  if (!properties) return cutovers;
  for (const [qualifiedSymbol, property] of properties) {
    const metadata = objectLiteralFromExpression(property.initializer);
    const metadataProperties = metadata ? objectProperties(metadata) : null;
    if (!metadataProperties || metadataProperties.size !== 2) return new Map();
    const category = literalString(metadataProperties.get('category')?.initializer);
    const taskIdExpression = metadataProperties.get('taskId')?.initializer;
    const taskId = taskIdExpression && unwrapExpression(taskIdExpression).kind === ts.SyntaxKind.NullKeyword
      ? null
      : literalString(taskIdExpression);
    if (!category || !ENTRYPOINT_CATEGORIES.has(category)
      || (category === 'core_task'
        ? !taskId || !new Set<string>(CORE_TASK_IDS).has(taskId)
        : taskId !== null)) {
      return new Map();
    }
    cutovers.set(qualifiedSymbol, { category, taskId });
  }
  return cutovers;
}

function literalOwnerId(value: ts.Expression): string | null {
  const unwrapped = unwrapExpression(value);
  if (!ts.isStringLiteral(unwrapped) || !unwrapped.text.trim()) return null;
  return unwrapped.text;
}

function hasCanonicalRegistry(artifact: SourceArtifact | null): boolean {
  if (!sourceIsUsable(artifact)) return false;
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
    const names = new Set<string>();
    for (const member of members) {
      if (!ts.isPropertySignature(member) || !member.name) continue;
      const name = propertyNameText(member.name);
      if (name) names.add(name);
    }
    if (['phase', 'generation', 'active'].every((name) => names.has(name))) return true;
  }
  return false;
}

function hasCoreTaskController(artifact: SourceArtifact | null): boolean {
  return Boolean(
    sourceIsUsable(artifact)
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

  const baseline = readBaselineFile(root, entry.file);
  if (!sourceIsUsable(baseline) || !findFunctionByQualifiedPath(baseline.sourceFile, entry.qualified_symbol)) {
    return false;
  }

  const current = artifactFromProduction(sources.productionFiles, entry.file);
  const cutover = sources.entrypointCutovers.get(entry.qualified_symbol);
  const isExplicitCutover = Boolean(
    cutover
    && cutover.category === entry.category
    && cutover.taskId === entry.task_id,
  );
  if (!current) return isExplicitCutover;
  if (!sourceIsUsable(current)) return false;
  const currentFunction = findFunctionByQualifiedPath(current.sourceFile, entry.qualified_symbol);
  if (!currentFunction) return isExplicitCutover;
  if (entry.category !== 'core_task') return true;
  return callsCanonicalLauncher(current.sourceFile, currentFunction);
}

function hasExportedTypeNamed(sourceFile: ts.SourceFile, name: string): boolean {
  return sourceFile.statements.some((statement) => (
    hasExportModifier(statement)
    && ((ts.isTypeAliasDeclaration(statement) && statement.name.text === name)
      || (ts.isInterfaceDeclaration(statement) && statement.name.text === name))
  ));
}

function hasExportedFunctionNamed(sourceFile: ts.SourceFile, names: Set<string>): boolean {
  return [...names].some((name) => Boolean(findExportedFunction(sourceFile, name)));
}

const EVENT_LIFECYCLE_PATH = 'web/src/features/interviewEvents/eventLifecycle.ts';
const EVENT_LIFECYCLE_EXPORTS = new Set(['classifyEventLifecycleV1', 'resolveEventLifecycleV1']);

function hasCentralEventClassifier(productionFiles: Map<string, string>): boolean {
  const source = productionFiles.get(EVENT_LIFECYCLE_PATH);
  if (source === undefined) return false;
  const artifact = parseSource(EVENT_LIFECYCLE_PATH, source);
  if (!sourceIsUsable(artifact)) return false;
  return hasExportedTypeNamed(artifact.sourceFile, 'EventLifecycleV1')
    && hasExportedFunctionNamed(artifact.sourceFile, EVENT_LIFECYCLE_EXPORTS);
}

function hasLocalDeclarationName(sourceFile: ts.SourceFile, name: string): boolean {
  let found = false;
  const visit = (node: ts.Node): void => {
    if (found) return;
    if (ts.isImportDeclaration(node)) return;
    if (ts.isVariableDeclaration(node)) {
      if (bindingNames(node.name).includes(name)) {
        found = true;
        return;
      }
    }
    if (
      (ts.isFunctionDeclaration(node) || ts.isClassDeclaration(node))
      && node.name
      && node.name.text === name
    ) {
      found = true;
      return;
    }
    if (isFunctionLikeWithBody(node)
      && node.parameters.some((parameter) => bindingNames(parameter.name).includes(name))) {
      found = true;
      return;
    }
    if (
      ts.isMethodDeclaration(node)
      || ts.isGetAccessorDeclaration(node)
      || ts.isSetAccessorDeclaration(node)
      || ts.isPropertyDeclaration(node)
    ) {
      const propertyName = node.name && propertyNameText(node.name);
      if (propertyName === name) {
        found = true;
        return;
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return found;
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
    if (!sourceIsUsable(artifact)) return false;
    return localMarkers.some((marker) => hasLocalDeclarationName(artifact.sourceFile, marker));
  });
}

const MATERIAL_CLASSIFICATION_PATH = 'web/src/features/materialSurfaces/materialClassification.ts';
const MATERIAL_CLASSIFICATION_EXPORTS = new Set([
  'classifyMaterialRecord',
  'projectExperienceMaterials',
  'projectExternalReferences',
]);
const RESUME_LINEAGE_PATH = 'web/src/features/materialSurfaces/resumeLineage.ts';
const MATERIAL_LABELS_PATH = 'web/src/features/materialSurfaces/materialLabels.ts';
const MATERIAL_LABEL_MAPPER_EXPORTS = new Set(['mapMaterialLabel']);

function hasCentralMaterialMapper(productionFiles: Map<string, string>): boolean {
  const classification = productionFiles.get(MATERIAL_CLASSIFICATION_PATH);
  const lineage = productionFiles.get(RESUME_LINEAGE_PATH);
  const labels = productionFiles.get(MATERIAL_LABELS_PATH);
  if (classification === undefined || lineage === undefined || labels === undefined) return false;
  const classificationArtifact = parseSource(MATERIAL_CLASSIFICATION_PATH, classification);
  const lineageArtifact = parseSource(RESUME_LINEAGE_PATH, lineage);
  const labelsArtifact = parseSource(MATERIAL_LABELS_PATH, labels);
  if (!sourceIsUsable(classificationArtifact)
    || !sourceIsUsable(lineageArtifact)
    || !sourceIsUsable(labelsArtifact)) return false;
  return [...MATERIAL_CLASSIFICATION_EXPORTS].every((name) => (
    Boolean(findExportedFunction(classificationArtifact.sourceFile, name))
  ))
    && Boolean(findExportedFunction(lineageArtifact.sourceFile, 'resolveResumeLineage'))
    && hasExportedFunctionNamed(labelsArtifact.sourceFile, MATERIAL_LABEL_MAPPER_EXPORTS);
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
    if (
      ts.isTemplateExpression(node)
      && (node.head.text.includes(needle)
        || node.templateSpans.some((span) => span.literal.text.includes(needle)))
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

  const productionArtifacts = [...sources.productionFiles.entries()]
    .map(([path, source]) => parseSource(path, source));
  if (productionArtifacts.some((artifact) => artifact.parseDiagnostics.length > 0)) {
    violations.push('source:parse-diagnostics');
  }
  const copyAuditResults = visibleCopy.map((item) => (
    !item.lexeme
    || productionArtifacts.some((artifact) => (
      artifact.path === item.file && artifact.parseDiagnostics.length > 0
    ))
    || productionArtifacts.some((artifact) => (
      sourceIsUsable(artifact) && sourceHasVisibleText(artifact.sourceFile, item.lexeme)
    ))
  ));
  if (copyAuditResults.some((forbidden) => forbidden)) violations.push('copy:forbidden-lexeme');

  if (!hasCentralMaterialMapper(sources.productionFiles)) violations.push('materials:missing-source-mapper');
  return [...new Set(violations)];
}

function realAuditSources(root: string): AuditSources {
  const registrySource = readSourceArtifact(root, ['web/src/features/coreTaskSurface/registry.ts']);
  return {
    productionFiles: collectProductionFiles(root),
    registrySource,
    contractsSource: readSourceArtifact(root, ['web/src/features/coreTaskSurface/contracts.ts']),
    controllerSource: readSourceArtifact(root, [
      'web/src/features/coreTaskSurface/controller.ts',
      'web/src/features/coreTaskSurface/controller.tsx',
    ]),
    entrypointCutovers: entrypointCutoversFromSource(registrySource),
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
        ['web/src/features/interviewEvents/eventLifecycle.ts', `const fake = '${fakeText}';`],
        ['web/src/features/materialSurfaces/materialClassification.ts', `const fake = '${fakeText}';`],
        ['web/src/features/materialSurfaces/resumeLineage.ts', `const fake = '${fakeText}';`],
        ['web/src/features/materialSurfaces/materialLabels.ts', `const fake = '${fakeText}';`],
        ['web/src/unrelated.ts', 'const oldCopy = "旧版评估";'],
      ]),
      entrypointCutovers: new Map(),
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
    const malformedContracts = artifactFromText(
      'web/src/features/coreTaskSurface/contracts.ts',
      'export type CoreTaskId = {',
    );
    expect(malformedContracts.parseDiagnostics.length).toBeGreaterThan(0);
    expect(hasCoreTaskContracts(malformedContracts)).toBe(false);
  });

  it('requires qualified lexical entrypoints, bound launchers, and explicit cutover mappings', () => {
    const root = repositoryRoot();
    const entries = entrypointsFromAsset(readAssets(root).assets.get(ASSET_NAMES[0]));
    const entry = entries.find((candidate) => (
      candidate.qualified_symbol === 'AppShellContent.openOfferNegotiation'
    ));
    expect(entry).toBeDefined();
    if (!entry) return;

    const sources = (body: string): AuditSources => ({
      productionFiles: new Map([['web/src/layout/AppShell.tsx', body]]),
      registrySource: null,
      contractsSource: null,
      controllerSource: null,
      entrypointCutovers: new Map(),
    });
    const nestedLauncher = `import { launchCoreTask } from './coreTaskSurface';
function AppShellContent() {
  const openOfferNegotiation = () => {
    function unreachable() { launchCoreTask(); }
  };
  return null;
}`;
    expect(entrypointHasAudit(root, entry, sources(nestedLauncher))).toBe(false);

    const importWithInnerBlockShadow = `import { launchCoreTask } from './coreTaskSurface';
function AppShellContent() {
  const openOfferNegotiation = () => {
    { const launchCoreTask = () => {}; }
    launchCoreTask();
  };
  return null;
}`;
    expect(entrypointHasAudit(root, entry, sources(importWithInnerBlockShadow))).toBe(true);

    const shadowedLauncher = `import { launchCoreTask } from './coreTaskSurface';
function AppShellContent() {
  const openOfferNegotiation = () => {
    const launchCoreTask = () => {};
    launchCoreTask();
  };
  return null;
}`;
    expect(entrypointHasAudit(root, entry, sources(shadowedLauncher))).toBe(false);

    const factoryLauncher = `import { launchCoreTask } from './coreTaskSurface';
function AppShellContent() {
  const openOfferNegotiation = () => {
    const launchCoreTask = factory();
    launchCoreTask();
  };
  return null;
}`;
    expect(entrypointHasAudit(root, entry, sources(factoryLauncher))).toBe(false);

    const loopAndCatchBindings = [
      `import { launchCoreTask } from './coreTaskSurface';
function AppShellContent() {
  const openOfferNegotiation = () => {
    for (const launchCoreTask of [() => {}]) {
      launchCoreTask();
    }
  };
  return null;
}`,
      `import { launchCoreTask } from './coreTaskSurface';
function AppShellContent() {
  const openOfferNegotiation = () => {
    for (const launchCoreTask in { value: 1 }) {
      launchCoreTask();
    }
  };
  return null;
}`,
      `import { launchCoreTask } from './coreTaskSurface';
function AppShellContent() {
  const openOfferNegotiation = () => {
    for (const launchCoreTask of [1]) {
      launchCoreTask();
    }
  };
  return null;
}`,
      `import { launchCoreTask } from './coreTaskSurface';
function AppShellContent() {
  const openOfferNegotiation = () => {
    try { return null; } catch (launchCoreTask) {
      launchCoreTask();
    }
  };
  return null;
}`,
    ];
    for (const source of loopAndCatchBindings) {
      expect(entrypointHasAudit(root, entry, sources(source))).toBe(false);
    }

    const postLexicalDeclarations = [
      `import { launchCoreTask } from './coreTaskSurface';
function AppShellContent() {
  const openOfferNegotiation = () => {
    launchCoreTask();
    let launchCoreTask;
  };
  return null;
}`,
      `import { launchCoreTask } from './coreTaskSurface';
function AppShellContent() {
  const openOfferNegotiation = () => {
    launchCoreTask();
    class launchCoreTask {}
  };
  return null;
}`,
      `import { launchCoreTask } from './coreTaskSurface';
function AppShellContent() {
  const openOfferNegotiation = () => {
    launchCoreTask();
    function launchCoreTask() { return null; }
  };
  return null;
}`,
      `import { launchCoreTask } from './coreTaskSurface';
function AppShellContent() {
  const openOfferNegotiation = () => {
    launchCoreTask();
    if (true) { var launchCoreTask = factory(); }
  };
  return null;
}`,
    ];
    for (const source of postLexicalDeclarations) {
      expect(entrypointHasAudit(root, entry, sources(source))).toBe(false);
    }

    const parameterLauncher = `import { launchCoreTask } from './coreTaskSurface';
function AppShellContent() {
  const openOfferNegotiation = (launchCoreTask = factory()) => launchCoreTask();
  return null;
}`;
    expect(entrypointHasAudit(root, entry, sources(parameterLauncher))).toBe(false);
    const parameterController = `import { launchCoreTask } from './coreTaskSurface';
function AppShellContent() {
  const openOfferNegotiation = (controller: unknown) => controller.launch();
  return null;
}`;
    expect(entrypointHasAudit(root, entry, sources(parameterController))).toBe(false);

    const controlledAlias = `import { launchCoreTask as canonicalLauncher } from './coreTaskSurface';
function AppShellContent() {
  const openOfferNegotiation = () => {
    const open = canonicalLauncher;
    open();
  };
  return null;
}`;
    expect(entrypointHasAudit(root, entry, sources(controlledAlias))).toBe(true);

    const wrongQualifiedOwner = `import { launchCoreTask } from './coreTaskSurface';
function OtherContainer() {
  const openOfferNegotiation = () => launchCoreTask();
  return null;
}`;
    expect(entrypointHasAudit(root, entry, sources(wrongQualifiedOwner))).toBe(false);
    expect(entrypointHasAudit(root, entry, sources(''))).toBe(false);

    const cutover = new Map<string, EntrypointCutover>([
      [entry.qualified_symbol, { category: entry.category, taskId: entry.task_id }],
    ]);
    expect(entrypointHasAudit(root, entry, { ...sources(''), entrypointCutovers: cutover })).toBe(true);
    expect(entrypointHasAudit(root, entry, {
      ...sources(''),
      entrypointCutovers: new Map([[entry.qualified_symbol, {
        category: 'core_task',
        taskId: 'materials.story',
      }]]),
    })).toBe(false);
  });

  it('accepts only complete canonical AST fixture surfaces', () => {
    const contracts = artifactFromText(
      'web/src/features/coreTaskSurface/contracts.ts',
      `export type CoreTaskId = ${CORE_TASK_IDS.map((taskId) => `'${taskId}'`).join(' | ')};
export function parseCoreTaskRef(ref: string) { return ref; }
export const coreTaskCanonicalKey = (ref: string) => ref;`,
    );
    const registry = artifactFromText(
      'web/src/features/coreTaskSurface/registry.ts',
      `export const CORE_TASK_REGISTRY = Object.freeze({
${CORE_TASK_IDS.map((taskId, index) => `  '${taskId}': { ownerId: 'owner-${index}' },`).join('\n')}
} as const);`,
    );
    const controller = artifactFromText(
      'web/src/features/coreTaskSurface/controller.ts',
      `export interface CoreTaskSurfaceState {
  phase: string;
  generation: number;
  active: boolean;
}
export function createCoreTaskSurfaceController() {
  return { phase: 'idle', generation: 0, active: false };
}`,
    );
    expect(hasCoreTaskContracts(contracts)).toBe(true);
    expect(hasCanonicalRegistry(registry)).toBe(true);
    expect(hasCoreTaskController(controller)).toBe(true);

    const productionFiles = new Map([
      [EVENT_LIFECYCLE_PATH, `export type EventLifecycleV1 = { eventId: number };
export function classifyEventLifecycleV1() { return { eventId: 1 }; }`],
      [MATERIAL_CLASSIFICATION_PATH, `export function classifyMaterialRecord() { return null; }
export function projectExperienceMaterials() { return []; }
export function projectExternalReferences() { return []; }`],
      [RESUME_LINEAGE_PATH, `export function resolveResumeLineage() { return null; }`],
      [MATERIAL_LABELS_PATH, `export function mapMaterialLabel() { return ''; }`],
    ]);
    expect(hasCentralEventClassifier(productionFiles)).toBe(true);
    expect(hasCentralMaterialMapper(productionFiles)).toBe(true);
  });

  it('counts only local event-classifier declarations, not imported references', () => {
    const imported = new Map([
      ['web/src/components/ApplicationDetail.tsx', `import { TERMINAL_EVENT_STATUSES } from './eventLifecycle';
export function ApplicationDetail() { return TERMINAL_EVENT_STATUSES; }`],
    ]);
    expect(hasLocalEventClassifier(imported)).toBe(false);

    const localConst = new Map([
      ['web/src/components/ApplicationDetail.tsx', `const TERMINAL_EVENT_STATUSES = new Set();
export function ApplicationDetail() { return TERMINAL_EVENT_STATUSES; }`],
    ]);
    expect(hasLocalEventClassifier(localConst)).toBe(true);

    const localFunction = new Map([
      ['web/src/components/ApplicationDetail.tsx', `function isUpcomingInterview() { return true; }
export function ApplicationDetail() { return isUpcomingInterview(); }`],
    ]);
    expect(hasLocalEventClassifier(localFunction)).toBe(true);

    const interfaceProperty = new Map([
      ['web/src/components/ApplicationDetail.tsx', `interface InterviewState {
  isUpcomingInterview: boolean;
}
export function ApplicationDetail() { return true; }`],
    ]);
    expect(hasLocalEventClassifier(interfaceProperty)).toBe(false);
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
