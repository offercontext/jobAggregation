import type {
  KnowledgeBriefRebuildResponse,
  ConfirmedInterviewKnowledgeNote,
  KnowledgeDeleteResponse,
  KnowledgeEvidence,
  KnowledgeEvidencePage,
  KnowledgeEvidenceSearchResponse,
  KnowledgeIngestResponse,
  KnowledgeJob,
  KnowledgeSource,
  KnowledgeSourceAssetsResponse,
  KnowledgeSourceBriefResponse,
  KnowledgeSourceJobsResponse,
} from '@/types/knowledge';
import { createApiClient } from './http';

const http = createApiClient({ baseURL: '/api', timeout: 30000 });

export async function fetchConfirmedInterviewKnowledgeNotes(): Promise<ConfirmedInterviewKnowledgeNote[]> {
  const { data } = await http.get<unknown>('/knowledge/notes');
  return parseConfirmedInterviewKnowledgeNotesResponse(data);
}

/**
 * Keep malformed successful responses distinct from a legitimate empty source.
 * Row-level eligibility remains owned by the material classifier; this boundary
 * only proves that the collection envelope itself is present and iterable.
 */
export function parseConfirmedInterviewKnowledgeNotesResponse(
  input: unknown,
): ConfirmedInterviewKnowledgeNote[] {
  try {
    if (typeof input !== 'object' || input === null) {
      throw new TypeError('invalid envelope');
    }
    const items = Reflect.get(input, 'items');
    if (!Array.isArray(items)) {
      throw new TypeError('invalid items');
    }
    const result: ConfirmedInterviewKnowledgeNote[] = [];
    for (let index = 0; index < items.length; index += 1) {
      result.push(items[index] as ConfirmedInterviewKnowledgeNote);
    }
    return result;
  } catch {
    throw new Error('经历素材来源暂时不可用');
  }
}

export async function fetchConfirmedInterviewKnowledgeNote(
  noteId: number,
): Promise<ConfirmedInterviewKnowledgeNote> {
  const { data } = await http.get<ConfirmedInterviewKnowledgeNote>(`/knowledge/notes/${noteId}`);
  return data;
}

export async function fetchKnowledgeSources(
  options: { includeArchived?: boolean } = {},
): Promise<KnowledgeSource[]> {
  const { data } = await http.get<KnowledgeSource[]>('/knowledge/sources', {
    params: options.includeArchived ? { include_archived: true } : undefined,
  });
  return data;
}

export async function fetchKnowledgeSource(sourceId: number): Promise<KnowledgeSource> {
  const { data } = await http.get<KnowledgeSource>(`/knowledge/sources/${sourceId}`);
  return data;
}

export async function fetchKnowledgeSourceEvidence(
  sourceId: number,
  params?: { after_ordinal?: number; limit?: number },
): Promise<KnowledgeEvidencePage> {
  const { data } = await http.get<KnowledgeEvidencePage>(
    `/knowledge/sources/${sourceId}/evidence`,
    { params },
  );
  return data;
}

export async function fetchKnowledgeSourceAssets(
  sourceId: number,
): Promise<KnowledgeSourceAssetsResponse> {
  const { data } = await http.get<KnowledgeSourceAssetsResponse>(
    `/knowledge/sources/${sourceId}/assets`,
  );
  return data;
}

export async function fetchKnowledgeSourceJobs(
  sourceId: number,
): Promise<KnowledgeSourceJobsResponse> {
  const { data } = await http.get<KnowledgeSourceJobsResponse>(
    `/knowledge/sources/${sourceId}/jobs`,
  );
  return data;
}

export async function fetchKnowledgeEvidence(evidenceId: string): Promise<KnowledgeEvidence> {
  const { data } = await http.get<KnowledgeEvidence>(`/knowledge/evidence/${evidenceId}`);
  return data;
}

export async function searchKnowledgeEvidence(
  query: string,
  options: { source_ids?: number[]; include_archived?: boolean; limit?: number } = {},
): Promise<KnowledgeEvidenceSearchResponse> {
  const { data } = await http.post<KnowledgeEvidenceSearchResponse>(
    '/knowledge/evidence/search',
    {
      query,
      source_ids: options.source_ids,
      include_archived: options.include_archived,
      limit: options.limit,
    },
  );
  return data;
}

