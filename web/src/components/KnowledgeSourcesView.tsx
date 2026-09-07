import { Fragment, useEffect, useRef, useState, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import {
  Alert,
  Button,
  Empty,
  Input,
  List,
  Modal,
  Progress,
  Space,
  Spin,
  Switch,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  Upload,
  message,
} from 'antd';
import type { UploadFile } from 'antd';
import {
  DeleteOutlined,
  EditOutlined,
  FormOutlined,
  InboxOutlined,
  PictureOutlined,
  QuestionCircleOutlined,
  ReadOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import {
  archiveKnowledgeSource,
  buildKnowledgeAssetContentUrl,
  buildKnowledgeSourceContentUrl,
  cancelKnowledgeJob,
  deleteKnowledgeSource,
  fetchKnowledgeSource,
  fetchKnowledgeSourceBrief,
  fetchKnowledgeSourceContent,
  fetchKnowledgeSourceEvidence,
  fetchKnowledgeSourceJobs,
  fetchKnowledgeSources,
  pasteKnowledgeSource,
  rebuildKnowledgeSourceBrief,
  searchKnowledgeEvidence,
  unarchiveKnowledgeSource,
  updateKnowledgeSourceTitle,
  uploadKnowledgeBundle,
  uploadKnowledgeSource,
} from '@/services/knowledge';
import type {
  BriefStatement,
  BriefValidationReport,
  KnowledgeBriefAttempt,
  KnowledgeBriefAttemptStep,
  KnowledgeEvidence,
  KnowledgeJob,
  KnowledgeSource,
  KnowledgeSourceBrief,
  KnowledgeSourceBriefResponse,
  KnowledgeSourceJobsResponse,
} from '@/types/knowledge';
import {
  projectExternalReferences,
} from '@/features/materialSurfaces/materialClassification';

const { Paragraph, Text, Title } = Typography;

const KNOWLEDGE_QUERY_KEY = ['knowledge', 'sources'] as const;

// KV1-02 / ADR-0002：V1 工作台不展示 Brief UI（列表 / 详情头 Brief Pill、Brief 块、
// 重建入口、Attempt timeline、Brief 暂缓原因与错误文案）。Brief 组件、query、mutation
// 与 handler 全部保留，V1.1 恢复 Brief 展示时把开关改回 true 即可，不重新引入自动 Brief。
const SHOW_BRIEF_UI = false;

const STATUS_LABEL: Record<string, string> = {
  active: '活跃',
  archived: '已归档',
  deleting: '删除中',
};
const EXTRACTION_LABEL: Record<string, string> = {
  pending: '等待解析',
  processing: '解析中',
  extracted: '已解析',
  failed: '解析失败',
};
const BRIEF_LABEL: Record<string, string> = {
  not_started: '尚未生成',
  pending: '排队中',
  processing: '生成中',
  ready: '已生成',
  failed: '生成失败',
  outdated: '已过期',
};

const SOURCE_LIFECYCLES = new Set(['active', 'archived', 'deleting']);
const EXTRACTION_STATUSES = new Set(['pending', 'processing', 'extracted', 'failed']);
const BRIEF_STATUSES = new Set([
  'not_started',
  'pending',
  'processing',
  'ready',
  'failed',
  'outdated',
]);

const SAFE_PROCESSING_FAILURE_COPY = '资料处理未完成，请稍后重试。';
const MAX_TITLE_LENGTH = 160;
const MAX_SUMMARY_LENGTH = 1200;
const MAX_DETAIL_LENGTH = 20_000;
const MAX_COLLECTION_LENGTH = 200;

type SafeRecord = Record<string, unknown>;

function safeRecord(value: unknown): SafeRecord | null {
  try {
    if (typeof value !== 'object' || value === null || Array.isArray(value)) return null;
    Reflect.ownKeys(value);
    return value as SafeRecord;
  } catch {
    return null;
  }
}

function safeRead(value: unknown, key: string): unknown {
  const record = safeRecord(value);
  if (!record) return undefined;
  try {
    return Reflect.get(record, key);
  } catch {
    return undefined;
  }
}

function safeHasOwn(value: unknown, key: string): boolean {
  const record = safeRecord(value);
  if (!record) return false;
  try {
    return Object.prototype.hasOwnProperty.call(record, key);
  } catch {
    // A hostile own-property trap is itself an invalid source envelope.
    return true;
  }
}

function safeArray(value: unknown, limit = MAX_COLLECTION_LENGTH): unknown[] {
  try {
    if (!Array.isArray(value)) return [];
    const length = Math.min(value.length, limit);
    const result: unknown[] = [];
    for (let index = 0; index < length; index += 1) result.push(value[index]);
    return result;
  } catch {
    return [];
  }
}

function isSafeArray(value: unknown): boolean {
  try {
    return Array.isArray(value);
  } catch {
    return false;
  }
}

function safeArrayProjection(
  value: unknown,
  limit = MAX_COLLECTION_LENGTH,
): { readonly values: readonly unknown[]; readonly unavailable: boolean } {
  try {
    if (!Array.isArray(value) || !Number.isSafeInteger(value.length) || value.length > limit) {
      return { values: Object.freeze([]), unavailable: true };
    }
    const values: unknown[] = [];
    let unavailable = false;
    for (let index = 0; index < value.length; index += 1) {
      if (!(index in value)) {
        unavailable = true;
        continue;
      }
      try {
        values.push(value[index]);
      } catch {
        unavailable = true;
      }
    }
    return { values: Object.freeze(values), unavailable };
  } catch {
    return { values: Object.freeze([]), unavailable: true };
  }
}

function normalizeHeadingPath(record: SafeRecord): { readonly value: readonly string[]; readonly valid: boolean } {
  if (!safeHasOwn(record, 'heading_path')) return { value: Object.freeze([]), valid: true };
  const source = safeArrayProjection(safeRead(record, 'heading_path'), 12);
  if (source.unavailable) return { value: Object.freeze([]), valid: false };
  const value: string[] = [];
  for (const candidate of source.values) {
    const heading = boundedText(candidate, '', 120);
    if (!heading) return { value: Object.freeze([]), valid: false };
    value.push(heading);
  }
  return { value: Object.freeze(value), valid: true };
}

function boundedText(value: unknown, fallback = '', limit = MAX_SUMMARY_LENGTH): string {
  if (typeof value !== 'string') return fallback;
  const normalized = value.trim();
  if (!normalized) return fallback;
  return normalized.length <= limit ? normalized : `${normalized.slice(0, limit)}…`;
}

function safePositiveId(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0 ? value : null;
}

function safeFiniteNumber(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

interface ExternalSourceListItem {
  readonly id: number;
  readonly title: string;
  readonly lifecycle: string;
  readonly extractionStatus: string;
  readonly briefStatus: string;
  readonly mainFilename: string;
  readonly totalBytes: number;
}

function externalSourceListItem(
  item: ReturnType<typeof projectExternalReferences>['items'][number],
): ExternalSourceListItem | null {
  const id = safePositiveId(safeRead(item.record, 'id'));
  if (id === null) return null;
  return Object.freeze({
    id,
    title: boundedText(item.title, '参考资料', MAX_TITLE_LENGTH),
    lifecycle: boundedText(safeRead(item.record, 'lifecycle'), 'unknown', 32),
    extractionStatus: boundedText(safeRead(item.record, 'extraction_status'), 'unknown', 32),
    briefStatus: boundedText(safeRead(item.record, 'brief_status'), 'unknown', 32),
    mainFilename: boundedText(safeRead(item.record, 'main_filename'), '未提供文件名', MAX_TITLE_LENGTH),
    totalBytes: Math.max(0, safeFiniteNumber(safeRead(item.record, 'total_bytes'))),
  });
}

export function normalizeKnowledgeSourceDetail(input: unknown, expectedId: number): KnowledgeSource | null {
  const record = safeRecord(input);
  if (!record || safePositiveId(safeRead(record, 'id')) !== expectedId) return null;
  const sourceKind = boundedText(safeRead(record, 'source_kind'), '', 48);
  if (!['markdown', 'text', 'bundle'].includes(sourceKind)) return null;
  const lifecycle = boundedText(safeRead(record, 'lifecycle'), '', 32);
  const extractionStatus = boundedText(safeRead(record, 'extraction_status'), '', 32);
  const briefStatus = boundedText(safeRead(record, 'brief_status'), '', 32);
  if (
    !SOURCE_LIFECYCLES.has(lifecycle)
    || !EXTRACTION_STATUSES.has(extractionStatus)
    || !BRIEF_STATUSES.has(briefStatus)
  ) {
    return null;
  }
  const provenanceValue = safeRecord(safeRead(record, 'provenance'));
  const title = boundedText(
    safeRead(record, 'display_title'),
    boundedText(safeRead(record, 'title'), '参考资料', MAX_TITLE_LENGTH),
    MAX_TITLE_LENGTH,
  );
  return Object.freeze({
    id: expectedId,
    source_kind: sourceKind,
    title,
    display_title: title,
    title_hint: boundedText(safeRead(record, 'title_hint'), '', MAX_TITLE_LENGTH),
    author: boundedText(safeRead(record, 'author'), '', MAX_TITLE_LENGTH),
    published_at: boundedText(safeRead(record, 'published_at'), '', 64) || null,
    main_filename: boundedText(safeRead(record, 'main_filename'), '未提供文件名', MAX_TITLE_LENGTH),
    main_media_type: boundedText(safeRead(record, 'main_media_type'), '', 80),
    total_bytes: Math.max(0, safeFiniteNumber(safeRead(record, 'total_bytes'))),
    token_count: Math.max(0, safeFiniteNumber(safeRead(record, 'token_count'))),
    lifecycle,
    extraction_status: extractionStatus,
    extraction_error_code: '',
    extraction_error_message: '',
    brief_status: briefStatus,
    brief_block_reason: '',
    brief_error_code: '',
    brief_error_message: '',
    active_snapshot_id: null,
    archived_at: boundedText(safeRead(record, 'archived_at'), '', 64) || null,
    created_at: boundedText(safeRead(record, 'created_at'), '', 64),
    updated_at: boundedText(safeRead(record, 'updated_at'), '', 64),
    provenance: Object.freeze({
      title: boundedText(safeRead(provenanceValue, 'title'), '', MAX_TITLE_LENGTH) || undefined,
      author: boundedText(safeRead(provenanceValue, 'author'), '', MAX_TITLE_LENGTH) || undefined,
      url: boundedText(safeRead(provenanceValue, 'url'), '', 500) || undefined,
      published_at: boundedText(safeRead(provenanceValue, 'published_at'), '', 64) || undefined,
      captured_at: boundedText(safeRead(provenanceValue, 'captured_at'), '', 64),
      metadata_extraction_version: '',
    }),
  });
}

export function knowledgeSourceCanMutate(source: KnowledgeSource | null): boolean {
  return source?.lifecycle === 'active' || source?.lifecycle === 'archived';
}

interface SafeEvidenceProjection {
  readonly items: KnowledgeEvidence[];
  readonly unavailable: boolean;
}

export function normalizeEvidencePage(input: unknown, expectedSourceId: number): SafeEvidenceProjection {
  const rawItems = safeRead(input, 'items');
  let unavailable = input !== undefined && !isSafeArray(rawItems);
  const items: KnowledgeEvidence[] = [];
  for (const candidate of safeArray(rawItems, 100)) {
    const record = safeRecord(candidate);
    const id = boundedText(safeRead(record, 'id'), '', 180);
    const sourceId = safePositiveId(safeRead(record, 'source_id'));
    const excerpt = boundedText(safeRead(record, 'canonical_excerpt'), '', 2400);
    if (!record || !id || sourceId !== expectedSourceId || !excerpt) {
      unavailable = true;
      continue;
    }
    const headingPath = normalizeHeadingPath(record);
    if (!headingPath.valid) {
      unavailable = true;
      continue;
    }
    items.push(Object.freeze({
      id,
      source_id: sourceId,
      snapshot_id: 0,
      kind: safeRead(record, 'kind') === 'asset' ? 'asset' : 'text',
      block_kind: '资料片段',
      ordinal: Math.max(0, safeFiniteNumber(safeRead(record, 'ordinal'))),
      heading_path: headingPath.value as string[],
      char_start: 0,
      char_end: 0,
      line_start: 0,
      line_end: 0,
      canonical_excerpt: excerpt,
      search_text: boundedText(safeRead(record, 'search_text'), '', 500),
      content_hash: '',
      asset_id: safePositiveId(safeRead(record, 'asset_id')),
      previous_evidence_id: null,
      next_evidence_id: null,
    }));
  }
  return { items, unavailable };
}

function normalizeOriginProjection(
  input: unknown,
  expectedSourceId: number,
): { readonly items: KnowledgeSourceJobsResponse['origins']; readonly unavailable: boolean } {
  const source = safeArrayProjection(input, 100);
  let unavailable = source.unavailable;
  const items = source.values.flatMap((candidate) => {
    const record = safeRecord(candidate);
    const id = safePositiveId(safeRead(record, 'id'));
    const sourceId = safePositiveId(safeRead(record, 'source_id'));
    if (!record || id === null || sourceId !== expectedSourceId) {
      unavailable = true;
      return [];
    }
    return [Object.freeze({
      id,
      source_id: sourceId,
      import_method: boundedText(safeRead(record, 'import_method'), '导入', 40),
      original_filename: boundedText(safeRead(record, 'original_filename'), '未提供文件名', MAX_TITLE_LENGTH),
      origin_url: boundedText(safeRead(record, 'origin_url'), '', 500),
      imported_at: boundedText(safeRead(record, 'imported_at'), '', 64),
    })];
  });
  return { items, unavailable };
}

export function normalizeJobProjection(
  input: unknown,
  expectedSourceId?: number,
): { readonly items: readonly KnowledgeJob[]; readonly unavailable: boolean } {
  const source = safeArrayProjection(input, 100);
  let unavailable = source.unavailable;
  const blockedIds = new Set<number>();
  const byId = new Map<number, KnowledgeJob>();
  for (const candidate of source.values) {
    const record = safeRecord(candidate);
    const id = safePositiveId(safeRead(record, 'id'));
    const kind = boundedText(safeRead(record, 'kind'), '', 32);
    const status = boundedText(safeRead(record, 'status'), 'unknown', 32);
    const owner = safePositiveId(safeRead(record, 'source_id'));
    if (!record || id === null || !['extract', 'brief', 'delete', 'process'].includes(kind)
      || !['pending', 'running', 'succeeded', 'failed', 'canceled'].includes(status)
      || (expectedSourceId !== undefined && owner !== expectedSourceId)) {
      unavailable = true;
      continue;
    }
    if (blockedIds.has(id) || byId.has(id)) {
      unavailable = true;
      blockedIds.add(id);
      byId.delete(id);
      continue;
    }
    byId.set(id, Object.freeze({
      id,
      kind: kind === 'delete' ? 'delete' : 'process',
      queue: '',
      source_id: null,
      snapshot_id: null,
      stage: '',
      status,
      progress: Math.max(0, Math.min(100, safeFiniteNumber(safeRead(record, 'progress')))),
      retry_count: Math.max(0, safeFiniteNumber(safeRead(record, 'retry_count'))),
      next_retry_at: boundedText(safeRead(record, 'next_retry_at'), '', 64) || null,
      error_code: '',
      error_message: safeRead(record, 'error_message') ? SAFE_PROCESSING_FAILURE_COPY : '',
      canceled: safeRead(record, 'canceled') === true,
      lease_owner: '',
      lease_expires_at: null,
      heartbeat_at: null,
      created_at: boundedText(safeRead(record, 'created_at'), '', 64),
      updated_at: boundedText(safeRead(record, 'updated_at'), '', 64),
    }));
  }
  return { items: Object.freeze([...byId.values()]), unavailable };
}

export function normalizeSearchHitProjection(input: unknown): {
  readonly items: readonly import('@/types/knowledge').KnowledgeEvidenceSearchHit[];
  readonly unavailable: boolean;
} {
  const source = safeArrayProjection(input, 50);
  let unavailable = source.unavailable;
  const blockedIds = new Set<string>();
  const byId = new Map<string, import('@/types/knowledge').KnowledgeEvidenceSearchHit>();
  for (const candidate of source.values) {
    const record = safeRecord(candidate);
    const evidenceId = boundedText(safeRead(record, 'evidence_id'), '', 180);
    const sourceId = safePositiveId(safeRead(record, 'source_id'));
    const snippet = boundedText(safeRead(record, 'snippet'), '', 800);
    if (!record || !evidenceId || sourceId === null || !snippet) {
      unavailable = true;
      continue;
    }
    if (blockedIds.has(evidenceId) || byId.has(evidenceId)) {
      unavailable = true;
      blockedIds.add(evidenceId);
      byId.delete(evidenceId);
      continue;
    }
    const headingPath = normalizeHeadingPath(record);
    if (!headingPath.valid) {
      unavailable = true;
      continue;
    }
    byId.set(evidenceId, Object.freeze({
      evidence_id: evidenceId,
      source_id: sourceId,
      snapshot_id: 0,
      block_kind: '资料片段',
      heading_path: headingPath.value as string[],
      char_start: 0,
      char_end: 0,
      line_start: 0,
      line_end: 0,
      canonical_excerpt: '',
      snippet,
      score: safeFiniteNumber(safeRead(record, 'score')),
      previous_evidence_id: null,
      next_evidence_id: null,
    }));
  }
  return { items: Object.freeze([...byId.values()]), unavailable };
}

export function normalizeSearchHits(input: unknown): import('@/types/knowledge').KnowledgeEvidenceSearchHit[] {
  return [...normalizeSearchHitProjection(input).items];
}

// Status Pill 变体：替换 antd 彩色圆点 Badge，统一精致化状态标识
type PillVariant = 'indigo' | 'green' | 'amber' | 'rose' | 'gray' | 'violet' | 'cyan';

function Pill({
  variant,
  children,
  className,
}: {
  variant: PillVariant;
  children: ReactNode;
  className?: string;
}) {
  return (
    <span className={`op-pill op-pill--${variant}${className ? ` ${className}` : ''}`}>
      <span className="op-pill-dot" />
      {children}
    </span>
  );
}

function lifecycleVariant(status: string): PillVariant {
  switch (status) {
    case 'active':
      return 'green';
    case 'archived':
      return 'gray';
    case 'deleting':
      return 'rose';
    default:
      return 'gray';
  }
}

function extractionVariant(status: string): PillVariant {
  switch (status) {
    case 'extracted':
      return 'indigo';
    case 'processing':
      return 'amber';
    case 'failed':
      return 'rose';
    default:
      return 'gray';
  }
}

function briefVariant(status: string): PillVariant {
  switch (status) {
    case 'ready':
      return 'violet';
    case 'processing':
    case 'pending':
    case 'outdated':
      return 'amber';
    case 'failed':
      return 'rose';
    default:
      return 'gray';
  }
}

function jobStatusVariant(status: string): PillVariant {
  switch (status) {
    case 'succeeded':
      return 'green';
    case 'running':
      return 'indigo';
    case 'pending':
      return 'amber';
    case 'failed':
      return 'rose';
    default:
      return 'gray';
  }
}

export default function KnowledgeSourcesView() {
  const queryClient = useQueryClient();
  const [includeArchived, setIncludeArchived] = useState(false);
  const sourcesQuery = useQuery({
    queryKey: [...KNOWLEDGE_QUERY_KEY, { includeArchived }],
    queryFn: () => fetchKnowledgeSources({ includeArchived }),
    // KV1-02：列表轮询只由 Extraction 在途状态触发；V1 不因 Brief 状态刷新
    // （Brief Pill 已隐藏，brief_status 变化不再影响列表展示）。
    refetchInterval: (query) => {
      const items = safeArray(query.state.data);
      return items.some((item) => {
        const status = safeRead(item, 'extraction_status');
        return status === 'pending' || status === 'processing';
      })
        ? 2000
        : false;
    },
  });
  const externalProjection = projectExternalReferences({
    sources: sourcesQuery.data,
    sourcesState: sourcesQuery.isLoading
      ? 'loading'
      : sourcesQuery.isError
        ? 'error'
        : 'ready',
  });
  const externalSources = externalProjection.items.flatMap((item) => {
    const projected = externalSourceListItem(item);
    return projected ? [projected] : [];
  });
  const externalSourceUnavailable = externalProjection.unavailable.some(
    (issue) => issue.kind !== 'captured_unavailable',
  ) || externalProjection.items.some((item) => item.sourceState === 'unavailable');
  const externalSourceListNotReady = externalProjection.state === 'loading'
    || externalProjection.state === 'error'
    || externalProjection.state === 'partial'
    || externalProjection.state === 'unavailable';
  const externalSourceIds = new Set(externalSources.map((source) => source.id));
  const [selectedSourceId, setSelectedSourceId] = useState<number | null>(null);
  const activeExternalSourceId = selectedSourceId !== null && externalSourceIds.has(selectedSourceId)
    ? selectedSourceId
    : null;
  // KI-08：搜索结果点击后，进入 Source 详情时定位/高亮对应 Evidence。
  // 保留 evidenceId + 命中片段用于详情面板滚动与高亮；定位完成后清空。
  const [highlightEvidenceId, setHighlightEvidenceId] = useState<string | null>(null);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [bundleOpen, setBundleOpen] = useState(false);
  const [pasteOpen, setPasteOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [activeSearch, setActiveSearch] = useState('');

  const uploadMutation = useMutation({
    mutationFn: ({ file, titleHint }: { file: File; titleHint: string }) =>
      uploadKnowledgeSource(file, titleHint),
    onSuccess: (data) => {
      if (data.deduplicated) {
        message.success('资料已导入，已进入已有资料来源');
      } else {
        message.success('资料已导入');
      }
      queryClient.invalidateQueries({ queryKey: KNOWLEDGE_QUERY_KEY });
      setSelectedSourceId(data.source.id);
      setUploadOpen(false);
    },
    onError: (error: unknown) => {
      const detail = extractErrorMessage(error);
      message.error(`上传失败：${detail}`);
    },
  });

  const bundleMutation = useMutation({
    mutationFn: ({
      main,
      assets,
      titleHint,
    }: {
      main: File;
      assets: File[];
      titleHint: string;
    }) => uploadKnowledgeBundle(main, assets, titleHint),
    onSuccess: (data) => {
      if (data.deduplicated) {
        message.success('资料已导入，已进入已有资料来源');
      } else {
        message.success('图文资料已导入，图片以附件形式保留');
      }
      queryClient.invalidateQueries({ queryKey: KNOWLEDGE_QUERY_KEY });
      setSelectedSourceId(data.source.id);
      setBundleOpen(false);
    },
    onError: (error: unknown) => {
      const detail = extractErrorMessage(error);
      message.error(`图文资料上传失败：${detail}`);
    },
  });

  const pasteMutation = useMutation({
    mutationFn: ({
      paste,
      titleHint,
      originUrl,
    }: {
      paste: string;
      titleHint: string;
      originUrl: string;
    }) => pasteKnowledgeSource(paste, { titleHint, originUrl }),
    onSuccess: (data) => {
      if (data.deduplicated) {
        message.success('资料已导入，已进入已有资料来源');
      } else {
        message.success('正文已导入');
      }
      queryClient.invalidateQueries({ queryKey: KNOWLEDGE_QUERY_KEY });
      setSelectedSourceId(data.source.id);
      setPasteOpen(false);
    },
    onError: (error: unknown) => {
      const detail = extractErrorMessage(error);
      message.error(`粘贴失败：${detail}`);
    },
  });

  const searchMutation = useMutation({
    mutationFn: (query: string) => searchKnowledgeEvidence(query, { limit: 20 }),
    onSuccess: (data) => {
      setActiveSearch(boundedText(safeRead(data, 'query'), searchQuery, 120));
    },
  });

  const searchProjection = normalizeSearchHitProjection(safeRead(searchMutation.data, 'hits'));
  const searchHits = [...searchProjection.items];
  const searchResponseUnavailable = externalSourceListNotReady
    || searchMutation.isError
    || (searchMutation.data !== undefined && searchProjection.unavailable);

  const handleSearch = () => {
    if (!searchQuery.trim()) {
      message.warning('请输入搜索关键词');
      return;
    }
    searchMutation.mutate(searchQuery);
  };

  return (
    <div style={{ padding: 24 }}>
      <div className="knowledge-page-header">
        <div className="knowledge-page-header-row">
          <span className="knowledge-page-mark" />
          <Title level={3} className="knowledge-page-title">
            素材库
          </Title>
          <span className="knowledge-page-count">
            共 <b>{externalSources.length}</b> 个来源
          </span>
        </div>
        <Paragraph type="secondary" className="knowledge-page-subtitle">
          上传 Markdown/Text、上传图文资料，或直接粘贴正文；系统按自然结构生成来源依据，并提供关键词检索。
        </Paragraph>
      </div>

      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <div className="knowledge-sources-toolbar">
          <Button
            type="primary"
            className="op-ai-btn"
            icon={<InboxOutlined />}
            onClick={() => setUploadOpen(true)}
          >
            上传 Markdown / Text
          </Button>
          <Button icon={<PictureOutlined />} onClick={() => setBundleOpen(true)}>
            上传图文资料
          </Button>
          <Button icon={<FormOutlined />} onClick={() => setPasteOpen(true)}>
            粘贴正文
          </Button>
          <span className="knowledge-toolbar-spacer" />
          <Input
            placeholder="搜索资料内容（中文/英文关键词）"
            className="knowledge-sources-search-input"
            style={{ width: 'min(320px, 100%)' }}
            value={searchQuery}
            onChange={(event) => setSearchQuery(event.target.value)}
            onPressEnter={handleSearch}
            prefix={<SearchOutlined />}
          />
          <Button onClick={handleSearch} loading={searchMutation.isPending}>
            搜索
          </Button>
          <Space size={6} align="center">
            <Switch
              checked={includeArchived}
              onChange={(checked) => setIncludeArchived(checked)}
              aria-label="显示归档资料"
            />
            <Text type="secondary">显示归档资料</Text>
          </Space>
        </div>

        {searchMutation.isError || searchMutation.data !== undefined ? (
          <SearchResultsPanel
            query={activeSearch}
            hits={searchHits.filter((hit) => externalSourceIds.has(hit.source_id))}
            unavailable={searchResponseUnavailable}
            onPick={(sourceId, evidenceId) => {
              // Search can outlive the detail cache populated during extraction.
              void queryClient.invalidateQueries({ queryKey: ['knowledge', 'source', sourceId] });
              setSelectedSourceId(sourceId);
              setHighlightEvidenceId(evidenceId);
            }}
          />
        ) : null}

        <div
          className="knowledge-sources-layout"
          style={{
            display: 'grid',
            gridTemplateColumns: 'minmax(240px, 320px) minmax(0, 1fr)',
            gap: 16,
            alignItems: 'start',
          }}
        >
          <SourceListPanel
            sources={externalSources}
            loading={externalProjection.state === 'loading'}
            error={externalProjection.state === 'error'}
            unavailable={externalSourceUnavailable}
            selectedId={activeExternalSourceId}
            onSelect={(id) => {
              setSelectedSourceId(id);
              setHighlightEvidenceId(null);
            }}
          />
          <SourceDetailPanel
            sourceId={activeExternalSourceId}
            highlightEvidenceId={highlightEvidenceId}
            onHighlightConsumed={() => setHighlightEvidenceId(null)}
            onDeleted={() => setSelectedSourceId(null)}
          />
        </div>
      </Space>

      <UploadModal
        open={uploadOpen}
        onClose={() => setUploadOpen(false)}
        uploading={uploadMutation.isPending}
        onSubmit={(file, titleHint) => uploadMutation.mutate({ file, titleHint })}
      />
      <BundleModal
        open={bundleOpen}
        onClose={() => setBundleOpen(false)}
        uploading={bundleMutation.isPending}
        onSubmit={(main, assets, titleHint) =>
          bundleMutation.mutate({ main, assets, titleHint })
        }
      />
      <PasteModal
        open={pasteOpen}
        onClose={() => setPasteOpen(false)}
        submitting={pasteMutation.isPending}
        onSubmit={(paste, titleHint, originUrl) =>
          pasteMutation.mutate({ paste, titleHint, originUrl })
        }
      />
    </div>
  );
}

function SourceListPanel({
  sources,
  loading,
  error,
  unavailable,
  selectedId,
  onSelect,
}: {
  sources: ExternalSourceListItem[];
  loading: boolean;
  error: boolean;
  unavailable: boolean;
  selectedId: number | null;
  onSelect: (id: number) => void;
}) {
  if (loading) {
    return (
      <div style={{ padding: 24 }}>
        <Spin />
      </div>
    );
  }
  if (error) {
    return (
      <div role="alert" style={{ border: '1px solid var(--op-border, #eee)', padding: 24, borderRadius: 8 }}>
        参考资料暂时无法读取，请稍后重试。
      </div>
    );
  }
  if (!sources.length) {
    if (unavailable) {
      return (
        <div role="alert" style={{ border: '1px solid var(--op-border, #eee)', padding: 24, borderRadius: 8 }}>
          参考资料暂时无法读取，请稍后重试。
        </div>
      );
    }
    return (
      <div style={{ border: '1px solid var(--op-border, #eee)', padding: 24, borderRadius: 8 }}>
        <Empty description="还没有资料来源" />
      </div>
    );
  }
  return (
    <div className="knowledge-source-list">
      {unavailable ? <p role="status">部分资料来源暂时不可用，已展示可用内容。</p> : null}
      <div className="knowledge-source-list-head">
        <span>资料列表</span>
        <span>{sources.length}</span>
      </div>
      <List
        dataSource={sources}
        rowKey={(item) => item.id}
        split={false}
        renderItem={(item) => (
          <List.Item key={item.id}>
            <div
              className={`knowledge-source-item${selectedId === item.id ? ' is-selected' : ''}`}
              onClick={() => onSelect(item.id)}
            >
              <div className="knowledge-source-item-body">
                <Text className="knowledge-source-item-title">{item.title}</Text>
                <div className="knowledge-pill-row">
                  <Pill variant={lifecycleVariant(item.lifecycle)}>
                    {STATUS_LABEL[item.lifecycle] ?? '状态待确认'}
                  </Pill>
                  <Pill variant={extractionVariant(item.extractionStatus)}>
                    {EXTRACTION_LABEL[item.extractionStatus] ?? '状态待确认'}
                  </Pill>
                  {SHOW_BRIEF_UI ? (
                    <Pill variant={briefVariant(item.briefStatus)}>
                      {BRIEF_LABEL[item.briefStatus] ?? '状态待确认'}
                    </Pill>
                  ) : null}
                </div>
                <Text className="knowledge-source-item-meta">
                  {item.mainFilename} · {formatBytes(item.totalBytes)}
                </Text>
              </div>
            </div>
          </List.Item>
        )}
      />
    </div>
  );
}

function SourceDetailPanel({
  sourceId,
  highlightEvidenceId,
  onHighlightConsumed,
  onDeleted,
}: {
  sourceId: number | null;
  highlightEvidenceId: string | null;
  onHighlightConsumed: () => void;
  onDeleted: () => void;
}) {
  if (sourceId == null) {
    return (
      <div style={{ border: '1px solid var(--op-border, #eee)', padding: 24, borderRadius: 8 }}>
        <Empty description="选择左侧的资料来源查看详情" />
      </div>
    );
  }
  return (
    <SourceDetailContent
      sourceId={sourceId}
      highlightEvidenceId={highlightEvidenceId}
      onHighlightConsumed={onHighlightConsumed}
      onDeleted={onDeleted}
    />
  );
}

function SourceDetailContent({
  sourceId,
  highlightEvidenceId,
  onHighlightConsumed,
  onDeleted,
}: {
  sourceId: number;
  highlightEvidenceId: string | null;
  onHighlightConsumed: () => void;
  onDeleted: () => void;
}) {
  const queryClient = useQueryClient();
  const sourceQuery = useQuery({
    queryKey: ['knowledge', 'source', sourceId],
    queryFn: () => fetchKnowledgeSource(sourceId),
    // KV1-02：轮询只由 Extraction 在途状态触发；V1 不因 Brief 状态持续刷新。
    refetchInterval: (query) => {
      const status = safeRead(query.state.data, 'extraction_status');
      return status === 'pending' || status === 'processing'
        ? 2000
        : false;
    },
  });
  const evidenceQuery = useQuery({
    queryKey: ['knowledge', 'source', sourceId, 'evidence'],
    queryFn: () => fetchKnowledgeSourceEvidence(sourceId, { limit: 50 }),
  });
  const contentQuery = useQuery({
    queryKey: ['knowledge', 'source', sourceId, 'content'],
    queryFn: () => fetchKnowledgeSourceContent(sourceId),
  });
  const extractionStatus = safeRead(sourceQuery.data, 'extraction_status');
  const snapshotId = safeRead(sourceQuery.data, 'active_snapshot_id');
  const previousExtraction = useRef<{ sourceId: number; status: unknown; snapshotId: unknown } | null>(null);
  useEffect(() => {
    if (!sourceQuery.data) return;
    const previous = previousExtraction.current;
    previousExtraction.current = { sourceId, status: extractionStatus, snapshotId };
    if (previous?.sourceId === sourceId && extractionStatus === 'extracted'
      && (previous.status !== extractionStatus || previous.snapshotId !== snapshotId)) {
      void queryClient.invalidateQueries({ queryKey: ['knowledge', 'source', sourceId, 'evidence'] });
      void queryClient.invalidateQueries({ queryKey: ['knowledge', 'source', sourceId, 'content'] });
    }
  }, [sourceId, extractionStatus, snapshotId, sourceQuery.data, queryClient]);
  // KV1-02：V1 不展示 Brief UI，briefQuery 在 V1 不启用（不发请求、不轮询）；
  // SHOW_BRIEF_UI 恢复 true 时自动启用 fetch 与 brief_status 轮询。
  const briefQuery = useQuery({
    queryKey: ['knowledge', 'source', sourceId, 'brief'],
    queryFn: () => fetchKnowledgeSourceBrief(sourceId),
    enabled: SHOW_BRIEF_UI,
    refetchInterval: (query) => {
      const status = query.state.data?.brief_status;
      return status === 'pending' || status === 'processing' ? 2000 : false;
    },
  });
  const jobsQuery = useQuery({
    queryKey: ['knowledge', 'source', sourceId, 'jobs'],
    queryFn: () => fetchKnowledgeSourceJobs(sourceId),
    refetchInterval: (query) => {
      const jobs = normalizeJobProjection(safeRead(query.state.data, 'jobs'), sourceId);
      return jobs.items.some((job) => job.status === 'pending' || job.status === 'running')
        ? 2000
        : false;
    },
  });
  const briefRebuildMutation = useMutation({
    mutationFn: (id: number) => rebuildKnowledgeSourceBrief(id),
    onSuccess: (data) => {
      message.success('已请求重新生成资料导读');
      // 立即用 202 响应刷新 source / brief 缓存，避免等下一轮 refetch 才看到「排队中」。
      queryClient.setQueryData<KnowledgeSource>(
        ['knowledge', 'source', sourceId],
        (prev) =>
          prev
            ? {
                ...prev,
                brief_status: data.brief_status,
                brief_block_reason: data.brief_block_reason,
                brief_error_code: data.brief_error_code,
                brief_error_message: data.brief_error_message,
              }
            : prev,
      );
      queryClient.setQueryData(
        ['knowledge', 'source', sourceId, 'brief'],
        (prev: KnowledgeSourceBriefResponse | undefined) =>
          prev
            ? {
                ...prev,
                brief_status: data.brief_status,
                brief_block_reason: data.brief_block_reason,
                brief_error_code: data.brief_error_code,
                brief_error_message: data.brief_error_message,
              }
            : prev,
      );
      queryClient.setQueryData<KnowledgeSource[]>(KNOWLEDGE_QUERY_KEY, (prev) =>
        prev?.map((item) =>
          item.id === sourceId
            ? {
                ...item,
                brief_status: data.brief_status,
                brief_block_reason: data.brief_block_reason,
                brief_error_code: data.brief_error_code,
                brief_error_message: data.brief_error_message,
              }
            : item,
        ),
      );
      queryClient.invalidateQueries({ queryKey: KNOWLEDGE_QUERY_KEY });
      queryClient.invalidateQueries({ queryKey: ['knowledge', 'source', sourceId] });
      queryClient.invalidateQueries({
        queryKey: ['knowledge', 'source', sourceId, 'brief'],
      });
      queryClient.invalidateQueries({
        queryKey: ['knowledge', 'source', sourceId, 'jobs'],
      });
    },
    onError: (error: unknown) => {
      const detail = extractErrorMessage(error);
      message.error(`资料导读重建失败：${detail}`);
    },
  });
  const [briefCitationTarget, setBriefCitationTarget] = useState<string | null>(
    null,
  );
  // KV1-02：V1 默认进入 Evidence 视图，而非暴露 Brief 缺失的状态总览。
  const [activeDetailTab, setActiveDetailTab] = useState('evidence');
  const handleCitationJump = (evidenceId: string) => {
    setBriefCitationTarget(evidenceId);
    setActiveDetailTab('evidence');
  };
  useEffect(() => {
    if (highlightEvidenceId) {
      setActiveDetailTab('evidence');
    }
  }, [highlightEvidenceId]);
  const [titleEditorOpen, setTitleEditorOpen] = useState(false);
  const [editingTitle, setEditingTitle] = useState('');
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  const [deleteConfirmationText, setDeleteConfirmationText] = useState('');
  const titleMutation = useMutation({
    mutationFn: ({ id, title }: { id: number; title: string }) =>
      updateKnowledgeSourceTitle(id, title),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: KNOWLEDGE_QUERY_KEY });
      queryClient.invalidateQueries({ queryKey: ['knowledge', 'source', sourceId] });
      message.success('展示标题已更新');
      setTitleEditorOpen(false);
    },
    onError: (error: unknown) => {
      const detail = extractErrorMessage(error);
      message.error(`标题更新失败：${detail}`);
    },
  });
  const archiveMutation = useMutation({
    mutationFn: (id: number) => archiveKnowledgeSource(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: KNOWLEDGE_QUERY_KEY });
      queryClient.invalidateQueries({ queryKey: ['knowledge', 'source', sourceId] });
      message.success('资料已归档');
    },
    onError: (error: unknown) => {
      const detail = extractErrorMessage(error);
      message.error(`归档失败：${detail}`);
    },
  });
  const unarchiveMutation = useMutation({
    mutationFn: (id: number) => unarchiveKnowledgeSource(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: KNOWLEDGE_QUERY_KEY });
      queryClient.invalidateQueries({ queryKey: ['knowledge', 'source', sourceId] });
      message.success('资料已取消归档');
    },
    onError: (error: unknown) => {
      const detail = extractErrorMessage(error);
      message.error(`取消归档失败：${detail}`);
    },
  });
  const deleteMutation = useMutation({
    mutationFn: (id: number) => deleteKnowledgeSource(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: KNOWLEDGE_QUERY_KEY });
      message.success('已提交永久删除，后台任务完成后资料将被移除');
      setDeleteConfirmOpen(false);
      setDeleteConfirmationText('');
      onDeleted();
    },
    onError: (error: unknown) => {
      const detail = extractErrorMessage(error);
      message.error(`删除失败：${detail}`);
    },
  });
  if (sourceQuery.isLoading) {
    return (
      <div style={{ padding: 24 }}>
        <Spin />
      </div>
    );
  }
  const source = normalizeKnowledgeSourceDetail(sourceQuery.data, sourceId);
  if (sourceQuery.isError || !source) {
    return (
      <div style={{ padding: 24 }}>
        <Alert
          type="warning"
          showIcon
          message="资料详情不可用"
          description="该资料可能已删除或暂时无法读取，请从左侧选择其他资料。"
        />
      </div>
    );
  }
  const evidenceProjection = normalizeEvidencePage(evidenceQuery.data, sourceId);
  const originProjection = normalizeOriginProjection(safeRead(jobsQuery.data, 'origins'), sourceId);
  const jobProjection = normalizeJobProjection(safeRead(jobsQuery.data, 'jobs'), sourceId);
  const safeOrigins = originProjection.items;
  const safeJobs = jobProjection.items;
  const jobsUnavailable = jobsQuery.isError
    || (jobsQuery.data !== undefined && (originProjection.unavailable || jobProjection.unavailable));
  const contentMalformed = contentQuery.data !== undefined && typeof contentQuery.data !== 'string';
  const safeContent = typeof contentQuery.data === 'string'
    ? boundedText(contentQuery.data, '', MAX_DETAIL_LENGTH)
    : undefined;
  const canMutate = knowledgeSourceCanMutate(source);
  const openTitleEditor = () => {
    if (!canMutate) return;
    setEditingTitle(source.display_title || source.title_hint || '');
    setTitleEditorOpen(true);
  };
  const isArchived = source.lifecycle === 'archived';
  return (
    <div className="knowledge-source-detail">
      <div className="knowledge-source-detail-header">
        <div>
          <Title level={4} className="knowledge-source-detail-title">
            {source.title}
          </Title>
          <div className="knowledge-source-detail-statuses">
            <Pill variant={lifecycleVariant(source.lifecycle)}>
              {STATUS_LABEL[source.lifecycle] ?? '状态待确认'}
            </Pill>
            <Pill variant={extractionVariant(source.extraction_status)}>
              {EXTRACTION_LABEL[source.extraction_status] ?? '状态待确认'}
            </Pill>
            {SHOW_BRIEF_UI ? (
              <Pill variant={briefVariant(source.brief_status)}>
                {BRIEF_LABEL[source.brief_status] ?? source.brief_status}
              </Pill>
            ) : null}
          </div>
        </div>
        <Space size={6} wrap className="knowledge-source-actions">
          <Button size="small" icon={<EditOutlined />} onClick={openTitleEditor} disabled={!canMutate}>
            编辑标题
          </Button>
          {isArchived ? (
            <Button
              size="small"
              onClick={() => {
                if (canMutate) unarchiveMutation.mutate(sourceId);
              }}
              loading={unarchiveMutation.isPending}
              disabled={!canMutate}
            >
              取消归档
            </Button>
          ) : (
            <Button
              size="small"
              onClick={() => {
                if (canMutate) archiveMutation.mutate(sourceId);
              }}
              loading={archiveMutation.isPending}
              disabled={!canMutate}
            >
              归档
            </Button>
          )}
          <Button
            size="small"
            danger
            icon={<DeleteOutlined />}
            disabled={!canMutate}
            onClick={() => {
              if (!canMutate) return;
              setDeleteConfirmationText('');
              setDeleteConfirmOpen(true);
            }}
          >
            永久删除该资料
          </Button>
        </Space>
      </div>

      <div className="knowledge-source-metadata">
        <SourceMetadataItem label="文件名" value={source.main_filename} />
        <SourceMetadataItem label="大小" value={formatBytes(source.total_bytes)} />
        <SourceMetadataItem label="展示标题" value={source.display_title || '使用推导标题'} />
        <SourceMetadataItem label="推导标题" value={source.title_hint || '无'} />
        {source.author ? <SourceMetadataItem label="作者" value={source.author} /> : null}
        {source.published_at ? (
          <SourceMetadataItem label="发布时间" value={formatDateTime(source.published_at)} />
        ) : null}
        {source.provenance?.url ? (
          <SourceMetadataItem label="来源 URL" value={source.provenance.url} />
        ) : null}
        <SourceMetadataItem label="导入时间" value={formatDateTime(source.created_at)} />
        <SourceMetadataItem
          label="来源依据"
          value={evidenceQuery.isLoading ? '—' : `${evidenceProjection.items.length} 条`}
        />
      </div>

      {SHOW_BRIEF_UI ? (
        <BriefBlock
          briefStatus={source.brief_status}
          data={briefQuery.data}
          loading={briefQuery.isLoading}
          onRebuild={() => {
            if (canMutate) briefRebuildMutation.mutate(sourceId);
          }}
          rebuilding={briefRebuildMutation.isPending}
          canMutate={canMutate}
          onCitationJump={handleCitationJump}
        />
      ) : null}

      <Tabs
        className="knowledge-source-tabs"
        activeKey={activeDetailTab}
        onChange={setActiveDetailTab}
        items={[
          {
            key: 'status',
            label: '处理记录',
            children: (
              <StatusBlock
                source={source}
                origins={safeOrigins}
                briefAttempts={safeArray(safeRead(briefQuery.data, 'attempts')) as KnowledgeBriefAttempt[]}
                onCitationJump={handleCitationJump}
              />
            ),
          },
          {
            key: 'evidence',
            label: `来源依据${evidenceProjection.items.length ? ` (${evidenceProjection.items.length})` : ''}`,
            children: (
              <EvidenceBlock
                evidence={evidenceProjection.items}
                loading={evidenceQuery.isLoading}
                unavailable={evidenceQuery.isError || evidenceProjection.unavailable}
                sourceId={sourceId}
                highlightEvidenceId={highlightEvidenceId ?? briefCitationTarget}
                onHighlightConsumed={() => {
                  onHighlightConsumed();
                  setBriefCitationTarget(null);
                }}
              />
            ),
          },
          {
            key: 'original',
            label: '资料正文',
            children: (
              <OriginalMarkdownBlock
                sourceId={sourceId}
                content={safeContent}
                loading={contentQuery.isLoading}
                error={contentQuery.isError || contentMalformed}
              />
            ),
          },
          {
            key: 'jobs',
            label: '处理状态',
            children: (
              <JobsBlock
                data={{ jobs: [...safeJobs], origins: [...safeOrigins] }}
                loading={jobsQuery.isLoading}
                unavailable={jobsUnavailable}
                canMutate={canMutate}
              />
            ),
          },
        ]}
      />

      <Modal
        title="编辑展示标题"
        open={titleEditorOpen}
        onCancel={() => setTitleEditorOpen(false)}
        onOk={() => {
          if (canMutate) {
            titleMutation.mutate({ id: sourceId, title: editingTitle.trim() });
          }
        }}
        okButtonProps={{ loading: titleMutation.isPending, disabled: !canMutate }}
        okText="保存"
        cancelText="取消"
      >
        <Space direction="vertical" style={{ width: '100%' }} size="small">
          <Input
            placeholder="留空则回退到推导标题"
            value={editingTitle}
            onChange={(event) => setEditingTitle(event.target.value)}
            maxLength={85}
            showCount
          />
          <Text type="secondary" style={{ fontSize: 12 }}>
            修改展示标题不会触发重新解析，已有来源依据会继续保留。
          </Text>
        </Space>
      </Modal>

      <Modal
        title="永久删除该资料"
        open={deleteConfirmOpen}
        onCancel={() => {
          setDeleteConfirmOpen(false);
          setDeleteConfirmationText('');
        }}
        okText="我已确认,永久删除"
        cancelText="取消"
        okButtonProps={{
          danger: true,
          loading: deleteMutation.isPending,
          disabled: !canMutate || deleteConfirmationText.trim() !== '删除',
        }}
        onOk={() => {
          if (canMutate) deleteMutation.mutate(sourceId);
        }}
      >
        <Space direction="vertical" style={{ width: '100%' }} size="small">
          <div className="knowledge-delete-warning">
            <Title level={5}>危险操作</Title>
            <Paragraph type="secondary">
              永久删除会清除原件、附件、来源依据、保存版本与处理记录,不可恢复。
              删除后相同内容可作为新的资料来源重新导入。
            </Paragraph>
          </div>
          <Alert
            type="error"
            showIcon
            message="此操作不可恢复"
            description="删除后原件、来源依据、附件和处理记录都会被清除。如果之后再次上传相同内容,会得到全新的资料来源。"
          />
          <Text type="secondary" style={{ fontSize: 12 }}>
            为防止误操作,请在下方输入框中输入"删除"以确认。
          </Text>
          <Input
            placeholder='输入"删除"以确认'
            value={deleteConfirmationText}
            onChange={(event) => setDeleteConfirmationText(event.target.value)}
          />
        </Space>
      </Modal>
    </div>
  );
}

