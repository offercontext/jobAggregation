// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { Resume } from '@/types/resume';
const update = vi.hoisted(() => vi.fn());
vi.mock('@/services/resumes', () => ({ updateResume: update }));
vi.mock('./ResumeImportReview', () => ({ default: () => <div role="dialog">分类核对已打开</div> }));
vi.mock('./ResumeEvidenceAuditPanel', () => ({ default: () => null }));
vi.mock('./ResumeFactSupplementWorkspace', () => ({ default: () => null }));
import ResumeEditorDrawer from './ResumeEditorDrawer';
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
let root: Root; let host: HTMLDivElement; let client: QueryClient;
const resume: Resume = { id: 7, title: '林晓', name: '林晓', source: 'upload', content_json: { raw_text: '林晓\n滨海大学\n技能 Python' }, parsed_data: '林晓\n滨海大学\n技能 Python', created_at: '2026-09-01T00:00:00Z', missing_sections: [], completion_percent: 0, is_master: true, parent_resume_id: null, file_path: '', source_file_path: '', parse_status: 'text-ready', deleted_at: null, is_complete: false };
const button = (label: string) => { const node = Array.from(host.querySelectorAll('button')).find((b) => b.textContent?.replace(/\s/g, '') === label); if (!node) throw new Error(label); return node; };
const click = async (label: string) => { await act(async () => button(label).click()); };
beforeEach(async () => { update.mockReset(); update.mockResolvedValue(resume); Object.defineProperty(window, 'matchMedia', { configurable: true, value: () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }) }); host = document.createElement('div'); document.body.append(host); root = createRoot(host); client = new QueryClient(); await act(async () => root.render(<QueryClientProvider client={client}><ResumeEditorDrawer resume={resume} open onClose={() => {}} /></QueryClientProvider>)); });
afterEach(() => { act(() => root.unmount()); host.remove(); client.clear(); });
it('separates read-only PDF source from other text', async () => {
  await click('PDF提取原文'); const raw = host.querySelector('textarea[aria-label="PDF 提取原文"]') as HTMLTextAreaElement;
  expect(raw.readOnly).toBe(true); expect(raw.value).toContain('滨海大学');
  await click('其他'); const other = host.querySelector('textarea') as HTMLTextAreaElement;
  expect(other.value).toBe('');
  await act(async () => { Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set?.call(other, '自愿补充'); other.dispatchEvent(new Event('input', { bubbles: true })); });
  await click('保存'); expect(update.mock.calls[0][1].content_json).toMatchObject({ raw_text: resume.parsed_data, additional_text: '自愿补充' });
});
it('opens classification explicitly and blocks it with unsaved manual edits', async () => {
  expect(host.textContent).toContain('已提取文字，待分类');
  const title = host.querySelector('input[placeholder="简历标题"]') as HTMLInputElement;
  await act(async () => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set?.call(title, '未保存'); title.dispatchEvent(new Event('input', { bubbles: true })); });
  await click('AI分类并核对'); expect(host.querySelector('[role="dialog"]')).toBeNull();
  await act(async () => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set?.call(title, '林晓'); title.dispatchEvent(new Event('input', { bubbles: true })); });
  await click('AI分类并核对'); expect(host.textContent).toContain('分类核对已打开');
});
it('opening advanced JSON without editing does not falsely block classification', async () => {
  await click('高级JSON');
  await click('AI分类并核对'); expect(host.textContent).toContain('分类核对已打开');
});
it('keeps malformed raw text in recovery instead of replacing it with parsed text', async () => {
  const malformed = { ...resume, content_json: { raw_text: 1 } } as unknown as Resume;
  await act(async () => root.render(<QueryClientProvider client={client}><ResumeEditorDrawer resume={malformed} open onClose={() => {}} /></QueryClientProvider>));
  expect(button('AI分类并核对').disabled).toBe(true);
  expect(host.textContent).toContain('历史字段类型与结构化编辑器不兼容');
  expect(host.querySelector('textarea')?.value).toContain('"raw_text": 1');
  expect(update).not.toHaveBeenCalled();
});
