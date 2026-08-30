import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import ts from 'typescript';
import { describe, expect, it } from 'vitest';

const STORY_OWNER = 'web/src/components/InterviewStoryDrawer.tsx';
const CANONICAL_COMPONENTS = [
  ['web/src/features/reviewReadiness/ProductActionConfirmation.tsx', 'ProductActionConfirmation'],
  ['web/src/features/reviewReadiness/ReviewReadinessNextStep.tsx', 'ReviewReadinessNextStep'],
  ['web/src/features/reviewReadiness/ReadinessFeedbackAdvisory.tsx', 'ReadinessFeedbackAdvisory'],
] as const;

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
  return null;
}

function variableInitializers(sourceFile: ts.SourceFile): Map<string, ts.Expression> {
  const result = new Map<string, ts.Expression>();
  const visit = (node: ts.Node): void => {
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer) {
      result.set(node.name.text, node.initializer);
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return result;
}

function expressionGeneratesClientStoryToken(
  expression: ts.Expression,
  bindings: Map<string, ts.Expression>,
  seen = new Set<string>(),
): boolean {
  if (ts.isParenthesizedExpression(expression)
    || ts.isAsExpression(expression)
    || ts.isNonNullExpression(expression)) {
    return expressionGeneratesClientStoryToken(expression.expression, bindings, seen);
  }
  if (ts.isIdentifier(expression)) {
    if (seen.has(expression.text)) return false;
    const initializer = bindings.get(expression.text);
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

function hasClientGeneratedStoryAuthorization(sourceFile: ts.SourceFile): boolean {
  const bindings = variableInitializers(sourceFile);
  let violation = false;
  const visit = (node: ts.Node): void => {
    if (violation) return;
    if (ts.isCallExpression(node)
      && ts.isIdentifier(node.expression)
      && node.expression.text === 'confirmInterviewStoryProposal') {
      const input = node.arguments[1];
      if (input && ts.isObjectLiteralExpression(input)) {
        for (const property of input.properties) {
          if (ts.isPropertyAssignment(property)
            && propertyName(property.name) === 'confirmation_token'
            && expressionGeneratesClientStoryToken(property.initializer, bindings)) {
            violation = true;
            return;
          }
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
  const storyPath = join(root, STORY_OWNER);
  const storySource = readFileSync(storyPath, 'utf8');
  if (hasClientGeneratedStoryAuthorization(parse(STORY_OWNER, storySource))) {
    violations.push('ui:client-authorization-token');
  }
  const missingComponent = CANONICAL_COMPONENTS.some(([relativePath, name]) => {
    const path = join(root, relativePath);
    if (!existsSync(path)) return true;
    return !hasExportedCanonicalComponent(parse(relativePath, readFileSync(path, 'utf8')), name);
  });
  if (missingComponent) violations.push('ui:missing-canonical-review-readiness-components');
  return violations;
}

describe('review readiness mechanical gate', () => {
  it('ignores comments, strings, and server-issued confirmation tokens', () => {
    const pseudo = parse('pseudo.tsx', `
      // confirmInterviewStoryProposal(id, { confirmation_token: key('story-confirm') });
      const text = "confirmation_token: crypto.randomUUID()";
      confirmInterviewStoryProposal(id, { confirmation_token: draft.serverConfirmationToken });
    `);
    expect(hasClientGeneratedStoryAuthorization(pseudo)).toBe(false);
  });

  it('detects a client-generated token only when it authorizes Story confirmation', () => {
    const unsafe = parse('unsafe.tsx', `
      const token = draft.confirmationToken ?? key('story-confirm');
      confirmInterviewStoryProposal(id, { confirmation_token: token });
    `);
    expect(hasClientGeneratedStoryAuthorization(unsafe)).toBe(true);
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