function SourceMetadataItem({ label, value }: { label: string; value: string }) {
  return (
    <div className="knowledge-source-metadata-item">
      <span className="knowledge-meta-label">{label}</span>
      <span className="knowledge-meta-value">{value}</span>
    </div>
  );
}

function StatusBlock({
  source,
  origins,
  briefAttempts,
  onCitationJump,
}: {
  source: KnowledgeSource;
  origins: KnowledgeSourceJobsResponse['origins'];
  briefAttempts: KnowledgeBriefAttempt[];
  onCitationJump: (evidenceId: string) => void;
}) {
  const extractionError = source.extraction_status === 'failed'
    ? SAFE_PROCESSING_FAILURE_COPY
    : '';
  const filterSummary = source.evidence_policy_summary;
  const filteredTotal = filterSummary?.filtered_block_total ?? 0;
  return (
    <div className="knowledge-status-record">
      <StatusLine label="资料状态" value={STATUS_LABEL[source.lifecycle] ?? '状态待确认'} />
      <StatusLine
        label="内容整理"
        value={EXTRACTION_LABEL[source.extraction_status] ?? '状态待确认'}
      />
      {SHOW_BRIEF_UI ? (
        <>
          <StatusLine label="资料导读" value={BRIEF_LABEL[source.brief_status] ?? source.brief_status} />
          <StatusLine label="资料导读暂缓原因" value={source.brief_block_reason || '无'} />
          <BriefAttemptTimeline attempts={briefAttempts} onCitationJump={onCitationJump} />
        </>
      ) : null}
      {extractionError ? (
        <Alert type="error" showIcon message="内容整理失败" description={extractionError} />
      ) : null}
      {filterSummary && filteredTotal > 0 ? (
        <div className="knowledge-status-filter-summary">
          <Space size={4} align="center">
            <Title level={5}>来源依据过滤统计</Title>
            <Tooltip title="系统在整理来源依据时按确定性规则过滤作者卡、阅读数、导航、图片壳、Obsidian/Evernote 残片等元数据样板；原文仍可完整查看，被过滤块不参与检索。">
              <Button
                type="text"
                size="small"
                icon={<QuestionCircleOutlined />}
                aria-label="查看来源依据过滤说明"
              />
            </Tooltip>
          </Space>
          <Text type="secondary" style={{ fontSize: 12 }}>
            已过滤 {filteredTotal} 个元数据样板块（不影响原文查看）。
          </Text>
          <Space size={4} wrap>
            {filterSummary.rules.map((rule) => (
              <Tag key={rule.rule_id}>
                {rule.label} ×{rule.count}
              </Tag>
            ))}
          </Space>
        </div>
      ) : null}
      <div className="knowledge-jobs-origins">
        <Space size={4} align="center" className="knowledge-jobs-origins-title">
          <Title level={5}>导入记录</Title>
          <Tooltip
            title="相同内容自动复用已有资料来源。重复上传相同字节时，系统会让上传结果进入已有资料来源，不会创建重复来源依据；每次导入都会追加一条导入记录。"
          >
            <Button
              type="text"
              size="small"
              icon={<QuestionCircleOutlined />}
              aria-label="查看去重说明"
            />
          </Tooltip>
        </Space>
        <List
          dataSource={origins}
          rowKey={(item) => item.id}
          renderItem={(item) => (
            <List.Item>
              <Space direction="vertical" size={2}>
                <Text>
                  {item.import_method}：{item.original_filename || '未提供文件名'}
                </Text>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {item.origin_url ? `URL：${item.origin_url} · ` : ''}
                  {formatDateTime(item.imported_at)}
                </Text>
              </Space>
            </List.Item>
          )}
        />
      </div>
    </div>
  );
}

