import type { ResumeContent } from '@/types/resume';

type ResumeEntry = Record<string, unknown>;

export interface StructuredResumeDraft {
  careerIntent: { targetRoles: string[]; targetLocations: string[] };
  contact: ResumeEntry;
  education: ResumeEntry[];
  experience: ResumeEntry[];
  projects: ResumeEntry[];
  skills: string[];
  rawText: string;
}

export type StructuredResumeParseResult =
  | { mode: 'structured'; draft: StructuredResumeDraft; source: ResumeContent }
  | { mode: 'recovery'; raw: string; reason: string };

function isRecord(value: unknown): value is ResumeEntry {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function decode(value: unknown): ResumeContent | null {
  if (typeof value === 'string') {
    try {
      const parsed = JSON.parse(value) as unknown;
      return isRecord(parsed) ? parsed as ResumeContent : null;
    } catch {
      return null;
    }
  }
  return isRecord(value) ? value as ResumeContent : null;
}

function isRecordArray(value: unknown): value is ResumeEntry[] {
  return Array.isArray(value) && value.every(isRecord);
}

export function parseStructuredResume(value: unknown): StructuredResumeParseResult {
  const content = decode(value);
  if (!content) return { mode: 'recovery', raw: typeof value === 'string' ? value : JSON.stringify(value, null, 2), reason: '历史 JSON 无法解析' };

  const careerIntent = content.career_intent ?? {};
  const contact = content.contact ?? {};
  const education = content.education ?? [];
  const experience = content.experience ?? [];
  const projects = content.projects ?? [];
  const skills = content.skills ?? [];
  const knownIntentFieldsValid = isRecord(careerIntent)
    && (!('target_roles' in careerIntent) || (Array.isArray(careerIntent.target_roles) && careerIntent.target_roles.every((item) => typeof item === 'string')))
    && (!('target_locations' in careerIntent) || (Array.isArray(careerIntent.target_locations) && careerIntent.target_locations.every((item) => typeof item === 'string')));
  const rawTextValid = !('raw_text' in content) || typeof content.raw_text === 'string';
  if (!knownIntentFieldsValid || !isRecord(contact) || !isRecordArray(education) || !isRecordArray(experience) || !isRecordArray(projects) || !Array.isArray(skills) || !skills.every((item) => typeof item === 'string') || !rawTextValid) {
    return { mode: 'recovery', raw: JSON.stringify(content, null, 2), reason: '历史字段类型与结构化编辑器不兼容' };
  }

  const roles = Array.isArray(careerIntent.target_roles) ? careerIntent.target_roles.filter((item): item is string => typeof item === 'string') : [];
  const locations = Array.isArray(careerIntent.target_locations) ? careerIntent.target_locations.filter((item): item is string => typeof item === 'string') : [];
  return {
    mode: 'structured',
    source: clone(content),
    draft: {
      careerIntent: { targetRoles: roles, targetLocations: locations },
      contact: clone(contact),
      education: clone(education),
      experience: clone(experience),
      projects: clone(projects),
      skills: clone(skills),
      rawText: typeof content.raw_text === 'string' ? content.raw_text : '',
    },
  };
}

export function serializeStructuredResume(original: unknown, draft: StructuredResumeDraft): ResumeContent {
  const parsed = parseStructuredResume(original);
  if (parsed.mode !== 'structured') throw new Error('历史简历无法安全保存，请使用恢复或高级编辑');
  const sourceIntent = isRecord(parsed.source.career_intent) ? parsed.source.career_intent : {};
  return {
    ...clone(parsed.source),
    career_intent: {
      ...clone(sourceIntent),
      target_roles: [...draft.careerIntent.targetRoles],
      target_locations: [...draft.careerIntent.targetLocations],
    },
    contact: clone(draft.contact),
    education: clone(draft.education),
    experience: clone(draft.experience),
    projects: clone(draft.projects),
    skills: [...draft.skills],
    raw_text: draft.rawText,
  };
}

export function buildAdvancedResumeJson(original: unknown, draft: StructuredResumeDraft): string {
  return JSON.stringify(serializeStructuredResume(original, draft), null, 2);
}
