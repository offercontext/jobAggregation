import type { CoreTaskId } from './contracts';

export interface CoreTaskOwner {
  readonly ownerId: string;
}

export type CoreTaskRegistry = Readonly<Record<CoreTaskId, CoreTaskOwner>>;

/**
 * The single desktop owner for every registered CoreTaskId. Values are UI owner
 * identifiers only; this registry intentionally has no service or repository
 * dependencies.
 */
export const CORE_TASK_REGISTRY: CoreTaskRegistry = Object.freeze({
  'application.opportunity_fit': Object.freeze({ ownerId: 'application-opportunity-fit' }),
  'application.material_kit': Object.freeze({ ownerId: 'application-material-kit' }),
  'application.interview_prepare': Object.freeze({ ownerId: 'application-interview-prepare' }),
  'application.interview_review': Object.freeze({ ownerId: 'application-interview-review' }),
  'application.general_review': Object.freeze({ ownerId: 'application-general-review' }),
  'application.offer_review': Object.freeze({ ownerId: 'application-offer-review' }),
  'application.record_outcome': Object.freeze({ ownerId: 'application-record-outcome' }),
  'interview.free_practice': Object.freeze({ ownerId: 'interview-free-practice' }),
  'materials.resume': Object.freeze({ ownerId: 'materials-resume' }),
  'materials.story': Object.freeze({ ownerId: 'materials-story' }),
  'materials.reference': Object.freeze({ ownerId: 'materials-reference' }),
} as const);

export type CoreTaskOwnerLookupResult =
  | { readonly ok: true; readonly ownerId: string }
  | { readonly ok: false; readonly reason: 'task_owner_unavailable' };

const TASK_OWNER_UNAVAILABLE: CoreTaskOwnerLookupResult = Object.freeze({
  ok: false,
  reason: 'task_owner_unavailable',
});

/** Looks up an owner without falling back to another task or legacy surface. */
export function getCoreTaskOwner(taskId: unknown): CoreTaskOwnerLookupResult {
  try {
    if (typeof taskId !== 'string' || !Object.prototype.hasOwnProperty.call(CORE_TASK_REGISTRY, taskId)) {
      return TASK_OWNER_UNAVAILABLE;
    }
    return {
      ok: true,
      ownerId: CORE_TASK_REGISTRY[taskId as CoreTaskId].ownerId,
    };
  } catch {
    return TASK_OWNER_UNAVAILABLE;
  }
}

export const resolveCoreTaskOwner = getCoreTaskOwner;
