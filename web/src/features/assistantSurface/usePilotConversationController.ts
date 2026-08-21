import {
  useCallback,
  useMemo,
  useReducer,
  useRef,
  useState,
  type MutableRefObject,
} from 'react';
import type { ConfirmationInput } from '@/services/chat';
import {
  streamChat as streamChatService,
  streamConfirmAction as streamConfirmActionService,
  type ChatContextInput,
  type ChatStreamRequestOptions,
} from '@/services/chat';
import type {
  ChatStartRequest,
  ChatUndo,
  Conversation,
  PendingAction,
  PilotContextAttachment,
  PilotPageContext,
} from '@/types/chat';
import type {
  ActiveConversationRequestOwner,
  UITurn,
} from '@/components/ChatPanel/model';
import { buildChatRequestContext, pendingAutoSelectReducer } from '@/components/ChatPanel/model';
import type { AssistantTaskState } from './assistantSurfaceReducer';

export type SendMessageOutcome = 'sent' | 'stopped' | 'failed' | 'ignored';

export interface ConfirmationExecution {
  conversationId: number;
  confirmationToken: string;
}

export interface ActiveConversationRequest extends ActiveConversationRequestOwner {
  controller: AbortController;
  runId?: string;
}

export interface PilotConversationActions {
  sendMessage: (text: string) => Promise<SendMessageOutcome>;
  selectConversation: (conversationId: number) => Promise<void>;
  startNewChat: () => boolean;
  retryLastMessage: () => void;
  clearLastFailure: () => void;
  handleConfirm: (input: ConfirmationInput) => Promise<void>;
  retryConfirmAction: () => void;
  refreshConfirmationStatus: () => Promise<void>;
  clearActiveContext: () => Promise<void>;
}

interface BuildRequestContextInput {
  conversationId?: number;
  draftContext: ChatStartRequest | null;
  offerApplicationId?: number;
  offerId?: number;
  pageContext?: PilotPageContext;
  attachments: PilotContextAttachment[];
}

const unavailableActions: PilotConversationActions = {
  sendMessage: async () => 'ignored',
  selectConversation: async () => undefined,
  startNewChat: () => false,
  retryLastMessage: () => undefined,
  clearLastFailure: () => undefined,
  handleConfirm: async () => undefined,
  retryConfirmAction: () => undefined,
  refreshConfirmationStatus: async () => undefined,
  clearActiveContext: async () => undefined,
};

