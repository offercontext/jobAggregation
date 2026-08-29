// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { renderToStaticMarkup } from 'react-dom/server';
import { App as AntApp } from 'antd';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ResumeCard from './ResumeCard';
import type { Resume } from '@/types/resume';

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const Card = ResumeCard as React.ComponentType<any>;

function renderCard(resume: Resume, onCompare?: (id: number) => void) {
  return renderToStaticMarkup(
    <AntApp>
      <Card
        resume={resume}
        onEdit={vi.fn()}
        onSetMaster={vi.fn()}
        onCopy={vi.fn()}
        onDelete={vi.fn()}
        onCompare={onCompare}
      />
    </AntApp>
  );
}

let root: Root | null = null;
let host: HTMLDivElement | null = null;

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
});

describe('ResumeCard v0.1', () => {
  it('renders structured resume metadata and excludes match/download/export actions', () => {
    const markup = renderCard({
      id: 7,
      name: 'legacy-name',
      file_path: '',
      parsed_data: 'legacy text',
      parse_status: 'structured-ready',
      title: '后端主简历',
      is_master: true,
      parent_resume_id: null,
      source: 'dialog',
      source_file_path: '',
      content_json: {
        career_intent: { target_roles: ['Backend Engineer'] },
        contact: { name: 'Ada' },
      },
      deleted_at: null,
      created_at: '2026-07-08T02:03:00Z',
      completion_percent: 67,
      missing_sections: ['career_intent', 'projects'],
      is_complete: false,
    } as Resume);

    expect(markup).toContain('后端主简历');
    expect(markup).toContain('主简历');
    expect(markup).toContain('Pilot 对话');
    expect(markup).toContain('67%');
    expect(markup).toContain('求职意向');
    expect(markup).toContain('项目经历');
    expect(markup).toContain('创建于');
    expect(markup).toContain('编辑');
    expect(markup).toContain('复制');
    expect(markup).toContain('基础简历不可删除');
    expect(markup).not.toContain('匹配');
    expect(markup).not.toContain('下载');
    expect(markup).not.toContain('导出');
  });

  it('shows set-as-master only for non-master resumes', () => {
    const resume = {
      id: 8,
      name: '',
      file_path: '',
      parsed_data: '',
      parse_status: 'structured-ready',
      title: '前端样例简历',
      is_master: false,
      parent_resume_id: 7,
      source: 'sample',
      source_file_path: '',
      content_json: {},
      deleted_at: null,
      created_at: '2026-07-08T04:00:00Z',
      completion_percent: 33,
      missing_sections: ['education'],
      is_complete: false,
    } as Resume;

    const markup = renderToStaticMarkup(
      <AntApp>
        <Card
          resume={resume}
          resumes={[resume, {
            id: 7,
            name: '',
            file_path: '',
            parsed_data: '',
            parse_status: 'text-ready',
            title: '基础版本',
            is_master: true,
            parent_resume_id: null,
            source: 'manual',
            source_file_path: '',
            content_json: {},
            deleted_at: null,
            created_at: '2026-07-08T00:00:00Z',
            completion_percent: 100,
            missing_sections: [],
            is_complete: true,
          } as Resume]}
          onEdit={vi.fn()}
          onSetMaster={vi.fn()}
          onCopy={vi.fn()}
          onDelete={vi.fn()}
        />
      </AntApp>,
    );

    expect(markup).toContain('岗位版本');
    expect(markup).toContain('基于 基础版本');
    expect(markup).toContain('设为基础简历');
    expect(markup).not.toContain('设为主简历');
    expect(markup).not.toContain('主简历不可删除');
  });

  it('shows an unconfirmed relationship and blocks lineage-creating copy actions', () => {
    const markup = renderCard({
      id: 12,
      name: '',
      file_path: '',
      parsed_data: '',
      parse_status: 'text-ready',
      title: '',
      is_master: false,
      parent_resume_id: 99,
      source: 'manual',
      source_file_path: '',
      content_json: {},
      deleted_at: null,
      created_at: '2026-08-06T04:00:00Z',
      completion_percent: 0,
      missing_sections: [],
      is_complete: false,
    } as Resume);

    expect(markup).toContain('关系待确认');
    expect(markup).toContain('关系待确认，暂不能复制');
    expect(markup).toContain('关系待确认，暂不能设为基础简历');
    expect(markup).toContain('未命名简历');
    expect(markup).not.toContain('#12');
  });

  it('renders a compare action and sends only the resume id to its callback', async () => {
    const resume = {
      id: 11,
      name: '',
      file_path: '',
      parsed_data: '',
      parse_status: 'text-ready',
      title: '中文岗位版本',
      is_master: false,
      parent_resume_id: 1,
      source: 'sample_copy',
      source_file_path: '',
      content_json: {},
      deleted_at: null,
      created_at: '2026-08-06T04:00:00Z',
      completion_percent: 90,
      missing_sections: [],
      is_complete: true,
    } as Resume;
    const onCompare = vi.fn();
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => {
      root?.render(
        <AntApp>
          <Card
            resume={resume}
            onEdit={vi.fn()}
            onSetMaster={vi.fn()}
            onCopy={vi.fn()}
            onDelete={vi.fn()}
            onCompare={onCompare}
          />
        </AntApp>,
      );
    });

    const button = Array.from(host.querySelectorAll('button')).find((item) => item.textContent?.includes('对比版本'));
    expect(button).not.toBeUndefined();
    await act(async () => (button as HTMLButtonElement).click());
    expect(onCompare).toHaveBeenCalledTimes(1);
    expect(onCompare).toHaveBeenCalledWith(11);
  });
});
