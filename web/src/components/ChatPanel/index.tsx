import { useEffect, useReducer, useRef, useState, createElement } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Drawer, App as AntApp, Button } from 'antd';
import {
  CloseOutlined,
  RobotOutlined,
  AppstoreOutlined,
  PlusOutlined,
  ExpandAltOutlined,
  StopOutlined,
} from '@ant-design/icons';
import {
  getSettings,
  SETTINGS_QUERY_KEY,
  updateAutoApprove,
  listConversations,
  getConversation,
  deleteConversation,
  updateConversation,
  undoLastWrite,
  type ConfirmationInput,
} from '@/services/chat';
import { getOffer } from '@/services/offers';
import { ONBOARDING_QUERY_KEY } from '@/services/onboarding';
import type {
  ChatResponse,
  ChatStartRequest,
  ChatStreamEvent,
  Conversation,
  PendingAction,
  PilotPageContext,
} from '@/types/chat';
import type { Offer } from '@/types/offer';
import {
  buildTurns,
  collectEvidence,
  pendingActionForConversation,
  shouldApplyConversationRequest,
  isCurrentVisibleConversationRequest,
  shouldAbortActiveRequestOnReplacement,
  clearOwnedConfirmationLock,
  shouldConsumeConfirmationSettlement,
  hasConfirmationSettled,
  pendingComposerDisabledReason,
  reloadConversationTurns,
  resolveActivePendingAction,
  toolMeta,
  confirmationInputForRetry,
  confirmationErrorAllowsImmediateRetry,
  confirmationErrorRequiresSync,
  shouldRestoreConfirmationRetryFocus,
  type EvidenceItem,
  type EvidenceTarget,
} from './model';
import {
  filterConversationsByView,
  firstPendingConversationId,
} from './conversationList';
import {
  createPilotPageContextRemovalState,
  deriveActivePageContext,
  pageContextChips,
  pageContextKey,
  pilotPageContextRemovalReducer,
} from '@/lib/pilotPageContext';
import { pilotQuickQuestions } from '@/lib/pilotAttachments';
import { usePilotAttachments } from '@/features/pilot/PilotAttachmentContext';
import { type PilotAttachmentConversationKey } from '@/features/pilot/PilotAttachmentContext';
import { capabilitiesForMode, type Capability } from './capabilities';
import ThreadRail from './ThreadRail';
import MessageBubble from './MessageBubble';
import ProposalCard from './ProposalCard';
import ThinkingIndicator from './ThinkingIndicator';
import Composer from './Composer';
import ContextAttachmentRail from './ContextAttachmentRail';
import NativePilotAttachmentDropSurface from './NativePilotAttachmentDropSurface';
import ContextPanel from './ContextPanel';
import PilotContextDropTarget from '@/components/KanbanBoard/PilotContextDropTarget';
import styles from './ChatPanel.module.css';
import type { PilotMascotActivity } from '@/features/pilotMascot/PilotMascot';
import { derivePilotMascotActivity } from '@/features/pilotMascot/pilotMascotActivity';
import {
  AssistantSurfaceProvider,
  useHasAssistantSurfaceProvider,
  usePilotConversationController,
} from '@/features/assistantSurface/AssistantSurfaceProvider';
import type {
  ConfirmationExecution,
  SendMessageOutcome,
} from '@/features/assistantSurface/usePilotConversationController';

export interface Props {
  open: boolean;
  controllerActive?: boolean;
  onClose: () => void;
  offerId?: number;
  onOpenSettings?: () => void;
  variant?: 'drawer' | 'rail' | 'page';
  onExpand?: () => void;
  onDataChanged?: () => void;
  startRequest?: ChatStartRequest;
  onStartRequestConsumed?: (requestKey: number) => boolean;
  pageContext?: PilotPageContext;
  attachmentDraftKey?: PilotAttachmentConversationKey;
  onAttachmentKeyChange?: (key?: PilotAttachmentConversationKey) => void;
  pilotDropTarget?: boolean;
  onOpenEvidence?: (target: EvidenceTarget) => void;
  onboardingFocusToken?: number;
  onOnboardingFocusConsumed?: (token: number) => void;
  onPrepareOfferNegotiation?: (offer: Offer) => void;
  onOpenInterviewStoryLibrary?: () => void;
  onOpenVoiceCoachingGrowth?: () => void;
  offers?: Offer[];
  onActivityChange?: (activity: PilotMascotActivity) => void;
  onReplyLifecycle?: (event: PilotReplyLifecycleEvent) => void;
  conversationRequest?: PilotConversationRequest;
  onConversationRequestConsumed?: (requestKey: number) => void;
}

export interface PilotReplyLifecycleEvent {
  status: 'success' | 'error';
  conversationId: number;
  background: boolean;
}

export interface PilotConversationRequest {
  requestKey: number;
  conversationId: number;
}

const INTERVIEW_STORY_PILOT_INTENTS = new Set([
  '整理面试故事',
  '帮我整理一个面试故事',
]);

export function isInterviewStoryPilotIntent(text: string): boolean {
  return INTERVIEW_STORY_PILOT_INTENTS.has(text.trim());
}

const VOICE_COACHING_PILOT_INTENTS = new Set([
  '查看表达成长',
  '查看我的表达成长',
]);

export function isVoiceCoachingPilotIntent(text: string): boolean {
  return VOICE_COACHING_PILOT_INTENTS.has(text.trim());
}

export function VoiceCoachingPilotEntry({ onOpen }: { onOpen: () => void }) {
  return (
    <Button type="text" icon={<RobotOutlined />} onClick={onOpen} data-testid="pilot-open-voice-coaching-growth">
      查看表达成长
    </Button>
  );
}

const CHAT_WIDTH_STORAGE_KEY = 'offerpilot.chatPanelWidth';
const DEFAULT_CHAT_WIDTH = 920;
const MIN_CHAT_WIDTH = 720;
const CONFIRMATION_RECONCILE_MAX_POLLS = 240;

function maxChatWidth() {
  return Math.max(MIN_CHAT_WIDTH, Math.min(window.innerWidth - 32, 1440));
}

function clampChatWidth(width: number) {
  return Math.max(MIN_CHAT_WIDTH, Math.min(width, maxChatWidth()));
}

function textArg(args: Record<string, unknown> | undefined, key: string): string | null {
  const value = args?.[key];
  return typeof value === 'string' && value.trim() ? value.trim().toLowerCase() : null;
}

function evidenceMatchesPendingAction(item: EvidenceItem, action: PendingAction): boolean {
  const id = action.args?.id;
  if (typeof id === 'number' || typeof id === 'string') {
    const needle = String(id);
    return item.id === needle || item.id.endsWith(`-${needle}`);
  }

  const company = textArg(action.args, 'company_name');
  const position = textArg(action.args, 'position_name');
  const title = textArg(action.args, 'title');
  const itemTitle = item.title.toLowerCase();
  const itemMeta = item.meta?.toLowerCase() ?? '';

  if (company && position) return itemTitle === company && itemMeta.includes(position);
  if (company) return itemTitle === company;
  if (title) return itemTitle === title;
  return false;
}

function pendingActionEvidence(evidence: EvidenceItem[], action: PendingAction): EvidenceItem[] {
  return evidence.filter((item) => evidenceMatchesPendingAction(item, action));
}

function isAbortError(error: unknown): boolean {
  const candidate = error as { code?: string; name?: string; message?: string };
  return (
    candidate?.code === 'ERR_CANCELED' ||
    candidate?.name === 'CanceledError' ||
    candidate?.name === 'AbortError' ||
    candidate?.message === 'canceled'
  );
}

