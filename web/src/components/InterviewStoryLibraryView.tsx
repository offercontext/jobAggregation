import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Alert, Button, Empty, Input, List, Space, Spin, Tag, Typography } from 'antd';
import {
  archiveInterviewStory,
  getInterviewStory,
  getInterviewStoryVersion,
  listInterviewStories,
  listInterviewStoryVersions,
  restoreInterviewStory,
} from '@/services/interviewStories';
import type {
  InterviewStory,
  InterviewStoryEvidenceLink,
  InterviewStorySourceState,
  InterviewStoryTargetKind,
  InterviewStoryVersion,
} from '@/types/interviewStory';
import { projectExperienceMaterials } from '@/features/materialSurfaces/materialClassification';
import styles from './InterviewStoryLibraryView.module.css';

const { Paragraph, Title, Text } = Typography;

const BLOCK_LABELS = {
  situation: '情境',
  task: '任务',
  action: '行动',
  result: '结果',
  reflection: '复盘',
} as const;

const SOURCE_LABELS = {
  resume_version: '简历版本',
  interview_note: '面试复盘',
  mock_turn: '模拟面试回答',
  user_assertion: '已冻结的用户确认陈述',
} as const;

const STORY_SOURCE_KINDS = new Set(['resume_version', 'interview_note', 'mock_turn', 'user_assertion']);
const SOURCE_STATE_NAMES = new Set(['current', 'changed', 'missing', 'frozen_user_assertion', 'error', 'deleted', 'unknown']);
const MAX_SOURCE_STATES = 64;
const MAX_EVIDENCE_LINKS = 256;
const MAX_ASSERTIONS = 64;
const MAX_BLOCKS = 12;
const MAX_SHORT_ITEMS = 12;

type UnknownRecord = Record<string, unknown>;

interface SafeRead {
  ok: boolean;
  value: unknown;
}

function recordOf(value: unknown): UnknownRecord | null {
  try {
    return typeof value === 'object' && value !== null && !Array.isArray(value)
      ? value as UnknownRecord
      : null;
  } catch {
    return null;
  }
}

/**
 * API results are typed at the service boundary, but the browser still has to
 * treat them as untrusted data.  In particular, a test adapter or a malformed
 * JSON decoder can hand us a Proxy whose getter throws.  Every field read in
 * this component goes through this small boundary before it is used.
 */
function readValue(record: UnknownRecord, key: string): SafeRead {
  try {
    return { ok: true, value: record[key] };
  } catch {
    return { ok: false, value: undefined };
  }
}

function safeText(value: unknown, maxLength: number): string | null {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  return trimmed.length > 0 && trimmed.length <= maxLength ? trimmed : null;
}

function safePositiveId(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0 ? value : null;
}

function safeNullablePositiveId(value: unknown): number | null | undefined {
  if (value === null) return null;
  const id = safePositiveId(value);
  return id === null ? undefined : id;
}

function readArray(value: unknown, maxLength: number): unknown[] | null {
  try {
    if (!Array.isArray(value) || !Number.isSafeInteger(value.length) || value.length < 0 || value.length > maxLength) {
      return null;
    }
    const result: unknown[] = [];
    for (let index = 0; index < value.length; index += 1) {
      if (!(index in value)) return null;
      result.push(value[index]);
    }
    return result;
  } catch {
    return null;
  }
}

function normalizeSourceStates(value: unknown): InterviewStorySourceState[] | null {
  const values = readArray(value, MAX_SOURCE_STATES);
  if (values === null) return null;
  const states: InterviewStorySourceState[] = [];
  for (const value of values) {
    const record = recordOf(value);
    if (!record) return null;
    const sourceKindRead = readValue(record, 'source_kind');
    const sourceIdRead = readValue(record, 'source_stable_id');
    const sourceVersionRead = readValue(record, 'source_version_or_snapshot');
    const stateRead = readValue(record, 'state');
    if (!sourceKindRead.ok || !sourceIdRead.ok || !sourceVersionRead.ok || !stateRead.ok) return null;
    const sourceKind = safeText(sourceKindRead.value, 128);
    const sourceStableId = safeText(sourceIdRead.value, 512);
    const sourceVersion = safeText(sourceVersionRead.value, 512);
    const state = safeText(stateRead.value, 64);
    if (!sourceKind || !STORY_SOURCE_KINDS.has(sourceKind) || !sourceStableId || !sourceVersion || !state || !SOURCE_STATE_NAMES.has(state)) {
      return null;
    }
    states.push(Object.freeze({
      source_kind: sourceKind as InterviewStorySourceState['source_kind'],
      source_stable_id: sourceStableId,
      source_version_or_snapshot: sourceVersion,
      // The public type predates the defensive source states.  Keep the
      // untrusted value out of the renderer while retaining the truthful name
      // for the warning projector below.
      state: state as InterviewStorySourceState['state'],
    }));
  }
  return Object.freeze(states) as unknown as InterviewStorySourceState[];
}

