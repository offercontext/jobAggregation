export type MaterialKitHandoffSource =
  | 'interview_event_card'
  | 'application_task'
  | 'pilot'
  | 'deep_link';

export interface MaterialKitHandoffHints {
  readonly suggestedResumeId?: number;
  readonly suggestedJdVersionId?: number;
}

export interface MaterialKitHandoff {
  readonly applicationId: number;
  readonly hints?: MaterialKitHandoffHints;
  readonly source: MaterialKitHandoffSource;
}

/** Input is intentionally opaque so legacy callers cannot make raw source data part of the contract. */
export type MaterialKitHandoffInput = unknown;

function validId(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function readHint(record: Record<string, unknown>, key: string): number | undefined {
  const value = record[key];
  return validId(value) ? value : undefined;
}

function normalizeHandoff(value: MaterialKitHandoffInput): MaterialKitHandoff | null {
  if (!isRecord(value) || !validId(value.applicationId)) return null;

  if (value.source !== 'interview_event_card'
    && value.source !== 'application_task'
    && value.source !== 'pilot'
    && value.source !== 'deep_link') return null;

  const rawHints = isRecord(value.hints) ? value.hints : {};
  const suggestedResumeId = readHint(rawHints, 'suggestedResumeId');
  const suggestedJdVersionId = readHint(rawHints, 'suggestedJdVersionId');
  const normalizedHints: { suggestedResumeId?: number; suggestedJdVersionId?: number } = {};
  if (suggestedResumeId !== undefined) normalizedHints.suggestedResumeId = suggestedResumeId;
  if (suggestedJdVersionId !== undefined) normalizedHints.suggestedJdVersionId = suggestedJdVersionId;
  const hints = Object.keys(normalizedHints).length === 0 ? undefined : Object.freeze(normalizedHints);
  return Object.freeze({
    applicationId: value.applicationId,
    ...(hints ? { hints } : {}),
    source: value.source,
  });
}

export interface MaterialKitHandoffStore {
  write: (handoff: MaterialKitHandoffInput) => void;
  consumeMaterialKitHandoff: (applicationId: number) => MaterialKitHandoff | null;
  discardMaterialKitHandoff: (applicationId: number) => boolean;
  clear: () => void;
}

export function createMaterialKitHandoffStore(): MaterialKitHandoffStore {
  let pending: MaterialKitHandoff | null = null;
  return {
    write: (value) => {
      let normalized: MaterialKitHandoff | null = null;
      try {
        normalized = normalizeHandoff(value);
      } catch {
        normalized = null;
      }
      // An invalid handoff must not leave a stale launch target armed. This is
      // especially important when a caller switches application context.
      pending = normalized;
    },
    consumeMaterialKitHandoff: (applicationId) => {
      if (!pending || pending.applicationId !== applicationId) return null;
      const value = pending;
      pending = null;
      return value;
    },
    discardMaterialKitHandoff: (applicationId) => {
      if (!pending || pending.applicationId !== applicationId) return false;
      pending = null;
      return true;
    },
    clear: () => {
      pending = null;
    },
  };
}

export const materialKitHandoffStore = createMaterialKitHandoffStore();

export const writeMaterialKitHandoff = materialKitHandoffStore.write;
export const consumeMaterialKitHandoff = materialKitHandoffStore.consumeMaterialKitHandoff;
export const discardMaterialKitHandoff = materialKitHandoffStore.discardMaterialKitHandoff;
