export const PILOT_MASCOT_VISIBILITY_KEY = 'offerpilot:pilot-mascot-visible';
export const PILOT_MASCOT_ZOOM_KEY = 'offerpilot:pilot-mascot-zoom';
export const PILOT_MASCOT_MIN_ZOOM = 0.8;
export const PILOT_MASCOT_MAX_ZOOM = 1.3;
export const PILOT_MASCOT_POSITION_KEY = 'offerpilot:pilot-mascot-position';
export const PILOT_MASCOT_ANIMATION_KEY = 'offerpilot:pilot-mascot-animation';

export type PilotMascotAnimationLevel = 'full' | 'minimal' | 'off';

export type PilotMascotPlacement = 'normal' | 'interview_studio';
export interface PilotMascotPosition { xRatio: number; yRatio: number }
export interface PilotMascotPositions { version: 1; normal: PilotMascotPosition; interview_studio: PilotMascotPosition }
export interface PilotMascotRect { left: number; top: number; right: number; bottom: number }

interface PilotMascotViewport { width: number; height: number }
interface PilotMascotFrame { width: number; height: number }

const PILOT_MASCOT_EDGE_GAP = 12;

export const DEFAULT_PILOT_MASCOT_POSITIONS: PilotMascotPositions = {
  version: 1,
  normal: { xRatio: 0.96, yRatio: 0.9 },
  interview_studio: { xRatio: 0.72, yRatio: 0.46 },
};

function boundedPosition(position: PilotMascotPosition, viewport: PilotMascotViewport, frame: PilotMascotFrame): PilotMascotPosition {
  const minX = PILOT_MASCOT_EDGE_GAP + frame.width / 2;
  const maxX = Math.max(minX, viewport.width - PILOT_MASCOT_EDGE_GAP - frame.width / 2);
  const minY = PILOT_MASCOT_EDGE_GAP + frame.height / 2;
  const maxY = Math.max(minY, viewport.height - PILOT_MASCOT_EDGE_GAP - frame.height / 2);
  return {
    xRatio: Math.min(0.98, Math.max(0.02, Math.min(maxX, Math.max(minX, position.xRatio * viewport.width)) / viewport.width)),
    yRatio: Math.min(0.98, Math.max(0.02, Math.min(maxY, Math.max(minY, position.yRatio * viewport.height)) / viewport.height)),
  };
}

function mascotRect(position: PilotMascotPosition, viewport: PilotMascotViewport, frame: PilotMascotFrame): PilotMascotRect {
  const left = position.xRatio * viewport.width - frame.width / 2;
  const top = position.yRatio * viewport.height - frame.height / 2;
  return { left, top, right: left + frame.width, bottom: top + frame.height };
}

function intersects(first: PilotMascotRect, second: PilotMascotRect): boolean {
  return first.left < second.right && first.right > second.left && first.top < second.bottom && first.bottom > second.top;
}

export function positionPilotMascotOutsideSafeAreas(
  position: PilotMascotPosition,
  viewport: PilotMascotViewport,
  frame: PilotMascotFrame,
  safeAreas: PilotMascotRect[],
): PilotMascotPosition {
  const bounded = boundedPosition(position, viewport, frame);
  if (!safeAreas.length || !safeAreas.some((area) => intersects(mascotRect(bounded, viewport, frame), area))) return bounded;

  const current = { x: bounded.xRatio * viewport.width, y: bounded.yRatio * viewport.height };
  const gap = 16;
  const xCandidates = [current.x];
  const yCandidates = [current.y];
  for (const area of safeAreas) {
    xCandidates.push(area.left - gap - frame.width / 2, area.right + gap + frame.width / 2);
    yCandidates.push(area.top - gap - frame.height / 2, area.bottom + gap + frame.height / 2);
  }
  const candidates = xCandidates.flatMap((x) => yCandidates.map((y) => boundedPosition({ xRatio: x / viewport.width, yRatio: y / viewport.height }, viewport, frame)));
  const valid = candidates.filter((candidate) => !safeAreas.some((area) => intersects(mascotRect(candidate, viewport, frame), area)));
  if (!valid.length) return bounded;
  return valid.sort((first, second) => {
    const firstDistance = Math.hypot(first.xRatio * viewport.width - current.x, first.yRatio * viewport.height - current.y);
    const secondDistance = Math.hypot(second.xRatio * viewport.width - current.x, second.yRatio * viewport.height - current.y);
    return firstDistance - secondDistance;
  })[0];
}

function normalizeRatio(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.min(0.98, Math.max(0.02, value));
}

