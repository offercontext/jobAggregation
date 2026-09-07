import { describe, expect, it } from 'vitest';
import { importFieldLabel, isImportFieldProtected, reindexImportFields } from './resumeImport';

describe('resume import candidates', () => {
  it('protects entire nonempty arrays and nested manual values', () => {
    const saved = { contact: { name: '手工姓名' }, experience: [{ company: '手工公司' }], skills: ['Python'], career_intent: { target_roles: ['运营'] } };
    expect(isImportFieldProtected(saved, 'contact.name')).toBe(true);
    expect(isImportFieldProtected(saved, 'contact.phone')).toBe(false);
    expect(isImportFieldProtected(saved, 'experience.1.company')).toBe(true);
    expect(isImportFieldProtected(saved, 'skills.1')).toBe(true);
    expect(isImportFieldProtected(saved, 'career_intent.target_roles.0')).toBe(true);
    expect(isImportFieldProtected({ contact: 'legacy' }, 'contact.name')).toBe(true);
    expect(isImportFieldProtected({ contact: { name: '  ' } }, 'contact.name')).toBe(false);
    expect(isImportFieldProtected({ skills: '' }, 'skills.0')).toBe(true);
  });
  it('reindexes deselected entries and nested highlights without mutating candidates', () => {
    const fields = [{ path: 'experience.2.company', value: '星河', evidence: '星河' }, { path: 'experience.2.highlights.3', value: '分析', evidence: '分析' }, { path: 'skills.4', value: 'Python', evidence: 'Python' }];
    expect(reindexImportFields(fields).map((f) => f.path)).toEqual(['experience.0.company', 'experience.0.highlights.0', 'skills.0']);
    expect(fields[0].path).toBe('experience.2.company');
  });
  it('uses human labels rather than private paths', () => {
    expect(importFieldLabel('education.0.school')).toBe('教育经历 1 · 学校');
    expect(importFieldLabel('experience.1.highlights.0')).toBe('工作经历 2 · 工作亮点 1');
  });
});
