import { renderToStaticMarkup } from 'react-dom/server';
import { App as AntApp } from 'antd';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { describe, expect, it } from 'vitest';
import KnowledgeSourcesView, {
  BriefValidationIssues,
  knowledgeSourceCanMutate,
  normalizeEvidencePage,
  normalizeJobProjection,
  normalizeKnowledgeSourceDetail,
  normalizeSearchHitProjection,
  normalizeSearchHits,
} from './KnowledgeSourcesView';
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
  it('fails closed and bounds hostile detail payloads', () => {
    expect(normalizeKnowledgeSourceDetail({ id: 3, source_kind: 'captured_interview_note' }, 3)).toBeNull();
    expect(normalizeKnowledgeSourceDetail({ id: 4, source_kind: 'markdown' }, 3)).toBeNull();

    const hostileProvenance = new Proxy({}, {
      get() { throw new Error('hostile getter'); },
      ownKeys() { throw new Error('hostile keys'); },
    });
    const projected = normalizeKnowledgeSourceDetail({
      id: 3,
      source_kind: 'markdown',
      title: 'a'.repeat(400),
      provenance: hostileProvenance,
      lifecycle: 'active',
      extraction_status: 'extracted',
      brief_status: 'ready',
    }, 3);
    expect(projected?.title.length).toBeLessThanOrEqual(161);
    expect(projected?.provenance.url).toBeUndefined();

    const { proxy, revoke } = Proxy.revocable([], {});
    revoke();
    expect(() => normalizeEvidencePage({ items: proxy }, 3)).not.toThrow();
    expect(normalizeEvidencePage({ items: proxy }, 3)).toMatchObject({ items: [], unavailable: true });
  });

  it('rejects unknown detail states and keeps deleting sources read-only', () => {
    const valid = {
      id: 3,
      source_kind: 'markdown',
      lifecycle: 'active',
      extraction_status: 'extracted',
      brief_status: 'ready',
    };
    expect(normalizeKnowledgeSourceDetail(valid, 3)).not.toBeNull();
    for (const patch of [
      { lifecycle: 'mystery' },
      { lifecycle: undefined },
      { extraction_status: 'mystery' },
      { extraction_status: undefined },
      { brief_status: 'mystery' },
      { brief_status: undefined },
    ]) {
      expect(normalizeKnowledgeSourceDetail({ ...valid, ...patch }, 3)).toBeNull();
    }
    const deleting = normalizeKnowledgeSourceDetail({ ...valid, lifecycle: 'deleting' }, 3);
    expect(deleting).not.toBeNull();
    expect(knowledgeSourceCanMutate(deleting)).toBe(false);
    expect(knowledgeSourceCanMutate(normalizeKnowledgeSourceDetail(valid, 3))).toBe(true);
  });

  it('drops malformed search rows and clamps user-visible snippets', () => {
    const hits = normalizeSearchHits([
      { evidence_id: 'e1', source_id: 3, snippet: 'x'.repeat(2_000) },
      { evidence_id: 'e2', source_id: 0, snippet: 'invalid owner' },
    ]);
    expect(hits).toHaveLength(1);
    expect(hits[0]?.snippet.length).toBeLessThanOrEqual(801);
  });

  it('keeps malformed job and search collections distinct from legitimate empty results', () => {
    expect(normalizeSearchHitProjection([])).toMatchObject({ items: [], unavailable: false });
    expect(normalizeSearchHitProjection(null)).toMatchObject({ items: [], unavailable: true });
    expect(normalizeSearchHitProjection([
      { evidence_id: 'e1', source_id: 3, snippet: '可用片段' },
      { evidence_id: '', source_id: 3, snippet: '损坏片段' },
    ])).toMatchObject({ items: [expect.objectContaining({ evidence_id: 'e1' })], unavailable: true });

    expect(normalizeJobProjection([])).toMatchObject({ items: [], unavailable: false });
    expect(normalizeJobProjection({})).toMatchObject({ items: [], unavailable: true });
    expect(normalizeJobProjection([
      { id: 1, kind: 'process', status: 'running', progress: 20 },
      { id: 0, kind: 'process', status: 'running' },
    ])).toMatchObject({ items: [expect.objectContaining({ id: 1 })], unavailable: true });
  });
  it('renders Source library surface with upload, bundle, paste entries and search box', () => {
    const markup = renderWithProviders();

    expect(markup).toContain('素材库');
    expect(markup).toContain('上传 Markdown / Text');
    expect(markup).toContain('上传图文资料');
    expect(markup).toContain('粘贴正文');
    expect(markup).toContain('来源依据');
    // SSR 阶段 React Query 处于 loading 状态；右栏空状态提供引导文案。
    expect(markup).toContain('选择左侧的资料来源查看详情');
    expect(markup).not.toContain('Wiki');
    expect(markup).not.toContain('Page');
  });

  it('does not claim frozen interview knowledge when the list is empty', () => {
    expect(renderWithProviders()).not.toContain('已保留当时版本');
  });

  it('keeps confirmed interview captures out of the external reference surface', () => {
    const markup = renderWithProviders();
    expect(markup).not.toContain('复盘沉淀');
    expect(markup).not.toContain('已确认面试片段');
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

    expect(markup).toContain('搜索资料内容');
    expect(markup).not.toContain('上传图文 Bundle');
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
    expect(viewSource).toContain("status === 'pending' || status === 'processing'");
    expect(viewSource).toContain("safeRead(query.state.data, 'extraction_status')");
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
    expect(structured).toContain('来源依据 1');
    expect(structured).not.toContain('ev_1');

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
    expect(legacy).toContain('来源依据 1');
    expect(legacy).not.toContain('ev_legacy');
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
