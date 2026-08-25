import type { ViewMode } from '@/layout/navigation';
import { STATUS_LABELS, type Application } from '@/types/application';
import type { PilotContextChip, PilotPageContext } from '@/types/chat';
import { OFFER_STATUS_LABELS, type Offer } from '@/types/offer';

export type { PilotContextChip, PilotPageContext } from '@/types/chat';

export const PILOT_VIEW_LABELS: Record<ViewMode, string> = {
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
};

interface BuildPilotPageContextOptions {
  view: ViewMode;
  selectedApplication?: Application;
  coachedOffer?: Offer;
}

export function buildPilotPageContext({
  view,
  selectedApplication,
  coachedOffer,
}: BuildPilotPageContextOptions): PilotPageContext | undefined {
  if (view === 'pilot') return undefined;

  const context: PilotPageContext = {
    view,
    label: PILOT_VIEW_LABELS[view],
  };

  if (selectedApplication) {
    context.entity = {
      kind: 'application',
      id: String(selectedApplication.id),
      label: `${selectedApplication.company_name} · ${selectedApplication.position_name}`,
      description: `当前状态：${STATUS_LABELS[selectedApplication.status]}`,
    };
  } else if (coachedOffer) {
    context.entity = {
      kind: 'offer',
      id: String(coachedOffer.id),
      label: `${coachedOffer.company_name} · ${coachedOffer.position_name} Offer`,
      description: `Offer 状态：${OFFER_STATUS_LABELS[coachedOffer.status]}`,
    };
  }

  return context;
}

export function pageContextKey(context?: PilotPageContext): string {
  if (!context) return '';

  const filters = context.filters
    ?.map(({ key, value }) => ({ key, value }))
    .sort((left, right) => left.key.localeCompare(right.key) || left.value.localeCompare(right.value));

  return JSON.stringify({
    view: context.view,
    ...(context.entity
      ? { entity: { kind: context.entity.kind, id: context.entity.id } }
      : {}),
    ...(filters?.length ? { filters } : {}),
  });
}

export function pageContextChips(context: PilotPageContext): PilotContextChip[] {
  return [
    { key: 'view', label: '页面', value: context.label },
    ...(context.entity
      ? [
          {
            key: 'entity',
            label: context.entity.kind === 'application' ? '投递' : 'Offer',
            value: context.entity.label,
          },
        ]
      : []),
    ...(context.filters?.map((filter) => ({
      key: `filter:${filter.key}`,
      label: filter.label,
      value: filter.value,
    })) ?? []),
  ];
}

export function removePageContextChip(
  context: PilotPageContext,
  chipKey: string
): PilotPageContext | undefined {
  if (chipKey === 'view') return undefined;

  if (chipKey === 'entity') {
    const { entity, ...withoutEntity } = context;
    void entity;
    return withoutEntity;
  }

  if (!chipKey.startsWith('filter:') || !context.filters) return context;

  const filterKey = chipKey.slice('filter:'.length);
  const filters = context.filters.filter((filter) => filter.key !== filterKey);
  if (filters.length === context.filters.length) return context;
  if (filters.length > 0) return { ...context, filters };

  const { filters: removedFilters, ...withoutFilters } = context;
  void removedFilters;
  return withoutFilters;
}

export interface PilotPageContextRemovalState {
  contextKey: string;
  removedChipKeys: string[];
}

export type PilotPageContextRemovalAction =
  | { type: 'sync'; contextKey: string }
  | { type: 'remove'; contextKey: string; chipKey: string };

export function createPilotPageContextRemovalState(
  contextKey: string,
): PilotPageContextRemovalState {
  return { contextKey, removedChipKeys: [] };
}

export function pilotPageContextRemovalReducer(
  state: PilotPageContextRemovalState,
  action: PilotPageContextRemovalAction,
): PilotPageContextRemovalState {
  if (action.type === 'sync') {
    return action.contextKey === state.contextKey
      ? state
      : createPilotPageContextRemovalState(action.contextKey);
  }

  const removedChipKeys = action.contextKey === state.contextKey ? state.removedChipKeys : [];
  if (removedChipKeys.includes(action.chipKey)) return state;
  return {
    contextKey: action.contextKey,
    removedChipKeys: [...removedChipKeys, action.chipKey],
  };
}

export function deriveActivePageContext(
  context: PilotPageContext | undefined,
  removalState: PilotPageContextRemovalState,
): PilotPageContext | undefined {
  if (!context || removalState.contextKey !== pageContextKey(context)) return context;

  return removalState.removedChipKeys.reduce<PilotPageContext | undefined>(
    (activeContext, chipKey) =>
      activeContext ? removePageContextChip(activeContext, chipKey) : undefined,
    context,
  );
}
