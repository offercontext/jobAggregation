import { describe, expect, it, vi } from 'vitest';
import type {
  MaterialKitOwnerDraft,
  MaterialKitOwnerConfirmation,
  MaterialKitOwnerProposalOperation,
} from './materialKitOwnerStore';
import { createMaterialKitOwnerStore } from './materialKitOwnerStore';

const draft: MaterialKitOwnerDraft = {
  hasLocalState: true,
  resumeID: 11,
  jdSnapshot: 'Backend services',
  jdVersionID: 3,
  status: 'ready',
  content: {
    resume_advice: { summary: 'summary', highlights: ['one'], rewrite_bullets: [], gaps: [], notes: '' },
    messages: [{ type: 'application_note', title: 'Note', body: 'body', notes: '' }],
    checklist: [{ id: 'one', label: 'One', done: false }],
  },
  proposalAssertions: 'I led the migration.',
  draftDirty: true,
};

const confirmation: MaterialKitOwnerConfirmation = {
  open: true,
  key: 'stable-session-key',
  submittedAt: '2026-08-30T10:00',
  error: null,
  previewValid: true,
  pending: true,
  resultUnknown: false,
  sourceConflict: false,
};

const proposal: MaterialKitOwnerProposalOperation = {
  applicationId: 7,
  proposalId: 3,
  proposalSha256: 'proposal-sha',
  key: '3:proposal-sha',
  pending: true,
  resultUnknown: false,
  sourceConflict: false,
};

describe('material kit owner store', () => {
  it('restores the complete draft and confirmation identity after release and reacquire', () => {
    const store = createMaterialKitOwnerStore();
    const first = store.acquire(7);
    expect(first.writeDraft(draft)).toBe(true);
    expect(first.writeConfirmation(confirmation)).toBe(true);
    expect(first.writeProposal(proposal)).toBe(true);
    first.release();

    const reopened = store.acquire(7);
    expect(reopened.read().draft).toEqual(draft);
    expect(reopened.read().confirmation).toMatchObject({
      key: confirmation.key,
      pending: false,
      resultUnknown: true,
    });
    expect(reopened.read().proposal).toMatchObject({
      key: proposal.key,
      pending: false,
      resultUnknown: true,
    });
    expect(reopened.read().generation).toBeGreaterThan(first.generation);
  });

  it('rejects stale writes and isolates another application', () => {
    const store = createMaterialKitOwnerStore();
    const oldLease = store.acquire(7);
    oldLease.writeDraft(draft);
    oldLease.writeConfirmation(confirmation);
    oldLease.writeProposal(proposal);
    const currentLease = store.acquire(7);
    const otherApp = store.acquire(8);

    expect(oldLease.isCurrent()).toBe(false);
    expect(oldLease.patchDraft({ jdSnapshot: 'late response' })).toBe(false);
    expect(oldLease.patchConfirmation({ key: 'late-key' })).toBe(false);
    expect(oldLease.patchProposal({ key: 'late-proposal' })).toBe(false);
    expect(currentLease.read().draft?.jdSnapshot).toBe('Backend services');
    expect(currentLease.read().confirmation?.key).toBe('stable-session-key');
    expect(currentLease.read().proposal?.key).toBe('3:proposal-sha');
    expect(otherApp.read().draft).toBeNull();
    expect(otherApp.read().confirmation).toBeNull();
  });

  it('keeps snapshots immutable to callers', () => {
    const store = createMaterialKitOwnerStore();
    const lease = store.acquire(7);
    lease.writeDraft(draft);
    const snapshot = lease.read();
    expect(Object.isFrozen(snapshot)).toBe(true);
    expect(Object.isFrozen(snapshot.draft)).toBe(true);
    expect(Object.isFrozen(snapshot.draft!.content)).toBe(true);
    expect(Object.isFrozen(snapshot.draft!.content.resume_advice)).toBe(true);
    expect(Object.isFrozen(snapshot.draft!.content.resume_advice.highlights)).toBe(true);
    expect(() => {
      (snapshot.draft!.content.resume_advice.highlights as string[]).push('mutated');
    }).toThrow();
    expect(lease.read().draft?.content.resume_advice.highlights).toEqual(['one']);
  });

  it('turns in-flight operations into explicit unknown when their generation is released', () => {
    const store = createMaterialKitOwnerStore();
    const lease = store.acquire(7);
    lease.writeConfirmation(confirmation);
    lease.writeProposal(proposal);

    lease.release();

    expect(store.getSnapshot(7).confirmation).toMatchObject({ pending: false, resultUnknown: true });
    expect(store.getSnapshot(7).proposal).toMatchObject({ pending: false, resultUnknown: true });
  });

  it('lets the canonical host bridge a standalone owner state without a local mirror', () => {
    const store = createMaterialKitOwnerStore();
    const listener = vi.fn();
    store.subscribe(7, listener);

    expect(store.updateTransientState(7, { pending: true, resultUnknown: false, sourceConflict: false })).toBe(true);
    expect(store.getSnapshot(7).confirmation).toMatchObject({ pending: true });
    expect(listener).toHaveBeenCalled();
  });

  it('isolates a hostile listener from owner writes and continues notifying peers', () => {
    const store = createMaterialKitOwnerStore();
    const peer = vi.fn();
    store.subscribe(7, () => {
      throw new Error('hostile listener');
    });
    store.subscribe(7, peer);
    const lease = store.acquire(7);

    expect(() => lease.writeDraft(draft)).not.toThrow();
    expect(peer).toHaveBeenCalled();
    expect(lease.read().draft).toEqual(draft);
  });

  it('does not acquire a writable owner for an invalid application id', () => {
    const store = createMaterialKitOwnerStore();
    const lease = store.acquire(0);

    expect(lease.generation).toBe(0);
    expect(lease.isCurrent()).toBe(false);
    expect(lease.writeDraft(draft)).toBe(false);
    expect(lease.writeConfirmation(confirmation)).toBe(false);
    expect(lease.writeProposal(proposal)).toBe(false);
    expect(store.getSnapshot(0).generation).toBe(0);
  });
});