function normalizeEvidenceLinks(value: unknown): InterviewStoryEvidenceLink[] | null {
  const values = readArray(value, MAX_EVIDENCE_LINKS);
  if (values === null) return null;
  const links: InterviewStoryEvidenceLink[] = [];
  for (const value of values) {
    const record = recordOf(value);
    if (!record) return null;
    const targetKindRead = readValue(record, 'target_kind');
    const targetIdRead = readValue(record, 'target_id');
    const sourceKindRead = readValue(record, 'source_kind');
    const sourceIdRead = readValue(record, 'source_stable_id');
    const sourceVersionRead = readValue(record, 'source_version_or_snapshot');
    const pathRead = readValue(record, 'source_path');
    const excerptRead = readValue(record, 'excerpt');
    const locationRead = readValue(record, 'text_location');
    if (!targetKindRead.ok || !targetIdRead.ok || !sourceKindRead.ok || !sourceIdRead.ok
      || !sourceVersionRead.ok || !pathRead.ok || !excerptRead.ok || !locationRead.ok) return null;
    const targetKind = safeText(targetKindRead.value, 64);
    const targetId = safeText(targetIdRead.value, 256);
    const sourceKind = safeText(sourceKindRead.value, 128);
    const sourceStableId = safeText(sourceIdRead.value, 512);
    const sourceVersion = safeText(sourceVersionRead.value, 512);
    const sourcePath = safeText(pathRead.value, 1024);
    const excerpt = safeText(excerptRead.value, 4_000);
    const textLocation = locationRead.value === undefined || locationRead.value === null
      ? ''
      : safeText(locationRead.value, 256);
    if (!targetKind || !['title', 'block', 'capability_label', 'applicable_question'].includes(targetKind)
      || !targetId || !sourceKind || !STORY_SOURCE_KINDS.has(sourceKind)
      || !sourceStableId || !sourceVersion || !sourcePath || !excerpt || textLocation === null) return null;
    links.push(Object.freeze({
      target_kind: targetKind as InterviewStoryTargetKind,
      target_id: targetId,
      source_kind: sourceKind as InterviewStoryEvidenceLink['source_kind'],
      source_stable_id: sourceStableId,
      source_version_or_snapshot: sourceVersion,
      source_path: sourcePath,
      excerpt,
      ...(textLocation ? { text_location: textLocation } : {}),
    }));
  }
  return Object.freeze(links) as unknown as InterviewStoryEvidenceLink[];
}

function normalizeContent(value: unknown): InterviewStoryVersion['content'] | null {
  const record = recordOf(value);
  if (!record) return null;
  const titleRead = readValue(record, 'title');
  const blocksRead = readValue(record, 'blocks');
  const labelsRead = readValue(record, 'capability_labels');
  const questionsRead = readValue(record, 'applicable_questions');
  const gapsRead = readValue(record, 'fact_gap_codes');
  if (!titleRead.ok || !blocksRead.ok || !labelsRead.ok || !questionsRead.ok || !gapsRead.ok) return null;

  const titleRecord = recordOf(titleRead.value);
  if (!titleRecord) return null;
  const titleIdRead = readValue(titleRecord, 'id');
  const titleTextRead = readValue(titleRecord, 'text');
  if (!titleIdRead.ok || !titleTextRead.ok || titleIdRead.value !== 'title') return null;
  const titleText = safeText(titleTextRead.value, 200);
  if (!titleText) return null;

  const blockValues = readArray(blocksRead.value, MAX_BLOCKS);
  const labelValues = readArray(labelsRead.value, MAX_SHORT_ITEMS);
  const questionValues = readArray(questionsRead.value, MAX_SHORT_ITEMS);
  const gapValues = readArray(gapsRead.value, MAX_SHORT_ITEMS);
  if (blockValues === null || labelValues === null || questionValues === null || gapValues === null) return null;

  const blockIds = new Set<string>();
  const blocks: InterviewStoryVersion['content']['blocks'] = [];
  for (const value of blockValues) {
    const block = recordOf(value);
    if (!block) return null;
    const idRead = readValue(block, 'id');
    const kindRead = readValue(block, 'kind');
    const textRead = readValue(block, 'text');
    const factModeRead = readValue(block, 'fact_mode');
    if (!idRead.ok || !kindRead.ok || !textRead.ok || !factModeRead.ok) return null;
    const id = safeText(idRead.value, 128);
    const kind = safeText(kindRead.value, 32);
    const text = safeText(textRead.value, 4_000);
    const factMode = safeText(factModeRead.value, 64);
    if (!id || blockIds.has(id) || !kind || !['situation', 'task', 'action', 'result', 'reflection'].includes(kind)
      || !text || !factMode || !['evidence_backed', 'user_view'].includes(factMode)) return null;
    blockIds.add(id);
    blocks.push(Object.freeze({
      id,
      kind: kind as InterviewStoryVersion['content']['blocks'][number]['kind'],
      text,
      fact_mode: factMode as InterviewStoryVersion['content']['blocks'][number]['fact_mode'],
    }));
  }

  const normalizeShortItems = (values: unknown[], maxTextLength: number): Array<{ id: string; text: string }> | null => {
    const ids = new Set<string>();
    const items: Array<{ id: string; text: string }> = [];
    for (const value of values) {
      const item = recordOf(value);
      if (!item) return null;
      const idRead = readValue(item, 'id');
      const textRead = readValue(item, 'text');
      if (!idRead.ok || !textRead.ok) return null;
      const id = safeText(idRead.value, 128);
      const text = safeText(textRead.value, maxTextLength);
      if (!id || ids.has(id) || !text) return null;
      ids.add(id);
      items.push(Object.freeze({ id, text }));
    }
    return items;
  };

  const capabilityLabels = normalizeShortItems(labelValues, 300);
  const applicableQuestions = normalizeShortItems(questionValues, 300);
  if (!capabilityLabels || !applicableQuestions) return null;
  const factGapCodes: string[] = [];
  for (const value of gapValues) {
    const code = safeText(value, 128);
    if (!code || code !== 'missing_result' || factGapCodes.includes(code)) return null;
    factGapCodes.push(code);
  }

  return Object.freeze({
    title: Object.freeze({ id: 'title' as const, text: titleText }),
    blocks: Object.freeze(blocks),
    capability_labels: Object.freeze(capabilityLabels),
    applicable_questions: Object.freeze(applicableQuestions),
    fact_gap_codes: Object.freeze(factGapCodes),
  }) as unknown as InterviewStoryVersion['content'];
}

