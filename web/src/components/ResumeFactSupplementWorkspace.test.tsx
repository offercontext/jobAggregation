// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { App as AntApp } from 'antd';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Resume } from '@/types/resume';
import type { ResumeAuditFinding } from '@/lib/resumeEvidenceAudit';

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const state = vi.hoisted(() => ({
  copyResume: vi.fn(),
  updateResume: vi.fn(),
  ai: vi.fn(),
}));

vi.mock('@/services/resumes', () => ({
  copyResume: state.copyResume,
  updateResume: state.updateResume,
}));

vi.mock('@/services/ai', () => ({ analyzeJD: state.ai }));

import ResumeFactSupplementWorkspace from './ResumeFactSupplementWorkspace';

const source: Resume = {
  id: 7,
  name: '筱哲主简历',
  file_path: '',
  parsed_data: '',
  parse_status: 'text-ready',
  title: '筱哲主简历',
  is_master: true,
  parent_resume_id: null,
  source: 'manual',
  source_file_path: '',
  content_json: {
    experience: [{ company: '云栖智能', highlights: ['负责订单服务'] }],
  },
  deleted_at: null,
  created_at: '2026-08-12T00:00:00Z',
  completion_percent: 80,
  missing_sections: [],
  is_complete: true,
};

const finding: ResumeAuditFinding = {
  id: 'facts-quantification',
  category: 'facts',
  status: 'review',
  title: '可补充真实事实',
  explanation: '如有真实数据，可以补充数量、规模、频率、时间或结果。',
  source: { path: '/experience/0/highlights/0', excerpt: '负责订单服务' },
};

let root: Root | undefined;
let container: HTMLDivElement | undefined;
let onClose: ReturnType<typeof vi.fn>;
let onCompleted: ReturnType<typeof vi.fn>;
let onCopyCreated: ReturnType<typeof vi.fn>;
let onContinueInCopy: ReturnType<typeof vi.fn>;
let onExitToLibrary: ReturnType<typeof vi.fn>;
let onCopyResultUnknown: ReturnType<typeof vi.fn>;

function renderWorkspace(options: { source?: Resume; finding?: ResumeAuditFinding } = {}) {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  onClose = vi.fn();
  onCompleted = vi.fn();
  onCopyCreated = vi.fn();
  onContinueInCopy = vi.fn();
  onExitToLibrary = vi.fn();
  onCopyResultUnknown = vi.fn();
  act(() => {
    root?.render(
      <AntApp>
        <ResumeFactSupplementWorkspace
          open
          source={options.source ?? source}
          finding={options.finding ?? finding}
          onClose={onClose}
          onCompleted={onCompleted}
          onCopyCreated={onCopyCreated}
          onContinueInCopy={onContinueInCopy}
          onExitToLibrary={onExitToLibrary}
          onCopyResultUnknown={onCopyResultUnknown}
        />
      </AntApp>,
    );
  });
}

function button(label: string): HTMLButtonElement {
  const target = Array.from(document.querySelectorAll('button'))
    .find((item) => item.textContent?.includes(label));
  if (!(target instanceof HTMLButtonElement)) throw new Error(`missing button: ${label}`);
  return target;
}

async function click(element: HTMLElement) {
  await act(async () => element.click());
}

async function fillFinalText(value: string) {
  const textarea = document.querySelector('textarea[aria-label="经确认的最终简历表述"]');
  if (!(textarea instanceof HTMLTextAreaElement)) throw new Error('missing textarea');
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
    setter?.call(textarea, value);
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

async function confirmTruth() {
  const input = document.querySelector('input[type="checkbox"]');
  if (!(input instanceof HTMLInputElement)) throw new Error('missing checkbox');
  await click(input);
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await new Promise((resolve) => window.setTimeout(resolve, 0));
  });
}

beforeEach(() => {
  state.copyResume.mockReset();
  state.updateResume.mockReset();
  state.ai.mockReset();
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: () => ({
      matches: false,
      addListener: () => undefined,
      removeListener: () => undefined,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      dispatchEvent: () => false,
    }),
  });
});

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
  document.querySelectorAll('.ant-message').forEach((node) => node.remove());
  root = undefined;
  container = undefined;
});

