import type {
  EditableMaterialKitStatus,
  MaterialKitContent,
} from '@/types/materialKit';

/**
 * State that belongs to the material-kit owner rather than to the drawer
 * surface.  Keeping it here means closing/unmounting a drawer cannot discard
 * a user draft or the identity of an unresolved confirmation attempt.
 */
export interface MaterialKitOwnerDraft {
  readonly hasLocalState: boolean;
  readonly resumeID?: number;
  readonly jdSnapshot: string;
  readonly jdVersionID?: number;
  readonly status: EditableMaterialKitStatus;
  readonly content: MaterialKitContent;
  readonly proposalAssertions: string;
  readonly draftDirty: boolean;
}

export interface MaterialKitOwnerConfirmation {
  readonly open: boolean;
  readonly key: string | null;
  readonly submittedAt: string;
  readonly error: string | null;
  readonly previewValid: boolean;
  readonly pending: boolean;
  readonly resultUnknown: boolean;
  readonly sourceConflict: boolean;
}

/**
 * A proposal write is a separate owner operation from evidence confirmation,
 * but it still belongs to the same application-scoped owner.  Keeping its
 * identity here means a destroyed review modal cannot lose an in-flight
 * accept/reject attempt or accidentally retry it for a new owner generation.
 */
export interface MaterialKitOwnerProposalOperation {
  readonly applicationId: number;
  readonly proposalId: number;
  readonly proposalSha256: string;
  readonly key: string;
  readonly pending: boolean;
  readonly resultUnknown: boolean;
  readonly sourceConflict: boolean;
}

export interface MaterialKitOwnerSnapshot {
  readonly applicationId: number;
  /** Zero means that no owner lease has been acquired for this application. */
  readonly generation: number;
  readonly draft: MaterialKitOwnerDraft | null;
  readonly confirmation: MaterialKitOwnerConfirmation | null;
  readonly proposal: MaterialKitOwnerProposalOperation | null;
}

export interface MaterialKitOwnerLease {
  readonly applicationId: number;
  readonly generation: number;
  read(): MaterialKitOwnerSnapshot;
  writeDraft(draft: MaterialKitOwnerDraft | null): boolean;
  patchDraft(patch: Partial<MaterialKitOwnerDraft>): boolean;
  writeConfirmation(confirmation: MaterialKitOwnerConfirmation | null): boolean;
  patchConfirmation(patch: Partial<MaterialKitOwnerConfirmation>): boolean;
  writeProposal(proposal: MaterialKitOwnerProposalOperation | null): boolean;
  patchProposal(patch: Partial<MaterialKitOwnerProposalOperation>): boolean;
  isCurrent(): boolean;
  release(): void;
}

export interface MaterialKitOwnerStore {
  acquire(applicationId: number): MaterialKitOwnerLease;
  getSnapshot(applicationId: number): MaterialKitOwnerSnapshot;
  subscribe(applicationId: number, listener: () => void): () => void;
  /** Bridge a standalone/mock owner into the same canonical snapshot. */
  updateTransientState(
    applicationId: number,
    state: Pick<MaterialKitOwnerConfirmation, 'pending' | 'resultUnknown' | 'sourceConflict'>,
  ): boolean;
  clear(): void;
}

interface Entry {
  applicationId: number;
  generation: number;
  draft: MaterialKitOwnerDraft | null;
  confirmation: MaterialKitOwnerConfirmation | null;
  proposal: MaterialKitOwnerProposalOperation | null;
  snapshot: MaterialKitOwnerSnapshot;
  listeners: Set<() => void>;
}

function cloneContent(content: MaterialKitContent): MaterialKitContent {
  const resumeAdvice = Object.freeze({
    summary: content.resume_advice?.summary || '',
    highlights: Object.freeze([...(content.resume_advice?.highlights || [])]),
    rewrite_bullets: Object.freeze([...(content.resume_advice?.rewrite_bullets || [])]),
    gaps: Object.freeze([...(content.resume_advice?.gaps || [])]),
    notes: content.resume_advice?.notes || '',
  });
  return Object.freeze({
    resume_advice: resumeAdvice,
    messages: Object.freeze((content.messages || []).map((message) => Object.freeze({ ...message }))),
    checklist: Object.freeze((content.checklist || []).map((item) => Object.freeze({ ...item }))),
  }) as MaterialKitContent;
}

