import { describe, expect, it } from 'vitest';

import { classifyEventLifecycle, classifyEventLifecycleV1, type EventLifecycleV1 } from './eventLifecycle';

describe('EventLifecycleV1', () => {
  it.each([
    ['todo', 'scheduled'], ['pending', 'scheduled'], ['scheduled', 'scheduled'],
    ['in_progress', 'in_progress'], ['done', 'completed'], ['completed', 'completed'],
    ['cancelled', 'cancelled'], ['deleted', 'cancelled'], ['soft_deleted', 'cancelled'],
  ] as const)('maps %s to %s', (status, expected: EventLifecycleV1) => {
    expect(classifyEventLifecycleV1(status)).toBe(expected);
    expect(classifyEventLifecycle(status)).toBe(expected);
  });

  it.each([undefined, null, '', 'unknown', 'TODO', 1, false, {}, Symbol('status')])('does not infer lifecycle for %s', (status) => {
    expect(() => classifyEventLifecycleV1(status)).not.toThrow();
    expect(classifyEventLifecycleV1(status)).toBe('unknown');
  });
});
