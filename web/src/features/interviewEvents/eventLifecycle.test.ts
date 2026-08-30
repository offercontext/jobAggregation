import { describe, expect, it } from 'vitest';

import { classifyEventLifecycle, classifyEventLifecycleV1, type EventLifecycleV1 } from './eventLifecycle';

describe('EventLifecycleV1', () => {
  it('matches every alias and unknown case in the shared backend fixture', async () => {
    const fsModule = 'node:fs';
    const { readFileSync } = (await import(fsModule)) as {
      readFileSync: (path: URL, encoding: string) => string;
    };
    const fixture = JSON.parse(readFileSync(
      new URL('../../../../tests/fixtures/review_readiness/event_lifecycle_v1.json', import.meta.url),
      'utf8',
    )) as {
      contract: string;
      unknown_fallback: EventLifecycleV1;
      cases: Array<{ status: unknown; expected: EventLifecycleV1 }>;
    };

    expect(fixture.contract).toBe('event_lifecycle_v1');
    for (const { status, expected } of fixture.cases) {
      expect(classifyEventLifecycleV1(status)).toBe(expected);
      expect(classifyEventLifecycle(status)).toBe(expected);
    }
    expect(classifyEventLifecycleV1('not-in-the-fixture')).toBe(fixture.unknown_fallback);
  });

  it.each([undefined, null, '', 'unknown', 'TODO', 1, false, {}, Symbol('status')])('does not infer lifecycle for %s', (status) => {
    expect(() => classifyEventLifecycleV1(status)).not.toThrow();
    expect(classifyEventLifecycleV1(status)).toBe('unknown');
  });
});
