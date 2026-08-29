import { describe, expect, it } from 'vitest';
import {
  MATERIAL_FLOW_COPY,
  materialEvidenceSourceLabel,
  materialFlowErrorMessage,
} from './materialFlowCopy';

describe('material flow error copy', () => {
  it('uses the task language for preparation and the current delivery record', () => {
    expect(MATERIAL_FLOW_COPY.drawer.materialKitTitle).toBe('投递准备');
    expect(MATERIAL_FLOW_COPY.drawer.evidenceHistoryTitle).toBe('本次投递记录');
    expect(materialEvidenceSourceLabel('evidence_bundle')).toBe('本次投递记录');
    expect(MATERIAL_FLOW_COPY.surface.readyUnsubmitted).toBe('记录已投递');
    expect(MATERIAL_FLOW_COPY.surface.submitted).toBe('查看本次投递记录');
  });

  it('uses a neutral message for a general HTTP 409', () => {
    expect(materialFlowErrorMessage({ response: { status: 409 } }, 'general'))
      .toBe('操作未完成，请稍后重试');
  });

  it('distinguishes an unclassified 502 from an unverifiable proposal', () => {
    expect(materialFlowErrorMessage({ response: { status: 502 } }, 'proposal'))
      .toBe('AI 服务暂不可用，请稍后重试');
    expect(materialFlowErrorMessage({
      response: { status: 502, data: { error_code: 'material_proposal_unverifiable' } },
    }, 'proposal'))
      .toBe('AI 输出未通过证据校验，已保护原简历且未创建草稿，请重试');
  });
});
