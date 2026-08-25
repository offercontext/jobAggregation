import { renderToStaticMarkup } from 'react-dom/server';
import { App as AntApp } from 'antd';
import { describe, expect, it } from 'vitest';
import source from './InterviewV01View.tsx?raw';
import InterviewV01View, { isUpcomingInterview } from './InterviewV01View';

async function loadWorkflowCss(): Promise<string> {
  const fsModule = 'node:fs';
  const { readFileSync } = (await import(fsModule)) as {
    readFileSync: (path: URL, encoding: string) => string;
  };
  return readFileSync(new URL('./ui/WorkflowSurface.module.css', import.meta.url), 'utf8');
}

describe('InterviewV01View', () => {
  it('organizes the workspace around upcoming, completed and free practice', () => {
    expect(source).toContain("label: '即将进行'");
    expect(source).toContain("label: '已完成'");
    expect(source).toContain("label: '自由练习'");
    expect(source).not.toContain("label: '模拟练习'");
    expect(source).not.toContain("label: '复盘与成长'");
  });
  it('renders the interview index loading surface without mock interview entry points', () => {
    const markup = renderToStaticMarkup(
      <AntApp>
        <InterviewV01View />
      </AntApp>,
    );

    expect(markup).toContain('面试');
    expect(markup).toContain('正在加载面试列表');
    expect(markup).toContain('data-testid="interview-surface"');
    expect(markup).not.toContain('模拟面试');
    expect(markup).not.toContain('新建复盘');
  });

  it('passes the selected event id to the direct preparation entry', () => {
    expect(source).toContain('onOpenPreparation?.(item.application_id, item.event_id)');
    expect(source).toContain('准备面试');
  });

  it('treats cancelled future events as ended and refreshes time buckets while mounted', () => {
    const item = {
      application_id: 1, event_id: 2, company_name: '示例', position_name: '工程师',
      scheduled_at: '2099-01-01T00:00:00Z', note_id: null, note_source_status: null,
      has_review_proposal: false, review_summary: null, has_confirmed_knowledge: false,
      preparation_available: true,
    };
    expect(isUpcomingInterview(item, Date.now(), 'cancelled')).toBe(false);
    expect(source).toContain('window.setInterval(() => setCurrentTime(Date.now()), 60_000)');
  });

  it('keeps the legacy mock callback reachable only when the preparation entry is unavailable', () => {
    expect(source).toContain('!onOpenPreparation && onOpenMockInterview');
    expect(source).toContain('开始模拟面试');
  });

  it('wraps Ant list actions instead of overflowing a narrow interview row', async () => {
    const workflowCss = await loadWorkflowCss();

    expect(source).toContain('className={workflowStyles.listRow}');
    expect(workflowCss).toContain(':global(.ant-list-item-action)');
    expect(workflowCss).toContain('flex-wrap: wrap');
  });

  it('keeps interview row content inset from the Ant list item border', async () => {
    const workflowCss = await loadWorkflowCss();

    expect(workflowCss).toMatch(/\.listRow\.listRow\s*\{[^}]*padding:\s*13px 14px;/s);
  });
});
