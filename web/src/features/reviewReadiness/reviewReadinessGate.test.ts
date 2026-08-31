import { execFileSync } from 'node:child_process';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, posix, relative } from 'node:path';
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
  | { kind: 'http-object' }
  | { kind: 'http-post-function' }
  | { kind: 'object-assign-function' }
  | { kind: 'object-define-property-function' }
  | { kind: 'reflect-set-function' }
  | { kind: 'bound-function'; target: AbstractValue; arguments: AbstractValue[] }
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
const HTTP_OBJECT: AbstractValue = { kind: 'http-object' };
const HTTP_POST_FUNCTION: AbstractValue = { kind: 'http-post-function' };
const OBJECT_ASSIGN_FUNCTION: AbstractValue = { kind: 'object-assign-function' };
const OBJECT_DEFINE_PROPERTY_FUNCTION: AbstractValue = { kind: 'object-define-property-function' };
const REFLECT_SET_FUNCTION: AbstractValue = { kind: 'reflect-set-function' };
const BUILTIN_OBJECT: AbstractValue = {
  kind: 'object',
  properties: new Map<string, AbstractValue>([
    ['assign', OBJECT_ASSIGN_FUNCTION],
    ['defineProperty', OBJECT_DEFINE_PROPERTY_FUNCTION],
  ]),
  unknownProperties: false,
};
const BUILTIN_REFLECT: AbstractValue = {
  kind: 'object',
  properties: new Map<string, AbstractValue>([
    ['set', REFLECT_SET_FUNCTION],
    ['defineProperty', OBJECT_DEFINE_PROPERTY_FUNCTION],
  ]),
  unknownProperties: false,
};

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

function walkStoryNode(node: ts.Node, visit: (node: ts.Node) => void): void {
  visit(node);
  ts.forEachChild(node, (child) => walkStoryNode(child, visit));
}

function evaluatedPropertyName(node: ts.PropertyName, scope: LexicalScope): string | null {
  const direct = propertyName(node);
  if (direct !== null) return direct;
  if (!ts.isComputedPropertyName(node)) return null;
  const value = evaluateExpression(node.expression, scope);
  return value.kind === 'string' ? value.text : null;
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
  if (left.kind === 'bound-function' && right.kind === 'bound-function'
    && left.target === right.target && left.arguments.length === right.arguments.length) return left;
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
  if (owner.kind === 'http-object' && name === 'post') return HTTP_POST_FUNCTION;
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
      { kind: 'local-function', node: statement, closure: scope },
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
    if (ts.isExpressionStatement(statement)) {
      evaluateExpression(statement.expression, scope, activeFunctions);
      continue;
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
    const argument = arguments_[index];
    declareBinding(
      parameter.name,
      argument
        ? evaluateExpression(argument, callerScope, activeFunctions)
        : UNKNOWN,
      functionScope,
      activeFunctions,
    );
  });
  if (!value.node.body) return UNKNOWN;
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
    return expression.text === 'Object'
        ? BUILTIN_OBJECT
        : expression.text === 'Reflect'
          ? BUILTIN_REFLECT
          : expression.text === 'http'
            ? HTTP_OBJECT
            : UNKNOWN;
  }
  if (ts.isStringLiteral(expression) || ts.isNoSubstitutionTemplateLiteral(expression)) {
    return { kind: 'string', text: expression.text };
  }
  if (ts.isTemplateExpression(expression)) {
    let text = expression.head.text;
    for (const span of expression.templateSpans) {
      const value = evaluateExpression(span.expression, scope, activeFunctions);
      text += (value.kind === 'string' ? value.text : '${}') + span.literal.text;
    }
    return { kind: 'string', text };
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
  if (ts.isElementAccessExpression(expression) && expression.argumentExpression) {
    const key = evaluateExpression(expression.argumentExpression, scope, activeFunctions);
    if (key.kind === 'string') {
      return propertyValue(
        evaluateExpression(expression.expression, scope, activeFunctions),
        key.text,
      );
    }
  }
  if (ts.isBinaryExpression(expression)) {
    if (expression.operatorToken.kind === ts.SyntaxKind.EqualsToken) {
      const value = evaluateExpression(expression.right, scope, activeFunctions);
      if (ts.isIdentifier(expression.left)) {
        scope.assign(expression.left.text, value);
      } else if (ts.isPropertyAccessExpression(expression.left)) {
        const owner = evaluateExpression(expression.left.expression, scope, activeFunctions);
        if (owner.kind === 'object') owner.properties.set(expression.left.name.text, value);
      } else if (ts.isElementAccessExpression(expression.left)
        && expression.left.argumentExpression) {
        const owner = evaluateExpression(expression.left.expression, scope, activeFunctions);
        const key = evaluateExpression(expression.left.argumentExpression, scope, activeFunctions);
        if (owner.kind === 'object' && key.kind === 'string') owner.properties.set(key.text, value);
      }
      return value;
    }
    if (expression.operatorToken.kind === ts.SyntaxKind.PlusToken) {
      const left = evaluateExpression(expression.left, scope, activeFunctions);
      const right = evaluateExpression(expression.right, scope, activeFunctions);
      if (left.kind === 'string' && right.kind === 'string') {
        return { kind: 'string', text: left.text + right.text };
      }
      return UNKNOWN;
    }
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
        const name = evaluatedPropertyName(property.name, scope);
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
    if (ts.isPropertyAccessExpression(expression.expression)
      && expression.expression.name.text === 'bind') {
      const target = evaluateExpression(expression.expression.expression, scope, activeFunctions);
      if (target.kind !== 'unknown') {
        return {
          kind: 'bound-function',
          target,
          arguments: expression.arguments.slice(1).map((argument) => (
            evaluateExpression(argument, scope, activeFunctions)
          )),
        };
      }
    }
    if (ts.isPropertyAccessExpression(expression.expression)
      && ts.isIdentifier(expression.expression.expression)
      && expression.expression.expression.text === 'Object'
      && expression.expression.name.text === 'assign'
      && expression.arguments.length > 0) {
      const target = evaluateExpression(expression.arguments[0], scope, activeFunctions);
      if (target.kind !== 'object') return UNKNOWN;
      for (const source of expression.arguments.slice(1)) {
        const value = evaluateExpression(source, scope, activeFunctions);
        if (value.kind !== 'object') {
          target.unknownProperties = true;
          continue;
        }
        for (const [name, nested] of value.properties) target.properties.set(name, nested);
        target.unknownProperties ||= value.unknownProperties;
      }
      return target;
    }
    if (ts.isPropertyAccessExpression(expression.expression)
      && ts.isIdentifier(expression.expression.expression)
      && expression.expression.expression.text === 'Reflect'
      && expression.expression.name.text === 'set'
      && expression.arguments.length >= 3) {
      const target = evaluateExpression(expression.arguments[0], scope, activeFunctions);
      const key = evaluateExpression(expression.arguments[1], scope, activeFunctions);
      const value = evaluateExpression(expression.arguments[2], scope, activeFunctions);
      if (target.kind === 'object' && key.kind === 'string') target.properties.set(key.text, value);
      return value;
    }
    const call = normalizedStoryCall(expression);
    let callee = evaluateExpression(call.callee, scope, activeFunctions);
    let argumentValues = call.arguments.map((argument) => evaluateExpression(argument, scope, activeFunctions));
    if (callee.kind === 'bound-function') {
      argumentValues = [...callee.arguments, ...argumentValues];
      callee = callee.target;
    }
    if (callee.kind === 'object-assign-function' && argumentValues.length > 0) {
      const target = argumentValues[0];
      if (target.kind !== 'object') return UNKNOWN;
      for (const value of argumentValues.slice(1)) {
        if (value.kind !== 'object') { target.unknownProperties = true; continue; }
        for (const [name, nested] of value.properties) target.properties.set(name, nested);
        target.unknownProperties ||= value.unknownProperties;
      }
      return target;
    }
    if ((callee.kind === 'reflect-set-function' || callee.kind === 'object-define-property-function')
      && argumentValues.length >= 3) {
      const target = argumentValues[0];
      const key = argumentValues[1];
      let value = argumentValues[2];
      if (callee.kind === 'object-define-property-function') value = propertyValue(value, 'value');
      if (target.kind === 'object' && key.kind === 'string') target.properties.set(key.text, value);
      return value;
    }
    if (callee.kind === 'server-response-function') return SERVER_RESPONSE;
    if (callee.kind === 'local-function') {
      return evaluateLocalFunction(
        callee,
        call.arguments,
        scope,
        activeFunctions,
      );
    }
  }
  return UNKNOWN;
}

function hasUnsafeConfirmationToken(value: AbstractValue): boolean {
  if (value.kind !== 'object') return true;
  const token = value.properties.get('confirmation_token');
  return token === undefined ? value.unknownProperties : token.kind !== 'server-token';
}

function callIsStoryConfirmHttpPost(node: ts.CallExpression, scope: LexicalScope): boolean {
  const call = normalizedStoryCall(node);
  const callee = evaluateExpression(call.callee, scope);
  const method = ts.isPropertyAccessExpression(call.callee)
    ? call.callee.name.text
    : ts.isElementAccessExpression(call.callee) && call.callee.argumentExpression
      ? evaluateExpression(call.callee.argumentExpression, scope)
      : UNKNOWN;
  const isPost = typeof method === 'string'
    ? method === 'post'
    : method.kind === 'string' && method.text === 'post';
  if ((!isPost && callee.kind !== 'http-post-function') || call.arguments.length < 2) return false;
  const endpoint = evaluateExpression(call.arguments[0], scope);
  return endpoint.kind === 'string'
    && endpoint.text.includes('interview-story-proposals')
    && endpoint.text.includes('/confirm');
}

function storyObjectProperty(
  object: ts.ObjectLiteralExpression,
  name: string,
): ts.Expression | null {
  for (const property of [...object.properties].reverse()) {
    if (ts.isPropertyAssignment(property) && propertyName(property.name) === name) return property.initializer;
    if (ts.isShorthandPropertyAssignment(property) && property.name.text === name) return property.name;
  }
  return null;
}

function storyObjectInitializer(
  node: ts.Expression,
  before = node.getStart(node.getSourceFile()),
  active = new Set<string>(),
): ts.ObjectLiteralExpression | null {
  if (ts.isParenthesizedExpression(node) || ts.isAsExpression(node)
    || ts.isSatisfiesExpression(node) || ts.isNonNullExpression(node)) {
    return storyObjectInitializer(node.expression, before, active);
  }
  if (ts.isObjectLiteralExpression(node)) return node;
  if (!ts.isIdentifier(node) || active.has(node.text)) return null;
  let initializer: ts.Expression | null = null;
  let position = -1;
  walkStoryNode(node.getSourceFile(), (candidate) => {
    if (ts.isVariableDeclaration(candidate) && ts.isIdentifier(candidate.name)
      && candidate.name.text === node.text && candidate.initializer
      && candidate.getStart(node.getSourceFile()) < before
      && candidate.getStart(node.getSourceFile()) > position) {
      position = candidate.getStart(node.getSourceFile());
      initializer = candidate.initializer;
    }
  });
  return initializer
    ? storyObjectInitializer(initializer, position, new Set(active).add(node.text))
    : null;
}

type StoryMutationBuiltin = 'Object.assign' | 'Object.defineProperty';
const storyMutationBuiltinsCache = new WeakMap<ts.SourceFile, Map<string, StoryMutationBuiltin>>();