function StatusLine({ label, value }: { label: string; value: string }) {
  return (
    <div className="knowledge-status-line">
      <span className="knowledge-status-line-label">{label}</span>
      <span>{value}</span>
    </div>
  );
}

const BRIEF_ATTEMPT_PHASE_LABEL: Record<string, string> = {
  generation: '生成候选',
  attempt_started: '开始处理',
  model_call: '模型调用',
  retry: '服务重试',
  provider_switch: '切换服务',
  model_response_parsed: '模型响应解析',
  program_check: '程序检查',
  validation_report: '校验报告',
  validation_result: '单条支持校验',
  validation: '来源依据支持校验',
  repair: '修复',
  repair_requested: '修复请求',
  repair_patch_parsed: '修复补丁解析',
  revalidation: '修复后复验',
  final: '最终结果',
};

const BRIEF_ATTEMPT_STATUS_LABEL: Record<string, string> = {
  pending: '排队中',
  running: '进行中',
  processing: '进行中',
  succeeded: '已完成',
  ready: '已完成',
  failed: '已失败',
  skipped: '已跳过',
};

const BRIEF_REASON_CODE_LABEL: Record<string, string> = {
  unsupported_qualifier: '限定词无直接证据',
  unsupported_inference: '推断超出证据',
  unsupported_claim: '陈述未被证据支持',
  contradicted_by_evidence: '与证据相矛盾',
  evidence_missing: '缺少引用证据',
  validator_parse_failed: '校验结果无法解析',
  validator_unknown_reason: '校验原因待确认',
};

