import { execFileSync } from 'node:child_process';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import ts from 'typescript';
import { describe, expect, it } from 'vitest';

const CANONICAL_COMPONENTS = [
  ['web/src/features/reviewReadiness/ProductActionConfirmation.tsx', 'ProductActionConfirmation'],
  ['web/src/features/reviewReadiness/ReviewReadinessNextStep.tsx', 'ReviewReadinessNextStep'],
  ['web/src/features/reviewReadiness/ReadinessFeedbackAdvisory.tsx', 'ReadinessFeedbackAdvisory'],
] as const;

type AbstractValue =
  | { kind: 'unknown' }
  | { kind: 'server-token' }
  | { kind: 'server-response' }
  | { kind: 'server-response-function' }
  | { kind: 'story-service-namespace' }
  | { kind: 'story-confirm-function' }
  | { kind: 'string'; text: string }
  | {
    kind: 'object';
    properties: Map<string, AbstractValue>;
    unknownProperties: boolean;
  }
  | {
    kind: 'local-function';
    node: ts.FunctionDeclaration | ts.FunctionExpression | ts.ArrowFunction;
    closure: LexicalScope;
  };

const UNKNOWN: AbstractValue = { kind: 'unknown' };
const SERVER_TOKEN: AbstractValue = { kind: 'server-token' };
const SERVER_RESPONSE: AbstractValue = { kind: 'server-response' };
const SERVER_RESPONSE_FUNCTION: AbstractValue = { kind: 'server-response-function' };
const STORY_SERVICE_NAMESPACE: AbstractValue = { kind: 'story-service-namespace' };
const STORY_CONFIRM_FUNCTION: AbstractValue = { kind: 'story-confirm-function' };

class LexicalScope {
  readonly parent: LexicalScope | null;
  readonly bindings = new Map<string, AbstractValue>();

  constructor(parent: LexicalScope | null = null) {
    this.parent = parent;
  }

  declare(name: string, value: AbstractValue): void {
    this.bindings.set(name, value);
  }

  lookup(name: string): { found: boolean; value: AbstractValue } {
    if (this.bindings.has(name)) {
      return { found: true, value: this.bindings.get(name)! };
    }
    return this.parent?.lookup(name) ?? { found: false, value: UNKNOWN };
  }

  assign(name: string, value: AbstractValue): void {
    if (this.bindings.has(name)) {
      this.bindings.set(name, value);
      return;
    }
    if (this.parent && this.parent.lookup(name).found) {
      this.parent.assign(name, value);
      return;
    }
    this.bindings.set(name, value);
  }
}