function normalizeAssertions(value: unknown): InterviewStoryVersion['assertions'] | null {
  const values = readArray(value, MAX_ASSERTIONS);
  if (values === null) return null;
  const ids = new Set<number>();
  const assertions: InterviewStoryVersion['assertions'] = [];
  for (const value of values) {
    const record = recordOf(value);
    if (!record) return null;
    const idRead = readValue(record, 'id');
    const statementRead = readValue(record, 'statement');
    const frozenRead = readValue(record, 'frozen');
    if (!idRead.ok || !statementRead.ok || !frozenRead.ok) return null;
    const id = safePositiveId(idRead.value);
    const statement = safeText(statementRead.value, 4_000);
    if (id === null || ids.has(id) || !statement || frozenRead.value !== true) return null;
    ids.add(id);
    assertions.push(Object.freeze({ id, statement, frozen: true as const }));
  }
  return Object.freeze(assertions) as unknown as InterviewStoryVersion['assertions'];
}

export interface NormalizedInterviewStoryVersionResult {
  readonly value: InterviewStoryVersion | null;
  readonly sourceIssue: 'changed' | 'missing' | 'error' | 'deleted' | 'unknown' | null;
}

function sourceIssueForStates(states: readonly InterviewStorySourceState[]): NormalizedInterviewStoryVersionResult['sourceIssue'] {
  const names = states.map((item) => String(item.state));
  for (const state of ['error', 'deleted', 'unknown', 'missing', 'changed'] as const) {
    if (names.includes(state)) return state;
  }
  return null;
}

function sourceIssueHint(value: unknown): NormalizedInterviewStoryVersionResult['sourceIssue'] {
  const values = readArray(value, MAX_SOURCE_STATES);
  if (values === null) return null;
  const states: InterviewStorySourceState[] = [];
  for (const item of values) {
    const record = recordOf(item);
    if (!record) continue;
    const stateRead = readValue(record, 'state');
    if (!stateRead.ok) continue;
    const state = safeText(stateRead.value, 64);
    if (!state || !SOURCE_STATE_NAMES.has(state)) continue;
    states.push({
      source_kind: 'user_assertion',
      source_stable_id: 'unavailable',
      source_version_or_snapshot: 'unavailable',
      state: state as InterviewStorySourceState['state'],
    });
  }
  return sourceIssueForStates(states);
}

function versionSourceWarning(issue: NonNullable<NormalizedInterviewStoryVersionResult['sourceIssue']>): string {
  switch (issue) {
    case 'changed': return '当前来源已变化，历史版本暂不可安全展示。';
    case 'missing': return '部分来源已缺失，历史版本暂不可安全展示。';
    case 'error': return '历史来源暂时不可用，当前版本无法展示。';
    case 'deleted': return '历史来源已删除，当前版本无法展示。';
    case 'unknown': return '历史来源状态未知，当前版本无法展示。';
  }
}

export function normalizeInterviewStoryVersion(input: unknown, expectedStoryId?: number, expectedVersionId?: number): NormalizedInterviewStoryVersionResult {
  const record = recordOf(input);
  if (!record) return { value: null, sourceIssue: null };
  const idRead = readValue(record, 'id');
  const storyIdRead = readValue(record, 'story_id');
  const numberRead = readValue(record, 'version_number');
  const originRead = readValue(record, 'origin_kind');
  const confirmedRead = readValue(record, 'confirmed_at');
  const fingerprintRead = readValue(record, 'source_fingerprint');
  const contentRead = readValue(record, 'content');
  const evidenceRead = readValue(record, 'evidence_links');
  const assertionsRead = readValue(record, 'assertions');
  const statesRead = readValue(record, 'source_states');
  if (!idRead.ok || !storyIdRead.ok || !numberRead.ok || !originRead.ok || !confirmedRead.ok
    || !fingerprintRead.ok || !contentRead.ok || !evidenceRead.ok || !assertionsRead.ok || !statesRead.ok) {
    return { value: null, sourceIssue: null };
  }
  const id = safePositiveId(idRead.value);
  const storyId = storyIdRead.value === undefined ? null : safePositiveId(storyIdRead.value);
  const versionNumber = safePositiveId(numberRead.value);
  const originKind = safeText(originRead.value, 32);
  const confirmedAt = confirmedRead.value === null ? null : safeText(confirmedRead.value, 128);
  const sourceFingerprint = safeText(fingerprintRead.value, 512);
  if (id === null || (expectedVersionId !== undefined && id !== expectedVersionId)
    || (storyIdRead.value !== undefined && storyId === null)
    || (expectedStoryId !== undefined && (storyId === null || storyId !== expectedStoryId))
    || versionNumber === null || !originKind || !['manual', 'proposal'].includes(originKind)
    || (confirmedRead.value !== null && !confirmedAt) || !sourceFingerprint) {
    return { value: null, sourceIssue: null };
  }
  const content = normalizeContent(contentRead.value);
  const evidenceLinks = normalizeEvidenceLinks(evidenceRead.value);
  const assertions = normalizeAssertions(assertionsRead.value);
  const sourceStates = normalizeSourceStates(statesRead.value);
  if (!content || !evidenceLinks || !assertions || !sourceStates) {
    return { value: null, sourceIssue: sourceIssueHint(statesRead.value) };
  }
  const value = Object.freeze({
    id,
    version_number: versionNumber,
    origin_kind: originKind as InterviewStoryVersion['origin_kind'],
    confirmed_at: confirmedAt,
    source_fingerprint: sourceFingerprint,
    content,
    evidence_links: evidenceLinks,
    assertions,
    source_states: sourceStates,
  }) as unknown as InterviewStoryVersion;
  return { value, sourceIssue: sourceIssueForStates(sourceStates) };
}

