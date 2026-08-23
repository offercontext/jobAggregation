// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  PILOT_MASCOT_ZOOM_KEY,
  PILOT_MASCOT_VISIBILITY_KEY,
  readPilotMascotZoom,
  readPilotMascotVisible,
  writePilotMascotZoom,
  writePilotMascotVisible,
  PILOT_MASCOT_ANIMATION_KEY,
  readPilotMascotAnimationLevel,
  writePilotMascotAnimationLevel,
} from './pilotMascotPreference';
import {
  PILOT_MASCOT_POSITION_KEY,
  DEFAULT_PILOT_MASCOT_POSITIONS,
  readPilotMascotPositions,
  resetPilotMascotPosition,
  writePilotMascotPosition,
} from './pilotMascotPreference';

describe('pilot mascot preference', () => {
  beforeEach(() => localStorage.clear());

  it('defaults to visible and persists explicit choices', () => {
    expect(readPilotMascotVisible()).toBe(true);
    writePilotMascotVisible(false);
    expect(localStorage.getItem(PILOT_MASCOT_VISIBILITY_KEY)).toBe('false');
    expect(readPilotMascotVisible()).toBe(false);
    writePilotMascotVisible(true);
    expect(readPilotMascotVisible()).toBe(true);
  });

  it('fails open when storage is unavailable', () => {
    const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    expect(readPilotMascotVisible()).toBe(true);
    getItem.mockRestore();
  });

  it('defaults zoom to 100% and persists clamped ten-percent steps', () => {
    expect(readPilotMascotZoom()).toBe(1);
    writePilotMascotZoom(1.24);
    expect(localStorage.getItem(PILOT_MASCOT_ZOOM_KEY)).toBe('1.2');
    expect(readPilotMascotZoom()).toBe(1.2);
    writePilotMascotZoom(9);
    expect(readPilotMascotZoom()).toBe(1.3);
    writePilotMascotZoom(-9);
    expect(readPilotMascotZoom()).toBe(0.8);
  });

  it('rejects malformed and non-finite zoom values without breaking Pilot', () => {
    localStorage.setItem(PILOT_MASCOT_ZOOM_KEY, 'not-a-number');
    expect(readPilotMascotZoom()).toBe(1);
    localStorage.setItem(PILOT_MASCOT_ZOOM_KEY, 'Infinity');
    expect(readPilotMascotZoom()).toBe(1);
    expect(() => writePilotMascotZoom(Number.NaN)).not.toThrow();
    expect(readPilotMascotZoom()).toBe(1);

    const blocked = {
      getItem: vi.fn(() => { throw new Error('blocked'); }),
      setItem: vi.fn(() => { throw new Error('blocked'); }),
    };
    expect(readPilotMascotZoom(blocked)).toBe(1);
    expect(() => writePilotMascotZoom(1.2, blocked)).not.toThrow();
  });

  it('persists independent normalized positions and resets one placement safely', () => {
    expect(readPilotMascotPositions()).toEqual(DEFAULT_PILOT_MASCOT_POSITIONS);
    writePilotMascotPosition('normal', { xRatio: 0.2, yRatio: 0.35 });
    writePilotMascotPosition('interview_studio', { xRatio: 2, yRatio: Number.NaN });
    const stored = readPilotMascotPositions();
    expect(stored.normal).toEqual({ xRatio: 0.2, yRatio: 0.35 });
    expect(stored.interview_studio).toEqual(DEFAULT_PILOT_MASCOT_POSITIONS.interview_studio);
    expect(localStorage.getItem(PILOT_MASCOT_POSITION_KEY)).toContain('interview_studio');
    resetPilotMascotPosition('normal');
    expect(readPilotMascotPositions().normal).toEqual(DEFAULT_PILOT_MASCOT_POSITIONS.normal);
  });

  it('fails closed to safe defaults for malformed versioned position data', () => {
    localStorage.setItem(PILOT_MASCOT_POSITION_KEY, JSON.stringify({ version: 2, normal: { xRatio: 0, yRatio: 0 } }));
    expect(readPilotMascotPositions()).toEqual(DEFAULT_PILOT_MASCOT_POSITIONS);
    localStorage.setItem(PILOT_MASCOT_POSITION_KEY, '{not-json');
    expect(readPilotMascotPositions()).toEqual(DEFAULT_PILOT_MASCOT_POSITIONS);
  });

  it('persists supported animation levels and falls back for old or illegal values', () => {
    expect(readPilotMascotAnimationLevel()).toBe('full');
    writePilotMascotAnimationLevel('minimal');
    expect(localStorage.getItem(PILOT_MASCOT_ANIMATION_KEY)).toBe('minimal');
    expect(readPilotMascotAnimationLevel()).toBe('minimal');
    writePilotMascotAnimationLevel('off');
    expect(readPilotMascotAnimationLevel()).toBe('off');
    localStorage.setItem(PILOT_MASCOT_ANIMATION_KEY, 'legacy');
    expect(readPilotMascotAnimationLevel()).toBe('full');
  });
});