function cloneDraft(draft: MaterialKitOwnerDraft | null): MaterialKitOwnerDraft | null {
  if (!draft) return null;
  return Object.freeze({
    ...draft,
    content: cloneContent(draft.content),
  });
}

function isValidPositiveId(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0;
}

function isValidContent(value: unknown): value is MaterialKitContent {
  try {
    if (!value || typeof value !== 'object') return false;
    const content = value as Record<string, unknown>;
    const advice = content.resume_advice;
    if (!advice || typeof advice !== 'object') return false;
    const adviceRecord = advice as Record<string, unknown>;
    return typeof adviceRecord.summary === 'string'
      && Array.isArray(adviceRecord.highlights)
      && adviceRecord.highlights.every((item) => typeof item === 'string')
      && Array.isArray(adviceRecord.rewrite_bullets)
      && adviceRecord.rewrite_bullets.every((item) => typeof item === 'string')
      && Array.isArray(adviceRecord.gaps)
      && adviceRecord.gaps.every((item) => typeof item === 'string')
      && typeof adviceRecord.notes === 'string'
      && Array.isArray(content.messages)
      && content.messages.every((item) => !!item && typeof item === 'object')
      && Array.isArray(content.checklist)
      && content.checklist.every((item) => !!item && typeof item === 'object');
  } catch {
    return false;
  }
}

function isValidDraft(value: MaterialKitOwnerDraft | null): boolean {
  try {
    return value === null || (
      typeof value === 'object'
      && typeof value.hasLocalState === 'boolean'
      && (value.resumeID === undefined || isValidPositiveId(value.resumeID))
      && typeof value.jdSnapshot === 'string'
      && (value.jdVersionID === undefined || isValidPositiveId(value.jdVersionID))
      && (value.status === 'draft' || value.status === 'ready')
      && isValidContent(value.content)
      && typeof value.proposalAssertions === 'string'
      && typeof value.draftDirty === 'boolean'
    );
  } catch {
    return false;
  }
}

function isValidConfirmation(value: MaterialKitOwnerConfirmation | null): boolean {
  try {
    return value === null || (
      typeof value === 'object'
      && typeof value.open === 'boolean'
      && (value.key === null || typeof value.key === 'string')
      && typeof value.submittedAt === 'string'
      && (value.error === null || typeof value.error === 'string')
      && typeof value.previewValid === 'boolean'
      && typeof value.pending === 'boolean'
      && typeof value.resultUnknown === 'boolean'
      && typeof value.sourceConflict === 'boolean'
    );
  } catch {
    return false;
  }
}

function isValidProposalOperation(value: MaterialKitOwnerProposalOperation | null): boolean {
  try {
    return value === null || (
      typeof value === 'object'
      && isValidPositiveId(value.applicationId)
      && isValidPositiveId(value.proposalId)
      && typeof value.proposalSha256 === 'string'
      && value.proposalSha256.length > 0
      && typeof value.key === 'string'
      && value.key.length > 0
      && typeof value.pending === 'boolean'
      && typeof value.resultUnknown === 'boolean'
      && typeof value.sourceConflict === 'boolean'
    );
  } catch {
    return false;
  }
}

function cloneConfirmation(confirmation: MaterialKitOwnerConfirmation | null): MaterialKitOwnerConfirmation | null {
  return confirmation ? Object.freeze({ ...confirmation }) : null;
}

function cloneProposal(proposal: MaterialKitOwnerProposalOperation | null): MaterialKitOwnerProposalOperation | null {
  return proposal ? Object.freeze({ ...proposal }) : null;
}

