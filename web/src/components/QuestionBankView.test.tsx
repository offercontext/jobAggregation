import { renderToStaticMarkup } from 'react-dom/server';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { App as AntApp } from 'antd';
import { describe, expect, it } from 'vitest';
import type { PracticeStats, Question } from '@/types/question';
import QuestionBankView from './QuestionBankView';
import source from './QuestionBankView.tsx?raw';

function renderWithQuestions(questions: Question[]) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  queryClient.setQueryData(['questions', {}], questions);
  queryClient.setQueryData(['questions', 'stats'], stats());
  queryClient.setQueryData(['questions-due'], questions);

  return renderToStaticMarkup(
    <QueryClientProvider client={queryClient}>
      <AntApp>
        <QuestionBankView />
      </AntApp>
    </QueryClientProvider>,
  );
}

function question(patch: Partial<Question> = {}): Question {
  return {
    id: 12,
    application_id: null,
    topic: 'system-design',
    category: '系统设计',
    difficulty: 'hard',
    question: '如何设计一个高并发短链系统？',
    reference_answer: '发号器、缓存、限流、异步写入。',
    tags: ['缓存', '限流'],
    source_type: 'ai_knowledge',
    status: 'new',
    practice_count: 0,
    last_practiced_at: null,
    next_review_at: null,
    created_at: '2026-07-09T10:00:00Z',
    updated_at: '2026-07-09T10:00:00Z',
    ...patch,
  };
}

function stats(patch: Partial<PracticeStats> = {}): PracticeStats {
  return {
    total: 1,
    new: 1,
    practicing: 0,
    mastered: 0,
    due: 1,
    today_reviews: 0,
    streak_days: 0,
    ...patch,
  };
}

describe('QuestionBankView', () => {
  it('renders the empty practice bank with AI generation and manual creation entry points', () => {
    const markup = renderWithQuestions([]);

    expect(markup).toContain('题库刷题');
    expect(markup).toContain('基于已记录的面试问题生成题目');
    expect(markup).toContain('AI 生成题目');
    expect(markup).toContain('手动添加');
    expect(markup).toContain('题库还是空的');
    expect(markup).toContain('从面试复盘生成题目，或手动添加');
  });

  it('renders generated questions with filters, status, difficulty, and edit/delete controls', () => {
    const markup = renderWithQuestions([question()]);

    expect(markup).toContain('搜索题目 / 分类 / 标签');
    expect(markup).toContain('全部状态');
    expect(markup).toContain('全部难度');
    expect(markup).toContain('如何设计一个高并发短链系统？');
    expect(markup).toContain('系统设计');
    expect(markup).toContain('困难');
    expect(markup).toContain('未刷');
    expect(markup).toContain('编辑题目');
    expect(markup).toContain('删除题目');
  });

  it('keeps the brush page limited to the bank and spaced review', () => {
    expect(source).toContain("value: 'question_bank'");
    expect(source).toContain("value: 'review'");
    expect(source).toContain("label: '今日复习'");
    expect(source).not.toContain('AdaptiveInterviewPracticeWorkspace');
    expect(source).not.toContain('InterviewReadinessCenter');
    expect(source).not.toContain('quick_practice');
  });

  it('keeps the spaced-review path wired to due questions, answer reveal, review ratings, and stats refresh', () => {
    expect(source).toContain('listDueQuestions(40)');
    expect(source).toContain('getPracticeStats()');
    expect(source).toContain('submitReview(id, rating)');
    expect(source).toContain('显示答案');
    expect(source).toContain('不会');
    expect(source).toContain('模糊');
    expect(source).toContain('掌握');
    expect(source).toContain("invalidateQueries({ queryKey: ['questions-due'] })");
    expect(source).toContain("invalidateQueries({ queryKey: ['questions'] })");
  });

  it('does not offer unsupported knowledge generation as executable', () => {
    expect(source).toContain('generateQuestions');
    expect(source).toContain("source,");
    expect(source).toContain("label: '参考资料（暂未开放）', value: 'knowledge', disabled: true");
    expect(source).toContain("useState<'knowledge' | 'notes'>('notes')");
    expect(source).toContain("label: '面试复盘真题'");
    expect(source).toContain('已存在的题目会自动去重');
  });

  it('uses the current Ant Design hidden-destroy API in overlays', () => {
    expect(source).toContain('destroyOnHidden');
    expect(source).not.toContain('destroyOnClose');
  });
});
