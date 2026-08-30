// @vitest-environment jsdom
import { act, createElement, type ComponentProps } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import PilotOpportunityFitV2Card, { type PilotOpportunityFitProjectionStatus } from './PilotOpportunityFitV2Card';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | undefined;
let container: HTMLDivElement;

function renderCard(
  status: PilotOpportunityFitProjectionStatus,
  overrides: Partial<ComponentProps<typeof PilotOpportunityFitV2Card>> = {},
) {
  const onOpenTask = vi.fn();
  root = createRoot(container);
  act(() => root?.render(createElement(PilotOpportunityFitV2Card, {
    status,
    summary: null,
    history: [],
    historyState: 'ready',
    onOpenTask,
    ...overrides,
  })));
  return onOpenTask;
}

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
});

afterEach(() => {
  act(() => root?.unmount());
  container.remove();
});

describe('PilotOpportunityFitV2Card', () => {
  it.each([
    ['idle', '开始'],
    ['pending', '等待确认'],
    ['result_unknown', '结果待确认'],
    ['ready', '查看结果'],
    ['source_conflict', '结果待确认'],
    ['unavailable', '暂时不可用'],
  ] as const)('projects %s using only safe status copy', (status, expected) => {
    const onOpenTask = renderCard(status);
    expect(container.textContent).toContain(expected);
    expect(container.textContent).toContain('打开岗位判断');
    expect(container.textContent).not.toMatch(/V[12]|schema|hash|token|snapshot|stage/i);
    expect(container.querySelector('textarea,input,select')).toBeNull();
    expect(container.querySelectorAll('button')).toHaveLength(1);
    act(() => container.querySelector('button')?.click());
    expect(onOpenTask).toHaveBeenCalledTimes(1);
  });

  it('renders a bounded immutable history projection and never exposes internal identity', () => {
    const onOpenTask = renderCard('ready', {
      summary: '基于冻结资料的摘要',
      history: [{
        internalKey: 'v2:7:42',
        createdAt: '2026-08-30T08:00:00Z',
        summary: '历史摘要',
        sourceState: 'source_changed',
      }],
    });
    expect(container.textContent).toContain('基于冻结资料的摘要');
    expect(container.textContent).toContain('历史摘要');
    expect(container.textContent).toContain('来源已更新，已有结果仍保留');
    expect(container.textContent).not.toContain('v2:7:42');
    expect(onOpenTask).not.toHaveBeenCalled();
  });

  it('does not render Invalid Date for malformed history timestamps', () => {
    renderCard('ready', {
      history: [{
        internalKey: 'v2:7:43',
        createdAt: '2026-02-30T09:00:00Z',
        summary: '历史摘要',
        sourceState: 'current',
      }],
    });
    expect(container.textContent).toContain('时间暂不可用');
    expect(container.textContent).not.toContain('Invalid Date');
  });

  it.each([
    ['loading', '历史记录加载中'],
    ['error', '部分历史暂时不可用'],
    ['absent', '暂时没有可查看的历史记录'],
  ] as const)('keeps %s history source explicit', (historyState, expected) => {
    renderCard('idle', { historyState });
    expect(container.textContent).toContain(expected);
    expect(container.textContent).not.toContain('暂无历史记录');
  });
});
