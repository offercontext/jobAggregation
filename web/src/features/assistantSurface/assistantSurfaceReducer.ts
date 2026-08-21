export type AssistantSurface = 'mascot' | 'haru_chat' | 'pilot_workspace';

export type AssistantTaskState =
  | 'idle'
  | 'running'
  | 'waiting_confirmation'
  | 'completed'
  | 'failed';

export interface AssistantCompletionNotice {
  status: Extract<AssistantTaskState, 'completed' | 'failed'>;
  conversationId: number;
}
export interface AssistantSurfaceState {
  surface: AssistantSurface;
  taskState: AssistantTaskState;
  completionNotice: AssistantCompletionNotice | null;
}

export type AssistantSurfaceAction =
  | { type: 'open_haru' }
  | { type: 'open_pilot' }
  | { type: 'open_pending' }
  | { type: 'close_surface' }
  | {
      type: 'task_state_changed';
      taskState: AssistantTaskState;
      conversationId?: number;
    }
  | { type: 'dismiss_notice' };

export const initialAssistantSurfaceState: AssistantSurfaceState = {
  surface: 'mascot',
  taskState: 'idle',
  completionNotice: null,
};

export function assistantSurfaceReducer(
  state: AssistantSurfaceState,
  action: AssistantSurfaceAction,
): AssistantSurfaceState {
  switch (action.type) {
    case 'open_haru':
      return state.surface === 'haru_chat' ? state : { ...state, surface: 'haru_chat' };
    case 'open_pilot':
    case 'open_pending':
      return state.surface === 'pilot_workspace'
        ? state
        : { ...state, surface: 'pilot_workspace' };
    case 'close_surface':
      return state.surface === 'mascot' ? state : { ...state, surface: 'mascot' };
    case 'task_state_changed': {
      const notice =
        (action.taskState === 'completed' || action.taskState === 'failed') &&
        action.conversationId !== undefined
          ? { status: action.taskState, conversationId: action.conversationId }
          : null;
      if (
        state.taskState === action.taskState &&
        state.completionNotice?.status === notice?.status &&
        state.completionNotice?.conversationId === notice?.conversationId
      ) {
        return state;
      }
      return { ...state, taskState: action.taskState, completionNotice: notice };
    }
    case 'dismiss_notice':
      return state.completionNotice === null ? state : { ...state, completionNotice: null };
  }
}