export function usePilotConversationControllerState() {
  const [turns, setTurns] = useState<UITurn[]>([]);
  const [conversationId, setConversationId] = useState<number>();
  const [pending, setPending] = useState<PendingAction | null>(null);
  const [loading, setLoading] = useState(false);
  const [autoApprove, setAutoApprove] = useState(false);
  const [hasKey, setHasKey] = useState(true);
  const [degraded, setDegraded] = useState(false);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [showArchived, setShowArchived] = useState(false);
  const [, dispatchPendingAutoSelect] = useReducer(pendingAutoSelectReducer, false);
  const [draftContext, setDraftContext] = useState<ChatStartRequest | null>(null);
  const [lastError, setLastError] = useState<string | null>(null);
  const [lastFailedText, setLastFailedText] = useState('');
  const [confirmError, setConfirmError] = useState<string | null>(null);
  const [confirmPhase, setConfirmPhase] = useState<'idle' | 'saving' | 'success' | 'error'>('idle');
  const [lastUndo, setLastUndo] = useState<ChatUndo | null>(null);
  const [loadingLabel, setLoadingLabel] = useState<string>();
  const [hasStreamingAssistantContent, setHasStreamingAssistantContent] = useState(false);
  const [composerResetKey, setComposerResetKey] = useState(0);
  const [followingContext, setFollowingContextState] = useState<PilotPageContext>();
  const [pinnedContext, setPinnedContext] = useState<PilotPageContext>();
  const [requestContextSnapshot, setRequestContextSnapshot] = useState<PilotPageContext>();
  const [attachments, setAttachments] = useState<PilotContextAttachment[]>([]);

  const activeRequestRef = useRef<ActiveConversationRequest | null>(null);
  const streamingAssistantActiveRef = useRef(false);
  const titleRefreshTimeoutsRef = useRef<number[]>([]);
  const lastConfirmationInputRef = useRef<ConfirmationInput | null>(null);
  const activePendingRef = useRef<PendingAction | null>(null);
  const activeConversationIdRef = useRef<number>();
  const confirmationMonitorRef = useRef(0);
  const confirmationLocksRef = useRef(new Map<number, ConfirmationExecution>());
  const confirmationReconcileOnOpenRef = useRef<ConfirmationExecution | null>(null);
  const lockedConfirmationRef = useRef<ConfirmationExecution | null>(null);
  const startedRequestKeyRef = useRef<number | null>(null);
  const pendingAutoSelectSuppressedRef = useRef(false);
  const conversationSelectionRequestRef = useRef(0);
  const activeConversationSelectionRef = useRef<number | null>(null);
  const conversationListRequestRef = useRef(0);
  const visibleRequestGenerationRef = useRef(0);
  const showArchivedRef = useRef(showArchived);
  const consumedOnboardingFocusTokenRef = useRef(0);
  const consumedConversationRequestRef = useRef(0);
  const openRef = useRef(false);
  const actionsRef = useRef<PilotConversationActions>(unavailableActions);
  const actionsOwnerRef = useRef<object | null>(null);
  const pinnedContextByConversationRef = useRef(new Map<number, PilotPageContext>());
  const stopFeedbackRef = useRef<(() => void) | null>(null);
  const taskStateReporterRef = useRef<((taskState: AssistantTaskState) => void) | null>(null);

  showArchivedRef.current = showArchived;
  activeConversationIdRef.current = conversationId;
  activePendingRef.current = pending;

  const bindActions = useCallback((owner: object, actions: PilotConversationActions) => {
    actionsOwnerRef.current = owner;
    actionsRef.current = actions;
  }, []);

  const releaseActions = useCallback((owner: object) => {
    if (actionsOwnerRef.current !== owner) return;
    actionsOwnerRef.current = null;
    actionsRef.current = unavailableActions;
  }, []);

  const setFollowingContext = useCallback((next: PilotPageContext | undefined) => {
    setFollowingContextState((current) => (
      JSON.stringify(current) === JSON.stringify(next) ? current : next
    ));
  }, []);

  const pinConversationContext = useCallback((id: number, context?: PilotPageContext) => {
    if (context) pinnedContextByConversationRef.current.set(id, context);
    else pinnedContextByConversationRef.current.delete(id);
    setPinnedContext(context);
  }, []);

  const activateConversationContext = useCallback((id?: number) => {
    setPinnedContext(id === undefined ? undefined : pinnedContextByConversationRef.current.get(id));
  }, []);

  const bindStopFeedback = useCallback((callback: (() => void) | null) => {
    stopFeedbackRef.current = callback;
  }, []);

  const bindTaskStateReporter = useCallback((callback: ((taskState: AssistantTaskState) => void) | null) => {
    taskStateReporterRef.current = callback;
  }, []);

  const beginActiveRequest = useCallback((
    kind: ActiveConversationRequest['kind'],
    conversationId?: number,
    confirmationToken?: string,
  ): ActiveConversationRequest | null => {
    if (activeRequestRef.current || activeConversationSelectionRef.current !== null) return null;
    const request: ActiveConversationRequest = {
      controller: new AbortController(),
      kind,
      conversationId,
      ...(confirmationToken ? { confirmationToken } : {}),
    };
    activeRequestRef.current = request;
    setLoading(true);
    return request;
  }, []);

  const finishActiveRequest = useCallback((request: ActiveConversationRequest): boolean => {
    if (activeRequestRef.current !== request) return false;
    activeRequestRef.current = null;
    setLoading(activeConversationSelectionRef.current !== null);
    return true;
  }, []);

  const beginConversationSelection = useCallback(() => {
    const requestId = ++conversationSelectionRequestRef.current;
    activeConversationSelectionRef.current = requestId;
    setLoading(true);
    return requestId;
  }, []);

  const finishConversationSelection = useCallback((requestId: number): boolean => {
    if (activeConversationSelectionRef.current !== requestId) return false;
    activeConversationSelectionRef.current = null;
    setLoading(activeRequestRef.current !== null);
    return true;
  }, []);

  const cancelConversationSelection = useCallback(() => {
    conversationSelectionRequestRef.current += 1;
    activeConversationSelectionRef.current = null;
    setLoading(activeRequestRef.current !== null);
  }, []);

  const ensureOwnedRequest = useCallback((request: ActiveConversationRequest) => {
    if (activeRequestRef.current === request) return;
    const error = new Error('Request ownership changed');
    error.name = 'AbortError';
    throw error;
  }, []);

  const buildRequestContext = useCallback((input: BuildRequestContextInput): ChatContextInput => {
    const base = input.conversationId === undefined && input.draftContext
      ? {
          context_type: input.draftContext.context_type,
          context_ref: input.draftContext.context_ref,
          mode: input.draftContext.mode,
          ...(input.draftContext.pilot_action
            ? { pilot_action: input.draftContext.pilot_action }
            : {}),
          ...(input.pageContext ? { page_context: input.pageContext } : {}),
        }
      : buildChatRequestContext({
          conversationId: input.conversationId,
          offerApplicationId: input.offerApplicationId,
          offerId: input.offerId,
          pageContext: input.pageContext,
        });
    return {
      ...base,
      ...(input.attachments.length ? { attachments: [...input.attachments] } : {}),
    };
  }, []);

  const streamChatRequest = useCallback((
    request: ActiveConversationRequest,
    message: string,
    activeConversationId: number | undefined,
    context: ChatContextInput,
    options: Omit<ChatStreamRequestOptions, 'signal'>,
  ) => {
    ensureOwnedRequest(request);
    return streamChatService(message, activeConversationId, context, {
      ...options,
      signal: request.controller.signal,
    });
  }, [ensureOwnedRequest]);

  const streamConfirmationRequest = useCallback((
    request: ActiveConversationRequest,
    activeConversationId: number,
    input: ConfirmationInput,
    options: Omit<ChatStreamRequestOptions, 'signal'>,
  ) => {
    ensureOwnedRequest(request);
    return streamConfirmActionService(activeConversationId, input, {
      ...options,
      signal: request.controller.signal,
    });
  }, [ensureOwnedRequest]);

  const stopActiveRequest = useCallback((options: { silent?: boolean } = {}) => {
    const activeRequest = activeRequestRef.current;
    if (!activeRequest) return false;
    activeRequest.controller.abort();
    activeRequestRef.current = null;
    setLoading(activeConversationSelectionRef.current !== null);
    taskStateReporterRef.current?.('idle');
    if (!options.silent) stopFeedbackRef.current?.();
    return true;
  }, []);

  const sendMessage = useCallback((text: string) => actionsRef.current.sendMessage(text), []);
  const selectConversation = useCallback(
    (id: number) => actionsRef.current.selectConversation(id),
    [],
  );
  const startNewChat = useCallback(() => actionsRef.current.startNewChat(), []);
  const retryLastMessage = useCallback(() => actionsRef.current.retryLastMessage(), []);
  const clearLastFailure = useCallback(() => actionsRef.current.clearLastFailure(), []);
  const retryConfirmAction = useCallback(() => actionsRef.current.retryConfirmAction(), []);
  const refreshConfirmationStatus = useCallback(
    () => actionsRef.current.refreshConfirmationStatus(),
    [],
  );
  const clearActiveContext = useCallback(() => actionsRef.current.clearActiveContext(), []);

  const approvePending = useCallback(
    (editedArgs?: Record<string, unknown>) => {
      const action = activePendingRef.current;
      if (!action) return Promise.resolve();
      return actionsRef.current.handleConfirm({
        approved: true,
        operation_id: action.operation_id,
        confirmation_token: action.confirmation_token,
        ...(editedArgs ? { edited_args: editedArgs } : {}),
      });
    },
    [],
  );

  const rejectPending = useCallback((rejectionFeedback?: string) => {
    const action = activePendingRef.current;
    if (!action) return Promise.resolve();
    return actionsRef.current.handleConfirm({
      approved: false,
      operation_id: action.operation_id,
      confirmation_token: action.confirmation_token,
      ...(rejectionFeedback ? { rejection_feedback: rejectionFeedback } : {}),
    });
  }, []);

  const taskState: AssistantTaskState = pending
    ? 'waiting_confirmation'
    : loading
      ? 'running'
      : lastError || confirmError
        ? 'failed'
        : 'idle';

  return useMemo(() => ({
    turns,
    setTurns,
    conversationId,
    setConversationId,
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
    followingContext,
    setFollowingContext,
    pinnedContext,
    pinConversationContext,
    activateConversationContext,
    requestContextSnapshot,
    setRequestContextSnapshot,
    attachments,
    setAttachments,
    taskState,
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
    activeConversationSelectionRef,
    conversationListRequestRef,
    visibleRequestGenerationRef,
    showArchivedRef,
    consumedOnboardingFocusTokenRef,
    consumedConversationRequestRef,
    openRef,
    bindActions,
    releaseActions,
    bindStopFeedback,
    bindTaskStateReporter,
    beginActiveRequest,
    beginConversationSelection,
    finishConversationSelection,
    cancelConversationSelection,
    buildRequestContext,
    finishActiveRequest,
    streamChatRequest,
    streamConfirmationRequest,
    stopActiveRequest,
    sendMessage,
    selectConversation,
    startNewChat,
    retryLastMessage,
    clearLastFailure,
    retryConfirmAction,
    refreshConfirmationStatus,
    clearActiveContext,
    approvePending,
    rejectPending,
  }), [
    autoApprove,
    activateConversationContext,
    bindActions,
    bindStopFeedback,
    bindTaskStateReporter,
    beginActiveRequest,
    beginConversationSelection,
    buildRequestContext,
    cancelConversationSelection,
    clearActiveContext,
    clearLastFailure,
    composerResetKey,
    confirmError,
    confirmPhase,
    conversationId,
    conversations,
    degraded,
    draftContext,
    followingContext,
    finishActiveRequest,
    finishConversationSelection,
    hasKey,
    hasStreamingAssistantContent,
    lastError,
    lastFailedText,
    lastUndo,
    loading,
    loadingLabel,
    pending,
    pinnedContext,
    pinConversationContext,
    requestContextSnapshot,
    refreshConfirmationStatus,
    rejectPending,
    releaseActions,
    retryConfirmAction,
    retryLastMessage,
    selectConversation,
    sendMessage,
    showArchived,
    setFollowingContext,
    startNewChat,
    stopActiveRequest,
    streamChatRequest,
    streamConfirmationRequest,
    taskState,
    turns,
    approvePending,
    attachments,
  ]);
}

export type PilotConversationController = ReturnType<typeof usePilotConversationControllerState>;

export type ControllerMutableRef<T> = MutableRefObject<T>;
