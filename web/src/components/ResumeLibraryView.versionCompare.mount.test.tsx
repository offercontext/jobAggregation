// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { App as AntApp } from 'antd';
import type { Resume } from '@/types/resume';

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const state = vi.hoisted(() => ({
  listResumes: vi.fn(),
  createResume: vi.fn(),
  createResumeFromSample: vi.fn(),
  copyResume: vi.fn(),
  deleteResume: vi.fn(),
  updateResume: vi.fn(),
  uploadResume: vi.fn(),
  aiCall: vi.fn(),
}));

vi.mock('@/services/resumes', () => ({
  listResumes: state.listResumes,
  createResume: state.createResume,
  createResumeFromSample: state.createResumeFromSample,
  copyResume: state.copyResume,
  deleteResume: state.deleteResume,
  updateResume: state.updateResume,
  uploadResume: state.uploadResume,
}));

vi.mock('@/services/ai', () => ({
  analyzeJD: state.aiCall,
  listJDAnalyses: state.aiCall,
}));

import ResumeLibraryView from './ResumeLibraryView';

function makeResume(id: number, overrides: Partial<Resume> = {}): Resume {
  return {
    id,
    name: `简历 ${id}`,
    file_path: '',
    parsed_data: '',
    parse_status: 'text-ready',
    title: id === 1 ? '中文主简历' : '中文岗位版本',
    is_master: id === 1,
    parent_resume_id: id === 1 ? null : 1,
    source: id === 1 ? 'manual' : 'sample_copy',
    source_file_path: '',
    content_json: {
      contact: { name: id === 1 ? '林晓' : '周宁' },
      experience: [{ highlights: [id === 1 ? '负责订单服务' : '重构订单服务'.repeat(80)] }],
    },
    deleted_at: null,
    created_at: `2026-08-06T00:0${id}:00Z`,
    completion_percent: 80,
    missing_sections: [],
    is_complete: true,
    ...overrides,
  };
}

let root: Root | null = null;
let host: HTMLDivElement | null = null;
let fetchSpy: { mockRestore: () => void };
let xhrOpenSpy: ReturnType<typeof vi.spyOn>;
let pushStateSpy: ReturnType<typeof vi.spyOn>;
let replaceStateSpy: ReturnType<typeof vi.spyOn>;

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, 20));
  });
}

function renderLibrary() {
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  act(() => {
    root?.render(
      <QueryClientProvider client={queryClient}>
        <AntApp><ResumeLibraryView /></AntApp>
      </QueryClientProvider>,
    );
  });
  return queryClient;
}

function findButton(label: string) {
  const button = Array.from(host?.querySelectorAll('button') ?? [])
    .find((item) => item.textContent?.includes(label) || item.getAttribute('aria-label') === label);
  if (!(button instanceof HTMLButtonElement)) throw new Error(`button not found: ${label}`);
  return button;
}

function findCompareButton() {
  const button = Array.from(host?.querySelectorAll('button') ?? [])
    .find((item) => item.textContent?.trim().length === 4);
  if (!(button instanceof HTMLButtonElement)) throw new Error('compare button not found');
  return button;
}

beforeEach(() => {
  const parent = makeResume(1);
  const target = makeResume(2);
  state.listResumes.mockResolvedValue([target, parent]);
  state.createResume.mockReset();
  state.createResumeFromSample.mockReset();
  state.copyResume.mockReset();
  state.deleteResume.mockReset();
  state.updateResume.mockReset();
  state.uploadResume.mockReset();
  state.aiCall.mockReset();
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: () => ({ matches: false, addListener: () => undefined, removeListener: () => undefined }),
  });
  window.scrollTo = vi.fn();
  fetchSpy = vi.spyOn(globalThis, 'fetch');
  xhrOpenSpy = vi.spyOn(XMLHttpRequest.prototype, 'open');
  pushStateSpy = vi.spyOn(window.history, 'pushState');
  replaceStateSpy = vi.spyOn(window.history, 'replaceState');
});

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  document.querySelectorAll('.ant-drawer-root').forEach((element) => element.remove());
  fetchSpy.mockRestore();
  xhrOpenSpy.mockRestore();
  pushStateSpy.mockRestore();
  replaceStateSpy.mockRestore();
  root = null;
  host = null;
});

