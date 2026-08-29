import { Component, lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { DndContext, PointerSensor, useSensor, useSensors } from '@dnd-kit/core';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Button, Layout, Spin, Tabs, message } from 'antd';
import { listApplications } from '@/services/applications';
import { listEvents } from '@/services/events';
import { listOffers } from '@/services/offers';
import { ONBOARDING_QUERY_KEY } from '@/services/onboarding';
import { listResumes } from '@/services/resumes';
import {
  getOpportunityFitReview,
  listOpportunityFitReviews,
  createOpportunityFitV2Triage,
  confirmOpportunityFitV2Triage,
  createOpportunityFitV2DeepReview,
  getOpportunityFitV2Review,
  findOpportunityFitV2SourceConflictStage,
  listOpportunityFitV2Reviews,
} from '@/services/opportunityFitReviews';
import { type PilotOpportunityFitMaterialHandoff } from '@/features/pilot/PilotOpportunityFitCard';
import PilotOpportunityFitV2Card, { type PilotOpportunityFitV2Draft } from '@/features/pilot/PilotOpportunityFitV2Card';
import {
  isOpportunityFitNotFoundError,
} from '@/features/pilot/pilotOpportunityFitLifecycle';
import { discardMaterialKitHandoff, writeMaterialKitHandoff } from '@/features/pilot/materialKitHandoff';
import type { Application } from '@/types/application';
import type { Offer } from '@/types/offer';
import {
  createOpportunityFitV2Draft,
  type OpportunityFitReview,
  type OpportunityFitV2Draft,
} from '@/types/opportunityFitReview';
import { getOpportunityFitErrorMessage } from '@/components/opportunityFitCopy';
import type { ChatStartRequest, PilotActionRequest, PilotContextAttachment } from '@/types/chat';
import Sidebar from './Sidebar';
import TopBar, { type TopBarAction } from './TopBar';
import AddApplicationForm from '@/components/AddApplicationForm';
import ApplicationDetail from '@/components/ApplicationDetail';
import type { InterviewReviewProposalAttemptState } from '@/components/InterviewReviewProposalDrawer';
import type { InterviewKnowledgeCaptureDraft } from '@/components/InterviewKnowledgeCaptureDrawer';
import type { InterviewPreparationAttemptState, InterviewPreparationDraft, InterviewPreparationKnowledgeOption } from '@/components/InterviewPreparationProposalDrawer';
import MockInterviewDrawer, { type MockInterviewDrawerDraft } from '@/components/MockInterviewDrawer';
import InterviewStudio, { type InterviewStudioContext as RealInterviewStudioContext, type QuickPracticeStudioContext } from '@/features/interviewStudio/InterviewStudio';
import type { VoiceCoachingRecommendation } from '@/types/voiceCoaching';
import InterviewStoryLibraryView, { type InterviewStoryOpenDraft } from '@/components/InterviewStoryLibraryView';
import InterviewStoryDrawer, { createInterviewStoryDraft, type InterviewStoryDraft } from '@/components/InterviewStoryDrawer';
import OfferNegotiationDrawer, { type OfferNegotiationDraft } from '@/components/OfferNegotiationDrawer';
import { listOfferBindingState } from '@/components/offerWorkspaceModel';
import { discardMockInterviewAttempt } from '@/services/mockInterviews';
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
import { getCurrentApplicationJd } from '@/services/applicationJdVersions';
import type { ApplicationJdDraft } from '@/types/applicationJdVersion';
import type { AdaptivePracticeFocus } from '@/types/adaptiveInterviewPractice';
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

const { Content } = Layout;

const KanbanBoard = lazy(() => import('@/components/KanbanBoard'));
const ApplicationListView = lazy(() => import('@/components/ApplicationListView'));
const CalendarView = lazy(() => import('@/components/CalendarView'));
const KnowledgeSourcesView = lazy(() => import('@/components/KnowledgeSourcesView'));
const QuestionBankView = lazy(() => import('@/components/QuestionBankView'));
const OfferCenterView = lazy(() => import('@/components/OfferCenterView'));
const DashboardView = lazy(() => import('@/features/dashboard/DashboardView'));
const RemindersView = lazy(() => import('@/features/reminders/RemindersView'));
const InterviewV01View = lazy(() => import('@/components/InterviewV01View'));
const VoiceCoachingGrowthView = lazy(() => import('@/components/VoiceCoachingGrowthView'));
const ResumeLibraryView = lazy(() => import('@/components/ResumeLibraryView'));
const SettingsView = lazy(() => import('@/components/SettingsView'));

const createPilotOpportunityFitV2Draft = createOpportunityFitV2Draft;

