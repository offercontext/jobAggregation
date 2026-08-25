// @vitest-environment jsdom
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import InterviewReadinessCenter from './InterviewReadinessCenter';

async function loadReadinessCss(): Promise<string> {
  const fsModule = 'node:fs';
  const { readFileSync } = (await import(fsModule)) as {
    readFileSync: (path: string | URL, encoding: string) => string;
  };
  return readFileSync('src/features/interviewReadiness/InterviewReadinessCenter.module.css', 'utf8');
}

describe('InterviewReadinessCenter', () => {
  it('explains both modes and keeps entry disabled until required sources are ready', () => {
    const markup = renderToStaticMarkup(
      <InterviewReadinessCenter applications={[]} events={[]} resumes={[]} />,
    );

    expect(markup).toContain('围绕真实投递练习');
    expect(markup).toContain('快速练习');
    expect(markup).toContain('开始前检查');
    expect(markup).toContain('当前 JD（只读）');
    expect(markup).toContain('进入模拟面试');
    expect(markup).toContain('disabled=""');
  });

  it('does not silently select the first application, event, or resume', () => {
    const markup = renderToStaticMarkup(
      <InterviewReadinessCenter
        applications={[{ id: 7, company_name: 'A', position_name: 'Engineer', status: 'active' } as never]}
        events={[{ id: 8, application_id: 7, event_type: 'interview', scheduled_at: '2026-08-15T10:00:00Z', status: 'scheduled' } as never]}
        resumes={[{ id: 9, title: '筱哲', name: '筱哲', deleted_at: null } as never]}
      />,
    );

    expect(markup).not.toContain('value="7" selected=""');
    expect(markup).not.toContain('value="8" selected=""');
    expect(markup).not.toContain('value="9" selected=""');
    expect(markup).toContain('请选择投递');
    expect(markup).toContain('请选择已排期面试');
    expect(markup).toContain('请选择已保存简历');
  });

  it('can be fixed to one task mode when embedded in the interview tabs', () => {
    const markup = renderToStaticMarkup(
      <InterviewReadinessCenter applications={[]} events={[]} resumes={[]} initialMode="quick" fixedMode="quick" />,
    );

    expect(markup).toContain('data-readiness-mode="quick"');
    expect(markup).not.toContain('aria-label="练习模式"');
  });

  it('supports secondary embedding and removes transforms for reduced motion', async () => {
    const markup = renderToStaticMarkup(
      <InterviewReadinessCenter applications={[]} events={[]} resumes={[]} actionEmphasis="secondary" />,
    );
    const styles = await loadReadinessCss();

    expect(markup).toContain('secondaryAction');
    expect(styles).toMatch(/prefers-reduced-motion:[^}]+reduce[\s\S]*transform:\s*none/);
  });
});