describe('ResumeLibraryView version compare mounted audit', () => {
  it('opens, selects, expands, collapses, and closes without writes, AI, HTTP, or navigation', async () => {
    renderLibrary();
    await flush();

    expect(state.listResumes).toHaveBeenCalledTimes(1);
    await act(async () => findButton('对比版本').click());
    expect(host?.textContent).toContain('仅比较当前已保存的简历内容');

    const select = host?.querySelector<HTMLSelectElement>('select[aria-label="基准版本"]');
    expect(select).not.toBeNull();
    await act(async () => {
      select!.value = '1';
      select!.dispatchEvent(new Event('change', { bubbles: true }));
    });
    expect(host?.textContent).toContain('工作经历');
    expect(host?.textContent).not.toContain('/experience/0/highlights/0');

    const details = host?.querySelector('details');
    const summary = details?.querySelector('summary');
    expect(details?.open).toBe(false);
    await act(async () => summary?.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    expect(host?.querySelector('details')?.open).toBe(true);
    await act(async () => summary?.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    expect(host?.querySelector('details')?.open).toBe(false);

    await act(async () => findButton('关闭版本对比').click());
    expect(state.createResume).not.toHaveBeenCalled();
    expect(state.createResumeFromSample).not.toHaveBeenCalled();
    expect(state.copyResume).not.toHaveBeenCalled();
    expect(state.deleteResume).not.toHaveBeenCalled();
    expect(state.updateResume).not.toHaveBeenCalled();
    expect(state.uploadResume).not.toHaveBeenCalled();
    expect(state.aiCall).not.toHaveBeenCalled();
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(xhrOpenSpy).not.toHaveBeenCalled();
    expect(pushStateSpy).not.toHaveBeenCalled();
    expect(replaceStateSpy).not.toHaveBeenCalled();
  });

  it('recomputes a saved target refresh and closes when the target is removed', async () => {
    const queryClient = renderLibrary();
    await flush();
    await act(async () => findCompareButton().click());

    act(() => {
      queryClient.setQueryData(['resumes'], [
        makeResume(2, {
          parent_resume_id: 1,
          content_json: {
            contact: { name: 'refreshed target' },
            experience: [{ highlights: ['refreshed evidence'] }],
          },
        }),
        makeResume(1),
      ]);
    });
    await flush();
    expect(host?.textContent).toContain('refreshed target');

    act(() => {
      queryClient.setQueryData(['resumes'], [makeResume(1)]);
    });
    await flush();
    expect(host?.querySelector('[role="dialog"]')).toBeNull();
  });

  it('connects an audit finding to a derived version and opens its parent comparison without AI', async () => {
    const source = makeResume(2, {
      content_json: {
        contact: { name: '筱哲' },
        experience: [{ highlights: ['负责订单服务'] }],
      },
    });
    const parent = makeResume(1);
    const copied = makeResume(3, {
      title: '中文岗位版本 · 事实补充',
      parent_resume_id: 2,
      is_master: false,
      content_json: source.content_json,
    });
    const saved = makeResume(3, {
      title: copied.title,
      parent_resume_id: 2,
      is_master: false,
      content_json: {
        contact: { name: '筱哲' },
        experience: [{ highlights: ['支撑每月 120 万笔订单处理'] }],
      },
    });
    state.listResumes.mockResolvedValue([source, parent]);
    state.copyResume.mockResolvedValue(copied);
    state.updateResume.mockResolvedValue(saved);
    renderLibrary();
    await flush();

    await act(async () => findButton('编辑').click());
    await act(async () => findButton('简历事实体检').click());
    const finding = Array.from(host?.querySelectorAll('details') ?? [])
      .find((item) => item.textContent?.includes('可补充真实事实'));
    if (!(finding instanceof HTMLDetailsElement)) throw new Error('fact finding not found');
    await act(async () => finding.querySelector('summary')?.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    await act(async () => findButton('补充真实事实').click());

    const textarea = host?.querySelector('textarea[aria-label="经确认的最终简历表述"]');
    if (!(textarea instanceof HTMLTextAreaElement)) throw new Error('fact textarea not found');
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
      setter?.call(textarea, '支撑每月 120 万笔订单处理');
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
    });
    const checkbox = host?.querySelector('input[type="checkbox"]');
    if (!(checkbox instanceof HTMLInputElement)) throw new Error('confirmation not found');
    await act(async () => checkbox.click());
    await act(async () => findButton('创建新版本并查看差异').click());
    await flush();

    expect(state.copyResume).toHaveBeenCalledTimes(1);
    expect(state.updateResume).toHaveBeenCalledWith(3, expect.objectContaining({ content_json: saved.content_json }));
    expect(host?.textContent).toContain('仅比较当前已保存的简历内容');
    expect(host?.textContent).toContain('负责订单服务');
    expect(host?.textContent).toContain('支撑每月 120 万笔订单处理');
    expect(state.aiCall).not.toHaveBeenCalled();
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(xhrOpenSpy).not.toHaveBeenCalled();
    expect(pushStateSpy).not.toHaveBeenCalled();
    expect(replaceStateSpy).not.toHaveBeenCalled();
  });

  it('refreshes the library instead of opening stale content when an update response is lost', async () => {
    const source = makeResume(2, {
      content_json: {
        contact: { name: '筱哲' },
        experience: [{ highlights: ['负责订单服务'] }],
      },
    });
    const copied = makeResume(4, {
      title: '筱哲事实补充副本',
      parent_resume_id: 2,
      is_master: false,
      content_json: source.content_json,
    });
    state.listResumes.mockResolvedValue([source, makeResume(1)]);
    state.copyResume.mockResolvedValue(copied);
    state.updateResume.mockRejectedValue(new Error('temporary'));
    renderLibrary();
    await flush();

    await act(async () => findButton('编辑').click());
    await act(async () => findButton('简历事实体检').click());
    const finding = Array.from(host?.querySelectorAll('details') ?? [])
      .find((item) => item.textContent?.includes('可补充真实事实'));
    if (!(finding instanceof HTMLDetailsElement)) throw new Error('fact finding not found');
    await act(async () => finding.querySelector('summary')?.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    await act(async () => findButton('补充真实事实').click());
    const textarea = host?.querySelector('textarea[aria-label="经确认的最终简历表述"]');
    if (!(textarea instanceof HTMLTextAreaElement)) throw new Error('fact textarea not found');
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
      setter?.call(textarea, '支撑每月 120 万笔订单处理');
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
    });
    const checkbox = host?.querySelector('input[type="checkbox"]');
    if (!(checkbox instanceof HTMLInputElement)) throw new Error('confirmation not found');
    await act(async () => checkbox.click());
    await act(async () => findButton('创建新版本并查看差异').click());
    await flush();

    expect(host?.textContent).toContain('保存结果待确认');
    await act(async () => findButton('返回简历库核对').click());
    await flush();

    expect(host?.querySelector('input[placeholder="简历标题"]')).toBeNull();
    expect(state.listResumes.mock.calls.length).toBeGreaterThanOrEqual(2);
    expect(state.copyResume).toHaveBeenCalledTimes(1);
  });
});