function storyMutationBuiltin(node: ts.CallExpression): StoryMutationBuiltin | null {
  const sourceFile = node.getSourceFile();
  let aliases = storyMutationBuiltinsCache.get(sourceFile);
  const direct = (expression: ts.Expression, known: Map<string, StoryMutationBuiltin>): StoryMutationBuiltin | null => {
    if (ts.isIdentifier(expression)) return known.get(expression.text) ?? null;
    if ((ts.isPropertyAccessExpression(expression) || ts.isElementAccessExpression(expression))
      && ts.isIdentifier(expression.expression) && expression.expression.text === 'Object') {
      const member = storyAdapterMember(expression);
      return member === 'assign' || member === 'defineProperty' ? `Object.${member}` : null;
    }
    return null;
  };
  if (!aliases) {
    aliases = new Map();
    let changed = true;
    while (changed) {
      changed = false;
      walkStoryNode(sourceFile, (candidate) => {
        let name: string | null = null;
        let expression: ts.Expression | null = null;
        if (ts.isVariableDeclaration(candidate) && ts.isIdentifier(candidate.name) && candidate.initializer) {
          name = candidate.name.text;
          expression = candidate.initializer;
        }
        if (ts.isBinaryExpression(candidate) && candidate.operatorToken.kind === ts.SyntaxKind.EqualsToken
          && ts.isIdentifier(candidate.left)) {
          name = candidate.left.text;
          expression = candidate.right;
        }
        const builtin = expression ? direct(expression, aliases!) : null;
        if (name && builtin && aliases!.get(name) !== builtin) {
          aliases!.set(name, builtin);
          changed = true;
        }
      });
    }
    storyMutationBuiltinsCache.set(sourceFile, aliases);
  }
  return direct(node.expression, aliases);
}

function storyObjectPropertyAt(
  expression: ts.Expression,
  name: string,
  before: number,
): ts.Expression | null {
  let result = storyObjectInitializer(expression, before);
  let value = result ? storyObjectProperty(result, name) : null;
  let position = value?.getStart(expression.getSourceFile()) ?? -1;
  if (!ts.isIdentifier(expression)) return value;
  walkStoryNode(expression.getSourceFile(), (candidate) => {
    if (ts.isBinaryExpression(candidate)
      && candidate.operatorToken.kind === ts.SyntaxKind.EqualsToken
      && (ts.isPropertyAccessExpression(candidate.left) || ts.isElementAccessExpression(candidate.left))
      && ts.isIdentifier(candidate.left.expression)
      && candidate.left.expression.text === expression.text
      && candidate.getStart(expression.getSourceFile()) < before
      && candidate.getStart(expression.getSourceFile()) > position) {
      const property = ts.isPropertyAccessExpression(candidate.left)
        ? candidate.left.name.text
        : candidate.left.argumentExpression
          && (ts.isStringLiteral(candidate.left.argumentExpression)
            || ts.isNoSubstitutionTemplateLiteral(candidate.left.argumentExpression))
          ? candidate.left.argumentExpression.text
          : null;
      if (property === name) {
        position = candidate.getStart(expression.getSourceFile());
        value = candidate.right;
      }
    }
    if (!ts.isCallExpression(candidate) || candidate.getStart(expression.getSourceFile()) >= before
      || candidate.getStart(expression.getSourceFile()) <= position
      || !candidate.arguments[0] || !ts.isIdentifier(candidate.arguments[0])
      || candidate.arguments[0].text !== expression.text) return;
    const builtin = storyMutationBuiltin(candidate);
    if (builtin === 'Object.assign') {
      for (const sourceExpression of candidate.arguments.slice(1)) {
        const source = storyObjectInitializer(sourceExpression, candidate.getStart(expression.getSourceFile()));
        const property = source ? storyObjectProperty(source, name) : null;
        if (property) {
          position = candidate.getStart(expression.getSourceFile());
          value = property;
        }
      }
    }
    if (builtin === 'Object.defineProperty'
      && candidate.arguments[1] && candidate.arguments[2]
      && (ts.isStringLiteral(candidate.arguments[1])
        || ts.isNoSubstitutionTemplateLiteral(candidate.arguments[1]))
      && candidate.arguments[1].text === name) {
      const descriptor = storyObjectInitializer(candidate.arguments[2], candidate.getStart(expression.getSourceFile()));
      const property = descriptor ? storyObjectProperty(descriptor, 'value') : null;
      if (property) {
        position = candidate.getStart(expression.getSourceFile());
        value = property;
      }
    }
  });
  return value;
}

function storyAdapterConfirmationRequest(
  node: ts.CallExpression,
  scope: LexicalScope,
): AbstractValue | null {
  const call = normalizedStoryCall(node);
  if (callIsStoryConfirmHttpPost(node, scope)) {
    return call.arguments[1] ? evaluateExpression(call.arguments[1], scope) : UNKNOWN;
  }
  if (ts.isIdentifier(call.callee) && call.callee.text === 'fetch'
    && call.arguments[0] && call.arguments[1]) {
    const options = storyObjectInitializer(call.arguments[1]);
    if (!options) return null;
    const endpoint = evaluateExpression(call.arguments[0], scope);
    const methodNode = storyObjectPropertyAt(call.arguments[1], 'method', node.getStart(node.getSourceFile()));
    const method = methodNode ? evaluateExpression(methodNode, scope) : UNKNOWN;
    if (endpoint.kind !== 'string' || !endpoint.text.includes('interview-story-proposals')
      || !endpoint.text.includes('/confirm') || method.kind !== 'string' || method.text.toUpperCase() !== 'POST') return null;
    const body = storyObjectPropertyAt(call.arguments[1], 'body', node.getStart(node.getSourceFile()));
    if (body && ts.isCallExpression(body)
      && ts.isPropertyAccessExpression(body.expression)
      && ts.isIdentifier(body.expression.expression)
      && body.expression.expression.text === 'JSON'
      && body.expression.name.text === 'stringify'
      && body.arguments[0]) return evaluateExpression(body.arguments[0], scope);
    return body ? evaluateExpression(body, scope) : UNKNOWN;
  }
  if ((ts.isPropertyAccessExpression(call.callee) || ts.isElementAccessExpression(call.callee))
    && storyAdapterMember(call.callee) === 'request'
    && call.arguments[0]) {
    const config = storyObjectInitializer(call.arguments[0]);
    if (!config) return null;
    const urlNode = storyObjectPropertyAt(call.arguments[0], 'url', node.getStart(node.getSourceFile()));
    const methodNode = storyObjectPropertyAt(call.arguments[0], 'method', node.getStart(node.getSourceFile()));
    const url = urlNode ? evaluateExpression(urlNode, scope) : UNKNOWN;
    const method = methodNode ? evaluateExpression(methodNode, scope) : UNKNOWN;
    if (url.kind !== 'string' || !url.text.includes('interview-story-proposals')
      || !url.text.includes('/confirm') || method.kind !== 'string' || method.text.toUpperCase() !== 'POST') return null;
    const data = storyObjectPropertyAt(call.arguments[0], 'data', node.getStart(node.getSourceFile()))
      ?? storyObjectPropertyAt(call.arguments[0], 'body', node.getStart(node.getSourceFile()));
    return data ? evaluateExpression(data, scope) : UNKNOWN;
  }
  return null;
}

type NormalizedStoryCall = { callee: ts.Expression; arguments: readonly ts.Expression[] };

const storyCallArraysCache = new WeakMap<ts.SourceFile, Map<string, ts.ArrayLiteralExpression>>();
const storyAdapterStringsCache = new WeakMap<ts.SourceFile, Map<string, string>>();

function storyStaticCallArray(node: ts.Expression): ts.ArrayLiteralExpression | null {
  if (ts.isParenthesizedExpression(node) || ts.isAsExpression(node) || ts.isSatisfiesExpression(node)) {
    return storyStaticCallArray(node.expression);
  }
  if (ts.isArrayLiteralExpression(node)) return node;
  if (!ts.isIdentifier(node)) return null;
  const sourceFile = node.getSourceFile();
  let arrays = storyCallArraysCache.get(sourceFile);
  if (!arrays) {
    arrays = new Map();
    walkStoryNode(sourceFile, (candidate) => {
      if (ts.isVariableDeclaration(candidate) && ts.isIdentifier(candidate.name) && candidate.initializer) {
        let initializer = candidate.initializer;
        while (ts.isParenthesizedExpression(initializer) || ts.isAsExpression(initializer)
          || ts.isSatisfiesExpression(initializer)) initializer = initializer.expression;
        if (ts.isArrayLiteralExpression(initializer)) arrays!.set(candidate.name.text, initializer);
      }
    });
    storyCallArraysCache.set(sourceFile, arrays);
  }
  return arrays.get(node.text) ?? null;
}

function storyAdapterMember(node: ts.Expression): string | null {
  if (ts.isPropertyAccessExpression(node)) return node.name.text;
  if (ts.isElementAccessExpression(node) && node.argumentExpression
    && (ts.isStringLiteral(node.argumentExpression) || ts.isNoSubstitutionTemplateLiteral(node.argumentExpression))) {
    return node.argumentExpression.text;
  }
  if (ts.isElementAccessExpression(node) && node.argumentExpression
    && ts.isIdentifier(node.argumentExpression)) {
    const sourceFile = node.getSourceFile();
    let strings = storyAdapterStringsCache.get(sourceFile);
    if (!strings) {
      strings = new Map();
      walkStoryNode(sourceFile, (candidate) => {
        if (ts.isVariableDeclaration(candidate) && ts.isIdentifier(candidate.name)
          && candidate.initializer
          && (ts.isStringLiteral(candidate.initializer) || ts.isNoSubstitutionTemplateLiteral(candidate.initializer))) {
          strings!.set(candidate.name.text, candidate.initializer.text);
        }
      });
      storyAdapterStringsCache.set(sourceFile, strings);
    }
    return strings.get(node.argumentExpression.text) ?? null;
  }
  return null;
}

function expandedStoryArguments(arguments_: readonly ts.Expression[]): readonly ts.Expression[] {
  const expanded: ts.Expression[] = [];
  for (const argument of arguments_) {
    if (ts.isSpreadElement(argument)) {
      const array = storyStaticCallArray(argument.expression);
      if (array) {
        expanded.push(...array.elements);
        continue;
      }
    }
    expanded.push(argument);
  }
  return expanded;
}

function normalizedStoryCall(node: ts.CallExpression): NormalizedStoryCall {
  const adapter = storyAdapterMember(node.expression);
  if ((ts.isPropertyAccessExpression(node.expression) || ts.isElementAccessExpression(node.expression))
    && (adapter === 'call' || adapter === 'apply')) {
    if (adapter === 'call') {
      return { callee: node.expression.expression, arguments: expandedStoryArguments(node.arguments.slice(1)) };
    }
    const list = node.arguments[1] ? storyStaticCallArray(node.arguments[1]) : null;
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
    && storyStaticCallArray(node.arguments[2])) {
    return { callee: node.arguments[0], arguments: [...storyStaticCallArray(node.arguments[2])!.elements] };
  }
  return { callee: node.expression, arguments: expandedStoryArguments(node.arguments) };
}

function resolveSourceModule(
  sources: Map<string, string>,
  importerPath: string,
  moduleName: string,
): string | null {
  const importer = importerPath.replace(/\\/g, '/');
  const base = moduleName.startsWith('@/')
    ? `web/src/${moduleName.slice(2)}`
    : moduleName.startsWith('.')
      ? posix.join(posix.dirname(importer), moduleName.replace(/\\/g, '/'))
      : null;
  if (base === null) return null;
  const normalizedBase = posix.normalize(base);
  if (isStoryServiceModule(normalizedBase)) return `${normalizedBase}.ts`;
  const candidates = [normalizedBase, `${normalizedBase}.ts`, `${normalizedBase}.tsx`, `${normalizedBase}/index.ts`, `${normalizedBase}/index.tsx`];
  return candidates.find((candidate) => [...sources.keys()].some(
    (path) => path.replace(/\\/g, '/') === candidate,
  )) ?? null;
}

const storyExportCache = new WeakMap<Map<string, string>, Map<string, AbstractValue>>();

function resolveStoryExport(
  sources: Map<string, string>,
  sourcePath: string,
  exportName: string,
  active = new Set<string>(),
): AbstractValue {
  if (active.size > 0) return resolveStoryExportUncached(sources, sourcePath, exportName, active);
  const cache = storyExportCache.get(sources) ?? new Map<string, AbstractValue>();
  storyExportCache.set(sources, cache);
  const key = `${sourcePath}:${exportName}`;
  const cached = cache.get(key);
  if (cached) return cached;
  const result = resolveStoryExportUncached(sources, sourcePath, exportName, active);
  cache.set(key, result);
  return result;
}