function freezeSnapshot(entry: Entry): MaterialKitOwnerSnapshot {
  return Object.freeze({
    applicationId: entry.applicationId,
    generation: entry.generation,
    draft: cloneDraft(entry.draft),
    confirmation: cloneConfirmation(entry.confirmation),
    proposal: cloneProposal(entry.proposal),
  });
}

function emptySnapshot(applicationId: number): MaterialKitOwnerSnapshot {
  return Object.freeze({
    applicationId,
    generation: 0,
    draft: null,
    confirmation: null,
    proposal: null,
  });
}

function notify(entry: Entry): void {
  entry.snapshot = freezeSnapshot(entry);
  for (const listener of entry.listeners) {
    try {
      listener();
    } catch {
      // One hostile or stale subscriber must not abort the owner write or
      // prevent the remaining surfaces from observing the new snapshot.
    }
  }
}

/**
 * A tiny application-scoped owner store.  Leases are generation checked so a
 * late response from an unmounted drawer cannot mutate a newly opened app or
 * a newly acquired owner generation.
 */
export function createMaterialKitOwnerStore(): MaterialKitOwnerStore {
  const entries = new Map<number, Entry>();
  const emptySnapshots = new Map<number, MaterialKitOwnerSnapshot>();
  const pendingListeners = new Map<number, Set<() => void>>();

  const getEntry = (applicationId: number): Entry | undefined => entries.get(applicationId);

  const createEntry = (applicationId: number): Entry => {
    const previous = entries.get(applicationId);
    const entry: Entry = {
      applicationId,
      generation: (previous?.generation || 0) + 1,
      draft: previous?.draft || null,
      confirmation: previous?.confirmation || null,
      proposal: previous?.proposal || null,
      snapshot: emptySnapshot(applicationId),
      listeners: previous?.listeners || pendingListeners.get(applicationId) || new Set(),
    };
    entries.set(applicationId, entry);
    emptySnapshots.delete(applicationId);
    entry.snapshot = freezeSnapshot(entry);
    return entry;
  };

  const acquire = (applicationId: number): MaterialKitOwnerLease => {
    if (!isValidPositiveId(applicationId)) {
      const snapshot = emptySnapshot(applicationId);
      return {
        applicationId,
        generation: 0,
        read: () => snapshot,
        writeDraft: () => false,
        patchDraft: () => false,
        writeConfirmation: () => false,
        patchConfirmation: () => false,
        writeProposal: () => false,
        patchProposal: () => false,
        isCurrent: () => false,
        release: () => undefined,
      };
    }
    const entry = createEntry(applicationId);
    notify(entry);

    let active = true;
    const leaseGeneration = entry.generation;
    const isCurrent = () => active && entries.get(applicationId) === entry && entry.generation === leaseGeneration;
    const ownsEntry = () => active && entries.get(applicationId) === entry;
    const lease: MaterialKitOwnerLease = {
      applicationId,
      generation: entry.generation,
      // Return a defensive copy for component callers.  `getSnapshot` below
      // remains stable for useSyncExternalStore, while a lease read cannot be
      // used to mutate the store accidentally.
      read: () => freezeSnapshot(entry),
      writeDraft: (draft) => {
        if (!ownsEntry() || !isValidDraft(draft)) return false;
        try {
          entry.draft = draft ? cloneDraft(draft) : null;
        } catch {
          return false;
        }
        notify(entry);
        return true;
      },
      patchDraft: (patch) => {
        if (!ownsEntry() || !entry.draft) return false;
        try {
          const nextDraft = { ...entry.draft, ...patch } as MaterialKitOwnerDraft;
          if (!isValidDraft(nextDraft)) return false;
          entry.draft = cloneDraft(nextDraft);
        } catch {
          return false;
        }
        notify(entry);
        return true;
      },
      writeConfirmation: (confirmation) => {
        if (!ownsEntry() || !isValidConfirmation(confirmation)) return false;
        try {
          entry.confirmation = cloneConfirmation(confirmation);
        } catch {
          return false;
        }
        notify(entry);
        return true;
      },
      patchConfirmation: (patch) => {
        if (!ownsEntry() || !entry.confirmation) return false;
        try {
          const nextConfirmation = { ...entry.confirmation, ...patch } as MaterialKitOwnerConfirmation;
          if (!isValidConfirmation(nextConfirmation)) return false;
          entry.confirmation = cloneConfirmation(nextConfirmation);
        } catch {
          return false;
        }
        notify(entry);
        return true;
      },
      writeProposal: (proposal) => {
        if (!ownsEntry() || !isValidProposalOperation(proposal)) return false;
        try {
          entry.proposal = cloneProposal(proposal);
        } catch {
          return false;
        }
        notify(entry);
        return true;
      },
      patchProposal: (patch) => {
        if (!ownsEntry() || !entry.proposal) return false;
        try {
          const nextProposal = { ...entry.proposal, ...patch } as MaterialKitOwnerProposalOperation;
          if (!isValidProposalOperation(nextProposal)) return false;
          entry.proposal = cloneProposal(nextProposal);
        } catch {
          return false;
        }
        notify(entry);
        return true;
      },
      isCurrent,
      release: () => {
        if (!active) return;
        active = false;
        // Once this generation is destroyed no callback is allowed to settle
        // the request.  Preserve its identity, but make the outcome explicit
        // so a future owner can recover instead of being stuck in `pending`.
        if (entries.get(applicationId) !== entry) return;
        let changed = false;
        if (entry.confirmation?.pending) {
          entry.confirmation = cloneConfirmation({
            ...entry.confirmation,
            pending: false,
            resultUnknown: true,
          });
          changed = true;
        }
        if (entry.proposal?.pending) {
          entry.proposal = cloneProposal({
            ...entry.proposal,
            pending: false,
            resultUnknown: true,
          });
          changed = true;
        }
        if (changed) notify(entry);
      },
    };
    return lease;
  };

  return {
    acquire,
    getSnapshot: (applicationId) => entries.get(applicationId)?.snapshot
      || emptySnapshots.get(applicationId)
      || (() => {
        const snapshot = emptySnapshot(applicationId);
        emptySnapshots.set(applicationId, snapshot);
        return snapshot;
      })(),
    subscribe: (applicationId, listener) => {
      const entry = getEntry(applicationId);
      const listeners = entry?.listeners || (() => {
        const next = pendingListeners.get(applicationId) || new Set<() => void>();
        pendingListeners.set(applicationId, next);
        return next;
      })();
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
        if (!entry && listeners.size === 0) pendingListeners.delete(applicationId);
      };
    },
    updateTransientState: (applicationId, state) => {
      if (typeof applicationId !== 'number' || !Number.isSafeInteger(applicationId) || applicationId <= 0) return false;
      if (typeof state?.pending !== 'boolean'
        || typeof state?.resultUnknown !== 'boolean'
        || typeof state?.sourceConflict !== 'boolean') return false;
      let entry = entries.get(applicationId);
      if (!entry) {
        if (!state.pending && !state.resultUnknown && !state.sourceConflict) return false;
        entry = createEntry(applicationId);
      }
      if (!entry.confirmation && !state.pending && !state.resultUnknown && !state.sourceConflict) return false;
      const existing = entry.confirmation || {
        open: false,
        key: null,
        submittedAt: '',
        error: null,
        previewValid: true,
        pending: false,
        resultUnknown: false,
        sourceConflict: false,
      } satisfies MaterialKitOwnerConfirmation;
      entry.confirmation = cloneConfirmation({ ...existing, ...state });
      notify(entry);
      return true;
    },
    clear: () => {
      for (const entry of entries.values()) {
        entry.listeners.clear();
      }
      entries.clear();
      emptySnapshots.clear();
      pendingListeners.clear();
    },
  };
}

export const materialKitOwnerStore = createMaterialKitOwnerStore();
