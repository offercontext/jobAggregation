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

/**
 * Explicitly records destructive Task 6 entrypoint removals. This is a strict
 * lexical manifest consumed by the baseline gate; it is not a runtime alias
 * and cannot make an old owner reachable again.
 */
export const CORE_TASK_ENTRYPOINT_CUTOVERS = Object.freeze({
  'AppShellContent.preparePilotMaterials': Object.freeze({
    category: 'core_task',
    taskId: 'application.material_kit',
  }),
  'ApplicationDetail.openOpportunityFit': Object.freeze({
    category: 'core_task',
    taskId: 'application.opportunity_fit',
  }),
  'ApplicationDetail.openMaterials': Object.freeze({
    category: 'core_task',
    taskId: 'application.material_kit',
  }),
  'AppShellContent.openPilotInterviewReview': Object.freeze({
    category: 'core_task',
    taskId: 'application.interview_review',
  }),
  'OfferCenterView.openNegotiation': Object.freeze({
    category: 'core_task',
    taskId: 'application.offer_review',
  }),
  'PilotOpportunityFitV2Card': Object.freeze({
    category: 'core_task',
    taskId: 'application.opportunity_fit',
  }),
  'InterviewReadinessCenter.actionFor': Object.freeze({
    category: 'core_task',
    taskId: 'application.interview_prepare',
  }),
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
