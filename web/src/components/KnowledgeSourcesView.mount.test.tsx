// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({
  confirmed: [] as unknown[],
  sources: [] as unknown,
  search: vi.fn(),
}));

vi.mock('@/services/knowledge', () => ({
  archiveKnowledgeSource: vi.fn(),
  buildKnowledgeAssetContentUrl: vi.fn(() => '#'),
  buildKnowledgeSourceContentUrl: vi.fn(() => '#'),
  cancelKnowledgeJob: vi.fn(),
  deleteKnowledgeSource: vi.fn(),
  fetchKnowledgeSource: vi.fn(),
  fetchKnowledgeSourceBrief: vi.fn(),
  fetchKnowledgeSourceContent: vi.fn(),
  fetchKnowledgeSourceEvidence: vi.fn(),
  fetchKnowledgeSourceJobs: vi.fn(),
  fetchKnowledgeSources: vi.fn(() => Promise.resolve(state.sources)),
  fetchConfirmedInterviewKnowledgeNotes: vi.fn(() => Promise.resolve(state.confirmed)),
  pasteKnowledgeSource: vi.fn(),
  rebuildKnowledgeSourceBrief: vi.fn(),
  searchKnowledgeEvidence: state.search,
  unarchiveKnowledgeSource: vi.fn(),
  updateKnowledgeSourceTitle: vi.fn(),
  uploadKnowledgeBundle: vi.fn(),
  uploadKnowledgeSource: vi.fn(),
}));

const { QueryClient, QueryClientProvider } = await import('@tanstack/react-query');
const { App: AntApp } = await import('antd');
const { default: KnowledgeSourcesView } = await import('./KnowledgeSourcesView');
const knowledgeService = await import('@/services/knowledge');

let root: Root | undefined;
let container: HTMLDivElement | undefined;

function renderView() {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  act(() => root?.render(
    <QueryClientProvider client={queryClient}>
      <AntApp><KnowledgeSourcesView /></AntApp>
    </QueryClientProvider>,
  ));
  return queryClient;
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, 20));
  });
}

function submitSearch(query = '延迟') {
  const input = container?.querySelector('input[placeholder^="搜索资料内容"]') as HTMLInputElement | null;
  if (!input) throw new Error('search input not found');
  act(() => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
    setter?.call(input, query);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  });
  const button = [...(container?.querySelectorAll('button') ?? [])]
    .find((candidate) => candidate.textContent?.replace(/\s/g, '') === '搜索');
  act(() => button?.dispatchEvent(new MouseEvent('click', { bubbles: true })));
}

beforeEach(() => {
  vi.clearAllMocks();
  Element.prototype.scrollIntoView = vi.fn();
  state.confirmed = [];
  state.sources = [];
  state.search.mockReset();
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: () => ({ matches: false, addListener: () => undefined, removeListener: () => undefined }),
  });
});

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
  root = undefined;
  container = undefined;
});

