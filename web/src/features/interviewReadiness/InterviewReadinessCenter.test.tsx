// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import source from './InterviewReadinessCenter.tsx?raw';
import InterviewReadinessCenter from './InterviewReadinessCenter';
import { createResumeSelectionLease } from '@/features/interviewEvents/resumeSelectionLease';
import { createInterviewPracticeCase } from '@/services/interviewPracticeCases';

vi.mock('@/services/interviewPracticeCases', () => ({
  createInterviewPracticeCase: vi.fn(),
}));

const createPracticeCaseMock = vi.mocked(createInterviewPracticeCase);

const quickResume = { id: 11, title: '基础版', is_master: true, parent_resume_id: null, deleted_at: null };

function setInputValue(selector: string, value: string) {
  const element = document.querySelector<HTMLInputElement | HTMLTextAreaElement>(selector);
  if (!element) throw new Error(`Missing input: ${selector}`);
  act(() => {
    const setter = Object.getOwnPropertyDescriptor(
      element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype,
      'value',
    )?.set;
    setter?.call(element, value);
    element.dispatchEvent(new Event('input', { bubbles: true }));
    element.dispatchEvent(new Event('change', { bubbles: true }));
  });
}

async function selectQuickResume() {
  const select = document.querySelector<HTMLElement>('#quick-readiness-resume');
  if (!select) throw new Error('Missing quick resume select');
  const trigger = select.querySelector<HTMLElement>('.ant-select-selector') ?? select;
  await act(async () => {
    trigger.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    trigger.click();
    await new Promise((resolve) => setTimeout(resolve, 20));
  });
  const option = [...document.body.querySelectorAll<HTMLElement>('[role="option"], .ant-select-item-option')]
    .find((item) => item.textContent?.includes('基础版'));
  if (!option) throw new Error('Missing quick resume option');
  await act(async () => {
    option.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    option.click();
    await new Promise((resolve) => setTimeout(resolve, 20));
  });
}

function quickStartButton(): HTMLButtonElement {
  const button = [...document.querySelectorAll<HTMLButtonElement>('button')]
    .find((item) => item.textContent?.includes('进入快速练习'));
  if (!button) throw new Error('Missing quick practice button');
  return button;
}

function renderQuickDraft(root: ReturnType<typeof createRoot>, resumes: unknown) {
  act(() => root.render(
    <InterviewReadinessCenter
      initialMode="quick"
      fixedMode="quick"
      resumes={resumes as never}
    />,
  ));
}

async function selectQuickDraft(root: ReturnType<typeof createRoot>) {
  renderQuickDraft(root, [quickResume]);
  setInputValue('input[placeholder="例如：后端工程师"]', '后端工程师');
  setInputValue('#quick-readiness-jd', '负责高并发 API 设计。');
  const checkbox = document.querySelector<HTMLInputElement>('input[type="checkbox"]');
  if (!checkbox) throw new Error('Missing JD confirmation checkbox');
  act(() => checkbox.click());
  await selectQuickResume();
  expect(quickStartButton().disabled).toBe(false);
}

beforeEach(() => {
  document.body.replaceChildren();
  createPracticeCaseMock.mockReset();
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: () => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn() }),
  });
});