export async function uploadKnowledgeSource(
  file: File,
  titleHint = '',
): Promise<KnowledgeIngestResponse> {
  const form = new FormData();
  form.append('file', file);
  if (titleHint) {
    form.append('title_hint', titleHint);
  }
  const { data } = await http.post<KnowledgeIngestResponse>('/knowledge/sources', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
  return data;
}

export async function uploadKnowledgeBundle(
  main: File,
  assets: File[],
  titleHint = '',
): Promise<KnowledgeIngestResponse> {
  const form = new FormData();
  form.append('file', main);
  assets.forEach((asset) => {
    form.append('files', asset, asset.name);
  });
  if (titleHint) {
    form.append('title_hint', titleHint);
  }
  const { data } = await http.post<KnowledgeIngestResponse>('/knowledge/sources', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
  return data;
}

export async function pasteKnowledgeSource(
  paste: string,
  options: { titleHint?: string; originUrl?: string } = {},
): Promise<KnowledgeIngestResponse> {
  const form = new FormData();
  form.append('paste', paste);
  if (options.titleHint) {
    form.append('title_hint', options.titleHint);
  }
  if (options.originUrl) {
    form.append('origin_url', options.originUrl);
  }
  const { data } = await http.post<KnowledgeIngestResponse>('/knowledge/sources', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
  return data;
}

export async function updateKnowledgeSourceTitle(
  sourceId: number,
  displayTitle: string,
): Promise<KnowledgeSource> {
  const { data } = await http.patch<KnowledgeSource>(
    `/knowledge/sources/${sourceId}`,
    { display_title: displayTitle },
  );
  return data;
}

export async function archiveKnowledgeSource(
  sourceId: number,
): Promise<KnowledgeSource> {
  const { data } = await http.post<KnowledgeSource>(
    `/knowledge/sources/${sourceId}/archive`,
  );
  return data;
}

export async function unarchiveKnowledgeSource(
  sourceId: number,
): Promise<KnowledgeSource> {
  const { data } = await http.post<KnowledgeSource>(
    `/knowledge/sources/${sourceId}/unarchive`,
  );
  return data;
}

export async function deleteKnowledgeSource(
  sourceId: number,
): Promise<KnowledgeDeleteResponse> {
  const { data } = await http.delete<KnowledgeDeleteResponse>(
    `/knowledge/sources/${sourceId}`,
  );
  return data;
}

export function buildKnowledgeSourceContentUrl(sourceId: number): string {
  return `/api/knowledge/sources/${sourceId}/content`;
}

export async function fetchKnowledgeSourceContent(sourceId: number): Promise<string> {
  const { data } = await http.get<ArrayBuffer>(`/knowledge/sources/${sourceId}/content`, {
    responseType: 'arraybuffer',
  });
  return decodeKnowledgeSourceContent(data);
}

export function decodeKnowledgeSourceContent(content: ArrayBuffer): string {
  let bytes: Uint8Array;
  try {
    if (!(content instanceof ArrayBuffer)) {
      throw new TypeError('invalid content envelope');
    }
    bytes = new Uint8Array(content);
  } catch {
    throw new Error('资料正文来源暂时不可用');
  }
  if (bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf) {
    return new TextDecoder('utf-8', { fatal: true }).decode(bytes.subarray(3));
  }
  if (bytes[0] === 0xff && bytes[1] === 0xfe) {
    return new TextDecoder('utf-16le', { fatal: true }).decode(bytes.subarray(2));
  }
  if (bytes[0] === 0xfe && bytes[1] === 0xff) {
    return new TextDecoder('utf-16be', { fatal: true }).decode(bytes.subarray(2));
  }
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(bytes);
  } catch {
    // Source 已由后端严格校验，剩余合法来源仅可能是 GBK/GB18030 家族。
    return new TextDecoder('gb18030', { fatal: true }).decode(bytes);
  }
}

export function buildKnowledgeAssetContentUrl(
  sourceId: number,
  assetId: number,
): string {
  return `/api/knowledge/sources/${sourceId}/assets/${assetId}/content`;
}

export async function cancelKnowledgeJob(jobId: number): Promise<KnowledgeJob> {
  const { data } = await http.post<KnowledgeJob>(
    `/knowledge/jobs/${jobId}/cancel`,
  );
  return data;
}

export async function fetchKnowledgeJob(jobId: number): Promise<KnowledgeJob> {
  const { data } = await http.get<KnowledgeJob>(`/knowledge/jobs/${jobId}`);
  return data;
}

export async function fetchKnowledgeSourceBrief(
  sourceId: number,
): Promise<KnowledgeSourceBriefResponse> {
  const { data } = await http.get<KnowledgeSourceBriefResponse>(
    `/knowledge/sources/${sourceId}/brief`,
  );
  return data;
}

export async function rebuildKnowledgeSourceBrief(
  sourceId: number,
): Promise<KnowledgeBriefRebuildResponse> {
  const { data } = await http.post<KnowledgeBriefRebuildResponse>(
    `/knowledge/sources/${sourceId}/brief/rebuild`,
  );
  return data;
}
