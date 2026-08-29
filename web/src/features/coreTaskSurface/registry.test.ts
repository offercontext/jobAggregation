import { describe, expect, it } from 'vitest';

import { CORE_TASK_IDS } from './contracts';
import {
  CORE_TASK_REGISTRY,
  getCoreTaskOwner,
  resolveCoreTaskOwner,
} from './registry';

describe('CORE_TASK_REGISTRY', () => {
  it('has exactly one frozen entry for every closed CoreTaskId', () => {
    expect(Object.keys(CORE_TASK_REGISTRY).sort()).toEqual([...CORE_TASK_IDS].sort());
    expect(Object.isFrozen(CORE_TASK_REGISTRY)).toBe(true);
    expect(Object.keys(CORE_TASK_REGISTRY)).toHaveLength(CORE_TASK_IDS.length);
  });

  it('assigns one stable unique owner ID to each task', () => {
    const owners = CORE_TASK_IDS.map((taskId) => CORE_TASK_REGISTRY[taskId]);

    expect(owners).toHaveLength(CORE_TASK_IDS.length);
    expect(owners.every((owner) => Object.keys(owner).length === 1)).toBe(true);
    expect(owners.every((owner) => typeof owner.ownerId === 'string' && owner.ownerId.length > 0)).toBe(true);
    expect(new Set(owners.map((owner) => owner.ownerId)).size).toBe(CORE_TASK_IDS.length);
  });

  it('looks up a registered owner without fallback', () => {
    expect(getCoreTaskOwner('application.material_kit')).toEqual({
      ok: true,
      ownerId: CORE_TASK_REGISTRY['application.material_kit'].ownerId,
    });
    expect(resolveCoreTaskOwner('materials.resume')).toEqual({
      ok: true,
      ownerId: CORE_TASK_REGISTRY['materials.resume'].ownerId,
    });
  });

  it.each([
    'application.not_registered',
    'materials.resume:resumeId=1',
    '',
    null,
    undefined,
    7,
    {},
  ] as const)('returns task_owner_unavailable for an unknown task: %s', (taskId) => {
    expect(getCoreTaskOwner(taskId)).toEqual({
      ok: false,
      reason: 'task_owner_unavailable',
    });
    expect(resolveCoreTaskOwner(taskId)).toEqual({
      ok: false,
      reason: 'task_owner_unavailable',
    });
  });

  it('does not expose mutable registry values', () => {
    for (const taskId of CORE_TASK_IDS) {
      expect(Object.isFrozen(CORE_TASK_REGISTRY[taskId])).toBe(true);
    }
  });
});
