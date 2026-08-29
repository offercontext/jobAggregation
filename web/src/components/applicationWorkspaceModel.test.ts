import { describe, expect, it } from 'vitest';
import { getApplicationWorkspaceStage } from './applicationWorkspaceModel';

describe('application stage workspace model', () => {
  it.each([
    ['pending', false, '准备投递', '完善投递材料'],
    ['applied', false, '已投递', '设置跟进时间'],
    ['written_test', false, '笔试', '准备笔试'],
    ['interview', false, '已约面试', '准备本轮面试'],
    ['interview', true, '面试结束', '完成面试复盘'],
    ['offer', false, '已获 Offer', '查看 Offer 与截止时间'],
    ['closed', false, '已结束', '记录结果与经验'],
  ] as const)('maps %s with completed interview=%s to one primary action', (status, hasCompletedInterview, label, primaryActionLabel) => {
    expect(getApplicationWorkspaceStage(status, { hasCompletedInterview })).toMatchObject({ label, primaryActionLabel });
  });

  it('keeps a completed interview in its completed stage after a review exists', () => {
    expect(getApplicationWorkspaceStage('interview', { hasCompletedInterview: true, hasInterviewReview: true })).toMatchObject({
      label: '面试结束',
      primaryActionLabel: '查看本轮复盘',
      action: 'interview-review',
    });
  });

  it('uses the canonical lifecycle when it is present, even if the compatibility boolean disagrees', () => {
    expect(getApplicationWorkspaceStage('interview', { lifecycle: 'completed', hasCompletedInterview: false })).toMatchObject({
      label: '面试结束',
      action: 'interview-review',
    });
    expect(getApplicationWorkspaceStage('interview', { lifecycle: 'scheduled', hasCompletedInterview: true })).toMatchObject({
      label: '已约面试',
      action: 'interview-prepare',
    });
  });

  it.each(['cancelled', 'unknown'] as const)('does not expose preparation for a %s event', (lifecycle) => {
    expect(getApplicationWorkspaceStage('interview', { lifecycle })).toMatchObject({ action: 'none' });
  });
});
