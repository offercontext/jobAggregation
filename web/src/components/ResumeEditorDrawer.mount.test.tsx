// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { App as AntApp } from 'antd';
import type { Resume } from '@/types/resume';

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const state = vi.hoisted(() => ({
  updateResume: vi.fn(),
  copyResume: vi.fn(),
  listResumes: vi.fn(),
  aiService: vi.fn(),
}));

vi.mock('@/services/resumes', () => ({
  updateResume: state.updateResume,
  copyResume: state.copyResume,
  listResumes: state.listResumes,
}));

vi.mock('@/services/ai', () => ({
  analyzeJD: state.aiService,
  listJDAnalyses: state.aiService,
}));

import ResumeEditorDrawer from './ResumeEditorDrawer';

let root: Root | undefined;
let container: HTMLDivElement | undefined;
let fetchSpy: { mockRestore: () => void };
let xhrOpenSpy: ReturnType<typeof vi.spyOn>;
let pushStateSpy: ReturnType<typeof vi.spyOn>;
let replaceStateSpy: ReturnType<typeof vi.spyOn>;
let onClose: ReturnType<typeof vi.fn>;
let onSaved: ReturnType<typeof vi.fn>;

const resume: Resume = {
  id: 1,
  name: '测试简历',
  file_path: '',
  parsed_data: '',
  parse_status: 'text-ready',
  title: '测试简历',
  is_master: true,
  parent_resume_id: null,
  source: 'manual',
  source_file_path: '',
  content_json: {
    contact: { name: '林晓' },
    education: [],
    experience: [{ highlights: ['负责订单服务'] }],
    projects: [],
    skills: ['TypeScript'],
    career_intent: { target_roles: ['前端工程师'] },
    custom_root: { keep: true },
  },
  deleted_at: null,
  created_at: '2026-08-06T00:00:00Z',
  completion_percent: 60,
  missing_sections: ['education'],
  is_complete: false,
};

function renderEditor() {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  onClose = vi.fn();
  onSaved = vi.fn();

  act(() => {
    root?.render(
      <QueryClientProvider client={queryClient}>
        <AntApp>
          <ResumeEditorDrawer resume={resume} open onClose={onClose} onSaved={onSaved} />
        </AntApp>
      </QueryClientProvider>,
    );
  });
}

function findButton(label: string): HTMLButtonElement {
  const button = Array.from(container?.querySelectorAll('button') ?? [])
    .find((item) => item.textContent?.includes(label));
  if (!(button instanceof HTMLButtonElement)) throw new Error(`button not found: ${label}`);
  return button;
}

async function click(element: Element) {
  await act(async () => {
    (element as HTMLElement).click();
  });
}

beforeEach(() => {
  state.updateResume.mockReset();
  state.copyResume.mockReset();
  state.listResumes.mockReset();
  state.aiService.mockReset();
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => undefined,
      removeListener: () => undefined,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      dispatchEvent: () => false,
    }),
  });
  fetchSpy = vi.spyOn(globalThis, 'fetch');
  xhrOpenSpy = vi.spyOn(XMLHttpRequest.prototype, 'open');
  pushStateSpy = vi.spyOn(window.history, 'pushState');
  replaceStateSpy = vi.spyOn(window.history, 'replaceState');
});

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
  fetchSpy.mockRestore();
  xhrOpenSpy.mockRestore();
  pushStateSpy.mockRestore();
  replaceStateSpy.mockRestore();
  root = undefined;
  container = undefined;
});

