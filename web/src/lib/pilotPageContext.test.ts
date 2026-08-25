import { describe, expect, it } from 'vitest';
import type { Application } from '@/types/application';
import type { Offer } from '@/types/offer';
import type { PilotPageContext } from '@/types/chat';
import {
  PILOT_VIEW_LABELS,
  buildPilotPageContext,
  createPilotPageContextRemovalState,
  deriveActivePageContext,
  pageContextChips,
  pageContextKey,
  pilotPageContextRemovalReducer,
  removePageContextChip,
} from './pilotPageContext';

const application: Application = {
  id: 42,
  company_name: '星海科技',
  position_name: '前端工程师',
  job_url: '',
  status: 'interview',
  source: '',
  notes: '',
  applied_at: '',
  created_at: '',
  updated_at: '',
};

const offer: Offer = {
  id: 7,
  application_id: 42,
  company_name: '星海科技',
  position_name: '前端工程师',
  status: 'negotiating',
  base_monthly: 30_000,
  months_per_year: 14,
  signing_bonus: 0,
  equity: '',
  perks: '',
  deadline: '',
  notes: '',
  assessment: '',
  total_cash: 420_000,
  created_at: '',
  updated_at: '',
};

describe('pilot page context', () => {
  it('labels every current module view', () => {
    expect(PILOT_VIEW_LABELS).toEqual({
      dashboard: '今日',
      board: '投递看板',
      'applications-list': '投递列表',
      calendar: '投递日历',
      reminders: '提醒',
      interview: '面试',
      reviews: '经历素材',
      offers: 'Offer',
      knowledge: '参考资料',
      questions: '题库',
      resumes: '简历',
      pilot: 'Pilot',
      settings: '设置',
    });
  });

  it('builds context for a normal module page', () => {
    expect(buildPilotPageContext({ view: 'dashboard' })).toEqual({
      view: 'dashboard',
      label: '今日',
    });
  });

  it('uses the selected application as the contextual entity', () => {
    expect(
      buildPilotPageContext({ view: 'board', selectedApplication: application, coachedOffer: offer })
    ).toEqual({
      view: 'board',
      label: '投递看板',
      entity: {
        kind: 'application',
        id: '42',
        label: '星海科技 · 前端工程师',
        description: '当前状态：面试',
      },
    });
  });

  it('uses the coached offer as the contextual entity', () => {
    expect(buildPilotPageContext({ view: 'offers', coachedOffer: offer })).toEqual({
      view: 'offers',
      label: 'Offer',
      entity: {
        kind: 'offer',
        id: '7',
        label: '星海科技 · 前端工程师 Offer',
        description: 'Offer 状态：谈判中',
      },
    });
  });

  it('does not create page context for the full Pilot page', () => {
    expect(buildPilotPageContext({ view: 'pilot', selectedApplication: application })).toBeUndefined();
  });

  it('uses stable identity fields for the page context key', () => {
    const first: PilotPageContext = {
      view: 'applications-list',
      label: '旧页面名',
      entity: { kind: 'application', id: '42', label: '旧实体名' },
      filters: [
        { key: 'status', label: '状态', value: 'interview' },
        { key: 'source', label: '来源', value: 'campus' },
      ],
    };
    const renamedAndReordered: PilotPageContext = {
      view: 'applications-list',
      label: '新页面名',
      entity: { kind: 'application', id: '42', label: '新实体名' },
      filters: [
        { key: 'source', label: '渠道', value: 'campus' },
        { key: 'status', label: '阶段', value: 'interview' },
      ],
    };

    expect(pageContextKey(first)).toBe(pageContextKey(renamedAndReordered));
    expect(JSON.parse(pageContextKey(first))).toEqual({
      view: 'applications-list',
      entity: { kind: 'application', id: '42' },
      filters: [
        { key: 'source', value: 'campus' },
        { key: 'status', value: 'interview' },
      ],
    });
    expect(pageContextKey()).toBe('');
  });

  it('derives fresh same-identity data while preserving removed chips', () => {
    const original: PilotPageContext = {
      view: 'applications-list',
      label: '投递列表',
      entity: {
        kind: 'application',
        id: '42',
        label: '星海科技 · 前端工程师',
        description: '当前状态：面试',
      },
      filters: [{ key: 'source', label: '来源', value: '校招' }],
    };
    const updated: PilotPageContext = {
      ...original,
      label: '最新投递',
      entity: {
        ...original.entity!,
        label: '星海智能 · 高级前端工程师',
        description: '当前状态：已录用',
      },
      filters: [{ key: 'source', label: '渠道', value: '校招' }],
    };
    const identity = pageContextKey(original);
    const state = pilotPageContextRemovalReducer(
      createPilotPageContextRemovalState(identity),
      { type: 'remove', contextKey: identity, chipKey: 'filter:source' },
    );

    expect(deriveActivePageContext(updated, state)).toEqual({
      view: 'applications-list',
      label: '最新投递',
      entity: {
        kind: 'application',
        id: '42',
        label: '星海智能 · 高级前端工程师',
        description: '当前状态：已录用',
      },
    });
  });

  it('ignores stale removals immediately when the semantic identity changes', () => {
    const oldIdentity = pageContextKey({ view: 'board', label: '投递看板' });
    const state = pilotPageContextRemovalReducer(
      createPilotPageContextRemovalState(oldIdentity),
      { type: 'remove', contextKey: oldIdentity, chipKey: 'view' },
    );
    const navigated: PilotPageContext = { view: 'calendar', label: '投递日历' };
    const newIdentity = pageContextKey(navigated);

    expect(deriveActivePageContext(navigated, state)).toEqual(navigated);
    expect(
      pilotPageContextRemovalReducer(state, { type: 'sync', contextKey: newIdentity }),
    ).toEqual(createPilotPageContextRemovalState(newIdentity));
  });

  it('creates readable chips for the view, entity, and filters', () => {
    const context: PilotPageContext = {
      view: 'applications-list',
      label: '投递列表',
      entity: { kind: 'application', id: '42', label: '星海科技 · 前端工程师' },
      filters: [{ key: 'status', label: '状态', value: '面试' }],
    };

    expect(pageContextChips(context)).toEqual([
      { key: 'view', label: '页面', value: '投递列表' },
      { key: 'entity', label: '投递', value: '星海科技 · 前端工程师' },
      { key: 'filter:status', label: '状态', value: '面试' },
    ]);
  });

  it('removes the entire page context with the view chip', () => {
    expect(removePageContextChip({ view: 'board', label: '投递看板' }, 'view')).toBeUndefined();
  });

  it('removes only the entity chip', () => {
    const context: PilotPageContext = {
      view: 'board',
      label: '投递看板',
      entity: { kind: 'application', id: '42', label: '星海科技 · 前端工程师' },
      filters: [{ key: 'status', label: '状态', value: '面试' }],
    };

    expect(removePageContextChip(context, 'entity')).toEqual({
      view: 'board',
      label: '投递看板',
      filters: [{ key: 'status', label: '状态', value: '面试' }],
    });
  });

  it('removes one filter and omits an empty filters property', () => {
    const context: PilotPageContext = {
      view: 'applications-list',
      label: '投递列表',
      filters: [
        { key: 'status', label: '状态', value: '面试' },
        { key: 'source', label: '来源', value: '校招' },
      ],
    };

    expect(removePageContextChip(context, 'filter:status')).toEqual({
      view: 'applications-list',
      label: '投递列表',
      filters: [{ key: 'source', label: '来源', value: '校招' }],
    });
    expect(
      removePageContextChip(
        { view: 'applications-list', label: '投递列表', filters: [context.filters![0]] },
        'filter:status'
      )
    ).toEqual({ view: 'applications-list', label: '投递列表' });
  });
});