function briefAttemptStatusVariant(status: string): PillVariant {
  switch (status) {
    case 'succeeded':
    case 'ready':
      return 'green';
    case 'failed':
      return 'rose';
    case 'running':
    case 'processing':
      return 'indigo';
    case 'pending':
      return 'amber';
    default:
      return 'gray';
  }
}

function briefStepStatusVariant(status: string): PillVariant {
  switch (status) {
    case 'completed':
    case 'succeeded':
    case 'ready':
      return 'green';
    case 'failed':
      return 'rose';
    case 'running':
    case 'processing':
      return 'indigo';
    default:
      return 'gray';
  }
}

function BriefAttemptTimeline({
  attempts,
  onCitationJump,
}: {
  attempts: KnowledgeBriefAttempt[];
  onCitationJump: (evidenceId: string) => void;
}) {
  const orderedAttempts = [...attempts].sort((left, right) => {
    const leftTime = Date.parse(left.created_at);
    const rightTime = Date.parse(right.created_at);
    if (Number.isNaN(leftTime) || Number.isNaN(rightTime)) {
      return left.id - right.id;
    }
    return leftTime - rightTime;
  });

  return (
    <div className="knowledge-brief-attempts">
      <Title level={5} className="knowledge-brief-attempts-title">
        资料导读处理记录
      </Title>
      {orderedAttempts.length === 0 ? (
        <Text type="secondary" style={{ fontSize: 12 }}>
          暂无资料导读处理记录
        </Text>
      ) : (
        <List
          className="knowledge-brief-attempt-list"
          dataSource={orderedAttempts}
          rowKey={(attempt) => attempt.id}
          renderItem={(attempt) => {
            const steps = [...(attempt.steps ?? [])].sort(
              (left, right) => left.sequence - right.sequence,
            );
            return (
              <List.Item>
                <div className="knowledge-brief-attempt">
                  <Space size={7} wrap>
                    <Text strong>处理记录</Text>
                    <Pill variant={briefAttemptStatusVariant(attempt.status)}>
                      {BRIEF_ATTEMPT_STATUS_LABEL[attempt.status] ?? attempt.status}
                    </Pill>
                    {attempt.created_at ? (
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {formatDateTime(attempt.created_at)}
                      </Text>
                    ) : null}
                  </Space>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    服务：{attempt.actual_provider_id || attempt.provider_id || '—'}
                    {attempt.actual_provider_model || attempt.provider_model
                      ? ` · 模型：${attempt.actual_provider_model || attempt.provider_model}`
                      : ''}
                    {attempt.latency_ms > 0 ? ` · ${attempt.latency_ms} ms` : ''}
                  </Text>
                  {steps.length > 0 ? (
                    <div className="knowledge-brief-step-list">
                      {steps.map((step) => (
                        <BriefAttemptStepView
                          key={`${attempt.id}-${step.sequence}`}
                          step={step}
                          onCitationJump={onCitationJump}
                        />
                      ))}
                    </div>
                  ) : (
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      暂无细分步骤（当前 API 尚未返回 steps）
                    </Text>
                  )}
                  {attempt.has_more ? (
                    <Text type="warning" style={{ fontSize: 12 }}>
                      处理记录仅显示前 {steps.length} / {attempt.total_steps ?? '多'} 条，尚未加载完整时间线。
                    </Text>
                  ) : null}
                </div>
              </List.Item>
            );
          }}
        />
      )}
    </div>
  );
}

