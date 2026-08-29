import { describe, expect, it } from 'vitest';
import { createMaterialKitHandoffStore } from './materialKitHandoff';

const handoff = {
  applicationId: 7,
  source: 'pilot' as const,
  hints: { suggestedResumeId: 11, suggestedJdVersionId: 3 },
};

describe('materialKitHandoffStore', () => {
  it('matches the application, freezes a minimal identity and consumes exactly once', () => {
    const store = createMaterialKitHandoffStore();
    store.write(handoff);

    const first = store.consumeMaterialKitHandoff(7);
    expect(first).toEqual(handoff);
    expect(Object.keys(first ?? {}).sort()).toEqual(['applicationId', 'hints', 'source']);
    expect(first).not.toBe(handoff);
    expect(Object.isFrozen(first)).toBe(true);
    expect(Object.isFrozen(first?.hints)).toBe(true);
    expect(first).not.toHaveProperty('jdText');
    expect(first).not.toHaveProperty('resumeId');
    expect(store.consumeMaterialKitHandoff(7)).toBeNull();
  });

  it('drops raw JD and invalid hint fields at the boundary', () => {
    const store = createMaterialKitHandoffStore();
    store.write({
      applicationId: 7,
      source: 'pilot',
      hints: {
        suggestedResumeId: 11,
        suggestedJdVersionId: 3,
        rawJd: 'must not cross the handoff boundary',
        resumeId: 12,
      },
      jdText: 'must not cross the handoff boundary',
      resumeId: 12,
    } as never);

    expect(store.consumeMaterialKitHandoff(7)).toEqual(handoff);
  });

  it('does not consume a handoff for another application', () => {
    const store = createMaterialKitHandoffStore();
    store.write(handoff);

    expect(store.consumeMaterialKitHandoff(8)).toBeNull();
    expect(store.consumeMaterialKitHandoff(7)).toEqual(handoff);
  });

  it('discards only the matching application handoff', () => {
    const store = createMaterialKitHandoffStore();
    store.write(handoff);

    expect(store.discardMaterialKitHandoff(8)).toBe(false);
    expect(store.consumeMaterialKitHandoff(7)).toEqual(handoff);

    store.write({ ...handoff, applicationId: 8, hints: { suggestedResumeId: 13 } });
    expect(store.discardMaterialKitHandoff(8)).toBe(true);
    expect(store.consumeMaterialKitHandoff(8)).toBeNull();
  });

  it('replaces pending state without exposing a mutable original', () => {
    const store = createMaterialKitHandoffStore();
    store.write(handoff);
    store.write({ ...handoff, applicationId: 8, hints: { suggestedResumeId: 14 } });

    expect(store.consumeMaterialKitHandoff(7)).toBeNull();
    expect(store.consumeMaterialKitHandoff(8)?.hints?.suggestedResumeId).toBe(14);
  });

  it('rejects unknown sources and clears a stale pending handoff', () => {
    const store = createMaterialKitHandoffStore();
    store.write(handoff);
    store.write({ applicationId: 7, source: 'legacy_inline_owner' } as never);

    expect(store.consumeMaterialKitHandoff(7)).toBeNull();
  });

  it('rejects malformed application identity without inventing a deep link', () => {
    const store = createMaterialKitHandoffStore();
    store.write({ applicationId: 7, source: 'pilot' });
    store.write({ applicationId: '7', source: 'pilot' } as never);

    expect(store.consumeMaterialKitHandoff(7)).toBeNull();
  });

  it('fails closed when a handoff getter throws', () => {
    const store = createMaterialKitHandoffStore();
    store.write({
      get applicationId(): number {
        throw new Error('hostile handoff');
      },
    } as never);
    expect(store.consumeMaterialKitHandoff(7)).toBeNull();
  });
});