type VersionSummary = Pick<InterviewStoryVersion, 'id' | 'version_number' | 'origin_kind' | 'confirmed_at' | 'source_fingerprint'>;

function normalizeVersionSummary(input: unknown, expectedStoryId?: number): VersionSummary | null {
  const record = recordOf(input);
  if (!record) return null;
  const idRead = readValue(record, 'id');
  const storyIdRead = readValue(record, 'story_id');
  const numberRead = readValue(record, 'version_number');
  const originRead = readValue(record, 'origin_kind');
  const confirmedRead = readValue(record, 'confirmed_at');
  const fingerprintRead = readValue(record, 'source_fingerprint');
  if (!idRead.ok || !storyIdRead.ok || !numberRead.ok || !originRead.ok || !confirmedRead.ok || !fingerprintRead.ok) return null;
  const id = safePositiveId(idRead.value);
  const storyId = storyIdRead.value === undefined ? null : safePositiveId(storyIdRead.value);
  const versionNumber = safePositiveId(numberRead.value);
  const originKind = safeText(originRead.value, 32);
  const confirmedAt = confirmedRead.value === null ? null : safeText(confirmedRead.value, 128);
  const sourceFingerprint = safeText(fingerprintRead.value, 512);
  if (id === null || (storyIdRead.value !== undefined && storyId === null)
    || (expectedStoryId !== undefined && storyId !== null && storyId !== expectedStoryId)
    || versionNumber === null || !originKind || !['manual', 'proposal'].includes(originKind)
    || (confirmedRead.value !== null && !confirmedAt) || !sourceFingerprint) return null;
  return Object.freeze({
    id,
    version_number: versionNumber,
    origin_kind: originKind as VersionSummary['origin_kind'],
    confirmed_at: confirmedAt,
    source_fingerprint: sourceFingerprint,
  });
}

export interface NormalizedInterviewStoryVersionsResult {
  readonly values: readonly VersionSummary[];
  readonly state: 'ready' | 'partial' | 'unavailable';
}

export function normalizeInterviewStoryVersions(input: unknown, expectedStoryId?: number): NormalizedInterviewStoryVersionsResult {
  const values = readArray(input, MAX_EVIDENCE_LINKS);
  if (values === null) return { values: Object.freeze([]), state: 'unavailable' };
  let invalid = false;
  const byId = new Map<number, VersionSummary>();
  for (const value of values) {
    const normalized = normalizeVersionSummary(value, expectedStoryId);
    if (!normalized) {
      invalid = true;
      continue;
    }
    const previous = byId.get(normalized.id);
    if (previous) {
      const previousKey = `${previous.version_number}:${previous.origin_kind}:${previous.confirmed_at ?? ''}:${previous.source_fingerprint}`;
      const nextKey = `${normalized.version_number}:${normalized.origin_kind}:${normalized.confirmed_at ?? ''}:${normalized.source_fingerprint}`;
      if (nextKey < previousKey) byId.set(normalized.id, normalized);
      if (nextKey !== previousKey) invalid = true;
    } else {
      byId.set(normalized.id, normalized);
    }
  }
  const result = [...byId.values()].sort((left, right) => right.version_number - left.version_number || right.id - left.id);
  return {
    values: Object.freeze(result),
    state: invalid ? (result.length > 0 ? 'partial' : 'unavailable') : 'ready',
  };
}

interface NormalizedStoryResult {
  readonly value: InterviewStory | null;
  readonly invalidVersion: boolean;
}