function BriefAttemptStepView({
  step,
  onCitationJump,
}: {
  step: KnowledgeBriefAttemptStep;
  onCitationJump: (evidenceId: string) => void;
}) {
  const output = step.output ?? {};
  const decision = step.decision ?? output.decision;
  const reasonCode = step.reason_code ?? output.reason_code;
  const evidenceIds = step.evidence_ids ?? [];
  const fragments = step.unsupported_fragments ?? output.unsupported_fragments ?? [];
  const explanation = step.explanation ?? output.explanation;
  const suggestedRewrite = step.suggested_rewrite ?? output.suggested_rewrite;
  return (
    <div className="knowledge-brief-step">
      <Space size={6} wrap>
        <Text strong>
          {BRIEF_ATTEMPT_PHASE_LABEL[step.phase] ?? '当前进度待确认'}
        </Text>
        <Pill variant={briefStepStatusVariant(step.status)}>
          {BRIEF_ATTEMPT_STATUS_LABEL[step.status] ?? step.status}
        </Pill>
        {step.block_path ? <Text code>{step.block_path}</Text> : null}
        {decision ? <Text type="secondary">判定：{decision}</Text> : null}
        {reasonCode ? (
          <Tag color="orange">
            原因：{BRIEF_REASON_CODE_LABEL[reasonCode] ?? reasonCode}
          </Tag>
        ) : null}
      </Space>
      {fragments.length > 0 ? (
        <div className="knowledge-brief-diagnostic-line">
          <Text type="secondary">未直接支持：</Text>
          <Space size={4} wrap>
            {fragments.map((fragment, index) => (
              <Tag key={`${fragment}-${index}`}>{fragment}</Tag>
            ))}
          </Space>
        </div>
      ) : null}
      {explanation ? (
        <Text type="secondary" className="knowledge-brief-diagnostic-text">
          {explanation}
        </Text>
      ) : null}
      {suggestedRewrite ? (
        <Text type="secondary" className="knowledge-brief-diagnostic-text">
          建议改写：{suggestedRewrite}
        </Text>
      ) : null}
      {evidenceIds.length > 0 ? (
        <BriefEvidenceLinks evidenceIds={evidenceIds} onCitationJump={onCitationJump} />
      ) : null}
      {step.latency_ms != null || step.token_input_count != null || step.token_output_count != null ? (
        <Text type="secondary" style={{ fontSize: 11 }}>
          {step.latency_ms != null ? `耗时 ${step.latency_ms} ms` : ''}
          {step.token_input_count != null ? ` · 输入 ${step.token_input_count} tokens` : ''}
          {step.token_output_count != null ? ` · 输出 ${step.token_output_count} tokens` : ''}
        </Text>
      ) : null}
    </div>
  );
}

