import { useEffect, useMemo, useRef, useState, useSyncExternalStore, type KeyboardEvent, type ReactNode } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Typography,
  Tag,
  Timeline,
  Button,
  Divider,
  Input,
  message,
  Empty,
  Spin,
  Popconfirm,
  Space,
  Modal,
  Dropdown,
  Alert,
} from 'antd';
import {
  ArrowLeftOutlined,
  CalendarOutlined,
  PlusOutlined,
  MoreOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import type { Application } from '@/types/application';
import type { Offer } from '@/types/offer';
import type { PilotActionRequest } from '@/types/chat';
import { listNotesByApp, createNote, deleteNote as removeNote, updateNote } from '@/services/notes';
import { listEvents } from '@/services/events';
import type { CreateNoteInput, InterviewNote } from '@/types/note';
import type { ScheduleEvent } from '@/types/event';
import { EVENT_TYPE_LABELS } from '@/types/event';
import { OFFER_STATUS_LABELS } from '@/types/offer';
import ScheduleEventForm from '@/components/ScheduleEventForm';
import ReviewFormDrawer from './ReviewFormDrawer';
import InterviewReviewProposalDrawer, {
  type InterviewReviewProposalAttemptState,
} from './InterviewReviewProposalDrawer';
import InterviewKnowledgeCaptureDrawer, {
  createInterviewKnowledgeCaptureDraft,
  type InterviewKnowledgeCaptureDraft,
} from './InterviewKnowledgeCaptureDrawer';
import InterviewPreparationProposalDrawer, {
  type InterviewPreparationDraft,
  type InterviewPreparationAttemptState,
  type InterviewPreparationKnowledgeOption,
} from './InterviewPreparationProposalDrawer';
import type { Resume } from '@/types/resume';
import MaterialKitDrawer from './MaterialKitDrawer';
import { materialKitOwnerStore } from '@/features/materialSurfaces/materialKitOwnerStore';
import OpportunityFitReviewDrawer, {
  type OpportunityFitOwnerProjection,
  type OpportunityFitOwnerStore,
} from './OpportunityFitReviewDrawer';
import ApplicationOutcomeDrawer from './ApplicationOutcomeDrawer';
import OfferNegotiationDrawer, { type OfferNegotiationDraft } from './OfferNegotiationDrawer';
import { getApplicationMaterialKit } from '@/services/materialKits';
import { listOpportunityFitV2Reviews } from '@/services/opportunityFitReviews';
import {
  type OpportunityFitReview,
} from '@/types/opportunityFitReview';
import { SourceStateTag } from './ui/SourceStateTag';
import { createPilotAttachmentDragBinding } from './PilotAttachmentHandle';
import { consumeMaterialKitHandoff, materialKitHandoffStore } from '@/features/pilot/materialKitHandoff';
import {
  getCurrentApplicationJd,
  getApplicationJdVersion,
  listApplicationJdVersions,
  saveApplicationJdVersion,
} from '@/services/applicationJdVersions';
import type { ApplicationJdDraft } from '@/types/applicationJdVersion';
import NextStepSuggestions from './NextStepSuggestions';
import type {
  NextStepDestination,
  NextStepSuggestions as NextStepSuggestionsModel,
  ReadonlyDestination,
  SuggestionSessionState,
} from '@/lib/nextStepSuggestions';
import styles from './ApplicationDetail.module.css';
import { getApplicationWorkspaceStage } from './applicationWorkspaceModel';
import { normalizeInterviewIndexItem } from '@/features/interviewEvents/interviewIndexContract';
import { projectInterviewEventCard } from '@/features/interviewEvents/interviewEventCard';
import { createCoreTaskSurfaceController, requestCoreTaskClose, type ActiveCoreTask, type CoreTaskLaunchResult, type CoreTaskSurfaceController } from '@/features/coreTaskSurface/controller';
import { CoreTaskSurfaceHost } from '@/features/coreTaskSurface/CoreTaskSurfaceHost';
import type { TaskLaunchRequest } from '@/features/coreTaskSurface/contracts';
import { isReviewReadinessDraftPending, isReviewReadinessDraftUnsaved, type ReviewReadinessOwnerDraft } from '@/features/reviewReadiness/contracts';
import {
  resolveApplicationTasks,
  type ApplicationTaskEvent,
  type ApplicationTaskReview,
  type ApplicationTaskResolution,
  type FrozenApplicationTaskSnapshot,
  type TaskSource,
} from '@/features/applicationTasks/applicationTaskResolver';

const { Title, Paragraph, Text } = Typography;

type ApplicationDetailTab = 'overview' | 'preparation' | 'progress';

const DETAIL_TABS: Array<{ id: ApplicationDetailTab; label: string }> = [
  { id: 'overview', label: '概览' },
  { id: 'preparation', label: '准备' },
  { id: 'progress', label: '进展' },
];

interface ApplicationProgressItem {
  id: string;
  title: string;
  timestamp?: string | null;
  detail?: string;
  kind: 'application' | 'event' | 'note' | 'offer';
}

const EVENT_SUBTYPE_LABELS: Readonly<Record<string, string>> = {
  assessment: '测评',
  technical: '技术面试',
  behavioral: '行为面试',
  phone: '电话沟通',
  onsite: '现场面试',
  hr: '人事面试',
  final: '终面',
  screening: '初筛',
};

const EVENT_STATUS_LABELS: Readonly<Record<string, string>> = {
  todo: '待处理',
  pending: '待确认',
  scheduled: '已安排',
  in_progress: '进行中',
  done: '已完成',
  completed: '已完成',
  cancelled: '已取消',
  deleted: '已取消',
  soft_deleted: '已取消',
};

function eventSubtypeLabel(value: string): string {
  if (!value) return '';
  return EVENT_SUBTYPE_LABELS[value] ?? (/\p{Script=Han}/u.test(value) ? value : '其他环节');
}

function eventStatusLabel(value: string): string {
  return EVENT_STATUS_LABELS[value] ?? '状态待确认';
}

function formatWorkspaceDate(value?: string | null, fallback = '待记录') {
  if (!value) return fallback;
  const date = dayjs(value);
  return date.isValid() ? date.format('YYYY-MM-DD HH:mm') : fallback;
}

function workspaceTimestamp(value?: string | null) {
  if (!value) return 0;
  const timestamp = dayjs(value).valueOf();
  return Number.isFinite(timestamp) ? timestamp : 0;
}

function isObjectRow(value: unknown): boolean {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

const TASK_COPY: Readonly<Record<string, { title: string; description: string; action: string }>> = Object.freeze({
  'application.opportunity_fit': { title: '岗位匹配与风险', description: '确认是否值得继续，以及需要补充的事实。', action: '开始判断' },
  'application.material_kit': { title: '投递准备', description: '选择简历、查看调整建议并完成提交前检查。', action: '打开准备' },
  'application.interview_prepare': { title: '面试准备', description: '围绕这场面试整理准备信息。', action: '开始准备' },
  'application.interview_review': { title: '面试复盘', description: '记录或查看这场面试的复盘。', action: '打开复盘' },
  'application.general_review': { title: '投递复盘', description: '查看不绑定具体事件的投递复盘。', action: '打开复盘' },
  'application.offer_review': { title: 'Offer 准备', description: '查看归属本次投递的 Offer 与准备信息。', action: '查看准备' },
  'application.record_outcome': { title: '投递结果', description: '记录本次投递的事实与结果。', action: '打开记录' },
});

function taskSource<T = never>(status: 'loading' | 'error' | 'ready' | 'absent', value?: T): TaskSource<T> {
  const immutableValue = value && typeof value === 'object' ? Object.freeze(value) : value;
  if (status === 'ready') return Object.freeze({ status: 'ready' as const, value: immutableValue as T });
  if (status === 'loading') return Object.freeze({ status: 'loading' as const });
  if (status === 'absent') return Object.freeze({ status: 'absent' as const });
  return Object.freeze({ status: 'error' as const });
}

function taskLaunchAccepted(result: CoreTaskLaunchResult): boolean {
  return result.kind === 'launched' || result.kind === 'focused_existing';
}

function emptyTaskSnapshot(): FrozenApplicationTaskSnapshot {
  return Object.freeze({
    application: taskSource('absent'),
    jd: taskSource('absent'),
    events: taskSource('absent'),
    offers: taskSource('absent'),
    materialKit: taskSource('absent'),
    reviews: taskSource('absent'),
    fit: taskSource('absent'),
    resume: taskSource('absent'),
    pending: taskSource('absent'),
    resultUnknown: taskSource('absent'),
  });
}

function projectApplicationEvent(
  event: ScheduleEvent,
  note: InterviewNote | undefined,
  now: number,
): ApplicationTaskEvent {
  // Missing ownership is intentionally represented as 0 so the resolver marks
  // the row unavailable instead of silently rebinding a legacy payload.
  const applicationId = event.application_id ?? 0;
  const schedule = event.scheduled_at;
  const scheduledAtState = typeof schedule === 'string' && schedule.length > 0 ? 'present' : 'absent';
  const normalized = normalizeInterviewIndexItem({
    application_id: applicationId,
    event_id: event.id,
    company_name: '',
    position_name: '',
    scheduled_at: schedule ?? '',
    scheduled_at_state: scheduledAtState,
    event_status: event.status,
    duration_minutes: event.duration_minutes,
    note_id: note?.id ?? null,
    note_source_status: null,
    has_review_proposal: false,
    review_summary: null,
    has_confirmed_knowledge: false,
    preparation_available: true,
  });
  const card = projectInterviewEventCard(normalized, now);
  return Object.freeze({
    applicationId,
    eventId: event.id,
    lifecycle: card.lifecycle,
    bucket: card.bucket,
    primaryAction: card.primaryAction,
    scheduledAtTimestamp: normalized.scheduleTimestamp,
    durationMinutes: normalized.duration_minutes,
    scheduledAtState,
  });
}

export interface ApplicationInterviewChoice {
  readonly eventId: number;
  readonly label: string;
  readonly scheduledAtTimestamp: number | null;
  readonly lifecycle: ApplicationTaskEvent['lifecycle'];
}

export interface ApplicationInterviewChoices {
  readonly preparation: readonly ApplicationInterviewChoice[];
  readonly review: readonly ApplicationInterviewChoice[];
}

/**
 * Project an application-only chooser from the same lifecycle contract used
 * by task resolution. Duplicate identities and malformed/foreign rows are
 * omitted instead of letting input order choose an owner.
 */
export function projectApplicationInterviewChoices(
  events: readonly ScheduleEvent[],
  applicationId: number,
  now: number,
): ApplicationInterviewChoices {
  const preparation: ApplicationInterviewChoice[] = [];
  const review: ApplicationInterviewChoice[] = [];
  if (!Number.isSafeInteger(applicationId) || applicationId <= 0 || !Number.isFinite(now)) {
    return Object.freeze({ preparation: Object.freeze(preparation), review: Object.freeze(review) });
  }
  try {
    if (!Array.isArray(events)) throw new TypeError('invalid events');
    const unique = new Map<number, ScheduleEvent | null>();
    for (let index = 0; index < events.length; index += 1) {
      if (!(index in events)) throw new TypeError('sparse events');
      const candidate = events[index];
      let eventId: unknown;
      try {
        eventId = candidate?.id;
      } catch {
        continue;
      }
      if (typeof eventId !== 'number' || !Number.isSafeInteger(eventId) || eventId <= 0) continue;
      unique.set(eventId, unique.has(eventId) ? null : candidate);
    }

    for (const [eventId, event] of unique) {
      if (!event) continue;
      try {
        if (event.application_id !== applicationId || event.event_type !== 'interview') continue;
        const projected = projectApplicationEvent(event, undefined, now);
        const subtype = typeof event.subtype === 'string' && event.subtype.trim()
          ? event.subtype.trim().slice(0, 80)
          : '面试';
        const timeLabel = projected.scheduledAtTimestamp === null
          ? '时间待确认'
          : formatWorkspaceDate(event.scheduled_at, '时间待确认');
        const choice = Object.freeze({
          eventId,
          label: `${subtype} · ${timeLabel}`,
          scheduledAtTimestamp: projected.scheduledAtTimestamp,
          lifecycle: projected.lifecycle,
        });
        const scheduledWithinWindow = projected.lifecycle === 'scheduled'
          && projected.bucket === 'upcoming'
          && projected.primaryAction === 'prepare'
          && projected.scheduledAtTimestamp !== null
          && projected.scheduledAtTimestamp > now
          && projected.scheduledAtTimestamp - now <= 24 * 60 * 60_000;
        const currentInProgress = projected.lifecycle === 'in_progress'
          && projected.bucket === 'upcoming'
          && projected.primaryAction === 'enter_preparation';
        if (scheduledWithinWindow || currentInProgress) preparation.push(choice);
        if (projected.lifecycle === 'completed'
          && projected.bucket === 'completed'
          && (projected.primaryAction === 'record_review' || projected.primaryAction === 'view_review')) {
          review.push(choice);
        }
      } catch {
        // A malformed row cannot become a chooser option.
      }
    }
  } catch {
    return Object.freeze({ preparation: Object.freeze([]), review: Object.freeze([]) });
  }
  const compare = (left: ApplicationInterviewChoice, right: ApplicationInterviewChoice): number => {
    if (left.scheduledAtTimestamp === null && right.scheduledAtTimestamp !== null) return 1;
    if (left.scheduledAtTimestamp !== null && right.scheduledAtTimestamp === null) return -1;
    if (left.scheduledAtTimestamp !== null && right.scheduledAtTimestamp !== null
      && left.scheduledAtTimestamp !== right.scheduledAtTimestamp) {
      return left.scheduledAtTimestamp < right.scheduledAtTimestamp ? -1 : 1;
    }
    return left.eventId - right.eventId;
  };
  preparation.sort(compare);
  review.sort(compare);
  return Object.freeze({
    preparation: Object.freeze(preparation),
    review: Object.freeze(review),
  });
}

interface ApplicationDetailProps {
  application: Application | null;
  open: boolean;
  onClose: () => void;
  /** Injected by AppShell; all task entrypoints share this controller. */
  taskController?: CoreTaskSurfaceController;
  onLaunchTask?: (request: TaskLaunchRequest) => CoreTaskLaunchResult;
  /** Narrow owner-internal Fit → Material transition adapter. */
  onConfirmedFitToMaterial?: (request: TaskLaunchRequest) => CoreTaskLaunchResult;
  onTaskSurfaceGuardChange?: (guard: { pending: boolean; unsaved: boolean }) => void;
  onOpenOffers?: () => void;
  offers?: Offer[];
  offersLoading?: boolean;
  offersError?: boolean;
  onRetryOffers?: () => void;
  onMockInterview?: (app: Application) => void;
  onAskPilot?: (app: Application, action?: PilotActionRequest) => void;
  onOpenPilotOpportunityFit?: (app: Application) => void;
  pilotInterviewReviewApplicationId?: number | null;
  onPilotInterviewReviewFocusConsumed?: () => void;
  pilotInterviewPreparationApplicationId?: number | null;
  pilotInterviewPreparationEventId?: number | null;
  onPilotInterviewPreparationFocusConsumed?: () => void;
  onAttachToPilot?: (attachment: import('@/types/chat').PilotContextAttachment) => void;
  /** A global/foreign Assistant attempt that cannot be safely attributed here. */
  externalTaskBlocked?: boolean;
  /** Owner-local Fit state used to guard the canonical task and bounded projection. */
  onOpportunityFitOwnerStateChange?: (state: { pending: boolean; resultUnknown: boolean; unsaved: boolean }) => void;
  onOpportunityFitProjectionChange?: (projection: OpportunityFitOwnerProjection) => void;
  opportunityFitOwnerStore?: OpportunityFitOwnerStore;
  interviewReviewProposalAttempts?: Record<number, InterviewReviewProposalAttemptState>;
  onInterviewReviewProposalAttemptChange?: (
    noteID: number,
    state: InterviewReviewProposalAttemptState | null,
  ) => void;
  reviewReadinessDrafts?: Readonly<Record<string, ReviewReadinessOwnerDraft>>;
  onReviewReadinessDraftChange?: (
    key: string,
    draft: ReviewReadinessOwnerDraft | null,
    retireOwnerKey?: string,
  ) => boolean | void;
  onOpenReviewStory?: (noteId: number, focusId: string) => void;
  onOpenReadinessPractice?: (launch: import('@/features/reviewReadiness/contracts').ReadinessPracticeLaunch) => void;
  onInterviewNoteChanged?: (noteID: number) => void;
  interviewKnowledgeCaptureDrafts?: Record<number, InterviewKnowledgeCaptureDraft>;
  onInterviewKnowledgeCaptureDraftChange?: (noteID: number, draft: InterviewKnowledgeCaptureDraft | null) => void;
  onInterviewKnowledgeCaptureNoteChanged?: (noteID: number) => void;
  resumes?: Resume[];
  interviewPreparationAttempts?: Record<string, InterviewPreparationAttemptState>;
  onInterviewPreparationAttemptChange?: (key: string, state: InterviewPreparationAttemptState | null) => void;
  interviewPreparationDrafts?: Record<string, InterviewPreparationDraft>;
  onInterviewPreparationDraftChange?: (key: string, draft: InterviewPreparationDraft | null) => void;
  interviewPreparationKnowledgeOptions?: InterviewPreparationKnowledgeOption[];
  /** AppShell's generation-fenced result from the locked readiness surface. */
  interviewPreparationSelection?: {
    readonly generation: number;
    readonly applicationId: number;
    readonly eventId: number;
    readonly resumeId: number;
  } | null;
  offerNegotiationDrafts?: Record<number, OfferNegotiationDraft>;
  onOfferNegotiationDraftChange?: (offerId: number, draft: OfferNegotiationDraft | null) => void;
  offerNegotiationEntryPoint?: 'ui' | 'pilot';
  resumesLoading?: boolean;
  resumesError?: boolean;
  taskNow?: number;
  nextStepSuggestions?: NextStepSuggestionsModel;
  nextStepSessionState?: SuggestionSessionState | null;
  onSetDisposition?: (applicationId: number, suggestionId: string, state: SuggestionSessionState | null) => void;
  onNextStepNavigate?: (destination: NextStepDestination | ReadonlyDestination) => void;
  isNavigationAvailable?: (destination: NextStepDestination | ReadonlyDestination) => boolean;
  onNextStepReadonlyNavigate?: (destination: ReadonlyDestination) => void;
  isReadonlyNavigationAvailable?: (destination: ReadonlyDestination) => boolean;
  applicationJdDraft?: ApplicationJdDraft;
  onApplicationJdDraftChange?: (applicationId: number, patch: Partial<ApplicationJdDraft> | null) => void;
}

export default function ApplicationDetail({ application, open, onClose, taskController, onLaunchTask, onConfirmedFitToMaterial, onTaskSurfaceGuardChange, onOpenOffers, offers, offersLoading = false, offersError = false, onRetryOffers, onMockInterview: _onMockInterview, onAskPilot, onOpenPilotOpportunityFit: _onOpenPilotOpportunityFit, externalTaskBlocked = false, onOpportunityFitOwnerStateChange, onOpportunityFitProjectionChange, opportunityFitOwnerStore, pilotInterviewReviewApplicationId, onPilotInterviewReviewFocusConsumed, pilotInterviewPreparationApplicationId, pilotInterviewPreparationEventId, onPilotInterviewPreparationFocusConsumed, onAttachToPilot, interviewReviewProposalAttempts, onInterviewReviewProposalAttemptChange, reviewReadinessDrafts, onReviewReadinessDraftChange, onOpenReviewStory, onOpenReadinessPractice, onInterviewNoteChanged, interviewKnowledgeCaptureDrafts, onInterviewKnowledgeCaptureDraftChange, onInterviewKnowledgeCaptureNoteChanged, resumes, resumesLoading = false, resumesError = false, taskNow, interviewPreparationAttempts, onInterviewPreparationAttemptChange, interviewPreparationDrafts, onInterviewPreparationDraftChange, interviewPreparationKnowledgeOptions = [], interviewPreparationSelection, offerNegotiationDrafts = {}, onOfferNegotiationDraftChange, offerNegotiationEntryPoint = 'ui', nextStepSuggestions, nextStepSessionState = null, onSetDisposition, onNextStepNavigate, isNavigationAvailable, onNextStepReadonlyNavigate, isReadonlyNavigationAvailable, applicationJdDraft, onApplicationJdDraftChange }: ApplicationDetailProps) {
  const queryClient = useQueryClient();
  const [eventFormOpen, setEventFormOpen] = useState(false);
  const [materialKitPrefill, setMaterialKitPrefill] = useState<{
    resumeID?: number;
    jdSnapshot?: string;
    jdVersionID?: number;
  }>({});
  const [editingNote, setEditingNote] = useState<InterviewNote | null>(null);
  const [knowledgeCaptureOpen, setKnowledgeCaptureOpen] = useState(false);
  const [pilotPreparationChoices, setPilotPreparationChoices] = useState<readonly ApplicationInterviewChoice[]>([]);
  const [pilotPreparationChooserOpen, setPilotPreparationChooserOpen] = useState(false);
  const [pilotReviewChoices, setPilotReviewChoices] = useState<readonly ApplicationInterviewChoice[]>([]);
  const [pilotReviewChooserOpen, setPilotReviewChooserOpen] = useState(false);
  const [jdEditorOpen, setJdEditorOpen] = useState(false);
  const [jdHistoryOpen, setJdHistoryOpen] = useState(false);
  const [selectedJdVersion, setSelectedJdVersion] = useState<number | null>(null);
  const [activeTab, setActiveTab] = useState<ApplicationDetailTab>('overview');
  const [opportunityFitOwnerState, setOpportunityFitOwnerState] = useState({
    pending: false,
    resultUnknown: false,
    unsaved: false,
  });
  const materialKitOwnerApplicationID = application?.id ?? 0;
  const materialKitOwnerSubscribe = useMemo(
    () => (listener: () => void) => materialKitOwnerStore.subscribe(materialKitOwnerApplicationID, listener),
    [materialKitOwnerApplicationID],
  );
  const materialKitOwnerGetSnapshot = useMemo(
    () => () => materialKitOwnerStore.getSnapshot(materialKitOwnerApplicationID),
    [materialKitOwnerApplicationID],
  );
  const materialKitOwnerSnapshot = useSyncExternalStore(
    materialKitOwnerSubscribe,
    materialKitOwnerGetSnapshot,
    materialKitOwnerGetSnapshot,
  );
  // The store snapshot is the only canonical material-owner state.  Deriving
  // this synchronously avoids a one-render mirror window in the host guard.
  const materialKitOwnerState = useMemo(() => ({
    applicationId: application?.id ?? null,
    pending: materialKitOwnerSnapshot.confirmation?.pending === true
      || materialKitOwnerSnapshot.proposal?.pending === true,
    resultUnknown: materialKitOwnerSnapshot.confirmation?.resultUnknown === true
      || materialKitOwnerSnapshot.proposal?.resultUnknown === true,
    sourceConflict: materialKitOwnerSnapshot.confirmation?.sourceConflict === true
      || materialKitOwnerSnapshot.proposal?.sourceConflict === true,
  }), [application?.id, materialKitOwnerSnapshot]);
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const handledPilotReviewIntentRef = useRef<string | null>(null);
  const handledPilotPreparationIntentRef = useRef<string | null>(null);
  const standaloneTaskControllerRef = useRef<CoreTaskSurfaceController | null>(null);
  if (!taskController && !standaloneTaskControllerRef.current) {
    standaloneTaskControllerRef.current = createCoreTaskSurfaceController();
  }
  const effectiveTaskController = taskController ?? standaloneTaskControllerRef.current!;
  const localTaskSurfaceGuardRef = useRef({ pending: false, unsaved: false });
  const taskSurfaceState = useSyncExternalStore(
    effectiveTaskController.subscribe,
    effectiveTaskController.getState,
    effectiveTaskController.getState,
  );
  const activeTask = taskSurfaceState.active?.ref.applicationId === application?.id
    ? taskSurfaceState.active
    : null;
  useEffect(() => {
    setOpportunityFitOwnerState({ pending: false, resultUnknown: false, unsaved: false });
  }, [application?.id]);
  // AppShell supplies the shared clock snapshot; the standalone fallback is
  // deterministic and never lets the adapter read wall-clock state.
  const resolverNow = taskNow ?? 0;
  const offerRecords = Array.isArray(offers) ? offers : [];
  const resumeRecords = Array.isArray(resumes) ? resumes : [];

  const launchTask = (request: TaskLaunchRequest): CoreTaskLaunchResult => {
    const result = onLaunchTask?.(request) ?? effectiveTaskController.launch(request);
    if (result.kind === 'launched' || result.kind === 'focused_existing') setActiveTab('preparation');
    return result;
  };

  const isTaskGenerationCurrent = (generation: number): boolean => (
    effectiveTaskController.getState().active?.generation === generation
  );

  const closeTask = (generation: number): boolean => {
    const active = effectiveTaskController.getState().active;
    if (!active || active.generation !== generation) return false;
    return requestCoreTaskClose(effectiveTaskController, active, localTaskSurfaceGuardRef.current);
  };

  const applicationJdQuery = useQuery({
    queryKey: ['application-jd-current', application?.id],
    queryFn: () => getCurrentApplicationJd(application!.id),
    enabled: Boolean(application) && open,
  });
  const jdHistoryQuery = useQuery({
    queryKey: ['application-jd-history', application?.id],
    queryFn: () => listApplicationJdVersions(application!.id),
    enabled: Boolean(application) && open && jdHistoryOpen,
  });
  const jdDetailQuery = useQuery({
    queryKey: ['application-jd-detail', application?.id, selectedJdVersion],
    queryFn: () => getApplicationJdVersion(application!.id, selectedJdVersion!),
    enabled: Boolean(application) && open && selectedJdVersion !== null,
  });
  // These read-only adapters reuse the exact cache keys consumed by the
  // canonical drawers. They only project source state for task resolution;
  // the drawers remain the existing mutation/HITL owners.
  const materialKitQuery = useQuery({
    queryKey: ['application-material-kit', application?.id],
    queryFn: () => getApplicationMaterialKit(application!.id),
    enabled: Boolean(application) && open,
  });
  const opportunityFitHistoryQuery = useQuery({
    queryKey: ['opportunity-fit-v2-reviews', application?.id],
    queryFn: () => listOpportunityFitV2Reviews(application!.id),
    enabled: Boolean(application) && open,
    retry: false,
  });
  const jdSave = useMutation({
    mutationFn: (draft: ApplicationJdDraft) => saveApplicationJdVersion(application!.id, {
      jd_text: draft.jdText,
      source_url: draft.sourceUrl.trim() || null,
      expected_current_version_id: draft.expectedCurrentVersionId,
      idempotency_key: draft.idempotencyKey!,
    }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['application-jd-current', application?.id] });
      queryClient.invalidateQueries({ queryKey: ['application-jd-history', application?.id] });
      onApplicationJdDraftChange?.(application!.id, null);
      setJdEditorOpen(false);
      message.success('\u5c97\u4f4d\u8d44\u6599\u5df2\u4fdd\u5b58');
    },
    onError: (error: Error & { status?: number; code?: string }) => {
      if (error.code === 'application_jd_stale_current_version') {
        void applicationJdQuery.refetch().then(({ data }) => {
          onApplicationJdDraftChange?.(application!.id, {
            expectedCurrentVersionId: data?.current?.id ?? null,
            idempotencyKey: null,
            pendingOperation: null,
            resultUnknown: false,
          });
        });
        message.error('\u5c97\u4f4d\u8d44\u6599\u5df2\u66f4\u65b0\uff0c\u5df2\u5237\u65b0\u5f53\u524d\u7248\u672c\uff0c\u8bf7\u786e\u8ba4\u540e\u518d\u4fdd\u5b58');
        return;
      }
      if (!error.status || error.status >= 500) {
        onApplicationJdDraftChange?.(application!.id, { resultUnknown: true, pendingOperation: 'save' });
        message.error('\u4fdd\u5b58\u7ed3\u679c\u5f85\u786e\u8ba4\uff0c\u53ef\u4f7f\u7528\u539f\u5c1d\u8bd5\u91cd\u8bd5');
        return;
      }
      onApplicationJdDraftChange?.(application!.id, { idempotencyKey: null, pendingOperation: null, resultUnknown: false });
      message.error('\u5c97\u4f4d\u8d44\u6599\u4e0d\u80fd\u4fdd\u5b58');
    },
  });

  const startJdEditor = () => {
    if (applicationJdQuery.isLoading || applicationJdQuery.isError) return;
    const current = applicationJdQuery.data?.current;
    const draft = applicationJdDraft;
    onApplicationJdDraftChange?.(application!.id, {
      jdText: draft?.jdText ?? current?.jd_text ?? '',
      sourceUrl: draft?.sourceUrl ?? current?.source_url ?? '',
      expectedCurrentVersionId: draft?.expectedCurrentVersionId ?? current?.id ?? null,
      idempotencyKey: draft?.idempotencyKey ?? null,
      resultUnknown: draft?.resultUnknown ?? false,
      pendingOperation: draft?.pendingOperation ?? null,
    });
    setJdEditorOpen(true);
  };

  const submitJd = () => {
    if (!application || !applicationJdDraft) return;
    const key = applicationJdDraft.idempotencyKey ?? crypto.randomUUID().replace(/[^A-Za-z0-9_-]/g, '').slice(0, 32);
    const draft = { ...applicationJdDraft, idempotencyKey: key, pendingOperation: 'save' as const };
    onApplicationJdDraftChange?.(application.id, draft);
    jdSave.mutate(draft);
  };

  useEffect(() => {
    setMaterialKitPrefill({});
    setActiveTab('overview');
    setPilotPreparationChooserOpen(false);
    setPilotPreparationChoices([]);
    setPilotReviewChooserOpen(false);
    setPilotReviewChoices([]);
    handledPilotReviewIntentRef.current = null;
    handledPilotPreparationIntentRef.current = null;
  }, [application?.id, open]);

  useEffect(() => {
    const active = effectiveTaskController.getState().active;
    if (active && application?.id && active.ref.applicationId !== application.id) {
      // A remounted detail must never display another application's owner.
      if (requestCoreTaskClose(effectiveTaskController, active, localTaskSurfaceGuardRef.current)) {
        effectiveTaskController.markClosed(active.generation);
      }
    }
    return () => {
      const current = effectiveTaskController.getState().active;
      if (current && current.ref.applicationId === application?.id) {
        if (requestCoreTaskClose(effectiveTaskController, current, localTaskSurfaceGuardRef.current)) {
          effectiveTaskController.markClosed(current.generation);
        }
      }
    };
  }, [application?.id, effectiveTaskController, open, taskController]);

  useEffect(() => {
    if (!application || !open) return;
    const handoff = consumeMaterialKitHandoff(application.id);
    if (!handoff) return;
    const suggestedResumeId = handoff.hints?.suggestedResumeId;
    const alreadyActive = activeTask?.ref.taskId === 'application.material_kit'
      && activeTask.ref.applicationId === application.id;
    if (!alreadyActive) {
      const request: TaskLaunchRequest = {
        ref: { taskId: 'application.material_kit', applicationId: application.id },
        source: 'deep_link',
        hints: suggestedResumeId ? { suggestedResumeId } : undefined,
      };
      const confirmedFitHandoff = activeTask?.ref.taskId === 'application.opportunity_fit'
        && activeTask.ref.applicationId === application.id
        && handoff.source === 'pilot';
      const result = confirmedFitHandoff
        ? (onConfirmedFitToMaterial?.(request) ?? launchTask(request))
        : launchTask(request);
      if (result.kind !== 'launched' && result.kind !== 'focused_existing') {
        // Preserve a denied handoff for the eventual guarded retry. This is a
        // local navigation token, not a domain write.
        materialKitHandoffStore.write(handoff);
        return;
      }
    }
    setMaterialKitPrefill((current) => Object.keys(current).length > 0 ? current : {
      resumeID: suggestedResumeId,
    });
  }, [activeTask, application?.id, onConfirmedFitToMaterial, open]);

  const notesQuery = useQuery({
    queryKey: ['notes', application?.id],
    queryFn: () => listNotesByApp(application!.id),
    enabled: !!application,
  });

  const eventsQuery = useQuery({
    queryKey: ['events', application?.id],
    queryFn: () => listEvents({ application_id: application!.id }),
    enabled: !!application && open,
  });

  const eventRowsAreObjects = Array.isArray(eventsQuery.data) && eventsQuery.data.every(isObjectRow);
  const noteRowsAreObjects = Array.isArray(notesQuery.data) && notesQuery.data.every(isObjectRow);
  const allEvents: ScheduleEvent[] = eventRowsAreObjects ? (eventsQuery.data as ScheduleEvent[]) : [];
  const noteRecords: InterviewNote[] = noteRowsAreObjects ? (notesQuery.data as InterviewNote[]) : [];
  const canonicalInterviewChoices = useMemo(
    () => projectApplicationInterviewChoices(allEvents, application?.id ?? 0, resolverNow),
    [allEvents, application?.id, resolverNow],
  );
  const interviewStageDataReady = application?.status !== 'interview'
    || (
      !eventsQuery.isLoading
      && !eventsQuery.isError
      && eventRowsAreObjects
      && !notesQuery.isLoading
      && !notesQuery.isError
      && noteRowsAreObjects
    );
  const interviewReviewDataReady = !eventsQuery.isLoading
    && !eventsQuery.isError
    && eventRowsAreObjects
    && !notesQuery.isLoading
    && !notesQuery.isError
    && noteRowsAreObjects;

  useEffect(() => {
    if (pilotInterviewReviewApplicationId == null) {
      handledPilotReviewIntentRef.current = null;
      return;
    }
    if (!application || !open || pilotInterviewReviewApplicationId !== application.id || !interviewReviewDataReady) return;
    const intentKey = `${application.id}:review`;
    if (handledPilotReviewIntentRef.current === intentKey) return;
    handledPilotReviewIntentRef.current = intentKey;
    // Pilot's application-only intent has no trusted Event identity. Always
    // show an explicit chooser, including the singleton case; only a user
    // click may construct the exact event review ref.
    setPilotReviewChoices(canonicalInterviewChoices.review);
    setPilotReviewChooserOpen(true);
    onPilotInterviewReviewFocusConsumed?.();
  }, [application, canonicalInterviewChoices.review, interviewReviewDataReady, open, onPilotInterviewReviewFocusConsumed, pilotInterviewReviewApplicationId]);

  useEffect(() => {
    if (pilotInterviewPreparationApplicationId == null) {
      handledPilotPreparationIntentRef.current = null;
      return;
    }
    if (!application || !open || pilotInterviewPreparationApplicationId !== application.id || eventsQuery.isLoading || eventsQuery.isError || !eventRowsAreObjects) return;
    const intentKey = `${application.id}:${pilotInterviewPreparationEventId ?? 'choose'}`;
    if (handledPilotPreparationIntentRef.current === intentKey) return;
    handledPilotPreparationIntentRef.current = intentKey;
    if (pilotInterviewPreparationEventId == null) {
      // An application-only intent enters an explicit chooser even when there
      // happens to be one event; event identity is never inferred here.
      setPilotPreparationChoices(canonicalInterviewChoices.preparation);
      setPilotPreparationChooserOpen(true);
    } else if (canonicalInterviewChoices.preparation.some(
      (choice) => choice.eventId === pilotInterviewPreparationEventId,
    )) {
      const result = launchTask({
        ref: {
          taskId: 'application.interview_prepare',
          applicationId: application.id,
          eventId: pilotInterviewPreparationEventId,
        },
        source: 'pilot',
        focus: 'current',
      });
      if (result.kind !== 'launched' && result.kind !== 'focused_existing') {
        handledPilotPreparationIntentRef.current = null;
        return;
      }
      setPilotPreparationChooserOpen(false);
    } else {
      setPilotPreparationChoices([]);
      setPilotPreparationChooserOpen(true);
    }
    onPilotInterviewPreparationFocusConsumed?.();
  }, [application, canonicalInterviewChoices.preparation, eventRowsAreObjects, eventsQuery.data, eventsQuery.isError, eventsQuery.isLoading, open, pilotInterviewPreparationEventId, onPilotInterviewPreparationFocusConsumed, pilotInterviewPreparationApplicationId]);

  const invalidateNotes = () => {
    if (application) queryClient.invalidateQueries({ queryKey: ['notes', application.id] });
    queryClient.invalidateQueries({ queryKey: ['notes', 'all'] });
  };

  const removeNoteMut = useMutation({
    mutationFn: (id: number) => removeNote(id),
    onSuccess: () => {
      message.success('已删除');
      invalidateNotes();
    },
    onError: () => message.error('删除失败'),
  });

  const updateNoteMut = useMutation({
    mutationFn: ({ id, input }: { id: number; input: CreateNoteInput }) => updateNote(id, input),
    onSuccess: (_data, variables) => {
      onInterviewNoteChanged?.(variables.id);
      onInterviewKnowledgeCaptureNoteChanged?.(variables.id);
      message.success('已更新面试复盘');
      setEditingNote(null);
      invalidateNotes();
    },
    onError: () => message.error('更新失败'),
  });

  const createEventNoteMut = useMutation({
    mutationFn: (input: CreateNoteInput) => createNote(application!.id, input),
    onSuccess: () => {
      message.success('已保存面试复盘');
      invalidateNotes();
    },
    onError: () => message.error('保存复盘失败'),
  });

  const closeDetail = () => {
    setEventFormOpen(false);
    setMaterialKitPrefill({});
    setEditingNote(null);
    setKnowledgeCaptureOpen(false);
    setPilotPreparationChoices([]);
    setPilotPreparationChooserOpen(false);
    setPilotReviewChoices([]);
    setPilotReviewChooserOpen(false);
    const active = effectiveTaskController.getState().active;
    if (active && active.ref.applicationId === application?.id
      && requestCoreTaskClose(effectiveTaskController, active, localTaskSurfaceGuardRef.current)) {
      effectiveTaskController.markClosed(active.generation);
    }
    onClose();
  };

  const openKnowledgeCapture = (note: InterviewNote) => {
    const existing = interviewKnowledgeCaptureDrafts?.[note.id] ?? createInterviewKnowledgeCaptureDraft();
    onInterviewKnowledgeCaptureDraftChange?.(note.id, existing);
    setEditingNote(note);
    setKnowledgeCaptureOpen(true);
  };

  const activeTaskRuntimeState = useMemo(() => {
    if (!activeTask) return { pending: false, resultUnknown: false };
    const fitOwnerPending = activeTask.ref.taskId === 'application.opportunity_fit'
      && opportunityFitOwnerState.pending;
    const materialOwnerStateForApplication = materialKitOwnerState.applicationId === application?.id
      ? materialKitOwnerState
      : null;
    const reviewNote = activeTask.ref.taskId === 'application.interview_review' && activeTask.ref.eventId !== undefined
      ? noteRecords.find((note) => note.application_event_id === activeTask.ref.eventId)
      : undefined;
    const preparationAttempt = activeTask.ref.taskId === 'application.interview_prepare' && activeTask.ref.eventId !== undefined
      ? interviewPreparationAttempts?.[`${application?.id}:${activeTask.ref.eventId}`]
      : undefined;
    const reviewAttempt = reviewNote ? interviewReviewProposalAttempts?.[reviewNote.id] : undefined;
    const localPending = Boolean(preparationAttempt || reviewAttempt);
    const localResultUnknown = Boolean(
      (preparationAttempt?.result_unknown)
      || (reviewAttempt?.result_unknown)
      || (activeTask.ref.taskId === 'application.opportunity_fit' && opportunityFitOwnerState.resultUnknown)
      || (activeTask.ref.taskId === 'application.material_kit' && materialOwnerStateForApplication?.resultUnknown),
    );
    return {
      pending: fitOwnerPending || localPending || Boolean(
        activeTask.ref.taskId === 'application.material_kit' && materialOwnerStateForApplication?.pending,
      ),
      resultUnknown: localResultUnknown,
    };
  }, [activeTask, application?.id, interviewPreparationAttempts, interviewReviewProposalAttempts, materialKitOwnerState, noteRecords, notesQuery.data, opportunityFitOwnerState.pending, opportunityFitOwnerState.resultUnknown]);

  const taskSnapshot = useMemo<FrozenApplicationTaskSnapshot>(() => {
    if (!application) return emptyTaskSnapshot();
    const currentJd = applicationJdQuery.data?.current;
    const eventSource: FrozenApplicationTaskSnapshot['events'] = eventsQuery.isLoading
      ? taskSource('loading')
      : eventsQuery.isError
        ? taskSource('error')
        : eventsQuery.data === undefined
          ? taskSource('absent')
          : !eventRowsAreObjects
            ? taskSource('error')
            : taskSource('ready', Object.freeze(allEvents
              .filter((event) => event.event_type === 'interview')
              .map((event) => projectApplicationEvent(
                event,
                noteRecords.find((note) => note.application_event_id === event.id),
                resolverNow,
              ))));
    const reviewRows = Object.freeze(noteRecords.map((note) => Object.freeze({
      applicationId: application.id,
      // Preserve an omitted event identity as omitted. The resolver then
      // emits event_contract_invalid instead of rebinding it to application
      // scope; only an explicit null is a general review.
      eventId: note.application_event_id,
      reviewId: note.id,
    } as unknown as ApplicationTaskReview)));
    const reviewSource: FrozenApplicationTaskSnapshot['reviews'] = notesQuery.isLoading
      ? taskSource('loading')
      : notesQuery.isError
        ? taskSource('error')
        : notesQuery.data === undefined
          ? taskSource('absent')
          : !noteRowsAreObjects
            ? taskSource('error')
            : taskSource('ready', reviewRows);
    const jdSource: FrozenApplicationTaskSnapshot['jd'] = applicationJdQuery.isLoading
      ? taskSource('loading')
      : applicationJdQuery.isError
        ? taskSource('error')
        : applicationJdQuery.data === undefined
          ? taskSource('absent')
          : taskSource('ready', currentJd ? { id: currentJd.id, versionId: currentJd.id } : null);
    const offerSource: FrozenApplicationTaskSnapshot['offers'] = offersLoading
      ? taskSource('loading')
      : offersError
        ? taskSource('error')
          : offers === undefined
            ? taskSource('absent')
          : !Array.isArray(offers) || offerRecords.some((offer) => !isObjectRow(offer))
            ? taskSource('error')
            : taskSource('ready', Object.freeze(offerRecords.map((offer) => Object.freeze({
              id: offer.id,
              applicationId: offer.application_id ?? 0,
              status: offer.status,
              deadline: offer.deadline,
            }))));
    const resumeSource: FrozenApplicationTaskSnapshot['resume'] = resumesLoading
      ? taskSource('loading')
      : resumesError
        ? taskSource('error')
      : resumes === undefined
        ? taskSource('absent')
        : !Array.isArray(resumes) || resumeRecords.some((resume) => !isObjectRow(resume))
          ? taskSource('error')
        : taskSource('ready', resumeRecords.length === 1
          ? { id: resumeRecords[0].id, selected: true }
          : null);
    const materialSource: FrozenApplicationTaskSnapshot['materialKit'] = materialKitQuery.isLoading
      ? taskSource('loading')
      : materialKitQuery.isError
        ? taskSource('error')
        : materialKitQuery.data === undefined
          ? taskSource('absent')
          : taskSource('ready', materialKitQuery.data ? {
            applicationId: materialKitQuery.data.application_id,
            status: materialKitQuery.data.status,
            updatedAt: materialKitQuery.data.updated_at,
          } : null);
    const fitSource: FrozenApplicationTaskSnapshot['fit'] = opportunityFitHistoryQuery.isLoading
      ? taskSource('loading')
      : opportunityFitHistoryQuery.isError
        ? taskSource('error')
        : opportunityFitHistoryQuery.data === undefined
          ? taskSource('absent')
          : !Array.isArray(opportunityFitHistoryQuery.data)
            || !opportunityFitHistoryQuery.data.every(isObjectRow)
            ? taskSource('error')
          : taskSource('ready', (() => {
            const history = opportunityFitHistoryQuery.data;
            const latest = [...history].sort((left, right) => (
              (typeof right.id === 'number' ? right.id : 0)
              - (typeof left.id === 'number' ? left.id : 0)
            ))[0];
            return latest ? { reviewId: latest.review_id, status: latest.latest_stage?.stage_status } : null;
          })());
    const runtimePendingSource: FrozenApplicationTaskSnapshot['pending'] = externalTaskBlocked
      ? taskSource('error')
      : activeTaskRuntimeState.pending && !activeTaskRuntimeState.resultUnknown && activeTask
        ? taskSource('ready', { ref: activeTask.ref })
        : taskSource('ready', null);
    const runtimeUnknownSource: FrozenApplicationTaskSnapshot['resultUnknown'] = externalTaskBlocked
      ? taskSource('error')
      : activeTaskRuntimeState.resultUnknown && activeTask
        ? taskSource('ready', { ref: activeTask.ref })
        : taskSource('ready', null);
    return Object.freeze({
      application: taskSource('ready', { id: application.id, status: application.status }),
      jd: jdSource,
      events: eventSource,
      offers: offerSource,
      materialKit: materialSource,
      reviews: reviewSource,
      fit: fitSource,
      resume: resumeSource,
      pending: runtimePendingSource,
      resultUnknown: runtimeUnknownSource,
    });
  }, [activeTask, activeTaskRuntimeState.pending, activeTaskRuntimeState.resultUnknown, allEvents, application?.id, application?.status, applicationJdQuery.data, applicationJdQuery.data?.current, applicationJdQuery.isError, applicationJdQuery.isLoading, eventRowsAreObjects, eventsQuery.data, eventsQuery.isError, eventsQuery.isLoading, externalTaskBlocked, materialKitQuery.data, materialKitQuery.isError, materialKitQuery.isLoading, noteRecords, noteRowsAreObjects, notesQuery.data, notesQuery.isError, notesQuery.isLoading, offers, offersError, offersLoading, opportunityFitHistoryQuery.data, opportunityFitHistoryQuery.isError, opportunityFitHistoryQuery.isLoading, resolverNow, resumeRecords, resumes, resumesError, resumesLoading]);
  const applicationTaskResolution: ApplicationTaskResolution = useMemo(
    () => resolveApplicationTasks(taskSnapshot, resolverNow),
    [resolverNow, taskSnapshot],
  );
  const taskOwnerOpen = taskSurfaceState.phase !== 'closing';
  const launchMaterialKit = (
    prefill: { resumeID?: number; jdSnapshot?: string; jdVersionID?: number } = {},
    confirmedFitToMaterial = false,
    source: TaskLaunchRequest['source'] = 'application_task_card',
  ) => {
    if (!application) return;
    const request: TaskLaunchRequest = {
      ref: { taskId: 'application.material_kit', applicationId: application.id },
      source,
      hints: prefill.resumeID ? { suggestedResumeId: prefill.resumeID } : undefined,
    };
    const result = confirmedFitToMaterial
      ? (onConfirmedFitToMaterial?.(request) ?? launchTask(request))
      : launchTask(request);
    if (result.kind === 'launched') setMaterialKitPrefill(prefill);
    return result;
  };

  const renderTaskOwner = (active: ActiveCoreTask): ReactNode => {
    if (!application) return null;
    if (active.ref.applicationId !== application.id) return null;
    const isCurrent = () => isTaskGenerationCurrent(active.generation);
    const close = () => closeTask(active.generation);
    switch (active.ref.taskId) {
      case 'application.opportunity_fit':
        return (
          <OpportunityFitReviewDrawer
            application={application}
            open={taskOwnerOpen}
            currentJdText={applicationJdQuery.data?.current?.jd_text ?? ''}
            jdVersionId={applicationJdQuery.data?.current?.id ?? null}
            onOwnerStateChange={(state) => {
              if (!isCurrent()) return;
              setOpportunityFitOwnerState(state);
              onOpportunityFitOwnerStateChange?.(state);
            }}
            onOwnerProjectionChange={(projection) => {
              if (isCurrent()) onOpportunityFitProjectionChange?.(projection);
            }}
            ownerStore={opportunityFitOwnerStore}
            onApplicationMissing={() => {
              if (isCurrent()) onClose();
            }}
            onClose={close}
            onPrepareMaterials={(reviewOrResumeId: OpportunityFitReview | number, jdText: string, jdVersionId?: number) => {
              if (!isCurrent() || !jdVersionId) return;
              const resumeID = typeof reviewOrResumeId === 'number'
                ? reviewOrResumeId
                : reviewOrResumeId.source.resume.id;
              launchMaterialKit({ resumeID, jdSnapshot: jdText, jdVersionID: jdVersionId }, true);
            }}
          />
        );
      case 'application.material_kit': {
        const ownerState = materialKitOwnerState.applicationId === application.id
          ? materialKitOwnerState
          : { pending: false, resultUnknown: false, sourceConflict: false };
        return (
          <MaterialKitDrawer
            application={application}
            open={taskOwnerOpen}
            onClose={() => {
              if (!closeTask(active.generation)) return;
              setMaterialKitPrefill({});
            }}
            initialResumeID={materialKitPrefill.resumeID}
            initialJdSnapshot={materialKitPrefill.jdSnapshot}
            initialJdVersionID={materialKitPrefill.jdSnapshot && !materialKitPrefill.jdVersionID
              ? undefined
              : materialKitPrefill.jdVersionID ?? applicationJdQuery.data?.current?.id}
            pendingState={ownerState.pending ? 'pending' : 'none'}
            resultUnknown={ownerState.resultUnknown}
            sourceConflict={ownerState.sourceConflict}
            onOwnerStateChange={(state) => {
              if (!isCurrent()) return;
              materialKitOwnerStore.updateTransientState(application.id, state);
            }}
          />
        );
      }
      case 'application.interview_prepare': {
        const eventId = active.ref.eventId;
        if (eventId === undefined) return <div role="alert">请先选择要准备的面试。</div>;
        const preparationTask = applicationTaskResolution.tasks.find(
          (task) => task.taskId === 'application.interview_prepare' && task.ref.eventId === eventId,
        );
        if (!preparationTask?.executable) return <div role="status">该面试当前不可准备，请先确认日程状态。</div>;
        const lockedSelection = interviewPreparationSelection
          && interviewPreparationSelection.generation === active.generation
          && interviewPreparationSelection.applicationId === application.id
          && interviewPreparationSelection.eventId === eventId
          && Number.isSafeInteger(interviewPreparationSelection.resumeId)
          && interviewPreparationSelection.resumeId > 0
          && resumeRecords.some((resume) => {
            try {
              const candidate = resume as Resume & { deleted?: boolean; hidden?: boolean; visible?: boolean };
              return candidate.id === interviewPreparationSelection.resumeId
                && candidate.deleted_at == null
                && candidate.deleted !== true
                && candidate.hidden !== true
                && candidate.visible !== false;
            } catch {
              return false;
            }
          })
          ? interviewPreparationSelection
          : null;
        if (taskController && !lockedSelection) {
          return <div role="status">请先在面试准备中心选择一份可用简历。</div>;
        }
        const preparationKey = `${application.id}:${eventId}`;
        return (
          <InterviewPreparationProposalDrawer
            key={`${application.id}:${eventId}:${active.generation}:${lockedSelection?.resumeId ?? 0}`}
            open={taskOwnerOpen}
            context={{
              applicationId: application.id,
              eventId,
              resumeId: lockedSelection?.resumeId ?? 0,
              jdText: applicationJdQuery.data?.current?.jd_text ?? '',
              jdVersionId: applicationJdQuery.data?.current?.id ?? null,
              knowledgeSelections: [],
              userAssertions: [],
            }}
            resumeOptions={resumeRecords}
            knowledgeOptions={interviewPreparationKnowledgeOptions}
            ownerGeneration={active.generation}
            attemptState={interviewPreparationAttempts?.[preparationKey]}
            draft={interviewPreparationDrafts?.[preparationKey]}
              onAttemptStateChange={(state) => {
                if (isCurrent()) onInterviewPreparationAttemptChange?.(preparationKey, state);
              }}
              onOpenPractice={onOpenReadinessPractice}
              onDraftChange={(draft) => {
                if (isCurrent()) onInterviewPreparationDraftChange?.(preparationKey, draft);
              }}
              onClose={close}
          />
        );
      }
      case 'application.interview_review':
      case 'application.general_review': {
        const eventId = active.ref.eventId;
        if (eventId !== undefined) {
          const reviewTask = applicationTaskResolution.tasks.find(
            (task) => task.taskId === 'application.interview_review' && task.ref.eventId === eventId,
          );
          if (!reviewTask?.executable) return <div role="status">该面试当前不可复盘，请先确认日程状态。</div>;
        }
        const note = eventId === undefined
          ? noteRecords.find((item) => item.application_event_id === null)
          : noteRecords.find((item) => item.application_event_id === eventId);
        const compatibleEditingNote = editingNote && (
          eventId === undefined
            ? editingNote.application_event_id === null
            : editingNote.application_event_id === eventId
        ) ? editingNote : undefined;
        const ownerNote = note ?? compatibleEditingNote;
        if (eventId !== undefined && note) {
          return (
            <InterviewReviewProposalDrawer
              open={taskOwnerOpen}
              note={note}
              applicationId={application.id}
              eventID={eventId}
              attemptState={interviewReviewProposalAttempts?.[note.id]}
              ownerGeneration={active.generation}
              recoveryOwnerGeneration={active.recoveryGeneration}
              readinessDrafts={reviewReadinessDrafts}
              onReadinessDraftChange={onReviewReadinessDraftChange}
              onAttemptStateChange={(state) => {
                if (isCurrent()) onInterviewReviewProposalAttemptChange?.(note.id, state);
              }}
              onOpenStory={onOpenReviewStory}
              onClose={() => {
                if (!isTaskGenerationCurrent(active.generation)) return;
                setEditingNote(null);
                close();
              }}
            />
          );
        }
        return (
          <ReviewFormDrawer
            open={taskOwnerOpen}
            applications={[application]}
            initialApplication={application}
            note={ownerNote}
            initialEventID={eventId ?? null}
            saving={updateNoteMut.isPending || createEventNoteMut.isPending}
            onSubmit={(input) => {
              if (!isCurrent()) return;
              const currentNote = ownerNote;
              if (currentNote) updateNoteMut.mutate({ id: currentNote.id, input });
              else createEventNoteMut.mutate(input);
            }}
            onClose={() => {
              if (!isTaskGenerationCurrent(active.generation)) return;
              setEditingNote(null);
              close();
            }}
          />
        );
      }
      case 'application.offer_review': {
        if (offersLoading) return <div role="status">Offer 信息正在读取。</div>;
        if (offersError) return <div role="alert">Offer 信息暂时无法读取。</div>;
        if (offers === undefined) return <div role="status">Offer 信息尚未加载。</div>;
        const linkedOffers = offerRecords.filter((offer) => offer.application_id === application.id);
        const suggestedOfferId = active.request.hints?.suggestedOfferId;
        const offer = linkedOffers.find((item) => item.id === suggestedOfferId)
          ?? (linkedOffers.length === 1 ? linkedOffers[0] : undefined);
        if (!offer) return <div role="status">暂无可聚焦的 Offer，请先确认本次投递的 Offer。</div>;
        return (
          <OfferNegotiationDrawer
            open={taskOwnerOpen}
            offer={offer}
            entrypoint={offerNegotiationEntryPoint}
            draft={offerNegotiationDrafts[offer.id]}
            onDraftChange={(draft) => {
              if (isCurrent()) onOfferNegotiationDraftChange?.(offer.id, draft);
            }}
            onClose={close}
          />
        );
      }
      case 'application.record_outcome':
        return (
          <ApplicationOutcomeDrawer
            application={application}
            open={taskOwnerOpen}
            onClose={close}
            resumes={resumeRecords}
            currentJd={applicationJdQuery.data?.current ?? null}
            events={allEvents}
            onAskPilot={isCurrent() ? onAskPilot : undefined}
          />
        );
      default:
        return <div role="alert">当前任务暂不可用，请稍后重试。</div>;
    }
  };

  useEffect(() => {
    const active = activeTask;
    const reviewNote = active?.ref.taskId === 'application.interview_review' && active.ref.eventId !== undefined
      ? noteRecords.find((note) => note.application_event_id === active.ref.eventId)
      : undefined;
    const pending = active && application
      ? Boolean(
      (active.ref.taskId === 'application.opportunity_fit'
        && (opportunityFitOwnerState.pending || opportunityFitOwnerState.resultUnknown))
      || (active.ref.taskId === 'application.material_kit'
        && materialKitOwnerState.applicationId === application.id
        && (materialKitOwnerState.pending || materialKitOwnerState.resultUnknown || materialKitOwnerState.sourceConflict))
      || (active.ref.taskId === 'application.interview_prepare' && active.ref.eventId !== undefined
        && interviewPreparationAttempts?.[`${application.id}:${active.ref.eventId}`])
      || (active.ref.taskId === 'application.interview_review' && active.ref.eventId !== undefined
        && reviewNote && (
          interviewReviewProposalAttempts?.[reviewNote.id]
          || Object.values(reviewReadinessDrafts ?? {}).some((draft) => (
            draft.ownerGeneration === active.generation
            && draft.noteId === reviewNote.id
            && isReviewReadinessDraftPending(draft)
          ))
        ))
      )
      : false;
    const unsaved = active
      ? Boolean(
      active.ref.taskId === 'application.opportunity_fit' && opportunityFitOwnerState.unsaved
      || active.ref.taskId === 'application.material_kit' && (
        Boolean(materialKitPrefill.jdSnapshot || materialKitPrefill.resumeID)
        || (
          materialKitOwnerSnapshot.generation > 0
          && Boolean(materialKitOwnerSnapshot.draft?.hasLocalState)
          && Boolean(materialKitOwnerSnapshot.draft?.draftDirty || materialKitOwnerSnapshot.draft?.resumeID || materialKitOwnerSnapshot.draft?.jdSnapshot)
        )
      )
      || active.ref.taskId === 'application.interview_prepare'
        && active.ref.eventId !== undefined
        && interviewPreparationDrafts?.[`${application?.id}:${active.ref.eventId}`]
      || active.ref.taskId === 'application.interview_review'
        && reviewNote
        && Object.values(reviewReadinessDrafts ?? {}).some((draft) => (
          draft.ownerGeneration === active.generation
          && draft.noteId === reviewNote.id
          && isReviewReadinessDraftUnsaved(draft)
        ))
      || active.ref.taskId === 'application.offer_review'
        && offerRecords.some((offer) => offer.application_id === application?.id && offerNegotiationDrafts?.[offer.id])
      )
      : false;
    localTaskSurfaceGuardRef.current = { pending, unsaved };
    onTaskSurfaceGuardChange?.({ pending, unsaved });
  }, [activeTask, application?.id, interviewPreparationAttempts, interviewPreparationDrafts, interviewReviewProposalAttempts, materialKitOwnerSnapshot, materialKitOwnerState, materialKitPrefill, noteRecords, notesQuery.data, offerNegotiationDrafts, offers, onTaskSurfaceGuardChange, opportunityFitOwnerState.pending, opportunityFitOwnerState.resultUnknown, opportunityFitOwnerState.unsaved, reviewReadinessDrafts]);

  if (!application || !open) return null;

  const applicationDragBinding = onAttachToPilot
    ? createPilotAttachmentDragBinding({
        kind: 'application',
        id: String(application.id),
        label: `${application.company_name} · ${application.position_name}`,
      })
    : undefined;

  const headerPrimaryTask = applicationTaskResolution.primaryTask;
  const upcomingPrepareTask = applicationTaskResolution.tasks.find(
    (task) => task.taskId === 'application.interview_prepare' && task.ref.eventId !== undefined && task.executable,
  );
  const upcomingEvent = upcomingPrepareTask?.ref.eventId === undefined
    ? undefined
    : allEvents.find((event) => event.id === upcomingPrepareTask.ref.eventId);
  const primaryInterviewLifecycle = headerPrimaryTask?.taskId === 'application.interview_prepare'
    ? 'scheduled'
    : headerPrimaryTask?.taskId === 'application.interview_review'
      ? 'completed'
      : 'unknown';
  const stage = getApplicationWorkspaceStage(application.status, {
    lifecycle: application.status === 'interview' ? primaryInterviewLifecycle : undefined,
    hasInterviewReview: headerPrimaryTask?.reason === 'interview_review_available',
  });
  const stageDataBlocked = application.status === 'interview' && !interviewStageDataReady;
  const stageDataHasError = eventsQuery.isError || notesQuery.isError;
  const stageLabel = stageDataBlocked
    ? stageDataHasError ? '面试进展暂不可读' : '面试进展读取中'
    : stage.label;
  const stagePrimaryActionLabel = stageDataBlocked
    ? stageDataHasError ? '重试日程和复盘' : '等待面试进展加载'
    : application.status === 'interview'
      ? headerPrimaryTask?.taskId === 'application.interview_prepare'
        ? '准备本轮面试'
        : headerPrimaryTask?.taskId === 'application.interview_review'
          ? headerPrimaryTask.reason === 'interview_review_available' ? '查看本轮复盘' : '完成面试复盘'
          : headerPrimaryTask
            ? TASK_COPY[headerPrimaryTask.taskId]?.action ?? '打开当前任务'
            : '暂无可用操作'
    : stage.primaryActionLabel;

  const retryStageData = () => {
    if (eventsQuery.isError) void eventsQuery.refetch();
    if (notesQuery.isError) void notesQuery.refetch();
  };

  const runStageAction = () => {
    if (stageDataBlocked) return;
    if (application.status === 'interview') {
      if (headerPrimaryTask) launchResolvedTask(headerPrimaryTask, 'application_header');
      return;
    }
    switch (stage.action) {
      case 'materials':
        {
          const currentJd = applicationJdQuery.data?.current;
          launchMaterialKit(currentJd ? { jdSnapshot: currentJd.jd_text, jdVersionID: currentJd.id } : {});
        }
        break;
      case 'followup':
      case 'written-test':
        setEventFormOpen(true);
        break;
      case 'offer': {
        const result = launchTask({
          ref: { taskId: 'application.offer_review', applicationId: application.id },
          source: 'application_header',
          focus: 'current',
        });
        // Standalone embedded callers historically used this callback to
        // navigate to the Offer collection. The composed AppShell keeps the
        // application-level owner mounted and therefore does not navigate.
        if (!taskController && result.kind === 'launched') onOpenOffers?.();
        break;
      }
      case 'outcome':
        launchTask({
          ref: { taskId: 'application.record_outcome', applicationId: application.id },
          source: 'application_header',
          focus: 'current',
        });
        break;
    }
  };

  const moreActionItems = [
    ...(onAskPilot ? [{ key: 'haru', label: '让 Haru 帮我', onClick: () => onAskPilot(application, { type: 'application_jd_save' }) }] : []),
    { key: 'jd', label: applicationJdQuery.data?.current ? '编辑岗位资料' : '添加岗位资料', onClick: startJdEditor },
    { key: 'schedule', label: '安排日程', onClick: () => setEventFormOpen(true) },
  ];

  const linkedOffers = offerRecords.filter((offer) => offer.application_id === application.id);
  const progressItems: ApplicationProgressItem[] = [
    {
      id: `application-created-${application.id}`,
      kind: 'application' as const,
      title: '创建投递',
      timestamp: application.created_at,
      detail: application.source ? `来源：${application.source}` : undefined,
    },
    {
      id: `application-updated-${application.id}`,
      kind: 'application' as const,
      title: `当前阶段：${stageLabel}`,
      timestamp: application.updated_at,
      detail: application.notes || undefined,
    },
    ...allEvents.map((event) => ({
      id: `event-${event.id}`,
      kind: 'event' as const,
      title: `${EVENT_TYPE_LABELS[event.event_type]}${event.subtype ? ` · ${eventSubtypeLabel(event.subtype)}` : ''}`,
      timestamp: event.scheduled_at,
      detail: [
        eventStatusLabel(event.status),
        event.location,
        event.notes,
      ].filter(Boolean).join(' · ') || undefined,
    })),
    ...noteRecords.map((note) => ({
      id: `note-${note.id}`,
      kind: 'note' as const,
      title: `面试复盘${note.round ? ` · ${note.round}` : ''}`,
      timestamp: note.created_at || note.date,
      detail: note.self_reflection || note.questions || note.difficulty_points || undefined,
    })),
    ...linkedOffers.map((offer) => ({
      id: `offer-${offer.id}`,
      kind: 'offer' as const,
      title: `Offer · ${offer.company_name} · ${offer.position_name}`,
      timestamp: offer.updated_at,
      detail: [
        `状态：${OFFER_STATUS_LABELS[offer.status]}`,
        offer.deadline ? `截止：${formatWorkspaceDate(offer.deadline, '未设置')}` : undefined,
      ].filter(Boolean).join(' · '),
    })),
  ].sort((left, right) => workspaceTimestamp(right.timestamp) - workspaceTimestamp(left.timestamp));

  const launchResolvedTask = (
    task: ApplicationTaskResolution['tasks'][number],
    source: TaskLaunchRequest['source'] = 'application_task_card',
  ) => {
    if (!task.executable || externalTaskBlocked) return;
    if (task.taskId === 'application.material_kit') {
      const currentJd = applicationJdQuery.data?.current;
      launchMaterialKit(currentJd ? {
        jdSnapshot: currentJd.jd_text,
        jdVersionID: currentJd.id,
      } : {}, false, source);
      return;
    }
    launchTask({
      ref: task.ref,
      source,
      focus: task.taskId === 'application.offer_review' ? 'current' : 'overview',
    });
  };

  const launchNoteReview = (note: InterviewNote) => {
    const eventId = note.application_event_id;
    const task = eventId === null
      ? applicationTaskResolution.tasks.find(
        (candidate) => candidate.taskId === 'application.general_review'
          && candidate.ref.applicationId === application.id,
      )
      : typeof eventId === 'number' && Number.isSafeInteger(eventId) && eventId > 0
        ? applicationTaskResolution.tasks.find(
          (candidate) => candidate.taskId === 'application.interview_review'
            && candidate.ref.eventId === eventId,
        )
        : undefined;
    if (!task?.executable || externalTaskBlocked) {
      message.warning('该复盘当前不可用，请先确认事件与复盘资料。');
      return;
    }
    const result = launchTask({
      ref: task.ref,
      source: 'application_task_card',
      focus: 'current',
    });
    if (taskLaunchAccepted(result)) setEditingNote(note);
  };

  const handleTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const direction = event.key === 'ArrowRight' || event.key === 'ArrowDown'
      ? 1
      : event.key === 'ArrowLeft' || event.key === 'ArrowUp'
        ? -1
        : 0;
    if (event.key === 'Home' || event.key === 'End') {
      event.preventDefault();
      const nextIndex = event.key === 'Home' ? 0 : DETAIL_TABS.length - 1;
      const nextTab = DETAIL_TABS[nextIndex];
      setActiveTab(nextTab.id);
      tabRefs.current[nextIndex]?.focus();
      return;
    }
    if (direction === 0) return;
    event.preventDefault();
    const nextIndex = (index + direction + DETAIL_TABS.length) % DETAIL_TABS.length;
    const nextTab = DETAIL_TABS[nextIndex];
    setActiveTab(nextTab.id);
    tabRefs.current[nextIndex]?.focus();
  };

  return (
    <>
      <Modal
        open={pilotPreparationChooserOpen}
        title="选择要准备的面试"
        footer={null}
        onCancel={() => {
          setPilotPreparationChooserOpen(false);
          setPilotPreparationChoices([]);
        }}
      >
        {pilotPreparationChoices.length > 0 ? (
          <Space direction="vertical" style={{ width: '100%' }}>
            {pilotPreparationChoices.map((choice) => (
              <Button
                key={choice.eventId}
                block
                disabled={externalTaskBlocked || !applicationTaskResolution.tasks.some(
                  (task) => task.taskId === 'application.interview_prepare'
                    && task.ref.eventId === choice.eventId
                    && task.executable,
                )}
                onClick={() => {
                  const task = applicationTaskResolution.tasks.find(
                    (candidate) => candidate.taskId === 'application.interview_prepare'
                      && candidate.ref.eventId === choice.eventId,
                  );
                  if (!task?.executable) return;
                  const result = launchTask({
                    ref: task.ref,
                    source: 'interview_event_card',
                    focus: 'current',
                  });
                  if (result.kind === 'launched' || result.kind === 'focused_existing') {
                    setPilotPreparationChooserOpen(false);
                    setPilotPreparationChoices([]);
                  }
                }}
              >
                {choice.label}
              </Button>
            ))}
          </Space>
        ) : <div role="status">当前没有可选择的面试，请先安排面试后再开始准备。</div>}
      </Modal>
      <Modal
        open={pilotReviewChooserOpen}
        title="选择要复盘的面试"
        footer={null}
        onCancel={() => {
          setPilotReviewChooserOpen(false);
          setPilotReviewChoices([]);
        }}
      >
        {pilotReviewChoices.length > 0 ? (
          <Space direction="vertical" style={{ width: '100%' }}>
            {pilotReviewChoices.map((choice) => (
              <Button
                key={choice.eventId}
                block
                disabled={externalTaskBlocked || !applicationTaskResolution.tasks.some(
                  (task) => task.taskId === 'application.interview_review'
                    && task.ref.eventId === choice.eventId
                    && task.executable,
                )}
                onClick={() => {
                  const task = applicationTaskResolution.tasks.find(
                    (candidate) => candidate.taskId === 'application.interview_review'
                      && candidate.ref.eventId === choice.eventId,
                  );
                  if (!task?.executable) {
                    message.warning('该面试尚无可用复盘，请先确认事件已完成。');
                    return;
                  }
                  const result = launchTask({
                    ref: {
                      taskId: 'application.interview_review',
                      applicationId: application.id,
                      eventId: choice.eventId,
                    },
                    source: 'interview_event_card',
                    focus: 'current',
                  });
                  if (taskLaunchAccepted(result)) {
                    setEditingNote(noteRecords.find((note) => note.application_event_id === choice.eventId) ?? null);
                    setPilotReviewChooserOpen(false);
                    setPilotReviewChoices([]);
                  }
                }}
              >
                {choice.label}
              </Button>
            ))}
          </Space>
        ) : <div role="status">当前没有可复盘的面试，请先完成或安排面试后再试。</div>}
      </Modal>
      <Modal
        open={jdEditorOpen}
        title={'\u6295\u9012\u5c97\u4f4d\u8d44\u6599'}
        okText={applicationJdDraft?.resultUnknown ? '\u4f7f\u7528\u539f\u5c1d\u8bd5\u91cd\u8bd5' : '\u4fdd\u5b58\u5c97\u4f4d\u8d44\u6599'}
        cancelText={'\u53d6\u6d88'}
        confirmLoading={jdSave.isPending}
        onOk={submitJd}
        onCancel={() => setJdEditorOpen(false)}
      >
        <Input.TextArea
          rows={10}
          value={applicationJdDraft?.jdText ?? applicationJdQuery.data?.current?.jd_text ?? ''}
          disabled={Boolean(applicationJdDraft?.resultUnknown)}
          onChange={(event) => onApplicationJdDraftChange?.(application!.id, { jdText: event.target.value })}
          placeholder={'\u7c98\u8d34\u5c97\u4f4d\u63cf\u8ff0'}
        />
        <Input
          style={{ marginTop: 12 }}
          value={applicationJdDraft?.sourceUrl ?? applicationJdQuery.data?.current?.source_url ?? ''}
          disabled={Boolean(applicationJdDraft?.resultUnknown)}
          onChange={(event) => onApplicationJdDraftChange?.(application!.id, { sourceUrl: event.target.value })}
          placeholder={'\u6765\u6e90 URL\uff08\u4ec5\u5c55\u793a\uff0c\u4e0d\u4f1a\u8bbf\u95ee\uff09'}
        />
        {applicationJdDraft?.resultUnknown && (
          <Paragraph type="warning" style={{ marginTop: 12, marginBottom: 0 }}>
            {'\u4fdd\u5b58\u7ed3\u679c\u5f85\u786e\u8ba4\uff0c\u8bf7\u4f7f\u7528\u539f\u5c1d\u8bd5\u91cd\u8bd5\u3002'}
          </Paragraph>
        )}
      </Modal>
      <Modal
        open={jdHistoryOpen}
        title={'\u5c97\u4f4d\u8d44\u6599\u5386\u53f2'}
        width={680}
        footer={null}
        onCancel={() => { setJdHistoryOpen(false); setSelectedJdVersion(null); }}
      >
        <div className={styles.jdHistoryList}>
          {jdHistoryQuery.isLoading ? <Spin /> : (jdHistoryQuery.data ?? []).map((version) => (
            <button
              key={version.id}
              type="button"
              className={`${styles.jdHistoryOption} ${selectedJdVersion === version.id ? styles.jdHistoryOptionSelected : ''}`}
              aria-pressed={selectedJdVersion === version.id}
              onClick={() => setSelectedJdVersion(version.id)}
            >
              <span className={styles.jdHistoryMeta}>
                <strong>版本 {version.version_number}</strong>
                <span>{version.source_kind === 'pilot' ? 'Pilot 保存' : '界面保存'}</span>
              </span>
              <span className={styles.jdHistoryPreview}>{version.preview.slice(0, 160)}</span>
            </button>
          ))}
          {!jdHistoryQuery.isLoading && (jdHistoryQuery.data ?? []).length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无岗位资料历史" />
          ) : null}
          {selectedJdVersion !== null && jdDetailQuery.data && (
            <div className={styles.jdHistoryDetail}>
              {jdDetailQuery.data.jd_text}
            </div>
          )}
        </div>
      </Modal>
      <section className={styles.detailWorkspace} {...applicationDragBinding}>
        <div className={styles.header}>
          <Button type="link" className={styles.backButton} icon={<ArrowLeftOutlined />} onClick={closeDetail}>
            返回上一层
          </Button>
          <div className={styles.titleRow}>
            <div className={styles.stageIdentity}>
              <Title level={3} className={styles.title}>
                {application.company_name} · {application.position_name}
              </Title>
              <Space wrap>
              <Tag color={stageDataBlocked ? 'orange' : 'green'}>{stageLabel}</Tag>
                <SourceStateTag state="current" detail="当前投递" />
                <Text type="secondary">
                  下一步时间：{eventsQuery.isLoading
                    ? '读取中'
                    : eventsQuery.isError
                      ? '暂时无法读取'
                      : upcomingEvent
                        ? dayjs(upcomingEvent.scheduled_at).format('M 月 D 日 HH:mm')
                        : '待安排'}
                </Text>
              </Space>
            </div>
            <Space>
              <Button
                type="primary"
                size="large"
                disabled={externalTaskBlocked
                  || (stageDataBlocked && !stageDataHasError)
                  || (application.status === 'interview' && !stageDataBlocked && headerPrimaryTask === null)}
                onClick={stageDataBlocked ? retryStageData : runStageAction}
              >
                {stagePrimaryActionLabel}
              </Button>
              <Dropdown menu={{ items: moreActionItems }} trigger={['click']}>
                <Button size="large" icon={<MoreOutlined />}>更多操作</Button>
              </Dropdown>
            </Space>
          </div>
        </div>

        <div className={styles.tabList} role="tablist" aria-label="投递详情分段">
          {DETAIL_TABS.map((tab, index) => (
            <button
              key={tab.id}
              ref={(element) => { tabRefs.current[index] = element; }}
              type="button"
              role="tab"
              id={`application-${tab.id}-tab`}
              aria-selected={activeTab === tab.id}
              aria-controls={`application-${tab.id}-panel`}
              tabIndex={activeTab === tab.id ? 0 : -1}
              className={`${styles.tab} ${activeTab === tab.id ? styles.tabActive : ''}`}
              onClick={() => setActiveTab(tab.id)}
              onKeyDown={(event) => handleTabKeyDown(event, index)}
            >
              {tab.label}
            </button>
          ))}
        </div>

        <div
          id="application-overview-panel"
          role="tabpanel"
          aria-labelledby="application-overview-tab"
          tabIndex={0}
          hidden={activeTab !== 'overview'}
          className={styles.tabPanel}
        >
          <section className={`${styles.workspaceSection} ${styles.overviewSection}`} aria-labelledby="application-overview-heading">
            <Title id="application-overview-heading" level={4} className={styles.workspaceSectionTitle}>概览</Title>
            <div className={styles.summaryGrid}>
              <div className={styles.summaryCard}>
                <Text type="secondary">当前阶段</Text>
                <Text strong>{stageLabel}</Text>
              </div>
              <div className={styles.summaryCard}>
                <Text type="secondary">下一时间</Text>
                <Text strong>
                  {eventsQuery.isLoading
                    ? '读取中'
                    : eventsQuery.isError
                      ? '暂时无法读取'
                      : upcomingEvent
                        ? formatWorkspaceDate(upcomingEvent.scheduled_at)
                        : '待安排'}
                </Text>
              </div>
              <div className={styles.summaryCard}>
                <Text type="secondary">最近变化</Text>
                <Text strong>{formatWorkspaceDate(application.updated_at, '暂无更新')}</Text>
                <Text type="secondary">投递记录已更新</Text>
              </div>
            </div>
            {eventsQuery.isError ? (
              <Alert type="warning" showIcon message="日程暂时无法读取" action={<Button size="small" onClick={() => void eventsQuery.refetch()}>重试</Button>} />
            ) : null}
            <div className={styles.overviewBlock}>
              <Text strong>JD 摘要</Text>
              {applicationJdQuery.isLoading ? <Spin size="small" /> : applicationJdQuery.isError ? (
                <Alert type="warning" showIcon message="岗位资料暂时无法读取" action={<Button size="small" onClick={() => void applicationJdQuery.refetch()}>重试</Button>} />
              ) : applicationJdQuery.data?.current ? (
                <Paragraph ellipsis={{ rows: 3 }} className={styles.overviewText}>
                  {applicationJdQuery.data.current.jd_text}
                </Paragraph>
              ) : <Text type="secondary">尚未确认岗位描述</Text>}
            </div>
            <div className={styles.overviewBlock}>
              <Text strong>备注</Text>
              <Paragraph type="secondary" className={styles.overviewText}>
                {application.notes || '暂无补充备注'}
              </Paragraph>
            </div>
          </section>

          {nextStepSuggestions && onSetDisposition && onNextStepNavigate && (
            <NextStepSuggestions
              applicationId={application.id}
              suggestions={nextStepSuggestions}
              sessionState={nextStepSessionState}
              onSetDisposition={onSetDisposition}
              onNavigate={onNextStepNavigate}
              isNavigationAvailable={isNavigationAvailable}
              onNavigateReadonly={onNextStepReadonlyNavigate}
              isReadonlyNavigationAvailable={isReadonlyNavigationAvailable}
            />
          )}
        </div>

        <div
          id="application-preparation-panel"
          role="tabpanel"
          aria-labelledby="application-preparation-tab"
          tabIndex={0}
          hidden={activeTab !== 'preparation'}
          className={styles.tabPanel}
        >
          <section className={styles.preparationIntro} aria-labelledby="application-preparation-heading">
            <Title id="application-preparation-heading" level={4} className={styles.workspaceSectionTitle}>准备</Title>
            <Text type="secondary">按下一步任务整理岗位判断、材料、沟通与复盘入口。</Text>
            {externalTaskBlocked ? (
              <Alert
                style={{ marginTop: 12 }}
                type="warning"
                showIcon
                message="有一项助手操作正在等待处理"
                description="请先在 Haru / Pilot 中完成确认；当前投递任务不会替换或暴露其他操作的身份。"
              />
            ) : null}
            <div className={styles.taskList}>
              {applicationTaskResolution.tasks.length > 0 ? applicationTaskResolution.tasks.map((task) => {
                const copy = TASK_COPY[task.taskId] ?? { title: '当前任务', description: '当前任务状态已更新。', action: '查看任务' };
                const unavailable = externalTaskBlocked || !task.executable || task.availability === 'loading' || task.availability === 'blocked' || task.availability === 'unavailable';
                return (
                  <div
                    className={styles.taskCard}
                    key={`${task.taskId}:${task.ref.eventId ?? task.ref.applicationId}`}
                    data-task-id={task.taskId}
                    data-primary={task.primary ? 'true' : undefined}
                  >
                    <div>
                      <Text strong>{copy.title}</Text>
                      <Paragraph type="secondary">{copy.description}</Paragraph>
                      <Text type="secondary">
                        {task.availability === 'loading' ? '正在读取状态'
                          : task.availability === 'unavailable' ? '当前资料暂不可用'
                            : task.availability === 'blocked' ? '还缺少必要资料'
                               : task.availability === 'waiting_confirmation' ? '等待确认'
                                : task.reason === 'result_unknown' ? '结果待确认' : ''}
                      </Text>
                    </div>
                    <Button
                      size="small"
                      type="default"
                      disabled={unavailable}
                      onClick={() => launchResolvedTask(task)}
                    >
                      {copy.action}
                    </Button>
                  </div>
                );
              }) : (
                <Text type="secondary">暂无可执行任务，必要资料加载完成后会显示在这里。</Text>
              )}
            </div>
            {applicationTaskResolution.hasLoading && <div role="status">任务状态正在读取</div>}
            {applicationTaskResolution.hasUnavailable && !applicationTaskResolution.hasLoading && (
              <div role="alert">部分任务暂不可用，请先检查相关资料</div>
            )}
          </section>

        <section className={styles.workspaceSection} aria-labelledby="application-materials-heading">
          <Title id="application-materials-heading" level={4} className={styles.workspaceSectionTitle}>岗位与材料</Title>
        <div style={{ border: '1px solid #e2e8f0', borderRadius: 10, padding: 14, marginBottom: 16 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
            <Text strong>{'\u6295\u9012\u5c97\u4f4d\u8d44\u6599'}</Text>
            <Space>
              <Button size="small" onClick={() => { setJdHistoryOpen(true); setSelectedJdVersion(null); }}>{'\u67e5\u770b\u5386\u53f2'}</Button>
              <Button
                size="small"
                disabled={applicationJdQuery.isLoading || applicationJdQuery.isError}
                onClick={startJdEditor}
              >
                {applicationJdQuery.isLoading
                  ? '读取中'
                  : applicationJdQuery.isError
                    ? '暂不可编辑'
                    : applicationJdQuery.data?.current
                      ? '\u66f4\u65b0 JD'
                      : '\u6dfb\u52a0 JD'}
              </Button>
            </Space>
          </div>
          {applicationJdQuery.isLoading ? <Spin size="small" /> : applicationJdQuery.isError ? (
            <Alert type="warning" showIcon message="岗位资料暂时无法读取" action={<Button size="small" onClick={() => void applicationJdQuery.refetch()}>重试</Button>} />
          ) : applicationJdQuery.data === undefined ? (
            <Text type="secondary">岗位资料状态尚未加载</Text>
          ) : applicationJdQuery.data?.current ? (
            <>
            <Paragraph ellipsis={{ rows: 3 }} style={{ margin: '10px 0 0', whiteSpace: 'pre-wrap' }}>
              {applicationJdQuery.data.current.jd_text}
            </Paragraph>
            <Space size={8} style={{ marginTop: 8 }}>
              <Text type="secondary">{'\u6765\u6e90\uff1a'}{applicationJdQuery.data.current.source_url}</Text>
              <Button
                size="small"
                onClick={() => {
                  void navigator.clipboard?.writeText(applicationJdQuery.data!.current!.source_url!);
                }}
              >
                {'\u590d\u5236\u6765\u6e90'}
              </Button>
            </Space>
            </>
          ) : <Text type="secondary">{'\u5c1a\u672a\u786e\u8ba4\u5c97\u4f4d\u63cf\u8ff0'}</Text>}
        </div>
        </section>

        <Divider />
        <section className={styles.workspaceSection} aria-labelledby="application-schedule-heading">
        <Title id="application-schedule-heading" level={4} className={styles.workspaceSectionTitle}>日程与沟通</Title>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
          <Title level={5} style={{ margin: 0 }}>
            <CalendarOutlined /> 日程
          </Title>
          <Button size="small" icon={<PlusOutlined />} onClick={() => setEventFormOpen(true)}>
            安排日程
          </Button>
        </div>
        {eventsQuery.isLoading ? (
          <div style={{ textAlign: 'center', padding: 16 }}>
            <Spin />
          </div>
        ) : eventsQuery.isError ? (
          <Alert style={{ marginBottom: 16 }} type="warning" showIcon message="日程暂时无法读取" action={<Button size="small" onClick={() => void eventsQuery.refetch()}>重试</Button>} />
        ) : eventsQuery.data !== undefined && !eventRowsAreObjects ? (
          <Alert style={{ marginBottom: 16 }} type="warning" showIcon message="日程数据格式异常，请重试" action={<Button size="small" onClick={() => void eventsQuery.refetch()}>重试</Button>} />
          ) : eventsQuery.data === undefined ? (
          <Text type="secondary">日程状态尚未加载</Text>
        ) : allEvents.length > 0 ? (
          <Space direction="vertical" style={{ width: '100%', marginBottom: 16 }}>
            {allEvents.map((event) => {
              const notesReady = !notesQuery.isLoading && !notesQuery.isError && noteRowsAreObjects;
              const linkedNote = notesReady ? noteRecords.find((note) => note.application_event_id === event.id) : undefined;
              const projected = taskSnapshot.events.status === 'ready'
                ? taskSnapshot.events.value.find((item) => item.eventId === event.id)
                : undefined;
              const terminalEvent = projected?.lifecycle === 'cancelled';
              const unavailableEvent = !projected
                || projected.lifecycle === 'unknown'
                || projected.bucket === 'unavailable';
              const needsStatusUpdate = projected?.bucket === 'needs_status_update';
              const completedEvent = projected?.lifecycle === 'completed';
              const preparationTask = applicationTaskResolution.tasks.find(
                (task) => task.taskId === 'application.interview_prepare' && task.ref.eventId === event.id,
              );
              const reviewTask = applicationTaskResolution.tasks.find(
                (task) => task.taskId === 'application.interview_review' && task.ref.eventId === event.id,
              );
              return (
              <div key={event.id} style={{ border: '1px solid #e2e8f0', borderRadius: 8, padding: 12 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12 }}>
                  <Text strong>{EVENT_TYPE_LABELS[event.event_type]}</Text>
                  <Text type="secondary">{dayjs(event.scheduled_at).format('YYYY-MM-DD HH:mm')}</Text>
                </div>
                <div style={{ color: '#64748b', fontSize: 13, marginTop: 4 }}>
                  时长 {event.duration_minutes} 分钟{event.location ? ` · ${event.location}` : ''}{terminalEvent ? ` · ${eventStatusLabel(event.status)}` : ''}
                </div>
                {event.event_type === 'interview' && terminalEvent ? (
                  <Text type="secondary">该面试已结束或取消，暂不提供准备与复盘操作。</Text>
                ) : event.event_type === 'interview' && unavailableEvent ? (
                  <Text type="secondary">该面试状态暂不可用，请先刷新日程后再操作。</Text>
                ) : event.event_type === 'interview' && needsStatusUpdate ? (
                  <Text type="secondary">该面试状态需要先更新，暂不提供准备与复盘操作。</Text>
                ) : event.event_type === 'interview' && !notesReady ? (
                  <Text type="secondary">面试复盘暂不可用，请先完成读取或重试。</Text>
                ) : event.event_type === 'interview' && (
                  <Space size={4}>
                    {completedEvent && <Button
                      size="small"
                      type="link"
                      disabled={externalTaskBlocked || !reviewTask?.executable}
                      onClick={() => {
                        if (externalTaskBlocked || !reviewTask?.executable) return;
                        const result = launchTask({
                          ref: reviewTask.ref,
                          source: 'interview_event_card',
                          focus: 'current',
                        });
                        if (taskLaunchAccepted(result)) setEditingNote(linkedNote ?? null);
                      }}
                    >
                      {linkedNote ? '查看复盘' : '记录复盘'}
                    </Button>}
                    {completedEvent && linkedNote && (
                      <Button size="small" type="link" onClick={() => openKnowledgeCapture(linkedNote)}>
                        保存为复盘沉淀
                      </Button>
                    )}
                    {!completedEvent && preparationTask?.executable && (
                      <Button
                        size="small"
                        type="link"
                        disabled={externalTaskBlocked}
                        onClick={() => launchTask({
                          ref: {
                            taskId: 'application.interview_prepare',
                            applicationId: application.id,
                            eventId: event.id,
                          },
                          source: 'interview_event_card',
                          focus: 'current',
                        })}
                      >
                        面试准备建议
                      </Button>
                    )}
                  </Space>
                )}
              </div>
              );
            })}
          </Space>
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无笔试、面试或测评日程" style={{ marginBottom: 16 }} />
        )}
        </section>
        <section className={styles.workspaceSection} aria-labelledby="application-interview-heading">
        <Title id="application-interview-heading" level={4} className={styles.workspaceSectionTitle}>面试</Title>
        <Title level={5} style={{ marginTop: 8 }}>
          面试复盘
        </Title>

        <Button
          icon={<PlusOutlined />}
          style={{ marginBottom: 16 }}
          onClick={() => {
            if (!interviewReviewDataReady) return;
            setPilotReviewChoices(canonicalInterviewChoices.review);
            setPilotReviewChooserOpen(true);
          }}
          disabled={!interviewReviewDataReady}
        >
          选择面试并开始复盘
        </Button>

        {notesQuery.isLoading ? (
          <div style={{ textAlign: 'center', padding: 24 }}>
            <Spin />
          </div>
        ) : notesQuery.isError ? (
          <Alert type="warning" showIcon message="面试复盘暂时无法读取" action={<Button size="small" onClick={() => void notesQuery.refetch()}>重试</Button>} />
        ) : notesQuery.data !== undefined && !noteRowsAreObjects ? (
          <Alert type="warning" showIcon message="面试复盘数据格式异常，请重试" action={<Button size="small" onClick={() => void notesQuery.refetch()}>重试</Button>} />
        ) : notesQuery.data === undefined ? (
          <Text type="secondary">面试复盘状态尚未加载</Text>
        ) : noteRecords.length > 0 ? (
          <Timeline
            items={noteRecords.map((n) => ({
              color: 'green',
              children: (
                <div
                  key={n.id}
                  style={{ paddingBottom: 8, borderBottom: '1px solid #f0f0f0' }}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                    <Text strong>
                      {n.round || '未标注轮次'} · {n.date} · 心情 {n.mood || '—'}
                    </Text>
                    <Space size={4}>
                      <Button
                        type="text"
                        size="small"
                        onClick={() => launchNoteReview(n)}
                      >
                        编辑
                      </Button>
                      <Button
                        type="text"
                        size="small"
                        onClick={() => launchNoteReview(n)}
                      >
                        复盘建议
                      </Button>
                      <Button type="text" size="small" onClick={() => openKnowledgeCapture(n)}>
                        保存为复盘沉淀
                      </Button>
                      <Popconfirm
                        title="删除这条复盘？"
                        onConfirm={() => removeNoteMut.mutate(n.id)}
                        okText="删除"
                        cancelText="取消"
                      >
                        <Button type="text" size="small" danger>
                          删除
                        </Button>
                      </Popconfirm>
                    </Space>
                  </div>
                  {n.questions && (
                    <div style={{ marginTop: 4 }}>
                      <Text type="secondary">问题：</Text>
                      {n.questions}
                    </div>
                  )}
                  {n.self_reflection && (
                    <div>
                      <Text type="secondary">反思：</Text>
                      {n.self_reflection}
                    </div>
                  )}
                  {n.difficulty_points && (
                    <div>
                      <Text type="secondary">难点：</Text>
                      {n.difficulty_points}
                    </div>
                  )}
                </div>
              ),
            }))}
          />
        ) : (
          <Empty description="还没有面试复盘" />
        )}
        </section>
        <section className={styles.workspaceSection} aria-labelledby="application-result-heading">
          <Title id="application-result-heading" level={4} className={styles.workspaceSectionTitle}>结果</Title>
          <Text type="secondary">
            {application.status === 'offer'
              ? '已进入 Offer 阶段，可通过顶部主操作查看事实、截止时间和待确认信息。'
              : application.status === 'closed'
                ? '该投递已结束，结果与经验记录保留在投递事实中。'
                : '尚未进入结果阶段，后续状态会继续在这里汇总。'}
          </Text>
          <div style={{ marginTop: 12 }}>
            <Button
              size="small"
              onClick={() => launchTask({
                ref: { taskId: 'application.record_outcome', applicationId: application.id },
                source: 'application_task_card',
                focus: 'current',
              })}
            >
              投递事实与结果
            </Button>
          </div>
        </section>
        </div>

        <div
          id="application-progress-panel"
          role="tabpanel"
          aria-labelledby="application-progress-tab"
          tabIndex={0}
          hidden={activeTab !== 'progress'}
          className={styles.tabPanel}
        >
          <section className={`${styles.workspaceSection} ${styles.progressSection}`} aria-labelledby="application-progress-heading">
            <Title id="application-progress-heading" level={4} className={styles.workspaceSectionTitle}>进展</Title>
            <Text type="secondary" className={styles.readOnlyNotice}>
              进展时间线只汇总现有投递、事件、面试复盘与归属 Offer，不会修改投递状态。
            </Text>
            {eventsQuery.isError ? (
              <Alert type="warning" showIcon message="部分日程进展暂时无法读取" action={<Button size="small" onClick={() => void eventsQuery.refetch()}>重试</Button>} />
            ) : null}
            {notesQuery.isError ? (
              <Alert type="warning" showIcon message="部分复盘进展暂时无法读取" action={<Button size="small" onClick={() => void notesQuery.refetch()}>重试</Button>} />
            ) : null}
            {offersError ? (
              <Alert type="warning" showIcon message="部分 Offer 进展暂时无法读取" action={onRetryOffers ? <Button size="small" onClick={onRetryOffers}>重试</Button> : undefined} />
            ) : null}
            {progressItems.length > 0 ? (
              <div className={styles.progressTimeline} role="list" aria-label="进展时间线">
                {progressItems.map((item) => (
                  <article key={item.id} className={styles.progressItem} role="listitem">
                    <span className={styles.progressMarker} aria-hidden="true" />
                    <div className={styles.progressBody}>
                      <div className={styles.progressHeader}>
                        <Text strong>{item.title}</Text>
                        <Text type="secondary">{formatWorkspaceDate(item.timestamp)}</Text>
                      </div>
                      {item.detail && <Paragraph type="secondary" className={styles.progressDetail}>{item.detail}</Paragraph>}
                    </div>
                  </article>
                ))}
              </div>
            ) : (
              <Empty description="暂无进展记录" />
            )}
          </section>
        </div>
      </section>

      {activeTask ? (
        <CoreTaskSurfaceHost
          controller={effectiveTaskController}
          renderOwner={renderTaskOwner}
          heading="当前任务"
          revealOnOpen
          closeGuard={(closingActive) => closingActive.generation === effectiveTaskController.getState().active?.generation
            ? localTaskSurfaceGuardRef.current
            : { pending: true, unsaved: true }}
        />
      ) : null}

      <ScheduleEventForm
        open={eventFormOpen}
        applications={[application]}
        initialApplication={application}
        onClose={() => setEventFormOpen(false)}
      />

      {knowledgeCaptureOpen && editingNote ? (
        <InterviewKnowledgeCaptureDrawer
          open
          note={editingNote}
          draft={interviewKnowledgeCaptureDrafts?.[editingNote.id] ?? createInterviewKnowledgeCaptureDraft()}
          onDraftChange={(draft) => onInterviewKnowledgeCaptureDraftChange?.(editingNote.id, draft)}
          onClose={() => {
            setKnowledgeCaptureOpen(false);
            setEditingNote(null);
          }}
        />
      ) : null}

    </>
  );
}