function normalizeInterviewStory(input: unknown, options: { detail: boolean; expectedStoryId?: number }): NormalizedStoryResult {
  const record = recordOf(input);
  if (!record) return { value: null, invalidVersion: false };
  const idRead = readValue(record, 'id');
  const titleRead = readValue(record, 'title');
  const statusRead = readValue(record, 'status');
  const currentRead = readValue(record, 'current_version_id');
  const revisionRead = readValue(record, 'story_revision');
  const numberRead = readValue(record, 'version_number');
  const statesRead = readValue(record, 'source_states');
  if (!idRead.ok || !titleRead.ok || !statusRead.ok || !currentRead.ok || !revisionRead.ok || !numberRead.ok || !statesRead.ok) {
    return { value: null, invalidVersion: false };
  }
  const id = safePositiveId(idRead.value);
  const title = safeText(titleRead.value, 200);
  const status = safeText(statusRead.value, 32);
  const currentVersionId = safeNullablePositiveId(currentRead.value);
  const storyRevision = safePositiveId(revisionRead.value);
  const versionNumber = safeNullablePositiveId(numberRead.value);
  const sourceStates = normalizeSourceStates(statesRead.value);
  if (id === null || (options.expectedStoryId !== undefined && id !== options.expectedStoryId)
    || !title || !status || !['active', 'archived'].includes(status)
    || currentVersionId === undefined || storyRevision === null || versionNumber === undefined || !sourceStates) {
    return { value: null, invalidVersion: false };
  }

  let version: InterviewStoryVersion | null | undefined;
  let invalidVersion = false;
  if (options.detail) {
    const versionRead = readValue(record, 'version');
    if (!versionRead.ok) {
      invalidVersion = true;
    } else if (versionRead.value !== undefined && versionRead.value !== null) {
      const normalizedVersion = normalizeInterviewStoryVersion(versionRead.value, id, currentVersionId ?? undefined);
      version = normalizedVersion.value;
      invalidVersion = !normalizedVersion.value;
    } else {
      version = null;
    }
  }
  const normalized = {
    id,
    title,
    status: status as InterviewStory['status'],
    current_version_id: currentVersionId,
    story_revision: storyRevision,
    version_number: versionNumber,
    source_states: sourceStates,
    ...(options.detail ? { version } : {}),
  } as InterviewStory;
  return { value: Object.freeze(normalized), invalidVersion };
}

function storyCanonicalKey(story: InterviewStory): string {
  return JSON.stringify({
    title: story.title,
    status: story.status,
    currentVersionId: story.current_version_id,
    storyRevision: story.story_revision,
    versionNumber: story.version_number,
    sourceStates: story.source_states.map((source) => ({
      kind: source.source_kind,
      identity: source.source_stable_id,
      version: source.source_version_or_snapshot,
      state: source.state,
    })),
  });
}

export function normalizeInterviewStoryList(input: unknown): { values: readonly InterviewStory[]; state: 'ready' | 'partial' | 'unavailable' } {
  const values = readArray(input, MAX_EVIDENCE_LINKS);
  if (values === null) return { values: Object.freeze([]), state: 'unavailable' };
  let invalid = false;
  const stories = new Map<number, { readonly value: InterviewStory; readonly key: string }>();
  for (const value of values) {
    const normalized = normalizeInterviewStory(value, { detail: false }).value;
    if (!normalized) {
      invalid = true;
      continue;
    }
    const key = storyCanonicalKey(normalized);
    const previous = stories.get(normalized.id);
    if (previous) {
      invalid = true;
      if (key < previous.key) stories.set(normalized.id, { value: normalized, key });
      continue;
    }
    stories.set(normalized.id, { value: normalized, key });
  }
  const result = [...stories.values()]
    .map((candidate) => candidate.value)
    .sort((left, right) => left.id - right.id || left.title.localeCompare(right.title));
  return {
    values: Object.freeze(result),
    state: invalid ? (result.length > 0 ? 'partial' : 'unavailable') : 'ready',
  };
}

export interface InterviewStoryOpenDraft {
  entrypoint: 'ui' | 'pilot';
  reviewNoteId?: number;
  targetStoryId?: number;
  expectedCurrentVersionId?: number;
  expectedStoryRevision?: number;
}

interface Props {
  onOpenDraft: (input: InterviewStoryOpenDraft) => void;
  onBack?: () => void;
}

function sourceStateLabel(story: InterviewStory): string | null {
  const states = safeSourceStates(story);
  if (states.some((state) => ['error', 'deleted', 'unknown'].includes(state))) {
    return '部分来源暂时不可用';
  }
  if (states.includes('changed')) return '来源已变化';
  if (states.includes('missing')) return '部分来源缺失';
  if (states.length > 0) return '保留冻结来源';
  return null;
}

function hasFrozenUserAssertion(story: InterviewStory): boolean {
  return safeSourceStates(story).includes('frozen_user_assertion');
}

function safeSourceStates(story: InterviewStory): string[] {
  const states: string[] = [];
  for (const sourceState of story.source_states) states.push(String(sourceState.state));
  return states;
}

function linksForTarget(
  version: InterviewStoryVersion,
  targetKind: InterviewStoryTargetKind,
  targetId: string,
): InterviewStoryEvidenceLink[] {
  return version.evidence_links.filter((link) => link.target_kind === targetKind && link.target_id === targetId);
}

function EvidenceDisclosure({
  links,
  target,
}: {
  links: InterviewStoryEvidenceLink[];
  target: string;
}) {
  if (links.length === 0) return null;
  return (
    <details className={styles.evidenceDisclosure} data-evidence-target={target}>
      <summary>{links.length} 条冻结证据</summary>
      <div className={styles.evidenceList}>
        {links.map((link, index) => (
          <div
            key={`${link.target_kind}-${link.target_id}-${link.source_kind}-${link.source_stable_id}-${link.source_version_or_snapshot}-${link.source_path}-${index}`}
            className={styles.evidenceItem}
          >
            <span className={styles.evidenceSource}>{SOURCE_LABELS[link.source_kind]}</span>
            <span className={styles.evidenceExcerpt}>{link.excerpt}</span>
            <code className={styles.evidencePath}>{link.source_path}</code>
          </div>
        ))}
      </div>
    </details>
  );
}