describe('InterviewReadinessCenter', () => {
  it('does not expose an application or event picker for real preparation', () => {
    const markup = renderToStaticMarkup(
      <InterviewReadinessCenter initialMode="real" fixedMode="real" />,
    );

    expect(markup).toContain('面试准备');
    expect(markup).not.toContain('选择投递');
    expect(markup).not.toContain('选择面试事件');
    expect(markup).not.toContain('请选择投递');
    expect(markup).not.toContain('请选择已排期面试');
  });

  it('renders a locked real event and resolves a single visible resume through the lease', () => {
    const lease = createResumeSelectionLease(4);
    const markup = renderToStaticMarkup(
      <InterviewReadinessCenter
        initialMode="real"
        fixedMode="real"
        lockedEvent={{ applicationId: 7, eventId: 8, companyName: 'Acme', positionName: '工程师' }}
        resumes={[{ id: 11, deleted_at: null } as never]}
        resumeSelectionLease={lease}
        generation={4}
        jdSource={{ status: 'ready', id: 3, text: '负责平台稳定性。' }}
      />,
    );

    expect(markup).toContain('Acme');
    expect(markup).toContain('工程师');
    expect(markup).toContain('当前 JD（只读）');
    expect(markup).toContain('已选择一份已保存简历');
    expect(markup).not.toContain('选择投递');
    expect(markup).not.toContain('选择面试事件');
  });

  it('keeps unresolved or invalid resume sources unavailable and does not launch preparation', () => {
    const open = vi.fn();
    const markup = renderToStaticMarkup(
      <InterviewReadinessCenter
        initialMode="real"
        fixedMode="real"
        lockedEvent={{ applicationId: 7, eventId: 8 }}
        resumes={{ status: 'loading' }}
        jdSource={{ status: 'ready', id: 3, text: 'JD' }}
        onLaunchTask={open}
      />,
    );

    expect(markup).toContain('暂时不可用');
    expect(markup).toContain('开始准备');
    expect(markup).toContain('disabled=""');
    expect(open).not.toHaveBeenCalled();
  });

  it('fails closed when a resume source getter throws during projection', () => {
    const hostile = {
      status: 'ready',
      get value(): never { throw new Error('resume source unavailable'); },
    } as never;
    const markup = renderToStaticMarkup(
      <InterviewReadinessCenter
        initialMode="real"
        fixedMode="real"
        lockedEvent={{ applicationId: 7, eventId: 8 }}
        resumes={hostile}
        jdSource={{ status: 'ready', id: 3, text: 'JD' }}
      />,
    );

    expect(markup).toContain('暂时不可用');
    expect(markup).toContain('简历状态暂时无法确认');
  });

  it('does not label an invalid ready JD as executable preparation input', () => {
    const markup = renderToStaticMarkup(
      <InterviewReadinessCenter
        initialMode="real"
        fixedMode="real"
        lockedEvent={{ applicationId: 7, eventId: 8 }}
        resumes={[{ id: 11, deleted_at: null } as never]}
        jdSource={{ status: 'ready', id: 0, text: '' }}
      />,
    );

    expect(markup).toContain('暂时不可用');
    expect(markup).toContain('岗位资料暂时无法读取');
  });

  it('keeps quick practice as the only editable mode and does not create a case while opening', () => {
    expect(source).toContain('createInterviewPracticeCase');
    expect(source).toContain('onClick={() => {');
    expect(source).toContain('mode === \'quick\'');
    expect(source).not.toContain('onOpenMockInterview');
  });

  it('drops the executable resume when the explicit selection is cleared', () => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      value: () => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn() }),
    });
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    act(() => root.render(
      <InterviewReadinessCenter
        fixedMode="real"
        lockedEvent={{ applicationId: 7, eventId: 8 }}
        resumes={[
          { id: 11, title: '基础版', is_master: true, parent_resume_id: null, deleted_at: null } as never,
          { id: 12, title: '岗位版', is_master: false, parent_resume_id: 11, deleted_at: null } as never,
        ]}
        selectedResumeId={11}
        jdSource={{ status: 'ready', id: 3, text: 'JD' }}
      />,
    ));
    const start = [...host.querySelectorAll<HTMLButtonElement>('button')].find((button) => button.textContent?.includes('开始准备'));
    expect(start?.disabled).toBe(false);
    const clear = host.querySelector<HTMLElement>('.ant-select-clear');
    expect(clear).toBeTruthy();
    act(() => clear?.dispatchEvent(new MouseEvent('mousedown', { bubbles: true })));
    expect(start?.disabled).toBe(true);
    act(() => root.unmount());
    host.remove();
  });

  it.each([
    ['deleted', [{ ...quickResume, deleted_at: '2026-08-30T00:00:00Z' }]],
    ['hidden', [{ ...quickResume, hidden: true }]],
    ['empty', []],
  ] as const)('does not create a quick case when the selected resume becomes %s after selection', async (_label, nextResumes) => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    await selectQuickDraft(root);

    renderQuickDraft(root, nextResumes);
    await act(async () => {
      quickStartButton().click();
      await Promise.resolve();
    });

    expect(createPracticeCaseMock).not.toHaveBeenCalled();
    act(() => root.unmount());
    host.remove();
  });

  it.each(['loading', 'error', 'unknown', 'absent'] as const)('does not create a quick case for %s envelope carrying stale resume rows', async (status) => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    await selectQuickDraft(root);

    renderQuickDraft(root, { status, value: [quickResume] });
    await act(async () => {
      quickStartButton().click();
      await Promise.resolve();
    });

    expect(createPracticeCaseMock).not.toHaveBeenCalled();
    act(() => root.unmount());
    host.remove();
  });

  it('supports secondary embedding and reduced motion', async () => {
    const markup = renderToStaticMarkup(
      <InterviewReadinessCenter initialMode="quick" fixedMode="quick" actionEmphasis="secondary" resumes={[]} />,
    );
    const fsModule = 'node:fs';
    const { readFileSync } = (await import(fsModule)) as {
      readFileSync: (path: string | URL, encoding: string) => string;
    };
    const styles = readFileSync('src/features/interviewReadiness/InterviewReadinessCenter.module.css', 'utf8');

    expect(markup).toContain('data-readiness-mode="quick"');
    expect(markup).toContain('secondaryAction');
    expect(styles).toMatch(/prefers-reduced-motion:[^}]+reduce[\s\S]*transform:\s*none/);
  });
});
