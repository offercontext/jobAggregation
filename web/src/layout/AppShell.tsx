import { Component, lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';
import { DndContext, PointerSensor, useSensor, useSensors } from '@dnd-kit/core';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Button, Layout, Spin, Tabs, message } from 'antd';
import { listApplications } from '@/services/applications';
import { listEvents } from '@/services/events';
import { listOffers } from '@/services/offers';
import { ONBOARDING_QUERY_KEY } from '@/services/onboarding';
import { listResumes } from '@/services/resumes';
import type { Application } from '@/types/application';
import type { ScheduleEvent } from '@/types/event';
import type { Offer } from '@/types/offer';
import type { ChatStartRequest, PilotActionRequest, PilotContextAttachment } from '@/types/chat';
import Sidebar from './Sidebar';
import TopBar, { type TopBarAction } from './TopBar';
import AddApplicationForm from '@/components/AddApplicationForm';
import ApplicationDetail from '@/components/ApplicationDetail';
import type { InterviewReviewProposalAttemptState } from '@/components/InterviewReviewProposalDrawer';
import type { InterviewKnowledgeCaptureDraft } from '@/components/InterviewKnowledgeCaptureDrawer';
import type { InterviewPreparationAttemptState, InterviewPreparationDraft, InterviewPreparationKnowledgeOption } from '@/components/InterviewPreparationProposalDrawer';
import InterviewStudio, { type InterviewStudioContext as RealInterviewStudioContext, type QuickPracticeStudioContext } from '@/features/interviewStudio/InterviewStudio';
import PilotOpportunityFitV2Card from '@/features/pilot/PilotOpportunityFitV2Card';
import { normalizeInterviewIndexItem } from '@/features/interviewEvents/interviewIndexContract';
import { projectInterviewEventCard, type InterviewEventCardModel } from '@/features/interviewEvents/interviewEventCard';
import {
  createOpportunityFitOwnerStore,
  type OpportunityFitOwnerProjection,
  type OpportunityFitOwnerStore,
} from '@/components/OpportunityFitReviewDrawer';
import { normalizeOpportunityFitHistoryDate } from '@/features/applicationTasks/opportunityFitHistory';
import { type InterviewStoryOpenDraft } from '@/components/InterviewStoryLibraryView';
import InterviewStoryDrawer, {
  createInterviewStoryDraft,
  type InterviewStoryDraft,
  type InterviewStoryDraftChangeContext,
} from '@/components/InterviewStoryDrawer';
import { type OfferNegotiationDraft } from '@/components/OfferNegotiationDrawer';
import { listOfferBindingState } from '@/components/offerWorkspaceModel';
import type { EvidenceTarget } from '@/components/ChatPanel/model';
import CommandPalette from './CommandPalette';
import { moduleTabsForView, type ViewMode } from './navigation';
import {
  pushWorkspaceView,
  readInitialWorkspaceView,
  subscribeToWorkspaceView,
} from './viewRoute';
import {
  derivePipelineInsights,
  toLegacyActionItems,
  type PipelineInsight,
} from '@/lib/pipelineInsights';
import { getPracticeStats } from '@/services/questions';
import type { ApplicationJdDraft } from '@/types/applicationJdVersion';
import type { AdaptivePracticeFocus, AdaptivePracticeOwnerDraft } from '@/types/adaptiveInterviewPractice';
import {
  isReviewReadinessDraftPending,
  isReviewReadinessDraftUnsaved,
  isSafeReviewReadinessTerminalDraft,
  type ProductActionOwnerDraft,
  type ProductActionUndoRequest,
  type ReviewReadinessOwnerDraft,
} from '@/features/reviewReadiness/contracts';
import { fetchConfirmedInterviewKnowledgeNotes } from '@/services/knowledge';
import { buildPilotPageContext } from '@/lib/pilotPageContext';
import {
  deriveNextStepSuggestions,
  type NextStepDestination,
  type NextStepFacts,
  type ReadonlyDestination,
  type SuggestionSessionState,
} from '@/lib/nextStepSuggestions';
import { PilotAttachmentProvider } from '@/features/pilot/PilotAttachmentContext';
import {
  usePilotAttachmentStore,
  type PilotAttachmentConversationKey,
} from '@/features/pilot/PilotAttachmentContext';
import { retainPilotAttachmentKey } from '@/features/pilot/attachmentHandoff';
import {
  type OnboardingAction,
  onboardingActionIntent,
} from '@/features/onboarding/actionRouting';
import dayjs from 'dayjs';
import PilotMascot, { type PilotMascotActivity } from '@/features/pilotMascot/PilotMascot';
import {
  readPilotMascotAnimationLevel,
  readPilotMascotZoom,
  readPilotMascotVisible,
  resetPilotMascotPosition,
  writePilotMascotAnimationLevel,
  writePilotMascotZoom,
  writePilotMascotVisible,
  type PilotMascotAnimationLevel,
} from '@/features/pilotMascot/pilotMascotPreference';
import {
  AssistantSurfaceProvider,
  useAssistantSurface,
  usePilotConversationController,
} from '@/features/assistantSurface/AssistantSurfaceProvider';
import HaruDock from '@/features/assistantSurface/HaruDock';
import PilotWorkspace from '@/features/assistantSurface/PilotWorkspace';
import {
  DEFAULT_APPLICATION_VIEW_STATE,
  type ApplicationViewState,
} from '@/components/KanbanBoard/applicationLifecycle';
import {
  createCoreTaskSurfaceController,
  launchCoreTask as launchCoreTaskViaController,
  requestCoreTaskClose,
  type ActiveCoreTask,
  type CoreTaskLaunchResult,
  type CoreTaskSurfaceController,
} from '@/features/coreTaskSurface/controller';
import type { TaskLaunchRequest } from '@/features/coreTaskSurface/contracts';

const { Content } = Layout;

export function adaptivePracticeOwnerIdentity(focus: AdaptivePracticeFocus | undefined): string {
  return focus ? `${focus.signalVersionId}:${focus.targetEventId}` : 'three-mode';
}

export function adaptivePracticeDraftGuard(
  drafts: Readonly<Record<string, AdaptivePracticeOwnerDraft>>,
  ownerGeneration: number,
): { pending: boolean; unsaved: boolean } {
  const relevant = Object.values(drafts).filter((draft) => draft.ownerGeneration === ownerGeneration);
  return {
    pending: relevant.some((draft) => draft.pendingOperation !== null || draft.resultUnknown),
    unsaved: relevant.some((draft) => Boolean(
      draft.answer || draft.reflection || draft.assessment || draft.startInput || draft.completionInput,
    )),
  };
}

export function transactReviewReadinessDraftSnapshot(
  current: Readonly<Record<string, ReviewReadinessOwnerDraft>>,
  ownerKey: string,
  draft: ReviewReadinessOwnerDraft | null,
  retireOwnerKey?: string,
): Record<string, ReviewReadinessOwnerDraft> | null {
  if (draft && draft.ownerKey !== ownerKey) return null;
  const retired = retireOwnerKey ? current[retireOwnerKey] : undefined;
  const installed = current[ownerKey];
  if (retireOwnerKey && draft && (
    !retired
    || retired.noteId !== draft.noteId
    || retired.proposalId !== draft.proposalId
    || retired.applicationId !== draft.applicationId
    || retired.ownerGeneration >= draft.ownerGeneration
  )) return null;
  if (!draft && !installed && !retired) return null;
  if (!draft && installed && retired && (
    installed.noteId !== retired.noteId
    || installed.proposalId !== retired.proposalId
    || installed.applicationId !== retired.applicationId
  )) return null;
  const next = { ...current };
  if (retireOwnerKey) delete next[retireOwnerKey];
  if (draft) next[ownerKey] = draft;
  else delete next[ownerKey];
  return next;
}

function reviewDraftCanonicalOwnerKey(draft: ReviewReadinessOwnerDraft): string {
  return `review:${draft.ownerGeneration}:${draft.noteId}:${draft.proposalId}`;
}

function sameReviewDraftIdentity(left: ReviewReadinessOwnerDraft, right: ReviewReadinessOwnerDraft): boolean {
  return left.noteId === right.noteId
    && left.proposalId === right.proposalId
    && left.applicationId === right.applicationId
    && left.ownerGeneration === right.ownerGeneration
    && left.ownerKey === right.ownerKey
    && left.ownerKey === reviewDraftCanonicalOwnerKey(left);
}

function sameSafeReviewTerminalLineage(left: ReviewReadinessOwnerDraft, right: ReviewReadinessOwnerDraft): boolean {
  return isSafeReviewReadinessTerminalDraft(left)
    && isSafeReviewReadinessTerminalDraft(right)
    && left.noteId === right.noteId
    && left.proposalId === right.proposalId
    && left.applicationId === right.applicationId
    && left.selectedFocusId === right.selectedFocusId
    && left.actionDraft?.operationId === right.actionDraft?.operationId
    && left.actionDraft?.status === right.actionDraft?.status
    && left.actionDraft?.result?.signal_id === right.actionDraft?.result?.signal_id
    && left.actionDraft?.result?.signal_version_id === right.actionDraft?.result?.signal_version_id;
}

function reviewUndoOriginGeneration(
  draft: ReviewReadinessOwnerDraft,
  request: ProductActionUndoRequest,
): number | null {
  const match = /^review:([1-9]\d*):([1-9]\d*):([1-9]\d*):(.+)$/.exec(request.originOwnerKey);
  if (!match || request.actionName !== 'save_review_readiness_signal') return null;
  const generation = Number(match[1]);
  const noteId = Number(match[2]);
  const proposalId = Number(match[3]);
  if (
    !Number.isSafeInteger(generation)
    || !Number.isSafeInteger(noteId)
    || !Number.isSafeInteger(proposalId)
    || generation > draft.ownerGeneration
    || noteId !== draft.noteId
    || proposalId !== draft.proposalId
    || !Number.isSafeInteger(draft.applicationId)
    || Number(draft.applicationId) <= 0
  ) return null;
  return generation;
}

function sameReviewUndoRequest(left: ProductActionUndoRequest, right: ProductActionUndoRequest): boolean {
  return left.ownerKey === right.ownerKey
    && left.originOwnerKey === right.originOwnerKey
    && left.parentOperationId === right.parentOperationId
    && left.actionName === right.actionName;
}

function authorizeInstalledReviewUndoTransition(
  installed: ReviewReadinessOwnerDraft,
  draft: ReviewReadinessOwnerDraft,
  request: ProductActionUndoRequest,
): boolean {
  const installedAction = installed.actionDraft;
  const incomingAction = draft.actionDraft;
  if (
    !installedAction
    || !incomingAction
    || !sameSafeReviewTerminalLineage(installed, draft)
    || incomingAction.status !== 'committed'
    || request.ownerKey !== incomingAction.ownerKey
    || request.parentOperationId !== incomingAction.operationId
    || request.actionName !== incomingAction.actionName
    || reviewUndoOriginGeneration(draft, request) === null
  ) return false;
  const existingRequest = installedAction.undoRequest;
  if (existingRequest) {
    if (!sameReviewUndoRequest(existingRequest, request)) return false;
  } else if (
    request.originOwnerKey !== installedAction.ownerKey
    || reviewUndoOriginGeneration(installed, request) !== installed.ownerGeneration
  ) return false;
  const pendingOrUnknown = incomingAction.undoStatus == null
    && incomingAction.undoRequest !== null
    && incomingAction.undoRequest !== undefined
    && sameReviewUndoRequest(incomingAction.undoRequest, request);
  const terminal = incomingAction.undoRequest == null
    && incomingAction.undoResultUnknown !== true
    && (incomingAction.undoStatus === 'committed' || incomingAction.undoStatus === 'failed');
  return pendingOrUnknown || terminal;
}

function routeLateReviewUndoSettlement(
  current: Readonly<Record<string, ReviewReadinessOwnerDraft>>,
  ownerKey: string,
  draft: ReviewReadinessOwnerDraft,
  lateUndoRequest: ProductActionUndoRequest,
): { ownerKey: string; draft: ReviewReadinessOwnerDraft } | null {
  if (
    ownerKey !== draft.ownerKey
    || reviewDraftCanonicalOwnerKey(draft) !== ownerKey
    || !isSafeReviewReadinessTerminalDraft(draft)
    || (!draft.actionDraft?.undoStatus && !draft.actionDraft?.undoResultUnknown)
    || lateUndoRequest.ownerKey !== draft.actionDraft.ownerKey
    || lateUndoRequest.originOwnerKey.length === 0
    || lateUndoRequest.parentOperationId !== draft.actionDraft.operationId
    || lateUndoRequest.actionName !== draft.actionDraft.actionName
  ) return null;
  const incomingAction = draft.actionDraft;
  const incomingUnknown = incomingAction.undoResultUnknown === true && incomingAction.undoStatus == null;
  const incomingTerminal = (incomingAction.undoStatus === 'committed' || incomingAction.undoStatus === 'failed')
    && incomingAction.undoResultUnknown !== true;
  if (
    (incomingUnknown && (
      !incomingAction.undoRequest
      || !sameReviewUndoRequest(incomingAction.undoRequest, lateUndoRequest)
    ))
    || (incomingTerminal && incomingAction.undoRequest != null)
    || (!incomingUnknown && !incomingTerminal)
  ) return null;
  const matches = Object.values(current).filter((candidate) => {
    const action = candidate.actionDraft;
    const undoRequest = action?.undoRequest;
    return candidate.ownerKey === reviewDraftCanonicalOwnerKey(candidate)
      && candidate.applicationId === draft.applicationId
      && candidate.noteId === draft.noteId
      && candidate.proposalId === draft.proposalId
      && isSafeReviewReadinessTerminalDraft(candidate)
      && action?.status === 'committed'
      && action.operationId === incomingAction.operationId
      && action.actionName === incomingAction.actionName
      && action.result?.signal_id === incomingAction.result?.signal_id
      && action.result?.signal_version_id === incomingAction.result?.signal_version_id
      && action.undoStatus == null
      && undoRequest?.ownerKey === action.ownerKey
      && undoRequest.originOwnerKey === lateUndoRequest.originOwnerKey
      && undoRequest.parentOperationId === action.operationId
      && undoRequest.actionName === action.actionName;
  });
  if (matches.length !== 1) return null;
  const target = matches[0]!;
  const targetAction = target.actionDraft!;
  const routed: ReviewReadinessOwnerDraft = {
    ...draft,
    ownerKey: target.ownerKey,
    ownerGeneration: target.ownerGeneration,
    actionDraft: {
      ...incomingAction,
      ownerKey: targetAction.ownerKey,
      undoRequest: incomingAction.undoRequest
        ? { ...incomingAction.undoRequest, ownerKey: targetAction.ownerKey }
        : null,
    },
  };
  return isSafeReviewReadinessTerminalDraft(routed)
    ? { ownerKey: target.ownerKey, draft: routed }
    : null;
}

