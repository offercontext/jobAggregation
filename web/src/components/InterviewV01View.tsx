import { useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Empty, List, Space, Spin, Tabs, Tag, Typography } from 'antd';
import { ArrowRightOutlined, BookOutlined, CompassOutlined, SoundOutlined } from '@ant-design/icons';
import { listInterviews } from '@/services/interviews';
import { listAdaptivePracticeRecommendations } from '@/services/adaptiveInterviewPractice';
import type { InterviewIndexItem } from '@/types/interviewIndex';
import type { AdaptivePracticeFocus, AdaptivePracticeRecommendation } from '@/types/adaptiveInterviewPractice';
import workflowStyles from './ui/WorkflowSurface.module.css';
import actionStyles from './InterviewNextActionCard.module.css';
import InterviewReadinessCenter from '@/features/interviewReadiness/InterviewReadinessCenter';
import type { QuickPracticeStudioContext, RealInterviewStudioContext } from '@/features/interviewReadiness/InterviewReadinessCenter';
import type { Application } from '@/types/application';
import type { ScheduleEvent } from '@/types/event';
import type { Resume } from '@/types/resume';

const { Paragraph, Title } = Typography;

type InterviewTabKey = 'upcoming' | 'completed' | 'practice';

interface Props {
  onOpenApplication?: (applicationId: number) => void;
  onOpenPreparation?: (applicationId: number, eventId: number) => void;
  /** Legacy entry retained for older hosts; only shown when preparation is unavailable. */
  onOpenMockInterview?: (applicationId: number, eventId: number) => void;
  onOpenStoryLibrary?: (reviewNoteId?: number) => void;
  onOpenAdaptivePractice?: (focus: AdaptivePracticeFocus) => void;
  onOpenVoiceCoachingGrowth?: () => void;
  onOpenQuestionBank?: () => void;
  /** Increases when the root workspace asks the interview page to start practice. */
  practiceRequestToken?: number;
  applications?: Application[];
  events?: ScheduleEvent[];
  eventsLoading?: boolean;
  eventsError?: boolean;
  onRetryEvents?: () => void;
  resumes?: Resume[];
  onOpenStudio?: (context: RealInterviewStudioContext | QuickPracticeStudioContext) => void;
}

interface InterviewListProps {
  items: InterviewIndexItem[];
  bucket: 'upcoming' | 'completed';
  loading: boolean;
  error: boolean;
  onOpenApplication?: (applicationId: number) => void;
  onOpenPreparation?: (applicationId: number, eventId: number) => void;
  onOpenMockInterview?: (applicationId: number, eventId: number) => void;
  onOpenStoryLibrary?: (reviewNoteId?: number) => void;
  eventStatuses: ReadonlyMap<number, string>;
}

const ENDED_EVENT_STATUSES = new Set(['cancelled', 'deleted', 'soft_deleted']);

function scheduledTimestamp(item: InterviewIndexItem): number {
  const raw = (item as InterviewIndexItem & { scheduled_at?: string | null }).scheduled_at;
  if (!raw) return Number.NaN;
  const value = Date.parse(raw);
  return Number.isFinite(value) ? value : Number.NaN;
}

/**
 * The index is a read-only projection. We only use its existing scheduled time
 * to group rows; this function never infers or writes an Application status.
 */
export function isUpcomingInterview(item: InterviewIndexItem, now = Date.now(), eventStatus?: string): boolean {
  if (eventStatus && ENDED_EVENT_STATUSES.has(eventStatus)) return false;
  const timestamp = scheduledTimestamp(item);
  return !Number.isFinite(timestamp) || timestamp >= now;
}

export async function listAllInterviews(): Promise<InterviewIndexItem[]> {
  const items: InterviewIndexItem[] = [];
  const seenCursors = new Set<string>();
  let cursor = '';
  while (true) {
    const result = await listInterviews(50, cursor);
    items.push(...result.items);
    const nextCursor = result.next_cursor ?? '';
    if (!nextCursor || seenCursors.has(nextCursor)) return items;
    seenCursors.add(nextCursor);
    cursor = nextCursor;
  }
}