function BriefEvidenceLinks({
  evidenceIds,
  onCitationJump,
}: {
  evidenceIds: string[];
  onCitationJump: (evidenceId: string) => void;
}) {
  return (
    <Space size={4} wrap className="knowledge-brief-step-evidence">
      <Text type="secondary" style={{ fontSize: 11 }}>
        来源依据：
      </Text>
      {evidenceIds.map((evidenceId, index) => (
        <Button
          key={evidenceId}
          size="small"
          type="link"
          onClick={() => onCitationJump(evidenceId)}
        >
          来源依据 {index + 1}
        </Button>
      ))}
    </Space>
  );
}

function BriefBlock({
  briefStatus,
  data,
  loading,
  onRebuild,
  rebuilding,
  canMutate,
  onCitationJump,
}: {
  briefStatus: string;
  data: KnowledgeSourceBriefResponse | undefined;
  loading: boolean;
  onRebuild: () => void;
  rebuilding: boolean;
  canMutate: boolean;
  onCitationJump: (evidenceId: string) => void;
}) {
  if (loading) {
    return <Spin />;
  }
  const brief: KnowledgeSourceBrief | null = data?.brief ?? null;
  const latestAttempt: KnowledgeBriefAttempt | null = data?.latest_attempt ?? null;
  const blockReason = data?.brief_block_reason ?? '';
  const errorMessage = data?.brief_error_message ? SAFE_PROCESSING_FAILURE_COPY : '';
  const showEmpty = briefStatus === 'not_started' || (!brief && !latestAttempt);
  // KI-10：旧 Brief 存在时 processing 表示"正在重建"；outdated 表示配置已变化；
  // rebuildFailed 表示最近重建未通过但旧 Brief 已保留（Spec §10.4）。
  const isRebuilding = briefStatus === 'processing' && brief != null;
  const outdated = brief?.outdated === true && !isRebuilding;
  const rebuildFailed =
    brief != null && latestAttempt?.status === 'failed' && !!latestAttempt.error_message;

  return (
    <div className="knowledge-brief-block">
      <div className="knowledge-brief-head">
        <Title level={5} className="knowledge-brief-title">
          <span className="knowledge-brief-spark">
            <ReadOutlined />
          </span>
          资料导读
        </Title>
        <Space size={6} wrap>
          <Pill variant={isRebuilding || outdated || rebuildFailed ? 'amber' : 'violet'}>
            {isRebuilding ? '正在重建' : BRIEF_LABEL[briefStatus] ?? briefStatus}
          </Pill>
          <Button
            size="small"
            onClick={onRebuild}
            loading={rebuilding}
            disabled={!canMutate || briefStatus === 'processing'}
          >
            {brief ? '重建资料导读' : '生成资料导读'}
          </Button>
        </Space>
      </div>
      {blockReason ? (
        <Alert
          type="warning"
          showIcon
          message={`资料导读暂缓：${blockReason}`}
          description="请先在设置中配置满足要求的服务，然后点击生成资料导读。"
        />
      ) : null}
      {outdated ? (
        <Alert
          type="warning"
          showIcon
          message="资料导读已相对当前配置过期"
          description="服务、提示词、结构或保存版本已变化，旧资料导读仍可查看，建议重建。"
          style={{ marginTop: 8 }}
        />
      ) : null}
      {errorMessage && briefStatus === 'failed' ? (
        <Alert
          type="error"
          showIcon
          message="最近一次资料导读校验未通过"
          description={errorMessage}
          style={{ marginTop: 8 }}
        />
      ) : null}
      {rebuildFailed ? (
        <Alert
          type="warning"
          showIcon
          message="最近一次重建未通过，已保留旧资料导读"
          description={SAFE_PROCESSING_FAILURE_COPY}
          style={{ marginTop: 8 }}
        />
      ) : null}
      {latestAttempt?.status === 'failed' &&
      (latestAttempt.validation_report.issues?.length ?? 0) > 0 ? (
        <BriefValidationIssues
          report={latestAttempt.validation_report}
          onCitationJump={onCitationJump}
        />
      ) : null}
      {showEmpty && !blockReason ? (
        <Empty description="尚未生成资料导读，可在上方点击「生成资料导读」" />
      ) : null}
      {brief ? (
        <BriefPayloadView brief={brief} onCitationJump={onCitationJump} />
      ) : null}
    </div>
  );
}

// KBR-05：issue_type → 中文 label。Source 状态区只显示稳定 error code + 失败总数 +
// 短摘要；Attempt 详情按 issue_type 区分 citation/support/coverage/repair 失败。
const ISSUE_TYPE_LABEL: Record<string, string> = {
  schema_invalid: 'Schema 非法',
  citation_missing: '引用缺失',
  citation_ownership: '引用越界',
  support_partial: '部分支持',
  support_unsupported: '未支持',
  support_contradicted: '相矛盾',
  validator_parse_failed: '校验结果无法解析',
  validator_call_failed: '校验暂时不可用',
  coverage_missing: '章节未覆盖',
  // KBR-06：repair patch 非法/越权。
  repair_invalid: '修复补丁非法',
  repair_unauthorized: '修复补丁越权',
};

