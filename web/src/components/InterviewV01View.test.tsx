import { renderToStaticMarkup } from 'react-dom/server';
import { App as AntApp } from 'antd';
import { describe, expect, it } from 'vitest';
import source from './InterviewV01View.tsx?raw';
import InterviewV01View, { projectInterviewEventCards } from './InterviewV01View';
import type { InterviewIndexItem } from '@/types/interviewIndex';

const row = (overrides: Partial<InterviewIndexItem> = {}): InterviewIndexItem => ({
  application_id: 7,
  event_id: 9,
  company_name: 'Acme',
  position_name: 'Engineer',
  scheduled_at: '2026-08-29T10:30:00Z',
  note_id: null,
  note_source_status: null,
  has_review_proposal: false,
  review_summary: null,
  has_confirmed_knowledge: false,
  preparation_available: true,
  event_status: 'todo',
  duration_minutes: 60,
  scheduled_at_state: 'present',
  ...overrides,
});

const NOW = Date.parse('2026-08-29T10:00:00Z');

describe('InterviewV01View', () => {
  it('organizes the workspace around upcoming, completed and free practice', () => {
    expect(source).toContain("label: '即将进行'");
    expect(source).toContain("label: '已完成'");
    expect(source).toContain("label: '自由练习'");
    expect(source).not.toContain("label: '模拟练习'");
    expect(source).not.toContain("label: '复盘与成长'");
  });

  it('renders the interview index loading surface without a generic mock entry', () => {
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

  it('delegates event cards to the central lifecycle projector and comparator', () => {
    expect(source).toContain('projectInterviewEventCard');
    expect(source).toContain('compareInterviewEventCards');
    expect(source).not.toContain('ENDED_EVENT_STATUSES');
    expect(source).not.toContain('isUpcomingInterview');
    expect(source).not.toContain('onOpenMockInterview');
    expect(source).not.toContain('function scheduledTimestamp');
  });

  it.each([
    ['done', 'completed', 'record_review'],
    ['cancelled', 'cancelled', 'none'],
    ['todo', 'needs_status_update', 'update_status'],
  ] as const)('keeps source status authoritative for %s', (event_status, bucket, primaryAction) => {
    const [card] = projectInterviewEventCards([row({
      event_status,
      ...(event_status === 'todo' ? { scheduled_at: '2026-08-29T09:00:00Z' } : {}),
    })], NOW);
    expect(card).toMatchObject({ bucket, primaryAction });
  });

  it('does not infer an event from wall-clock time when status is terminal', () => {
    const [card] = projectInterviewEventCards([row({ event_status: 'done', scheduled_at: '2099-01-01T00:00:00Z' })], NOW);
    expect(card).toMatchObject({ lifecycle: 'completed', bucket: 'completed', primaryAction: 'record_review' });
  });

  it('uses the exported card order for equal times and deduplicates one event identity', () => {
    const cards = projectInterviewEventCards([
      row({ event_id: 12 }),
      row({ event_id: 10 }),
      row({ event_id: 10, company_name: 'A different duplicate' }),
    ], NOW);
    expect(cards.map((card) => card.eventId)).toEqual([10, 12]);
  });

  it('fails closed for duplicate source identities independent of source order', () => {
    const left = { id: 9, application_id: 7, event_type: 'interview', status: 'todo' } as never;
    const right = { id: 9, application_id: 7, event_type: 'interview', status: 'done' } as never;
    for (const events of [[left, right], [right, left]]) {
      const [card] = projectInterviewEventCards([row()], NOW, events);
      expect(card).toMatchObject({ bucket: 'unavailable', primaryAction: 'none' });
      expect(card.contractReasons).toContain('source_mismatch');
    }
  });

  it('does not trust an index row missing from an explicitly loaded event source', () => {
    const [card] = projectInterviewEventCards([row()], NOW, []);
    expect(card).toMatchObject({ bucket: 'unavailable', primaryAction: 'none' });
    expect(card.contractReasons).toContain('source_mismatch');
  });

  it('isolates a hostile index row without hiding valid event cards', () => {
    const hostile = new Proxy(row({ event_id: 99 }), {
      get() { throw new Error('hostile interview index row'); },
    });
    expect(() => projectInterviewEventCards([hostile, row({ event_id: 10 })], NOW)).not.toThrow();
    expect(projectInterviewEventCards([hostile, row({ event_id: 10 })], NOW).map((card) => card.eventId)).toEqual([10]);
  });

  it('keeps one canonical free-practice launcher and no write on initial render', () => {
    expect(source).toContain('onOpenFreePractice');
    expect(source).not.toContain('createInterviewPracticeCase');
    expect(source).not.toContain('onOpenStudio');
  });

  it('keeps the free-practice tab available without a readiness form on the event tab', () => {
    const markup = renderToStaticMarkup(
      <AntApp>
        <InterviewV01View />
      </AntApp>,
    );
    expect(markup).toContain('自由练习');
    expect(markup).not.toContain('选择投递');
    expect(markup).not.toContain('选择面试事件');
  });

  it('keeps interview row content inset from the Ant list item border', async () => {
    const fsModule = 'node:fs';
    const { readFileSync } = (await import(fsModule)) as {
      readFileSync: (path: URL, encoding: string) => string;
    };
    const workflowCss = readFileSync(new URL('./ui/WorkflowSurface.module.css', import.meta.url), 'utf8');

    expect(source).toContain('className={workflowStyles.listRow}');
    expect(workflowCss).toMatch(/\.listRow\.listRow\s*\{[^}]*padding:\s*13px 14px;/s);
  });
});
