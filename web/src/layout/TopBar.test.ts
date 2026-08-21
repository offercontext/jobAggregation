import source from './TopBar.tsx?raw';
import { describe, expect, it } from 'vitest';

describe('top bar actions', () => {
  it('does not expose a redundant right-side chat button', () => {
    expect(source).not.toContain('右侧对话');
    expect(source).not.toContain('onOpenChat');
    expect(source).not.toContain('showContextualPilot');
  });

  it('names the command entry 快速打开', () => {
    expect(source).toContain('快速打开');
    expect(source).not.toContain('搜索 <');
  });

  it('routes the gear through the unified settings label', () => {
    expect(source).toContain('aria-label="设置"');
    expect(source).not.toContain('aria-label="AI 设置"');
  });
});
