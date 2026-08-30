import { useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Empty, List, Space, Spin, Tabs, Tag, Typography } from 'antd';
import { ArrowRightOutlined, BookOutlined, CompassOutlined, SoundOutlined } from '@ant-design/icons';
import { listInterviews } from '@/services/interviews';
import { listAdaptivePracticeRecommendations } from '@/services/adaptiveInterviewPractice';
import type { InterviewIndexItem } from '@/types/interviewIndex';
import type { AdaptivePracticeFocus, AdaptivePracticeRecommendation } from '@/types/adaptiveInterviewPractice';
import { normalizeInterviewIndexItem } from '@/features/interviewEvents/interviewIndexContract';
import {
  compareInterviewEventCards,
  projectInterviewEventCard,
  type InterviewEventCardModel,
} from '@/features/interviewEvents/interviewEventCard';
import type { TaskLaunchRequest } from '@/features/coreTaskSurface/contracts';
import workflowStyles from './ui/WorkflowSurface.module.css';
import actionStyles from './InterviewNextActionCard.module.css';
import type { Application } from '@/types/application';
import type { ScheduleEvent } from '@/types/event';
import type { Resume } from '@/types/resume';

const { Paragraph, Title } = Typography;

type InterviewTabKey = 'upcoming' | 'completed' | 'practice';
type TaskLauncher = (request: TaskLaunchRequest) => unknown;

export interface InterviewV01ViewProps {
  onOpenApplication?: (applicationId: number) => void;
  /** Canonical task launcher supplied by the single workspace controller. */
  onOpenTask?: TaskLauncher;
  /** Explicit alias used by controller hosts that name the boundary launch. */
  onLaunchTask?: TaskLauncher;
  /** Transitional exact preparation adapter; it must already target the canonical owner. */
  onOpenPreparation?: (applicationId: number, eventId: number) => void;
  onOpenEventEditor?: (applicationId: number, eventId: number) => void;
  onOpenFreePractice?: () => void;
  /** Navigation-only story library entry; event cards never pass a note id. */
  onOpenStoryLibrary?: (reviewNoteId?: number) => void;
  onOpenAdaptivePractice?: (focus: AdaptivePracticeFocus) => void;
  onOpenVoiceCoachingGrowth?: () => void;
  onOpenQuestionBank?: () => void;
  /** Increases when the root workspace asks the interview page to focus practice. */
  practiceRequestToken?: number;
  /** Retained as read-only composition inputs for hosts that already load them. */
  applications?: Application[];
  events?: ScheduleEvent[];
  eventsLoading?: boolean;
  eventsError?: boolean;
  onRetryEvents?: () => void;
  resumes?: Resume[];
}

type InterviewListProps = {
  cards: readonly InterviewEventCardModel[];
  bucket: 'upcoming' | 'completed';
  loading: boolean;
  error: boolean;
  onOpenApplication?: (applicationId: number) => void;
  onOpenTask?: TaskLauncher;
  onLaunchTask?: TaskLauncher;
  onOpenPreparation?: (applicationId: number, eventId: number) => void;
  onOpenEventEditor?: (applicationId: number, eventId: number) => void;
  onRetryEvents?: () => void;
};

const PRIMARY_LABELS: Readonly<Partial<Record<InterviewEventCardModel['primaryAction'], string>>> = Object.freeze({
  prepare: '准备面试',
  enter_preparation: '进入面试准备',
  record_review: '记录复盘',
  view_review: '查看复盘',
  update_status: '更新事件状态',
});

const LIFECYCLE_LABELS: Readonly<Record<InterviewEventCardModel['lifecycle'], string>> = Object.freeze({
  scheduled: '待进行',
  in_progress: '进行中',
  completed: '已完成',
  cancelled: '已取消',
  unknown: '状态待确认',
});

const BUCKET_LABELS: Readonly<Record<InterviewEventCardModel['bucket'], string>> = Object.freeze({
  upcoming: '即将进行',
  completed: '已完成',
  cancelled: '已取消',
  needs_status_update: '状态待更新',
  unavailable: '暂不可用',
});

