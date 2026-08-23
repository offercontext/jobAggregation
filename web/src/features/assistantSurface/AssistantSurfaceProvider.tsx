import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useReducer,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import {
  assistantSurfaceReducer,
  initialAssistantSurfaceState,
  type AssistantTaskState,
} from './assistantSurfaceReducer';
import {
  usePilotConversationControllerState,
  type PilotConversationController,
} from './usePilotConversationController';

export interface AssistantReplyLifecycleEvent {
  status: 'success' | 'error';
  conversationId: number;
  background: boolean;
}

export interface AssistantConversationRequest {
  requestKey: number;
  conversationId: number;
}

interface AssistantSurfaceContextValue {
  surface: 'mascot' | 'haru_chat' | 'pilot_workspace';
  taskState: AssistantTaskState;
  completionNotice: { status: 'completed' | 'failed'; conversationId: number } | null;
  conversationRequest?: AssistantConversationRequest;
  openHaru: () => void;
  openPilot: () => void;
  openPending: () => void;
  openConversation: (conversationId: number) => void;
  openCompletionNotice: () => void;
  consumeConversationRequest: (requestKey: number) => void;
  closeSurface: () => void;
  dismissNotice: () => void;
  reportTaskState: (taskState: AssistantTaskState, conversationId?: number) => void;
  reportReplyLifecycle: (event: AssistantReplyLifecycleEvent) => void;
}

const AssistantSurfaceContext = createContext<AssistantSurfaceContextValue | null>(null);
const PilotConversationContext = createContext<PilotConversationController | null>(null);

export function AssistantSurfaceProvider({ children }: { children: ReactNode }) {
  const controller = usePilotConversationControllerState();
  const [state, dispatch] = useReducer(assistantSurfaceReducer, initialAssistantSurfaceState);
  const [conversationRequest, setConversationRequest] = useState<AssistantConversationRequest>();
  const nextConversationRequestKeyRef = useRef(0);
  const requestGenerationRef = useRef(0);
  const activeRequestRef = useRef<{
    generation: number;
    conversationId?: number;
    terminal: boolean;
  } | null>(null);
  const reportedLifecycleGenerationRef = useRef<number | null>(null);
  const openHaru = useCallback(() => dispatch({ type: 'open_haru' }), []);
  const openPilot = useCallback(() => dispatch({ type: 'open_pilot' }), []);
  const openPending = useCallback(() => dispatch({ type: 'open_pending' }), []);
  const openConversation = useCallback((conversationId: number) => {
    setConversationRequest({
      requestKey: ++nextConversationRequestKeyRef.current,
      conversationId,
    });
    dispatch({ type: 'open_haru' });
  }, []);
  const openCompletionNotice = useCallback(() => {
    const notice = state.completionNotice;
    if (!notice) return;
    openConversation(notice.conversationId);
    dispatch({ type: 'dismiss_notice' });
  }, [openConversation, state.completionNotice]);
  const consumeConversationRequest = useCallback((requestKey: number) => {
    setConversationRequest((current) => (
      current?.requestKey === requestKey ? undefined : current
    ));
  }, []);
  const closeSurface = useCallback(() => dispatch({ type: 'close_surface' }), []);
  const dismissNotice = useCallback(() => dispatch({ type: 'dismiss_notice' }), []);
  const reportTaskState = useCallback((taskState: AssistantTaskState, conversationId?: number) => {
    if (taskState === 'running') {
      const active = activeRequestRef.current;
      if (!active || active.conversationId !== conversationId || active.terminal) {
        activeRequestRef.current = {
          generation: ++requestGenerationRef.current,
          conversationId,
          terminal: false,
        };
      }
    } else if (taskState === 'completed' || taskState === 'failed') {
      const active = activeRequestRef.current;
      let alreadyReported = false;
      if (!active || active.conversationId !== conversationId) {
        activeRequestRef.current = {
          generation: ++requestGenerationRef.current,
          conversationId,
          terminal: true,
        };
      } else {
        alreadyReported = reportedLifecycleGenerationRef.current === active.generation;
        active.terminal = true;
      }
      reportedLifecycleGenerationRef.current = activeRequestRef.current?.generation ?? null;
      dispatch({
        type: 'task_state_changed',
        taskState,
        conversationId,
        notify: !alreadyReported,
      });
      return;
    } else if (taskState === 'idle') {
      const active = activeRequestRef.current;
      if (active) {
        active.terminal = true;
        reportedLifecycleGenerationRef.current = active.generation;
      }
    }
    dispatch({ type: 'task_state_changed', taskState, conversationId });
  }, []);
  const reportReplyLifecycle = useCallback((event: AssistantReplyLifecycleEvent) => {
    const active = activeRequestRef.current;
    const current = active?.conversationId === event.conversationId ? active : null;
    const requestGeneration = current?.generation ?? ++requestGenerationRef.current;
    if (reportedLifecycleGenerationRef.current === requestGeneration) return;

    if (current) {
      current.terminal = true;
    } else {
      activeRequestRef.current = {
        generation: requestGeneration,
        conversationId: event.conversationId,
        terminal: true,
      };
    }
    reportedLifecycleGenerationRef.current = requestGeneration;
    dispatch({
      type: 'task_state_changed',
      taskState: event.status === 'success' ? 'completed' : 'failed',
      conversationId: event.conversationId,
      notify: event.background,
    });
  }, []);
  controller.bindTaskStateReporter(reportTaskState);
  const surfaceValue = useMemo(() => ({
    ...state,
    conversationRequest,
    openHaru,
    openPilot,
    openPending,
    openConversation,
    openCompletionNotice,
    consumeConversationRequest,
    closeSurface,
    dismissNotice,
    reportTaskState,
    reportReplyLifecycle,
  }), [
    closeSurface,
    consumeConversationRequest,
    conversationRequest,
    dismissNotice,
    openCompletionNotice,
    openConversation,
    openHaru,
    openPending,
    openPilot,
    reportReplyLifecycle,
    reportTaskState,
    state,
  ]);

  return (
    <PilotConversationContext.Provider value={controller}>
      <AssistantSurfaceContext.Provider value={surfaceValue}>
        {children}
      </AssistantSurfaceContext.Provider>
    </PilotConversationContext.Provider>
  );
}

export function useAssistantSurface(): AssistantSurfaceContextValue {
  const value = useContext(AssistantSurfaceContext);
  if (!value) throw new Error('useAssistantSurface must be used inside AssistantSurfaceProvider');
  return value;
}

export function usePilotConversationController(): PilotConversationController {
  const value = useContext(PilotConversationContext);
  if (!value) {
    throw new Error('usePilotConversationController must be used inside AssistantSurfaceProvider');
  }
  return value;
}

export function useHasAssistantSurfaceProvider(): boolean {
  return useContext(PilotConversationContext) !== null;
}
