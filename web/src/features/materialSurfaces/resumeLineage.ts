import type { Resume } from '@/types/resume';

export type ResumeLineageKind = 'base' | 'job_variant' | 'independent' | 'relationship_unknown';
export type ResumeLineageLabel = '基础简历' | '岗位版本' | '其他简历' | '关系待确认';

export interface ResumeLineage {
  readonly kind: ResumeLineageKind;
  readonly parent?: Resume;
  readonly label: ResumeLineageLabel;
  readonly detail?: string;
}

type ResumeRow = Resume & {
  readonly deleted?: boolean;
  readonly hidden?: boolean;
  readonly visible?: boolean;
};

interface IndexedResume {
  readonly row: Resume;
  readonly id: number;
  readonly parentId: number | null;
  readonly isMaster: boolean;
  readonly visible: boolean;
  readonly valid: boolean;
}

const UNKNOWN_LINEAGE: ResumeLineage = Object.freeze({
  kind: 'relationship_unknown',
  label: '关系待确认',
});

const BASE_LINEAGE: ResumeLineage = Object.freeze({
  kind: 'base',
  label: '基础简历',
});

const INDEPENDENT_LINEAGE: ResumeLineage = Object.freeze({
  kind: 'independent',
  label: '其他简历',
});

/**
 * Resolve the relationship of one visible resume without changing the source
 * collection or attempting to repair malformed rows. Any ambiguous relation
 * is deliberately projected as a safe, action-blocking state.
 */
export function resolveResumeLineage(
  resumes: readonly Resume[],
  targetId: number,
): ResumeLineage {
  if (!isValidId(targetId)) return UNKNOWN_LINEAGE;

  const index = indexResumes(resumes);
  const target = index.get(targetId);
  if (!target || !target.valid || !target.visible) return UNKNOWN_LINEAGE;

  if (target.isMaster) {
    return target.parentId === null ? BASE_LINEAGE : UNKNOWN_LINEAGE;
  }
  if (target.parentId === null) return INDEPENDENT_LINEAGE;
  if (!isValidId(target.parentId)) return UNKNOWN_LINEAGE;

  const visited = new Set<number>();
  let current: IndexedResume | undefined = target;
  let parentId: number | null = target.parentId;
  let directParent: IndexedResume | undefined;

  while (parentId !== null) {
    if (!current || visited.has(current.id)) return UNKNOWN_LINEAGE;
    visited.add(current.id);

    const parent = index.get(parentId);
    if (!parent || !parent.valid || !parent.visible || visited.has(parent.id)) {
      return UNKNOWN_LINEAGE;
    }
    if (!directParent) directParent = parent;

    // A master row may only be the root of a lineage. A parent row with a
    // conflicting master flag is not a trustworthy ancestor to display.
    if (parent.isMaster && parent.parentId !== null) return UNKNOWN_LINEAGE;

    current = parent;
    parentId = parent.parentId;
    if (parentId !== null && !isValidId(parentId)) return UNKNOWN_LINEAGE;
  }

  if (!directParent) return UNKNOWN_LINEAGE;
  const detail = `基于 ${visibleTitle(directParent.row)}`;
  return Object.freeze({
    kind: 'job_variant',
    label: '岗位版本',
    parent: directParent.row,
    detail,
  });
}

function indexResumes(resumes: readonly Resume[]): Map<number, IndexedResume> {
  const index = new Map<number, IndexedResume>();
  const ambiguousIds = new Set<number>();
  let rows: readonly Resume[];
  try {
    rows = Array.isArray(resumes) ? resumes : [];
  } catch {
    return index;
  }

  let length: number;
  try {
    length = rows.length;
  } catch {
    return index;
  }

  for (let position = 0; position < length; position += 1) {
    let row: Resume;
    try {
      row = rows[position];
    } catch {
      continue;
    }
    const parsed = readResume(row);
    if (!parsed) continue;

    const previous = index.get(parsed.id);
    if (previous) {
      // Duplicate identity is ambiguous even when both rows happen to look
      // equivalent. Do not let input ordering choose a lineage parent.
      ambiguousIds.add(parsed.id);
      index.set(parsed.id, { ...parsed, valid: false });
    } else {
      index.set(parsed.id, parsed);
    }
  }
  for (const id of ambiguousIds) {
    const entry = index.get(id);
    if (entry) index.set(id, { ...entry, valid: false });
  }
  return index;
}

function readResume(row: Resume): IndexedResume | null {
  if (!isObject(row)) return null;
  try {
    const id = row.id;
    const parentId = row.parent_resume_id;
    const isMaster = row.is_master;
    const visible = !isDeletedOrHidden(row as ResumeRow);
    const valid = isValidId(id)
      && (parentId === null || isValidId(parentId))
      && typeof isMaster === 'boolean';
    if (!isValidId(id)) return null;
    return {
      row,
      id,
      parentId: valid && parentId !== null ? parentId : null,
      isMaster: typeof isMaster === 'boolean' ? isMaster : false,
      visible,
      valid,
    };
  } catch {
    return null;
  }
}

function isDeletedOrHidden(row: ResumeRow): boolean {
  try {
    return row.deleted_at !== null && row.deleted_at !== undefined
      || row.deleted === true
      || row.hidden === true
      || row.visible === false;
  } catch {
    return true;
  }
}

function visibleTitle(resume: Resume): string {
  try {
    const title = typeof resume.title === 'string' ? resume.title.trim() : '';
    const name = typeof resume.name === 'string' ? resume.name.trim() : '';
    const value = title || name || '这份简历';
    return Array.from(value).slice(0, 80).join('');
  } catch {
    return '这份简历';
  }
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function isValidId(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0;
}
