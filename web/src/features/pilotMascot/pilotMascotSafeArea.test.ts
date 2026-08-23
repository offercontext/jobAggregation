import { describe, expect, it } from 'vitest';
import { positionPilotMascotOutsideSafeAreas, type PilotMascotRect } from './pilotMascotPreference';

describe('positionPilotMascotOutsideSafeAreas', () => {
  it('moves a Studio mascot away from the evidence rail and keeps it in the viewport', () => {
    const viewport = { width: 1440, height: 900 };
    const frame = { width: 238, height: 370 };
    const evidenceRail: PilotMascotRect = { left: 1110, top: 90, right: 1440, bottom: 660 };
    const next = positionPilotMascotOutsideSafeAreas({ xRatio: 0.9, yRatio: 0.5 }, viewport, frame, [evidenceRail]);
    const left = next.xRatio * viewport.width - frame.width / 2;
    const top = next.yRatio * viewport.height - frame.height / 2;

    expect(left).toBeGreaterThanOrEqual(12);
    expect(top).toBeGreaterThanOrEqual(12);
    expect(left + frame.width).toBeLessThanOrEqual(viewport.width - 12);
    expect(top + frame.height).toBeLessThanOrEqual(viewport.height - 12);
    expect(left >= evidenceRail.left && top < evidenceRail.bottom && left + frame.width > evidenceRail.left).toBe(false);
  });

  it('combines horizontal and vertical safe areas instead of resolving only one collision', () => {
    const viewport = { width: 1280, height: 800 };
    const frame = { width: 116, height: 174 };
    const evidenceRail: PilotMascotRect = { left: 918, top: 119, right: 1248, bottom: 781 };
    const composer: PilotMascotRect = { left: 0, top: 346, right: 1280, bottom: 720 };
    const next = positionPilotMascotOutsideSafeAreas({ xRatio: 0.72, yRatio: 0.46 }, viewport, frame, [evidenceRail, composer]);
    const left = next.xRatio * viewport.width - frame.width / 2;
    const top = next.yRatio * viewport.height - frame.height / 2;
    const mascot = { left, top, right: left + frame.width, bottom: top + frame.height };

    expect(mascot.right).toBeLessThanOrEqual(evidenceRail.left);
    expect(mascot.bottom).toBeLessThanOrEqual(composer.top);
  });
});
