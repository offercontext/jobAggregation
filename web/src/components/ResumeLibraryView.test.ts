import { describe, expect, it } from 'vitest';
import { createServer } from 'vite';
import source from './ResumeLibraryView.tsx?raw';
import cardSource from './ResumeCard.tsx?raw';
import editorSource from './ResumeEditorDrawer.tsx?raw';
import compareSource from './ResumeVersionCompareDrawer.tsx?raw';
import factSource from './ResumeFactSupplementWorkspace.tsx?raw';

async function onboardingStyles() {
  const server = await createServer({
    configFile: 'vite.config.ts',
    server: { middlewareMode: true },
    appType: 'custom',
  });

  try {
    return (await server.transformRequest('/src/components/ResumeLibraryView.module.css?raw'))?.code ?? '';
  } finally {
    await server.close();
  }
}

describe('ResumeLibraryView onboarding source contract', () => {
  it('uses user-facing creation copy and keeps samples in the low-frequency menu', () => {
    expect(source).toContain('和 Haru 创建初稿');
    expect(source).toContain('上传现有简历');
    expect(source).toContain('更多创建方式');
    expect(source).not.toContain('和 Pilot 创建薄版');
    expect(source).not.toContain('>上传 PDF<');
    expect(source).not.toContain('<Button type="primary"');
  });
  it('lets the shell trigger the page-owned upload controller without replaying a stale token', () => {
    expect(source).toContain('uploadRequestToken?: number;');
    expect(source).toContain('lastUploadRequestTokenRef');
    expect(source).toContain('uploadRequestToken > previous');
  });
  it('focuses the resume creation entry without creating a resume', () => {
    expect(source).toContain('onboardingFocusToken?: number;');
    expect(source).toContain('data-onboarding-target="resume-create"');
    expect(source).toContain('onboardingEntryRef.current?.focus({ preventScroll: true });');
    expect(source).not.toContain('onboardingFocusToken && createDialogMut.mutate()');
  });

  it('keeps the onboarding outline visible while its pulse runs', async () => {
    const styles = await onboardingStyles();

    expect(styles).toContain('outline: 2px solid var(--op-primary);');
    expect(styles).toContain('box-shadow:');
    expect(styles).not.toContain('outline-color: transparent;');
  });

  it('shares one resume lineage vocabulary across every resume surface', () => {
    const surfaces = [cardSource, editorSource, compareSource, factSource];

    for (const surface of surfaces) {
      expect(surface).toContain('formatResumeLineage');
      expect(surface).not.toContain('主简历');
      expect(surface).not.toContain('父版本');
      expect(surface).not.toContain('简历 #');
    }
    expect(source).toContain('resumes={resumes}');
    expect(editorSource).toContain('resolveResumeLineage');
    expect(compareSource).toContain('resolveResumeLineage');
    expect(factSource).toContain('resolveResumeLineage');
  });
});
