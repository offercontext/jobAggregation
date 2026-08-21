import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useReducer,
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

interface AssistantSurfaceContextValue {
  surface: 'mascot' | 'haru_chat' | 'pilot_workspace';
  taskState: AssistantTaskState;
  completionNotice: { status: 'completed' | 'failed'; conversationId: number } | null;
  openHaru: () => void;
  openPilot: () => void;
  openPending: () => void;
  closeSurface: () => void;
  dismissNotice: () => void;
  reportTaskState: (taskState: AssistantTaskState, conversationId?: number) => void;
}

const AssistantSurfaceContext = createContext<AssistantSurfaceContextValue | null>(null);
const PilotConversationContext = createContext<PilotConversationController | null>(null);

export function AssistantSurfaceProvider({ children }: { children: ReactNode }) {
  const controller = usePilotConversationControllerState();
  const [state, dispatch] = useReducer(assistantSurfaceReducer, initialAssistantSurfaceState);
  const openHaru = useCallback(() => dispatch({ type: 'open_haru' }), []);
  const openPilot = useCallback(() => dispatch({ type: 'open_pilot' }), []);
  const openPending = useCallback(() => dispatch({ type: 'open_pending' }), []);
  const closeSurface = useCallback(() => dispatch({ type: 'close_surface' }), []);
  const dismissNotice = useCallback(() => dispatch({ type: 'dismiss_notice' }), []);
  const reportTaskState = useCallback((taskState: AssistantTaskState, conversationId?: number) => {
    dispatch({ type: 'task_state_changed', taskState, conversationId });
  }, []);
  controller.bindTaskStateReporter(reportTaskState);
  const surfaceValue = useMemo(() => ({
    ...state,
    openHaru,
    openPilot,
    openPending,
    closeSurface,
    dismissNotice,
    reportTaskState,
  }), [closeSurface, dismissNotice, openHaru, openPending, openPilot, reportTaskState, state]);

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
