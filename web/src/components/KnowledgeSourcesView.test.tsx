import { renderToStaticMarkup } from 'react-dom/server';
import { App as AntApp } from 'antd';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { describe, expect, it } from 'vitest';
import KnowledgeSourcesView, { BriefValidationIssues } from './KnowledgeSourcesView';
import viewSource from './KnowledgeSourcesView.tsx?raw';

function renderWithProviders() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  });
  return renderToStaticMarkup(
    <QueryClientProvider client={queryClient}>
      <AntApp>
        <KnowledgeSourcesView />
      </AntApp>
    </QueryClientProvider>
  );
}

describe('KnowledgeSourcesView', () => {
  it('renders Source library surface with upload, bundle, paste entries and search box', () => {
    const markup = renderWithProviders();

    expect(markup).toContain('资料来源');
    expect(markup).toContain('上传 Markdown / Text');
    expect(markup).toContain('上传图文 Bundle');
    expect(markup).toContain('粘贴正文');
    expect(markup).toContain('来源依据');
    // SSR 阶段 React Query 处于 loading 状态；右栏空状态提供引导文案。
    expect(markup).toContain('选择左侧的资料来源查看详情');
    expect(markup).not.toContain('Wiki');
    expect(markup).not.toContain('Page');
  });

  it('does not claim frozen interview knowledge when the list is empty', () => {
    expect(renderWithProviders()).not.toContain('已冻结来源');
  });

  it('does not expose legacy Page/Review/Index/Lint/Config entries', () => {
    const markup = renderWithProviders();

    expect(markup).not.toContain('自动 Wiki');
    expect(markup).not.toContain('Wikilink');
    expect(markup).not.toContain('Protected Page');
    expect(markup).not.toContain('Mutation Review');
  });

  it('keeps KI-05 dedup guidance out of the main source library surface', () => {
    const markup = renderWithProviders();

    // 去重说明移入已选 Source 的“导入记录”提示图标，不再占用资料来源首页空间。
    expect(markup).not.toContain('相同内容自动复用已有 Source');
    expect(markup).not.toContain('自动创建 Page');
  });

  it('renders KI-06 archive filter switch without legacy wiki surface', () => {
    const markup = renderWithProviders();

    expect(markup).toContain('显示归档资料');
    // 危险区永久删除入口在 Source 详情中,SSR 时不会出现(需要选中 Source),
    // 但服务契约由 services/knowledge.test.ts 验证。
  });

  it('exposes KI-08 evidence search entry with CJK-friendly placeholder', () => {
    const markup = renderWithProviders();

    expect(markup).toContain('搜索来源依据');
    expect(markup).toContain('中文/英文关键词');
  });

  it('keeps KI-09 Brief Attempt timeline code retained for V1.1 (KV1-02 / ADR-0002)', () => {
    // KV1-02：V1 不渲染 Brief UI，但 BriefAttemptTimeline 组件代码保留以备 V1.1。
    // 验证组件源码仍在（attempt.has_more / 尚未加载完整时间线），V1.1 恢复时可用。
    expect(typeof KnowledgeSourcesView).toBe('function');
    expect(viewSource).toContain('attempt.has_more');
    expect(viewSource).toContain('尚未加载完整时间线');
  });

  it('polls list/detail by Extraction in-flight state only (KV1-02: not Brief)', () => {
    // KV1-02 / ADR-0002：V1 轮询只由 Extraction pending/processing 触发，不因 Brief
    // 状态刷新；Brief Pill 已隐藏，brief_status 不再驱动列表/详情轮询。
    expect(viewSource).toContain("item.extraction_status === 'pending'");
    expect(viewSource).toContain("source.extraction_status === 'pending'");
    // briefQuery 由 SHOW_BRIEF_UI 控制 enabled，V1 不发请求、不轮询 brief_status。
    expect(viewSource).toContain('enabled: SHOW_BRIEF_UI');
    // briefRebuildMutation 的乐观刷新逻辑保留，V1.1 恢复 Brief UI 时复用。
    expect(viewSource).toContain('setQueryData');
  });

  it('hides Brief UI surface in V1 (KV1-02 / ADR-0002)', () => {
    // V1 工作台不展示 Brief Pill / Brief 块 / 重建入口 / Attempt timeline；Brief 组件
    // 代码保留以备 V1.1，由 SHOW_BRIEF_UI 开关统一隐藏。
    expect(viewSource).toContain('const SHOW_BRIEF_UI = false');
    // 4 处 Brief 表面（列表 Pill / 详情头 Pill / BriefBlock / StatusBlock Brief 区块）
    // 都被 SHOW_BRIEF_UI 包裹，V1 不渲染。
    expect((viewSource.match(/\{SHOW_BRIEF_UI \?/g) ?? []).length).toBeGreaterThanOrEqual(4);
    // briefQuery 在 V1 不启用：不发请求、不轮询。
    expect(viewSource).toContain('enabled: SHOW_BRIEF_UI');
  });

  it('renders structured validation diagnostics and remains compatible with legacy reasons', () => {
    const structured = renderToStaticMarkup(
      <AntApp>
        <BriefValidationIssues
          report={{
            failure_count: 1,
            issues: [
              {
                block_path: 'key_points[1]',
                issue_type: 'support_partial',
                decision: 'partial',
                reason_code: 'unsupported_qualifier',
                unsupported_fragments: ['默认'],
                explanation: 'Evidence 未说明这是默认行为。<原文>',
                suggested_rewrite: '仅陈述 Evidence 直接支持的条件。',
                evidence_ids: ['ev_1'],
              },
            ],
          }}
          onCitationJump={() => undefined}
        />
      </AntApp>,
    );
    expect(structured).toContain('限定词无直接证据');
    expect(structured).toContain('默认');
    expect(structured).toContain('建议改写');
    // 诊断内容按普通文本渲染，不能把模型输出解释为 HTML。
    expect(structured).toContain('&lt;原文&gt;');
    expect(structured).toContain('ev_1');

    const legacy = renderToStaticMarkup(
      <AntApp>
        <BriefValidationIssues
          report={{
            issues: [
              {
                block_path: 'limitations[0]',
                issue_type: 'support_partial',
                decision: 'partial',
                reason: '旧版校验摘要',
                evidence_ids: ['ev_legacy'],
              },
            ],
          }}
          onCitationJump={() => undefined}
        />
      </AntApp>,
    );
    expect(legacy).toContain('旧版校验摘要');
    expect(legacy).toContain('ev_legacy');
  });

  it('does not expose one-time Knowledge reset product entry', () => {
    // KBR-07 收缩为本地 CLI 后，前端不得再出现清空入口、确认对话框或 reset mutation。
    const markup = renderWithProviders();

    expect(markup).not.toContain('清空 Knowledge 数据');
    expect(markup).not.toContain('清空 Knowledge 数据域');
    expect(markup).not.toContain('确认清空');
    expect(viewSource).not.toContain('resetKnowledgeDomain');
    expect(viewSource).not.toContain('resetMutation');
    expect(viewSource).not.toContain('/knowledge/reset');
  });
});
