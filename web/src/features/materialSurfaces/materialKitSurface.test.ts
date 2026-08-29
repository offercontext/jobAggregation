import { describe, expect, it } from 'vitest';
import {
  projectMaterialKitSurface,
  type MaterialKitSurfaceInput,
} from './materialKitSurface';

const baseInput: MaterialKitSurfaceInput = {
  applicationId: 7,
  jd: { status: 'ready', value: { id: 3, text: 'Build reliable services' } },
  resumes: { status: 'ready', value: [{ id: 11, deletedAt: null }] },
  materialKit: { status: 'ready', value: null },
};

function project(overrides: Partial<MaterialKitSurfaceInput> = {}) {
  return projectMaterialKitSurface({ ...baseInput, ...overrides });
}

describe('projectMaterialKitSurface', () => {
  it.each([
    ['loading', { sourceState: 'loading' as const }, 'none'],
    ['missing JD', { jd: { status: 'absent' as const, value: null } }, 'open_jd'],
    ['missing Resume', { resumes: { status: 'ready' as const, value: [{ id: 11, deletedAt: null }, { id: 12, deletedAt: null }] }, selectedResumeId: null }, 'select_resume'],
    ['not generated', { materialKit: { status: 'ready' as const, value: null }, selectedResumeId: 11 }, 'generate'],
    ['dirty draft', { materialKit: { status: 'ready' as const, value: { status: 'draft' as const } }, draftDirty: true, selectedResumeId: 11 }, 'save'],
    ['waiting confirmation', { pendingState: 'pending' as const, selectedResumeId: 11 }, 'confirm'],
    ['result unknown', { pendingState: 'unknown' as const, selectedResumeId: 11 }, 'resolve'],
    ['source conflict', { sourceConflict: true, selectedResumeId: 11 }, 'resolve'],
    ['ready and unsubmitted', { materialKit: { status: 'ready' as const, value: { status: 'ready' as const } }, selectedResumeId: 11 }, 'record_submission'],
    ['submitted', { materialKit: { status: 'ready' as const, value: { status: 'submitted' as const } }, selectedResumeId: 11 }, 'view_submission'],
  ] as const)('maps %s to one primary action', (_name, overrides, actionId) => {
    const result = project(overrides);
    expect(result.primaryAction.id).toBe(actionId);
    const primaryActions = result.actions.filter((action) => action.primary);
    expect(primaryActions).toHaveLength(actionId === 'none' ? 0 : 1);
    if (actionId !== 'none') expect(primaryActions[0]).toEqual(result.primaryAction);
    expect(result.hasExecutableAction).toBe(actionId !== 'none');
  });

  it('does not treat an absent or invalid source as an empty material kit', () => {
    expect(project({ sourceState: 'error' }).state).toBe('unavailable');
    expect(project({ materialKit: { status: 'error', value: null } }).state).toBe('unavailable');
    expect(project({ jd: { status: 'ready', value: { id: 0, text: '' } } }).state).toBe('missing_jd');
    expect(project({ resumes: { status: 'absent', value: null } }).state).toBe('missing_resume');
    expect(project({
      materialKit: { status: 'ready', value: { applicationId: 99, status: 'ready' } },
    }).state).toBe('unavailable');
    expect(project({
      jd: { status: 'ready', value: { id: 3, text: 'Old JD', deleted: true } },
    }).state).toBe('missing_jd');
  });

  it('keeps primary action identity independent of handoff hints and freezes the model', () => {
    const result = project({ selectedResumeId: 11, suggestedResumeId: 12 });
    expect(result.key).toBe('application.material_kit:applicationId=7');
    expect(result.key).not.toContain('12');
    expect(Object.isFrozen(result)).toBe(true);
    expect(Object.isFrozen(result.actions)).toBe(true);
    expect(Object.isFrozen(result.primaryAction)).toBe(true);
  });

  it('does not auto-select a master resume when multiple resumes are visible', () => {
    const result = project({
      resumes: {
        status: 'ready',
        value: [
          { id: 11, deletedAt: null, isMaster: true },
          { id: 12, deletedAt: null, isMaster: false },
        ],
      },
      selectedResumeId: null,
    });
    expect(result.state).toBe('missing_resume');
    expect(result.primaryAction.id).toBe('select_resume');
  });

  it('requires an explicit resume selection even when exactly one resume is visible', () => {
    const result = project({ selectedResumeId: null });
    expect(result.state).toBe('missing_resume');
    expect(result.primaryAction.id).toBe('select_resume');
    expect(result.hasExecutableAction).toBe(true);
  });

  it('exposes no primary action while a source is loading or unavailable', () => {
    for (const overrides of [
      { sourceState: 'loading' as const },
      { sourceState: 'error' as const },
      { materialKit: { status: 'error' as const, value: null } },
    ]) {
      const result = project(overrides);
      expect(result.primaryAction.id).toBe('none');
      expect(result.primaryAction.primary).toBe(false);
      expect(result.actions.some((action) => action.primary)).toBe(false);
      expect(result.hasExecutableAction).toBe(false);
    }
  });

  it('fails closed for an invalid application or hostile source getter', () => {
    expect(project({ applicationId: 0 }).state).toBe('unavailable');
    const hostile = {
      get status(): string {
        throw new Error('do not expose source error');
      },
    };
    const result = project({ materialKit: hostile as never });
    expect(result.state).toBe('unavailable');
    expect(result.primaryAction.id).toBe('none');
    expect(result.reason).toBe('source_unavailable');
  });
});
