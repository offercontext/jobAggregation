import { expect, it } from 'vitest';
import { storySourceLabel, storySourceGroups } from './storySourcePresentation';

it('uses human labels without leaking unknown paths', () => {
  expect(storySourceLabel('resume_version', '/content_json/skills/0')).toBe('技能 · 第1项');
  expect(storySourceLabel('resume_version', '/content_json/experience/0/company')).toBe('工作经历1 · 公司');
  expect(storySourceLabel('resume_version', '/content_json/raw_text')).toBe('简历原文');
  expect(storySourceLabel('mock_turn', '/turns/004/answer')).toBe('第4题 · 我的回答');
  expect(storySourceLabel('mock_turn', '/turns/004/question')).toBe('第4题 · 面试官提问');
  expect(storySourceLabel('interview_note', '/self_reflection')).toBe('我的复盘');
  expect(storySourceLabel('resume_version', '/content_json/private~1key')).toBe('其他内容');
});

it('groups mock turns by attempt without conflating selection identity or mutating input', () => {
  const sources = { resumes: [], interview_notes: [], mock_turns: [
    { attempt_id: 7, turn_no: 1, label: '模拟面试 #7 · 第1题', leaves: [{ path: '/turns/001/question', preview: '为什么？' }, { path: '/turns/001/answer', preview: '因为…' }] },
    { attempt_id: 7, turn_no: 2, label: '模拟面试 #7 · 第2题', leaves: [{ path: '/turns/002/answer', preview: '我负责排查' }] },
    { attempt_id: 8, turn_no: 1, label: '模拟面试 #8 · 第1题', leaves: [{ path: '/turns/001/answer', preview: '另一次回答' }] },
  ] };
  const before = JSON.stringify(sources);
  const groups = storySourceGroups(sources);
  expect(groups).toHaveLength(2);
  expect(groups[0].title).toBe('模拟面试记录 7');
  expect(storySourceGroups({ ...sources, mock_turns: sources.mock_turns.slice(2) })[0].title).toBe('模拟面试记录 8');
  expect(groups[0].sections).toHaveLength(2);
  expect(groups[0].sections[1].leaves[0].selection).toEqual({ source_kind: 'mock_turn', source_id: 7, path: '/turns/002/answer' });
  expect(JSON.stringify(sources)).toBe(before);
});