describe('ResumeEditorDrawer mounted audit flow', () => {
  it('keeps structured edits when switching through advanced JSON and saving', async () => {
    state.updateResume.mockResolvedValue(resume);
    renderEditor();
    const saveButton = container?.querySelector('button.ant-btn-primary');
    if (!(saveButton instanceof HTMLButtonElement)) throw new Error('save button not found');
    await click(findButton('基本信息'));

    const nameLabel = Array.from(container?.querySelectorAll('label') ?? []).find((item) => item.textContent === '姓名');
    const nameInput = nameLabel?.parentElement?.querySelector('input');
    if (!(nameInput instanceof HTMLInputElement)) throw new Error('name input not found');
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set?.call(nameInput, 'Grace');
      nameInput.dispatchEvent(new Event('input', { bubbles: true }));
    });

    await click(findButton('高级 JSON'));
    const advanced = container?.querySelector('textarea');
    if (!(advanced instanceof HTMLTextAreaElement)) throw new Error('advanced JSON input not found');
    expect(JSON.parse(advanced.value)).toMatchObject({ contact: { name: 'Grace' }, custom_root: { keep: true } });

    const advancedContent = JSON.parse(advanced.value);
    advancedContent.contact.name = 'Marie';
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set?.call(advanced, JSON.stringify(advancedContent));
      advanced.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await click(findButton('高级 JSON'));
    const updatedNameInput = Array.from(container?.querySelectorAll('label') ?? [])
      .find((item) => item.textContent === '姓名')?.parentElement?.querySelector('input');
    expect(updatedNameInput).toBeInstanceOf(HTMLInputElement);
    expect((updatedNameInput as HTMLInputElement).value).toBe('Marie');

    await click(saveButton);
    await act(async () => Promise.resolve());
    expect(state.updateResume).toHaveBeenCalledWith(1, expect.objectContaining({
      content_json: expect.objectContaining({ contact: expect.objectContaining({ name: 'Marie' }), custom_root: { keep: true } }),
    }));
  });

  it('opens, expands, collapses, and closes the audit without write, AI, HTTP, or navigation calls', async () => {
    renderEditor();

    expect(container?.textContent).toContain('基础简历');
    expect(container?.textContent).not.toContain('主简历');

    const openButton = findButton('简历事实体检');
    expect(openButton.getAttribute('aria-expanded')).toBe('false');
    expect(container?.textContent).not.toContain('只检查当前简历中可观察的信息');

    await click(openButton);
    expect(openButton.getAttribute('aria-expanded')).toBe('true');
    expect(container?.textContent).toContain('只检查当前简历中可观察的信息');
    expect(container?.textContent).toContain('无法判断');

    const details = container?.querySelector('details') as HTMLDetailsElement;
    const summary = details?.querySelector('summary') as HTMLElement;
    expect(details?.open).toBe(false);
    await click(summary);
    expect(details?.open).toBe(true);
    await click(summary);
    expect(details?.open).toBe(false);

    await click(openButton);
    expect(openButton.getAttribute('aria-expanded')).toBe('false');
    expect(container?.textContent).not.toContain('只检查当前简历中可观察的信息');
    expect(state.updateResume).not.toHaveBeenCalled();
    expect(state.aiService).not.toHaveBeenCalled();
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(xhrOpenSpy).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    expect(onSaved).not.toHaveBeenCalled();
    expect(pushStateSpy).not.toHaveBeenCalled();
    expect(replaceStateSpy).not.toHaveBeenCalled();
  });

  it('opens the fact workspace from an actionable saved finding without starting a write', async () => {
    renderEditor();
    await click(findButton('简历事实体检'));

    const reviewFinding = Array.from(container?.querySelectorAll('details') ?? [])
      .find((item) => item.textContent?.includes('可补充真实事实'));
    if (!(reviewFinding instanceof HTMLDetailsElement)) throw new Error('review finding not found');
    const summary = reviewFinding.querySelector('summary');
    if (!(summary instanceof HTMLElement)) throw new Error('summary not found');
    await click(summary);
    await click(findButton('补充真实事实'));

    expect(document.body.querySelector('[role="dialog"]')?.getAttribute('aria-label'))
      .toContain('可补充真实事实');
    expect(document.body.textContent).toContain('系统不会替你估算数字');
    expect(state.copyResume).not.toHaveBeenCalled();
    expect(state.updateResume).not.toHaveBeenCalled();
    expect(state.aiService).not.toHaveBeenCalled();
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(xhrOpenSpy).not.toHaveBeenCalled();
  });

  it('does not open a fact supplement workspace while the editor has unsaved changes', async () => {
    renderEditor();
    const title = container?.querySelector('input[placeholder="简历标题"]');
    if (!(title instanceof HTMLInputElement)) throw new Error('title input not found');
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
      setter?.call(title, '尚未保存的标题');
      title.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await click(findButton('简历事实体检'));
    const reviewFinding = Array.from(container?.querySelectorAll('details') ?? [])
      .find((item) => item.textContent?.includes('可补充真实事实'));
    if (!(reviewFinding instanceof HTMLDetailsElement)) throw new Error('review finding not found');
    const summary = reviewFinding.querySelector('summary');
    if (!(summary instanceof HTMLElement)) throw new Error('summary not found');
    await click(summary);
    await click(findButton('补充真实事实'));

    expect(document.body.querySelector('[role="dialog"]')).toBeNull();
    expect(document.body.textContent).toContain('请先保存或取消当前编辑');
    expect(state.copyResume).not.toHaveBeenCalled();
    expect(state.updateResume).not.toHaveBeenCalled();
  });

  it('allows fact supplementation after an edit is reverted to the saved value', async () => {
    renderEditor();
    const title = container?.querySelector('input[placeholder="简历标题"]');
    if (!(title instanceof HTMLInputElement)) throw new Error('title input not found');
    const setTitle = async (value: string) => {
      await act(async () => {
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        setter?.call(title, value);
        title.dispatchEvent(new Event('input', { bubbles: true }));
      });
    };
    await setTitle('临时标题');
    await setTitle('测试简历');
    await click(findButton('简历事实体检'));
    const reviewFinding = Array.from(container?.querySelectorAll('details') ?? [])
      .find((item) => item.textContent?.includes('可补充真实事实'));
    if (!(reviewFinding instanceof HTMLDetailsElement)) throw new Error('review finding not found');
    const summary = reviewFinding.querySelector('summary');
    if (!(summary instanceof HTMLElement)) throw new Error('summary not found');
    await click(summary);
    await click(findButton('补充真实事实'));

    expect(document.body.querySelector('[role="dialog"]')).not.toBeNull();
    expect(state.copyResume).not.toHaveBeenCalled();
  });
});
