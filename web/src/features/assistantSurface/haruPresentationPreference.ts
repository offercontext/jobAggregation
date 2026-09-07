export const HARU_PRESENTATION_KEY = 'offerpilot.haru.presentationMode';
export type HaruPresentationMode = 'compact' | 'character';
export function readHaruPresentation(storage?: Pick<Storage, 'getItem'>): HaruPresentationMode {
  try { return (storage ?? window.localStorage).getItem(HARU_PRESENTATION_KEY) === 'character' ? 'character' : 'compact'; } catch { return 'compact'; }
}
export function writeHaruPresentation(mode: HaruPresentationMode, storage?: Pick<Storage, 'setItem'>): void {
  try { (storage ?? window.localStorage).setItem(HARU_PRESENTATION_KEY, mode); } catch { /* Session preference still applies if storage is blocked. */ }
}
export function effectiveHaruPresentation(mode: HaruPresentationMode, width: number, height: number): HaruPresentationMode {
  return width >= 1280 && height >= 760 ? mode : 'compact';
}
