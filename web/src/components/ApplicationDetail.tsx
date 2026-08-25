import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Typography,
  Tag,
  Timeline,
  Button,
  Divider,
  Form,
  Input,
  Select,
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
import OpportunityFitReviewDrawer from './OpportunityFitReviewDrawer';
import ApplicationOutcomeDrawer from './ApplicationOutcomeDrawer';
import {
  createOpportunityFitV2Draft,
  type OpportunityFitReview,
  type OpportunityFitV2Draft,
} from '@/types/opportunityFitReview';
import { SourceStateTag } from './ui/SourceStateTag';
import { createPilotAttachmentDragBinding } from './PilotAttachmentHandle';
import { consumeMaterialKitHandoff } from '@/features/pilot/materialKitHandoff';
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

const { Title, Paragraph, Text } = Typography;

const MOOD_OPTIONS = [
  { value: 'good', label: '好' },
  { value: 'normal', label: '一般' },
  { value: 'bad', label: '差' },
];

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

const TERMINAL_EVENT_STATUSES = new Set(['cancelled', 'deleted', 'soft_deleted']);

function isTerminalScheduleEvent(event: ScheduleEvent): boolean {
  return TERMINAL_EVENT_STATUSES.has(event.status);
}

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

interface ApplicationDetailProps {
  application: Application | null;
  open: boolean;
  onClose: () => void;
  onOpenOffers?: () => void;
  offers?: Offer[];
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
  interviewReviewProposalAttempts?: Record<number, InterviewReviewProposalAttemptState>;
  onInterviewReviewProposalAttemptChange?: (
    noteID: number,
    state: InterviewReviewProposalAttemptState | null,
  ) => void;
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
  nextStepSuggestions?: NextStepSuggestionsModel;
  nextStepSessionState?: SuggestionSessionState | null;
  onSetDisposition?: (applicationId: number, suggestionId: string, state: SuggestionSessionState | null) => void;
  onNextStepNavigate?: (destination: NextStepDestination | ReadonlyDestination) => void;
  isNavigationAvailable?: (destination: NextStepDestination | ReadonlyDestination) => boolean;
  onNextStepReadonlyNavigate?: (destination: ReadonlyDestination) => void;
  isReadonlyNavigationAvailable?: (destination: ReadonlyDestination) => boolean;
  applicationJdDraft?: ApplicationJdDraft;
  onApplicationJdDraftChange?: (applicationId: number, patch: Partial<ApplicationJdDraft> | null) => void;
  opportunityFitDraft?: OpportunityFitV2Draft;
  onOpportunityFitDraftChange?: (applicationId: number, patch: Partial<OpportunityFitV2Draft> | null) => void;
}

