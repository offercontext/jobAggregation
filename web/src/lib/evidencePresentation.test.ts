import { describe, expect, it } from 'vitest';
import { evidenceLocationLabel, evidenceSourceLabel, resumeEvidenceLocation } from './evidencePresentation';

describe('evidence presentation', () => {
  it.each(['/experience/0/highlights/1', '/content_json/experience/0/highlights/1', '/resume/content_json/experience/0/highlights/1'])(
    'normalizes the display of %s without changing the reference', (path) => {
      expect(resumeEvidenceLocation(path)).toBe('工作经历1 · 经历亮点 2');
    },
  );
  it('labels bundled resume, JD, knowledge and user evidence', () => {
    expect(evidenceLocationLabel('evidence_bundle', '/resume/content_json/projects/0/highlights/2')).toBe('项目经历1 · 经历亮点 3');
    expect(evidenceLocationLabel('jd', '/jd/text')).toBe('岗位要求');
    expect(evidenceSourceLabel('knowledge_evidence')).toBe('已确认知识依据');
    expect(evidenceLocationLabel('knowledge_evidence', '/knowledge/0/evidence/0')).toBe('知识证据片段');
    expect(evidenceLocationLabel('user_assertion', '/user_assertions/0/text')).toBe('我的补充说明 1');
    expect(evidenceSourceLabel('confirmed_readiness_feedback')).toBe('已确认复盘重点');
    expect(evidenceLocationLabel('confirmed_readiness_feedback', '/readiness_feedback/0/statement')).toBe('复盘准备重点 1');
    expect(evidenceLocationLabel('confirmed_readiness_feedback', '/readiness_feedback/0/evidence/1/excerpt')).toBe('复盘准备重点 1 · 证据 2');
  });
  it('does not expose unrecognized machine identifiers as visible labels', () => {
    expect(evidenceSourceLabel('internal_source')).toBe('参考依据');
    expect(evidenceSourceLabel('__proto__')).toBe('参考依据');
    expect(evidenceSourceLabel('toString')).toBe('参考依据');
    expect(evidenceLocationLabel('internal_source', '/internal/token')).toBe('来源片段');
    expect(resumeEvidenceLocation('/unknown_secret')).toBe('其他内容');
  });
});