function repositoryRoot(): string {
  return execFileSync('git', ['rev-parse', '--show-toplevel'], { encoding: 'utf8' }).trim();
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

function propertyName(node: ts.PropertyName): string | null {
  if (ts.isIdentifier(node) || ts.isStringLiteral(node) || ts.isNumericLiteral(node)) {
    return node.text;
  }
  if (ts.isComputedPropertyName(node) && ts.isStringLiteral(node.expression)) {
    return node.expression.text;
  }
  return null;
}

function unwrap(expression: ts.Expression): ts.Expression {
  if (ts.isParenthesizedExpression(expression)
    || ts.isAsExpression(expression)
    || ts.isNonNullExpression(expression)
    || ts.isSatisfiesExpression(expression)) {
    return unwrap(expression.expression);
  }
  return expression;
}

function mergeValues(left: AbstractValue, right: AbstractValue): AbstractValue {
  if (left.kind === 'server-token' && right.kind === 'server-token') return SERVER_TOKEN;
  if (left.kind === 'server-response' && right.kind === 'server-response') {
    return SERVER_RESPONSE;
  }
  if (left.kind === 'server-response-function' && right.kind === 'server-response-function') {
    return SERVER_RESPONSE_FUNCTION;
  }
  if (left.kind === 'story-confirm-function' && right.kind === 'story-confirm-function') {
    return STORY_CONFIRM_FUNCTION;
  }
  if (left.kind === 'string' && right.kind === 'string' && left.text === right.text) {
    return left;
  }
  if (left.kind === 'object' && left === right) return left;
  if (left.kind === 'local-function'
    && right.kind === 'local-function'
    && left.node === right.node) return left;
  return UNKNOWN;
}

function isStoryServiceModule(moduleName: string): boolean {
  return /(?:^|\/)services\/interviewStories$/.test(moduleName);
}

function isStoryServerResponseFunction(name: string): boolean {
  return name === 'createInterviewStoryProposal'
    || name === 'getInterviewStoryProposal'
    || /^(?:create|get|load|propose|recover|request|refresh)InterviewStoryProductAction/.test(name);
}

function objectWithServerToken(): AbstractValue {
  return {
    kind: 'object',
    properties: new Map([['confirmation_token', SERVER_TOKEN]]),
    unknownProperties: false,
  };
}

function propertyValue(owner: AbstractValue, name: string): AbstractValue {
  if (owner.kind === 'object') return owner.properties.get(name) ?? UNKNOWN;
  if (owner.kind === 'story-service-namespace') {
    if (name === 'confirmInterviewStoryProposal') return STORY_CONFIRM_FUNCTION;
    return isStoryServerResponseFunction(name) ? SERVER_RESPONSE_FUNCTION : UNKNOWN;
  }
  if (owner.kind === 'server-response'
    && (name === 'confirmation_token' || name === 'confirmationToken')) {
    return SERVER_TOKEN;
  }
  if (name === 'serverConfirmationToken' || name === 'server_confirmation_token') {
    return SERVER_TOKEN;
  }
  return UNKNOWN;
}

function declareBinding(
  name: ts.BindingName,
  rawValue: AbstractValue,
  scope: LexicalScope,
  activeFunctions = new Set<ts.Node>(),
): void {
  if (ts.isIdentifier(name)) {
    scope.declare(name.text, rawValue);
    return;
  }
  if (!ts.isObjectBindingPattern(name)) return;
  for (const element of name.elements) {
    if (element.dotDotDotToken) {
      declareBinding(element.name, UNKNOWN, scope, activeFunctions);
      continue;
    }
    const key = element.propertyName
      ? propertyName(element.propertyName)
      : ts.isIdentifier(element.name) ? element.name.text : null;
    let value = key === null ? UNKNOWN : propertyValue(rawValue, key);
    if (element.initializer) {
      value = mergeValues(
        value,
        evaluateExpression(element.initializer, scope, activeFunctions),
      );
    }
    declareBinding(element.name, value, scope, activeFunctions);
  }
}

function declareFunctionDeclarations(
  statements: readonly ts.Statement[],
  scope: LexicalScope,
): void {
  for (const statement of statements) {
    if (!ts.isFunctionDeclaration(statement) || !statement.name) continue;
    scope.declare(
      statement.name.text,
      statement.name.text === 'confirmInterviewStoryProposal'
        ? STORY_CONFIRM_FUNCTION
        : { kind: 'local-function', node: statement, closure: scope },
    );
  }
}

function evaluateStatements(
  statements: readonly ts.Statement[],
  scope: LexicalScope,
  activeFunctions: Set<ts.Node>,
): AbstractValue | null {
  declareFunctionDeclarations(statements, scope);
  for (const statement of statements) {
    if (ts.isFunctionDeclaration(statement)) continue;
    if (ts.isVariableStatement(statement)) {
      for (const declaration of statement.declarationList.declarations) {
        declareBinding(
          declaration.name,
          declaration.initializer
            ? evaluateExpression(declaration.initializer, scope, activeFunctions)
            : UNKNOWN,
          scope,
          activeFunctions,
        );
      }
      continue;
    }
    if (ts.isReturnStatement(statement)) {
      return statement.expression
        ? evaluateExpression(statement.expression, scope, activeFunctions)
        : UNKNOWN;
    }
    if (ts.isBlock(statement)) {
      const returned = evaluateStatements(
        statement.statements,
        new LexicalScope(scope),
        activeFunctions,
      );
      if (returned !== null) return returned;
      continue;
    }
    if (ts.isIfStatement(statement)) {
      const thenValue = ts.isBlock(statement.thenStatement)
        ? evaluateStatements(
          statement.thenStatement.statements,
          new LexicalScope(scope),
          activeFunctions,
        )
        : null;
      const elseValue = statement.elseStatement && ts.isBlock(statement.elseStatement)
        ? evaluateStatements(
          statement.elseStatement.statements,
          new LexicalScope(scope),
          activeFunctions,
        )
        : null;
      if (thenValue !== null || elseValue !== null) {
        return thenValue !== null && elseValue !== null
          ? mergeValues(thenValue, elseValue)
          : UNKNOWN;
      }
    }
  }
  return null;
}

function evaluateLocalFunction(
  value: Extract<AbstractValue, { kind: 'local-function' }>,
  arguments_: readonly ts.Expression[],
  callerScope: LexicalScope,
  activeFunctions: Set<ts.Node>,
): AbstractValue {
  if (activeFunctions.has(value.node)) return UNKNOWN;
  const nextActive = new Set(activeFunctions).add(value.node);
  const functionScope = new LexicalScope(value.closure);
  value.node.parameters.forEach((parameter, index) => {
    declareBinding(
      parameter.name,
      arguments_[index]
        ? evaluateExpression(arguments_[index], callerScope, activeFunctions)
        : UNKNOWN,
      functionScope,
      activeFunctions,
    );
  });
  if (!ts.isBlock(value.node.body)) {
    return evaluateExpression(value.node.body, functionScope, nextActive);
  }
  return evaluateStatements(value.node.body.statements, functionScope, nextActive) ?? UNKNOWN;
}

function evaluateExpression(
  rawExpression: ts.Expression,
  scope: LexicalScope,
  activeFunctions = new Set<ts.Node>(),
): AbstractValue {
  const expression = unwrap(rawExpression);
  if (ts.isIdentifier(expression)) {
    const binding = scope.lookup(expression.text);
    if (binding.found) return binding.value;
    return expression.text === 'confirmInterviewStoryProposal'
      ? STORY_CONFIRM_FUNCTION
      : UNKNOWN;
  }
  if (ts.isStringLiteral(expression) || ts.isNoSubstitutionTemplateLiteral(expression)) {
    return { kind: 'string', text: expression.text };
  }
  if (ts.isTemplateExpression(expression)) {
    return {
      kind: 'string',
      text: [
        expression.head.text,
        ...expression.templateSpans.map((span) => span.literal.text),
      ].join(''),
    };
  }
  if (ts.isAwaitExpression(expression)) {
    return evaluateExpression(expression.expression, scope, activeFunctions);
  }
  if (ts.isPropertyAccessExpression(expression)) {
    return propertyValue(
      evaluateExpression(expression.expression, scope, activeFunctions),
      expression.name.text,
    );
  }
  if (ts.isElementAccessExpression(expression)
    && expression.argumentExpression
    && (ts.isStringLiteral(expression.argumentExpression)
      || ts.isNoSubstitutionTemplateLiteral(expression.argumentExpression))) {
    return propertyValue(
      evaluateExpression(expression.expression, scope, activeFunctions),
      expression.argumentExpression.text,
    );
  }
  if (ts.isBinaryExpression(expression)) {
    if (expression.operatorToken.kind === ts.SyntaxKind.QuestionQuestionToken
      || expression.operatorToken.kind === ts.SyntaxKind.BarBarToken) {
      return mergeValues(
        evaluateExpression(expression.left, scope, activeFunctions),
        evaluateExpression(expression.right, scope, activeFunctions),
      );
    }
    return UNKNOWN;
  }
  if (ts.isConditionalExpression(expression)) {
    return mergeValues(
      evaluateExpression(expression.whenTrue, scope, activeFunctions),
      evaluateExpression(expression.whenFalse, scope, activeFunctions),
    );
  }
  if (ts.isFunctionExpression(expression) || ts.isArrowFunction(expression)) {
    return { kind: 'local-function', node: expression, closure: scope };
  }
  if (ts.isObjectLiteralExpression(expression)) {
    const value: AbstractValue = {
      kind: 'object',
      properties: new Map(),
      unknownProperties: false,
    };
    for (const property of expression.properties) {
      if (ts.isPropertyAssignment(property)) {
        const name = propertyName(property.name);
        if (name !== null) {
          value.properties.set(
            name,
            evaluateExpression(property.initializer, scope, activeFunctions),
          );
        }
      } else if (ts.isShorthandPropertyAssignment(property)) {
        value.properties.set(
          property.name.text,
          evaluateExpression(property.name, scope, activeFunctions),
        );
      } else if (ts.isSpreadAssignment(property)) {
        const spread = evaluateExpression(property.expression, scope, activeFunctions);
        if (spread.kind === 'object') {
          for (const [name, nested] of spread.properties) value.properties.set(name, nested);
          value.unknownProperties ||= spread.unknownProperties;
        } else if (spread.kind === 'server-response') {
          value.properties.set('confirmation_token', SERVER_TOKEN);
        } else {
          value.properties.delete('confirmation_token');
          value.unknownProperties = true;
        }
      }
    }
    return value;
  }
  if (ts.isCallExpression(expression)) {
    const callee = evaluateExpression(expression.expression, scope, activeFunctions);
    if (callee.kind === 'server-response-function') return SERVER_RESPONSE;
    if (callee.kind === 'local-function') {
      return evaluateLocalFunction(
        callee,
        expression.arguments,
        scope,
        activeFunctions,
      );
    }
  }
  return UNKNOWN;
}

function assignProperty(
  ownerName: string,
  property: string,
  value: AbstractValue,
  scope: LexicalScope,
): void {
  const existing = scope.lookup(ownerName);
  if (existing.found && existing.value.kind === 'object') {
    existing.value.properties.set(property, value);
    return;
  }
  const replacement: AbstractValue = {
    kind: 'object',
    properties: new Map([[property, value]]),
    unknownProperties: false,
  };
  scope.assign(ownerName, replacement);
}

function hasUnsafeConfirmationToken(value: AbstractValue): boolean {
  if (value.kind !== 'object') return true;
  const token = value.properties.get('confirmation_token');
  return token === undefined ? value.unknownProperties : token.kind !== 'server-token';
}

function callIsStoryConfirmHttpPost(node: ts.CallExpression, scope: LexicalScope): boolean {
  if (!ts.isPropertyAccessExpression(node.expression)
    || node.expression.name.text !== 'post'
    || node.arguments.length < 2) return false;
  const endpoint = evaluateExpression(node.arguments[0], scope);
  return endpoint.kind === 'string'
    && endpoint.text.includes('interview-story-proposals')
    && endpoint.text.includes('/confirm');
}

function hasClientGeneratedStoryAuthorization(sourceFile: ts.SourceFile): boolean {
  let violation = false;

  const visit = (node: ts.Node, scope: LexicalScope): void => {
    if (violation) return;
    if (ts.isSourceFile(node)) {
      declareFunctionDeclarations(node.statements, scope);
      for (const statement of node.statements) visit(statement, scope);
      return;
    }
    if (ts.isImportDeclaration(node)) {
      const clause = node.importClause;
      const moduleName = ts.isStringLiteral(node.moduleSpecifier)
        ? node.moduleSpecifier.text
        : '';
      const storyService = isStoryServiceModule(moduleName);
      if (clause?.name) scope.declare(clause.name.text, UNKNOWN);
      if (clause?.namedBindings && ts.isNamedImports(clause.namedBindings)) {
        for (const specifier of clause.namedBindings.elements) {
          const imported = specifier.propertyName?.text ?? specifier.name.text;
          scope.declare(
            specifier.name.text,
            imported === 'confirmInterviewStoryProposal'
              ? STORY_CONFIRM_FUNCTION
              : storyService && isStoryServerResponseFunction(imported)
                ? SERVER_RESPONSE_FUNCTION
                : UNKNOWN,
          );
        }
      } else if (clause?.namedBindings && ts.isNamespaceImport(clause.namedBindings)) {
        scope.declare(
          clause.namedBindings.name.text,
          storyService ? STORY_SERVICE_NAMESPACE : UNKNOWN,
        );
      }
      return;
    }
    if (ts.isFunctionDeclaration(node)) {
      if (node.name) {
        scope.declare(
          node.name.text,
          node.name.text === 'confirmInterviewStoryProposal'
            ? STORY_CONFIRM_FUNCTION
            : { kind: 'local-function', node, closure: scope },
        );
      }
      if (node.body) {
        const child = new LexicalScope(scope);
        node.parameters.forEach((parameter, index) => {
          declareBinding(
            parameter.name,
            node.name?.text === 'confirmInterviewStoryProposal' && index === 1
              ? objectWithServerToken()
              : UNKNOWN,
            child,
          );
        });
        visit(node.body, child);
      }
      return;
    }
    if (ts.isFunctionExpression(node) || ts.isArrowFunction(node)) {
      const child = new LexicalScope(scope);
      for (const parameter of node.parameters) {
        declareBinding(parameter.name, UNKNOWN, child);
      }
      visit(node.body, child);
      return;
    }
    if (ts.isBlock(node)) {
      const child = new LexicalScope(scope);
      declareFunctionDeclarations(node.statements, child);
      for (const statement of node.statements) visit(statement, child);
      return;
    }
    if (ts.isVariableStatement(node)) {
      for (const declaration of node.declarationList.declarations) visit(declaration, scope);
      return;
    }
    if (ts.isVariableDeclaration(node)) {
      if (node.initializer) visit(node.initializer, scope);
      declareBinding(
        node.name,
        node.initializer ? evaluateExpression(node.initializer, scope) : UNKNOWN,
        scope,
      );
      return;
    }
    if (ts.isBinaryExpression(node)
      && node.operatorToken.kind === ts.SyntaxKind.EqualsToken) {
      visit(node.right, scope);
      const value = evaluateExpression(node.right, scope);
      if (ts.isIdentifier(node.left)) {
        scope.assign(node.left.text, value);
      } else if (ts.isPropertyAccessExpression(node.left)
        && ts.isIdentifier(node.left.expression)) {
        assignProperty(node.left.expression.text, node.left.name.text, value, scope);
      }
      return;
    }
    if (ts.isCallExpression(node)) {
      const callee = evaluateExpression(node.expression, scope);
      const request = node.arguments[1]
        ? evaluateExpression(node.arguments[1], scope)
        : UNKNOWN;
      if ((callee.kind === 'story-confirm-function' || callIsStoryConfirmHttpPost(node, scope))
        && hasUnsafeConfirmationToken(request)) {
        violation = true;
        return;
      }
      visit(node.expression, scope);
      for (const argument of node.arguments) visit(argument, scope);
      return;
    }
    ts.forEachChild(node, (child) => visit(child, scope));
  };

  visit(sourceFile, new LexicalScope());
  return violation;
}

function hasExportedCanonicalComponent(sourceFile: ts.SourceFile, name: string): boolean {
  const exported = (node: ts.Node & { modifiers?: ts.NodeArray<ts.ModifierLike> }): boolean => (
    Boolean(node.modifiers?.some((modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword))
  );
  const rendersJsx = (node: ts.Node): boolean => {
    let found = false;
    const visit = (child: ts.Node): void => {
      if (found) return;
      if (ts.isJsxElement(child)
        || ts.isJsxSelfClosingElement(child)
        || ts.isJsxFragment(child)) {
        found = true;
        return;
      }
      if (child !== node
        && (ts.isFunctionDeclaration(child)
          || ts.isFunctionExpression(child)
          || ts.isArrowFunction(child))) return;
      ts.forEachChild(child, visit);
    };
    visit(node);
    return found;
  };
  for (const statement of sourceFile.statements) {
    if (ts.isFunctionDeclaration(statement)
      && statement.name?.text === name
      && exported(statement)
      && statement.body
      && rendersJsx(statement.body)) return true;
    if (ts.isVariableStatement(statement) && exported(statement)) {
      if (statement.declarationList.declarations.some((declaration) => (
        ts.isIdentifier(declaration.name)
        && declaration.name.text === name
        && Boolean(declaration.initializer)
        && (ts.isArrowFunction(declaration.initializer!)
          || ts.isFunctionExpression(declaration.initializer!))
        && rendersJsx(declaration.initializer!)
      ))) return true;
    }
  }
  return false;
}

function normalizeSourcePath(path: string): string {
  return path.replaceAll('\\', '/').replace(/\.tsx?$/, '');
}

function importTargetsSource(
  importerPath: string,
  moduleName: string,
  targetPath: string,
): boolean {
  let resolved: string;
  if (moduleName.startsWith('@/')) {
    resolved = `web/src/${moduleName.slice(2)}`;
  } else if (moduleName.startsWith('.')) {
    resolved = join(dirname(importerPath), moduleName);
  } else {
    return false;
  }
  return normalizeSourcePath(resolved) === normalizeSourcePath(targetPath);
}

function ownerImportsAndRenders(
  ownerPath: string,
  ownerSource: string,
  componentPath: string,
  componentName: string,
): boolean {
  const sourceFile = parse(ownerPath, ownerSource);
  const localNames = new Set<string>();
  for (const statement of sourceFile.statements) {
    if (!ts.isImportDeclaration(statement)
      || !ts.isStringLiteral(statement.moduleSpecifier)
      || !importTargetsSource(
        ownerPath,
        statement.moduleSpecifier.text,
        componentPath,
      )) continue;
    const clause = statement.importClause;
    if (clause?.namedBindings && ts.isNamedImports(clause.namedBindings)) {
      for (const specifier of clause.namedBindings.elements) {
        const imported = specifier.propertyName?.text ?? specifier.name.text;
        if (imported === componentName) localNames.add(specifier.name.text);
      }
    }
  }
  if (localNames.size === 0) return false;
  let rendered = false;
  const visit = (node: ts.Node): void => {
    if (rendered) return;
    if ((ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node))
      && ts.isIdentifier(node.tagName)
      && localNames.has(node.tagName.text)) {
      rendered = true;
      return;
    }
    if (ts.isCallExpression(node)
      && ts.isIdentifier(node.expression)
      && localNames.has(node.expression.text)) {
      rendered = true;
      return;
    }
    if (ts.isCallExpression(node)
      && ts.isPropertyAccessExpression(node.expression)
      && node.expression.name.text === 'createElement'
      && node.arguments[0]
      && ts.isIdentifier(node.arguments[0])
      && localNames.has(node.arguments[0].text)) {
      rendered = true;
      return;
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return rendered;
}

const COMPONENT_OWNERS = new Map<string, readonly string[]>([
  ['ProductActionConfirmation', [
    'web/src/components/InterviewReviewProposalDrawer.tsx',
    'web/src/components/InterviewStoryDrawer.tsx',
  ]],
  ['ReviewReadinessNextStep', [
    'web/src/components/InterviewReviewProposalDrawer.tsx',
  ]],
  ['ReadinessFeedbackAdvisory', [
    'web/src/components/AdaptiveInterviewPracticeWorkspace.tsx',
    'web/src/components/InterviewPreparationProposalDrawer.tsx',
  ]],
]);

function clientAuthorizationViolations(sources: Map<string, string>): string[] {
  const unsafe = [...sources.entries()].some(([path, source]) => (
    /\.tsx?$/.test(path)
    && !/\.(?:test|spec)\.tsx?$/.test(path)
    && hasClientGeneratedStoryAuthorization(parse(path, source))
  ));
  return unsafe ? ['ui:client-authorization-token'] : [];
}

function canonicalComponentViolations(sources: Map<string, string>): string[] {
  const missing = CANONICAL_COMPONENTS.some(([componentPath, componentName]) => {
    const source = sources.get(componentPath);
    if (source === undefined
      || !hasExportedCanonicalComponent(parse(componentPath, source), componentName)) {
      return true;
    }
    return !(COMPONENT_OWNERS.get(componentName) ?? []).some((ownerPath) => {
      const ownerSource = sources.get(ownerPath);
      return ownerSource !== undefined && ownerImportsAndRenders(
        ownerPath,
        ownerSource,
        componentPath,
        componentName,
      );
    });
  });
  return missing ? ['ui:missing-canonical-review-readiness-components'] : [];
}

function collectProductionSources(root: string): Map<string, string> {
  const sources = new Map<string, string>();
  const sourceRoot = join(root, 'web', 'src');
  const walk = (directory: string): void => {
    for (const name of readdirSync(directory)) {
      const path = join(directory, name);
      if (statSync(path).isDirectory()) {
        walk(path);
      } else if (/\.tsx?$/.test(name) && !/\.(?:test|spec)\.tsx?$/.test(name)) {
        const relativePath = relative(root, path).replaceAll('\\', '/');
        sources.set(relativePath, readFileSync(path, 'utf8'));
      }
    }
  };
  walk(sourceRoot);
  return sources;
}

function sourceViolations(root: string): string[] {
  const sources = collectProductionSources(root);
  return [
    ...clientAuthorizationViolations(sources),
    ...canonicalComponentViolations(sources),
  ];
}

describe('review readiness mechanical gate', () => {
  it('ignores comments, strings, and aliased server-issued draft tokens', () => {
    const safe = parse('safe.tsx', `
      // confirmInterviewStoryProposal(id, { confirmation_token: key('story-confirm') });
      const text = "confirmation_token: crypto.randomUUID()";
      const confirmation_token = draft.serverConfirmationToken;
      const base = { content: draft.content };
      const request = { ...base, confirmation_token };
      const send = confirmInterviewStoryProposal;
      send(id, request);
    `);
    expect(hasClientGeneratedStoryAuthorization(safe)).toBe(false);
  });

  it('detects request variables, shorthand, spreads, aliases, and property writes', () => {
    const unsafeCases = [
      `const confirmation_token = key('story-confirm');
       const request = { confirmation_token };
       confirmInterviewStoryProposal(id, request);`,
      `const auth = { confirmation_token: crypto.randomUUID() };
       const request = { content, ...auth };
       const send = confirmInterviewStoryProposal;
       send(id, request);`,
      `const request = { ...input };
       request.confirmation_token = key('story-confirm');
       confirmInterviewStoryProposal(id, request);`,
    ];
    for (const source of unsafeCases) {
      expect(hasClientGeneratedStoryAuthorization(parse('unsafe.tsx', source))).toBe(true);
    }
  });

  it('rejects every token without an explicit server-issued source through local helpers', () => {
    const unsafeCases = [
      `function makeToken() { return crypto.randomUUID(); }
       const alias = makeToken;
       confirmInterviewStoryProposal(id, { confirmation_token: alias() });`,
      `const token = \`\${Date.now()}-\${Math.random()}\`;
       confirmInterviewStoryProposal(id, { confirmation_token: token });`,
      `function first() { return second(); }
       const second = () => third();
       function third() { return Date.now(); }
       const alias = first;
       confirmInterviewStoryProposal(id, { confirmation_token: alias() });`,
      `confirmInterviewStoryProposal(id, { confirmation_token: 'client-value' });`,
      `confirmInterviewStoryProposal(id, { confirmation_token: opaqueToken() });`,
      `confirmInterviewStoryProposal(id, { ...opaqueRequest() });`,
    ];
    for (const source of unsafeCases) {
      expect(hasClientGeneratedStoryAuthorization(parse('unsafe-source.tsx', source))).toBe(true);
    }
  });

  it('allows server response tokens propagated through local helpers and aliases', () => {
    const safe = parse('server-source.tsx', `
      import { getInterviewStoryProposal as loadProposal } from '@/services/interviewStories';
      const takeToken = (response) => response.confirmation_token;
      function forwardToken(response) {
        const helper = takeToken;
        return helper(response);
      }
      async function owner() {
        const response = await loadProposal(id);
        const confirmation_token = forwardToken(response);
        const request = { confirmation_token };
        confirmInterviewStoryProposal(id, request);
      }
      async function destructuredOwner() {
        const { confirmation_token: serverToken } = await loadProposal(id);
        confirmInterviewStoryProposal(id, { confirmation_token: serverToken });
      }
    `);
    expect(hasClientGeneratedStoryAuthorization(safe)).toBe(false);
  });

  it('detects token generation inside the Story service HTTP path', () => {
    const unsafeService = parse('interviewStories.ts', `
      function confirmInterviewStoryProposal(attemptId, input) {
        const endpoint = \`/interview-story-proposals/\${attemptId}/confirm\`;
        const request = {
          ...input,
          confirmation_token: input.confirmation_token ?? crypto.randomUUID(),
        };
        return http.post(endpoint, request);
      }
    `);
    expect(hasClientGeneratedStoryAuthorization(unsafeService)).toBe(true);
  });

  it('scans every production TS/TSX source for Story confirmation authorization', () => {
    const sources = new Map<string, string>([
      ['web/src/components/InterviewStoryDrawer.tsx', `
        confirmInterviewStoryProposal(id, {
          confirmation_token: draft.serverConfirmationToken,
        });
      `],
      ['web/src/services/interviewStories.ts', `
        http.post('/interview-story-proposals/1/confirm', input);
      `],
      ['web/src/features/reviewReadiness/useProductActionConfirmation.ts', `
        import * as stories from '@/services/interviewStories';
        const request = { confirmation_token: crypto.randomUUID() };
        stories.confirmInterviewStoryProposal(id, request);
      `],
    ]);
    expect(clientAuthorizationViolations(sources)).toEqual([
      'ui:client-authorization-token',
    ]);
  });

  it('uses lexical scope and call-point assignment order for token flow', () => {
    const unsafeBeforeSafeWrite = parse('ordered.tsx', `
      const request = { confirmation_token: key('story-confirm') };
      confirmInterviewStoryProposal(id, request);
      request.confirmation_token = draft.serverConfirmationToken;
    `);
    expect(hasClientGeneratedStoryAuthorization(unsafeBeforeSafeWrite)).toBe(true);

    const nestedShadow = parse('shadowed.tsx', `
      function owner() {
        const token = draft.serverConfirmationToken;
        confirmInterviewStoryProposal(id, { confirmation_token: token });
      }
      function unrelated() {
        const token = key('story-confirm');
        return token;
      }
    `);
    expect(hasClientGeneratedStoryAuthorization(nestedShadow)).toBe(false);
  });

  it('requires an exported React component shape, not a name-only declaration', () => {
    const emptyFunction = parse('empty.tsx', `
      export function ProductActionConfirmation() { return null; }
    `);
    const numberExport = parse('number.tsx', `
      export const ProductActionConfirmation = 1;
    `);
    const pseudo = parse('pseudo.tsx', `
      const text = 'ProductActionConfirmation';
      function ProductActionConfirmation() { return null; }
    `);
    const exact = parse('exact.tsx', `
      export function ProductActionConfirmation() { return <section />; }
    `);
    expect(hasExportedCanonicalComponent(emptyFunction, 'ProductActionConfirmation')).toBe(false);
    expect(hasExportedCanonicalComponent(numberExport, 'ProductActionConfirmation')).toBe(false);
    expect(hasExportedCanonicalComponent(pseudo, 'ProductActionConfirmation')).toBe(false);
    expect(hasExportedCanonicalComponent(exact, 'ProductActionConfirmation')).toBe(true);
  });

  it('requires every canonical component to be imported and rendered by an approved owner', () => {
    const componentSources = new Map<string, string>([
      ['web/src/features/reviewReadiness/ProductActionConfirmation.tsx', `
        export function ProductActionConfirmation() { return <section />; }
      `],
      ['web/src/features/reviewReadiness/ReviewReadinessNextStep.tsx', `
        export function ReviewReadinessNextStep() { return <section />; }
      `],
      ['web/src/features/reviewReadiness/ReadinessFeedbackAdvisory.tsx', `
        export function ReadinessFeedbackAdvisory() { return <section />; }
      `],
      ['web/src/components/InterviewReviewProposalDrawer.tsx', 'export function Owner() { return null; }'],
      ['web/src/components/InterviewStoryDrawer.tsx', 'export function Owner() { return null; }'],
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', 'export function Owner() { return null; }'],
      ['web/src/components/InterviewPreparationProposalDrawer.tsx', 'export function Owner() { return null; }'],
    ]);
    expect(canonicalComponentViolations(componentSources)).toEqual([
      'ui:missing-canonical-review-readiness-components',
    ]);

    componentSources.set('web/src/components/InterviewReviewProposalDrawer.tsx', `
      import ProductActionConfirmation from '@/features/reviewReadiness/ProductActionConfirmation';
      import { ReviewReadinessNextStep } from '@/features/reviewReadiness/ReviewReadinessNextStep';
      export function Owner() {
        return <><ProductActionConfirmation /><ReviewReadinessNextStep /></>;
      }
    `);
    componentSources.set('web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `
      import { ReadinessFeedbackAdvisory } from '@/features/reviewReadiness/ReadinessFeedbackAdvisory';
      export function Owner() { return <ReadinessFeedbackAdvisory />; }
    `);
    expect(canonicalComponentViolations(componentSources)).toEqual([
      'ui:missing-canonical-review-readiness-components',
    ]);

    componentSources.set('web/src/components/InterviewReviewProposalDrawer.tsx', `
      import { ProductActionConfirmation } from '@/features/reviewReadiness/ProductActionConfirmation';
      import { ReviewReadinessNextStep } from '@/features/reviewReadiness/ReviewReadinessNextStep';
      export function Owner() {
        return <><ProductActionConfirmation /><ReviewReadinessNextStep /></>;
      }
    `);
    expect(canonicalComponentViolations(componentSources)).toEqual([]);
  });

  it('closes client Story authorization and canonical component gaps', () => {
    // Intentional RED until Task 11 installs the server-token owner flow and components.
    expect(sourceViolations(repositoryRoot())).toEqual([]);
  });
});
