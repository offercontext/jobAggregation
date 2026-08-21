import { describe, expect, it } from 'vitest';
import source from './ApplicationDetail.tsx?raw';

describe('ApplicationDetail staged workspace', () => {
  it('keeps one stage action and moves low-frequency actions into more', () => {
    expect(source).toContain('getApplicationWorkspaceStage');
    expect(source).toContain('stage.primaryActionLabel');
    expect(source).toContain('更多操作');
    expect(source).toContain('menu={{ items: moreActionItems');
  });

  it('uses stable business sections and the unified Haru entry copy', () => {
    expect(source).toContain('概览');
    expect(source).toContain('岗位与材料');
    expect(source).toContain('日程与沟通');
    expect(source).toContain('面试');
    expect(source).toContain('结果');
    expect(source).toContain('让 Haru 帮我');
    expect(source).not.toContain('问 Pilot');
    expect(source).not.toContain('在 Pilot 中评估');
  });
});