export function authorizeReviewReadinessDraftTransaction(
  current: Readonly<Record<string, ReviewReadinessOwnerDraft>>,
  ownerKey: string,
  draft: ReviewReadinessOwnerDraft | null,
  retireOwnerKey: string | undefined,
  active: ActiveCoreTask | null,
  lateUndoRequest?: ProductActionUndoRequest,
): {
  readonly snapshot: Record<string, ReviewReadinessOwnerDraft>;
  readonly affectsCurrentOwner: boolean;
  readonly settledGeneration: number | null;
} | null {
  const installed = current[ownerKey];
  const undoOriginGeneration = draft && lateUndoRequest
    ? reviewUndoOriginGeneration(draft, lateUndoRequest)
    : null;
  if (draft && lateUndoRequest && undoOriginGeneration === null) return null;
  if (draft && !installed && !retireOwnerKey && lateUndoRequest) {
    const lateUndo = routeLateReviewUndoSettlement(current, ownerKey, draft, lateUndoRequest);
    if (lateUndo) {
      return {
        snapshot: { ...current, [lateUndo.ownerKey]: lateUndo.draft },
        affectsCurrentOwner: active?.generation === lateUndo.draft.ownerGeneration
          && active.ref.applicationId === lateUndo.draft.applicationId,
        settledGeneration: isReviewReadinessDraftPending(lateUndo.draft) ? null : undoOriginGeneration,
      };
    }
  }
  if (draft) {
    if (draft.ownerKey !== ownerKey || reviewDraftCanonicalOwnerKey(draft) !== ownerKey) return null;
    if (installed) {
      if (retireOwnerKey || !sameReviewDraftIdentity(installed, draft)) return null;
      if (lateUndoRequest && !authorizeInstalledReviewUndoTransition(installed, draft, lateUndoRequest)) return null;
    } else {
      const activeOwnsDraft = active?.ref.taskId === 'application.interview_review'
        && active.generation === draft.ownerGeneration
        && active.ref.applicationId === draft.applicationId;
      if (!activeOwnsDraft) return null;
      if (retireOwnerKey) {
        const retired = current[retireOwnerKey];
        if (!retired) return null;
        const recoveryMigration = active.recoveryGeneration === retired.ownerGeneration;
        const safeTerminalMigration = sameSafeReviewTerminalLineage(retired, draft);
        if (!recoveryMigration && !safeTerminalMigration) return null;
      }
    }
    const snapshot = transactReviewReadinessDraftSnapshot(current, ownerKey, draft, retireOwnerKey);
    if (!snapshot) return null;
    return {
      snapshot,
      affectsCurrentOwner: active?.generation === draft.ownerGeneration
        && active.ref.applicationId === draft.applicationId,
      settledGeneration: isSafeReviewReadinessTerminalDraft(draft) && !isReviewReadinessDraftPending(draft)
        ? undoOriginGeneration
          ?? (retireOwnerKey ? current[retireOwnerKey]?.ownerGeneration ?? draft.ownerGeneration : draft.ownerGeneration)
        : null,
    };
  }
  if (!installed || installed.ownerKey !== reviewDraftCanonicalOwnerKey(installed)) return null;
  if (retireOwnerKey) {
    const retired = current[retireOwnerKey];
    if (retired && (
      retired.noteId !== installed.noteId
      || retired.proposalId !== installed.proposalId
      || retired.applicationId !== installed.applicationId
    )) return null;
  }
  const snapshot = transactReviewReadinessDraftSnapshot(current, ownerKey, null, retireOwnerKey);
  if (!snapshot) return null;
  return {
    snapshot,
    affectsCurrentOwner: active?.generation === installed.ownerGeneration
      && active.ref.applicationId === installed.applicationId,
    settledGeneration: installed.ownerGeneration,
  };
}

function sameProductActionUndoRequest(
  left: ProductActionUndoRequest | null | undefined,
  right: ProductActionUndoRequest | null | undefined,
): boolean {
  return Boolean(left && right
    && left.ownerKey === right.ownerKey
    && left.originOwnerKey === right.originOwnerKey
    && left.parentOperationId === right.parentOperationId
    && left.actionName === right.actionName);
}

function canonicalInterviewStoryActionOwnerKey(draft: InterviewStoryDraft): string | null {
  return draft.attemptId === null
    ? null
    : `story:${draft.attemptId}:${draft.attemptGenerationRevision}:${draft.productActionGeneration}`;
}

function exactInterviewStoryUndoRequest(
  action: ProductActionOwnerDraft,
  request: ProductActionUndoRequest | null | undefined,
): request is ProductActionUndoRequest {
  return Boolean(request
    && request.ownerKey === action.ownerKey
    && request.originOwnerKey.length > 0
    && request.parentOperationId === action.operationId
    && request.actionName === action.actionName
    && request.actionName === 'confirm_interview_story');
}

function sameInterviewStoryScope(left: InterviewStoryDraft, right: InterviewStoryDraft): boolean {
  return left.entrypoint === right.entrypoint
    && left.applicationId === right.applicationId
    && left.reviewNoteId === right.reviewNoteId
    && left.targetStoryId === right.targetStoryId;
}

function sameInterviewStoryUndoEnvelope(left: InterviewStoryDraft, right: InterviewStoryDraft): boolean {
  const { productAction: _leftAction, ...leftEnvelope } = left;
  const { productAction: _rightAction, ...rightEnvelope } = right;
  return JSON.stringify(leftEnvelope) === JSON.stringify(rightEnvelope);
}

function sameInterviewStoryUndoAction(
  left: ProductActionOwnerDraft,
  right: ProductActionOwnerDraft,
): boolean {
  const {
    undoStatus: _leftUndoStatus,
    undoReplayed: _leftUndoReplayed,
    undoRequest: _leftUndoRequest,
    undoResultUnknown: _leftUndoResultUnknown,
    ...leftFrozen
  } = left;
  const {
    undoStatus: _rightUndoStatus,
    undoReplayed: _rightUndoReplayed,
    undoRequest: _rightUndoRequest,
    undoResultUnknown: _rightUndoResultUnknown,
    ...rightFrozen
  } = right;
  return JSON.stringify(leftFrozen) === JSON.stringify(rightFrozen);
}

export function authorizeInterviewStoryDraftUpdate(
  current: InterviewStoryDraft | undefined,
  next: InterviewStoryDraft | null,
  context?: InterviewStoryDraftChangeContext,
): boolean {
  if (next === null) return context === undefined;
  if (!current || !sameInterviewStoryScope(current, next)) return false;

  const currentAction = current.productAction;
  if (!context) {
    return !currentAction?.undoRequest && currentAction?.undoResultUnknown !== true;
  }

  const nextAction = next.productAction;
  const request = context.undoRequest;
  if (
    !currentAction
    || !nextAction
    || current.attemptId === null
    || current.attemptId !== next.attemptId
    || current.attemptGenerationRevision !== next.attemptGenerationRevision
    || current.productActionGeneration !== next.productActionGeneration
    || canonicalInterviewStoryActionOwnerKey(current) !== currentAction.ownerKey
    || canonicalInterviewStoryActionOwnerKey(next) !== nextAction.ownerKey
    || currentAction.ownerKey !== nextAction.ownerKey
    || currentAction.operationId !== nextAction.operationId
    || currentAction.actionName !== 'confirm_interview_story'
    || nextAction.actionName !== currentAction.actionName
    || !exactInterviewStoryUndoRequest(currentAction, request)
    || !sameInterviewStoryUndoEnvelope(current, next)
    || !sameInterviewStoryUndoAction(currentAction, nextAction)
  ) return false;

  const existingRequest = currentAction.undoRequest;
  if (existingRequest) {
    if (!sameProductActionUndoRequest(existingRequest, request)) return false;
  } else if (
    currentAction.status !== 'committed'
    || currentAction.undoStatus != null
    || currentAction.undoResultUnknown === true
    || request.originOwnerKey !== currentAction.ownerKey
  ) return false;

  const nextPendingOrUnknown = exactInterviewStoryUndoRequest(nextAction, nextAction.undoRequest)
    && sameProductActionUndoRequest(nextAction.undoRequest, request)
    && nextAction.undoStatus == null;
  const nextTerminal = nextAction.undoRequest == null
    && nextAction.undoResultUnknown !== true
    && (nextAction.undoStatus === 'committed' || nextAction.undoStatus === 'failed');
  return nextPendingOrUnknown || nextTerminal;
}

export function transactAdaptivePracticeDraftSnapshot(
  current: Readonly<Record<string, AdaptivePracticeOwnerDraft>>,
  ownerKey: string,
  draft: AdaptivePracticeOwnerDraft | null,
  retireOwnerKey?: string,
): Record<string, AdaptivePracticeOwnerDraft> | null {
  if (draft && draft.ownerKey !== ownerKey) return null;
  const retired = retireOwnerKey ? current[retireOwnerKey] : undefined;
  const installed = current[ownerKey];
  if (retireOwnerKey && draft && (
    !retired
    || retired.signalVersionId !== draft.signalVersionId
    || retired.targetEventId !== draft.targetEventId
    || retired.planId !== draft.planId
    || retired.ownerGeneration >= draft.ownerGeneration
  )) return null;
  if (!draft && !installed && !retired) return null;
  if (!draft && installed && retired && (
    installed.signalVersionId !== retired.signalVersionId
    || installed.targetEventId !== retired.targetEventId
    || installed.planId !== retired.planId
  )) return null;
  const next = { ...current };
  if (retireOwnerKey) delete next[retireOwnerKey];
  if (draft) next[ownerKey] = draft;
  else delete next[ownerKey];
  return next;
}

export function adaptivePracticeSubOwnerReplacementDenied(input: {
  currentFocus: AdaptivePracticeFocus | undefined;
  nextFocus: AdaptivePracticeFocus | undefined;
  drafts: Readonly<Record<string, AdaptivePracticeOwnerDraft>>;
  ownerGeneration: number;
  surfaceGuard: { pending: boolean; unsaved: boolean };
}): boolean {
  if (adaptivePracticeOwnerIdentity(input.currentFocus) === adaptivePracticeOwnerIdentity(input.nextFocus)) return false;
  const draftGuard = adaptivePracticeDraftGuard(input.drafts, input.ownerGeneration);
  return input.surfaceGuard.pending || input.surfaceGuard.unsaved || draftGuard.pending || draftGuard.unsaved;
}

export function adaptivePracticeGuardAfterOwnerLaunch(input: {
  launchKind: 'launched' | 'focused_existing';
  drafts: Readonly<Record<string, AdaptivePracticeOwnerDraft>>;
  ownerGeneration: number;
  recoveryOwnerGeneration?: number | null;
  nextFocus?: AdaptivePracticeFocus;
  surfaceGuard: { pending: boolean; unsaved: boolean };
}): { pending: boolean; unsaved: boolean } {
  if (input.launchKind === 'launched') {
    if (input.recoveryOwnerGeneration == null) return { pending: false, unsaved: false };
    const recoveryIdentity = adaptivePracticeOwnerIdentity(input.nextFocus);
    const recoverable = Object.values(input.drafts).filter((draft) => (
      draft.ownerGeneration === input.recoveryOwnerGeneration
      && (draft.pendingOperation !== null || draft.resultUnknown || (draft.planId !== null && Boolean(draft.answer || draft.reflection || draft.assessment)))
      && (draft.signalVersionId === null && draft.targetEventId === null
        ? recoveryIdentity === 'three-mode'
        : recoveryIdentity === `${draft.signalVersionId}:${draft.targetEventId}`)
    ));
    return {
      pending: recoverable.some((draft) => draft.pendingOperation !== null || draft.resultUnknown),
      unsaved: recoverable.some((draft) => Boolean(
        draft.answer || draft.reflection || draft.assessment || draft.startInput || draft.completionInput,
      )),
    };
  }
  const draftGuard = adaptivePracticeDraftGuard(input.drafts, input.ownerGeneration);
  return {
    pending: input.surfaceGuard.pending || draftGuard.pending,
    unsaved: input.surfaceGuard.unsaved || draftGuard.unsaved,
  };
}

export function settleRecoveredCoreTaskAfterGuardTransition(
  controller: CoreTaskSurfaceController,
  previous: { pending: boolean; unsaved: boolean },
  next: { pending: boolean; unsaved: boolean },
): void {
  if ((!previous.pending && !previous.unsaved) || next.pending || next.unsaved) return;
  const active = controller.getState().active;
  if (!active) return;
  controller.settleRecovery(active.recoveryGeneration ?? active.generation);
}

export function closeCoreTaskOwnerWithGuard(
  controller: CoreTaskSurfaceController,
  guard: { pending: boolean; unsaved: boolean },
): void {
  const active = controller.getState().active;
  if (!active) return;
  if (requestCoreTaskClose(controller, active, guard)) controller.markClosed(active.generation);
}

const PILOT_FIT_PROJECTION_STATUSES = new Set<OpportunityFitOwnerProjection['status']>([
  'idle',
  'pending',
  'result_unknown',
  'ready',
  'source_conflict',
  'unavailable',
]);
const PILOT_FIT_HISTORY_STATES = new Set<OpportunityFitOwnerProjection['historyState']>([
  'ready',
  'loading',
  'error',
  'absent',
]);

/** Keep the composition-root projection bounded even when a child is
 * replaced with an untyped implementation. AppShell stores only this frozen
 * view; the Drawer remains the owner of mutable fit state. */
