import { describe, expect, it } from 'vitest';
import source from './KnowledgeSourcesView.tsx?raw';

describe('KnowledgeSourcesView user language', () => {
  it('uses product language for visible knowledge concepts', () => {
    expect(source).toContain('来源依据');
    expect(source).toContain('资料导读');
    expect(source).toContain('保存版本');
    expect(source).toContain('内容整理');
    expect(source).toContain('处理记录');
    expect(source).toContain('技术详情');
    expect(source).toContain('搜索来源依据');
    expect(source).not.toContain('选择左侧的 Source');
    expect(source).not.toContain('未匹配 Evidence');
    expect(source).not.toContain('确认 Extraction 已完成');
    expect(source).not.toContain('条 Evidence');
    expect(source).not.toContain('Evidence ID');
    expect(source).not.toContain('已有 Source');
    expect(source).not.toContain('新 Source');
    expect(source).not.toContain('重复 Evidence');
    expect(source).not.toContain('Origin 记录');
  });
});
