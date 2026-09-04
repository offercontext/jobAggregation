// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/services/questions', () => ({
  listQuestions: vi.fn().mockResolvedValue([]),
  listDueQuestions: vi.fn().mockResolvedValue([]),
  getPracticeStats: vi.fn().mockResolvedValue({ total: 0, new: 0, practicing: 0, mastered: 0, due: 0, today_reviews: 0, streak_days: 0 }),
  createQuestion: vi.fn(), deleteQuestion: vi.fn(), generateQuestions: vi.fn(), submitReview: vi.fn(), updateQuestion: vi.fn(),
}));

const { default: QuestionBankView } = await import('./QuestionBankView');

let root: Root;
let host: HTMLDivElement;

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  window.matchMedia = () => ({ matches: false, addListener: () => undefined, removeListener: () => undefined }) as unknown as MediaQueryList;
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.clearAllMocks();
});

describe('QuestionBankView question-bank and spaced-review owner', () => {
  it('keeps only the bank and today review surfaces', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    await act(async () => root.render(<QueryClientProvider client={queryClient}><QuestionBankView /></QueryClientProvider>));
    const mode = (label: string) => [...host.querySelectorAll<HTMLElement>('.ant-segmented-item')]
      .find((item) => item.textContent === label);
    expect(mode('题库')).toBeTruthy();
    expect(mode('今日复习')).toBeTruthy();
    expect(mode('复盘训练')).toBeFalsy();
    expect(mode('快速练习')).toBeFalsy();
    expect(host.querySelector('[data-testid="interview-readiness-center"]')).toBeNull();
    expect(host.querySelector('[data-testid="adaptive-practice-workspace"]')).toBeNull();

    act(() => mode('今日复习')?.click());
    expect(host.querySelector<HTMLElement>('[aria-label="今日复习模式"]')?.hidden).toBe(false);
    expect(host.querySelector<HTMLElement>('[aria-label="题库模式"]')?.hidden).toBe(true);
  });

  it('opens today review when the top-level starts a new brushing session', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    await act(async () => root.render(<QueryClientProvider client={queryClient}><QuestionBankView practiceRequestToken={1} /></QueryClientProvider>));
    expect(host.querySelector<HTMLElement>('[aria-label="今日复习模式"]')?.hidden).toBe(false);
    expect(host.querySelector<HTMLElement>('[aria-label="题库模式"]')?.hidden).toBe(true);
  });
});
