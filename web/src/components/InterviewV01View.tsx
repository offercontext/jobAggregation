import { useEffect, useState } from 'react';
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

interface Props {
  onOpenApplication?: (applicationId: number) => void;
  onOpenPreparation?: (applicationId: number, eventId: number) => void;
  onOpenMockInterview?: (applicationId: number, eventId: number) => void;
  onOpenStoryLibrary?: (reviewNoteId?: number) => void;
  onOpenAdaptivePractice?: (focus: AdaptivePracticeFocus) => void;
  onOpenVoiceCoachingGrowth?: () => void;
  onOpenQuestionBank?: () => void;
  applications?: Application[];
  events?: ScheduleEvent[];
  resumes?: Resume[];
  onOpenStudio?: (context: RealInterviewStudioContext | QuickPracticeStudioContext) => void;
}

export default function InterviewV01View({ onOpenApplication, onOpenPreparation, onOpenMockInterview, onOpenStoryLibrary, onOpenAdaptivePractice, onOpenVoiceCoachingGrowth, onOpenQuestionBank, applications, events, resumes, onOpenStudio }: Props) {
  const [items, setItems] = useState<InterviewIndexItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [practice, setPractice] = useState<AdaptivePracticeRecommendation | null>(null);
  const [practiceError, setPracticeError] = useState(false);
  const hasReadinessCenter = applications !== undefined || events !== undefined || resumes !== undefined || onOpenStudio !== undefined;
  const [activeTab, setActiveTab] = useState<'prepare' | 'practice' | 'questions' | 'growth'>(hasReadinessCenter ? 'prepare' : 'growth');

  useEffect(() => {
    let active = true;
    listInterviews().then((result) => {
      if (active) setItems(result.items);
    }).catch(() => {
      if (active) setError(true);
    }).finally(() => {
      if (active) setLoading(false);
    });
    return () => { active = false; };
  }, []);

  const loadPractice = () => {
    setPracticeError(false);
    return listAdaptivePracticeRecommendations()
      .then((result) => setPractice(result[0] ?? null))
      .catch(() => { setPractice(null); setPracticeError(true); });
  };

  useEffect(() => { void loadPractice(); }, []);

  return (
    <div data-testid="interview-surface" className={`${workflowStyles.surface} op-view-enter`} style={{ padding: 24 }}>
      <Tabs
        activeKey={activeTab}
        onChange={(key) => setActiveTab(key as 'prepare' | 'practice' | 'questions' | 'growth')}
        items={[
          { key: 'prepare', label: '待准备' },
          { key: 'practice', label: '模拟练习' },
          { key: 'questions', label: '题库' },
          { key: 'growth', label: '复盘与成长' },
        ]}
      />
      {activeTab === 'prepare' && hasReadinessCenter ? (
        <InterviewReadinessCenter
          applications={applications}
          events={events}
          resumes={resumes}
          onOpenApplication={onOpenApplication}
          onOpenPreparation={onOpenPreparation}
          onOpenStudio={onOpenStudio}
        />
      ) : null}
      {activeTab === 'questions' ? (
        <div className="op-empty-state">
          <Empty description="从题库选择题目，继续已有刷题计划。" image={Empty.PRESENTED_IMAGE_SIMPLE}>
            {onOpenQuestionBank ? <Button type="primary" icon={<BookOutlined />} onClick={onOpenQuestionBank}>进入题库</Button> : null}
          </Empty>
        </div>
      ) : null}
      {activeTab === 'practice' || activeTab === 'growth' || (!hasReadinessCenter && activeTab === 'prepare') ? <>
      <div className="op-section-heading" style={{ marginBottom: 20 }}>
        <div>
          <Title level={3} style={{ margin: 0 }}>面试</Title>
          <Paragraph type="secondary" style={{ margin: '6px 0 0' }}>
            {activeTab === 'practice' ? '开始一次文本、语音或快速模拟练习。' : '查看面试记录、复盘、故事和薄弱点。'}
          </Paragraph>
        </div>
        <Space wrap>
          {onOpenVoiceCoachingGrowth ? <Button icon={<SoundOutlined />} onClick={onOpenVoiceCoachingGrowth}>表达成长</Button> : null}
          {onOpenStoryLibrary ? <Button type={activeTab === 'growth' ? 'primary' : 'default'} data-story-audit="ui-library" onClick={() => onOpenStoryLibrary()}>经历与故事</Button> : null}
        </Space>
      </div>
      {practice && onOpenAdaptivePractice ? (
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
          <Button className={actionStyles.action} type="primary" size="large" onClick={() => onOpenAdaptivePractice({ proposalId: practice.proposal_id, focusId: practice.focus_id })}>
            <CompassOutlined /> 开始这项训练 <ArrowRightOutlined />
          </Button>
        </section>
      ) : null}
      {practiceError && onOpenAdaptivePractice ? <Alert style={{ marginBottom: 20 }} type="warning" showIcon message="复盘训练建议暂时无法加载" action={<Button size="large" onClick={() => void loadPractice()}>重新加载建议</Button>} /> : null}
      {loading ? <Spin aria-label="正在加载面试列表" /> : null}
      {error ? <Alert type="error" showIcon message="面试列表暂时无法加载，请稍后重试。" /> : null}
      {!loading && !error && items.length === 0 ? (
        <div className="op-empty-state">
          <Empty description="暂无已安排的面试" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        </div>
      ) : null}
      {!loading && !error && items.length > 0 ? (
        <List
          dataSource={items}
          renderItem={(item) => (
            <List.Item className={workflowStyles.listRow} actions={[
              <Button key="detail" type="link" onClick={() => onOpenApplication?.(item.application_id)}>
                查看投递详情
              </Button>,
              item.preparation_available ? (
                <Button key="prepare" type="link" onClick={() => onOpenPreparation?.(item.application_id, item.event_id)}>
                  准备面试
                </Button>
              ) : null,
              <Button key="mock" type="link" onClick={() => onOpenMockInterview?.(item.application_id, item.event_id)}>
                开始模拟面试
              </Button>,
              item.note_id && onOpenStoryLibrary ? (
                <Button key="story" type="link" onClick={() => onOpenStoryLibrary(item.note_id ?? undefined)}>
                  整理为故事
                </Button>
              ) : null,
            ]}>
              <List.Item.Meta
                title={`${item.company_name} · ${item.position_name}`}
                description={(
                  <Space wrap className="op-long-text">
                    <span>{new Date(item.scheduled_at).toLocaleString()}</span>
                    <Tag>{item.note_id ? '已有复盘' : '待记录复盘'}</Tag>
                    {item.review_summary ? <span>{item.review_summary}</span> : null}
                    {item.note_source_status === 'source_changed' ? <Tag color="warning">来源已变化</Tag> : null}
                    {item.has_review_proposal ? <Tag color="blue">有复盘建议</Tag> : null}
                    {item.has_confirmed_knowledge ? <Tag color="green">已有确认知识</Tag> : null}
                    {item.preparation_available ? <Tag>可准备面试</Tag> : null}
                  </Space>
                )}
              />
            </List.Item>
          )}
        />
      ) : null}
      </> : null}
    </div>
  );
}
