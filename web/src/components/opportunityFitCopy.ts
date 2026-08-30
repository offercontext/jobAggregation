export type OpportunityFitStatus = 'met' | 'unmet' | 'unknown';

const FIT_STATUS_LABELS: Record<OpportunityFitStatus, string> = {
  met: '已满足',
  unmet: '未满足',
  unknown: '待确认',
};

export const OPPORTUNITY_FIT_COPY = {
  drawer: {
    title: '岗位决策漏斗',
    description: '先判断是否值得投入，再决定是否准备材料。分析只基于本地 Application、选定简历、用户粘贴 JD 和你的补充断言。',
    history: '历史评估（只读快照）',
    view: '查看',
    resumeLabel: '用于审阅的简历',
    resumePlaceholder: '选择一份简历',
    jdLabel: '用户粘贴的 JD',
    jdSourceLabel: '用户粘贴 JD',
    jdPlaceholder: '只粘贴岗位要求文本；不会抓取链接。',
    assertionsLabel: '本次补充断言（每行一条）',
    assertionsPlaceholder: '例如：我可以在上海办公',
    assertionsHint: '最多 10 条，每条最多 500 字。',
    assertionsTooMany: '最多填写 10 条非空断言。',
    assertionsTooLong: '每条断言最多 500 字。',
    humanConfirmation: '人工确认',
    humanConfirmationDescription: 'AI 只生成带证据引用的分析，不会自动接受、投递或访问外部招聘平台。',
    startTriage: '开始快速判断',
    sourceFrozen: '已保留当时版本',
    sourceChanged: '来源已更新，已有结果仍保留',
    triage: '快速判断',
    hardConstraints: '岗位约束',
    fitSignals: '候选人匹配信号',
    gaps: '差距与待确认问题',
    nextQuestions: '截止日期',
    notStated: '未在输入材料中陈述',
    evidenceSources: '证据来源',
    jdOriginal: 'JD 原文',
    candidateAssertions: '用户断言（用户提供，未外部核验）',
    deepReview: '深入分析',
    recommendedPath: '建议路径',
    nextActions: '下一步行动',
    prepareMaterials: '去准备材料',
    startDeepReview: '开始深入分析',
    noDirectEvidence: '无直接证据引用',
  },
  evidence: {
    resume: '简历',
    jd: '岗位描述（仅用于分析方向）',
    user_assertion: '用户断言（用户提供，未外部核验）',
    unknown: '未知证据来源',
  },
  errors: {
    unverifiable: 'AI 输出未通过证据校验，可重试；原简历已保护，未创建草稿。',
    providerUnavailable: 'AI 服务配置或提供方不可用，请检查设置后重试',
    aiServiceUnavailable: 'AI 服务暂不可用，请稍后重试',
    generalConflict: '操作未完成，请稍后重试',
    notFound: '请求的岗位评估不存在或不可用，请刷新后重试',
    invalidRequest: '输入内容无法处理，请检查后重试',
    fallback: '操作失败，请稍后重试',
  },
  enums: {
    recommendation: {
      advance: '建议推进',
      hold: '需要澄清',
      decline: '建议放弃',
    },
    fitStatus: FIT_STATUS_LABELS,
    gapKind: {
      required: '必要条件',
      preferred: '优先条件',
    },
    recommendedPath: {
      prepare_materials: '建议准备材料',
      clarify_first: '建议先澄清',
      do_not_pursue: '不建议继续推进',
    },
  },
} as const;

function fixedCopyLabel(
  mapping: Readonly<Record<string, string>>,
  value: string,
  fallback: string,
): string {
  return Object.prototype.hasOwnProperty.call(mapping, value) ? mapping[value] : fallback;
}

function responseDetails(error: unknown): { status?: number; errorCode?: string } {
  if (typeof error !== 'object' || error === null) return {};
  const response = (error as { response?: unknown }).response;
  if (typeof response !== 'object' || response === null) return {};

  const responseRecord = response as { status?: unknown; data?: unknown };
  const data = typeof responseRecord.data === 'object' && responseRecord.data !== null
    ? responseRecord.data as { error_code?: unknown }
    : undefined;
  const status = typeof responseRecord.status === 'number' ? responseRecord.status : undefined;
  const errorCode = typeof data?.error_code === 'string' ? data.error_code : undefined;
  return { status, errorCode };
}

export function getOpportunityFitErrorMessage(error: unknown): string {
  const { status, errorCode } = responseDetails(error);

  if (errorCode === 'opportunity_fit_unverifiable') return OPPORTUNITY_FIT_COPY.errors.unverifiable;
  if (errorCode === 'opportunity_fit_provider_error') return OPPORTUNITY_FIT_COPY.errors.providerUnavailable;
  if (status === 404) return OPPORTUNITY_FIT_COPY.errors.notFound;
  if (status === 410) return '岗位评估确认已过期，请开始新的评估。';
  if (status === 409) return OPPORTUNITY_FIT_COPY.errors.generalConflict;
  if (status === 422) return OPPORTUNITY_FIT_COPY.errors.invalidRequest;
  if (status === 502) return OPPORTUNITY_FIT_COPY.errors.aiServiceUnavailable;
  return OPPORTUNITY_FIT_COPY.errors.fallback;
}

export function opportunityFitEvidenceLabel(source: string): string {
  return fixedCopyLabel(OPPORTUNITY_FIT_COPY.evidence, source, OPPORTUNITY_FIT_COPY.evidence.unknown);
}

export function opportunityFitRecommendationLabel(value: string): string {
  return fixedCopyLabel(OPPORTUNITY_FIT_COPY.enums.recommendation, value, '未知建议');
}

export function opportunityFitRecommendationColor(value: string): 'green' | 'gold' | 'red' {
  if (value === 'advance') return 'green';
  if (value === 'decline') return 'red';
  return 'gold';
}

export function opportunityFitStatusLabel(value: string): string {
  return fixedCopyLabel(FIT_STATUS_LABELS, value, '未知状态');
}

export function opportunityFitGapKindLabel(value: string): string {
  return fixedCopyLabel(OPPORTUNITY_FIT_COPY.enums.gapKind, value, '未知条件');
}

export function opportunityFitRecommendedPathLabel(value: string): string {
  return fixedCopyLabel(OPPORTUNITY_FIT_COPY.enums.recommendedPath, value, '未知建议路径');
}