function resolveStoryExportUncached(
  sources: Map<string, string>,
  sourcePath: string,
  exportName: string,
  active = new Set<string>(),
): AbstractValue {
  const key = `${sourcePath}:${exportName}`;
  if (active.has(key)) return UNKNOWN;
  const nextActive = new Set(active).add(key);
  if (isStoryServiceModule(normalizeSourcePath(sourcePath))) {
    if (exportName === 'confirmInterviewStoryProposal') return STORY_CONFIRM_FUNCTION;
    if (isStoryServerResponseFunction(exportName)) return SERVER_RESPONSE_FUNCTION;
  }
  const source = [...sources.entries()].find(
    ([path]) => path.replace(/\\/g, '/') === sourcePath,
  )?.[1];
  if (source === undefined) return UNKNOWN;
  const sourceFile = parse(sourcePath, source);
  const importedLocals = new Map<string, { path: string; name: string }>();
  const confirmLocals = new Set<string>();
  const confirmNamespaces = new Set<string>();
  for (const statement of sourceFile.statements) {
    if (!ts.isImportDeclaration(statement) || !ts.isStringLiteral(statement.moduleSpecifier)) continue;
    const target = resolveSourceModule(sources, sourcePath, statement.moduleSpecifier.text);
    if (!target || !statement.importClause?.namedBindings) continue;
    if (ts.isNamedImports(statement.importClause.namedBindings)) {
      for (const specifier of statement.importClause.namedBindings.elements) {
        const imported = {
          path: target,
          name: specifier.propertyName?.text ?? specifier.name.text,
        };
        importedLocals.set(specifier.name.text, imported);
        if (resolveStoryExport(sources, imported.path, imported.name, nextActive).kind === 'story-confirm-function') {
          confirmLocals.add(specifier.name.text);
        }
      }
    } else if (propertyValue(
      resolveStoryNamespace(sources, target),
      'confirmInterviewStoryProposal',
    ).kind === 'story-confirm-function') {
      confirmNamespaces.add(statement.importClause.namedBindings.name.text);
    }
  }
  const delegatesCallerRequest = (
    node: ts.CallExpression,
    parameters: readonly ts.ParameterDeclaration[],
  ): boolean => {
    let confirm = ts.isIdentifier(node.expression) && confirmLocals.has(node.expression.text);
    if ((ts.isPropertyAccessExpression(node.expression) || ts.isElementAccessExpression(node.expression))
      && ts.isIdentifier(node.expression.expression)
      && confirmNamespaces.has(node.expression.expression.text)) {
      const member = ts.isPropertyAccessExpression(node.expression)
        ? node.expression.name.text
        : node.expression.argumentExpression && ts.isStringLiteral(node.expression.argumentExpression)
          ? node.expression.argumentExpression.text
          : null;
      confirm ||= member === 'confirmInterviewStoryProposal';
    }
    if (!confirm || !node.arguments[1]) return false;
    const parameterNames = new Set(parameters.flatMap((parameter) => (
      ts.isIdentifier(parameter.name) ? [parameter.name.text] : []
    )));
    let forwards = false;
    walkStoryNode(node.arguments[1], (child) => {
      if (ts.isIdentifier(child) && parameterNames.has(child.text)) forwards = true;
    });
    return forwards;
  };
  let wrappersChanged = true;
  while (wrappersChanged) {
    wrappersChanged = false;
    for (const statement of sourceFile.statements) {
      let name: string | null = null;
      let body: ts.Node | null = null;
      if (ts.isFunctionDeclaration(statement) && statement.name && statement.body) {
        name = statement.name.text;
        body = statement.body;
      }
      if (ts.isVariableStatement(statement)) {
        for (const declaration of statement.declarationList.declarations) {
          if (ts.isIdentifier(declaration.name) && declaration.initializer
            && (ts.isArrowFunction(declaration.initializer) || ts.isFunctionExpression(declaration.initializer))) {
            const initializer = declaration.initializer;
            let delegates = false;
            walkStoryNode(initializer.body, (node) => {
              if (ts.isCallExpression(node)
                && delegatesCallerRequest(node, initializer.parameters)) delegates = true;
            });
            if (delegates && !confirmLocals.has(declaration.name.text)) {
              confirmLocals.add(declaration.name.text);
              wrappersChanged = true;
            }
          }
        }
      }
      if (name && body) {
        let delegates = false;
        walkStoryNode(body, (node) => {
          if (ts.isCallExpression(node) && ts.isFunctionDeclaration(statement)
            && delegatesCallerRequest(node, statement.parameters)) delegates = true;
        });
        if (delegates && !confirmLocals.has(name)) {
          confirmLocals.add(name);
          wrappersChanged = true;
        }
      }
    }
  }
  for (const statement of sourceFile.statements) {
    if (!ts.isExportDeclaration(statement) || !statement.exportClause) continue;
    const target = statement.moduleSpecifier && ts.isStringLiteral(statement.moduleSpecifier)
      ? resolveSourceModule(sources, sourcePath, statement.moduleSpecifier.text)
      : null;
    if (ts.isNamespaceExport(statement.exportClause)) {
      if (statement.exportClause.name.text === exportName && target) {
        return resolveStoryNamespace(sources, target);
      }
      continue;
    }
    if (!ts.isNamedExports(statement.exportClause)) continue;
    for (const specifier of statement.exportClause.elements) {
      if (specifier.name.text !== exportName) continue;
      const original = specifier.propertyName?.text ?? specifier.name.text;
      if (target) return resolveStoryExport(sources, target, original, nextActive);
      const imported = importedLocals.get(original);
      if (imported) return resolveStoryExport(sources, imported.path, imported.name, nextActive);
      if (confirmLocals.has(original)) return STORY_CONFIRM_FUNCTION;
    }
  }
  for (const statement of sourceFile.statements) {
    const exported = ts.canHaveModifiers(statement)
      && Boolean(ts.getModifiers(statement)?.some((modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword));
    const defaulted = ts.canHaveModifiers(statement)
      && Boolean(ts.getModifiers(statement)?.some((modifier) => modifier.kind === ts.SyntaxKind.DefaultKeyword));
    if (exported && ts.isFunctionDeclaration(statement) && statement.name
      && confirmLocals.has(statement.name.text)
      && (statement.name.text === exportName || (defaulted && exportName === 'default'))) {
      return STORY_CONFIRM_FUNCTION;
    }
    if (exported && ts.isVariableStatement(statement)) {
      for (const declaration of statement.declarationList.declarations) {
        if (ts.isIdentifier(declaration.name) && declaration.name.text === exportName
          && confirmLocals.has(declaration.name.text)) return STORY_CONFIRM_FUNCTION;
      }
    }
    if (ts.isExportAssignment(statement) && !statement.isExportEquals && exportName === 'default'
      && ts.isIdentifier(statement.expression) && confirmLocals.has(statement.expression.text)) {
      return STORY_CONFIRM_FUNCTION;
    }
  }
  return UNKNOWN;
}

function resolveStoryNamespace(
  sources: Map<string, string>,
  sourcePath: string,
): AbstractValue {
  if (isStoryServiceModule(normalizeSourcePath(sourcePath))) return STORY_SERVICE_NAMESPACE;
  const source = sources.get(sourcePath);
  if (source === undefined) return UNKNOWN;
  const properties = new Map<string, AbstractValue>();
  const sourceFile = parse(sourcePath, source);
  for (const statement of sourceFile.statements) {
    if (ts.isExportDeclaration(statement) && statement.exportClause) {
      if (ts.isNamespaceExport(statement.exportClause)) {
        const name = statement.exportClause.name.text;
        const value = resolveStoryExport(sources, sourcePath, name);
        if (value.kind !== 'unknown') properties.set(name, value);
      } else if (ts.isNamedExports(statement.exportClause)) {
        for (const specifier of statement.exportClause.elements) {
          const value = resolveStoryExport(sources, sourcePath, specifier.name.text);
          if (value.kind !== 'unknown') properties.set(specifier.name.text, value);
        }
      }
    }
    const exported = ts.canHaveModifiers(statement)
      && Boolean(ts.getModifiers(statement)?.some((modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword));
    if (exported && ts.isFunctionDeclaration(statement) && statement.name) {
      const value = resolveStoryExport(sources, sourcePath, statement.name.text);
      if (value.kind !== 'unknown') properties.set(statement.name.text, value);
    }
    if (exported && ts.isVariableStatement(statement)) {
      for (const declaration of statement.declarationList.declarations) {
        if (!ts.isIdentifier(declaration.name)) continue;
        const value = resolveStoryExport(sources, sourcePath, declaration.name.text);
        if (value.kind !== 'unknown') properties.set(declaration.name.text, value);
      }
    }
  }
  return properties.size > 0
    ? { kind: 'object', properties, unknownProperties: false }
    : UNKNOWN;
}

function hasClientGeneratedStoryAuthorization(
  sourceFile: ts.SourceFile,
  context?: { path: string; sources: Map<string, string> },
): boolean {
  let violation = false;
  const activeVisits = new Set<ts.Node>();
  const trustedStoryImplementation = isStoryServiceModule(normalizeSourcePath(
    context?.path ?? sourceFile.fileName,
  ).replace(/\.ts$/, ''));

  const visit = (node: ts.Node, scope: LexicalScope): void => {
    if (violation) return;
    if (ts.isSourceFile(node)) {
      declareFunctionDeclarations(node.statements, scope);
      if (trustedStoryImplementation) {
        scope.assign('confirmInterviewStoryProposal', STORY_CONFIRM_FUNCTION);
      }
      for (const statement of node.statements) visit(statement, scope);
      return;
    }
    if (ts.isImportDeclaration(node)) {
      const clause = node.importClause;
      const moduleName = ts.isStringLiteral(node.moduleSpecifier)
        ? node.moduleSpecifier.text
        : '';
      const storyService = isStoryServiceModule(moduleName);
      if (clause?.name) {
        const target = context && ts.isStringLiteral(node.moduleSpecifier)
          ? resolveSourceModule(context.sources, context.path, node.moduleSpecifier.text)
          : null;
        const resolved = target && context
          ? resolveStoryExport(context.sources, target, 'default')
          : UNKNOWN;
        scope.declare(clause.name.text, resolved);
      }
      if (clause?.namedBindings && ts.isNamedImports(clause.namedBindings)) {
        for (const specifier of clause.namedBindings.elements) {
          const imported = specifier.propertyName?.text ?? specifier.name.text;
          const target = context && ts.isStringLiteral(node.moduleSpecifier)
            ? resolveSourceModule(context.sources, context.path, node.moduleSpecifier.text)
            : null;
          const resolved = target && context
            ? resolveStoryExport(context.sources, target, imported)
            : UNKNOWN;
          scope.declare(
            specifier.name.text,
            resolved.kind !== 'unknown'
              ? resolved
              : storyService && imported === 'confirmInterviewStoryProposal'
              ? STORY_CONFIRM_FUNCTION
              : storyService && isStoryServerResponseFunction(imported)
                ? SERVER_RESPONSE_FUNCTION
                : UNKNOWN,
          );
        }
      } else if (clause?.namedBindings && ts.isNamespaceImport(clause.namedBindings)) {
        const target = context && ts.isStringLiteral(node.moduleSpecifier)
          ? resolveSourceModule(context.sources, context.path, node.moduleSpecifier.text)
          : null;
        const resolved = target && context
          ? resolveStoryNamespace(context.sources, target)
          : UNKNOWN;
        scope.declare(
          clause.namedBindings.name.text,
          resolved.kind !== 'unknown' ? resolved : storyService ? STORY_SERVICE_NAMESPACE : UNKNOWN,
        );
      }
      return;
    }
    if (ts.isFunctionDeclaration(node)) {
      if (node.name) {
        scope.declare(
          node.name.text,
          trustedStoryImplementation && node.name.text === 'confirmInterviewStoryProposal'
            ? STORY_CONFIRM_FUNCTION
            : { kind: 'local-function', node, closure: scope },
        );
      }
      if (node.body && trustedStoryImplementation && node.name?.text === 'confirmInterviewStoryProposal') {
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
      if (node.initializer && !ts.isFunctionExpression(node.initializer)
        && !ts.isArrowFunction(node.initializer)) visit(node.initializer, scope);
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
      evaluateExpression(node, scope);
      return;
    }
    if (ts.isCallExpression(node)) {
      evaluateExpression(node, scope);
      const call = normalizedStoryCall(node);
      let callee = evaluateExpression(call.callee, scope);
      let argumentValues = call.arguments.map((argument) => evaluateExpression(argument, scope));
      if (callee.kind === 'bound-function') {
        argumentValues = [...callee.arguments, ...argumentValues];
        callee = callee.target;
      }
      const request = argumentValues[1] ?? UNKNOWN;
      const adapterRequest = storyAdapterConfirmationRequest(node, scope);
      if (((callee.kind === 'story-confirm-function' && hasUnsafeConfirmationToken(request))
        || (adapterRequest !== null && hasUnsafeConfirmationToken(adapterRequest)))) {
        violation = true;
        return;
      }
      if (callee.kind === 'local-function' && callee.node.body && !activeVisits.has(callee.node)) {
        activeVisits.add(callee.node);
        const functionScope = new LexicalScope(callee.closure);
        callee.node.parameters.forEach((parameter, index) => {
          declareBinding(
            parameter.name,
            call.arguments[index] ? evaluateExpression(call.arguments[index], scope) : UNKNOWN,
            functionScope,
          );
        });
        visit(callee.node.body, functionScope);
        activeVisits.delete(callee.node);
        if (violation) return;
      }
      visit(call.callee, scope);
      for (const argument of call.arguments) visit(argument, scope);
      return;
    }
    ts.forEachChild(node, (child) => visit(child, scope));
  };

  visit(sourceFile, new LexicalScope());
  return violation;
}

function storyStaticBoolean(node: ts.Expression): boolean | null {
  const expression = ts.isParenthesizedExpression(node) ? node.expression : node;
  if (expression.kind === ts.SyntaxKind.TrueKeyword) return true;
  if (expression.kind === ts.SyntaxKind.FalseKeyword) return false;
  if (ts.isNumericLiteral(expression)) return Number(expression.text) !== 0;
  return null;
}

function storyStatementTerminates(statement: ts.Statement): boolean {
  if (ts.isReturnStatement(statement) || ts.isThrowStatement(statement)) return true;
  if (ts.isBlock(statement)) return statement.statements.some(storyStatementTerminates);
  if (ts.isIfStatement(statement)) {
    const condition = storyStaticBoolean(statement.expression);
    if (condition === true) return storyStatementTerminates(statement.thenStatement);
    if (condition === false) return Boolean(statement.elseStatement
      && storyStatementTerminates(statement.elseStatement));
    return storyStatementTerminates(statement.thenStatement)
      && Boolean(statement.elseStatement && storyStatementTerminates(statement.elseStatement));
  }
  if (ts.isTryStatement(statement) && statement.finallyBlock) {
    return statement.finallyBlock.statements.some(storyStatementTerminates);
  }
  return false;
}

function storyNodeStaticallyReachable(node: ts.Node): boolean {
  let current = node;
  while (current.parent) {
    const parent = current.parent;
    if (ts.isIfStatement(parent)) {
      const condition = storyStaticBoolean(parent.expression);
      if (condition === false && parent.thenStatement === current) return false;
      if (condition === true && parent.elseStatement === current) return false;
    }
    if (ts.isConditionalExpression(parent)) {
      const condition = storyStaticBoolean(parent.condition);
      if (condition === false && parent.whenTrue === current) return false;
      if (condition === true && parent.whenFalse === current) return false;
    }
    if (ts.isBinaryExpression(parent) && parent.right === current) {
      const condition = storyStaticBoolean(parent.left);
      if (parent.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken
        && condition === false) return false;
      if (parent.operatorToken.kind === ts.SyntaxKind.BarBarToken
        && condition === true) return false;
    }
    if (ts.isBlock(parent)) {
      const statement = parent.statements.find((candidate) => (
        current === candidate || (current.pos >= candidate.pos && current.end <= candidate.end)
      ));
      if (statement) {
        const index = parent.statements.indexOf(statement);
        if (parent.statements.slice(0, index).some(storyStatementTerminates)) return false;
      }
    }
    current = parent;
  }
  return true;
}

function storyNodeBelongsToExportedOwner(node: ts.Node): boolean {
  let current: ts.Node | undefined = node.parent;
  while (current && !ts.isSourceFile(current)) {
    if (ts.isFunctionDeclaration(current)) {
      const exported = Boolean(ts.getModifiers(current)?.some((modifier) => (
        modifier.kind === ts.SyntaxKind.ExportKeyword || modifier.kind === ts.SyntaxKind.DefaultKeyword
      )));
      if (exported) return true;
      if (!current.name) return false;
      const helperName = current.name.text;
      const sourceFile = current.getSourceFile();
      let calledByExportedOwner = false;
      walkStoryNode(sourceFile, (candidate) => {
        if (calledByExportedOwner || !ts.isCallExpression(candidate)
          || !ts.isIdentifier(candidate.expression)
          || candidate.expression.text !== helperName
          || !storyNodeStaticallyReachable(candidate)) return;
        if (storyNodeBelongsToExportedOwner(candidate)) calledByExportedOwner = true;
      });
      return calledByExportedOwner;
    }
    if (ts.isArrowFunction(current) || ts.isFunctionExpression(current)) {
      const declaration = current.parent;
      const statement = ts.isVariableDeclaration(declaration)
        && ts.isVariableDeclarationList(declaration.parent)
        && ts.isVariableStatement(declaration.parent.parent)
        ? declaration.parent.parent
        : null;
      const exported = Boolean(statement && ts.getModifiers(statement)?.some((modifier) => (
        modifier.kind === ts.SyntaxKind.ExportKeyword
      )));
      if (exported) return true;
      if (!ts.isVariableDeclaration(declaration) || !ts.isIdentifier(declaration.name)) return false;
      const helperName = declaration.name.text;
      let calledByExportedOwner = false;
      walkStoryNode(current.getSourceFile(), (candidate) => {
        if (calledByExportedOwner || !ts.isCallExpression(candidate)
          || !ts.isIdentifier(candidate.expression) || candidate.expression.text !== helperName
          || (candidate.pos >= current!.pos && candidate.end <= current!.end)
          || !storyNodeStaticallyReachable(candidate)) return;
        if (storyNodeBelongsToExportedOwner(candidate)) calledByExportedOwner = true;
      });
      return calledByExportedOwner;
    }
    current = current.parent;
  }
  return true;
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
        if (storyNodeStaticallyReachable(child)) found = true;
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
        && (((ts.isArrowFunction(declaration.initializer!)
          || ts.isFunctionExpression(declaration.initializer!))
          && rendersJsx(declaration.initializer!))
          || (ts.isCallExpression(declaration.initializer!)
            && (ts.isPropertyAccessExpression(declaration.initializer!.expression)
              || ts.isElementAccessExpression(declaration.initializer!.expression))
            && /^(?:memo|forwardRef)$/.test(storyAdapterMember(declaration.initializer!.expression) ?? '')
            && Boolean(declaration.initializer!.arguments[0])
            && (ts.isArrowFunction(declaration.initializer!.arguments[0])
              || ts.isFunctionExpression(declaration.initializer!.arguments[0]))
            && rendersJsx(declaration.initializer!.arguments[0])))
      ))) return true;
    }
  }
  return false;
}

