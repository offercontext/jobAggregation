import type { ApplicationStatus } from '@/types/application';
import type { EventLifecycleV1 } from '@/features/interviewEvents/eventLifecycle';

export interface ApplicationWorkspaceStage {
  label: '准备投递' | '已投递' | '笔试' | '已约面试' | '面试结束' | '已获 Offer' | '已结束';
  primaryActionLabel: string;
  action: 'materials' | 'followup' | 'written-test' | 'interview-prepare' | 'interview-review' | 'offer' | 'outcome';
}

export function getApplicationWorkspaceStage(
  status: ApplicationStatus,
  options: { lifecycle?: EventLifecycleV1; hasCompletedInterview?: boolean; hasInterviewReview?: boolean } = {},
): ApplicationWorkspaceStage {
  switch (status) {
    case 'pending':
      return { label: '准备投递', primaryActionLabel: '完善投递材料', action: 'materials' };
    case 'applied':
      return { label: '已投递', primaryActionLabel: '设置跟进时间', action: 'followup' };
    case 'written_test':
      return { label: '笔试', primaryActionLabel: '准备笔试', action: 'written-test' };
    case 'interview':
      return (options.lifecycle === 'completed' || (options.lifecycle === undefined && options.hasCompletedInterview === true))
        ? {
            label: '面试结束',
            primaryActionLabel: options.hasInterviewReview ? '查看本轮复盘' : '完成面试复盘',
            action: 'interview-review',
          }
        : { label: '已约面试', primaryActionLabel: '准备本轮面试', action: 'interview-prepare' };
    case 'offer':
      return { label: '已获 Offer', primaryActionLabel: '查看 Offer 与截止时间', action: 'offer' };
    case 'closed':
      return { label: '已结束', primaryActionLabel: '记录结果与经验', action: 'outcome' };
  }
}