export function BriefValidationIssues({
  report,
  onCitationJump,
}: {
  report: BriefValidationReport;
  onCitationJump: (evidenceId: string) => void;
}) {
  // Attempt/处理记录展示全部失败项，每项可定位到候选 Brief block 与已引用 Evidence。
  // 详情不复制 Evidence 正文，按 evidence_id 跳转到本地 Evidence。
  const issues = report.issues ?? [];
  if (issues.length === 0) {
    return null;
  }
  return (
    <Alert
      type="error"
      showIcon
      style={{ marginTop: 8 }}
      message={
        report.summary ??
        `资料导读质量校验失败：共 ${report.failure_count ?? issues.length} 条`
      }
      description={
        <Space direction="vertical" size={6} style={{ width: '100%' }}>
          {issues.map((issue, index) => (
            <div
              key={`${issue.block_path}-${index}`}
              className="knowledge-brief-issue"
            >
              <Space size={6} wrap>
                <Pill variant="rose">
                  {ISSUE_TYPE_LABEL[issue.issue_type] ?? issue.issue_type}
                </Pill>
                <Text strong>{issue.block_path || '（全局）'}</Text>
              </Space>
              <div>
                {issue.reason_code ? (
                  <Tag color="orange">
                    原因：{BRIEF_REASON_CODE_LABEL[issue.reason_code] ?? issue.reason_code}
                  </Tag>
                ) : null}
                {issue.unsupported_fragments?.length ? (
                  <div className="knowledge-brief-diagnostic-line">
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      未直接支持：
                    </Text>
                    <Space size={4} wrap>
                      {issue.unsupported_fragments.map((fragment, fragmentIndex) => (
                        <Tag key={`${fragment}-${fragmentIndex}`}>{fragment}</Tag>
                      ))}
                    </Space>
                  </div>
                ) : null}
                {issue.explanation || issue.reason ? (
                  <Text type="secondary" className="knowledge-brief-diagnostic-text">
                    {issue.explanation || issue.reason}
                  </Text>
                ) : null}
                {issue.suggested_rewrite ? (
                  <Text type="secondary" className="knowledge-brief-diagnostic-text">
                    建议改写：{issue.suggested_rewrite}
                  </Text>
                ) : null}
              </div>
              {issue.evidence_ids?.length > 0 ? (
                <BriefEvidenceLinks
                  evidenceIds={issue.evidence_ids}
                  onCitationJump={onCitationJump}
                />
              ) : null}
            </div>
          ))}
        </Space>
      }
    />
  );
}

