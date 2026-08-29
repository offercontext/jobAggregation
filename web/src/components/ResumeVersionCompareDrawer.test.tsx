// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Resume } from '@/types/resume';
import ResumeVersionCompareDrawer from './ResumeVersionCompareDrawer';

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

function makeResume(id: number, overrides: Partial<Resume> = {}): Resume {
  return {
    id,
    name: `简历 ${id}`,
    file_path: '',
    parsed_data: '',
    parse_status: 'text-ready',
    title: `中文版本 ${id}`,
    is_master: false,
    parent_resume_id: null,
    source: 'manual',
    source_file_path: '',
    content_json: {
      contact: { name: `候选人 ${id}` },
      experience: [{ highlights: [`经历 ${id}`] }],
    },
    deleted_at: null,
    created_at: `2026-08-06T00:0${id}:00Z`,
    completion_percent: 60,
    missing_sections: [],
    is_complete: true,
    ...overrides,
  };
}

let root: Root | null = null;
let host: HTMLDivElement | null = null;

function renderDrawer(target: Resume, candidates: Resume[], onClose = vi.fn()) {
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => {
    root?.render(
      <ResumeVersionCompareDrawer
        open
        target={target}
        candidates={candidates}
        onClose={onClose}
      />,
    );
  });
  return onClose;
}

function drawerRoot() {
  const element = document.querySelector('[data-resume-version-compare]');
  if (!(element instanceof HTMLElement)) throw new Error('compare drawer not found');
  return element;
}

function compareSelect() {
  const select = drawerRoot().querySelector<HTMLSelectElement>('select[aria-label="基准版本"]');
  if (!select) throw new Error('baseline select not found');
  return select;
}

async function chooseBaseline(value: string) {
  await act(async () => {
    const select = compareSelect();
    select.value = value;
    select.dispatchEvent(new Event('change', { bubbles: true }));
  });
}

