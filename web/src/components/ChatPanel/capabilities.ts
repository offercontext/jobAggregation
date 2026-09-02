import type { ComponentType } from 'react';
import {
  UnorderedListOutlined,
  FileSearchOutlined,
  FileTextOutlined,
  ProfileOutlined,
  CalendarOutlined,
  SearchOutlined,
  DollarOutlined,
  SwapOutlined,
  PlusCircleOutlined,
  EditOutlined,
  DeleteOutlined,
  SaveOutlined,
  MessageOutlined,
  CompassOutlined,
  AimOutlined,
  FlagOutlined,
  SolutionOutlined,
  AudioOutlined,
  AuditOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons';

export type ToolKind = 'read' | 'write';

export interface ToolMeta {
  /** Human-readable Chinese label shown in the process timeline. */
  label: string;
  kind: ToolKind;
  icon: ComponentType;
}

/**
 * Metadata for backend agent tools keyed by the
 * tool name the model emits. Used by ProcessTimeline to render each step and
 * by ProposalCard to pick an icon for write confirmations.
 */
export const TOOL_META: Record<string, ToolMeta> = {
  // ---- read ----
  list_applications: { label: '查看投递列表', kind: 'read', icon: UnorderedListOutlined },
  get_application: { label: '查看投递详情', kind: 'read', icon: ProfileOutlined },
  list_jd_analyses: { label: '查看 JD 分析', kind: 'read', icon: FileSearchOutlined },
  get_jd_analysis: { label: '查看 JD 分析详情', kind: 'read', icon: FileSearchOutlined },
  list_resumes: { label: '查看简历', kind: 'read', icon: SolutionOutlined },
  get_resume: { label: '读取简历', kind: 'read', icon: SolutionOutlined },
  list_resume_matches: { label: '查看简历匹配记录', kind: 'read', icon: SolutionOutlined },
  list_notes: { label: '查看复盘记录', kind: 'read', icon: FileTextOutlined },
  list_application_events: { label: '查看投递事件', kind: 'read', icon: CalendarOutlined },
  get_application_event: { label: '查看事件详情', kind: 'read', icon: CalendarOutlined },
  list_offers: { label: '查看 Offer 列表', kind: 'read', icon: DollarOutlined },
  get_offer: { label: '查看 Offer 详情', kind: 'read', icon: DollarOutlined },
  compare_offers: { label: '对比 Offer', kind: 'read', icon: SwapOutlined },
  // ---- write ----
  save_application_jd_version: { label: '保存岗位资料', kind: 'write', icon: SaveOutlined },
  create_application_submission_snapshot: {
    label: '冻结投递事实',
    kind: 'write',
    icon: SafetyCertificateOutlined,
  },
  record_application_outcome: {
    label: '记录投递结果',
    kind: 'write',
    icon: AuditOutlined,
  },
  create_application: { label: '新建投递', kind: 'write', icon: PlusCircleOutlined },
  update_application_status: { label: '更新投递状态', kind: 'write', icon: EditOutlined },
  add_note: { label: '添加复盘记录', kind: 'write', icon: PlusCircleOutlined },
  update_note: { label: '更新复盘记录', kind: 'write', icon: EditOutlined },
  delete_note: { label: '删除复盘记录', kind: 'write', icon: DeleteOutlined },
  create_application_event: { label: '新建投递事件', kind: 'write', icon: PlusCircleOutlined },
  update_application_event: { label: '更新投递事件', kind: 'write', icon: EditOutlined },
  delete_application_event: { label: '删除投递事件', kind: 'write', icon: DeleteOutlined },
  create_offer: { label: '新建 Offer', kind: 'write', icon: PlusCircleOutlined },
  update_offer: { label: '更新 Offer', kind: 'write', icon: EditOutlined },
  save_offer_assessment: { label: '保存 Offer 评估', kind: 'write', icon: SaveOutlined },
  resume_update_career_intent: { label: '更新简历求职意向', kind: 'write', icon: AimOutlined },
  resume_rewrite_highlight: { label: '改写简历亮点', kind: 'write', icon: EditOutlined },
};

export function toolMeta(name: string): ToolMeta {
  return TOOL_META[name] ?? { label: name, kind: 'read', icon: SearchOutlined };
}

export interface Capability {
  id: string;
  group: string;
  label: string;
  hint: string;
  /** Prompt sent (or inserted) when the capability is triggered. */
  prompt: string;
  icon: ComponentType;
}

/** Capabilities offered in the default general assistant mode. */
export const GENERAL_CAPABILITIES: Capability[] = [
  {
    id: 'list-apps',
    group: '投递',
    label: '查看投递进度',
    hint: '列出当前所有投递及状态',
    prompt: '我现在有哪些投递记录？请按状态分组列出。',
    icon: UnorderedListOutlined,
  },
  {
    id: 'weak-points',
    group: '复盘',
    label: '汇总复盘薄弱点',
    hint: '总结最近面试复盘里反复出现的短板',
    prompt: '总结我最近复盘记录里反复出现的薄弱点，并给出改进建议。',
    icon: CompassOutlined,
  },
  {
    id: 'log-review',
    group: '复盘',
    label: '记录一次面试复盘',
    hint: '把刚结束的面试整理成复盘',
    prompt: '帮我记录刚才的面试复盘，我先口述，你帮我结构化整理。',
    icon: EditOutlined,
  },
  {
    id: 'upcoming',
    group: '日程',
    label: '近期日程',
    hint: '查看最近的笔试、面试、Offer 进展和截止事项',
    prompt: '帮我看看最近有哪些笔试、面试、Offer 进展和截止事项。',
    icon: CalendarOutlined,
  },
];

/** Capabilities offered when the thread is bound to an offer (nego coach). */
export const NEGO_CAPABILITIES: Capability[] = [
  {
    id: 'assess',
    group: 'Offer',
    label: '评估这份 Offer',
    hint: '判断值不值得接受',
    prompt: '帮我分析这个 offer 值不值得接受，从薪资、成长、风险综合评估。',
    icon: AimOutlined,
  },
  {
    id: 'simulate',
    group: '谈薪',
    label: '模拟 HR 压价',
    hint: 'HR 说预算有限时怎么回应',
    prompt: '模拟 HR 说预算有限，我该怎么回应？请给我可直接说出口的话术。',
    icon: MessageOutlined,
  },
  {
    id: 'signing-bonus',
    group: '谈薪',
    label: '争取更高签字费',
    hint: '生成可直接使用的话术',
    prompt: '帮我准备争取更高签字费的话术。',
    icon: SolutionOutlined,
  },
  {
    id: 'compare',
    group: 'Offer',
    label: '对比手上的 Offer',
    hint: '按总包 / 成长 / 风险综合对比',
    prompt: '对比我手上的几个 offer，哪个更值得接受？',
    icon: SwapOutlined,
  },
  {
    id: 'red-line',
    group: '谈薪',
    label: '谈薪红线自检',
    hint: '哪些话不能说、底线在哪',
    prompt: '谈薪时有哪些红线和底线是我必须守住的？帮我做个自检清单。',
    icon: FlagOutlined,
  },
];

export function capabilitiesForMode(isNego: boolean): Capability[] {
  return isNego ? NEGO_CAPABILITIES : GENERAL_CAPABILITIES;
}

export { AudioOutlined };
