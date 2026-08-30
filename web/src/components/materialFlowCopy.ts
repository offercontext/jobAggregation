import type { MaterialEvidenceSource } from '@/types/materialRevisionProposal';

export const MATERIAL_FLOW_COPY = {
  proposal: {
    title: 'AI 建议，仅供人工审核',
    warning: 'AI 建议仅供人工审核。在你确认之前，不会修改任何简历。',
    reject: '拒绝提案',
    accept: '接受选中的修改',
    sourceResume: '来源简历',
    jdDirection: 'JD 方向',
    generatedAt: '生成时间',
    before: '修改前',
    after: '修改后',
    why: '修改原因',
    userAssertion: '用户断言',
    empty: '当前没有可由证据安全支持的简历改写建议',
    confirmTitle: '确认创建派生简历',
    createDerivedResume: '创建派生简历',
    backToReview: '返回审核',
    confirmBody: '这会创建一个新的派生简历版本，并将投递准备改为指向该版本。不会覆盖来源简历。',
    selectChange: '选择此项变更',
  },
  drawer: {
    materialKitTitle: '投递准备',
    evidenceHistoryTitle: '本次投递记录',
    candidateFactsLabel: '候选人补充事实（每行一条）',
    candidateFactsPlaceholder: '每行填写一条候选人事实',
    generateProposal: '生成基于证据的简历提案',
    proposalValidationTooMany: '最多填写 10 条非空断言',
    proposalValidationTooLong: '每条断言不得超过 500 个字符',
    proposalAccepted: '已根据人工确认创建派生简历',
    confirmationKindUnknown: '其他确认方式',
    evidencePreviewFallback: '材料证据尚未准备完成，请补充必要来源',
  },
  surface: {
    loading: '正在加载投递准备，请稍候',
    missingJd: '补充岗位资料',
    missingResume: '选择本次简历',
    notGenerated: '生成投递准备',
    dirtyDraft: '保存修改',
    waitingConfirmation: '查看并确认',
    resultUnknown: '确认处理结果',
    readyUnsubmitted: '记录已投递',
    submitted: '查看本次投递记录',
  },
  errors: {
    proposalUnverifiable: 'AI 输出未通过证据校验，已保护原简历且未创建草稿，请重试',
    proposalSourceConflict: '原始来源已发生变化，请重新生成提案后再审核',
    confirmationSourceConflict: '提交材料已变化，请重新核对',
    aiServiceUnavailable: 'AI 服务暂不可用，请稍后重试',
    generalConflict: '操作未完成，请稍后重试',
    notFound: '请求的材料已不存在或不可用，请刷新后重试',
    invalidRequest: '输入内容无法处理，请检查后重试',
    fallback: '操作未完成，请稍后重试',
  },
} as const;

const EVIDENCE_SOURCE_LABELS: Record<MaterialEvidenceSource, string> = {
  resume: '简历',
  evidence_bundle: '本次投递记录',
  user_assertion: '用户断言',
};

const EVIDENCE_PREVIEW_ISSUE_LABELS: Record<string, string> = {
  '缺少投递材料包': '缺少投递准备',
  '投递材料包不唯一': '投递准备不唯一，请保留一份有效投递准备',
  '缺少职位描述': '缺少职位描述，请补充 JD',
  '缺少关联简历': '缺少关联简历，请先选择简历',
  '关联简历不存在或已删除': '关联简历不存在或已删除，请重新选择简历',
  '简历内容不是 JSON 对象': '简历内容格式异常，请重新选择简历',
  '材料包内容不是 JSON 对象': '材料包内容格式异常，请重新生成材料包',
  '缺少已选择的简历': '缺少已选择的简历，请先选择简历',
};

export function materialEvidenceSourceLabel(source: string): string {
  return EVIDENCE_SOURCE_LABELS[source as MaterialEvidenceSource] || '未知证据来源';
}

export function materialEvidencePreviewIssueLabels(issues: string[]): string[] {
  const labels = issues.map((issue) => EVIDENCE_PREVIEW_ISSUE_LABELS[issue] || MATERIAL_FLOW_COPY.drawer.evidencePreviewFallback);
  return [...new Set(labels)];
}

function responseDetails(error: unknown): { status?: number; code?: string } {
  if (typeof error !== 'object' || error === null) return {};
  const response = (error as { response?: unknown }).response;
  if (typeof response !== 'object' || response === null) return {};
  const responseRecord = response as { status?: unknown; data?: unknown };
  const data = typeof responseRecord.data === 'object' && responseRecord.data !== null
    ? responseRecord.data as { error_code?: unknown; code?: unknown }
    : undefined;
  const status = typeof responseRecord.status === 'number' ? responseRecord.status : undefined;
  const codeValue = data?.error_code ?? data?.code;
  const code = typeof codeValue === 'string' ? codeValue : undefined;
  return { status, code };
}

export type MaterialFlowErrorContext = 'proposal' | 'confirmation' | 'general';

export function materialFlowErrorMessage(
  error: unknown,
  context: MaterialFlowErrorContext = 'general',
): string {
  const { status, code } = responseDetails(error);
  if (code === 'material_proposal_unverifiable') {
    return MATERIAL_FLOW_COPY.errors.proposalUnverifiable;
  }
  if (status === 502 && context === 'proposal') return MATERIAL_FLOW_COPY.errors.aiServiceUnavailable;
  if (status === 409) {
    return context === 'proposal'
      ? MATERIAL_FLOW_COPY.errors.proposalSourceConflict
      : context === 'confirmation'
        ? MATERIAL_FLOW_COPY.errors.confirmationSourceConflict
        : MATERIAL_FLOW_COPY.errors.generalConflict;
  }
  if (status === 404) return MATERIAL_FLOW_COPY.errors.notFound;
  if (status === 422) return MATERIAL_FLOW_COPY.errors.invalidRequest;
  return MATERIAL_FLOW_COPY.errors.fallback;
}

export function isMaterialFlowSourceConflict(error: unknown): boolean {
  return responseDetails(error).status === 409;
}

export function materialConfirmationKindLabel(value: string): string {
  return value === 'user_asserted' ? '用户确认' : MATERIAL_FLOW_COPY.drawer.confirmationKindUnknown;
}
