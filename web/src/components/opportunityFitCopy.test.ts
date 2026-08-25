import { describe, expect, it } from 'vitest';
import {
  getOpportunityFitErrorMessage,
  OPPORTUNITY_FIT_COPY,
  opportunityFitEvidenceLabel,
  opportunityFitGapKindLabel,
  opportunityFitRecommendationLabel,
  opportunityFitRecommendationColor,
  opportunityFitRecommendedPathLabel,
  opportunityFitStatusLabel,
} from './opportunityFitCopy';

describe('opportunity fit copy', () => {
  it('uses the Chinese task language for triage, deep review, and frozen sources', () => {
    expect(OPPORTUNITY_FIT_COPY.drawer.startTriage).toBe('开始快速判断');
    expect(OPPORTUNITY_FIT_COPY.drawer.triage).toBe('快速判断');
    expect(OPPORTUNITY_FIT_COPY.drawer.startDeepReview).toBe('开始深入分析');
    expect(OPPORTUNITY_FIT_COPY.drawer.deepReview).toBe('深入分析');
    expect(OPPORTUNITY_FIT_COPY.drawer.sourceFrozen).toBe('已保留当时版本');
    expect(OPPORTUNITY_FIT_COPY.drawer.sourceChanged).toBe('原资料已更新，本次结果仍使用旧版');
  });

  it('maps a provider 502 to safe configuration copy', () => {
    expect(getOpportunityFitErrorMessage({
      response: {
        status: 502,
        data: {
          error_code: 'opportunity_fit_provider_error',
          error: 'raw provider text',
        },
      },
    })).toBe('AI 服务配置或提供方不可用，请检查设置后重试');
  });

  it('maps an unverifiable 502 without exposing the response error', () => {
    expect(getOpportunityFitErrorMessage({
      response: {
        status: 502,
        data: {
          error_code: 'opportunity_fit_unverifiable',
          error: 'raw provider text',
        },
      },
    })).toBe('AI 输出未通过证据校验，可重试；原简历已保护，未创建草稿。');
  });

  it('maps an unclassified 502 to generic AI availability copy', () => {
    expect(getOpportunityFitErrorMessage({
      response: {
        status: 502,
        data: { error: 'raw provider text', issues: ['raw issue'] },
      },
    })).toBe('AI 服务暂不可用，请稍后重试');
  });

  it('maps a general 409 to neutral copy', () => {
    expect(getOpportunityFitErrorMessage({
      response: { status: 409, data: { error: 'raw conflict text' } },
    })).toBe('操作未完成，请稍后重试');
  });

  it('does not expose an Error message for unknown errors', () => {
    expect(getOpportunityFitErrorMessage(new Error('raw axios message')))
      .toBe('操作失败，请稍后重试');
  });

  it('maps only the supported Opportunity Fit evidence sources', () => {
    expect(opportunityFitEvidenceLabel('resume')).toBe('简历');
    expect(opportunityFitEvidenceLabel('jd')).toBe('岗位描述（仅用于分析方向）');
    expect(opportunityFitEvidenceLabel('user_assertion'))
      .toBe('用户断言（用户提供，未外部核验）');
    expect(opportunityFitEvidenceLabel('evidence_bundle')).toBe('未知证据来源');
    expect(opportunityFitEvidenceLabel('__proto__')).toBe('未知证据来源');
  });

  it('maps every user-visible Opportunity Fit enum to Chinese copy', () => {
    expect(['advance', 'hold', 'decline'].map(opportunityFitRecommendationLabel))
      .toEqual(['建议推进', '需要澄清', '建议放弃']);
    expect(opportunityFitRecommendationLabel('constructor')).toBe('未知建议');
    expect(['met', 'unmet', 'unknown'].map(opportunityFitStatusLabel))
      .toEqual(['已满足', '未满足', '待确认']);
    expect(opportunityFitStatusLabel('__proto__')).toBe('未知状态');
    expect(['required', 'preferred'].map(opportunityFitGapKindLabel))
      .toEqual(['必要条件', '优先条件']);
    expect(opportunityFitGapKindLabel('constructor')).toBe('未知条件');
    expect(['prepare_materials', 'clarify_first', 'do_not_pursue'].map(opportunityFitRecommendedPathLabel))
      .toEqual(['建议准备材料', '建议先澄清', '不建议继续推进']);
    expect(opportunityFitRecommendedPathLabel('__proto__')).toBe('未知建议路径');
    expect(['advance', 'hold', 'decline'].map(opportunityFitRecommendationColor))
      .toEqual(['green', 'gold', 'red']);
  });
});
