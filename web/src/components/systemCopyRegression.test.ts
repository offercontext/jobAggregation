import { describe, expect, it } from 'vitest';
import { MATERIAL_FLOW_COPY } from './materialFlowCopy';
import { OPPORTUNITY_FIT_COPY } from './opportunityFitCopy';
import nextStepSuggestions from './NextStepSuggestions.tsx?raw';
import interviewStudioSource from '@/features/interviewStudio/InterviewStudio.tsx?raw';

const LEGACY_FIXED_PHRASES = [
  'AI recommendation',
  'Reject proposal',
  'Generate evidence-gated resume proposal',
  'No safe evidence-backed changes are available.',
  'Request failed with status code 502',
  'Evidence source',
  'Start Deep Review',
  'Next step',
  'Snooze',
  'Ignore',
  'Current source',
  'Source changed',
];

describe('controlled proposal copy stays localized', () => {
  it('does not reintroduce known fixed English UI phrases', () => {
    const controlledCopy = JSON.stringify({ MATERIAL_FLOW_COPY, OPPORTUNITY_FIT_COPY });

    for (const phrase of LEGACY_FIXED_PHRASES) {
      expect(controlledCopy).not.toContain(phrase);
    }
  });

  it('keeps user and evidence text outside the fixed copy mapping', () => {
    expect(MATERIAL_FLOW_COPY.proposal.empty).toContain('证据');
    expect(OPPORTUNITY_FIT_COPY.evidence.jd).toContain('岗位描述');
    expect(OPPORTUNITY_FIT_COPY.evidence.user_assertion).toContain('用户断言');
  });
  it('keeps next-step suggestions read-only and free of legacy fixed English copy', () => {
    for (const phrase of LEGACY_FIXED_PHRASES.slice(-5)) {
      expect(nextStepSuggestions).not.toMatch(new RegExp(`[\"']${phrase}[\"']`));
    }
    expect(nextStepSuggestions).not.toMatch(/@\/services\//);
    expect(nextStepSuggestions).not.toContain('localStorage');
  });

  it('keeps retry recovery copy user-facing rather than exposing internal keys', () => {
    expect(interviewStudioSource).not.toContain('原 key');
    expect(interviewStudioSource).not.toContain('原保存 key');
    expect(interviewStudioSource).toContain('使用原尝试恢复');
    expect(interviewStudioSource).toContain('原保存尝试');
  });
});
