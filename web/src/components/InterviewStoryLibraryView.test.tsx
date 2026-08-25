// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const service = vi.hoisted(() => ({
  list: vi.fn(),
  get: vi.fn(),
  listVersions: vi.fn(),
  getVersion: vi.fn(),
  archive: vi.fn(),
  restore: vi.fn(),
}));
vi.mock('@/services/interviewStories', () => ({
  listInterviewStories: service.list,
  getInterviewStory: service.get,
  listInterviewStoryVersions: service.listVersions,
  getInterviewStoryVersion: service.getVersion,
  archiveInterviewStory: service.archive,
  restoreInterviewStory: service.restore,
}));

const { default: InterviewStoryLibraryView } = await import('./InterviewStoryLibraryView');

let root: Root | undefined;
let container: HTMLDivElement | undefined;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: () => ({ matches: false, addListener: () => undefined, removeListener: () => undefined }),
  });
  service.list.mockReset();
  service.get.mockReset();
  service.listVersions.mockReset();
  service.getVersion.mockReset();
  service.archive.mockReset();
  service.restore.mockReset();
  service.list.mockResolvedValue([{ id: 8, title: '订单延迟排查', status: 'active', current_version_id: 12, story_revision: 2, version_number: 2, source_states: [] }]);
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
});

describe('InterviewStoryLibraryView', () => {
  it('shows a Chinese Story entry and opens a user-initiated draft without writing', async () => {
    const open = vi.fn();
    act(() => root?.render(<InterviewStoryLibraryView onOpenDraft={open} />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    expect(container?.textContent).toContain('面试故事库');
    expect(container?.textContent).toContain('订单延迟排查');
    const create = [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent === '新建故事');
    expect(create?.className).not.toContain('ant-btn-primary');
    act(() => create?.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    expect(open).toHaveBeenCalledWith({ entrypoint: 'ui', reviewNoteId: undefined });
  });

  it('filters archived Stories and reads immutable version history without a write', async () => {
    const open = vi.fn();
    const version = {
      id: 12,
      version_number: 2,
      origin_kind: 'manual' as const,
      confirmed_at: '2026-08-10T00:00:00+00:00',
      source_fingerprint: 'frozen-source',
      content: {
        title: { id: 'title' as const, text: '订单延迟排查' },
        blocks: [{ id: 'situation_001', kind: 'situation' as const, text: '线上延迟', fact_mode: 'evidence_backed' as const }],
        capability_labels: [], applicable_questions: [], fact_gap_codes: [],
      },
      evidence_links: [{
        target_kind: 'title' as const, target_id: 'title', source_kind: 'user_assertion' as const,
        source_stable_id: '1', source_version_or_snapshot: 'assertion:1', source_path: '/statement', excerpt: '这是我的陈述',
      }],
      assertions: [{ id: 1, statement: '这是我的陈述', frozen: true as const }],
      source_states: [{
        source_kind: 'user_assertion' as const, source_stable_id: '1', source_version_or_snapshot: 'assertion:1', state: 'frozen_user_assertion' as const,
      }],
    };
    service.get.mockResolvedValue({ id: 8, title: '订单延迟排查', status: 'active', current_version_id: 12, story_revision: 2, version_number: 2, source_states: [], version });
    service.listVersions.mockResolvedValue([{ id: 12, version_number: 2, origin_kind: 'manual', confirmed_at: version.confirmed_at, source_fingerprint: version.source_fingerprint }]);
    service.getVersion.mockResolvedValue(version);
    act(() => root?.render(<InterviewStoryLibraryView onOpenDraft={open} />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    act(() => [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent === '查看版本')?.click());
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(service.get).toHaveBeenCalledWith(8);
    expect(service.listVersions).toHaveBeenCalledWith(8);
    expect(container?.textContent).toContain('已冻结的用户确认陈述');

    act(() => [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent === '已归档')?.click());
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(service.list).toHaveBeenLastCalledWith('archived', '');
    expect(service.archive).not.toHaveBeenCalled();
    expect(service.restore).not.toHaveBeenCalled();
  });

  it('keeps the last selected immutable version when an older history request resolves late', async () => {
    let resolveFirst: (value: unknown) => void = () => undefined;
    let resolveSecond: (value: unknown) => void = () => undefined;
    const version = (id: number, title: string) => ({
      id, version_number: id, origin_kind: 'manual' as const, confirmed_at: '2026-08-10T00:00:00+00:00', source_fingerprint: `fp-${id}`,
      content: { title: { id: 'title' as const, text: title }, blocks: [], capability_labels: [], applicable_questions: [], fact_gap_codes: ['missing_result'] },
      evidence_links: [], assertions: [], source_states: [],
    });
    service.get.mockResolvedValue({ id: 8, title: '订单延迟排查', status: 'active', current_version_id: 2, story_revision: 2, version_number: 2, source_states: [], version: version(2, '当前版本') });
    service.listVersions.mockResolvedValue([
      { id: 2, version_number: 2, origin_kind: 'manual', confirmed_at: '2026-08-10T00:00:00+00:00', source_fingerprint: 'fp-2' },
      { id: 1, version_number: 1, origin_kind: 'manual', confirmed_at: '2026-08-09T00:00:00+00:00', source_fingerprint: 'fp-1' },
    ]);
    service.getVersion
      .mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve; }))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveSecond = resolve; }));
    act(() => root?.render(<InterviewStoryLibraryView onOpenDraft={() => {}} />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    act(() => [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent === '查看版本')?.click());
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    const versionButtons = [...(container?.querySelectorAll('button') ?? [])].filter((button) => button.textContent?.startsWith('查看版本 '));
    act(() => versionButtons.find((button) => button.textContent === '查看版本 1')?.click());
    act(() => versionButtons.find((button) => button.textContent === '查看版本 2')?.click());
    await act(async () => { resolveSecond(version(2, '后选择的版本')); await Promise.resolve(); });
    await act(async () => { resolveFirst(version(1, '过期响应')); await Promise.resolve(); });

    expect(container?.textContent).toContain('后选择的版本');
    expect(container?.textContent).not.toContain('过期响应');
  });

  it('groups immutable STAR content and frozen evidence instead of rendering a wall of tags', async () => {
    const version = {
      id: 18,
      version_number: 3,
      origin_kind: 'proposal' as const,
      confirmed_at: '2026-08-12T08:00:00+00:00',
      source_fingerprint: 'story-source-fingerprint',
      content: {
        title: { id: 'title' as const, text: '线上延迟排查与风险同步' },
        blocks: [
          { id: 'situation_001', kind: 'situation' as const, text: '发布后接口延迟明显上升。', fact_mode: 'evidence_backed' as const },
          { id: 'task_001', kind: 'task' as const, text: '在不影响交易的前提下定位问题。', fact_mode: 'evidence_backed' as const },
          { id: 'action_001', kind: 'action' as const, text: '分段定位数据库连接池并同步风险。', fact_mode: 'evidence_backed' as const },
          { id: 'result_001', kind: 'result' as const, text: '延迟恢复并补齐监控。', fact_mode: 'evidence_backed' as const },
          { id: 'reflection_001', kind: 'reflection' as const, text: '以后会更早建立基线。', fact_mode: 'user_view' as const },
        ],
        capability_labels: [{ id: 'capability_001', text: '故障定位' }],
        applicable_questions: [{ id: 'question_001', text: '请介绍一次线上问题排查。' }],
        fact_gap_codes: [],
      },
      evidence_links: [
        { target_kind: 'title' as const, target_id: 'title', source_kind: 'interview_note' as const, source_stable_id: 'note:1', source_version_or_snapshot: 'note:1', source_path: '/questions', excerpt: '请介绍一次线上问题排查。' },
        { target_kind: 'block' as const, target_id: 'situation_001', source_kind: 'interview_note' as const, source_stable_id: 'note:1', source_version_or_snapshot: 'note:1', source_path: '/self_reflection', excerpt: '发布后接口延迟明显上升。' },
        { target_kind: 'block' as const, target_id: 'action_001', source_kind: 'user_assertion' as const, source_stable_id: 'assertion:1', source_version_or_snapshot: 'assertion:1', source_path: '/statement', excerpt: '我分段定位数据库连接池并同步风险。' },
      ],
      assertions: [{ id: 1, statement: '我分段定位数据库连接池并同步风险。', frozen: true as const }],
      source_states: [
        { source_kind: 'interview_note' as const, source_stable_id: 'note:1', source_version_or_snapshot: 'note:1', state: 'current' as const },
        { source_kind: 'user_assertion' as const, source_stable_id: 'assertion:1', source_version_or_snapshot: 'assertion:1', state: 'frozen_user_assertion' as const },
      ],
    };
    service.get.mockResolvedValue({
      id: 8,
      title: version.content.title.text,
      status: 'active',
      current_version_id: version.id,
      story_revision: 3,
      version_number: 3,
      source_states: version.source_states,
      version,
    });
    service.listVersions.mockResolvedValue([{ id: version.id, version_number: 3, origin_kind: 'proposal', confirmed_at: version.confirmed_at, source_fingerprint: version.source_fingerprint }]);
    act(() => root?.render(<InterviewStoryLibraryView onOpenDraft={() => {}} />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    act(() => [...(container?.querySelectorAll('button') ?? [])].find((button) => button.textContent === '查看版本')?.click());
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    const content = container?.querySelector('[data-testid="story-version-content"]');
    expect(content).not.toBeNull();
    expect(content?.textContent).toContain('适用问题');
    expect(content?.textContent).toContain('情境');
    expect(content?.textContent).toContain('行动');
    expect(content?.textContent).toContain('复盘');
    expect(content?.textContent).toContain('3 条证据引用');
    const evidenceGroups = content?.querySelectorAll('details[data-evidence-target]') ?? [];
    expect(evidenceGroups.length).toBe(3);
    expect([...evidenceGroups].every((details) => !(details as HTMLDetailsElement).open)).toBe(true);
    expect(container?.querySelectorAll('[data-testid="flat-evidence-tag"]').length).toBe(0);
    expect(service.archive).not.toHaveBeenCalled();
    expect(service.restore).not.toHaveBeenCalled();
  });
});
