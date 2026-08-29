import { describe, expect, it } from 'vitest';
import {
  formatResumeLineage,
  mapMaterialLabel,
  type MaterialLabelInput,
} from './materialLabels';

describe('shared material labels', () => {
  it.each([
    ['base', '基础简历'],
    ['job_variant', '岗位版本'],
    ['independent', '其他简历'],
    ['relationship_unknown', '关系待确认'],
  ] as const)('maps %s to one user-facing label', (kind, label) => {
    expect(mapMaterialLabel(kind)).toBe(label);
  });

  it('formats valid lineage with a visible parent title and never an internal identity', () => {
    const lineage: MaterialLabelInput = {
      kind: 'job_variant',
      label: '岗位版本',
      detail: '基于 后端基础简历',
    };

    expect(formatResumeLineage(lineage)).toBe('岗位版本 · 基于 后端基础简历');
    expect(formatResumeLineage(lineage)).not.toMatch(/#|parent_resume_id|is_master|\bid\b/i);
  });

  it('never invents a parent detail for an anomalous relationship', () => {
    expect(formatResumeLineage({ kind: 'relationship_unknown', label: '关系待确认', detail: '基于 #7' })).toBe('关系待确认');
  });

  it('uses safe bounded labels for known non-resume material categories', () => {
    expect(mapMaterialLabel('experience_story')).toBe('经历故事');
    expect(mapMaterialLabel('confirmed_capture')).toBe('已确认面试片段');
    expect(mapMaterialLabel('external_reference')).toBe('参考资料');
    expect(mapMaterialLabel('unclassified')).toBe('暂无法归类');
  });
});