describe('KnowledgeSourcesView mounted source states', () => {
  it('retains a search target until asynchronously loaded evidence is present', async () => {
    const source = { id: 3, source_kind: 'markdown', lifecycle: 'active',
      title: '搜索测试资料', extraction_status: 'extracted', brief_status: 'pending' };
    state.sources = [source];
    vi.mocked(knowledgeService.fetchKnowledgeSource).mockResolvedValue(source as never);
    vi.mocked(knowledgeService.fetchKnowledgeSourceContent).mockResolvedValue('解析前正文');
    vi.mocked(knowledgeService.fetchKnowledgeSourceJobs).mockResolvedValue({ jobs: [], origins: [] } as never);
    let resolveEvidence!: (value: unknown) => void;
    vi.mocked(knowledgeService.fetchKnowledgeSourceEvidence).mockImplementation(() => new Promise((resolve) => {
      resolveEvidence = resolve as (value: unknown) => void;
    }));
    state.search.mockResolvedValue({ query: '延迟', hits: [{ evidence_id: 'e1', source_id: 3, snippet: '迟到的依据' }] });
    renderView();
    await flush();
    submitSearch();
    await flush();
    const open = [...container!.querySelectorAll('button')].find((button) => button.textContent?.includes('打开并定位'));
    expect(open).toBeTruthy();
    act(() => open!.click());
    await flush();
    await act(async () => resolveEvidence({ items: [{ id: 'e1', source_id: 3,
      canonical_excerpt: '迟到的依据', heading_path: [], ordinal: 1 }], next_cursor: null }));
    await flush();
    expect(Element.prototype.scrollIntoView).toHaveBeenCalledOnce();
  });
  it('refreshes evidence after extraction completes without reopening the source', async () => {
    const source = { id: 3, source_kind: 'markdown', lifecycle: 'active',
      title: '解析测试资料', display_title: '解析测试资料', extraction_status: 'processing', brief_status: 'pending' };
    state.sources = [source];
    vi.mocked(knowledgeService.fetchKnowledgeSource).mockResolvedValue(source as never);
    vi.mocked(knowledgeService.fetchKnowledgeSourceContent).mockResolvedValue('解析前正文');
    vi.mocked(knowledgeService.fetchKnowledgeSourceJobs).mockResolvedValue({ jobs: [], origins: [] } as never);
    vi.mocked(knowledgeService.fetchKnowledgeSourceEvidence).mockResolvedValue({ items: [], next_cursor: null });
    const client = renderView();
    await flush();
    const sourceButton = container!.querySelector<HTMLElement>('.knowledge-source-item');
    expect(sourceButton).toBeTruthy();
    act(() => sourceButton!.click());
    await flush();
    const before = vi.mocked(knowledgeService.fetchKnowledgeSourceEvidence).mock.calls.length;
    const contentBefore = vi.mocked(knowledgeService.fetchKnowledgeSourceContent).mock.calls.length;
    expect(before).toBeGreaterThan(0);
    expect(contentBefore).toBeGreaterThan(0);
    vi.mocked(knowledgeService.fetchKnowledgeSourceContent).mockResolvedValue('解析完成后的正文');
    vi.mocked(knowledgeService.fetchKnowledgeSourceEvidence).mockResolvedValue({ items: [{
      id: 'e1', source_id: 3, canonical_excerpt: '解析完成后的依据', heading_path: [], ordinal: 1,
    }], next_cursor: null } as never);
    act(() => client.setQueryData(['knowledge', 'source', 3], { ...source, extraction_status: 'extracted', active_snapshot_id: 1 }));
    await flush();
    await flush();
    expect(knowledgeService.fetchKnowledgeSourceEvidence).toHaveBeenCalledTimes(before + 1);
    expect(container!.textContent).toContain('解析完成后的依据');
    expect(knowledgeService.fetchKnowledgeSourceContent).toHaveBeenCalledTimes(contentBefore + 1);
    const contentTab = [...container!.querySelectorAll('[role="tab"]')].find((tab) => tab.textContent?.includes('资料正文'));
    expect(contentTab).toBeTruthy();
    act(() => (contentTab as HTMLElement).click());
    await flush();
    expect(container!.textContent).toContain('解析完成后的正文');
  });
  it('keeps the empty knowledge list neutral', async () => {
    renderView();
    await flush();

    expect(container?.textContent).toContain('还没有资料来源');
    expect(container?.textContent).not.toContain('复盘沉淀');
  });

  it.each([null, undefined, {}])('shows malformed successful source collections as unavailable (%o)', async (sources) => {
    state.sources = sources;
    renderView();
    await flush();

    expect(container?.textContent).toContain('参考资料暂时无法读取');
    expect(container?.textContent).not.toContain('还没有资料来源');
  });

  it('does not render confirmed history even when the legacy query has data', async () => {
    state.confirmed = [{
      id: 1,
      title: '复盘片段',
      source_status: 'source_changed',
      content: { blocks: [{ block_id: 'b1', text: '回答', evidence_refs: [] }] },
      evidence: [],
    }];
    renderView();
    await flush();

    expect(container?.textContent).not.toContain('复盘片段');
    expect(container?.textContent).not.toContain('原资料已更新');
  });

  it('keeps captured source rows out of the external reference list', async () => {
    state.sources = [{
      id: 31,
      source_kind: 'captured_interview_note',
      title: '内部面试片段',
      display_title: '内部面试片段',
    }];
    renderView();
    await flush();

    expect(container?.textContent).not.toContain('内部面试片段');
    expect(container?.textContent).toContain('还没有资料来源');
  });

  it.each([
    ['error', () => state.search.mockRejectedValue(new Error('network'))],
    ['null', () => state.search.mockResolvedValue(null)],
  ] as const)('renders an explicit unavailable panel for a search %s response', async (_kind, configure) => {
    configure();
    renderView();
    await flush();
    submitSearch();
    await flush();

    expect(container?.textContent).toContain('搜索结果暂时不可用');
    expect(container?.textContent).not.toContain('未匹配资料内容');
  });

  it('does not present search hits as unmatched while the source list is still loading', async () => {
    state.sources = new Promise(() => undefined);
    state.search.mockResolvedValue({
      query: '延迟',
      hits: [{ evidence_id: 'e1', source_id: 3, snippet: '安全片段' }],
    });
    renderView();
    await flush();
    submitSearch();
    await flush();

    expect(container?.textContent).toContain('搜索结果暂时不可用');
    expect(container?.textContent).not.toContain('未匹配资料内容');
  });
});