function formatScheduledAt(item: InterviewIndexItem): string {
  const timestamp = scheduledTimestamp(item);
  return Number.isFinite(timestamp) ? new Date(timestamp).toLocaleString() : '时间待确认';
}

function InterviewList({
  items,
  bucket,
  loading,
  error,
  onOpenApplication,
  onOpenPreparation,
  onOpenMockInterview,
  onOpenStoryLibrary,
  eventStatuses,
}: InterviewListProps) {
  if (loading) return <Spin aria-label="正在加载面试列表" />;
  if (error) return <Alert type="error" showIcon message="面试列表暂时无法加载，请稍后重试。" />;
  if (items.length === 0) {
    return (
      <div className="op-empty-state">
        <Empty
          description={bucket === 'upcoming' ? '暂无即将进行的面试' : '暂无已完成的面试'}
          image={Empty.PRESENTED_IMAGE_SIMPLE}
        />
      </div>
    );
  }

  return (
    <List
      dataSource={items}
      renderItem={(item) => {
        const eventStatus = eventStatuses.get(item.event_id);
        const wasCancelled = Boolean(eventStatus && ENDED_EVENT_STATUSES.has(eventStatus));
        return (
        <List.Item className={workflowStyles.listRow} actions={[
          onOpenApplication ? (
            <Button key="detail" type="link" onClick={() => onOpenApplication(item.application_id)}>
              查看投递详情
            </Button>
          ) : null,
          bucket === 'upcoming' && item.preparation_available && onOpenPreparation ? (
            <Button key="prepare" type="link" onClick={() => onOpenPreparation?.(item.application_id, item.event_id)}>
              准备面试
            </Button>
          ) : null,
          // Keep the old drawer reachable for legacy hosts, but never show it
          // next to the canonical preparation entry supplied by AppShell.
          bucket === 'upcoming' && !onOpenPreparation && onOpenMockInterview ? (
            <Button key="legacy-mock" type="link" onClick={() => onOpenMockInterview(item.application_id, item.event_id)}>
              开始模拟面试
            </Button>
          ) : null,
          bucket === 'completed' && item.note_id && onOpenStoryLibrary ? (
            <Button key="story" type="link" onClick={() => onOpenStoryLibrary(item.note_id ?? undefined)}>
              整理为故事
            </Button>
          ) : null,
        ].filter(Boolean)}>
          <List.Item.Meta
            title={`${item.company_name} · ${item.position_name}`}
            description={(
              <Space wrap className="op-long-text">
                <span>{formatScheduledAt(item)}</span>
                <Tag>{wasCancelled ? '已取消' : item.note_id ? '已有复盘' : bucket === 'completed' ? '待记录复盘' : '待进行'}</Tag>
                {item.review_summary ? <span>{item.review_summary}</span> : null}
                {item.note_source_status === 'source_changed' ? <Tag color="warning">原资料已更新，本次结果仍使用旧版</Tag> : null}
                {item.has_review_proposal ? <Tag color="blue">有复盘建议</Tag> : null}
                {item.has_confirmed_knowledge ? <Tag color="green">已有复盘沉淀</Tag> : null}
                {bucket === 'upcoming' && item.preparation_available ? <Tag>可准备面试</Tag> : null}
              </Space>
            )}
          />
        </List.Item>
        );
      }}
    />
  );
}

