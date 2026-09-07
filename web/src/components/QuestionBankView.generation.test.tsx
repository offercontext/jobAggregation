// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { App as AntApp } from 'antd';
import { expect, it, vi } from 'vitest';
import { generateQuestions } from '@/services/questions';
import QuestionBankView from './QuestionBankView';

vi.mock('@/services/questions', () => ({
  listQuestions: vi.fn(async () => []),
  listDueQuestions: vi.fn(async () => []),
  getPracticeStats: vi.fn(async () => ({ total: 0, due: 0, today_reviews: 0, streak_days: 0 })),
  generateQuestions: vi.fn(async () => ({ count: 0, skipped: 0 })),
  createQuestion: vi.fn(), deleteQuestion: vi.fn(), submitReview: vi.fn(), updateQuestion: vi.fn(),
}));

it('disables knowledge generation and submits the explicit notes default', async () => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  Object.defineProperty(window, 'matchMedia', { configurable: true, value: () => ({
    matches: false, addListener: vi.fn(), removeListener: vi.fn(),
  }) });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const host = document.createElement('div');
  document.body.appendChild(host);
  const root = createRoot(host);
  try {
    await act(async () => { root.render(<QueryClientProvider client={client}><AntApp><QuestionBankView /></AntApp></QueryClientProvider>); });
    const open = [...host.querySelectorAll('button')].find((button) => button.textContent?.includes('AI 生成题目'));
    expect(open).toBeTruthy();
    await act(async () => open!.click());
    const layer = host.querySelector('[aria-label="AI 生成题目"]')!;
    const knowledge = [...layer.querySelectorAll('label')].find((label) => label.textContent?.includes('参考资料'))!;
    expect(knowledge.querySelector('input')?.disabled).toBe(true);
    const notes = [...layer.querySelectorAll('label')].find((label) => label.textContent?.includes('面试复盘真题'))!;
    expect(notes.querySelector('input')?.checked).toBe(true);
    const generate = [...layer.querySelectorAll('button')].find((button) => button.textContent?.includes('开始生成'));
    expect(generate).toBeTruthy();
    await act(async () => generate!.click());
    expect(generateQuestions).toHaveBeenCalledOnce();
    expect(generateQuestions).toHaveBeenCalledWith({ source: 'notes', count: 8 });
  } finally {
    await act(async () => root.unmount());
    client.clear();
    host.remove();
  }
});
