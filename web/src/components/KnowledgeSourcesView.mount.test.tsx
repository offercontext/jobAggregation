// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({
  confirmed: [] as unknown[],
  sources: [] as unknown,
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
  searchKnowledgeEvidence: vi.fn(),
  unarchiveKnowledgeSource: vi.fn(),
  updateKnowledgeSourceTitle: vi.fn(),
  uploadKnowledgeBundle: vi.fn(),
  uploadKnowledgeSource: vi.fn(),
}));

const { QueryClient, QueryClientProvider } = await import('@tanstack/react-query');
const { App: AntApp } = await import('antd');
const { default: KnowledgeSourcesView } = await import('./KnowledgeSourcesView');

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
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, 20));
  });
}

beforeEach(() => {
  state.confirmed = [];
  state.sources = [];
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
});
