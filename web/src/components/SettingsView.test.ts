import { describe, expect, it } from 'vitest';
import source from './SettingsView.tsx?raw';

describe('SettingsView localization', () => {
  it('groups the workspace settings into stable product sections', () => {
    expect(source).toContain('AI 与模型');
    expect(source).toContain('数据与备份');
    expect(source).toContain('Haru 与外观');
    expect(source).toContain('语音');
    expect(source).toContain('高级与诊断');
    expect(source).toContain('<AISettingsDrawer');
    expect(source).toContain('高级运行信息');
    expect(source).toContain('查看运行日志与诊断');
    expect(source).not.toContain('onOpenAISettings');
  });

  it('uses Chinese product copy for settings and diagnostics', () => {
    expect(source).toContain('设置');
    expect(source).toContain('AI 与模型');
    expect(source).toContain('高级与诊断');
    expect(source).toContain('配置 AI');
    expect(source).toContain('导出备份');
    expect(source).toContain('复制诊断信息');
    expect(source).toContain('重新打开新手引导');
    expect(source).toContain('数据目录');
    expect(source).toContain('日志筛选');
    expect(source).toContain('多供应商');
    expect(source).toContain('Fallback');
    expect(source).not.toContain('>Settings<');
    expect(source).not.toContain('AI runtime');
    expect(source).not.toContain('Runtime diagnostics');
    expect(source).not.toContain('Configure AI');
    expect(source).not.toContain('Details unavailable');
  });

  it('exposes local backup export from the settings page', () => {
    expect(source).toContain('exportBackup');
    expect(source).toContain('导出备份');
    expect(source).toContain('/backups/export');
  });

  it('declares the paginated diagnostics controls and recovery copy', () => {
    expect(source).toContain('Pagination');
    expect(source).toContain('LOG_PAGE_SIZE');
    expect(source).toContain('重试日志加载');
    expect(source).toContain('360');
  });
});
