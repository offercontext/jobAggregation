export const DESKTOP_TASK_FLOW_GOLDEN = {
  baseline: '0c10e05e256eb757d5f89a8b009dcea193f2fc78',
  navigationGroups: ['主要任务', '常用资料', '辅助'],
  primaryNavigation: ['今日', '投递', '面试', 'Offer'],
  resourceNavigation: ['素材库'],
  applicationTabs: ['概览', '准备', '进展'],
  interviewTabs: ['即将进行', '已完成', '自由练习'],
  resourceTabs: ['简历', '经历素材', '参考资料'],
  topBarActions: ['添加投递', '开始练习', '录入 Offer', '上传简历', '添加经历'],
  assistantOwners: { providers: 1, controllers: 1 },
  surfaceSwitchRequestDelta: { chat: 0, sse: 0 },
  desktopWidths: [768, 1024, 1280, 1440],
} as const;