function eventSourceMap(events: readonly ScheduleEvent[] | undefined): {
  readonly values: ReadonlyMap<number, ScheduleEvent | null>;
  readonly unavailable: boolean;
} {
  const byId = new Map<number, ScheduleEvent | null>();
  let unavailable = false;
  try {
    for (const event of events ?? []) {
      try {
        const eventId = event.id;
        if (typeof eventId === 'number' && Number.isSafeInteger(eventId) && eventId > 0) {
          if (byId.has(eventId)) byId.set(eventId, null);
          else byId.set(eventId, event);
        } else unavailable = true;
      } catch {
        unavailable = true;
      }
    }
  } catch {
    unavailable = true;
  }
  return { values: byId, unavailable };
}

/**
 * Projects and orders the read-only interview index in one place. Duplicate
 * application/event identities are collapsed after the central card
 * projector has selected the deterministic representation.
 */
export function projectInterviewEventCards(
  items: readonly InterviewIndexItem[],
  now = Date.now(),
  events?: readonly ScheduleEvent[],
): readonly InterviewEventCardModel[] {
  const sources = eventSourceMap(events);
  const projected: InterviewEventCardModel[] = [];
  let length = 0;
  try {
    length = items.length;
  } catch {
    return Object.freeze(projected);
  }
  for (let index = 0; index < length; index += 1) {
    try {
      if (!(index in items)) continue;
      const item = items[index];
      let source: ScheduleEvent | null | undefined;
      try {
        source = sources.unavailable
          ? null
          : sources.values.has(item.event_id)
            ? sources.values.get(item.event_id)
            : events === undefined
              ? undefined
              : null;
      } catch {
        source = null;
      }
      const normalized = normalizeInterviewIndexItem(item, source);
      // Collection rows without a trustworthy identity cannot be rendered or
      // focused safely. Keep the single-row projector fail-closed for direct
      // diagnostics, but never synthesize an application/event identity (0)
      // inside the canonical list.
      if (normalized.application_id === null || normalized.event_id === null) continue;
      projected.push(projectInterviewEventCard(normalized, now));
    } catch {
      // One hostile row must not hide other valid Event cards or escape the
      // read-only projection boundary.
    }
  }
  projected.sort(compareInterviewEventCards);
  const seen = new Set<string>();
  const unique = projected.filter((card) => {
    const identity = `${card.applicationId}:${card.eventId}`;
    if (seen.has(identity)) return false;
    seen.add(identity);
    return true;
  });
  return Object.freeze(unique);
}

function formatCardTime(card: InterviewEventCardModel): string {
  return Number.isFinite(card.scheduledAtTimestamp)
    ? new Date(card.scheduledAtTimestamp).toLocaleString()
    : '时间待确认';
}

function taskRequestFor(card: InterviewEventCardModel): TaskLaunchRequest | null {
  if (card.primaryAction === 'prepare' || card.primaryAction === 'enter_preparation') {
    return {
      ref: {
        taskId: 'application.interview_prepare',
        applicationId: card.applicationId,
        eventId: card.eventId,
      },
      source: 'interview_event_card',
      focus: 'current',
    };
  }
  if (card.primaryAction === 'record_review' || card.primaryAction === 'view_review') {
    return {
      ref: {
        taskId: 'application.interview_review',
        applicationId: card.applicationId,
        eventId: card.eventId,
      },
      source: 'interview_event_card',
      focus: 'current',
    };
  }
  return null;
}

