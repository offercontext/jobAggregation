import { describe, expect, it } from 'vitest';
import source from './knowledge.ts?raw';
import {
  decodeKnowledgeSourceContent,
  parseConfirmedInterviewKnowledgeNotesResponse,
} from './knowledge';

describe('confirmed interview knowledge source envelope', () => {
  it('preserves a legitimate empty collection', () => {
    expect(parseConfirmedInterviewKnowledgeNotesResponse({ items: [] })).toEqual([]);
  });

  it.each([
    null,
    {},
    { items: null },
    { items: undefined },
    { items: 'not-a-list' },
  ])('rejects malformed successful responses instead of presenting them as empty', (input) => {
    expect(() => parseConfirmedInterviewKnowledgeNotesResponse(input)).toThrow(
      '经历素材来源暂时不可用',
    );
  });

  it('fails closed for a revoked collection proxy', () => {
    const { proxy, revoke } = Proxy.revocable([], {});
    revoke();
    expect(() => parseConfirmedInterviewKnowledgeNotesResponse({ items: proxy })).toThrow(
      '经历素材来源暂时不可用',
    );
  });
});

describe('knowledge service KI-03 contract', () => {
  it('exposes upload/paste/list/detail/evidence/search endpoints without legacy wiki helpers', () => {
    expect(source).toContain('/knowledge/sources');
    expect(source).toContain('/knowledge/evidence/search');
    expect(source).toContain('/knowledge/sources/');
    expect(source).toContain('uploadKnowledgeSource');
    expect(source).toContain('pasteKnowledgeSource');
    expect(source).toContain('searchKnowledgeEvidence');
    expect(source).toContain('buildKnowledgeSourceContentUrl');
    expect(source).toContain('fetchKnowledgeSourceContent');
    expect(source).toContain("responseType: 'arraybuffer'");
    expect(source).not.toContain('fetchKnowledgePages');
    expect(source).not.toContain('searchWiki');
    expect(source).not.toContain('addToWiki');
  });
});

describe('knowledge source content decoding', () => {
  it('decodes UTF-8, UTF-16 BOM and GB18030 source bytes for the Markdown preview', () => {
    const utf8 = new TextEncoder().encode('# UTF-8\n中文').buffer;
    const utf16le = new Uint8Array([0xff, 0xfe, 0x2d, 0x4e, 0x87, 0x65]).buffer;
    const gb18030 = new Uint8Array([0xd6, 0xd0, 0xce, 0xc4]).buffer;

    expect(decodeKnowledgeSourceContent(utf8)).toBe('# UTF-8\n中文');
    expect(decodeKnowledgeSourceContent(utf16le)).toBe('中文');
    expect(decodeKnowledgeSourceContent(gb18030)).toBe('中文');
  });

  it.each([null, undefined, {}, 'text'])('rejects malformed successful content responses (%o)', (input) => {
    expect(() => decodeKnowledgeSourceContent(input as never)).toThrow('资料正文来源暂时不可用');
  });
});

describe('knowledge service KI-04 contract', () => {
  it('exposes bundle upload and asset download endpoints', () => {
    expect(source).toContain('uploadKnowledgeBundle');
    expect(source).toContain('fetchKnowledgeSourceAssets');
    expect(source).toContain('buildKnowledgeAssetContentUrl');
    expect(source).toContain('/assets/');
    expect(source).toContain('assets/${assetId}/content');
  });
});

describe('knowledge service KI-05 contract', () => {
  it('exposes display_title PATCH endpoint without legacy wiki helpers', () => {
    expect(source).toContain('updateKnowledgeSourceTitle');
    expect(source).toContain('`/knowledge/sources/${sourceId}`');
    expect(source).toContain('display_title');
    expect(source).toContain('.patch<');
  });
});

describe('knowledge service KI-06 contract', () => {
  it('exposes archive / unarchive / delete endpoints with include_archived filter', () => {
    expect(source).toContain('archiveKnowledgeSource');
    expect(source).toContain('unarchiveKnowledgeSource');
    expect(source).toContain('deleteKnowledgeSource');
    expect(source).toContain('/archive');
    expect(source).toContain('/unarchive');
    expect(source).toContain('.delete<');
    expect(source).toContain('includeArchived');
    expect(source).toContain('include_archived');
  });
});

describe('knowledge service KBR-07 reset contract', () => {
  it('does not expose long-lived Knowledge reset HTTP client', () => {
    // 一次性迁移只保留本地 CLI；前端 service 不得再暴露 reset 入口。
    expect(source).not.toContain('resetKnowledgeDomain');
    expect(source).not.toContain('/knowledge/reset');
    expect(source).not.toContain('KnowledgeResetSummary');
  });
});
