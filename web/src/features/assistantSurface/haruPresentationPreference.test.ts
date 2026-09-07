import { expect, it, vi } from 'vitest';
import { readHaruPresentation, writeHaruPresentation, effectiveHaruPresentation, HARU_PRESENTATION_KEY } from './haruPresentationPreference';
it('defaults to compact, validates storage and survives blocked storage', () => {
  expect(readHaruPresentation({ getItem: () => null })).toBe('compact');
  expect(readHaruPresentation({ getItem: () => 'character' })).toBe('character');
  expect(readHaruPresentation({ getItem: () => 'invalid' })).toBe('compact');
  expect(readHaruPresentation({ getItem: () => { throw new Error(); } })).toBe('compact');
  expect(() => writeHaruPresentation('character', { setItem: () => { throw new Error(); } })).not.toThrow();
});
it('temporarily degrades without writing over the saved preference', () => {
  const setItem = vi.fn(); writeHaruPresentation('character', { setItem });
  expect(setItem).toHaveBeenCalledWith(HARU_PRESENTATION_KEY, 'character');
  expect(effectiveHaruPresentation('character', 1200, 900)).toBe('compact');
  expect(effectiveHaruPresentation('character', 1792, 720)).toBe('compact');
  expect(effectiveHaruPresentation('character', 1792, 853)).toBe('character');
  expect(setItem).toHaveBeenCalledTimes(1);
});