function createMockInterviewDraft(): MockInterviewDrawerDraft {
  return {
    jdText: '',
    jdVersionId: undefined,
    attemptKey: null,
    questionKey: null,
    feedbackKey: null,
    turnKey: null,
    nextQuestionKey: null,
    confirmationKey: null,
    answerSubmitted: false,
    editedBlocks: {},
    attemptId: null,
    turnNo: 1,
    question: '',
    answer: '',
    proposalId: null,
    proposal: null,
    selectedIds: [],
    preparationItemIds: [],
    resultUnknown: false,
    voiceCoachingReview: null,
    voicePracticeFocus: null,
    hasSavedVoiceCoachingSnapshot: false,
    hasSubmittedVoiceAnswer: false,
    error: null,
  };
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
  const [view, setView] = useState<ViewMode>(readInitialWorkspaceView);
  const [applicationViewState, setApplicationViewState] = useState<ApplicationViewState>(
    DEFAULT_APPLICATION_VIEW_STATE,
  );
  const [adaptivePracticeFocus, setAdaptivePracticeFocus] = useState<AdaptivePracticeFocus | undefined>();
  const [addOpen, setAddOpen] = useState(false);
  const [resumeUploadRequestToken, setResumeUploadRequestToken] = useState(0);
  const [offerCreateRequestToken, setOfferCreateRequestToken] = useState(0);
  const [interviewPracticeRequestToken, setInterviewPracticeRequestToken] = useState(0);
  const [pilotMascotVisible, setPilotMascotVisible] = useState(readPilotMascotVisible);
  const [pilotMascotZoom, setPilotMascotZoom] = useState(readPilotMascotZoom);
  const [pilotMascotAnimationLevel, setPilotMascotAnimationLevel] = useState(readPilotMascotAnimationLevel);
  const [pilotMascotPositionResetToken, setPilotMascotPositionResetToken] = useState(0);
  const [systemReducedMotion, setSystemReducedMotion] = useState(() =>
    typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
  );
  const [pilotMascotActivity, setPilotMascotActivity] = useState<PilotMascotActivity>('idle');
  const [pilotApplicationContext, setPilotApplicationContext] = useState<{ applicationId: number; pilotDraftKey: string } | null>(null);
  const pilotV2DraftsRef = useRef(new Map<number, PilotOpportunityFitV2Draft>());
  const [pilotV2Draft, setPilotV2Draft] = useState<PilotOpportunityFitV2Draft | null>(null);
  const pilotV2GenerationRef = useRef(0);
  const pilotV2HistoryRequestRef = useRef(0);
  const [pilotV2OperationPending, setPilotV2OperationPending] = useState(false);
  const [pilotV2HistoryPending, setPilotV2HistoryPending] = useState(false);
  const [pilotLegacyReview, setPilotLegacyReview] = useState<OpportunityFitReview | null>(null);
  const [pilotInterviewReviewApplicationId, setPilotInterviewReviewApplicationId] = useState<number | null>(null);
  const [pilotInterviewPreparationApplicationId, setPilotInterviewPreparationApplicationId] = useState<number | null>(null);
  const [pilotInterviewPreparationEventId, setPilotInterviewPreparationEventId] = useState<number | null>(null);
  const [mockInterviewContext, setMockInterviewContext] = useState<{ applicationId: number; eventId: number } | null>(null);
  const mockInterviewDraftsRef = useRef(new Map<string, MockInterviewDrawerDraft>());
  const [mockInterviewDraft, setMockInterviewDraft] = useState<MockInterviewDrawerDraft | null>(null);
  const [interviewStudioContext, setInterviewStudioContext] = useState<(RealInterviewStudioContext | QuickPracticeStudioContext) | null>(null);
  const [interviewStudioHaruVisible, setInterviewStudioHaruVisible] = useState(false);
  const [interviewStudioEvidenceOpen, setInterviewStudioEvidenceOpen] = useState(true);
  const [offerNegotiationOffer, setOfferNegotiationOffer] = useState<Offer | null>(null);
  const [offerNegotiationEntryPoint, setOfferNegotiationEntryPoint] = useState<'ui' | 'pilot'>('ui');
  const offerNegotiationDraftsRef = useRef(new Map<number, OfferNegotiationDraft>());
  const [offerNegotiationDrafts, setOfferNegotiationDrafts] = useState<Record<number, OfferNegotiationDraft>>({});
  const offerNegotiationPilotDraftsRef = useRef(new Map<number, OfferNegotiationDraft>());
  const [offerNegotiationPilotDrafts, setOfferNegotiationPilotDrafts] = useState<Record<number, OfferNegotiationDraft>>({});
  const offerNegotiationOverlayRef = useRef<HTMLDivElement | null>(null);
  const offerNegotiationPreviousFocusRef = useRef<HTMLElement | null>(null);
  const pilotApplicationContextRef = useRef(pilotApplicationContext);
  pilotApplicationContextRef.current = pilotApplicationContext;
  const invalidatePilotV2History = () => {
    pilotV2HistoryRequestRef.current += 1;
    setPilotV2HistoryPending(false);
  };
  const [resumeOnboardingFocusToken, setResumeOnboardingFocusToken] = useState(0);
  const [pilotOnboardingFocusToken, setPilotOnboardingFocusToken] = useState(0);
  const nextPilotOnboardingFocusToken = useRef(0);
  const [selected, setSelected] = useState<Application | null>(null);
  const opportunityFitDraftsRef = useRef(new Map<number, OpportunityFitV2Draft>());
  const [opportunityFitDrafts, setOpportunityFitDrafts] = useState<Record<number, OpportunityFitV2Draft>>({});
  const applicationJdDraftsRef = useRef(new Map<number, ApplicationJdDraft>());
  const [applicationJdDrafts, setApplicationJdDrafts] = useState<Record<number, ApplicationJdDraft>>({});
  const [interviewReviewProposalAttempts, setInterviewReviewProposalAttempts] = useState<Record<number, InterviewReviewProposalAttemptState>>({});
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
  const exitPilotContext = ({ preserveUnknownAttempt = true }: { preserveUnknownAttempt?: boolean } = {}) => {
    pilotV2GenerationRef.current += 1;
    invalidatePilotV2History();
    setPilotV2OperationPending(false);
    setPilotV2HistoryPending(false);
    const current = pilotApplicationContextRef.current;
    if (!current) return;
    const draft = pilotV2DraftsRef.current.get(current.applicationId);
    if (draft) {
      const requestPending = Boolean(
        (draft.triageKey && (!draft.triage || ['generating', 'provider_unknown'].includes(draft.triage.stage_status)))
        || (draft.deepKey && (!draft.deep || ['generating', 'provider_unknown'].includes(draft.deep.stage_status))),
      );
      const retain = preserveUnknownAttempt && (draft.resultUnknown || requestPending);
      if (retain) {
        const retained = {
          ...draft,
          resultUnknown: true,
          error: '结果待确认，请使用原尝试重试。',
        };
        pilotV2DraftsRef.current.set(current.applicationId, retained);
        setPilotV2Draft(retained);
      } else {
        pilotV2DraftsRef.current.delete(current.applicationId);
        setPilotV2Draft(null);
      }
    }
    setPilotApplicationContext(null);
  };
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
  const { data: resumesData } = useQuery({
    queryKey: ['resumes'],
    queryFn: listResumes,
    enabled: true,
  });
  const { data: confirmedInterviewKnowledgeNotesData } = useQuery({
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

  const pilotV2HistoryQuery = useQuery({
    queryKey: ['opportunity-fit-v2-reviews', pilotApplicationContext?.applicationId],
    queryFn: () => listOpportunityFitV2Reviews(pilotApplicationContext!.applicationId),
    enabled: Boolean(pilotApplicationContext),
    retry: false,
  });
  const pilotLegacyHistoryQuery = useQuery({
    queryKey: ['opportunity-fit-v1-reviews', pilotApplicationContext?.applicationId],
    queryFn: () => listOpportunityFitReviews(pilotApplicationContext!.applicationId),
    enabled: Boolean(pilotApplicationContext),
    retry: false,
  });
  const pilotApplicationJdQuery = useQuery({
    queryKey: ['application-jd-current', pilotApplicationContext?.applicationId],
    queryFn: () => getCurrentApplicationJd(pilotApplicationContext!.applicationId),
    enabled: Boolean(pilotApplicationContext),
    retry: false,
  });
  useEffect(() => {
    if (!pilotApplicationContext || !pilotApplicationJdQuery.data?.current) return;
    const current = pilotV2DraftsRef.current.get(pilotApplicationContext.applicationId);
    if (!current || current.jdVersionId === pilotApplicationJdQuery.data.current.id) return;
    const hasFrozenAttempt = Boolean(
      current.triageKey
      || current.deepKey
      || current.triage
      || current.deep
      || current.resultUnknown,
    );
    if (hasFrozenAttempt) return;
    const next = {
      ...current,
      jdVersionId: pilotApplicationJdQuery.data.current.id,
      jdText: pilotApplicationJdQuery.data.current.jd_text,
    };
    pilotV2DraftsRef.current.set(next.applicationId, next);
    setPilotV2Draft(next);
  }, [pilotApplicationContext, pilotApplicationJdQuery.data?.current?.id, pilotApplicationJdQuery.data?.current?.jd_text]);
  const handlePilotNotFound = () => {
    const current = pilotApplicationContextRef.current;
    if (current) discardMaterialKitHandoff(current.applicationId);
    message.error('当前投递或岗位评估已不存在，请重新打开。');
    exitPilotContext({ preserveUnknownAttempt: false });
    setView('dashboard');
  };

  useEffect(() => {
    if (
      isOpportunityFitNotFoundError(pilotV2HistoryQuery.error)
      || isOpportunityFitNotFoundError(pilotLegacyHistoryQuery.error)
    ) {
      handlePilotNotFound();
    }
  }, [pilotV2HistoryQuery.error, pilotLegacyHistoryQuery.error]);

  // Backend serializes an empty []T slice as JSON `null` (Go encoding/json).
  // React Query's `= []` default only applies when data is `undefined`, so an
  // explicit null-coalesce is needed to keep downstream iterators safe.
  const apps = applications ?? [];
  const evs = eventsData ?? [];
  const ofrs = offersData ?? [];
  const resumes = resumesData ?? [];
  const [suggestionSessionStates, setSuggestionSessionStates] = useState<Record<string, SuggestionSessionState>>({});

  const buildNextStepFacts = (applicationId: number): NextStepFacts => {
    const application = apps.find((item) => item.id === applicationId);
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
        : { status: 'known', value: ofrs.filter((offer) => offer.application_id === applicationId) },
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

  const updateOpportunityFitDraft = useCallback((applicationId: number, patch: Partial<OpportunityFitV2Draft> | null) => {
    const current = opportunityFitDraftsRef.current.get(applicationId) ?? createOpportunityFitV2Draft(applicationId);
    if (patch === null) {
      opportunityFitDraftsRef.current.delete(applicationId);
      setOpportunityFitDrafts((state) => {
        const next = { ...state };
        delete next[applicationId];
        return next;
      });
      return;
    }
    const next = { ...current, ...patch };
    opportunityFitDraftsRef.current.set(applicationId, next);
    setOpportunityFitDrafts((state) => ({ ...state, [applicationId]: next }));
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
    setSelected(null);
    setEvidenceFocus(null);
    setView(nextView);
  }), []);

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
      setSelected(null);
    }
  }, [apps, selected]);

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
    exitPilotContext();
    setSelected(app);
  };

  const updateInterviewReviewProposalAttempt = (
    noteId: number,
    state: InterviewReviewProposalAttemptState | null,
  ) => {
    setInterviewReviewProposalAttempts((current) => {
      const next = { ...current };
      if (state?.result_unknown) next[noteId] = state;
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
    setSelected(null);
    const currentPilot = pilotApplicationContextRef.current;
    if (currentPilot && currentPilot.applicationId !== app.id) {
      exitPilotContext();
    }
    const v2Draft = pilotV2DraftsRef.current.get(app.id) ?? createPilotOpportunityFitV2Draft(app.id);
    pilotV2DraftsRef.current.set(app.id, v2Draft);
    setPilotV2Draft(v2Draft);
    setPilotLegacyReview(null);
    setPilotApplicationContext((current) => current?.applicationId === app.id
      ? current
      : {
        applicationId: app.id,
        pilotDraftKey: crypto.randomUUID(),
      });
    assistantSurface.openPilot();
    setView('pilot');
  };

  const openPilotInterviewReview = (applicationId: number) => {
    const app = apps.find((item) => item.id === applicationId);
    if (!app) return;
    exitPilotContext();
    setPilotInterviewReviewApplicationId(applicationId);
    setView('board');
    setSelected(app);
  };

  const openPilotInterviewPreparation = (applicationId: number, eventId?: number) => {
    const app = apps.find((item) => item.id === applicationId);
    if (!app) return;
    exitPilotContext();
    setPilotInterviewPreparationApplicationId(applicationId);
    setPilotInterviewPreparationEventId(eventId ?? null);
    setView('board');
    setSelected(app);
  };

  const openMockInterview = async (
    applicationId: number,
    eventId: number,
    voicePracticeFocus?: VoiceCoachingRecommendation,
  ) => {
    const draftKey = `${applicationId}:${eventId}`;
    const existingDraft = mockInterviewDraftsRef.current.get(draftKey);
    const preserveRecoveryDraft = Boolean(
      existingDraft && (
        existingDraft.resultUnknown
        || existingDraft.voiceCoachingReview?.saveState === 'unknown'
        || existingDraft.voiceCoachingReview?.saveState === 'conflict'
      )
    );
    let draft = preserveRecoveryDraft
      ? existingDraft!
      : voicePracticeFocus
        ? createMockInterviewDraft()
        : existingDraft ?? createMockInterviewDraft();
    const hasFrozenAttempt = Boolean(
      draft.attemptKey
      || draft.questionKey
      || draft.turnKey
      || draft.nextQuestionKey
      || draft.feedbackKey
      || draft.confirmationKey
      || draft.attemptId
      || draft.proposalId
      || draft.resultUnknown,
    );
    if (!hasFrozenAttempt) {
      try {
        const currentJd = await getCurrentApplicationJd(applicationId);
        if (currentJd.current) {
          draft = {
            ...draft,
            jdVersionId: currentJd.current.id,
            jdText: currentJd.current.jd_text,
          };
        } else {
          draft = { ...draft, jdVersionId: undefined, jdText: '' };
        }
      } catch {
        draft = { ...draft, jdVersionId: undefined, jdText: '', error: '岗位资料暂时无法加载，请稍后重试' };
      }
    }
    if (voicePracticeFocus && !hasFrozenAttempt) {
      draft = { ...draft, voicePracticeFocus };
    }
    mockInterviewDraftsRef.current.set(draftKey, draft);
    setMockInterviewDraft(draft);
    setMockInterviewContext({ applicationId, eventId });
  };

  const openOfferNegotiation = (offer: Offer, entrypoint: 'ui' | 'pilot' = 'ui') => {
    if (listOfferBindingState(offer) === 'unbound') {
      message.warning('历史未绑定 Offer 仅支持只读查看');
      return;
    }
    setOfferNegotiationOffer(offer);
    setOfferNegotiationEntryPoint(entrypoint);
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

  const handleOfferNegotiationDraftChange = useCallback((offerId: number, draft: OfferNegotiationDraft | null) => {
    updateOfferNegotiationDraft(offerId, draft);
  }, [updateOfferNegotiationDraft]);

  const handleOfferNegotiationDrawerDraftChange = useCallback((draft: OfferNegotiationDraft | null) => {
    if (offerNegotiationOffer) {
      if (draft) {
        offerNegotiationPilotDraftsRef.current.set(offerNegotiationOffer.id, draft);
        setOfferNegotiationPilotDrafts((current) => ({ ...current, [offerNegotiationOffer.id]: draft }));
      } else {
        offerNegotiationPilotDraftsRef.current.delete(offerNegotiationOffer.id);
        setOfferNegotiationPilotDrafts((current) => {
          const next = { ...current };
          delete next[offerNegotiationOffer.id];
          return next;
        });
      }
    }
  }, [offerNegotiationOffer]);

  useEffect(() => {
    if (!offerNegotiationOffer) return;

    offerNegotiationPreviousFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const overlay = offerNegotiationOverlayRef.current;
    const selector = 'button, [href], input, textarea, select, [tabindex]:not([tabindex="-1"])';
    const getFocusable = () => Array.from(overlay?.querySelectorAll<HTMLElement>(selector) ?? [])
      .filter((element) => !element.hasAttribute('disabled') && element.getAttribute('aria-hidden') !== 'true');

    const firstFocusable = getFocusable()[0];
    (firstFocusable ?? overlay)?.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        setOfferNegotiationOffer(null);
        return;
      }
      if (event.key !== 'Tab') return;

      const focusable = getFocusable();
      if (focusable.length === 0) {
        event.preventDefault();
        overlay?.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      offerNegotiationPreviousFocusRef.current?.focus();
      offerNegotiationPreviousFocusRef.current = null;
    };
  }, [offerNegotiationOffer]);

  const updateMockInterviewDraft = (patch: Partial<MockInterviewDrawerDraft>) => {
    if (!mockInterviewContext || !mockInterviewDraft) return;
    const draftKey = `${mockInterviewContext.applicationId}:${mockInterviewContext.eventId}`;
    const currentDraft = mockInterviewDraftsRef.current.get(draftKey) ?? mockInterviewDraft;
    const next = { ...currentDraft, ...patch };
    mockInterviewDraftsRef.current.set(draftKey, next);
    setMockInterviewDraft(next);
  };

  const closeMockInterview = async () => {
    const context = mockInterviewContext;
    const draft = mockInterviewDraft;
    if (!context || !draft) {
      setMockInterviewContext(null);
      setMockInterviewDraft(null);
      return;
    }
    if (!draft.attemptId && draft.attemptKey) {
      const retained = {
        ...draft,
        resultUnknown: true,
        error: '操作结果待确认，请稍后使用原尝试重试。',
      };
      mockInterviewDraftsRef.current.set(`${context.applicationId}:${context.eventId}`, retained);
      setMockInterviewContext(null);
      setMockInterviewDraft(null);
      return;
    }
    if (!draft.attemptId || draft.resultUnknown) {
      setMockInterviewContext(null);
      setMockInterviewDraft(null);
      return;
    }
    const preserveSubmittedVoiceAnswer = Boolean(
      draft.hasSubmittedVoiceAnswer
      || draft.voiceCoachingReview?.saveState === 'unknown'
      || draft.voiceCoachingReview?.saveState === 'conflict',
    );
    if (draft.hasSavedVoiceCoachingSnapshot || preserveSubmittedVoiceAnswer) {
      mockInterviewDraftsRef.current.set(`${context.applicationId}:${context.eventId}`, draft);
      setMockInterviewContext(null);
      setMockInterviewDraft(null);
      return;
    }
    try {
      await discardMockInterviewAttempt({
        applicationId: context.applicationId,
        eventId: context.eventId,
        attemptId: draft.attemptId,
      });
      mockInterviewDraftsRef.current.delete(`${context.applicationId}:${context.eventId}`);
      setMockInterviewContext(null);
      setMockInterviewDraft(null);
    } catch (error) {
      const response = (error as { response?: { status?: number; data?: { error_code?: string } } })?.response;
      if (response?.status === 404 || response?.data?.error_code === 'mock_interview_attempt_confirmed') {
        mockInterviewDraftsRef.current.delete(`${context.applicationId}:${context.eventId}`);
        setMockInterviewContext(null);
        setMockInterviewDraft(null);
        message.info(response?.data?.error_code === 'mock_interview_attempt_confirmed' ? '本次模拟面试已保存，可在历史记录中查看。' : '本次模拟面试已关闭。');
        return;
      }
      updateMockInterviewDraft({ error: '操作结果待确认，请稍后使用原尝试重试。' });
    }
  };

  const updatePilotV2Draft = (patch: Partial<PilotOpportunityFitV2Draft>) => {
    if (!pilotV2Draft) return;
    const next = { ...pilotV2Draft, ...patch };
    pilotV2DraftsRef.current.set(next.applicationId, next);
    setPilotV2Draft(next);
  };

  const v2ErrorMessage = (error: unknown): string => getOpportunityFitErrorMessage(error);

  const v2ErrorCode = (error: unknown): string | undefined => {
    if (typeof error !== 'object' || error === null) return undefined;
    const response = (error as { response?: { data?: { error_code?: unknown } } }).response;
    return typeof response?.data?.error_code === 'string' ? response.data.error_code : undefined;
  };

  const v2SourceConflictCopy = '岗位资料版本已变化，当前评估仅供只读查看。';

  const recoverPilotV2SourceConflict = async (
    stage: 'triage' | 'deep_review',
    idempotencyKey: string,
    reviewID?: number,
  ): Promise<Awaited<ReturnType<typeof findOpportunityFitV2SourceConflictStage>>> => {
    if (!pilotV2Draft) return { status: 'not_found' };
    try {
      return await findOpportunityFitV2SourceConflictStage(
        pilotV2Draft.applicationId,
        stage,
        idempotencyKey,
        reviewID,
      );
    } catch {
      return { status: 'unknown' };
    }
  };

  const pilotV2RecoveryUnknownCopy = '操作结果待确认，请使用原尝试重试。';

  const v2FailureDisposition = (error: unknown): 'unknown' | 'definite' => {
    if (typeof error !== 'object' || error === null) return 'unknown';
    const response = (error as { response?: unknown }).response;
    if (typeof response !== 'object' || response === null) return 'unknown';
    const record = response as { status?: unknown; data?: unknown };
    const data = typeof record.data === 'object' && record.data !== null
      ? record.data as { error_code?: unknown }
      : undefined;
    if (data?.error_code === 'opportunity_fit_unverifiable') return 'definite';
    if (data?.error_code === 'opportunity_fit_provider_error') return 'unknown';
    return typeof record.status === 'number' && record.status >= 500 ? 'unknown' : 'definite';
  };

  const startPilotV2Triage = async (input: Parameters<typeof createOpportunityFitV2Triage>[1]) => {
    if (!pilotV2Draft) return;
    invalidatePilotV2History();
    const requestDraft = pilotV2Draft;
    const generation = pilotV2GenerationRef.current;
    const key = requestDraft.triageKey ?? input.idempotency_key;
    setPilotV2OperationPending(true);
    updatePilotV2Draft({ triageKey: key, error: null });
    try {
      const result = await createOpportunityFitV2Triage(requestDraft.applicationId, { ...input, idempotency_key: key });
      if (generation !== pilotV2GenerationRef.current) return;
      updatePilotV2Draft({ triage: result, triageKey: key, resultUnknown: false, error: null });
    } catch (error) {
      if (generation !== pilotV2GenerationRef.current) return;
      const errorCode = v2ErrorCode(error);
      if (errorCode === 'application_jd_source_conflict' || errorCode === 'opportunity_fit_source_conflict') {
        const conflict = await recoverPilotV2SourceConflict('triage', key);
        if (generation !== pilotV2GenerationRef.current) return;
        if (conflict.status === 'found') {
          updatePilotV2Draft({
            triage: conflict.stage,
            triageKey: null,
            resultUnknown: false,
            error: v2SourceConflictCopy,
          });
          return;
        }
        if (conflict.status === 'unknown') {
          updatePilotV2Draft({ triageKey: key, resultUnknown: true, error: pilotV2RecoveryUnknownCopy });
          return;
        }
        if (conflict.status === 'application_missing') {
          handlePilotNotFound();
          return;
        }
        if (conflict.status === 'review_missing') {
          startNewPilotV2Review();
          return;
        }
      }
      if (
        typeof error === 'object'
        && error !== null
        && typeof (error as { response?: { data?: { error_code?: unknown } } }).response?.data?.error_code === 'string'
        && (error as { response: { data: { error_code: string } } }).response.data.error_code
          === 'opportunity_fit_triage_confirmation_expired'
      ) {
        startNewPilotV2Review();
        return;
      }
      const disposition = v2FailureDisposition(error);
      updatePilotV2Draft({
        triageKey: disposition === 'unknown' ? key : null,
        resultUnknown: disposition === 'unknown',
        error: v2ErrorMessage(error),
      });
      if (isOpportunityFitNotFoundError(error)) handlePilotNotFound();
    } finally {
      if (generation === pilotV2GenerationRef.current) setPilotV2OperationPending(false);
    }
  };

  const recoverPilotV2TriageConfirmation = async (
    requestDraft: PilotOpportunityFitV2Draft,
  ): Promise<NonNullable<PilotOpportunityFitV2Draft['triage']> | null> => {
    if (!requestDraft.triage) return null;
    try {
      const session = await getOpportunityFitV2Review(
        requestDraft.applicationId,
        requestDraft.triage.review_id,
      );
      const current = session.stages.find((stage) => (
        stage.stage === 'triage' && stage.stage_id === requestDraft.triage?.stage_id
      )) ?? session.stages.find((stage) => stage.stage === 'triage');
      return current?.stage_status === 'confirmed' ? current : null;
    } catch {
      return null;
    }
  };

  const confirmPilotV2Triage = async () => {
    if (!pilotV2Draft?.triage?.confirmation_token) return;
    invalidatePilotV2History();
    const requestDraft = pilotV2Draft;
    const requestTriage = requestDraft.triage;
    if (!requestTriage?.confirmation_token) return;
    const generation = pilotV2GenerationRef.current;
    setPilotV2OperationPending(true);
    try {
      const result = await confirmOpportunityFitV2Triage(
        requestDraft.applicationId,
        requestTriage.review_id,
        requestTriage.stage_id,
        requestTriage.confirmation_token,
      );
      if (generation !== pilotV2GenerationRef.current) return;
      updatePilotV2Draft({ triage: result, resultUnknown: false, error: null });
    } catch (error) {
      if (generation !== pilotV2GenerationRef.current) return;
      const errorCode = v2ErrorCode(error);
      if (errorCode === 'opportunity_fit_triage_confirmation_expired') {
        startNewPilotV2Review();
        return;
      }
      if (
        errorCode === 'opportunity_fit_triage_confirmation_consumed'
        || v2FailureDisposition(error) === 'unknown'
      ) {
        const current = await recoverPilotV2TriageConfirmation(requestDraft);
        if (generation !== pilotV2GenerationRef.current) return;
        if (current) {
          updatePilotV2Draft({ triage: current, resultUnknown: false, error: null });
          return;
        }
        updatePilotV2Draft({ resultUnknown: true, error: pilotV2RecoveryUnknownCopy });
        return;
      }
      if (pilotV2Draft.resultUnknown && v2FailureDisposition(error) === 'definite') {
        startNewPilotV2Review();
        return;
      }
      updatePilotV2Draft({ error: v2ErrorMessage(error) });
    } finally {
      if (generation === pilotV2GenerationRef.current) setPilotV2OperationPending(false);
    }
  };

  const startPilotV2DeepReview = async () => {
    if (!pilotV2Draft?.triage || pilotV2Draft.triage.stage_status !== 'confirmed') return;
    invalidatePilotV2History();
    const requestDraft = pilotV2Draft;
    const requestTriage = requestDraft.triage;
    if (!requestTriage) return;
    const generation = pilotV2GenerationRef.current;
    const key = requestDraft.deepKey ?? crypto.randomUUID();
    setPilotV2OperationPending(true);
    updatePilotV2Draft({ deepKey: key, error: null });
    try {
      const result = await createOpportunityFitV2DeepReview(
        requestDraft.applicationId,
        requestTriage.review_id,
        {
          schema_version: 2,
          resume_id: requestDraft.resumeId ?? 0,
          jd_source_label: '用户粘贴 JD',
          candidate_assertions: requestDraft.assertionsText.split(/\r?\n/).map((item) => item.trim()).filter(Boolean),
          idempotency_key: key,
          parent_triage_stage_id: requestTriage.stage_id,
        },
      );
      if (generation !== pilotV2GenerationRef.current) return;
      updatePilotV2Draft({ deep: result, deepKey: key, resultUnknown: false, error: null });
    } catch (error) {
      if (generation !== pilotV2GenerationRef.current) return;
      const errorCode = v2ErrorCode(error);
      if (errorCode === 'application_jd_source_conflict' || errorCode === 'opportunity_fit_source_conflict') {
        const conflict = await recoverPilotV2SourceConflict(
          'deep_review',
          key,
          requestTriage.review_id,
        );
        if (generation !== pilotV2GenerationRef.current) return;
        if (conflict.status === 'found') {
          updatePilotV2Draft({
            deep: conflict.stage,
            deepKey: null,
            resultUnknown: false,
            error: v2SourceConflictCopy,
          });
          return;
        }
        if (conflict.status === 'unknown') {
          updatePilotV2Draft({ deepKey: key, resultUnknown: true, error: pilotV2RecoveryUnknownCopy });
          return;
        }
        if (conflict.status === 'application_missing') {
          handlePilotNotFound();
          return;
        }
        if (conflict.status === 'review_missing') {
          startNewPilotV2Review();
          return;
        }
      }
      const disposition = v2FailureDisposition(error);
      updatePilotV2Draft({
        deepKey: disposition === 'unknown' ? key : null,
        resultUnknown: disposition === 'unknown',
        error: v2ErrorMessage(error),
      });
      if (isOpportunityFitNotFoundError(error)) handlePilotNotFound();
    } finally {
      if (generation === pilotV2GenerationRef.current) setPilotV2OperationPending(false);
    }
  };

  const viewPilotV2History = async (reviewId: number) => {
    if (!pilotV2Draft) return;
    const requestDraft = pilotV2Draft;
    const generation = pilotV2GenerationRef.current;
    const requestGeneration = ++pilotV2HistoryRequestRef.current;
    setPilotV2HistoryPending(true);
    try {
      const result = await getOpportunityFitV2Review(requestDraft.applicationId, reviewId);
      if (generation !== pilotV2GenerationRef.current || requestGeneration !== pilotV2HistoryRequestRef.current) return;
      const triage = result.stages.find((stage) => stage.stage === 'triage') ?? null;
      const deep = [...result.stages].reverse().find((stage) => stage.stage === 'deep_review') ?? null;
      setPilotLegacyReview(null);
      updatePilotV2Draft({ triage, deep, historical: true, error: null });
    } catch (error) {
      if (generation !== pilotV2GenerationRef.current || requestGeneration !== pilotV2HistoryRequestRef.current) return;
      updatePilotV2Draft({ error: v2ErrorMessage(error) });
    } finally {
      if (generation === pilotV2GenerationRef.current && requestGeneration === pilotV2HistoryRequestRef.current) {
        setPilotV2HistoryPending(false);
      }
    }
  };

  const viewPilotLegacyHistory = async (reviewId: number) => {
    if (!pilotV2Draft) return;
    const requestDraft = pilotV2Draft;
    const generation = pilotV2GenerationRef.current;
    const requestGeneration = ++pilotV2HistoryRequestRef.current;
    setPilotV2HistoryPending(true);
    try {
      const result = await getOpportunityFitReview(requestDraft.applicationId, reviewId);
      if (generation !== pilotV2GenerationRef.current || requestGeneration !== pilotV2HistoryRequestRef.current) return;
      updatePilotV2Draft({ triage: null, deep: null, historical: true, error: null });
      setPilotLegacyReview(result);
    } catch (error) {
      if (generation !== pilotV2GenerationRef.current || requestGeneration !== pilotV2HistoryRequestRef.current) return;
      if (isOpportunityFitNotFoundError(error)) handlePilotNotFound();
      else message.error(getOpportunityFitErrorMessage(error));
    } finally {
      if (generation === pilotV2GenerationRef.current && requestGeneration === pilotV2HistoryRequestRef.current) {
        setPilotV2HistoryPending(false);
      }
    }
  };

  const startNewPilotV2Review = () => {
    if (!pilotV2Draft) return;
    pilotV2GenerationRef.current += 1;
    invalidatePilotV2History();
    setPilotV2OperationPending(false);
    setPilotV2HistoryPending(false);
    const next = createPilotOpportunityFitV2Draft(pilotV2Draft.applicationId);
    const currentJd = pilotApplicationJdQuery.data?.current;
    if (currentJd) {
      next.jdVersionId = currentJd.id;
      next.jdText = currentJd.jd_text;
    }
    pilotV2DraftsRef.current.set(next.applicationId, next);
    setPilotV2Draft(next);
    setPilotLegacyReview(null);
  };

  const preparePilotMaterials = (handoff: PilotOpportunityFitMaterialHandoff) => {
    if (!handoff.jdVersionId) return;
    writeMaterialKitHandoff({
      applicationId: handoff.applicationId,
      resumeId: handoff.resumeId,
      jdText: handoff.jdText,
      jdVersionId: handoff.jdVersionId,
    });
    const app = apps.find((item) => item.id === handoff.applicationId);
    exitPilotContext();
    setView('board');
    if (app) setSelected(app);
  };

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
    navigateToView('board');
    setSelected(app);
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
      case 'material_kit_entry':
        goDetailById(destination.applicationId);
        return;
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
      case 'interview_review_history':
      case 'interview_review_selection':
        return;
      case 'opportunity_fit_history':
        {
          const app = apps.find((item) => item.id === destination.applicationId);
          if (app) startPilotOpportunityFit(app);
        }
        return;
    }
  };

  const handleNextStepReadonlyNavigate = (destination: ReadonlyDestination) => {
    if (destination.kind === 'application_detail') goDetailById(destination.applicationId);
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

  const updateInterviewStoryDraft = (draft: InterviewStoryDraft | null) => {
    const scope = activeInterviewStoryDraftScope;
    if (!scope) return;
    if (draft === null) setInterviewStoryLibraryRevision((current) => current + 1);
    setInterviewStoryDrafts((current) => {
      const next = { ...current };
      if (draft === null) {
        interviewStoryDraftsRef.current.delete(scope);
        delete next[scope];
      } else {
        interviewStoryDraftsRef.current.set(scope, draft);
        next[scope] = draft;
      }
      return next;
    });
  };

  const workspaceContent = selectedApp ? (
    <ApplicationDetail
      application={selectedApp}
      offers={ofrs}
      offersError={offersError}
      onRetryOffers={() => void qc.invalidateQueries({ queryKey: ['offers'] })}
      open
      onClose={() => setSelected(null)}
      onOpenOffers={() => {
        setSelected(null);
        navigateToView('offers');
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
      applicationJdDraft={selectedApp ? applicationJdDrafts[selectedApp.id] : undefined}
      onApplicationJdDraftChange={updateApplicationJdDraft}
      opportunityFitDraft={selectedApp ? opportunityFitDrafts[selectedApp.id] : undefined}
      onOpportunityFitDraftChange={updateOpportunityFitDraft}
      interviewReviewProposalAttempts={interviewReviewProposalAttempts}
      onInterviewReviewProposalAttemptChange={updateInterviewReviewProposalAttempt}
      onInterviewNoteChanged={clearInterviewReviewProposalAttempt}
      interviewKnowledgeCaptureDrafts={interviewKnowledgeCaptureDrafts}
      onInterviewKnowledgeCaptureDraftChange={updateInterviewKnowledgeCaptureDraft}
      onInterviewKnowledgeCaptureNoteChanged={clearInterviewKnowledgeCaptureDraft}
      resumes={resumes}
      interviewPreparationAttempts={interviewPreparationAttempts}
      onInterviewPreparationAttemptChange={updateInterviewPreparationAttempt}
      interviewPreparationDrafts={interviewPreparationDrafts}
      onInterviewPreparationDraftChange={updateInterviewPreparationDraft}
      interviewPreparationKnowledgeOptions={interviewPreparationKnowledgeOptions}
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
              negotiationDrafts={offerNegotiationDrafts}
              onNegotiationDraftChange={handleOfferNegotiationDraftChange}
              focusOfferId={offerEvidenceFocus?.id}
              onEvidenceFocusConsumed={offerEvidenceFocus ? () => clearEvidenceFocus(offerEvidenceFocus) : undefined}
            />
          )}
          {view === 'knowledge' && <KnowledgeSourcesView />}
          {view === 'reviews' && (
            <InterviewStoryLibraryView
              key={interviewStoryLibraryRevision}
              onBack={() => setView('resumes')}
              onOpenDraft={openInterviewStoryDraft}
            />
          )}
          {view === 'questions' && <QuestionBankView adaptiveFocus={adaptivePracticeFocus} onAdaptiveFocusConsumed={() => setAdaptivePracticeFocus(undefined)} />}
          {view === 'interview' && (voiceCoachingGrowthOpen ? (
            <VoiceCoachingGrowthView
              onBack={() => setVoiceCoachingGrowthOpen(false)}
              onPractice={(recommendation) => {
                setVoiceCoachingGrowthOpen(false);
                void openMockInterview(
                  recommendation.application_id,
                  recommendation.event_id,
                  recommendation,
                );
              }}
            />
          ) : interviewStoryLibraryOpen ? (
            <InterviewStoryLibraryView
              key={interviewStoryLibraryRevision}
              onBack={() => setInterviewStoryLibraryOpen(false)}
              onOpenDraft={openInterviewStoryDraft}
            />
          ) : (
            <InterviewV01View
              practiceRequestToken={interviewPracticeRequestToken}
              onOpenApplication={goDetailById}
              onOpenPreparation={openPilotInterviewPreparation}
              onOpenMockInterview={openMockInterview}
              applications={apps}
              events={evs}
              eventsLoading={eventsLoading}
              eventsError={eventsError}
              onRetryEvents={() => void qc.invalidateQueries({ queryKey: ['events'] })}
              resumes={resumes}
              onOpenStudio={(context) => {
                setInterviewStudioHaruVisible(false);
                setInterviewStudioContext(context);
              }}
              onOpenStoryLibrary={(reviewNoteId) => {
                setVoiceCoachingGrowthOpen(false);
                setInterviewStoryLibraryOpen(true);
                if (reviewNoteId) openInterviewStoryDraft({ entrypoint: 'ui', reviewNoteId });
              }}
              onOpenVoiceCoachingGrowth={openVoiceCoachingGrowth}
              onOpenQuestionBank={() => navigateToView('questions')}
              onOpenAdaptivePractice={(focus) => {
                setAdaptivePracticeFocus(focus);
                navigateToView('questions');
              }}
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
                  draft={pilotV2Draft ?? createPilotOpportunityFitV2Draft(pilotApplicationContext.applicationId)}
                  resumes={resumes}
                  history={pilotV2HistoryQuery.data ?? []}
                  legacyHistory={pilotLegacyHistoryQuery.data ?? []}
                  legacyReview={pilotLegacyReview}
                  historyLoading={pilotV2HistoryPending || pilotV2HistoryQuery.isLoading || pilotV2HistoryQuery.isFetching}
                  legacyHistoryLoading={pilotLegacyHistoryQuery.isLoading || pilotLegacyHistoryQuery.isFetching}
                  triageLoading={Boolean(pilotV2Draft?.triageKey && !pilotV2Draft?.triage && !pilotV2Draft?.error)}
                  deepLoading={Boolean(pilotV2Draft?.deepKey && !pilotV2Draft?.deep && !pilotV2Draft?.error)}
                  onChange={updatePilotV2Draft}
                  onStartTriage={startPilotV2Triage}
                  onConfirmTriage={() => void confirmPilotV2Triage()}
                  onStartDeepReview={() => void startPilotV2DeepReview()}
                  onViewHistory={(reviewId) => void viewPilotV2History(reviewId)}
                  onViewLegacyHistory={(reviewId) => void viewPilotLegacyHistory(reviewId)}
                  onStartNew={startNewPilotV2Review}
                  restartDisabled={pilotV2OperationPending}
                  historyDisabled={pilotV2OperationPending}
                  onPrepareMaterials={(resumeId, jdText, jdVersionId) => preparePilotMaterials({
                    applicationId: pilotApplicationContext.applicationId,
                    resumeId,
                    jdText,
                    jdVersionId,
                  })}
                  onOpenInterviewReview={openPilotInterviewReview}
                  onOpenInterviewPreparation={openPilotInterviewPreparation}
                  interviewEvents={evs.filter((event) => event.application_id === pilotApplicationContext.applicationId && event.event_type === 'interview' && Boolean(event.scheduled_at))}
                  onOpenMockInterview={openMockInterview}
                  onCancel={() => {
                    exitPilotContext();
                    setView('dashboard');
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
        onClick: () => setInterviewPracticeRequestToken((token) => token + 1),
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
              offers={ofrs}
            />
          </div>
        </Content>
      </Layout>

      {view !== 'pilot' && !interviewStudioContext ? (
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
          onClose={() => setInterviewStudioContext(null)}
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
      {mockInterviewContext && mockInterviewDraft ? (
        <MockInterviewDrawer
          open
          applicationId={mockInterviewContext.applicationId}
          eventId={mockInterviewContext.eventId}
          resumes={resumes}
          draft={mockInterviewDraft}
          onDraftChange={updateMockInterviewDraft}
          onVoiceActivityChange={setPilotMascotActivity}
          onClose={() => void closeMockInterview()}
        />
      ) : null}
      {interviewStoryDrawerOpen && interviewStoryDraft ? (
        <InterviewStoryDrawer
          open
          draft={interviewStoryDraft}
          onDraftChange={updateInterviewStoryDraft}
          onClose={() => setInterviewStoryDrawerOpen(false)}
        />
      ) : null}
      {offerNegotiationOffer ? (
        <div
          className="op-offer-negotiation-overlay"
          data-testid="offer-negotiation-overlay"
          role="dialog"
          aria-modal="true"
          aria-label={`为 ${offerNegotiationOffer.company_name} 准备谈薪`}
          ref={offerNegotiationOverlayRef}
          tabIndex={-1}
          style={{ position: 'fixed', inset: 0, zIndex: 1100 }}
        >
          <div className="op-offer-negotiation-surface">
            <OfferNegotiationDrawer
              key={`${offerNegotiationEntryPoint}-${offerNegotiationOffer.id}`}
              open
              offer={offerNegotiationOffer}
              entrypoint={offerNegotiationEntryPoint}
              draft={offerNegotiationEntryPoint === 'pilot'
                ? offerNegotiationPilotDrafts[offerNegotiationOffer.id]
                : offerNegotiationDrafts[offerNegotiationOffer.id]}
              onDraftChange={handleOfferNegotiationDrawerDraftChange}
              onClose={() => setOfferNegotiationOffer(null)}
            />
          </div>
        </div>
      ) : null}
      </Layout>
    </DndContext>
  );
}
