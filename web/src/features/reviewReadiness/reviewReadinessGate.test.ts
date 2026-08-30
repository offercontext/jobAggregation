import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import ts from 'typescript';
import { describe, expect, it } from 'vitest';

const STORY_AUTHORIZATION_SOURCES = [
  'web/src/components/InterviewStoryDrawer.tsx',
  'web/src/services/interviewStories.ts',
] as const;
const CANONICAL_COMPONENTS = [
  ['web/src/features/reviewReadiness/ProductActionConfirmation.tsx', 'ProductActionConfirmation'],
  ['web/src/features/reviewReadiness/ReviewReadinessNextStep.tsx', 'ReviewReadinessNextStep'],
  ['web/src/features/reviewReadiness/ReadinessFeedbackAdvisory.tsx', 'ReadinessFeedbackAdvisory'],
] as const;

type BindingIndex = {
  initializers: Map<string, ts.Expression>;
  propertyWrites: Map<string, Map<string, ts.Expression[]>>;
};

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

function bindingIndex(sourceFile: ts.SourceFile): BindingIndex {
  const initializers = new Map<string, ts.Expression>();
  const propertyWrites = new Map<string, Map<string, ts.Expression[]>>();
  const visit = (node: ts.Node): void => {
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer) {
      initializers.set(node.name.text, node.initializer);
    }
    if (ts.isBinaryExpression(node)
      && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
      && ts.isPropertyAccessExpression(node.left)
      && ts.isIdentifier(node.left.expression)) {
      const owner = node.left.expression.text;
      const property = node.left.name.text;
      const byProperty = propertyWrites.get(owner) ?? new Map<string, ts.Expression[]>();
      byProperty.set(property, [...(byProperty.get(property) ?? []), node.right]);
      propertyWrites.set(owner, byProperty);
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return { initializers, propertyWrites };
}

function expressionGeneratesClientStoryToken(
  rawExpression: ts.Expression,
  bindings: BindingIndex,
  seen = new Set<string>(),
): boolean {
  const expression = unwrap(rawExpression);
  if (ts.isIdentifier(expression)) {
    if (seen.has(expression.text)) return false;
    const initializer = bindings.initializers.get(expression.text);
    if (!initializer) return false;
    return expressionGeneratesClientStoryToken(
      initializer,
      bindings,
      new Set([...seen, expression.text]),
    );
  }
  if (ts.isBinaryExpression(expression)) {
    return expressionGeneratesClientStoryToken(expression.left, bindings, seen)
      || expressionGeneratesClientStoryToken(expression.right, bindings, seen);
  }
  if (ts.isConditionalExpression(expression)) {
    return expressionGeneratesClientStoryToken(expression.whenTrue, bindings, seen)
      || expressionGeneratesClientStoryToken(expression.whenFalse, bindings, seen);
  }
  if (!ts.isCallExpression(expression)) return false;
  if (ts.isIdentifier(expression.expression) && expression.expression.text === 'key') {
    return expression.arguments.some((argument) => (
      ts.isStringLiteral(argument) && argument.text === 'story-confirm'
    ));
  }
  if (ts.isPropertyAccessExpression(expression.expression)) {
    const owner = expression.expression.expression;
    const member = expression.expression.name.text;
    return (
      member === 'randomUUID' && ts.isIdentifier(owner) && owner.text === 'crypto'
    ) || (
      member === 'random' && ts.isIdentifier(owner) && owner.text === 'Math'
    );
  }
  return false;
}

function effectiveObjectProperty(
  rawExpression: ts.Expression,
  wanted: string,
  bindings: BindingIndex,
  seen = new Set<string>(),
): ts.Expression[] {
  const expression = unwrap(rawExpression);
  if (ts.isIdentifier(expression)) {
    if (seen.has(expression.text)) return [];
    const nextSeen = new Set([...seen, expression.text]);
    let values = bindings.initializers.has(expression.text)
      ? effectiveObjectProperty(
        bindings.initializers.get(expression.text)!,
        wanted,
        bindings,
        nextSeen,
      )
      : [];
    const writes = bindings.propertyWrites.get(expression.text)?.get(wanted) ?? [];
    if (writes.length > 0) values = [writes[writes.length - 1]];
    return values;
  }
  if (!ts.isObjectLiteralExpression(expression)) return [];
  let values: ts.Expression[] = [];
  for (const property of expression.properties) {
    if (ts.isPropertyAssignment(property) && propertyName(property.name) === wanted) {
      values = [property.initializer];
    } else if (ts.isShorthandPropertyAssignment(property) && property.name.text === wanted) {
      values = [property.name];
    } else if (ts.isSpreadAssignment(property)) {
      const spreadValues = effectiveObjectProperty(property.expression, wanted, bindings, seen);
      if (spreadValues.length > 0) values = spreadValues;
    }
  }
  return values;
}

function expressionIsStoryConfirmationFunction(
  rawExpression: ts.Expression,
  bindings: BindingIndex,
  seen = new Set<string>(),
): boolean {
  const expression = unwrap(rawExpression);
  if (!ts.isIdentifier(expression)) return false;
  if (expression.text === 'confirmInterviewStoryProposal') return true;
  if (seen.has(expression.text)) return false;
  const initializer = bindings.initializers.get(expression.text);
  return Boolean(initializer) && expressionIsStoryConfirmationFunction(
    initializer!,
    bindings,
    new Set([...seen, expression.text]),
  );
}

function staticText(
  rawExpression: ts.Expression,
  bindings: BindingIndex,
  seen = new Set<string>(),
): string | null {
  const expression = unwrap(rawExpression);
  if (ts.isStringLiteral(expression) || ts.isNoSubstitutionTemplateLiteral(expression)) {
    return expression.text;
  }
  if (ts.isTemplateExpression(expression)) {
    return [expression.head.text, ...expression.templateSpans.map((span) => span.literal.text)].join('');
  }
  if (ts.isIdentifier(expression) && !seen.has(expression.text)) {
    const initializer = bindings.initializers.get(expression.text);
    if (initializer) {
      return staticText(initializer, bindings, new Set([...seen, expression.text]));
    }
  }
  return null;
}

function isStoryConfirmHttpPost(node: ts.CallExpression, bindings: BindingIndex): boolean {
  if (!ts.isPropertyAccessExpression(node.expression)
    || node.expression.name.text !== 'post'
    || node.arguments.length < 2) return false;
  const endpoint = staticText(node.arguments[0], bindings);
  return Boolean(endpoint?.includes('interview-story-proposals') && endpoint.includes('/confirm'));
}

function hasClientGeneratedStoryAuthorization(sourceFile: ts.SourceFile): boolean {
  const bindings = bindingIndex(sourceFile);
  let violation = false;
  const visit = (node: ts.Node): void => {
    if (violation) return;
    if (ts.isCallExpression(node)) {
      let input: ts.Expression | undefined;
      if (expressionIsStoryConfirmationFunction(node.expression, bindings)) {
        input = node.arguments[1];
      } else if (isStoryConfirmHttpPost(node, bindings)) {
        input = node.arguments[1];
      }
      if (input) {
        const tokenValues = effectiveObjectProperty(
          input,
          'confirmation_token',
          bindings,
        );
        if (tokenValues.some((value) => (
          expressionGeneratesClientStoryToken(value, bindings)
        ))) {
          violation = true;
          return;
        }
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return violation;
}

function hasExportedCanonicalComponent(sourceFile: ts.SourceFile, name: string): boolean {
  const exported = (node: ts.Node & { modifiers?: ts.NodeArray<ts.ModifierLike> }): boolean => (
    Boolean(node.modifiers?.some((modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword))
  );
  for (const statement of sourceFile.statements) {
    if (ts.isFunctionDeclaration(statement)
      && statement.name?.text === name
      && exported(statement)) return true;
    if (ts.isVariableStatement(statement) && exported(statement)) {
      if (statement.declarationList.declarations.some((declaration) => (
        ts.isIdentifier(declaration.name)
        && declaration.name.text === name
        && Boolean(declaration.initializer)
      ))) return true;
    }
  }
  return false;
}

function sourceViolations(root: string): string[] {
  const violations: string[] = [];
  const hasClientToken = STORY_AUTHORIZATION_SOURCES.some((relativePath) => {
    const source = readFileSync(join(root, relativePath), 'utf8');
    return hasClientGeneratedStoryAuthorization(parse(relativePath, source));
  });
  if (hasClientToken) violations.push('ui:client-authorization-token');
  const missingComponent = CANONICAL_COMPONENTS.some(([relativePath, name]) => {
    const path = join(root, relativePath);
    if (!existsSync(path)) return true;
    return !hasExportedCanonicalComponent(parse(relativePath, readFileSync(path, 'utf8')), name);
  });
  if (missingComponent) violations.push('ui:missing-canonical-review-readiness-components');
  return violations;
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

  it('requires a real exported canonical component declaration', () => {
    const pseudo = parse('pseudo.tsx', `
      const text = 'ProductActionConfirmation';
      function ProductActionConfirmation() { return null; }
    `);
    const exact = parse('exact.tsx', `
      export function ProductActionConfirmation() { return null; }
    `);
    expect(hasExportedCanonicalComponent(pseudo, 'ProductActionConfirmation')).toBe(false);
    expect(hasExportedCanonicalComponent(exact, 'ProductActionConfirmation')).toBe(true);
  });

  it('closes client Story authorization and canonical component gaps', () => {
    // Intentional RED until Task 11 installs the server-token owner flow and components.
    expect(sourceViolations(repositoryRoot())).toEqual([]);
  });
});