describe('ResumeFactSupplementWorkspace', () => {
  it('keeps the original visible, requires explicit confirmation, and creates a derived version without AI', async () => {
    const copied = { ...source, id: 8, title: '筱哲主简历 · 事实补充', is_master: false, parent_resume_id: 7 };
    const saved = {
      ...copied,
      content_json: { experience: [{ company: '云栖智能', highlights: ['将订单接口平均响应时间从 320ms 降至 180ms'] }] },
    };
    state.copyResume.mockResolvedValue(copied);
    state.updateResume.mockResolvedValue(saved);
    renderWorkspace();

    expect(document.body.textContent).toContain('负责订单服务');
    expect(document.body.textContent).toContain('系统不会替你估算数字');
    expect(document.body.textContent).toContain('基础简历');
    expect(document.body.textContent).not.toContain('#7');
    expect(document.body.textContent).not.toContain('/experience/0/highlights/0');
    expect(button('创建新版本并查看差异').disabled).toBe(true);

    await fillFinalText('将订单接口平均响应时间从 320ms 降至 180ms');
    await confirmTruth();
    expect(button('创建新版本并查看差异').disabled).toBe(false);
    await click(button('创建新版本并查看差异'));
    await flush();

    expect(state.copyResume).toHaveBeenCalledTimes(1);
    expect(state.copyResume).toHaveBeenCalledWith(7, expect.objectContaining({ title: expect.stringContaining('事实补充') }));
    expect(state.updateResume).toHaveBeenCalledWith(8, expect.objectContaining({
      content_json: saved.content_json,
    }));
    expect(onCopyCreated).toHaveBeenCalledWith(copied);
    expect(onCompleted).toHaveBeenCalledWith(saved);
    expect(state.ai).not.toHaveBeenCalled();
  });

  it('freezes and refreshes when the copied version update result is unknown', async () => {
    const copied = { ...source, id: 9, title: '筱哲主简历 · 事实补充', is_master: false, parent_resume_id: 7 };
    state.copyResume.mockResolvedValue(copied);
    state.updateResume.mockRejectedValueOnce(new Error('response lost'));
    renderWorkspace();
    await fillFinalText('支撑每月 120 万笔订单处理');
    await confirmTruth();
    await click(button('创建新版本并查看差异'));
    await flush();

    expect(document.body.textContent).toContain('保存结果待确认');
    expect(button('返回简历库核对')).toBeTruthy();
    expect((document.querySelector('button[aria-label="关闭事实补充工作台"]') as HTMLButtonElement).disabled).toBe(true);

    expect(state.copyResume).toHaveBeenCalledTimes(1);
    expect(state.updateResume).toHaveBeenCalledTimes(1);
    expect(state.updateResume.mock.calls[0][0]).toBe(9);
    expect(onCompleted).not.toHaveBeenCalled();
    expect(onCopyCreated).toHaveBeenCalledWith(copied);
    expect(onCopyResultUnknown).toHaveBeenCalledTimes(1);
  });

  it('freezes the form when a copy result cannot be confirmed without guessing a matching copy', async () => {
    state.copyResume.mockRejectedValue(new Error('network'));
    renderWorkspace();
    await fillFinalText('负责订单服务治理并完成稳定性复盘');
    await confirmTruth();
    await click(button('创建新版本并查看差异'));
    await flush();

    expect(document.body.textContent).toContain('创建结果待确认');
    expect(button('返回简历库核对')).toBeTruthy();
    expect((document.querySelector('textarea[aria-label="经确认的最终简历表述"]') as HTMLTextAreaElement | null)?.disabled).toBe(true);
    expect(state.updateResume).not.toHaveBeenCalled();
    expect(onCopyResultUnknown).toHaveBeenCalledTimes(1);
    await click(button('返回简历库核对'));
    expect(onExitToLibrary).toHaveBeenCalledTimes(1);
  });

  it('does not overwrite a copied version when the saved source changed before copying', async () => {
    state.copyResume.mockResolvedValue({
      ...source,
      id: 10,
      parent_resume_id: 7,
      is_master: false,
      content_json: {
        experience: [{ company: '云栖智能', highlights: ['已由另一处更新'] }],
      },
    });
    renderWorkspace();
    await fillFinalText('支撑每月 120 万笔订单处理');
    await confirmTruth();
    await click(button('创建新版本并查看差异'));
    await flush();

    expect(document.body.textContent).toContain('来源已变化');
    expect(button('打开已创建版本重新体检')).toBeTruthy();
    expect(state.copyResume).toHaveBeenCalledTimes(1);
    expect(state.updateResume).not.toHaveBeenCalled();
    await click(button('打开已创建版本重新体检'));
    expect(onContinueInCopy).toHaveBeenCalledWith(expect.objectContaining({ id: 10 }));
  });

  it('uses the full saved leaf for CAS when the audit preview is truncated', async () => {
    const fullText = '界'.repeat(241);
    const longSource = {
      ...source,
      content_json: { experience: [{ highlights: [fullText] }] },
    };
    const longFinding: ResumeAuditFinding = {
      ...finding,
      source: {
        path: '/experience/0/highlights/0',
        excerpt: `${'界'.repeat(160)}…`,
        fullText,
      },
    };
    const copied = { ...longSource, id: 11, parent_resume_id: 7, is_master: false };
    state.copyResume.mockResolvedValue(copied);
    state.updateResume.mockImplementation(async (id, input) => ({ ...copied, id, ...input }));

    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    onClose = vi.fn();
    onCompleted = vi.fn();
    onCopyCreated = vi.fn();
    onContinueInCopy = vi.fn();
    onExitToLibrary = vi.fn();
    onCopyResultUnknown = vi.fn();
    act(() => {
      root?.render(
        <AntApp>
          <ResumeFactSupplementWorkspace
            open
            source={longSource}
            finding={longFinding}
            onClose={onClose}
            onCompleted={onCompleted}
          />
        </AntApp>,
      );
    });

    expect((document.querySelector('textarea[aria-label="经确认的最终简历表述"]') as HTMLTextAreaElement).value)
      .toBe(fullText);
    await fillFinalText('完成超长经历要点的真实事实补充');
    await confirmTruth();
    await click(button('创建新版本并查看差异'));
    await flush();

    expect(state.updateResume).toHaveBeenCalledTimes(1);
    expect(onCompleted).toHaveBeenCalledTimes(1);
  });

  it('does not create another lineage while the source relationship is unconfirmed', async () => {
    renderWorkspace({ source: { ...source, is_master: false, parent_resume_id: 99 } });

    expect(document.body.textContent).toContain('关系待确认');
    await fillFinalText('完成真实事实补充');
    await confirmTruth();
    expect(button('创建新版本并查看差异').disabled).toBe(true);
    expect(state.copyResume).not.toHaveBeenCalled();
  });
});
