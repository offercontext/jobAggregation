import { describe, expect, it } from 'vitest';
import type { Application } from '@/types/application';
import { buildApplicationSearchCommands, buildPipelineNavigationCommands } from './CommandPalette';
import source from './CommandPalette.tsx?raw';

function app(patch: Partial<Application>): Application {
  return {
    id: patch.id ?? 1,
    company_name: patch.company_name ?? 'ByteDance',
    position_name: patch.position_name ?? 'Backend',
    job_url: patch.job_url ?? '',
    status: patch.status ?? 'applied',
    source: patch.source ?? 'web',
    notes: patch.notes ?? '',
    applied_at: patch.applied_at ?? '2026-07-01T09:00:00+08:00',
    first_pending_at: patch.first_pending_at ?? null,
    first_applied_at: patch.first_applied_at ?? '2026-07-01T09:00:00+08:00',
    first_written_test_at: patch.first_written_test_at ?? null,
    first_interview_at: patch.first_interview_at ?? null,
    first_offer_at: patch.first_offer_at ?? null,
    closed_reason: patch.closed_reason ?? '',
    closed_at: patch.closed_at ?? null,
    deleted_at: patch.deleted_at ?? null,
    created_at: patch.created_at ?? '2026-07-01T09:00:00+08:00',
    updated_at: patch.updated_at ?? '2026-07-02T09:00:00+08:00',
  };
}

describe('CommandPalette resume commands', () => {
  it('uses resume-library/new-resume wording instead of resume-match wording', () => {
    expect(source).toContain('data-testid="command-palette-results"');
    expect(source).toContain('打开简历库');
    expect(source).toContain('新建简历');
    expect(source).toContain('上传简历');
    expect(source).not.toContain('简历匹配');
  });

  it('offers Haru and the full Pilot workspace as separate assistant destinations', () => {
    expect(source).toContain('问 Haru');
    expect(source).toContain('打开 Pilot 工作区');
    expect(source).toContain('快速打开');
  });

  it('uses the unified settings destination wording', () => {
    expect(source).toContain("label: '打开设置'");
    expect(source).not.toContain('打开 AI 设置');
  });

  it('builds application search commands without soft-deleted rows', () => {
    const opened: number[] = [];
    const commands = buildApplicationSearchCommands(
      [
        app({ id: 1, company_name: 'ByteDance' }),
        app({ id: 2, company_name: 'ByteDance Archive', deleted_at: '2026-07-08T09:00:00+08:00' }),
      ],
      'byte',
      (item) => opened.push(item.id),
      () => undefined
    );

    expect(commands.map((command) => command.key)).toEqual(['app-1']);
    commands[0].run();
    expect(opened).toEqual([1]);
  });

  it('exposes direct commands for v0.1 pipeline surfaces', () => {
    const navigated: string[] = [];
    const commands = buildPipelineNavigationCommands(
      (view) => navigated.push(view),
      () => undefined
    );

    expect(commands.map((command) => command.label)).toEqual([
      '打开投递看板',
      '打开投递列表',
      '打开事件日历',
      '打开今日提醒',
    ]);

    commands[1].run();
    expect(navigated).toEqual(['applications-list']);
  });
});
