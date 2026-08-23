export interface HaruRect {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

interface HaruSize {
  width: number;
  height: number;
}

export interface HaruWindowPositionInput {
  anchor: HaruRect;
  viewport: HaruSize;
  surface: HaruSize;
  bounds?: Partial<HaruRect>;
}

export interface HaruWindowPosition {
  left: number;
  top: number;
  direction: 'left-up' | 'left-down' | 'right-up' | 'right-down';
}

export const HARU_WINDOW_EDGE_GAP = 12;
const HARU_WINDOW_ANCHOR_GAP = 12;

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(Math.max(value, minimum), Math.max(minimum, maximum));
}

export function positionHaruWindow({
  anchor,
  viewport,
  surface,
  bounds,
}: HaruWindowPositionInput): HaruWindowPosition {
  const minimumLeft = Math.max(HARU_WINDOW_EDGE_GAP, bounds?.left ?? HARU_WINDOW_EDGE_GAP);
  const minimumTop = Math.max(HARU_WINDOW_EDGE_GAP, bounds?.top ?? HARU_WINDOW_EDGE_GAP);
  const maximumRight = Math.min(
    viewport.width - HARU_WINDOW_EDGE_GAP,
    bounds?.right ?? viewport.width - HARU_WINDOW_EDGE_GAP,
  );
  const maximumBottom = Math.min(
    viewport.height - HARU_WINDOW_EDGE_GAP,
    bounds?.bottom ?? viewport.height - HARU_WINDOW_EDGE_GAP,
  );
  const maxLeft = maximumRight - surface.width;
  const maxTop = maximumBottom - surface.height;
  const anchorCenter = (anchor.left + anchor.right) / 2;
  const preferLeft = anchorCenter >= viewport.width / 2;
  const leftCandidate = anchor.left - HARU_WINDOW_ANCHOR_GAP - surface.width;
  const rightCandidate = anchor.right + HARU_WINDOW_ANCHOR_GAP;
  const leftFits = leftCandidate >= minimumLeft;
  const rightFits = rightCandidate <= maxLeft;

  const horizontal: 'left' | 'right' = preferLeft
    ? leftFits || !rightFits ? 'left' : 'right'
    : rightFits || !leftFits ? 'right' : 'left';
  const left = clamp(
    horizontal === 'left' ? leftCandidate : rightCandidate,
    minimumLeft,
    maxLeft,
  );

  const upwardTop = anchor.bottom - surface.height;
  const downwardTop = anchor.bottom + HARU_WINDOW_ANCHOR_GAP;
  const upwardFits = upwardTop >= minimumTop;
  const vertical: 'up' | 'down' = upwardFits ? 'up' : 'down';
  const top = clamp(
    vertical === 'up' ? upwardTop : downwardTop,
    minimumTop,
    maxTop,
  );

  return {
    left,
    top,
    direction: `${horizontal}-${vertical}`,
  };
}
