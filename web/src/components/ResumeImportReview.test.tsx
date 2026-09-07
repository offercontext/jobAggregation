// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { Resume } from '@/types/resume';
const api = vi.hoisted(() => ({ previewResumeStructure: vi.fn(), confirmResumeStructure: vi.fn(), getResume: vi.fn() }));
vi.mock('@/services/resumes', () => api);
import ResumeImportReview from './ResumeImportReview';
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
let root: Root; let host: HTMLDivElement;
const saved = vi.fn(); const close = vi.fn();
const resume: Resume = { id: 7, title: '林晓简历', name: '林晓简历', source: 'upload', content_json: { raw_text: '林晓 滨海大学 Python', contact: { name: '手工姓名' } }, parsed_data: '林晓 滨海大学 Python', file_path: '', source_file_path: '', parse_status: 'text-ready', deleted_at: null, is_complete: false, completion_percent: 0, missing_sections: [], is_master: true, parent_resume_id: null, created_at: '2026-09-01T00:00:00Z' };
const preview = { resume_id: 7, source_fingerprint: 'version-1', fields: [{ path: 'contact.name', value: '林晓', evidence: '林晓' }, { path: 'education.0.school', value: '滨海大学', evidence: '滨海大学' }] };
const button = (label: string) => { const value = Array.from(document.querySelectorAll('button')).find((b) => b.textContent?.replace(/\s/g, '').includes(label)); if (!value) throw new Error(label); return value; };
const click = async (label: string) => { await act(async () => { button(label).click(); }); };
const render = async (value = resume) => { await act(async () => root.render(<ResumeImportReview resume={value} onSaved={saved} onClose={close} />)); };
beforeEach(() => { vi.clearAllMocks(); Object.defineProperty(window, 'matchMedia', { configurable: true, value: () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }) }); host = document.createElement('div'); document.body.append(host); root = createRoot(host); api.previewResumeStructure.mockResolvedValue(preview); api.confirmResumeStructure.mockResolvedValue(resume); });
afterEach(() => { act(() => root.unmount()); host.remove(); });
it('requires an explicit AI action, preserves manual fields and confirms only chosen blank fields', async () => {
  await render(); expect(api.previewResumeStructure).not.toHaveBeenCalled();
  await click('开始分类'); expect(api.previewResumeStructure).toHaveBeenCalledTimes(1);
  expect(document.body.textContent).toContain('已有内容将保留');
  expect(api.confirmResumeStructure).not.toHaveBeenCalled();
  await click('确认填入空白模块');
  expect(api.confirmResumeStructure.mock.calls[0][0]).toBe(7);
  expect(api.confirmResumeStructure.mock.calls[0][1]).toEqual({ source_fingerprint: 'version-1', fields: [preview.fields[1]] });
  expect(saved).toHaveBeenCalledWith(resume);
});
it('cancelling a preview never writes', async () => { await render(); await click('开始分类'); await click('取消'); expect(close).toHaveBeenCalledOnce(); expect(api.confirmResumeStructure).not.toHaveBeenCalled(); });
it('ignores a late preview from another resume and aborts its request', async () => {
  let resolve!: (value: typeof preview) => void;
  api.previewResumeStructure.mockReturnValue(new Promise((done) => { resolve = done; }));
  await render(); await click('开始分类'); const signal = api.previewResumeStructure.mock.calls[0][1] as AbortSignal;
  await render({ ...resume, id: 8 }); expect(signal.aborted).toBe(true);
  await act(async () => resolve(preview)); expect(document.body.textContent).not.toContain('滨海大学'); expect(saved).not.toHaveBeenCalled();
});
it('keeps failed confirmation candidates and recovers by reading, never rerunning AI', async () => {
  api.confirmResumeStructure.mockRejectedValue(new Error('secret raw provider content'));
  api.getResume.mockResolvedValue(resume);
  await render(); await click('开始分类'); await click('确认填入空白模块');
  expect(document.body.textContent).not.toContain('secret raw provider content');
  expect(document.body.textContent).toContain('滨海大学');
  await click('重新读取简历'); expect(api.getResume).toHaveBeenCalledOnce(); expect(api.previewResumeStructure).toHaveBeenCalledOnce(); expect(api.confirmResumeStructure).toHaveBeenCalledOnce();
  expect(saved).not.toHaveBeenCalled(); expect(document.body.textContent).toContain('滨海大学');
  expect(button('确认填入空白模块').disabled).toBe(false);
});
it('retains old candidates after a changed-source read until explicit exit', async () => {
  api.confirmResumeStructure.mockRejectedValue(new Error('response lost'));
  const changed = { ...resume, content_json: { ...resume.content_json, contact: { name: '新姓名' } } };
  api.getResume.mockResolvedValue(changed);
  await render(); await click('开始分类'); await click('确认填入空白模块'); await click('重新读取简历');
  expect(saved).not.toHaveBeenCalled(); expect(document.body.textContent).toContain('滨海大学');
  expect(document.body.textContent).toContain('来源或内容已变化');
  await click('结束核对并返回'); expect(saved).toHaveBeenCalledWith(changed);
  expect(api.confirmResumeStructure).toHaveBeenCalledOnce();
});
it('revokes a preview when only extraction metadata changes', async () => {
  let resolve!: (value: typeof preview) => void;
  api.previewResumeStructure.mockReturnValue(new Promise((done) => { resolve = done; }));
  await render(); await click('开始分类'); const signal = api.previewResumeStructure.mock.calls[0][1] as AbortSignal;
  await render({ ...resume, parsed_data: '新的提取文本' }); expect(signal.aborted).toBe(true);
  await act(async () => resolve(preview)); expect(document.body.textContent).not.toContain('滨海大学');
});
it('a generation failure does not clear source or write', async () => { api.previewResumeStructure.mockRejectedValue({ response: { data: { code: 'resume_structure_empty_source' } } }); await render(); await click('开始分类'); expect(document.body.textContent).toContain('扫描件'); expect(api.confirmResumeStructure).not.toHaveBeenCalled(); });
it('retains editable candidates after a definite validation rejection without another AI call', async () => {
  api.confirmResumeStructure.mockRejectedValue({ response: { status: 422, data: { code: 'resume_structure_unsupported_value' } } });
  await render(); await click('开始分类'); await click('确认填入空白模块');
  expect(document.body.textContent).toContain('候选未保存');
  expect(button('确认填入空白模块').disabled).toBe(false);
  expect(api.previewResumeStructure).toHaveBeenCalledOnce(); expect(api.confirmResumeStructure).toHaveBeenCalledOnce();
});
it('rejects a mismatched preview identity', async () => { api.previewResumeStructure.mockResolvedValue({ ...preview, resume_id: 99 }); await render(); await click('开始分类'); expect(document.body.textContent).not.toContain('滨海大学'); expect(api.confirmResumeStructure).not.toHaveBeenCalled(); });
