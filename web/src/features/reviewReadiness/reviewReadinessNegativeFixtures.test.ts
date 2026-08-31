import { execFileSync } from 'node:child_process';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, posix, relative } from 'node:path';
import ts from 'typescript';
import { describe, expect, it } from 'vitest';

type SourceMap = Map<string, string>;

const CORE_TASK_IDS = new Set([
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
]);
const TOP_LEVEL_NAV = new Set(['today', 'applications', 'interview', 'offers', 'resources', 'settings']);
const CORE_TASK_IDENTITY = new Map<string, readonly string[]>([
  ['application.opportunity_fit', ['applicationId']],
  ['application.material_kit', ['applicationId']],
  ['application.interview_prepare', ['applicationId', 'eventId']],
  ['application.interview_review', ['applicationId', 'eventId']],
  ['application.general_review', ['applicationId']],
  ['application.offer_review', ['applicationId']],
  ['application.record_outcome', ['applicationId']],
  ['interview.free_practice', []],
  ['materials.resume', ['resumeId']],
  ['materials.story', ['storyId']],
  ['materials.reference', ['sourceId']],
]);

function normalizedPath(path: string): string {
  return path.replace(/\\/g, '/');
}

function productionEntries(sources: SourceMap): [string, string][] {
  return [...sources.entries()]
    .map(([path, source]) => [normalizedPath(path), source] as [string, string])
    .filter(([path]) => (
      /^web\/src\//.test(path)
      && /\.tsx?$/.test(path)
      && !/\.(?:test|spec)\.tsx?$/.test(path)
      && !/\.stories\.tsx?$/.test(path)
      && !/(?:^|\/)__(?:tests|fixtures)__(?:\/|$)/.test(path)
      && !/(?:^|\/)\.cache(?:\/|$)/.test(path)
      && !/(?:^|\/)node_modules(?:\/|$)/.test(path)
    ));
}

function parse(path: string, source: string): ts.SourceFile {
  return ts.createSourceFile(
    path,
    source,
    ts.ScriptTarget.Latest,
    true,
    path.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
}

function walk(node: ts.Node, visit: (node: ts.Node) => void): void {
  visit(node);
  ts.forEachChild(node, (child) => walk(child, visit));
}

function staticBooleanValue(node: ts.Expression): boolean | null {
  const expression = ts.isParenthesizedExpression(node) ? node.expression : node;
  if (expression.kind === ts.SyntaxKind.TrueKeyword) return true;
  if (expression.kind === ts.SyntaxKind.FalseKeyword) return false;
  if (ts.isNumericLiteral(expression)) return Number(expression.text) !== 0;
  return null;
}

const statementTerminationCache = new WeakMap<ts.Statement, boolean>();

function statementDefinitelyTerminates(statement: ts.Statement): boolean {
  const cached = statementTerminationCache.get(statement);
  if (cached !== undefined) return cached;
  let result = false;
  if (ts.isReturnStatement(statement) || ts.isThrowStatement(statement)
    || ts.isBreakStatement(statement) || ts.isContinueStatement(statement)) result = true;
  else if (ts.isBlock(statement)) result = statement.statements.some(statementDefinitelyTerminates);
  else if (ts.isIfStatement(statement)) {
    const condition = staticBooleanValue(statement.expression);
    if (condition === true) result = statementDefinitelyTerminates(statement.thenStatement);
    if (condition === false) {
      result = Boolean(statement.elseStatement && statementDefinitelyTerminates(statement.elseStatement));
    } else if (condition !== true) {
      result = statementDefinitelyTerminates(statement.thenStatement)
        && Boolean(statement.elseStatement && statementDefinitelyTerminates(statement.elseStatement));
    }
  } else if (ts.isTryStatement(statement)) {
    if (statement.finallyBlock?.statements.some(statementDefinitelyTerminates)) result = true;
    else {
      const tryTerminates = statement.tryBlock.statements.some(statementDefinitelyTerminates);
      const catchTerminates = Boolean(statement.catchClause
        && statement.catchClause.block.statements.some(statementDefinitelyTerminates));
      result = tryTerminates && (!statement.catchClause || catchTerminates);
    }
  }
  statementTerminationCache.set(statement, result);
  return result;
}

const staticReachabilityCache = new WeakMap<ts.Node, boolean>();

function staticallyReachable(node: ts.Node): boolean {
  const cached = staticReachabilityCache.get(node);
  if (cached !== undefined) return cached;
  const result = staticallyReachableUncached(node);
  staticReachabilityCache.set(node, result);
  return result;
}

function staticallyReachableUncached(node: ts.Node): boolean {
  let current: ts.Node = node;
  while (current.parent) {
    const parent = current.parent;
    if (ts.isWhileStatement(parent) && parent.statement === current
      && staticBooleanValue(parent.expression) === false) return false;
    if (ts.isForStatement(parent) && parent.statement === current
      && parent.condition && staticBooleanValue(parent.condition) === false) return false;
    if (ts.isIfStatement(parent)) {
      const condition = staticBooleanValue(parent.expression);
      if (condition === true && parent.elseStatement === current) return false;
      if (condition === false && parent.thenStatement === current) return false;
    }
    if (ts.isBlock(parent)) {
      const statement = parent.statements.find((candidate) => (
        current === candidate || (current.pos >= candidate.pos && current.end <= candidate.end)
      ));
      if (statement) {
        const index = parent.statements.indexOf(statement);
        if (parent.statements.slice(0, index).some(statementDefinitelyTerminates)) return false;
      }
    }
    current = parent;
  }
  return true;
}

function propertyName(node: ts.PropertyName | ts.MemberName): string | null {
  if (ts.isIdentifier(node) || ts.isStringLiteral(node) || ts.isNumericLiteral(node)) return node.text;
  if (ts.isComputedPropertyName(node) && ts.isStringLiteral(node.expression)) return node.expression.text;
  return null;
}

function bindingNames(name: ts.BindingName): string[] {
  if (ts.isIdentifier(name)) return [name.text];
  return name.elements.flatMap((element) => (
    ts.isOmittedExpression(element) ? [] : bindingNames(element.name)
  ));
}

type RuntimeFunctionLike =
  | ts.FunctionDeclaration
  | ts.FunctionExpression
  | ts.ArrowFunction
  | ts.MethodDeclaration
  | ts.ConstructorDeclaration
  | ts.GetAccessorDeclaration
  | ts.SetAccessorDeclaration;

function isRuntimeFunctionLike(node: ts.Node): node is RuntimeFunctionLike {
  return ts.isFunctionDeclaration(node)
    || ts.isFunctionExpression(node)
    || ts.isArrowFunction(node)
    || ts.isMethodDeclaration(node)
    || ts.isConstructorDeclaration(node)
    || ts.isGetAccessorDeclaration(node)
    || ts.isSetAccessorDeclaration(node);
}

function expressionContainsIdentifier(
  expression: ts.Node,
  predicate: (name: string) => boolean,
): boolean {
  let found = false;
  walk(expression, (node) => {
    if (ts.isIdentifier(node) && predicate(node.text)) found = true;
  });
  return found;
}

type NormalizedCall = { callee: ts.Expression; arguments: readonly ts.Expression[] };

const staticCallArraysCache = new WeakMap<ts.SourceFile, Map<string, ts.ArrayLiteralExpression>>();
const staticAdapterStringsCache = new WeakMap<ts.SourceFile, Map<string, string>>();

function staticCallArray(node: ts.Expression): ts.ArrayLiteralExpression | null {
  if (ts.isParenthesizedExpression(node) || ts.isAsExpression(node) || ts.isSatisfiesExpression(node)) {
    return staticCallArray(node.expression);
  }
  if (ts.isArrayLiteralExpression(node)) return node;
  if (!ts.isIdentifier(node)) return null;
  const sourceFile = node.getSourceFile();
  let arrays = staticCallArraysCache.get(sourceFile);
  if (!arrays) {
    arrays = new Map<string, ts.ArrayLiteralExpression>();
    walk(sourceFile, (candidate) => {
      if (ts.isVariableDeclaration(candidate) && ts.isIdentifier(candidate.name) && candidate.initializer) {
        let initializer = candidate.initializer;
        while (ts.isParenthesizedExpression(initializer) || ts.isAsExpression(initializer)
          || ts.isSatisfiesExpression(initializer)) initializer = initializer.expression;
        if (ts.isArrayLiteralExpression(initializer)) arrays!.set(candidate.name.text, initializer);
      }
    });
    staticCallArraysCache.set(sourceFile, arrays);
  }
  return arrays.get(node.text) ?? null;
}

function adapterMember(node: ts.Expression): string | null {
  if (ts.isPropertyAccessExpression(node)) return node.name.text;
  if (ts.isElementAccessExpression(node) && node.argumentExpression) {
    const direct = literalText(node.argumentExpression);
    if (direct !== null) return direct;
    if (ts.isIdentifier(node.argumentExpression)) {
      const sourceFile = node.getSourceFile();
      let strings = staticAdapterStringsCache.get(sourceFile);
      if (!strings) {
        strings = new Map();
        let changed = true;
        while (changed) {
          changed = false;
          walk(sourceFile, (candidate) => {
            if (!ts.isVariableDeclaration(candidate) || !ts.isIdentifier(candidate.name) || !candidate.initializer) return;
            const value = staticString(candidate.initializer, strings!);
            if (value !== null && strings!.get(candidate.name.text) !== value) {
              strings!.set(candidate.name.text, value);
              changed = true;
            }
          });
        }
        staticAdapterStringsCache.set(sourceFile, strings);
      }
      return strings.get(node.argumentExpression.text) ?? null;
    }
  }
  return null;
}

function expandedCallArguments(arguments_: readonly ts.Expression[]): readonly ts.Expression[] {
  const expanded: ts.Expression[] = [];
  for (const argument of arguments_) {
    if (ts.isSpreadElement(argument)) {
      const array = staticCallArray(argument.expression);
      if (array) {
        expanded.push(...array.elements);
        continue;
      }
    }
    expanded.push(argument);
  }
  return expanded;
}

function normalizedCall(node: ts.CallExpression): NormalizedCall {
  const adapter = adapterMember(node.expression);
  if ((ts.isPropertyAccessExpression(node.expression) || ts.isElementAccessExpression(node.expression))
    && (adapter === 'call' || adapter === 'apply')) {
    if (adapter === 'call') {
      return { callee: node.expression.expression, arguments: expandedCallArguments(node.arguments.slice(1)) };
    }
    const list = node.arguments[1] ? staticCallArray(node.arguments[1]) : null;
    if (list) {
      return { callee: node.expression.expression, arguments: [...list.elements] };
    }
  }
  if ((ts.isPropertyAccessExpression(node.expression) || ts.isElementAccessExpression(node.expression))
    && ts.isIdentifier(node.expression.expression)
    && node.expression.expression.text === 'Reflect'
    && adapter === 'apply'
    && node.arguments[0]
    && node.arguments[2]
    && staticCallArray(node.arguments[2])) {
    return { callee: node.arguments[0], arguments: [...staticCallArray(node.arguments[2])!.elements] };
  }
  return { callee: node.expression, arguments: expandedCallArguments(node.arguments) };
}

function expressionRootIsTainted(expression: ts.Expression, tainted: Set<string>): boolean {
  if (ts.isParenthesizedExpression(expression)
    || ts.isAsExpression(expression)
    || ts.isNonNullExpression(expression)
    || ts.isSatisfiesExpression(expression)
    || ts.isAwaitExpression(expression)) {
    return expressionRootIsTainted(expression.expression, tainted);
  }
  if (ts.isIdentifier(expression)) return tainted.has(expression.text);
  if (ts.isPropertyAccessExpression(expression)) {
    return expressionRootIsTainted(expression.expression, tainted);
  }
  if (ts.isElementAccessExpression(expression)) {
    return expressionRootIsTainted(expression.expression, tainted);
  }
  if (ts.isCallExpression(expression)) {
    return expressionRootIsTainted(expression.expression, tainted);
  }
  if (ts.isObjectLiteralExpression(expression)) {
    return expression.properties.some((property) => (
      (ts.isSpreadAssignment(property) && expressionRootIsTainted(property.expression, tainted))
      || (ts.isPropertyAssignment(property) && expressionRootIsTainted(property.initializer, tainted))
      || (ts.isShorthandPropertyAssignment(property) && tainted.has(property.name.text))
    ));
  }
  return false;
}

function modulePath(
  sources: SourceMap,
  importerPath: string,
  moduleName: string,
): string | null {
  const base = moduleName.startsWith('@/')
    ? `web/src/${moduleName.slice(2)}`
    : moduleName.startsWith('.')
      ? posix.join(posix.dirname(normalizedPath(importerPath)), normalizedPath(moduleName))
      : null;
  if (base === null) return null;
  const collapsed = posix.normalize(base);
  const candidates = [collapsed, `${collapsed}.ts`, `${collapsed}.tsx`, `${collapsed}/index.ts`, `${collapsed}/index.tsx`];
  return candidates.find((candidate) => sources.has(candidate)) ?? `${collapsed}.ts`;
}

function namespaceExportResolution(
  sources: SourceMap,
  path: string,
  exportName: string,
): { path: string; exportName: string } | null {
  const [namespace, ...rest] = exportName.split('.');
  if (!namespace || rest.length === 0) return null;
  const source = sources.get(path);
  if (source === undefined) return null;
  const sourceFile = parse(path, source);
  for (const statement of sourceFile.statements) {
    if (!ts.isExportDeclaration(statement) || !statement.exportClause
      || !ts.isNamespaceExport(statement.exportClause)
      || statement.exportClause.name.text !== namespace
      || !statement.moduleSpecifier || !ts.isStringLiteral(statement.moduleSpecifier)) continue;
    const target = modulePath(sources, path, statement.moduleSpecifier.text);
    if (target) return { path: target, exportName: rest.join('.') };
  }
  return null;
}

function usedMemberPaths(sourceFile: ts.SourceFile, root: string): string[] {
  const paths = new Set<string>();
  const strings = new Map<string, string>();
  let changed = true;
  while (changed) {
    changed = false;
    walk(sourceFile, (node) => {
      if (!ts.isVariableDeclaration(node) || !ts.isIdentifier(node.name) || !node.initializer) return;
      const value = staticString(node.initializer, strings);
      if (value !== null && !strings.has(node.name.text)) {
        strings.set(node.name.text, value);
        changed = true;
      }
    });
  }
  walk(sourceFile, (node) => {
    if (!ts.isPropertyAccessExpression(node) && !ts.isElementAccessExpression(node)) return;
    const members: string[] = [];
    let current: ts.Expression = node;
    while (ts.isPropertyAccessExpression(current) || ts.isElementAccessExpression(current)) {
      const name = ts.isPropertyAccessExpression(current)
        ? current.name.text
        : current.argumentExpression ? staticString(current.argumentExpression, strings) : null;
      if (!name) return;
      members.unshift(name);
      current = current.expression;
    }
    if (ts.isIdentifier(current) && current.text === root && members.length > 0) {
      paths.add(members.join('.'));
    }
  });
  return [...paths];
}

function hasDefaultModifier(node: ts.Node): boolean {
  return ts.canHaveModifiers(node)
    && Boolean(ts.getModifiers(node)?.some((modifier) => modifier.kind === ts.SyntaxKind.DefaultKeyword));
}

function isDomainServicePath(path: string): boolean {
  const normalized = normalizedPath(path).replace(/\.tsx?$/, '');
  return !/features\/reviewReadiness\/service$/.test(normalized)
    && (/(?:^|\/)services\/[^/]*(?:notes|applications|stories|practice|preparation|readiness|interviewreview|productaction|outcomes)/i.test(normalized)
      || /(?:notes|applications|stories|practice|preparation|readiness)(?:Service|Repository)$/i.test(normalized));
}

function functionHasDomainHttpWrite(node: RuntimeFunctionLike): boolean {
  if (!node.body) return false;
  let found = false;
  walk(node.body, (child) => {
    if (!ts.isCallExpression(child) || child.arguments.length === 0) return;
    const method = ts.isPropertyAccessExpression(child.expression)
      ? child.expression.name.text
      : ts.isElementAccessExpression(child.expression) && child.expression.argumentExpression
        ? literalText(child.expression.argumentExpression) ?? ''
        : '';
    const route = staticString(child.arguments[0], new Map());
    if (/^(?:post|put|patch|delete)$/i.test(method)
      && route
      && /(?:^|\/)(?:interview-notes|interview-review-proposals|applications|interview-stories|interview-practice|interview-preparation|product-actions|readiness)(?:\/|$)/i.test(route.replace(/\$\{\}/g, 'x'))) {
      found = true;
    }
  });
  return found;
}

function exportedDomainProvenance(
  sources: SourceMap,
  path: string,
  exportName: string,
  active = new Set<string>(),
): boolean {
  const key = `${path}:${exportName}`;
  if (active.has(key)) return false;
  if (isDomainServicePath(path)) return true;
  const source = sources.get(path);
  if (source === undefined) return false;
  const sourceFile = parse(path, source);
  const imports = new Map<string, { path: string; name: string }>();
  const taintedLocals = new Set<string>();
  for (const statement of sourceFile.statements) {
    if (!ts.isImportDeclaration(statement) || !ts.isStringLiteral(statement.moduleSpecifier)
      || !statement.importClause?.namedBindings || !ts.isNamedImports(statement.importClause.namedBindings)) continue;
    const target = modulePath(sources, path, statement.moduleSpecifier.text);
    if (!target) continue;
    for (const specifier of statement.importClause.namedBindings.elements) {
      imports.set(specifier.name.text, { path: target, name: specifier.propertyName?.text ?? specifier.name.text });
      if (exportedDomainProvenance(
        sources,
        target,
        specifier.propertyName?.text ?? specifier.name.text,
        new Set(active).add(key),
      )) taintedLocals.add(specifier.name.text);
    }
  }
  let changed = true;
  while (changed) {
    changed = false;
    walk(sourceFile, (node) => {
      if (ts.isVariableDeclaration(node) && node.initializer
        && expressionRootIsTainted(node.initializer, taintedLocals)) {
        for (const name of bindingNames(node.name)) {
          if (!taintedLocals.has(name)) { taintedLocals.add(name); changed = true; }
        }
      }
      if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
        && expressionRootIsTainted(node.right, taintedLocals)
        && (ts.isPropertyAccessExpression(node.left) || ts.isElementAccessExpression(node.left))
        && ts.isIdentifier(node.left.expression)
        && !taintedLocals.has(node.left.expression.text)) {
        taintedLocals.add(node.left.expression.text);
        changed = true;
      }
      if (isRuntimeFunctionLike(node) && node.body) {
        const name = functionName(node);
        if (!name) return;
        let callsDomain = false;
        walk(node.body, (child) => {
          if (ts.isCallExpression(child)
            && expressionRootIsTainted(child.expression, taintedLocals)) callsDomain = true;
        });
        if (callsDomain && !taintedLocals.has(name)) {
          taintedLocals.add(name);
          changed = true;
        }
      }
    });
  }
  const nextActive = new Set(active).add(key);
  for (const statement of sourceFile.statements) {
    if (!ts.isExportDeclaration(statement)) continue;
    const target = statement.moduleSpecifier && ts.isStringLiteral(statement.moduleSpecifier)
      ? modulePath(sources, path, statement.moduleSpecifier.text)
      : null;
    if (!statement.exportClause) {
      if (target && exportedDomainProvenance(sources, target, exportName, nextActive)) return true;
      continue;
    }
    if (!ts.isNamedExports(statement.exportClause)) continue;
    for (const specifier of statement.exportClause.elements) {
      if (specifier.name.text !== exportName) continue;
      const original = specifier.propertyName?.text ?? specifier.name.text;
      if (target && exportedDomainProvenance(sources, target, original, nextActive)) return true;
      const imported = imports.get(original);
      if (imported && exportedDomainProvenance(sources, imported.path, imported.name, nextActive)) return true;
      if (taintedLocals.has(original)) return true;
    }
  }
  for (const statement of sourceFile.statements) {
    if (ts.isExportAssignment(statement) && !statement.isExportEquals
      && exportName === 'default'
      && expressionRootIsTainted(statement.expression, taintedLocals)) return true;
    const exported = ts.canHaveModifiers(statement)
      && Boolean(ts.getModifiers(statement)?.some((modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword));
    if (!exported) continue;
    if (ts.isFunctionDeclaration(statement)
      && (statement.name?.text === exportName || (exportName === 'default' && hasDefaultModifier(statement)))
      && (taintedLocals.has(statement.name?.text ?? '') || functionHasDomainHttpWrite(statement))) return true;
    if (ts.isVariableStatement(statement) && statement.declarationList.declarations.some((declaration) => (
      ts.isIdentifier(declaration.name) && declaration.name.text === exportName
      && taintedLocals.has(exportName)
    ))) return true;
  }
  return false;
}

function moduleHasDomainProvenance(
  sources: SourceMap,
  path: string,
  active = new Set<string>(),
): boolean {
  if (active.has(path)) return false;
  if (isDomainServicePath(path)) return true;
  const source = sources.get(path);
  if (source === undefined) return false;
  const sourceFile = parse(path, source);
  for (const statement of sourceFile.statements) {
    if (!ts.isExportDeclaration(statement)) continue;
    const target = statement.moduleSpecifier && ts.isStringLiteral(statement.moduleSpecifier)
      ? modulePath(sources, path, statement.moduleSpecifier.text)
      : null;
    if (statement.exportClause && ts.isNamedExports(statement.exportClause)) {
      if (statement.exportClause.elements.some((specifier) => (
        exportedDomainProvenance(sources, path, specifier.name.text)
      ))) return true;
    } else if (target && moduleHasDomainProvenance(sources, target, new Set(active).add(path))) return true;
  }
  return false;
}

const domainDefaultMembersCache = new WeakMap<SourceMap, Map<string, Set<string>>>();

function exportedDomainDefaultMembers(sources: SourceMap, path: string): Set<string> {
  const cache = domainDefaultMembersCache.get(sources) ?? new Map<string, Set<string>>();
  domainDefaultMembersCache.set(sources, cache);
  const cached = cache.get(path);
  if (cached) return new Set(cached);
  const source = sources.get(path);
  if (source === undefined) return new Set();
  const sourceFile = parse(path, source);
  const domainLocals = new Set<string>();
  for (const statement of sourceFile.statements) {
    if (!ts.isImportDeclaration(statement) || !ts.isStringLiteral(statement.moduleSpecifier)
      || !statement.importClause) continue;
    const target = modulePath(sources, path, statement.moduleSpecifier.text);
    if (!target) continue;
    if (statement.importClause.name && exportedDomainProvenance(sources, target, 'default')) {
      domainLocals.add(statement.importClause.name.text);
    }
    if (statement.importClause.namedBindings && ts.isNamedImports(statement.importClause.namedBindings)) {
      for (const specifier of statement.importClause.namedBindings.elements) {
        if (exportedDomainProvenance(
          sources,
          target,
          specifier.propertyName?.text ?? specifier.name.text,
        )) domainLocals.add(specifier.name.text);
      }
    }
  }
  const objects = new Map<string, ts.ObjectLiteralExpression>();
  walk(sourceFile, (node) => {
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name)
      && node.initializer && ts.isObjectLiteralExpression(node.initializer)) {
      objects.set(node.name.text, node.initializer);
    }
  });
  const members = new Map<string, Set<string>>();
  for (const [name, object] of objects) {
    const owned = new Set<string>();
    for (const property of object.properties) {
      if (!ts.isPropertyAssignment(property) && !ts.isShorthandPropertyAssignment(property)) continue;
      const member = propertyName(property.name);
      const value = ts.isPropertyAssignment(property) ? property.initializer : property.name;
      if (member && ts.isIdentifier(value) && domainLocals.has(value.text)) owned.add(member);
    }
    members.set(name, owned);
  }
  walk(sourceFile, (node) => {
    if (!ts.isBinaryExpression(node) || node.operatorToken.kind !== ts.SyntaxKind.EqualsToken
      || (!ts.isPropertyAccessExpression(node.left) && !ts.isElementAccessExpression(node.left))
      || !ts.isIdentifier(node.left.expression) || !ts.isIdentifier(node.right)
      || !domainLocals.has(node.right.text)) return;
    const member = memberMethod(node.left, new Map());
    if (member) {
      const owned = members.get(node.left.expression.text) ?? new Set<string>();
      owned.add(member);
      members.set(node.left.expression.text, owned);
    }
  });
  const result = new Set<string>();
  for (const statement of sourceFile.statements) {
    if (!ts.isExportAssignment(statement) || statement.isExportEquals) continue;
    if (ts.isIdentifier(statement.expression)) {
      for (const member of members.get(statement.expression.text) ?? []) result.add(member);
    }
  }
  cache.set(path, new Set(result));
  return result;
}

function hasDirectReviewDomainCrud(
  path: string,
  sourceFile: ts.SourceFile,
  sources: SourceMap,
  endpoints: readonly string[],
): boolean {
  if (!path.startsWith('web/src/features/reviewReadiness/')
    || path === 'web/src/features/reviewReadiness/service.ts') return false;
  const tainted = new Set<string>();
  const domainValueSeeds = new Set<string>();
  const domainMemberSeeds = new Set<string>();
  for (const statement of sourceFile.statements) {
    if (!ts.isImportDeclaration(statement) || !ts.isStringLiteral(statement.moduleSpecifier)) continue;
    const moduleName = statement.moduleSpecifier.text.replace(/\\/g, '/');
    const target = modulePath(sources, path, moduleName);
    if (!target || /features\/reviewReadiness\/service(?:\.ts)?$/.test(target)) continue;
    const clause = statement.importClause;
    if (clause?.name) {
      const members = exportedDomainDefaultMembers(sources, target);
      if (members.size > 0) {
        tainted.add(clause.name.text);
        for (const member of members) domainMemberSeeds.add(`${clause.name.text}.${member}`);
      } else if (exportedDomainProvenance(sources, target, 'default')) {
        tainted.add(clause.name.text);
        domainValueSeeds.add(clause.name.text);
      }
    }
    if (clause?.namedBindings && ts.isNamedImports(clause.namedBindings)) {
      for (const specifier of clause.namedBindings.elements) {
        const imported = specifier.propertyName?.text ?? specifier.name.text;
        if (exportedDomainProvenance(sources, target, imported)) {
          tainted.add(specifier.name.text);
          domainValueSeeds.add(specifier.name.text);
        }
      }
    }
    if (clause?.namedBindings && ts.isNamespaceImport(clause.namedBindings)) {
      if (moduleHasDomainProvenance(sources, target)) {
        tainted.add(clause.namedBindings.name.text);
        domainMemberSeeds.add(`${clause.namedBindings.name.text}.*`);
      }
    }
  }
  let changed = true;
  while (changed) {
    changed = false;
    walk(sourceFile, (node) => {
      if (ts.isVariableDeclaration(node) && node.initializer
        && expressionRootIsTainted(node.initializer, tainted)) {
        for (const name of bindingNames(node.name)) {
          if (!tainted.has(name)) {
            tainted.add(name);
            changed = true;
          }
        }
      }
      if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
        && ts.isIdentifier(node.left) && expressionRootIsTainted(node.right, tainted)
        && !tainted.has(node.left.text)) {
        tainted.add(node.left.text);
        changed = true;
      }
      if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
        && (ts.isPropertyAccessExpression(node.left) || ts.isElementAccessExpression(node.left))
        && ts.isIdentifier(node.left.expression)
        && expressionRootIsTainted(node.right, tainted)
        && !tainted.has(node.left.expression.text)) {
        tainted.add(node.left.expression.text);
        changed = true;
      }
      if (ts.isFunctionDeclaration(node) && node.name && node.body) {
        let returnsTainted = false;
        walk(node.body, (child) => {
          if (ts.isReturnStatement(child)
            && child.expression
            && expressionRootIsTainted(child.expression, tainted)) returnsTainted = true;
        });
        if (returnsTainted && !tainted.has(node.name.text)) {
          tainted.add(node.name.text);
          changed = true;
        }
      }
    });
  }
  const domainCallables = callableProvenance(sourceFile, domainValueSeeds, domainMemberSeeds);
  let violation = false;
  walk(sourceFile, (node) => {
    if (!ts.isCallExpression(node)) return;
    const callee = node.expression;
    if (domainCallables.isTarget(callee)) {
      violation = true;
      return;
    }
  });
  return violation || endpoints.some((endpoint) => (
    /^\/(?:interview-notes|interview-review-proposals|applications|interview-stories|interview-practice|interview-preparation|product-actions|readiness)(?:\/|$)/i.test(endpoint)
  ));
}

function hasDisallowedReviewServiceRoute(path: string, endpoints: readonly string[]): boolean {
  if (path !== 'web/src/features/reviewReadiness/service.ts') return false;
  const allowed = [
    /^\/interview-notes\/\$\{\}\/readiness-feedback-candidates$/,
    /^\/interview-notes\/\$\{\}\/readiness-focus-actions$/,
    /^\/product-actions\/\$\{\}(?:\/decisions)?$/,
    /^\/interview-notes\/\$\{\}\/readiness-focus-actions\/\$\{\}$/,
    /^\/applications\/\$\{\}\/product-actions\/\$\{\}\/rejection-control$/,
    /^\/applications\/\$\{\}\/events\/\$\{\}\/readiness-feedback$/,
    /^\/interview-practice\/focus\/\$\{\}$/,
    /^\/applications\/\$\{\}\/readiness-signals\/\$\{\}\/undo$/,
    /^\/interview-stories\/\$\{\}\/product-action-undo$/,
  ];
  return endpoints.some((endpoint) => (
    endpoint.startsWith('/') && !allowed.some((pattern) => pattern.test(endpoint))
  ));
}

function literalText(node: ts.Node): string | null {
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) return node.text;
  if (ts.isTemplateExpression(node)) {
    return [node.head.text, ...node.templateSpans.map((span) => span.literal.text)].join('${}');
  }
  return null;
}

function staticString(node: ts.Node, bindings: Map<string, string>, nested = false): string | null {
  const direct = literalText(node);
  if (direct !== null) return direct;
  if (ts.isIdentifier(node)) return bindings.get(node.text) ?? (nested ? '${}' : null);
  if (ts.isParenthesizedExpression(node) || ts.isAsExpression(node)
    || ts.isNonNullExpression(node) || ts.isSatisfiesExpression(node)) {
    return staticString(node.expression, bindings, nested);
  }
  if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.PlusToken) {
    const left = staticString(node.left, bindings, true);
    const right = staticString(node.right, bindings, true);
    return left === null || right === null ? null : left + right;
  }
  if (ts.isCallExpression(node)
    && ts.isPropertyAccessExpression(node.expression)
    && node.expression.name.text === 'join'
    && ts.isArrayLiteralExpression(node.expression.expression)) {
    const separator = node.arguments[0] ? staticString(node.arguments[0], bindings) : ',';
    const elements = node.expression.expression.elements.map((element) => staticString(element, bindings));
    return separator === null || elements.some((element) => element === null)
      ? null
      : elements.join(separator);
  }
  if (ts.isCallExpression(node)
    && ts.isPropertyAccessExpression(node.expression)
    && node.expression.name.text === 'concat') {
    const head = staticString(node.expression.expression, bindings);
    const tail = node.arguments.map((argument) => staticString(argument, bindings));
    return head === null || tail.some((part) => part === null)
      ? null
      : head + tail.join('');
  }
  return nested ? '${}' : null;
}

type MutationBuiltin = 'Object.assign' | 'Object.defineProperty' | 'Reflect.set' | 'Reflect.defineProperty';
type MutationNamespace = 'Object' | 'Reflect';
type MutationBuiltinWrite = {
  pos: number;
  expression?: ts.Expression;
  destructured?: { owner: ts.Expression; member: string };
};
type LexicalBindingIndex = { key: (node: ts.Node, name: string) => string };
type MutationBuiltinState = {
  writes: Map<string, MutationBuiltinWrite[]>;
  strings: Map<string, string>;
  bindings: LexicalBindingIndex;
};
const mutationBuiltinCache = new WeakMap<ts.SourceFile, MutationBuiltinState>();

function lexicalBindingIndex(sourceFile: ts.SourceFile): LexicalBindingIndex {
  const declarations = new Map<ts.Node, Set<string>>();
  const declare = (scope: ts.Node, name: string): void => {
    const names = declarations.get(scope) ?? new Set<string>();
    names.add(name);
    declarations.set(scope, names);
  };
  const isFunctionScope = (node: ts.Node): node is ts.FunctionLikeDeclaration => (
    ts.isFunctionDeclaration(node) || ts.isFunctionExpression(node)
    || ts.isArrowFunction(node) || ts.isMethodDeclaration(node)
  );
  const scopes = (node: ts.Node): ts.Node[] => {
    const result: ts.Node[] = [];
    let current: ts.Node | undefined = node;
    while (current) {
      if (ts.isBlock(current) || ts.isSourceFile(current) || isFunctionScope(current)) result.push(current);
      current = current.parent;
    }
    return result;
  };
  const blockScope = (node: ts.Node): ts.Node => (
    scopes(node).find((scope) => ts.isBlock(scope) || ts.isSourceFile(scope)) ?? sourceFile
  );
  walk(sourceFile, (node) => {
    if (ts.isImportClause(node)) {
      if (node.name) declare(sourceFile, node.name.text);
      if (node.namedBindings && ts.isNamedImports(node.namedBindings)) {
        for (const specifier of node.namedBindings.elements) declare(sourceFile, specifier.name.text);
      }
      if (node.namedBindings && ts.isNamespaceImport(node.namedBindings)) declare(sourceFile, node.namedBindings.name.text);
    }
    if (ts.isVariableDeclaration(node)) {
      for (const name of bindingNames(node.name)) declare(blockScope(node), name);
    }
    if (ts.isFunctionDeclaration(node) && node.name) declare(blockScope(node.parent), node.name.text);
    if (isFunctionScope(node)) {
      for (const parameter of node.parameters) {
        for (const name of bindingNames(parameter.name)) declare(node, name);
      }
    }
  });
  return {
    key: (node, name) => {
      const scope = scopes(node).find((candidate) => declarations.get(candidate)?.has(name));
      return scope ? `${scope.kind}:${scope.pos}:${scope.end}:${name}` : `unbound:${name}`;
    },
  };
}

function mutationBuiltin(node: ts.CallExpression): MutationBuiltin | null {
  const sourceFile = node.getSourceFile();
  let state = mutationBuiltinCache.get(sourceFile);
  if (!state) {
    const bindings = lexicalBindingIndex(sourceFile);
    const strings = new Map<string, string>();
    let stringsChanged = true;
    while (stringsChanged) {
      stringsChanged = false;
      walk(sourceFile, (candidate) => {
        if (!ts.isVariableDeclaration(candidate) || !ts.isIdentifier(candidate.name) || !candidate.initializer) return;
        const value = staticString(candidate.initializer, strings);
        if (value !== null && !strings.has(candidate.name.text)) {
          strings.set(candidate.name.text, value);
          stringsChanged = true;
        }
      });
    }
    const writes = new Map<string, MutationBuiltinWrite[]>();
    const record = (key: string, write: MutationBuiltinWrite): void => {
      const values = writes.get(key) ?? [];
      values.push(write);
      writes.set(key, values);
    };
    walk(sourceFile, (candidate) => {
      const pos = candidate.getStart(sourceFile);
      if (ts.isVariableDeclaration(candidate) && candidate.initializer) {
        if (ts.isIdentifier(candidate.name)) record(bindings.key(candidate, candidate.name.text), { pos, expression: candidate.initializer });
        if (ts.isObjectBindingPattern(candidate.name)) {
          for (const element of candidate.name.elements) {
            if (!ts.isIdentifier(element.name)) continue;
            const member = element.propertyName
              ? propertyName(element.propertyName)
              : element.name.text;
            if (member) record(bindings.key(element, element.name.text), {
              pos,
              destructured: { owner: candidate.initializer, member },
            });
          }
        }
      }
      if (ts.isBinaryExpression(candidate) && candidate.operatorToken.kind === ts.SyntaxKind.EqualsToken
        && ts.isIdentifier(candidate.left)) record(bindings.key(candidate.left, candidate.left.text), { pos, expression: candidate.right });
    });
    for (const values of writes.values()) values.sort((left, right) => left.pos - right.pos);
    state = { writes, strings, bindings };
    mutationBuiltinCache.set(sourceFile, state);
  }
  const latest = (identifier: ts.Identifier, before: number): MutationBuiltinWrite | null => (
    [...(state!.writes.get(state!.bindings.key(identifier, identifier.text)) ?? [])]
      .reverse().find((write) => write.pos < before) ?? null
  );
  const namespaceValue = (
    expression: ts.Expression,
    before: number,
    active = new Set<string>(),
  ): MutationNamespace | null => {
    if (ts.isParenthesizedExpression(expression) || ts.isAsExpression(expression)
      || ts.isSatisfiesExpression(expression) || ts.isNonNullExpression(expression)) {
      return namespaceValue(expression.expression, before, active);
    }
    if (!ts.isIdentifier(expression)) return null;
    if ((expression.text === 'Object' || expression.text === 'Reflect')
      && state!.bindings.key(expression, expression.text) === `unbound:${expression.text}`) return expression.text;
    if (active.has(expression.text)) return null;
    const write = latest(expression, before);
    return write?.expression
      ? namespaceValue(write.expression, write.pos, new Set(active).add(expression.text))
      : null;
  };
  const builtinValue = (
    expression: ts.Expression,
    before: number,
    active = new Set<string>(),
  ): MutationBuiltin | null => {
    if (ts.isParenthesizedExpression(expression) || ts.isAsExpression(expression)
      || ts.isSatisfiesExpression(expression) || ts.isNonNullExpression(expression)) {
      return builtinValue(expression.expression, before, active);
    }
    if (ts.isCallExpression(expression)
      && (ts.isPropertyAccessExpression(expression.expression)
        || ts.isElementAccessExpression(expression.expression))
      && adapterMember(expression.expression) === 'bind') {
      return builtinValue(expression.expression.expression, expression.getStart(sourceFile), active);
    }
    if (ts.isIdentifier(expression)) {
      if (active.has(expression.text)) return null;
      const write = latest(expression, before);
      if (!write) return null;
      const next = new Set(active).add(expression.text);
      if (write.expression) return builtinValue(write.expression, write.pos, next);
      if (write.destructured) {
        const owner = namespaceValue(write.destructured.owner, write.pos, next);
        const member = write.destructured.member;
        if (owner === 'Object' && /^(?:assign|defineProperty)$/.test(member)) return `Object.${member}` as MutationBuiltin;
        if (owner === 'Reflect' && /^(?:set|defineProperty)$/.test(member)) return `Reflect.${member}` as MutationBuiltin;
      }
      return null;
    }
    if (ts.isPropertyAccessExpression(expression) || ts.isElementAccessExpression(expression)) {
      const owner = namespaceValue(expression.expression, before, active);
      const member = ts.isPropertyAccessExpression(expression)
        ? expression.name.text
        : expression.argumentExpression ? staticString(expression.argumentExpression, state!.strings) : null;
      if (owner === 'Object' && /^(?:assign|defineProperty)$/.test(member ?? '')) return `Object.${member}` as MutationBuiltin;
      if (owner === 'Reflect' && /^(?:set|defineProperty)$/.test(member ?? '')) return `Reflect.${member}` as MutationBuiltin;
    }
    return null;
  };
  return builtinValue(normalizedCall(node).callee, node.getStart(sourceFile));
}

type MutationWrapper = {
  name: string;
  owner: RuntimeFunctionLike;
  sourceFile: ts.SourceFile;
  mutation: ts.CallExpression;
  receiverIndex: number;
  valueIndexes: number[];
  parameterKeys: string[];
  callable: CallableProvenance;
};
const mutationWrapperCache = new WeakMap<ts.SourceFile, MutationWrapper[]>();

function expressionReferencesBinding(
  expression: ts.Node,
  bindings: LexicalBindingIndex,
  expected: Set<string>,
): boolean {
  let found = false;
  walk(expression, (candidate) => {
    if (found || !ts.isIdentifier(candidate)) return;
    if (expected.has(bindings.key(candidate, candidate.text))) found = true;
  });
  return found;
}

function runtimeFunctionLabel(node: RuntimeFunctionLike): string {
  const direct = functionName(node);
  if (direct) return direct;
  if (ts.isPropertyAssignment(node.parent)) {
    const name = propertyName(node.parent.name);
    if (name) return name;
  }
  return `anonymous@${node.getStart(node.getSourceFile())}`;
}

function mutationWrapperParameters(
  sourceFile: ts.SourceFile,
  node: RuntimeFunctionLike,
  mutation: ts.CallExpression,
  bindings = lexicalBindingIndex(sourceFile),
): { receiverIndex: number; valueIndexes: number[]; parameterKeys: string[] } | null {
  const parameterKeys = node.parameters.map((parameter) => {
    const names = bindingNames(parameter.name);
    return names.length > 0 ? bindings.key(parameter, names[0]) : '';
  });
  const call = normalizedCall(mutation);
  const receiver = call.arguments[0];
  if (!receiver || !ts.isIdentifier(receiver)) return null;
  const receiverKey = bindings.key(receiver, receiver.text);
  const receiverIndex = parameterKeys.indexOf(receiverKey);
  if (receiverIndex < 0) return null;
  const valueIndexes: number[] = [];
  node.parameters.forEach((parameter, index) => {
    if (index === receiverIndex) return;
    const expected = new Set(bindingNames(parameter.name).map((name) => bindings.key(parameter, name)));
    if (call.arguments.slice(1).some((argument) => expressionReferencesBinding(
      argument,
      bindings,
      expected,
    ))) valueIndexes.push(index);
  });
  return { receiverIndex, valueIndexes, parameterKeys };
}

function localMutationWrappers(sourceFile: ts.SourceFile): MutationWrapper[] {
  const cached = mutationWrapperCache.get(sourceFile);
  if (cached) return cached;
  const pending: Omit<MutationWrapper, 'callable'>[] = [];
  walk(sourceFile, (node) => {
    if (!isRuntimeFunctionLike(node) || !node.body) return;
    const visitBody = (body: ts.Node): void => {
      if (ts.isCallExpression(body) && mutationBuiltin(body) !== null) {
        const parameters = mutationWrapperParameters(sourceFile, node, body);
        if (parameters) {
          pending.push({
            name: runtimeFunctionLabel(node),
            owner: node,
            sourceFile,
            mutation: body,
            ...parameters,
          });
        }
      }
      ts.forEachChild(body, (child) => {
        // A nested callback is a different callable. Do not attribute its
        // mutation to the containing wrapper (or its parameter names).
        if (isRuntimeFunctionLike(child)) return;
        visitBody(child);
      });
    };
    visitBody(node.body);
  });
  const provenanceByOwner = new Map<RuntimeFunctionLike, CallableProvenance>();
  const wrappers = pending.map((wrapper): MutationWrapper => {
    let callable = provenanceByOwner.get(wrapper.owner);
    if (!callable) {
      callable = callableProvenance(sourceFile, [], [], false, [wrapper.owner]);
      provenanceByOwner.set(wrapper.owner, callable);
    }
    return { ...wrapper, callable };
  });
  mutationWrapperCache.set(sourceFile, wrappers);
  return wrappers;
}

type CrossCallableReference =
  | { kind: 'function'; path: string; node: RuntimeFunctionLike }
  | { kind: 'object'; path: string; node: ts.ObjectLiteralExpression }
  | { kind: 'namespace'; path: string };

type CrossCallableResolver = {
  isTarget: (expression: ts.Expression, target: RuntimeFunctionLike) => boolean;
};

type CrossCallableWrite = {
  pos: number;
  expression?: ts.Expression;
  reference?: CrossCallableReference;
  destructured?: { source: ts.Expression; path: string[] };
};

type CrossCallableImport = {
  path: string;
  exportName: string;
  namespace?: boolean;
};

type CrossCallableModule = {
  path: string;
  sourceFile: ts.SourceFile;
  bindings: LexicalBindingIndex;
  bindingCache: WeakMap<ts.SourceFile, LexicalBindingIndex>;
  stringWrites: Map<string, Array<{ pos: number; expression: ts.Expression }>>;
  writes: Map<string, CrossCallableWrite[]>;
  imports: Map<string, CrossCallableImport>;
};

function crossStaticString(
  module: CrossCallableModule,
  node: ts.Node,
  before: number,
  active = new Set<string>(),
  nested = false,
): string | null {
  const direct = literalText(node);
  if (direct !== null) return direct;
  if (ts.isIdentifier(node)) {
    const bindings = node.getSourceFile() === module.sourceFile
      ? module.bindings
      : module.bindingCache.get(node.getSourceFile()) ?? lexicalBindingIndex(node.getSourceFile());
    module.bindingCache.set(node.getSourceFile(), bindings);
    const key = bindings.key(node, node.text);
    if (active.has(key)) return nested ? '${}' : null;
    const write = [...(module.stringWrites.get(key) ?? [])]
      .reverse()
      .find((candidate) => candidate.pos < before && staticallyReachable(candidate.expression));
    return write
      ? crossStaticString(module, write.expression, write.pos, new Set(active).add(key), nested)
      : nested ? '${}' : null;
  }
  if (ts.isParenthesizedExpression(node) || ts.isAsExpression(node)
    || ts.isNonNullExpression(node) || ts.isSatisfiesExpression(node)) {
    return crossStaticString(module, node.expression, before, active, nested);
  }
  if (ts.isTemplateExpression(node)) {
    let value = node.head.text;
    for (const span of node.templateSpans) {
      const part = crossStaticString(module, span.expression, before, active, true);
      if (part === null) return null;
      value += part + span.literal.text;
    }
    return value;
  }
  if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.PlusToken) {
    const left = crossStaticString(module, node.left, before, active, true);
    const right = crossStaticString(module, node.right, before, active, true);
    return left === null || right === null ? null : left + right;
  }
  return nested ? '${}' : null;
}

const crossMutationWrapperCache = new WeakMap<SourceMap, MutationWrapper[]>();
const crossCallableResolverCache = new WeakMap<SourceMap, CrossCallableResolver>();

function isExportedDeclaration(node: ts.Node): boolean {
  return ts.canHaveModifiers(node)
    && Boolean(ts.getModifiers(node)?.some((modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword));
}

function crossCallableResolver(sources: SourceMap): CrossCallableResolver {
  const cached = crossCallableResolverCache.get(sources);
  if (cached) return cached;
  const modules = new Map<string, CrossCallableModule>();
  for (const [rawPath, source] of sources) {
    const path = normalizedPath(rawPath);
    if (!/^web\/src\/.*\.tsx?$/.test(path)) continue;
    const sourceFile = parse(path, source);
    const bindings = lexicalBindingIndex(sourceFile);
    const bindingCache = new WeakMap<ts.SourceFile, LexicalBindingIndex>();
    bindingCache.set(sourceFile, bindings);
    modules.set(path, {
      path,
      sourceFile,
      bindings,
      bindingCache,
      stringWrites: new Map(),
      writes: new Map(),
      imports: new Map(),
    });
  }
  // Parse once per module. The source file object used for the lexical index
  // must be the same object attached to every expression in the module.
  const record = (module: CrossCallableModule, key: string, write: CrossCallableWrite): void => {
    const writes = module.writes.get(key) ?? [];
    writes.push(write);
    module.writes.set(key, writes);
  };
  for (const module of modules.values()) {
    const { sourceFile, bindings } = module;
    const crossPropertyName = (node: ts.PropertyName, before = sourceFile.end): string | null => {
      const direct = propertyName(node);
      if (direct !== null) return direct;
      return ts.isComputedPropertyName(node)
        ? crossStaticString(module, node.expression, before)
        : null;
    };
    const recordObjectBinding = (
      pattern: ts.ObjectBindingPattern,
      source: ts.Expression,
      pos: number,
    ): void => {
      const visitBinding = (name: ts.BindingName, path: string[]): void => {
        if (ts.isIdentifier(name)) {
          record(module, bindings.key(name, name.text), { pos, destructured: { source, path } });
          return;
        }
        if (!ts.isObjectBindingPattern(name)) return;
        for (const element of name.elements) {
          if (ts.isOmittedExpression(element)) continue;
          const member = element.propertyName
            ? crossPropertyName(element.propertyName, pos)
            : ts.isIdentifier(element.name) ? element.name.text : null;
          if (member !== null) visitBinding(element.name, [...path, member]);
        }
      };
      visitBinding(pattern, []);
    };
    const recordObjectAssignment = (
      pattern: ts.ObjectLiteralExpression,
      source: ts.Expression,
      pos: number,
    ): void => {
      const visitPattern = (current: ts.ObjectLiteralExpression, path: string[]): void => {
        for (const member of current.properties) {
          if (ts.isShorthandPropertyAssignment(member)) {
            record(module, bindings.key(member.name, member.name.text), {
              pos,
              destructured: { source, path: [...path, member.name.text] },
            });
            continue;
          }
          if (!ts.isPropertyAssignment(member)) continue;
          const property = crossPropertyName(member.name, pos);
          if (property === null) continue;
          if (ts.isIdentifier(member.initializer)) {
            record(module, bindings.key(member.initializer, member.initializer.text), {
              pos,
              destructured: { source, path: [...path, property] },
            });
          } else if (ts.isObjectLiteralExpression(member.initializer)) {
            visitPattern(member.initializer, [...path, property]);
          }
        }
      };
      visitPattern(pattern, []);
    };
    for (const statement of sourceFile.statements) {
      if (ts.isImportDeclaration(statement) && ts.isStringLiteral(statement.moduleSpecifier)
        && statement.importClause) {
        const target = modulePath(sources, module.path, statement.moduleSpecifier.text);
        if (target) {
          if (statement.importClause.name) module.imports.set(
            bindings.key(statement.importClause.name, statement.importClause.name.text),
            { path: target, exportName: 'default' },
          );
          if (statement.importClause.namedBindings && ts.isNamedImports(statement.importClause.namedBindings)) {
            for (const specifier of statement.importClause.namedBindings.elements) module.imports.set(
              bindings.key(specifier.name, specifier.name.text),
              { path: target, exportName: specifier.propertyName?.text ?? specifier.name.text },
            );
          }
          if (statement.importClause.namedBindings && ts.isNamespaceImport(statement.importClause.namedBindings)) {
            module.imports.set(
              bindings.key(statement.importClause.namedBindings.name, statement.importClause.namedBindings.name.text),
              { path: target, exportName: '*', namespace: true },
            );
          }
        }
      }
    }
    const recordStringWrite = (key: string, pos: number, expression: ts.Expression): void => {
      const writes = module.stringWrites.get(key) ?? [];
      writes.push({ pos, expression });
      module.stringWrites.set(key, writes);
    };
    walk(sourceFile, (node) => {
      if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer) {
        recordStringWrite(bindings.key(node, node.name.text), node.getStart(sourceFile), node.initializer);
      }
      if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
        && ts.isIdentifier(node.left)) {
        recordStringWrite(bindings.key(node.left, node.left.text), node.getStart(sourceFile), node.right);
      }
    });
    for (const writes of module.stringWrites.values()) writes.sort((left, right) => left.pos - right.pos);
    walk(sourceFile, (node) => {
      if (ts.isFunctionDeclaration(node) && node.name) {
        record(module, bindings.key(node.parent, node.name.text), {
          pos: -Infinity,
          reference: { kind: 'function', path: module.path, node },
        });
      }
      if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name)) {
        record(module, bindings.key(node, node.name.text), {
          pos: node.getStart(sourceFile),
          expression: node.initializer,
        });
      }
      if (ts.isVariableDeclaration(node) && ts.isObjectBindingPattern(node.name) && node.initializer) {
        recordObjectBinding(node.name, node.initializer, node.getStart(sourceFile));
      }
      if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
        && ts.isIdentifier(node.left)) {
        record(module, bindings.key(node.left, node.left.text), {
          pos: node.getStart(sourceFile),
          expression: node.right,
        });
      }
      if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
        && ts.isObjectLiteralExpression(node.left)) {
        recordObjectAssignment(node.left, node.right, node.getStart(sourceFile));
      }
    });
    for (const writes of module.writes.values()) writes.sort((left, right) => left.pos - right.pos);
  }

  const unique = (values: CrossCallableReference[]): CrossCallableReference[] => values.filter((value, index) => (
    values.findIndex((candidate) => (
      candidate.kind === value.kind
      && (candidate.kind === 'namespace' && value.kind === 'namespace'
        ? candidate.path === value.path
        : candidate.kind !== 'namespace' && value.kind !== 'namespace'
          && candidate.node === value.node)
    )) === index
  ));
  const activeExports = new Set<string>();
  const activeExpressions = new Set<string>();
  const exportReferenceCache = new Map<string, CrossCallableReference[]>();
  const expressionReferenceCache = new WeakMap<ts.SourceFile, Map<string, CrossCallableReference[]>>();
  const expressionBindingCache = new WeakMap<ts.SourceFile, LexicalBindingIndex>();

  const resolveExport = (path: string, exportName: string): CrossCallableReference[] => {
    const key = `${path}:${exportName}`;
    if (activeExports.has(key)) return [];
    const cached = exportReferenceCache.get(key);
    if (cached) return cached;
    const module = modules.get(normalizedPath(path));
    if (!module) return [];
    activeExports.add(key);
    const result: CrossCallableReference[] = [];
    const topLevel = (name: string): CrossCallableReference[] => {
      for (const statement of module.sourceFile.statements) {
        if (ts.isFunctionDeclaration(statement) && statement.name?.text === name) {
          return [{ kind: 'function', path: module.path, node: statement }];
        }
        if (ts.isVariableStatement(statement)) {
          const declaration = statement.declarationList.declarations.find((candidate) => (
            ts.isIdentifier(candidate.name) && candidate.name.text === name
          ));
          if (declaration?.initializer) return resolveExpression(declaration.initializer, module, module.sourceFile.end);
        }
        if (ts.isImportDeclaration(statement) && statement.importClause) {
          if (statement.importClause.name?.text === name) {
            const target = statement.moduleSpecifier && ts.isStringLiteral(statement.moduleSpecifier)
              ? modulePath(sources, module.path, statement.moduleSpecifier.text) : null;
            if (target) return resolveExport(target, 'default');
          }
          if (statement.importClause.namedBindings && ts.isNamedImports(statement.importClause.namedBindings)) {
            const specifier = statement.importClause.namedBindings.elements.find((candidate) => candidate.name.text === name);
            const target = statement.moduleSpecifier && ts.isStringLiteral(statement.moduleSpecifier)
              ? modulePath(sources, module.path, statement.moduleSpecifier.text) : null;
            if (specifier && target) return resolveExport(target, specifier.propertyName?.text ?? specifier.name.text);
          }
          if (statement.importClause.namedBindings && ts.isNamespaceImport(statement.importClause.namedBindings)
            && statement.importClause.namedBindings.name.text === name) {
            const target = statement.moduleSpecifier && ts.isStringLiteral(statement.moduleSpecifier)
              ? modulePath(sources, module.path, statement.moduleSpecifier.text) : null;
            if (target) return [{ kind: 'namespace', path: target }];
          }
        }
      }
      return [];
    };
    for (const statement of module.sourceFile.statements) {
      if (ts.isExportDeclaration(statement)) {
        const target = statement.moduleSpecifier && ts.isStringLiteral(statement.moduleSpecifier)
          ? modulePath(sources, module.path, statement.moduleSpecifier.text) : null;
        if (!statement.exportClause) {
          if (target) result.push(...resolveExport(target, exportName));
          continue;
        }
        if (ts.isNamespaceExport(statement.exportClause)) {
          if (exportName === statement.exportClause.name.text && target) result.push({ kind: 'namespace', path: target });
          continue;
        }
        const specifier = statement.exportClause.elements.find((candidate) => candidate.name.text === exportName);
        if (specifier) {
          const original = specifier.propertyName?.text ?? specifier.name.text;
          result.push(...(target ? resolveExport(target, original) : topLevel(original)));
        }
      }
      if (ts.isExportAssignment(statement) && !statement.isExportEquals && exportName === 'default') {
        result.push(...resolveExpression(statement.expression, module, statement.getStart(module.sourceFile)));
      }
      if (exportName === 'default' && ts.isFunctionDeclaration(statement)
        && hasDefaultModifier(statement)) {
        result.push({ kind: 'function', path: module.path, node: statement });
      }
      if (ts.isFunctionDeclaration(statement) && isExportedDeclaration(statement)
        && statement.name?.text === exportName) {
        result.push({ kind: 'function', path: module.path, node: statement });
      }
      if (ts.isVariableStatement(statement) && isExportedDeclaration(statement)) {
        const declaration = statement.declarationList.declarations.find((candidate) => (
          ts.isIdentifier(candidate.name) && candidate.name.text === exportName
        ));
        if (declaration?.initializer) result.push(...resolveExpression(declaration.initializer, module, statement.getStart(module.sourceFile)));
      }
    }
    activeExports.delete(key);
    const resolved = unique(result);
    exportReferenceCache.set(key, resolved);
    return resolved;
  };

  const resolveIdentifier = (
    node: ts.Identifier,
    module: CrossCallableModule,
    before: number,
  ): CrossCallableReference[] => {
    let bindings = module.bindings;
    if (node.getSourceFile() !== module.sourceFile) {
      bindings = expressionBindingCache.get(node.getSourceFile()) ?? lexicalBindingIndex(node.getSourceFile());
      expressionBindingCache.set(node.getSourceFile(), bindings);
    }
    const key = bindings.key(node, node.text);
    const imported = module.imports.get(key);
    if (imported) {
      return imported.namespace ? [{ kind: 'namespace', path: imported.path }] : resolveExport(imported.path, imported.exportName);
    }
    const writes = (module.writes.get(key) ?? []).filter((write) => (
      write.pos < before
      && staticallyReachable(write.expression ?? write.destructured?.source ?? module.sourceFile)
    ));
    const write = writes[writes.length - 1];
    if (!write) return [];
    if (write.reference) return [write.reference];
    if (write.destructured) {
      let references = resolveExpression(write.destructured.source, module, before);
      for (const property of write.destructured.path) {
        references = references.flatMap((reference) => resolveObjectProperty(reference, property));
      }
      return references;
    }
    return write.expression ? resolveExpression(write.expression, module, write.pos) : [];
  };

  const resolveObjectProperty = (
    reference: CrossCallableReference,
    property: string,
  ): CrossCallableReference[] => {
    if (reference.kind === 'namespace') return resolveExport(reference.path, property);
    if (reference.kind !== 'object') return [];
    const owner = modules.get(reference.path);
    if (!owner) return [];
    const crossPropertyName = (node: ts.PropertyName, before = owner.sourceFile.end): string | null => {
      const direct = propertyName(node);
      if (direct !== null) return direct;
      return ts.isComputedPropertyName(node)
        ? crossStaticString(owner, node.expression, before)
        : null;
    };
    for (const member of [...reference.node.properties].reverse()) {
      if (ts.isPropertyAssignment(member)
        && crossPropertyName(member.name, member.getStart(owner.sourceFile)) === property) {
        return resolveExpression(member.initializer, owner, owner.sourceFile.end);
      }
      if (ts.isShorthandPropertyAssignment(member) && member.name.text === property) {
        return resolveIdentifier(member.name, owner, owner.sourceFile.end);
      }
      if (ts.isMethodDeclaration(member)
        && crossPropertyName(member.name, member.getStart(owner.sourceFile)) === property) {
        return [{ kind: 'function', path: reference.path, node: member }];
      }
    }
    return [];
  };

  const resolveExpression = (
    expression: ts.Expression,
    module: CrossCallableModule,
    before: number,
  ): CrossCallableReference[] => {
    let node = expression;
    while (ts.isParenthesizedExpression(node) || ts.isAsExpression(node)
      || ts.isSatisfiesExpression(node) || ts.isNonNullExpression(node)) node = node.expression;
    const guard = `${module.path}:${node.pos}:${node.end}:${before}`;
    if (activeExpressions.has(guard)) return [];
    const sourceCache = expressionReferenceCache.get(node.getSourceFile()) ?? new Map<string, CrossCallableReference[]>();
    expressionReferenceCache.set(node.getSourceFile(), sourceCache);
    const cached = sourceCache.get(`${node.pos}:${node.end}:${before}`);
    if (cached) return cached;
    activeExpressions.add(guard);
    let result: CrossCallableReference[] = [];
    if (ts.isIdentifier(node)) result = resolveIdentifier(node, module, before);
    else if (isRuntimeFunctionLike(node)) result = [{ kind: 'function', path: module.path, node }];
    else if (ts.isObjectLiteralExpression(node)) result = [{ kind: 'object', path: module.path, node }];
    else if (ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node)) {
      const property = ts.isPropertyAccessExpression(node)
        ? node.name.text
        : node.argumentExpression
          ? crossStaticString(module, node.argumentExpression, before)
          : null;
      if (property) for (const reference of resolveExpression(node.expression, module, before)) {
        result.push(...resolveObjectProperty(reference, property));
      }
    } else if (ts.isConditionalExpression(node)) {
      result = [
        ...resolveExpression(node.whenTrue, module, before),
        ...resolveExpression(node.whenFalse, module, before),
      ];
    } else if (ts.isCallExpression(node)) {
      const adapter = adapterMember(node.expression);
      if ((adapter === 'bind' || adapter === 'call')
        && (ts.isPropertyAccessExpression(node.expression) || ts.isElementAccessExpression(node.expression))) {
        result = resolveExpression(node.expression.expression, module, before);
      } else {
        const callees = resolveExpression(node.expression, module, before);
        for (const callee of callees) {
          if (callee.kind !== 'function' || !callee.node.body) continue;
          const parameter = callee.node.parameters.findIndex((candidate) => (
            ts.isIdentifier(candidate.name)
          ));
          if (parameter < 0 || !node.arguments[parameter]) continue;
          for (const statement of ts.isBlock(callee.node.body) ? callee.node.body.statements : []) {
            if (!ts.isReturnStatement(statement) || !statement.expression) continue;
            if (ts.isIdentifier(statement.expression)
              && ts.isIdentifier(callee.node.parameters[parameter].name)
              && statement.expression.text === callee.node.parameters[parameter].name.text) {
              result.push(...resolveExpression(node.arguments[parameter], module, before));
            }
          }
        }
      }
    }
    activeExpressions.delete(guard);
    const resolved = unique(result);
    sourceCache.set(`${node.pos}:${node.end}:${before}`, resolved);
    return resolved;
  };

  const resolver: CrossCallableResolver = {
    isTarget: (expression, target) => {
      const module = modules.get(normalizedPath(expression.getSourceFile().fileName));
      if (!module) return false;
      const references = resolveExpression(expression, module, expression.getStart(module.sourceFile));
      return references
        .some((reference) => reference.kind === 'function'
          && reference.path === normalizedPath(target.getSourceFile().fileName)
          && reference.node.pos === target.pos
          && reference.node.end === target.end);
    },
  };
  crossCallableResolverCache.set(sources, resolver);
  return resolver;
}

function mutationWrappersForSourceMap(sources: SourceMap): MutationWrapper[] {
  const cached = crossMutationWrapperCache.get(sources);
  if (cached) return cached;
  const pending: Omit<MutationWrapper, 'callable'>[] = [];
  for (const [rawPath, source] of sources) {
    const path = normalizedPath(rawPath);
    if (!/^web\/src\/.*\.tsx?$/.test(path)) continue;
    if (!/\b(?:Object|Reflect)\b/.test(source)) continue;
    const sourceFile = parse(path, source);
    const bindings = lexicalBindingIndex(sourceFile);
    const visit = (node: ts.Node, owner: RuntimeFunctionLike | null): void => {
      const currentOwner = isRuntimeFunctionLike(node) ? node : owner;
      const mutation = currentOwner && ts.isCallExpression(node) ? mutationBuiltin(node) : null;
      if (currentOwner && ts.isCallExpression(node) && mutation !== null) {
        const parameters = mutationWrapperParameters(sourceFile, currentOwner, node, bindings);
        if (parameters) pending.push({
          name: runtimeFunctionLabel(currentOwner),
          owner: currentOwner,
          sourceFile,
          mutation: node,
          ...parameters,
        });
      }
      ts.forEachChild(node, (child) => visit(child, currentOwner));
    };
    visit(sourceFile, null);
  }
  const provenance = crossCallableResolver(sources);
  const wrappers = pending.map((wrapper) => ({
    ...wrapper,
    callable: { isTarget: (expression: ts.Expression) => provenance.isTarget(expression, wrapper.owner) },
  }));
  crossMutationWrapperCache.set(sources, wrappers);
  return wrappers;
}

function mutationWrappers(sourceFile: ts.SourceFile, sources?: SourceMap): MutationWrapper[] {
  return sources ? mutationWrappersForSourceMap(sources) : localMutationWrappers(sourceFile);
}

type WrapperStringState = {
  bindings: LexicalBindingIndex;
  writes: Map<string, Array<{ pos: number; expression: ts.Expression }>>;
};
const wrapperStringStateCache = new WeakMap<ts.SourceFile, WrapperStringState>();

function unknownWrapperExpression(): ts.Expression {
  return ts.factory.createObjectLiteralExpression();
}

function wrapperStringState(sourceFile: ts.SourceFile): WrapperStringState {
  const cached = wrapperStringStateCache.get(sourceFile);
  if (cached) return cached;
  const bindings = lexicalBindingIndex(sourceFile);
  const writes = new Map<string, Array<{ pos: number; expression: ts.Expression }>>();
  const record = (key: string, pos: number, expression: ts.Expression): void => {
    const values = writes.get(key) ?? [];
    values.push({ pos, expression });
    writes.set(key, values);
  };
  walk(sourceFile, (node) => {
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer) {
      record(bindings.key(node, node.name.text), node.getStart(sourceFile), node.initializer);
    }
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
      && ts.isIdentifier(node.left)) {
      record(bindings.key(node.left, node.left.text), node.getStart(sourceFile), node.right);
    }
  });
  for (const values of writes.values()) values.sort((left, right) => left.pos - right.pos);
  const state = { bindings, writes };
  wrapperStringStateCache.set(sourceFile, state);
  return state;
}

function wrapperStaticExpression(
  node: ts.Expression,
  state: WrapperStringState,
  before: number,
  parameterIndexes: ReadonlyMap<string, number>,
  callArguments: readonly ts.Expression[] | undefined,
  active = new Set<string>(),
): ts.Expression | null {
  const direct = ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)
    ? node.text
    : null;
  if (direct !== null) return ts.factory.createStringLiteral(direct);
  if (ts.isIdentifier(node)) {
    const key = state.bindings.key(node, node.text);
    const parameterIndex = parameterIndexes.get(key);
    if (parameterIndex !== undefined) return callArguments?.[parameterIndex] ?? unknownWrapperExpression();
    if (active.has(key)) return unknownWrapperExpression();
    const write = [...(state.writes.get(key) ?? [])]
      .reverse()
      .find((candidate) => candidate.pos < before && staticallyReachable(candidate.expression));
    return write
      ? wrapperStaticExpression(
        write.expression,
        state,
        write.pos,
        parameterIndexes,
        callArguments,
        new Set(active).add(key),
      )
      : unknownWrapperExpression();
  }
  if (ts.isParenthesizedExpression(node) || ts.isAsExpression(node)
    || ts.isNonNullExpression(node) || ts.isSatisfiesExpression(node)) {
    return wrapperStaticExpression(node.expression, state, before, parameterIndexes, callArguments, active)
      ?? unknownWrapperExpression();
  }
  const plus = (left: ts.Expression, right: ts.Expression): ts.Expression => (
    ts.factory.createBinaryExpression(
      left,
      ts.factory.createToken(ts.SyntaxKind.PlusToken),
      right,
    )
  );
  if (ts.isTemplateExpression(node)) {
    let result: ts.Expression = ts.factory.createStringLiteral(node.head.text);
    for (const span of node.templateSpans) {
      const expression = wrapperStaticExpression(
        span.expression,
        state,
        before,
        parameterIndexes,
        callArguments,
        active,
      );
      result = plus(result, expression ?? unknownWrapperExpression());
      if (span.literal.text) result = plus(result, ts.factory.createStringLiteral(span.literal.text));
    }
    return result;
  }
  if (ts.isCallExpression(node)
    && ts.isPropertyAccessExpression(node.expression)
    && node.expression.name.text === 'concat') {
    let result = wrapperStaticExpression(
      node.expression.expression,
      state,
      before,
      parameterIndexes,
      callArguments,
      active,
    );
    if (!result) result = unknownWrapperExpression();
    for (const argument of node.arguments) {
      const part = wrapperStaticExpression(
        argument,
        state,
        before,
        parameterIndexes,
        callArguments,
        active,
      );
      result = plus(result, part ?? unknownWrapperExpression());
    }
    return result;
  }
  if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.PlusToken) {
    const left = wrapperStaticExpression(node.left, state, before, parameterIndexes, callArguments, active);
    const right = wrapperStaticExpression(node.right, state, before, parameterIndexes, callArguments, active);
    return plus(left ?? unknownWrapperExpression(), right ?? unknownWrapperExpression());
  }
  return unknownWrapperExpression();
}

function mutationPropertyExpression(
  node: ts.CallExpression,
  name: string,
  strings: Map<string, string>,
  wrapper?: MutationWrapper,
  callArguments?: readonly ts.Expression[],
): ts.Expression | null {
  const call = normalizedCall(node);
  const builtin = mutationBuiltin(node);
  const wrapperState = wrapper ? wrapperStringState(wrapper.sourceFile) : null;
  const wrapperParameterIndexes = new Map<string, number>();
  if (wrapper) wrapper.parameterKeys.forEach((key, index) => {
    if (key) wrapperParameterIndexes.set(key, index);
  });
  const wrapperResolvedExpression = (expression: ts.Expression): ts.Expression | null => {
    if (!wrapper || !wrapperState) return null;
    return wrapperStaticExpression(
      expression,
      wrapperState,
      expression.getStart(wrapper.sourceFile),
      wrapperParameterIndexes,
      callArguments,
    );
  };
  const wrapperStaticString = (expression: ts.Expression): string | null => {
    const resolved = wrapperResolvedExpression(expression);
    return resolved ? staticString(resolved, strings) : null;
  };
  const wrapperValue = (expression: ts.Expression): ts.Expression => {
    return wrapperResolvedExpression(expression) ?? ts.factory.createObjectLiteralExpression();
  };
  const propertyMatches = (property: ts.PropertyAssignment): boolean => {
    const direct = propertyName(property.name);
    if (direct === name) return true;
    return ts.isComputedPropertyName(property.name)
      && wrapperStaticString(property.name.expression) === name;
  };
  if (builtin === 'Object.assign') {
    for (const source of [...call.arguments.slice(1)].reverse()) {
      if (!ts.isObjectLiteralExpression(source)) continue;
      const property = [...source.properties].reverse().find((candidate) => (
        ts.isPropertyAssignment(candidate) && propertyMatches(candidate)
      ));
      if (property && ts.isPropertyAssignment(property)) return wrapperValue(property.initializer);
    }
  }
  if (builtin === 'Reflect.set' && call.arguments[1] && call.arguments[2]
    && wrapperStaticString(call.arguments[1]) === name) return wrapperValue(call.arguments[2]);
  if ((builtin === 'Object.defineProperty' || builtin === 'Reflect.defineProperty')
    && call.arguments[1] && call.arguments[2]
    && wrapperStaticString(call.arguments[1]) === name
    && ts.isObjectLiteralExpression(call.arguments[2])) {
    const property = [...call.arguments[2].properties].reverse().find((candidate) => (
      ts.isPropertyAssignment(candidate) && propertyName(candidate.name) === 'value'
    ));
    if (property && ts.isPropertyAssignment(property)) return wrapperValue(property.initializer);
  }
  return null;
}

type StringWrite = { pos: number; expression: ts.Expression };

function orderedStaticString(
  node: ts.Node,
  before: number,
  writes: Map<string, StringWrite[]>,
  active = new Set<string>(),
  nested = false,
): string | null {
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) return node.text;
  if (ts.isTemplateExpression(node)) {
    let text = node.head.text;
    for (const span of node.templateSpans) {
      const value = orderedStaticString(span.expression, before, writes, active, true);
      if (value === null) return null;
      text += value + span.literal.text;
    }
    return text;
  }
  if (ts.isIdentifier(node)) {
    if (active.has(node.text)) return nested ? '${}' : null;
    const write = [...(writes.get(node.text) ?? [])].reverse().find((candidate) => candidate.pos < before);
    return write
      ? orderedStaticString(write.expression, write.pos, writes, new Set(active).add(node.text), nested)
      : nested ? '${}' : null;
  }
  if (ts.isParenthesizedExpression(node) || ts.isAsExpression(node)
    || ts.isNonNullExpression(node) || ts.isSatisfiesExpression(node)) {
    return orderedStaticString(node.expression, before, writes, active, nested);
  }
  if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.PlusToken) {
    const left = orderedStaticString(node.left, before, writes, active, true);
    const right = orderedStaticString(node.right, before, writes, active, true);
    return left === null || right === null ? null : left + right;
  }
  return nested ? '${}' : null;
}

function memberMethod(node: ts.Expression, strings: Map<string, string>): string | null {
  if (ts.isPropertyAccessExpression(node)) return node.name.text;
  if (ts.isElementAccessExpression(node) && node.argumentExpression) {
    return staticString(node.argumentExpression, strings);
  }
  return null;
}

const transportExportCache = new WeakMap<SourceMap, Map<string, boolean>>();

function exportedTransportProvenance(
  sources: SourceMap,
  path: string,
  exportName: string,
  active = new Set<string>(),
): boolean {
  if (active.size > 0) return exportedTransportProvenanceUncached(sources, path, exportName, active);
  const cache = transportExportCache.get(sources) ?? new Map<string, boolean>();
  transportExportCache.set(sources, cache);
  const key = `${path}:${exportName}`;
  const cached = cache.get(key);
  if (cached !== undefined) return cached;
  const result = exportedTransportProvenanceUncached(sources, path, exportName, active);
  cache.set(key, result);
  return result;
}

function exportedTransportProvenanceUncached(
  sources: SourceMap,
  path: string,
  exportName: string,
  active = new Set<string>(),
): boolean {
  const key = `${path}:${exportName}`;
  if (active.has(key)) return false;
  const namespace = namespaceExportResolution(sources, path, exportName);
  if (namespace) {
    return exportedTransportProvenance(
      sources,
      namespace.path,
      namespace.exportName,
      new Set(active).add(key),
    );
  }
  const source = sources.get(path);
  if (source === undefined) return false;
  const sourceFile = parse(path, source);
  const next = new Set(active).add(key);
  const localTransports = new Set<string>();
  let localChanged = true;
  while (localChanged) {
    localChanged = false;
    walk(sourceFile, (node) => {
      if (!ts.isVariableDeclaration(node) || !ts.isIdentifier(node.name) || !node.initializer) return;
      const text = node.initializer.getText(sourceFile);
      const transport = /\bhttp\s*(?:\.|\[)\s*['"]?(?:get|post|put|patch|delete|request)/i.test(text)
        || (/\.bind\s*\(/.test(text) && /\bhttp\b/.test(text))
        || (ts.isIdentifier(node.initializer) && localTransports.has(node.initializer.text));
      if (transport && !localTransports.has(node.name.text)) {
        localTransports.add(node.name.text);
        localChanged = true;
      }
    });
  }
  for (const statement of sourceFile.statements) {
    if (ts.isExportDeclaration(statement) && statement.exportClause
      && ts.isNamedExports(statement.exportClause)) {
      const target = statement.moduleSpecifier && ts.isStringLiteral(statement.moduleSpecifier)
        ? modulePath(sources, path, statement.moduleSpecifier.text)
        : null;
      for (const specifier of statement.exportClause.elements) {
        if (specifier.name.text !== exportName || !target) continue;
        if (exportedTransportProvenance(
          sources,
          target,
          specifier.propertyName?.text ?? specifier.name.text,
          next,
        )) return true;
      }
    }
    const exported = ts.canHaveModifiers(statement)
      && Boolean(ts.getModifiers(statement)?.some((modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword));
    if (exported && ts.isVariableStatement(statement)) {
      for (const declaration of statement.declarationList.declarations) {
        if (!ts.isIdentifier(declaration.name) || declaration.name.text !== exportName || !declaration.initializer) continue;
        const text = declaration.initializer.getText(sourceFile);
        if (/\bhttp\s*(?:\.|\[)\s*['"]?(?:get|post|put|patch|delete|request)/i.test(text)
          || (/\.bind\s*\(/.test(text) && /\bhttp\b/.test(text))) return true;
      }
    }
    if (exported && ts.isFunctionDeclaration(statement) && statement.name?.text === exportName
      && statement.body && /\bhttp\s*(?:\.|\[)/.test(statement.body.getText(sourceFile))) return true;
    if (ts.isFunctionDeclaration(statement) && exportName === 'default' && hasDefaultModifier(statement)
      && statement.body && /\bhttp\s*(?:\.|\[)/.test(statement.body.getText(sourceFile))) return true;
    if (ts.isExportAssignment(statement) && !statement.isExportEquals && exportName === 'default'
      && ts.isIdentifier(statement.expression) && localTransports.has(statement.expression.text)) return true;
  }
  return false;
}

function transportEndpoints(sourceFile: ts.SourceFile, path?: string, sources?: SourceMap): string[] {
  if (path && sources && sources.size > 100
    && !/web\/src\/features\/reviewReadiness\//.test(path)
    && !/(?:stories|write-operations)/i.test(sourceFile.text)) return [];
  const lexicalTransport = /\b(?:http|fetch|axios|createApiClient)\b/.test(sourceFile.text);
  if (!lexicalTransport) {
    if (!/['"`]\s*\//.test(sourceFile.text)) return [];
    let importsTransport = false;
    if (path && sources && /\bimport\b/.test(sourceFile.text)) {
      for (const statement of sourceFile.statements) {
        if (!ts.isImportDeclaration(statement) || !ts.isStringLiteral(statement.moduleSpecifier)
          || !statement.importClause) continue;
        const target = modulePath(sources, path, statement.moduleSpecifier.text);
        if (!target) continue;
        if (statement.importClause.name && exportedTransportProvenance(sources, target, 'default')) {
          importsTransport = true;
        }
        if (statement.importClause.namedBindings && ts.isNamedImports(statement.importClause.namedBindings)) {
          for (const specifier of statement.importClause.namedBindings.elements) {
            const imported = specifier.propertyName?.text ?? specifier.name.text;
            if (exportedTransportProvenance(sources, target, imported)) importsTransport = true;
            for (const member of usedMemberPaths(sourceFile, specifier.name.text)) {
              if (exportedTransportProvenance(sources, target, `${imported}.${member}`)) importsTransport = true;
            }
          }
        }
        if (statement.importClause.namedBindings && ts.isNamespaceImport(statement.importClause.namedBindings)) {
          for (const member of usedMemberPaths(sourceFile, statement.importClause.namedBindings.name.text)) {
            if (exportedTransportProvenance(sources, target, member)) importsTransport = true;
          }
        }
        if (importsTransport) break;
      }
    }
    if (!importsTransport) return [];
  }
  const stringBindings = new Map<string, string>();
  const stringWrites = new Map<string, StringWrite[]>();
  walk(sourceFile, (node) => {
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer) {
      const writes = stringWrites.get(node.name.text) ?? [];
      writes.push({ pos: node.getStart(sourceFile), expression: node.initializer });
      stringWrites.set(node.name.text, writes);
    }
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
      && ts.isIdentifier(node.left)) {
      const writes = stringWrites.get(node.left.text) ?? [];
      writes.push({ pos: node.getStart(sourceFile), expression: node.right });
      stringWrites.set(node.left.text, writes);
    }
  });
  const importedTransportFunctions = new Set<string>(['fetch']);
  const importedTransportMembers = new Set<string>();
  const transportFunctions = new Set<string>(importedTransportFunctions);
  const transportObjects = new Set<string>(['http']);
  for (const statement of sourceFile.statements) {
    if (!ts.isImportDeclaration(statement) || !ts.isStringLiteral(statement.moduleSpecifier)) continue;
    if (statement.moduleSpecifier.text === 'axios' && statement.importClause?.name) {
      transportObjects.add(statement.importClause.name.text);
    }
    if (path && sources && statement.importClause?.namedBindings
      && ts.isNamedImports(statement.importClause.namedBindings)) {
      const target = modulePath(sources, path, statement.moduleSpecifier.text);
      if (target) {
        for (const specifier of statement.importClause.namedBindings.elements) {
          const imported = specifier.propertyName?.text ?? specifier.name.text;
          if (exportedTransportProvenance(sources, target, imported)) {
            transportFunctions.add(specifier.name.text);
            importedTransportFunctions.add(specifier.name.text);
          }
        }
      }
    }
    if (path && sources && statement.importClause?.name) {
      const target = modulePath(sources, path, statement.moduleSpecifier.text);
      if (target && exportedTransportProvenance(sources, target, 'default')) {
        transportFunctions.add(statement.importClause.name.text);
        importedTransportFunctions.add(statement.importClause.name.text);
      }
    }
    if (path && sources && statement.importClause?.namedBindings
      && ts.isNamespaceImport(statement.importClause.namedBindings)) {
      const target = modulePath(sources, path, statement.moduleSpecifier.text);
      const local = statement.importClause.namedBindings.name.text;
      if (target) {
        for (const member of usedMemberPaths(sourceFile, local)) {
          if (exportedTransportProvenance(sources, target, member)) {
            importedTransportMembers.add(`${local}.${member}`);
          }
        }
      }
    }
  }
  const isTransportMember = (node: ts.Expression, before = node.getStart(sourceFile)): boolean => {
    const method = ts.isPropertyAccessExpression(node)
      ? node.name.text
      : ts.isElementAccessExpression(node) && node.argumentExpression
        ? orderedStaticString(node.argumentExpression, before, stringWrites)
        : null;
    if (method === null || !/^(?:get|post|put|patch|delete|request)$/i.test(method)) return false;
    if (!ts.isPropertyAccessExpression(node) && !ts.isElementAccessExpression(node)) return false;
    return ts.isIdentifier(node.expression) && transportObjects.has(node.expression.text);
  };
  let changed = true;
  while (changed) {
    changed = false;
    walk(sourceFile, (node) => {
      if (ts.isVariableDeclaration(node) && node.initializer) {
        if (ts.isIdentifier(node.name)) {
          const value = staticString(node.initializer, stringBindings);
          if (value !== null && !stringBindings.has(node.name.text)) {
            stringBindings.set(node.name.text, value);
            changed = true;
          }
          if (ts.isCallExpression(node.initializer) && ts.isIdentifier(node.initializer.expression)
            && /^(?:createApiClient|axios)$/i.test(node.initializer.expression.text)
            && !transportObjects.has(node.name.text)) {
            transportObjects.add(node.name.text);
            changed = true;
          }
          if (ts.isIdentifier(node.initializer) && transportObjects.has(node.initializer.text)
            && !transportObjects.has(node.name.text)) {
            transportObjects.add(node.name.text);
            changed = true;
          }
          const isTransport = isTransportMember(node.initializer)
            || (ts.isCallExpression(node.initializer)
              && ts.isPropertyAccessExpression(node.initializer.expression)
              && node.initializer.expression.name.text === 'bind'
              && isTransportMember(node.initializer.expression.expression, node.getStart(sourceFile)))
            || (ts.isIdentifier(node.initializer) && transportFunctions.has(node.initializer.text));
          if (isTransport && !transportFunctions.has(node.name.text)) {
            transportFunctions.add(node.name.text);
            changed = true;
          }
        } else if (ts.isObjectBindingPattern(node.name) && ts.isIdentifier(node.initializer)
          && transportObjects.has(node.initializer.text)) {
          for (const element of node.name.elements) {
            const imported = element.propertyName
              ? propertyName(element.propertyName)
              : ts.isIdentifier(element.name) ? element.name.text : null;
            for (const local of bindingNames(element.name)) {
              if (imported && /^(?:get|post|put|patch|delete|request)$/i.test(imported)
                && !transportFunctions.has(local)) {
                transportFunctions.add(local);
                changed = true;
              }
            }
          }
        }
      }
      if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
        && ts.isIdentifier(node.left)) {
        const value = staticString(node.right, stringBindings);
        if (value !== null && !stringBindings.has(node.left.text)) {
          stringBindings.set(node.left.text, value);
          changed = true;
        }
        if ((isTransportMember(node.right)
          || (ts.isIdentifier(node.right) && transportFunctions.has(node.right.text)))
          && !transportFunctions.has(node.left.text)) {
          transportFunctions.add(node.left.text);
          changed = true;
        }
      }
    });
  }

  const transportWrites = new Map<string, Array<{ pos: number; transport: boolean }>>();
  const objectTransportMethods = new Set<string>();
  const orderedFunctions = new Set(importedTransportFunctions);
  const recordTransport = (name: string, pos: number, transport: boolean): void => {
    const writes = transportWrites.get(name) ?? [];
    writes.push({ pos, transport });
    transportWrites.set(name, writes);
    if (transport) orderedFunctions.add(name);
    else orderedFunctions.delete(name);
  };
  const orderedTransportExpression = (expression: ts.Expression, pos: number): boolean => (
    isTransportMember(expression, pos)
    || (ts.isCallExpression(expression)
      && ts.isPropertyAccessExpression(expression.expression)
      && expression.expression.name.text === 'bind'
      && isTransportMember(expression.expression.expression, pos))
    || (ts.isIdentifier(expression) && orderedFunctions.has(expression.text))
  );
  walk(sourceFile, (node) => {
    if (!staticallyReachable(node)) return;
    const pos = node.getStart(sourceFile);
    if (ts.isVariableDeclaration(node) && node.initializer) {
      if (ts.isIdentifier(node.name)) {
        if (ts.isObjectLiteralExpression(node.initializer)) {
          for (const property of node.initializer.properties) {
            if (ts.isPropertyAssignment(property)) {
              const name = propertyName(property.name);
              if (name && orderedTransportExpression(property.initializer, pos)) {
                objectTransportMethods.add(`${node.name.text}.${name}`);
              }
            }
            if (ts.isMethodDeclaration(property) && property.body) {
              let wrapsTransport = false;
              walk(property.body, (child) => {
                if (ts.isCallExpression(child) && isTransportMember(child.expression, child.getStart(sourceFile))) {
                  wrapsTransport = true;
                }
              });
              const name = propertyName(property.name);
              if (name && wrapsTransport) objectTransportMethods.add(`${node.name.text}.${name}`);
            }
          }
        }
        recordTransport(node.name.text, pos, orderedTransportExpression(node.initializer, pos));
      } else if (ts.isObjectBindingPattern(node.name) && ts.isIdentifier(node.initializer)
        && transportObjects.has(node.initializer.text)) {
        for (const element of node.name.elements) {
          const method = element.propertyName
            ? propertyName(element.propertyName)
            : ts.isIdentifier(element.name) ? element.name.text : null;
          for (const local of bindingNames(element.name)) {
            recordTransport(local, pos, Boolean(method && /^(?:get|post|put|patch|delete|request)$/i.test(method)));
          }
        }
      }
    }
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
      && ts.isIdentifier(node.left)) {
      recordTransport(node.left.text, pos, orderedTransportExpression(node.right, pos));
    }
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
      && (ts.isPropertyAccessExpression(node.left) || ts.isElementAccessExpression(node.left))
      && ts.isIdentifier(node.left.expression)) {
      const name = ts.isPropertyAccessExpression(node.left)
        ? node.left.name.text
        : node.left.argumentExpression
          ? orderedStaticString(node.left.argumentExpression, pos, stringWrites)
          : null;
      if (name && orderedTransportExpression(node.right, pos)) {
        objectTransportMethods.add(`${node.left.expression.text}.${name}`);
      }
    }
  });
  const identifierIsTransportAt = (name: string, pos: number): boolean => {
    const write = [...(transportWrites.get(name) ?? [])].reverse().find((candidate) => candidate.pos < pos);
    return write?.transport ?? importedTransportFunctions.has(name);
  };
  const transportReturningFunctions = new Set<string>();
  changed = true;
  while (changed) {
    changed = false;
    walk(sourceFile, (node) => {
      if (!isRuntimeFunctionLike(node) || !node.body) return;
      const name = functionName(node);
      if (!name) return;
      let returnsTransport = false;
      walk(node.body, (child) => {
        if (!ts.isReturnStatement(child) || !child.expression) return;
        if (orderedTransportExpression(child.expression, child.getStart(sourceFile))
          || (ts.isCallExpression(child.expression)
            && ts.isIdentifier(child.expression.expression)
            && transportReturningFunctions.has(child.expression.expression.text))) returnsTransport = true;
      });
      if (returnsTransport && !transportReturningFunctions.has(name)) {
        transportReturningFunctions.add(name);
        changed = true;
      }
    });
  }
  const transportMemberSeeds = new Set<string>(objectTransportMethods);
  for (const member of importedTransportMembers) transportMemberSeeds.add(member);
  for (const owner of transportObjects) {
    for (const method of ['get', 'post', 'put', 'patch', 'delete', 'request']) {
      transportMemberSeeds.add(`${owner}.${method}`);
    }
  }
  const needsTransportProvenance = /\b(?:http|fetch|axios|createApiClient)\b/.test(sourceFile.text)
    || ((path === undefined || /web\/src\/features\/reviewReadiness\//.test(path))
      && (importedTransportFunctions.size > 1
        || importedTransportMembers.size > 0
        || objectTransportMethods.size > 0));
  const transportProvenance = needsTransportProvenance
    ? callableProvenance(sourceFile, importedTransportFunctions, transportMemberSeeds)
    : null;
  const callIsTransportAt = (expression: ts.Expression, pos: number): boolean => {
    if (transportProvenance?.isTarget(expression)) return true;
    if (ts.isIdentifier(expression)) return identifierIsTransportAt(expression.text, pos);
    if (ts.isCallExpression(expression) && ts.isIdentifier(expression.expression)
      && transportReturningFunctions.has(expression.expression.text)) return true;
    return isTransportMember(expression, pos);
  };

  const forwarding = new Map<string, Set<number>>();
  changed = true;
  while (changed) {
    changed = false;
    walk(sourceFile, (node) => {
      if (!isRuntimeFunctionLike(node) || !node.body) return;
      const name = functionName(node);
      if (!name) return;
      const indexes = forwarding.get(name) ?? new Set<number>();
      walk(node.body, (child) => {
        if (!ts.isCallExpression(child) || child.arguments.length === 0) return;
        const directTransport = isTransportMember(child.expression)
          || (ts.isIdentifier(child.expression) && transportFunctions.has(child.expression.text));
        const nestedIndexes = ts.isIdentifier(child.expression) ? forwarding.get(child.expression.text) : undefined;
        const argumentIndexes = directTransport ? [0] : [...(nestedIndexes ?? [])];
        for (const argumentIndex of argumentIndexes) {
          const argument = child.arguments[argumentIndex];
          if (!argument) continue;
          node.parameters.forEach((parameter, parameterIndex) => {
            const names = bindingNames(parameter.name);
            if (names.some((parameterName) => expressionContainsIdentifier(
              argument,
              (candidate) => candidate === parameterName,
            ))) indexes.add(parameterIndex);
          });
        }
      });
      if (indexes.size > (forwarding.get(name)?.size ?? 0)) {
        forwarding.set(name, indexes);
        changed = true;
      }
    });
  }

  const endpoints: string[] = [];
  const requestConfig = (
    expression: ts.Expression,
    before: number,
    active = new Set<string>(),
  ): ts.ObjectLiteralExpression | null => {
    if (ts.isParenthesizedExpression(expression) || ts.isAsExpression(expression)
      || ts.isSatisfiesExpression(expression) || ts.isNonNullExpression(expression)) {
      return requestConfig(expression.expression, before, active);
    }
    if (ts.isObjectLiteralExpression(expression)) return expression;
    if (!ts.isIdentifier(expression) || active.has(expression.text)) return null;
    const write = [...(stringWrites.get(expression.text) ?? [])]
      .reverse()
      .find((candidate) => candidate.pos < before && staticallyReachable(candidate.expression));
    return write
      ? requestConfig(write.expression, write.pos, new Set(active).add(expression.text))
      : null;
  };
  const requestConfigProperty = (
    expression: ts.Expression,
    name: string,
    before: number,
  ): ts.Expression | null => {
    const config = requestConfig(expression, before);
    let value: ts.Expression | null = null;
    let position = -1;
    if (config) {
      for (const property of [...config.properties].reverse()) {
        if (ts.isPropertyAssignment(property) && propertyName(property.name) === name) {
          value = property.initializer;
          position = property.getStart(sourceFile);
          break;
        }
      }
    }
    if (!ts.isIdentifier(expression)) return value;
    walk(sourceFile, (node) => {
      if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
        && (ts.isPropertyAccessExpression(node.left) || ts.isElementAccessExpression(node.left))
        && ts.isIdentifier(node.left.expression) && node.left.expression.text === expression.text
        && node.getStart(sourceFile) < before && node.getStart(sourceFile) > position
        && memberMethod(node.left, stringBindings) === name) {
          value = node.right;
          position = node.getStart(sourceFile);
      }
      if (!ts.isCallExpression(node) || node.getStart(sourceFile) >= before
        || node.getStart(sourceFile) <= position) return;
      const call = normalizedCall(node);
      const targetsConfig = Boolean(call.arguments[0] && ts.isIdentifier(call.arguments[0])
        && call.arguments[0].text === expression.text);
      const builtin = targetsConfig ? mutationBuiltin(node) : null;
      if (targetsConfig && builtin === 'Object.assign') {
        for (const sourceExpression of call.arguments.slice(1)) {
          const source = requestConfig(sourceExpression, node.getStart(sourceFile));
          if (!source) continue;
          const property = [...source.properties].reverse().find((candidate) => (
            ts.isPropertyAssignment(candidate) && propertyName(candidate.name) === name
          ));
          if (property && ts.isPropertyAssignment(property)) {
            value = property.initializer;
            position = node.getStart(sourceFile);
          }
        }
      }
      if (builtin === 'Reflect.set' && call.arguments[1] && call.arguments[2]
        && staticString(call.arguments[1], stringBindings) === name) {
        value = call.arguments[2];
        position = node.getStart(sourceFile);
      }
      if ((builtin === 'Object.defineProperty' || builtin === 'Reflect.defineProperty')
        && call.arguments[1] && call.arguments[2]
        && staticString(call.arguments[1], stringBindings) === name) {
        const descriptor = requestConfig(call.arguments[2], node.getStart(sourceFile));
        const property = descriptor && [...descriptor.properties].reverse().find((candidate) => (
          ts.isPropertyAssignment(candidate) && propertyName(candidate.name) === 'value'
        ));
        if (property && ts.isPropertyAssignment(property)) {
          value = property.initializer;
          position = node.getStart(sourceFile);
        }
      }
      for (const wrapper of mutationWrappers(sourceFile, sources)) {
        const receiver = call.arguments[wrapper.receiverIndex];
        if (!wrapper.callable.isTarget(call.callee) || !receiver || !ts.isIdentifier(receiver)
          || receiver.text !== expression.text) continue;
        const property = mutationPropertyExpression(
          wrapper.mutation,
          name,
          stringBindings,
          wrapper,
          call.arguments,
        );
        if (property) {
          value = property;
          position = node.getStart(sourceFile);
        }
      }
    });
    return value;
  };
  walk(sourceFile, (node) => {
    if (!staticallyReachable(node) || !ts.isCallExpression(node) || node.arguments.length === 0) return;
    const call = normalizedCall(node);
    const transport = callIsTransportAt(call.callee, node.getStart(sourceFile));
    if (transport && (ts.isPropertyAccessExpression(call.callee) || ts.isElementAccessExpression(call.callee))
      && adapterMember(call.callee) === 'request'
      && call.arguments[0]) {
      const config = requestConfig(call.arguments[0], node.getStart(sourceFile));
      if (!config) return;
      const url = requestConfigProperty(call.arguments[0], 'url', node.getStart(sourceFile));
      const value = url ? orderedStaticString(url, node.getStart(sourceFile), stringWrites) : null;
      if (value !== null) endpoints.push(value);
      return;
    }
    const indexes = transport ? [0] : ts.isIdentifier(call.callee)
      ? [...(forwarding.get(call.callee.text) ?? [])]
      : [];
    for (const index of indexes) {
      const argument = call.arguments[index];
      if (!argument) continue;
      const value = orderedStaticString(argument, node.getStart(sourceFile), stringWrites);
      if (value !== null) endpoints.push(value);
    }
  });
  return endpoints;
}

function nodeHasInProgress(node: ts.Node): boolean {
  let found = false;
  walk(node, (child) => {
    const value = literalText(child);
    if (value === 'in_progress') found = true;
  });
  return found;
}

function nodeHasExactPair(node: ts.Node): boolean {
  if (expressionContainsIdentifier(node, (name) => name === 'sameExactPair')
    || hasStructuralExactPair(node)) return true;
  const names = new Set<string>();
  walk(node, (child) => {
    if (ts.isIdentifier(child)) names.add(child.text.replace(/_/g, '').toLowerCase());
  });
  return [...names].some((name) => /signal.*version/.test(name))
    && [...names].some((name) => /target.*event/.test(name));
}

function isIteratorFirstValue(node: ts.Node): boolean {
  if (!ts.isPropertyAccessExpression(node) || node.name.text !== 'value'
    || !ts.isCallExpression(node.expression)
    || !ts.isPropertyAccessExpression(node.expression.expression)
    || node.expression.expression.name.text !== 'next'
    || !ts.isCallExpression(node.expression.expression.expression)
    || !ts.isPropertyAccessExpression(node.expression.expression.expression.expression)) return false;
  return node.expression.expression.expression.expression.name.text === 'values';
}

function isImmediateLoopReturnIife(node: ts.Node): boolean {
  if (!ts.isCallExpression(node)) return false;
  let callee: ts.Expression = node.expression;
  while (ts.isParenthesizedExpression(callee)) callee = callee.expression;
  if (!ts.isArrowFunction(callee) && !ts.isFunctionExpression(callee)) return false;
  let found = false;
  walk(callee.body, (child) => {
    if (found || (!ts.isForStatement(child) && !ts.isForInStatement(child)
      && !ts.isForOfStatement(child) && !ts.isWhileStatement(child)
      && !ts.isDoStatement(child))) return;
    walk(child.statement, (nested) => {
      if (ts.isReturnStatement(nested) && nested.expression) found = true;
    });
  });
  return found;
}

function exactFieldNames(node: ts.Node): Set<string> {
  const names = new Set<string>();
  walk(node, (child) => {
    if (ts.isPropertyAccessExpression(child)) names.add(child.name.text.replace(/_/g, '').toLowerCase());
    if (ts.isElementAccessExpression(child) && child.argumentExpression) {
      const key = literalText(child.argumentExpression);
      if (key) names.add(key.replace(/_/g, '').toLowerCase());
    }
    if ((ts.isPropertySignature(child) || ts.isPropertyAssignment(child))) {
      const key = propertyName(child.name);
      if (key) names.add(key.replace(/_/g, '').toLowerCase());
    }
    if (ts.isBindingElement(child)) {
      const key = child.propertyName
        ? propertyName(child.propertyName)
        : ts.isIdentifier(child.name) ? child.name.text : null;
      if (key) names.add(key.replace(/_/g, '').toLowerCase());
    }
  });
  return names;
}

function hasStructuralExactPair(node: ts.Node): boolean {
  const names = exactFieldNames(node);
  return [...names].some((name) => /readinesssignalversionid/.test(name))
    && [...names].some((name) => /targeteventid/.test(name));
}

function containingFunction(node: ts.Node): RuntimeFunctionLike | null {
  let current: ts.Node | undefined = node.parent;
  while (current) {
    if (isRuntimeFunctionLike(current)) return current;
    current = current.parent;
  }
  return null;
}

function isLegacyOnlyFallback(node: ts.CallExpression): boolean {
  let statement: ts.Statement | null = null;
  let current: ts.Node | undefined = node;
  while (current?.parent) {
    if (ts.isIfStatement(current.parent)) {
      const condition = current.parent.expression.getText().toLowerCase();
      if (current.parent.thenStatement === current && /legacy/.test(condition)) return true;
      if (current.parent.elseStatement === current
        && /(?:exact|readiness.*signal|target.*event)/.test(condition)) return true;
    }
    if (ts.isStatement(current)) statement = current;
    if (statement && ts.isBlock(current.parent)) {
      const block = current.parent;
      const index = block.statements.indexOf(statement);
      if (index < 0) return false;
      const guarded = block.statements.slice(0, index).some((candidate) => {
        if (!ts.isIfStatement(candidate)
          || !expressionContainsIdentifier(candidate.expression, (name) => /^focus$/i.test(name))) return false;
        let returns = false;
        walk(candidate.thenStatement, (child) => { if (ts.isReturnStatement(child)) returns = true; });
        return returns;
      });
      if (guarded) return true;
    }
    current = current.parent;
  }
  return false;
}

function resolvedTypeFields(
  node: ts.TypeNode,
  declarations: Map<string, ts.TypeNode>,
  active = new Set<string>(),
): Set<string> {
  const fields = exactFieldNames(node);
  if (ts.isTypeReferenceNode(node) && ts.isIdentifier(node.typeName)) {
    if (active.has(node.typeName.text)) return fields;
    const declaration = declarations.get(node.typeName.text);
    if (declaration) {
      for (const field of resolvedTypeFields(
        declaration,
        declarations,
        new Set(active).add(node.typeName.text),
      )) fields.add(field);
    }
  }
  if (ts.isUnionTypeNode(node) || ts.isIntersectionTypeNode(node)) {
    for (const type of node.types) {
      for (const field of resolvedTypeFields(type, declarations, active)) fields.add(field);
    }
  }
  return fields;
}

function resolvedTypeHasExactPair(node: ts.TypeNode, declarations: Map<string, ts.TypeNode>): boolean {
  const fields = resolvedTypeFields(node, declarations);
  return fields.has('readinesssignalversionid') && fields.has('targeteventid');
}

function exportedTypeHasExactPair(
  sources: SourceMap,
  path: string,
  exportName: string,
  active = new Set<string>(),
): boolean {
  const key = `${path}:${exportName}`;
  if (active.has(key)) return false;
  const source = sources.get(path);
  if (source === undefined) return false;
  const sourceFile = parse(path, source);
  const declarations = new Map<string, ts.TypeNode>();
  const importedTypes = new Map<string, { path: string; name: string }>();
  for (const statement of sourceFile.statements) {
    if (!ts.isImportDeclaration(statement) || !ts.isStringLiteral(statement.moduleSpecifier)
      || !statement.importClause?.namedBindings || !ts.isNamedImports(statement.importClause.namedBindings)) continue;
    const target = modulePath(sources, path, statement.moduleSpecifier.text);
    if (!target) continue;
    for (const specifier of statement.importClause.namedBindings.elements) {
      importedTypes.set(specifier.name.text, {
        path: target,
        name: specifier.propertyName?.text ?? specifier.name.text,
      });
    }
  }
  for (const statement of sourceFile.statements) {
    if (ts.isTypeAliasDeclaration(statement)) declarations.set(statement.name.text, statement.type);
    if (ts.isInterfaceDeclaration(statement)) {
      const parts: ts.TypeNode[] = [ts.factory.createTypeLiteralNode(statement.members)];
      for (const clause of statement.heritageClauses ?? []) {
        for (const type of clause.types) {
          if (ts.isIdentifier(type.expression)) {
            parts.push(ts.factory.createTypeReferenceNode(type.expression.text, type.typeArguments));
          }
        }
      }
      declarations.set(statement.name.text, parts.length === 1 ? parts[0] : ts.factory.createIntersectionTypeNode(parts));
    }
  }
  const local = declarations.get(exportName);
  if (local && resolvedTypeHasExactPair(local, declarations)) return true;
  const next = new Set(active).add(key);
  if (local) {
    let inheritedExact = false;
    walk(local, (node) => {
      if (!ts.isTypeReferenceNode(node) || !ts.isIdentifier(node.typeName)) return;
      const imported = importedTypes.get(node.typeName.text);
      if (imported && exportedTypeHasExactPair(sources, imported.path, imported.name, next)) inheritedExact = true;
    });
    if (inheritedExact) return true;
  }
  if (exportName === 'default') {
    for (const statement of sourceFile.statements) {
      if ((ts.isTypeAliasDeclaration(statement) || ts.isInterfaceDeclaration(statement))
        && hasDefaultModifier(statement)) {
        const declared = declarations.get(statement.name.text);
        if (declared && resolvedTypeHasExactPair(declared, declarations)) return true;
      }
      if (ts.isExportAssignment(statement) && !statement.isExportEquals
        && ts.isIdentifier(statement.expression)) {
        const declared = declarations.get(statement.expression.text);
        if (declared && resolvedTypeHasExactPair(declared, declarations)) return true;
      }
    }
  }
  for (const statement of sourceFile.statements) {
    if (!ts.isExportDeclaration(statement) || !statement.exportClause
      || !ts.isNamedExports(statement.exportClause)) continue;
    const target = statement.moduleSpecifier && ts.isStringLiteral(statement.moduleSpecifier)
      ? modulePath(sources, path, statement.moduleSpecifier.text)
      : null;
    for (const specifier of statement.exportClause.elements) {
      if (specifier.name.text !== exportName || !target) continue;
      if (exportedTypeHasExactPair(
        sources,
        target,
        specifier.propertyName?.text ?? specifier.name.text,
        next,
      )) return true;
    }
  }
  return false;
}

const implicitSelectorExportCache = new WeakMap<SourceMap, Map<string, boolean>>();

function exportedImplicitSelector(
  sources: SourceMap,
  path: string,
  exportName: string,
  active = new Set<string>(),
): boolean {
  if (active.size > 0) return exportedImplicitSelectorUncached(sources, path, exportName, active);
  const cache = implicitSelectorExportCache.get(sources) ?? new Map<string, boolean>();
  implicitSelectorExportCache.set(sources, cache);
  const key = `${path}:${exportName}`;
  const cached = cache.get(key);
  if (cached !== undefined) return cached;
  const result = exportedImplicitSelectorUncached(sources, path, exportName, active);
  cache.set(key, result);
  return result;
}

function exportedImplicitSelectorUncached(
  sources: SourceMap,
  path: string,
  exportName: string,
  active = new Set<string>(),
): boolean {
  const key = `${path}:${exportName}`;
  if (active.has(key)) return false;
  const namespace = namespaceExportResolution(sources, path, exportName);
  if (namespace) {
    return exportedImplicitSelector(
      sources,
      namespace.path,
      namespace.exportName,
      new Set(active).add(key),
    );
  }
  const source = sources.get(path);
  if (source === undefined) return false;
  const sourceFile = parse(path, source);
  const next = new Set(active).add(key);
  const importedSelectors = new Set<string>();
  for (const statement of sourceFile.statements) {
    if (!ts.isImportDeclaration(statement) || !ts.isStringLiteral(statement.moduleSpecifier)
      || !statement.importClause) continue;
    const target = modulePath(sources, path, statement.moduleSpecifier.text);
    if (!target) continue;
    if (statement.importClause.name
      && exportedImplicitSelector(sources, target, 'default', next)) {
      importedSelectors.add(statement.importClause.name.text);
    }
    if (statement.importClause.namedBindings && ts.isNamedImports(statement.importClause.namedBindings)) {
      for (const specifier of statement.importClause.namedBindings.elements) {
        if (exportedImplicitSelector(
          sources,
          target,
          specifier.propertyName?.text ?? specifier.name.text,
          next,
        )) importedSelectors.add(specifier.name.text);
      }
    }
  }
  for (const statement of sourceFile.statements) {
    if (ts.isExportDeclaration(statement) && statement.exportClause
      && ts.isNamedExports(statement.exportClause)) {
      const target = statement.moduleSpecifier && ts.isStringLiteral(statement.moduleSpecifier)
        ? modulePath(sources, path, statement.moduleSpecifier.text)
        : null;
      for (const specifier of statement.exportClause.elements) {
        if (specifier.name.text === exportName && target
          && exportedImplicitSelector(sources, target, specifier.propertyName?.text ?? specifier.name.text, next)) return true;
      }
    }
    let body: ts.Node | null = null;
    if (ts.isFunctionDeclaration(statement)
      && (statement.name?.text === exportName || (exportName === 'default' && hasDefaultModifier(statement)))) {
      body = statement.body ?? null;
    }
    if (ts.isVariableStatement(statement)) {
      const declaration = statement.declarationList.declarations.find((candidate) => (
        ts.isIdentifier(candidate.name) && candidate.name.text === exportName
      ));
      if (declaration?.initializer && (ts.isArrowFunction(declaration.initializer)
        || ts.isFunctionExpression(declaration.initializer))) body = declaration.initializer.body;
    }
    if (body) {
      let fallback = false;
      walk(body, (node) => {
        if (ts.isReturnStatement(node) && node.expression
          && ts.isIdentifier(node.expression)
          && importedSelectors.has(node.expression.text)) fallback = true;
        if (ts.isElementAccessExpression(node) && node.argumentExpression
          && ts.isBinaryExpression(node.argumentExpression)
          && node.argumentExpression.operatorToken.kind === ts.SyntaxKind.MinusToken
          && ts.isPropertyAccessExpression(node.argumentExpression.left)
          && node.argumentExpression.left.name.text === 'length'
          && ts.isNumericLiteral(node.argumentExpression.right)
          && node.argumentExpression.right.text === '1') fallback = true;
        if (ts.isVariableDeclaration(node) && ts.isArrayBindingPattern(node.name)
          && node.name.elements.filter((element) => !ts.isOmittedExpression(element)).length === 1) fallback = true;
        if (ts.isVariableDeclaration(node) && ts.isObjectBindingPattern(node.name)
          && node.name.elements.some((element) => (
            element.propertyName ? propertyName(element.propertyName) === '0'
              : ts.isIdentifier(element.name) ? element.name.text === '0' : false
          ))) fallback = true;
        if (isIteratorFirstValue(node) || isImmediateLoopReturnIife(node)) fallback = true;
        if (!ts.isCallExpression(node)) return;
        const name = ts.isPropertyAccessExpression(node.expression)
          ? node.expression.name.text
          : ts.isIdentifier(node.expression) ? node.expression.text : '';
        if ((/^(?:find|findLast|filter)$/i.test(name) && nodeHasInProgress(node) && !nodeHasExactPair(node))
          || /^(?:nearest|first|only|any|shift|pop|at)$/i.test(name)
          || (/^reduce(?:Right)?$/.test(name) && !nodeHasExactPair(node))) fallback = true;
      });
      if (fallback) return true;
    }
    if (ts.isExportAssignment(statement) && !statement.isExportEquals && exportName === 'default'
      && ts.isIdentifier(statement.expression) && importedSelectors.has(statement.expression.text)) return true;
  }
  return false;
}

type CallableProvenance = {
  isTarget: (node: ts.Expression) => boolean;
};

function callableProvenance(
  sourceFile: ts.SourceFile,
  seedValues: Iterable<string>,
  seedMembers: Iterable<string> = [],
  seedLocalValues = false,
  seedFunctionNodes: Iterable<RuntimeFunctionLike> = [],
): CallableProvenance {
  type ObjectId = ts.ObjectLiteralExpression | string;
  type Value =
    | { kind: 'target' }
    | { kind: 'object'; id: ObjectId }
    | { kind: 'function'; node: RuntimeFunctionLike }
    | { kind: 'union'; values: Value[] }
    | { kind: 'nullish' }
    | { kind: 'unknown' };
  type Write =
    | { pos: number; node: ts.Node; expression: ts.Expression; logical?: 'nullish' | 'or' | 'and' }
    | { pos: number; node: ts.Node; value: Value }
    | { pos: number; node: ts.Node; source: ts.Expression; property: string };
  type MemberWrite = {
    pos: number;
    owner: ts.Expression;
    property: string;
    expression: ts.Expression;
    logical?: 'nullish' | 'or' | 'and';
  };
  const TARGET: Value = { kind: 'target' };
  const UNKNOWN_VALUE: Value = { kind: 'unknown' };
  const NULLISH_VALUE: Value = { kind: 'nullish' };
  const seedValueNames = new Set(seedValues);
  const targetFunctionNodes = new Set(seedFunctionNodes);
  const rootScope = sourceFile;
  const declarations = new Map<ts.Node, Set<string>>();
  const bindingScopes = new Map<string, ts.Node>();
  const declare = (scope: ts.Node, name: string): void => {
    const names = declarations.get(scope) ?? new Set<string>();
    names.add(name);
    declarations.set(scope, names);
  };
  const lexicalScopes = (node: ts.Node): ts.Node[] => {
    const scopes: ts.Node[] = [];
    let current: ts.Node | undefined = node;
    while (current) {
      if (ts.isBlock(current) || isRuntimeFunctionLike(current) || ts.isSourceFile(current)) scopes.push(current);
      current = current.parent;
    }
    return scopes;
  };
  const declarationScope = (node: ts.Node): ts.Node => (
    lexicalScopes(node).find((scope) => ts.isBlock(scope) || ts.isSourceFile(scope)) ?? rootScope
  );
  for (const name of seedValueNames) declare(rootScope, name);
  for (const member of seedMembers) declare(rootScope, member.split('.')[0]);
  walk(sourceFile, (node) => {
    if (ts.isImportClause(node)) {
      if (node.name) declare(rootScope, node.name.text);
      if (node.namedBindings && ts.isNamedImports(node.namedBindings)) {
        for (const specifier of node.namedBindings.elements) declare(rootScope, specifier.name.text);
      }
      if (node.namedBindings && ts.isNamespaceImport(node.namedBindings)) declare(rootScope, node.namedBindings.name.text);
    }
    if (ts.isVariableDeclaration(node)) {
      const scope = declarationScope(node);
      for (const name of bindingNames(node.name)) declare(scope, name);
    }
    if (ts.isFunctionDeclaration(node) && node.name) declare(declarationScope(node.parent), node.name.text);
    if (isRuntimeFunctionLike(node)) {
      for (const parameter of node.parameters) {
        for (const name of bindingNames(parameter.name)) declare(node, name);
      }
    }
  });
  const bindingKey = (node: ts.Node, name: string): string => {
    const scope = lexicalScopes(node).find((candidate) => declarations.get(candidate)?.has(name)) ?? rootScope;
    const key = `${scope.kind}:${scope.pos}:${scope.end}:${name}`;
    bindingScopes.set(key, scope);
    return key;
  };
  const writes = new Map<string, Write[]>();
  const memberWrites: MemberWrite[] = [];
  const seedProperties = new Map<ObjectId, Map<string, Value>>();
  const seedMemberEntries = [...seedMembers];
  const seedMembersByOwner = new Map<string, Set<string>>();
  const record = (key: string, write: Write): void => {
    const values = writes.get(key) ?? [];
    values.push(write);
    writes.set(key, values);
  };
  for (const name of seedValueNames) {
    record(bindingKey(rootScope, name), { pos: -Infinity, node: rootScope, value: TARGET });
    if (seedLocalValues) {
      for (const [scope, names] of declarations) {
        if (scope !== rootScope && names.has(name)) {
          record(`${scope.kind}:${scope.pos}:${scope.end}:${name}`, {
            pos: -Infinity,
            node: scope,
            value: TARGET,
          });
        }
      }
    }
  }
  for (const member of seedMemberEntries) {
    const [owner, ...path] = member.split('.');
    if (!owner || path.length === 0) continue;
    const rootId: ObjectId = `seed:${owner}`;
    let id: ObjectId = rootId;
    path.forEach((property, index) => {
      const properties = seedProperties.get(id) ?? new Map<string, Value>();
      if (index === path.length - 1) {
        properties.set(property, TARGET);
      } else {
        const nestedId = `seed:${owner}.${path.slice(0, index + 1).join('.')}`;
        properties.set(property, { kind: 'object', id: nestedId });
        seedProperties.set(id, properties);
        id = nestedId;
      }
      seedProperties.set(id, index === path.length - 1 ? properties : seedProperties.get(id) ?? new Map());
    });
    const names = seedMembersByOwner.get(owner) ?? new Set<string>();
    names.add(path[0]);
    seedMembersByOwner.set(owner, names);
    record(bindingKey(rootScope, owner), { pos: -Infinity, node: rootScope, value: { kind: 'object', id: rootId } });
  }
  const constantStrings = new Map<string, string>();
  let stringsChanged = true;
  while (stringsChanged) {
    stringsChanged = false;
    walk(sourceFile, (node) => {
      if (!ts.isVariableDeclaration(node) || !ts.isIdentifier(node.name) || !node.initializer) return;
      const text = staticString(node.initializer, constantStrings);
      if (text !== null && !constantStrings.has(node.name.text)) {
        constantStrings.set(node.name.text, text);
        stringsChanged = true;
      }
    });
  }
  const directMemberName = (
    node: ts.PropertyAccessExpression | ts.ElementAccessExpression,
  ): string | null => (
    ts.isPropertyAccessExpression(node)
      ? node.name.text
      : node.argumentExpression ? staticString(node.argumentExpression, constantStrings) : null
  );
  const objectLiteralInitializers = new Map<string, ts.ObjectLiteralExpression>();
  const mutationBuiltinAliases = new Map<string, string>();
  walk(sourceFile, (node) => {
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer) {
      if (ts.isObjectLiteralExpression(node.initializer)) objectLiteralInitializers.set(node.name.text, node.initializer);
      if (ts.isPropertyAccessExpression(node.initializer)
        && ts.isIdentifier(node.initializer.expression)
        && ((node.initializer.expression.text === 'Reflect'
          && /^(?:set|defineProperty)$/.test(node.initializer.name.text))
          || (node.initializer.expression.text === 'Object'
            && node.initializer.name.text === 'defineProperty'))) {
        mutationBuiltinAliases.set(
          node.name.text,
          `${node.initializer.expression.text}.${node.initializer.name.text}`,
        );
      }
    }
  });
  walk(sourceFile, (node) => {
    if (ts.isFunctionDeclaration(node) && node.name && !seedValueNames.has(node.name.text)) {
      record(bindingKey(node.parent, node.name.text), {
        pos: -Infinity,
        node,
        value: targetFunctionNodes.has(node) ? TARGET : { kind: 'function', node },
      });
    }
    if (ts.isVariableDeclaration(node)) {
      const pos = node.getStart(sourceFile);
      if (ts.isIdentifier(node.name) && !seedValueNames.has(node.name.text)) {
        if (node.initializer) record(bindingKey(node, node.name.text), { pos, node, expression: node.initializer });
        else record(bindingKey(node, node.name.text), { pos, node, value: NULLISH_VALUE });
        if (node.initializer && ts.isObjectLiteralExpression(node.initializer)) {
          const seeded = seedMembersByOwner.get(node.name.text);
          if (seeded) {
            const properties = seedProperties.get(node.initializer) ?? new Map<string, Value>();
            for (const property of seeded) properties.set(property, TARGET);
            seedProperties.set(node.initializer, properties);
          }
        }
      }
      if (node.initializer && ts.isObjectBindingPattern(node.name)) {
        for (const element of node.name.elements) {
          const property = element.propertyName
            ? propertyName(element.propertyName)
            : ts.isIdentifier(element.name) ? element.name.text : null;
          if (!property) continue;
          for (const name of bindingNames(element.name)) {
            record(bindingKey(node, name), { pos, node, source: node.initializer, property });
          }
        }
      }
    }
    if (ts.isBinaryExpression(node)
      && (node.operatorToken.kind === ts.SyntaxKind.EqualsToken
        || node.operatorToken.kind === ts.SyntaxKind.QuestionQuestionEqualsToken
        || node.operatorToken.kind === ts.SyntaxKind.BarBarEqualsToken
        || node.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandEqualsToken)) {
      const pos = node.getStart(sourceFile);
      if (ts.isIdentifier(node.left)) {
        record(bindingKey(node.left, node.left.text), {
          pos,
          node,
          expression: node.right,
          logical: node.operatorToken.kind === ts.SyntaxKind.QuestionQuestionEqualsToken
            ? 'nullish'
            : node.operatorToken.kind === ts.SyntaxKind.BarBarEqualsToken
              ? 'or'
              : node.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandEqualsToken ? 'and' : undefined,
        });
      }
      if (ts.isPropertyAccessExpression(node.left) || ts.isElementAccessExpression(node.left)) {
        const property = directMemberName(node.left);
        if (property) memberWrites.push({
          pos,
          owner: node.left.expression,
          property,
          expression: node.right,
          logical: node.operatorToken.kind === ts.SyntaxKind.QuestionQuestionEqualsToken
            ? 'nullish'
            : node.operatorToken.kind === ts.SyntaxKind.BarBarEqualsToken
              ? 'or'
              : node.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandEqualsToken ? 'and' : undefined,
        });
      }
    }
    if (ts.isCallExpression(node)
      && ts.isPropertyAccessExpression(node.expression)
      && ts.isIdentifier(node.expression.expression)
      && node.expression.expression.text === 'Object'
      && node.expression.name.text === 'assign'
      && node.arguments[0]) {
      for (const rawSource of node.arguments.slice(1)) {
        const source = ts.isObjectLiteralExpression(rawSource)
          ? rawSource
          : ts.isIdentifier(rawSource) ? objectLiteralInitializers.get(rawSource.text) : null;
        if (!source) continue;
        for (const propertyNode of source.properties) {
          if (!ts.isPropertyAssignment(propertyNode) && !ts.isShorthandPropertyAssignment(propertyNode)) continue;
          const property = propertyName(propertyNode.name);
          if (!property) continue;
          memberWrites.push({
            pos: node.getStart(sourceFile),
            owner: node.arguments[0],
            property,
            expression: ts.isPropertyAssignment(propertyNode) ? propertyNode.initializer : propertyNode.name,
          });
        }
      }
    }
    if (ts.isCallExpression(node)
      && ((ts.isPropertyAccessExpression(node.expression)
        && ts.isIdentifier(node.expression.expression))
        || (ts.isIdentifier(node.expression) && mutationBuiltinAliases.has(node.expression.text)))
      && node.arguments[0]) {
      let builtin = '';
      if (ts.isIdentifier(node.expression)) builtin = mutationBuiltinAliases.get(node.expression.text) ?? '';
      else if (ts.isPropertyAccessExpression(node.expression)
        && ts.isIdentifier(node.expression.expression)) {
        builtin = `${node.expression.expression.text}.${node.expression.name.text}`;
      }
      let property: string | null = null;
      let expression: ts.Expression | null = null;
      if (builtin === 'Reflect.set' && node.arguments[1] && node.arguments[2]) {
        property = staticString(node.arguments[1], constantStrings);
        expression = node.arguments[2];
      }
      if (/^(?:Object|Reflect)\.defineProperty$/.test(builtin) && node.arguments[1] && node.arguments[2]
        && ts.isObjectLiteralExpression(node.arguments[2])) {
        property = staticString(node.arguments[1], constantStrings);
        const valueProperty = [...node.arguments[2].properties].reverse().find((candidate) => (
          ts.isPropertyAssignment(candidate) && propertyName(candidate.name) === 'value'
        ));
        if (valueProperty && ts.isPropertyAssignment(valueProperty)) expression = valueProperty.initializer;
      }
      if (property && expression) memberWrites.push({
        pos: node.getStart(sourceFile),
        owner: node.arguments[0],
        property,
        expression,
      });
    }
  });
  for (const values of writes.values()) values.sort((left, right) => left.pos - right.pos);
  memberWrites.sort((left, right) => left.pos - right.pos);
  const functionCallPositions = new Map<string, number[]>();
  walk(sourceFile, (node) => {
    if (!ts.isCallExpression(node)) return;
    const call = normalizedCall(node);
    if (!ts.isIdentifier(call.callee)) return;
    const key = bindingKey(call.callee, call.callee.text);
    const positions = functionCallPositions.get(key) ?? [];
    positions.push(node.getStart(sourceFile));
    functionCallPositions.set(key, positions);
  });
  const functionBindingKey = (node: RuntimeFunctionLike): string | null => {
    const name = functionName(node);
    if (!name) return null;
    if (ts.isFunctionDeclaration(node)) return bindingKey(node.parent, name);
    return bindingKey(node.parent, name);
  };
  const callableRoots = new Map<string, string>();
  const callableMembers = new Map<string, string>();
  const functionKeysByName = new Map<string, string>();
  walk(sourceFile, (node) => {
    if (!isRuntimeFunctionLike(node)) return;
    const name = functionName(node);
    const key = functionBindingKey(node);
    if (name && key) {
      callableRoots.set(name, name);
      functionKeysByName.set(name, key);
    }
  });
  let aliasesChanged = true;
  while (aliasesChanged) {
    aliasesChanged = false;
    walk(sourceFile, (node) => {
      if (!ts.isVariableDeclaration(node) || !ts.isIdentifier(node.name) || !node.initializer) return;
      if (ts.isIdentifier(node.initializer)) {
        const root = callableRoots.get(node.initializer.text);
        if (root && callableRoots.get(node.name.text) !== root) {
          callableRoots.set(node.name.text, root);
          aliasesChanged = true;
        }
      }
      if (ts.isObjectLiteralExpression(node.initializer)) {
        for (const property of node.initializer.properties) {
          if (!ts.isPropertyAssignment(property) && !ts.isShorthandPropertyAssignment(property)) continue;
          const key = propertyName(property.name);
          const expression = ts.isPropertyAssignment(property) ? property.initializer : property.name;
          if (!key || !ts.isIdentifier(expression)) continue;
          const root = callableRoots.get(expression.text);
          const member = `${node.name.text}.${key}`;
          if (root && callableMembers.get(member) !== root) {
            callableMembers.set(member, root);
            aliasesChanged = true;
          }
        }
      }
    });
  }
  walk(sourceFile, (node) => {
    if (!ts.isCallExpression(node)) return;
    const call = normalizedCall(node);
    let root: string | undefined;
    if (ts.isIdentifier(call.callee)) root = callableRoots.get(call.callee.text);
    if (ts.isPropertyAccessExpression(call.callee)) {
      if (ts.isIdentifier(call.callee.expression)) {
        root = callableMembers.get(`${call.callee.expression.text}.${call.callee.name.text}`);
      }
    }
    const key = root && functionKeysByName.get(root);
    if (!key) return;
    const positions = functionCallPositions.get(key) ?? [];
    const position = node.getStart(sourceFile);
    if (!positions.includes(position)) positions.push(position);
    functionCallPositions.set(key, positions);
  });

  const sameValue = (left: Value, right: Value): boolean => (
    left.kind === right.kind
    && (left.kind === 'target'
      || left.kind === 'unknown'
      || left.kind === 'nullish'
      || (left.kind === 'object' && right.kind === 'object' && left.id === right.id)
      || (left.kind === 'function' && right.kind === 'function' && left.node === right.node)
      || (left.kind === 'union' && right.kind === 'union'
        && left.values.length === right.values.length
        && left.values.every((candidate) => right.values.some((other) => sameValue(candidate, other)))))
  );
  const joinValues = (...candidates: Value[]): Value => {
    const flat = candidates.flatMap((candidate) => candidate.kind === 'union' ? candidate.values : [candidate]);
    const unique = flat.filter((candidate, index) => (
      flat.findIndex((other) => sameValue(candidate, other)) === index
    ));
    return unique.length === 1 ? unique[0] : { kind: 'union', values: unique };
  };
  const containsTarget = (candidate: Value): boolean => (
    candidate.kind === 'target'
    || (candidate.kind === 'union' && candidate.values.some(containsTarget))
  );
  const branchOf = (node: ts.Node): { owner: ts.IfStatement; side: 'then' | 'else' } | null => {
    let current: ts.Node = node;
    while (current.parent) {
      if (ts.isIfStatement(current.parent)) {
        if (current.parent.thenStatement === current) return { owner: current.parent, side: 'then' };
        if (current.parent.elseStatement === current) return { owner: current.parent, side: 'else' };
      }
      current = current.parent;
    }
    return null;
  };
  const staticBoolean = (node: ts.Expression): boolean | null => {
    return staticBooleanValue(node);
  };
  const nodeWithin = (node: ts.Node, owner: ts.Node): boolean => (
    node === owner || (node.pos >= owner.pos && node.end <= owner.end)
  );
  const definitelyTerminates = (statement: ts.Statement): boolean => {
    if (ts.isReturnStatement(statement) || ts.isThrowStatement(statement)
      || ts.isBreakStatement(statement) || ts.isContinueStatement(statement)) return true;
    if (ts.isBlock(statement)) return statement.statements.some(definitelyTerminates);
    if (ts.isIfStatement(statement)) {
      const condition = staticBoolean(statement.expression);
      if (condition === true) return definitelyTerminates(statement.thenStatement);
      if (condition === false) return Boolean(statement.elseStatement && definitelyTerminates(statement.elseStatement));
      return definitelyTerminates(statement.thenStatement)
        && Boolean(statement.elseStatement && definitelyTerminates(statement.elseStatement));
    }
    return false;
  };
  const isReachable = (node: ts.Node): boolean => {
    let current: ts.Node = node;
    while (current.parent) {
      const parent = current.parent;
      if (ts.isWhileStatement(parent) && parent.statement === current
        && staticBoolean(parent.expression) === false) return false;
      if (ts.isForStatement(parent) && parent.statement === current
        && parent.condition && staticBoolean(parent.condition) === false) return false;
      if (ts.isIfStatement(parent)) {
        const condition = staticBoolean(parent.expression);
        if (condition === true && parent.elseStatement === current) return false;
        if (condition === false && parent.thenStatement === current) return false;
      }
      if (ts.isBlock(parent)) {
        const statement = parent.statements.find((candidate) => nodeWithin(current, candidate));
        if (statement) {
          const index = parent.statements.indexOf(statement);
          if (parent.statements.slice(0, index).some(definitelyTerminates)) return false;
        }
      }
      current = parent;
    }
    return true;
  };
  const loopOf = (node: ts.Node): ts.IterationStatement | null => {
    let current: ts.Node = node;
    while (current.parent) {
      const parent = current.parent;
      if ((ts.isWhileStatement(parent) || ts.isDoStatement(parent)
        || ts.isForStatement(parent) || ts.isForInStatement(parent) || ts.isForOfStatement(parent))
        && nodeWithin(current, parent.statement)) return parent;
      if (isRuntimeFunctionLike(parent)) return null;
      current = parent;
    }
    return null;
  };
  const loopBreakPositions = (owner: ts.IterationStatement): number[] => {
    const positions: number[] = [];
    walk(owner.statement, (candidate) => {
      if (!ts.isBreakStatement(candidate) || !isReachable(candidate)) return;
      let current: ts.Node | undefined = candidate.parent;
      while (current && current !== owner) {
        if (isRuntimeFunctionLike(current)
          || (ts.isIterationStatement(current, false) && current !== owner)
          || ts.isSwitchStatement(current)) return;
        current = current.parent;
      }
      if (current === owner) positions.push(candidate.getStart(sourceFile));
    });
    return positions.sort((left, right) => left - right);
  };
  const potentialThrowPositions = (block: ts.Block): number[] => {
    const positions: number[] = [];
    const visit = (candidate: ts.Node): void => {
      if (candidate !== block && isRuntimeFunctionLike(candidate)) return;
      if ((ts.isCallExpression(candidate) || ts.isThrowStatement(candidate)) && isReachable(candidate)) {
        positions.push(candidate.getStart(sourceFile));
      }
      ts.forEachChild(candidate, visit);
    };
    visit(block);
    return positions.sort((left, right) => left - right);
  };
  const switchOf = (node: ts.Node): { owner: ts.SwitchStatement; clause: ts.CaseOrDefaultClause } | null => {
    let current: ts.Node = node;
    while (current.parent) {
      if (ts.isCaseClause(current.parent) || ts.isDefaultClause(current.parent)) {
        const clause = current.parent;
        const owner = clause.parent.parent;
        return ts.isSwitchStatement(owner) ? { owner, clause } : null;
      }
      if (isRuntimeFunctionLike(current.parent)) return null;
      current = current.parent;
    }
    return null;
  };
  const switchBreakPositions = (
    clause: ts.CaseOrDefaultClause,
    owner: ts.SwitchStatement,
  ): number[] => {
    const positions: number[] = [];
    walk(clause, (candidate) => {
      if (!ts.isBreakStatement(candidate)) return;
      let current: ts.Node | undefined = candidate.parent;
      while (current && current !== owner) {
        if (isRuntimeFunctionLike(current)
          || ts.isIterationStatement(current, false)
          || (ts.isSwitchStatement(current) && current !== owner)) return;
        current = current.parent;
      }
      if (current === owner) positions.push(candidate.getStart(sourceFile));
    });
    return positions.sort((left, right) => left - right);
  };
  const clauseDefinitelyStops = (
    clause: ts.CaseOrDefaultClause,
    owner: ts.SwitchStatement,
  ): boolean => {
    const directFlowStops = (statement: ts.Statement): boolean => {
      if (ts.isBreakStatement(statement) || ts.isReturnStatement(statement)
        || ts.isThrowStatement(statement)) return true;
      if (ts.isBlock(statement)) return statement.statements.some(directFlowStops);
      if (ts.isIfStatement(statement)) {
        const condition = staticBoolean(statement.expression);
        if (condition === true) return directFlowStops(statement.thenStatement);
        if (condition === false) return Boolean(statement.elseStatement && directFlowStops(statement.elseStatement));
        return directFlowStops(statement.thenStatement)
          && Boolean(statement.elseStatement && directFlowStops(statement.elseStatement));
      }
      return false;
    };
    return switchBreakPositions(clause, owner).length > 0
      && clause.statements.some(directFlowStops);
  };
  const tryOf = (node: ts.Node): { owner: ts.TryStatement; side: 'try' | 'catch' | 'finally' } | null => {
    let current: ts.Node = node;
    while (current.parent) {
      if (ts.isTryStatement(current.parent)) {
        if (current.parent.tryBlock === current) return { owner: current.parent, side: 'try' };
        if (current.parent.catchClause === current) return { owner: current.parent, side: 'catch' };
        if (current.parent.finallyBlock === current) return { owner: current.parent, side: 'finally' };
      }
      if (isRuntimeFunctionLike(current.parent)) return null;
      current = current.parent;
    }
    return null;
  };
  const joinOuterConditionalControls = (
    node: ts.Node,
    handled: ts.Node,
    baseline: Value,
    nested: Value,
  ): Value => {
    let state = nested;
    let current: ts.Node | undefined = handled;
    while (current?.parent) {
      const parent: ts.Node = current.parent;
      if (isRuntimeFunctionLike(parent)) break;
      if ((ts.isWhileStatement(parent) || ts.isForStatement(parent)
          || ts.isForInStatement(parent) || ts.isForOfStatement(parent))
        && nodeWithin(node, parent.statement)) {
        const condition = ts.isWhileStatement(parent)
          ? staticBoolean(parent.expression)
          : ts.isForStatement(parent) && parent.condition
            ? staticBoolean(parent.condition)
            : null;
        if (condition !== true) state = joinValues(baseline, state);
      } else if (ts.isIfStatement(parent)
        && (nodeWithin(node, parent.thenStatement)
          || Boolean(parent.elseStatement && nodeWithin(node, parent.elseStatement)))) {
        const condition = staticBoolean(parent.expression);
        const onThen = nodeWithin(node, parent.thenStatement);
        const selected = condition === null || (condition === true) === onThen;
        if (condition === null) state = joinValues(baseline, state);
        else if (!selected) state = baseline;
      }
      current = parent;
    }
    return state;
  };
  const value = (
    raw: ts.Expression,
    before: number,
    active = new Set<string>(),
    environment = new Map<string, Value>(),
  ): Value => {
    const node = ts.isParenthesizedExpression(raw) || ts.isAsExpression(raw)
      || ts.isNonNullExpression(raw) || ts.isSatisfiesExpression(raw) ? raw.expression : raw;
    const guard = `${node.kind}:${node.pos}:${node.end}:${before}`;
    if (active.has(guard)) return UNKNOWN_VALUE;
    const next = new Set(active).add(guard);
    if (ts.isIdentifier(node)) {
      const key = bindingKey(node, node.text);
      const contextual = environment.get(key);
      if (contextual) return contextual;
      type EffectiveWrite = { write: Write; pos: number };
      const candidates: EffectiveWrite[] = (writes.get(key) ?? []).flatMap((write) => {
        if (!isReachable(write.node)) return [];
        const writer = containingFunction(write.node);
        const scope = bindingScopes.get(key);
        const captured = Boolean(writer && scope && !nodeWithin(scope, writer!));
        if (!captured) return write.pos < before ? [{ write, pos: write.pos }] : [];
        const writerKey = writer && functionBindingKey(writer);
        return writerKey
          ? (functionCallPositions.get(writerKey) ?? [])
            .filter((position) => position < before)
            .map((position) => ({ write, pos: position }))
          : [];
      }).sort((left, right) => left.pos - right.pos);
      let state: Value = UNKNOWN_VALUE;
      const processedControls = new Set<ts.Node>();
      const writeValue = (candidate: EffectiveWrite): Value => {
        const { write } = candidate;
        if ('value' in write) return write.value;
        if ('source' in write) {
          const owner = value(write.source, candidate.pos, next, environment);
          if (owner.kind === 'object') {
            return propertyValueAt(owner.id, write.property, candidate.pos, next, environment);
          }
          if (owner.kind === 'union') {
            return joinValues(...owner.values.map((candidate) => (
              candidate.kind === 'object'
                ? propertyValueAt(candidate.id, write.property, before, next, environment)
                : UNKNOWN_VALUE
            )));
          }
          return UNKNOWN_VALUE;
        }
        return value(write.expression, candidate.pos, next, environment);
      };
      const applyWrite = (candidate: EffectiveWrite, previous: Value): Value => {
        const assigned = writeValue(candidate);
        if (!('expression' in candidate.write) || !candidate.write.logical) return assigned;
        if (candidate.write.logical === 'and') {
          if (previous.kind === 'nullish') return previous;
          if (previous.kind === 'unknown') return joinValues(previous, assigned);
          if (previous.kind === 'union') {
            return joinValues(...previous.values.map((part) => part.kind === 'nullish' ? part : assigned));
          }
          return assigned;
        }
        if (previous.kind === 'nullish') return assigned;
        if (previous.kind === 'unknown') return joinValues(previous, assigned);
        if (previous.kind === 'union') {
          return joinValues(...previous.values.map((part) => part.kind === 'nullish' ? assigned : part));
        }
        return previous;
      };
      for (const candidate of candidates) {
        const branch = branchOf(candidate.write.node);
        if (branch && branch.owner.end < before) {
          if (processedControls.has(branch.owner)) continue;
          processedControls.add(branch.owner);
          const branchWrites = candidates.filter((write) => branchOf(write.write.node)?.owner === branch.owner);
          const thenWrite = [...branchWrites].reverse().find((write) => branchOf(write.write.node)?.side === 'then');
          const elseWrite = [...branchWrites].reverse().find((write) => branchOf(write.write.node)?.side === 'else');
          const baseline = state;
          state = joinOuterConditionalControls(candidate.write.node, branch.owner, baseline, joinValues(
            thenWrite ? writeValue(thenWrite) : state,
            elseWrite ? writeValue(elseWrite) : state,
          ));
          continue;
        }
        const loop = loopOf(candidate.write.node);
        if (loop && loop.end < before) {
          if (processedControls.has(loop)) continue;
          processedControls.add(loop);
          const loopWrites = candidates.filter((write) => loopOf(write.write.node) === loop);
          if (loopWrites.length > 0) {
            const baseline = state;
            const exits: Value[] = [];
            const breaks = loopBreakPositions(loop);
            let pathState = state;
            for (const loopWrite of loopWrites) {
              if (breaks.some((position) => position < loopWrite.write.node.getStart(sourceFile))) {
                exits.push(pathState);
              }
              pathState = applyWrite(loopWrite, pathState);
            }
            exits.push(pathState);
            const loopState = ts.isDoStatement(loop)
              ? joinValues(...exits)
              : joinValues(state, ...exits);
            state = joinOuterConditionalControls(candidate.write.node, loop, baseline, loopState);
          }
          continue;
        }
        const switchBranch = switchOf(candidate.write.node);
        if (switchBranch && switchBranch.owner.end < before) {
          if (processedControls.has(switchBranch.owner)) continue;
          processedControls.add(switchBranch.owner);
          const switchWrites = candidates.filter((write) => switchOf(write.write.node)?.owner === switchBranch.owner);
          const clauses = switchBranch.owner.caseBlock.clauses;
          const exits: Value[] = [];
          clauses.forEach((_, entry) => {
            let pathState = state;
            for (let index = entry; index < clauses.length; index += 1) {
              const clause = clauses[index];
              const breaks = switchBreakPositions(clause, switchBranch.owner);
              const writesInClause = switchWrites.filter((candidate) => switchOf(candidate.write.node)?.clause === clause);
              for (const candidateWrite of writesInClause) {
                if (breaks.some((position) => position < candidateWrite.write.node.getStart(sourceFile))) {
                  exits.push(pathState);
                }
                pathState = writeValue(candidateWrite);
              }
              if (breaks.length > 0) exits.push(pathState);
              if (clauseDefinitelyStops(clause, switchBranch.owner)) break;
              if (index === clauses.length - 1) exits.push(pathState);
            }
          });
          if (!clauses.some(ts.isDefaultClause)) exits.push(state);
          state = joinValues(...exits);
          continue;
        }
        const tryBranch = tryOf(candidate.write.node);
        if (tryBranch && tryBranch.owner.end < before) {
          if (processedControls.has(tryBranch.owner)) continue;
          processedControls.add(tryBranch.owner);
          const tryWrites = candidates.filter((write) => tryOf(write.write.node)?.owner === tryBranch.owner);
          const finalOn = (side: 'try' | 'catch' | 'finally'): EffectiveWrite | undefined => (
            [...tryWrites].reverse().find((write) => tryOf(write.write.node)?.side === side)
          );
          const trySideWrites = tryWrites.filter((write) => tryOf(write.write.node)?.side === 'try');
          const catchWrite = finalOn('catch');
          let tryState = state;
          const thrownStates: Value[] = [];
          let cursor = tryBranch.owner.tryBlock.getStart(sourceFile);
          const throws = potentialThrowPositions(tryBranch.owner.tryBlock);
          for (const tryWrite of trySideWrites) {
            if (throws.some((position) => position >= cursor && position < tryWrite.write.node.getStart(sourceFile))) {
              thrownStates.push(tryState);
            }
            tryState = applyWrite(tryWrite, tryState);
            cursor = tryWrite.write.node.end;
          }
          if (throws.some((position) => position >= cursor)) thrownStates.push(tryState);
          const thrownState = thrownStates.length > 0 ? joinValues(...thrownStates) : tryState;
          const catchState = catchWrite
            ? (branchOf(catchWrite.write.node)
              ? joinValues(thrownState, applyWrite(catchWrite, thrownState))
              : applyWrite(catchWrite, thrownState))
            : thrownState;
          const definitelyThrows = tryBranch.owner.tryBlock.statements.some(statementDefinitelyTerminates);
          state = definitelyThrows && tryBranch.owner.catchClause
            ? catchState
            : joinValues(tryState, catchState);
          const finallyWrite = finalOn('finally');
          if (finallyWrite) {
            const previous = state;
            const applied = applyWrite(finallyWrite, previous);
            state = branchOf(finallyWrite.write.node)
              ? joinValues(previous, applied)
              : applied;
          }
          continue;
        }
        state = applyWrite(candidate, state);
      }
      return state;
    }
    if (ts.isObjectLiteralExpression(node)) return { kind: 'object', id: node };
    if (ts.isArrowFunction(node) || ts.isFunctionExpression(node)) {
      return targetFunctionNodes.has(node) ? TARGET : { kind: 'function', node };
    }
    if (ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node)) {
      const property = directMemberName(node);
      const owner = value(node.expression, before, next, environment);
      if (!property) return UNKNOWN_VALUE;
      if (owner.kind === 'object') return propertyValueAt(owner.id, property, before, next, environment);
      if (owner.kind === 'union') {
        return joinValues(...owner.values.map((candidate) => (
          candidate.kind === 'object'
            ? propertyValueAt(candidate.id, property, before, next, environment)
            : UNKNOWN_VALUE
        )));
      }
      return UNKNOWN_VALUE;
    }
    if (ts.isCallExpression(node)) {
      if (ts.isPropertyAccessExpression(node.expression)
        && node.expression.name.text === 'bind') {
        const target = value(node.expression.expression, before, next, environment);
        if (containsTarget(target)) return TARGET;
        if (target.kind === 'function' || target.kind === 'union') return target;
      }
      const call = normalizedCall(node);
      const callee = value(call.callee, before, next, environment);
      const invoke = (candidate: Value): Value => {
        if (candidate.kind !== 'function') return UNKNOWN_VALUE;
        const childEnvironment = new Map(environment);
        candidate.node.parameters.forEach((parameter, index) => {
          const argument = call.arguments[index];
          for (const name of bindingNames(parameter.name)) {
            childEnvironment.set(
              bindingKey(parameter, name),
              argument ? value(argument, before, next, environment) : UNKNOWN_VALUE,
            );
          }
        });
        return functionReturn(candidate.node, before, next, childEnvironment);
      };
      return callee.kind === 'union'
        ? joinValues(...callee.values.map(invoke))
        : invoke(callee);
    }
    if (ts.isConditionalExpression(node)) {
      return joinValues(
        value(node.whenTrue, before, next, environment),
        value(node.whenFalse, before, next, environment),
      );
    }
    return UNKNOWN_VALUE;
  };
  const propertyValueAt = (
    id: ObjectId,
    property: string,
    before: number,
    active: Set<string>,
    environment: Map<string, Value>,
  ): Value => {
    const guard = `property:${typeof id === 'string' ? id : id.pos}:${property}:${before}`;
    if (active.has(guard)) return UNKNOWN_VALUE;
    const next = new Set(active).add(guard);
    const seeded = seedProperties.get(id)?.get(property) ?? seedProperties.get(id)?.get('*');
    let state: Value = seeded ?? UNKNOWN_VALUE;
    if (!seeded && typeof id !== 'string') {
      for (const item of [...id.properties].reverse()) {
        if (ts.isSpreadAssignment(item)) {
          const spread = value(item.expression, id.getStart(sourceFile), next, environment);
          if (spread.kind === 'object') {
            const nested = propertyValueAt(spread.id, property, id.getStart(sourceFile), next, environment);
            if (nested.kind !== 'unknown') { state = nested; break; }
          }
          continue;
        }
        if (ts.isPropertyAssignment(item) && propertyName(item.name) === property) {
          state = value(item.initializer, id.getStart(sourceFile), next, environment);
          break;
        }
        if (ts.isShorthandPropertyAssignment(item) && item.name.text === property) {
          state = value(item.name, id.getStart(sourceFile), next, environment);
          break;
        }
        if (ts.isMethodDeclaration(item) && propertyName(item.name) === property) {
          state = targetFunctionNodes.has(item) ? TARGET : { kind: 'function', node: item };
          break;
        }
      }
    }
    const relevant = memberWrites.filter((write) => {
      if (write.pos >= before || write.property !== property || !isReachable(write.owner)) return false;
      const owner = value(write.owner, write.pos, next, environment);
      return owner.kind === 'object' && owner.id === id;
    });
    const processedControls = new Set<ts.Node>();
    const applyMemberWrite = (write: MemberWrite, previous: Value): Value => {
      const assigned = value(write.expression, write.pos, next, environment);
      if (!write.logical) return assigned;
      if (write.logical === 'and') {
        if (previous.kind === 'nullish') return previous;
        if (previous.kind === 'unknown') return joinValues(previous, assigned);
        if (previous.kind === 'union') {
          return joinValues(...previous.values.map((part) => part.kind === 'nullish' ? part : assigned));
        }
        return assigned;
      }
      if (previous.kind === 'nullish') return assigned;
      if (previous.kind === 'unknown') return joinValues(previous, assigned);
      if (previous.kind === 'union') {
        return joinValues(...previous.values.map((part) => part.kind === 'nullish' ? assigned : part));
      }
      return previous;
    };
    for (const write of relevant) {
      const branch = branchOf(write.owner);
      if (branch && branch.owner.end < before) {
        if (processedControls.has(branch.owner)) continue;
        processedControls.add(branch.owner);
        const branchWrites = relevant.filter((candidate) => branchOf(candidate.owner)?.owner === branch.owner);
        const thenWrite = [...branchWrites].reverse().find((candidate) => branchOf(candidate.owner)?.side === 'then');
        const elseWrite = [...branchWrites].reverse().find((candidate) => branchOf(candidate.owner)?.side === 'else');
        const baseline = state;
        state = joinOuterConditionalControls(write.owner, branch.owner, baseline, joinValues(
          thenWrite ? applyMemberWrite(thenWrite, state) : state,
          elseWrite ? applyMemberWrite(elseWrite, state) : state,
        ));
        continue;
      }
      const loop = loopOf(write.owner);
      if (loop && loop.end < before) {
        if (processedControls.has(loop)) continue;
        processedControls.add(loop);
        const loopWrites = relevant.filter((candidate) => loopOf(candidate.owner) === loop);
        if (loopWrites.length > 0) {
          const baseline = state;
          const exits: Value[] = [];
          const breaks = loopBreakPositions(loop);
          let pathState = state;
          for (const loopWrite of loopWrites) {
            if (breaks.some((position) => position < loopWrite.owner.getStart(sourceFile))) {
              exits.push(pathState);
            }
            pathState = applyMemberWrite(loopWrite, pathState);
          }
          exits.push(pathState);
          const loopState = ts.isDoStatement(loop)
            ? joinValues(...exits)
            : joinValues(state, ...exits);
          state = joinOuterConditionalControls(write.owner, loop, baseline, loopState);
        }
        continue;
      }
      const switchBranch = switchOf(write.owner);
      if (switchBranch && switchBranch.owner.end < before) {
        if (processedControls.has(switchBranch.owner)) continue;
        processedControls.add(switchBranch.owner);
        const switchWrites = relevant.filter((candidate) => switchOf(candidate.owner)?.owner === switchBranch.owner);
        const clauses = switchBranch.owner.caseBlock.clauses;
        const exits: Value[] = [];
        clauses.forEach((_, entry) => {
          let pathState = state;
          for (let index = entry; index < clauses.length; index += 1) {
            const clause = clauses[index];
            const breaks = switchBreakPositions(clause, switchBranch.owner);
            const writes = switchWrites.filter((candidate) => switchOf(candidate.owner)?.clause === clause);
            for (const candidateWrite of writes) {
              if (breaks.some((position) => position < candidateWrite.owner.getStart(sourceFile))) {
                exits.push(pathState);
              }
              pathState = applyMemberWrite(candidateWrite, pathState);
            }
            if (breaks.length > 0) exits.push(pathState);
            if (clauseDefinitelyStops(clause, switchBranch.owner)) break;
            if (index === clauses.length - 1) exits.push(pathState);
          }
        });
        if (!clauses.some(ts.isDefaultClause)) exits.push(state);
        state = joinValues(...exits);
        continue;
      }
      const tryBranch = tryOf(write.owner);
      if (tryBranch && tryBranch.owner.end < before) {
        if (processedControls.has(tryBranch.owner)) continue;
        processedControls.add(tryBranch.owner);
        const tryWrites = relevant.filter((candidate) => tryOf(candidate.owner)?.owner === tryBranch.owner);
        const finalOn = (side: 'try' | 'catch' | 'finally'): MemberWrite | undefined => (
          [...tryWrites].reverse().find((candidate) => tryOf(candidate.owner)?.side === side)
        );
        const trySideWrites = tryWrites.filter((candidate) => tryOf(candidate.owner)?.side === 'try');
        const catchWrite = finalOn('catch');
        let tryState = state;
        const thrownStates: Value[] = [];
        let cursor = tryBranch.owner.tryBlock.getStart(sourceFile);
        const throws = potentialThrowPositions(tryBranch.owner.tryBlock);
        for (const tryWrite of trySideWrites) {
          if (throws.some((position) => position >= cursor && position < tryWrite.owner.getStart(sourceFile))) {
            thrownStates.push(tryState);
          }
          tryState = applyMemberWrite(tryWrite, tryState);
          cursor = tryWrite.owner.end;
        }
        if (throws.some((position) => position >= cursor)) thrownStates.push(tryState);
        const thrownState = thrownStates.length > 0 ? joinValues(...thrownStates) : tryState;
        const catchState = catchWrite
          ? (branchOf(catchWrite.owner)
            ? joinValues(thrownState, applyMemberWrite(catchWrite, thrownState))
            : applyMemberWrite(catchWrite, thrownState))
          : thrownState;
        const definitelyThrows = tryBranch.owner.tryBlock.statements.some(statementDefinitelyTerminates);
        state = definitelyThrows && tryBranch.owner.catchClause
          ? catchState
          : joinValues(tryState, catchState);
        const finallyWrite = finalOn('finally');
        if (finallyWrite) {
          const previous = state;
          const applied = applyMemberWrite(finallyWrite, previous);
          state = branchOf(finallyWrite.owner) ? joinValues(previous, applied) : applied;
        }
        continue;
      }
      state = applyMemberWrite(write, state);
    }
    return state;
  };
  const functionReturn = (
    node: RuntimeFunctionLike,
    before: number,
    active: Set<string>,
    environment: Map<string, Value>,
  ): Value => {
    if (!node.body) return UNKNOWN_VALUE;
    if (!ts.isBlock(node.body)) return value(node.body, before, active, environment);
    type ReturnFlow = { returns: Value[]; fallsThrough: boolean };
    const sequence = (statements: readonly ts.Statement[]): ReturnFlow => {
      const returns: Value[] = [];
      let fallsThrough = true;
      for (const statement of statements) {
        if (!fallsThrough) break;
        const result = statementFlow(statement);
        returns.push(...result.returns);
        fallsThrough = result.fallsThrough;
      }
      return { returns, fallsThrough };
    };
    const statementFlow = (statement: ts.Statement): ReturnFlow => {
      if (ts.isReturnStatement(statement)) {
        return {
          returns: [statement.expression
            ? value(statement.expression, before, active, environment)
            : UNKNOWN_VALUE],
          fallsThrough: false,
        };
      }
      if (ts.isThrowStatement(statement)) return { returns: [], fallsThrough: false };
      if (ts.isBlock(statement)) return sequence(statement.statements);
      if (ts.isIfStatement(statement)) {
        const condition = staticBoolean(statement.expression);
        if (condition === true) return statementFlow(statement.thenStatement);
        if (condition === false) {
          return statement.elseStatement
            ? statementFlow(statement.elseStatement)
            : { returns: [], fallsThrough: true };
        }
        const thenFlow = statementFlow(statement.thenStatement);
        const elseFlow = statement.elseStatement
          ? statementFlow(statement.elseStatement)
          : { returns: [], fallsThrough: true };
        return {
          returns: [...thenFlow.returns, ...elseFlow.returns],
          fallsThrough: thenFlow.fallsThrough || elseFlow.fallsThrough,
        };
      }
      if (ts.isWhileStatement(statement)) {
        const condition = staticBoolean(statement.expression);
        if (condition === false) return { returns: [], fallsThrough: true };
        const body = statementFlow(statement.statement);
        return { returns: body.returns, fallsThrough: condition !== true };
      }
      if (ts.isDoStatement(statement)) {
        const body = statementFlow(statement.statement);
        if (!body.fallsThrough) return body;
        return {
          returns: body.returns,
          fallsThrough: staticBoolean(statement.expression) !== true,
        };
      }
      if (ts.isForStatement(statement)) {
        const condition = statement.condition ? staticBoolean(statement.condition) : true;
        if (condition === false) return { returns: [], fallsThrough: true };
        const body = statementFlow(statement.statement);
        return { returns: body.returns, fallsThrough: condition !== true };
      }
      if (ts.isForInStatement(statement) || ts.isForOfStatement(statement)) {
        const body = statementFlow(statement.statement);
        return { returns: body.returns, fallsThrough: true };
      }
      if (ts.isSwitchStatement(statement)) {
        const flows = statement.caseBlock.clauses.map((clause) => sequence(clause.statements));
        return {
          returns: flows.flatMap((flow) => flow.returns),
          fallsThrough: !statement.caseBlock.clauses.some(ts.isDefaultClause)
            || flows.some((flow) => flow.fallsThrough),
        };
      }
      if (ts.isTryStatement(statement)) {
        const tryFlow = sequence(statement.tryBlock.statements);
        const catchFlow = statement.catchClause
          ? sequence(statement.catchClause.block.statements)
          : { returns: [], fallsThrough: true };
        const combined: ReturnFlow = {
          returns: [...tryFlow.returns, ...catchFlow.returns],
          fallsThrough: tryFlow.fallsThrough || catchFlow.fallsThrough,
        };
        if (!statement.finallyBlock) return combined;
        const finallyFlow = sequence(statement.finallyBlock.statements);
        return finallyFlow.fallsThrough
          ? { returns: [...combined.returns, ...finallyFlow.returns], fallsThrough: combined.fallsThrough }
          : finallyFlow;
      }
      return { returns: [], fallsThrough: true };
    };
    const flow = sequence(node.body.statements);
    const candidates = flow.fallsThrough ? [...flow.returns, UNKNOWN_VALUE] : flow.returns;
    return candidates.length > 0 ? joinValues(...candidates) : UNKNOWN_VALUE;
  };
  const hasCapturedWrites = [...writes].some(([key, candidates]) => {
    const scope = bindingScopes.get(key);
    return Boolean(scope && candidates.some((candidate) => {
      const writer = containingFunction(candidate.node);
      return writer && !nodeWithin(scope, writer);
    }));
  });
  if (hasCapturedWrites) {
    let callPointsChanged = true;
    while (callPointsChanged) {
      callPointsChanged = false;
      walk(sourceFile, (node) => {
        if (!ts.isCallExpression(node)) return;
        const position = node.getStart(sourceFile);
        const call = normalizedCall(node);
        const callee = value(call.callee, position);
        const callableNodes = (candidate: Value): RuntimeFunctionLike[] => (
          candidate.kind === 'function'
            ? [candidate.node]
            : candidate.kind === 'union' ? candidate.values.flatMap(callableNodes) : []
        );
        for (const callable of callableNodes(callee)) {
          const key = functionBindingKey(callable);
          if (!key) continue;
          const positions = functionCallPositions.get(key) ?? [];
          if (!positions.includes(position)) {
            positions.push(position);
            positions.sort((left, right) => left - right);
            functionCallPositions.set(key, positions);
            callPointsChanged = true;
          }
          callable.parameters.forEach((parameter, index) => {
            const argument = call.arguments[index];
            if (!argument || !callable.body) return;
            const parameterNames = new Set(bindingNames(parameter.name));
            const invokedNames = new Set<string>();
            walk(callable.body, (child) => {
              if (ts.isCallExpression(child) && ts.isIdentifier(child.expression)
                && parameterNames.has(child.expression.text)) invokedNames.add(child.expression.text);
              if (ts.isCallExpression(child) && ts.isPropertyAccessExpression(child.expression)
                && /^(?:call|apply)$/.test(child.expression.name.text)
                && ts.isIdentifier(child.expression.expression)
                && parameterNames.has(child.expression.expression.text)) invokedNames.add(child.expression.expression.text);
            });
            if (invokedNames.size === 0) return;
            const argumentValue = value(argument, position);
            const passedValues: Value[] = [];
            const collectArrayBinding = (pattern: ts.ArrayBindingPattern, expression: ts.Expression): void => {
              if (!ts.isArrayLiteralExpression(expression)) return;
              pattern.elements.forEach((element, elementIndex) => {
                if (ts.isOmittedExpression(element)) return;
                const passed = expression.elements[elementIndex];
                if (!passed) return;
                if (ts.isArrayBindingPattern(element.name)) {
                  collectArrayBinding(element.name, passed);
                  return;
                }
                const localNames = bindingNames(element.name);
                if (localNames.some((name) => invokedNames.has(name))) {
                  passedValues.push(value(passed, position));
                }
              });
            };
            if (ts.isIdentifier(parameter.name) && invokedNames.has(parameter.name.text)) {
              passedValues.push(argumentValue);
            } else if (ts.isObjectBindingPattern(parameter.name)) {
              for (const element of parameter.name.elements) {
                const localNames = bindingNames(element.name);
                if (!localNames.some((name) => invokedNames.has(name))) continue;
                const property = element.propertyName
                  ? propertyName(element.propertyName)
                  : ts.isIdentifier(element.name) ? element.name.text : null;
                if (property && argumentValue.kind === 'object') {
                  passedValues.push(propertyValueAt(
                    argumentValue.id,
                    property,
                    position,
                    new Set(),
                    new Map(),
                  ));
                }
              }
            } else if (ts.isArrayBindingPattern(parameter.name)
              && ts.isArrayLiteralExpression(argument)) {
              collectArrayBinding(parameter.name, argument);
            }
            for (const passed of passedValues.flatMap(callableNodes)) {
              const passedKey = functionBindingKey(passed);
              if (!passedKey) continue;
              const passedPositions = functionCallPositions.get(passedKey) ?? [];
              if (passedPositions.includes(position)) continue;
              passedPositions.push(position);
              passedPositions.sort((left, right) => left - right);
              functionCallPositions.set(passedKey, passedPositions);
              callPointsChanged = true;
            }
          });
        }
      });
    }
  }
  return {
    isTarget: (node) => {
      const owner = containingFunction(node);
      const ownerKey = owner && functionBindingKey(owner);
      const calls = ownerKey ? (functionCallPositions.get(ownerKey) ?? []) : [];
      const evaluationPoints = calls.length > 0 ? calls : [node.getStart(sourceFile)];
      return evaluationPoints.some((callBefore) => containsTarget(value(node, callBefore)));
    },
  };
}

function hasImplicitPracticeTarget(
  sourceFile: ts.SourceFile,
  path?: string,
  sources?: SourceMap,
): boolean {
  const importedSelectors = new Set<string>();
  const importedSelectorMembers = new Set<string>();
  const importedExactTypes = new Set<string>();
  const importedExactTypeMembers = new Set<string>();
  if (path && sources) {
    for (const statement of sourceFile.statements) {
      if (!ts.isImportDeclaration(statement) || !ts.isStringLiteral(statement.moduleSpecifier)
        || !statement.importClause) continue;
      const target = modulePath(sources, path, statement.moduleSpecifier.text);
      if (!target) continue;
      if (statement.importClause.name
        && exportedImplicitSelector(sources, target, 'default')) {
        importedSelectors.add(statement.importClause.name.text);
      }
      if (statement.importClause.name
        && exportedTypeHasExactPair(sources, target, 'default')) {
        importedExactTypes.add(statement.importClause.name.text);
      }
      if (statement.importClause.namedBindings && ts.isNamedImports(statement.importClause.namedBindings)) {
        for (const specifier of statement.importClause.namedBindings.elements) {
          const imported = specifier.propertyName?.text ?? specifier.name.text;
          if (exportedTypeHasExactPair(sources, target, imported)) {
            importedExactTypes.add(specifier.name.text);
          }
          if (exportedImplicitSelector(sources, target, imported)) {
            importedSelectors.add(specifier.name.text);
          }
          for (const member of usedMemberPaths(sourceFile, specifier.name.text)) {
            if (exportedImplicitSelector(sources, target, `${imported}.${member}`)) {
              importedSelectorMembers.add(`${specifier.name.text}.${member}`);
            }
          }
        }
      }
      if (statement.importClause.namedBindings && ts.isNamespaceImport(statement.importClause.namedBindings)) {
        const local = statement.importClause.namedBindings.name.text;
        walk(sourceFile, (node) => {
          if (!ts.isTypeReferenceNode(node) || !ts.isQualifiedName(node.typeName)
            || !ts.isIdentifier(node.typeName.left) || node.typeName.left.text !== local) return;
          if (exportedTypeHasExactPair(sources, target, node.typeName.right.text)) {
            importedExactTypeMembers.add(`${local}.${node.typeName.right.text}`);
          }
        });
        for (const member of usedMemberPaths(sourceFile, local)) {
          if (exportedImplicitSelector(sources, target, member)) {
            importedSelectorMembers.add(`${local}.${member}`);
          }
        }
      }
    }
  }
  const stringBindings = new Map<string, string>();
  let stringsChanged = true;
  while (stringsChanged) {
    stringsChanged = false;
    walk(sourceFile, (node) => {
      if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer) {
        const value = staticString(node.initializer, stringBindings);
        if (value !== null && !stringBindings.has(node.name.text)) {
          stringBindings.set(node.name.text, value);
          stringsChanged = true;
        }
      }
      if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
        && ts.isIdentifier(node.left)) {
        const value = staticString(node.right, stringBindings);
        if (value !== null && !stringBindings.has(node.left.text)) {
          stringBindings.set(node.left.text, value);
          stringsChanged = true;
        }
      }
    });
  }
  const typeDeclarations = new Map<string, ts.TypeNode>();
  for (const name of importedExactTypes) {
    typeDeclarations.set(name, ts.factory.createTypeLiteralNode([
      ts.factory.createPropertySignature(undefined, 'readinessSignalVersionId', undefined, ts.factory.createKeywordTypeNode(ts.SyntaxKind.NumberKeyword)),
      ts.factory.createPropertySignature(undefined, 'targetEventId', undefined, ts.factory.createKeywordTypeNode(ts.SyntaxKind.NumberKeyword)),
    ]));
  }
  for (const statement of sourceFile.statements) {
    if (ts.isTypeAliasDeclaration(statement)) typeDeclarations.set(statement.name.text, statement.type);
    if (ts.isInterfaceDeclaration(statement)) {
      const parts: ts.TypeNode[] = [ts.factory.createTypeLiteralNode(statement.members)];
      for (const clause of statement.heritageClauses ?? []) {
        for (const type of clause.types) {
          if (ts.isIdentifier(type.expression)) {
            parts.push(ts.factory.createTypeReferenceNode(type.expression.text, type.typeArguments));
          }
        }
      }
      typeDeclarations.set(statement.name.text, parts.length === 1 ? parts[0] : ts.factory.createIntersectionTypeNode(parts));
    }
  }
  const selectorProvenance = callableProvenance(sourceFile, importedSelectors, importedSelectorMembers);
  const functions = new Map<string, RuntimeFunctionLike>();
  const functionAliases = new Map<string, string>();
  const exactFunctions = new Set<RuntimeFunctionLike>();
  walk(sourceFile, (node) => {
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name)
      && node.initializer && ts.isIdentifier(node.initializer)) {
      functionAliases.set(node.name.text, node.initializer.text);
    }
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
      && ts.isIdentifier(node.left) && ts.isIdentifier(node.right)) {
      functionAliases.set(node.left.text, node.right.text);
    }
  });
  walk(sourceFile, (node) => {
    if (!isRuntimeFunctionLike(node)) return;
    const name = functionName(node);
    if (name) functions.set(name, node);
    const typedExact = node.parameters.some((parameter) => (
      Boolean(parameter.type)
      && (resolvedTypeHasExactPair(parameter.type!, typeDeclarations)
        || (ts.isTypeReferenceNode(parameter.type!)
          && ((ts.isIdentifier(parameter.type!.typeName)
            && importedExactTypes.has(parameter.type!.typeName.text))
            || (ts.isQualifiedName(parameter.type!.typeName)
              && ts.isIdentifier(parameter.type!.typeName.left)
              && importedExactTypeMembers.has(`${parameter.type!.typeName.left.text}.${parameter.type!.typeName.right.text}`)))))
    ));
    if (typedExact || hasStructuralExactPair(node)) exactFunctions.add(node);
  });
  let changed = true;
  while (changed) {
    changed = false;
    for (const owner of [...exactFunctions]) {
      if (!owner.body) continue;
      walk(owner.body, (node) => {
        if (!ts.isCallExpression(node) || !ts.isIdentifier(node.expression)
          || isLegacyOnlyFallback(node)) return;
        let targetName = node.expression.text;
        const seen = new Set<string>();
        while (functionAliases.has(targetName) && !seen.has(targetName)) {
          seen.add(targetName);
          targetName = functionAliases.get(targetName)!;
        }
        const target = functions.get(targetName);
        if (target && !exactFunctions.has(target)) {
          exactFunctions.add(target);
          changed = true;
        }
      });
    }
  }
  let violation = false;
  walk(sourceFile, (node) => {
    if (!staticallyReachable(node)) return;
    const owner = containingFunction(node);
    const exactContext = owner ? exactFunctions.has(owner) : hasStructuralExactPair(sourceFile);
    if (ts.isElementAccessExpression(node)
      && node.argumentExpression
      && ts.isNumericLiteral(node.argumentExpression)
      && node.argumentExpression.text === '0'
      && exactContext) {
      violation = true;
      return;
    }
    if (ts.isVariableDeclaration(node) && ts.isArrayBindingPattern(node.name)
      && node.name.elements.filter((element) => !ts.isOmittedExpression(element)).length === 1
      && exactContext) {
      violation = true;
      return;
    }
    if (ts.isVariableDeclaration(node) && ts.isObjectBindingPattern(node.name)
      && node.name.elements.some((element) => (
        element.propertyName ? propertyName(element.propertyName) === '0'
          : ts.isIdentifier(element.name) ? element.name.text === '0' : false
      ))
      && exactContext) {
      violation = true;
      return;
    }
    if (exactContext && (isIteratorFirstValue(node) || isImmediateLoopReturnIife(node))) {
      violation = true;
      return;
    }
    if (!ts.isCallExpression(node) || !exactContext) return;
    const calleeName = ts.isIdentifier(node.expression)
      ? node.expression.text
      : ts.isPropertyAccessExpression(node.expression)
        ? node.expression.name.text
        : ts.isElementAccessExpression(node.expression) && node.expression.argumentExpression
          ? staticString(node.expression.argumentExpression, stringBindings) ?? ''
          : '';
    if (selectorProvenance.isTarget(node.expression)
      && !isLegacyOnlyFallback(node)) {
      violation = true;
      return;
    }
    if (/^(?:nearest|first|only|any)$/i.test(calleeName)) {
      violation = true;
      return;
    }
    if (/^(?:shift|pop)$/i.test(calleeName)) {
      violation = true;
      return;
    }
    if (calleeName === 'at') {
      violation = true;
      return;
    }
    if (/^reduce(?:Right)?$/.test(calleeName) && !nodeHasExactPair(node)) {
      violation = true;
      return;
    }
    if (calleeName === 'sort'
      && expressionContainsIdentifier(node, (name) => /(?:scheduled|date|time)/i.test(name))) {
      violation = true;
      return;
    }
    if ((ts.isPropertyAccessExpression(node.expression) || ts.isElementAccessExpression(node.expression))
      && /^(?:find|findLast|filter)$/i.test(calleeName)
      && nodeHasInProgress(node)
      && !nodeHasExactPair(node)
      && !isLegacyOnlyFallback(node)) violation = true;
  });
  walk(sourceFile, (node) => {
    if (violation || !staticallyReachable(node) || !ts.isElementAccessExpression(node)
      || !node.argumentExpression) return;
    const owner = containingFunction(node);
    const exactContext = owner ? exactFunctions.has(owner) : hasStructuralExactPair(sourceFile);
    if (!exactContext || !ts.isBinaryExpression(node.argumentExpression)
      || node.argumentExpression.operatorToken.kind !== ts.SyntaxKind.MinusToken
      || !ts.isPropertyAccessExpression(node.argumentExpression.left)
      || node.argumentExpression.left.name.text !== 'length'
      || !ts.isNumericLiteral(node.argumentExpression.right)
      || node.argumentExpression.right.text !== '1') return;
    violation = true;
  });
  return violation;
}

function collectLiteralTypes(node: ts.TypeNode, output: Set<string>): void {
  if (ts.isUnionTypeNode(node)) {
    for (const type of node.types) collectLiteralTypes(type, output);
  } else if (ts.isLiteralTypeNode(node)) {
    const value = literalText(node.literal);
    if (value !== null) output.add(value);
  } else if (ts.isTemplateLiteralTypeNode(node)) {
    let value = node.head.text;
    for (const span of node.templateSpans) {
      const alternatives = new Set<string>();
      collectLiteralTypes(span.type, alternatives);
      if (alternatives.size !== 1) return;
      value += [...alternatives][0] + span.literal.text;
    }
    output.add(value);
  }
}

function setEquals(left: Set<string>, right: Set<string>): boolean {
  return left.size === right.size && [...left].every((item) => right.has(item));
}

function closedSetValueProvenance(
  sourceFile: ts.SourceFile,
  runtimeName: string,
): (node: ts.Expression, before?: number, active?: Set<string>) => boolean {
  const writes = new Map<string, Array<{ pos: number; expression: ts.Expression }>>();
  walk(sourceFile, (node) => {
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer) {
      const values = writes.get(node.name.text) ?? [];
      values.push({ pos: node.getStart(sourceFile), expression: node.initializer });
      writes.set(node.name.text, values);
    }
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
      && ts.isIdentifier(node.left)) {
      const values = writes.get(node.left.text) ?? [];
      values.push({ pos: node.getStart(sourceFile), expression: node.right });
      writes.set(node.left.text, values);
    }
  });
  const isRuntimeValue = (
    node: ts.Expression,
    before = node.getStart(sourceFile),
    active = new Set<string>(),
  ): boolean => {
    if (ts.isParenthesizedExpression(node) || ts.isAsExpression(node)
      || ts.isSatisfiesExpression(node) || ts.isNonNullExpression(node)) {
      return isRuntimeValue(node.expression, before, active);
    }
    if (ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node)) {
      return isRuntimeValue(node.expression, before, active);
    }
    if (!ts.isIdentifier(node)) return false;
    if (node.text === runtimeName) return true;
    if (active.has(node.text)) return false;
    const write = [...(writes.get(node.text) ?? [])]
      .reverse()
      .find((candidate) => candidate.pos < before && staticallyReachable(candidate.expression));
    return Boolean(write && isRuntimeValue(
      write.expression,
      write.pos,
      new Set(active).add(node.text),
    ));
  };
  return isRuntimeValue;
}

function hasCoreTaskExpansion(sourceFile: ts.SourceFile, sources?: SourceMap): boolean {
  let union: Set<string> | null = null;
  let array: string[] | null = null;
  let runtimeName = 'CORE_TASK_IDS';
  for (const statement of sourceFile.statements) {
    if (!ts.isExportDeclaration(statement) || statement.moduleSpecifier
      || !statement.exportClause || !ts.isNamedExports(statement.exportClause)) continue;
    for (const specifier of statement.exportClause.elements) {
      if (specifier.name.text === 'CORE_TASK_IDS') runtimeName = specifier.propertyName?.text ?? specifier.name.text;
    }
  }
  const initializers = new Map<string, ts.Expression>();
  walk(sourceFile, (node) => {
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer) {
      initializers.set(node.name.text, node.initializer);
    }
  });
  const collectStrings = (node: ts.Expression, active = new Set<string>()): string[] | null => {
    if (ts.isParenthesizedExpression(node) || ts.isAsExpression(node) || ts.isSatisfiesExpression(node)) {
      return collectStrings(node.expression, active);
    }
    if (ts.isIdentifier(node) && initializers.has(node.text) && !active.has(node.text)) {
      return collectStrings(initializers.get(node.text)!, new Set(active).add(node.text));
    }
    if (!ts.isArrayLiteralExpression(node)) return null;
    const values: string[] = [];
    for (const element of node.elements) {
      if (ts.isSpreadElement(element)) {
        const spread = collectStrings(element.expression, active);
        if (!spread) return null;
        values.push(...spread);
      } else {
        const direct = literalText(element);
        if (direct === null) return null;
        values.push(direct);
      }
    }
    return values;
  };
  for (const statement of sourceFile.statements) {
    if (ts.isTypeAliasDeclaration(statement) && statement.name.text === 'CoreTaskId') {
      union = new Set();
      collectLiteralTypes(statement.type, union);
    }
    if (ts.isVariableStatement(statement)) {
      for (const declaration of statement.declarationList.declarations) {
        if (!ts.isIdentifier(declaration.name) || declaration.name.text !== runtimeName || !declaration.initializer) continue;
        array = collectStrings(declaration.initializer);
      }
    }
  }
  const isRuntimeValue = closedSetValueProvenance(sourceFile, runtimeName);
  let mutated = false;
  walk(sourceFile, (node) => {
    if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)
      && isRuntimeValue(node.expression.expression, node.getStart(sourceFile))
      && /^(?:push|pop|shift|unshift|splice|sort|reverse|copyWithin|fill)$/.test(node.expression.name.text)) mutated = true;
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
      && (ts.isPropertyAccessExpression(node.left) || ts.isElementAccessExpression(node.left))
      && isRuntimeValue(node.left.expression, node.getStart(sourceFile))) mutated = true;
    if (ts.isCallExpression(node) && normalizedCall(node).arguments[0]
      && (mutationBuiltin(node) !== null
        || (ts.isPropertyAccessExpression(node.expression)
          && ts.isIdentifier(node.expression.expression)
          && node.expression.expression.text === 'Reflect' && node.expression.name.text === 'set'))
      && isRuntimeValue(normalizedCall(node).arguments[0], node.getStart(sourceFile))) mutated = true;
    if (ts.isCallExpression(node)) {
      const call = normalizedCall(node);
      for (const wrapper of mutationWrappers(sourceFile, sources)) {
        const receiver = call.arguments[wrapper.receiverIndex];
        if (wrapper.callable.isTarget(call.callee) && receiver
          && isRuntimeValue(receiver, node.getStart(sourceFile))) mutated = true;
      }
    }
  });
  return mutated || (union !== null && !setEquals(union, CORE_TASK_IDS))
    || (array !== null && (array.length !== CORE_TASK_IDS.size
      || array.some((value, index) => value !== [...CORE_TASK_IDS][index])))
    || (sourceFile.statements.some((statement) => ts.isVariableStatement(statement)
      && statement.declarationList.declarations.some((declaration) => (
        ts.isIdentifier(declaration.name) && declaration.name.text === runtimeName
      ))) && array === null);
}

function hasNavigationExpansion(sourceFile: ts.SourceFile, sources?: SourceMap): boolean {
  let runtimeName = 'MODULE_NAV';
  for (const statement of sourceFile.statements) {
    if (!ts.isExportDeclaration(statement) || statement.moduleSpecifier
      || !statement.exportClause || !ts.isNamedExports(statement.exportClause)) continue;
    for (const specifier of statement.exportClause.elements) {
      if (specifier.name.text === 'MODULE_NAV') runtimeName = specifier.propertyName?.text ?? specifier.name.text;
    }
  }
  const initializers = new Map<string, ts.Expression>();
  walk(sourceFile, (node) => {
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer) {
      initializers.set(node.name.text, node.initializer);
    }
  });
  const collectKeys = (node: ts.Node, active = new Set<string>()): Set<string> => {
    const keys = new Set<string>();
    const visit = (child: ts.Node): void => {
      if (ts.isIdentifier(child) && initializers.has(child.text) && !active.has(child.text)) {
        for (const key of collectKeys(initializers.get(child.text)!, new Set(active).add(child.text))) keys.add(key);
        return;
      }
      if (ts.isPropertyAssignment(child) && propertyName(child.name) === 'key') {
        const value = literalText(child.initializer);
        if (value !== null) keys.add(value);
      }
      ts.forEachChild(child, visit);
    };
    visit(node);
    return keys;
  };
  const collectOrderedKeys = (node: ts.Expression, active = new Set<string>()): string[] | null => {
    if (ts.isParenthesizedExpression(node) || ts.isAsExpression(node) || ts.isSatisfiesExpression(node)) {
      return collectOrderedKeys(node.expression, active);
    }
    if (ts.isIdentifier(node) && initializers.has(node.text) && !active.has(node.text)) {
      return collectOrderedKeys(initializers.get(node.text)!, new Set(active).add(node.text));
    }
    if (!ts.isArrayLiteralExpression(node)) return null;
    const keys: string[] = [];
    for (const element of node.elements) {
      if (ts.isSpreadElement(element)) {
        const spread = collectOrderedKeys(element.expression, active);
        if (!spread) return null;
        keys.push(...spread);
      } else if (ts.isObjectLiteralExpression(element)) {
        const property = element.properties.find((candidate) => (
          ts.isPropertyAssignment(candidate) && propertyName(candidate.name) === 'key'
        ));
        if (!property || !ts.isPropertyAssignment(property)) return null;
        const value = literalText(property.initializer);
        if (value === null) return null;
        keys.push(value);
      } else return null;
    }
    return keys;
  };
  let declared = false;
  let initialKeys = new Set<string>();
  let orderedKeys: string[] | null = null;
  for (const statement of sourceFile.statements) {
    if (!ts.isVariableStatement(statement)) continue;
    for (const declaration of statement.declarationList.declarations) {
      if (!ts.isIdentifier(declaration.name) || declaration.name.text !== runtimeName || !declaration.initializer) continue;
      declared = true;
      initialKeys = collectKeys(declaration.initializer);
      orderedKeys = collectOrderedKeys(declaration.initializer);
    }
  }
  if (declared && (!setEquals(initialKeys, TOP_LEVEL_NAV)
    || orderedKeys === null
    || orderedKeys.length !== TOP_LEVEL_NAV.size
    || orderedKeys.some((value, index) => value !== [...TOP_LEVEL_NAV][index]))) return true;
  const isRuntimeValue = closedSetValueProvenance(sourceFile, runtimeName);
  let postMutation = false;
  walk(sourceFile, (node) => {
    if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)
      && isRuntimeValue(node.expression.expression, node.getStart(sourceFile))
      && /^(?:push|pop|shift|unshift|splice|sort|reverse|copyWithin|fill)$/.test(node.expression.name.text)) postMutation = true;
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
      && (ts.isPropertyAccessExpression(node.left) || ts.isElementAccessExpression(node.left))
      && isRuntimeValue(node.left.expression, node.getStart(sourceFile))) postMutation = true;
    if (ts.isCallExpression(node) && normalizedCall(node).arguments[0]
      && (mutationBuiltin(node) !== null
        || (ts.isPropertyAccessExpression(node.expression)
          && ts.isIdentifier(node.expression.expression)
          && node.expression.expression.text === 'Reflect' && node.expression.name.text === 'set'))
      && isRuntimeValue(normalizedCall(node).arguments[0], node.getStart(sourceFile))) postMutation = true;
    if (ts.isCallExpression(node)) {
      const call = normalizedCall(node);
      for (const wrapper of mutationWrappers(sourceFile, sources)) {
        const receiver = call.arguments[wrapper.receiverIndex];
        if (wrapper.callable.isTarget(call.callee) && receiver
          && isRuntimeValue(receiver, node.getStart(sourceFile))) postMutation = true;
      }
    }
  });
  if (postMutation) return true;
  const nav = callableProvenance(sourceFile, ['MODULE_NAV']);
  let expanded = false;
  walk(sourceFile, (node) => {
    let mutation: ts.Node | null = null;
    if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)
      && nav.isTarget(node.expression.expression)
      && /^(?:push|unshift|splice)$/i.test(node.expression.name.text)) mutation = node;
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
      && ((ts.isIdentifier(node.left) && nav.isTarget(node.left))
        || (ts.isElementAccessExpression(node.left)
          && nav.isTarget(node.left.expression)))) mutation = node.right;
    if (mutation && [...collectKeys(mutation)].some((key) => !TOP_LEVEL_NAV.has(key))) expanded = true;
  });
  return expanded;
}

const genericUndoExportCache = new WeakMap<SourceMap, Map<string, boolean>>();

function exportedGenericUndoProvenance(
  sources: SourceMap,
  path: string,
  exportName: string,
  active = new Set<string>(),
): boolean {
  if (active.size > 0) return exportedGenericUndoProvenanceUncached(sources, path, exportName, active);
  const cache = genericUndoExportCache.get(sources) ?? new Map<string, boolean>();
  genericUndoExportCache.set(sources, cache);
  const key = `${path}:${exportName}`;
  const cached = cache.get(key);
  if (cached !== undefined) return cached;
  const result = exportedGenericUndoProvenanceUncached(sources, path, exportName, active);
  cache.set(key, result);
  return result;
}

function exportedGenericUndoProvenanceUncached(
  sources: SourceMap,
  path: string,
  exportName: string,
  active = new Set<string>(),
): boolean {
  const key = `${path}:${exportName}`;
  if (active.has(key)) return false;
  const namespace = namespaceExportResolution(sources, path, exportName);
  if (namespace) {
    return exportedGenericUndoProvenance(
      sources,
      namespace.path,
      namespace.exportName,
      new Set(active).add(key),
    );
  }
  if (/(?:write)?operations?/i.test(path)
    && /^(?:undoOperation|undoWriteOperation)$/i.test(exportName)) return true;
  const source = sources.get(path);
  if (source === undefined) return false;
  const sourceFile = parse(path, source);
  if (/(?:write)?operations?/i.test(path) && exportName === 'default'
    && sourceFile.statements.some((statement) => (
      (ts.isFunctionDeclaration(statement) && hasDefaultModifier(statement)
        && /^(?:undoOperation|undoWriteOperation)$/i.test(statement.name?.text ?? ''))
      || (ts.isExportAssignment(statement) && !statement.isExportEquals
        && ts.isIdentifier(statement.expression)
        && /^(?:undoOperation|undoWriteOperation)$/i.test(statement.expression.text))
    ))) return true;
  const next = new Set(active).add(key);
  for (const statement of sourceFile.statements) {
    if (!ts.isExportDeclaration(statement) || !statement.exportClause
      || !ts.isNamedExports(statement.exportClause)) continue;
    const target = statement.moduleSpecifier && ts.isStringLiteral(statement.moduleSpecifier)
      ? modulePath(sources, path, statement.moduleSpecifier.text)
      : null;
    for (const specifier of statement.exportClause.elements) {
      if (specifier.name.text !== exportName || !target) continue;
      if (exportedGenericUndoProvenance(
        sources,
        target,
        specifier.propertyName?.text ?? specifier.name.text,
        next,
      )) return true;
    }
  }
  return false;
}

function hasGenericUndoServiceCall(path: string, sourceFile: ts.SourceFile, sources: SourceMap): boolean {
  const undoFunctions = new Set<string>();
  const undoMembers = new Set<string>();
  for (const statement of sourceFile.statements) {
    if (!ts.isImportDeclaration(statement) || !ts.isStringLiteral(statement.moduleSpecifier)
      || !statement.importClause) continue;
    const target = modulePath(sources, path, statement.moduleSpecifier.text) ?? '';
    if (statement.importClause.name
      && exportedGenericUndoProvenance(sources, target, 'default')) {
      undoFunctions.add(statement.importClause.name.text);
    }
    if (statement.importClause.namedBindings
      && ts.isNamespaceImport(statement.importClause.namedBindings)) {
      const local = statement.importClause.namedBindings.name.text;
      for (const member of usedMemberPaths(sourceFile, local)) {
        if (exportedGenericUndoProvenance(sources, target, member)) {
          undoMembers.add(`${local}.${member}`);
        }
      }
      continue;
    }
    if (!statement.importClause.namedBindings) continue;
    if (!ts.isNamedImports(statement.importClause.namedBindings)) continue;
    for (const specifier of statement.importClause.namedBindings.elements) {
      const imported = specifier.propertyName?.text ?? specifier.name.text;
      if (exportedGenericUndoProvenance(sources, target, imported)) undoFunctions.add(specifier.name.text);
    }
  }
  if (undoFunctions.size === 0 && undoMembers.size === 0) return false;
  const provenance = callableProvenance(sourceFile, undoFunctions, undoMembers);
  let violation = false;
  walk(sourceFile, (node) => {
    if (staticallyReachable(node)
      && ts.isCallExpression(node) && provenance.isTarget(normalizedCall(node).callee)) violation = true;
  });
  return violation;
}

const readinessOwnershipCache = new WeakMap<SourceMap, Map<string, boolean>>();

function exportedReadinessOwnership(
  sources: SourceMap,
  path: string,
  exportName: string,
  active = new Set<string>(),
): boolean {
  const key = `${path}:${exportName}`;
  if (active.has(key)) return false;
  const cache = readinessOwnershipCache.get(sources) ?? new Map<string, boolean>();
  readinessOwnershipCache.set(sources, cache);
  const cached = cache.get(key);
  if (cached !== undefined) return cached;
  const result = exportedReadinessOwnershipUncached(sources, path, exportName, active);
  cache.set(key, result);
  return result;
}

function exportedReadinessOwnershipUncached(
  sources: SourceMap,
  path: string,
  exportName: string,
  active = new Set<string>(),
): boolean {
  const key = `${path}:${exportName}`;
  if (active.has(key)) return false;
  const ownershipExport = /^(?:getEventReadinessFeedback|getReadinessSignal|getReadinessFeedback|listReadinessSignals|createReadinessSignal|decideProductAction)$/i.test(exportName);
  if (ownershipExport && isDomainServicePath(path)) return true;
  const source = sources.get(path);
  if (source === undefined) return false;
  const sourceFile = parse(path, source);
  const next = new Set(active).add(key);
  const ownedLocals = new Set<string>();
  for (const statement of sourceFile.statements) {
    if (!ts.isImportDeclaration(statement) || !ts.isStringLiteral(statement.moduleSpecifier)
      || !statement.importClause?.namedBindings || !ts.isNamedImports(statement.importClause.namedBindings)) continue;
    const target = modulePath(sources, path, statement.moduleSpecifier.text);
    if (!target) continue;
    for (const specifier of statement.importClause.namedBindings.elements) {
      if (exportedReadinessOwnership(
        sources,
        target,
        specifier.propertyName?.text ?? specifier.name.text,
        next,
      )) ownedLocals.add(specifier.name.text);
    }
  }
  let changed = true;
  while (changed) {
    changed = false;
    walk(sourceFile, (node) => {
      if (!isRuntimeFunctionLike(node) || !node.body) return;
      const name = functionName(node);
      if (!name) return;
      let delegates = false;
      walk(node.body, (child) => {
        if (ts.isCallExpression(child)
          && expressionRootIsTainted(child.expression, ownedLocals)) delegates = true;
      });
      if (delegates && !ownedLocals.has(name)) { ownedLocals.add(name); changed = true; }
    });
  }
  for (const statement of sourceFile.statements) {
    if (!ts.isExportDeclaration(statement) || !statement.exportClause
      || !ts.isNamedExports(statement.exportClause)) continue;
    const target = statement.moduleSpecifier && ts.isStringLiteral(statement.moduleSpecifier)
      ? modulePath(sources, path, statement.moduleSpecifier.text)
      : null;
    for (const specifier of statement.exportClause.elements) {
      if (specifier.name.text !== exportName) continue;
      const original = specifier.propertyName?.text ?? specifier.name.text;
      if (target && exportedReadinessOwnership(sources, target, original, next)) return true;
      if (ownedLocals.has(original)) return true;
    }
  }
  for (const statement of sourceFile.statements) {
    const exported = ts.canHaveModifiers(statement)
      && Boolean(ts.getModifiers(statement)?.some((modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword));
    if (!exported) continue;
    if (ts.isFunctionDeclaration(statement) && statement.name?.text === exportName
      && ownedLocals.has(exportName)) return true;
  }
  return false;
}

const readinessModuleCache = new WeakMap<SourceMap, Map<string, boolean>>();

function moduleHasReadinessOwnership(sources: SourceMap, path: string): boolean {
  const cache = readinessModuleCache.get(sources) ?? new Map<string, boolean>();
  readinessModuleCache.set(sources, cache);
  const cached = cache.get(path);
  if (cached !== undefined) return cached;
  const source = sources.get(path);
  if (source === undefined) return false;
  const sourceFile = parse(path, source);
  let found = false;
  walk(sourceFile, (node) => {
    if ((ts.isFunctionDeclaration(node) || ts.isVariableDeclaration(node))
      && node.name && ts.isIdentifier(node.name)
      && exportedReadinessOwnership(sources, path, node.name.text)) found = true;
  });
  cache.set(path, found);
  return found;
}

const readinessDefaultMembersCache = new WeakMap<SourceMap, Map<string, Set<string>>>();

function exportedReadinessDefaultMembers(
  sources: SourceMap,
  path: string,
  active = new Set<string>(),
): Set<string> {
  if (active.has(path)) return new Set();
  const cache = readinessDefaultMembersCache.get(sources) ?? new Map<string, Set<string>>();
  readinessDefaultMembersCache.set(sources, cache);
  const cached = cache.get(path);
  if (cached) return new Set(cached);
  const source = sources.get(path);
  if (source === undefined) return new Set();
  const sourceFile = parse(path, source);
  const next = new Set(active).add(path);
  const owned = new Set<string>();
  for (const statement of sourceFile.statements) {
    if (!ts.isImportDeclaration(statement) || !ts.isStringLiteral(statement.moduleSpecifier)
      || !statement.importClause) continue;
    const target = modulePath(sources, path, statement.moduleSpecifier.text);
    if (!target) continue;
    if (statement.importClause.name
      && exportedReadinessOwnership(sources, target, 'default', next)) owned.add(statement.importClause.name.text);
    if (statement.importClause.namedBindings && ts.isNamedImports(statement.importClause.namedBindings)) {
      for (const specifier of statement.importClause.namedBindings.elements) {
        if (exportedReadinessOwnership(
          sources,
          target,
          specifier.propertyName?.text ?? specifier.name.text,
          next,
        )) owned.add(specifier.name.text);
      }
    }
  }
  const objects = new Map<string, ts.ObjectLiteralExpression>();
  walk(sourceFile, (node) => {
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name)
      && node.initializer && ts.isObjectLiteralExpression(node.initializer)) objects.set(node.name.text, node.initializer);
  });
  const result = new Set<string>();
  const collect = (object: ts.ObjectLiteralExpression): void => {
    for (const property of object.properties) {
      if (!ts.isPropertyAssignment(property) && !ts.isShorthandPropertyAssignment(property)) continue;
      const name = propertyName(property.name);
      const value = ts.isPropertyAssignment(property) ? property.initializer : property.name;
      if (name && ts.isIdentifier(value) && owned.has(value.text)) result.add(name);
    }
  };
  for (const statement of sourceFile.statements) {
    if (!ts.isExportAssignment(statement)) continue;
    if (ts.isObjectLiteralExpression(statement.expression)) collect(statement.expression);
    if (ts.isIdentifier(statement.expression)) {
      const object = objects.get(statement.expression.text);
      if (object) collect(object);
    }
  }
  cache.set(path, new Set(result));
  return result;
}

const exportedRouteConstantCache = new WeakMap<SourceMap, Map<string, string | null>>();

function exportedRouteConstant(
  sources: SourceMap,
  path: string,
  exportName: string,
  active = new Set<string>(),
): string | null {
  const key = `${path}:${exportName}`;
  if (active.has(key)) return null;
  const cache = exportedRouteConstantCache.get(sources) ?? new Map<string, string | null>();
  exportedRouteConstantCache.set(sources, cache);
  if (cache.has(key)) return cache.get(key) ?? null;
  const source = sources.get(path);
  if (source === undefined) return null;
  const sourceFile = parse(path, source);
  const strings = new Map<string, string>();
  let changed = true;
  while (changed) {
    changed = false;
    walk(sourceFile, (node) => {
      if (!ts.isVariableDeclaration(node) || !ts.isIdentifier(node.name) || !node.initializer) return;
      const value = staticString(node.initializer, strings);
      if (value !== null && strings.get(node.name.text) !== value) {
        strings.set(node.name.text, value);
        changed = true;
      }
    });
  }
  let result: string | null = null;
  for (const statement of sourceFile.statements) {
    if (ts.isVariableStatement(statement)
      && ts.getModifiers(statement)?.some((modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword)) {
      for (const declaration of statement.declarationList.declarations) {
        if (ts.isIdentifier(declaration.name) && declaration.name.text === exportName) {
          result = strings.get(declaration.name.text) ?? null;
        }
      }
    }
    if (exportName === 'default' && ts.isExportAssignment(statement) && !statement.isExportEquals) {
      result = staticString(statement.expression, strings);
    }
    if (ts.isExportDeclaration(statement) && statement.exportClause && ts.isNamedExports(statement.exportClause)) {
      for (const specifier of statement.exportClause.elements) {
        if (specifier.name.text !== exportName) continue;
        const imported = specifier.propertyName?.text ?? specifier.name.text;
        if (statement.moduleSpecifier && ts.isStringLiteral(statement.moduleSpecifier)) {
          const target = modulePath(sources, path, statement.moduleSpecifier.text);
          if (target) result = exportedRouteConstant(sources, target, imported, new Set(active).add(key));
        } else result = strings.get(imported) ?? null;
      }
    }
  }
  cache.set(key, result);
  return result;
}

function pilotOwnsReviewReadiness(path: string, sourceFile: ts.SourceFile, sources: SourceMap): boolean {
  if (!/(?:\/features\/(?:pilot|haru|chat)\/|\/components\/(?:Chat[^/]*\.tsx?$|ChatPanel\/)|\/(?:Pilot|Haru)[^/]*\.tsx?$)/i.test(path)) return false;
  const owned = new Set<string>();
  const chatSurface = /\/components\/(?:Chat[^/]*\.tsx?$|ChatPanel\/)|\/features\/chat\//i.test(path);
  const ownedMembers = new Set<string>();
  const importedRouteStrings = new Map<string, string>();
  for (const statement of sourceFile.statements) {
    if (!ts.isImportDeclaration(statement) || !ts.isStringLiteral(statement.moduleSpecifier)
      || !statement.importClause) continue;
    const target = modulePath(sources, path, statement.moduleSpecifier.text);
    if (!target) continue;
    if (statement.importClause.name
      && exportedReadinessOwnership(sources, target, 'default')) {
      owned.add(statement.importClause.name.text);
    }
    if (statement.importClause.name) {
      const value = exportedRouteConstant(sources, target, 'default');
      if (value !== null) importedRouteStrings.set(statement.importClause.name.text, value);
    }
    if (statement.importClause.name) {
      for (const member of exportedReadinessDefaultMembers(sources, target)) {
        ownedMembers.add(`${statement.importClause.name.text}.${member}`);
      }
    }
    if (!statement.importClause.namedBindings) continue;
    if (ts.isNamespaceImport(statement.importClause.namedBindings)) {
      if (moduleHasReadinessOwnership(sources, target)) {
        owned.add(statement.importClause.namedBindings.name.text);
      }
      continue;
    }
    if (!ts.isNamedImports(statement.importClause.namedBindings)) continue;
    for (const specifier of statement.importClause.namedBindings.elements) {
      const imported = specifier.propertyName?.text ?? specifier.name.text;
      if (exportedReadinessOwnership(sources, target, imported)) owned.add(specifier.name.text);
      const value = exportedRouteConstant(sources, target, imported);
      if (value !== null) importedRouteStrings.set(specifier.name.text, value);
    }
  }
  const ownedCallables = owned.size > 0 || ownedMembers.size > 0
    ? callableProvenance(sourceFile, owned, ownedMembers)
    : null;
  const routeStrings = new Map<string, string>(importedRouteStrings);
  const routeInitializers = new Map<string, ts.Expression>();
  let routeStringsChanged = true;
  while (routeStringsChanged) {
    routeStringsChanged = false;
    walk(sourceFile, (node) => {
      if (!ts.isVariableDeclaration(node) || !ts.isIdentifier(node.name) || !node.initializer) return;
      routeInitializers.set(node.name.text, node.initializer);
      const value = staticString(node.initializer, routeStrings);
      if (value !== null && routeStrings.get(node.name.text) !== value) {
        routeStrings.set(node.name.text, value);
        routeStringsChanged = true;
      }
    });
  }
  const routeString = (node: ts.Expression, active = new Set<string>()): string | null => {
    const direct = staticString(node, routeStrings);
    if (direct !== null) return direct;
    if (ts.isIdentifier(node) && routeInitializers.has(node.text) && !active.has(node.text)) {
      return routeString(routeInitializers.get(node.text)!, new Set(active).add(node.text));
    }
    if ((ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node))
      && ts.isIdentifier(node.expression)
      && routeInitializers.has(node.expression.text)) {
      const object = routeInitializers.get(node.expression.text)!;
      const member = ts.isPropertyAccessExpression(node)
        ? node.name.text
        : node.argumentExpression ? staticString(node.argumentExpression, routeStrings) : null;
      if (member && ts.isObjectLiteralExpression(object)) {
        for (const property of [...object.properties].reverse()) {
          if (ts.isPropertyAssignment(property) && propertyName(property.name) === member) {
            return routeString(property.initializer, new Set(active).add(node.expression.text));
          }
        }
      }
    }
    return null;
  };
  let changed = true;
  while (changed) {
    changed = false;
    walk(sourceFile, (node) => {
      if (ts.isVariableDeclaration(node) && node.initializer
        && expressionRootIsTainted(node.initializer, owned)) {
        for (const name of bindingNames(node.name)) {
          if (!owned.has(name)) { owned.add(name); changed = true; }
        }
      }
    });
  }
  let violation = false;
  walk(sourceFile, (node) => {
    if (ts.isCallExpression(node) && node.arguments.some((argument) => {
      const route = routeString(argument);
      return Boolean(route && /(?:^|\/)(?:product-actions|readiness(?:-signals|-feedback)?)(?:\/|$)/i.test(route));
    })) violation = true;
    if (ts.isNewExpression(node) && ts.isIdentifier(node.expression)
      && node.expression.text === 'EventSource'
      && node.arguments?.some((argument) => {
        const route = routeString(argument);
        return Boolean(route && /(?:^|\/)(?:product-actions|readiness(?:-signals|-feedback)?)(?:\/|$)/i.test(route));
      })) violation = true;
    if (ts.isCallExpression(node)
      && (expressionRootIsTainted(node.expression, owned)
        || Boolean(ownedCallables?.isTarget(node.expression)))) violation = true;
    if (!chatSurface && ts.isIdentifier(node) && /^(?:readinessSignal|readinessEvidence|productActionDraft|confirmationToken|confirmation_token)$/i.test(node.text)) violation = true;
    if (!chatSurface && (ts.isPropertyAccessExpression(node) || ts.isPropertyAssignment(node))
      && propertyName(node.name)
      && /^(?:readinessSignal|readinessEvidence|productActionDraft|confirmationToken|confirmation_token)$/i.test(propertyName(node.name)!)) violation = true;
  });
  return violation;
}

function pilotHasInexactOwnerOpen(path: string, sourceFile: ts.SourceFile): boolean {
  if (!/(?:\/features\/(?:pilot|haru)\/|\/(?:Pilot|Haru)[^/]*\.tsx?$)/i.test(path)) return false;
  const launchers = new Set(['launchCoreTask', 'openCoreTask', 'onOpenTask']);
  const initializers = new Map<string, ts.Expression>();
  for (const statement of sourceFile.statements) {
    if (!ts.isImportDeclaration(statement) || !statement.importClause?.namedBindings
      || !ts.isNamedImports(statement.importClause.namedBindings)) continue;
    for (const specifier of statement.importClause.namedBindings.elements) {
      const imported = specifier.propertyName?.text ?? specifier.name.text;
      if (/^(?:launchCoreTask|openCoreTask|onOpenTask)$/i.test(imported)) launchers.add(specifier.name.text);
    }
  }
  let changed = true;
  while (changed) {
    changed = false;
    walk(sourceFile, (node) => {
      if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer) {
        if (!initializers.has(node.name.text)) initializers.set(node.name.text, node.initializer);
        if (ts.isIdentifier(node.initializer) && launchers.has(node.initializer.text)
          && !launchers.has(node.name.text)) { launchers.add(node.name.text); changed = true; }
      }
      if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
        && ts.isIdentifier(node.left)) {
        if (!initializers.has(node.left.text)) initializers.set(node.left.text, node.right);
        if (ts.isIdentifier(node.right) && launchers.has(node.right.text)
          && !launchers.has(node.left.text)) { launchers.add(node.left.text); changed = true; }
      }
    });
  }
  const taskStrings = new Map<string, string>();
  let taskStringsChanged = true;
  while (taskStringsChanged) {
    taskStringsChanged = false;
    for (const [name, initializer] of initializers) {
      const value = staticString(initializer, taskStrings);
      if (value !== null && taskStrings.get(name) !== value) {
        taskStrings.set(name, value);
        taskStringsChanged = true;
      }
    }
  }
  const resolveObject = (expression: ts.Expression, active = new Set<string>()): ts.ObjectLiteralExpression | null => {
    if (ts.isObjectLiteralExpression(expression)) return expression;
    if (ts.isParenthesizedExpression(expression) || ts.isAsExpression(expression)
      || ts.isSatisfiesExpression(expression)) return resolveObject(expression.expression, active);
    if (!ts.isIdentifier(expression) || active.has(expression.text)) return null;
    const initializer = initializers.get(expression.text);
    return initializer ? resolveObject(initializer, new Set(active).add(expression.text)) : null;
  };
  const resolvedProperty = (
    object: ts.ObjectLiteralExpression,
    name: string,
    active = new Set<ts.ObjectLiteralExpression>(),
  ): ts.Expression | null => {
    if (active.has(object)) return null;
    const next = new Set(active).add(object);
    for (const property of [...object.properties].reverse()) {
      if (ts.isPropertyAssignment(property) && propertyName(property.name) === name) return property.initializer;
      if (ts.isShorthandPropertyAssignment(property) && property.name.text === name) return property.name;
      if (ts.isSpreadAssignment(property)) {
        const spread = resolveObject(property.expression);
        if (spread) {
          const nested = resolvedProperty(spread, name, next);
          if (nested) return nested;
        }
      }
    }
    return null;
  };
  let violation = false;
  walk(sourceFile, (node) => {
    if (!ts.isCallExpression(node)) return;
    const name = ts.isIdentifier(node.expression)
      ? node.expression.text
      : ts.isPropertyAccessExpression(node.expression) ? node.expression.name.text : '';
    if (!launchers.has(name)) return;
    const candidateExpression = node.arguments.length > 1 ? node.arguments[1] : node.arguments[0];
    if (!candidateExpression) return;
    const candidate = resolveObject(candidateExpression);
    if (!candidate) return;
    const refValue = resolveObject(resolvedProperty(candidate, 'ref') ?? candidate);
    const contextValue = resolveObject(resolvedProperty(candidate, 'context') ?? candidate);
    if (!refValue && !contextValue) return;
    const taskValue = (refValue && resolvedProperty(refValue, 'taskId'))
      || resolvedProperty(candidate, 'taskId');
    const taskId = taskValue ? staticString(taskValue, taskStrings) : null;
    if (taskId === null) return;
    const required = CORE_TASK_IDENTITY.get(taskId);
    const isDefined = (value: ts.Expression | null): boolean => Boolean(
      value
      && value.kind !== ts.SyntaxKind.NullKeyword
      && !(ts.isIdentifier(value) && value.text === 'undefined')
      && !(ts.isVoidExpression(value))
    );
    if (!required || required.some((field) => !(
      isDefined(refValue ? resolvedProperty(refValue, field) : null)
      || isDefined(contextValue ? resolvedProperty(contextValue, field) : null)
      || isDefined(resolvedProperty(candidate, field))
    ))) violation = true;
  });
  return violation;
}

function sensitiveName(name: string): boolean {
  const canonical = name.replace(/[_-]/g, '').toLowerCase();
  return /^(?:confirmationtoken|idempotencykey|operationid|actioncallid|actioncalluuid|uuid|[a-z0-9]*uuid|previoustitle|frozen(?:(?:decision)?(?:body|payload|input)|decision)|readinessevidence|readinessfeedback|readinesssignal|usernote|excerpt|coretaskrecoverystate|recoverycertificate|productactiondraft)$/.test(canonical);
}

function expressionIsSensitive(
  node: ts.Node,
  tainted: Set<string>,
  sensitiveFunctions: Set<string>,
): boolean {
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node) || ts.isJsxText(node)) return false;
  if (ts.isIdentifier(node)) return sensitiveName(node.text) || tainted.has(node.text);
  if (ts.isPropertyAccessExpression(node)) {
    return sensitiveName(node.name.text)
      || expressionIsSensitive(node.expression, tainted, sensitiveFunctions);
  }
  if (ts.isElementAccessExpression(node)) {
    const key = node.argumentExpression ? literalText(node.argumentExpression) : null;
    return (key !== null && sensitiveName(key))
      || expressionIsSensitive(node.expression, tainted, sensitiveFunctions)
      || (Boolean(node.argumentExpression) && expressionIsSensitive(node.argumentExpression!, tainted, sensitiveFunctions));
  }
  if (ts.isCallExpression(node)) {
    if (ts.isIdentifier(node.expression) && sensitiveFunctions.has(node.expression.text)) return true;
    return node.arguments.some((argument) => expressionIsSensitive(argument, tainted, sensitiveFunctions));
  }
  if (ts.isObjectLiteralExpression(node)) {
    return node.properties.some((property) => {
      if (ts.isSpreadAssignment(property)) return expressionIsSensitive(property.expression, tainted, sensitiveFunctions);
      if (ts.isPropertyAssignment(property)) {
        return Boolean(propertyName(property.name) && sensitiveName(propertyName(property.name)!))
          || expressionIsSensitive(property.initializer, tainted, sensitiveFunctions);
      }
      if (ts.isShorthandPropertyAssignment(property)) return sensitiveName(property.name.text) || tainted.has(property.name.text);
      return false;
    });
  }
  let found = false;
  ts.forEachChild(node, (child) => {
    if (expressionIsSensitive(child, tainted, sensitiveFunctions)) found = true;
  });
  return found;
}

function functionName(node: ts.FunctionLikeDeclaration): string | null {
  if ('name' in node && node.name && ts.isIdentifier(node.name)) return node.name.text;
  if (ts.isVariableDeclaration(node.parent) && ts.isIdentifier(node.parent.name)) return node.parent.name.text;
  return null;
}

const sensitiveSinkExportCache = new WeakMap<SourceMap, Map<string, boolean>>();

function exportedSensitiveSink(
  sources: SourceMap,
  path: string,
  exportName: string,
  active = new Set<string>(),
): boolean {
  if (active.size > 0) return exportedSensitiveSinkUncached(sources, path, exportName, active);
  const cache = sensitiveSinkExportCache.get(sources) ?? new Map<string, boolean>();
  sensitiveSinkExportCache.set(sources, cache);
  const key = `${path}:${exportName}`;
  const cached = cache.get(key);
  if (cached !== undefined) return cached;
  const result = exportedSensitiveSinkUncached(sources, path, exportName, active);
  cache.set(key, result);
  return result;
}

function exportedSensitiveSinkUncached(
  sources: SourceMap,
  path: string,
  exportName: string,
  active = new Set<string>(),
): boolean {
  const key = `${path}:${exportName}`;
  if (active.has(key)) return false;
  const namespace = namespaceExportResolution(sources, path, exportName);
  if (namespace) {
    return exportedSensitiveSink(
      sources,
      namespace.path,
      namespace.exportName,
      new Set(active).add(key),
    );
  }
  const source = sources.get(path);
  if (source === undefined) return false;
  const sourceFile = parse(path, source);
  const next = new Set(active).add(key);
  for (const statement of sourceFile.statements) {
    if (ts.isExportDeclaration(statement) && statement.exportClause
      && ts.isNamedExports(statement.exportClause)) {
      const target = statement.moduleSpecifier && ts.isStringLiteral(statement.moduleSpecifier)
        ? modulePath(sources, path, statement.moduleSpecifier.text)
        : null;
      for (const specifier of statement.exportClause.elements) {
        if (specifier.name.text === exportName && target
          && exportedSensitiveSink(
            sources,
            target,
            specifier.propertyName?.text ?? specifier.name.text,
            next,
          )) return true;
      }
    }
    let fn: RuntimeFunctionLike | null = null;
    if (ts.isFunctionDeclaration(statement)
      && (statement.name?.text === exportName || (exportName === 'default' && hasDefaultModifier(statement)))) fn = statement;
    if (ts.isVariableStatement(statement)) {
      const declaration = statement.declarationList.declarations.find((candidate) => (
        ts.isIdentifier(candidate.name) && candidate.name.text === exportName
      ));
      if (declaration?.initializer && (ts.isArrowFunction(declaration.initializer)
        || ts.isFunctionExpression(declaration.initializer))) fn = declaration.initializer;
    }
    if (!fn?.body) continue;
    const parameters = new Set(fn.parameters.flatMap((parameter) => bindingNames(parameter.name)));
    let sinksParameter = false;
    walk(fn.body, (node) => {
      if (!ts.isCallExpression(node) || !node.arguments.some((argument) => (
        ts.isIdentifier(argument) && parameters.has(argument.text)
      ))) return;
      const callee = node.expression.getText(sourceFile);
      if (/(?:sessionStorage|localStorage|console|location|searchParams|urlParams).*(?:setItem|log|warn|error|assign|replace|set|append)/i.test(callee)) {
        sinksParameter = true;
      }
    });
    if (sinksParameter) return true;
  }
  return false;
}

function hasSensitiveClientPersistence(
  sourceFile: ts.SourceFile,
  path?: string,
  sources?: SourceMap,
): boolean {
  if (!/(?:confirmation(?:[_-]?token)?|idempotency[_-]?key|operation[_-]?id|action[_-]?call|randomUUID|\buuid\b|previous[_-]?title|frozen(?:Decision)?(?:Body|Payload|Input)|readiness(?:Evidence|Feedback|Signal)|user[_-]?note|excerpt|coreTaskRecoveryState|recoveryCertificate|productActionDraft)/i.test(sourceFile.text)) {
    return false;
  }
  const hasLexicalSink = /(?:sessionStorage|localStorage|\bconsole\b|history\s*\.\s*(?:pushState|replaceState)|=\s*history\b|window\s*\.\s*location|=\s*location\b|\blocation\s*\.\s*(?:href|hash|search|assign|replace)|\b(?:params|searchParams|urlParams)\s*\.\s*(?:set|append)|new\s+(?:URL|URLSearchParams)\b)/.test(sourceFile.text);
  let mayUseImportedSink = false;
  if (!hasLexicalSink && path && sources && /\bimport\b/.test(sourceFile.text)) {
    const calledRoots = new Set<string>();
    walk(sourceFile, (node) => {
      if (!ts.isCallExpression(node)) return;
      let expression: ts.Expression = node.expression;
      while (ts.isPropertyAccessExpression(expression) || ts.isElementAccessExpression(expression)) {
        expression = expression.expression;
      }
      if (ts.isIdentifier(expression)) calledRoots.add(expression.text);
    });
    for (const statement of sourceFile.statements) {
      if (!ts.isImportDeclaration(statement) || !ts.isStringLiteral(statement.moduleSpecifier)
        || !statement.importClause) continue;
      const target = modulePath(sources, path, statement.moduleSpecifier.text);
      if (!target) continue;
      if (statement.importClause.name && calledRoots.has(statement.importClause.name.text)
        && exportedSensitiveSink(sources, target, 'default')) {
        mayUseImportedSink = true;
      }
      if (statement.importClause.namedBindings && ts.isNamedImports(statement.importClause.namedBindings)) {
        for (const specifier of statement.importClause.namedBindings.elements) {
          const imported = specifier.propertyName?.text ?? specifier.name.text;
          if (!calledRoots.has(specifier.name.text)) continue;
          if (exportedSensitiveSink(sources, target, imported)) mayUseImportedSink = true;
          for (const member of usedMemberPaths(sourceFile, specifier.name.text)) {
            if (exportedSensitiveSink(sources, target, `${imported}.${member}`)) mayUseImportedSink = true;
          }
        }
      }
      if (statement.importClause.namedBindings && ts.isNamespaceImport(statement.importClause.namedBindings)) {
        if (!calledRoots.has(statement.importClause.namedBindings.name.text)) continue;
        for (const member of usedMemberPaths(sourceFile, statement.importClause.namedBindings.name.text)) {
          if (exportedSensitiveSink(sources, target, member)) mayUseImportedSink = true;
        }
      }
      if (mayUseImportedSink) break;
    }
  }
  if (!mayUseImportedSink
    && !hasLexicalSink) {
    return false;
  }
  const tainted = new Set<string>();
  const sensitiveFunctions = new Set<string>();
  const privacyStrings = new Map<string, string>();
  let privacyStringsChanged = true;
  while (privacyStringsChanged) {
    privacyStringsChanged = false;
    walk(sourceFile, (node) => {
      if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer) {
        const value = staticString(node.initializer, privacyStrings);
        if (value !== null && !privacyStrings.has(node.name.text)) {
          privacyStrings.set(node.name.text, value);
          privacyStringsChanged = true;
        }
      }
      if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
        && ts.isIdentifier(node.left)) {
        const value = staticString(node.right, privacyStrings);
        if (value !== null && !privacyStrings.has(node.left.text)) {
          privacyStrings.set(node.left.text, value);
          privacyStringsChanged = true;
        }
      }
    });
  }
  type PrivacyKeyWrite = { pos: number; node: ts.Node; sensitive: boolean };
  const privacyKeyWrites = new Map<string, PrivacyKeyWrite[]>();
  const computedKeyNames = new Set<string>();
  walk(sourceFile, (node) => {
    if (ts.isElementAccessExpression(node) && node.argumentExpression
      && ts.isIdentifier(node.argumentExpression)) computedKeyNames.add(node.argumentExpression.text);
  });
  const recordPrivacyKey = (name: string, node: ts.Node, expression: ts.Expression): void => {
    if (!computedKeyNames.has(name)) return;
    const value = staticString(expression, privacyStrings);
    if (value === null) return;
    const writes = privacyKeyWrites.get(name) ?? [];
    writes.push({ pos: node.getStart(sourceFile), node, sensitive: sensitiveName(value) });
    privacyKeyWrites.set(name, writes);
  };
  walk(sourceFile, (node) => {
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer) {
      recordPrivacyKey(node.name.text, node, node.initializer);
    }
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
      && ts.isIdentifier(node.left)) recordPrivacyKey(node.left.text, node, node.right);
  });
  const writeMayBeSkipped = (node: ts.Node): boolean => {
    let current: ts.Node = node;
    while (current.parent) {
      const parent = current.parent;
      if (isRuntimeFunctionLike(parent)) break;
      if (ts.isIfStatement(parent)) {
        const condition = staticBooleanValue(parent.expression);
        const inThen = current === parent.thenStatement
          || (current.pos >= parent.thenStatement.pos && current.end <= parent.thenStatement.end);
        if (condition === null || (condition === true) !== inThen) return true;
      }
      if ((ts.isWhileStatement(parent) || ts.isForStatement(parent)
        || ts.isForInStatement(parent) || ts.isForOfStatement(parent)
        || ts.isSwitchStatement(parent) || ts.isTryStatement(parent))) return true;
      current = parent;
    }
    return false;
  };
  const sensitiveKeyAt = (name: string, before: number): boolean => {
    let state = false;
    for (const write of privacyKeyWrites.get(name) ?? []) {
      if (write.pos >= before || !staticallyReachable(write.node)) continue;
      state = writeMayBeSkipped(write.node) ? state || write.sensitive : write.sensitive;
    }
    return state;
  };
  const expressionIsSensitiveAt = (node: ts.Node): boolean => {
    const contextual = new Set(tainted);
    for (const name of privacyKeyWrites.keys()) {
      if (sensitiveKeyAt(name, node.getStart(sourceFile))) contextual.add(name);
      else contextual.delete(name);
    }
    return expressionIsSensitive(node, contextual, sensitiveFunctions);
  };
  const storageObjects = new Set(['sessionStorage', 'localStorage']);
  const urlObjects = new Set<string>();
  const locationObjects = new Set(['location']);
  const storageSinks = new Set<string>();
  const otherSinks = new Set<string>();
  const objectSinks = new Set<string>();
  const forwardingSinks = new Map<string, Set<number>>();
  const sinkStrings = new Map<string, string>();
  const sinkAliases = new Map<string, string>();
  const importedSinkFunctions = new Set<string>();
  const importedSinkMembers = new Set<string>();
  const isStorageReceiver = (node: ts.Expression): boolean => {
    if (ts.isParenthesizedExpression(node) || ts.isAsExpression(node)
      || ts.isSatisfiesExpression(node) || ts.isNonNullExpression(node)) {
      return isStorageReceiver(node.expression);
    }
    if (ts.isIdentifier(node)) return storageObjects.has(node.text);
    if ((ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node))
      && ts.isIdentifier(node.expression)
      && /^(?:window|globalThis)$/.test(node.expression.text)) {
      const storage = memberMethod(node, sinkStrings);
      return storage !== null && storageObjects.has(storage);
    }
    return false;
  };
  if (mayUseImportedSink && path && sources) {
    for (const statement of sourceFile.statements) {
      if (!ts.isImportDeclaration(statement) || !ts.isStringLiteral(statement.moduleSpecifier)
        || !statement.importClause) continue;
      const target = modulePath(sources, path, statement.moduleSpecifier.text);
      if (!target) continue;
      if (statement.importClause.name && exportedSensitiveSink(sources, target, 'default')) {
        forwardingSinks.set(statement.importClause.name.text, new Set([0]));
        importedSinkFunctions.add(statement.importClause.name.text);
      }
      if (statement.importClause.namedBindings && ts.isNamedImports(statement.importClause.namedBindings)) {
        for (const specifier of statement.importClause.namedBindings.elements) {
          const imported = specifier.propertyName?.text ?? specifier.name.text;
          if (exportedSensitiveSink(sources, target, imported)) {
            forwardingSinks.set(specifier.name.text, new Set([0]));
            importedSinkFunctions.add(specifier.name.text);
          }
          for (const member of usedMemberPaths(sourceFile, specifier.name.text)) {
            if (exportedSensitiveSink(sources, target, `${imported}.${member}`)) {
              importedSinkMembers.add(`${specifier.name.text}.${member}`);
            }
          }
        }
      }
      if (statement.importClause.namedBindings && ts.isNamespaceImport(statement.importClause.namedBindings)) {
        const local = statement.importClause.namedBindings.name.text;
        for (const member of usedMemberPaths(sourceFile, local)) {
          if (exportedSensitiveSink(sources, target, member)) {
            importedSinkMembers.add(`${local}.${member}`);
          }
        }
      }
    }
  }
  const boundSinkExpression = (value: ts.Expression): boolean => {
    if (ts.isIdentifier(value)) return storageSinks.has(value.text) || otherSinks.has(value.text);
    if ((ts.isPropertyAccessExpression(value) || ts.isElementAccessExpression(value))
      && ts.isIdentifier(value.expression)) {
      const method = memberMethod(value, sinkStrings) ?? '';
      return (method === 'setItem' && storageObjects.has(value.expression.text))
        || (/^(?:log|info|warn|error|debug)$/i.test(method) && value.expression.text === 'console')
        || (/^(?:assign|replace)$/i.test(method) && locationObjects.has(value.expression.text));
    }
    return ts.isCallExpression(value)
      && ts.isPropertyAccessExpression(value.expression)
      && value.expression.name.text === 'bind'
      && boundSinkExpression(value.expression.expression);
  };
  let changed = true;
  while (changed) {
    changed = false;
    walk(sourceFile, (node) => {
      if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer) {
        if (ts.isIdentifier(node.initializer)) sinkAliases.set(node.name.text, node.initializer.text);
        const value = staticString(node.initializer, sinkStrings);
        if (value !== null && !sinkStrings.has(node.name.text)) {
          sinkStrings.set(node.name.text, value);
          changed = true;
        }
        if (ts.isNewExpression(node.initializer) && ts.isIdentifier(node.initializer.expression)
          && /^(?:URL|URLSearchParams)$/i.test(node.initializer.expression.text)
          && !urlObjects.has(node.name.text)) {
          urlObjects.add(node.name.text);
          changed = true;
        }
        if (ts.isIdentifier(node.initializer) && urlObjects.has(node.initializer.text)
          && !urlObjects.has(node.name.text)) {
          urlObjects.add(node.name.text);
          changed = true;
        }
        if (ts.isIdentifier(node.initializer) && locationObjects.has(node.initializer.text)
          && !locationObjects.has(node.name.text)) {
          locationObjects.add(node.name.text);
          changed = true;
        }
        if (ts.isPropertyAccessExpression(node.initializer)
          && ts.isIdentifier(node.initializer.expression)
          && node.initializer.expression.text === 'window'
          && node.initializer.name.text === 'location'
          && !locationObjects.has(node.name.text)) {
          locationObjects.add(node.name.text);
          changed = true;
        }
      }
      if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer) {
        const windowStorage = (ts.isPropertyAccessExpression(node.initializer)
          || ts.isElementAccessExpression(node.initializer))
          && ts.isIdentifier(node.initializer.expression)
          && node.initializer.expression.text === 'window'
          && Boolean(memberMethod(node.initializer, sinkStrings)
            && storageObjects.has(memberMethod(node.initializer, sinkStrings)!));
        if (((ts.isIdentifier(node.initializer) && storageObjects.has(node.initializer.text)) || windowStorage)
          && !storageObjects.has(node.name.text)) {
          storageObjects.add(node.name.text);
          changed = true;
        }
        if ((ts.isPropertyAccessExpression(node.initializer) || ts.isElementAccessExpression(node.initializer))
          && memberMethod(node.initializer, sinkStrings) === 'setItem'
          && ts.isIdentifier(node.initializer.expression)
          && storageObjects.has(node.initializer.expression.text)
          && !storageSinks.has(node.name.text)) {
          storageSinks.add(node.name.text);
          changed = true;
        }
        if ((ts.isPropertyAccessExpression(node.initializer) || ts.isElementAccessExpression(node.initializer))
          && ((ts.isIdentifier(node.initializer.expression)
            && node.initializer.expression.text === 'console'
            && /^(?:log|info|warn|error|debug)$/i.test(memberMethod(node.initializer, sinkStrings) ?? ''))
            || (ts.isIdentifier(node.initializer.expression)
              && node.initializer.expression.text === 'history'
              && /^(?:pushState|replaceState)$/i.test(memberMethod(node.initializer, sinkStrings) ?? '')))
          && !otherSinks.has(node.name.text)) {
          otherSinks.add(node.name.text);
          changed = true;
        }
        if (ts.isCallExpression(node.initializer)
          && ts.isPropertyAccessExpression(node.initializer.expression)
          && node.initializer.expression.name.text === 'bind') {
          const target = node.initializer.expression.expression;
          const memberSink = (ts.isPropertyAccessExpression(target) || ts.isElementAccessExpression(target))
            && memberMethod(target, sinkStrings) === 'setItem'
            && ts.isIdentifier(target.expression) && storageObjects.has(target.expression.text);
          const aliasSink = ts.isIdentifier(target) && storageSinks.has(target.text)
            && Boolean(node.initializer.arguments[0])
            && ts.isIdentifier(node.initializer.arguments[0])
            && storageObjects.has(node.initializer.arguments[0].text);
          if ((memberSink || aliasSink) && !storageSinks.has(node.name.text)) {
            storageSinks.add(node.name.text);
            changed = true;
          }
        }
        if (expressionIsSensitiveAt(node.initializer) && !tainted.has(node.name.text)) {
          tainted.add(node.name.text);
          changed = true;
        }
      }
      if (ts.isBindingElement(node)) {
        const key = node.propertyName
          ? propertyName(node.propertyName)
          : ts.isIdentifier(node.name) ? node.name.text : null;
        if (key && sensitiveName(key)) {
          for (const name of bindingNames(node.name)) {
            if (!tainted.has(name)) { tainted.add(name); changed = true; }
          }
        }
      }
      if (ts.isVariableDeclaration(node) && ts.isObjectBindingPattern(node.name)
        && node.initializer && ts.isIdentifier(node.initializer)) {
        for (const element of node.name.elements) {
          const method = element.propertyName
            ? propertyName(element.propertyName)
            : ts.isIdentifier(element.name) ? element.name.text : null;
          for (const local of bindingNames(element.name)) {
            if (storageObjects.has(node.initializer.text) && method === 'setItem'
              && !storageSinks.has(local)) { storageSinks.add(local); changed = true; }
            if (node.initializer.text === 'console'
              && /^(?:log|info|warn|error|debug)$/i.test(method ?? '')
              && !otherSinks.has(local)) { otherSinks.add(local); changed = true; }
            if (locationObjects.has(node.initializer.text)
              && /^(?:assign|replace)$/i.test(method ?? '')
              && !otherSinks.has(local)) { otherSinks.add(local); changed = true; }
          }
        }
      }
      if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name)
        && node.initializer && ts.isObjectLiteralExpression(node.initializer)) {
        for (const property of node.initializer.properties) {
          if (!ts.isPropertyAssignment(property)) continue;
          const name = propertyName(property.name);
          const value = property.initializer;
          const boundSink = ts.isCallExpression(value) && ts.isPropertyAccessExpression(value.expression)
            && value.expression.name.text === 'bind'
            && (((ts.isPropertyAccessExpression(value.expression.expression)
              || ts.isElementAccessExpression(value.expression.expression))
              && memberMethod(value.expression.expression, sinkStrings) === 'setItem')
              || (ts.isIdentifier(value.expression.expression)
                && (storageSinks.has(value.expression.expression.text)
                  || otherSinks.has(value.expression.expression.text))));
          if (name && boundSink && !objectSinks.has(`${node.name.text}.${name}`)) {
            objectSinks.add(`${node.name.text}.${name}`);
            changed = true;
          }
        }
      }
      if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
        && ts.isIdentifier(node.left)) {
        if (ts.isIdentifier(node.right)) sinkAliases.set(node.left.text, node.right.text);
        if (ts.isIdentifier(node.right) && storageObjects.has(node.right.text)
          && !storageObjects.has(node.left.text)) { storageObjects.add(node.left.text); changed = true; }
        if (ts.isIdentifier(node.right) && locationObjects.has(node.right.text)
          && !locationObjects.has(node.left.text)) { locationObjects.add(node.left.text); changed = true; }
        if ((ts.isPropertyAccessExpression(node.right) || ts.isElementAccessExpression(node.right))
          && memberMethod(node.right, sinkStrings) === 'setItem'
          && ts.isIdentifier(node.right.expression) && storageObjects.has(node.right.expression.text)
          && !storageSinks.has(node.left.text)) { storageSinks.add(node.left.text); changed = true; }
        if (ts.isCallExpression(node.right) && ts.isPropertyAccessExpression(node.right.expression)
          && node.right.expression.name.text === 'bind'
          && (ts.isPropertyAccessExpression(node.right.expression.expression)
            || ts.isElementAccessExpression(node.right.expression.expression))
          && memberMethod(node.right.expression.expression, sinkStrings) === 'setItem'
          && !storageSinks.has(node.left.text)) { storageSinks.add(node.left.text); changed = true; }
        if (expressionIsSensitiveAt(node.right)
          && !tainted.has(node.left.text)) { tainted.add(node.left.text); changed = true; }
      }
      if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
        && (ts.isPropertyAccessExpression(node.left) || ts.isElementAccessExpression(node.left))
        && ts.isIdentifier(node.left.expression)
        && boundSinkExpression(node.right)) {
        const name = memberMethod(node.left, sinkStrings);
        if (name && !objectSinks.has(`${node.left.expression.text}.${name}`)) {
          objectSinks.add(`${node.left.expression.text}.${name}`);
          changed = true;
        }
      }
      if (isRuntimeFunctionLike(node) && node.body) {
        const name = functionName(node);
        if (!name) return;
        let sensitiveReturn = false;
        walk(node.body, (child) => {
          if (ts.isReturnStatement(child)
            && child.expression
            && expressionIsSensitiveAt(child.expression)) sensitiveReturn = true;
        });
        if (sensitiveReturn && !sensitiveFunctions.has(name)) {
          sensitiveFunctions.add(name);
          changed = true;
        }
      }
    });
  }

  const sinkReturningFunctions = new Set<string>();
  const sinkMemberSeeds = new Set<string>(objectSinks);
  for (const member of importedSinkMembers) sinkMemberSeeds.add(member);
  for (const owner of storageObjects) sinkMemberSeeds.add(`${owner}.setItem`);
  for (const method of ['log', 'info', 'warn', 'error', 'debug']) sinkMemberSeeds.add(`console.${method}`);
  for (const method of ['pushState', 'replaceState']) sinkMemberSeeds.add(`history.${method}`);
  for (const owner of locationObjects) {
    sinkMemberSeeds.add(`${owner}.assign`);
    sinkMemberSeeds.add(`${owner}.replace`);
  }
  const sinkProvenance = callableProvenance(
    sourceFile,
    importedSinkFunctions,
    sinkMemberSeeds,
  );
  const callIsDirectSink = (node: ts.CallExpression): boolean => {
    const call = normalizedCall(node);
    if (sinkProvenance.isTarget(call.callee)) return true;
    if (ts.isCallExpression(call.callee) && ts.isIdentifier(call.callee.expression)
      && sinkReturningFunctions.has(call.callee.expression.text)) return true;
    if (!ts.isPropertyAccessExpression(call.callee) && !ts.isElementAccessExpression(call.callee)) return false;
    const owner = call.callee.expression;
    const method = memberMethod(call.callee, sinkStrings) ?? '';
    if (method === 'setItem' && ts.isIdentifier(owner) && storageObjects.has(owner.text)) return true;
    if (/^(?:set|append)$/i.test(method)
      && ts.isIdentifier(owner)
      && (urlObjects.has(owner.text) || /(?:params|searchParams|urlParams)$/i.test(owner.text))) return true;
    if (/^(?:pushState|replaceState)$/i.test(method)
      && ts.isIdentifier(owner)
      && owner.text === 'history') return true;
    if (/^(?:log|info|warn|error|debug)$/i.test(method)
      && ts.isIdentifier(owner)
      && owner.text === 'console') return true;
    return false;
  };

  changed = true;
  while (changed) {
    changed = false;
    walk(sourceFile, (node) => {
      if (!isRuntimeFunctionLike(node) || !node.body) return;
      const name = functionName(node);
      if (!name) return;
      let returnsSink = false;
      walk(node.body, (child) => {
        if (ts.isReturnStatement(child) && child.expression
          && (boundSinkExpression(child.expression)
            || (ts.isCallExpression(child.expression)
              && ts.isIdentifier(child.expression.expression)
              && sinkReturningFunctions.has(child.expression.expression.text)))) returnsSink = true;
      });
      if (returnsSink && !sinkReturningFunctions.has(name)) {
        sinkReturningFunctions.add(name);
        changed = true;
      }
    });
  }

  changed = true;
  while (changed) {
    changed = false;
    walk(sourceFile, (node) => {
      if (!isRuntimeFunctionLike(node) || !node.body) return;
      const name = functionName(node);
      if (!name) return;
      const indexes = forwardingSinks.get(name) ?? new Set<number>();
      walk(node.body, (child) => {
        if (ts.isBinaryExpression(child) && child.operatorToken.kind === ts.SyntaxKind.EqualsToken
          && (ts.isPropertyAccessExpression(child.left) || ts.isElementAccessExpression(child.left))
          && ts.isIdentifier(child.left.expression) && locationObjects.has(child.left.expression.text)
          && /^(?:href|hash|search)$/i.test(memberMethod(child.left, sinkStrings) ?? '')) {
          node.parameters.forEach((parameter, parameterIndex) => {
            const names = bindingNames(parameter.name);
            if (names.some((parameterName) => expressionContainsIdentifier(
              child.right,
              (candidate) => candidate === parameterName,
            ))) indexes.add(parameterIndex);
          });
        }
        if (!ts.isCallExpression(child)) return;
        const call = normalizedCall(child);
        const nested = ts.isIdentifier(call.callee)
          ? forwardingSinks.get(call.callee.text)
          : undefined;
        const sinkArguments = callIsDirectSink(child)
          ? call.arguments.map((_, index) => index)
          : [...(nested ?? [])];
        for (const argumentIndex of sinkArguments) {
          const argument = call.arguments[argumentIndex];
          if (!argument) continue;
          node.parameters.forEach((parameter, parameterIndex) => {
            const names = bindingNames(parameter.name);
            if (names.some((parameterName) => expressionContainsIdentifier(argument, (candidate) => candidate === parameterName))) {
              indexes.add(parameterIndex);
            }
          });
        }
      });
      if (indexes.size > (forwardingSinks.get(name)?.size ?? 0)) {
        forwardingSinks.set(name, indexes);
        changed = true;
      }
    });
  }

  changed = true;
  while (changed) {
    changed = false;
    for (const [alias, target] of sinkAliases) {
      const indexes = forwardingSinks.get(target);
      if (indexes && !forwardingSinks.has(alias)) {
        forwardingSinks.set(alias, new Set(indexes));
        changed = true;
      }
    }
  }

  let violation = false;
  walk(sourceFile, (node) => {
    if (!staticallyReachable(node)) return;
    if (ts.isCallExpression(node)) {
      const call = normalizedCall(node);
      if (callIsDirectSink(node)
        && call.arguments.some((argument) => expressionIsSensitiveAt(argument))) {
        violation = true;
      }
      if (ts.isIdentifier(call.callee)) {
        const indexes = forwardingSinks.get(call.callee.text);
        if (indexes && [...indexes].some((index) => (
          Boolean(call.arguments[index])
          && expressionIsSensitiveAt(call.arguments[index])
        ))) violation = true;
      }
      for (const wrapper of mutationWrappers(sourceFile, sources)) {
        const receiver = call.arguments[wrapper.receiverIndex];
        if (!wrapper.callable.isTarget(call.callee) || !receiver || !isStorageReceiver(receiver)) continue;
        if (wrapper.valueIndexes.some((index) => (
          Boolean(call.arguments[index]) && expressionIsSensitiveAt(call.arguments[index])
        ))) violation = true;
      }
      if (mutationBuiltin(node) === 'Object.assign'
        && call.arguments[0] && isStorageReceiver(call.arguments[0])
        && call.arguments.slice(1).some((argument) => expressionIsSensitiveAt(argument))) violation = true;
      if (mutationBuiltin(node) === 'Reflect.set'
        && call.arguments[0] && isStorageReceiver(call.arguments[0])
        && call.arguments[2] && expressionIsSensitiveAt(call.arguments[2])) violation = true;
      if ((mutationBuiltin(node) === 'Object.defineProperty'
          || mutationBuiltin(node) === 'Reflect.defineProperty')
        && call.arguments[0] && isStorageReceiver(call.arguments[0]) && call.arguments[2]) {
        let descriptor = call.arguments[2];
        while (ts.isParenthesizedExpression(descriptor) || ts.isAsExpression(descriptor)
          || ts.isSatisfiesExpression(descriptor)) descriptor = descriptor.expression;
        if (ts.isObjectLiteralExpression(descriptor)) {
          const value = [...descriptor.properties].reverse().find((property) => (
            ts.isPropertyAssignment(property) && propertyName(property.name) === 'value'
          ));
          if (value && ts.isPropertyAssignment(value)
            && expressionIsSensitiveAt(value.initializer)) violation = true;
        }
      }
    }
    if (ts.isBinaryExpression(node)
      && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
      && (ts.isPropertyAccessExpression(node.left) || ts.isElementAccessExpression(node.left))
      && ((ts.isIdentifier(node.left.expression) && locationObjects.has(node.left.expression.text))
        || (ts.isPropertyAccessExpression(node.left.expression)
          && ts.isIdentifier(node.left.expression.expression)
          && node.left.expression.expression.text === 'window'
          && node.left.expression.name.text === 'location'))
      && /^(?:href|hash|search)$/i.test(memberMethod(node.left, sinkStrings) ?? '')
      && expressionIsSensitiveAt(node.right)) violation = true;
    if (ts.isBinaryExpression(node)
      && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
      && (ts.isPropertyAccessExpression(node.left) || ts.isElementAccessExpression(node.left))
      && isStorageReceiver(node.left.expression)
      && expressionIsSensitiveAt(node.right)) violation = true;
  });
  return violation;
}

function isEmptyArray(node: ts.Expression): boolean {
  if (ts.isParenthesizedExpression(node) || ts.isAsExpression(node)
    || ts.isSatisfiesExpression(node) || ts.isNonNullExpression(node)) {
    return isEmptyArray(node.expression);
  }
  if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)
    && ts.isIdentifier(node.expression.expression)
    && node.expression.expression.text === 'Object'
    && node.expression.name.text === 'freeze'
    && node.arguments[0]) return isEmptyArray(node.arguments[0]);
  if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)
    && ts.isIdentifier(node.expression.expression)
    && node.expression.expression.text === 'Array'
    && node.expression.name.text === 'of') return node.arguments.length === 0;
  if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)
    && ts.isIdentifier(node.expression.expression)
    && node.expression.expression.text === 'Array'
    && node.expression.name.text === 'from'
    && node.arguments[0]) {
    if (isEmptyArray(node.arguments[0])) return true;
    let source = node.arguments[0];
    while (ts.isParenthesizedExpression(source) || ts.isAsExpression(source)
      || ts.isSatisfiesExpression(source)) source = source.expression;
    if (ts.isObjectLiteralExpression(source)) {
      const length = [...source.properties].reverse().find((property) => (
        ts.isPropertyAssignment(property) && propertyName(property.name) === 'length'
      ));
      return Boolean(length && ts.isPropertyAssignment(length)
        && ts.isNumericLiteral(length.initializer) && length.initializer.text === '0');
    }
    return false;
  }
  return (ts.isArrayLiteralExpression(node) && node.elements.length === 0)
    || ((ts.isCallExpression(node) || ts.isNewExpression(node))
      && ts.isIdentifier(node.expression)
      && node.expression.text === 'Array'
      && ((node.arguments?.length ?? 0) === 0
        || (node.arguments?.length === 1 && ts.isNumericLiteral(node.arguments[0])
          && node.arguments[0].text === '0')));
}

function containsMissingToEmptyDefault(node: ts.Expression): boolean {
  if (ts.isBinaryExpression(node)
    && (node.operatorToken.kind === ts.SyntaxKind.QuestionQuestionToken
      || node.operatorToken.kind === ts.SyntaxKind.BarBarToken)
    && isEmptyArray(node.right)) return true;
  if (ts.isConditionalExpression(node)) {
    return isEmptyArray(node.whenTrue) || isEmptyArray(node.whenFalse);
  }
  return false;
}

function referencesPreparationIds(node: ts.Node): boolean {
  let found = false;
  walk(node, (child) => {
    if (ts.isPropertyAccessExpression(child)
      && child.name.text === 'readiness_feedback_version_ids') found = true;
    if (ts.isElementAccessExpression(child) && child.argumentExpression
      && literalText(child.argumentExpression) === 'readiness_feedback_version_ids') found = true;
  });
  return found;
}

const preparationDefaultExportCache = new WeakMap<SourceMap, Map<string, boolean>>();

function exportedEmptyPreparationValue(
  sources: SourceMap,
  path: string,
  exportName: string,
  active = new Set<string>(),
): boolean {
  const key = `${path}:${exportName}`;
  if (active.has(key)) return false;
  const source = sources.get(path);
  if (source === undefined) return false;
  const sourceFile = parse(path, source);
  const next = new Set(active).add(key);
  const emptyLocals = new Set<string>();
  for (const statement of sourceFile.statements) {
    if (!ts.isVariableStatement(statement)) continue;
    for (const declaration of statement.declarationList.declarations) {
      if (ts.isIdentifier(declaration.name) && declaration.initializer && isEmptyArray(declaration.initializer)) {
        emptyLocals.add(declaration.name.text);
      }
    }
  }
  for (const statement of sourceFile.statements) {
    if (ts.isVariableStatement(statement)) {
      const exported = Boolean(ts.getModifiers(statement)?.some((modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword));
      for (const declaration of statement.declarationList.declarations) {
        if (exported && ts.isIdentifier(declaration.name) && declaration.name.text === exportName
          && declaration.initializer && isEmptyArray(declaration.initializer)) return true;
      }
    }
    if (ts.isExportDeclaration(statement) && statement.exportClause && ts.isNamedExports(statement.exportClause)) {
      const target = statement.moduleSpecifier && ts.isStringLiteral(statement.moduleSpecifier)
        ? modulePath(sources, path, statement.moduleSpecifier.text)
        : null;
      for (const specifier of statement.exportClause.elements) {
        if (specifier.name.text === exportName && target
          && exportedEmptyPreparationValue(
            sources,
            target,
            specifier.propertyName?.text ?? specifier.name.text,
            next,
          )) return true;
      }
    }
    if (exportName === 'default' && ts.isExportAssignment(statement) && !statement.isExportEquals
      && ((ts.isIdentifier(statement.expression) && emptyLocals.has(statement.expression.text))
        || isEmptyArray(statement.expression))) return true;
  }
  return false;
}

function exportedPreparationDefault(
  sources: SourceMap,
  path: string,
  exportName: string,
  active = new Set<string>(),
): boolean {
  if (active.size > 0) return exportedPreparationDefaultUncached(sources, path, exportName, active);
  const cache = preparationDefaultExportCache.get(sources) ?? new Map<string, boolean>();
  preparationDefaultExportCache.set(sources, cache);
  const key = `${path}:${exportName}`;
  const cached = cache.get(key);
  if (cached !== undefined) return cached;
  const result = exportedPreparationDefaultUncached(sources, path, exportName, active);
  cache.set(key, result);
  return result;
}

function exportedPreparationDefaultUncached(
  sources: SourceMap,
  path: string,
  exportName: string,
  active = new Set<string>(),
): boolean {
  const key = `${path}:${exportName}`;
  if (active.has(key)) return false;
  const namespace = namespaceExportResolution(sources, path, exportName);
  if (namespace) {
    return exportedPreparationDefault(
      sources,
      namespace.path,
      namespace.exportName,
      new Set(active).add(key),
    );
  }
  const source = sources.get(path);
  if (source === undefined) return false;
  const sourceFile = parse(path, source);
  const next = new Set(active).add(key);
  for (const statement of sourceFile.statements) {
    if (ts.isExportDeclaration(statement) && statement.exportClause
      && ts.isNamedExports(statement.exportClause)) {
      const target = statement.moduleSpecifier && ts.isStringLiteral(statement.moduleSpecifier)
        ? modulePath(sources, path, statement.moduleSpecifier.text)
        : null;
      for (const specifier of statement.exportClause.elements) {
        if (specifier.name.text === exportName && target
          && exportedPreparationDefault(
            sources,
            target,
            specifier.propertyName?.text ?? specifier.name.text,
            next,
          )) return true;
      }
    }
    let fn: RuntimeFunctionLike | null = null;
    if (ts.isFunctionDeclaration(statement)
      && (statement.name?.text === exportName || (exportName === 'default' && hasDefaultModifier(statement)))) fn = statement;
    if (ts.isVariableStatement(statement)) {
      const declaration = statement.declarationList.declarations.find((candidate) => (
        ts.isIdentifier(candidate.name) && candidate.name.text === exportName
      ));
      if (declaration?.initializer && (ts.isArrowFunction(declaration.initializer)
        || ts.isFunctionExpression(declaration.initializer))) fn = declaration.initializer;
    }
    if (!fn?.body) continue;
    const parameters = new Set(fn.parameters.flatMap((parameter) => bindingNames(parameter.name)));
    let defaultsParameter = fn.parameters.some((parameter) => (
      Boolean(parameter.initializer) && isEmptyArray(parameter.initializer!)
    ));
    walk(fn.body, (node) => {
      if (ts.isBinaryExpression(node)
        && (node.operatorToken.kind === ts.SyntaxKind.QuestionQuestionToken
          || node.operatorToken.kind === ts.SyntaxKind.BarBarToken)
        && ts.isIdentifier(node.left) && parameters.has(node.left.text)
        && isEmptyArray(node.right)) defaultsParameter = true;
    });
    if (defaultsParameter) return true;
  }
  return false;
}

function hasPreparationMissingDefault(path: string, sourceFile: ts.SourceFile, sources?: SourceMap): boolean {
  if (!/interviewPreparation/i.test(path)) return false;
  const emptyValues = new Set<string>();
  const preparationSources = new Set<string>();
  const preparationMembers = new Set<string>();
  const presenceBindings = new Map<string, string>();
  const receiverAliases = new Map<string, string>();
  const preparationSourceMembers = new Set<string>();
  const preparationStrings = new Map<string, string>();
  let preparationStringsChanged = true;
  while (preparationStringsChanged) {
    preparationStringsChanged = false;
    walk(sourceFile, (node) => {
      if (!ts.isVariableDeclaration(node) || !ts.isIdentifier(node.name) || !node.initializer) return;
      const value = staticString(node.initializer, preparationStrings);
      if (value !== null && preparationStrings.get(node.name.text) !== value) {
        preparationStrings.set(node.name.text, value);
        preparationStringsChanged = true;
      }
    });
  }
  walk(sourceFile, (node) => {
    if ((ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node))
      && ts.isIdentifier(node.expression)
      && memberMethod(node, preparationStrings) === 'readiness_feedback_version_ids') {
      preparationSourceMembers.add(`${node.expression.text}.readiness_feedback_version_ids`);
    }
  });
  const preparationProvenance = callableProvenance(sourceFile, [], preparationSourceMembers);
  walk(sourceFile, (node) => {
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name)
      && node.initializer && ts.isIdentifier(node.initializer)) {
      receiverAliases.set(node.name.text, node.initializer.text);
    }
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
      && ts.isIdentifier(node.left) && ts.isIdentifier(node.right)) {
      receiverAliases.set(node.left.text, node.right.text);
    }
  });
  const canonicalReceiver = (receiver: string | null): string | null => {
    if (receiver === null) return null;
    let current = receiver;
    const seen = new Set<string>();
    while (receiverAliases.has(current) && !seen.has(current)) {
      seen.add(current);
      current = receiverAliases.get(current)!;
    }
    return current;
  };
  const ownKeyReceiver = (node: ts.Expression): string | null => {
    if (!ts.isCallExpression(node) || node.arguments.length < 2) return null;
    const key = staticString(node.arguments[node.arguments.length - 1], preparationStrings);
    if (key !== 'readiness_feedback_version_ids') return null;
    if (ts.isPropertyAccessExpression(node.expression)
      && ts.isIdentifier(node.expression.expression)
      && node.expression.expression.text === 'Object'
      && node.expression.name.text === 'hasOwn') return canonicalReceiver(node.arguments[0]?.getText(sourceFile) ?? null);
    const prototypeCall = ts.isPropertyAccessExpression(node.expression)
      && node.expression.name.text === 'call'
      && ts.isPropertyAccessExpression(node.expression.expression)
      && node.expression.expression.name.text === 'hasOwnProperty'
      && ts.isPropertyAccessExpression(node.expression.expression.expression)
      && node.expression.expression.expression.name.text === 'prototype'
      && ts.isIdentifier(node.expression.expression.expression.expression)
      && node.expression.expression.expression.expression.text === 'Object';
    return prototypeCall ? canonicalReceiver(node.arguments[0]?.getText(sourceFile) ?? null) : null;
  };
  const isDirectOwnKey = (node: ts.Expression): boolean => ownKeyReceiver(node) !== null;
  const isPreparationSource = (node: ts.Expression): boolean => {
    if (preparationProvenance.isTarget(node)) return true;
    let references = false;
    walk(node, (child) => {
      if ((ts.isPropertyAccessExpression(child) || ts.isElementAccessExpression(child))
        && memberMethod(child, preparationStrings) === 'readiness_feedback_version_ids') references = true;
    });
    if (references) return true;
    return false;
  };
  const isEmptyValue = (node: ts.Expression, active = new Set<string>()): boolean => {
    if (isEmptyArray(node)) return true;
    if (ts.isIdentifier(node)) return emptyValues.has(node.text);
    if (ts.isParenthesizedExpression(node) || ts.isAsExpression(node)
      || ts.isSatisfiesExpression(node) || ts.isNonNullExpression(node)) {
      return isEmptyValue(node.expression, active);
    }
    if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)
      && node.expression.name.text === 'slice' && node.arguments.length === 0) {
      return isEmptyValue(node.expression.expression, active);
    }
    if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)
      && node.expression.name.text === 'concat' && node.arguments.length === 0) {
      return isEmptyValue(node.expression.expression, active);
    }
    if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)
      && ts.isIdentifier(node.expression.expression)
      && node.expression.expression.text === 'Object'
      && node.expression.name.text === 'freeze' && node.arguments[0]) {
      return isEmptyValue(node.arguments[0], active);
    }
    if (ts.isArrayLiteralExpression(node) && node.elements.length > 0) {
      return node.elements.every((element) => (
        ts.isSpreadElement(element) && isEmptyValue(element.expression, active)
      ));
    }
    return false;
  };
  let changed = true;
  while (changed) {
    changed = false;
    walk(sourceFile, (node) => {
      if (!ts.isVariableDeclaration(node) || !ts.isIdentifier(node.name) || !node.initializer) return;
      if (isEmptyValue(node.initializer)
        && !emptyValues.has(node.name.text)) { emptyValues.add(node.name.text); changed = true; }
      if (isDirectOwnKey(node.initializer)) {
        presenceBindings.set(node.name.text, ownKeyReceiver(node.initializer)!);
      }
      if (isPreparationSource(node.initializer)
        && !preparationSources.has(node.name.text)) {
        preparationSources.add(node.name.text);
        changed = true;
      }
      if (ts.isObjectLiteralExpression(node.initializer)) {
        for (const property of node.initializer.properties) {
          if (!ts.isPropertyAssignment(property) && !ts.isShorthandPropertyAssignment(property)) continue;
          const name = propertyName(property.name);
          const value = ts.isPropertyAssignment(property) ? property.initializer : property.name;
          if (name && isPreparationSource(value)
            && !preparationMembers.has(`${node.name.text}.${name}`)) {
            preparationMembers.add(`${node.name.text}.${name}`);
            changed = true;
          }
        }
      }
    });
  }
  const defaultFunctions = new Set<string>();
  const defaultMembers = new Set<string>();
  const anonymousDefaultFunctions = new Set<RuntimeFunctionLike>();
  if (sources) {
    for (const statement of sourceFile.statements) {
      if (!ts.isImportDeclaration(statement) || !ts.isStringLiteral(statement.moduleSpecifier)
        || !statement.importClause) continue;
      const target = modulePath(sources, path, statement.moduleSpecifier.text);
      if (!target) continue;
      if (statement.importClause.namedBindings && ts.isNamedImports(statement.importClause.namedBindings)) {
        for (const specifier of statement.importClause.namedBindings.elements) {
          const imported = specifier.propertyName?.text ?? specifier.name.text;
          if (exportedEmptyPreparationValue(sources, target, imported)) emptyValues.add(specifier.name.text);
        }
      }
      if (statement.importClause.name
        && exportedPreparationDefault(sources, target, 'default')) {
        defaultFunctions.add(statement.importClause.name.text);
      }
      if (statement.importClause.namedBindings && ts.isNamedImports(statement.importClause.namedBindings)) {
        for (const specifier of statement.importClause.namedBindings.elements) {
          const imported = specifier.propertyName?.text ?? specifier.name.text;
          if (exportedPreparationDefault(sources, target, imported)) defaultFunctions.add(specifier.name.text);
          for (const member of usedMemberPaths(sourceFile, specifier.name.text)) {
            if (exportedPreparationDefault(sources, target, `${imported}.${member}`)) {
              defaultMembers.add(`${specifier.name.text}.${member}`);
            }
          }
        }
      }
      if (statement.importClause.namedBindings && ts.isNamespaceImport(statement.importClause.namedBindings)) {
        const local = statement.importClause.namedBindings.name.text;
        for (const member of usedMemberPaths(sourceFile, local)) {
          if (exportedPreparationDefault(sources, target, member)) {
            defaultMembers.add(`${local}.${member}`);
          }
        }
      }
    }
  }
  walk(sourceFile, (node) => {
    if (!isRuntimeFunctionLike(node)) return;
    const name = functionName(node);
    if (node.parameters.some((parameter) => (
      Boolean(parameter.initializer) && isEmptyValue(parameter.initializer!)
    ))) {
      if (name) defaultFunctions.add(name);
      else anonymousDefaultFunctions.add(node);
    }
    if (node.body) {
      let defaultsInside = false;
      walk(node.body, (child) => {
        if (!ts.isBinaryExpression(child)
          || (child.operatorToken.kind !== ts.SyntaxKind.QuestionQuestionToken
            && child.operatorToken.kind !== ts.SyntaxKind.BarBarToken)
          || !isEmptyValue(child.right)
          || !ts.isIdentifier(child.left)) return;
        const leftName = child.left.text;
        if (node.parameters.some((parameter) => (
          ts.isIdentifier(parameter.name) && parameter.name.text === leftName
        ))) defaultsInside = true;
      });
      if (defaultsInside) {
        if (name) defaultFunctions.add(name);
        else anonymousDefaultFunctions.add(node);
      }
    }
  });
  const defaultReturningFunctions = new Set<string>();
  changed = true;
  while (changed) {
    changed = false;
    walk(sourceFile, (node) => {
      if (!isRuntimeFunctionLike(node) || !node.body) return;
      const name = functionName(node);
      if (!name) return;
      let returnsDefault = false;
      walk(node.body, (child) => {
        if (!ts.isReturnStatement(child) || !child.expression) return;
        if (ts.isIdentifier(child.expression) && defaultFunctions.has(child.expression.text)) returnsDefault = true;
        if (ts.isCallExpression(child.expression) && ts.isIdentifier(child.expression.expression)
          && defaultReturningFunctions.has(child.expression.expression.text)) returnsDefault = true;
      });
      if (returnsDefault && !defaultReturningFunctions.has(name)) {
        defaultReturningFunctions.add(name);
        changed = true;
      }
    });
  }
  const defaultCallableProvenance = callableProvenance(
    sourceFile,
    defaultFunctions,
    defaultMembers,
    true,
    anonymousDefaultFunctions,
  );
  const isPresenceTest = (node: ts.Expression, receiver: string | null): boolean => (
    (ownKeyReceiver(node) !== null && ownKeyReceiver(node) === receiver)
    || (ts.isIdentifier(node) && presenceBindings.get(node.text) === receiver)
    || ((ts.isParenthesizedExpression(node) || ts.isAsExpression(node)) && isPresenceTest(node.expression, receiver))
  );
  const preparationReceiver = (node: ts.Node): string | null => {
    let receiver: string | null = null;
    walk(node, (child) => {
      if (receiver !== null) return;
      if ((ts.isPropertyAccessExpression(child) || ts.isElementAccessExpression(child))
        && memberMethod(child, preparationStrings) === 'readiness_feedback_version_ids') {
        receiver = canonicalReceiver(child.expression.getText(sourceFile));
      }
    });
    return receiver;
  };
  const isOwnKeyGuarded = (node: ts.Node, receiver = preparationReceiver(node)): boolean => {
    let current: ts.Node | undefined = node;
    while (current?.parent) {
      const parent: ts.Node = current.parent;
      if (ts.isConditionalExpression(parent) && parent.whenTrue === current) {
        if (isPresenceTest(parent.condition, receiver)) return true;
      }
      if (ts.isIfStatement(parent) && parent.thenStatement === current
        && isPresenceTest(parent.expression, receiver)) return true;
      current = parent;
    }
    current = node;
    while (current?.parent) {
      if (ts.isBlock(current.parent) || ts.isSourceFile(current.parent)) {
        const statement = current.parent.statements.find((candidate) => (
          current === candidate || (current!.pos >= candidate.pos && current!.end <= candidate.end)
        ));
        if (statement) {
          const index = current.parent.statements.indexOf(statement);
          for (const previous of current.parent.statements.slice(0, index).reverse()) {
            if (!ts.isIfStatement(previous)
              || !statementDefinitelyTerminates(previous.thenStatement)
              || !ts.isPrefixUnaryExpression(previous.expression)
              || previous.expression.operator !== ts.SyntaxKind.ExclamationToken) continue;
            if (isPresenceTest(previous.expression.operand, receiver)) return true;
          }
        }
      }
      current = current.parent;
    }
    return false;
  };
  let violation = false;
  walk(sourceFile, (node) => {
    if (!staticallyReachable(node)) return;
    if (ts.isBindingElement(node)) {
      const key = node.propertyName
        ? propertyName(node.propertyName)
        : ts.isIdentifier(node.name) ? node.name.text : null;
      if (key === 'readiness_feedback_version_ids'
        && node.initializer
        && isEmptyValue(node.initializer)) violation = true;
    }
    if (ts.isVariableDeclaration(node)
      && ts.isIdentifier(node.name)
      && node.name.text === 'readiness_feedback_version_ids'
      && node.initializer
      && (isEmptyValue(node.initializer) || containsMissingToEmptyDefault(node.initializer))) violation = true;
    if (ts.isPropertyAssignment(node)
      && propertyName(node.name) === 'readiness_feedback_version_ids'
      && containsMissingToEmptyDefault(node.initializer)
      && !isOwnKeyGuarded(node)) violation = true;
    if (ts.isBinaryExpression(node)
      && (node.operatorToken.kind === ts.SyntaxKind.QuestionQuestionToken
        || node.operatorToken.kind === ts.SyntaxKind.BarBarToken)
      && isEmptyValue(node.right)
      && isPreparationSource(node.left)
      && !isOwnKeyGuarded(node)) violation = true;
    if (ts.isConditionalExpression(node)
      && (isEmptyArray(node.whenTrue) || isEmptyArray(node.whenFalse))
      && referencesPreparationIds(node)
      && !isPresenceTest(node.condition, preparationReceiver(node))
      && !isOwnKeyGuarded(node)) violation = true;
    if (ts.isCallExpression(node)
      && (defaultCallableProvenance.isTarget(node.expression)
        || (ts.isIdentifier(node.expression) && defaultFunctions.has(node.expression.text))
        || (ts.isCallExpression(node.expression) && ts.isIdentifier(node.expression.expression)
          && defaultReturningFunctions.has(node.expression.expression.text)))
      && node.arguments.some((argument) => isPreparationSource(argument))) violation = true;
  });
  return violation;
}

function auditFrontendSources(sources: SourceMap): string[] {
  const violations = new Set<string>();
  for (const [path, source] of productionEntries(sources)) {
    const sourceFile = parse(path, source);
    const endpoints = transportEndpoints(sourceFile, path, sources);
    if (hasDirectReviewDomainCrud(path, sourceFile, sources, endpoints)) violations.add('ui:direct-domain-crud');
    if (hasDisallowedReviewServiceRoute(path, endpoints)) violations.add('ui:review-service-route');
    if (/AdaptiveInterviewPracticeWorkspace\.tsx$/.test(path)
      && hasImplicitPracticeTarget(sourceFile, path, sources)) violations.add('practice:implicit-target-fallback');
    if (path === 'web/src/features/coreTaskSurface/contracts.ts'
      && hasCoreTaskExpansion(sourceFile, sources)) violations.add('core-task:id-union-expanded');
    if (path === 'web/src/layout/navigation.ts'
      && hasNavigationExpansion(sourceFile, sources)) violations.add('navigation:top-level-expanded');
    if (pilotOwnsReviewReadiness(path, sourceFile, sources)) violations.add('pilot:review-readiness-ownership');
    if (pilotHasInexactOwnerOpen(path, sourceFile)) violations.add('pilot:inexact-owner-open');
    if (endpoints.some((endpoint) => /\/write-operations\/[^/]*\/undo(?:\/|$)/i.test(endpoint))
      || hasGenericUndoServiceCall(path, sourceFile, sources)) {
      violations.add('ui:generic-operation-undo');
    }
    if (endpoints.some((endpoint) => /(?:\/api)?\/stories(?:\/|$)/i.test(endpoint))) violations.add('ui:stories-api-alias');
    if (hasSensitiveClientPersistence(sourceFile, path, sources)) violations.add('privacy:sensitive-client-persistence');
    if (hasPreparationMissingDefault(path, sourceFile, sources)) violations.add('preparation:missing-defaulted-empty');
  }
  return [...violations];
}

function collectProductionSources(): SourceMap {
  const root = execFileSync('git', ['rev-parse', '--show-toplevel'], { encoding: 'utf8' }).trim();
  const sourceRoot = join(root, 'web', 'src');
  const sources = new Map<string, string>();
  const visit = (directory: string): void => {
    for (const name of readdirSync(directory)) {
      const path = join(directory, name);
      if (statSync(path).isDirectory()) visit(path);
      else if (/\.tsx?$/.test(name) && !/\.(?:test|spec)\.tsx?$/.test(name)) {
        sources.set(relative(root, path).replace(/\\/g, '/'), readFileSync(path, 'utf8'));
      }
    }
  };
  visit(sourceRoot);
  return sources;
}

let productionAuditCache: { violations: string[]; durationMs: number } | null = null;

function auditProductionOnce(): { violations: string[]; durationMs: number } {
  if (productionAuditCache) return productionAuditCache;
  const startedAt = performance.now();
  const violations = auditFrontendSources(collectProductionSources());
  productionAuditCache = { violations, durationMs: performance.now() - startedAt };
  return productionAuditCache;
}

describe('review readiness negative fixture gate', () => {
  it('rejects direct, aliased, spread, helper, and call-chain domain CRUD', () => {
    const fixtures = [
      `import { updateInterviewNote } from '@/services/notes'; updateInterviewNote(1, input);`,
      `import { updateInterviewNote as mutate } from '@/services/notes'; mutate(1, input);`,
      `import * as notes from '@/services/notes'; const api = { ...notes }; api.updateInterviewNote(1, input);`,
      `import * as notes from '@/services/notes'; function helper() { return notes.client(); } helper().notes().update(input);`,
    ];
    for (const source of fixtures) {
      expect(auditFrontendSources(new Map([
        ['web/src/features/reviewReadiness/Owner.tsx', source],
      ]))).toContain('ui:direct-domain-crud');
    }
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/Owner.tsx', `
        import { proposeReviewReadinessAction as propose, decideProductAction, undoReadinessSignal, undoInterviewStory } from './service';
        const typed = { propose, decideProductAction, undoReadinessSignal, undoInterviewStory };
        function helper() { return typed; }
        helper().propose(noteId, request);
      `],
    ]))).not.toContain('ui:direct-domain-crud');
  });

  it('rejects exact V2 Practice fallback and time/nearest inference but permits isolated legacy selection', () => {
    const unsafe = [
      `type Exact = { readinessSignalVersionId: number; targetEventId: number }; function pick(focus: Exact, plans) { return plans.find((plan) => plan.status === 'in_progress'); }`,
      `type Exact = { readinessSignalVersionId: number; targetEventId: number }; function pick(focus: Exact, plans) { const exact = plans.find((plan) => sameExactPair(plan, focus)); return exact ?? otherInProgress.find((plan) => plan.status === 'in_progress'); }`,
      `type Exact = { readinessSignalVersionId: number; targetEventId: number }; function pick(focus: Exact, events) { return nearest(events.sort((a, b) => +new Date(a.scheduled_at) - +new Date(b.scheduled_at))); }`,
      `type Exact = { readinessSignalVersionId: number; targetEventId: number }; function pick(focus: Exact, plans) { return first(plans); }`,
    ];
    for (const source of unsafe) {
      expect(auditFrontendSources(new Map([
        ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', source],
      ]))).toContain('practice:implicit-target-fallback');
    }
    expect(auditFrontendSources(new Map([
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `function legacy(plans) { return plans.find((plan) => plan.status === 'in_progress'); }`],
    ]))).not.toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `
        function exact(focus, plans) {
          return plans.find((plan) => plan.status === 'in_progress' && sameExactPair(plan, focus));
        }
      `],
    ]))).not.toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `
        type ExactRequest = { readinessSignalVersionId: number; targetEventId: number };
        type LegacyFocus = { mode: 'legacy' };
        function legacySelect(plans) { return plans.find((plan) => plan.status === 'in_progress'); }
        function owner(focus: ExactRequest | LegacyFocus, plans) {
          if (isLegacyFocus(focus)) { return legacySelect(plans); }
          return plans.find((plan) => sameExactPair(plan, focus));
        }
      `],
    ]))).not.toContain('practice:implicit-target-fallback');
  });

  it('rejects CoreTask and top-level navigation expansion', () => {
    const sources = new Map<string, string>([
      ['web/src/features/coreTaskSurface/contracts.ts', `
        export type CoreTaskId = 'application.interview_review' | 'review.readiness';
        export const CORE_TASK_IDS = ['application.interview_review', 'review.readiness'] as const;
      `],
      ['web/src/layout/navigation.ts', `
        export const MODULE_NAV = [{ key: 'today' }, { key: 'readiness' }];
      `],
    ]);
    expect(auditFrontendSources(sources)).toEqual(expect.arrayContaining([
      'core-task:id-union-expanded',
      'navigation:top-level-expanded',
    ]));
  });

  it('rejects Haru/Pilot Signal, token, evidence, and draft ownership', () => {
    const fixtures = [
      `const readinessSignal = await getEventReadinessFeedback(applicationId, eventId);`,
      `const { confirmation_token: token } = proposal;`,
      `const draft = { ...productActionDraft };`,
      `render(readinessEvidence);`,
    ];
    for (const source of fixtures) {
      expect(auditFrontendSources(new Map([
        ['web/src/features/pilot/PilotReviewCard.tsx', source],
      ]))).toContain('pilot:review-readiness-ownership');
    }
    expect(auditFrontendSources(new Map([
      ['web/src/features/pilot/PilotReviewCard.tsx', `
        launchCoreTask(controller, { ref: { taskId: 'application.interview_review', applicationId: 3 } });
      `],
    ]))).toContain('pilot:inexact-owner-open');
    expect(auditFrontendSources(new Map([
      ['web/src/features/pilot/PilotReviewCard.tsx', `
        launchCoreTask(controller, { ref: { taskId: 'application.interview_review', applicationId: 3, eventId: 5 } });
      `],
    ]))).not.toEqual(expect.arrayContaining([
      'pilot:inexact-owner-open',
      'pilot:review-readiness-ownership',
    ]));
  });

  it('rejects generic undo and /api/stories aliases while accepting owner-scoped undo', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/service.ts', `http.post(\`/write-operations/\${operationId}/undo\`, input);`],
    ]))).toContain('ui:generic-operation-undo');
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/service.ts', `http.post('/api/stories/7/undo', input);`],
    ]))).toContain('ui:stories-api-alias');
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/Owner.tsx', `
        import { undoWriteOperation as undo } from '@/services/writeOperations'; undo(operationId);
      `],
    ]))).toContain('ui:generic-operation-undo');
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/service.ts', `http.post(\`/interview-stories/\${storyId}/product-action-undo\`, { parent_operation_id: parentOperationId });`],
    ]))).not.toEqual(expect.arrayContaining(['ui:generic-operation-undo', 'ui:stories-api-alias']));
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/format.ts', `function undoOperation(label) { return label; } undoOperation('preview');`],
    ]))).not.toContain('ui:generic-operation-undo');
  });

  it('rejects sensitive storage, URL, and logging through aliases, spreads, and helpers', () => {
    const fixtures = [
      `const token = proposal.confirmation_token; const store = sessionStorage; store.setItem('owner', token);`,
      `const frozen = { ...frozenDecisionBody }; localStorage.setItem('draft', JSON.stringify(frozen));`,
      `function value(input) { return input.previous_title; } params.set('state', value(result));`,
      `const detail = { ...readinessEvidence }; history.replaceState(detail, '', location.href);`,
      `function log(value) { console.info(value); } log(actionCallUuid);`,
      `const save = sessionStorage.setItem.bind(sessionStorage); save('recovery', coreTaskRecoveryState);`,
    ];
    for (const source of fixtures) {
      expect(auditFrontendSources(new Map([
        ['web/src/layout/AppShell.tsx', source],
      ]))).toContain('privacy:sensitive-client-persistence');
    }
  });

  it('preserves Preparation missing versus explicit empty presence', () => {
    const unsafe = [
      `const { readiness_feedback_version_ids = [] } = input; return http.post('/prepare', { readiness_feedback_version_ids });`,
      `const readiness_feedback_version_ids = input.readiness_feedback_version_ids ?? []; return http.post('/prepare', { ...input, readiness_feedback_version_ids });`,
      `return http.post('/prepare', { ...input, readiness_feedback_version_ids: input.readiness_feedback_version_ids || [] });`,
    ];
    for (const source of unsafe) {
      expect(auditFrontendSources(new Map([
        ['web/src/services/interviewPreparationProposals.ts', source],
      ]))).toContain('preparation:missing-defaulted-empty');
    }
    expect(auditFrontendSources(new Map([
      ['web/src/services/interviewPreparationProposals.ts', `
        const { readiness_feedback_version_ids, ...legacyBody } = input;
        const body = Object.prototype.hasOwnProperty.call(input, 'readiness_feedback_version_ids')
          ? { ...legacyBody, readiness_feedback_version_ids }
          : legacyBody;
        return http.post('/prepare', body);
      `],
    ]))).not.toContain('preparation:missing-defaulted-empty');
  });

  it('tracks domain CRUD provenance through assignments and module facades without flagging ordinary methods', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/services/reviewFacade.ts', `export { updateInterviewNote as save } from '@/services/notes';`],
      ['web/src/features/reviewReadiness/Owner.tsx', `
        import { save as imported } from '@/services/reviewFacade';
        let mutation; mutation = imported; mutation(noteId, input);
      `],
    ]))).toContain('ui:direct-domain-crud');
    expect(auditFrontendSources(new Map([
      ['web/src/facades/reviewFacade.ts', `
        import { updateInterviewNote } from '@/services/notes';
        export function save(...args) { return updateInterviewNote(...args); }
      `],
      ['web/src/features/reviewReadiness/Owner.tsx', `
        import { save } from '@/facades/reviewFacade'; save(noteId, input);
      `],
    ]))).toContain('ui:direct-domain-crud');
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/Owner.tsx', `
        const route = '/interview-' + 'notes/' + noteId;
        http['patch'](route, input);
      `],
    ]))).toContain('ui:direct-domain-crud');
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/Owner.tsx', `
        const cache = new Map(); cache.delete(key);
        const model = { delete() {} }; model.delete();
        http.post('/format/preview', input);
      `],
    ]))).not.toContain('ui:direct-domain-crud');
  });

  it('tracks exact V2 target provenance through typed request helpers and computed find calls', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `
        type ExactPracticeRequest = { readinessSignalVersionId: number; targetEventId: number };
        const method = 'find';
        function select(plans, request: ExactPracticeRequest) {
          return plans[method]((plan) => plan.status === 'in_progress');
        }
      `],
    ]))).toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `
        function legacy(focus, plans) {
          if (focus.mode !== 'legacy') return undefined;
          return plans.find((plan) => plan.status === 'in_progress');
        }
      `],
    ]))).not.toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `
        type LegacyPracticeFocus = { mode: 'legacy' };
        function select(focus: LegacyPracticeFocus, plans) {
          return plans.find((plan) => plan.status === 'in_progress');
        }
      `],
    ]))).not.toContain('practice:implicit-target-fallback');
  });

  it('resolves endpoint dataflow and transport wrappers but ignores inert formatting literals', () => {
    const fixtures = [
      `let route; route = '/api/' + 'stories/7'; const send = http['post']; send(route, input);`,
      `const { post: send } = http; function wrapper(path, body) { return send(path, body); } wrapper('/write-operations/' + operationId + '/undo', input);`,
    ];
    for (const source of fixtures) {
      expect(auditFrontendSources(new Map([
        ['web/src/features/reviewReadiness/service.ts', source],
      ]))).toEqual(expect.arrayContaining([
        expect.stringMatching(/^ui:(?:stories-api-alias|generic-operation-undo)$/),
      ]));
    }
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/format.ts', `
        const label = '/api/' + 'stories/7';
        const formatter = { post(value) { return value; } };
        const { post: format } = formatter;
        format(label);
      `],
    ]))).not.toContain('ui:stories-api-alias');
  });

  it('uses Pilot service and launch provenance, including request variables and aliases', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/services/readiness.ts', `export async function getEventReadinessFeedback() {}`],
      ['web/src/features/pilot/PilotCard.tsx', `
        import { getEventReadinessFeedback as load } from '@/services/readiness';
        load(applicationId, eventId);
      `],
    ]))).toContain('pilot:review-readiness-ownership');
    expect(auditFrontendSources(new Map([
      ['web/src/features/pilot/PilotCard.tsx', `
        const open = launchCoreTask;
        const request = { ref: { taskId: 'application.interview_review', applicationId } };
        open(controller, request);
      `],
    ]))).toContain('pilot:inexact-owner-open');
    expect(auditFrontendSources(new Map([
      ['web/src/features/pilot/PilotCard.tsx', `
        import { launchCoreTask as open } from '@/features/coreTaskSurface/controller';
        const request = { ref: { taskId: 'application.interview_review', applicationId, eventId } };
        open(controller, request);
      `],
    ]))).not.toContain('pilot:inexact-owner-open');
    expect(auditFrontendSources(new Map([
      ['web/src/features/pilot/PilotCard.tsx', `
        const request = {
          taskId: 'application.interview_review',
          context: { applicationId, eventId },
        };
        launchCoreTask(request);
      `],
    ]))).not.toContain('pilot:inexact-owner-open');
  });

  it('propagates sensitive values through destructuring, computed sinks, and multi-hop wrappers precisely', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/layout/AppShell.tsx', `
        const { confirmation_token: secret } = proposal;
        const method = 'setItem';
        function inner(value) { sessionStorage[method]('state', value); }
        function outer(value) { inner(value); }
        outer(secret);
      `],
    ]))).toContain('privacy:sensitive-client-persistence');
    expect(auditFrontendSources(new Map([
      ['web/src/layout/AppShell.tsx', `
        const statementCount = rows.length;
        const previousPageCount = pages.length;
        console.info(statementCount, previousPageCount);
      `],
    ]))).not.toContain('privacy:sensitive-client-persistence');
    expect(auditFrontendSources(new Map([
      ['web/src/layout/AppShell.tsx', `
        const sinkMethod = 'setItem';
        function persist({ confirmation_token: secret }) {
          const save = sessionStorage[sinkMethod];
          save('state', secret);
        }
      `],
    ]))).toContain('privacy:sensitive-client-persistence');
  });

  it('tracks Preparation presence semantics independently of the final binding name', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/services/interviewPreparationProposals.ts', `
        const { readiness_feedback_version_ids: ids = [] } = input;
        return http.post('/prepare', { readiness_feedback_version_ids: ids });
      `],
    ]))).toContain('preparation:missing-defaulted-empty');
    expect(auditFrontendSources(new Map([
      ['web/src/services/interviewPreparationProposals.ts', `
        return http.post('/prepare', { readiness_feedback_version_ids: [] });
      `],
    ]))).not.toContain('preparation:missing-defaulted-empty');
    expect(auditFrontendSources(new Map([
      ['web/src/services/interviewPreparationProposals.ts', `
        const ids = input.readiness_feedback_version_ids ?? [];
        return http.post('/prepare', { readiness_feedback_version_ids: ids });
      `],
    ]))).toContain('preparation:missing-defaulted-empty');
  });

  it('tracks CRUD provenance through object/default facades', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/facades/reviewCrud.ts', `
        import { updateInterviewNote } from '@/services/notes';
        const api = { save: updateInterviewNote };
        export default api;
      `],
      ['web/src/features/reviewReadiness/Owner.tsx', `
        import crud from '@/facades/reviewCrud'; crud.save(noteId, input);
      `],
    ]))).toContain('ui:direct-domain-crud');
    expect(auditFrontendSources(new Map([
      ['web/src/facades/format.ts', `export default { save(value) { return value; } };`],
      ['web/src/features/reviewReadiness/Owner.tsx', `
        import format from '@/facades/format'; format.save(input);
      `],
    ]))).not.toContain('ui:direct-domain-crud');
  });

  it('propagates V2 provenance through function aliases and composed types without tainting guarded legacy helpers', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `
        type SignalPart = { readinessSignalVersionId: number };
        type EventPart = { targetEventId: number };
        type ExactRequest = SignalPart & EventPart;
        function choose(plans) { return plans.find((plan) => plan.status === 'in_progress'); }
        const selector = choose;
        function owner(request: ExactRequest, plans) { return selector(plans); }
      `],
    ]))).toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `
        type ExactRequest = { readinessSignalVersionId: number; targetEventId: number };
        type LegacyFocus = { mode: 'legacy' };
        function legacySelect(plans) { return plans.find((plan) => plan.status === 'in_progress'); }
        function owner(focus: ExactRequest | LegacyFocus, plans) {
          if ('readinessSignalVersionId' in focus) return plans.find((plan) => sameExactPair(plan, focus));
          return legacySelect(plans);
        }
      `],
    ]))).not.toContain('practice:implicit-target-fallback');
  });

  it('uses statement-order endpoint and bound/imported transport provenance', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/service.ts', `
        let route = '/safe'; route = '/api/stories/7'; http.post(route, input);
      `],
    ]))).toContain('ui:stories-api-alias');
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/service.ts', `
        let route = '/api/stories/7'; route = '/safe'; http.post(route, input);
      `],
    ]))).not.toContain('ui:stories-api-alias');
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/service.ts', `
        const send = http.post.bind(http); send('/api/stories/7', input);
      `],
    ]))).toContain('ui:stories-api-alias');
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/service.ts', `
        const transport = { send: http.post.bind(http) };
        transport.send('/api/stories/7', input);
      `],
    ]))).toContain('ui:stories-api-alias');
    expect(auditFrontendSources(new Map([
      ['web/src/facades/transport.ts', `export const send = http.post.bind(http);`],
      ['web/src/features/reviewReadiness/service.ts', `
        import { send } from '@/facades/transport'; send('/api/stories/7', input);
      `],
    ]))).toContain('ui:stories-api-alias');
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/format.ts', `
        let send = http.post; send = format;
        send('/api/stories/7');
      `],
    ]))).not.toContain('ui:stories-api-alias');
  });

  it('tracks Haru namespace services and exact task-ref spreads', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/services/readiness.ts', `export function getEventReadinessFeedback() {}`],
      ['web/src/features/haru/HaruCard.tsx', `
        import * as readiness from '@/services/readiness';
        readiness.getEventReadinessFeedback(applicationId, eventId);
      `],
    ]))).toContain('pilot:review-readiness-ownership');
    expect(auditFrontendSources(new Map([
      ['web/src/features/haru/HaruCard.tsx', `
        const identity = { applicationId, eventId };
        launchCoreTask({ ref: { taskId: 'application.interview_review', ...identity } });
      `],
    ]))).not.toContain('pilot:inexact-owner-open');
    expect(auditFrontendSources(new Map([
      ['web/src/features/haru/HaruCard.tsx', `
        const identity = { applicationId };
        launchCoreTask({ ref: { taskId: 'application.interview_review', ...identity } });
      `],
    ]))).toContain('pilot:inexact-owner-open');
  });

  it('tracks sensitive assignment aliases, destructured sinks, and location aliases', () => {
    const fixtures = [
      `let secret; secret = proposal.confirmation_token; let store; store = sessionStorage; store.setItem('state', secret);`,
      `const secret = proposal.confirmation_token; const { setItem } = sessionStorage; setItem('state', secret);`,
      `const secret = proposal.confirmation_token; const { log } = console; log(secret);`,
      `const secret = proposal.confirmation_token; let save; save = sessionStorage.setItem.bind(sessionStorage); save('state', secret);`,
      `const secret = proposal.confirmation_token; const loc = location; loc.href = secret;`,
    ];
    for (const source of fixtures) {
      expect(auditFrontendSources(new Map([
        ['web/src/layout/AppShell.tsx', source],
      ]))).toContain('privacy:sensitive-client-persistence');
    }
  });

  it('tracks Preparation EMPTY/helper defaults but permits own-key guarded explicit empty', () => {
    const unsafe = [
      `const EMPTY = []; const ids = input.readiness_feedback_version_ids ?? EMPTY; return http.post('/prepare', { readiness_feedback_version_ids: ids });`,
      `const EMPTY = []; function withDefault(ids = EMPTY) { return ids; } const ids = withDefault(input.readiness_feedback_version_ids);`,
    ];
    for (const source of unsafe) {
      expect(auditFrontendSources(new Map([
        ['web/src/services/interviewPreparationProposals.ts', source],
      ]))).toContain('preparation:missing-defaulted-empty');
    }
    expect(auditFrontendSources(new Map([
      ['web/src/services/interviewPreparationProposals.ts', `
        const present = Object.prototype.hasOwnProperty.call(input, 'readiness_feedback_version_ids');
        const body = present
          ? { readiness_feedback_version_ids: input.readiness_feedback_version_ids ?? [] }
          : legacyBody;
        return http.post('/prepare', body);
      `],
    ]))).not.toContain('preparation:missing-defaulted-empty');
  });

  it('rejects top-level navigation expansion through post-initialization mutations', () => {
    const mutations = [
      `const MODULE_NAV = [{ key: 'today' }, { key: 'applications' }, { key: 'interview' }, { key: 'offers' }, { key: 'resources' }, { key: 'settings' }]; MODULE_NAV.push({ key: 'readiness' });`,
      `const MODULE_NAV = [{ key: 'today' }, { key: 'applications' }, { key: 'interview' }, { key: 'offers' }, { key: 'resources' }, { key: 'settings' }]; MODULE_NAV.splice(1, 0, { key: 'readiness' });`,
      `let MODULE_NAV = [{ key: 'today' }, { key: 'applications' }, { key: 'interview' }, { key: 'offers' }, { key: 'resources' }, { key: 'settings' }]; MODULE_NAV[0] = { key: 'readiness' };`,
      `let MODULE_NAV = [{ key: 'today' }, { key: 'applications' }, { key: 'interview' }, { key: 'offers' }, { key: 'resources' }, { key: 'settings' }]; MODULE_NAV = [...MODULE_NAV, { key: 'readiness' }];`,
    ];
    for (const source of mutations) {
      expect(auditFrontendSources(new Map([
        ['web/src/layout/navigation.ts', source],
      ]))).toContain('navigation:top-level-expanded');
    }
  });

  it('tracks CRUD provenance assigned into facade members', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/facades/reviewCrud.ts', `
        import { updateInterviewNote } from '@/services/notes';
        const api = {}; api.save = updateInterviewNote; export default api;
      `],
      ['web/src/features/reviewReadiness/Owner.tsx', `
        import api from '@/facades/reviewCrud'; api.save(noteId, input);
      `],
    ]))).toContain('ui:direct-domain-crud');
  });

  it('tracks exact V2 imported selector summaries and destructured field aliases', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/selectors/practice.ts', `
        export function selectPlan(plans) { return plans.find((plan) => plan.status === 'in_progress'); }
      `],
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `
        import { selectPlan as choose } from '@/selectors/practice';
        function owner(request, plans) {
          const { readinessSignalVersionId: signalVersion, targetEventId: event } = request;
          void signalVersion; void event; return choose(plans);
        }
      `],
    ]))).toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      ['web/src/selectors/practice.ts', `
        export function legacySelect(plans) { return plans.find((plan) => plan.status === 'in_progress'); }
      `],
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `
        import { legacySelect } from '@/selectors/practice';
        function owner(focus, plans) {
          if (isLegacyFocus(focus)) return legacySelect(plans);
          return plans.find((plan) => sameExactPair(plan, focus));
        }
      `],
    ]))).not.toContain('practice:implicit-target-fallback');
  });

  it('tracks endpoint wrappers declared as object methods', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/service.ts', `
        const transport = { send(path, body) { return http.post(path, body); } };
        transport.send('/api/stories/7', input);
      `],
    ]))).toContain('ui:stories-api-alias');
  });

  it('tracks Haru default readiness facades', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/facades/readiness.ts', `
        export { getEventReadinessFeedback as default } from '@/services/readiness';
      `],
      ['web/src/services/readiness.ts', `export function getEventReadinessFeedback() {}`],
      ['web/src/features/haru/HaruCard.tsx', `
        import loadReadiness from '@/facades/readiness'; loadReadiness(applicationId, eventId);
      `],
    ]))).toContain('pilot:review-readiness-ownership');
  });

  it('tracks bound destructured, navigation, and object sensitive sinks', () => {
    const fixtures = [
      `const secret = proposal.confirmation_token; const { setItem } = sessionStorage; const save = setItem.bind(sessionStorage); save('state', secret);`,
      `const secret = proposal.confirmation_token; const { assign: navigate } = location; navigate(secret);`,
      `const secret = proposal.confirmation_token; const navigate = (value) => { location.href = value; }; navigate(secret);`,
      `const secret = proposal.confirmation_token; const sinks = { save: sessionStorage.setItem.bind(sessionStorage) }; sinks.save('state', secret);`,
    ];
    for (const source of fixtures) {
      expect(auditFrontendSources(new Map([
        ['web/src/layout/AppShell.tsx', source],
      ]))).toContain('privacy:sensitive-client-persistence');
    }
  });

  it('tracks Preparation source aliases and helper-body empty defaults', () => {
    const fixtures = [
      `const source = input.readiness_feedback_version_ids; const ids = source ?? [];`,
      `function defaultIds(value) { return value ?? []; } const ids = defaultIds(input.readiness_feedback_version_ids);`,
    ];
    for (const source of fixtures) {
      expect(auditFrontendSources(new Map([
        ['web/src/services/interviewPreparationProposals.ts', source],
      ]))).toContain('preparation:missing-defaulted-empty');
    }
  });

  it('tracks top-level navigation mutations through aliases', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/layout/navigation.ts', `
        const MODULE_NAV = [{ key: 'today' }, { key: 'applications' }, { key: 'interview' }, { key: 'offers' }, { key: 'resources' }, { key: 'settings' }];
        const nav = MODULE_NAV; nav.push({ key: 'readiness' });
      `],
    ]))).toContain('navigation:top-level-expanded');
  });

  it('tracks same-file CRUD functions assigned into members', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/Owner.tsx', `
        import { updateInterviewNote } from '@/services/notes';
        const api = {}; api.save = updateInterviewNote; api.save(noteId, input);
      `],
    ]))).toContain('ui:direct-domain-crud');
  });

  it('tracks imported V2 selectors through aliases, object members, and callable factories', () => {
    const selectorModule = ['web/src/selectors/practice.ts', `
      export function choose(plans) { return plans.find((plan) => plan.status === 'in_progress'); }
    `] as const;
    const exactType = `type Exact = { readinessSignalVersionId: number; targetEventId: number };`;
    const probes = [
      `${exactType} import { choose } from '@/selectors/practice'; const pick = choose; function owner(request: Exact, plans) { return pick(plans); }`,
      `${exactType} import { choose } from '@/selectors/practice'; const selectors = { pick: choose }; function owner(request: Exact, plans) { return selectors.pick(plans); }`,
      `${exactType} import { choose } from '@/selectors/practice'; function get() { return choose; } function owner(request: Exact, plans) { return get()(plans); }`,
    ];
    for (const source of probes) {
      expect(auditFrontendSources(new Map([
        selectorModule,
        ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', source],
      ])), source).toContain('practice:implicit-target-fallback');
    }
  });

  it('tracks post-init and returned bound transports', () => {
    const probes = [
      `const transport = {}; transport.send = http.post.bind(http); transport.send('/api/stories/7', input);`,
      `function getPost() { return http.post.bind(http); } getPost()('/api/stories/7', input);`,
    ];
    for (const source of probes) {
      expect(auditFrontendSources(new Map([
        ['web/src/features/reviewReadiness/service.ts', source],
      ]))).toContain('ui:stories-api-alias');
    }
  });

  it('tracks generic undo through objects, namespaces, and callable factories', () => {
    const operations = ['web/src/services/writeOperations.ts', `export function undoWriteOperation(id) {}`] as const;
    const probes = [
      `import { undoWriteOperation as undo } from '@/services/writeOperations'; const api = { undo }; api.undo(id);`,
      `import * as operations from '@/services/writeOperations'; operations.undoWriteOperation(id);`,
      `import { undoWriteOperation as undo } from '@/services/writeOperations'; function getUndo() { return undo; } getUndo()(id);`,
    ];
    for (const source of probes) {
      expect(auditFrontendSources(new Map([
        operations,
        ['web/src/features/reviewReadiness/Owner.tsx', source],
      ]))).toContain('ui:generic-operation-undo');
    }
  });

  it('tracks Haru default-export object readiness facades', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/services/readiness.ts', `export function getEventReadinessFeedback() {}`],
      ['web/src/facades/readiness.ts', `
        import { getEventReadinessFeedback } from '@/services/readiness';
        export default { load: getEventReadinessFeedback };
      `],
      ['web/src/features/haru/HaruCard.tsx', `
        import readiness from '@/facades/readiness'; readiness.load(applicationId, eventId);
      `],
    ]))).toContain('pilot:review-readiness-ownership');
  });

  it('tracks sensitive aliases, returned sinks, post-init members, and location href aliases', () => {
    const probes = [
      `const secret = proposal.confirmation_token; function save(value) { sessionStorage.setItem('state', value); } const persist = save; persist(secret);`,
      `const secret = proposal.confirmation_token; function getSink() { return sessionStorage.setItem.bind(sessionStorage); } getSink()('state', secret);`,
      `const secret = proposal.confirmation_token; const sinks = {}; sinks.save = sessionStorage.setItem.bind(sessionStorage); sinks.save('state', secret);`,
      `const secret = proposal.confirmation_token; const nav = location; nav.href = secret;`,
    ];
    for (const source of probes) {
      expect(auditFrontendSources(new Map([
        ['web/src/layout/AppShell.tsx', source],
      ]))).toContain('privacy:sensitive-client-persistence');
    }
  });

  it('tracks Preparation sources in object properties and callable default factories', () => {
    const probes = [
      `const box = { ids: input.readiness_feedback_version_ids }; const ids = box.ids ?? [];`,
      `function defaultIds(value) { return value ?? []; } function getDefault() { return defaultIds; } const ids = getDefault()(input.readiness_feedback_version_ids);`,
    ];
    for (const source of probes) {
      expect(auditFrontendSources(new Map([
        ['web/src/services/interviewPreparationProposals.ts', source],
      ]))).toContain('preparation:missing-defaulted-empty');
    }
  });

  it('tracks navigation mutations through factories, object properties, and destructured receivers', () => {
    const prefix = `const MODULE_NAV = [{ key: 'today' }, { key: 'applications' }, { key: 'interview' }, { key: 'offers' }, { key: 'resources' }, { key: 'settings' }];`;
    const probes = [
      `${prefix} function getNav() { return MODULE_NAV; } getNav().push({ key: 'readiness' });`,
      `${prefix} const holder = { nav: MODULE_NAV }; holder.nav.push({ key: 'readiness' });`,
      `${prefix} const holder = { nav: MODULE_NAV }; const { nav } = holder; nav.push({ key: 'readiness' });`,
    ];
    for (const source of probes) {
      expect(auditFrontendSources(new Map([
        ['web/src/layout/navigation.ts', source],
      ]))).toContain('navigation:top-level-expanded');
    }
  });

  it('requires structural V2 provenance instead of parameter names', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `
        function owner(focus, plans) { return plans.find((plan) => plan.status === 'in_progress'); }
      `],
    ]))).not.toContain('practice:implicit-target-fallback');
  });

  it('preserves Preparation explicit-empty semantics behind standard own-key guards', () => {
    const safe = [
      `const present = Object.hasOwn(input, 'readiness_feedback_version_ids'); const body = present ? { readiness_feedback_version_ids: input.readiness_feedback_version_ids ?? [] } : legacyBody;`,
      `if (Object.prototype.hasOwnProperty.call(input, 'readiness_feedback_version_ids')) { return { readiness_feedback_version_ids: input.readiness_feedback_version_ids ?? [] }; }`,
    ];
    for (const source of safe) {
      expect(auditFrontendSources(new Map([
        ['web/src/services/interviewPreparationProposals.ts', source],
      ]))).not.toContain('preparation:missing-defaulted-empty');
    }
  });

  it('preserves callable object identity through aliases, spreads, destructuring, and object factories', () => {
    const selectorModule = ['web/src/selectors/practice.ts', `export function choose(plans) { return plans.find((plan) => plan.status === 'in_progress'); }`] as const;
    const prefix = `type Exact = { readinessSignalVersionId: number; targetEventId: number }; import { choose } from '@/selectors/practice';`;
    const probes = [
      `${prefix} const api = { pick: choose }; const copy = api; function owner(request: Exact, plans) { return copy.pick(plans); }`,
      `${prefix} const api = { pick: choose }; const spread = { ...api }; function owner(request: Exact, plans) { return spread.pick(plans); }`,
      `${prefix} const api = { pick: choose }; const { pick: select } = api; function owner(request: Exact, plans) { return select(plans); }`,
      `${prefix} const api = { pick: choose }; function getApi() { return api; } const { pick } = getApi(); function owner(request: Exact, plans) { return pick(plans); }`,
    ];
    for (const source of probes) {
      expect(auditFrontendSources(new Map([
        selectorModule,
        ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', source],
      ])), source).toContain('practice:implicit-target-fallback');
    }
  });

  it('shares object identity provenance across undo, navigation, and Haru ownership gates', () => {
    const operations = ['web/src/services/writeOperations.ts', `export function undoWriteOperation(id) {}`] as const;
    const undoProbes = [
      `import { undoWriteOperation as undo } from '@/services/writeOperations'; const api = { undo }; const copy = api; copy.undo(id);`,
      `import { undoWriteOperation as undo } from '@/services/writeOperations'; const api = { undo }; const spread = { ...api }; const { undo: run } = spread; run(id);`,
      `import { undoWriteOperation as undo } from '@/services/writeOperations'; const api = { undo }; function getApi() { return api; } const { undo: run } = getApi(); run(id);`,
    ];
    for (const source of undoProbes) {
      expect(auditFrontendSources(new Map([
        operations,
        ['web/src/features/reviewReadiness/Owner.tsx', source],
      ])), source).toContain('ui:generic-operation-undo');
    }

    const navPrefix = `const MODULE_NAV = [{ key: 'today' }, { key: 'applications' }, { key: 'interview' }, { key: 'offers' }, { key: 'resources' }, { key: 'settings' }];`;
    const navProbes = [
      `${navPrefix} const box = { nav: MODULE_NAV }; const copy = box; copy.nav.push({ key: 'readiness' });`,
      `${navPrefix} const box = { nav: MODULE_NAV }; const spread = { ...box }; const { nav } = spread; nav.push({ key: 'readiness' });`,
      `${navPrefix} const box = { nav: MODULE_NAV }; function getBox() { return box; } const { nav } = getBox(); nav.push({ key: 'readiness' });`,
    ];
    for (const source of navProbes) {
      expect(auditFrontendSources(new Map([
        ['web/src/layout/navigation.ts', source],
      ])), source).toContain('navigation:top-level-expanded');
    }

    const readinessSources = [
      ['web/src/services/readiness.ts', `export function getEventReadinessFeedback() {}`],
      ['web/src/facades/readiness.ts', `import { getEventReadinessFeedback } from '@/services/readiness'; export default { load: getEventReadinessFeedback };`],
    ] as const;
    const haruProbes = [
      `import readiness from '@/facades/readiness'; const copy = readiness; copy.load(applicationId, eventId);`,
      `import readiness from '@/facades/readiness'; const spread = { ...readiness }; const { load } = spread; load(applicationId, eventId);`,
      `import readiness from '@/facades/readiness'; function getApi() { return readiness; } const { load } = getApi(); load(applicationId, eventId);`,
    ];
    for (const source of haruProbes) {
      expect(auditFrontendSources(new Map([
        ...readinessSources,
        ['web/src/features/haru/HaruCard.tsx', source],
      ])), source).toContain('pilot:review-readiness-ownership');
    }
  });

  it('tracks nested CRUD member paths', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/Owner.tsx', `
        import { updateInterviewNote } from '@/services/notes';
        const api = {}; const holder = { api }; holder.api.save = updateInterviewNote; holder.api.save(noteId, input);
      `],
    ]))).toContain('ui:direct-domain-crud');
  });

  it('tracks returned transports through aliases, spreads, and destructuring', () => {
    const probes = [
      `function getPost() { return http.post.bind(http); } const send = getPost(); send('/api/stories/7', input);`,
      `const api = { send: http.post.bind(http) }; const copy = api; copy.send('/api/stories/7', input);`,
      `const api = { send: http.post.bind(http) }; const spread = { ...api }; spread.send('/api/stories/7', input);`,
      `const api = { send: http.post.bind(http) }; const { send } = api; send('/api/stories/7', input);`,
    ];
    for (const source of probes) {
      expect(auditFrontendSources(new Map([
        ['web/src/features/reviewReadiness/service.ts', source],
      ]))).toContain('ui:stories-api-alias');
    }
  });

  it('tracks sensitive sink object identity and window.location aliases', () => {
    const probes = [
      `const secret = proposal.confirmation_token; const api = { save: sessionStorage.setItem.bind(sessionStorage) }; const copy = api; copy.save('state', secret);`,
      `const secret = proposal.confirmation_token; const api = { save: sessionStorage.setItem.bind(sessionStorage) }; const spread = { ...api }; const { save } = spread; save('state', secret);`,
      `const secret = proposal.confirmation_token; const nav = window.location; nav.href = secret;`,
    ];
    for (const source of probes) {
      expect(auditFrontendSources(new Map([
        ['web/src/layout/AppShell.tsx', source],
      ]))).toContain('privacy:sensitive-client-persistence');
    }
  });

  it('tracks Preparation factory values, object aliases, destructuring, and post-init members', () => {
    const probes = [
      `function make() { return input.readiness_feedback_version_ids; } const d = make(); const ids = d ?? [];`,
      `const box = { ids: input.readiness_feedback_version_ids }; const alias = box; const ids = alias.ids ?? [];`,
      `const box = { ids: input.readiness_feedback_version_ids }; const { ids: source } = box; const ids = source ?? [];`,
      `const box = {}; box.ids = input.readiness_feedback_version_ids; const ids = box.ids ?? [];`,
    ];
    for (const source of probes) {
      expect(auditFrontendSources(new Map([
        ['web/src/services/interviewPreparationProposals.ts', source],
      ]))).toContain('preparation:missing-defaulted-empty');
    }
  });

  it('applies statement-order kills and unsafe reassignment for callable gates', () => {
    const selector = ['web/src/selectors/practice.ts', `export function choose(plans) { return plans.find((plan) => plan.status === 'in_progress'); }`] as const;
    const exact = `type Exact = { readinessSignalVersionId: number; targetEventId: number }; import { choose } from '@/selectors/practice'; const safe = (plans) => plans;`;
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} let pick = choose; pick = safe; function owner(request: Exact, plans) { return pick(plans); }`],
    ]))).not.toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} let pick = safe; pick = choose; function owner(request: Exact, plans) { return pick(plans); }`],
    ]))).toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} const api = { pick: choose }; api.pick = safe; function owner(request: Exact, plans) { return api.pick(plans); }`],
    ]))).not.toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} const api = { pick: safe }; api.pick = choose; function owner(request: Exact, plans) { return api.pick(plans); }`],
    ]))).toContain('practice:implicit-target-fallback');

    const operations = ['web/src/services/writeOperations.ts', `export function undoWriteOperation(id) {}`] as const;
    expect(auditFrontendSources(new Map([
      operations,
      ['web/src/features/reviewReadiness/Owner.tsx', `import { undoWriteOperation } from '@/services/writeOperations'; let undo = undoWriteOperation; undo = () => undefined; undo(id);`],
    ]))).not.toContain('ui:generic-operation-undo');
    expect(auditFrontendSources(new Map([
      operations,
      ['web/src/features/reviewReadiness/Owner.tsx', `import { undoWriteOperation } from '@/services/writeOperations'; let undo = () => undefined; undo = undoWriteOperation; undo(id);`],
    ]))).toContain('ui:generic-operation-undo');
    expect(auditFrontendSources(new Map([
      operations,
      ['web/src/features/reviewReadiness/Owner.tsx', `import { undoWriteOperation } from '@/services/writeOperations'; const api = { undo: undoWriteOperation }; api.undo = () => undefined; api.undo(id);`],
    ]))).not.toContain('ui:generic-operation-undo');

    const navPrefix = `const MODULE_NAV = [{ key: 'today' }, { key: 'applications' }, { key: 'interview' }, { key: 'offers' }, { key: 'resources' }, { key: 'settings' }]; const safe = [];`;
    expect(auditFrontendSources(new Map([
      ['web/src/layout/navigation.ts', `${navPrefix} let nav = MODULE_NAV; nav = safe; nav.push({ key: 'readiness' });`],
    ]))).not.toContain('navigation:top-level-expanded');
    expect(auditFrontendSources(new Map([
      ['web/src/layout/navigation.ts', `${navPrefix} let nav = safe; nav = MODULE_NAV; nav.push({ key: 'readiness' });`],
    ]))).toContain('navigation:top-level-expanded');
    expect(auditFrontendSources(new Map([
      ['web/src/layout/navigation.ts', `${navPrefix} const holder = { nav: MODULE_NAV }; holder.nav = safe; holder.nav.push({ key: 'readiness' });`],
    ]))).not.toContain('navigation:top-level-expanded');

    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/service.ts', `const safe = (path) => path; let send = http.post; send = safe; send('/api/stories/7');`],
    ]))).not.toContain('ui:stories-api-alias');
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/service.ts', `const safe = (path) => path; let send = safe; send = http.post; send('/api/stories/7');`],
    ]))).toContain('ui:stories-api-alias');
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/service.ts', `const safe = (path) => path; const api = { send: http.post }; api.send = safe; api.send('/api/stories/7');`],
    ]))).not.toContain('ui:stories-api-alias');
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/service.ts', `const safe = (path) => path; const api = { send: safe }; api.send = http.post; api.send('/api/stories/7');`],
    ]))).toContain('ui:stories-api-alias');
  });

  it('joins conditional and if/else control-flow branches conservatively', () => {
    const selector = ['web/src/selectors/practice.ts', `export function choose(plans) { return plans.find((plan) => plan.status === 'in_progress'); }`] as const;
    const exact = `type Exact = { readinessSignalVersionId: number; targetEventId: number }; import { choose } from '@/selectors/practice'; const safe = (plans) => plans;`;
    const v2Unsafe = [
      `${exact} let pick = flag ? choose : safe; function owner(request: Exact, plans) { return pick(plans); }`,
      `${exact} let pick = safe; if (flag) { pick = choose; } else { pick = safe; } function owner(request: Exact, plans) { return pick(plans); }`,
    ];
    for (const source of v2Unsafe) {
      expect(auditFrontendSources(new Map([
        selector,
        ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', source],
      ])), source).toContain('practice:implicit-target-fallback');
    }
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} let pick = choose; if (flag) { pick = safe; } else { pick = safe; } function owner(request: Exact, plans) { return pick(plans); }`],
    ]))).not.toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} const api = { pick: safe }; if (flag) api.pick = choose; else api.pick = safe; function owner(request: Exact, plans) { return api.pick(plans); }`],
    ]))).toContain('practice:implicit-target-fallback');

    const operations = ['web/src/services/writeOperations.ts', `export function undoWriteOperation(id) {}`] as const;
    expect(auditFrontendSources(new Map([
      operations,
      ['web/src/features/reviewReadiness/Owner.tsx', `import { undoWriteOperation } from '@/services/writeOperations'; const safe = () => undefined; let undo = safe; if (flag) undo = undoWriteOperation; else undo = safe; undo(id);`],
    ]))).toContain('ui:generic-operation-undo');
    expect(auditFrontendSources(new Map([
      operations,
      ['web/src/features/reviewReadiness/Owner.tsx', `import { undoWriteOperation } from '@/services/writeOperations'; const safe = () => undefined; let undo = undoWriteOperation; if (flag) undo = safe; else undo = safe; undo(id);`],
    ]))).not.toContain('ui:generic-operation-undo');

    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/service.ts', `const safe = (path) => path; let send = safe; if (flag) send = http.post; else send = safe; send('/api/stories/7');`],
    ]))).toContain('ui:stories-api-alias');
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/service.ts', `const safe = (path) => path; let send = http.post; if (flag) send = safe; else send = safe; send('/api/stories/7');`],
    ]))).not.toContain('ui:stories-api-alias');
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/service.ts', `const safe = (path) => path; const api = { send: safe }; if (flag) api.send = http.post; else api.send = safe; api.send('/api/stories/7');`],
    ]))).toContain('ui:stories-api-alias');
  });

  it('respects lexical shadowing and closure capture at the call environment', () => {
    const selector = ['web/src/selectors/practice.ts', `export function choose(plans) { return plans.find((plan) => plan.status === 'in_progress'); }`] as const;
    const exact = `type Exact = { readinessSignalVersionId: number; targetEventId: number }; import { choose } from '@/selectors/practice'; const safe = (plans) => plans;`;
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} const pick = choose; { const pick = safe; void pick; } function owner(request: Exact, plans) { return pick(plans); }`],
    ]))).toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} const pick = safe; { const pick = choose; void pick; } function owner(request: Exact, plans) { return pick(plans); }`],
    ]))).not.toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} let pick = safe; const run = () => pick(); pick = choose; function owner(request: Exact) { return run(); }`],
    ]))).toContain('practice:implicit-target-fallback');
  });

  it('propagates callable provenance through helper arguments and parameters', () => {
    const selector = ['web/src/selectors/practice.ts', `export function choose(plans) { return plans.find((plan) => plan.status === 'in_progress'); }`] as const;
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `
        type Exact = { readinessSignalVersionId: number; targetEventId: number };
        import { choose } from '@/selectors/practice'; function identity(fn) { return fn; }
        const pick = identity(choose); function owner(request: Exact, plans) { return pick(plans); }
      `],
    ]))).toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      ['web/src/services/writeOperations.ts', `export function undoWriteOperation(id) {}`],
      ['web/src/features/reviewReadiness/Owner.tsx', `
        import { undoWriteOperation } from '@/services/writeOperations'; function identity(fn) { return fn; }
        const undo = identity(undoWriteOperation); undo(id);
      `],
    ]))).toContain('ui:generic-operation-undo');
  });

  it('tracks Preparation default factories after assignment', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/services/interviewPreparationProposals.ts', `
        function defaultIds(value) { return value ?? []; }
        function make() { return defaultIds; }
        const d = make(); d(input.readiness_feedback_version_ids);
      `],
    ]))).toContain('preparation:missing-defaulted-empty');
  });

  it('applies flow-sensitive kills to CRUD, sensitive sinks, and Preparation sources', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/Owner.tsx', `import { updateInterviewNote } from '@/services/notes'; let save = updateInterviewNote; save = (value) => value; save(input);`],
    ]))).not.toContain('ui:direct-domain-crud');
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/Owner.tsx', `import { updateInterviewNote } from '@/services/notes'; let save = (value) => value; save = updateInterviewNote; save(input);`],
    ]))).toContain('ui:direct-domain-crud');
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/Owner.tsx', `import { updateInterviewNote } from '@/services/notes'; const api = { save: updateInterviewNote }; api.save = (value) => value; api.save(input);`],
    ]))).not.toContain('ui:direct-domain-crud');
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/Owner.tsx', `import { updateInterviewNote } from '@/services/notes'; const api = { save: (value) => value }; api.save = updateInterviewNote; api.save(input);`],
    ]))).toContain('ui:direct-domain-crud');
    expect(auditFrontendSources(new Map([
      ['web/src/layout/AppShell.tsx', `const secret = proposal.confirmation_token; let save = sessionStorage.setItem.bind(sessionStorage); save = () => undefined; save('state', secret);`],
    ]))).not.toContain('privacy:sensitive-client-persistence');
    expect(auditFrontendSources(new Map([
      ['web/src/layout/AppShell.tsx', `const secret = proposal.confirmation_token; let save = () => undefined; save = sessionStorage.setItem.bind(sessionStorage); save('state', secret);`],
    ]))).toContain('privacy:sensitive-client-persistence');
    expect(auditFrontendSources(new Map([
      ['web/src/layout/AppShell.tsx', `const secret = proposal.confirmation_token; const api = { save: sessionStorage.setItem.bind(sessionStorage) }; api.save = () => undefined; api.save('state', secret);`],
    ]))).not.toContain('privacy:sensitive-client-persistence');
    expect(auditFrontendSources(new Map([
      ['web/src/layout/AppShell.tsx', `const secret = proposal.confirmation_token; const api = { save: () => undefined }; api.save = sessionStorage.setItem.bind(sessionStorage); api.save('state', secret);`],
    ]))).toContain('privacy:sensitive-client-persistence');
    expect(auditFrontendSources(new Map([
      ['web/src/services/interviewPreparationProposals.ts', `let source = input.readiness_feedback_version_ids; source = explicitIds; const ids = source ?? [];`],
    ]))).not.toContain('preparation:missing-defaulted-empty');
    expect(auditFrontendSources(new Map([
      ['web/src/services/interviewPreparationProposals.ts', `let source = explicitIds; source = input.readiness_feedback_version_ids; const ids = source ?? [];`],
    ]))).toContain('preparation:missing-defaulted-empty');
    expect(auditFrontendSources(new Map([
      ['web/src/services/interviewPreparationProposals.ts', `const box = { ids: input.readiness_feedback_version_ids }; box.ids = explicitIds; const ids = box.ids ?? [];`],
    ]))).not.toContain('preparation:missing-defaulted-empty');
    expect(auditFrontendSources(new Map([
      ['web/src/services/interviewPreparationProposals.ts', `const box = { ids: explicitIds }; box.ids = input.readiness_feedback_version_ids; const ids = box.ids ?? [];`],
    ]))).toContain('preparation:missing-defaulted-empty');
  });

  it('joins loop, switch, and try control-flow without treating unreachable loops as writes', () => {
    const selector = ['web/src/selectors/practice.ts', `export function choose(plans) { return plans.find((plan) => plan.status === 'in_progress'); }`] as const;
    const exact = `type Exact = { readinessSignalVersionId: number; targetEventId: number }; import { choose } from '@/selectors/practice'; const safe = (plans) => plans;`;
    for (const body of [
      `let pick = safe; while (flag) { pick = choose; }`,
      `let pick = safe; for (; flag;) { pick = choose; break; }`,
      `let pick = safe; for (const item of items) { pick = choose; }`,
      `let pick = safe; switch (mode) { case 'implicit': pick = choose; break; default: pick = safe; }`,
      `let pick = safe; try { pick = choose; } catch { pick = safe; }`,
    ]) {
      expect(auditFrontendSources(new Map([
        selector,
        ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} ${body} function owner(request: Exact, plans) { return pick(plans); }`],
      ])), body).toContain('practice:implicit-target-fallback');
    }
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} let pick = safe; while (false) { pick = choose; } function owner(request: Exact, plans) { return pick(plans); }`],
    ]))).not.toContain('practice:implicit-target-fallback');

    expect(auditFrontendSources(new Map([
      ['web/src/services/writeOperations.ts', `export function undoWriteOperation(id) {}`],
      ['web/src/features/reviewReadiness/Owner.tsx', `import { undoWriteOperation } from '@/services/writeOperations'; const safe = () => undefined; let undo = safe; for (; flag;) { undo = undoWriteOperation; break; } undo(id);`],
    ]))).toContain('ui:generic-operation-undo');
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/service.ts', `const safe = (path) => path; let send = safe; try { send = http.post; } catch { send = safe; } send('/api/stories/7');`],
    ]))).toContain('ui:stories-api-alias');
  });

  it('keeps only reachable function returns and preserves may-target early returns', () => {
    const selector = ['web/src/selectors/practice.ts', `export function choose(plans) { return plans.find((plan) => plan.status === 'in_progress'); }`] as const;
    const prefix = `type Exact = { readinessSignalVersionId: number; targetEventId: number }; import { choose } from '@/selectors/practice'; const safe = (plans) => plans;`;
    for (const factory of [
      `function make(flag) { if (flag) return choose; return safe; }`,
      `function make() { return choose; return safe; }`,
    ]) {
      expect(auditFrontendSources(new Map([
        selector,
        ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${prefix} ${factory} const pick = make(flag); function owner(request: Exact, plans) { return pick(plans); }`],
      ])), factory).toContain('practice:implicit-target-fallback');
    }
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${prefix} function make() { return safe; return choose; } const pick = make(); function owner(request: Exact, plans) { return pick(plans); }`],
    ]))).not.toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${prefix} function make() { if (false) return choose; return safe; } const pick = make(); function owner(request: Exact, plans) { return pick(plans); }`],
    ]))).not.toContain('practice:implicit-target-fallback');
  });

  it('applies closure writes only at reachable call points and preserves earlier unsafe calls', () => {
    const selector = ['web/src/selectors/practice.ts', `export function choose(plans) { return plans.find((plan) => plan.status === 'in_progress'); }`] as const;
    const prefix = `type Exact = { readinessSignalVersionId: number; targetEventId: number }; import { choose } from '@/selectors/practice'; const safe = (plans) => plans;`;
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${prefix} let pick = safe; const setImplicit = () => { pick = choose; }; function owner(request: Exact, plans) { return pick(plans); }`],
    ]))).not.toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${prefix} let pick = safe; function setImplicit() { return; pick = choose; } setImplicit(); function owner(request: Exact, plans) { return pick(plans); }`],
    ]))).not.toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${prefix} let pick = safe; const setImplicit = () => { pick = choose; }; setImplicit(); function owner(request: Exact, plans) { return pick(plans); }`],
    ]))).toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${prefix} let pick = safe; const setImplicit = () => { pick = choose; }; function owner(request: Exact, plans) { return pick(plans); } setImplicit(); owner(request, plans); pick = safe; owner(request, plans);`],
    ]))).toContain('practice:implicit-target-fallback');
  });

  it('tracks anonymous Preparation default factories returned through an assigned callable', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/services/interviewPreparationProposals.ts', `function make() { return (value) => value ?? []; } const d = make(); d(input.readiness_feedback_version_ids);`],
    ]))).toContain('preparation:missing-defaulted-empty');
    expect(auditFrontendSources(new Map([
      ['web/src/services/interviewPreparationProposals.ts', `function make() { return (value) => value; } const d = make(); d(input.readiness_feedback_version_ids);`],
    ]))).not.toContain('preparation:missing-defaulted-empty');
  });

  it('applies loop, switch, try, and reachability joins to callable object members', () => {
    const selector = ['web/src/selectors/practice.ts', `export function choose(plans) { return plans.find((plan) => plan.status === 'in_progress'); }`] as const;
    const exact = `type Exact = { readinessSignalVersionId: number; targetEventId: number }; import { choose } from '@/selectors/practice'; const safe = (plans) => plans;`;
    for (const mutation of [
      `while (flag) { api.pick = choose; }`,
      `for (; flag;) { api.pick = choose; break; }`,
      `switch (mode) { case 'implicit': api.pick = choose; break; default: api.pick = safe; }`,
      `try { api.pick = choose; } catch { api.pick = safe; }`,
    ]) {
      expect(auditFrontendSources(new Map([
        selector,
        ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} const api = { pick: safe }; ${mutation} function owner(request: Exact, plans) { return api.pick(plans); }`],
      ])), mutation).toContain('practice:implicit-target-fallback');
    }
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} const api = { pick: safe }; while (false) { api.pick = choose; } function owner(request: Exact, plans) { return api.pick(plans); }`],
    ]))).not.toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      ['web/src/services/writeOperations.ts', `export function undoWriteOperation(id) {}`],
      ['web/src/features/reviewReadiness/Owner.tsx', `import { undoWriteOperation } from '@/services/writeOperations'; const safe = () => undefined; const api = { undo: safe }; for (const item of items) api.undo = undoWriteOperation; api.undo(id);`],
    ]))).toContain('ui:generic-operation-undo');
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/service.ts', `const safe = (path) => path; const api = { send: safe }; try { api.send = http.post; } catch { api.send = safe; } api.send('/api/stories/7');`],
    ]))).toContain('ui:stories-api-alias');
  });

  it('summarizes reachable returns inside while, do, for, and for-of statements', () => {
    const selector = ['web/src/selectors/practice.ts', `export function choose(plans) { return plans.find((plan) => plan.status === 'in_progress'); }`] as const;
    const prefix = `type Exact = { readinessSignalVersionId: number; targetEventId: number }; import { choose } from '@/selectors/practice'; const safe = (plans) => plans;`;
    for (const factory of [
      `function make(flag) { while (flag) { return choose; } return safe; }`,
      `function make() { do { return choose; } while (false); return safe; }`,
      `function make(flag) { for (; flag;) { return choose; } return safe; }`,
      `function make(items) { for (const item of items) { return choose; } return safe; }`,
    ]) {
      expect(auditFrontendSources(new Map([
        selector,
        ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${prefix} ${factory} const pick = make(flag); function owner(request: Exact, plans) { return pick(plans); }`],
      ])), factory).toContain('practice:implicit-target-fallback');
    }
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${prefix} function make() { while (false) { return choose; } return safe; } const pick = make(); function owner(request: Exact, plans) { return pick(plans); }`],
    ]))).not.toContain('practice:implicit-target-fallback');
  });

  it('projects captured writes through aliases, members, and Function.call only when invoked', () => {
    const selector = ['web/src/selectors/practice.ts', `export function choose(plans) { return plans.find((plan) => plan.status === 'in_progress'); }`] as const;
    const prefix = `type Exact = { readinessSignalVersionId: number; targetEventId: number }; import { choose } from '@/selectors/practice'; const safe = (plans) => plans; let pick = safe; const poison = () => { pick = choose; };`;
    for (const invoke of [`const alias = poison; alias();`, `const api = { poison }; api.poison();`, `poison.call(null);`]) {
      expect(auditFrontendSources(new Map([
        selector,
        ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${prefix} ${invoke} function owner(request: Exact, plans) { return pick(plans); }`],
      ])), invoke).toContain('practice:implicit-target-fallback');
    }
    for (const dormant of [`const alias = poison; void alias;`, `const api = { poison }; void api;`]) {
      expect(auditFrontendSources(new Map([
        selector,
        ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${prefix} ${dormant} function owner(request: Exact, plans) { return pick(plans); }`],
      ])), dormant).not.toContain('practice:implicit-target-fallback');
    }
  });

  it('follows returned selectors and generic undo through cross-file facades and pure re-exports', () => {
    const selector = ['web/src/selectors/practice.ts', `export function choose(plans) { return plans.find((plan) => plan.status === 'in_progress'); }`] as const;
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/selectors/facade.ts', `import { choose } from './practice'; export function getSelector() { return choose; }`],
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `type Exact = { readinessSignalVersionId: number; targetEventId: number }; import { getSelector } from '@/selectors/facade'; function owner(request: Exact, plans) { return getSelector()(plans); }`],
    ]))).toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      ['web/src/selectors/facade.ts', `const safe = (plans) => plans; export function getSelector() { return safe; }`],
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `type Exact = { readinessSignalVersionId: number; targetEventId: number }; import { getSelector } from '@/selectors/facade'; function owner(request: Exact, plans) { return getSelector()(plans); }`],
    ]))).not.toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      ['web/src/services/writeOperations.ts', `export function undoWriteOperation(id) {}`],
      ['web/src/services/operationsFacade.ts', `export { undoWriteOperation as undo } from './writeOperations';`],
      ['web/src/features/reviewReadiness/Owner.tsx', `import { undo } from '@/services/operationsFacade'; undo(id);`],
    ]))).toContain('ui:generic-operation-undo');
    expect(auditFrontendSources(new Map([
      ['web/src/utils/undo.ts', `export function undo(id) {}`],
      ['web/src/services/operationsFacade.ts', `export { undo } from '@/utils/undo';`],
      ['web/src/features/reviewReadiness/Owner.tsx', `import { undo } from '@/services/operationsFacade'; undo(id);`],
    ]))).not.toContain('ui:generic-operation-undo');
  });

  it('propagates Preparation defaults and sensitive sinks through imported helpers', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/utils/defaultIds.ts', `export function defaultIds(value) { return value ?? []; }`],
      ['web/src/services/interviewPreparationProposals.ts', `import { defaultIds } from '@/utils/defaultIds'; defaultIds(input.readiness_feedback_version_ids);`],
    ]))).toContain('preparation:missing-defaulted-empty');
    expect(auditFrontendSources(new Map([
      ['web/src/utils/defaultIds.ts', `export function defaultIds(value) { return value; }`],
      ['web/src/services/interviewPreparationProposals.ts', `import { defaultIds } from '@/utils/defaultIds'; defaultIds(input.readiness_feedback_version_ids);`],
    ]))).not.toContain('preparation:missing-defaulted-empty');
    expect(auditFrontendSources(new Map([
      ['web/src/utils/persist.ts', `export function persist(value) { sessionStorage.setItem('state', value); }`],
      ['web/src/layout/AppShell.tsx', `import { persist } from '@/utils/persist'; persist(proposal.confirmation_token);`],
    ]))).toContain('privacy:sensitive-client-persistence');
    expect(auditFrontendSources(new Map([
      ['web/src/utils/persist.ts', `export function persist(value) { return value; }`],
      ['web/src/layout/AppShell.tsx', `import { persist } from '@/utils/persist'; persist(proposal.confirmation_token);`],
    ]))).not.toContain('privacy:sensitive-client-persistence');
  });

  it('joins every nested control-flow ancestor for identifier and member callables', () => {
    const selector = ['web/src/selectors/practice.ts', `export function choose(plans) { return plans.find((plan) => plan.status === 'in_progress'); }`] as const;
    const exact = `type Exact = { readinessSignalVersionId: number; targetEventId: number }; import { choose } from '@/selectors/practice'; const safe = (plans) => plans;`;
    for (const body of [
      `let pick = safe; while (outer) { if (inner) pick = choose; }`,
      `let pick = safe; if (outer) { for (const item of items) { pick = choose; } }`,
      `const api = { pick: safe }; while (outer) { if (inner) api.pick = choose; }`,
      `const api = { pick: safe }; if (outer) { for (const item of items) api.pick = choose; }`,
    ]) {
      const call = body.includes('api.') ? 'api.pick(plans)' : 'pick(plans)';
      expect(auditFrontendSources(new Map([
        selector,
        ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} ${body} function owner(request: Exact, plans) { return ${call}; }`],
      ])), body).toContain('practice:implicit-target-fallback');
    }
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} let pick = safe; while (outer) { if (inner) pick = safe; } function owner(request: Exact, plans) { return pick(plans); }`],
    ]))).not.toContain('practice:implicit-target-fallback');
    for (const body of [
      `let pick = choose; while (outer) { if (inner) pick = safe; else pick = safe; }`,
      `let pick = choose; if (outer) { do { pick = safe; } while (inner); }`,
      `const api = { pick: choose }; while (outer) { if (inner) api.pick = safe; else api.pick = safe; }`,
      `const api = { pick: choose }; if (outer) { do { api.pick = safe; } while (inner); }`,
    ]) {
      const call = body.includes('api.') ? 'api.pick(plans)' : 'pick(plans)';
      expect(auditFrontendSources(new Map([
        selector,
        ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} ${body} function owner(request: Exact, plans) { return ${call}; }`],
      ])), body).toContain('practice:implicit-target-fallback');
    }
    expect(auditFrontendSources(new Map([
      ['web/src/services/writeOperations.ts', `export function undoWriteOperation(id) {}`],
      ['web/src/features/reviewReadiness/Owner.tsx', `import { undoWriteOperation } from '@/services/writeOperations'; const safe = () => undefined; let undo = safe; while (outer) { if (inner) undo = undoWriteOperation; } undo(id);`],
    ]))).toContain('ui:generic-operation-undo');
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/service.ts', `const safe = (path) => path; const api = { send: safe }; if (outer) { for (const item of items) { api.send = http.post; } } api.send('/api/stories/7');`],
    ]))).toContain('ui:stories-api-alias');
    expect(auditFrontendSources(new Map([
      ['web/src/services/interviewPreparationProposals.ts', `const safe = (value) => value; const defaults = (value) => value ?? []; let d = safe; while (outer) { if (inner) d = defaults; } d(input.readiness_feedback_version_ids);`],
    ]))).toContain('preparation:missing-defaulted-empty');
    expect(auditFrontendSources(new Map([
      ['web/src/layout/AppShell.tsx', `const safe = () => undefined; const api = { persist: safe }; if (outer) { for (const item of items) api.persist = sessionStorage.setItem.bind(sessionStorage); } api.persist('state', proposal.confirmation_token);`],
    ]))).toContain('privacy:sensitive-client-persistence');
  });

  it('applies captured closure writes through destructure, spread, factories, and bind at call points', () => {
    const selector = ['web/src/selectors/practice.ts', `export function choose(plans) { return plans.find((plan) => plan.status === 'in_progress'); }`] as const;
    const prefix = `type Exact = { readinessSignalVersionId: number; targetEventId: number }; import { choose } from '@/selectors/practice'; const safe = (plans) => plans; let pick = safe; const poison = () => { pick = choose; };`;
    for (const invoke of [
      `const box = { poison }; const { poison: alias } = box; alias();`,
      `const box = { ...{ poison } }; box.poison();`,
      `function make() { return poison; } make()();`,
      `const bound = poison.bind(null); bound();`,
    ]) {
      expect(auditFrontendSources(new Map([
        selector,
        ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${prefix} ${invoke} function owner(request: Exact, plans) { return pick(plans); }`],
      ])), invoke).toContain('practice:implicit-target-fallback');
    }
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${prefix} const box = { ...{ poison } }; const bound = box.poison.bind(null); void bound; function owner(request: Exact, plans) { return pick(plans); }`],
    ]))).not.toContain('practice:implicit-target-fallback');
  });

  it('resolves default and namespace facades for selectors and generic undo', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/selectors/practice.ts', `export default function choose(plans) { return plans.find((plan) => plan.status === 'in_progress'); }`],
      ['web/src/selectors/facade.ts', `export { default } from './practice';`],
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `type Exact = { readinessSignalVersionId: number; targetEventId: number }; import choose from '@/selectors/facade'; function owner(request: Exact, plans) { return choose(plans); }`],
    ]))).toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      ['web/src/selectors/practice.ts', `export function choose(plans) { return plans.find((plan) => plan.status === 'in_progress'); }`],
      ['web/src/selectors/facade.ts', `export * as selectors from './practice';`],
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `type Exact = { readinessSignalVersionId: number; targetEventId: number }; import * as facade from '@/selectors/facade'; function owner(request: Exact, plans) { return facade.selectors.choose(plans); }`],
    ]))).toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      ['web/src/services/writeOperations.ts', `export default function undoWriteOperation(id) {}`],
      ['web/src/services/operationsFacade.ts', `export { default } from './writeOperations';`],
      ['web/src/features/reviewReadiness/Owner.tsx', `import undo from '@/services/operationsFacade'; undo(id);`],
    ]))).toContain('ui:generic-operation-undo');
    expect(auditFrontendSources(new Map([
      ['web/src/services/writeOperations.ts', `export function undoWriteOperation(id) {}`],
      ['web/src/services/operationsFacade.ts', `export * as operations from './writeOperations';`],
      ['web/src/features/reviewReadiness/Owner.tsx', `import * as facade from '@/services/operationsFacade'; facade.operations.undoWriteOperation(id);`],
    ]))).toContain('ui:generic-operation-undo');
  });

  it('resolves default and namespace facades for transport, Preparation, and sensitive sinks', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/utils/transport.ts', `const send = http.post.bind(http); export default send;`],
      ['web/src/features/reviewReadiness/service.ts', `import send from '@/utils/transport'; send('/api/stories/7');`],
    ]))).toContain('ui:stories-api-alias');
    expect(auditFrontendSources(new Map([
      ['web/src/utils/transport.ts', `export const send = http.post.bind(http);`],
      ['web/src/utils/transportFacade.ts', `export * as transport from './transport';`],
      ['web/src/features/reviewReadiness/service.ts', `import * as facade from '@/utils/transportFacade'; facade.transport.send('/api/stories/7');`],
    ]))).toContain('ui:stories-api-alias');
    expect(auditFrontendSources(new Map([
      ['web/src/utils/defaultIds.ts', `export default function defaultIds(value) { return value ?? []; }`],
      ['web/src/services/interviewPreparationProposals.ts', `import defaultIds from '@/utils/defaultIds'; defaultIds(input.readiness_feedback_version_ids);`],
    ]))).toContain('preparation:missing-defaulted-empty');
    expect(auditFrontendSources(new Map([
      ['web/src/utils/defaultIds.ts', `export function defaultIds(value) { return value ?? []; }`],
      ['web/src/utils/defaultFacade.ts', `export * as defaults from './defaultIds';`],
      ['web/src/services/interviewPreparationProposals.ts', `import * as facade from '@/utils/defaultFacade'; facade.defaults.defaultIds(input.readiness_feedback_version_ids);`],
    ]))).toContain('preparation:missing-defaulted-empty');
    expect(auditFrontendSources(new Map([
      ['web/src/utils/persist.ts', `export default function persist(value) { sessionStorage.setItem('state', value); }`],
      ['web/src/layout/AppShell.tsx', `import persist from '@/utils/persist'; persist(proposal.confirmation_token);`],
    ]))).toContain('privacy:sensitive-client-persistence');
    expect(auditFrontendSources(new Map([
      ['web/src/utils/persist.ts', `export function persist(value) { sessionStorage.setItem('state', value); }`],
      ['web/src/utils/persistFacade.ts', `export * as persistence from './persist';`],
      ['web/src/layout/AppShell.tsx', `import * as facade from '@/utils/persistFacade'; facade.persistence.persist(proposal.confirmation_token);`],
    ]))).toContain('privacy:sensitive-client-persistence');
  });

  it('resolves computed constant members and default service facades across analyzers', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/services/notes.ts', `export default function updateInterviewNote(id, input) {}`],
      ['web/src/features/reviewReadiness/Owner.tsx', `import update from '@/services/notes'; update(id, input);`],
    ]))).toContain('ui:direct-domain-crud');
    const computedCases: Array<[string, string, string]> = [
      ['practice:implicit-target-fallback', `export function choose(plans) { return plans.find((plan) => plan.status === 'in_progress'); }`, `type Exact = { readinessSignalVersionId: number; targetEventId: number }; import * as ns from '@/selectors/practice'; const KEY = 'cho' + 'ose'; function owner(request: Exact, plans) { return ns[KEY](plans); }`],
      ['ui:generic-operation-undo', `export function undoWriteOperation(id) {}`, `import * as ns from '@/services/writeOperations'; const KEY = 'undoWrite' + 'Operation'; ns[KEY](id);`],
    ];
    expect(auditFrontendSources(new Map([
      ['web/src/selectors/practice.ts', computedCases[0][1]],
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', computedCases[0][2]],
    ]))).toContain(computedCases[0][0]);
    expect(auditFrontendSources(new Map([
      ['web/src/services/writeOperations.ts', computedCases[1][1]],
      ['web/src/features/reviewReadiness/Owner.tsx', computedCases[1][2]],
    ]))).toContain(computedCases[1][0]);
    expect(auditFrontendSources(new Map([
      ['web/src/utils/transport.ts', `export const send = http.post.bind(http);`],
      ['web/src/utils/transportFacade.ts', `export * as transport from './transport';`],
      ['web/src/features/reviewReadiness/service.ts', `import * as ns from '@/utils/transportFacade'; const A = 'trans'; const KEY = A + 'port'; const METHOD = 'se' + 'nd'; ns[KEY][METHOD]('/api/stories/7');`],
    ]))).toContain('ui:stories-api-alias');
    expect(auditFrontendSources(new Map([
      ['web/src/utils/defaultIds.ts', `export function defaultIds(value) { return value ?? []; }`],
      ['web/src/utils/defaultFacade.ts', `export * as defaults from './defaultIds';`],
      ['web/src/services/interviewPreparationProposals.ts', `import * as ns from '@/utils/defaultFacade'; const KEY = 'def' + 'aults'; const METHOD = 'default' + 'Ids'; ns[KEY][METHOD](input.readiness_feedback_version_ids);`],
    ]))).toContain('preparation:missing-defaulted-empty');
    expect(auditFrontendSources(new Map([
      ['web/src/utils/persist.ts', `export function persist(value) { sessionStorage.setItem('state', value); }`],
      ['web/src/utils/persistFacade.ts', `export * as persistence from './persist';`],
      ['web/src/layout/AppShell.tsx', `import * as ns from '@/utils/persistFacade'; const KEY = 'persis' + 'tence'; const METHOD = 'per' + 'sist'; ns[KEY][METHOD](proposal.confirmation_token);`],
    ]))).toContain('privacy:sensitive-client-persistence');
  });

  it('projects higher-order calls and abrupt loop/try flow precisely', () => {
    const selector = ['web/src/selectors/practice.ts', `export function choose(plans) { return plans.find((plan) => plan.status === 'in_progress'); }`] as const;
    const exact = `type Exact = { readinessSignalVersionId: number; targetEventId: number }; import { choose } from '@/selectors/practice'; const safe = (plans) => plans;`;
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} let pick = safe; const poison = () => { pick = choose; }; function invoke(fn) { fn(); } invoke(poison); function owner(request: Exact, plans) { return pick(plans); }`],
    ]))).toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} let pick = safe; for (const item of items) { pick = choose; break; pick = safe; } function owner(request: Exact, plans) { return pick(plans); }`],
    ]))).toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} let pick = safe; try { pick = choose; throw error; pick = safe; } catch { pick = safe; } function owner(request: Exact, plans) { return pick(plans); }`],
    ]))).not.toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} let pick = safe; try { pick = choose; throw error; } catch { if (flag) pick = safe; } function owner(request: Exact, plans) { return pick(plans); }`],
    ]))).toContain('practice:implicit-target-fallback');
  });

  it('requires Preparation own-key guards to use the same receiver', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/services/interviewPreparationProposals.ts', `const present = Object.hasOwn(input, 'readiness_feedback_version_ids'); if (present) { const ids = other.readiness_feedback_version_ids ?? []; }`],
    ]))).toContain('preparation:missing-defaulted-empty');
    expect(auditFrontendSources(new Map([
      ['web/src/services/interviewPreparationProposals.ts', `const present = Object.hasOwn(input, 'readiness_feedback_version_ids'); if (present) { const ids = input.readiness_feedback_version_ids ?? []; }`],
    ]))).not.toContain('preparation:missing-defaulted-empty');
  });

  it('joins computed constant privacy keys including confirmation_token', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/layout/AppShell.tsx', `const KEY = 'confirmation_' + 'token'; sessionStorage.setItem('state', proposal[KEY]);`],
    ]))).toContain('privacy:sensitive-client-persistence');
    expect(auditFrontendSources(new Map([
      ['web/src/layout/AppShell.tsx', `let KEY = 'safe'; if (flag) KEY = 'confirmation_token'; sessionStorage.setItem('state', proposal[KEY]);`],
    ]))).toContain('privacy:sensitive-client-persistence');
    expect(auditFrontendSources(new Map([
      ['web/src/layout/AppShell.tsx', `const KEY = 'display_name'; sessionStorage.setItem('state', proposal[KEY]);`],
    ]))).not.toContain('privacy:sensitive-client-persistence');
    expect(auditFrontendSources(new Map([
      ['web/src/layout/AppShell.tsx', `let KEY = 'confirmation_token'; KEY = 'display_name'; sessionStorage.setItem('state', proposal[KEY]);`],
    ]))).not.toContain('privacy:sensitive-client-persistence');
  });

  it('resolves imported Exact types and never exempts an Exact owner by legacy naming alone', () => {
    const selector = ['web/src/selectors/practice.ts', `export function choose(plans) { return plans.find((plan) => plan.status === 'in_progress'); }`] as const;
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/types/practice.ts', `export type ExactFocus = { readinessSignalVersionId: number; targetEventId: number };`],
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `import type { ExactFocus } from '@/types/practice'; import { choose } from '@/selectors/practice'; function legacyOwner(focus: ExactFocus, plans) { return choose(plans); }`],
    ]))).toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/types/practice.ts', `export type LegacyFocus = { proposalId: number };`],
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `import type { LegacyFocus } from '@/types/practice'; import { choose } from '@/selectors/practice'; function legacyOwner(focus: LegacyFocus, plans) { return choose(plans); }`],
    ]))).not.toContain('practice:implicit-target-fallback');
  });

  it('requires constant task identity and defined exact owner ids', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/features/haru/Haru.tsx', `const TASK = 'application.interview_' + 'review'; launchCoreTask({ taskId: TASK, ref: { applicationId, eventId: undefined } });`],
    ]))).toContain('pilot:inexact-owner-open');
    expect(auditFrontendSources(new Map([
      ['web/src/features/haru/Haru.tsx', `const TASK = 'application.interview_' + 'review'; launchCoreTask({ taskId: TASK, ref: { applicationId, eventId } });`],
    ]))).not.toContain('pilot:inexact-owner-open');
  });

  it('rejects Chat and Haru readiness ownership including direct HTTP routes', () => {
    for (const [path, source] of [
      ['web/src/components/Chat.tsx', `http.get('/applications/1/events/2/readiness-feedback');`],
      ['web/src/features/haru/Haru.tsx', `http.post('/product-actions/abc/decisions', input);`],
    ] as const) {
      expect(auditFrontendSources(new Map([[path, source]])), path).toContain('pilot:review-readiness-ownership');
    }
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/Owner.tsx', `http.get('/applications/1/events/2/readiness-feedback');`],
    ]))).not.toContain('pilot:review-readiness-ownership');
  });

  it('ignores privacy and undo calls after an unconditional return', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/layout/AppShell.tsx', `function owner() { return; sessionStorage.setItem('state', proposal.confirmation_token); }`],
    ]))).not.toContain('privacy:sensitive-client-persistence');
    expect(auditFrontendSources(new Map([
      ['web/src/services/writeOperations.ts', `export function undoWriteOperation(id) {}`],
      ['web/src/features/reviewReadiness/Owner.tsx', `import { undoWriteOperation } from '@/services/writeOperations'; function owner() { return; undoWriteOperation(id); }`],
    ]))).not.toContain('ui:generic-operation-undo');
  });

  it('ignores statically unreachable numeric branches and false for loops only', () => {
    const selector = ['web/src/selectors/practice.ts', `export function choose(plans) { return plans.find((plan) => plan.status === 'in_progress'); }`] as const;
    const prefix = `type Exact = { readinessSignalVersionId: number; targetEventId: number }; import { choose } from '@/selectors/practice'; const safe = (plans) => plans;`;
    for (const dead of [`if (0) { pick = choose; }`, `for (; false;) { pick = choose; }`]) {
      expect(auditFrontendSources(new Map([
        selector,
        ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${prefix} let pick = safe; ${dead} function owner(request: Exact, plans) { return pick(plans); }`],
      ])), dead).not.toContain('practice:implicit-target-fallback');
    }
    for (const live of [`if (1) { pick = choose; }`, `for (; flag;) { pick = choose; }`]) {
      expect(auditFrontendSources(new Map([
        selector,
        ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${prefix} let pick = safe; ${live} function owner(request: Exact, plans) { return pick(plans); }`],
      ])), live).toContain('practice:implicit-target-fallback');
    }
    expect(auditFrontendSources(new Map([
      ['web/src/services/writeOperations.ts', `export function undoWriteOperation(id) {}`],
      ['web/src/features/reviewReadiness/Owner.tsx', `import { undoWriteOperation } from '@/services/writeOperations'; const safe = () => undefined; let undo = safe; for (; false;) undo = undoWriteOperation; undo(id);`],
    ]))).not.toContain('ui:generic-operation-undo');
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/service.ts', `const safe = (path) => path; let send = safe; if (0) send = http.post; send('/api/stories/7');`],
    ]))).not.toContain('ui:stories-api-alias');
    expect(auditFrontendSources(new Map([
      ['web/src/services/interviewPreparationProposals.ts', `const safe = (value) => value; const defaults = (value) => value ?? []; let d = safe; for (; false;) d = defaults; d(input.readiness_feedback_version_ids);`],
    ]))).not.toContain('preparation:missing-defaulted-empty');
    expect(auditFrontendSources(new Map([
      ['web/src/layout/AppShell.tsx', `const safe = () => undefined; let persist = safe; if (0) persist = sessionStorage.setItem.bind(sessionStorage); persist('state', proposal.confirmation_token);`],
    ]))).not.toContain('privacy:sensitive-client-persistence');
  });

  it('models switch fallthrough to distinguish all-safe from may-target paths', () => {
    const selector = ['web/src/selectors/practice.ts', `export function choose(plans) { return plans.find((plan) => plan.status === 'in_progress'); }`] as const;
    const prefix = `type Exact = { readinessSignalVersionId: number; targetEventId: number }; import { choose } from '@/selectors/practice'; const safe = (plans) => plans;`;
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${prefix} let pick = choose; switch (mode) { case 'a': pick = choose; case 'b': pick = safe; break; default: pick = safe; } function owner(request: Exact, plans) { return pick(plans); }`],
    ]))).not.toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${prefix} let pick = safe; switch (mode) { case 'a': pick = choose; case 'b': if (flag) break; pick = safe; break; default: pick = safe; } function owner(request: Exact, plans) { return pick(plans); }`],
    ]))).toContain('practice:implicit-target-fallback');
    const switchAllSafe = `switch (mode) { case 'a': callable = target; case 'b': callable = safe; break; default: callable = safe; }`;
    const switchMayTarget = `switch (mode) { case 'a': callable = target; case 'b': if (flag) break; callable = safe; break; default: callable = safe; }`;
    expect(auditFrontendSources(new Map([
      ['web/src/services/writeOperations.ts', `export function undoWriteOperation(id) {}`],
      ['web/src/features/reviewReadiness/Owner.tsx', `import { undoWriteOperation as target } from '@/services/writeOperations'; const safe = () => undefined; let callable = target; ${switchAllSafe} callable(id);`],
    ]))).not.toContain('ui:generic-operation-undo');
    expect(auditFrontendSources(new Map([
      ['web/src/services/writeOperations.ts', `export function undoWriteOperation(id) {}`],
      ['web/src/features/reviewReadiness/Owner.tsx', `import { undoWriteOperation as target } from '@/services/writeOperations'; const safe = () => undefined; let callable = safe; ${switchMayTarget} callable(id);`],
    ]))).toContain('ui:generic-operation-undo');
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/service.ts', `const target = http.post; const safe = (path) => path; let callable = target; ${switchAllSafe} callable('/api/stories/7');`],
    ]))).not.toContain('ui:stories-api-alias');
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/service.ts', `const target = http.post; const safe = (path) => path; let callable = safe; ${switchMayTarget} callable('/api/stories/7');`],
    ]))).toContain('ui:stories-api-alias');
    expect(auditFrontendSources(new Map([
      ['web/src/services/interviewPreparationProposals.ts', `const target = (value) => value ?? []; const safe = (value) => value; let callable = target; ${switchAllSafe} callable(input.readiness_feedback_version_ids);`],
    ]))).not.toContain('preparation:missing-defaulted-empty');
    expect(auditFrontendSources(new Map([
      ['web/src/services/interviewPreparationProposals.ts', `const target = (value) => value ?? []; const safe = (value) => value; let callable = safe; ${switchMayTarget} callable(input.readiness_feedback_version_ids);`],
    ]))).toContain('preparation:missing-defaulted-empty');
    expect(auditFrontendSources(new Map([
      ['web/src/layout/AppShell.tsx', `const target = sessionStorage.setItem.bind(sessionStorage); const safe = () => undefined; let callable = target; ${switchAllSafe} callable('state', proposal.confirmation_token);`],
    ]))).not.toContain('privacy:sensitive-client-persistence');
    expect(auditFrontendSources(new Map([
      ['web/src/layout/AppShell.tsx', `const target = sessionStorage.setItem.bind(sessionStorage); const safe = () => undefined; let callable = safe; ${switchMayTarget} callable('state', proposal.confirmation_token);`],
    ]))).toContain('privacy:sensitive-client-persistence');
  });

  it('models conditional loop exits, potential try throws, call, nullish assignment, and Object.assign', () => {
    const selector = ['web/src/selectors/practice.ts', `export function choose(plans) { return plans.find((plan) => plan.status === 'in_progress'); }`] as const;
    const exact = `type Exact = { readinessSignalVersionId: number; targetEventId: number }; import { choose } from '@/selectors/practice'; const safe = (plans) => plans;`;
    for (const body of [
      `let pick = safe; for (const item of items) { pick = choose; if (flag) break; pick = safe; }`,
      `let pick = safe; try { pick = choose; maybeThrow(); pick = safe; } catch {}`,
      `let pick = safe; const poison = () => { pick = choose; }; function invoke(fn) { fn.call(null); } invoke(poison);`,
      `let pick; pick ??= choose;`,
      `const api = {}; api.pick ??= choose;`,
      `const api = { pick: safe }; Object.assign(api, { pick: choose });`,
      `const api = { pick: safe }; for (const item of items) { api.pick = choose; if (flag) break; api.pick = safe; }`,
      `const api = { pick: safe }; try { api.pick = choose; maybeThrow(); api.pick = safe; } catch {}`,
    ]) {
      const call = body.includes('api') ? 'api.pick(plans)' : 'pick(plans)';
      expect(auditFrontendSources(new Map([
        selector,
        ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} ${body} function owner(request: Exact, plans) { return ${call}; }`],
      ])), body).toContain('practice:implicit-target-fallback');
    }
    for (const body of [
      `let pick = safe; pick ??= choose;`,
      `const api = { pick: safe }; Object.assign(api, { pick: choose }); api.pick = safe;`,
      `const api = { pick: safe }; api.pick ??= choose;`,
    ]) {
      const call = body.includes('api') ? 'api.pick(plans)' : 'pick(plans)';
      expect(auditFrontendSources(new Map([
        selector,
        ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} ${body} function owner(request: Exact, plans) { return ${call}; }`],
      ])), body).not.toContain('practice:implicit-target-fallback');
    }
  });

  it('recognizes CRUD writers by HTTP behavior and Chat readiness routes by constant dataflow', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/utils/writer.ts', `import http from '@/utils/http'; export default function write(id, input) { return http.patch('/interview-notes/' + id, input); }`],
      ['web/src/features/reviewReadiness/Owner.tsx', `import write from '@/utils/writer'; write(id, input);`],
    ]))).toContain('ui:direct-domain-crud');
    expect(auditFrontendSources(new Map([
      ['web/src/utils/writer.ts', `import http from '@/utils/http'; export default function write(id, input) { return http.patch('/interview-notes/' + id, input); }`],
      ['web/src/utils/writerFacade.ts', `import write from './writer'; const api = { write }; export default api;`],
      ['web/src/features/reviewReadiness/Owner.tsx', `import api from '@/utils/writerFacade'; api.write(id, input);`],
    ]))).toContain('ui:direct-domain-crud');
    expect(auditFrontendSources(new Map([
      ['web/src/utils/writer.ts', `import http from '@/utils/http'; export default function write(id, input) { return http.patch('/preferences/' + id, input); }`],
      ['web/src/features/reviewReadiness/Owner.tsx', `import write from '@/utils/writer'; write(id, input);`],
    ]))).not.toContain('ui:direct-domain-crud');
    expect(auditFrontendSources(new Map([
      ['web/src/components/Chat.tsx', `const BASE = '/applications/1'; const ROUTE = BASE + '/events/2/readiness-feedback'; http.get(ROUTE);`],
    ]))).toContain('pilot:review-readiness-ownership');
    expect(auditFrontendSources(new Map([
      ['web/src/components/Chat.tsx', `const BASE = '/applications/1'; const ROUTE = BASE + '/events/2'; http.get(ROUTE);`],
    ]))).not.toContain('pilot:review-readiness-ownership');
  });

  it('resolves joined privacy keys, Array defaults, aliases, and finally reachability', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/layout/AppShell.tsx', `const KEY = ['confirmation', 'token'].join('_'); sessionStorage.setItem('state', proposal[KEY]);`],
    ]))).toContain('privacy:sensitive-client-persistence');
    expect(auditFrontendSources(new Map([
      ['web/src/layout/AppShell.tsx', `const KEY = ['display', 'name'].join('_'); sessionStorage.setItem('state', proposal[KEY]);`],
    ]))).not.toContain('privacy:sensitive-client-persistence');
    expect(auditFrontendSources(new Map([
      ['web/src/services/interviewPreparationProposals.ts', `const ids = input.readiness_feedback_version_ids ?? Array();`],
    ]))).toContain('preparation:missing-defaulted-empty');
    expect(auditFrontendSources(new Map([
      ['web/src/services/interviewPreparationProposals.ts', `const ids = input.readiness_feedback_version_ids ?? Array(1);`],
    ]))).not.toContain('preparation:missing-defaulted-empty');
    expect(auditFrontendSources(new Map([
      ['web/src/services/interviewPreparationProposals.ts', `const alias = input; const present = Object.hasOwn(input, 'readiness_feedback_version_ids'); if (present) { const ids = alias.readiness_feedback_version_ids ?? []; }`],
    ]))).not.toContain('preparation:missing-defaulted-empty');
    expect(auditFrontendSources(new Map([
      ['web/src/layout/AppShell.tsx', `function owner() { try {} finally { return; } sessionStorage.setItem('state', proposal.confirmation_token); }`],
    ]))).not.toContain('privacy:sensitive-client-persistence');
    expect(auditFrontendSources(new Map([
      ['web/src/services/writeOperations.ts', `export function undoWriteOperation(id) {}`],
      ['web/src/features/reviewReadiness/Owner.tsx', `import { undoWriteOperation } from '@/services/writeOperations'; function owner() { try {} finally { return; } undoWriteOperation(id); }`],
    ]))).not.toContain('ui:generic-operation-undo');
  });

  it('resolves default imported Exact types without legacy name exemptions', () => {
    const selector = ['web/src/selectors/practice.ts', `export function choose(plans) { return plans.find((plan) => plan.status === 'in_progress'); }`] as const;
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/types/practice.ts', `export default interface ExactFocus { readinessSignalVersionId: number; targetEventId: number }`],
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `import type ExactFocus from '@/types/practice'; import { choose } from '@/selectors/practice'; function legacyOwner(focus: ExactFocus, plans) { return choose(plans); }`],
    ]))).toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/types/practice.ts', `export default interface LegacyFocus { proposalId: number }`],
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `import type LegacyFocus from '@/types/practice'; import { choose } from '@/selectors/practice'; function legacyOwner(focus: LegacyFocus, plans) { return choose(plans); }`],
    ]))).not.toContain('practice:implicit-target-fallback');
  });

  it('keeps CoreTask and navigation closed under template and identifier spreads', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/features/coreTaskSurface/contracts.ts', `type CoreTaskId = 'application.opportunity_fit' | 'application.material_kit' | 'application.interview_prepare' | 'application.interview_review' | 'application.general_review' | 'application.offer_review' | 'application.record_outcome' | 'interview.free_practice' | 'materials.resume' | 'materials.story' | 'materials.reference' | \`application.${'new_task'}\`;`],
    ]))).toContain('core-task:id-union-expanded');
    expect(auditFrontendSources(new Map([
      ['web/src/features/coreTaskSurface/contracts.ts', `const BASE = ['application.opportunity_fit', 'application.material_kit', 'application.interview_prepare', 'application.interview_review', 'application.general_review', 'application.offer_review', 'application.record_outcome', 'interview.free_practice', 'materials.resume', 'materials.story', 'materials.reference']; const EXTRA = ['review.readiness']; export const CORE_TASK_IDS = [...BASE, ...EXTRA];`],
    ]))).toContain('core-task:id-union-expanded');
    expect(auditFrontendSources(new Map([
      ['web/src/features/coreTaskSurface/contracts.ts', `const BASE = ['application.opportunity_fit', 'application.material_kit', 'application.interview_prepare', 'application.interview_review', 'application.general_review', 'application.offer_review', 'application.record_outcome', 'interview.free_practice', 'materials.resume', 'materials.story', 'materials.reference']; export const CORE_TASK_IDS = [...BASE];`],
    ]))).not.toContain('core-task:id-union-expanded');
    expect(auditFrontendSources(new Map([
      ['web/src/layout/navigation.ts', `const BASE = [{ key: 'today' }, { key: 'applications' }, { key: 'interview' }, { key: 'offers' }, { key: 'resources' }, { key: 'settings' }]; const EXTRA = [{ key: 'readiness' }]; export const MODULE_NAV = [...BASE, ...EXTRA];`],
    ]))).toContain('navigation:top-level-expanded');
    expect(auditFrontendSources(new Map([
      ['web/src/layout/navigation.ts', `const BASE = [{ key: 'today' }, { key: 'applications' }, { key: 'interview' }, { key: 'offers' }, { key: 'resources' }, { key: 'settings' }]; export const MODULE_NAV = [...BASE];`],
    ]))).not.toContain('navigation:top-level-expanded');
  });

  it('uses exact-ref object spread and last-write semantics', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/features/haru/Haru.tsx', `const base = { applicationId, eventId }; const ref = { ...base, eventId: undefined }; launchCoreTask({ taskId: 'application.interview_review', ref });`],
    ]))).toContain('pilot:inexact-owner-open');
    expect(auditFrontendSources(new Map([
      ['web/src/features/haru/Haru.tsx', `const base = { applicationId, eventId }; const ref = { eventId: undefined, ...base }; launchCoreTask({ taskId: 'application.interview_review', ref });`],
    ]))).not.toContain('pilot:inexact-owner-open');
  });

  it('unwraps call, apply, and Reflect.apply across transports, undo, and sensitive sinks', () => {
    for (const source of [
      `Reflect.apply(http.post, http, ['/api/stories/7', input]);`,
      `http.post.call(http, '/api/stories/7', input);`,
    ]) expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/service.ts', source],
    ])), source).toContain('ui:stories-api-alias');
    expect(auditFrontendSources(new Map([
      ['web/src/services/writeOperations.ts', `export function undoWriteOperation(id) {}`],
      ['web/src/features/reviewReadiness/Owner.tsx', `import { undoWriteOperation } from '@/services/writeOperations'; undoWriteOperation.apply(null, [id]);`],
    ]))).toContain('ui:generic-operation-undo');
    for (const source of [
      `sessionStorage.setItem.call(sessionStorage, 'state', proposal.confirmation_token);`,
      `sessionStorage.setItem.apply(sessionStorage, ['state', proposal.confirmation_token]);`,
    ]) expect(auditFrontendSources(new Map([
      ['web/src/layout/AppShell.tsx', source],
    ])), source).toContain('privacy:sensitive-client-persistence');
  });

  it('models destructured higher-order calls, apply, mutation builtins, ||=, and finally joins', () => {
    const selector = ['web/src/selectors/practice.ts', `export function choose(plans) { return plans.find((plan) => plan.status === 'in_progress'); }`] as const;
    const exact = `type Exact = { readinessSignalVersionId: number; targetEventId: number }; import { choose } from '@/selectors/practice'; const safe = (plans) => plans;`;
    for (const body of [
      `let pick = safe; const poison = () => { pick = choose; }; function invoke({ fn }) { fn(); } invoke({ fn: poison });`,
      `let pick = safe; const poison = () => { pick = choose; }; poison.apply(null, []);`,
      `const api = { pick: safe }; Reflect.set(api, 'pick', choose);`,
      `const api = { pick: safe }; Object.defineProperty(api, 'pick', { value: choose });`,
      `let pick; pick ||= choose;`,
      `let pick = safe; try { pick = choose; } finally { if (flag) pick = safe; }`,
    ]) {
      const call = body.includes('api') ? 'api.pick(plans)' : 'pick(plans)';
      expect(auditFrontendSources(new Map([
        selector,
        ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} ${body} function owner(request: Exact, plans) { return ${call}; }`],
      ])), body).toContain('practice:implicit-target-fallback');
    }
    for (const body of [
      `let pick = safe; pick ||= choose;`,
      `let pick = safe; try { pick = choose; } finally { pick = safe; }`,
    ]) expect(auditFrontendSources(new Map([
      selector,
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} ${body} function owner(request: Exact, plans) { return pick(plans); }`],
    ])), body).not.toContain('practice:implicit-target-fallback');
  });

  it('resolves object route members and String.concat sensitive keys', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/components/Chat.tsx', `const ROUTES = { readiness: '/applications/1/events/2/readiness-feedback' }; http.get(ROUTES.readiness);`],
    ]))).toContain('pilot:review-readiness-ownership');
    expect(auditFrontendSources(new Map([
      ['web/src/components/Chat.tsx', `const ROUTES = { detail: '/applications/1/events/2' }; http.get(ROUTES.detail);`],
    ]))).not.toContain('pilot:review-readiness-ownership');
    expect(auditFrontendSources(new Map([
      ['web/src/layout/AppShell.tsx', `const KEY = 'confirmation'.concat('_', 'token'); sessionStorage.setItem('state', proposal[KEY]);`],
    ]))).toContain('privacy:sensitive-client-persistence');
  });

  it('tracks semantic empty Preparation values through wrappers and casts', () => {
    for (const fallback of [
      `EMPTY.slice()`,
      `[...EMPTY]`,
      `Object.freeze([])`,
      `([] as string[])`,
    ]) expect(auditFrontendSources(new Map([
      ['web/src/services/interviewPreparationProposals.ts', `const EMPTY = []; const ids = input.readiness_feedback_version_ids ?? ${fallback};`],
    ])), fallback).toContain('preparation:missing-defaulted-empty');
    expect(auditFrontendSources(new Map([
      ['web/src/services/interviewPreparationProposals.ts', `const NONEMPTY = ['v1']; const ids = input.readiness_feedback_version_ids ?? NONEMPTY.slice();`],
    ]))).not.toContain('preparation:missing-defaulted-empty');
  });

  it('resolves Exact type ownership through namespace imports', () => {
    const selector = ['web/src/selectors/practice.ts', `export function choose(plans) { return plans.find((plan) => plan.status === 'in_progress'); }`] as const;
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/types/practice.ts', `export type ExactFocus = { readinessSignalVersionId: number; targetEventId: number };`],
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `import type * as Types from '@/types/practice'; import { choose } from '@/selectors/practice'; function owner(focus: Types.ExactFocus, plans) { return choose(plans); }`],
    ]))).toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      selector,
      ['web/src/types/practice.ts', `export type LegacyFocus = { proposalId: number };`],
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `import type * as Types from '@/types/practice'; import { choose } from '@/selectors/practice'; function owner(focus: Types.LegacyFocus, plans) { return choose(plans); }`],
    ]))).not.toContain('practice:implicit-target-fallback');
  });

  it('canonicalizes Preparation receiver aliases assigned after declaration', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/services/interviewPreparationProposals.ts', `let alias; alias = input; const present = Object.hasOwn(input, 'readiness_feedback_version_ids'); if (present) { const ids = alias.readiness_feedback_version_ids ?? []; }`],
    ]))).not.toContain('preparation:missing-defaulted-empty');
    expect(auditFrontendSources(new Map([
      ['web/src/services/interviewPreparationProposals.ts', `let alias; alias = other; const present = Object.hasOwn(input, 'readiness_feedback_version_ids'); if (present) { const ids = alias.readiness_feedback_version_ids ?? []; }`],
    ]))).toContain('preparation:missing-defaulted-empty');
  });

  it('unwraps computed and identifier-array adapters for transport, undo, and storage', () => {
    for (const source of [
      `const args = ['/api/stories/7', input]; http.post['apply'](http, args);`,
      `Reflect['apply'](http.post, http, ['/api/stories/7', input]);`,
    ]) expect(auditFrontendSources(new Map([['web/src/features/reviewReadiness/service.ts', source]])), source).toContain('ui:stories-api-alias');
    expect(auditFrontendSources(new Map([
      ['web/src/services/writeOperations.ts', `export function undoWriteOperation(id) {}`],
      ['web/src/features/reviewReadiness/Owner.tsx', `import { undoWriteOperation } from '@/services/writeOperations'; const args = [id]; undoWriteOperation['apply'](null, args);`],
    ]))).toContain('ui:generic-operation-undo');
    expect(auditFrontendSources(new Map([
      ['web/src/layout/AppShell.tsx', `const args = ['state', proposal.confirmation_token]; sessionStorage.setItem['apply'](sessionStorage, args);`],
    ]))).toContain('privacy:sensitive-client-persistence');
  });

  it('models tuple higher-order calls, &&=, identifier patches, and aliased Reflect.set', () => {
    const selector = ['web/src/selectors/practice.ts', `export function choose(plans) { return plans.find((plan) => plan.status === 'in_progress'); }`] as const;
    const exact = `type Exact = { readinessSignalVersionId: number; targetEventId: number }; import { choose } from '@/selectors/practice'; const safe = (plans) => plans;`;
    for (const body of [
      `let pick = safe; const poison = () => { pick = choose; }; function invoke([fn]) { fn(); } invoke([poison]);`,
      `let pick = safe; pick &&= choose;`,
      `const api = { pick: safe }; const patch = { pick: choose }; Object.assign(api, patch);`,
      `const api = { pick: safe }; const set = Reflect.set; set(api, 'pick', choose);`,
    ]) {
      const call = body.includes('api') ? 'api.pick(plans)' : 'pick(plans)';
      expect(auditFrontendSources(new Map([selector, ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} ${body} function owner(request: Exact, plans) { return ${call}; }`]])), body).toContain('practice:implicit-target-fallback');
    }
    expect(auditFrontendSources(new Map([selector, ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} let pick; pick &&= choose; function owner(request: Exact, plans) { return pick?.(plans); }`]]))).not.toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([selector, ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} let pick = choose; pick &&= safe; function owner(request: Exact, plans) { return pick(plans); }`]]))).not.toContain('practice:implicit-target-fallback');
  });

  it('propagates Preparation empties across modules, concat, and negative guard successors', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/utils/emptyIds.ts', `export const EMPTY_IDS = [];`],
      ['web/src/services/interviewPreparationProposals.ts', `import { EMPTY_IDS } from '@/utils/emptyIds'; const ids = input.readiness_feedback_version_ids ?? EMPTY_IDS.concat();`],
    ]))).toContain('preparation:missing-defaulted-empty');
    expect(auditFrontendSources(new Map([
      ['web/src/services/interviewPreparationProposals.ts', `const KEY = 'readiness_feedback_version_ids'; if (!Object.hasOwn(input, KEY)) return; const ids = input[KEY] ?? [];`],
    ]))).not.toContain('preparation:missing-defaulted-empty');
    expect(auditFrontendSources(new Map([
      ['web/src/services/interviewPreparationProposals.ts', `const KEY = 'readiness_feedback_version_ids'; if (!Object.hasOwn(other, KEY)) return; const ids = input[KEY] ?? [];`],
    ]))).toContain('preparation:missing-defaulted-empty');
  });

  it('resolves Exact interface heritage locally and across modules', () => {
    const selector = ['web/src/selectors/practice.ts', `export function choose(plans) { return plans.find((plan) => plan.status === 'in_progress'); }`] as const;
    for (const types of [
      [`export interface BaseExact { readinessSignalVersionId: number; targetEventId: number } export interface ExactFocus extends BaseExact { label: string }`, `import type { ExactFocus } from '@/types/practice';`],
      [`export interface BaseExact { readinessSignalVersionId: number; targetEventId: number }`, `import type { BaseExact } from '@/types/base'; interface ExactFocus extends BaseExact { label: string }`],
    ] as const) expect(auditFrontendSources(new Map([
      selector,
      [types[1].includes("@/types/base") ? 'web/src/types/base.ts' : 'web/src/types/practice.ts', types[0]],
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${types[1]} import { choose } from '@/selectors/practice'; function owner(focus: ExactFocus, plans) { return choose(plans); }`],
    ]))).toContain('practice:implicit-target-fallback');
  });

  it('rejects filtered, duplicated, and reordered closed-set declarations', () => {
    const ids = `'application.opportunity_fit', 'application.material_kit', 'application.interview_prepare', 'application.interview_review', 'application.general_review', 'application.offer_review', 'application.record_outcome', 'interview.free_practice', 'materials.resume', 'materials.story', 'materials.reference'`;
    for (const expression of [`[${ids}].filter((id) => id !== 'materials.story')`, `[${ids}, 'materials.reference']`, `['materials.reference', ${ids}]`]) {
      expect(auditFrontendSources(new Map([['web/src/features/coreTaskSurface/contracts.ts', `export const CORE_TASK_IDS = ${expression};`]])), expression).toContain('core-task:id-union-expanded');
    }
    const nav = `{ key: 'today' }, { key: 'applications' }, { key: 'interview' }, { key: 'offers' }, { key: 'resources' }, { key: 'settings' }`;
    for (const expression of [`[${nav}].filter((item) => item.key !== 'settings')`, `[${nav}, { key: 'settings' }]`, `[{ key: 'settings' }, ${nav}]`]) {
      expect(auditFrontendSources(new Map([['web/src/layout/navigation.ts', `export const MODULE_NAV = ${expression};`]])), expression).toContain('navigation:top-level-expanded');
    }
  });

  it('normalizes constant adapters, as-const arrays, and spread transports', () => {
    for (const source of [
      `const METHOD = 'apply'; const args = ['/api/stories/7', input] as const; http.post[METHOD](http, args);`,
      `const args = ['/api/stories/7', input] as const; http.post(...args);`,
    ]) expect(auditFrontendSources(new Map([['web/src/features/reviewReadiness/service.ts', source]])), source).toContain('ui:stories-api-alias');
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/service.ts', `const args = ['/preferences/7', input] as const; http.post(...args);`],
    ]))).not.toContain('ui:stories-api-alias');
  });

  it('models recursive tuple parameters and defineProperty aliases', () => {
    const selector = ['web/src/selectors/practice.ts', `export function choose(plans) { return plans.find((plan) => plan.status === 'in_progress'); }`] as const;
    const exact = `type Exact = { readinessSignalVersionId: number; targetEventId: number }; import { choose } from '@/selectors/practice'; const safe = (plans) => plans;`;
    for (const body of [
      `let pick = safe; const poison = () => { pick = choose; }; function invoke([[fn]]) { fn(); } invoke([[poison]]);`,
      `const api = { pick: safe }; const define = Object.defineProperty; define(api, 'pick', { value: choose });`,
      `const api = { pick: safe }; Reflect.defineProperty(api, 'pick', { value: choose });`,
    ]) {
      const call = body.includes('api') ? 'api.pick(plans)' : 'pick(plans)';
      expect(auditFrontendSources(new Map([selector, ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} ${body} function owner(request: Exact, plans) { return ${call}; }`]])), body).toContain('practice:implicit-target-fallback');
    }
  });

  it('propagates default-exported Preparation empties through facade re-exports', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/utils/emptyIds.ts', `const EMPTY_IDS = [] as const; export default EMPTY_IDS;`],
      ['web/src/utils/emptyFacade.ts', `export { default as EMPTY_IDS } from './emptyIds';`],
      ['web/src/services/interviewPreparationProposals.ts', `import { EMPTY_IDS } from '@/utils/emptyFacade'; const ids = input.readiness_feedback_version_ids ?? EMPTY_IDS;`],
    ]))).toContain('preparation:missing-defaulted-empty');
    expect(auditFrontendSources(new Map([
      ['web/src/utils/emptyIds.ts', `const IDS = ['v1']; export default IDS;`],
      ['web/src/utils/emptyFacade.ts', `export { default as EMPTY_IDS } from './emptyIds';`],
      ['web/src/services/interviewPreparationProposals.ts', `import { EMPTY_IDS } from '@/utils/emptyFacade'; const ids = input.readiness_feedback_version_ids ?? EMPTY_IDS;`],
    ]))).not.toContain('preparation:missing-defaulted-empty');
  });

  it('audits exported closed-set aliases and post-declaration mutations', () => {
    const ids = `['application.opportunity_fit', 'application.material_kit', 'application.interview_prepare', 'application.interview_review', 'application.general_review', 'application.offer_review', 'application.record_outcome', 'interview.free_practice', 'materials.resume', 'materials.story', 'materials.reference']`;
    expect(auditFrontendSources(new Map([['web/src/features/coreTaskSurface/contracts.ts', `const IDS = ${ids}; export { IDS as CORE_TASK_IDS };`]]))).not.toContain('core-task:id-union-expanded');
    for (const mutation of [`IDS.push('materials.reference')`, `IDS.reverse()`]) expect(auditFrontendSources(new Map([['web/src/features/coreTaskSurface/contracts.ts', `const IDS = ${ids}; export { IDS as CORE_TASK_IDS }; ${mutation};`]])), mutation).toContain('core-task:id-union-expanded');
    const nav = `[{ key: 'today' }, { key: 'applications' }, { key: 'interview' }, { key: 'offers' }, { key: 'resources' }, { key: 'settings' }]`;
    expect(auditFrontendSources(new Map([['web/src/layout/navigation.ts', `const NAV = ${nav}; export { NAV as MODULE_NAV };`]]))).not.toContain('navigation:top-level-expanded');
    for (const mutation of [`NAV.push({ key: 'settings' })`, `NAV.reverse()`]) expect(auditFrontendSources(new Map([['web/src/layout/navigation.ts', `const NAV = ${nav}; export { NAV as MODULE_NAV }; ${mutation};`]])), mutation).toContain('navigation:top-level-expanded');
  });

  it('rejects sensitive Web Storage property and computed assignments', () => {
    for (const source of [
      `sessionStorage.state = proposal.confirmation_token;`,
      `const KEY = 'state'; localStorage[KEY] = proposal.confirmation_token;`,
    ]) expect(auditFrontendSources(new Map([['web/src/layout/AppShell.tsx', source]])), source).toContain('privacy:sensitive-client-persistence');
    expect(auditFrontendSources(new Map([
      ['web/src/layout/AppShell.tsx', `sessionStorage.theme = proposal.display_name;`],
    ]))).not.toContain('privacy:sensitive-client-persistence');
  });

  it('extracts http.request config routes and EventSource readiness ownership', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/Owner.tsx', `http.request({ url: '/interview-notes/' + id, method: 'PATCH', data: input });`],
    ]))).toContain('ui:direct-domain-crud');
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/Owner.tsx', `http.request({ url: '/preferences/' + id, method: 'PATCH', data: input });`],
    ]))).not.toContain('ui:direct-domain-crud');
    expect(auditFrontendSources(new Map([
      ['web/src/components/Chat.tsx', `new EventSource('/applications/1/events/2/readiness-feedback');`],
    ]))).toContain('pilot:review-readiness-ownership');
    expect(auditFrontendSources(new Map([
      ['web/src/components/Chat.tsx', `new EventSource('/applications/1/events/2');`],
    ]))).not.toContain('pilot:review-readiness-ownership');
  });

  it('resolves identifier transport routes and request configs', () => {
    for (const source of [
      `const route = '/interview-notes/' + id; fetch(route, { method: 'PATCH' });`,
      `const route = '/interview-notes/' + id; const config = { url: route, method: 'PATCH', data: input }; http.request(config);`,
    ]) expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/Owner.tsx', source],
    ])), source).toContain('ui:direct-domain-crud');
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/Owner.tsx', `const route = '/preferences/' + id; const config = { url: route, method: 'PATCH', data: input }; http.request(config);`],
    ]))).not.toContain('ui:direct-domain-crud');
  });

  it('tracks request config property writes and imported EventSource routes', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/Owner.tsx', `const config = {}; config.url = '/interview-notes/' + id; config.method = 'PATCH'; http.request(config);`],
    ]))).toContain('ui:direct-domain-crud');
    expect(auditFrontendSources(new Map([
      ['web/src/routes/readiness.ts', `export const STREAM_ROUTE = '/applications/1/events/2/readiness-feedback';`],
      ['web/src/components/Chat.tsx', `import { STREAM_ROUTE } from '@/routes/readiness'; new EventSource(STREAM_ROUTE);`],
    ]))).toContain('pilot:review-readiness-ownership');
    expect(auditFrontendSources(new Map([
      ['web/src/routes/readiness.ts', `export const STREAM_ROUTE = '/applications/1/events/2';`],
      ['web/src/components/Chat.tsx', `import { STREAM_ROUTE } from '@/routes/readiness'; new EventSource(STREAM_ROUTE);`],
    ]))).not.toContain('pilot:review-readiness-ownership');
  });

  it('tracks Object.assign and defineProperty transport config mutations', () => {
    for (const source of [
      `const config = {}; Object.assign(config, { url: '/interview-notes/' + id, method: 'PATCH' }); http.request(config);`,
      `const config = {}; Object.defineProperty(config, 'url', { value: '/interview-notes/' + id }); http.request(config);`,
    ]) expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/Owner.tsx', source],
    ])), source).toContain('ui:direct-domain-crud');
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/Owner.tsx', `const config = {}; Object.assign(config, { url: '/preferences/' + id, method: 'PATCH' }); http.request(config);`],
    ]))).not.toContain('ui:direct-domain-crud');
  });

  it('shares computed and aliased mutation builtin provenance for transport', () => {
    for (const source of [
      `const config = {}; const assign = Object['assign']; assign(config, { url: '/interview-notes/' + id }); http.request(config);`,
      `const METHOD = 'defineProperty'; const config = {}; Object[METHOD](config, 'url', { value: '/interview-notes/' + id }); http.request(config);`,
    ]) expect(auditFrontendSources(new Map([['web/src/features/reviewReadiness/Owner.tsx', source]])), source).toContain('ui:direct-domain-crud');
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/Owner.tsx', `const local = {}; const assign = Object.assign; assign(local, { url: '/interview-notes/' + id }); http.get('/preferences');`],
    ]))).not.toContain('ui:direct-domain-crud');
  });

  it('resolves namespace, destructured, and Reflect mutation builtins for transport', () => {
    for (const source of [
      `const config = {}; const O = Object; const { assign: merge } = O; merge(config, { url: '/interview-notes/' + id }); http.request(config);`,
      `const config = {}; const R = Reflect; const METHOD = 'set'; const write = R[METHOD]; write(config, 'url', '/interview-notes/' + id); http.request(config);`,
      `const config = {}; const define = Reflect['defineProperty']; define(config, 'url', { value: '/interview-notes/' + id }); http.request(config);`,
    ]) expect(auditFrontendSources(new Map([['web/src/features/reviewReadiness/Owner.tsx', source]])), source).toContain('ui:direct-domain-crud');
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/Owner.tsx', `const local = {}; const R = Reflect; const { set } = R; set(local, 'url', '/interview-notes/' + id); http.get('/preferences');`],
    ]))).not.toContain('ui:direct-domain-crud');
  });

  it('applies statement-order kills to mutation builtin aliases', () => {
    const config = `const config = {};`;
    expect(auditFrontendSources(new Map([['web/src/features/reviewReadiness/Owner.tsx', `${config} let assign = Object.assign; assign = noop; assign(config, { url: '/interview-notes/' + id }); http.request(config);`]]))).not.toContain('ui:direct-domain-crud');
    expect(auditFrontendSources(new Map([['web/src/features/reviewReadiness/Owner.tsx', `${config} let assign = noop; assign = Object.assign; assign(config, { url: '/interview-notes/' + id }); http.request(config);`]]))).toContain('ui:direct-domain-crud');
    expect(auditFrontendSources(new Map([['web/src/layout/AppShell.tsx', `let define = Object.defineProperty; define = noop; define(window.sessionStorage, 'state', { value: proposal.confirmation_token });`]]))).not.toContain('privacy:sensitive-client-persistence');
    const ids = `['application.opportunity_fit', 'application.material_kit', 'application.interview_prepare', 'application.interview_review', 'application.general_review', 'application.offer_review', 'application.record_outcome', 'interview.free_practice', 'materials.resume', 'materials.story', 'materials.reference']`;
    expect(auditFrontendSources(new Map([['web/src/features/coreTaskSurface/contracts.ts', `const IDS = ${ids}; export { IDS as CORE_TASK_IDS }; let set = Reflect.set; set = noop; set(IDS, 0, 'review.readiness');`]]))).not.toContain('core-task:id-union-expanded');
  });

  it('binds mutation builtin aliases by lexical declaration identity', () => {
    const routeMutation = `assign(config, { url: '/interview-notes/' + id });`;
    expect(auditFrontendSources(new Map([['web/src/features/reviewReadiness/Owner.tsx', `const config = {}; const assign = Object.assign; { const assign = noop; assign(config, {}); } ${routeMutation} http.request(config);`]]))).toContain('ui:direct-domain-crud');
    expect(auditFrontendSources(new Map([['web/src/features/reviewReadiness/Owner.tsx', `const config = {}; const assign = noop; { const assign = Object.assign; assign(local, {}); } ${routeMutation} http.request(config);`]]))).not.toContain('ui:direct-domain-crud');
    expect(auditFrontendSources(new Map([['web/src/features/reviewReadiness/Owner.tsx', `const config = {}; const assign = Object.assign; function helper(assign) { ${routeMutation} } helper(noop); http.request(config);`]]))).not.toContain('ui:direct-domain-crud');
    expect(auditFrontendSources(new Map([['web/src/features/reviewReadiness/Owner.tsx', `const config = {}; const assign = Object.assign; function helper() { ${routeMutation} } helper(); http.request(config);`]]))).toContain('ui:direct-domain-crud');
  });

  it('canonicalizes mutation builtin bind, call, apply, and Reflect.apply adapters', () => {
    for (const source of [
      `const config = {}; Object.assign.call(Object, config, { url: '/interview-notes/' + id }); http.request(config);`,
      `const config = {}; const args = [config, 'url', { value: '/interview-notes/' + id }] as const; Reflect.apply(Object.defineProperty, Object, args); http.request(config);`,
      `const config = {}; const write = Reflect.set.bind(Reflect); write(config, 'url', '/interview-notes/' + id); http.request(config);`,
    ]) expect(auditFrontendSources(new Map([['web/src/features/reviewReadiness/Owner.tsx', source]])), source).toContain('ui:direct-domain-crud');
    for (const source of [
      `Object.defineProperty.apply(Object, [window.sessionStorage, 'state', { value: proposal.confirmation_token }]);`,
      `const args = [globalThis.localStorage, 'state', proposal.confirmation_token] as const; Reflect.apply(Reflect.set, Reflect, args);`,
      `const assign = Object.assign.bind(Object); assign(window.sessionStorage, { state: proposal.confirmation_token });`,
    ]) expect(auditFrontendSources(new Map([['web/src/layout/AppShell.tsx', source]])), source).toContain('privacy:sensitive-client-persistence');
    const ids = `['application.opportunity_fit', 'application.material_kit', 'application.interview_prepare', 'application.interview_review', 'application.general_review', 'application.offer_review', 'application.record_outcome', 'interview.free_practice', 'materials.resume', 'materials.story', 'materials.reference']`;
    for (const mutation of [
      `Reflect.set.call(Reflect, IDS, 0, 'review.readiness')`,
      `const args = [IDS, 0, { value: 'review.readiness' }]; Reflect.apply(Reflect.defineProperty, Reflect, args)`,
    ]) expect(auditFrontendSources(new Map([['web/src/features/coreTaskSurface/contracts.ts', `const IDS = ${ids}; export { IDS as CORE_TASK_IDS }; ${mutation};`]])), mutation).toContain('core-task:id-union-expanded');
    expect(auditFrontendSources(new Map([['web/src/features/reviewReadiness/Owner.tsx', `const config = {}; let assign = Object.assign.bind(Object); assign = noop; assign(config, { url: '/interview-notes/' + id }); http.request(config);`]]))).not.toContain('ui:direct-domain-crud');
    expect(auditFrontendSources(new Map([['web/src/layout/AppShell.tsx', `const local = {}; Reflect.set.call(Reflect, local, 'state', proposal.confirmation_token);`]]))).not.toContain('privacy:sensitive-client-persistence');
  });

  it('propagates mutation helper receiver and value parameters to callsites', () => {
    for (const source of [
      `const config = {}; function patch(target) { Object.assign(target, { url: '/interview-notes/' + id }); } patch(config); http.request(config);`,
      `const config = {}; function patch(target) { Object.defineProperty(target, 'url', { value: '/interview-notes/' + id }); } patch(config); http.request(config);`,
    ]) expect(auditFrontendSources(new Map([['web/src/features/reviewReadiness/Owner.tsx', source]])), source).toContain('ui:direct-domain-crud');
    expect(auditFrontendSources(new Map([['web/src/features/reviewReadiness/Owner.tsx', `const config = {}; const local = {}; function patch(target) { Object.assign(target, { url: '/interview-notes/' + id }); } patch(local); http.request(config);`]]))).not.toContain('ui:direct-domain-crud');
    for (const source of [
      `function persist(storage, value) { Reflect.set(storage, 'state', value); } persist(window.sessionStorage, proposal.confirmation_token);`,
      `function persist(storage, value) { Object.defineProperty(storage, 'state', { value }); } persist(globalThis.localStorage, proposal.confirmation_token);`,
    ]) expect(auditFrontendSources(new Map([['web/src/layout/AppShell.tsx', source]])), source).toContain('privacy:sensitive-client-persistence');
    expect(auditFrontendSources(new Map([['web/src/layout/AppShell.tsx', `const local = {}; function persist(storage, value) { Reflect.set(storage, 'state', value); } persist(local, proposal.confirmation_token);`]]))).not.toContain('privacy:sensitive-client-persistence');
    const ids = `['application.opportunity_fit', 'application.material_kit', 'application.interview_prepare', 'application.interview_review', 'application.general_review', 'application.offer_review', 'application.record_outcome', 'interview.free_practice', 'materials.resume', 'materials.story', 'materials.reference']`;
    expect(auditFrontendSources(new Map([['web/src/features/coreTaskSurface/contracts.ts', `const IDS = ${ids}; export { IDS as CORE_TASK_IDS }; function patch(target) { Object.assign(target, { 0: 'review.readiness' }); } patch(IDS);`]]))).toContain('core-task:id-union-expanded');
    expect(auditFrontendSources(new Map([['web/src/features/coreTaskSurface/contracts.ts', `const IDS = ${ids}; export { IDS as CORE_TASK_IDS }; const local = []; function patch(target) { Object.assign(target, { 0: 'review.readiness' }); } patch(local);`]]))).not.toContain('core-task:id-union-expanded');
  });

  it('resolves mutation helper aliases and local object methods through callable provenance', () => {
    for (const source of [
      `const config = {}; function patch(target) { Object.assign(target, { url: '/interview-notes/' + id }); } const alias = patch; alias(config); http.request(config);`,
      `const config = {}; const helpers = { patch(target) { Object.defineProperty(target, 'url', { value: '/interview-notes/' + id }); } }; helpers.patch(config); http.request(config);`,
    ]) expect(auditFrontendSources(new Map([['web/src/features/reviewReadiness/Owner.tsx', source]])), source).toContain('ui:direct-domain-crud');
    expect(auditFrontendSources(new Map([['web/src/features/reviewReadiness/Owner.tsx', `const config = {}; const local = {}; function patch(target) { Object.assign(target, { url: '/interview-notes/' + id }); } const helpers = { patch }; helpers.patch(local); http.request(config);`]]))).not.toContain('ui:direct-domain-crud');
    for (const source of [
      `function persist(storage, value) { Reflect.set(storage, 'state', value); } const alias = persist; alias(window.sessionStorage, proposal.confirmation_token);`,
      `const helpers = { persist(storage, value) { Object.defineProperty(storage, 'state', { value }); } }; helpers.persist(globalThis.localStorage, proposal.confirmation_token);`,
    ]) expect(auditFrontendSources(new Map([['web/src/layout/AppShell.tsx', source]])), source).toContain('privacy:sensitive-client-persistence');
    expect(auditFrontendSources(new Map([['web/src/layout/AppShell.tsx', `const local = {}; function persist(storage, value) { Reflect.set(storage, 'state', value); } const helpers = { persist }; helpers.persist(local, proposal.confirmation_token);`]]))).not.toContain('privacy:sensitive-client-persistence');
    const ids = `['application.opportunity_fit', 'application.material_kit', 'application.interview_prepare', 'application.interview_review', 'application.general_review', 'application.offer_review', 'application.record_outcome', 'interview.free_practice', 'materials.resume', 'materials.story', 'materials.reference']`;
    for (const mutation of [
      `function patch(target) { Object.assign(target, { 0: 'review.readiness' }); } const alias = patch; alias(IDS);`,
      `const helpers = { patch(target) { Reflect.set(target, 0, 'review.readiness'); } }; helpers.patch(IDS);`,
    ]) expect(auditFrontendSources(new Map([['web/src/features/coreTaskSurface/contracts.ts', `const IDS = ${ids}; export { IDS as CORE_TASK_IDS }; ${mutation}`]])), mutation).toContain('core-task:id-union-expanded');
    expect(auditFrontendSources(new Map([['web/src/features/coreTaskSurface/contracts.ts', `const IDS = ${ids}; export { IDS as CORE_TASK_IDS }; const local = []; function patch(target) { Object.assign(target, { 0: 'review.readiness' }); } const helpers = { patch }; helpers.patch(local);`]]))).not.toContain('core-task:id-union-expanded');
  });

  it('follows mutation wrappers across source-map exports and callable object properties', () => {
    const sources = new Map<string, string>([
      ['web/src/services/mutationHelpers.ts', `
        export const mutate = { apply: (receiver, value) => Object.assign(receiver, { url: value, state: value }) };
      `],
      ['web/src/facades/mutationFacade.ts', `export { mutate as default } from '@/services/mutationHelpers';`],
      ['web/src/features/reviewReadiness/Owner.tsx', `
        import mutate from '@/facades/mutationFacade';
        const config = {};
        mutate.apply(config, '/interview-notes/' + id);
        http.request(config);
      `],
      ['web/src/layout/AppShell.tsx', `
        import mutate from '@/facades/mutationFacade';
        mutate.apply(window.sessionStorage, proposal.confirmation_token);
      `],
    ]);
    expect(auditFrontendSources(sources)).toEqual(expect.arrayContaining([
      'ui:direct-domain-crud',
      'privacy:sensitive-client-persistence',
    ]));
  });

  it('follows named and re-exported mutation wrapper functions for closed sets', () => {
    const ids = `['application.opportunity_fit', 'application.material_kit', 'application.interview_prepare', 'application.interview_review', 'application.general_review', 'application.offer_review', 'application.record_outcome', 'interview.free_practice', 'materials.resume', 'materials.story', 'materials.reference']`;
    const sources = new Map<string, string>([
      ['web/src/services/mutationHelpers.ts', `
        export const patch = (receiver, value) => Object.assign(receiver, { 0: value });
      `],
      ['web/src/facades/mutationFacade.ts', `export { patch as apply } from '@/services/mutationHelpers';`],
      ['web/src/features/coreTaskSurface/contracts.ts', `
        import { apply } from '@/facades/mutationFacade';
        const IDS = ${ids};
        apply(IDS, 'review.readiness');
        export { IDS as CORE_TASK_IDS };
      `],
    ]);
    expect(auditFrontendSources(sources)).toContain('core-task:id-union-expanded');
    expect(auditFrontendSources(new Map([
      ...sources,
      ['web/src/features/coreTaskSurface/contracts.ts', `
        import { apply } from '@/facades/mutationFacade';
        const local = [];
        apply(local, 'review.readiness');
        const IDS = ${ids};
        export { IDS as CORE_TASK_IDS };
      `],
    ]))).not.toContain('core-task:id-union-expanded');
  });

  it('follows callable object-property wrappers for closed-set controls', () => {
    const ids = `['application.opportunity_fit', 'application.material_kit', 'application.interview_prepare', 'application.interview_review', 'application.general_review', 'application.offer_review', 'application.record_outcome', 'interview.free_practice', 'materials.resume', 'materials.story', 'materials.reference']`;
    const sources = new Map<string, string>([
      ['web/src/services/mutationHelpers.ts', `
        export const mutations = {
          patch: function (receiver, value) { Object.assign(receiver, { 0: value }); },
        };
      `],
      ['web/src/facades/mutationFacade.ts', `export { mutations as default } from '@/services/mutationHelpers';`],
      ['web/src/features/coreTaskSurface/contracts.ts', `
        import mutations from '@/facades/mutationFacade';
        const IDS = ${ids};
        mutations.patch(IDS, 'review.readiness');
        export { IDS as CORE_TASK_IDS };
      `],
    ]);
    expect(auditFrontendSources(sources)).toContain('core-task:id-union-expanded');
    expect(auditFrontendSources(new Map([
      ...sources,
      ['web/src/features/coreTaskSurface/contracts.ts', `
        import mutations from '@/facades/mutationFacade';
        const local = [];
        mutations.patch(local, 'review.readiness');
        const IDS = ${ids};
        export { IDS as CORE_TASK_IDS };
      `],
    ]))).not.toContain('core-task:id-union-expanded');
  });

  it('resolves cross-module object callable destructuring and static computed aliases', () => {
    const ids = `['application.opportunity_fit', 'application.material_kit', 'application.interview_prepare', 'application.interview_review', 'application.general_review', 'application.offer_review', 'application.record_outcome', 'interview.free_practice', 'materials.resume', 'materials.story', 'materials.reference']`;
    const sources = new Map<string, string>([
      ['web/src/services/mutationHelpers.ts', `
        export const mutations = {
          patch: (receiver, value) => Object.assign(receiver, { url: value, state: value }),
        };
      `],
      ['web/src/facades/mutationFacade.ts', `export * from '@/services/mutationHelpers';`],
      ['web/src/features/reviewReadiness/Owner.tsx', `
        import * as facade from '@/facades/mutationFacade';
        const { mutations, mutations: { patch: apply } } = facade;
        const config = {};
        apply(config, '/interview-notes/' + id);
        http.request(config);
        const key = 'patch';
        const secondConfig = {};
        mutations[key](secondConfig, '/interview-notes/' + id);
        http.request(secondConfig);
      `],
      ['web/src/layout/AppShell.tsx', `
        import * as facade from '@/facades/mutationFacade';
        const { mutations } = facade;
        const key = 'patch';
        const apply = mutations[key];
        apply(window.sessionStorage, proposal.confirmation_token);
      `],
      ['web/src/features/coreTaskSurface/contracts.ts', `
        import { mutations } from '@/facades/mutationFacade';
        const { patch } = mutations;
        const IDS = ${ids};
        patch(IDS, 'review.readiness');
        export { IDS as CORE_TASK_IDS };
      `],
    ]);
    expect(auditFrontendSources(sources)).toEqual(expect.arrayContaining([
      'ui:direct-domain-crud',
      'privacy:sensitive-client-persistence',
      'core-task:id-union-expanded',
    ]));

    const safeSources = new Map<string, string>([
      ['web/src/services/mutationHelpers.ts', sources.get('web/src/services/mutationHelpers.ts')!],
      ['web/src/facades/mutationFacade.ts', sources.get('web/src/facades/mutationFacade.ts')!],
      ['web/src/layout/Safe.tsx', `
        const unrelated = { patch: (receiver, value) => Object.assign(receiver, { state: value }) };
        const { patch: apply } = unrelated;
        const local = {};
        apply(local, proposal.confirmation_token);
      `],
    ]);
    expect(auditFrontendSources(safeSources)).not.toContain('privacy:sensitive-client-persistence');
  });

  it('uses lexical and call-point static keys across all mutation domains', () => {
    const ids = `['application.opportunity_fit', 'application.material_kit', 'application.interview_prepare', 'application.interview_review', 'application.general_review', 'application.offer_review', 'application.record_outcome', 'interview.free_practice', 'materials.resume', 'materials.story', 'materials.reference']`;
    const shared = [
      ['web/src/services/mutationHelpers.ts', `
        export const mutations = {
          patch: (receiver, value) => Object.assign(receiver, { url: value, state: value, 0: value }),
        };
      `],
      ['web/src/facades/mutationFacade.ts', `export * from '@/services/mutationHelpers';`],
    ] as [string, string][];
    const positive = new Map<string, string>([
      ...shared,
      ['web/src/features/reviewReadiness/Owner.tsx', `
        import * as facade from '@/facades/mutationFacade';
        const { mutations } = facade;
        const outerKey = 'nope';
        function owner() {
          const key = outerKey;
          {
            const key = 'patch';
            const alias = mutations[key];
            const config = {};
            alias(config, '/interview-notes/' + id);
            http.request(config);
          }
        }
        owner();
      `],
      ['web/src/layout/AppShell.tsx', `
        import * as facade from '@/facades/mutationFacade';
        const { mutations } = facade;
        const key = 'nope';
        function persist() {
          const key = 'patch';
          const alias = mutations[key];
          alias(window.sessionStorage, proposal.confirmation_token);
        }
        persist();
      `],
      ['web/src/features/coreTaskSurface/contracts.ts', `
        import * as facade from '@/facades/mutationFacade';
        const { mutations } = facade;
        let orderedKey = 'nope';
        orderedKey = 'patch';
        const orderedAlias = mutations[orderedKey];
        const IDS = ${ids};
        orderedAlias(IDS, 'review.readiness');
        function owner() {
          const key = 'patch';
          const alias = mutations[key];
          alias(IDS, 'review.readiness');
        }
        owner();
        export { IDS as CORE_TASK_IDS };
      `],
    ]);
    expect(auditFrontendSources(positive)).toEqual(expect.arrayContaining([
      'ui:direct-domain-crud',
      'privacy:sensitive-client-persistence',
      'core-task:id-union-expanded',
    ]));

    const negative = new Map<string, string>([
      ...shared,
      ['web/src/features/reviewReadiness/Owner.tsx', `
        import * as facade from '@/facades/mutationFacade';
        const { mutations } = facade;
        const key = 'patch';
        function owner() {
          {
            const key = 'nope';
            const alias = mutations[key];
            const config = {};
            alias(config, '/interview-notes/' + id);
            http.request(config);
          }
        }
        owner();
      `],
      ['web/src/layout/AppShell.tsx', `
        import * as facade from '@/facades/mutationFacade';
        const { mutations } = facade;
        const key = 'patch';
        function persist() {
          const key = 'nope';
          const alias = mutations[key];
          alias(window.sessionStorage, proposal.confirmation_token);
        }
        persist();
        let orderedKey = 'patch';
        orderedKey = 'nope';
        const orderedAlias = mutations[orderedKey];
        orderedAlias(window.sessionStorage, proposal.confirmation_token);
      `],
      ['web/src/features/coreTaskSurface/contracts.ts', `
        import * as facade from '@/facades/mutationFacade';
        const { mutations } = facade;
        const key = 'patch';
        function owner() {
          const key = 'nope';
          const alias = mutations[key];
          const IDS = ${ids};
          alias(IDS, 'review.readiness');
        }
        const IDS = ${ids};
        export { IDS as CORE_TASK_IDS };
      `],
    ]);
    expect(auditFrontendSources(negative)).toEqual([]);
  });

  it('resolves wrapper-source routes and fields before substituting caller arguments', () => {
    const shared = [
      ['web/src/services/mutationHelpers.ts', `
        const routeRoot = '/interview-notes/';
        const routeField = 'url';
        const privacyField = 'state';
        const closedField = '0';
        export const mutations = {
          patchTemplate: (receiver, value) => {
            const route = \`\${routeRoot}\${value}\`;
            const field = routeField;
            Object.assign(receiver, { [field]: route });
          },
          patchConcat: (receiver, value) => {
            const route = routeRoot.concat(value);
            const field = routeField;
            Object.assign(receiver, { [field]: route });
          },
          persist: (receiver, value) => {
            const field = privacyField;
            Object.assign(receiver, { [field]: value });
          },
          append: (receiver, value) => {
            const field = closedField;
            Object.assign(receiver, { [field]: value });
          },
        };
      `],
      ['web/src/facades/mutationFacade.ts', `export { mutations as default } from '@/services/mutationHelpers';`],
    ] as [string, string][];
    const ownerPath = 'web/src/features/reviewReadiness/Owner.tsx';
    const positive = new Map<string, string>([
      ...shared,
      [ownerPath, `
        import mutations from '@/facades/mutationFacade';
        const routeRoot = '/preferences/';
        const routeField = 'state';
        const config = {};
        mutations.patchTemplate(config, 'note-123');
        http.request(config);
        const secondConfig = {};
        mutations.patchConcat(secondConfig, noteId);
        http.request(secondConfig);
      `],
      ['web/src/layout/AppShell.tsx', `
        import mutations from '@/facades/mutationFacade';
        const privacyField = 'theme';
        mutations.persist(window.sessionStorage, proposal.confirmation_token);
      `],
      ['web/src/features/coreTaskSurface/contracts.ts', `
        import mutations from '@/facades/mutationFacade';
        const closedField = 'state';
        const IDS = ['application.opportunity_fit', 'application.material_kit', 'application.interview_prepare', 'application.interview_review', 'application.general_review', 'application.offer_review', 'application.record_outcome', 'interview.free_practice', 'materials.resume', 'materials.story', 'materials.reference'];
        mutations.append(IDS, 'review.readiness');
        export { IDS as CORE_TASK_IDS };
      `],
    ]);
    const endpoints = transportEndpoints(parse(ownerPath, positive.get(ownerPath)!), ownerPath, positive);
    expect(endpoints).toEqual(['/interview-notes/note-123', '/interview-notes/${}']);
    expect(auditFrontendSources(positive)).toEqual(expect.arrayContaining([
      'ui:direct-domain-crud',
      'privacy:sensitive-client-persistence',
      'core-task:id-union-expanded',
    ]));

    const safeSources = new Map<string, string>([
      ['web/src/services/mutationHelpers.ts', shared[0][1].replace("const routeRoot = '/interview-notes/';", "const routeRoot = '/preferences/';")],
      shared[1],
      [ownerPath, `
        import mutations from '@/facades/mutationFacade';
        const routeRoot = '/interview-notes/';
        const routeField = 'url';
        const route = '/interview-notes/';
        const config = {};
        mutations.patchTemplate(config, 'note-123');
        http.request(config);
        const secondConfig = {};
        mutations.patchConcat(secondConfig, noteId);
        http.request(secondConfig);
      `],
    ]);
    expect(transportEndpoints(parse(ownerPath, safeSources.get(ownerPath)!), ownerPath, safeSources)).toEqual([
      '/preferences/note-123',
      '/preferences/${}',
    ]);
    expect(auditFrontendSources(safeSources)).toEqual([]);

    const unresolvedWrapperSources = new Map<string, string>([
      ['web/src/services/mutationHelpers.ts', shared[0][1].replace("const routeRoot = '/interview-notes/';", 'const routeRoot = getRouteRoot();')],
      shared[1],
      [ownerPath, safeSources.get(ownerPath)!],
    ]);
    expect(transportEndpoints(
      parse(ownerPath, unresolvedWrapperSources.get(ownerPath)!),
      ownerPath,
      unresolvedWrapperSources,
    )).not.toEqual(expect.arrayContaining([
      '/interview-notes/note-123',
      '/interview-notes/${}',
    ]));
    expect(auditFrontendSources(unresolvedWrapperSources)).toEqual([]);
  });

  it('uses lexical parameter identity for mutation wrapper values', () => {
    const source = `
      function persist(receiver, value) {
        Object.assign(receiver, { state: (() => { const value = 'display-only'; return value; })() });
      }
      persist(window.sessionStorage, proposal.confirmation_token);
    `;
    expect(auditFrontendSources(new Map([['web/src/layout/AppShell.tsx', source]]))).not.toContain('privacy:sensitive-client-persistence');
    expect(auditFrontendSources(new Map([['web/src/layout/AppShell.tsx', source.replace("state: (() => { const value = 'display-only'; return value; })()", 'state: value')]]))).toContain('privacy:sensitive-client-persistence');
  });

  it('rejects canonical window and globalThis Web Storage assignments', () => {
    for (const source of [
      `window.sessionStorage.state = proposal.confirmation_token;`,
      `const KEY = 'state'; globalThis['localStorage'][KEY] = proposal.confirmation_token;`,
    ]) expect(auditFrontendSources(new Map([['web/src/layout/AppShell.tsx', source]])), source).toContain('privacy:sensitive-client-persistence');
    expect(auditFrontendSources(new Map([
      ['web/src/layout/AppShell.tsx', `window.sessionStorage.theme = proposal.display_name;`],
    ]))).not.toContain('privacy:sensitive-client-persistence');
  });

  it('rejects Object.assign and Reflect.set Web Storage writes', () => {
    for (const source of [
      `Object.assign(window.sessionStorage, { state: proposal.confirmation_token });`,
      `Reflect.set(globalThis.localStorage, 'state', proposal.confirmation_token);`,
    ]) expect(auditFrontendSources(new Map([['web/src/layout/AppShell.tsx', source]])), source).toContain('privacy:sensitive-client-persistence');
    expect(auditFrontendSources(new Map([
      ['web/src/layout/AppShell.tsx', `Reflect.set(window.sessionStorage, 'theme', proposal.display_name);`],
    ]))).not.toContain('privacy:sensitive-client-persistence');
  });

  it('rejects sensitive Web Storage defineProperty values', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/layout/AppShell.tsx', `Object.defineProperty(window.sessionStorage, 'state', { value: proposal.confirmation_token });`],
    ]))).toContain('privacy:sensitive-client-persistence');
    for (const source of [
      `Object.defineProperty(window.sessionStorage, 'theme', { value: proposal.display_name });`,
      `const local = {}; Object.defineProperty(local, 'state', { value: proposal.confirmation_token });`,
    ]) expect(auditFrontendSources(new Map([['web/src/layout/AppShell.tsx', source]])), source).not.toContain('privacy:sensitive-client-persistence');
  });

  it('shares computed and aliased mutation builtin provenance for Web Storage', () => {
    for (const source of [
      `const define = Object['defineProperty']; define(window.sessionStorage, 'state', { value: proposal.confirmation_token });`,
      `const METHOD = 'assign'; Object[METHOD](globalThis.localStorage, { state: proposal.confirmation_token });`,
    ]) expect(auditFrontendSources(new Map([['web/src/layout/AppShell.tsx', source]])), source).toContain('privacy:sensitive-client-persistence');
    expect(auditFrontendSources(new Map([
      ['web/src/layout/AppShell.tsx', `const local = {}; const define = Object.defineProperty; define(local, 'state', { value: proposal.confirmation_token });`],
    ]))).not.toContain('privacy:sensitive-client-persistence');
  });

  it('resolves namespace, destructured, and Reflect mutation builtins for Web Storage', () => {
    for (const source of [
      `const R = Reflect; const { set: write } = R; write(window.sessionStorage, 'state', proposal.confirmation_token);`,
      `const R = Reflect; const define = R['defineProperty']; define(globalThis.localStorage, 'state', { value: proposal.confirmation_token });`,
      `const O = Object; const { assign } = O; assign(window.sessionStorage, { state: proposal.confirmation_token });`,
    ]) expect(auditFrontendSources(new Map([['web/src/layout/AppShell.tsx', source]])), source).toContain('privacy:sensitive-client-persistence');
    expect(auditFrontendSources(new Map([
      ['web/src/layout/AppShell.tsx', `const local = {}; const R = Reflect; const set = R.set; set(local, 'state', proposal.confirmation_token);`],
    ]))).not.toContain('privacy:sensitive-client-persistence');
  });

  it('rejects implicit Practice findLast, shift, pop, and length-minus-one target inference', () => {
    const exact = `type Exact = { readinessSignalVersionId: number; targetEventId: number };`;
    for (const body of [
      `plans.findLast((plan) => plan.status === 'in_progress')`,
      `plans.shift()`,
      `plans.pop()`,
      `plans[plans.length - 1]`,
    ]) expect(auditFrontendSources(new Map([
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} function owner(focus: Exact, plans) { return ${body}; }`],
    ])), body).toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      ['web/src/selectors/practice.ts', `export function choose(plans) { return plans.findLast((plan) => plan.status === 'in_progress'); }`],
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} import { choose } from '@/selectors/practice'; function owner(focus: Exact, plans) { return choose(plans); }`],
    ]))).toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} function owner(focus: Exact, plans) { return plans.findLast((plan) => sameExactPair(plan, focus)); }`],
    ]))).not.toContain('practice:implicit-target-fallback');
  });

  it('rejects Practice at-selection and single-item array destructuring', () => {
    const exact = `type Exact = { readinessSignalVersionId: number; targetEventId: number };`;
    for (const body of [
      `return plans.at(-1);`,
      `return plans.at(index);`,
      `const [plan] = plans; return plan;`,
    ]) expect(auditFrontendSources(new Map([
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} function owner(focus: Exact, plans) { ${body} }`],
    ])), body).toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      ['web/src/selectors/practice.ts', `export function choose(plans) { return plans.at(-1); }`],
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} import { choose } from '@/selectors/practice'; function owner(focus: Exact, plans) { return choose(plans); }`],
    ]))).toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} function owner(focus: Exact, plans) { return plans.find((plan) => sameExactPair(plan, focus)); }`],
    ]))).not.toContain('practice:implicit-target-fallback');
  });

  it('rejects Practice secondary destructuring, numeric object binding, and reduce inference', () => {
    const exact = `type Exact = { readinessSignalVersionId: number; targetEventId: number };`;
    for (const body of [
      `const [, plan] = plans; return plan;`,
      `const { 0: plan } = plans; return plan;`,
      `return plans.reduce((selected, plan) => selected ?? plan);`,
    ]) expect(auditFrontendSources(new Map([
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} function owner(focus: Exact, plans) { ${body} }`],
    ])), body).toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      ['web/src/selectors/practice.ts', `export function choose(plans) { const { 0: plan } = plans; return plan; }`],
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} import { choose } from '@/selectors/practice'; function owner(focus: Exact, plans) { return choose(plans); }`],
    ]))).toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} function owner(focus: Exact, plans) { return plans.reduce((selected, plan) => sameExactPair(plan, focus) ? plan : selected, undefined); }`],
    ]))).not.toContain('practice:implicit-target-fallback');
  });

  it('rejects Practice immediate loop returns, iterator first values, and reduceRight inference', () => {
    const exact = `type Exact = { readinessSignalVersionId: number; targetEventId: number };`;
    for (const body of [
      `return (() => { for (const plan of plans) return plan; })();`,
      `return plans.values().next().value;`,
      `return plans.reduceRight((selected, plan) => selected ?? plan);`,
    ]) expect(auditFrontendSources(new Map([
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} function owner(focus: Exact, plans) { ${body} }`],
    ])), body).toContain('practice:implicit-target-fallback');
    expect(auditFrontendSources(new Map([
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `${exact} function owner(focus: Exact, plans) { return plans.reduceRight((selected, plan) => sameExactPair(plan, focus) ? plan : selected, undefined); }`],
    ]))).not.toContain('practice:implicit-target-fallback');
  });

  it('rejects indexed and property assignment mutations of closed sets', () => {
    const ids = `['application.opportunity_fit', 'application.material_kit', 'application.interview_prepare', 'application.interview_review', 'application.general_review', 'application.offer_review', 'application.record_outcome', 'interview.free_practice', 'materials.resume', 'materials.story', 'materials.reference']`;
    for (const mutation of [`IDS[0] = 'review.readiness'`, `IDS.extra = 'review.readiness'`]) expect(auditFrontendSources(new Map([['web/src/features/coreTaskSurface/contracts.ts', `const IDS = ${ids}; export { IDS as CORE_TASK_IDS }; ${mutation};`]])), mutation).toContain('core-task:id-union-expanded');
    const nav = `[{ key: 'today' }, { key: 'applications' }, { key: 'interview' }, { key: 'offers' }, { key: 'resources' }, { key: 'settings' }]`;
    for (const mutation of [`NAV[0] = { key: 'readiness' }`, `NAV.extra = { key: 'readiness' }`]) expect(auditFrontendSources(new Map([['web/src/layout/navigation.ts', `const NAV = ${nav}; export { NAV as MODULE_NAV }; ${mutation};`]])), mutation).toContain('navigation:top-level-expanded');
  });

  it('rejects closed-set alias mutations and nested navigation writes', () => {
    const ids = `['application.opportunity_fit', 'application.material_kit', 'application.interview_prepare', 'application.interview_review', 'application.general_review', 'application.offer_review', 'application.record_outcome', 'interview.free_practice', 'materials.resume', 'materials.story', 'materials.reference']`;
    expect(auditFrontendSources(new Map([['web/src/features/coreTaskSurface/contracts.ts', `const IDS = ${ids}; export { IDS as CORE_TASK_IDS }; const alias = IDS; alias.push('review.readiness');`]]))).toContain('core-task:id-union-expanded');
    const nav = `[{ key: 'today' }, { key: 'applications' }, { key: 'interview' }, { key: 'offers' }, { key: 'resources' }, { key: 'settings' }]`;
    for (const mutation of [`const alias = NAV; alias.reverse()`, `NAV[0].key = 'readiness'`]) expect(auditFrontendSources(new Map([['web/src/layout/navigation.ts', `const NAV = ${nav}; export { NAV as MODULE_NAV }; ${mutation};`]])), mutation).toContain('navigation:top-level-expanded');
    expect(auditFrontendSources(new Map([['web/src/layout/navigation.ts', `const NAV = ${nav}; export { NAV as MODULE_NAV }; const other = [{ key: 'today' }]; other[0].key = 'readiness';`]]))).not.toContain('navigation:top-level-expanded');
  });

  it('rejects Reflect.set and Object.assign closed-set mutations', () => {
    const ids = `['application.opportunity_fit', 'application.material_kit', 'application.interview_prepare', 'application.interview_review', 'application.general_review', 'application.offer_review', 'application.record_outcome', 'interview.free_practice', 'materials.resume', 'materials.story', 'materials.reference']`;
    for (const mutation of [`Reflect.set(IDS, 0, 'review.readiness')`, `Object.assign(IDS, { 0: 'review.readiness' })`]) expect(auditFrontendSources(new Map([['web/src/features/coreTaskSurface/contracts.ts', `const IDS = ${ids}; export { IDS as CORE_TASK_IDS }; ${mutation};`]])), mutation).toContain('core-task:id-union-expanded');
    const nav = `[{ key: 'today' }, { key: 'applications' }, { key: 'interview' }, { key: 'offers' }, { key: 'resources' }, { key: 'settings' }]`;
    for (const mutation of [`Reflect.set(NAV, 0, { key: 'readiness' })`, `Object.assign(NAV, { 0: { key: 'readiness' } })`]) expect(auditFrontendSources(new Map([['web/src/layout/navigation.ts', `const NAV = ${nav}; export { NAV as MODULE_NAV }; ${mutation};`]])), mutation).toContain('navigation:top-level-expanded');
    expect(auditFrontendSources(new Map([['web/src/layout/navigation.ts', `const NAV = ${nav}; export { NAV as MODULE_NAV }; const other = []; Reflect.set(other, 0, { key: 'readiness' });`]]))).not.toContain('navigation:top-level-expanded');
  });

  it('rejects defineProperty closed-set mutations only on the exported value', () => {
    const ids = `['application.opportunity_fit', 'application.material_kit', 'application.interview_prepare', 'application.interview_review', 'application.general_review', 'application.offer_review', 'application.record_outcome', 'interview.free_practice', 'materials.resume', 'materials.story', 'materials.reference']`;
    expect(auditFrontendSources(new Map([['web/src/features/coreTaskSurface/contracts.ts', `const IDS = ${ids}; export { IDS as CORE_TASK_IDS }; Object.defineProperty(IDS, 0, { value: 'review.readiness' });`]]))).toContain('core-task:id-union-expanded');
    const nav = `[{ key: 'today' }, { key: 'applications' }, { key: 'interview' }, { key: 'offers' }, { key: 'resources' }, { key: 'settings' }]`;
    expect(auditFrontendSources(new Map([['web/src/layout/navigation.ts', `const NAV = ${nav}; export { NAV as MODULE_NAV }; Object.defineProperty(NAV, 0, { value: { key: 'readiness' } });`]]))).toContain('navigation:top-level-expanded');
    expect(auditFrontendSources(new Map([['web/src/layout/navigation.ts', `const NAV = ${nav}; export { NAV as MODULE_NAV }; const local = []; Object.defineProperty(local, 0, { value: { key: 'readiness' } });`]]))).not.toContain('navigation:top-level-expanded');
  });

  it('shares computed and aliased mutation builtin provenance for closed sets', () => {
    const ids = `['application.opportunity_fit', 'application.material_kit', 'application.interview_prepare', 'application.interview_review', 'application.general_review', 'application.offer_review', 'application.record_outcome', 'interview.free_practice', 'materials.resume', 'materials.story', 'materials.reference']`;
    for (const mutation of [`const assign = Object['assign']; assign(IDS, { 0: 'review.readiness' })`, `const METHOD = 'defineProperty'; Object[METHOD](IDS, 0, { value: 'review.readiness' })`]) expect(auditFrontendSources(new Map([['web/src/features/coreTaskSurface/contracts.ts', `const IDS = ${ids}; export { IDS as CORE_TASK_IDS }; ${mutation};`]])), mutation).toContain('core-task:id-union-expanded');
    const nav = `[{ key: 'today' }, { key: 'applications' }, { key: 'interview' }, { key: 'offers' }, { key: 'resources' }, { key: 'settings' }]`;
    expect(auditFrontendSources(new Map([['web/src/layout/navigation.ts', `const NAV = ${nav}; export { NAV as MODULE_NAV }; const local = []; const assign = Object.assign; assign(local, { 0: { key: 'readiness' } });`]]))).not.toContain('navigation:top-level-expanded');
  });

  it('resolves namespace, destructured, and Reflect mutation builtins for closed sets', () => {
    const ids = `['application.opportunity_fit', 'application.material_kit', 'application.interview_prepare', 'application.interview_review', 'application.general_review', 'application.offer_review', 'application.record_outcome', 'interview.free_practice', 'materials.resume', 'materials.story', 'materials.reference']`;
    for (const mutation of [
      `const O = Object; const { defineProperty: define } = O; define(IDS, 0, { value: 'review.readiness' })`,
      `const R = Reflect; const set = R['set']; set(IDS, 0, 'review.readiness')`,
      `const R = Reflect; const { defineProperty } = R; defineProperty(IDS, 0, { value: 'review.readiness' })`,
    ]) expect(auditFrontendSources(new Map([['web/src/features/coreTaskSurface/contracts.ts', `const IDS = ${ids}; export { IDS as CORE_TASK_IDS }; ${mutation};`]])), mutation).toContain('core-task:id-union-expanded');
    const nav = `[{ key: 'today' }, { key: 'applications' }, { key: 'interview' }, { key: 'offers' }, { key: 'resources' }, { key: 'settings' }]`;
    expect(auditFrontendSources(new Map([['web/src/layout/navigation.ts', `const NAV = ${nav}; export { NAV as MODULE_NAV }; const local = []; const R = Reflect; const set = R.set; set(local, 0, { key: 'readiness' });`]]))).not.toContain('navigation:top-level-expanded');
  });

  it('treats empty Array.of as a missing Preparation default only', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/services/interviewPreparationProposals.ts', `const ids = input.readiness_feedback_version_ids ?? Array.of();`],
    ]))).toContain('preparation:missing-defaulted-empty');
    expect(auditFrontendSources(new Map([
      ['web/src/services/interviewPreparationProposals.ts', `const ids = input.readiness_feedback_version_ids ?? Array.of('v1');`],
    ]))).not.toContain('preparation:missing-defaulted-empty');
  });

  it('treats Array.from of an empty literal as a missing Preparation default', () => {
    expect(auditFrontendSources(new Map([
      ['web/src/services/interviewPreparationProposals.ts', `const ids = input.readiness_feedback_version_ids ?? Array.from([]);`],
    ]))).toContain('preparation:missing-defaulted-empty');
    expect(auditFrontendSources(new Map([
      ['web/src/services/interviewPreparationProposals.ts', `const ids = input.readiness_feedback_version_ids ?? Array.from(['v1']);`],
    ]))).not.toContain('preparation:missing-defaulted-empty');
  });

  it('recognizes zero-length Array constructors and Array.from sources', () => {
    for (const fallback of [`Array(0)`, `new Array(0)`, `Array.from({ length: 0 })`]) expect(auditFrontendSources(new Map([
      ['web/src/services/interviewPreparationProposals.ts', `const ids = input.readiness_feedback_version_ids ?? ${fallback};`],
    ])), fallback).toContain('preparation:missing-defaulted-empty');
    for (const fallback of [`Array(1)`, `new Array(1)`, `Array.from({ length: 1 })`]) expect(auditFrontendSources(new Map([
      ['web/src/services/interviewPreparationProposals.ts', `const ids = input.readiness_feedback_version_ids ?? ${fallback};`],
    ])), fallback).not.toContain('preparation:missing-defaulted-empty');
  });

  it('ignores comments, inert strings, tests, caches, and other worktrees', () => {
    const fake = `
      // sessionStorage.setItem('token', confirmation_token)
      const prose = "http.post('/api/stories') first(otherInProgress) previous_title";
      { const endpoint = '/api/stories'; void endpoint; }
      { const endpoint = '/safe'; void endpoint; }
    `;
    expect(auditFrontendSources(new Map([
      ['web/src/features/reviewReadiness/safe.ts', fake],
      ['web/src/features/reviewReadiness/safe.test.ts', `localStorage.setItem('token', confirmation_token);`],
      ['web/src/features/reviewReadiness/__tests__/safe.ts', `localStorage.setItem('token', confirmation_token);`],
      ['web/src/features/reviewReadiness/__fixtures__/safe.ts', `localStorage.setItem('token', confirmation_token);`],
      ['web/src/features/reviewReadiness/safe.stories.tsx', `localStorage.setItem('token', confirmation_token);`],
      ['web/.cache/src/features/reviewReadiness/safe.ts', `localStorage.setItem('token', confirmation_token);`],
      ['.worktrees/other/web/src/features/reviewReadiness/safe.ts', `localStorage.setItem('token', confirmation_token);`],
      ['tests/fixtures/reviewReadiness.ts', `localStorage.setItem('token', confirmation_token);`],
    ]))).toEqual([]);
  });

  it('keeps the production audit within its bounded performance baseline', () => {
    expect(auditProductionOnce().durationMs).toBeLessThan(30_000);
  });

  it('keeps the production frontend inside the closed ownership and privacy boundary', () => {
    expect(auditProductionOnce().violations).toEqual([]);
  });
});
