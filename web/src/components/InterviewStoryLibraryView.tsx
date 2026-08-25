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
  InterviewStoryTargetKind,
  InterviewStoryVersion,
} from '@/types/interviewStory';
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
  if (story.source_states.some((item) => item.state === 'changed')) return '来源已变化';
  if (story.source_states.some((item) => item.state === 'missing')) return '部分来源缺失';
  if (story.source_states.length > 0) return '保留冻结来源';
  return null;
}

function hasFrozenUserAssertion(story: InterviewStory): boolean {
  return story.source_states.some((item) => item.state === 'frozen_user_assertion');
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
  const [status, setStatus] = useState<'active' | 'archived'>('active');
  const [query, setQuery] = useState('');
  const [selectedStory, setSelectedStory] = useState<InterviewStory | null>(null);
  const [versions, setVersions] = useState<Array<Pick<InterviewStoryVersion, 'id' | 'version_number' | 'origin_kind' | 'confirmed_at' | 'source_fingerprint'>>>([]);
  const [selectedVersion, setSelectedVersion] = useState<InterviewStoryVersion | null>(null);
  const historyRequestGeneration = useRef(0);

  const load = (nextStatus = status, nextQuery = query) => {
    setLoading(true);
    setError(false);
    void listInterviewStories(nextStatus, nextQuery).then(setStories).catch(() => setError(true)).finally(() => setLoading(false));
  };

  useEffect(() => { load(status, query); }, [status, query]);

  const toggleArchive = async (story: InterviewStory) => {
    try {
      const updated = story.status === 'active'
        ? await archiveInterviewStory(story.id, story.story_revision)
        : await restoreInterviewStory(story.id, story.story_revision);
      setStories((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch {
      setError(true);
    }
  };

  const openStory = async (storyId: number) => {
    const generation = ++historyRequestGeneration.current;
    try {
      const [story, history] = await Promise.all([getInterviewStory(storyId), listInterviewStoryVersions(storyId)]);
      if (generation !== historyRequestGeneration.current) return;
      setSelectedStory(story);
      setVersions(history);
      setSelectedVersion(story.version ?? null);
    } catch {
      setError(true);
    }
  };

  const openVersion = async (storyId: number, versionId: number) => {
    const generation = ++historyRequestGeneration.current;
    try {
      const version = await getInterviewStoryVersion(storyId, versionId);
      if (generation !== historyRequestGeneration.current) return;
      setSelectedVersion(version);
    } catch {
      setError(true);
    }
  };

  const closeHistory = () => {
    historyRequestGeneration.current += 1;
    setSelectedStory(null);
    setSelectedVersion(null);
    setVersions([]);
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
      {!loading && !error && stories.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有已确认的面试故事" /> : null}
      {!loading && !error && stories.length > 0 ? (
        <List
          className={styles.storyList}
          dataSource={stories}
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
                    {state ? <Tag color={state === '来源已变化' ? 'warning' : 'default'}>{state}</Tag> : null}
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

      {selectedStory && selectedVersion ? (
        <section className={styles.history} aria-label="故事版本历史">
          <div className={styles.historyHeader}>
            <div>
              <Title level={4} className={styles.historyTitle}>版本 {selectedVersion.version_number} · 已确认历史</Title>
              <div className={styles.historyMeta}>
                <Tag>{selectedVersion.origin_kind === 'manual' ? '手动保存' : 'AI 建议后确认'}</Tag>
                <span>{uniqueSourceCount} 个冻结来源 · {selectedVersion.evidence_links.length} 条证据引用</span>
              </div>
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
                aria-pressed={selectedVersion.id === version.id}
                onClick={() => void openVersion(selectedStory.id, version.id)}
              >
                查看版本 {version.version_number}
              </button>
            ))}
          </div>

          {selectedVersion.source_states.some((item) => item.state === 'changed' || item.state === 'missing') ? (
            <Alert type="warning" showIcon message="当前来源已变化，以下内容仍是当时确认的冻结版本。" />
          ) : <Alert type="info" showIcon message="以下内容来自已确认的冻结版本。" />}

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
        </section>
      ) : null}
    </section>
  );
}