function streamLoadingLabel(event: ChatStreamEvent): string | undefined {
  const data = event.data as Record<string, unknown>;
  if (event.event === 'status') {
    return typeof data.label === 'string' && data.label.trim() ? data.label : undefined;
  }
  if (event.event === 'tool_call') {
    const summary = typeof data.summary === 'string' ? data.summary.trim() : '';
    return summary ? `正在处理：${summary}` : '正在调用本地能力';
  }
  if (event.event === 'tool_result') {
    return data.status === 'error' ? '本地能力返回错误' : '已获得本地结果';
  }
  if (event.event === 'confirmation_required') {
    return '正在准备确认卡片';
  }
  return undefined;
}

export default function ChatPanel(props: Props) {
  const hasProvider = useHasAssistantSurfaceProvider();
  if (!hasProvider) {
    return (
      <AssistantSurfaceProvider>
        <ChatPanelView {...props} />
      </AssistantSurfaceProvider>
    );
  }
  return <ChatPanelView {...props} />;
}

function ChatPanelView({
  open,
  controllerActive = false,
  onClose,
  offerId,
  onOpenSettings,
  variant = 'drawer',
  onExpand,
  onDataChanged,
  startRequest,
  onStartRequestConsumed,
  pageContext,
  attachmentDraftKey,
  onAttachmentKeyChange,
  pilotDropTarget = false,
  onOpenEvidence,
  onboardingFocusToken,
  onOnboardingFocusConsumed,
  onPrepareOfferNegotiation,
  onOpenInterviewStoryLibrary,
  onOpenVoiceCoachingGrowth,
  offers = [],
  onActivityChange,
  onReplyLifecycle,
  conversationRequest,
  onConversationRequestConsumed,
}: Props) {
  const queryClient = useQueryClient();
  const { message: toast } = AntApp.useApp();
  const {
    activeKey: activeAttachmentKey,
    attachments,
    notice: attachmentNotice,
    addAttachment,
    removeAttachment,
    setActiveConversationKey,
    clearAttachmentsByKey,
    beginNewAttachmentDraft,
    ensureNewAttachmentDraft,
  } = usePilotAttachments();
  const incomingPageContextKey = pageContextKey(pageContext);
  const controller = usePilotConversationController();
  const {
    turns,
    setTurns,
    conversationId: convID,
    setConversationId: setConvID,
    pending,
    setPending,
    loading,
    setLoading,
    autoApprove,
    setAutoApprove,
    hasKey,
    setHasKey,
    degraded,
    setDegraded,
    conversations,
    setConversations,
    showArchived,
    setShowArchived,
    dispatchPendingAutoSelect,
    draftContext,
    setDraftContext,
    lastError,
    setLastError,
    lastFailedText,
    setLastFailedText,
    confirmError,
    setConfirmError,
    confirmPhase,
    setConfirmPhase,
    lastUndo,
    setLastUndo,
    loadingLabel,
    setLoadingLabel,
    hasStreamingAssistantContent,
    setHasStreamingAssistantContent,
    composerResetKey,
    setComposerResetKey,
    pinnedContext,
    pinConversationContext,
    activateConversationContext,
    setRequestContextSnapshot,
    setAttachments: setControllerAttachments,
    activeRequestRef,
    streamingAssistantActiveRef,
    titleRefreshTimeoutsRef,
    lastConfirmationInputRef,
    activePendingRef,
    activeConversationIdRef,
    confirmationMonitorRef,
    confirmationLocksRef,
    confirmationReconcileOnOpenRef,
    lockedConfirmationRef,
    startedRequestKeyRef,
    pendingAutoSelectSuppressedRef,
    conversationSelectionRequestRef,
    conversationListRequestRef,
    visibleRequestGenerationRef,
    showArchivedRef,
    consumedOnboardingFocusTokenRef,
    consumedConversationRequestRef,
    openRef,
    bindActions,
    releaseActions,
    bindStopFeedback,
    beginActiveRequest,
    beginConversationSelection,
    buildRequestContext,
    cancelConversationSelection,
    finishActiveRequest,
    finishConversationSelection,
    streamChatRequest,
    streamConfirmationRequest,
    stopActiveRequest,
    approvePending,
    rejectPending,
  } = controller;
  const [offer, setOffer] = useState<Offer | null>(null);
  const [panelOpen, setPanelOpen] = useState(false);

  useEffect(() => {
    setControllerAttachments((current) => {
      const unchanged = current.length === attachments.length
        && current.every((item, index) => {
          const next = attachments[index];
          return item.kind === next?.kind && item.id === next.id && item.label === next.label;
        });
      return unchanged ? current : attachments;
    });
  }, [attachments, setControllerAttachments]);

  useEffect(() => {
    const activity = derivePilotMascotActivity({
      loading,
      confirmationPhase: confirmPhase,
      hasError: Boolean(lastError || confirmError),
      degraded,
      hasPending: Boolean(pending),
    });
    onActivityChange?.(activity);
  }, [confirmError, confirmPhase, degraded, lastError, loading, onActivityChange, pending]);

  useEffect(
    () => () => {
      onActivityChange?.('idle');
    },
    [onActivityChange],
  );
  const [onboardingFocusActive, setOnboardingFocusActive] = useState(false);
  const [onboardingFocusEventToken, setOnboardingFocusEventToken] = useState<number>();
  const [pageContextRemovalState, dispatchPageContextRemoval] = useReducer(
    pilotPageContextRemovalReducer,
    incomingPageContextKey,
    createPilotPageContextRemovalState,
  );
  const activePageContext = deriveActivePageContext(pageContext, pageContextRemovalState);
  const [drawerWidth, setDrawerWidth] = useState(() => {
    const stored = Number(localStorage.getItem(CHAT_WIDTH_STORAGE_KEY));
    return Number.isFinite(stored) && stored > 0 ? clampChatWidth(stored) : DEFAULT_CHAT_WIDTH;
  });
  const endRef = useRef<HTMLDivElement>(null);
  const threadOfferId = useRef<number | undefined>(undefined);
  const actionOwnerRef = useRef({});
  const confirmRetryButtonRef = useRef<HTMLButtonElement | null>(null);
  const restoreConfirmationRetryFocusRef = useRef(false);
  const handoffAttachmentKeyRef = useRef<PilotAttachmentConversationKey>();
  const conversationSurfaceActive = open || controllerActive;
  openRef.current = conversationSurfaceActive;
  showArchivedRef.current = showArchived;
  bindStopFeedback(() => toast.info('已停止当前回复'));
  const docked = variant === 'rail';
  const inlinePage = variant === 'page';

  const activeConv = conversations.find((c) => c.id === convID);
  const isNego = activeConv ? activeConv.mode === 'nego_coach' : offerId !== undefined;
  const contextResetKey = JSON.stringify({
    conversationId: convID ?? null,
    draftRequestKey: draftContext?.requestKey ?? null,
    offerId: offerId ?? null,
    pageContext: incomingPageContextKey,
    isNego,
  });
  const capabilities = capabilitiesForMode(isNego);
  const activePending = resolveActivePendingAction(pending, conversations, convID);
  activePendingRef.current = activePending;
  const settingsQuery = useQuery({
    queryKey: SETTINGS_QUERY_KEY,
    queryFn: getSettings,
    enabled: conversationSurfaceActive,
  });

  useEffect(() => {
    if (!onboardingFocusToken || !conversationSurfaceActive || onboardingFocusToken === consumedOnboardingFocusTokenRef.current) return;

    consumedOnboardingFocusTokenRef.current = onboardingFocusToken;
    setOnboardingFocusEventToken(onboardingFocusToken);
    onOnboardingFocusConsumed?.(onboardingFocusToken);
  }, [conversationSurfaceActive, onboardingFocusToken, onOnboardingFocusConsumed]);

  useEffect(() => {
    if (!onboardingFocusEventToken) return;

    setOnboardingFocusActive(true);
    const timeoutId = window.setTimeout(() => setOnboardingFocusActive(false), 2400);
    return () => window.clearTimeout(timeoutId);
  }, [onboardingFocusEventToken]);

  useEffect(() => {
    if (attachmentDraftKey) {
      handoffAttachmentKeyRef.current = attachmentDraftKey;
      setActiveConversationKey(attachmentDraftKey);
      return;
    }
    if (convID !== undefined) {
      setActiveConversationKey(`conversation:${convID}`);
      return;
    }
    if (draftContext) {
      setActiveConversationKey(`new:${draftContext.requestKey}`);
      return;
    }
    if (
      handoffAttachmentKeyRef.current !== undefined &&
      handoffAttachmentKeyRef.current === activeAttachmentKey
    ) {
      return;
    }
    ensureNewAttachmentDraft();
  }, [
    activeAttachmentKey,
    attachmentDraftKey,
    convID,
    draftContext?.requestKey,
    ensureNewAttachmentDraft,
    setActiveConversationKey,
  ]);

  useEffect(() => {
    onAttachmentKeyChange?.(activeAttachmentKey);
  }, [activeAttachmentKey, onAttachmentKeyChange]);

  useEffect(() => {
    dispatchPageContextRemoval({ type: 'sync', contextKey: incomingPageContextKey });
  }, [incomingPageContextKey]);

  useEffect(() => {
    activeConversationIdRef.current = convID;
    confirmationMonitorRef.current += 1;
  }, [convID]);

  useEffect(() => {
    if (conversationSurfaceActive) {
      const reconciliation = confirmationReconcileOnOpenRef.current;
      confirmationReconcileOnOpenRef.current = null;
      if (
        reconciliation &&
        activeConversationIdRef.current === reconciliation.conversationId
      ) {
        lockedConfirmationRef.current = reconciliation;
        visibleRequestGenerationRef.current += 1;
        const reopenGeneration = visibleRequestGenerationRef.current;
        setConfirmPhase('saving');
        const monitorId = ++confirmationMonitorRef.current;
        void monitorConfirmationCompletion(
          reconciliation.conversationId,
          monitorId,
          reopenGeneration,
          reconciliation.confirmationToken,
          reconciliation,
        );
      }
    }
  }, [conversationSurfaceActive]);

  useEffect(() => {
    if (
      !shouldRestoreConfirmationRetryFocus(
        restoreConfirmationRetryFocusRef.current,
        confirmError,
        loading,
      )
    ) {
      return;
    }
    confirmRetryButtonRef.current?.focus();
    restoreConfirmationRetryFocusRef.current = false;
  }, [confirmError, loading]);

  function markPendingAutoSelect(action: 'suppress' | 'allow') {
    pendingAutoSelectSuppressedRef.current = action === 'suppress';
    dispatchPendingAutoSelect(action);
  }

  function isCurrentVisibleRequest(requestGeneration: number) {
    return isCurrentVisibleConversationRequest(
      requestGeneration,
      visibleRequestGenerationRef.current,
    );
  }

  function refreshConversations(includeArchived = showArchivedRef.current) {
    const requestId = ++conversationListRequestRef.current;
    listConversations(includeArchived)
      .then((items) => {
        if (requestId === conversationListRequestRef.current) setConversations(items);
      })
      .catch(() => undefined);
  }

  function scheduleTitleRefresh() {
    titleRefreshTimeoutsRef.current.forEach((id) => window.clearTimeout(id));
    titleRefreshTimeoutsRef.current = [1000, 3000].map((delay) =>
      window.setTimeout(() => {
        refreshConversations();
      }, delay),
    );
  }

  useEffect(() => () => {
    titleRefreshTimeoutsRef.current.forEach((id) => window.clearTimeout(id));
  }, []);

  useEffect(() => {
    if (!conversationSurfaceActive) return;
    if (offerId !== threadOfferId.current) {
      stopActiveRequest({ silent: true });
      cancelConversationSelection();
      visibleRequestGenerationRef.current += 1;
      streamingAssistantActiveRef.current = false;
      setConvID(undefined);
      setTurns([]);
      setPending(null);
      setLoading(false);
      setLoadingLabel(undefined);
      setRequestContextSnapshot(undefined);
      activateConversationContext(undefined);
      markPendingAutoSelect('allow');
      lastConfirmationInputRef.current = null;
      setDegraded(false);
      threadOfferId.current = offerId;
    }
    refreshConversations(showArchived);
  }, [conversationSurfaceActive, offerId, showArchived]);

  useEffect(() => {
    if (!settingsQuery.data) return;
    setAutoApprove(settingsQuery.data.chat_auto_approve_writes);
    setHasKey(settingsQuery.data.has_api_key);
  }, [settingsQuery.data]);

  useEffect(() => {
    if (offerId === undefined) {
      setOffer(null);
      return;
    }
    getOffer(offerId)
      .then(setOffer)
      .catch(() => setOffer(null));
  }, [offerId]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [turns, pending, loading]);

  useEffect(() => {
    const onResize = () => setDrawerWidth((w) => clampChatWidth(w));
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  useEffect(() => {
    localStorage.setItem(CHAT_WIDTH_STORAGE_KEY, String(drawerWidth));
  }, [drawerWidth]);

  function startResize(e: React.PointerEvent<HTMLDivElement>) {
    e.preventDefault();
    const startX = e.clientX;
    const startWidth = drawerWidth;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';

    const onMove = (move: PointerEvent) => {
      setDrawerWidth(clampChatWidth(startWidth + startX - move.clientX));
    };
    const onUp = () => {
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
  }

  function startNewChat() {
    if (activeRequestRef.current?.kind === 'confirmation') {
      toast.info('操作确认仍在处理中，请稍候');
      return false;
    }
    beginNewAttachmentDraft();
    markPendingAutoSelect('suppress');
    cancelConversationSelection();
    visibleRequestGenerationRef.current += 1;
    if (shouldAbortActiveRequestOnReplacement(activeRequestRef.current)) {
      stopActiveRequest({ silent: true });
    }
    lockedConfirmationRef.current = null;
    streamingAssistantActiveRef.current = false;
    setConvID(undefined);
    setTurns([]);
    setPending(null);
    lastConfirmationInputRef.current = null;
    setDegraded(false);
    setPanelOpen(false);
    setLastError(null);
    setLastFailedText('');
    setConfirmError(null);
    setConfirmPhase('idle');
    setLastUndo(null);
    setLoadingLabel(undefined);
    setHasStreamingAssistantContent(false);
    setDraftContext(null);
    setRequestContextSnapshot(undefined);
    activateConversationContext(undefined);
    setComposerResetKey((key) => key + 1);
    setLoading(false);
    return true;
  }

  useEffect(() => {
    if (!startRequest || startedRequestKeyRef.current === startRequest.requestKey) return;
    if (!startNewChat()) return;
    setDraftContext(startRequest);
  }, [startRequest?.requestKey, loading]);

  useEffect(() => {
    if (!draftContext?.initialMessage || startedRequestKeyRef.current === draftContext.requestKey) return;
    if (onStartRequestConsumed && !onStartRequestConsumed(draftContext.requestKey)) {
      startedRequestKeyRef.current = draftContext.requestKey;
      return;
    }
    startedRequestKeyRef.current = draftContext.requestKey;
    void sendMessage(draftContext.initialMessage);
  }, [draftContext?.requestKey, onStartRequestConsumed]);

  async function selectConversation(id: number) {
    markPendingAutoSelect('allow');
    if (id === convID) return;
    setDraftContext(null);
    setRequestContextSnapshot(undefined);
    activateConversationContext(id);
    visibleRequestGenerationRef.current += 1;
    const visibleRequestGeneration = visibleRequestGenerationRef.current;
    const requestId = beginConversationSelection();
    try {
      const stored = await getConversation(id);
      if (
        !shouldApplyConversationRequest(
          requestId,
          conversationSelectionRequestRef.current,
          pendingAutoSelectSuppressedRef.current,
        )
      ) return;
      setConvID(id);
      setTurns(buildTurns(stored));
      const selectedPending = pendingActionForConversation(conversations, id);
      setPending(selectedPending);
      lastConfirmationInputRef.current = null;
      setDegraded(false);
      setConfirmError(null);
      setHasStreamingAssistantContent(false);
      const conversation = conversations.find((item) => item.id === id);
      const selectedConfirmationLock = confirmationLocksRef.current.get(id);
      if (selectedConfirmationLock) {
        lockedConfirmationRef.current = selectedConfirmationLock;
        setConfirmPhase('saving');
        const monitorId = ++confirmationMonitorRef.current;
        void monitorConfirmationCompletion(
          id,
          monitorId,
          visibleRequestGeneration,
          selectedConfirmationLock.confirmationToken,
          selectedConfirmationLock,
        );
      } else {
        confirmationLocksRef.current.delete(id);
        lockedConfirmationRef.current = null;
        setConfirmPhase('idle');
      }
      setLastUndo(conversation?.last_write_undo ?? null);
    } catch (e: any) {
      if (requestId === conversationSelectionRequestRef.current) {
        toast.error(e?.response?.data?.error ?? '加载对话失败');
      }
    } finally {
      finishConversationSelection(requestId);
    }
  }

  useEffect(() => {
    if (
      !conversationSurfaceActive
      || !conversationRequest
      || conversationRequest.requestKey === consumedConversationRequestRef.current
    ) return;
    consumedConversationRequestRef.current = conversationRequest.requestKey;
    onConversationRequestConsumed?.(conversationRequest.requestKey);
    void selectConversation(conversationRequest.conversationId);
  }, [conversationRequest?.conversationId, conversationRequest?.requestKey, conversationSurfaceActive, onConversationRequestConsumed]);

  async function removeConversation(id: number) {
    try {
      await deleteConversation(id);
      if (id === convID) startNewChat();
      refreshConversations();
    } catch (e: any) {
      toast.error(e?.response?.data?.error ?? '删除失败');
    }
  }

  async function handleConversationUpdate(id: number, payload: Parameters<typeof updateConversation>[1]) {
    try {
      const updated = await updateConversation(id, payload);
      if (updated.archived_at) {
        setConversations((items) => items.filter((item) => item.id !== id));
        if (id === convID) startNewChat();
      } else {
        setConversations((items) => items.map((item) => (item.id === id ? updated : item)));
      }
      refreshConversations();
    } catch (e: any) {
      toast.error(e?.response?.data?.error ?? '更新对话失败');
    }
  }

  async function clearActiveContext() {
    if (!convID) {
      setDraftContext(null);
      activateConversationContext(undefined);
      return;
    }
    await handleConversationUpdate(convID, { context_type: 'workspace', context_ref: '' });
    pinConversationContext(convID, undefined);
  }

  useEffect(() => {
    if (!conversationSurfaceActive || draftContext !== null || convID !== undefined || turns.length > 0 || loading) return;
    if (showArchived) return;
    if (pendingAutoSelectSuppressedRef.current) return;
    const activeConversations = filterConversationsByView(conversations, 'active');
    const pendingConversationId = firstPendingConversationId(activeConversations);
    if (pendingConversationId === undefined) return;
    void selectConversation(pendingConversationId);
  }, [conversationSurfaceActive, convID, conversations, turns.length, loading, showArchived, draftContext]);

  async function finishMessage(
    resp: Extract<ChatResponse, { type: 'message' }>,
    visibleRequestGeneration: number,
  ): Promise<boolean> {
    const storedTurns = await reloadConversationTurns(resp.conversation_id, getConversation);
    if (!isCurrentVisibleRequest(visibleRequestGeneration)) {
      refreshConversations();
      return false;
    }
    setConvID(resp.conversation_id);
    setPending(null);
    setDegraded(!!resp.degraded);
    setConfirmError(null);
    setLastUndo(resp.undo ?? null);
    if (storedTurns) {
      setTurns(storedTurns);
    } else {
      setTurns((t) => {
        if (streamingAssistantActiveRef.current) {
          const last = t[t.length - 1];
          if (last?.role === 'assistant') {
            return [...t.slice(0, -1), { ...last, content: resp.message }];
          }
        }
        return [...t, { role: 'assistant', content: resp.message }];
      });
    }
    streamingAssistantActiveRef.current = false;
    refreshConversations();
    return true;
  }

  function appendAssistantDelta(delta: string) {
    if (!delta) return;
    setHasStreamingAssistantContent(true);
    setTurns((items) => {
      if (!streamingAssistantActiveRef.current) {
        streamingAssistantActiveRef.current = true;
        return [...items, { role: 'assistant', content: delta }];
      }
      const last = items[items.length - 1];
      if (last?.role !== 'assistant') {
        return [...items, { role: 'assistant', content: delta }];
      }
      return [...items.slice(0, -1), { ...last, content: last.content + delta }];
    });
  }

  function finalizeStreamedAssistant(message: string) {
    if (!streamingAssistantActiveRef.current || !message) return;
    setTurns((items) => {
      const last = items[items.length - 1];
      if (last?.role !== 'assistant') return items;
      return [...items.slice(0, -1), { ...last, content: message }];
    });
  }

  async function syncConversationAfterAbort(
    conversationId: number | undefined,
    visibleRequestGeneration: number,
  ): Promise<PendingAction | null | undefined> {
    let nextConversations: Conversation[] | undefined;
    try {
      const requestId = ++conversationListRequestRef.current;
      const includeArchived = showArchivedRef.current;
      nextConversations = await listConversations(includeArchived);
      if (requestId === conversationListRequestRef.current) {
        setConversations(nextConversations);
      }
    } catch {
      nextConversations = undefined;
    }
    if (!conversationId) return undefined;
    try {
      const stored = await getConversation(conversationId);
      const summary = (nextConversations ?? conversations).find((item) => item.id === conversationId);
      const nextPending = pendingActionForConversation(
        nextConversations ?? conversations,
        conversationId,
      );
      if (!isCurrentVisibleRequest(visibleRequestGeneration)) {
        return nextPending;
      }
      setConvID(conversationId);
      setTurns(buildTurns(stored));
      setPending(nextPending);
      setLastUndo(summary?.last_write_undo ?? null);
      return nextPending;
    } catch {
      refreshConversations();
      return undefined;
    }
  }

  async function monitorConfirmationCompletion(
    conversationId: number,
    monitorId: number,
    visibleRequestGeneration: number,
    expectedConfirmationToken: string,
    execution: ConfirmationExecution,
  ) {
    let pollCount = 0;
    while (
      pollCount < CONFIRMATION_RECONCILE_MAX_POLLS &&
      confirmationMonitorRef.current === monitorId &&
      activeConversationIdRef.current === conversationId &&
      isCurrentVisibleRequest(visibleRequestGeneration)
    ) {
      await new Promise((resolve) => window.setTimeout(resolve, 500));
      pollCount += 1;
      if (
        confirmationMonitorRef.current !== monitorId ||
        activeConversationIdRef.current !== conversationId
      ) {
        return;
      }
      const nextPending = await syncConversationAfterAbort(
        conversationId,
        visibleRequestGeneration,
      );
      if (!isCurrentVisibleRequest(visibleRequestGeneration)) return;
      if (
        shouldConsumeConfirmationSettlement(
          nextPending,
          expectedConfirmationToken,
          isCurrentVisibleRequest(visibleRequestGeneration),
        )
      ) {
        if (clearOwnedConfirmationLock(confirmationLocksRef.current, conversationId, execution)) {
          if (lockedConfirmationRef.current === execution) lockedConfirmationRef.current = null;
          setConfirmPhase('idle');
          onDataChanged?.();
        }
        return;
      }
    }
  }

  async function refreshConfirmationStatus() {
    const locked = lockedConfirmationRef.current;
    if (!locked || activeConversationIdRef.current !== locked.conversationId) return;
    const visibleRequestGeneration = visibleRequestGenerationRef.current;
    const nextPending = await syncConversationAfterAbort(
      locked.conversationId,
      visibleRequestGeneration,
    );
    if (!isCurrentVisibleRequest(visibleRequestGeneration)) return;
    if (
      shouldConsumeConfirmationSettlement(
        nextPending,
        locked.confirmationToken,
        isCurrentVisibleRequest(visibleRequestGeneration),
      )
    ) {
      if (clearOwnedConfirmationLock(confirmationLocksRef.current, locked.conversationId, locked)) {
        if (lockedConfirmationRef.current === locked) lockedConfirmationRef.current = null;
        setConfirmPhase('idle');
        onDataChanged?.();
      }
    } else {
      setConfirmPhase('saving');
    }
  }

  async function sendMessage(text: string): Promise<SendMessageOutcome> {
    const trimmed = text.trim();
    if (!trimmed || loading || activePending) return 'ignored';
    if (onOpenInterviewStoryLibrary && isInterviewStoryPilotIntent(trimmed)) {
      // This is a local navigation intent.  It must not create a Chat message,
      // call a Provider, or make a Story-domain write before the user selects
      // original sources and confirms the later Story action.
      onOpenInterviewStoryLibrary();
      return 'sent';
    }
    if (onOpenVoiceCoachingGrowth && isVoiceCoachingPilotIntent(trimmed)) {
      // Local read-only navigation: no Chat message, Provider call, or domain write.
      onOpenVoiceCoachingGrowth();
      return 'sent';
    }
    const requestLease = beginActiveRequest('chat', convID);
    if (!requestLease) return 'ignored';
    const attachmentDraftKeyAtSend = activeAttachmentKey ?? ensureNewAttachmentDraft();
    if (convID === undefined) markPendingAutoSelect('suppress');
    const visibleRequestGeneration = ++visibleRequestGenerationRef.current;
    lastConfirmationInputRef.current = null;
    setLastError(null);
    setLastFailedText('');
    setConfirmError(null);
    setConfirmPhase('idle');
    setLoadingLabel('正在理解你的问题');
    setTurns((t) => [...t, { role: 'user', content: trimmed }]);
    streamingAssistantActiveRef.current = false;
    setHasStreamingAssistantContent(false);
    const requestPageContext = convID === undefined ? activePageContext : pinnedContext;
    setRequestContextSnapshot(requestPageContext);
    let streamConversationId = convID;
    try {
      const isNew = convID === undefined;
      const requestContext = buildRequestContext({
        conversationId: convID,
        draftContext,
        offerApplicationId: offer?.application_id,
        offerId,
        pageContext: requestPageContext,
        attachments,
      });
      const resp = await streamChatRequest(requestLease, trimmed, convID, requestContext, {
        onEvent: (event) => {
          if (event.event === 'user_message_saved') {
            void queryClient.invalidateQueries({ queryKey: ONBOARDING_QUERY_KEY });
          }
          if (!isCurrentVisibleRequest(visibleRequestGeneration)) return;
          if (event.conversation_id) {
            streamConversationId = event.conversation_id;
            if (isNew) {
              setConvID(event.conversation_id);
              pinConversationContext(event.conversation_id, requestPageContext);
            }
          }
          if (event.event === 'assistant_delta') {
            const data = event.data as { delta?: unknown };
            if (typeof data.delta === 'string') appendAssistantDelta(data.delta);
          }
          if (event.event === 'assistant_message') {
            const data = event.data as { message?: unknown };
            if (typeof data.message === 'string') finalizeStreamedAssistant(data.message);
          }
          const label = streamLoadingLabel(event);
          if (label) setLoadingLabel(label);
        },
      });
      if (!isCurrentVisibleRequest(visibleRequestGeneration)) {
        refreshConversations();
        if (attachmentDraftKeyAtSend) clearAttachmentsByKey(attachmentDraftKeyAtSend);
        return 'sent';
      }
      if (isNew) {
        setConvID(resp.conversation_id);
        pinConversationContext(resp.conversation_id, requestPageContext);
      }
      if (resp.type === 'confirmation_required') {
        setLoadingLabel('正在准备确认卡片');
        setConvID(resp.conversation_id);
        setPending(resp.pending_action);
        setConfirmPhase('idle');
        const storedTurns = await reloadConversationTurns(resp.conversation_id, getConversation);
        if (isCurrentVisibleRequest(visibleRequestGeneration) && storedTurns) {
          setTurns(storedTurns);
        }
        refreshConversations();
      } else {
        const applied = await finishMessage(resp, visibleRequestGeneration);
        if (applied && autoApprove) onDataChanged?.();
        if (applied && resp.degraded) {
          toast.info('当前模型不支持工具调用，已切换为只读摘要模式');
        }
      }
       if (isNew) {
         refreshConversations();
         scheduleTitleRefresh();
       }
      if (attachmentDraftKeyAtSend) clearAttachmentsByKey(attachmentDraftKeyAtSend);
      const completedConversationId = resp.conversation_id ?? streamConversationId;
      if (completedConversationId !== undefined) {
        onReplyLifecycle?.({
          status: 'success',
          conversationId: completedConversationId,
          background: !openRef.current,
        });
      }
      return 'sent';
    } catch (e: any) {
      if (!isCurrentVisibleRequest(visibleRequestGeneration)) {
        refreshConversations();
        return 'ignored';
      }
      if (isAbortError(e)) {
        await syncConversationAfterAbort(streamConversationId, visibleRequestGeneration);
        return 'stopped';
      }
      const error = e?.response?.data?.error ?? e?.message ?? '对话失败，请稍后重试';
      if (streamingAssistantActiveRef.current) {
        streamingAssistantActiveRef.current = false;
        await syncConversationAfterAbort(streamConversationId, visibleRequestGeneration);
        if (!isCurrentVisibleRequest(visibleRequestGeneration)) return 'ignored';
        setLastError(error);
        setLastFailedText(trimmed);
        toast.error(error);
        if (streamConversationId !== undefined) {
          onReplyLifecycle?.({
            status: 'error',
            conversationId: streamConversationId,
            background: !openRef.current,
          });
        }
        return 'failed';
      }
      setTurns((items) => {
        const last = items[items.length - 1];
        return last?.role === 'user' && last.content === trimmed ? items.slice(0, -1) : items;
      });
      setLastError(error);
      setLastFailedText(trimmed);
      toast.error(error);
      if (streamConversationId !== undefined) {
        onReplyLifecycle?.({
          status: 'error',
          conversationId: streamConversationId,
          background: !openRef.current,
        });
      }
      return 'failed';
    } finally {
      finishActiveRequest(requestLease);
      if (isCurrentVisibleRequest(visibleRequestGeneration)) {
        setLoadingLabel(undefined);
        setRequestContextSnapshot(undefined);
      }
    }
  }

  function retryLastMessage() {
    if (!lastFailedText || loading || activePending) return;
    void sendMessage(lastFailedText);
  }

  function clearLastFailure() {
    setLastError(null);
  }

  function activePendingLabel(action: PendingAction | null): string {
    if (!action) return '本次写入';
    return action.workflow?.current_label || toolMeta(action.tool_name).label;
  }

  async function handleConfirm(input: ConfirmationInput) {
    if (!convID) return;
    if (input.confirmation_token !== activePendingRef.current?.confirmation_token) return;
    if (confirmationLocksRef.current.has(convID)) return;
    const requestLease = beginActiveRequest('confirmation', convID, input.confirmation_token);
    if (!requestLease) return;
    const visibleRequestGeneration = ++visibleRequestGenerationRef.current;
    const confirmationExecution: ConfirmationExecution = {
      conversationId: convID,
      confirmationToken: input.confirmation_token,
    };
    confirmationLocksRef.current.set(convID, confirmationExecution);
    const approved = input.approved;
    lastConfirmationInputRef.current = confirmationInputForRetry(input);
    setConfirmPhase(approved ? 'saving' : 'idle');
    setLoadingLabel(approved ? `正在执行：${activePendingLabel(activePending)}` : '正在取消本次写入');
    streamingAssistantActiveRef.current = false;
    setHasStreamingAssistantContent(false);
    try {
      const resp = await streamConfirmationRequest(requestLease, convID, input, {
        onEvent: (event) => {
          if (!isCurrentVisibleRequest(visibleRequestGeneration)) return;
          if (event.event === 'assistant_delta') {
            const data = event.data as { delta?: unknown };
            if (typeof data.delta === 'string') appendAssistantDelta(data.delta);
          }
          if (event.event === 'assistant_message') {
            const data = event.data as { message?: unknown };
            if (typeof data.message === 'string') finalizeStreamedAssistant(data.message);
          }
          const label = streamLoadingLabel(event);
          if (label) setLoadingLabel(label);
        },
      });
      if (!isCurrentVisibleRequest(visibleRequestGeneration)) {
        refreshConversations();
        return;
      }
      clearOwnedConfirmationLock(confirmationLocksRef.current, convID, confirmationExecution);
      if (lockedConfirmationRef.current === confirmationExecution) {
        lockedConfirmationRef.current = null;
      }
      if (resp.type === 'confirmation_required') {
        restoreConfirmationRetryFocusRef.current = false;
        lastConfirmationInputRef.current = null;
        setConfirmError(null);
        setConvID(resp.conversation_id);
        setPending(resp.pending_action);
        setConfirmPhase('idle');
        const storedTurns = await reloadConversationTurns(resp.conversation_id, getConversation);
        if (isCurrentVisibleRequest(visibleRequestGeneration) && storedTurns) {
          setTurns(storedTurns);
        }
        refreshConversations();
      } else {
        restoreConfirmationRetryFocusRef.current = false;
        const applied = await finishMessage(resp, visibleRequestGeneration);
        if (!applied) return;
        lastConfirmationInputRef.current = null;
        if (!approved || resp.write_status === 'cancelled') {
          setConfirmPhase('idle');
        } else if (resp.write_status === 'success') {
          setConfirmPhase('success');
          if (approved && applied) onDataChanged?.();
        } else if (resp.write_status === 'failed') {
          const error = resp.write_error || '写入失败，请检查记录后重试。';
          setConfirmError(error);
          setConfirmPhase('error');
          toast.error(error);
        } else {
          const error = '写入结果未知，请检查记录后再继续。';
          setConfirmError(error);
          setConfirmPhase('error');
          toast.error(error);
        }
      }
    } catch (e: any) {
      if (!isCurrentVisibleRequest(visibleRequestGeneration)) {
        refreshConversations();
        return;
      }
      if (isAbortError(e)) {
        restoreConfirmationRetryFocusRef.current = false;
        await syncConversationAfterAbort(convID, visibleRequestGeneration);
        return;
      }
      const error = e?.response?.data?.error ?? e?.message ?? '确认失败';
      if (confirmationErrorAllowsImmediateRetry(e?.code)) {
        clearOwnedConfirmationLock(confirmationLocksRef.current, convID, confirmationExecution);
        if (lockedConfirmationRef.current === confirmationExecution) {
          lockedConfirmationRef.current = null;
        }
        restoreConfirmationRetryFocusRef.current = true;
        setConfirmError(error);
        setConfirmPhase('error');
        toast.error(error);
        return;
      }
      if (confirmationErrorRequiresSync(e?.code)) {
        restoreConfirmationRetryFocusRef.current = false;
        lastConfirmationInputRef.current = null;
        streamingAssistantActiveRef.current = false;
        if (e?.code === 'stale_pending_action') setPending(null);
        const nextPending = await syncConversationAfterAbort(convID, visibleRequestGeneration);
        if (!isCurrentVisibleRequest(visibleRequestGeneration)) return;
        const confirmationSettled = hasConfirmationSettled(
          nextPending,
          input.confirmation_token,
        );
        if (confirmationSettled) {
          clearOwnedConfirmationLock(confirmationLocksRef.current, convID, confirmationExecution);
          if (lockedConfirmationRef.current === confirmationExecution) {
            lockedConfirmationRef.current = null;
          }
        }
        setConfirmError(null);
        if (
          e?.code === 'confirmation_in_progress' &&
          convID &&
          !confirmationSettled &&
          nextPending !== null
        ) {
          lockedConfirmationRef.current = confirmationExecution;
          setConfirmPhase('saving');
          const monitorId = ++confirmationMonitorRef.current;
          void monitorConfirmationCompletion(
            convID,
            monitorId,
            visibleRequestGeneration,
            input.confirmation_token,
            confirmationExecution,
          );
        } else {
          setConfirmPhase('idle');
        }
        toast.error(error);
        return;
      }
      if (streamingAssistantActiveRef.current) {
        streamingAssistantActiveRef.current = false;
        await syncConversationAfterAbort(convID, visibleRequestGeneration);
        if (!isCurrentVisibleRequest(visibleRequestGeneration)) return;
      }
      lockedConfirmationRef.current = confirmationExecution;
      setConfirmError(null);
      setConfirmPhase('saving');
      const monitorId = ++confirmationMonitorRef.current;
      void monitorConfirmationCompletion(
        convID,
        monitorId,
        visibleRequestGeneration,
        input.confirmation_token,
        confirmationExecution,
      );
      toast.error(error);
    } finally {
      finishActiveRequest(requestLease);
      if (isCurrentVisibleRequest(visibleRequestGeneration)) {
        setLoadingLabel(undefined);
      }
    }
  }

  function retryConfirmAction() {
    if (!activePending || loading) return;
    const retryInput = confirmationInputForRetry(lastConfirmationInputRef.current);
    if (!retryInput) return;
    if (retryInput.confirmation_token !== activePending.confirmation_token) {
      restoreConfirmationRetryFocusRef.current = false;
      lastConfirmationInputRef.current = null;
      setConfirmError(null);
      setConfirmPhase('idle');
      return;
    }
    restoreConfirmationRetryFocusRef.current = true;
    void handleConfirm(retryInput);
  }

  async function handleUndoLastWrite() {
    if (!convID || !lastUndo || loading) return;
    const requestLease = beginActiveRequest('undo', convID);
    if (!requestLease) return;
    const visibleRequestGeneration = ++visibleRequestGenerationRef.current;
    setConfirmPhase('saving');
    try {
      const resp = await undoLastWrite(
        convID,
        lastUndo.parent_operation_id,
        { signal: requestLease.controller.signal },
      );
      const applied = await finishMessage(resp, visibleRequestGeneration);
      if (applied) {
        setLastUndo(null);
        setConfirmPhase('success');
        toast.success('已撤销最近一次 AI 写入');
      }
      if (applied) onDataChanged?.();
    } catch (e: any) {
      if (!isCurrentVisibleRequest(visibleRequestGeneration)) return;
      if (isAbortError(e)) return;
      const error = e?.response?.data?.error ?? '撤销失败';
      setConfirmPhase('error');
      toast.error(error);
    } finally {
      finishActiveRequest(requestLease);
      if (isCurrentVisibleRequest(visibleRequestGeneration)) {
        setLoadingLabel(undefined);
      }
    }
  }

  async function toggleAutoApprove(value: boolean) {
    setAutoApprove(value);
    try {
      const settings = await updateAutoApprove(value);
      setAutoApprove(settings.chat_auto_approve_writes);
      setHasKey(settings.has_api_key);
    } catch {
      setAutoApprove(!value);
      toast.error('设置保存失败');
    }
  }

  function handleCapability(cap: Capability) {
    setPanelOpen(false);
    void sendMessage(cap.prompt);
  }

  function removeRequestContextChip(chipKey: string) {
    if (!activePageContext) return;
    dispatchPageContextRemoval({ type: 'remove', contextKey: incomingPageContextKey, chipKey });
  }

  bindActions(actionOwnerRef.current, {
    sendMessage,
    selectConversation,
    startNewChat,
    retryLastMessage,
    clearLastFailure,
    handleConfirm,
    retryConfirmAction,
    refreshConfirmationStatus,
    clearActiveContext,
  });

  useEffect(() => () => releaseActions(actionOwnerRef.current), [releaseActions]);

  const composerDisabled = loading || !!activePending || !hasKey;
  const composerDisabledReason = !hasKey
    ? '先配置 API key 后即可对话'
    : activePending
      ? pendingComposerDisabledReason(activePending)
      : loading
        ? 'AI 正在处理，可点击停止当前回复'
        : undefined;
  const showEmpty = turns.length === 0 && !activePending && !loading;
  const threadEvidence = collectEvidence(turns);
  const confirmationEvidence = activePending ? pendingActionEvidence(threadEvidence, activePending) : [];
  const activeRequestChips = activePageContext ? pageContextChips(activePageContext) : [];
  const attachmentSuggestions = pilotQuickQuestions(attachments);
  const contextLabel =
    !convID && draftContext
      ? draftContext.context_label
      : activeConv?.context_label
        ? activeConv.context_label
        : activeConv?.context_type === 'application' && activeConv.context_ref
        ? `投递 #${activeConv.context_ref}`
      : isNego
        ? '谈薪上下文'
        : convID
          ? '工作台'
          : null;
  const canClearContext = draftContext !== null || (!!convID && !!activeConv && activeConv.context_type !== 'workspace');

  const iconBtnStyle: React.CSSProperties = {
    border: '1px solid var(--op-border)',
    background: 'var(--op-surface)',
    color: 'var(--op-muted)',
    borderRadius: 8,
    width: 32,
    height: 32,
    cursor: 'pointer',
    marginLeft: 8,
  };

  if (!open) return null;

  const workspace = (
    <>
      {!docked && !inlinePage && (
        <div
          className={styles.resizeHandle}
          role="separator"
          aria-orientation="vertical"
          aria-label="调整 Pilot 宽度"
          onPointerDown={startResize}
        />
      )}
      <div
        className={`${styles.workspace} ${docked ? styles.workspaceDocked : ''} ${
          inlinePage ? styles.workspacePage : ''
        } ${onboardingFocusActive ? styles.onboardingFocus : ''}`}
        data-onboarding-target="pilot"
      >
        {onboardingFocusActive ? (
          <div className={styles.srOnly} role="status" aria-live="polite">
            Pilot 输入框已就绪
          </div>
        ) : null}
        <header className={styles.header}>
          <div className={styles.avatar} aria-hidden="true">
            <RobotOutlined />
          </div>
          <div style={{ minWidth: 0 }}>
            <div className={`${styles.headTitle} op-gradient-text`}>OfferPilot 领航员</div>
            <div className={styles.headSub}>基于投递、日程、复盘与 Offer 实时作答</div>
          </div>
          <span className={styles.modeBadge}>{isNego ? '谈薪教练' : '通用助手'}</span>
          {docked && (
            <button
              type="button"
              className={styles.dockedNewChat}
              aria-label="新建对话"
              title="新建对话"
              onClick={startNewChat}
              style={{ ...iconBtnStyle, display: 'inline-flex', alignItems: 'center', justifyContent: 'center' }}
            >
              {createElement(PlusOutlined)}
            </button>
          )}
          {docked && onExpand && (
            <button
              type="button"
              className={styles.dockedExpand}
              aria-label="打开 Pilot tab"
              title="打开 Pilot tab"
              onClick={onExpand}
              style={{ ...iconBtnStyle, display: 'inline-flex', alignItems: 'center', justifyContent: 'center' }}
            >
              {createElement(ExpandAltOutlined)}
            </button>
          )}
          <button
            type="button"
            className={styles.panelToggle}
            aria-label="上下文面板"
            onClick={() => setPanelOpen((v) => !v)}
            style={iconBtnStyle}
          >
            {createElement(AppstoreOutlined)}
          </button>
          {((!docked && !inlinePage) || offerId !== undefined) && (
            <button
              type="button"
              aria-label={docked ? '退出 Offer 上下文' : '关闭'}
              onClick={onClose}
              style={{ ...iconBtnStyle, display: 'inline-flex', alignItems: 'center', justifyContent: 'center' }}
            >
              {createElement(CloseOutlined)}
            </button>
          )}
        </header>

        <div className={styles.body}>
          <ThreadRail
            conversations={conversations}
            activeId={convID}
            showArchived={showArchived}
            onViewChange={setShowArchived}
            onSelect={selectConversation}
            onNew={startNewChat}
            onDelete={removeConversation}
            onUpdate={handleConversationUpdate}
          />

          <section className={styles.center}>
            <div className={styles.stream}>
              {showEmpty ? (
                <div className={styles.empty}>
                  <div className={styles.emptyTitle}>
                    {isNego ? '开始谈薪辅导' : '你好，我是 OfferPilot 领航员'}
                  </div>
                  <div className={styles.emptyHint}>
                    {isNego
                      ? '我会结合这份 Offer 与你的复盘，帮你评估、准备话术、模拟谈判。'
                      : '我能查询投递、日程、复盘与知识库，并在写入前向你确认。'}
                  </div>
                  <div className={styles.emptyPrompts}>
                    {capabilities.map((cap) => (
                      <button
                        key={cap.id}
                        type="button"
                        className={styles.promptCard}
                        disabled={composerDisabled}
                        onClick={() => handleCapability(cap)}
                      >
                        <span className={styles.promptIcon} aria-hidden="true">
                          {createElement(cap.icon)}
                        </span>
                        {cap.label}
                      </button>
                    ))}
                  </div>
                </div>
              ) : (
                turns.map((turn, i) => (
                  <MessageBubble
                    key={i}
                    turn={turn}
                    index={i}
                    onOpenEvidence={onOpenEvidence}
                    actionsDisabled={composerDisabled}
                    onAction={(action) => void sendMessage(`继续处理：${action}`)}
                  />
                ))
              )}

              {activeRequestChips.length > 0 ? (
                <div className={styles.requestContextRow} aria-label="本次请求上下文">
                  {activeRequestChips.map((chip) => (
                    <div key={chip.key} className={styles.requestContextChip}>
                      <span className={styles.requestContextChipText}>
                        <span className={styles.requestContextLabel}>{chip.label}</span>
                        <span className={styles.requestContextValue} title={chip.value}>{chip.value}</span>
                      </span>
                      <button
                        type="button"
                        className={styles.requestContextClear}
                        aria-label={`移除${chip.label}`}
                        title={`移除${chip.label}`}
                        onClick={() => removeRequestContextChip(chip.key)}
                        disabled={loading}
                      >
                        {createElement(CloseOutlined)}
                      </button>
                    </div>
                  ))}
                </div>
              ) : null}

              {contextLabel ? (
                <div className={styles.contextBadge}>
                  <span>当前上下文</span>
                  <b>{contextLabel}</b>
                  {canClearContext ? (
                    <button
                      type="button"
                      className={styles.contextClear}
                      aria-label="移除当前上下文"
                      title="移除当前上下文"
                      onClick={clearActiveContext}
                      disabled={loading}
                    >
                      {createElement(CloseOutlined)}
                    </button>
                  ) : null}
                </div>
              ) : null}

              {loading && !activePending && !hasStreamingAssistantContent && (
                <ThinkingIndicator label={loadingLabel} />
              )}
              <div ref={endRef} />
            </div>

            {activePending && (
              <div className={styles.pendingDock}>
                {loading && !hasStreamingAssistantContent ? <ThinkingIndicator label={loadingLabel} /> : null}
                {confirmError ? (
                  <div className={styles.confirmRecovery} role="alert">
                    <span>
                      {confirmError}
                      <small>
                        {lastConfirmationInputRef.current?.approved === false
                          ? '重试会继续提交拒绝；也可以在下方审核卡片修改反馈。'
                          : '重试会保留本次编辑；如需放弃，请使用下方审核卡片。'}
                      </small>
                    </span>
                    <button
                      ref={confirmRetryButtonRef}
                      type="button"
                      onClick={retryConfirmAction}
                      disabled={loading}
                    >
                      {loading
                        ? '处理中…'
                        : lastConfirmationInputRef.current?.approved === false
                          ? '重试拒绝'
                          : '重试执行'}
                    </button>
                  </div>
                ) : null}
                {confirmPhase === 'saving' && !loading ? (
                  <div className={styles.confirmRecovery} role="status">
                    <span>确认操作仍在处理中，请刷新状态后再继续。</span>
                    <button type="button" onClick={refreshConfirmationStatus}>
                      刷新状态
                    </button>
                  </div>
                ) : null}
                <ProposalCard
                  key={`${convID}:${activePending.confirmation_token}`}
                  action={activePending}
                  loading={loading || confirmPhase === 'saving'}
                  evidence={confirmationEvidence}
                  onOpenEvidence={onOpenEvidence}
                  onConfirm={approvePending}
                  onCancel={rejectPending}
                />
              </div>
            )}

            {loading && (
              <div className={styles.stopDock}>
                <Button
                  danger
                  icon={<StopOutlined />}
                  aria-label="停止当前回复"
                  onClick={() => stopActiveRequest()}
                >
                  停止当前回复
                </Button>
              </div>
            )}

            {(confirmPhase !== 'idle' || lastUndo) && (
              <div className={styles.writeStatus}>
                <span>
                  {confirmPhase === 'saving'
                    ? '正在保存'
                    : confirmPhase === 'error'
                      ? '保存失败'
                      : confirmPhase === 'success'
                        ? '保存成功'
                        : '最近一次 AI 写入可撤销'}
                </span>
                {lastUndo ? (
                  <button type="button" onClick={handleUndoLastWrite} disabled={loading}>
                    撤销最近一次 AI 写入
                  </button>
                ) : null}
              </div>
            )}

            {!hasKey && (
              <div className={styles.inlineKeyNotice}>
                <span>尚未配置 API key，配置后即可使用 Pilot 对话和工具调用。</span>
                {onOpenSettings && (
                  <button type="button" onClick={onOpenSettings}>
                    打开设置
                  </button>
                )}
              </div>
            )}
            {lastError && lastFailedText ? (
              <div className={styles.retryNotice}>
                <span>{lastError}</span>
                <button type="button" onClick={retryLastMessage} disabled={composerDisabled}>
                  重新发送
                </button>
                <button type="button" onClick={clearLastFailure}>
                  关闭提示
                </button>
              </div>
            ) : null}
            <ContextAttachmentRail
              attachments={attachments}
              disabled={composerDisabled}
              onRemove={removeAttachment}
              onNativeDrop={addAttachment}
            />
            {attachmentNotice ? <div className={styles.attachmentNotice} role="status">{attachmentNotice}</div> : null}
              {onOpenVoiceCoachingGrowth ? (
                <div style={{ display: 'flex', justifyContent: 'flex-end', minHeight: 40 }}>
                  <VoiceCoachingPilotEntry onOpen={onOpenVoiceCoachingGrowth} />
                </div>
              ) : null}
              <Composer
                capabilities={capabilities}
                disabled={composerDisabled}
                disabledReason={composerDisabledReason}
                resetKey={composerResetKey}
                suggestions={attachmentSuggestions}
                onboardingFocusToken={onboardingFocusEventToken}
                onSend={sendMessage}
              />
          </section>

          <ContextPanel
            floating={panelOpen}
            isNego={isNego}
            offer={offer}
            capabilities={capabilities}
            evidence={threadEvidence}
            autoApprove={autoApprove}
            hasKey={hasKey}
            degraded={degraded}
            disabled={composerDisabled}
            onCapability={handleCapability}
            onToggleAutoApprove={toggleAutoApprove}
            onOpenSettings={onOpenSettings}
            onOpenEvidence={onOpenEvidence}
            onPrepareOfferNegotiation={onPrepareOfferNegotiation}
            onOpenInterviewStoryLibrary={onOpenInterviewStoryLibrary}
            offers={offers}
            contextKey={contextResetKey}
          />
        </div>
      </div>
    </>
  );

  const nativeDropWorkspace = (
    <NativePilotAttachmentDropSurface disabled={composerDisabled} onNativeDrop={addAttachment}>
      {workspace}
    </NativePilotAttachmentDropSurface>
  );

  const panelWorkspace = pilotDropTarget ? (
    <PilotContextDropTarget disabled={composerDisabled} onNativeDrop={addAttachment}>
      {workspace}
    </PilotContextDropTarget>
  ) : nativeDropWorkspace;

  if (docked || inlinePage) return panelWorkspace;

  return (
    <Drawer
      placement="right"
      width={drawerWidth}
      style={{ maxWidth: '100vw' }}
      open={open}
      onClose={onClose}
      title={null}
      closable={false}
      styles={{ body: { padding: 16, height: '100%', overflow: 'hidden', position: 'relative' } }}
    >
      {panelWorkspace}
    </Drawer>
  );
}