export default function ApplicationDetail({ application, open, onClose, onOpenOffers, offers = [], offersError = false, onRetryOffers, onMockInterview, onAskPilot, onOpenPilotOpportunityFit, pilotInterviewReviewApplicationId, onPilotInterviewReviewFocusConsumed, pilotInterviewPreparationApplicationId, pilotInterviewPreparationEventId, onPilotInterviewPreparationFocusConsumed, onAttachToPilot, interviewReviewProposalAttempts, onInterviewReviewProposalAttemptChange, onInterviewNoteChanged, interviewKnowledgeCaptureDrafts, onInterviewKnowledgeCaptureDraftChange, onInterviewKnowledgeCaptureNoteChanged, resumes = [], interviewPreparationAttempts, onInterviewPreparationAttemptChange, interviewPreparationDrafts, onInterviewPreparationDraftChange, interviewPreparationKnowledgeOptions = [], nextStepSuggestions, nextStepSessionState = null, onSetDisposition, onNextStepNavigate, isNavigationAvailable, onNextStepReadonlyNavigate, isReadonlyNavigationAvailable, applicationJdDraft, onApplicationJdDraftChange, opportunityFitDraft, onOpportunityFitDraftChange }: ApplicationDetailProps) {
  const queryClient = useQueryClient();
  const [form] = Form.useForm();
  const [eventFormOpen, setEventFormOpen] = useState(false);
  const [materialKitOpen, setMaterialKitOpen] = useState(false);
  const [opportunityFitOpen, setOpportunityFitOpen] = useState(false);
  const [applicationOutcomeOpen, setApplicationOutcomeOpen] = useState(false);
  const [materialKitPrefill, setMaterialKitPrefill] = useState<{
    resumeID?: number;
    jdSnapshot?: string;
    jdVersionID?: number;
  }>({});
  const [materialKitApplicationId, setMaterialKitApplicationId] = useState<number | null>(null);
  const [editingNote, setEditingNote] = useState<InterviewNote | null>(null);
  const [reviewFormOpen, setReviewFormOpen] = useState(false);
  const [reviewProposalOpen, setReviewProposalOpen] = useState(false);
  const [knowledgeCaptureOpen, setKnowledgeCaptureOpen] = useState(false);
  const [reviewEventID, setReviewEventID] = useState<number | null>(null);
  const [preparationOpen, setPreparationOpen] = useState(false);
  const [preparationEventID, setPreparationEventID] = useState<number | null>(null);
  const [pilotPreparationChoices, setPilotPreparationChoices] = useState<ScheduleEvent[]>([]);
  const [jdEditorOpen, setJdEditorOpen] = useState(false);
  const [jdHistoryOpen, setJdHistoryOpen] = useState(false);
  const [selectedJdVersion, setSelectedJdVersion] = useState<number | null>(null);
  const [activeTab, setActiveTab] = useState<ApplicationDetailTab>('overview');
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);

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
    setMaterialKitOpen(false);
    setMaterialKitApplicationId(null);
    setApplicationOutcomeOpen(false);
    setActiveTab('overview');
  }, [application?.id, open]);

  useEffect(() => {
    if (!application || !open) return;
    const handoff = consumeMaterialKitHandoff(application.id);
    if (!handoff || !handoff.jdVersionId) return;
    setMaterialKitPrefill({
      resumeID: handoff.resumeId,
      jdSnapshot: handoff.jdText,
      jdVersionID: handoff.jdVersionId,
    });
    setMaterialKitApplicationId(application.id);
    setMaterialKitOpen(true);
  }, [application?.id, open]);

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

  const activeEvents = useMemo(
    () => (eventsQuery.data ?? []).filter((event) => !isTerminalScheduleEvent(event)),
    [eventsQuery.data],
  );
  const interviewStageDataReady = application?.status !== 'interview'
    || (
      !eventsQuery.isLoading
      && !eventsQuery.isError
      && Array.isArray(eventsQuery.data)
      && !notesQuery.isLoading
      && !notesQuery.isError
      && Array.isArray(notesQuery.data)
    );

  useEffect(() => {
    if (!application || !open || pilotInterviewReviewApplicationId !== application.id || !interviewStageDataReady) return;
    const completedInterview = activeEvents
      .filter((event) => event.event_type === 'interview' && dayjs(event.scheduled_at).isBefore(dayjs()))
      .sort((left, right) => dayjs(right.scheduled_at).valueOf() - dayjs(left.scheduled_at).valueOf())[0];
    const linkedNote = completedInterview
      ? notesQuery.data?.find((note) => note.application_event_id === completedInterview.id)
      : undefined;
    setEditingNote(linkedNote ?? null);
    setReviewEventID(linkedNote?.application_event_id ?? completedInterview?.id ?? null);
    setPreparationOpen(false);
    setPreparationEventID(null);
    setReviewFormOpen(!linkedNote);
    setReviewProposalOpen(Boolean(linkedNote));
    onPilotInterviewReviewFocusConsumed?.();
  }, [activeEvents, application, interviewStageDataReady, notesQuery.data, open, onPilotInterviewReviewFocusConsumed, pilotInterviewReviewApplicationId]);

  useEffect(() => {
    if (!application || !open || pilotInterviewPreparationApplicationId !== application.id || eventsQuery.isLoading || eventsQuery.isError || !eventsQuery.data) return;
    const interviewEvents = activeEvents.filter((event) => event.event_type === 'interview');
    if (interviewEvents.length === 0) return;
    const requestedEvent = pilotInterviewPreparationEventId == null
      ? null
      : interviewEvents.find((event) => event.id === pilotInterviewPreparationEventId) ?? null;
    if (requestedEvent) {
      setPreparationEventID(requestedEvent.id);
      setPreparationOpen(true);
      onPilotInterviewPreparationFocusConsumed?.();
      return;
    }
    if (interviewEvents.length === 1) {
      setPreparationEventID(interviewEvents[0].id);
      setPreparationOpen(true);
    } else {
      setPilotPreparationChoices(interviewEvents);
    }
    onPilotInterviewPreparationFocusConsumed?.();
  }, [activeEvents, application, eventsQuery.data, eventsQuery.isError, eventsQuery.isLoading, open, pilotInterviewPreparationEventId, onPilotInterviewPreparationFocusConsumed, pilotInterviewPreparationApplicationId]);

  const invalidateNotes = () => {
    if (application) queryClient.invalidateQueries({ queryKey: ['notes', application.id] });
    queryClient.invalidateQueries({ queryKey: ['notes', 'all'] });
  };

  const addNote = useMutation({
    mutationFn: (input: CreateNoteInput) => createNote(application!.id, input),
    onSuccess: () => {
      message.success('已添加面试复盘');
      form.resetFields();
      invalidateNotes();
    },
    onError: () => message.error('添加失败'),
  });

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
      setReviewFormOpen(false);
      setReviewEventID(null);
      invalidateNotes();
    },
    onError: () => message.error('更新失败'),
  });

  const createEventNoteMut = useMutation({
    mutationFn: (input: CreateNoteInput) => createNote(application!.id, input),
    onSuccess: () => {
      message.success('已保存面试复盘');
      setReviewFormOpen(false);
      setReviewEventID(null);
      invalidateNotes();
    },
    onError: () => message.error('保存复盘失败'),
  });

  const closeDetail = () => {
    setEventFormOpen(false);
    setMaterialKitOpen(false);
    setMaterialKitApplicationId(null);
    setOpportunityFitOpen(false);
    setMaterialKitPrefill({});
    setEditingNote(null);
    setReviewFormOpen(false);
    setReviewProposalOpen(false);
    setKnowledgeCaptureOpen(false);
    setReviewEventID(null);
    setPilotPreparationChoices([]);
    setPreparationOpen(false);
    setPreparationEventID(null);
    onClose();
  };

  const openKnowledgeCapture = (note: InterviewNote) => {
    const existing = interviewKnowledgeCaptureDrafts?.[note.id] ?? createInterviewKnowledgeCaptureDraft();
    onInterviewKnowledgeCaptureDraftChange?.(note.id, existing);
    setEditingNote(note);
    setKnowledgeCaptureOpen(true);
  };

  if (!application || !open) return null;

  if (eventFormOpen) {
    return (
      <ScheduleEventForm
        open={eventFormOpen}
        applications={[application]}
        initialApplication={application}
        onClose={() => setEventFormOpen(false)}
      />
    );
  }

  if (reviewFormOpen) {
    return (
      <ReviewFormDrawer
        open={reviewFormOpen}
        applications={[application]}
        initialApplication={application}
        note={editingNote}
        initialEventID={reviewEventID}
         saving={updateNoteMut.isPending || createEventNoteMut.isPending}
         onSubmit={(input) => {
           if (editingNote) {
             updateNoteMut.mutate({ id: editingNote.id, input });
           } else {
             createEventNoteMut.mutate(input);
           }
         }}
        onClose={() => {
          setReviewFormOpen(false);
          setEditingNote(null);
          setReviewEventID(null);
        }}
      />
    );
  }

  if (materialKitOpen && materialKitApplicationId === application.id) {
    return (
      <MaterialKitDrawer
        application={application}
        open={materialKitOpen}
        onClose={() => {
          setMaterialKitOpen(false);
          setMaterialKitApplicationId(null);
          setMaterialKitPrefill({});
        }}
        initialResumeID={materialKitPrefill.resumeID}
        initialJdSnapshot={materialKitPrefill.jdSnapshot}
        initialJdVersionID={materialKitPrefill.jdSnapshot && !materialKitPrefill.jdVersionID
          ? undefined
          : materialKitPrefill.jdVersionID ?? applicationJdQuery.data?.current?.id}
      />
    );
  }

  if (applicationOutcomeOpen) {
    return (
      <ApplicationOutcomeDrawer
        application={application}
        open
        onClose={() => setApplicationOutcomeOpen(false)}
        resumes={resumes}
        currentJd={applicationJdQuery.data?.current ?? null}
        events={eventsQuery.data ?? []}
        onAskPilot={onAskPilot}
      />
    );
  }

  if (reviewProposalOpen && editingNote) {
    return (
      <InterviewReviewProposalDrawer
        open={reviewProposalOpen}
        note={editingNote}
        eventID={editingNote.application_event_id}
        attemptState={interviewReviewProposalAttempts?.[editingNote.id]}
        onAttemptStateChange={(state) => onInterviewReviewProposalAttemptChange?.(editingNote.id, state)}
        onClose={() => {
          setReviewProposalOpen(false);
          setEditingNote(null);
        }}
      />
    );
  }

  if (preparationOpen && preparationEventID !== null) {
    const preparationKey = `${application.id}:${preparationEventID}`;
    return (
      <InterviewPreparationProposalDrawer
        key={`${application.id}:${preparationEventID}`}
        open
        context={{
          applicationId: application.id,
          eventId: preparationEventID,
          resumeId: 0,
          jdText: applicationJdQuery.data?.current?.jd_text ?? '',
          jdVersionId: applicationJdQuery.data?.current?.id ?? null,
          knowledgeSelections: [],
          userAssertions: [],
        }}
        resumeOptions={resumes}
        knowledgeOptions={interviewPreparationKnowledgeOptions}
        attemptState={interviewPreparationAttempts?.[preparationKey]}
        draft={interviewPreparationDrafts?.[preparationKey]}
        onAttemptStateChange={(state) => onInterviewPreparationAttemptChange?.(preparationKey, state)}
        onDraftChange={(draft) => onInterviewPreparationDraftChange?.(preparationKey, draft)}
        onClose={() => {
          setPreparationOpen(false);
          setPreparationEventID(null);
        }}
      />
    );
  }

  if (knowledgeCaptureOpen && editingNote) {
    return (
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
    );
  }

  if (opportunityFitOpen) {
    return (
      <OpportunityFitReviewDrawer
        application={application}
        open={opportunityFitOpen}
        currentJdText={applicationJdQuery.data?.current?.jd_text ?? ''}
        jdVersionId={applicationJdQuery.data?.current?.id ?? null}
        draft={opportunityFitDraft ?? createOpportunityFitV2Draft(application.id)}
        onDraftChange={(patch) => onOpportunityFitDraftChange?.(application.id, patch)}
        onApplicationMissing={onClose}
        onClose={() => setOpportunityFitOpen(false)}
        onPrepareMaterials={(reviewOrResumeId: OpportunityFitReview | number, jdText: string, jdVersionId?: number) => {
          if (!jdVersionId) return;
          const resumeID = typeof reviewOrResumeId === 'number'
            ? reviewOrResumeId
            : reviewOrResumeId.source.resume.id;
          setMaterialKitPrefill({ resumeID, jdSnapshot: jdText, jdVersionID: jdVersionId });
          setMaterialKitApplicationId(application.id);
          setOpportunityFitOpen(false);
          setMaterialKitOpen(true);
        }}
      />
    );
  }

  const applicationDragBinding = onAttachToPilot
    ? createPilotAttachmentDragBinding({
        kind: 'application',
        id: String(application.id),
        label: `${application.company_name} · ${application.position_name}`,
      })
    : undefined;

  const interviewEvents = activeEvents.filter((event) => event.event_type === 'interview');
  const completedInterview = interviewEvents
    .filter((event) => dayjs(event.scheduled_at).isBefore(dayjs()))
    .sort((left, right) => dayjs(right.scheduled_at).valueOf() - dayjs(left.scheduled_at).valueOf())[0];
  const upcomingEvent = activeEvents
    .filter((event) => dayjs(event.scheduled_at).isAfter(dayjs()))
    .sort((left, right) => dayjs(left.scheduled_at).valueOf() - dayjs(right.scheduled_at).valueOf())[0];
  const stage = getApplicationWorkspaceStage(application.status, {
    hasCompletedInterview: Boolean(completedInterview),
    hasInterviewReview: Boolean(completedInterview && notesQuery.data?.some((note) => note.application_event_id === completedInterview.id)),
  });
  const stageDataBlocked = application.status === 'interview' && !interviewStageDataReady;
  const stageDataHasError = eventsQuery.isError || notesQuery.isError;
  const stageLabel = stageDataBlocked
    ? stageDataHasError ? '面试进展暂不可读' : '面试进展读取中'
    : stage.label;
  const stagePrimaryActionLabel = stageDataBlocked
    ? stageDataHasError ? '重试日程和复盘' : '等待面试进展加载'
    : stage.primaryActionLabel;

  const retryStageData = () => {
    if (eventsQuery.isError) void eventsQuery.refetch();
    if (notesQuery.isError) void notesQuery.refetch();
  };

  const openMaterials = () => {
    const currentJd = applicationJdQuery.data?.current;
    setMaterialKitPrefill(currentJd ? { jdSnapshot: currentJd.jd_text, jdVersionID: currentJd.id } : {});
    setMaterialKitApplicationId(application.id);
    setMaterialKitOpen(true);
  };

  const runStageAction = () => {
    if (stageDataBlocked) return;
    switch (stage.action) {
      case 'materials':
        openMaterials();
        break;
      case 'followup':
      case 'written-test':
        setEventFormOpen(true);
        break;
      case 'interview-prepare': {
        const nextInterview = interviewEvents
          .filter((event) => dayjs(event.scheduled_at).isAfter(dayjs()))
          .sort((left, right) => dayjs(left.scheduled_at).valueOf() - dayjs(right.scheduled_at).valueOf())[0] ?? interviewEvents[0];
        if (nextInterview) {
          setPreparationEventID(nextInterview.id);
          setPreparationOpen(true);
        } else {
          setEventFormOpen(true);
        }
        break;
      }
      case 'interview-review': {
        const linkedNote = completedInterview
          ? notesQuery.data?.find((note) => note.application_event_id === completedInterview.id)
          : undefined;
        if (linkedNote) {
          setEditingNote(linkedNote);
          setReviewEventID(linkedNote.application_event_id ?? completedInterview?.id ?? null);
          setReviewFormOpen(false);
          setReviewProposalOpen(true);
          break;
        }
        setReviewEventID(completedInterview?.id ?? null);
        setEditingNote(null);
        setReviewFormOpen(true);
        setReviewProposalOpen(false);
        break;
      }
      case 'offer':
        if (onOpenOffers) {
          onOpenOffers();
          break;
        }
        setApplicationOutcomeOpen(true);
        break;
      case 'outcome':
        setApplicationOutcomeOpen(true);
        break;
    }
  };

  const moreActionItems = [
    ...(onAskPilot ? [{ key: 'haru', label: '让 Haru 帮我', onClick: () => onAskPilot(application, { type: 'application_jd_save' }) }] : []),
    ...(onOpenPilotOpportunityFit ? [{ key: 'fit', label: '评估岗位匹配', onClick: () => onOpenPilotOpportunityFit(application) }] : []),
    { key: 'materials', label: '打开投递材料', onClick: openMaterials },
    { key: 'decision', label: '岗位决策漏斗', onClick: () => setOpportunityFitOpen(true) },
    { key: 'facts', label: '投递事实与结果', onClick: () => setApplicationOutcomeOpen(true) },
    ...(onMockInterview ? [{ key: 'mock', label: '开始模拟面试', onClick: () => onMockInterview(application) }] : []),
  ];

  const linkedOffers = offers.filter((offer) => offer.application_id === application.id);
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
    ...(eventsQuery.data ?? []).map((event) => ({
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
    ...(notesQuery.data ?? []).map((note) => ({
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

  const openOpportunityFit = () => {
    setActiveTab('preparation');
    if (onOpenPilotOpportunityFit) {
      onOpenPilotOpportunityFit(application);
      return;
    }
    setOpportunityFitOpen(true);
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
        open={pilotPreparationChoices.length > 1}
        title="选择要准备的面试"
        footer={null}
        onCancel={() => setPilotPreparationChoices([])}
      >
        <Space direction="vertical" style={{ width: '100%' }}>
          {pilotPreparationChoices.map((event) => (
            <Button
              key={event.id}
              block
              onClick={() => {
                setPreparationEventID(event.id);
                setPreparationOpen(true);
                setPilotPreparationChoices([]);
              }}
            >
              {event.subtype || '面试'} · {event.scheduled_at}
            </Button>
          ))}
        </Space>
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
                disabled={stageDataBlocked && !stageDataHasError}
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
            <div className={styles.taskList}>
              <div className={styles.taskCard}>
                <div>
                  <Text strong>岗位匹配与风险</Text>
                  <Paragraph type="secondary">先确认是否值得继续，以及需要补充的事实。</Paragraph>
                </div>
                <Button size="small" onClick={openOpportunityFit}>开始判断</Button>
              </div>
              <div className={styles.taskCard}>
                <div>
                  <Text strong>投递准备</Text>
                  <Paragraph type="secondary">选择简历、查看调整建议并完成提交前检查。</Paragraph>
                </div>
                <Button size="small" onClick={openMaterials}>打开准备</Button>
              </div>
              <div className={styles.taskCard}>
                <div>
                  <Text strong>本次投递记录</Text>
                  <Paragraph type="secondary">冻结实际使用的简历、JD 与材料，并记录外部结果。</Paragraph>
                </div>
                <Button size="small" onClick={() => setApplicationOutcomeOpen(true)}>打开记录</Button>
              </div>
            </div>
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
        ) : eventsQuery.data && eventsQuery.data.length > 0 ? (
          <Space direction="vertical" style={{ width: '100%', marginBottom: 16 }}>
            {eventsQuery.data.map((event) => {
              const notesReady = !notesQuery.isLoading && !notesQuery.isError && Array.isArray(notesQuery.data);
              const linkedNote = notesReady ? notesQuery.data?.find((note) => note.application_event_id === event.id) : undefined;
              const terminalEvent = isTerminalScheduleEvent(event);
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
                ) : event.event_type === 'interview' && !notesReady ? (
                  <Text type="secondary">面试复盘暂不可用，请先完成读取或重试。</Text>
                ) : event.event_type === 'interview' && (
                  <Space size={4}>
                    <Button
                      size="small"
                      type="link"
                      onClick={() => {
                        setReviewEventID(event.id);
                        setEditingNote(linkedNote ?? null);
                        if (linkedNote) setReviewProposalOpen(true);
                        else setReviewFormOpen(true);
                      }}
                    >
                      {linkedNote ? '查看复盘' : '记录复盘'}
                    </Button>
                    {linkedNote && (
                      <Button size="small" type="link" onClick={() => openKnowledgeCapture(linkedNote)}>
                        保存为复盘沉淀
                      </Button>
                    )}
                    <Button
                      size="small"
                      type="link"
                      onClick={() => {
                        setPreparationEventID(event.id);
                        setPreparationOpen(true);
                      }}
                    >
                      面试准备建议
                    </Button>
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

        <Form
          form={form}
          layout="vertical"
          onFinish={(v) => addNote.mutate(v)}
          style={{ marginBottom: 16 }}
        >
          <div style={{ display: 'flex', gap: 8 }}>
            <Form.Item name="round" style={{ flex: 1 }} label="轮次">
              <Input placeholder="一面" />
            </Form.Item>
            <Form.Item name="date" style={{ flex: 1 }} label="日期">
              <Input placeholder="2026-07-01" />
            </Form.Item>
            <Form.Item name="mood" style={{ flex: 1 }} label="心情">
              <Select options={MOOD_OPTIONS} allowClear placeholder="选择" />
            </Form.Item>
          </div>
          <Form.Item name="questions" label="面试问题">
            <Input.TextArea rows={2} placeholder="被问到的问题…" />
          </Form.Item>
          <Form.Item name="self_reflection" label="自我反思">
            <Input.TextArea rows={2} placeholder="表现如何、哪里可以改…" />
          </Form.Item>
          <Form.Item name="difficulty_points" label="难点/薄弱点">
            <Input.TextArea rows={2} placeholder="哪些知识点没答好" />
          </Form.Item>
          <Button
            htmlType="submit"
            icon={<PlusOutlined />}
            loading={addNote.isPending}
          >
            添加复盘
          </Button>
        </Form>

        {notesQuery.isLoading ? (
          <div style={{ textAlign: 'center', padding: 24 }}>
            <Spin />
          </div>
        ) : notesQuery.isError ? (
          <Alert type="warning" showIcon message="面试复盘暂时无法读取" action={<Button size="small" onClick={() => void notesQuery.refetch()}>重试</Button>} />
        ) : notesQuery.data && notesQuery.data.length > 0 ? (
          <Timeline
            items={notesQuery.data.map((n) => ({
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
                        onClick={() => {
                          setEditingNote(n);
                          setReviewEventID(n.application_event_id ?? null);
                          setReviewFormOpen(true);
                        }}
                      >
                        编辑
                      </Button>
                      <Button
                        type="text"
                        size="small"
                        onClick={() => {
                          setEditingNote(n);
                          setReviewProposalOpen(true);
                        }}
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

    </>
  );
}
