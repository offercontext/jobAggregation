import { describe, expect, it } from 'vitest';
import { buildEvidenceEntries, evidenceKey } from './evidenceLocator';

describe('interview studio evidence locator', () => {
  it('labels frozen source references and prior-answer follow-up references', () => {
    const refs = [
      { source: 'turn', path: '/turns/002/answer', excerpt: '我会先拆分接口边界。' },
      { source: 'jd', path: '/jd/text', excerpt: '负责 Python 服务的稳定性。' },
      { source: 'resume', path: '/resume/content_json/raw_text', excerpt: '维护过异步任务系统。' },
    ];

    expect(buildEvidenceEntries(refs)).toEqual([
      { key: evidenceKey(refs[0]), label: '上一轮回答 · 第 2 轮', ...refs[0] },
      { key: evidenceKey(refs[1]), label: '岗位描述 · 本次练习快照', ...refs[1] },
      { key: evidenceKey(refs[2]), label: '简历快照 · 简历原文', ...refs[2] },
    ]);
  });
  it('keeps distinct reference identities even when friendly labels match', () => {
    const first = { source: 'future_source', path: '/private/one', excerpt: '第一段依据' };
    const second = { ...first, path: '/private/two' };
    const entries = buildEvidenceEntries([first, second, first, { ...first, excerpt: ' ' }]);
    expect(entries).toHaveLength(2);
    expect(entries.map((entry) => entry.key)).toEqual([evidenceKey(first), evidenceKey(second)]);
    expect(entries.map((entry) => entry.label)).toEqual(['参考依据 · 来源片段', '参考依据 · 来源片段']);
  });
});