function primaryCanExecute(
  card: InterviewEventCardModel,
  props: Pick<InterviewListProps, 'onOpenTask' | 'onLaunchTask' | 'onOpenPreparation' | 'onOpenEventEditor'>,
): boolean {
  if (card.primaryAction === 'update_status') return Boolean(props.onOpenEventEditor);
  if (card.primaryAction === 'prepare' || card.primaryAction === 'enter_preparation') {
    return Boolean(props.onOpenTask || props.onLaunchTask || props.onOpenPreparation);
  }
  if (card.primaryAction === 'record_review' || card.primaryAction === 'view_review') {
    return Boolean(props.onOpenTask || props.onLaunchTask);
  }
  return false;
}

function InterviewList({
  cards,
  bucket,
  loading,
  error,
  onOpenApplication,
  onOpenTask,
  onLaunchTask,
  onOpenPreparation,
  onOpenEventEditor,
  onRetryEvents,
}: InterviewListProps) {
  if (loading) return <Spin aria-label="正在加载面试列表" />;
  if (error) return <Alert type="error" showIcon message="面试列表暂时无法加载，请稍后重试。" />;
  if (cards.length === 0) {
    return (
      <div className="op-empty-state">
        <Empty
          description={bucket === 'upcoming' ? '暂无即将进行的面试' : '暂无已完成或已取消的面试'}
          image={Empty.PRESENTED_IMAGE_SIMPLE}
        />
      </div>
    );
  }

  return (
    <List
      dataSource={[...cards]}
      rowKey={(card) => `${card.applicationId}:${card.eventId}`}
      renderItem={(card) => {
        const primaryLabel = PRIMARY_LABELS[card.primaryAction];
        const request = taskRequestFor(card);
        const canExecute = primaryCanExecute(card, {
          onOpenTask,
          onLaunchTask,
          onOpenPreparation,
          onOpenEventEditor,
        });
        const launch = onOpenTask ?? onLaunchTask;
        const actions: React.ReactNode[] = [];
        if (primaryLabel) {
          actions.push(
            <Button
              key="primary"
              type="primary"
              data-interview-primary="true"
              disabled={!canExecute}
              onClick={() => {
                if (card.primaryAction === 'update_status') {
                  onOpenEventEditor?.(card.applicationId, card.eventId);
                } else if (request && launch) {
                  launch(request);
                } else if (card.primaryAction === 'prepare' || card.primaryAction === 'enter_preparation') {
                  onOpenPreparation?.(card.applicationId, card.eventId);
                }
              }}
            >
              {primaryLabel}
            </Button>,
          );
        }
        for (const secondary of card.secondaryActions) {
          if (secondary === 'retry' && onRetryEvents) {
            actions.push(<Button key="retry" type="link" onClick={onRetryEvents}>重试</Button>);
          }
          if (secondary === 'view_application' && onOpenApplication) {
            actions.push(<Button key="application" type="link" onClick={() => onOpenApplication(card.applicationId)}>查看投递详情</Button>);
          }
        }
        const lifecycleLabel = LIFECYCLE_LABELS[card.lifecycle];
        return (
          <List.Item
            className={workflowStyles.listRow}
            data-testid={`interview-event-card-${card.eventId}`}
            data-interview-card-bucket={card.bucket}
            actions={actions}
          >
            <List.Item.Meta
              title={`${card.companyName} · ${card.positionName}`}
              description={(
                <Space wrap className="op-long-text">
                  <span>{formatCardTime(card)}</span>
                  <Tag>{BUCKET_LABELS[card.bucket]}</Tag>
                  <Tag>{lifecycleLabel}</Tag>
                  {card.noteId !== null && card.lifecycle === 'completed' ? <span>已有复盘</span> : null}
                  {card.contractReasons.length > 0 ? <span>部分状态待确认</span> : null}
                </Space>
              )}
            />
          </List.Item>
        );
      }}
    />
  );
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

export default function InterviewV01View({
  onOpenApplication,
  onOpenTask,
  onLaunchTask,
  onOpenPreparation,
  onOpenEventEditor,
  onOpenFreePractice,
  onOpenStoryLibrary,
  onOpenAdaptivePractice,
  onOpenVoiceCoachingGrowth,
  onOpenQuestionBank,
  practiceRequestToken,
  events,
  eventsLoading,
  eventsError,
  onRetryEvents,
}: InterviewV01ViewProps) {
  const [items, setItems] = useState<InterviewIndexItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [practice, setPractice] = useState<AdaptivePracticeRecommendation | null>(null);
  const [practiceError, setPracticeError] = useState(false);
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

  const cards = useMemo(
    () => projectInterviewEventCards(items, currentTime, events),
    [currentTime, events, items],
  );
  const upcomingCards = useMemo(
    () => cards.filter((card) => card.bucket === 'upcoming' || card.bucket === 'needs_status_update' || card.bucket === 'unavailable'),
    [cards],
  );
  const completedCards = useMemo(
    () => cards.filter((card) => card.bucket === 'completed' || card.bucket === 'cancelled'),
    [cards],
  );

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
        eventStateNotice ?? (
          <section aria-labelledby="upcoming-interviews-title">
            <Title id="upcoming-interviews-title" level={3}>即将进行</Title>
            <InterviewList
              cards={upcomingCards}
              bucket="upcoming"
              loading={loading}
              error={error}
              onOpenApplication={onOpenApplication}
              onOpenTask={onOpenTask}
              onLaunchTask={onLaunchTask}
              onOpenPreparation={onOpenPreparation}
              onOpenEventEditor={onOpenEventEditor}
              onRetryEvents={onRetryEvents}
            />
          </section>
        )
      ) : null}

      {activeTab === 'completed' ? (
        eventStateNotice ?? (
          <section aria-labelledby="completed-interviews-title">
            <div className="op-section-heading" style={{ marginBottom: 20 }}>
              <div>
                <Title id="completed-interviews-title" level={3} style={{ margin: 0 }}>已完成</Title>
                <Paragraph type="secondary" style={{ margin: '6px 0 0' }}>查看本次面试的复盘和来源状态。</Paragraph>
              </div>
              <Space wrap>
                {onOpenVoiceCoachingGrowth ? <Button icon={<SoundOutlined />} onClick={onOpenVoiceCoachingGrowth}>表达成长</Button> : null}
                {onOpenStoryLibrary ? <Button data-story-audit="ui-library" onClick={() => onOpenStoryLibrary()}>经历素材</Button> : null}
              </Space>
            </div>
            <InterviewList
              cards={completedCards}
              bucket="completed"
              loading={loading}
              error={error}
              onOpenApplication={onOpenApplication}
              onOpenTask={onOpenTask}
              onLaunchTask={onLaunchTask}
              onOpenPreparation={onOpenPreparation}
              onOpenEventEditor={onOpenEventEditor}
              onRetryEvents={onRetryEvents}
            />
          </section>
        )
      ) : null}

      {activeTab === 'practice' ? (
        <section data-testid="free-practice-workspace" aria-labelledby="free-practice-title">
          <div className="op-section-heading" style={{ marginBottom: 20 }}>
            <div>
              <Title id="free-practice-title" level={3} style={{ margin: 0 }}>自由练习</Title>
              <Paragraph type="secondary" style={{ margin: '6px 0 0' }}>题库和快速练习共用同一练习工作台，开始前不会自动调用模型。</Paragraph>
            </div>
            <Space wrap>
              {onOpenFreePractice ? <Button type="primary" onClick={onOpenFreePractice}>开始自由练习</Button> : null}
              {onOpenQuestionBank ? <Button icon={<BookOutlined />} onClick={onOpenQuestionBank}>进入题库</Button> : null}
            </Space>
          </div>
          {practice ? (
            <section className={actionStyles.card} aria-labelledby="interview-next-action-title">
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
          {!practice && !practiceError ? (
            <div className="op-empty-state" style={{ marginTop: 20 }}>
              <Empty description="从题库选择题目，或开始一次快速练习。" image={Empty.PRESENTED_IMAGE_SIMPLE} />
            </div>
          ) : null}
        </section>
      ) : null}
    </div>
  );
}