export default function InterviewV01View({
  onOpenApplication,
  onOpenPreparation,
  onOpenMockInterview,
  onOpenStoryLibrary,
  onOpenAdaptivePractice,
  onOpenVoiceCoachingGrowth,
  onOpenQuestionBank,
  practiceRequestToken,
  applications,
  events,
  eventsLoading,
  eventsError,
  onRetryEvents,
  resumes,
  onOpenStudio,
}: Props) {
  const [items, setItems] = useState<InterviewIndexItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [practice, setPractice] = useState<AdaptivePracticeRecommendation | null>(null);
  const [practiceError, setPracticeError] = useState(false);
  const hasReadinessCenter = applications !== undefined || events !== undefined || resumes !== undefined || onOpenStudio !== undefined;
  const [activeTab, setActiveTab] = useState<InterviewTabKey>('upcoming');
  const [currentTime, setCurrentTime] = useState(() => Date.now());
  const lastPracticeRequestTokenRef = useRef<number | undefined>(practiceRequestToken);

  useEffect(() => {
    if (practiceRequestToken === undefined) return;
    const previous = lastPracticeRequestTokenRef.current;
    lastPracticeRequestTokenRef.current = practiceRequestToken;
    if (practiceRequestToken > 0 && previous !== undefined && practiceRequestToken > previous) {
      setActiveTab('practice');
    }
  }, [practiceRequestToken]);

  useEffect(() => {
    let active = true;
    listAllInterviews().then((result) => {
      if (active) setItems(result);
    }).catch(() => {
      if (active) setError(true);
    }).finally(() => {
      if (active) setLoading(false);
    });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => setCurrentTime(Date.now()), 60_000);
    return () => window.clearInterval(timer);
  }, []);

  const loadPractice = () => {
    setPracticeError(false);
    return listAdaptivePracticeRecommendations()
      .then((result) => setPractice(result[0] ?? null))
      .catch(() => { setPractice(null); setPracticeError(true); });
  };

  useEffect(() => { void loadPractice(); }, []);

  const eventStatuses = useMemo(
    () => new Map((events ?? []).map((event) => [event.id, event.status])),
    [events],
  );

  const groupedItems = useMemo(() => {
    const upcoming = items.filter((item) => isUpcomingInterview(item, currentTime, eventStatuses.get(item.event_id))).sort((a, b) => {
      const left = scheduledTimestamp(a);
      const right = scheduledTimestamp(b);
      if (!Number.isFinite(left)) return 1;
      if (!Number.isFinite(right)) return -1;
      return left - right;
    });
    const completed = items.filter((item) => !isUpcomingInterview(item, currentTime, eventStatuses.get(item.event_id))).sort((a, b) => scheduledTimestamp(b) - scheduledTimestamp(a));
    return { upcoming, completed };
  }, [currentTime, eventStatuses, items]);

  const eventStateNotice = eventsLoading ? (
    <Spin aria-label="正在确认面试状态" />
  ) : eventsError ? (
    <Alert
      type="error"
      showIcon
      message="面试状态暂时无法确认，请重试后查看。"
      action={onRetryEvents ? <Button size="small" aria-label="重试面试状态" onClick={onRetryEvents}>重试</Button> : undefined}
    />
  ) : null;

  return (
    <div data-testid="interview-surface" className={`${workflowStyles.surface} op-view-enter`} style={{ padding: 24 }}>
      <div className="op-section-heading" style={{ marginBottom: 18 }}>
        <div>
          <Title level={2} style={{ margin: 0 }}>面试</Title>
          <Paragraph type="secondary" style={{ margin: '6px 0 0' }}>
            围绕具体面试事件准备，完成后再进入自由练习和复盘沉淀。
          </Paragraph>
        </div>
      </div>
      <Tabs
        activeKey={activeTab}
        onChange={(key) => setActiveTab(key as InterviewTabKey)}
        items={[
          { key: 'upcoming', label: '即将进行' },
          { key: 'completed', label: '已完成' },
          { key: 'practice', label: '自由练习' },
        ]}
      />

      {activeTab === 'upcoming' ? (
        eventStateNotice ?? <>
          {hasReadinessCenter ? (
            <InterviewReadinessCenter
              initialMode="real"
              fixedMode="real"
              actionEmphasis="secondary"
              applications={applications}
              events={events}
              resumes={resumes}
              onOpenApplication={onOpenApplication}
              onOpenPreparation={onOpenPreparation}
              onOpenStudio={onOpenStudio}
            />
          ) : null}
          <section aria-labelledby="upcoming-interviews-title" style={{ marginTop: hasReadinessCenter ? 24 : 0 }}>
            <Title id="upcoming-interviews-title" level={3}>即将进行</Title>
            <InterviewList
              items={groupedItems.upcoming}
              bucket="upcoming"
              loading={loading}
              error={error}
              onOpenApplication={onOpenApplication}
              onOpenPreparation={onOpenPreparation}
              onOpenMockInterview={onOpenMockInterview}
              eventStatuses={eventStatuses}
            />
          </section>
        </>
      ) : null}

      {activeTab === 'completed' ? (
        eventStateNotice ?? <section aria-labelledby="completed-interviews-title">
          <div className="op-section-heading" style={{ marginBottom: 20 }}>
            <div>
              <Title id="completed-interviews-title" level={3} style={{ margin: 0 }}>已完成</Title>
              <Paragraph type="secondary" style={{ margin: '6px 0 0' }}>查看本次面试的复盘、来源状态和经历素材入口。</Paragraph>
            </div>
            <Space wrap>
              {onOpenVoiceCoachingGrowth ? <Button icon={<SoundOutlined />} onClick={onOpenVoiceCoachingGrowth}>表达成长</Button> : null}
              {onOpenStoryLibrary ? <Button data-story-audit="ui-library" onClick={() => onOpenStoryLibrary()}>经历素材</Button> : null}
            </Space>
          </div>
          <InterviewList
            items={groupedItems.completed}
            bucket="completed"
            loading={loading}
            error={error}
            onOpenApplication={onOpenApplication}
            onOpenStoryLibrary={onOpenStoryLibrary}
            eventStatuses={eventStatuses}
          />
        </section>
      ) : null}

      {activeTab === 'practice' ? (
        <section aria-labelledby="free-practice-title">
          <div className="op-section-heading" style={{ marginBottom: 20 }}>
            <div>
              <Title id="free-practice-title" level={3} style={{ margin: 0 }}>自由练习</Title>
              <Paragraph type="secondary" style={{ margin: '6px 0 0' }}>题库和快速练习共用同一练习工作台，文字与语音只是本次练习的回答方式。</Paragraph>
            </div>
            {onOpenQuestionBank ? <Button icon={<BookOutlined />} onClick={onOpenQuestionBank}>进入题库</Button> : null}
          </div>
          {hasReadinessCenter ? (
            <InterviewReadinessCenter
              initialMode="quick"
              fixedMode="quick"
              actionEmphasis="secondary"
              applications={applications}
              events={events}
              resumes={resumes}
              onOpenApplication={onOpenApplication}
              onOpenPreparation={onOpenPreparation}
              onOpenStudio={onOpenStudio}
            />
          ) : null}
          {practice ? (
            <section className={actionStyles.card} aria-labelledby="interview-next-action-title" style={{ marginTop: hasReadinessCenter ? 20 : 0 }}>
              <div className={actionStyles.content}>
                <span className={actionStyles.eyebrow}>下一项行动</span>
                <h2 id="interview-next-action-title" className={actionStyles.title}>{practice.title}</h2>
                <p className={actionStyles.observation}>{practice.observation}</p>
                <div className={actionStyles.meta} aria-label="训练说明">
                  <span className={actionStyles.metaItem}>来自已保存复盘</span>
                  <span className={actionStyles.metaItem}>适合一次短时训练</span>
                  <span className={actionStyles.metaItem}>不会自动写入故事库</span>
                </div>
              </div>
              {onOpenAdaptivePractice ? (
                <Button className={actionStyles.action} size="large" onClick={() => onOpenAdaptivePractice({ proposalId: practice.proposal_id, focusId: practice.focus_id })}>
                  <CompassOutlined /> 开始这项训练 <ArrowRightOutlined />
                </Button>
              ) : null}
            </section>
          ) : null}
          {practiceError && onOpenAdaptivePractice ? <Alert style={{ marginTop: 20 }} type="warning" showIcon message="复盘训练建议暂时无法加载" action={<Button size="large" onClick={() => void loadPractice()}>重新加载建议</Button>} /> : null}
          {!hasReadinessCenter && !practice && !practiceError ? (
            <div className="op-empty-state" style={{ marginTop: 20 }}>
              <Empty description="从题库选择题目，或开始一次快速练习。" image={Empty.PRESENTED_IMAGE_SIMPLE} />
            </div>
          ) : null}
        </section>
      ) : null}
    </div>
  );
}