function freezePilotFitProjection(value: unknown): OpportunityFitOwnerProjection | null {
  try {
    if (!value || typeof value !== 'object') return null;
    const projection = value as Record<string, unknown>;
    const applicationId = projection.applicationId;
    const status = projection.status;
    const summary = projection.summary;
    const historyState = projection.historyState;
    if (!Number.isSafeInteger(applicationId) || (applicationId as number) <= 0) return null;
    if (typeof status !== 'string' || !PILOT_FIT_PROJECTION_STATUSES.has(status as OpportunityFitOwnerProjection['status'])) return null;
    if (summary !== null && (typeof summary !== 'string' || summary.trim().length === 0 || summary.length > 8_000)) return null;
    if (typeof historyState !== 'string' || !PILOT_FIT_HISTORY_STATES.has(historyState as OpportunityFitOwnerProjection['historyState'])) return null;
    if (!Array.isArray(projection.history)) return null;
    if (projection.history.length > 100) return null;
    const history = projection.history.map((value) => {
      if (!value || typeof value !== 'object') throw new Error('invalid history projection');
      const item = value as Record<string, unknown>;
      if (typeof item.internalKey !== 'string' || item.internalKey.length === 0 || item.internalKey.length > 256) throw new Error('invalid history key');
      const createdAt = normalizeOpportunityFitHistoryDate(item.createdAt);
      if (!createdAt) throw new Error('invalid history date');
      if (typeof item.summary !== 'string' || item.summary.trim().length === 0 || item.summary.length > 8_000) throw new Error('invalid history summary');
      if (item.sourceState !== 'current' && item.sourceState !== 'source_changed' && item.sourceState !== 'unavailable') throw new Error('invalid history source');
      return Object.freeze({
        internalKey: item.internalKey,
        createdAt,
        summary: item.summary,
        sourceState: item.sourceState,
      });
    });
    return Object.freeze({
      applicationId: applicationId as number,
      status: status as OpportunityFitOwnerProjection['status'],
      summary: summary as string | null,
      history: Object.freeze(history),
      historyState: historyState as OpportunityFitOwnerProjection['historyState'],
    });
  } catch {
    return null;
  }
}

const KanbanBoard = lazy(() => import('@/components/KanbanBoard'));
const ApplicationListView = lazy(() => import('@/components/ApplicationListView'));
const CalendarView = lazy(() => import('@/components/CalendarView'));
const KnowledgeSourcesView = lazy(() => import('@/components/KnowledgeSourcesView'));
const ExperienceMaterialsView = lazy(() => import('@/components/ExperienceMaterialsView'));
const QuestionBankView = lazy(() => import('@/components/QuestionBankView'));
const OfferCenterView = lazy(() => import('@/components/OfferCenterView'));
const DashboardView = lazy(() => import('@/features/dashboard/DashboardView'));
const RemindersView = lazy(() => import('@/features/reminders/RemindersView'));
const InterviewV01View = lazy(() => import('@/components/InterviewV01View'));
const InterviewReadinessCenter = lazy(() => import('@/features/interviewReadiness/InterviewReadinessCenter'));
const VoiceCoachingGrowthView = lazy(() => import('@/components/VoiceCoachingGrowthView'));
const ResumeLibraryView = lazy(() => import('@/components/ResumeLibraryView'));
const SettingsView = lazy(() => import('@/components/SettingsView'));


export interface ApplicationOfferScope {
  readonly offers: Offer[] | undefined;
  readonly hasInvalidOwner: boolean;
}

export type PilotInterviewReviewIntent =
  | { readonly kind: 'choose'; readonly applicationId: number }
  | { readonly kind: 'event'; readonly applicationId: number; readonly eventId: number }
  | { readonly kind: 'invalid' };

type UniqueInterviewEventResult =
  | { readonly ok: true; readonly event: ScheduleEvent }
  | { readonly ok: false };

export type InterviewEventSourceState = 'loading' | 'error' | 'ready' | 'absent' | 'unknown';

export interface ExactInterviewTaskInput {
  readonly taskId: 'application.interview_prepare' | 'application.interview_review';
  readonly applicationId: number;
  readonly eventId: number;
  readonly events: unknown;
  readonly sourceState: InterviewEventSourceState;
  readonly now: number;
}

export type ExactInterviewTaskResult =
  | { readonly ok: true; readonly event: ScheduleEvent; readonly card: InterviewEventCardModel }
  | { readonly ok: false; readonly reason: 'source_unavailable' | 'event_unavailable' | 'event_not_executable' };

function resolveUniqueInterviewEvent(
  applicationId: number,
  eventId: number,
  events: readonly Pick<ScheduleEvent, 'id' | 'application_id' | 'event_type'>[],
): UniqueInterviewEventResult {
  if (!Number.isSafeInteger(applicationId) || applicationId <= 0
    || !Number.isSafeInteger(eventId) || eventId <= 0) return { ok: false };
  let matched: ScheduleEvent | null = null;
  try {
    if (!Array.isArray(events)) return { ok: false };
    for (let index = 0; index < events.length; index += 1) {
      if (!(index in events)) return { ok: false };
      const candidate = events[index];
      if (candidate === null || typeof candidate !== 'object') return { ok: false };
      const candidateId = candidate.id;
      if (!Number.isSafeInteger(candidateId) || candidateId <= 0) return { ok: false };
      if (candidateId !== eventId) continue;
      if (matched !== null
        || candidate.application_id !== applicationId
        || candidate.event_type !== 'interview') return { ok: false };
      matched = candidate as ScheduleEvent;
    }
  } catch {
    return { ok: false };
  }
  return matched ? { ok: true, event: matched } : { ok: false };
}

function projectExactInterviewEventCard(event: ScheduleEvent, now: number): InterviewEventCardModel | null {
  try {
    const source = event as unknown as Record<PropertyKey, unknown>;
    const scheduledAt = source.scheduled_at;
    const hasScheduleState = Object.prototype.hasOwnProperty.call(source, 'scheduled_at_state');
    const scheduledAtState = hasScheduleState
      ? source.scheduled_at_state
      : typeof scheduledAt === 'string' && scheduledAt.length > 0 ? 'present' : 'absent';
    const preparationAvailable = Object.prototype.hasOwnProperty.call(source, 'preparation_available')
      ? source.preparation_available
      : true;
    const normalized = normalizeInterviewIndexItem({
      application_id: source.application_id,
      event_id: source.id,
      company_name: source.company_name,
      position_name: source.position_name,
      scheduled_at: scheduledAt,
      scheduled_at_state: scheduledAtState,
      event_status: source.status,
      duration_minutes: source.duration_minutes,
      note_id: source.note_id ?? null,
      note_source_status: null,
      has_review_proposal: false,
      review_summary: null,
      has_confirmed_knowledge: false,
      preparation_available: preparationAvailable,
    });
    return projectInterviewEventCard(normalized, now);
  } catch {
    return null;
  }
}

/**
 * Validates an exact interview task at the same source/lifecycle/card boundary
 * used by the interview surfaces. A task cannot be launched from cached,
 * unresolved, terminal, expired, or otherwise non-executable event data.
 */
export function resolveExecutableInterviewTask(input: ExactInterviewTaskInput): ExactInterviewTaskResult {
  try {
    if (input.sourceState !== 'ready' || !Array.isArray(input.events)) {
      return { ok: false, reason: 'source_unavailable' };
    }
    const resolved = resolveUniqueInterviewEvent(
      input.applicationId,
      input.eventId,
      input.events as readonly Pick<ScheduleEvent, 'id' | 'application_id' | 'event_type'>[],
    );
    if (!resolved.ok) return { ok: false, reason: 'event_unavailable' };
    const card = projectExactInterviewEventCard(resolved.event, input.now);
    if (!card) return { ok: false, reason: 'event_not_executable' };
    const executable = input.taskId === 'application.interview_prepare'
      ? (card.lifecycle === 'scheduled' || card.lifecycle === 'in_progress')
        && (card.primaryAction === 'prepare' || card.primaryAction === 'enter_preparation')
      : card.lifecycle === 'completed'
        && (card.primaryAction === 'record_review' || card.primaryAction === 'view_review');
    return executable
      ? { ok: true, event: resolved.event, card }
      : { ok: false, reason: 'event_not_executable' };
  } catch {
    return { ok: false, reason: 'event_unavailable' };
  }
}

export interface InterviewPreparationSelection {
  readonly generation: number;
  readonly applicationId: number;
  readonly eventId: number;
  readonly resumeId: number;
}

/**
 * Resolve Pilot's review intent without guessing an Event identity. An
 * application-only request always remains a chooser request, even for a
 * singleton event; an explicit event must be present in the scoped read.
 */
export function resolvePilotInterviewReviewIntent(
  applicationId: number,
  eventId: number | undefined,
  events: readonly Pick<ScheduleEvent, 'id' | 'application_id' | 'event_type'>[],
): PilotInterviewReviewIntent {
  if (!Number.isSafeInteger(applicationId) || applicationId <= 0) return { kind: 'invalid' };
  if (eventId === undefined) return { kind: 'choose', applicationId };
  if (!Number.isSafeInteger(eventId) || eventId <= 0) return { kind: 'invalid' };
  return resolveUniqueInterviewEvent(applicationId, eventId, events).ok
    ? { kind: 'event', applicationId, eventId }
    : { kind: 'invalid' };
}

/**
 * Project the global Offer read into one Application scope. Foreign rows are
 * deliberately omitted, while malformed ownership remains visible as a
 * fail-closed source error instead of looking like an empty collection.
 */
export function scopeApplicationOffers(
  offers: readonly Offer[] | undefined,
  applicationId: number,
): ApplicationOfferScope {
  if (offers === undefined) return { offers: undefined, hasInvalidOwner: false };
  if (!Array.isArray(offers) || !Number.isSafeInteger(applicationId) || applicationId <= 0) {
    return { offers: [], hasInvalidOwner: true };
  }
  const scoped: Offer[] = [];
  let hasInvalidOwner = false;
  for (const candidate of offers) {
    let owner: unknown;
    try {
      owner = (candidate as Offer | undefined)?.application_id;
    } catch {
      hasInvalidOwner = true;
      continue;
    }
    if (typeof owner !== 'number' || !Number.isSafeInteger(owner) || owner <= 0) {
      hasInvalidOwner = true;
      continue;
    }
    if (owner === applicationId) scoped.push(candidate);
  }
  return { offers: scoped, hasInvalidOwner };
}

function interviewStoryDraftScope(input: InterviewStoryOpenDraft): string {
  const target = input.targetStoryId ?? 'new';
  const note = input.reviewNoteId ?? 'all-notes';
  return `${input.entrypoint}:story:${target}:note:${note}`;
}

class ViewErrorBoundary extends Component<{ children: ReactNode }, { hasError: boolean }> {
  state = { hasError: false };

  static getDerivedStateFromError() {
    return { hasError: true };
  }

  render() {
    if (this.state.hasError) {
      return (
        <div style={{ textAlign: 'center', padding: 48, color: 'var(--op-muted)' }}>
          <div style={{ marginBottom: 16 }}>View failed to load.</div>
          <Button onClick={() => window.location.reload()}>Reload</Button>
        </div>
      );
    }

    return this.props.children;
  }
}

export default function AppShell() {
  return (
    <PilotAttachmentProvider>
      <AssistantSurfaceProvider>
        <AppShellContent />
      </AssistantSurfaceProvider>
    </PilotAttachmentProvider>
  );
}

