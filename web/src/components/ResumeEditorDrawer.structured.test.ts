import { describe, expect, it } from 'vitest';
import source from './ResumeEditorDrawer.tsx?raw';

describe('ResumeEditorDrawer structured editor contract', () => {
  it('defaults to structured sections with entry controls and keeps JSON secondary', () => {
    expect(source).toContain('parseStructuredResume');
    expect(source).toContain('serializeStructuredResume');
    expect(source).toContain('基本信息');
    expect(source).toContain('教育经历');
    expect(source).toContain('工作经历');
    expect(source).toContain('项目经历');
    expect(source).toContain('技能');
    expect(source).toContain('高级 JSON');
    expect(source).toContain('上移');
    expect(source).toContain('下移');
    expect(source).toContain('删除');
  });

  it('blocks ordinary overwrite when recovery is required and confirms dirty close', () => {
    expect(source).toContain("parsed.mode === 'recovery'");
    expect(source).toContain('无法安全使用结构化表单');
    expect(source).toContain('Modal.confirm');
    expect(source).toContain('有未保存的更改');
    expect(source).toContain("parseStructuredResume(content).mode !== 'structured'");
  });
});
