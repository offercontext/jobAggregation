import { evidenceLocationLabel, evidenceSourceLabel } from '@/lib/evidencePresentation';

export interface StudioEvidenceReference {
  source: string;
  path: string;
  excerpt: string;
}

export interface StudioEvidenceEntry extends StudioEvidenceReference {
  key: string;
  label: string;
}

export function evidenceKey(reference: StudioEvidenceReference): string {
  return `${reference.source}:${reference.path}:${reference.excerpt}`;
}

function turnNumber(path: string): string | null {
  const match = path.match(/\/turns\/(\d+)\/answer/);
  return match ? String(Number(match[1])) : null;
}

function labelFor(reference: StudioEvidenceReference): string {
  const turn = reference.source === 'turn' ? turnNumber(reference.path) : null;
  if (turn) return `上一轮回答 · 第 ${turn} 轮`;
  if (reference.source === 'jd') return '岗位描述 · 本次练习快照';
  if (reference.source === 'resume') return `简历快照 · ${evidenceLocationLabel(reference.source, reference.path)}`;
  return `${evidenceSourceLabel(reference.source)} · ${evidenceLocationLabel(reference.source, reference.path)}`;
}

export function buildEvidenceEntries(
  references: StudioEvidenceReference[] | undefined,
): StudioEvidenceEntry[] {
  const seen = new Set<string>();
  return (references ?? []).filter((reference) => {
    const key = evidenceKey(reference);
    if (seen.has(key) || !reference.excerpt.trim()) return false;
    seen.add(key);
    return true;
  }).map((reference) => ({
    ...reference,
    key: evidenceKey(reference),
    label: labelFor(reference),
  }));
}