function AppShellContent() {
  const assistantSurface = useAssistantSurface();
  const pilotController = usePilotConversationController();
  const [offerNegotiationEntryPoint, setOfferNegotiationEntryPoint] = useState<'ui' | 'pilot'>('ui');
  const taskSurfaceGuardRef = useRef({ pending: false, unsaved: false });
  const pilotControllerRef = useRef(pilotController);
  const fitToMaterialTransitionRef = useRef<{ applicationId: number; generation: number } | null>(null);
  pilotControllerRef.current = pilotController;
  // Composition-root authority: every application task opener delegates to
  // this one generation-safe controller and no child creates another owner.
  const coreTaskControllerRef = useRef<CoreTaskSurfaceController | null>(null);
  if (!coreTaskControllerRef.current) {
    coreTaskControllerRef.current = createCoreTaskSurfaceController({
      hasPending: () => {
        const pilot = pilotControllerRef.current;
        return taskSurfaceGuardRef.current.pending
          || Boolean(
            pilot.pending
            || pilot.loading
            || pilot.activeRequestRef.current
            || pilot.activePendingRef.current
            || pilot.confirmPhase === 'saving'
            || taskSurfaceGuardRef.current.pending,
          );
      },
      hasUnsavedChanges: (active) => {
        const transition = fitToMaterialTransitionRef.current;
        if (
          transition
          && transition.applicationId === active.ref.applicationId
          && transition.generation === active.generation
          && active.ref.taskId === 'application.opportunity_fit'
        ) return false;
        return taskSurfaceGuardRef.current.unsaved;
      },
    });
  }
  const coreTaskController: CoreTaskSurfaceController = coreTaskControllerRef.current;
  const coreTaskSurfaceState = useSyncExternalStore(
    coreTaskController.subscribe,
    coreTaskController.getState,
    coreTaskController.getState,
  );
  const opportunityFitOwnerStoreRef = useRef<OpportunityFitOwnerStore | null>(null);
  if (!opportunityFitOwnerStoreRef.current) {
    opportunityFitOwnerStoreRef.current = createOpportunityFitOwnerStore();
  }
  const launchCoreTask = useCallback((request: TaskLaunchRequest): CoreTaskLaunchResult => {
    const result = launchCoreTaskViaController(coreTaskController, request);
    // Entrypoint affects only the renderer's draft bucket. A duplicate focus
    // must retain the existing bucket and never reset an active owner.
    if (result.kind === 'launched' && request.ref.taskId === 'application.offer_review') {
      setOfferNegotiationEntryPoint(request.source === 'pilot' ? 'pilot' : 'ui');
    }
    return result;
  }, [coreTaskController]);
  const launchConfirmedFitToMaterial = useCallback((request: TaskLaunchRequest): CoreTaskLaunchResult => {
    const active = coreTaskController.getState().active;
    const transition = active
      && active.ref.taskId === 'application.opportunity_fit'
      && active.ref.applicationId === request.ref.applicationId
      && request.ref.taskId === 'application.material_kit'
        ? { applicationId: request.ref.applicationId!, generation: active.generation }
        : null;
    fitToMaterialTransitionRef.current = transition;
    const result = launchCoreTask(request);
    fitToMaterialTransitionRef.current = null;
    return result;
  }, [coreTaskController, launchCoreTask]);
  const closeCoreTaskSurface = useCallback(() => {
    const active = coreTaskController.getState().active;
    const guardBeforeClose = taskSurfaceGuardRef.current;
    if (active) closeCoreTaskOwnerWithGuard(coreTaskController, guardBeforeClose);
    // Closing the visual owner must not discard an in-flight or uncertain
    // operation. Detail normally refreshes this ref before unmount; preserve
    // a guarded snapshot when the close happens in the same event turn.
    taskSurfaceGuardRef.current = guardBeforeClose.pending || guardBeforeClose.unsaved
      ? guardBeforeClose
      : { pending: false, unsaved: false };
    setInterviewPreparationSelection(null);
    if (active?.ref.taskId === 'interview.free_practice') {
      adaptivePracticeFocusRef.current = undefined;
      setAdaptivePracticeFocus(undefined);
    }
  }, [coreTaskController]);
  const [view, setView] = useState<ViewMode>(readInitialWorkspaceView);
  const [applicationViewState, setApplicationViewState] = useState<ApplicationViewState>(
    DEFAULT_APPLICATION_VIEW_STATE,
  );
  const [adaptivePracticeFocus, setAdaptivePracticeFocus] = useState<AdaptivePracticeFocus | undefined>();
  const adaptivePracticeFocusRef = useRef<AdaptivePracticeFocus | undefined>();
  const [adaptivePracticeDrafts, setAdaptivePracticeDrafts] = useState<Record<string, AdaptivePracticeOwnerDraft>>({});
  const adaptivePracticeDraftsRef = useRef<Record<string, AdaptivePracticeOwnerDraft>>({});
  const [addOpen, setAddOpen] = useState(false);
  const [resumeUploadRequestToken, setResumeUploadRequestToken] = useState(0);
  const [offerCreateRequestToken, setOfferCreateRequestToken] = useState(0);
  const [pilotMascotVisible, setPilotMascotVisible] = useState(readPilotMascotVisible);
  const [pilotMascotZoom, setPilotMascotZoom] = useState(readPilotMascotZoom);
  const [pilotMascotAnimationLevel, setPilotMascotAnimationLevel] = useState(readPilotMascotAnimationLevel);
  const [pilotMascotPositionResetToken, setPilotMascotPositionResetToken] = useState(0);
  const [systemReducedMotion, setSystemReducedMotion] = useState(() =>
    typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
  );
  const [pilotMascotActivity, setPilotMascotActivity] = useState<PilotMascotActivity>('idle');
  const [pilotApplicationContext, setPilotApplicationContext] = useState<number | null>(null);
  const [pilotFitProjections, setPilotFitProjections] = useState<Record<number, OpportunityFitOwnerProjection>>({});
  const [pilotInterviewReviewApplicationId, setPilotInterviewReviewApplicationId] = useState<number | null>(null);
  const [pilotInterviewPreparationApplicationId, setPilotInterviewPreparationApplicationId] = useState<number | null>(null);
  const [pilotInterviewPreparationEventId, setPilotInterviewPreparationEventId] = useState<number | null>(null);
  const [interviewPreparationSelection, setInterviewPreparationSelection] = useState<InterviewPreparationSelection | null>(null);
  const [interviewStudioContext, setInterviewStudioContext] = useState<(RealInterviewStudioContext | QuickPracticeStudioContext) | null>(null);
  const [interviewStudioHaruVisible, setInterviewStudioHaruVisible] = useState(false);
  const [interviewStudioEvidenceOpen, setInterviewStudioEvidenceOpen] = useState(true);
  const openQuickPracticeStudio = useCallback((context: QuickPracticeStudioContext) => {
    setInterviewStudioHaruVisible(false);
    setInterviewStudioContext(context);
  }, []);
  useEffect(() => {
    if (!interviewPreparationSelection) return;
    const active = coreTaskSurfaceState.active;
    if (
      active?.generation !== interviewPreparationSelection.generation
      || active.ref.taskId !== 'application.interview_prepare'
      || active.ref.applicationId !== interviewPreparationSelection.applicationId
      || active.ref.eventId !== interviewPreparationSelection.eventId
    ) {
      setInterviewPreparationSelection(null);
    }
  }, [coreTaskSurfaceState.active, interviewPreparationSelection]);
  const offerNegotiationDraftsRef = useRef(new Map<number, OfferNegotiationDraft>());
  const [offerNegotiationDrafts, setOfferNegotiationDrafts] = useState<Record<number, OfferNegotiationDraft>>({});
  const offerNegotiationPilotDraftsRef = useRef(new Map<number, OfferNegotiationDraft>());
  const [offerNegotiationPilotDrafts, setOfferNegotiationPilotDrafts] = useState<Record<number, OfferNegotiationDraft>>({});
  const [resumeOnboardingFocusToken, setResumeOnboardingFocusToken] = useState(0);
  const [pilotOnboardingFocusToken, setPilotOnboardingFocusToken] = useState(0);
  const nextPilotOnboardingFocusToken = useRef(0);
  const [selected, setSelected] = useState<Application | null>(null);
  const applicationJdDraftsRef = useRef(new Map<number, ApplicationJdDraft>());
  const [applicationJdDrafts, setApplicationJdDrafts] = useState<Record<number, ApplicationJdDraft>>({});
  const [interviewReviewProposalAttempts, setInterviewReviewProposalAttempts] = useState<Record<number, InterviewReviewProposalAttemptState>>({});
  const [reviewReadinessDrafts, setReviewReadinessDrafts] = useState<Record<string, ReviewReadinessOwnerDraft>>({});
  const reviewReadinessDraftsRef = useRef<Record<string, ReviewReadinessOwnerDraft>>({});
  const [interviewKnowledgeCaptureDrafts, setInterviewKnowledgeCaptureDrafts] = useState<Record<number, InterviewKnowledgeCaptureDraft>>({});
  const [interviewPreparationAttempts, setInterviewPreparationAttempts] = useState<Record<string, InterviewPreparationAttemptState>>({});
  const [interviewPreparationDrafts, setInterviewPreparationDrafts] = useState<Record<string, InterviewPreparationDraft>>({});
  const [interviewStoryLibraryOpen, setInterviewStoryLibraryOpen] = useState(false);
  const [voiceCoachingGrowthOpen, setVoiceCoachingGrowthOpen] = useState(false);
  const [interviewStoryDrawerOpen, setInterviewStoryDrawerOpen] = useState(false);
  const [interviewStoryLibraryRevision, setInterviewStoryLibraryRevision] = useState(0);
  const interviewStoryDraftsRef = useRef(new Map<string, InterviewStoryDraft>());
  const [interviewStoryDrafts, setInterviewStoryDrafts] = useState<Record<string, InterviewStoryDraft>>({});
  const [activeInterviewStoryDraftScope, setActiveInterviewStoryDraftScope] = useState<string | null>(null);
  const [evidenceFocus, setEvidenceFocus] = useState<Exclude<EvidenceTarget, { kind: 'application' }> | null>(null);
  const [coachOfferId, setCoachOfferId] = useState<number | undefined>(undefined);
  const [chatStartRequest, setChatStartRequest] = useState<ChatStartRequest>();
  const [activePilotAttachmentKey, setActivePilotAttachmentKey] = useState<PilotAttachmentConversationKey>();
  const [pendingAttachmentDraftKey, setPendingAttachmentDraftKey] = useState<PilotAttachmentConversationKey>();
  const pilotAttachmentDraftKey = pendingAttachmentDraftKey;
  const pendingAttachmentDraftKeyRef = useRef<PilotAttachmentConversationKey>();
  const nextChatStartRequestKey = useRef(0);
  const consumedChatStartRequestKeysRef = useRef(new Set<number>());
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [now, setNow] = useState(() => dayjs());
  const contentRef = useRef<HTMLElement | null>(null);
  const currentViewRef = useRef(view);
  const skipNextHistorySyncRef = useRef(false);
  const hasMountedRouteRef = useRef(false);
  currentViewRef.current = view;
  const [pilotRailAvailable, setPilotRailAvailable] = useState(() =>
    typeof window === 'undefined' ? false : window.matchMedia('(min-width: 1180px)').matches
  );
  const kanbanSensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
  );
  const { addAttachment: addAttachmentToKey, createNewDraftWithAttachment } = usePilotAttachmentStore();

  const { data: applications = [], isLoading, isError: appsError } = useQuery({
    queryKey: ['applications'],
    queryFn: () => listApplications(),
  });
  const { data: eventsData, isLoading: eventsLoading, isError: eventsError } = useQuery({
    queryKey: ['events'],
    queryFn: () => listEvents(),
  });
  const { data: offersData, isLoading: offersLoading, isError: offersError } = useQuery({
    queryKey: ['offers'],
    queryFn: () => listOffers(),
  });
  const { data: practiceStats, isLoading: practiceLoading, isError: practiceError } = useQuery({
    queryKey: ['questions', 'stats'],
    queryFn: () => getPracticeStats(),
    retry: false,
  });
  const { data: resumesData, isLoading: resumesLoading, isError: resumesError } = useQuery({
    queryKey: ['resumes'],
    queryFn: listResumes,
    enabled: true,
  });
  const {
    data: confirmedInterviewKnowledgeNotesData,
    isLoading: confirmedInterviewKnowledgeNotesLoading,
    isError: confirmedInterviewKnowledgeNotesError,
  } = useQuery({
    queryKey: ['knowledge', 'confirmed-interview-notes'],
    queryFn: fetchConfirmedInterviewKnowledgeNotes,
    staleTime: 30000,
  });
  const confirmedInterviewKnowledgeNotes = confirmedInterviewKnowledgeNotesData ?? [];
  const interviewPreparationKnowledgeOptions = useMemo<InterviewPreparationKnowledgeOption[]>(
    () => confirmedInterviewKnowledgeNotes
      .filter((note) => note.source_status === 'frozen')
      .flatMap((note) => (note.evidence ?? []).map((evidence) => ({
        evidence_id: evidence.id,
        note_version_id: note.version_id,
        label: `${note.title} · ${evidence.path}`,
        excerpt: evidence.excerpt,
      }))),
    [confirmedInterviewKnowledgeNotes],
  );


  // Backend serializes an empty []T slice as JSON `null` (Go encoding/json).
  // React Query's `= []` default only applies when data is `undefined`, so an
  // explicit null-coalesce is needed to keep downstream iterators safe.
  const apps = Array.isArray(applications) ? applications : [];
  const evs = Array.isArray(eventsData) ? eventsData : [];
  const eventSourceState: InterviewEventSourceState = eventsLoading
    ? 'loading'
    : eventsError
      ? 'error'
      : Array.isArray(eventsData)
        ? 'ready'
        : 'unknown';
  const ofrs = Array.isArray(offersData) ? offersData : [];
  const resumes = Array.isArray(resumesData) ? resumesData : [];
  const [suggestionSessionStates, setSuggestionSessionStates] = useState<Record<string, SuggestionSessionState>>({});

  const updatePilotFitProjection = useCallback((projection: OpportunityFitOwnerProjection) => {
    const safeProjection = freezePilotFitProjection(projection);
    if (!safeProjection) return;
    setPilotFitProjections((current) => ({ ...current, [safeProjection.applicationId]: safeProjection }));
  }, []);

  const buildNextStepFacts = (applicationId: number): NextStepFacts => {
    const application = apps.find((item) => item.id === applicationId);
    const offerScope = scopeApplicationOffers(offersData, applicationId);
    return {
      application: application
        ? { status: 'known', value: application }
        : { status: 'unknown', reason: 'not_visible' },
      availableResumes: resumesData === undefined
        ? { status: 'unknown', reason: 'not_loaded' }
        : { status: 'known', value: resumes },
      events: eventsData === undefined
        ? { status: 'unknown', reason: 'not_loaded' }
        : { status: 'known', value: evs.filter((event) => event.application_id === applicationId) },
      offers: offersData === undefined
        ? { status: 'unknown', reason: 'not_loaded' }
        : offerScope.hasInvalidOwner
          ? { status: 'unknown', reason: 'not_visible' }
          : { status: 'known', value: offerScope.offers ?? [] },
      confirmedKnowledge: confirmedInterviewKnowledgeNotesData === undefined
        ? { status: 'unknown', reason: 'not_loaded' }
        : { status: 'known', value: confirmedInterviewKnowledgeNotes },
      practiceStats: practiceStats === undefined
        ? { status: 'unknown', reason: 'not_loaded' }
        : { status: 'known', value: practiceStats },
      jd: { status: 'unknown', reason: 'not_supported' },
      fitReview: { status: 'unknown', reason: 'not_loaded' },
      materialKit: { status: 'unknown', reason: 'not_loaded' },
      interviewPreparationHistory: { status: 'unknown', reason: 'not_loaded' },
      mockInterviewHistory: { status: 'unknown', reason: 'not_loaded' },
    };
  };

  const updateSuggestionSessionState = (
    applicationId: number,
    suggestionId: string,
    state: SuggestionSessionState | null,
  ) => {
    // The session scope is applicationId + suggestionId; it is never persisted.
    const key = `${applicationId}:${suggestionId}`;
    setSuggestionSessionStates((current) => {
      const next = { ...current };
      if (state?.stateKey) next[key] = state;
      else delete next[key];
      return next;
    });
  };

  const updateApplicationJdDraft = useCallback((applicationId: number, patch: Partial<ApplicationJdDraft> | null) => {
    const current = applicationJdDraftsRef.current.get(applicationId) ?? {
      jdText: '',
      sourceUrl: '',
      expectedCurrentVersionId: null,
      idempotencyKey: null,
      resultUnknown: false,
      pendingOperation: null,
    };
    if (patch === null) {
      applicationJdDraftsRef.current.delete(applicationId);
      setApplicationJdDrafts((state) => {
        const next = { ...state };
        delete next[applicationId];
        return next;
      });
      return;
    }
    const next = { ...current, ...patch };
    applicationJdDraftsRef.current.set(applicationId, next);
    setApplicationJdDrafts((state) => ({ ...state, [applicationId]: next }));
  }, []);

  const qc = useQueryClient();
  const refreshWorkspaceData = () => {
    void qc.invalidateQueries({ queryKey: ['applications'] });
    void qc.invalidateQueries({ queryKey: ['events'] });
    void qc.invalidateQueries({ queryKey: ['calendar'] });
    void qc.invalidateQueries({ queryKey: ['offers'] });
    void qc.invalidateQueries({ queryKey: ['questions', 'stats'] });
    void qc.invalidateQueries({ queryKey: ['chat', 'conversations'] });
    void qc.invalidateQueries({ queryKey: ONBOARDING_QUERY_KEY });
  };

  useEffect(() => {
    const id = window.setInterval(() => setNow(dayjs()), 60_000);
    return () => window.clearInterval(id);
  }, []);

  useEffect(() => {
    const media = window.matchMedia('(min-width: 1180px)');
    const sync = () => setPilotRailAvailable(media.matches);
    sync();
    media.addEventListener('change', sync);
    return () => media.removeEventListener('change', sync);
  }, []);

  useEffect(() => {
    const media = window.matchMedia('(prefers-reduced-motion: reduce)');
    const sync = () => setSystemReducedMotion(media.matches);
    sync();
    media.addEventListener('change', sync);
    return () => media.removeEventListener('change', sync);
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setPaletteOpen((v) => !v);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  useEffect(() => subscribeToWorkspaceView((nextView) => {
    if (nextView === currentViewRef.current) return;
    skipNextHistorySyncRef.current = true;
    closeCoreTaskSurface();
    setSelected(null);
    setEvidenceFocus(null);
    setView(nextView);
  }), [closeCoreTaskSurface]);

  useEffect(() => {
    if (!hasMountedRouteRef.current) {
      hasMountedRouteRef.current = true;
      return;
    }
    if (skipNextHistorySyncRef.current) {
      skipNextHistorySyncRef.current = false;
      return;
    }
    pushWorkspaceView(view);
  }, [view]);

  const pipelineActions = useMemo(
    () => derivePipelineInsights({ apps, events: evs, offers: ofrs, practiceStats, weeklyTarget: 6, now }),
    [apps, evs, ofrs, practiceStats, now]
  );
  const actions = useMemo(() => toLegacyActionItems(pipelineActions), [pipelineActions]);

  const selectedApp = selected
    ? apps.find((a) => a.id === selected.id) ?? null
    : null;
  const selectedNextStepSuggestions = selectedApp
    ? deriveNextStepSuggestions(buildNextStepFacts(selectedApp.id), 'detail', now.toDate())
    : null;
  const selectedNextStepCandidate = selectedNextStepSuggestions?.candidates[0];
  const selectedNextStepSessionState = selectedNextStepCandidate
    ? suggestionSessionStates[`${selectedApp?.id}:${selectedNextStepCandidate.id}`] ?? null
    : null;

  const hasLivePilotWork = () => {
    const pilot = pilotControllerRef.current;
    return Boolean(
      pilot.pending
      || pilot.loading
      || pilot.activeRequestRef.current
      || pilot.activePendingRef.current
      || pilot.confirmPhase === 'saving'
      || taskSurfaceGuardRef.current.pending,
    );
  };

  useEffect(() => {
    if (!selectedApp || !selectedNextStepCandidate) return;
    const key = `${selectedApp.id}:${selectedNextStepCandidate.id}`;
    setSuggestionSessionStates((current) => {
      const existing = current[key];
      if (!existing || existing.stateKey === selectedNextStepCandidate.stateKey) return current;
      const next = { ...current };
      delete next[key];
      return next;
    });
  }, [selectedApp, selectedNextStepCandidate]);
  const coachedOffer = ofrs.find((offer) => offer.id === coachOfferId);
  const pageContext = useMemo(
    () =>
      buildPilotPageContext({
        view,
        selectedApplication: selectedApp ?? undefined,
        coachedOffer,
      }),
    [view, selectedApp, coachedOffer]
  );
  const moduleTabs = moduleTabsForView(view);
  const selectedOfferScope = selectedApp
    ? scopeApplicationOffers(offersData, selectedApp.id)
    : { offers: offersData, hasInvalidOwner: false };
  const pilotOfferScope = pilotApplicationContext
    ? scopeApplicationOffers(offersData, pilotApplicationContext)
    : { offers: ofrs, hasInvalidOwner: false };
  const assistantTaskWorkActive = Boolean(
    pilotController.pending
    || pilotController.loading
    || pilotController.activeRequestRef.current
    || pilotController.activePendingRef.current
    || pilotController.confirmPhase === 'saving'
    || taskSurfaceGuardRef.current.pending,
  );
  const activeCoreTask = coreTaskSurfaceState.active;
  const externalTaskBlockedForDetail = Boolean(
    assistantTaskWorkActive
    && (!selectedApp || !activeCoreTask || activeCoreTask.ref.applicationId !== selectedApp.id),
  );

  useEffect(() => {
    if (pageContext) pilotController.setFollowingContext(pageContext);
  }, [pageContext, pilotController.setFollowingContext]);

  useEffect(() => {
    if (view !== 'pilot' || !pilotController.pending) return;
    const focusPending = window.setTimeout(() => {
      const proposal = document.querySelector<HTMLElement>(
        '[data-pilot-surface-host] [role="group"][aria-label="AI 修改提议"]',
      );
      const focusable = proposal?.querySelector<HTMLElement>(
        'input:not(:disabled), textarea:not(:disabled), select:not(:disabled), button:not(:disabled), [tabindex]:not([tabindex="-1"])',
      );
      focusable?.focus();
    }, 0);
    return () => window.clearTimeout(focusPending);
  }, [pilotController.pending, view]);

  useEffect(() => {
    window.scrollTo({ top: 0, left: 0 });
    const focusMain = window.setTimeout(() => contentRef.current?.focus({ preventScroll: true }), 0);
    return () => window.clearTimeout(focusMain);
  }, [selectedApp?.id, view]);

  useEffect(() => {
    if (selected && !apps.some((app) => app.id === selected.id)) {
      closeCoreTaskSurface();
      setSelected(null);
    }
  }, [apps, closeCoreTaskSurface, selected]);

  const shouldShowContextualPilot = view !== 'pilot';
  const contextualPilotRailMode = shouldShowContextualPilot
    && pilotRailAvailable
    && assistantSurface.surface === 'mascot'
    && !pilotMascotVisible;
  const contextualPilotOpen = assistantSurface.surface === 'pilot_workspace' || contextualPilotRailMode;
  const pilotControllerTransportActive = view === 'pilot'
    || contextualPilotOpen
    || assistantSurface.surface === 'haru_chat'
    || pilotController.loading
    || pilotController.confirmPhase === 'saving'
    || Boolean(pilotController.pending);

  const openChat = (offerId?: number) => {
    setCoachOfferId(offerId);
    if (view === 'pilot') {
      setView('dashboard');
    }
    assistantSurface.openHaru();
  };

  const setPilotMascotPreference = (visible: boolean) => {
    writePilotMascotVisible(visible);
    setPilotMascotVisible(visible);
    if (!visible) assistantSurface.closeSurface();
  };

  const setPilotMascotZoomPreference = (zoom: number) => {
    writePilotMascotZoom(zoom);
    setPilotMascotZoom(zoom);
  };

  const setPilotMascotAnimationPreference = (level: PilotMascotAnimationLevel) => {
    writePilotMascotAnimationLevel(level);
    setPilotMascotAnimationLevel(level);
  };

  const resetPilotMascotPositionPreference = () => {
    resetPilotMascotPosition('normal');
    setPilotMascotPositionResetToken((token) => token + 1);
  };

  const attachToPilot = (attachment: PilotContextAttachment) => {
    const attachmentKey =
      activePilotAttachmentKey ?? pendingAttachmentDraftKeyRef.current ?? pendingAttachmentDraftKey;
    if (attachmentKey) {
      pendingAttachmentDraftKeyRef.current = attachmentKey;
      setPendingAttachmentDraftKey(attachmentKey);
      addAttachmentToKey(attachmentKey, attachment);
      return;
    }
    const key = createNewDraftWithAttachment(attachment);
    pendingAttachmentDraftKeyRef.current = key;
    setPendingAttachmentDraftKey(key);
  };

  const syncPilotAttachmentKey = (key?: PilotAttachmentConversationKey) => {
    setActivePilotAttachmentKey((currentKey) => retainPilotAttachmentKey(currentKey, key));
    if (key) {
      pendingAttachmentDraftKeyRef.current = undefined;
      setPendingAttachmentDraftKey(undefined);
    }
  };

  const handoffPilotAttachmentDraft = () => {
    const attachmentKey =
      activePilotAttachmentKey ?? pendingAttachmentDraftKeyRef.current ?? pendingAttachmentDraftKey;
    if (!attachmentKey) return;
    pendingAttachmentDraftKeyRef.current = attachmentKey;
    setPendingAttachmentDraftKey(attachmentKey);
  };

  const startApplicationChat = (application: Application, action?: PilotActionRequest) => {
    if (action?.type === 'application_submission_snapshot' || action?.type === 'application_outcome_record') {
      const task = launchCoreTaskViaController(coreTaskController, {
        ref: { taskId: 'application.record_outcome', applicationId: application.id },
        source: 'haru',
        focus: 'current',
      });
      if (task.kind === 'invalid' || task.kind === 'unavailable' || task.kind === 'replacement_denied') return;
      setSelected(application);
      setView('board');
      if (task.kind === 'focused_existing') return;
    }
    const initialMessage = action?.type === 'application_submission_snapshot'
      ? '确认本次实际投递材料'
      : action?.type === 'application_outcome_record'
        ? '确认记录本次投递结果'
        : '保存岗位资料';
    setCoachOfferId(undefined);
    setChatStartRequest({
      requestKey: ++nextChatStartRequestKey.current,
      context_type: 'application',
      context_ref: String(application.id),
      context_label: `${application.company_name} · ${application.position_name}`,
      mode: 'general',
      ...(action ? { initialMessage, pilot_action: action } : {}),
    });
    if (view !== 'pilot') {
      assistantSurface.openHaru();
    }
  };

  const claimChatStartRequest = (requestKey: number) => {
    if (consumedChatStartRequestKeysRef.current.has(requestKey)) return false;
    consumedChatStartRequestKeysRef.current.add(requestKey);
    setChatStartRequest((current) => (current?.requestKey === requestKey ? undefined : current));
    return true;
  };

  const navigateToView = (nextView: ViewMode, { preserveEvidenceFocus = false }: { preserveEvidenceFocus?: boolean } = {}) => {
    const activeBeforeNavigation = coreTaskController.getState().active;
    closeCoreTaskSurface();
    if (activeBeforeNavigation?.ref.taskId === 'interview.free_practice') {
      setInterviewStudioContext(null);
      setInterviewStudioHaruVisible(false);
    }
    setSelected(null);
    if (!preserveEvidenceFocus) setEvidenceFocus(null);
    if (nextView === 'pilot') {
      assistantSurface.openPilot();
    } else if (view === 'pilot' && assistantSurface.surface === 'pilot_workspace') {
      assistantSurface.closeSurface();
    }
    setView(nextView);
  };

  const previousAssistantSurfaceRef = useRef(assistantSurface.surface);
  useEffect(() => {
    const becamePilot = previousAssistantSurfaceRef.current !== 'pilot_workspace'
      && assistantSurface.surface === 'pilot_workspace';
    previousAssistantSurfaceRef.current = assistantSurface.surface;
    if (becamePilot && view !== 'pilot') {
      setCoachOfferId(undefined);
      setSelected(null);
      setView('pilot');
      return;
    }
    if (!becamePilot && assistantSurface.surface === 'pilot_workspace' && view !== 'pilot') {
      assistantSurface.closeSurface();
    }
  }, [assistantSurface.closeSurface, assistantSurface.surface, view]);

  const consumePilotOnboardingFocus = (token: number) => {
    setPilotOnboardingFocusToken((current) => (current === token ? 0 : current));
  };

  const handleOnboardingAction = (action: OnboardingAction) => {
    const intent = onboardingActionIntent(action, pilotRailAvailable);
    if (intent.view) navigateToView(intent.view);
    if (intent.openApplicationForm) setAddOpen(true);
    if (intent.focusResumeEntry) setResumeOnboardingFocusToken((token) => token + 1);
    if (intent.openPilotDrawer) assistantSurface.openHaru();
    if (intent.focusPilot) {
      nextPilotOnboardingFocusToken.current += 1;
      setPilotOnboardingFocusToken(nextPilotOnboardingFocusToken.current);
    }
  };

  const openApplicationDetail = (app: Application) => {
    const active = coreTaskController.getState().active;
    if (active && active.ref.applicationId !== app.id) {
      if (taskSurfaceGuardRef.current.pending || taskSurfaceGuardRef.current.unsaved || hasLivePilotWork()) {
        message.warning('当前任务还有未完成内容，请先处理后再切换投递');
        return;
      }
      closeCoreTaskOwnerWithGuard(coreTaskController, { pending: false, unsaved: false });
      taskSurfaceGuardRef.current = { pending: false, unsaved: false };
    }
    if (!active || active.ref.applicationId !== app.id) setPilotApplicationContext(null);
    setSelected(app);
  };

  const updateInterviewReviewProposalAttempt = (
    noteId: number,
    state: InterviewReviewProposalAttemptState | null,
  ) => {
    setInterviewReviewProposalAttempts((current) => {
      const next = { ...current };
      // Keep the running attempt as well as an uncertain result in the
      // composition-root guard. The Drawer sends `result_unknown: false`
      // before its request starts; dropping that state would let a second
      // owner replace an in-flight review.
      if (state) next[noteId] = state;
      else delete next[noteId];
      return next;
    });
  };

  const clearInterviewReviewProposalAttempt = (noteId: number) => {
    setInterviewReviewProposalAttempts((current) => {
      if (!(noteId in current)) return current;
      const next = { ...current };
      delete next[noteId];
      return next;
    });
  };

  const updateInterviewKnowledgeCaptureDraft = (
    noteId: number,
    draft: InterviewKnowledgeCaptureDraft | null,
  ) => {
    setInterviewKnowledgeCaptureDrafts((current) => {
      const next = { ...current };
      if (draft) next[noteId] = draft;
      else delete next[noteId];
      return next;
    });
  };

  const clearInterviewKnowledgeCaptureDraft = (noteId: number) => {
    setInterviewKnowledgeCaptureDrafts((current) => {
      if (!(noteId in current)) return current;
      const next = { ...current };
      delete next[noteId];
      return next;
    });
  };

  const updateInterviewPreparationAttempt = (
    key: string,
    state: InterviewPreparationAttemptState | null,
  ) => {
    setInterviewPreparationAttempts((current) => {
      const next = { ...current };
      if (state) next[key] = state;
      else delete next[key];
      return next;
    });
  };

  const updateInterviewPreparationDraft = (
    key: string,
    draft: InterviewPreparationDraft | null,
  ) => {
    setInterviewPreparationDrafts((current) => {
      const next = { ...current };
      if (draft) next[key] = draft;
      else delete next[key];
      return next;
    });
    if (!draft) {
      setInterviewPreparationAttempts((current) => {
        if (!(key in current)) return current;
        const next = { ...current };
        delete next[key];
        return next;
      });
    }
  };

  const startPilotOpportunityFit = (app: Application) => {
    const launchResult = launchCoreTaskViaController(coreTaskController, {
      ref: { taskId: 'application.opportunity_fit', applicationId: app.id },
      source: 'pilot',
      focus: 'current',
    });
    if (launchResult.kind === 'invalid' || launchResult.kind === 'unavailable' || launchResult.kind === 'replacement_denied') return;
    setPilotApplicationContext(app.id);
    setSelected(app);
    setView('board');
  };

  const openPilotInterviewPreparation = (applicationId: number, eventId?: number) => {
    const app = apps.find((item) => item.id === applicationId);
    if (!app) return;
    const intent = eventId === undefined
      ? { kind: 'choose' as const, applicationId }
      : resolvePilotInterviewReviewIntent(applicationId, eventId, evs);
    if (eventId !== undefined && intent.kind !== 'event') {
      message.warning('指定的面试当前不可用，请从面试列表重新选择。');
      return;
    }
    const trustedEventId = intent.kind === 'event' ? intent.eventId : undefined;
    if (trustedEventId === undefined) {
      // Keep application-only intent in the explicit event chooser. No event
      // identity is inferred from ordering, time, or a singleton collection.
      const active = coreTaskController.getState().active;
      if (active && active.ref.applicationId !== applicationId
        && (taskSurfaceGuardRef.current.pending || taskSurfaceGuardRef.current.unsaved || hasLivePilotWork())) {
        message.warning('当前任务还有未完成内容，请先处理后再切换投递');
        return;
      }
      if (active && active.ref.applicationId !== applicationId) {
        closeCoreTaskOwnerWithGuard(coreTaskController, { pending: false, unsaved: false });
        taskSurfaceGuardRef.current = { pending: false, unsaved: false };
      }
    } else {
      const launchResult = openExactInterviewTask({
        ref: { taskId: 'application.interview_prepare', applicationId, eventId: trustedEventId },
        source: 'pilot',
        focus: 'current',
      });
      if (launchResult.kind === 'invalid' || launchResult.kind === 'unavailable' || launchResult.kind === 'replacement_denied') return;
      setPilotInterviewPreparationApplicationId(null);
      setPilotInterviewPreparationEventId(null);
      return;
    }
    setPilotApplicationContext(null);
    setPilotInterviewPreparationApplicationId(applicationId);
    setPilotInterviewPreparationEventId(trustedEventId ?? null);
    setView('board');
    openApplicationDetail(app);
  };

  function openExactInterviewTask(request: TaskLaunchRequest): CoreTaskLaunchResult {
    const { ref } = request;
    const applicationId = ref.applicationId;
    const eventId = ref.eventId;
    if (
      (ref.taskId !== 'application.interview_prepare' && ref.taskId !== 'application.interview_review')
      || !Number.isSafeInteger(applicationId)
      || (applicationId ?? 0) <= 0
      || !Number.isSafeInteger(eventId)
      || (eventId ?? 0) <= 0
    ) {
      return { kind: 'invalid', reason: 'invalid_task_identity' };
    }
    const app = apps.find((item) => item.id === applicationId);
    const exactEvent = resolveExecutableInterviewTask({
      taskId: ref.taskId,
      applicationId: applicationId!,
      eventId: eventId!,
      events: eventsData,
      sourceState: eventSourceState,
      now: now.valueOf(),
    });
    if (!app || !exactEvent.ok) {
      message.warning('指定的面试当前不可用，请刷新面试列表后重试。');
      return { kind: 'unavailable', reason: 'task_owner_unavailable' };
    }
    const result = launchCoreTask(request);
    if (result.kind !== 'launched' && result.kind !== 'focused_existing') return result;
    setPilotApplicationContext(null);
    setPilotInterviewPreparationApplicationId(null);
    setPilotInterviewPreparationEventId(null);
    if (ref.taskId === 'application.interview_prepare') {
      const suggestedResumeId = request.hints?.suggestedResumeId;
      const validResumeId = Number.isSafeInteger(suggestedResumeId) && (suggestedResumeId ?? 0) > 0
        ? suggestedResumeId as number
        : null;
      const existingSelection = interviewPreparationSelection
        && interviewPreparationSelection.generation === result.generation
        && interviewPreparationSelection.applicationId === applicationId
        && interviewPreparationSelection.eventId === eventId
        ? interviewPreparationSelection
        : null;
      if (validResumeId !== null) {
        setInterviewPreparationSelection(Object.freeze({
          generation: result.generation,
          applicationId: applicationId!,
          eventId: eventId!,
          resumeId: validResumeId,
        }));
        setSelected(app);
        setView('board');
      } else if (existingSelection) {
        setSelected(app);
        setView('board');
      } else {
        setInterviewPreparationSelection(null);
        setSelected(null);
        setVoiceCoachingGrowthOpen(false);
        setInterviewStoryLibraryOpen(false);
        setView('interview');
      }
      return result;
    }
    setInterviewPreparationSelection(null);
    setSelected(app);
    setView('board');
    return result;
  }

  function launchTaskFromApplicationDetail(request: TaskLaunchRequest): CoreTaskLaunchResult {
    return request.ref.taskId === 'application.interview_prepare'
      ? openExactInterviewTask(request)
      : launchCoreTask(request);
  }

  const launchAdaptivePracticeOwner = (
    nextFocus: AdaptivePracticeFocus | undefined,
    request: TaskLaunchRequest,
  ): CoreTaskLaunchResult => {
    const active = coreTaskController.getState().active;
    if (active?.ref.taskId === 'interview.free_practice') {
      if (adaptivePracticeSubOwnerReplacementDenied({
        currentFocus: adaptivePracticeFocusRef.current,
        nextFocus,
        drafts: adaptivePracticeDraftsRef.current,
        ownerGeneration: active.generation,
        surfaceGuard: taskSurfaceGuardRef.current,
      })) {
        message.warning('当前练习还有待确认或未保存内容，请先处理后再切换。');
        return { kind: 'replacement_denied', reason: 'replacement_guard_denied', generation: active.generation };
      }
    }
    const result = launchCoreTask({
      ...request,
      childOwnerIdentity: adaptivePracticeOwnerIdentity(nextFocus),
    });
    if (result.kind !== 'launched' && result.kind !== 'focused_existing') return result;
    const normalizedFocus = nextFocus ? { ...nextFocus, ownerGeneration: result.generation } : undefined;
    adaptivePracticeFocusRef.current = normalizedFocus;
    setAdaptivePracticeFocus(normalizedFocus);
    taskSurfaceGuardRef.current = adaptivePracticeGuardAfterOwnerLaunch({
      launchKind: result.kind,
      drafts: adaptivePracticeDraftsRef.current,
      ownerGeneration: result.generation,
      recoveryOwnerGeneration: coreTaskController.getState().active?.recoveryGeneration,
      nextFocus: normalizedFocus,
      surfaceGuard: taskSurfaceGuardRef.current,
    });
    return result;
  };

  const openFreePractice = (): CoreTaskLaunchResult => {
    const result = launchAdaptivePracticeOwner(undefined, {
      ref: { taskId: 'interview.free_practice' },
      source: 'deep_link',
      focus: 'current',
    });
    if (result.kind !== 'launched' && result.kind !== 'focused_existing') return result;
    setSelected(null);
    setEvidenceFocus(null);
    setVoiceCoachingGrowthOpen(false);
    setInterviewStoryLibraryOpen(false);
    setView('interview');
    return result;
  };

  const updateReviewReadinessDraft = (
    key: string,
    draft: ReviewReadinessOwnerDraft | null,
    retireOwnerKey?: string,
    undoRequest?: ProductActionUndoRequest,
  ): boolean => {
    const active = coreTaskController.getState().active;
    const authorized = authorizeReviewReadinessDraftTransaction(
      reviewReadinessDraftsRef.current, key, draft, retireOwnerKey, active, undoRequest,
    );
    if (!authorized) return false;
    reviewReadinessDraftsRef.current = authorized.snapshot;
    if (draft && authorized.affectsCurrentOwner) {
      const nextGuard = {
        pending: isReviewReadinessDraftPending(draft),
        unsaved: isReviewReadinessDraftUnsaved(draft),
      };
      settleRecoveredCoreTaskAfterGuardTransition(coreTaskController, taskSurfaceGuardRef.current, nextGuard);
      taskSurfaceGuardRef.current = nextGuard;
    }
    if (authorized.settledGeneration !== null) {
      coreTaskController.settleRecovery(authorized.settledGeneration);
    }
    if (!draft && authorized.affectsCurrentOwner) {
      settleRecoveredCoreTaskAfterGuardTransition(coreTaskController, taskSurfaceGuardRef.current, { pending: false, unsaved: false });
      taskSurfaceGuardRef.current = { pending: false, unsaved: false };
    }
    setReviewReadinessDrafts(authorized.snapshot);
    return true;
  };

  const updateAdaptivePracticeDraft = (key: string, draft: AdaptivePracticeOwnerDraft | null, retireOwnerKey?: string): boolean => {
    const active = coreTaskController.getState().active;
    if (draft && active?.ref.taskId === 'interview.free_practice' && draft.ownerGeneration !== active.generation) return false;
    if (retireOwnerKey && draft
      && active?.recoveryGeneration !== adaptivePracticeDraftsRef.current[retireOwnerKey]?.ownerGeneration) return false;
    const nextSnapshot = transactAdaptivePracticeDraftSnapshot(adaptivePracticeDraftsRef.current, key, draft, retireOwnerKey);
    if (!nextSnapshot) return false;
    adaptivePracticeDraftsRef.current = nextSnapshot;
    setAdaptivePracticeDrafts(nextSnapshot);
    return true;
  };

  const openReadinessPractice = (focus: AdaptivePracticeFocus): CoreTaskLaunchResult => {
    const result = launchAdaptivePracticeOwner(focus, {
      ref: { taskId: 'interview.free_practice' },
      source: 'application_task_card',
      focus: 'source',
    });
    if (result.kind !== 'launched' && result.kind !== 'focused_existing') return result;
    setSelected(null);
    setEvidenceFocus(null);
    setVoiceCoachingGrowthOpen(false);
    setInterviewStoryLibraryOpen(false);
    setView('interview');
    return result;
  };

  const openInterviewEventEditor = (applicationId: number, eventId: number) => {
    const resolved = resolveUniqueInterviewEvent(applicationId, eventId, evs);
    const event = resolved.ok ? resolved.event : null;
    if (!event || typeof event.scheduled_at !== 'string' || event.scheduled_at.length === 0) {
      message.warning('该面试日程当前不可编辑，请刷新后重试。');
      return;
    }
    setEvidenceFocus({ kind: 'event', id: event.id, scheduledAt: event.scheduled_at });
    navigateToView('calendar', { preserveEvidenceFocus: true });
  };

  const openOfferNegotiation = (offer: Offer, entrypoint: 'ui' | 'pilot' = 'ui') => {
    if (listOfferBindingState(offer) === 'unbound') {
      message.warning('历史未绑定 Offer 仅支持只读查看');
      return;
    }
    if (!Number.isSafeInteger(offer.id) || offer.id <= 0) {
      message.warning('当前 Offer 信息暂不可用');
      return;
    }
    const application = apps.find((item) => item.id === offer.application_id);
    if (!application) {
      message.warning('所属投递当前不可见');
      return;
    }
    const launchResult = launchCoreTaskViaController(coreTaskController, {
      ref: { taskId: 'application.offer_review', applicationId: application.id },
      source: entrypoint === 'pilot' ? 'pilot' : 'application_header',
      focus: 'current',
      hints: { suggestedOfferId: offer.id },
    });
    if (launchResult.kind === 'invalid' || launchResult.kind === 'unavailable' || launchResult.kind === 'replacement_denied') return;
    if (launchResult.kind === 'focused_existing') {
      setSelected(application);
      setView('board');
      return;
    }
    setOfferNegotiationEntryPoint(entrypoint);
    setSelected(application);
    setView('board');
  };

  const updateOfferNegotiationDraft = useCallback((offerId: number, draft: OfferNegotiationDraft | null) => {
    if (draft) {
      offerNegotiationDraftsRef.current.set(offerId, draft);
      setOfferNegotiationDrafts((current) => ({ ...current, [offerId]: draft }));
    } else {
      offerNegotiationDraftsRef.current.delete(offerId);
      setOfferNegotiationDrafts((current) => {
        const next = { ...current };
        delete next[offerId];
        return next;
      });
    }
  }, []);

  const handleOfferNegotiationDrawerDraftChange = useCallback((offerId: number, draft: OfferNegotiationDraft | null) => {
    if (draft) {
      offerNegotiationPilotDraftsRef.current.set(offerId, draft);
      setOfferNegotiationPilotDrafts((current) => ({ ...current, [offerId]: draft }));
    } else {
      offerNegotiationPilotDraftsRef.current.delete(offerId);
      setOfferNegotiationPilotDrafts((current) => {
        const next = { ...current };
        delete next[offerId];
        return next;
      });
    }
  }, []);

  const clearEvidenceFocus = (target: EvidenceTarget) => {
    setEvidenceFocus((current) => (current === target ? null : current));
  };

  const openEvidence = (target: EvidenceTarget) => {
    if (view === 'pilot' && !pilotRailAvailable) {
      assistantSurface.closeSurface();
    }
    if (target.kind === 'application') {
      setEvidenceFocus(null);
      const app = apps.find((item) => item.id === target.id);
      if (app) {
        if (view === 'pilot') setView('board');
        openApplicationDetail(app);
      } else {
        message.warning('引用的记录已不存在');
      }
      return;
    }

    setSelected(null);
    setEvidenceFocus(target);
    if (target.kind === 'offer') {
      navigateToView('offers', { preserveEvidenceFocus: true });
      return;
    }
    if (target.kind === 'resume') {
      navigateToView('resumes', { preserveEvidenceFocus: true });
      return;
    }
    navigateToView('calendar', { preserveEvidenceFocus: true });
  };

  const goDetailById = (appId: number) => {
    const app = apps.find((a) => a.id === appId);
    if (app) openApplicationDetail(app);
  };

  const openCanonicalApplication = (appId: number) => {
    const app = apps.find((item) => item.id === appId);
    if (!app) {
      message.warning('所属投递当前不可见');
      return;
    }
    const active = coreTaskController.getState().active;
    if (active
      && active.ref.applicationId !== app.id
      && (taskSurfaceGuardRef.current.pending || taskSurfaceGuardRef.current.unsaved || hasLivePilotWork())) {
      message.warning('当前任务还有未完成内容，请先处理后再切换投递');
      return;
    }
    navigateToView('board');
    openApplicationDetail(app);
  };

  const runPipelineAction = (item: PipelineInsight) => {
    if (item.primaryAction.target === 'board' && item.appId) {
      goDetailById(item.appId);
      return;
    }

    navigateToView(item.primaryAction.target);
  };

  const handleNextStepNavigate = (destination: NextStepDestination | ReadonlyDestination) => {
    switch (destination.kind) {
      case 'application_detail':
        goDetailById(destination.applicationId);
        return;
      case 'material_kit_entry': {
        const app = apps.find((item) => item.id === destination.applicationId);
        if (!app) return;
        const launchResult = launchCoreTaskViaController(coreTaskController, {
          ref: { taskId: 'application.material_kit', applicationId: destination.applicationId },
          source: 'deep_link',
          focus: 'current',
        });
        if (launchResult.kind === 'invalid' || launchResult.kind === 'unavailable' || launchResult.kind === 'replacement_denied') return;
        openApplicationDetail(app);
        return;
      }
      case 'pilot_opportunity_fit':
        {
          const app = apps.find((item) => item.id === destination.applicationId);
          if (app) startPilotOpportunityFit(app);
        }
        return;
      case 'interview_event':
        openPilotInterviewPreparation(destination.applicationId, destination.eventId);
        return;
      case 'interview_event_selection':
        goDetailById(destination.applicationId);
        return;
      case 'interview_review':
      case 'interview_review_history': {
        const app = apps.find((item) => item.id === destination.applicationId);
        if (!app) return;
        const launchResult = launchCoreTaskViaController(coreTaskController, {
          ref: {
            taskId: 'application.interview_review',
            applicationId: destination.applicationId,
            eventId: destination.eventId,
          },
          source: 'deep_link',
          focus: 'current',
        });
        if (launchResult.kind === 'invalid' || launchResult.kind === 'unavailable' || launchResult.kind === 'replacement_denied') return;
        openApplicationDetail(app);
        return;
      }
      case 'interview_review_selection':
        goDetailById(destination.applicationId);
        return;
      case 'opportunity_fit_history':
        {
          const app = apps.find((item) => item.id === destination.applicationId);
          if (!app) return;
        const launchResult = launchCoreTaskViaController(coreTaskController, {
            ref: { taskId: 'application.opportunity_fit', applicationId: destination.applicationId },
            source: 'deep_link',
            focus: 'history',
          });
          if (launchResult.kind === 'invalid' || launchResult.kind === 'unavailable' || launchResult.kind === 'replacement_denied') return;
          openApplicationDetail(app);
        }
        return;
    }
  };

  const handleNextStepReadonlyNavigate = (destination: ReadonlyDestination) => {
    handleNextStepNavigate(destination);
  };

  const isNextStepReadonlyNavigationAvailable = (destination: ReadonlyDestination) => (
    destination.kind === 'application_detail'
  );

  const isNextStepNavigationAvailable = (destination: NextStepDestination | ReadonlyDestination) => (
    ![
      'interview_review',
      'interview_review_history',
      'interview_review_selection',
      'opportunity_fit_history',
    ].includes(destination.kind)
  );

  const calendarEvidenceFocus = evidenceFocus?.kind === 'event' ? evidenceFocus : undefined;
  const offerEvidenceFocus = evidenceFocus?.kind === 'offer' ? evidenceFocus : undefined;
  const resumeEvidenceFocus = evidenceFocus?.kind === 'resume' ? evidenceFocus : undefined;

  const openInterviewStoryDraft = (
    input: InterviewStoryOpenDraft,
    { preserveView = false }: { preserveView?: boolean } = {},
  ) => {
    const scope = interviewStoryDraftScope(input);
    if (!preserveView) setView('interview');
    setVoiceCoachingGrowthOpen(false);
    setInterviewStoryLibraryOpen(true);
    setActiveInterviewStoryDraftScope(scope);
    setInterviewStoryDrafts((current) => {
      const existing = interviewStoryDraftsRef.current.get(scope);
      const next = existing ?? createInterviewStoryDraft(input.entrypoint, input.reviewNoteId, {
        applicationId: input.applicationId,
        targetStoryId: input.targetStoryId,
        expectedCurrentVersionId: input.expectedCurrentVersionId,
        expectedStoryRevision: input.expectedStoryRevision,
      });
      interviewStoryDraftsRef.current.set(scope, next);
      return { ...current, [scope]: next };
    });
    setInterviewStoryDrawerOpen(true);
  };

  const openVoiceCoachingGrowth = () => {
    setView('interview');
    setInterviewStoryLibraryOpen(false);
    setVoiceCoachingGrowthOpen(true);
  };

  const interviewStoryDraft = activeInterviewStoryDraftScope
    ? interviewStoryDrafts[activeInterviewStoryDraftScope] ?? null
    : null;

  const updateInterviewStoryDraft = (
    draft: InterviewStoryDraft | null,
    context?: InterviewStoryDraftChangeContext,
  ) => {
    const scope = activeInterviewStoryDraftScope;
    if (!scope) return false;
    const currentDraft = interviewStoryDraftsRef.current.get(scope);
    if (!authorizeInterviewStoryDraftUpdate(currentDraft, draft, context)) return false;
    if (draft === null) setInterviewStoryLibraryRevision((current) => current + 1);
    if (draft === null) interviewStoryDraftsRef.current.delete(scope);
    else interviewStoryDraftsRef.current.set(scope, draft);
    setInterviewStoryDrafts((current) => {
      const next = { ...current };
      if (draft === null) {
        delete next[scope];
      } else {
        next[scope] = draft;
      }
      return next;
    });
    return true;
  };

  const activeInterviewPreparation = coreTaskSurfaceState.active?.ref.taskId === 'application.interview_prepare'
    ? coreTaskSurfaceState.active
    : null;
  const activeInterviewPreparationApplication = activeInterviewPreparation?.ref.applicationId
    ? apps.find((item) => item.id === activeInterviewPreparation.ref.applicationId) ?? null
    : null;
  const activeInterviewPreparationEvent = activeInterviewPreparation?.ref.eventId
    ? (() => {
      const resolved = resolveUniqueInterviewEvent(
        activeInterviewPreparation.ref.applicationId ?? 0,
        activeInterviewPreparation.ref.eventId ?? 0,
        evs,
      );
      return resolved.ok ? resolved.event : null;
    })()
    : null;

  const workspaceContent = selectedApp ? (
    <ApplicationDetail
      application={selectedApp}
      taskController={coreTaskController}
      onLaunchTask={launchTaskFromApplicationDetail}
      onConfirmedFitToMaterial={launchConfirmedFitToMaterial}
      onTaskSurfaceGuardChange={(guard) => { taskSurfaceGuardRef.current = guard; }}
      taskNow={now.valueOf()}
      offers={selectedOfferScope.offers}
      offersLoading={offersLoading}
      offersError={offersError || selectedOfferScope.hasInvalidOwner}
      onRetryOffers={() => void qc.invalidateQueries({ queryKey: ['offers'] })}
      open
      onClose={() => {
        closeCoreTaskSurface();
        setSelected(null);
      }}
      onAskPilot={startApplicationChat}
      onOpenPilotOpportunityFit={startPilotOpportunityFit}
      pilotInterviewReviewApplicationId={pilotInterviewReviewApplicationId}
      onPilotInterviewReviewFocusConsumed={() => setPilotInterviewReviewApplicationId(null)}
      pilotInterviewPreparationApplicationId={pilotInterviewPreparationApplicationId}
      pilotInterviewPreparationEventId={pilotInterviewPreparationEventId}
      onPilotInterviewPreparationFocusConsumed={() => {
        setPilotInterviewPreparationApplicationId(null);
        setPilotInterviewPreparationEventId(null);
      }}
      onAttachToPilot={attachToPilot}
      resumes={resumesData}
      resumesLoading={resumesLoading}
      resumesError={resumesError}
      externalTaskBlocked={externalTaskBlockedForDetail}
      onOpportunityFitProjectionChange={updatePilotFitProjection}
      opportunityFitOwnerStore={opportunityFitOwnerStoreRef.current ?? undefined}
      offerNegotiationEntryPoint={offerNegotiationEntryPoint}
      offerNegotiationDrafts={offerNegotiationEntryPoint === 'pilot' ? offerNegotiationPilotDrafts : offerNegotiationDrafts}
      onOfferNegotiationDraftChange={offerNegotiationEntryPoint === 'pilot' ? handleOfferNegotiationDrawerDraftChange : updateOfferNegotiationDraft}
      applicationJdDraft={selectedApp ? applicationJdDrafts[selectedApp.id] : undefined}
      onApplicationJdDraftChange={updateApplicationJdDraft}
      interviewReviewProposalAttempts={interviewReviewProposalAttempts}
      onInterviewReviewProposalAttemptChange={updateInterviewReviewProposalAttempt}
      reviewReadinessDrafts={reviewReadinessDrafts}
      onReviewReadinessDraftChange={updateReviewReadinessDraft}
      onOpenReviewStory={(noteId) => openInterviewStoryDraft({ entrypoint: 'ui', applicationId: selectedApp.id, reviewNoteId: noteId })}
      onOpenReadinessPractice={openReadinessPractice}
      onInterviewNoteChanged={clearInterviewReviewProposalAttempt}
      interviewKnowledgeCaptureDrafts={interviewKnowledgeCaptureDrafts}
      onInterviewKnowledgeCaptureDraftChange={updateInterviewKnowledgeCaptureDraft}
      onInterviewKnowledgeCaptureNoteChanged={clearInterviewKnowledgeCaptureDraft}
      interviewPreparationAttempts={interviewPreparationAttempts}
      onInterviewPreparationAttemptChange={updateInterviewPreparationAttempt}
      interviewPreparationDrafts={interviewPreparationDrafts}
      onInterviewPreparationDraftChange={updateInterviewPreparationDraft}
      interviewPreparationKnowledgeOptions={interviewPreparationKnowledgeOptions}
      interviewPreparationSelection={interviewPreparationSelection}
      nextStepSuggestions={selectedNextStepSuggestions ?? undefined}
      nextStepSessionState={selectedNextStepSessionState}
      onSetDisposition={updateSuggestionSessionState}
      onNextStepNavigate={handleNextStepNavigate}
      isNavigationAvailable={isNextStepNavigationAvailable}
      onNextStepReadonlyNavigate={handleNextStepReadonlyNavigate}
      isReadonlyNavigationAvailable={isNextStepReadonlyNavigationAvailable}
    />
  ) : (
    <>
      {moduleTabs.length > 1 && (
        <Tabs
          className="op-module-tabs"
          activeKey={view}
          onChange={(key) => navigateToView(key as ViewMode)}
          items={moduleTabs.map((item) => ({ key: item.view, label: item.label }))}
        />
      )}
      <Suspense
        fallback={
          <div style={{ textAlign: 'center', padding: 48 }}>
            <Spin size="large" />
          </div>
        }
      >
        <div className="op-view-enter" style={view === 'pilot' ? { display: 'contents' } : undefined}>
          {view === 'dashboard' && (
            <DashboardView
              applications={apps}
              events={evs}
              offers={ofrs}
              practiceStats={practiceStats}
              dataState={{ eventsLoading, eventsError, offersLoading, offersError, practiceLoading, practiceError }}
              onNavigate={navigateToView}
              onOpenDetailById={goDetailById}
              onAddApplication={() => setAddOpen(true)}
              onOnboardingAction={handleOnboardingAction}
            />
          )}
          {view === 'board' && (
            <KanbanBoard
              applications={apps}
              onOpenDetail={openApplicationDetail}
              onAttachToPilot={attachToPilot}
              viewState={applicationViewState}
            />
          )}
          {view === 'applications-list' && (
            <ApplicationListView
              applications={apps}
               events={evs}
               onOpenDetail={openApplicationDetail}
               onAskPilot={startApplicationChat}
               onAttachToPilot={attachToPilot}
               viewState={applicationViewState}
               onViewStateChange={setApplicationViewState}
            />
          )}
          {view === 'calendar' && (
            <CalendarView
              applications={apps}
              onOpenDetail={openApplicationDetail}
              focusEvent={calendarEvidenceFocus}
              onEvidenceFocusConsumed={calendarEvidenceFocus ? () => clearEvidenceFocus(calendarEvidenceFocus) : undefined}
            />
          )}
          {view === 'reminders' && (
            <RemindersView onNavigate={navigateToView} onOpenDetailById={goDetailById} />
          )}
          {view === 'offers' && (
            <OfferCenterView
              applications={apps}
              onAddApplication={() => setAddOpen(true)}
              createRequestToken={offerCreateRequestToken}
              onOpenApplication={openCanonicalApplication}
              onCoach={(offer) => openChat(offer.id)}
              onAttachToPilot={attachToPilot}
              onOpenNegotiation={(offer) => openOfferNegotiation(offer, 'ui')}
              focusOfferId={offerEvidenceFocus?.id}
              onEvidenceFocusConsumed={offerEvidenceFocus ? () => clearEvidenceFocus(offerEvidenceFocus) : undefined}
            />
          )}
          {view === 'knowledge' && <KnowledgeSourcesView />}
          {view === 'reviews' && (
            <ExperienceMaterialsView
              key={interviewStoryLibraryRevision}
              onBack={() => setView('resumes')}
              onOpenDraft={openInterviewStoryDraft}
              confirmedCaptures={confirmedInterviewKnowledgeNotes}
              confirmedCapturesLoading={confirmedInterviewKnowledgeNotesLoading}
              confirmedCapturesError={confirmedInterviewKnowledgeNotesError}
            />
          )}
          {view === 'questions' && <QuestionBankView
            adaptiveFocus={adaptivePracticeFocus}
            adaptiveOwnerGeneration={adaptivePracticeFocus?.ownerGeneration}
            adaptivePracticeDrafts={adaptivePracticeDrafts}
            onAdaptivePracticeDraftChange={updateAdaptivePracticeDraft}
            onAdaptiveFocusConsumed={() => undefined}
            quickPracticeResumes={resumesLoading
              ? { status: 'loading' }
              : resumesError
                ? { status: 'error' }
                : { status: 'ready', value: resumes }}
            onOpenStudio={openQuickPracticeStudio}
          />}
          {view === 'interview' && (voiceCoachingGrowthOpen ? (
            <VoiceCoachingGrowthView
              onBack={() => setVoiceCoachingGrowthOpen(false)}
              onPractice={() => {
                setVoiceCoachingGrowthOpen(false);
                openFreePractice();
              }}
            />
          ) : interviewStoryLibraryOpen ? (
            <ExperienceMaterialsView
              key={interviewStoryLibraryRevision}
              onBack={() => setInterviewStoryLibraryOpen(false)}
              onOpenDraft={openInterviewStoryDraft}
              confirmedCaptures={confirmedInterviewKnowledgeNotes}
              confirmedCapturesLoading={confirmedInterviewKnowledgeNotesLoading}
              confirmedCapturesError={confirmedInterviewKnowledgeNotesError}
            />
          ) : activeInterviewPreparation ? (
            <InterviewReadinessCenter
              lockedEvent={activeInterviewPreparationApplication && activeInterviewPreparationEvent
                ? {
                    applicationId: activeInterviewPreparationApplication.id,
                    eventId: activeInterviewPreparationEvent.id,
                    companyName: activeInterviewPreparationApplication.company_name,
                    positionName: activeInterviewPreparationApplication.position_name,
                  }
                : null}
              fixedMode="real"
              generation={activeInterviewPreparation.generation}
              selectedResumeId={interviewPreparationSelection?.generation === activeInterviewPreparation.generation
                ? interviewPreparationSelection.resumeId
                : null}
              resumes={resumesLoading
                ? { status: 'loading' }
                : resumesError
                  ? { status: 'error' }
                  : { status: 'ready', value: resumes }}
              onOpenTask={openExactInterviewTask}
            />
          ) : coreTaskSurfaceState.active?.ref.taskId === 'interview.free_practice' ? (
            <QuestionBankView
              adaptiveFocus={adaptivePracticeFocus}
              adaptiveOwnerGeneration={coreTaskSurfaceState.active.generation}
              recoveryOwnerGeneration={coreTaskSurfaceState.active.recoveryGeneration}
              adaptivePracticeDrafts={adaptivePracticeDrafts}
              onAdaptivePracticeDraftChange={updateAdaptivePracticeDraft}
              onAdaptivePracticeGuardChange={(guard) => {
                const active = coreTaskController.getState().active;
                if (active?.ref.taskId === 'interview.free_practice'
                  && active.generation === coreTaskSurfaceState.active?.generation) {
                  settleRecoveredCoreTaskAfterGuardTransition(coreTaskController, taskSurfaceGuardRef.current, guard);
                  taskSurfaceGuardRef.current = guard;
                }
              }}
               onAdaptiveFocusConsumed={() => undefined}
               quickPracticeResumes={resumesLoading
                 ? { status: 'loading' }
                 : resumesError
                   ? { status: 'error' }
                   : { status: 'ready', value: resumes }}
               onOpenStudio={openQuickPracticeStudio}
             />
          ) : (
            <InterviewV01View
              onOpenApplication={goDetailById}
              onOpenTask={openExactInterviewTask}
              onOpenPreparation={(applicationId, eventId) => {
                openExactInterviewTask({
                  ref: { taskId: 'application.interview_prepare', applicationId, eventId },
                  source: 'interview_event_card',
                  focus: 'current',
                });
              }}
              onOpenEventEditor={openInterviewEventEditor}
              onOpenFreePractice={openFreePractice}
              events={evs}
              eventsLoading={eventsLoading}
              eventsError={eventsError}
              onRetryEvents={() => void qc.invalidateQueries({ queryKey: ['events'] })}
              onOpenStoryLibrary={(reviewNoteId) => {
                setVoiceCoachingGrowthOpen(false);
                setInterviewStoryLibraryOpen(true);
                if (reviewNoteId) openInterviewStoryDraft({ entrypoint: 'ui', reviewNoteId });
              }}
              onOpenVoiceCoachingGrowth={openVoiceCoachingGrowth}
              onOpenQuestionBank={() => navigateToView('questions')}
            />
          ))}
          {view === 'resumes' && (
            <ResumeLibraryView
              uploadRequestToken={resumeUploadRequestToken}
              onUploadRequestConsumed={() => setResumeUploadRequestToken(0)}
              onAttachToPilot={attachToPilot}
              focusResumeId={resumeEvidenceFocus?.id}
              onEvidenceFocusConsumed={resumeEvidenceFocus ? () => clearEvidenceFocus(resumeEvidenceFocus) : undefined}
              onboardingFocusToken={resumeOnboardingFocusToken}
            />
          )}
          {view === 'pilot' && (
            <div
              className="op-pilot-page-layout"
              style={{ display: 'contents' }}
            >
              {pilotApplicationContext ? (
                <PilotOpportunityFitV2Card
                  status={pilotFitProjections[pilotApplicationContext]?.status ?? 'idle'}
                  summary={pilotFitProjections[pilotApplicationContext]?.summary}
                  history={pilotFitProjections[pilotApplicationContext]?.history}
                  historyState={pilotFitProjections[pilotApplicationContext]?.historyState ?? 'loading'}
                  onOpenTask={() => {
                    const app = apps.find((item) => item.id === pilotApplicationContext);
                    if (app) startPilotOpportunityFit(app);
                  }}
                />
              ) : null}
            </div>
          )}
          {view === 'settings' && (
            <SettingsView
              pilotMascotVisible={pilotMascotVisible}
              onPilotMascotVisibleChange={setPilotMascotPreference}
              pilotMascotZoom={pilotMascotZoom}
              onPilotMascotZoomChange={setPilotMascotZoomPreference}
              pilotMascotAnimationLevel={pilotMascotAnimationLevel}
              onPilotMascotAnimationLevelChange={setPilotMascotAnimationPreference}
              onPilotMascotResetPosition={resetPilotMascotPositionPreference}
              systemReducedMotion={systemReducedMotion}
            />
          )}
        </div>
      </Suspense>
    </>
  );

  let topBarPrimaryAction: TopBarAction | undefined;
  if (!selectedApp) {
    if (['dashboard', 'reminders', 'board', 'applications-list'].includes(view)) {
      topBarPrimaryAction = {
        label: '添加投递',
        ariaLabel: '添加投递',
        onClick: () => setAddOpen(true),
      };
    } else if (view === 'interview') {
      topBarPrimaryAction = {
        label: '开始练习',
        ariaLabel: '开始面试练习',
        onClick: () => { openFreePractice(); },
      };
    } else if (view === 'offers') {
      topBarPrimaryAction = {
        label: apps.length === 0 ? '添加投递' : '录入 Offer',
        ariaLabel: apps.length === 0 ? '添加投递后录入 Offer' : '录入 Offer',
        onClick: apps.length === 0
          ? () => setAddOpen(true)
          : () => setOfferCreateRequestToken((token) => token + 1),
      };
    } else if (view === 'resumes') {
      topBarPrimaryAction = {
        label: '上传简历',
        ariaLabel: '上传简历',
        onClick: () => setResumeUploadRequestToken((token) => token + 1),
      };
    } else if (view === 'reviews') {
      topBarPrimaryAction = {
        label: '添加经历',
        ariaLabel: '添加经历素材',
        onClick: () => openInterviewStoryDraft({ entrypoint: 'ui' }, { preserveView: true }),
      };
    }
  }

  return (
    <DndContext sensors={kanbanSensors}>
      <Layout
      className="op-app-shell"
      style={{ minHeight: '100dvh', background: 'var(--op-layout-bg)' }}
      hasSider
    >
      <Sidebar
        view={view}
        onChange={navigateToView}
        reminderCount={actions.length}
      />
      <Layout
        className={`op-app-main${view === 'pilot' ? ' op-app-main-pilot' : ''}`}
        style={{
          background: 'var(--op-layout-bg)',
          minWidth: 0,
          width: '100%',
          paddingRight: contextualPilotRailMode ? 380 : undefined,
        }}
      >
        <TopBar
          primaryAction={topBarPrimaryAction}
          onSearch={() => setPaletteOpen(true)}
          onOpenSettings={() => navigateToView('settings')}
        />
        <Content
          ref={contentRef}
          tabIndex={-1}
          aria-label="主要内容"
          className={`op-app-content${view === 'pilot' ? ' op-app-content-pilot' : ''}`}
          style={{
            padding: '0 24px 24px',
            ...(view === 'pilot' && !isLoading && !appsError
              ? {
                  display: 'grid',
                  gridTemplateColumns: pilotApplicationContext
                    ? 'minmax(0, 1fr) minmax(320px, 0.7fr)'
                    : 'minmax(0, 1fr)',
                  gap: 16,
                }
              : {}),
          }}
        >
          {isLoading ? (
            <div style={{ textAlign: 'center', padding: 48 }}>
              <Spin size="large" />
            </div>
          ) : appsError ? (
            <div style={{ textAlign: 'center', padding: 48, color: 'var(--op-muted)' }}>
              加载失败，请稍后重试
            </div>
          ) : (
            <ViewErrorBoundary key={selectedApp ? `application-${selectedApp.id}` : view}>
              {workspaceContent}
            </ViewErrorBoundary>
          )}
          <div
            className={contextualPilotRailMode ? 'op-pilot-rail' : 'op-pilot-background-host'}
            data-pilot-surface-host
            aria-label={contextualPilotRailMode ? 'Pilot' : undefined}
            style={contextualPilotRailMode ? { position: 'fixed', inset: '0 0 0 auto', zIndex: 1040 } : undefined}
          >
            <PilotWorkspace
              pageActive={view === 'pilot'}
              variant={view === 'pilot' ? 'page' : contextualPilotRailMode ? 'rail' : 'drawer'}
              open={view === 'pilot' || contextualPilotOpen}
              controllerActive={pilotControllerTransportActive}
              onboardingFocusToken={pilotOnboardingFocusToken}
              onOnboardingFocusConsumed={consumePilotOnboardingFocus}
              pilotDropTarget={view !== 'pilot'}
              onClose={() => {
                if (view !== 'pilot') assistantSurface.closeSurface();
                if (
                  !pilotController.loading
                  && !pilotController.activeRequestRef.current
                  && !pilotController.pending
                  && !pilotController.activePendingRef.current
                ) {
                  setCoachOfferId(undefined);
                }
              }}
              offerId={coachOfferId}
              onOpenSettings={() => navigateToView('settings')}
              onExpand={() => {
                handoffPilotAttachmentDraft();
                nextPilotOnboardingFocusToken.current += 1;
                setPilotOnboardingFocusToken(nextPilotOnboardingFocusToken.current);
                navigateToView('pilot');
              }}
              startRequest={chatStartRequest}
              onStartRequestConsumed={claimChatStartRequest}
              onDataChanged={refreshWorkspaceData}
              pageContext={view === 'pilot' ? pilotController.followingContext : pageContext}
              attachmentDraftKey={pilotAttachmentDraftKey}
              onAttachmentKeyChange={syncPilotAttachmentKey}
              onOpenEvidence={openEvidence}
              onPrepareOfferNegotiation={(offer) => openOfferNegotiation(offer, 'pilot')}
              onOpenInterviewStoryLibrary={() => openInterviewStoryDraft({ entrypoint: 'pilot' })}
              onOpenVoiceCoachingGrowth={openVoiceCoachingGrowth}
              onActivityChange={setPilotMascotActivity}
              onReplyLifecycle={assistantSurface.reportReplyLifecycle}
              conversationRequest={assistantSurface.conversationRequest}
              onConversationRequestConsumed={assistantSurface.consumeConversationRequest}
              offers={pilotOfferScope.hasInvalidOwner ? [] : pilotOfferScope.offers ?? []}
            />
          </div>
        </Content>
      </Layout>

      {view !== 'pilot' && !interviewStudioContext && !coreTaskSurfaceState.active ? (
        <HaruDock
          visible={pilotMascotVisible}
          activity={pilotMascotActivity}
          onHide={() => setPilotMascotPreference(false)}
          zoom={pilotMascotZoom}
          onZoomChange={setPilotMascotZoomPreference}
          animationLevel={pilotMascotAnimationLevel}
          positionResetToken={pilotMascotPositionResetToken}
          onExpand={() => {
            nextPilotOnboardingFocusToken.current += 1;
            setPilotOnboardingFocusToken(nextPilotOnboardingFocusToken.current);
            navigateToView('pilot');
          }}
        />
      ) : null}

      {interviewStudioContext ? (
        <InterviewStudio
          context={interviewStudioContext}
          onClose={() => {
            setInterviewStudioContext(null);
            const active = coreTaskController.getState().active;
            if (active?.ref.taskId === 'interview.free_practice') closeCoreTaskSurface();
          }}
          onToggleHaru={() => setInterviewStudioHaruVisible((visible) => !visible)}
          onActivityChange={setPilotMascotActivity}
          onEvidenceVisibilityChange={setInterviewStudioEvidenceOpen}
        />
      ) : null}

      {interviewStudioContext && interviewStudioHaruVisible ? (
        <PilotMascot
          activity={pilotMascotActivity}
          panelOpen
          onTogglePilot={() => undefined}
          onHide={() => setInterviewStudioHaruVisible(false)}
          zoom={pilotMascotZoom}
          onZoomChange={setPilotMascotZoomPreference}
          animationLevel={pilotMascotAnimationLevel}
          positionResetToken={pilotMascotPositionResetToken}
          studioEvidenceOpen={interviewStudioEvidenceOpen}
          placement="interview-studio"
        />
      ) : null}

      <AddApplicationForm open={addOpen} onClose={() => setAddOpen(false)} />
      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        applications={apps}
        onNavigate={navigateToView}
        onOpenDetail={openApplicationDetail}
        onAddApplication={() => setAddOpen(true)}
        onOpenResume={() => navigateToView('resumes')}
        onUploadResume={() => {
          navigateToView('resumes');
          setResumeUploadRequestToken((token) => token + 1);
        }}
        onOpenChat={() => openChat(undefined)}
        onOpenPilot={() => navigateToView('pilot')}
        onOpenSettings={() => navigateToView('settings')}
        pipelineActions={pipelineActions}
        onRunPipelineAction={runPipelineAction}
      />
      {interviewStoryDrawerOpen && interviewStoryDraft ? (
        <InterviewStoryDrawer
          open
          draft={interviewStoryDraft}
          onDraftChange={updateInterviewStoryDraft}
          onClose={() => setInterviewStoryDrawerOpen(false)}
        />
      ) : null}
      </Layout>
    </DndContext>
  );
}
