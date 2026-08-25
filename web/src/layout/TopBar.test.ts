import source from './TopBar.tsx?raw';
import { describe, expect, it } from 'vitest';

describe('top bar actions', () => {
  it('is a page-aware presentation component without owning navigation state', () => {
    expect(source).not.toContain('右侧对话');
    expect(source).not.toContain('onOpenChat');
    expect(source).not.toContain('showContextualPilot');
    expect(source).toContain('primaryAction');
    expect(source).toContain('primaryAction.onClick');
    expect(source).toContain('primaryAction.label');
  });

  it('names the command entry 快速打开', () => {
    expect(source).toContain('快速打开');
    expect(source).not.toContain('搜索 <');
  });

  it('routes the gear through the unified settings label', () => {
    expect(source).toContain('aria-label="设置"');
    expect(source).not.toContain('aria-label="AI 设置"');
  });

  it('removes the strong streak treatment while retaining keyboard-visible controls', () => {
    expect(source).not.toContain('streakDays');
    expect(source).toContain('快速打开');
    expect(source).toContain('className={styles.primaryAction}');
    expect(source).toContain('className={styles.actionButton}');
  });
});
