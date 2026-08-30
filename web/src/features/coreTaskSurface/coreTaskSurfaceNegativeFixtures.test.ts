import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import ts from 'typescript';
import { describe, expect, it } from 'vitest';

type Violation =
  | 'direct-service-mutation'
  | 'pilot-mutation-prop'
  | 'legacy-owner-or-shadow'
  | 'cross-domain-projection'
  | 'component-local-source-mapping';

function repositoryRoot(): string {
  return execFileSync('git', ['rev-parse', '--show-toplevel'], { encoding: 'utf8' }).trim();
}

function readProduction(relativePath: string): string {
  return readFileSync(join(repositoryRoot(), relativePath), 'utf8');
}

function parse(relativePath: string, source: string): ts.SourceFile {
  return ts.createSourceFile(
    relativePath,
    source,
    ts.ScriptTarget.Latest,
    true,
    relativePath.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
}

function visit(sourceFile: ts.SourceFile, callback: (node: ts.Node) => void): void {
  const walk = (node: ts.Node): void => {
    callback(node);
    ts.forEachChild(node, walk);
  };
  walk(sourceFile);
}

function importedName(specifier: ts.ImportSpecifier): string {
  return specifier.propertyName?.text ?? specifier.name.text;
}

function isMutationName(name: string): boolean {
  return /^(?:create|update|delete|save|submit|confirm|start|generate|retry|upload|copy|mark|post|put|patch)[A-Z_]/.test(name);
}

function directServiceMutationViolations(relativePath: string, source: string): Violation[] {
  const sourceFile = parse(relativePath, source);
  const importedMutators = new Set<string>();
  const serviceNamespaces = new Set<string>();

  for (const statement of sourceFile.statements) {
    if (!ts.isImportDeclaration(statement)
      || !ts.isStringLiteral(statement.moduleSpecifier)
      || !/(?:^|\/)services(?:\/|$)/i.test(statement.moduleSpecifier.text)
      || !statement.importClause
      || statement.importClause.isTypeOnly) continue;
    const clause = statement.importClause;
    if (clause.name && isMutationName(clause.name.text)) importedMutators.add(clause.name.text);
    if (clause.namedBindings && ts.isNamespaceImport(clause.namedBindings)) {
      serviceNamespaces.add(clause.namedBindings.name.text);
    }
    if (clause.namedBindings && ts.isNamedImports(clause.namedBindings)) {
      for (const specifier of clause.namedBindings.elements) {
        if (!specifier.isTypeOnly && isMutationName(importedName(specifier))) {
          importedMutators.add(specifier.name.text);
        }
      }
    }
  }

  const violations: Violation[] = [];
  visit(sourceFile, (node) => {
    if (!ts.isCallExpression(node)) return;
    const expression = node.expression;
    if (ts.isIdentifier(expression)
      && (importedMutators.has(expression.text) || expression.text === 'fetch' || expression.text === 'axios')) {
      violations.push('direct-service-mutation');
      return;
    }
    if (ts.isPropertyAccessExpression(expression)
      && ts.isIdentifier(expression.expression)
      && serviceNamespaces.has(expression.expression.text)
      && isMutationName(expression.name.text)) {
      violations.push('direct-service-mutation');
    }
  });
  return [...new Set(violations)];
}

const PILOT_MUTATION_NAMES = new Set([
  'draft',
  'dispatch',
  'resumes',
  'resumeEvidenceProof',
  'onChange',
  'onStartTriage',
  'onRetryTriage',
  'onConfirmTriage',
  'onStartDeepReview',
  'onViewHistory',
  'onViewLegacyHistory',
  'onStartNew',
  'onPrepareMaterials',
  'onOpenInterviewReview',
  'onOpenInterviewPreparation',
  'onOpenMockInterview',
  'onCancel',
  'retry',
  'useMutation',
]);

function pilotMutationViolations(relativePath: string, source: string): Violation[] {
  const sourceFile = parse(relativePath, source);
  const violations: Violation[] = [];
  visit(sourceFile, (node) => {
    if (ts.isIdentifier(node) && PILOT_MUTATION_NAMES.has(node.text)) violations.push('pilot-mutation-prop');
  });
  return [...new Set(violations)];
}

const LEGACY_OWNER_NAMES = new Set([
  'pilotV2Draft',
  'pilotV2Drafts',
  'pilotV2OperationPending',
  'pilotV2HistoryPending',
  'pilotV2GenerationRef',
  'pilotLegacyReview',
  'startPilotV2Triage',
  'recoverPilotV2SourceConflict',
  'recoverPilotV2TriageConfirmation',
  'confirmPilotV2Triage',
  'startPilotV2DeepReview',
  'viewPilotV2History',
  'viewPilotLegacyHistory',
  'startNewPilotV2Review',
  'updatePilotV2Draft',
]);

function legacySurfaceViolations(relativePath: string, source: string): Violation[] {
  const sourceFile = parse(relativePath, source);
  const violations: Violation[] = [];
  visit(sourceFile, (node) => {
    if (ts.isIdentifier(node) && LEGACY_OWNER_NAMES.has(node.text)) violations.push('legacy-owner-or-shadow');
  });
  return [...new Set(violations)];
}

function crossDomainProjectionViolations(relativePath: string, source: string): Violation[] {
  const sourceFile = parse(relativePath, source);
  const isReviewSurface = /(?:reviews|Review)/.test(relativePath);
  const isKnowledgeSurface = /(?:knowledge|KnowledgeSources)/.test(relativePath);
  const violations: Violation[] = [];
  for (const statement of sourceFile.statements) {
    if (!ts.isImportDeclaration(statement)
      || !ts.isStringLiteral(statement.moduleSpecifier)) continue;
    const moduleName = statement.moduleSpecifier.text;
    if ((isReviewSurface && /knowledge|KnowledgeSources/i.test(moduleName))
      || (isKnowledgeSurface && /reviews|OpportunityFitReview/i.test(moduleName))) {
      violations.push('cross-domain-projection');
    }
  }
  return [...new Set(violations)];
}

function componentLocalMappingViolations(relativePath: string, source: string): Violation[] {
  const sourceFile = parse(relativePath, source);
  const mappingProperties = new Set(['is_master', 'parent_resume_id', 'origin_kind', 'source_kind']);
  const userLabels = ['基础简历', '岗位版本', '其他简历', '经历故事', '外部参考资料'];
  let readsMappingProperty = false;
  visit(sourceFile, (node) => {
    if (ts.isPropertyAccessExpression(node) && mappingProperties.has(node.name.text)) readsMappingProperty = true;
  });
  return readsMappingProperty && userLabels.some((label) => source.includes(label))
    ? ['component-local-source-mapping']
    : [];
}

describe('core task surface negative fixtures', () => {
  it('rejects direct service writes outside a task owner', () => {
    const fixture = `import { createOpportunityFitV2Triage } from '@/services/opportunityFitReviews';
export function PilotProjection() {
  void createOpportunityFitV2Triage({});
  return null;
}`;
    expect(directServiceMutationViolations('web/src/features/pilot/PilotProjection.tsx', fixture))
      .toContain('direct-service-mutation');
    expect(directServiceMutationViolations(
      'web/src/features/pilot/PilotOpportunityFitV2Card.tsx',
      readProduction('web/src/features/pilot/PilotOpportunityFitV2Card.tsx'),
    )).toEqual([]);
  });

  it('rejects mutation props and callbacks on the Pilot projection', () => {
    const fixture = `interface Props {
  draft: unknown;
  onStartTriage: () => void;
  onConfirmTriage: () => void;
}
export function PilotProjection({ draft, onStartTriage }: Props) {
  onStartTriage();
  return <span>{draft}</span>;
}`;
    expect(pilotMutationViolations('web/src/features/pilot/PilotProjection.tsx', fixture))
      .toEqual(expect.arrayContaining(['pilot-mutation-prop']));
    expect(pilotMutationViolations(
      'web/src/features/pilot/PilotOpportunityFitV2Card.tsx',
      readProduction('web/src/features/pilot/PilotOpportunityFitV2Card.tsx'),
    )).toEqual([]);
  });

  it('rejects legacy owner state and shadow handlers', () => {
    const fixture = `const pilotV2Draft = new Map();
function startPilotV2Triage() { return pilotV2Draft; }
export function AppShellShadow() { return startPilotV2Triage(); }`;
    expect(legacySurfaceViolations('web/src/layout/AppShell.tsx', fixture))
      .toContain('legacy-owner-or-shadow');
    expect(legacySurfaceViolations('web/src/layout/AppShell.tsx', readProduction('web/src/layout/AppShell.tsx')))
      .toEqual([]);
  });

  it('rejects cross-domain Review/Knowledge projections', () => {
    const fixture = `import { fetchKnowledgeSources } from '@/services/knowledge';
export function reviewProjection() { return fetchKnowledgeSources(); }`;
    expect(crossDomainProjectionViolations('web/src/features/reviews/reviewProjection.ts', fixture))
      .toContain('cross-domain-projection');
    expect(crossDomainProjectionViolations(
      'web/src/components/ExperienceMaterialsView.tsx',
      readProduction('web/src/components/ExperienceMaterialsView.tsx'),
    )).toEqual([]);
    expect(crossDomainProjectionViolations(
      'web/src/components/KnowledgeSourcesView.tsx',
      readProduction('web/src/components/KnowledgeSourcesView.tsx'),
    )).toEqual([]);
  });

  it('rejects component-local Resume/source mapping labels', () => {
    const fixture = `export function ResumeCard({ resume }: { resume: any }) {
  return <span>{resume.is_master ? '基础简历' : '其他简历'}</span>;
}`;
    expect(componentLocalMappingViolations('web/src/components/ResumeCard.tsx', fixture))
      .toContain('component-local-source-mapping');
    expect(componentLocalMappingViolations(
      'web/src/components/ResumeCard.tsx',
      readProduction('web/src/components/ResumeCard.tsx'),
    )).toEqual([]);
  });
});
