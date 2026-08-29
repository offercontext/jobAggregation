import type { Resume } from '@/types/resume';
import {
  resolveResumeLineage,
  type ResumeLineage,
  type ResumeLineageKind,
} from './resumeLineage';

export type MaterialLabelKey =
  | ResumeLineageKind
  | 'experience_story'
  | 'confirmed_capture'
  | 'captured_unavailable'
  | 'external_reference'
  | 'unclassified';

export interface MaterialLabelInput {
  readonly kind: MaterialLabelKey;
  readonly label?: string;
  readonly detail?: string;
}

export const MATERIAL_LABELS: Readonly<Record<MaterialLabelKey, string>> = Object.freeze({
  base: '基础简历',
  job_variant: '岗位版本',
  independent: '其他简历',
  relationship_unknown: '关系待确认',
  experience_story: '经历故事',
  confirmed_capture: '已确认面试片段',
  captured_unavailable: '部分来源暂时不可用',
  external_reference: '参考资料',
  unclassified: '暂无法归类',
});

/** Map domain material kinds to the one shared user vocabulary. */
export function mapMaterialLabel(
  value: MaterialLabelKey | Pick<ResumeLineage, 'kind' | 'label'> | MaterialLabelInput,
): string {
  const kind = typeof value === 'string' ? value : value.kind;
  return MATERIAL_LABELS[kind] ?? MATERIAL_LABELS.unclassified;
}

/**
 * Format a lineage for UI copy. Relationship details are intentionally only
 * accepted for a valid job variant and are bounded/filtered before rendering.
 */
export function formatResumeLineage(
  lineage: Pick<ResumeLineage, 'kind' | 'label' | 'detail'> | MaterialLabelInput,
): string {
  const label = mapMaterialLabel(lineage);
  if (lineage.kind !== 'job_variant') return label;

  const detail = safeLineageDetail(lineage.detail);
  return detail ? `${label} · ${detail}` : label;
}

/** Resolve and format a resume relationship for any resume surface. */
export function mapResumeLineageLabel(
  resumes: readonly Resume[],
  targetId: number,
): string {
  return formatResumeLineage(resolveResumeLineage(resumes, targetId));
}

/** Return a safe user title without exposing an internal resume identity. */
export function resumeDisplayTitle(resume: Pick<Resume, 'title' | 'name'>): string {
  try {
    const title = typeof resume.title === 'string' ? resume.title.trim() : '';
    const name = typeof resume.name === 'string' ? resume.name.trim() : '';
    return Array.from(title || name || '未命名简历').slice(0, 120).join('');
  } catch {
    return '未命名简历';
  }
}

function safeLineageDetail(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const detail = value.trim();
  if (!detail || Array.from(detail).length > 120 || !detail.startsWith('基于 ')) return null;
  if (/[#]|parent_resume_id|is_master|source_id|snapshot_id|fingerprint|sha256|\b(?:resume_)?id\b/i.test(detail)) {
    return null;
  }
  return detail;
}