function normalizePosition(value: unknown, fallback: PilotMascotPosition): PilotMascotPosition {
  if (!value || typeof value !== 'object') return fallback;
  const candidate = value as { xRatio?: unknown; yRatio?: unknown };
  if (typeof candidate.xRatio !== 'number' || typeof candidate.yRatio !== 'number' || !Number.isFinite(candidate.xRatio) || !Number.isFinite(candidate.yRatio)) return fallback;
  return { xRatio: normalizeRatio(candidate.xRatio), yRatio: normalizeRatio(candidate.yRatio) };
}

export function readPilotMascotPositions(storage?: Pick<Storage, 'getItem'>): PilotMascotPositions {
  try {
    const raw = (storage ?? window.localStorage).getItem(PILOT_MASCOT_POSITION_KEY);
    if (!raw) return DEFAULT_PILOT_MASCOT_POSITIONS;
    const parsed = JSON.parse(raw) as { version?: unknown; normal?: unknown; interview_studio?: unknown };
    if (parsed.version !== 1) return DEFAULT_PILOT_MASCOT_POSITIONS;
    return {
      version: 1,
      normal: normalizePosition(parsed.normal, DEFAULT_PILOT_MASCOT_POSITIONS.normal),
      interview_studio: normalizePosition(parsed.interview_studio, DEFAULT_PILOT_MASCOT_POSITIONS.interview_studio),
    };
  } catch {
    return DEFAULT_PILOT_MASCOT_POSITIONS;
  }
}

export function writePilotMascotPosition(
  placement: PilotMascotPlacement,
  position: PilotMascotPosition,
  storage?: Pick<Storage, 'getItem' | 'setItem'>,
): void {
  if (!Number.isFinite(position.xRatio) || !Number.isFinite(position.yRatio)) return;
  try {
    const current = readPilotMascotPositions(storage);
    const next = {
      ...current,
      [placement]: normalizePosition(position, current[placement]),
    } as PilotMascotPositions;
    (storage ?? window.localStorage).setItem(PILOT_MASCOT_POSITION_KEY, JSON.stringify(next));
  } catch {
    // Position persistence is an enhancement and must not block the Studio.
  }
}

export function resetPilotMascotPosition(
  placement: PilotMascotPlacement,
  storage?: Pick<Storage, 'getItem' | 'setItem'>,
): void {
  writePilotMascotPosition(placement, DEFAULT_PILOT_MASCOT_POSITIONS[placement], storage);
}

export function normalizePilotMascotZoom(value: number): number {
  if (!Number.isFinite(value)) return 1;
  const stepped = Math.round(value * 10) / 10;
  return Math.min(PILOT_MASCOT_MAX_ZOOM, Math.max(PILOT_MASCOT_MIN_ZOOM, stepped));
}

export function readPilotMascotVisible(storage?: Pick<Storage, 'getItem'>): boolean {
  try {
    const target = storage ?? window.localStorage;
    return target.getItem(PILOT_MASCOT_VISIBILITY_KEY) !== 'false';
  } catch {
    return true;
  }
}

export function writePilotMascotVisible(
  visible: boolean,
  storage?: Pick<Storage, 'setItem'>,
): void {
  try {
    const target = storage ?? window.localStorage;
    target.setItem(PILOT_MASCOT_VISIBILITY_KEY, String(visible));
  } catch {
    // A blocked storage backend must not affect Pilot availability.
  }
}

export function readPilotMascotZoom(storage?: Pick<Storage, 'getItem'>): number {
  try {
    const target = storage ?? window.localStorage;
    const stored = target.getItem(PILOT_MASCOT_ZOOM_KEY);
    if (stored === null || stored.trim() === '') return 1;
    const parsed = Number(stored);
    return Number.isFinite(parsed) ? normalizePilotMascotZoom(parsed) : 1;
  } catch {
    return 1;
  }
}

export function writePilotMascotZoom(
  zoom: number,
  storage?: Pick<Storage, 'setItem'>,
): void {
  if (!Number.isFinite(zoom)) return;
  try {
    const target = storage ?? window.localStorage;
    target.setItem(PILOT_MASCOT_ZOOM_KEY, String(normalizePilotMascotZoom(zoom)));
  } catch {
    // A blocked storage backend must not affect Pilot availability.
  }
}

export function readPilotMascotAnimationLevel(
  storage?: Pick<Storage, 'getItem'>,
): PilotMascotAnimationLevel {
  try {
    const value = (storage ?? window.localStorage).getItem(PILOT_MASCOT_ANIMATION_KEY);
    return value === 'minimal' || value === 'off' || value === 'full' ? value : 'full';
  } catch {
    return 'full';
  }
}

export function writePilotMascotAnimationLevel(
  level: PilotMascotAnimationLevel,
  storage?: Pick<Storage, 'setItem'>,
): void {
  if (level !== 'full' && level !== 'minimal' && level !== 'off') return;
  try {
    (storage ?? window.localStorage).setItem(PILOT_MASCOT_ANIMATION_KEY, level);
  } catch {
    // A blocked storage backend must not affect Haru availability.
  }
}
