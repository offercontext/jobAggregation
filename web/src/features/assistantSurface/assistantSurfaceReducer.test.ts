import { describe, expect, it } from 'vitest';
import {
  assistantSurfaceReducer,
  initialAssistantSurfaceState,
  type AssistantSurfaceState,
} from './assistantSurfaceReducer';

describe('assistant surface reducer', () => {
  it('keeps one mutually exclusive primary conversation surface', () => {
    const haru = assistantSurfaceReducer(initialAssistantSurfaceState, { type: 'open_haru' });
    expect(haru.surface).toBe('haru_chat');

    const pilot = assistantSurfaceReducer(haru, { type: 'open_pilot' });
    expect(pilot.surface).toBe('pilot_workspace');

    const closed = assistantSurfaceReducer(pilot, { type: 'close_surface' });
    expect(closed.surface).toBe('mascot');
  });

  it('opens the full Pilot workspace for a pending confirmation without changing task ownership', () => {
    const running: AssistantSurfaceState = {
      surface: 'haru_chat',
      taskState: 'waiting_confirmation',
      completionNotice: null,
    };

    expect(assistantSurfaceReducer(running, { type: 'open_pending' })).toEqual({
      ...running,
      surface: 'pilot_workspace',
    });
  });

  it('closing presentation does not mutate an active task state', () => {
    const running: AssistantSurfaceState = {
      surface: 'haru_chat',
      taskState: 'running',
      completionNotice: null,
    };

    expect(assistantSurfaceReducer(running, { type: 'close_surface' })).toEqual({
      ...running,
      surface: 'mascot',
    });
  });

  it('records one completion notice and dismisses it explicitly', () => {
    const completed = assistantSurfaceReducer(initialAssistantSurfaceState, {
      type: 'task_state_changed',
      taskState: 'completed',
      conversationId: 42,
    });
    expect(completed.completionNotice).toEqual({ status: 'completed', conversationId: 42 });

    const repeated = assistantSurfaceReducer(completed, {
      type: 'task_state_changed',
      taskState: 'completed',
      conversationId: 42,
    });
    expect(repeated).toBe(completed);

    expect(assistantSurfaceReducer(completed, { type: 'dismiss_notice' }).completionNotice).toBeNull();
  });
});
