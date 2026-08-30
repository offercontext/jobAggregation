import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import type { MaterialRecord } from '@/features/materialSurfaces/materialClassification';

vi.mock('./InterviewStoryLibraryView', () => ({
  default: () => <div data-testid="story-library">经历故事</div>,
}));

import ExperienceMaterialsView from './ExperienceMaterialsView';

const metadata = {
  origin_note_id: 21,
  application_event_id: 7,
  note_fingerprint: 'fingerprint',
  capture_schema_version: 'interview-note-capture-v1',
};

const capture: MaterialRecord = {
  id: 11,
  origin_kind: 'confirmed_interview_capture',
  source_id: 31,
  version_id: 41,
  captured_at: '2026-08-29T10:00:00Z',
  capture_metadata: metadata,
  title: '接口迁移片段',
  content: { blocks: [{ block_id: 'b1', text: '我完成了迁移。' }] },
};

describe('ExperienceMaterialsView', () => {
  it('combines the Story library with confirmed captures and excludes broken captures', () => {
    const markup = renderToStaticMarkup(
      <ExperienceMaterialsView
        confirmedCaptures={[capture, {
          id: 12,
          origin_kind: 'confirmed_interview_capture',
          source_id: 32,
          version_id: 42,
          captured_at: '2026-08-29T10:00:00Z',
          title: '损坏片段',
        }]}
      />,
    );
    expect(markup).toContain('经历故事');
    expect(markup).toContain('接口迁移片段');
    expect(markup).not.toContain('损坏片段');
    expect(markup).toContain('经历素材');
  });

  it('keeps source loading, error, and empty states explicit', () => {
    expect(renderToStaticMarkup(<ExperienceMaterialsView confirmedCapturesLoading />)).toContain('正在加载经历素材');
    expect(renderToStaticMarkup(<ExperienceMaterialsView confirmedCapturesError />)).toContain('面试片段暂时不可用');
    expect(renderToStaticMarkup(<ExperienceMaterialsView confirmedCaptures={[]} />)).toContain('还没有已确认的面试片段');
  });

  it('fails closed for an explicit ready envelope without captures, including revoked proxies', () => {
    expect(renderToStaticMarkup(
      <ExperienceMaterialsView confirmedCapturesState="ready" confirmedCaptures={null as never} />,
    )).toContain('面试片段暂时不可用');

    const { proxy, revoke } = Proxy.revocable([], {});
    revoke();
    expect(() => renderToStaticMarkup(
      <ExperienceMaterialsView confirmedCapturesState="ready" confirmedCaptures={proxy as never} />,
    )).not.toThrow();
    expect(renderToStaticMarkup(
      <ExperienceMaterialsView confirmedCapturesState="ready" confirmedCaptures={proxy as never} />,
    )).toContain('面试片段暂时不可用');
  });
});
