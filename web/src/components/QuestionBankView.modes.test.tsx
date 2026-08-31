// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./AdaptiveInterviewPracticeWorkspace', () => ({
  default: ({ focus }: { focus?: { signalVersionId: number; targetEventId: number } }) => <div
    data-testid="review-feedback-mode"
    data-signal-version={focus?.signalVersionId ?? 'ordinary'}
    data-target-event={focus?.targetEventId ?? 'ordinary'}
  >复盘训练内容</div>,
}));
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

describe('QuestionBankView three-mode free-practice owner', () => {
  it('interactively keeps review feedback, question bank and quick practice in one owner', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    await act(async () => root.render(<QueryClientProvider client={queryClient}><QuestionBankView /></QueryClientProvider>));
    const mode = (label: string) => [...host.querySelectorAll<HTMLElement>('.ant-segmented-item')]
      .find((item) => item.textContent === label);
    expect(mode('复盘训练')).toBeTruthy();
    expect(mode('题库')).toBeTruthy();
    expect(mode('快速练习')).toBeTruthy();
    act(() => mode('复盘训练')?.click());
    expect(host.querySelector<HTMLElement>('[aria-label="复盘训练模式"]')?.hidden).toBe(false);
    expect(host.querySelector('[data-testid="review-feedback-mode"]')).not.toBeNull();
    act(() => mode('快速练习')?.click());
    expect(host.querySelector<HTMLElement>('[aria-label="快速练习模式"]')?.hidden).toBe(false);
    expect(host.querySelector<HTMLElement>('[aria-label="复盘训练模式"]')?.hidden).toBe(true);
  });

  it('clears a consumed exact focus when the mounted owner transitions to ordinary practice', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const render = (adaptiveFocus?: { ownerGeneration: number; signalVersionId: number; targetEventId: number }) => root.render(
      <QueryClientProvider client={queryClient}><QuestionBankView adaptiveFocus={adaptiveFocus} adaptiveOwnerGeneration={8} /></QueryClientProvider>,
    );
    await act(async () => render({ ownerGeneration: 8, signalVersionId: 91, targetEventId: 103 }));
    const workspace = () => host.querySelector<HTMLElement>('[data-testid="review-feedback-mode"]');
    expect(workspace()?.dataset.signalVersion).toBe('91');
    expect(workspace()?.dataset.targetEvent).toBe('103');

    await act(async () => render(undefined));
    const mode = [...host.querySelectorAll<HTMLElement>('.ant-segmented-item')]
      .find((item) => item.textContent === '复盘训练');
    act(() => mode?.click());
    expect(workspace()?.dataset.signalVersion).toBe('ordinary');
    expect(workspace()?.dataset.targetEvent).toBe('ordinary');
  });
});
