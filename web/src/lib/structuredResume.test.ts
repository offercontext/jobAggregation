import { describe, expect, it } from 'vitest';
import { buildAdvancedResumeJson, parseStructuredResume, serializeStructuredResume } from './structuredResume';

describe('structured resume round trip', () => {
  it('edits known fields while preserving unknown top-level and nested fields', () => {
    const original = {
      career_intent: { target_roles: ['Engineer'], target_locations: ['上海'], private_extension: { source: 'legacy' } },
      contact: { name: 'Ada', email: 'old@example.com', custom_contact: 'keep' },
      education: [{ school: 'A', degree: 'BSc', transcript_id: 9 }],
      experience: [{ company: 'Old Co', title: 'Intern', highlights: ['Built API'], extension: { score: 5 } }],
      projects: [], skills: ['TypeScript'], raw_text: 'original', custom_root: { keep: true },
    };
    const parsed = parseStructuredResume(original);
    expect(parsed.mode).toBe('structured');
    if (parsed.mode !== 'structured') return;
    parsed.draft.contact.email = 'new@example.com';
    parsed.draft.education[0].school = 'New School';
    const result = serializeStructuredResume(original, parsed.draft);
    expect(result).toMatchObject({
      custom_root: { keep: true },
      contact: { email: 'new@example.com', custom_contact: 'keep' },
      education: [{ school: 'New School', transcript_id: 9 }],
      experience: [{ extension: { score: 5 } }],
      career_intent: { private_extension: { source: 'legacy' } },
    });
  });

  it('preserves entry identity through reorder and supports add/delete', () => {
    const original = { education: [{ school: 'A', x: 1 }, { school: 'B', x: 2 }], experience: [], projects: [], skills: [] };
    const parsed = parseStructuredResume(original);
    if (parsed.mode !== 'structured') throw new Error('expected structured mode');
    parsed.draft.education = [parsed.draft.education[1], { school: 'C' }];
    const result = serializeStructuredResume(original, parsed.draft);
    expect(result.education).toEqual([{ school: 'B', x: 2 }, { school: 'C' }]);
  });

  it('opens advanced JSON from the current structured draft without losing edits or extensions', () => {
    const original = {
      contact: { name: 'Ada' }, education: [], experience: [], projects: [], skills: [], extension: { keep: true },
    };
    const parsed = parseStructuredResume(original);
    if (parsed.mode !== 'structured') throw new Error('expected structured mode');
    parsed.draft.contact.name = 'Grace';

    expect(JSON.parse(buildAdvancedResumeJson(original, parsed.draft))).toMatchObject({
      contact: { name: 'Grace' },
      extension: { keep: true },
    });
  });

  it('enters recovery mode for invalid or incompatible historical JSON', () => {
    expect(parseStructuredResume('{not-json')).toMatchObject({ mode: 'recovery' });
    expect(parseStructuredResume({ education: 'not-an-array' })).toMatchObject({ mode: 'recovery' });
    expect(parseStructuredResume({ career_intent: { target_roles: ['Engineer', 7] } })).toMatchObject({ mode: 'recovery' });
    expect(parseStructuredResume({ raw_text: { legacy: true } })).toMatchObject({ mode: 'recovery' });
    expect(() => serializeStructuredResume('{not-json', {
      careerIntent: { targetRoles: [], targetLocations: [] }, contact: {}, education: [], experience: [], projects: [], skills: [], rawText: '',
    })).toThrow(/无法安全保存/);
  });
});
