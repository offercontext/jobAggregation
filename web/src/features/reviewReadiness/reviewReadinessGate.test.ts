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
  | { kind: 'generated-token' }
  | { kind: 'story-confirm-function' }
  | { kind: 'string'; text: string }
  | { kind: 'object'; properties: Map<string, AbstractValue> };

const UNKNOWN: AbstractValue = { kind: 'unknown' };
const GENERATED_TOKEN: AbstractValue = { kind: 'generated-token' };
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
    || ts.isNonNullExpression(expression)) {
    return unwrap(expression.expression);
  }
  return expression;
}

function mergeValues(left: AbstractValue, right: AbstractValue): AbstractValue {
  if (left.kind === 'generated-token' || right.kind === 'generated-token') {
    return GENERATED_TOKEN;
  }
  if (left.kind === 'story-confirm-function' || right.kind === 'story-confirm-function') {
    return STORY_CONFIRM_FUNCTION;
  }
  if (left.kind === 'string' && right.kind === 'string' && left.text === right.text) {
    return left;
  }
  if (left.kind === 'object' && left === right) return left;
  return UNKNOWN;
}

function evaluateExpression(rawExpression: ts.Expression, scope: LexicalScope): AbstractValue {
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
  if (ts.isPropertyAccessExpression(expression)) {
    const owner = evaluateExpression(expression.expression, scope);
    return owner.kind === 'object'
      ? owner.properties.get(expression.name.text) ?? UNKNOWN
      : UNKNOWN;
  }
  if (ts.isBinaryExpression(expression)) {
    return mergeValues(
      evaluateExpression(expression.left, scope),
      evaluateExpression(expression.right, scope),
    );
  }
  if (ts.isConditionalExpression(expression)) {
    return mergeValues(
      evaluateExpression(expression.whenTrue, scope),
      evaluateExpression(expression.whenFalse, scope),
    );
  }
  if (ts.isObjectLiteralExpression(expression)) {
    const value: AbstractValue = { kind: 'object', properties: new Map() };
    for (const property of expression.properties) {
      if (ts.isPropertyAssignment(property)) {
        const name = propertyName(property.name);
        if (name !== null) {
          value.properties.set(name, evaluateExpression(property.initializer, scope));
        }
      } else if (ts.isShorthandPropertyAssignment(property)) {
        value.properties.set(property.name.text, evaluateExpression(property.name, scope));
      } else if (ts.isSpreadAssignment(property)) {
        const spread = evaluateExpression(property.expression, scope);
        if (spread.kind === 'object') {
          for (const [name, nested] of spread.properties) value.properties.set(name, nested);
        }
      }
    }
    return value;
  }
  if (ts.isCallExpression(expression)) {
    if (ts.isIdentifier(expression.expression)
      && expression.expression.text === 'key'
      && expression.arguments.some((argument) => (
        ts.isStringLiteral(argument) && argument.text === 'story-confirm'
      ))) return GENERATED_TOKEN;
    if (ts.isPropertyAccessExpression(expression.expression)) {
      const owner = expression.expression.expression;
      const member = expression.expression.name.text;
      if ((member === 'randomUUID' && ts.isIdentifier(owner) && owner.text === 'crypto')
        || (member === 'random' && ts.isIdentifier(owner) && owner.text === 'Math')) {
        return GENERATED_TOKEN;
      }
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
  const replacement: AbstractValue = { kind: 'object', properties: new Map([[property, value]]) };
  scope.assign(ownerName, replacement);
}

function hasGeneratedConfirmationToken(value: AbstractValue): boolean {
  return value.kind === 'object'
    && value.properties.get('confirmation_token')?.kind === 'generated-token';
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
      for (const statement of node.statements) visit(statement, scope);
      return;
    }
    if (ts.isImportDeclaration(node)) {
      const clause = node.importClause;
      if (clause?.name) scope.declare(clause.name.text, UNKNOWN);
      if (clause?.namedBindings && ts.isNamedImports(clause.namedBindings)) {
        for (const specifier of clause.namedBindings.elements) {
          const imported = specifier.propertyName?.text ?? specifier.name.text;
          scope.declare(
            specifier.name.text,
            imported === 'confirmInterviewStoryProposal' ? STORY_CONFIRM_FUNCTION : UNKNOWN,
          );
        }
      } else if (clause?.namedBindings && ts.isNamespaceImport(clause.namedBindings)) {
        scope.declare(clause.namedBindings.name.text, {
          kind: 'object',
          properties: new Map([
            ['confirmInterviewStoryProposal', STORY_CONFIRM_FUNCTION],
          ]),
        });
      }
      return;
    }
    if (ts.isFunctionDeclaration(node)) {
      if (node.name) {
        scope.declare(
          node.name.text,
          node.name.text === 'confirmInterviewStoryProposal'
            ? STORY_CONFIRM_FUNCTION
            : UNKNOWN,
        );
      }
      if (node.body) {
        const child = new LexicalScope(scope);
        for (const parameter of node.parameters) {
          if (ts.isIdentifier(parameter.name)) child.declare(parameter.name.text, UNKNOWN);
        }
        visit(node.body, child);
      }
      return;
    }
    if (ts.isFunctionExpression(node) || ts.isArrowFunction(node)) {
      const child = new LexicalScope(scope);
      for (const parameter of node.parameters) {
        if (ts.isIdentifier(parameter.name)) child.declare(parameter.name.text, UNKNOWN);
      }
      visit(node.body, child);
      return;
    }
    if (ts.isBlock(node)) {
      const child = new LexicalScope(scope);
      for (const statement of node.statements) visit(statement, child);
      return;
    }
    if (ts.isVariableStatement(node)) {
      for (const declaration of node.declarationList.declarations) visit(declaration, scope);
      return;
    }
    if (ts.isVariableDeclaration(node)) {
      if (node.initializer) visit(node.initializer, scope);
      if (ts.isIdentifier(node.name)) {
        scope.declare(
          node.name.text,
          node.initializer ? evaluateExpression(node.initializer, scope) : UNKNOWN,
        );
      }
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
        && hasGeneratedConfirmationToken(request)) {
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
