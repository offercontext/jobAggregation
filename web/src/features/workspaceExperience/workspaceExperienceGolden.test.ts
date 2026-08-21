import { describe, expect, it } from 'vitest';
import { WORKSPACE_EXPERIENCE_GOLDEN } from './workspaceExperienceGolden';

describe('desktop workspace synthetic golden', () => {
  it('pins the phase-two baseline and product surface without user data', () => {
    expect(WORKSPACE_EXPERIENCE_GOLDEN.baseline).toBe('2f6e895e02b86f33052a2e507e9b0404bb82f4b5');
    expect(WORKSPACE_EXPERIENCE_GOLDEN.primaryNavigation).toEqual(['今日', '投递', '面试', '资料']);
    expect(WORKSPACE_EXPERIENCE_GOLDEN.weakNavigation).toEqual(['设置']);
    expect(WORKSPACE_EXPERIENCE_GOLDEN.applicationStages).toEqual([
      '准备投递', '已投递', '笔试', '已约面试', '面试结束', '已获 Offer', '已结束',
    ]);
    expect(JSON.stringify(WORKSPACE_EXPERIENCE_GOLDEN)).not.toMatch(/api[_-]?key|sk-[a-z0-9]/i);
  });
});
