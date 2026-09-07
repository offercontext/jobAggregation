import type { ResumeImportField } from '@/types/resume';

const LABELS: Record<string, string> = { contact: '基本信息', name: '姓名/名称', email: '邮箱', phone: '电话', location: '所在地', career_intent: '求职意向', target_roles: '目标岗位', target_locations: '目标城市', education: '教育经历', school: '学校', degree: '学历', major: '专业', start_date: '开始时间', end_date: '结束时间', experience: '工作经历', company: '公司', title: '职位', highlights: '工作亮点', projects: '项目经历', role: '职责', skills: '技能' };
export function importFieldLabel(path: string): string {
  return path.split('.').reduce<string[]>((parts, part) => {
    if (/^\d+$/.test(part)) parts[parts.length - 1] += ` ${Number(part) + 1}`;
    else parts.push(LABELS[part] ?? '字段');
    return parts;
  }, []).join(' · ');
}

export function isImportFieldProtected(content: unknown, path: string): boolean {
  let current: unknown = content;
  for (const segment of path.split('.')) {
    if (Array.isArray(current)) return current.length > 0;
    if (current === undefined || current === null) return false;
    if (typeof current === 'string' && !current.trim()) return /^\d+$/.test(segment);
    if (typeof current !== 'object') return true;
    current = (current as Record<string, unknown>)[segment];
  }
  return current !== undefined && current !== null && !(typeof current === 'string' && !current.trim());
}

// Removing an entire entry or a highlight must not leave sparse server arrays.
export function reindexImportFields(fields: readonly ResumeImportField[]): ResumeImportField[] {
  const indexes = new Map<string, Map<string, number>>();
  return [...fields].sort((a, b) => a.path.localeCompare(b.path, 'en', { numeric: true })).map((field) => {
    const source = field.path.split('.');
    const path = source.map((part, position) => {
      if (!/^\d+$/.test(part)) return part;
      const parent = source.slice(0, position).join('.');
      const siblings = indexes.get(parent) ?? new Map<string, number>();
      if (!siblings.has(part)) siblings.set(part, siblings.size);
      indexes.set(parent, siblings);
      return String(siblings.get(part));
    }).join('.');
    return { ...field, path };
  });
}
