export type ViewMode =
  | 'dashboard'
  | 'board'
  | 'applications-list'
  | 'calendar'
  | 'reminders'
  | 'interview'
  | 'reviews'
  | 'offers'
  | 'knowledge'
  | 'questions'
  | 'resumes'
  | 'pilot'
  | 'settings';

export type ModuleKey =
  | 'today'
  | 'applications'
  | 'interview'
  | 'resources'
  | 'pilot'
  | 'settings';

export interface ModuleNavItem {
  key: ModuleKey;
  label: string;
  defaultView: ViewMode;
}

export interface ModuleTabItem {
  view: ViewMode;
  label: string;
}

export const MODULE_NAV: ModuleNavItem[] = [
  { key: 'today', label: '今日', defaultView: 'dashboard' },
  { key: 'applications', label: '投递', defaultView: 'board' },
  { key: 'interview', label: '面试', defaultView: 'interview' },
  { key: 'resources', label: '资料', defaultView: 'resumes' },
  { key: 'settings', label: '设置', defaultView: 'settings' },
];

export const MODULE_TABS: Record<ModuleKey, ModuleTabItem[]> = {
  today: [
    { view: 'dashboard', label: '今日重点' },
    { view: 'reminders', label: '提醒' },
  ],
  applications: [
    { view: 'board', label: '看板' },
    { view: 'applications-list', label: '列表' },
    { view: 'calendar', label: '日历' },
    { view: 'offers', label: 'Offer' },
  ],
  interview: [
    { view: 'interview', label: '面试' },
  ],
  resources: [
    { view: 'resumes', label: '简历' },
    { view: 'reviews', label: '经历与故事' },
    { view: 'knowledge', label: '学习资料' },
  ],
  pilot: [{ view: 'pilot', label: '会话中心' }],
  settings: [{ view: 'settings', label: '设置' }],
};

const VIEW_TO_MODULE: Partial<Record<ViewMode, ModuleKey>> = {
  dashboard: 'today',
  reminders: 'today',
  resumes: 'resources',
  reviews: 'resources',
  knowledge: 'resources',
  questions: 'interview',
  board: 'applications',
  'applications-list': 'applications',
  calendar: 'applications',
  offers: 'applications',
  interview: 'interview',
  pilot: 'pilot',
  settings: 'settings',
};

const DEFAULT_VIEW_BY_MODULE: Record<ModuleKey, ViewMode> = {
  today: 'dashboard',
  applications: 'board',
  interview: 'interview',
  resources: 'resumes',
  pilot: 'pilot',
  settings: 'settings',
};

export function resolveModuleForView(view: ViewMode): ModuleKey {
  const module = VIEW_TO_MODULE[view];
  if (!module) throw new Error(`View ${view} is not part of v0.1 navigation`);
  return module;
}

export function defaultViewForModule(module: ModuleKey): ViewMode {
  return DEFAULT_VIEW_BY_MODULE[module];
}

export function moduleTabsForView(view: ViewMode): ModuleTabItem[] {
  return MODULE_TABS[resolveModuleForView(view)];
}
