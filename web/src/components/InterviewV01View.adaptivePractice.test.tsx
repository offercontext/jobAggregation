// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: () => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() }),
});

const services = vi.hoisted(() => ({ interviews: vi.fn(), recommendations: vi.fn() }));
vi.mock('@/services/interviews', () => ({ listInterviews: services.interviews }));
vi.mock('@/services/adaptiveInterviewPractice', () => ({
  listAdaptivePracticeRecommendations: services.recommendations,
}));

const { default: InterviewV01View } = await import('./InterviewV01View');
let root: Root | undefined;
let container: HTMLDivElement | undefined;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  services.interviews.mockReset().mockResolvedValue({ items: [], next_cursor: null });
  services.recommendations.mockReset().mockResolvedValue([{
    proposal_id: 4, focus_id: 'focus-1', application_id: 2, application_event_id: 3,
    interview_note_id: 5, company_name: '云栖智能', position_name: '后端工程师',
    drill_kind: 'difficulty_breakdown', title: '拆解卡住的关键一步',
    observation: '影响范围追问时，回答节奏被打断。', reason: '这是复盘里明确记录的卡点。',
    prompt: '写出三步推进方式。', source_path: '/difficulty_points',
    source_excerpt: '被追问影响范围时卡住了。', source_fingerprint: 'fp',
  }]);
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => { act(() => root?.unmount()); container?.remove(); });

