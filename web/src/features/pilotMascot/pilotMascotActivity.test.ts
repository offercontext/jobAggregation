import { describe, expect, it } from 'vitest';
import { derivePilotMascotActivity } from './pilotMascotActivity';

describe('derivePilotMascotActivity', () => {
  it.each([
    [{ loading: false, confirmationPhase: 'idle', hasError: false, degraded: false, hasPending: false }, 'idle'],
    [{ loading: true, confirmationPhase: 'idle', hasError: false, degraded: false, hasPending: false }, 'thinking'],
    [{ loading: false, confirmationPhase: 'saving', hasError: false, degraded: false, hasPending: false }, 'thinking'],
    [{ loading: false, confirmationPhase: 'success', hasError: false, degraded: false, hasPending: false }, 'success'],
    [{ loading: false, confirmationPhase: 'idle', hasError: true, degraded: false, hasPending: false }, 'error'],
    [{ loading: false, confirmationPhase: 'idle', hasError: false, degraded: true, hasPending: false }, 'error'],
    [{ loading: false, confirmationPhase: 'idle', hasError: false, degraded: false, hasPending: true }, 'waiting_confirmation'],
  ] as const)('maps real Pilot facts to %s', (facts, expected) => {
    expect(derivePilotMascotActivity(facts)).toBe(expected);
  });
});
