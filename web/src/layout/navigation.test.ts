import { describe, expect, it } from 'vitest';
import {
  NAVIGATION_GROUPS,
  MODULE_NAV,
  defaultViewForModule,
  moduleTabsForView,
  resolveModuleForView,
} from './navigation';

describe('module navigation contract', () => {
  it('keeps task, resource, and utility groups while removing Pilot from navigation', () => {
    expect(MODULE_NAV.map((item) => item.label)).toEqual([
      '今日',
      '投递',
      '面试',
      'Offer',
      '素材库',
      '设置',
    ]);
    expect(NAVIGATION_GROUPS).toEqual([
      { key: 'primary', label: '主要任务' },
      { key: 'resources', label: '常用资料' },
      { key: 'utility', label: '辅助' },
    ]);
    expect(MODULE_NAV.filter((item) => item.group === 'primary').map((item) => item.label)).toEqual([
      '今日',
      '投递',
      '面试',
      'Offer',
    ]);
    expect(MODULE_NAV.filter((item) => item.group === 'resources').map((item) => item.label)).toEqual(['素材库']);
    expect(MODULE_NAV.filter((item) => item.group === 'utility').map((item) => item.label)).toEqual(['设置']);

    expect(MODULE_NAV.some((item) => item.label === 'Pilot')).toBe(false);
    expect(MODULE_NAV.some((item) => item.label === '面试')).toBe(true);
    expect(resolveModuleForView('dashboard')).toBe('today');
    expect(resolveModuleForView('reminders')).toBe('today');
    expect(resolveModuleForView('board')).toBe('applications');
    expect(resolveModuleForView('applications-list')).toBe('applications');
    expect(resolveModuleForView('calendar')).toBe('today');
    expect(resolveModuleForView('offers')).toBe('offers');
    expect(resolveModuleForView('questions')).toBe('interview');
    expect(resolveModuleForView('interview')).toBe('interview');
    expect(resolveModuleForView('resumes')).toBe('resources');
    expect(resolveModuleForView('knowledge')).toBe('resources');
    expect(resolveModuleForView('pilot')).toBe('pilot');
  });

  it('selects stable defaults for module clicks', () => {
    expect(defaultViewForModule('today')).toBe('dashboard');
    expect(defaultViewForModule('applications')).toBe('board');
    expect(defaultViewForModule('interview')).toBe('interview');
    expect(defaultViewForModule('offers')).toBe('offers');
    expect(defaultViewForModule('resources')).toBe('resumes');
    expect(defaultViewForModule('settings')).toBe('settings');
  });

  it('exposes in-module tabs for secondary workflows', () => {
    expect(moduleTabsForView('calendar')).toEqual([
      { view: 'dashboard', label: '概览' },
      { view: 'reminders', label: '提醒' },
      { view: 'calendar', label: '日历' },
    ]);
    expect(moduleTabsForView('board')).toEqual([
      { view: 'board', label: '看板' },
      { view: 'applications-list', label: '列表' },
    ]);
    expect(moduleTabsForView('dashboard')).toEqual([
      { view: 'dashboard', label: '概览' },
      { view: 'reminders', label: '提醒' },
      { view: 'calendar', label: '日历' },
    ]);
    expect(moduleTabsForView('offers')).toEqual([{ view: 'offers', label: 'Offer' }]);
    expect(moduleTabsForView('interview')).toEqual([
      { view: 'interview', label: '面试' },
      { view: 'questions', label: '刷题' },
    ]);
    expect(moduleTabsForView('questions')).toEqual([
      { view: 'interview', label: '面试' },
      { view: 'questions', label: '刷题' },
    ]);
    expect(moduleTabsForView('knowledge')).toEqual([
      { view: 'resumes', label: '简历' },
      { view: 'reviews', label: '经历素材' },
      { view: 'knowledge', label: '参考资料' },
    ]);
    expect(moduleTabsForView('pilot')).toEqual([{ view: 'pilot', label: '会话中心' }]);
  });
});
