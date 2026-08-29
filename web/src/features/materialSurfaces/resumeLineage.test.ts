import { describe, expect, it } from 'vitest';
import type { Resume } from '@/types/resume';
import { resolveResumeLineage } from './resumeLineage';

function makeResume(id: number, overrides: Partial<Resume> = {}): Resume {
  return {
    id,
    name: `简历 ${id}`,
    file_path: '',
    parsed_data: '',
    parse_status: 'text-ready',
    title: `版本 ${id}`,
    is_master: false,
    parent_resume_id: null,
    source: 'manual',
    source_file_path: '',
    content_json: {},
    deleted_at: null,
    created_at: '2026-08-30T00:00:00Z',
    completion_percent: 100,
    missing_sections: [],
    is_complete: true,
    ...overrides,
  };
}

describe('resolveResumeLineage', () => {
  it('classifies the visible master without a parent as the base resume', () => {
    const base = makeResume(1, { is_master: true });

    expect(resolveResumeLineage([base], 1)).toEqual({
      kind: 'base',
      label: '基础简历',
    });
  });

  it('classifies a visible derived resume and exposes only the parent record for safe presentation', () => {
    const base = makeResume(1, { is_master: true, title: '后端基础简历' });
    const variant = makeResume(2, { parent_resume_id: 1, title: '岗位定制版本' });

    const result = resolveResumeLineage([variant, base], 2);

    expect(result.kind).toBe('job_variant');
    expect(result.label).toBe('岗位版本');
    expect(result.parent).toBe(base);
    expect(result.detail).toBe('基于 后端基础简历');
  });

  it('classifies a non-master without a parent as another resume', () => {
    expect(resolveResumeLineage([makeResume(3)], 3)).toEqual({
      kind: 'independent',
      label: '其他简历',
    });
  });

  it.each([
    ['missing parent', makeResume(2, { parent_resume_id: 99 }), [makeResume(2, { parent_resume_id: 99 })]],
    ['self reference', makeResume(2, { parent_resume_id: 2 }), [makeResume(2, { parent_resume_id: 2 })]],
    ['master with parent', makeResume(2, { is_master: true, parent_resume_id: 1 }), [makeResume(2, { is_master: true, parent_resume_id: 1 }), makeResume(1, { is_master: true })]],
    ['deleted parent', makeResume(2, { parent_resume_id: 1 }), [makeResume(2, { parent_resume_id: 1 }), makeResume(1, { is_master: true, deleted_at: '2026-08-30T00:00:00Z' })]],
    ['hidden parent', makeResume(2, { parent_resume_id: 1 }), [makeResume(2, { parent_resume_id: 1 }), makeResume(1, { is_master: true, hidden: true } as Partial<Resume> & { hidden: boolean })]],
  ] as const)('fails closed for %s', (_name, target, resumes) => {
    expect(resolveResumeLineage(resumes, target.id)).toEqual({
      kind: 'relationship_unknown',
      label: '关系待确认',
    });
  });

  it('fails closed for two-node and longer ancestor cycles', () => {
    const twoA = makeResume(1, { parent_resume_id: 2 });
    const twoB = makeResume(2, { parent_resume_id: 1 });
    const threeA = makeResume(3, { parent_resume_id: 4 });
    const threeB = makeResume(4, { parent_resume_id: 5 });
    const threeC = makeResume(5, { parent_resume_id: 3 });

    expect(resolveResumeLineage([twoA, twoB], 1).kind).toBe('relationship_unknown');
    expect(resolveResumeLineage([threeA, threeB, threeC], 3).kind).toBe('relationship_unknown');
  });

  it('does not mutate the input collection or resume rows', () => {
    const base = makeResume(1, { is_master: true });
    const variant = makeResume(2, { parent_resume_id: 1 });
    const resumes = [variant, base];
    const before = JSON.stringify(resumes);

    resolveResumeLineage(resumes, 2);

    expect(JSON.stringify(resumes)).toBe(before);
    expect(resumes).toEqual([variant, base]);
  });

  it('fails closed when a source row is malformed instead of inventing a relationship', () => {
    const malformed = makeResume(2, { parent_resume_id: 1, is_master: 'false' as unknown as boolean });
    const base = makeResume(1, { is_master: true });

    expect(resolveResumeLineage([malformed, base], 2).kind).toBe('relationship_unknown');
  });

  it('fails closed for duplicate identities and hostile source getters', () => {
    const base = makeResume(1, { is_master: true });
    const duplicate = makeResume(1, { is_master: true });
    const hostile = new Proxy(makeResume(3, { is_master: true }), {
      get() {
        throw new Error('source read failed');
      },
    });

    expect(resolveResumeLineage([base, duplicate], 1).kind).toBe('relationship_unknown');
    expect(resolveResumeLineage([makeResume(2, { parent_resume_id: 3 }), hostile as unknown as Resume], 2).kind)
      .toBe('relationship_unknown');
  });
});