describe('InterviewV01View adaptive practice entry', () => {
  it('groups existing interview events into upcoming and completed tabs', async () => {
    services.interviews.mockResolvedValue({ items: [
      {
        application_id: 1, event_id: 11, company_name: '未来公司', position_name: '工程师',
        scheduled_at: new Date(Date.now() + 86_400_000).toISOString(), note_id: null,
        note_source_status: null, has_review_proposal: false, review_summary: null,
        has_confirmed_knowledge: false, preparation_available: true,
      },
      {
        application_id: 2, event_id: 22, company_name: '过去公司', position_name: '工程师',
        scheduled_at: new Date(Date.now() - 86_400_000).toISOString(), note_id: 9,
        note_source_status: 'current', has_review_proposal: false, review_summary: null,
        has_confirmed_knowledge: true, preparation_available: false,
      },
    ]});
    act(() => root?.render(<InterviewV01View />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(container?.textContent).toContain('未来公司');
    expect(container?.textContent).not.toContain('过去公司');
    act(() => [...(container?.querySelectorAll('[role="tab"]') ?? [])].find((tab) => tab.textContent?.includes('已完成'))?.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    expect(container?.textContent).toContain('过去公司');
    expect(container?.textContent).not.toContain('未来公司');
  });

  it('places question bank and readiness in one free-practice workspace', async () => {
    const openQuestionBank = vi.fn();
    act(() => root?.render(<InterviewV01View onOpenQuestionBank={openQuestionBank} applications={[]} events={[]} resumes={[]} />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    act(() => [...(container?.querySelectorAll('[role="tab"]') ?? [])].find((tab) => tab.textContent?.includes('自由练习'))?.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    expect(container?.textContent).toContain('快速练习');
    expect(container?.textContent).toContain('进入题库');
    expect(container?.querySelector('[role="tablist"][aria-label="练习模式"]')).toBeNull();
    act(() => [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent?.includes('进入题库'))?.click());
    expect(openQuestionBank).toHaveBeenCalledOnce();
  });

  it('switches only for a newly incremented request token and does not replay a stale token on mount', async () => {
    act(() => root?.render(<InterviewV01View practiceRequestToken={1} applications={[]} events={[]} resumes={[]} />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(container?.querySelector('[data-readiness-mode="quick"]')).toBeNull();
    act(() => root?.render(<InterviewV01View practiceRequestToken={2} applications={[]} events={[]} resumes={[]} />));
    expect(container?.querySelector('[data-readiness-mode="quick"]')).not.toBeNull();
  });

  it('loads every cursor page before deriving upcoming and completed buckets', async () => {
    services.interviews
      .mockResolvedValueOnce({ items: [{
        application_id: 1, event_id: 11, company_name: '第一页公司', position_name: '工程师',
        scheduled_at: new Date(Date.now() - 86_400_000).toISOString(), note_id: 9,
        note_source_status: 'current', has_review_proposal: false, review_summary: null,
        has_confirmed_knowledge: false, preparation_available: false,
      }], next_cursor: 'cursor-2' })
      .mockResolvedValueOnce({ items: [{
        application_id: 2, event_id: 22, company_name: '第二页公司', position_name: '工程师',
        scheduled_at: new Date(Date.now() + 86_400_000).toISOString(), note_id: null,
        note_source_status: null, has_review_proposal: false, review_summary: null,
        has_confirmed_knowledge: false, preparation_available: true,
      }], next_cursor: null });

    act(() => root?.render(<InterviewV01View />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); });

    expect(services.interviews).toHaveBeenNthCalledWith(1, 50, '');
    expect(services.interviews).toHaveBeenNthCalledWith(2, 50, 'cursor-2');
    expect(container?.textContent).toContain('第二页公司');
    act(() => [...(container?.querySelectorAll('[role="tab"]') ?? [])].find((tab) => tab.textContent?.includes('已完成'))?.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    expect(container?.textContent).toContain('第一页公司');
  });

  it('does not present a cancelled future event as pending', async () => {
    services.interviews.mockResolvedValue({ items: [{
      application_id: 1, event_id: 11, company_name: '取消公司', position_name: '工程师',
      scheduled_at: new Date(Date.now() + 86_400_000).toISOString(), note_id: null,
      note_source_status: null, has_review_proposal: false, review_summary: null,
      has_confirmed_knowledge: false, preparation_available: true,
    }], next_cursor: null });
    const cancelledEvent = {
      id: 11, application_id: 1, event_type: 'interview' as const, subtype: 'technical', tags: [], round: 1,
      scheduled_at: new Date(Date.now() + 86_400_000).toISOString(), duration_minutes: 60,
      location: '', notes: '', status: 'cancelled', created_at: new Date().toISOString(),
    };
    act(() => root?.render(<InterviewV01View events={[cancelledEvent]} />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(container?.textContent).not.toContain('取消公司');
    act(() => [...(container?.querySelectorAll('[role="tab"]') ?? [])].find((tab) => tab.textContent?.includes('已完成'))?.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    expect(container?.textContent).toContain('取消公司');
    expect(container?.textContent).toContain('已取消');
    expect(container?.textContent).not.toContain('待进行');
  });

  it('does not classify or prepare interviews while the event source is unavailable', async () => {
    services.interviews.mockResolvedValue({ items: [{
      application_id: 1, event_id: 11, company_name: '状态未知公司', position_name: '工程师',
      scheduled_at: new Date(Date.now() + 86_400_000).toISOString(), note_id: null,
      note_source_status: null, has_review_proposal: false, review_summary: null,
      has_confirmed_knowledge: false, preparation_available: true,
    }], next_cursor: null });
    const retry = vi.fn();
    const prepare = vi.fn();

    act(() => root?.render(
      <InterviewV01View events={[]} eventsError onRetryEvents={retry} onOpenPreparation={prepare} />,
    ));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    expect(container?.textContent).toContain('面试状态暂时无法确认，请重试后查看。');
    expect(container?.textContent).not.toContain('状态未知公司');
    expect(container?.textContent).not.toContain('待进行');
    expect(container?.textContent).not.toContain('准备面试');
    act(() => container?.querySelector<HTMLButtonElement>('[aria-label="重试面试状态"]')?.click());
    expect(retry).toHaveBeenCalledOnce();
    expect(prepare).not.toHaveBeenCalled();
  });

  it('shows one recommendation and only navigates after the user clicks it', async () => {
    const open = vi.fn();
    act(() => root?.render(<InterviewV01View onOpenAdaptivePractice={open} />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    act(() => [...(container?.querySelectorAll('[role="tab"]') ?? [])].find((tab) => tab.textContent?.includes('自由练习'))?.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    expect(container?.textContent).toContain('下一项行动');
    expect(container?.textContent).toContain('拆解卡住的关键一步');
    expect(container?.textContent).toContain('来自已保存复盘');
    expect(container?.textContent).toContain('适合一次短时训练');
    expect(container?.textContent).toContain('不会自动写入故事库');
    expect(open).not.toHaveBeenCalled();
    act(() => [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent?.includes('开始这项训练'))?.click());
    expect(open).toHaveBeenCalledWith({ proposalId: 4, focusId: 'focus-1' });
    const entry = [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent?.includes('开始这项训练'));
    expect(entry?.className).toContain('ant-btn-lg');
    expect(container?.querySelector('.ant-btn-primary')).toBeNull();
  });

  it('shows a retry state when recommendations cannot be loaded', async () => {
    services.recommendations.mockRejectedValue(new Error('network'));
    act(() => root?.render(<InterviewV01View onOpenAdaptivePractice={() => {}} />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    act(() => [...(container?.querySelectorAll('[role="tab"]') ?? [])].find((tab) => tab.textContent?.includes('自由练习'))?.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    expect(container?.textContent).toContain('复盘训练建议暂时无法加载');
    expect(container?.textContent).toContain('重新加载建议');
  });

  it('opens the local expression growth history without starting AI work', async () => {
    const openGrowth = vi.fn();
    act(() => root?.render(<InterviewV01View onOpenVoiceCoachingGrowth={openGrowth} />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    act(() => [...(container?.querySelectorAll('[role="tab"]') ?? [])].find((tab) => tab.textContent?.includes('已完成'))?.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    const entry = [...(container?.querySelectorAll('button') ?? [])]
      .find((button) => button.textContent?.includes('表达成长'));
    expect(entry).toBeTruthy();
    act(() => entry?.click());
    expect(openGrowth).toHaveBeenCalledOnce();
  });
});
