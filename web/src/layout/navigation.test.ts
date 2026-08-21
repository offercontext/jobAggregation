import { describe, expect, it } from 'vitest';
import {
  MODULE_NAV,
  defaultViewForModule,
  moduleTabsForView,
  resolveModuleForView,
} from './navigation';

describe('module navigation contract', () => {
  it('keeps four business destinations plus Settings and removes Pilot from primary navigation', () => {
    expect(MODULE_NAV.map((item) => item.label)).toEqual([
      '今日',
      '投递',
      '面试',
      '资料',
      '设置',
    ]);

    expect(MODULE_NAV.some((item) => item.label === 'Pilot')).toBe(false);
    expect(MODULE_NAV.some((item) => item.label === '面试')).toBe(true);
    expect(resolveModuleForView('dashboard')).toBe('today');
    expect(resolveModuleForView('reminders')).toBe('today');
    expect(resolveModuleForView('board')).toBe('applications');
    expect(resolveModuleForView('applications-list')).toBe('applications');
    expect(resolveModuleForView('calendar')).toBe('applications');
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
    expect(defaultViewForModule('resources')).toBe('resumes');
    expect(defaultViewForModule('settings')).toBe('settings');
  });

  it('exposes in-module tabs for secondary workflows', () => {
    expect(moduleTabsForView('calendar')).toEqual([
      { view: 'board', label: '看板' },
      { view: 'applications-list', label: '列表' },
      { view: 'calendar', label: '日历' },
      { view: 'offers', label: 'Offer' },
    ]);
    expect(moduleTabsForView('dashboard')).toEqual([
      { view: 'dashboard', label: '今日重点' },
      { view: 'reminders', label: '提醒' },
    ]);
    expect(moduleTabsForView('interview')).toEqual([
      { view: 'interview', label: '面试' },
    ]);
    expect(moduleTabsForView('knowledge')).toEqual([
      { view: 'resumes', label: '简历' },
      { view: 'reviews', label: '经历与故事' },
      { view: 'knowledge', label: '学习资料' },
    ]);
    expect(moduleTabsForView('pilot')).toEqual([{ view: 'pilot', label: '会话中心' }]);
  });
});
