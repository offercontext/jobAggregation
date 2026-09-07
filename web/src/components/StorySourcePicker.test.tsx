// @vitest-environment jsdom
import { act, useState } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import StorySourcePicker from './StorySourcePicker';
import type { InterviewStorySourceCandidates, InterviewStorySourceSelection } from '@/types/interviewStory';

const sources: InterviewStorySourceCandidates = {
  resumes: [{ id: 2, label: '林晓 · 后端简历', leaves: [{ path: '/content_json/raw_text', preview: '我负责服务稳定性。'.repeat(40) }] }],
  interview_notes: [{ id: 4, label: '星云科技 · 一面', leaves: [{ path: '/questions', preview: '如何定位慢请求？' }] }],
  mock_turns: [{ attempt_id: 7, turn_no: 4, label: '模拟面试 #7 · 第4题', leaves: [{ path: '/turns/004/question', preview: '你如何做监控？' }, { path: '/turns/004/answer', preview: '我先定义指标。' }] }],
};
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
let root: Root; let host: HTMLDivElement;
const toggle = vi.fn();
beforeEach(() => { host = document.createElement('div'); document.body.append(host); root = createRoot(host); toggle.mockReset(); });
afterEach(() => { act(() => root.unmount()); host.remove(); });
const render = (frozen = false) => act(() => root.render(<StorySourcePicker candidates={sources} selections={[]} frozen={frozen} onToggle={toggle} />));
it('starts folded and unselected, exposes human labels and preserves exact question/answer identities', () => {
  render();
  expect(host.textContent).not.toMatch(/\/content_json|\/turns/);
  expect(host.querySelector('details')?.open).toBe(false);
  expect(host.querySelectorAll('input:checked')).toHaveLength(0);
  const answer = Array.from(host.querySelectorAll('label')).find((l) => l.textContent?.includes('第4题 · 我的回答'))?.querySelector('input');
  act(() => answer?.click());
  expect(toggle).toHaveBeenCalledTimes(1);
  expect(toggle).toHaveBeenCalledWith({ source_kind: 'mock_turn', source_id: 7, path: '/turns/004/answer' });
});
it('search and category changes never select or discard items, and selected-only brings them back', () => {
  function Harness() {
    const [selected, setSelected] = useState<InterviewStorySourceSelection[]>([]);
    return <StorySourcePicker candidates={sources} selections={selected} frozen={false} onToggle={(s) => { toggle(s); setSelected([s]); }} />;
  }
  act(() => root.render(<Harness />));
  act(() => (host.querySelector('input[type="checkbox"]') as HTMLInputElement).click());
  const search = host.querySelector('input[type="search"]')!;
  act(() => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(search, '不存在'); search.dispatchEvent(new Event('input', { bubbles: true })); });
  expect(host.textContent).toContain('没有匹配的材料');
  act(() => Array.from(host.querySelectorAll('button')).find((b) => b.textContent?.includes('只看已选'))!.click());
  expect(host.textContent).toContain('林晓 · 后端简历');
  expect(host.querySelectorAll('input:checked')).toHaveLength(1);
  expect(toggle).toHaveBeenCalledTimes(1);
});
it('expands long previews without selecting and frozen sources cannot be toggled', () => {
  render(true);
  const expand = Array.from(host.querySelectorAll('button')).find((b) => b.textContent === '展开原文预览')!;
  act(() => expand.click());
  expect(host.textContent).toContain('收起原文预览');
  act(() => (host.querySelector('input[type="checkbox"]') as HTMLInputElement).click());
  expect(toggle).not.toHaveBeenCalled();
  expect(host.querySelector('input[type="checkbox"]')).toHaveProperty('disabled', true);
});
it('selected-only omits empty question sections', () => {
  const candidates = { ...sources, mock_turns: [...sources.mock_turns, { attempt_id: 7, turn_no: 5, label: '第5题', leaves: [{ path: '/turns/005/answer', preview: '另一条回答' }] }] };
  act(() => root.render(<StorySourcePicker candidates={candidates} selections={[{ source_kind: 'mock_turn', source_id: 7, path: '/turns/004/answer' }]} frozen={false} onToggle={toggle} />));
  act(() => Array.from(host.querySelectorAll('button')).find((b) => b.textContent?.includes('只看已选'))!.click());
  expect(Array.from(host.querySelectorAll('h4')).map((node) => node.textContent)).toEqual(['第4题']);
});