function VersionSection({ title, children, links, target }: {
  title: string;
  children: ReactNode;
  links?: InterviewStoryEvidenceLink[];
  target?: string;
}) {
  return (
    <section className={styles.versionSection}>
      <div className={styles.versionSectionHeader}>
        <span className={styles.versionSectionLabel}>{title}</span>
        {links && target ? <EvidenceDisclosure links={links} target={target} /> : null}
      </div>
      <div className={styles.versionSectionBody}>{children}</div>
    </section>
  );
}

export default function InterviewStoryLibraryView({ onOpenDraft, onBack }: Props) {
  const [stories, setStories] = useState<InterviewStory[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [storyListState, setStoryListState] = useState<'ready' | 'partial' | 'unavailable'>('ready');
  const [status, setStatus] = useState<'active' | 'archived'>('active');
  const [query, setQuery] = useState('');
  const [selectedStory, setSelectedStory] = useState<InterviewStory | null>(null);
  const [versions, setVersions] = useState<readonly VersionSummary[]>([]);
  const [selectedVersion, setSelectedVersion] = useState<InterviewStoryVersion | null>(null);
  const [historyState, setHistoryState] = useState<'idle' | 'loading' | 'ready' | 'partial' | 'unavailable' | 'error'>('idle');
  const [historyMessage, setHistoryMessage] = useState<string | null>(null);
  const storyListRequestGeneration = useRef(0);
  const historyRequestGeneration = useRef(0);

  const load = (nextStatus = status, nextQuery = query) => {
    const generation = ++storyListRequestGeneration.current;
    setLoading(true);
    setError(false);
    setStoryListState('ready');
    setStories([]);
    void Promise.resolve()
      .then(() => listInterviewStories(nextStatus, nextQuery))
      .then((raw) => {
        if (generation !== storyListRequestGeneration.current) return;
        const normalized = normalizeInterviewStoryList(raw);
        setStories([...normalized.values]);
        setStoryListState(normalized.state);
      })
      .catch(() => {
        if (generation !== storyListRequestGeneration.current) return;
        setStories([]);
        setStoryListState('unavailable');
        setError(true);
      })
      .finally(() => {
        if (generation === storyListRequestGeneration.current) setLoading(false);
      });
  };

  useEffect(() => { load(status, query); }, [status, query]);

  let storyProjection: ReturnType<typeof projectExperienceMaterials>;
  try {
    storyProjection = projectExperienceMaterials({ stories });
  } catch {
    storyProjection = {
      items: Object.freeze([]),
      state: 'unavailable',
      hasUnavailable: true,
      unavailable: Object.freeze([]),
    };
  }
  const visibleStories = storyProjection.items
    .filter((item) => item.kind === 'experience_story')
    .map((item) => item.record as unknown as InterviewStory);
  const effectiveStoryListState = storyProjection.state === 'unavailable' || storyListState === 'unavailable'
    ? 'unavailable'
    : storyProjection.state === 'partial' || storyListState === 'partial'
      ? 'partial'
      : storyProjection.state;

  const toggleArchive = async (story: InterviewStory) => {
    const generation = storyListRequestGeneration.current;
    try {
      const updated = story.status === 'active'
        ? await archiveInterviewStory(story.id, story.story_revision)
        : await restoreInterviewStory(story.id, story.story_revision);
      if (generation !== storyListRequestGeneration.current) return;
      const normalized = normalizeInterviewStory(updated, { detail: false, expectedStoryId: story.id }).value;
      if (!normalized) {
        setStoryListState('partial');
        return;
      }
      setStories((current) => current.map((item) => item.id === normalized.id ? normalized : item));
    } catch {
      if (generation !== storyListRequestGeneration.current) return;
      setError(true);
    }
  };

  const openStory = async (storyId: number) => {
    const generation = ++historyRequestGeneration.current;
    setHistoryState('loading');
    setHistoryMessage(null);
    setSelectedStory(null);
    setSelectedVersion(null);
    setVersions([]);
    try {
      const [rawStory, rawHistory] = await Promise.all([getInterviewStory(storyId), listInterviewStoryVersions(storyId)]);
      if (generation !== historyRequestGeneration.current) return;
      const normalizedStory = normalizeInterviewStory(rawStory, { detail: true, expectedStoryId: storyId });
      const normalizedHistory = normalizeInterviewStoryVersions(rawHistory, storyId);
      if (!normalizedStory.value) {
        setHistoryState('unavailable');
        setHistoryMessage('故事历史暂时不可用，请稍后重试。');
        return;
      }
      setSelectedStory(normalizedStory.value);
      setVersions(normalizedHistory.values);
      const unusableHistory = normalizedHistory.state !== 'ready' || normalizedStory.invalidVersion;
      if (unusableHistory) {
        setSelectedVersion(null);
        setHistoryState(normalizedHistory.state === 'unavailable' || normalizedStory.invalidVersion ? 'unavailable' : 'partial');
        setHistoryMessage(normalizedStory.invalidVersion
          ? '当前版本暂时不可用，无法安全展示。'
          : normalizedHistory.state === 'unavailable'
            ? '版本历史暂时不可用，请稍后重试。'
            : '部分版本暂时不可用，已隐藏不完整内容。');
      } else {
        const selected = normalizedStory.value.version ?? null;
        setSelectedVersion(selected);
        const sourceIssue = selected ? sourceIssueForStates(selected.source_states) : null;
        setHistoryState(selected && !sourceIssue ? 'ready' : 'unavailable');
        setHistoryMessage(selected
          ? (sourceIssue ? versionSourceWarning(sourceIssue) : null)
          : '当前版本暂时不可用，无法安全展示。');
      }
    } catch {
      if (generation !== historyRequestGeneration.current) return;
      setSelectedStory(null);
      setSelectedVersion(null);
      setVersions([]);
      setHistoryState('error');
      setHistoryMessage('故事历史暂时无法加载，请稍后重试。');
    }
  };

  const openVersion = async (storyId: number, versionId: number) => {
    const generation = ++historyRequestGeneration.current;
    setSelectedVersion(null);
    setHistoryState('loading');
    setHistoryMessage(null);
    try {
      const rawVersion = await getInterviewStoryVersion(storyId, versionId);
      if (generation !== historyRequestGeneration.current) return;
      const normalized = normalizeInterviewStoryVersion(rawVersion, storyId, versionId);
      if (!normalized.value) {
        setHistoryState('unavailable');
        setHistoryMessage(normalized.sourceIssue
          ? versionSourceWarning(normalized.sourceIssue)
          : '当前版本暂时不可用，无法安全展示。');
        return;
      }
      setSelectedVersion(normalized.value);
      if (normalized.sourceIssue) {
        setHistoryState('unavailable');
        setHistoryMessage(versionSourceWarning(normalized.sourceIssue));
      } else {
        setHistoryState('ready');
        setHistoryMessage(null);
      }
    } catch {
      if (generation !== historyRequestGeneration.current) return;
      setSelectedVersion(null);
      setHistoryState('error');
      setHistoryMessage('当前版本暂时无法加载，请稍后重试。');
    }
  };

  const closeHistory = () => {
    historyRequestGeneration.current += 1;
    setSelectedStory(null);
    setSelectedVersion(null);
    setVersions([]);
    setHistoryState('idle');
    setHistoryMessage(null);
  };

  const uniqueSourceCount = selectedVersion
    ? new Set(selectedVersion.evidence_links.map((link) => `${link.source_kind}:${link.source_stable_id}:${link.source_version_or_snapshot}`)).size
    : 0;

  return (
    <section className={styles.library} aria-label="面试故事库">
      <header className={styles.pageHeader}>
        <div className={styles.pageHeading}>
          <Title level={3} className={styles.pageTitle}>面试故事库</Title>
          <Paragraph type="secondary" className={styles.pageDescription}>
            只在你选择原始证据并确认后保存；故事版本会保留当时的来源，不会写入知识库或自动用于面试。
          </Paragraph>
        </div>
        <Space wrap className={styles.headerActions}>
          {onBack ? <Button onClick={onBack}>返回面试</Button> : null}
          <Button onClick={() => onOpenDraft({ entrypoint: 'ui', reviewNoteId: undefined })}>新建故事</Button>
        </Space>
      </header>

      <div className={styles.toolbar}>
        <div className={styles.statusSwitch} role="group" aria-label="故事状态">
          <button type="button" aria-pressed={status === 'active'} onClick={() => setStatus('active')}>使用中</button>
          <button type="button" aria-pressed={status === 'archived'} onClick={() => setStatus('archived')}>已归档</button>
        </div>
        <Input.Search
          aria-label="搜索面试故事"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onSearch={(value) => setQuery(value)}
          placeholder="搜索故事标题"
          className={styles.search}
        />
      </div>

      {loading ? <Spin aria-label="正在加载面试故事" /> : null}
      {error ? <Alert type="error" showIcon message="故事库暂时无法加载，请稍后重试。" action={<Button size="small" onClick={() => load()}>重试</Button>} /> : null}
      {!loading && !error && effectiveStoryListState === 'unavailable' ? (
        <Alert type="warning" showIcon message="故事来源暂时不可用，请稍后重试。" />
      ) : null}
      {!loading && !error && effectiveStoryListState === 'partial' ? (
        <Alert type="warning" showIcon message="部分故事暂时不可用，已展示可用故事。" />
      ) : null}
      {!loading && !error && effectiveStoryListState !== 'unavailable' && visibleStories.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有已确认的面试故事" />
      ) : null}
      {!loading && !error && visibleStories.length > 0 ? (
        <List
          className={styles.storyList}
          dataSource={visibleStories}
          renderItem={(story) => {
            const state = sourceStateLabel(story);
            const assertion = hasFrozenUserAssertion(story);
            return (
              <List.Item className={styles.storyItem}>
                <div className={styles.storySummary}>
                  <div className={styles.storyTitleRow}>
                    <Text strong className={styles.storyTitle}>{story.title}</Text>
                    <Tag color={story.status === 'active' ? 'purple' : 'default'}>{story.status === 'active' ? '使用中' : '已归档'}</Tag>
                  </div>
                  <div className={styles.storyMeta}>
                    <span>版本 {story.version_number ?? 0}</span>
                    {state ? <Tag color={state === '来源已变化' || state === '部分来源暂时不可用' ? 'warning' : 'default'}>{state}</Tag> : null}
                    {assertion ? <span>包含用户确认陈述</span> : null}
                  </div>
                </div>
                <div className={styles.storyActions}>
                  <Button onClick={() => void openStory(story.id)}>查看版本</Button>
                  <Button type="text" onClick={() => void toggleArchive(story)}>
                    {story.status === 'active' ? '归档' : '恢复'}
                  </Button>
                </div>
              </List.Item>
            );
          }}
        />
      ) : null}

      {!selectedStory && historyState !== 'idle' ? (
        <Alert
          type={historyState === 'error' || historyState === 'unavailable' ? 'warning' : 'info'}
          showIcon
          message={historyState === 'loading' ? '正在加载故事版本…' : historyMessage ?? '故事历史暂时不可用，请稍后重试。'}
        />
      ) : null}

      {selectedStory && historyState !== 'idle' ? (
        <section className={styles.history} aria-label="故事版本历史">
          <div className={styles.historyHeader}>
            <div>
              <Title level={4} className={styles.historyTitle}>
                {selectedVersion && historyState === 'ready' ? `版本 ${selectedVersion.version_number} · 已确认历史` : '故事版本历史'}
              </Title>
              {selectedVersion && historyState === 'ready' ? (
                <div className={styles.historyMeta}>
                  <Tag>{selectedVersion.origin_kind === 'manual' ? '手动保存' : 'AI 建议后确认'}</Tag>
                  <span>{uniqueSourceCount} 个冻结来源 · {selectedVersion.evidence_links.length} 条证据引用</span>
                </div>
              ) : null}
            </div>
            <Space wrap className={styles.historyActions}>
              {selectedStory.status === 'active' && selectedStory.current_version_id ? (
                <Button onClick={() => onOpenDraft({
                  entrypoint: 'ui',
                  targetStoryId: selectedStory.id,
                  expectedCurrentVersionId: selectedStory.current_version_id ?? undefined,
                  expectedStoryRevision: selectedStory.story_revision,
                })}>基于此故事新建版本</Button>
              ) : null}
              <Button onClick={closeHistory}>关闭历史</Button>
            </Space>
          </div>

          <div className={styles.versionRail} aria-label="故事版本选择">
            {versions.map((version) => (
              <button
                key={version.id}
                type="button"
                aria-pressed={selectedVersion?.id === version.id}
                onClick={() => void openVersion(selectedStory.id, version.id)}
              >
                查看版本 {version.version_number}
              </button>
            ))}
          </div>

          {historyState === 'loading' ? <Alert type="info" showIcon message="正在加载故事版本…" /> : null}
          {historyState === 'error' ? (
            <Alert
              type="error"
              showIcon
              message={historyMessage ?? '故事历史暂时无法加载，请稍后重试。'}
              action={<Button size="small" onClick={() => void openStory(selectedStory.id)}>重试</Button>}
            />
          ) : null}
          {(historyState === 'partial' || historyState === 'unavailable') ? (
            <Alert
              type="warning"
              showIcon
              message={historyMessage ?? (historyState === 'partial' ? '部分版本暂时不可用，已隐藏不完整内容。' : '当前版本暂时不可用，无法安全展示。')}
            />
          ) : null}
          {selectedVersion && historyState === 'ready' ? (
            <>
              <Alert type="info" showIcon message="以下内容来自已确认的冻结版本。" />
              <div className={styles.versionContent} data-testid="story-version-content">
                <div className={styles.evidenceSummary}>
                  <span>{uniqueSourceCount} 个冻结来源</span>
                  <strong>{selectedVersion.evidence_links.length} 条证据引用</strong>
                </div>
                <VersionSection
                  title="故事标题"
                  links={linksForTarget(selectedVersion, 'title', selectedVersion.content.title.id)}
                  target="title:title"
                >
                  <h3 className={styles.storyHeadline}>{selectedVersion.content.title.text}</h3>
                </VersionSection>

                {selectedVersion.content.applicable_questions.length > 0 ? (
                  <VersionSection title="适用问题">
                    <div className={styles.compactList}>
                      {selectedVersion.content.applicable_questions.map((question) => (
                        <div key={question.id}>
                          <span>{question.text}</span>
                          <EvidenceDisclosure links={linksForTarget(selectedVersion, 'applicable_question', question.id)} target={`applicable_question:${question.id}`} />
                        </div>
                      ))}
                    </div>
                  </VersionSection>
                ) : null}

                {selectedVersion.content.blocks.map((block) => (
                  <VersionSection
                    key={block.id}
                    title={BLOCK_LABELS[block.kind]}
                    links={linksForTarget(selectedVersion, 'block', block.id)}
                    target={`block:${block.id}`}
                  >
                    <p>{block.text}</p>
                  </VersionSection>
                ))}

                {selectedVersion.content.capability_labels.length > 0 ? (
                  <VersionSection title="能力标签">
                    <div className={styles.capabilityList}>
                      {selectedVersion.content.capability_labels.map((label) => (
                        <div key={label.id} className={styles.capabilityItem}>
                          <Tag>{label.text}</Tag>
                          <EvidenceDisclosure links={linksForTarget(selectedVersion, 'capability_label', label.id)} target={`capability_label:${label.id}`} />
                        </div>
                      ))}
                    </div>
                  </VersionSection>
                ) : null}
              </div>
            </>
          ) : null}
        </section>
      ) : null}
    </section>
  );
}