function BriefPayloadView({
  brief,
  onCitationJump,
}: {
  brief: KnowledgeSourceBrief;
  onCitationJump: (evidenceId: string) => void;
}) {
  const { payload } = brief;
  return (
    <div style={{ width: '100%' }}>
      <BriefStatementList
        title="概述"
        items={payload.overview}
        onCitationJump={onCitationJump}
      />
      <BriefStatementList
        title="关键要点"
        items={payload.key_points}
        onCitationJump={onCitationJump}
      />
      <div className="knowledge-brief-section">
        <Title level={5} className="knowledge-brief-section-label">
          章节导读
        </Title>
        {payload.section_guides.map((item, index) => (
          <div key={`${item.section_key}-${index}`} className="knowledge-section-guide">
            <span className="knowledge-section-guide-path">
              {item.heading_path.map((segment, segmentIndex) => (
                <Fragment key={segmentIndex}>
                  {segmentIndex > 0 && (
                    <span className="knowledge-section-guide-sep">/</span>
                  )}
                  {segment}
                </Fragment>
              ))}
            </span>
            <div className="knowledge-section-guide-summary">{item.summary}</div>
            <BriefCitationChips evidenceIds={item.evidence_ids} onJump={onCitationJump} />
          </div>
        ))}
      </div>
      <BriefStatementList
        title="局限与未覆盖"
        accent="warning"
        items={payload.limitations}
        onCitationJump={onCitationJump}
      />
      {payload.coverage && payload.coverage.length > 0 ? (
        <div className="knowledge-brief-section">
          <Title level={5} className="knowledge-brief-section-label">
            章节覆盖
          </Title>
          <div className="knowledge-coverage-row">
            {payload.coverage.map((item) => (
              <span
                key={item.section_key}
                className={`knowledge-chip-coverage${
                  item.status === 'covered' ? ' is-covered' : ' is-skipped'
                }`}
              >
                {item.section_key}
                {item.status === 'skipped'
                  ? `（已跳过：${item.skipped_reason || '—'}）`
                  : ''}
              </span>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function BriefStatementList({
  title,
  accent,
  items,
  onCitationJump,
}: {
  title: string;
  accent?: 'warning';
  items: BriefStatement[];
  onCitationJump: (evidenceId: string) => void;
}) {
  if (!items.length) {
    return null;
  }
  return (
    <div className="knowledge-brief-section">
      <Title level={5} className="knowledge-brief-section-label">
        {title}
      </Title>
      {items.map((item, index) => (
        <div
          key={`${title}-${index}`}
          className={`knowledge-brief-statement${accent ? ` knowledge-brief-statement--${accent}` : ''}`}
        >
          <div className="knowledge-brief-statement-text">{item.statement}</div>
          <BriefCitationChips evidenceIds={item.evidence_ids} onJump={onCitationJump} />
        </div>
      ))}
    </div>
  );
}

function BriefCitationChips({
  evidenceIds,
  onJump,
}: {
  evidenceIds: string[];
  onJump: (evidenceId: string) => void;
}) {
  if (!evidenceIds.length) {
    return null;
  }
  return (
    <div className="knowledge-citation-chips">
      {evidenceIds.map((id, index) => (
        <button key={id} type="button" className="knowledge-chip-cite" onClick={() => onJump(id)}>
          来源依据 {index + 1}
        </button>
      ))}
    </div>
  );
}

function EvidenceBlock({
  evidence,
  loading,
  unavailable,
  sourceId,
  highlightEvidenceId,
  onHighlightConsumed,
}: {
  evidence: KnowledgeEvidence[];
  loading: boolean;
  unavailable: boolean;
  sourceId: number;
  highlightEvidenceId: string | null;
  onHighlightConsumed: () => void;
}) {
  const highlightRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!highlightEvidenceId || loading || !highlightRef.current) return;
    if (highlightRef.current) {
      highlightRef.current.scrollIntoView({ behavior: 'auto', block: 'center' });
    }
    onHighlightConsumed();
  }, [highlightEvidenceId, loading, evidence, onHighlightConsumed]);
  if (loading) {
    return <Spin />;
  }
  if (!evidence.length && !unavailable) {
    return (
      <Empty description="尚未生成来源依据" />
    );
  }
  return (
    <div>
      {unavailable ? (
        <Alert
          type="warning"
          showIcon
          message="部分来源依据暂时不可用"
          description={evidence.length ? '已展示可安全读取的内容。' : '请稍后重试。'}
        />
      ) : null}
      <List
        className="knowledge-evidence-list"
        dataSource={evidence}
        rowKey={(item) => item.id}
        renderItem={(item) => {
          const isHighlighted = highlightEvidenceId === item.id;
          return (
            <List.Item>
              <div
                ref={isHighlighted ? highlightRef : undefined}
                className={`knowledge-evidence-item${isHighlighted ? ' is-hit' : ''}`}
              >
                <div className="knowledge-evidence-meta">
                  <span className="knowledge-evidence-kind">{item.block_kind}</span>
                  {isHighlighted ? (
                    <Pill variant="amber" className="knowledge-evidence-hit-badge">
                      搜索命中
                    </Pill>
                  ) : null}
                </div>
                {item.heading_path.length ? (
                  <div className="knowledge-evidence-path">
                    {item.heading_path.map((segment, segmentIndex) => (
                      <Fragment key={segmentIndex}>
                        {segmentIndex > 0 && (
                          <span className="knowledge-evidence-path-sep">›</span>
                        )}
                        {segment}
                      </Fragment>
                    ))}
                  </div>
                ) : null}
                {item.kind === 'asset' && item.asset_id != null ? (
                  <AssetEvidenceView
                    sourceId={sourceId}
                    assetId={item.asset_id}
                    alt={item.search_text}
                  />
                ) : null}
                <MarkdownContent content={item.canonical_excerpt} />
              </div>
            </List.Item>
          );
        }}
      />
    </div>
  );
}

function OriginalMarkdownBlock({
  sourceId,
  content,
  loading,
  error,
}: {
  sourceId: number;
  content: string | undefined;
  loading: boolean;
  error: boolean;
}) {
  if (loading) {
    return <Spin />;
  }
  if (error || content === undefined) {
    return (
      <Alert
        type="warning"
        showIcon
        message="无法读取资料正文"
        description="可以下载原件后在本地查看。"
      />
    );
  }
  return (
    <div className="knowledge-original-markdown">
      <div className="knowledge-original-markdown-toolbar">
        <Text type="secondary">以下为资料正文预览，过长内容会缩略显示。</Text>
        <Button size="small" href={buildKnowledgeSourceContentUrl(sourceId)} target="_blank">
          下载原件
        </Button>
      </div>
      <MarkdownContent content={content} />
    </div>
  );
}

function MarkdownContent({ content }: { content: string }) {
  const safeContent = boundedText(content, '内容暂时不可用', MAX_DETAIL_LENGTH);
  return (
    <div className="knowledge-markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[[rehypeHighlight, { detect: true, ignoreMissing: true }]]}
      >
        {safeContent}
      </ReactMarkdown>
    </div>
  );
}

function AssetEvidenceView({
  sourceId,
  assetId,
  alt,
}: {
  sourceId: number;
  assetId: number;
  alt: string;
}) {
  const url = buildKnowledgeAssetContentUrl(sourceId, assetId);
  return (
    <Space direction="vertical" size={4} style={{ width: '100%' }}>
      {/* eslint-disable-next-line jsx-a11y/alt-text */}
      <img
        src={url}
        alt={alt || '图文资料附件'}
        loading="lazy"
        className="knowledge-evidence-asset"
      />
      <Button size="small" href={url} target="_blank" rel="noopener noreferrer">
        下载原图
      </Button>
    </Space>
  );
}

function JobsBlock({
  data,
  loading,
  unavailable,
  canMutate,
}: {
  data: KnowledgeSourceJobsResponse;
  loading: boolean;
  unavailable: boolean;
  canMutate: boolean;
}) {
  const queryClient = useQueryClient();
  const cancelMutation = useMutation({
    mutationFn: (jobId: number) => cancelKnowledgeJob(jobId),
    onSuccess: () => {
      message.success('已请求取消');
      queryClient.invalidateQueries({ queryKey: ['knowledge'] });
    },
    onError: () => {
      message.error('取消失败，请稍后重试。');
    },
  });
  if (loading) {
    return <Spin />;
  }
  return (
    <div>
      {unavailable ? (
        <Alert
          type="warning"
          showIcon
          message="部分处理记录暂时不可用"
          description="已隐藏不完整的处理记录，请稍后重试。"
        />
      ) : null}
      {!unavailable && data.jobs.length === 0 ? <Empty description="暂无处理记录" /> : null}
      <List
        className="knowledge-jobs-list"
        dataSource={data.jobs}
        rowKey={(item) => item.id}
        renderItem={(item) => (
          <List.Item>
            <Space direction="vertical" size={2} style={{ width: '100%' }}>
              <Space size={8} wrap>
                <span className="knowledge-evidence-kind">{item.kind === 'delete' ? '删除任务' : '资料处理任务'}</span>
                <Pill variant={jobStatusVariant(item.status)}>
                  {JOB_STATUS_LABEL[item.status] ?? '状态待确认'}
                </Pill>
                {item.canceled ? <Pill variant="rose">已取消</Pill> : null}
              </Space>
              {item.progress > 0 ? <Progress percent={item.progress} size="small" /> : null}
              {item.created_at ? (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  开始时间：{formatDateTime(item.created_at)}
                </Text>
              ) : null}
              {(item.retry_count ?? 0) > 0 ? (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  重试次数：{item.retry_count}
                  {item.next_retry_at
                    ? ` · 下次重试 ${formatDateTime(item.next_retry_at)}`
                    : ''}
                </Text>
              ) : null}
      {item.error_message ? (
                <Alert
                  type="error"
                  showIcon
                  message={SAFE_PROCESSING_FAILURE_COPY}
                />
              ) : null}
              {canMutate && isJobCancellable(item) ? (
                <Button
                  size="small"
                  danger
                  loading={cancelMutation.isPending}
                  onClick={() => {
                    if (canMutate) cancelMutation.mutate(item.id);
                  }}
                >
                  取消任务
                </Button>
              ) : null}
            </Space>
          </List.Item>
        )}
      />
    </div>
  );
}

const JOB_STATUS_LABEL: Record<string, string> = {
  pending: '排队中',
  running: '运行中',
  succeeded: '已完成',
  failed: '已失败',
  canceled: '已取消',
};

function isJobCancellable(job: KnowledgeJob): boolean {
  return !job.canceled && job.status !== 'succeeded' && job.status !== 'failed' && job.status !== 'canceled';
}

function SearchResultsPanel({
  query,
  hits,
  unavailable,
  onPick,
}: {
  query: string;
  hits: import('@/types/knowledge').KnowledgeEvidenceSearchHit[];
  unavailable: boolean;
  onPick: (sourceId: number, evidenceId: string) => void;
}) {
  if (!hits.length && unavailable) {
    return (
      <Alert
        type="warning"
        showIcon
        message="搜索结果暂时不可用"
        description="请稍后重试。"
      />
    );
  }
  if (!hits.length) {
    return (
      <Alert
        type="info"
        showIcon
        message={`未匹配资料内容：${query}`}
        description="尝试更宽的关键词，或确认内容整理已完成。"
      />
    );
  }
  return (
    <Alert
      type="success"
      showIcon
      message={`命中 ${hits.length} 条资料内容：${query}`}
      description={
        <>
          {unavailable ? <Text type="warning">部分结果暂时不可用，已展示可安全读取的内容。</Text> : null}
          <List
            dataSource={hits}
            rowKey={(item) => item.evidence_id}
            renderItem={(item) => (
            <List.Item
              actions={[
                <Button
                  key="open"
                  size="small"
                  onClick={() => onPick(item.source_id, item.evidence_id)}
                >
                  打开并定位
                </Button>,
              ]}
            >
              <Space direction="vertical" size={2} style={{ width: '100%' }}>
                <Space size={6}>
                  <span className="knowledge-evidence-kind">{item.block_kind}</span>
                  {item.heading_path.length ? (
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {item.heading_path.join(' / ')}
                    </Text>
                  ) : null}
                </Space>
                <Text>{boundedText(item.snippet, '内容暂时不可用', 800)}</Text>
              </Space>
            </List.Item>
            )}
          />
        </>
      }
    />
  );
}

function UploadModal({
  open,
  onClose,
  uploading,
  onSubmit,
}: {
  open: boolean;
  onClose: () => void;
  uploading: boolean;
  onSubmit: (file: File, titleHint: string) => void;
}) {
  const [fileList, setFileList] = useState<UploadFile[]>([]);
  const [titleHint, setTitleHint] = useState('');
  const pickedFile = fileList[0]?.originFileObj as File | undefined;

  const handleUpload = () => {
    if (!pickedFile) {
      message.warning('请选择一个 Markdown 或 Text 文件');
      return;
    }
    onSubmit(pickedFile, titleHint);
  };

  return (
    <Modal
      title="上传 Markdown / Text 资料"
      open={open}
      onCancel={() => {
        setFileList([]);
        setTitleHint('');
        onClose();
      }}
      onOk={handleUpload}
      okButtonProps={{ loading: uploading }}
      okText="开始导入"
    >
      <Space direction="vertical" style={{ width: '100%' }} size="middle">
        <Upload.Dragger
          accept=".md,.txt,text/markdown,text/plain"
          maxCount={1}
          beforeUpload={() => false}
          fileList={fileList}
          onChange={({ fileList: next }) => setFileList(next)}
        >
          <p className="ant-upload-drag-icon">
            <InboxOutlined />
          </p>
          <p className="ant-upload-text">点击或拖拽 .md / .txt 文件到此处</p>
          <p className="ant-upload-hint">
            单文件最大 5 MiB / 64,000 tokens；UTF-8（含 BOM）、UTF-16 BOM 或高置信 GBK/GB18030
          </p>
        </Upload.Dragger>
        <Input
          placeholder="可选：展示标题（不填则用文件名或首个 # 标题）"
          value={titleHint}
          onChange={(event) => setTitleHint(event.target.value)}
        />
      </Space>
    </Modal>
  );
}

function BundleModal({
  open,
  onClose,
  uploading,
  onSubmit,
}: {
  open: boolean;
  onClose: () => void;
  uploading: boolean;
  onSubmit: (main: File, assets: File[], titleHint: string) => void;
}) {
  const [mainFileList, setMainFileList] = useState<UploadFile[]>([]);
  const [assetFileList, setAssetFileList] = useState<UploadFile[]>([]);
  const [titleHint, setTitleHint] = useState('');

  const mainFile = mainFileList[0]?.originFileObj as File | undefined;
  const assetFiles = assetFileList
    .map((item) => item.originFileObj as File | undefined)
    .filter((value): value is File => Boolean(value));

  const handleSubmit = () => {
    if (!mainFile) {
      message.warning('请选择 Markdown 主文件');
      return;
    }
    if (assetFiles.length === 0) {
      message.warning('图文资料至少需要一张图片附件');
      return;
    }
    onSubmit(mainFile, assetFiles, titleHint);
  };

  return (
    <Modal
      title="上传图文资料"
      open={open}
      onCancel={() => {
        setMainFileList([]);
        setAssetFileList([]);
        setTitleHint('');
        onClose();
      }}
      onOk={handleSubmit}
      okButtonProps={{ loading: uploading }}
      okText="开始导入"
      width={640}
    >
      <Space direction="vertical" style={{ width: '100%' }} size="middle">
        <div>
          <Title level={5} style={{ marginBottom: 4 }}>
            Markdown 主文件
          </Title>
          <Upload.Dragger
            accept=".md,text/markdown"
            maxCount={1}
            beforeUpload={() => false}
            fileList={mainFileList}
            onChange={({ fileList: next }) => setMainFileList(next)}
          >
            <p className="ant-upload-drag-icon">
              <InboxOutlined />
            </p>
            <p className="ant-upload-text">点击或拖拽 .md 主文件</p>
            <p className="ant-upload-hint">主文件最大 5 MiB；图片引用必须使用扁平相对路径</p>
          </Upload.Dragger>
        </div>
        <div>
          <Title level={5} style={{ marginBottom: 4 }}>
            图片附件（PNG / JPEG / WebP）
          </Title>
          <Upload.Dragger
            accept=".png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp"
            multiple
            beforeUpload={() => false}
            fileList={assetFileList}
            onChange={({ fileList: next }) => setAssetFileList(next)}
          >
            <p className="ant-upload-drag-icon">
              <PictureOutlined />
            </p>
            <p className="ant-upload-text">点击或拖拽多张图片到此处</p>
            <p className="ant-upload-hint">
              单图 ≤ 10 MiB / 40 MP；图文资料总大小 ≤ 50 MiB；附件数量 ≤ 50
            </p>
          </Upload.Dragger>
        </div>
        <Input
          placeholder="可选：展示标题（不填则用文件名或首个 # 标题）"
          value={titleHint}
          onChange={(event) => setTitleHint(event.target.value)}
        />
        <Alert
          type="info"
          showIcon
          message="系统不会 OCR 图片内容，也不会调用多模态模型"
          description="alt 文本作为作者原文参与检索；图片字节不会进入 FTS。"
        />
      </Space>
    </Modal>
  );
}

function PasteModal({
  open,
  onClose,
  submitting,
  onSubmit,
}: {
  open: boolean;
  onClose: () => void;
  submitting: boolean;
  onSubmit: (paste: string, titleHint: string, originUrl: string) => void;
}) {
  const [paste, setPaste] = useState('');
  const [titleHint, setTitleHint] = useState('');
  const [originUrl, setOriginUrl] = useState('');

  const handleSubmit = () => {
    if (!paste.trim()) {
      message.warning('请粘贴正文内容');
      return;
    }
    onSubmit(paste, titleHint, originUrl);
  };

  return (
    <Modal
      title="粘贴正文"
      open={open}
      onCancel={() => {
        setPaste('');
        setTitleHint('');
        setOriginUrl('');
        onClose();
      }}
      onOk={handleSubmit}
      okButtonProps={{ loading: submitting }}
      okText="开始导入"
      width={640}
    >
      <Space direction="vertical" style={{ width: '100%' }} size="middle">
        <Input.TextArea
          placeholder="在此粘贴 Markdown 正文（系统会作为虚拟 main.md 进入同一 Pipeline）"
          value={paste}
          onChange={(event) => setPaste(event.target.value)}
          autoSize={{ minRows: 8, maxRows: 18 }}
        />
        <Input
          placeholder="可选：展示标题（不填则用首个 # 标题或首段内容）"
          value={titleHint}
          onChange={(event) => setTitleHint(event.target.value)}
        />
        <Input
          placeholder="可选：来源 URL（仅作为 provenance 保存，系统不会发起网络请求）"
          value={originUrl}
          onChange={(event) => setOriginUrl(event.target.value)}
        />
      </Space>
    </Modal>
  );
}

function formatBytes(total: number): string {
  if (total < 1024) return `${total} B`;
  if (total < 1024 * 1024) return `${(total / 1024).toFixed(1)} KB`;
  return `${(total / 1024 / 1024).toFixed(2)} MB`;
}

function formatDateTime(value: string | null): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function extractErrorMessage(error: unknown): string {
  return error ? '操作未完成，请稍后重试' : '未知错误';
}
