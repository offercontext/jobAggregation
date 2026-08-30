// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: () => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() }),
});

const services = vi.hoisted(() => ({ interviews: vi.fn(), recommendations: vi.fn(), createPracticeCase: vi.fn() }));
vi.mock('@/services/interviews', () => ({ listInterviews: services.interviews }));
vi.mock('@/services/adaptiveInterviewPractice', () => ({
  listAdaptivePracticeRecommendations: services.recommendations,
}));
vi.mock('@/services/interviewPracticeCases', () => ({
  createInterviewPracticeCase: services.createPracticeCase,
}));

const { default: InterviewV01View } = await import('./InterviewV01View');
let root: Root | undefined;
let container: HTMLDivElement | undefined;

const row = (overrides: Record<string, unknown> = {}) => ({
  application_id: 7, event_id: 9, company_name: '示例公司', position_name: '工程师',
  scheduled_at: new Date(Date.now() + 86_400_000).toISOString(), note_id: null,
  note_source_status: null, has_review_proposal: false, review_summary: null,
  has_confirmed_knowledge: false, preparation_available: true,
  event_status: 'todo', duration_minutes: 60, scheduled_at_state: 'present',
  ...overrides,
});

const flush = async () => {
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
};

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  services.interviews.mockReset().mockResolvedValue({ items: [], next_cursor: null });
  services.recommendations.mockReset().mockResolvedValue([]);
  services.createPracticeCase.mockReset();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => { act(() => root?.unmount()); container?.remove(); });

describe('InterviewV01View event and free-practice surface', () => {
  it('renders source-status cards in their lifecycle buckets rather than using date-only groups', async () => {
    services.interviews.mockResolvedValue({ items: [
      row({ event_id: 11, company_name: '未来已完成', event_status: 'done' }),
      row({ event_id: 12, company_name: '过去待更新', scheduled_at: new Date(Date.now() - 86_400_000).toISOString(), event_status: 'todo' }),
      row({ event_id: 13, company_name: '进行中', event_status: 'in_progress' }),
      row({ event_id: 14, company_name: '取消事件', event_status: 'cancelled' }),
    ], next_cursor: null });
    act(() => root?.render(<InterviewV01View />));
    await flush();

    expect(container?.textContent).toContain('过去待更新');
    expect(container?.textContent).toContain('进行中');
    expect(container?.textContent).not.toContain('未来已完成');
    expect(container?.textContent).not.toContain('取消事件');

    act(() => [...(container?.querySelectorAll('[role="tab"]') ?? [])].find((tab) => tab.textContent?.includes('已完成'))?.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    expect(container?.textContent).toContain('未来已完成');
    expect(container?.textContent).toContain('取消事件');
    expect(container?.textContent).not.toContain('过去待更新');
  });

  it('gives each actionable card one primary task and no story side action', async () => {
    const launchTask = vi.fn();
    const openApplication = vi.fn();
    services.interviews.mockResolvedValue({ items: [row({ event_id: 31 })], next_cursor: null });
    act(() => root?.render(<InterviewV01View onLaunchTask={launchTask} onOpenApplication={openApplication} />));
    await flush();

    const primaryActions = [...(container?.querySelectorAll<HTMLButtonElement>('[data-interview-primary="true"]') ?? [])];
    expect(primaryActions).toHaveLength(1);
    expect(primaryActions[0]?.textContent).toContain('准备面试');
    expect(container?.textContent).not.toContain('整理为故事');

    act(() => primaryActions[0]?.click());
    expect(launchTask).toHaveBeenCalledWith({
      ref: { taskId: 'application.interview_prepare', applicationId: 7, eventId: 31 },
      source: 'interview_event_card',
      focus: 'current',
    });
    act(() => [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent?.includes('查看投递详情'))?.click());
    expect(openApplication).toHaveBeenCalledWith(7);
  });

  it('renders terminal and unavailable cards without executable primary actions', async () => {
    services.interviews.mockResolvedValue({ items: [
      row({ event_id: 41, event_status: 'done', note_id: 5 }),
      row({ event_id: 42, event_status: 'mystery' }),
    ], next_cursor: null });
    act(() => root?.render(<InterviewV01View />));
    await flush();
    act(() => [...(container?.querySelectorAll('[role="tab"]') ?? [])].find((tab) => tab.textContent?.includes('已完成'))?.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    expect(container?.textContent).toContain('已有复盘');
    expect(container?.textContent).toContain('查看复盘');
    expect(container?.querySelectorAll('[data-interview-primary="true"]')).toHaveLength(1);

    act(() => [...(container?.querySelectorAll('[role="tab"]') ?? [])].find((tab) => tab.textContent?.includes('即将进行'))?.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    expect(container?.textContent).toContain('暂不可用');
    expect(container?.querySelector('[data-interview-card-bucket="unavailable"] [data-interview-primary="true"]')).toBeNull();
  });

  it('does not render a generic application or event selector before an exact event is chosen', async () => {
    act(() => root?.render(<InterviewV01View applications={[]} events={[]} resumes={[]} />));
    await flush();
    expect(container?.querySelector('#readiness-application')).toBeNull();
    expect(container?.querySelector('#readiness-event')).toBeNull();
    expect(container?.textContent).not.toContain('选择投递');
  });

  it('opens free practice only through the canonical launcher and performs no write on mount', async () => {
    const openFreePractice = vi.fn();
    act(() => root?.render(<InterviewV01View onOpenFreePractice={openFreePractice} />));
    await flush();
    expect(services.createPracticeCase).not.toHaveBeenCalled();
    act(() => [...(container?.querySelectorAll('[role="tab"]') ?? [])].find((tab) => tab.textContent?.includes('自由练习'))?.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    const entry = [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent?.includes('开始自由练习'));
    expect(entry).toBeTruthy();
    expect(openFreePractice).not.toHaveBeenCalled();
    act(() => entry?.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    expect(openFreePractice).toHaveBeenCalledOnce();
    expect(services.createPracticeCase).not.toHaveBeenCalled();
  });

  it('switches to practice only for a newly incremented external request token', async () => {
    act(() => root?.render(<InterviewV01View practiceRequestToken={1} />));
    await flush();
    expect(container?.querySelector('[data-testid="free-practice-workspace"]')).toBeNull();
    act(() => root?.render(<InterviewV01View practiceRequestToken={2} />));
    expect(container?.querySelector('[data-testid="free-practice-workspace"]')).not.toBeNull();
  });

  it('loads every cursor page before projecting cards', async () => {
    services.interviews
      .mockResolvedValueOnce({ items: [row({ event_id: 51, company_name: '第一页公司' })], next_cursor: 'cursor-2' })
      .mockResolvedValueOnce({ items: [row({ event_id: 52, company_name: '第二页公司' })], next_cursor: null });
    act(() => root?.render(<InterviewV01View />));
    await flush();
    expect(services.interviews).toHaveBeenNthCalledWith(1, 50, '');
    expect(services.interviews).toHaveBeenNthCalledWith(2, 50, 'cursor-2');
    expect(container?.textContent).toContain('第二页公司');
  });
});
