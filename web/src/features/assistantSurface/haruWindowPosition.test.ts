import { describe, expect, it } from 'vitest';
import { positionHaruWindow } from './haruWindowPosition';

const surface = { width: 392, height: 620 };

describe('positionHaruWindow', () => {
  it('opens left and upward from a right-side anchor', () => {
    const position = positionHaruWindow({
      anchor: { left: 1290, top: 650, right: 1406, bottom: 824 },
      viewport: { width: 1440, height: 900 },
      surface,
    });
    expect(position.direction).toBe('left-up');
    expect(position.left + surface.width + 12).toBeLessThanOrEqual(1290);
    expect(position.top).toBeLessThan(650);
  });

  it('opens right and upward from a left-side anchor', () => {
    const position = positionHaruWindow({
      anchor: { left: 24, top: 650, right: 140, bottom: 824 },
      viewport: { width: 1024, height: 900 },
      surface,
    });
    expect(position.direction).toBe('right-up');
    expect(position.left).toBeGreaterThanOrEqual(140 + 12);
    expect(position.top).toBeLessThan(650);
  });

  it('opens downward when there is not enough space above the anchor', () => {
    const position = positionHaruWindow({
      anchor: { left: 884, top: 24, right: 1000, bottom: 198 },
      viewport: { width: 1024, height: 900 },
      surface,
    });
    expect(position.direction).toBe('left-down');
    expect(position.top).toBeGreaterThanOrEqual(198 + 12);
  });

  it.each([768, 1024, 1440])('stays at least 12px inside a %ipx viewport', (width) => {
    const position = positionHaruWindow({
      anchor: { left: width - 18, top: 2, right: width + 30, bottom: 176 },
      viewport: { width, height: 900 },
      surface,
    });
    expect(position.left).toBeGreaterThanOrEqual(12);
    expect(position.top).toBeGreaterThanOrEqual(12);
    expect(position.left + surface.width).toBeLessThanOrEqual(width - 12);
    expect(position.top + surface.height).toBeLessThanOrEqual(900 - 12);
  });

  it('honors desktop navigation and scrollbar-safe content bounds', () => {
    const navigationSafe = positionHaruWindow({
      anchor: { left: 24, top: 650, right: 140, bottom: 824 },
      viewport: { width: 1024, height: 900 },
      surface,
      bounds: { left: 212, right: 800 },
    });
    const scrollbarSafe = positionHaruWindow({
      anchor: { left: 300, top: 650, right: 416, bottom: 824 },
      viewport: { width: 1024, height: 900 },
      surface,
      bounds: { left: 212, right: 800 },
    });
    expect(navigationSafe.left).toBeGreaterThanOrEqual(212);
    expect(scrollbarSafe.left + surface.width).toBeLessThanOrEqual(800);
  });
});