beforeEach(() => {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: () => ({
      matches: false,
      media: '',
      onchange: null,
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
  host?.remove();
  document.querySelectorAll('.ant-drawer-root').forEach((element) => element.remove());
  root = null;
  host = null;
});

describe('ResumeVersionCompareDrawer', () => {
  it('prefers the parent baseline and sorts the remaining candidates by descending id', () => {
    const target = makeResume(3, { parent_resume_id: 2 });
    const parent = makeResume(2, { is_master: true });
    const master = makeResume(1, { is_master: true });
    const other = makeResume(9);

    renderDrawer(target, [target, master, other, parent]);

    const select = compareSelect();
    expect(select.value).toBe('2');
    expect(Array.from(select.options).map((option) => option.value)).toEqual(['', '2', '9', '1']);
    expect(select.options[1].textContent).toContain('中文版本 2');
    expect(select.options[1].textContent).toContain('基础简历');
    expect(select.options[1].textContent).not.toContain('#2');
    expect(select.options[1].textContent).not.toContain('父版本');
    expect(select.textContent).not.toContain('v1');
    expect(select.textContent).not.toContain('v2');
  });

  it('does not replace a missing or absent parent with another candidate', () => {
    const target = makeResume(3, { parent_resume_id: 99 });
    renderDrawer(target, [target, makeResume(2), makeResume(1)]);
    expect(compareSelect().value).toBe('');
    expect(drawerRoot().textContent).toContain('请选择一个基准版本');

    act(() => root?.unmount());
    host?.remove();
    host = null;
    root = null;
    renderDrawer(makeResume(4), [makeResume(4), makeResume(1)]);
    expect(compareSelect().value).toBe('');
  });

  it('does not offer a deleted parent as a baseline or expose relationship internals', () => {
    const target = makeResume(3, { parent_resume_id: 2 });
    const deletedParent = makeResume(2, { is_master: true, deleted_at: '2026-08-30T00:00:00Z' });

    renderDrawer(target, [target, deletedParent, makeResume(1)]);

    expect(compareSelect().value).toBe('');
    expect(Array.from(compareSelect().options).map((option) => option.textContent).join(' ')).not.toContain('中文版本 2');
    expect(drawerRoot().textContent).not.toContain('parent_resume_id');
    expect(drawerRoot().textContent).not.toContain('is_master');
  });

  it('does not offer hidden resume rows as comparison candidates', () => {
    const target = makeResume(4);
    const hidden = { ...makeResume(3), hidden: true } as Resume;
    const invisible = { ...makeResume(2), visible: false } as Resume;

    renderDrawer(target, [target, hidden, invisible, makeResume(1)]);

    const labels = Array.from(compareSelect().options).map((option) => option.textContent).join(' ');
    expect(labels).not.toContain('中文版本 3');
    expect(labels).not.toContain('中文版本 2');
    expect(labels).toContain('中文版本 1');
  });

  it('keeps a manual baseline across candidate refresh and reapplies only a new target parent', async () => {
    const target = makeResume(3, { parent_resume_id: 2 });
    const parent = makeResume(2);
    const other = makeResume(1);
    renderDrawer(target, [target, parent, other]);

    await chooseBaseline('1');
    expect(compareSelect().value).toBe('1');

    act(() => {
      root?.render(
        <ResumeVersionCompareDrawer
          open
          target={target}
          candidates={[target, { ...parent, title: '刷新后的父版本' }, other]}
          onClose={vi.fn()}
        />,
      );
    });
    expect(compareSelect().value).toBe('1');

    const nextTarget = makeResume(4, { parent_resume_id: 2 });
    act(() => {
      root?.render(
        <ResumeVersionCompareDrawer
          open
          target={nextTarget}
          candidates={[nextTarget, parent, other]}
          onClose={vi.fn()}
        />,
      );
    });
    expect(compareSelect().value).toBe('2');

    act(() => {
      root?.render(
        <ResumeVersionCompareDrawer
          open
          target={nextTarget}
          candidates={[nextTarget, other]}
          onClose={vi.fn()}
        />,
      );
    });
    expect(compareSelect().value).toBe('');
    expect(drawerRoot().textContent).toContain('请选择一个基准版本');
  });

  it('shows saved-content comparison, safe values, and only expandable truncated text', async () => {
    const base = makeResume(1, {
      title: '基准标题',
      source: 'manual',
      is_master: true,
      content_json: { contact: { name: '林晓' }, raw_text: '短内容' },
    });
    const target = makeResume(2, {
      parent_resume_id: 1,
      title: '目标标题',
      source: 'sample_copy',
      is_master: false,
      content_json: { contact: { name: '林晓' }, raw_text: '长文本'.repeat(80) },
    });

    renderDrawer(target, [target, base]);
    expect(drawerRoot().textContent).toContain('仅比较当前已保存的简历内容');
    expect(drawerRoot().textContent).toContain('数组按位置比较，不推断经历、公司或项目是否为同一项');
    expect(drawerRoot().textContent).toContain('修改 1');
    expect(drawerRoot().textContent).toContain('其他结构化字段');
    expect(drawerRoot().textContent).not.toContain('/raw_text');
    expect(drawerRoot().querySelectorAll('details')).toHaveLength(1);

    const details = drawerRoot().querySelector('details');
    const summary = details?.querySelector('summary');
    expect(details?.open).toBe(false);
    expect(details?.querySelector('pre')).toBeNull();
    expect(details?.parentElement?.querySelector('pre')?.textContent).toContain('长文本');
    expect(summary?.textContent).toContain('展开完整内容');
    await act(async () => summary?.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    await act(async () => details?.dispatchEvent(new Event('toggle', { bubbles: true })));
    expect(drawerRoot().querySelector('details')?.open).toBe(true);
    expect(details?.querySelector('pre')?.textContent).toContain('长文本');

    const nextTarget = makeResume(4, {
      parent_resume_id: 1,
      content_json: { contact: { name: '周宁' }, raw_text: '另一段长文本'.repeat(80) },
    });
    act(() => {
      root?.render(
        <ResumeVersionCompareDrawer
          open
          target={nextTarget}
          candidates={[nextTarget, base]}
          onClose={vi.fn()}
        />,
      );
    });
    expect(drawerRoot().querySelector('details')?.open).toBe(false);
  });

  it('renders only the existing side for added and removed items', () => {
    const base = makeResume(1, {
      content_json: { removed: 'gone' },
    });
    const target = makeResume(2, {
      parent_resume_id: 1,
      content_json: { added: 'new' },
    });

    renderDrawer(target, [target, base]);

    const added = Array.from(drawerRoot().querySelectorAll<HTMLElement>('[data-diff-module="other"]'))
      .find((item) => item.textContent?.includes('new'));
    const removed = Array.from(drawerRoot().querySelectorAll<HTMLElement>('[data-diff-module="other"]'))
      .find((item) => item.textContent?.includes('gone'));
    expect(added?.querySelectorAll('pre')).toHaveLength(1);
    expect(added?.textContent).toContain('new');
    expect(added?.textContent).not.toContain('不存在');
    expect(removed?.querySelectorAll('pre')).toHaveLength(1);
    expect(removed?.textContent).toContain('gone');
    expect(removed?.textContent).not.toContain('不存在');
  });

  it('renders the Chinese empty state and calls only the close callback', () => {
    const onClose = renderDrawer(
      makeResume(2, { parent_resume_id: 1, content_json: { contact: { name: '林晓' } } }),
      [makeResume(2, { parent_resume_id: 1, content_json: { contact: { name: '林晓' } } }), makeResume(1, { content_json: { contact: { name: '林晓' } } })],
    );

    expect(drawerRoot().textContent).toContain('暂无差异');
    const close = drawerRoot().querySelector<HTMLButtonElement>('button[aria-label="关闭版本对比"]');
    expect(close).not.toBeNull();
    act(() => close?.click());
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('focuses the dialog, traps keyboard focus, closes on Escape, and restores the opener', () => {
    const opener = document.createElement('button');
    opener.textContent = 'open compare';
    document.body.appendChild(opener);
    opener.focus();

    const onClose = renderDrawer(
      makeResume(2, { parent_resume_id: 1 }),
      [makeResume(2, { parent_resume_id: 1 }), makeResume(1)],
      vi.fn(),
    );
    const dialog = drawerRoot().querySelector<HTMLElement>('[role="dialog"]');
    const select = compareSelect();
    expect(dialog).not.toBeNull();
    expect(document.activeElement).toBe(dialog);

    const close = drawerRoot().querySelector<HTMLButtonElement>('button');
    expect(close).not.toBeNull();
    close?.focus();
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true }));
    expect(document.activeElement).toBe(select);

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(onClose).toHaveBeenCalledTimes(1);
    act(() => {
      root?.render(
        <ResumeVersionCompareDrawer
          open={false}
          target={makeResume(2, { parent_resume_id: 1 })}
          candidates={[makeResume(2, { parent_resume_id: 1 }), makeResume(1)]}
          onClose={onClose}
        />,
      );
    });
    expect(document.activeElement).toBe(opener);
    opener.remove();
  });
});