function normalizeSourcePath(path: string): string {
  return path.replace(/\\/g, '/').replace(/\.tsx?$/, '');
}

function isProductionWebSourcePath(path: string): boolean {
  const normalized = path.replace(/\\/g, '/');
  return /^web\/src\//.test(normalized)
    && /\.tsx?$/.test(normalized)
    && !/\.(?:test|spec)\.tsx?$/.test(normalized)
    && !/\.stories\.tsx?$/.test(normalized)
    && !/(?:^|\/)__(?:tests|fixtures)__(?:\/|$)/.test(normalized)
    && !/(?:^|\/)\.cache(?:\/|$)/.test(normalized)
    && !/(?:^|\/)node_modules(?:\/|$)/.test(normalized);
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
  const declarations = new Map<ts.Node, Set<string>>();
  const declare = (scope: ts.Node, name: string): void => {
    const names = declarations.get(scope) ?? new Set<string>();
    names.add(name);
    declarations.set(scope, names);
  };
  const isStoryFunctionScope = (node: ts.Node): node is ts.FunctionLikeDeclaration => (
    ts.isFunctionDeclaration(node) || ts.isFunctionExpression(node)
    || ts.isArrowFunction(node) || ts.isMethodDeclaration(node)
  );
  const storyScopes = (node: ts.Node): ts.Node[] => {
    const scopes: ts.Node[] = [];
    let current: ts.Node | undefined = node;
    while (current) {
      if (ts.isBlock(current) || ts.isSourceFile(current) || isStoryFunctionScope(current)) scopes.push(current);
      current = current.parent;
    }
    return scopes;
  };
  const storyBlockScope = (node: ts.Node): ts.Node => (
    storyScopes(node).find((scope) => ts.isBlock(scope) || ts.isSourceFile(scope)) ?? sourceFile
  );
  const storyBindingNames = (name: ts.BindingName): string[] => {
    if (ts.isIdentifier(name)) return [name.text];
    return name.elements.flatMap((element) => (
      ts.isOmittedExpression(element) ? [] : storyBindingNames(element.name)
    ));
  };
  walkStoryNode(sourceFile, (node) => {
    if (ts.isImportClause(node)) {
      if (node.name) declare(sourceFile, node.name.text);
      if (node.namedBindings && ts.isNamedImports(node.namedBindings)) {
        for (const specifier of node.namedBindings.elements) declare(sourceFile, specifier.name.text);
      }
      if (node.namedBindings && ts.isNamespaceImport(node.namedBindings)) declare(sourceFile, node.namedBindings.name.text);
    }
    if (ts.isVariableDeclaration(node)) {
      for (const name of storyBindingNames(node.name)) declare(storyBlockScope(node), name);
    }
    if (ts.isFunctionDeclaration(node) && node.name) declare(storyBlockScope(node.parent), node.name.text);
    if (isStoryFunctionScope(node)) {
      for (const parameter of node.parameters) {
        for (const name of storyBindingNames(parameter.name)) declare(node, name);
      }
    }
  });
  const bindingKey = (node: ts.Node, name: string): string => {
    const scope = storyScopes(node).find((candidate) => declarations.get(candidate)?.has(name));
    return scope ? `${scope.kind}:${scope.pos}:${scope.end}:${name}` : `unbound:${name}`;
  };
  const importedKeys = new Set([...localNames].map((name) => bindingKey(sourceFile, name)));
  const componentWrites = new Map<string, Array<{ pos: number; expression: ts.Expression }>>();
  const componentMemberWrites = new Map<string, Array<{ pos: number; expression: ts.Expression }>>();
  walkStoryNode(sourceFile, (node) => {
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer) {
      const key = bindingKey(node, node.name.text);
      const writes = componentWrites.get(key) ?? [];
      writes.push({ pos: node.getStart(sourceFile), expression: node.initializer });
      componentWrites.set(key, writes);
      if (ts.isObjectLiteralExpression(node.initializer)) {
        for (const property of node.initializer.properties) {
          if (!ts.isPropertyAssignment(property) && !ts.isShorthandPropertyAssignment(property)) continue;
          const member = propertyName(property.name);
          if (!member) continue;
          const memberKey = `${key}.${member}`;
          const memberWrites = componentMemberWrites.get(memberKey) ?? [];
          memberWrites.push({
            pos: node.getStart(sourceFile),
            expression: ts.isPropertyAssignment(property) ? property.initializer : property.name,
          });
          componentMemberWrites.set(memberKey, memberWrites);
        }
      }
    }
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
      && ts.isIdentifier(node.left)
      && storyNodeStaticallyReachable(node)
      && storyNodeBelongsToExportedOwner(node)) {
      const key = bindingKey(node.left, node.left.text);
      const writes = componentWrites.get(key) ?? [];
      writes.push({ pos: node.getStart(sourceFile), expression: node.right });
      componentWrites.set(key, writes);
    }
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
      && (ts.isPropertyAccessExpression(node.left) || ts.isElementAccessExpression(node.left))
      && ts.isIdentifier(node.left.expression)
      && storyNodeStaticallyReachable(node)
      && storyNodeBelongsToExportedOwner(node)) {
      const member = ts.isPropertyAccessExpression(node.left)
        ? node.left.name.text
        : node.left.argumentExpression
          && (ts.isStringLiteral(node.left.argumentExpression)
            || ts.isNoSubstitutionTemplateLiteral(node.left.argumentExpression))
          ? node.left.argumentExpression.text
          : null;
      if (member) {
        const key = `${bindingKey(node.left.expression, node.left.expression.text)}.${member}`;
        const writes = componentMemberWrites.get(key) ?? [];
        writes.push({ pos: node.getStart(sourceFile), expression: node.right });
        componentMemberWrites.set(key, writes);
      }
    }
  });
  function isComponentValue(identifier: ts.Identifier, before: number, active = new Set<string>()): boolean {
    const key = bindingKey(identifier, identifier.text);
    if (importedKeys.has(key)) return true;
    if (active.has(key)) return false;
    const write = [...(componentWrites.get(key) ?? [])]
      .reverse()
      .find((candidate) => candidate.pos < before);
    return Boolean(write && isComponentExpression(write.expression, write.pos, new Set(active).add(key)));
  }
  function isComponentExpression(
    expression: ts.Expression,
    before: number,
    active = new Set<string>(),
  ): boolean {
    if (ts.isIdentifier(expression)) return isComponentValue(expression, before, active);
    if ((ts.isPropertyAccessExpression(expression) || ts.isElementAccessExpression(expression))
      && ts.isIdentifier(expression.expression)) {
      const member = ts.isPropertyAccessExpression(expression)
        ? expression.name.text
        : expression.argumentExpression
          && (ts.isStringLiteral(expression.argumentExpression)
            || ts.isNoSubstitutionTemplateLiteral(expression.argumentExpression))
          ? expression.argumentExpression.text
          : null;
      if (!member) return false;
      const key = `${bindingKey(expression.expression, expression.expression.text)}.${member}`;
      if (active.has(key)) return false;
      const write = [...(componentMemberWrites.get(key) ?? [])]
        .reverse()
        .find((candidate) => candidate.pos < before);
      return Boolean(write && isComponentExpression(write.expression, write.pos, new Set(active).add(key)));
    }
    return false;
  }
  let rendered = false;
  const visit = (node: ts.Node): void => {
    if (rendered) return;
    if ((ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node))
      && (ts.isIdentifier(node.tagName) || ts.isPropertyAccessExpression(node.tagName))
      && isComponentExpression(node.tagName, node.getStart(sourceFile))
      && storyNodeStaticallyReachable(node)
      && storyNodeBelongsToExportedOwner(node)) {
      rendered = true;
      return;
    }
    if (ts.isCallExpression(node)
      && (ts.isIdentifier(node.expression) || ts.isPropertyAccessExpression(node.expression))
      && isComponentExpression(node.expression, node.getStart(sourceFile))
      && storyNodeStaticallyReachable(node)
      && storyNodeBelongsToExportedOwner(node)) {
      rendered = true;
      return;
    }
    if (ts.isCallExpression(node)
      && ts.isPropertyAccessExpression(node.expression)
      && node.expression.name.text === 'createElement'
      && node.arguments[0]
      && (ts.isIdentifier(node.arguments[0]) || ts.isPropertyAccessExpression(node.arguments[0]))
      && isComponentExpression(node.arguments[0], node.getStart(sourceFile))
      && storyNodeStaticallyReachable(node)
      && storyNodeBelongsToExportedOwner(node)) {
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
    isProductionWebSourcePath(path)
    && /(?:confirmation|serverConfirmationToken|confirmInterviewStory|interview-story-proposals[^\n]*confirm)/i.test(source)
    && hasClientGeneratedStoryAuthorization(parse(path, source), { path, sources })
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
        const relativePath = relative(root, path).replace(/\\/g, '/');
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
  const storyConsumer = (fileName: string, source: string): ts.SourceFile => parse(fileName, `
    import { confirmInterviewStoryProposal } from '@/services/interviewStories';
    ${source}
  `);

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
      expect(hasClientGeneratedStoryAuthorization(storyConsumer('unsafe.tsx', source))).toBe(true);
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
      expect(hasClientGeneratedStoryAuthorization(storyConsumer('unsafe-source.tsx', source))).toBe(true);
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

  it('keeps helper arguments local and treats declaration-only functions as UNKNOWN', () => {
    const safeLocalArgument = parse('web/src/components/local-argument.tsx', `
      import { getInterviewStoryProposal as loadProposal } from '@/services/interviewStories';
      const argument = crypto.randomUUID();
      const take = (response) => response.confirmation_token;
      async function owner() {
        const response = await loadProposal(id);
        confirmInterviewStoryProposal(id, { confirmation_token: take(response) });
      }
    `);
    expect(hasClientGeneratedStoryAuthorization(safeLocalArgument)).toBe(false);

    const declarationOnly = storyConsumer('web/src/components/declaration-only.tsx', `
      declare function opaqueToken(): string;
      confirmInterviewStoryProposal(id, { confirmation_token: opaqueToken() });
    `);
    expect(hasClientGeneratedStoryAuthorization(declarationOnly)).toBe(true);
  });

  it('normalizes Windows imports and scopes in-memory scans to production web src', () => {
    expect(importTargetsSource(
      'web\\src\\components\\InterviewStoryDrawer.tsx',
      '..\\features\\reviewReadiness\\ProductActionConfirmation',
      'web/src/features/reviewReadiness/ProductActionConfirmation.tsx',
    )).toBe(true);
    const unsafe = `confirmInterviewStoryProposal(id, { confirmation_token: crypto.randomUUID() });`;
    expect(clientAuthorizationViolations(new Map([
      ['web/src/components/unsafe.test.tsx', unsafe],
      ['web/src/components/__tests__/unsafe.tsx', unsafe],
      ['web/src/components/__fixtures__/unsafe.tsx', unsafe],
      ['web/src/components/unsafe.stories.tsx', unsafe],
      ['web/.cache/src/components/unsafe.tsx', unsafe],
      ['.worktrees/other/web/src/components/unsafe.tsx', unsafe],
      ['tests/fixtures/unsafe.tsx', unsafe],
    ]))).toEqual([]);
  });

  it('detects token generation inside the Story service HTTP path', () => {
    const unsafeService = parse('web/src/services/interviewStories.ts', `
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

  it('rejects computed, mutation-helper, and local-facade Story authorization probes', () => {
    const computed = storyConsumer('computed.tsx', `
      const tokenKey = 'confirmation_token';
      const aliasKey = tokenKey;
      const request = { [aliasKey]: crypto.randomUUID() };
      confirmInterviewStoryProposal(id, request);
    `);
    expect(hasClientGeneratedStoryAuthorization(computed)).toBe(true);

    const computedMutation = storyConsumer('computed-mutation.tsx', `
      const tokenKey = 'confirmation_token';
      const request = {};
      request[tokenKey] = crypto.randomUUID();
      confirmInterviewStoryProposal(id, request);
    `);
    expect(hasClientGeneratedStoryAuthorization(computedMutation)).toBe(true);

    const mutationHelper = storyConsumer('mutation-helper.tsx', `
      const tokenKey = 'confirmation_token';
      const request = {};
      function attach(target, value) { target[tokenKey] = value; return target; }
      confirmInterviewStoryProposal(id, attach(request, crypto.randomUUID()));
    `);
    expect(hasClientGeneratedStoryAuthorization(mutationHelper)).toBe(true);

    expect(clientAuthorizationViolations(new Map([
      ['web/src/services/storyFacade.ts', `
        export { confirmInterviewStoryProposal as submitStory } from './interviewStories';
      `],
      ['web/src/features/reviewReadiness/Owner.tsx', `
        import { submitStory as submit } from '@/services/storyFacade';
        submit(id, { confirmation_token: crypto.randomUUID() });
      `],
    ]))).toContain('ui:client-authorization-token');

    expect(clientAuthorizationViolations(new Map([
      ['web/src/services/storyFacade.ts', `
        export { confirmInterviewStoryProposal as submitStory } from './interviewStories';
      `],
      ['web/src/features/reviewReadiness/Owner.tsx', `
        import * as facade from '@/services/storyFacade';
        facade.submitStory(id, { confirmation_token: crypto.randomUUID() });
      `],
    ]))).toContain('ui:client-authorization-token');

    const computedHttp = parse('web/src/services/interviewStories.ts', `
      const method = 'post';
      const tokenKey = 'confirmation_token';
      const route = '/interview-story-' + 'proposals/7/confirm';
      http[method](route, { [tokenKey]: crypto.randomUUID() });
    `);
    expect(hasClientGeneratedStoryAuthorization(computedHttp)).toBe(true);
  });

  it('uses lexical scope and call-point assignment order for token flow', () => {
    const unsafeBeforeSafeWrite = storyConsumer('ordered.tsx', `
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

  it('tracks template keys, object mutation APIs, and default or namespace Story facades', () => {
    const mutations = [
      `const suffix = 'token'; const key = \`confirmation_\${suffix}\`;
       const request = {}; Object.assign(request, { [key]: crypto.randomUUID() });
       confirmInterviewStoryProposal(id, request);`,
      `const key = 'confirmation_token'; const request = {};
       Reflect.set(request, key, crypto.randomUUID());
       confirmInterviewStoryProposal(id, request);`,
    ];
    for (const source of mutations) {
      expect(hasClientGeneratedStoryAuthorization(storyConsumer('mutation-probe.tsx', source))).toBe(true);
    }

    expect(clientAuthorizationViolations(new Map([
      ['web/src/services/defaultStoryFacade.ts', `export { confirmInterviewStoryProposal as default } from './interviewStories';`],
      ['web/src/features/reviewReadiness/DefaultOwner.tsx', `
        import submit from '@/services/defaultStoryFacade';
        submit(id, { confirmation_token: crypto.randomUUID() });
      `],
    ]))).toContain('ui:client-authorization-token');
    expect(clientAuthorizationViolations(new Map([
      ['web/src/services/storyNamespaceFacade.ts', `export * as stories from './interviewStories';`],
      ['web/src/features/reviewReadiness/NamespaceOwner.tsx', `
        import { stories } from '@/services/storyNamespaceFacade';
        stories.confirmInterviewStoryProposal(id, { confirmation_token: crypto.randomUUID() });
      `],
    ]))).toContain('ui:client-authorization-token');

    expect(clientAuthorizationViolations(new Map([
      ['web/src/services/safeStoryFacade.ts', `
        import { getInterviewStoryProposal as load, confirmInterviewStoryProposal as submit } from './interviewStories';
        export async function submitServerToken(id) {
          const proposal = await load(id);
          return submit(id, { confirmation_token: proposal.confirmation_token });
        }
      `],
    ]))).not.toContain('ui:client-authorization-token');
  });

  it('tracks aliased mutation primitives and analyzes local Story wrappers at call context', () => {
    const mutations = [
      `const { assign } = Object; const key = 'confirmation_token'; const request = {};
       assign(request, { [key]: crypto.randomUUID() });
       confirmInterviewStoryProposal(id, request);`,
      `const define = Object.defineProperty; const request = {};
       define(request, 'confirmation_token', { value: crypto.randomUUID() });
       confirmInterviewStoryProposal(id, request);`,
      `const { set } = Reflect; const request = {};
       set(request, 'confirmation_token', crypto.randomUUID());
       confirmInterviewStoryProposal(id, request);`,
    ];
    for (const source of mutations) {
      expect(hasClientGeneratedStoryAuthorization(storyConsumer('aliased-mutation.tsx', source))).toBe(true);
    }

    const safeWrapper = storyConsumer('safe-wrapper.tsx', `
      import { getInterviewStoryProposal as load } from '@/services/interviewStories';
      function submitToken(id, token) {
        return confirmInterviewStoryProposal(id, { confirmation_token: token });
      }
      async function owner() {
        const proposal = await load(id);
        submitToken(id, proposal.confirmation_token);
      }
    `);
    expect(hasClientGeneratedStoryAuthorization(safeWrapper)).toBe(false);
    const unsafeWrapper = storyConsumer('unsafe-wrapper.tsx', `
      function submitToken(id, token) {
        return confirmInterviewStoryProposal(id, { confirmation_token: token });
      }
      submitToken(id, crypto.randomUUID());
    `);
    expect(hasClientGeneratedStoryAuthorization(unsafeWrapper)).toBe(true);
  });

  it('trusts Story confirmation names only when service provenance establishes them', () => {
    const localUtility = parse('web/src/utils/storyFormatting.ts', `
      function confirmInterviewStoryProposal(id, input) { return format(input); }
      confirmInterviewStoryProposal(id, { confirmation_token: crypto.randomUUID() });
    `);
    expect(hasClientGeneratedStoryAuthorization(localUtility)).toBe(false);
    expect(clientAuthorizationViolations(new Map([
      ['web/src/utils/storyFormatting.ts', `export function confirmInterviewStoryProposal(id, input) { return input; }`],
      ['web/src/features/reviewReadiness/Owner.tsx', `
        import { confirmInterviewStoryProposal } from '@/utils/storyFormatting';
        confirmInterviewStoryProposal(id, { confirmation_token: crypto.randomUUID() });
      `],
    ]))).not.toContain('ui:client-authorization-token');
    expect(clientAuthorizationViolations(new Map([
      ['web/src/services/interviewStories.ts', `export function confirmInterviewStoryProposal(id, input) { return http.post('/interview-story-proposals/1/confirm', input); }`],
      ['web/src/features/reviewReadiness/Owner.tsx', `
        import { confirmInterviewStoryProposal } from '@/services/interviewStories';
        confirmInterviewStoryProposal(id, { confirmation_token: crypto.randomUUID() });
      `],
    ]))).toContain('ui:client-authorization-token');
  });

  it('follows Story confirmation wrappers, namespace/default facades, and aliased HTTP posts', () => {
    const unsafeOwner = `submit(id, { confirmation_token: crypto.randomUUID() });`;
    expect(clientAuthorizationViolations(new Map([
      ['web/src/services/interviewStories.ts', `export function confirmInterviewStoryProposal(id, input) { return http.post('/interview-story-proposals/1/confirm', input); }`],
      ['web/src/services/storyFacade.ts', `import { confirmInterviewStoryProposal as confirm } from './interviewStories'; export function submit(id, input) { return confirm(id, input); }`],
      ['web/src/features/reviewReadiness/Owner.tsx', `import { submit } from '@/services/storyFacade'; ${unsafeOwner}`],
    ]))).toContain('ui:client-authorization-token');
    expect(clientAuthorizationViolations(new Map([
      ['web/src/services/interviewStories.ts', `export function confirmInterviewStoryProposal(id, input) { return http.post('/interview-story-proposals/1/confirm', input); }`],
      ['web/src/services/storyFacade.ts', `import { confirmInterviewStoryProposal as confirm } from './interviewStories'; const submit = (id, input) => confirm(id, input); export default submit;`],
      ['web/src/features/reviewReadiness/Owner.tsx', `import submit from '@/services/storyFacade'; ${unsafeOwner}`],
    ]))).toContain('ui:client-authorization-token');
    expect(clientAuthorizationViolations(new Map([
      ['web/src/services/interviewStories.ts', `export function confirmInterviewStoryProposal(id, input) { return http.post('/interview-story-proposals/1/confirm', input); }`],
      ['web/src/services/storyFacade.ts', `import { confirmInterviewStoryProposal as confirm } from './interviewStories'; export function submit(id, input) { return confirm(id, input); }`],
      ['web/src/services/storyNamespace.ts', `export * as story from './storyFacade';`],
      ['web/src/features/reviewReadiness/Owner.tsx', `import * as facade from '@/services/storyNamespace'; facade.story.submit(id, { confirmation_token: crypto.randomUUID() });`],
    ]))).toContain('ui:client-authorization-token');
    expect(clientAuthorizationViolations(new Map([
      ['web/src/services/interviewStories.ts', `const send = http.post.bind(http); export function confirmInterviewStoryProposal(id, input) { return send('/interview-story-proposals/1/confirm', input); }`],
      ['web/src/features/reviewReadiness/Owner.tsx', `import { confirmInterviewStoryProposal as submit } from '@/services/interviewStories'; ${unsafeOwner}`],
    ]))).toContain('ui:client-authorization-token');
    expect(clientAuthorizationViolations(new Map([
      ['web/src/services/interviewStories.ts', `export function confirmInterviewStoryProposal(id, input) { return http.post('/interview-story-proposals/1/confirm', input); }`],
      ['web/src/services/storyFacade.ts', `import { confirmInterviewStoryProposal as confirm } from './interviewStories'; export function submit(id, input) { return confirm(id, input); }`],
      ['web/src/features/reviewReadiness/Owner.tsx', `import { submit } from '@/services/storyFacade'; const KEY = 'confirmation_' + 'token'; submit(id, { [KEY]: crypto.randomUUID() });`],
    ]))).toContain('ui:client-authorization-token');
    expect(clientAuthorizationViolations(new Map([
      ['web/src/services/storyFacade.ts', `export function submit(id, input) { return format(input); }`],
      ['web/src/features/reviewReadiness/Owner.tsx', `import { submit } from '@/services/storyFacade'; ${unsafeOwner}`],
    ]))).not.toContain('ui:client-authorization-token');
    expect(clientAuthorizationViolations(new Map([
      ['web/src/services/interviewStories.ts', `export function confirmInterviewStoryProposal(id, input) { return http.post('/interview-story-proposals/1/confirm', input); } export function getInterviewStoryProposal(id) { return http.get('/interview-story-proposals/' + id); }`],
      ['web/src/services/storyFacade.ts', `import * as stories from './interviewStories'; export function submit(id, input) { return stories.confirmInterviewStoryProposal(id, input); }`],
      ['web/src/features/reviewReadiness/Owner.tsx', `import { submit } from '@/services/storyFacade'; submit(id, { confirmation_token: crypto.randomUUID() });`],
    ]))).toContain('ui:client-authorization-token');
    expect(clientAuthorizationViolations(new Map([
      ['web/src/services/interviewStories.ts', `export function confirmInterviewStoryProposal(id, input) { return http.post('/interview-story-proposals/1/confirm', input); } export function getInterviewStoryProposal(id) { return { confirmation_token: serverConfirmationToken }; }`],
      ['web/src/services/storyFacade.ts', `import * as stories from './interviewStories'; export function submit(id, ignored) { const draft = stories.getInterviewStoryProposal(id); return stories.confirmInterviewStoryProposal(id, { confirmation_token: draft.confirmation_token }); }`],
      ['web/src/features/reviewReadiness/Owner.tsx', `import { submit } from '@/services/storyFacade'; submit(id, crypto.randomUUID());`],
    ]))).not.toContain('ui:client-authorization-token');
  });

  it('ignores unreachable component returns and if-false renders', () => {
    const deadComponent = parse('dead.tsx', `export function ProductActionConfirmation() { return null; return <section />; }`);
    expect(hasExportedCanonicalComponent(deadComponent, 'ProductActionConfirmation')).toBe(false);
    const sources = new Map<string, string>([
      ['web/src/features/reviewReadiness/ProductActionConfirmation.tsx', `export function ProductActionConfirmation() { return <section />; }`],
      ['web/src/features/reviewReadiness/ReviewReadinessNextStep.tsx', `export function ReviewReadinessNextStep() { return <section />; }`],
      ['web/src/features/reviewReadiness/ReadinessFeedbackAdvisory.tsx', `export function ReadinessFeedbackAdvisory() { return <section />; }`],
      ['web/src/components/InterviewReviewProposalDrawer.tsx', `import { ProductActionConfirmation } from '@/features/reviewReadiness/ProductActionConfirmation'; import { ReviewReadinessNextStep } from '@/features/reviewReadiness/ReviewReadinessNextStep'; export function Owner() { if (false) return <><ProductActionConfirmation /><ReviewReadinessNextStep /></>; return null; }`],
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `import { ReadinessFeedbackAdvisory } from '@/features/reviewReadiness/ReadinessFeedbackAdvisory'; export function Owner() { if (false) return <ReadinessFeedbackAdvisory />; return null; }`],
    ]);
    expect(canonicalComponentViolations(sources)).toContain('ui:missing-canonical-review-readiness-components');
  });

  it('unwraps call, apply, and Reflect.apply for Story confirmation', () => {
    const service = ['web/src/services/interviewStories.ts', `export function confirmInterviewStoryProposal(id, input) {} export function getInterviewStoryProposal(id) { return server; }`] as const;
    const facade = ['web/src/services/storyFacade.ts', `import * as stories from './interviewStories'; export function submit(id, input) { return stories.confirmInterviewStoryProposal(id, input); }`] as const;
    for (const owner of [
      `import { submit } from '@/services/storyFacade'; submit.call(null, id, { confirmation_token: crypto.randomUUID() });`,
      `import { submit } from '@/services/storyFacade'; submit.apply(null, [id, { confirmation_token: crypto.randomUUID() }]);`,
      `Reflect.apply(http.post, http, ['/interview-story-proposals/1/confirm', { confirmation_token: crypto.randomUUID() }]);`,
      `http.post.call(http, '/interview-story-proposals/1/confirm', { confirmation_token: crypto.randomUUID() });`,
    ]) {
      expect(clientAuthorizationViolations(new Map([
        service,
        facade,
        ['web/src/components/InterviewStoryDrawer.tsx', owner],
      ])), owner).toContain('ui:client-authorization-token');
    }
    expect(clientAuthorizationViolations(new Map([
      service,
      facade,
      ['web/src/components/InterviewStoryDrawer.tsx', `import { submit } from '@/services/storyFacade'; import { getInterviewStoryProposal } from '@/services/interviewStories'; const draft = getInterviewStoryProposal(id); submit.apply(null, [id, { confirmation_token: draft.confirmation_token }]);`],
    ]))).not.toContain('ui:client-authorization-token');
  });

  it('does not count false conditional JSX or uncalled nested renders as canonical ownership', () => {
    for (const source of [
      `export function ProductActionConfirmation() { return false ? <section /> : null; }`,
      `export function ProductActionConfirmation() { function hidden() { return <section />; } return null; }`,
    ]) {
      expect(hasExportedCanonicalComponent(parse('component.tsx', source), 'ProductActionConfirmation'), source).toBe(false);
    }
    const components = new Map<string, string>([
      ['web/src/features/reviewReadiness/ProductActionConfirmation.tsx', `export function ProductActionConfirmation() { return <section />; }`],
      ['web/src/features/reviewReadiness/ReviewReadinessNextStep.tsx', `export function ReviewReadinessNextStep() { return <section />; }`],
      ['web/src/features/reviewReadiness/ReadinessFeedbackAdvisory.tsx', `export function ReadinessFeedbackAdvisory() { return <section />; }`],
      ['web/src/components/InterviewReviewProposalDrawer.tsx', `import { ProductActionConfirmation } from '@/features/reviewReadiness/ProductActionConfirmation'; import { ReviewReadinessNextStep } from '@/features/reviewReadiness/ReviewReadinessNextStep'; function hidden() { return <ProductActionConfirmation />; } export function Owner() { return false ? <ReviewReadinessNextStep /> : null; }`],
      ['web/src/components/InterviewStoryDrawer.tsx', `import { ProductActionConfirmation } from '@/features/reviewReadiness/ProductActionConfirmation'; export function Owner() { return false ? <ProductActionConfirmation /> : null; }`],
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `import { ReadinessFeedbackAdvisory } from '@/features/reviewReadiness/ReadinessFeedbackAdvisory'; export function Owner() { return false ? <ReadinessFeedbackAdvisory /> : null; }`],
      ['web/src/components/InterviewPreparationProposalDrawer.tsx', `import { ReadinessFeedbackAdvisory } from '@/features/reviewReadiness/ReadinessFeedbackAdvisory'; function hidden() { return <ReadinessFeedbackAdvisory />; } export function Owner() { return null; }`],
    ]);
    expect(canonicalComponentViolations(components)).toContain('ui:missing-canonical-review-readiness-components');
  });

  it('unwraps computed adapters, identifier apply arrays, and bound Story calls exactly', () => {
    const service = ['web/src/services/interviewStories.ts', `export function confirmInterviewStoryProposal(id, input) {} export function getInterviewStoryProposal(id) { return server; }`] as const;
    const facade = ['web/src/services/storyFacade.ts', `import { confirmInterviewStoryProposal } from './interviewStories'; export function submit(id, input) { return confirmInterviewStoryProposal(id, input); }`] as const;
    for (const owner of [
      `import { submit } from '@/services/storyFacade'; const args = [id, { confirmation_token: crypto.randomUUID() }]; submit['apply'](null, args);`,
      `Reflect['apply'](http.post, http, ['/interview-story-proposals/1/confirm', { confirmation_token: crypto.randomUUID() }]);`,
      `import { submit } from '@/services/storyFacade'; const bound = submit.bind(null, id, { confirmation_token: crypto.randomUUID() }); bound();`,
    ]) expect(clientAuthorizationViolations(new Map([
      service,
      facade,
      ['web/src/components/InterviewStoryDrawer.tsx', owner],
    ])), owner).toContain('ui:client-authorization-token');
    expect(clientAuthorizationViolations(new Map([
      service,
      facade,
      ['web/src/components/InterviewStoryDrawer.tsx', `import { submit } from '@/services/storyFacade'; import { getInterviewStoryProposal } from '@/services/interviewStories'; const draft = getInterviewStoryProposal(id); const bound = submit.bind(null, id, { confirmation_token: draft.confirmation_token }); bound();`],
    ]))).not.toContain('ui:client-authorization-token');
  });

  it('handles logical JSX reachability, reachable helpers, and React.memo exports', () => {
    for (const source of [
      `export function ProductActionConfirmation() { return false && <section />; }`,
      `export function ProductActionConfirmation() { return true || <section />; }`,
    ]) expect(hasExportedCanonicalComponent(parse('component.tsx', source), 'ProductActionConfirmation'), source).toBe(false);
    expect(hasExportedCanonicalComponent(parse('component.tsx', `export const ProductActionConfirmation = React.memo(function ProductActionConfirmation() { return <section />; });`), 'ProductActionConfirmation')).toBe(true);
    const sources = new Map<string, string>([
      ['web/src/features/reviewReadiness/ProductActionConfirmation.tsx', `export function ProductActionConfirmation() { return <section />; }`],
      ['web/src/features/reviewReadiness/ReviewReadinessNextStep.tsx', `export function ReviewReadinessNextStep() { return <section />; }`],
      ['web/src/features/reviewReadiness/ReadinessFeedbackAdvisory.tsx', `export function ReadinessFeedbackAdvisory() { return <section />; }`],
      ['web/src/components/InterviewReviewProposalDrawer.tsx', `import { ProductActionConfirmation } from '@/features/reviewReadiness/ProductActionConfirmation'; import { ReviewReadinessNextStep } from '@/features/reviewReadiness/ReviewReadinessNextStep'; function helper() { return <><ProductActionConfirmation /><ReviewReadinessNextStep /></>; } export function Owner() { return helper(); }`],
      ['web/src/components/InterviewStoryDrawer.tsx', `import { ProductActionConfirmation } from '@/features/reviewReadiness/ProductActionConfirmation'; export function Owner() { return <ProductActionConfirmation />; }`],
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `import { ReadinessFeedbackAdvisory } from '@/features/reviewReadiness/ReadinessFeedbackAdvisory'; export function Owner() { return <ReadinessFeedbackAdvisory />; }`],
      ['web/src/components/InterviewPreparationProposalDrawer.tsx', `import { ReadinessFeedbackAdvisory } from '@/features/reviewReadiness/ReadinessFeedbackAdvisory'; export function Owner() { return <ReadinessFeedbackAdvisory />; }`],
    ]);
    expect(canonicalComponentViolations(sources)).toEqual([]);
  });

  it('resolves constant adapters, as-const arrays, and spread Story calls', () => {
    const service = ['web/src/services/interviewStories.ts', `export function confirmInterviewStoryProposal(id, input) {} export function getInterviewStoryProposal(id) { return server; }`] as const;
    const facade = ['web/src/services/storyFacade.ts', `import { confirmInterviewStoryProposal } from './interviewStories'; export function submit(id, input) { return confirmInterviewStoryProposal(id, input); }`] as const;
    for (const owner of [
      `import { submit } from '@/services/storyFacade'; const METHOD = 'apply'; const args = [id, { confirmation_token: crypto.randomUUID() }] as const; submit[METHOD](null, args);`,
      `import { submit } from '@/services/storyFacade'; const args = [id, { confirmation_token: crypto.randomUUID() }] as const; submit(...args);`,
      `const args = ['/interview-story-proposals/1/confirm', { confirmation_token: crypto.randomUUID() }] as const; http.post(...args);`,
    ]) expect(clientAuthorizationViolations(new Map([service, facade, ['web/src/components/InterviewStoryDrawer.tsx', owner]])), owner).toContain('ui:client-authorization-token');
    expect(clientAuthorizationViolations(new Map([
      service,
      facade,
      ['web/src/components/InterviewStoryDrawer.tsx', `import { submit } from '@/services/storyFacade'; import { getInterviewStoryProposal } from '@/services/interviewStories'; const draft = getInterviewStoryProposal(id); const args = [id, { confirmation_token: draft.confirmation_token }] as const; submit(...args);`],
    ]))).not.toContain('ui:client-authorization-token');
  });

  it('accepts React.forwardRef components and reachable const-arrow owner helpers', () => {
    expect(hasExportedCanonicalComponent(parse('component.tsx', `export const ProductActionConfirmation = React.forwardRef(function ProductActionConfirmation(props, ref) { return <section ref={ref} />; });`), 'ProductActionConfirmation')).toBe(true);
    const sources = new Map<string, string>([
      ['web/src/features/reviewReadiness/ProductActionConfirmation.tsx', `export function ProductActionConfirmation() { return <section />; }`],
      ['web/src/features/reviewReadiness/ReviewReadinessNextStep.tsx', `export function ReviewReadinessNextStep() { return <section />; }`],
      ['web/src/features/reviewReadiness/ReadinessFeedbackAdvisory.tsx', `export function ReadinessFeedbackAdvisory() { return <section />; }`],
      ['web/src/components/InterviewReviewProposalDrawer.tsx', `import { ProductActionConfirmation } from '@/features/reviewReadiness/ProductActionConfirmation'; import { ReviewReadinessNextStep } from '@/features/reviewReadiness/ReviewReadinessNextStep'; const helper = () => <><ProductActionConfirmation /><ReviewReadinessNextStep /></>; export const Owner = () => helper();`],
      ['web/src/components/InterviewStoryDrawer.tsx', `import { ProductActionConfirmation } from '@/features/reviewReadiness/ProductActionConfirmation'; export const Owner = () => <ProductActionConfirmation />;`],
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `import { ReadinessFeedbackAdvisory } from '@/features/reviewReadiness/ReadinessFeedbackAdvisory'; export const Owner = () => <ReadinessFeedbackAdvisory />;`],
      ['web/src/components/InterviewPreparationProposalDrawer.tsx', `import { ReadinessFeedbackAdvisory } from '@/features/reviewReadiness/ReadinessFeedbackAdvisory'; export const Owner = () => <ReadinessFeedbackAdvisory />;`],
    ]);
    expect(canonicalComponentViolations(sources)).toEqual([]);
  });

  it('detects Story authorization in fetch and http.request adapters', () => {
    for (const source of [
      `fetch('/interview-story-proposals/1/confirm', { method: 'POST', body: JSON.stringify({ confirmation_token: crypto.randomUUID() }) });`,
      `http.request({ url: '/interview-story-proposals/1/confirm', method: 'POST', data: { confirmation_token: crypto.randomUUID() } });`,
    ]) expect(clientAuthorizationViolations(new Map([['web/src/components/InterviewStoryDrawer.tsx', source]])), source).toContain('ui:client-authorization-token');
    expect(clientAuthorizationViolations(new Map([
      ['web/src/services/interviewStories.ts', `export function getInterviewStoryProposal(id) { return server; }`],
      ['web/src/components/InterviewStoryDrawer.tsx', `import { getInterviewStoryProposal } from '@/services/interviewStories'; const draft = getInterviewStoryProposal(id); fetch('/interview-story-proposals/1/confirm', { method: 'POST', body: JSON.stringify({ confirmation_token: draft.confirmation_token }) });`],
    ]))).not.toContain('ui:client-authorization-token');
    expect(clientAuthorizationViolations(new Map([
      ['web/src/components/InterviewStoryDrawer.tsx', `fetch('/interview-story-proposals/1', { method: 'GET' }); http.request({ url: '/preferences', method: 'PATCH', data: { theme: 'dark' } });`],
    ]))).not.toContain('ui:client-authorization-token');
  });

  it('resolves identifier-initialized Story adapter routes and configs', () => {
    for (const source of [
      `const route = '/interview-story-proposals/1/confirm'; const options = { method: 'POST', body: JSON.stringify({ confirmation_token: crypto.randomUUID() }) }; fetch(route, options);`,
      `const route = '/interview-story-proposals/1/confirm'; const config = { url: route, method: 'POST', data: { confirmation_token: crypto.randomUUID() } }; http.request(config);`,
    ]) expect(clientAuthorizationViolations(new Map([['web/src/components/InterviewStoryDrawer.tsx', source]])), source).toContain('ui:client-authorization-token');
    expect(clientAuthorizationViolations(new Map([
      ['web/src/services/interviewStories.ts', `export function getInterviewStoryProposal(id) { return server; }`],
      ['web/src/components/InterviewStoryDrawer.tsx', `import { getInterviewStoryProposal } from '@/services/interviewStories'; const draft = getInterviewStoryProposal(id); const route = '/interview-story-proposals/1/confirm'; const options = { method: 'POST', body: JSON.stringify({ confirmation_token: draft.confirmation_token }) }; fetch(route, options);`],
    ]))).not.toContain('ui:client-authorization-token');
    expect(clientAuthorizationViolations(new Map([
      ['web/src/components/InterviewStoryDrawer.tsx', `const route = '/preferences'; const config = { url: route, method: 'POST', data: { confirmation_token: crypto.randomUUID() } }; http.request(config);`],
    ]))).not.toContain('ui:client-authorization-token');
  });

  it('tracks Story request config property writes after initialization', () => {
    for (const source of [
      `const route = '/interview-story-proposals/1/confirm'; const options = {}; options.method = 'POST'; options.body = JSON.stringify({ confirmation_token: crypto.randomUUID() }); fetch(route, options);`,
      `const route = '/interview-story-proposals/1/confirm'; const config = {}; config.url = route; config.method = 'POST'; config.data = { confirmation_token: crypto.randomUUID() }; http.request(config);`,
    ]) expect(clientAuthorizationViolations(new Map([['web/src/components/InterviewStoryDrawer.tsx', source]])), source).toContain('ui:client-authorization-token');
    expect(clientAuthorizationViolations(new Map([
      ['web/src/components/InterviewStoryDrawer.tsx', `const config = {}; config.url = '/preferences'; config.method = 'POST'; config.data = { confirmation_token: crypto.randomUUID() }; http.request(config);`],
    ]))).not.toContain('ui:client-authorization-token');
  });

  it('tracks Object.assign and defineProperty Story config mutations', () => {
    for (const source of [
      `const options = {}; Object.assign(options, { method: 'POST', body: JSON.stringify({ confirmation_token: crypto.randomUUID() }) }); fetch('/interview-story-proposals/1/confirm', options);`,
      `const config = {}; Object.defineProperty(config, 'url', { value: '/interview-story-proposals/1/confirm' }); Object.defineProperty(config, 'method', { value: 'POST' }); Object.defineProperty(config, 'data', { value: { confirmation_token: crypto.randomUUID() } }); http.request(config);`,
    ]) expect(clientAuthorizationViolations(new Map([['web/src/components/InterviewStoryDrawer.tsx', source]])), source).toContain('ui:client-authorization-token');
    expect(clientAuthorizationViolations(new Map([
      ['web/src/services/interviewStories.ts', `export function getInterviewStoryProposal(id) { return server; }`],
      ['web/src/components/InterviewStoryDrawer.tsx', `import { getInterviewStoryProposal } from '@/services/interviewStories'; const draft = getInterviewStoryProposal(id); const options = {}; Object.assign(options, { method: 'POST', body: JSON.stringify({ confirmation_token: draft.confirmation_token }) }); fetch('/interview-story-proposals/1/confirm', options);`],
    ]))).not.toContain('ui:client-authorization-token');
    expect(clientAuthorizationViolations(new Map([
      ['web/src/components/InterviewStoryDrawer.tsx', `const options = {}; Object.assign(options, { method: 'POST', body: JSON.stringify({ confirmation_token: crypto.randomUUID() }) }); fetch('/preferences', options);`],
    ]))).not.toContain('ui:client-authorization-token');
  });

  it('resolves computed and aliased Story mutation builtins', () => {
    for (const source of [
      `const options = {}; const assign = Object['assign']; assign(options, { method: 'POST', body: JSON.stringify({ confirmation_token: crypto.randomUUID() }) }); fetch('/interview-story-proposals/1/confirm', options);`,
      `const METHOD = 'defineProperty'; const config = {}; Object[METHOD](config, 'url', { value: '/interview-story-proposals/1/confirm' }); Object[METHOD](config, 'method', { value: 'POST' }); Object[METHOD](config, 'data', { value: { confirmation_token: crypto.randomUUID() } }); http.request(config);`,
    ]) expect(clientAuthorizationViolations(new Map([['web/src/components/InterviewStoryDrawer.tsx', source]])), source).toContain('ui:client-authorization-token');
    expect(clientAuthorizationViolations(new Map([
      ['web/src/components/InterviewStoryDrawer.tsx', `const local = {}; const assign = Object.assign; assign(local, { token: crypto.randomUUID() }); fetch('/preferences', { method: 'GET' });`],
    ]))).not.toContain('ui:client-authorization-token');
  });

  it('canonicalizes Story mutation bind, call, apply, and Reflect.apply adapters', () => {
    for (const source of [
      `const request = {}; Object.assign.call(Object, request, { confirmation_token: crypto.randomUUID() }); confirmInterviewStoryProposal(1, request);`,
      `const request = {}; const args = [request, 'confirmation_token', crypto.randomUUID()] as const; Reflect.apply(Reflect.set, Reflect, args); confirmInterviewStoryProposal(1, request);`,
      `const request = {}; const set = Reflect.set.bind(Reflect); set(request, 'confirmation_token', crypto.randomUUID()); confirmInterviewStoryProposal(1, request);`,
    ]) expect(hasClientGeneratedStoryAuthorization(storyConsumer('mutation-adapter.tsx', source)), source).toBe(true);
    const safe = storyConsumer('mutation-adapter-safe.tsx', `const request = {}; const assign = Object.assign.bind(Object); assign(request, { confirmation_token: draft.serverConfirmationToken }); confirmInterviewStoryProposal(1, request);`);
    expect(hasClientGeneratedStoryAuthorization(safe)).toBe(false);
  });

  it('accepts local component aliases only when they preserve the imported value', () => {
    const base = new Map<string, string>([
      ['web/src/features/reviewReadiness/ProductActionConfirmation.tsx', `export function ProductActionConfirmation() { return <section />; }`],
      ['web/src/features/reviewReadiness/ReviewReadinessNextStep.tsx', `export function ReviewReadinessNextStep() { return <section />; }`],
      ['web/src/features/reviewReadiness/ReadinessFeedbackAdvisory.tsx', `export function ReadinessFeedbackAdvisory() { return <section />; }`],
      ['web/src/components/InterviewReviewProposalDrawer.tsx', `import { ProductActionConfirmation } from '@/features/reviewReadiness/ProductActionConfirmation'; import { ReviewReadinessNextStep } from '@/features/reviewReadiness/ReviewReadinessNextStep'; const Confirm = ProductActionConfirmation; const Next = ReviewReadinessNextStep; export function Owner() { return <><Confirm /><Next /></>; }`],
      ['web/src/components/InterviewStoryDrawer.tsx', `import { ProductActionConfirmation } from '@/features/reviewReadiness/ProductActionConfirmation'; const Confirm = ProductActionConfirmation; export function Owner() { return <Confirm />; }`],
      ['web/src/components/AdaptiveInterviewPracticeWorkspace.tsx', `import { ReadinessFeedbackAdvisory } from '@/features/reviewReadiness/ReadinessFeedbackAdvisory'; const Advisory = ReadinessFeedbackAdvisory; export function Owner() { return <Advisory />; }`],
      ['web/src/components/InterviewPreparationProposalDrawer.tsx', `import { ReadinessFeedbackAdvisory } from '@/features/reviewReadiness/ReadinessFeedbackAdvisory'; const Advisory = ReadinessFeedbackAdvisory; export function Owner() { return <Advisory />; }`],
    ]);
    expect(canonicalComponentViolations(base)).toEqual([]);
    base.set('web/src/components/InterviewReviewProposalDrawer.tsx', `import { ProductActionConfirmation } from '@/features/reviewReadiness/ProductActionConfirmation'; import { ReviewReadinessNextStep } from '@/features/reviewReadiness/ReviewReadinessNextStep'; const Confirm = OtherComponent; const Next = ReviewReadinessNextStep; export function Owner() { return <><Confirm /><Next /></>; }`);
    base.set('web/src/components/InterviewStoryDrawer.tsx', `import { ProductActionConfirmation } from '@/features/reviewReadiness/ProductActionConfirmation'; const Confirm = OtherComponent; export function Owner() { return <Confirm />; }`);
    expect(canonicalComponentViolations(base)).toContain('ui:missing-canonical-review-readiness-components');
  });

  it('tracks later component alias assignments at the render point', () => {
    const component = ['web/src/features/reviewReadiness/ProductActionConfirmation.tsx', `export function ProductActionConfirmation() { return <section />; }`] as const;
    const good = `import { ProductActionConfirmation } from '@/features/reviewReadiness/ProductActionConfirmation'; let Confirm = OtherComponent; Confirm = ProductActionConfirmation; export function Owner() { return <Confirm />; }`;
    expect(ownerImportsAndRenders('web/src/components/InterviewStoryDrawer.tsx', good, component[0], 'ProductActionConfirmation')).toBe(true);
    const killed = `import { ProductActionConfirmation } from '@/features/reviewReadiness/ProductActionConfirmation'; let Confirm = ProductActionConfirmation; Confirm = OtherComponent; export function Owner() { return <Confirm />; }`;
    expect(ownerImportsAndRenders('web/src/components/InterviewStoryDrawer.tsx', killed, component[0], 'ProductActionConfirmation')).toBe(false);
  });

  it('tracks canonical component object-member aliases at the render point', () => {
    const componentPath = 'web/src/features/reviewReadiness/ProductActionConfirmation.tsx';
    const good = `import { ProductActionConfirmation } from '@/features/reviewReadiness/ProductActionConfirmation'; const Components = { Confirm: ProductActionConfirmation }; export function Owner() { return <Components.Confirm />; }`;
    expect(ownerImportsAndRenders('web/src/components/InterviewStoryDrawer.tsx', good, componentPath, 'ProductActionConfirmation')).toBe(true);
    const wrongMember = `import { ProductActionConfirmation } from '@/features/reviewReadiness/ProductActionConfirmation'; const Components = { Confirm: ProductActionConfirmation, Other }; export function Owner() { return <Components.Other />; }`;
    expect(ownerImportsAndRenders('web/src/components/InterviewStoryDrawer.tsx', wrongMember, componentPath, 'ProductActionConfirmation')).toBe(false);
    const nonExported = `import { ProductActionConfirmation } from '@/features/reviewReadiness/ProductActionConfirmation'; const Components = { Confirm: ProductActionConfirmation }; function hidden() { return <Components.Confirm />; } export function Owner() { return null; }`;
    expect(ownerImportsAndRenders('web/src/components/InterviewStoryDrawer.tsx', nonExported, componentPath, 'ProductActionConfirmation')).toBe(false);
  });

  it('requires canonical component member writes to be reachable from an exported owner', () => {
    const componentPath = 'web/src/features/reviewReadiness/ProductActionConfirmation.tsx';
    for (const mutation of [
      `if (false) Components.Confirm = ProductActionConfirmation;`,
      `function hidden() { Components.Confirm = ProductActionConfirmation; }`,
    ]) {
      const source = `import { ProductActionConfirmation } from '@/features/reviewReadiness/ProductActionConfirmation'; const Components = { Confirm: Other }; ${mutation} export function Owner() { return <Components.Confirm />; }`;
      expect(ownerImportsAndRenders('web/src/components/InterviewStoryDrawer.tsx', source, componentPath, 'ProductActionConfirmation'), mutation).toBe(false);
    }
    const reachable = `import { ProductActionConfirmation } from '@/features/reviewReadiness/ProductActionConfirmation'; const Components = { Confirm: Other }; function activate() { Components.Confirm = ProductActionConfirmation; } export function Owner() { activate(); return <Components.Confirm />; }`;
    expect(ownerImportsAndRenders('web/src/components/InterviewStoryDrawer.tsx', reachable, componentPath, 'ProductActionConfirmation')).toBe(true);
  });

  it('requires ordinary component alias writes to be reachable from an exported owner', () => {
    const componentPath = 'web/src/features/reviewReadiness/ProductActionConfirmation.tsx';
    for (const mutation of [
      `if (false) Confirm = ProductActionConfirmation;`,
      `function hidden() { Confirm = ProductActionConfirmation; }`,
    ]) {
      const source = `import { ProductActionConfirmation } from '@/features/reviewReadiness/ProductActionConfirmation'; let Confirm = Other; ${mutation} export function Owner() { return <Confirm />; }`;
      expect(ownerImportsAndRenders('web/src/components/InterviewStoryDrawer.tsx', source, componentPath, 'ProductActionConfirmation'), mutation).toBe(false);
    }
    const reachable = `import { ProductActionConfirmation } from '@/features/reviewReadiness/ProductActionConfirmation'; let Confirm = Other; function activate() { Confirm = ProductActionConfirmation; } export function Owner() { activate(); return <Confirm />; }`;
    expect(ownerImportsAndRenders('web/src/components/InterviewStoryDrawer.tsx', reachable, componentPath, 'ProductActionConfirmation')).toBe(true);
  });

  it('keeps canonical component writes bound to their lexical alias identity', () => {
    const componentPath = 'web/src/features/reviewReadiness/ProductActionConfirmation.tsx';
    for (const source of [
      `import { ProductActionConfirmation } from '@/features/reviewReadiness/ProductActionConfirmation'; let Confirm = Other; { let Confirm = ProductActionConfirmation; void Confirm; } export function Owner() { return <Confirm />; }`,
      `import { ProductActionConfirmation } from '@/features/reviewReadiness/ProductActionConfirmation'; let Confirm = Other; function activate(Confirm) { Confirm = ProductActionConfirmation; } export function Owner() { activate(Other); return <Confirm />; }`,
      `import { ProductActionConfirmation } from '@/features/reviewReadiness/ProductActionConfirmation'; const Components = { Confirm: Other }; { const Components = { Confirm: ProductActionConfirmation }; void Components; } export function Owner() { return <Components.Confirm />; }`,
      `import { ProductActionConfirmation } from '@/features/reviewReadiness/ProductActionConfirmation'; const Components = { Confirm: Other }; function activate(Components) { Components.Confirm = ProductActionConfirmation; } export function Owner() { activate({}); return <Components.Confirm />; }`,
    ]) expect(ownerImportsAndRenders('web/src/components/InterviewStoryDrawer.tsx', source, componentPath, 'ProductActionConfirmation'), source).toBe(false);
    const captured = `import { ProductActionConfirmation } from '@/features/reviewReadiness/ProductActionConfirmation'; let Confirm = Other; const Components = { Confirm: Other }; function activate() { Confirm = ProductActionConfirmation; Components.Confirm = ProductActionConfirmation; } export function Owner() { activate(); return <><Confirm /><Components.Confirm /></>; }`;
    expect(ownerImportsAndRenders('web/src/components/InterviewStoryDrawer.tsx', captured, componentPath, 'ProductActionConfirmation')).toBe(true);
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
    expect(sourceViolations(repositoryRoot())).toEqual([]);
  });
});
