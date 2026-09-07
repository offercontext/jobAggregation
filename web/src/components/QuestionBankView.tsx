import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Button,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Segmented,
  Select,
  Skeleton,
  Space,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import {
  BulbOutlined,
  DeleteOutlined,
  EditOutlined,
  FireOutlined,
  PlusOutlined,
  ReloadOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';
import {
  createQuestion,
  deleteQuestion,
  generateQuestions,
  getPracticeStats,
  listDueQuestions,
  listQuestions,
  submitReview,
  updateQuestion,
} from '@/services/questions';
import type {
  Question,
  QuestionDifficulty,
  QuestionInput,
  QuestionStatus,
  ReviewRating,
} from '@/types/question';
import styles from './QuestionBankView.module.css';

const { Paragraph } = Typography;

const DIFFICULTY_META: Record<QuestionDifficulty, { label: string; color: string }> = {
  easy: { label: '简单', color: 'green' },
  medium: { label: '中等', color: 'gold' },
  hard: { label: '困难', color: 'red' },
};

const STATUS_META: Record<QuestionStatus, { label: string; color: string }> = {
  new: { label: '未刷', color: 'default' },
  practicing: { label: '练习中', color: 'blue' },
  mastered: { label: '已掌握', color: 'green' },
};

interface QuestionBankViewProps {
  focusId?: number;
  practiceRequestToken?: number;
}

export default function QuestionBankView({
  focusId,
  practiceRequestToken,
}: QuestionBankViewProps) {
  const [tab, setTab] = useState<'question_bank' | 'review'>(practiceRequestToken ? 'review' : 'question_bank');
  const lastPracticeRequestTokenRef = useRef<number | undefined>(practiceRequestToken);

  // When launched from a mock-interview drill link, surface the target id.
  useEffect(() => {
    if (focusId) {
      message.info(`正在定位题目 #${focusId}，可在题库中按 ID 查找`);
    }
  }, [focusId]);

  useEffect(() => {
    const previous = lastPracticeRequestTokenRef.current;
    lastPracticeRequestTokenRef.current = practiceRequestToken;
    if (practiceRequestToken !== undefined && practiceRequestToken > 0 && practiceRequestToken > (previous ?? 0)) {
      setTab('review');
    }
  }, [practiceRequestToken]);

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <div>
          <h1 className={styles.title}>题库刷题</h1>
          <p className={styles.subtitle}>基于已记录的面试问题生成题目，管理题库，按间隔复习巩固面试知识</p>
        </div>
        <Segmented
          value={tab}
          onChange={(v) => setTab(v as 'question_bank' | 'review')}
          options={[
            { label: '题库', value: 'question_bank' },
            { label: '今日复习', value: 'review' },
          ]}
        />
      </div>

      <section hidden={tab !== 'question_bank'} aria-label="题库模式"><BankTab /></section>
      <section hidden={tab !== 'review'} aria-label="今日复习模式"><PracticeTab /></section>
    </div>
  );
}

/* ----------------------------- 题库 tab ----------------------------- */

function BankTab() {
  const qc = useQueryClient();
  const [status, setStatus] = useState<QuestionStatus | 'all'>('all');
  const [difficulty, setDifficulty] = useState<QuestionDifficulty | 'all'>('all');
  const [search, setSearch] = useState('');
  const [generateOpen, setGenerateOpen] = useState(false);
  const [editing, setEditing] = useState<Question | null>(null);
  const [manualOpen, setManualOpen] = useState(false);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  const filter = useMemo(
    () => ({
      ...(status !== 'all' ? { status } : {}),
      ...(difficulty !== 'all' ? { difficulty } : {}),
    }),
    [status, difficulty],
  );

  const { data: questions = [], isLoading } = useQuery({
    queryKey: ['questions', filter],
    queryFn: () => listQuestions(filter),
  });

  const visible = useMemo(() => {
    const term = search.trim().toLowerCase();
    if (!term) return questions;
    return questions.filter(
      (q) =>
        q.question.toLowerCase().includes(term) ||
        q.category.toLowerCase().includes(term) ||
        q.tags.some((t) => t.toLowerCase().includes(term)),
    );
  }, [questions, search]);

  const delMutation = useMutation({
    mutationFn: (id: number) => deleteQuestion(id),
    onSuccess: () => {
      message.success('已删除');
      qc.invalidateQueries({ queryKey: ['questions'] });
    },
    onError: () => message.error('删除失败'),
  });

  const toggleExpand = (id: number) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  if (generateOpen) {
    return <GenerateDrawer open={generateOpen} onClose={() => setGenerateOpen(false)} />;
  }

  return (
    <>
      <div className={styles.toolbar}>
        <Button
          type="primary"
          icon={<ThunderboltOutlined />}
          className="op-ai-btn"
          onClick={() => setGenerateOpen(true)}
        >
          AI 生成题目
        </Button>
        <Button icon={<PlusOutlined />} onClick={() => setManualOpen(true)}>
          手动添加
        </Button>
        <span className={styles.spacer} />
        <Input.Search
          placeholder="搜索题目 / 分类 / 标签"
          allowClear
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          style={{ width: 240 }}
        />
        <Select
          value={status}
          onChange={setStatus}
          style={{ width: 120 }}
          aria-label="按状态筛选"
          options={[
            { label: '全部状态', value: 'all' },
            { label: '未刷', value: 'new' },
            { label: '练习中', value: 'practicing' },
            { label: '已掌握', value: 'mastered' },
          ]}
        />
        <Select
          value={difficulty}
          onChange={setDifficulty}
          style={{ width: 110 }}
          aria-label="按难度筛选"
          options={[
            { label: '全部难度', value: 'all' },
            { label: '简单', value: 'easy' },
            { label: '中等', value: 'medium' },
            { label: '困难', value: 'hard' },
          ]}
        />
      </div>

      {isLoading ? (
        <Space direction="vertical" size={10} style={{ width: '100%' }}>
          {[0, 1, 2].map((i) => (
            <div key={i} className={styles.qCard}>
              <Skeleton active paragraph={{ rows: 2 }} title={false} />
            </div>
          ))}
        </Space>
      ) : visible.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={
            questions.length === 0 ? '题库还是空的' : '没有符合条件的题目'
          }
          style={{ padding: '48px 0' }}
        >
          {questions.length === 0 && (
            <Button
              type="primary"
              icon={<ThunderboltOutlined />}
              className="op-ai-btn"
              onClick={() => setGenerateOpen(true)}
            >
              从面试复盘生成题目，或手动添加
            </Button>
          )}
        </Empty>
      ) : (
        <div className={styles.qList}>
          {visible.map((q) => (
            <div key={q.id} className={styles.qCard}>
              <div className={styles.qMeta}>
                {q.category && <Tag color="purple">{q.category}</Tag>}
                <Tag color={DIFFICULTY_META[q.difficulty].color}>
                  {DIFFICULTY_META[q.difficulty].label}
                </Tag>
                <Tag color={STATUS_META[q.status].color}>{STATUS_META[q.status].label}</Tag>
                {q.practice_count > 0 && (
                  <span style={{ fontSize: 12, color: 'var(--op-muted)' }}>
                    刷题 {q.practice_count} 次
                  </span>
                )}
                <div className={styles.qActions}>
                  <Tooltip title="编辑">
                    <Button
                      type="text"
                      size="small"
                      aria-label="编辑题目"
                      icon={<EditOutlined />}
                      onClick={() => setEditing(q)}
                    />
                  </Tooltip>
                  <Popconfirm
                    title="删除这道题？"
                    okText="删除"
                    cancelText="取消"
                    okButtonProps={{ danger: true }}
                    onConfirm={() => delMutation.mutate(q.id)}
                  >
                    <Tooltip title="删除">
                      <Button type="text" size="small" aria-label="删除题目" icon={<DeleteOutlined />} danger />
                    </Tooltip>
                  </Popconfirm>
                </div>
              </div>
              <div
                className={styles.qStem}
                onClick={() => toggleExpand(q.id)}
                role="button"
                tabIndex={0}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    toggleExpand(q.id);
                  }
                }}
              >
                {q.question}
              </div>
              {expanded.has(q.id) && q.reference_answer && (
                <div className={styles.qAnswer}>{q.reference_answer}</div>
              )}
              {expanded.has(q.id) && !q.reference_answer && (
                <div className={styles.qAnswer} style={{ color: 'var(--op-muted)' }}>
                  （暂无参考答案）
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      <QuestionFormModal
        open={manualOpen || !!editing}
        question={editing}
        onClose={() => {
          setManualOpen(false);
          setEditing(null);
        }}
      />
    </>
  );
}

/* --------------------------- 生成抽屉 --------------------------- */

function GenerateDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const [source, setSource] = useState<'knowledge' | 'notes'>('notes');
  const [count, setCount] = useState(8);

  const genMutation = useMutation({
    mutationFn: () =>
      generateQuestions({
        source,
        count,
      }),
    onSuccess: (res) => {
      const extra = res.skipped > 0 ? `，跳过 ${res.skipped} 道重复` : '';
      message.success(`已生成 ${res.count} 道题${extra}`);
      qc.invalidateQueries({ queryKey: ['questions'] });
      qc.invalidateQueries({ queryKey: ['questions-due'] });
      onClose();
    },
    onError: (err: unknown) => {
      const msg =
        (err as { response?: { data?: { error?: string } } })?.response?.data?.error ??
        '生成失败，请检查 AI 配置';
      message.error(msg);
    },
  });

  if (!open) return null;

  return (
    <section aria-label="AI 生成题目">
      <div className={styles.layerHeader}>
        <div>
          <Button type="link" className={styles.backButton} onClick={onClose}>
            返回题库
          </Button>
          <h2 className={styles.layerTitle}>AI 生成题目</h2>
        </div>
      </div>
      <Space direction="vertical" size={20} style={{ width: '100%' }}>
        <div>
          <div style={{ marginBottom: 8, fontWeight: 600 }}>来源</div>
          <Segmented
            block
            value={source}
            onChange={(v) => setSource(v as 'knowledge' | 'notes')}
            options={[
              { label: '参考资料（暂未开放）', value: 'knowledge', disabled: true },
              { label: '面试复盘真题', value: 'notes' },
            ]}
          />
        </div>

        {source === 'knowledge' && (
          <Paragraph type="secondary" style={{ margin: 0 }}>
            参考资料出题暂未开放，可选择面试复盘或手动添加题目。
          </Paragraph>
        )}

        {source === 'notes' && (
          <Paragraph type="secondary" style={{ margin: 0 }}>
            将从已记录的面试问题中提炼题目。参考资料出题暂未开放，可先手动添加题目。
          </Paragraph>
        )}

        <div>
          <div style={{ marginBottom: 8, fontWeight: 600 }}>生成数量</div>
          <InputNumber min={1} max={20} value={count} onChange={(v) => setCount(v ?? 8)} style={{ width: '100%' }} />
        </div>

        <Button
          type="primary"
          block
          className="op-ai-btn"
          icon={<ThunderboltOutlined />}
          loading={genMutation.isPending}
          onClick={() => genMutation.mutate()}
        >
          {genMutation.isPending ? 'AI 正在出题…' : '开始生成'}
        </Button>
        <Paragraph type="secondary" style={{ margin: 0, fontSize: 12 }}>
          已存在的题目会自动去重（精确 + 近似），不会重复入库。
        </Paragraph>
      </Space>
    </section>
  );
}

/* --------------------------- 手动增/改 --------------------------- */

function QuestionFormModal({
  open,
  question,
  onClose,
}: {
  open: boolean;
  question: Question | null;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [form] = Form.useForm();
  const isEdit = !!question;

  useEffect(() => {
    if (open) {
      form.setFieldsValue({
        question: question?.question ?? '',
        category: question?.category ?? '',
        difficulty: question?.difficulty ?? 'medium',
        reference_answer: question?.reference_answer ?? '',
        tags: question?.tags ?? [],
      });
    }
  }, [open, question, form]);

  const saveMutation = useMutation({
    mutationFn: (values: QuestionInput) =>
      isEdit ? updateQuestion(question!.id, values) : createQuestion(values),
    onSuccess: () => {
      message.success(isEdit ? '已保存' : '已添加');
      qc.invalidateQueries({ queryKey: ['questions'] });
      onClose();
    },
    onError: () => message.error('保存失败'),
  });

  return (
    <Modal
      title={isEdit ? '编辑题目' : '手动添加题目'}
      open={open}
      onCancel={onClose}
      onOk={() => form.submit()}
      okText="保存"
      cancelText="取消"
      confirmLoading={saveMutation.isPending}
      destroyOnHidden
    >
      <Form
        form={form}
        layout="vertical"
        onFinish={(values) => saveMutation.mutate(values as QuestionInput)}
        style={{ marginTop: 12 }}
      >
        <Form.Item
          name="question"
          label="题目"
          rules={[{ required: true, message: '请输入题目' }]}
        >
          <Input.TextArea rows={2} placeholder="例如：什么是 goroutine 泄漏？如何排查？" />
        </Form.Item>
        <Space size={12} style={{ display: 'flex' }}>
          <Form.Item name="category" label="分类" style={{ flex: 1 }}>
            <Input placeholder="如 Go并发 / 系统设计" />
          </Form.Item>
          <Form.Item name="difficulty" label="难度" style={{ width: 120 }}>
            <Select
              options={[
                { label: '简单', value: 'easy' },
                { label: '中等', value: 'medium' },
                { label: '困难', value: 'hard' },
              ]}
            />
          </Form.Item>
        </Space>
        <Form.Item name="tags" label="标签">
          <Select mode="tags" placeholder="回车添加标签" tokenSeparators={[',']} />
        </Form.Item>
        <Form.Item name="reference_answer" label="参考答案">
          <Input.TextArea rows={4} placeholder="要点式参考答案" />
        </Form.Item>
      </Form>
    </Modal>
  );
}

/* ----------------------------- 刷题打卡 tab ----------------------------- */

function PracticeTab() {
  const qc = useQueryClient();
  const [revealed, setRevealed] = useState(false);
  const [doneToday, setDoneToday] = useState(0);

  const { data: stats } = useQuery({
    queryKey: ['questions', 'stats'],
    queryFn: () => getPracticeStats(),
  });
  const { data: due = [], isLoading } = useQuery({
    queryKey: ['questions-due'],
    queryFn: () => listDueQuestions(40),
  });

  const current = due[0];

  const reviewMutation = useMutation({
    mutationFn: ({ id, rating }: { id: number; rating: ReviewRating }) => submitReview(id, rating),
    onSuccess: () => {
      setRevealed(false);
      setDoneToday((n) => n + 1);
      qc.invalidateQueries({ queryKey: ['questions-due'] });
      qc.invalidateQueries({ queryKey: ['questions'] });
    },
    onError: () => message.error('打卡失败'),
  });

  const rate = (rating: ReviewRating) => {
    if (!current || reviewMutation.isPending) return;
    reviewMutation.mutate({ id: current.id, rating });
  };

  // Keyboard shortcuts: space reveals, 1/2/3 rate.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement;
      if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')) return;
      if (!current) return;
      if (!revealed && (e.key === ' ' || e.key === 'Enter')) {
        e.preventDefault();
        setRevealed(true);
      } else if (revealed && (e.key === '1' || e.key === '2' || e.key === '3')) {
        e.preventDefault();
        rate(Number(e.key) as ReviewRating);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  const sessionTotal = doneToday + due.length;
  const progress = sessionTotal > 0 ? Math.round((doneToday / sessionTotal) * 100) : 0;
  const masteryRate =
    stats && stats.total > 0 ? Math.round((stats.mastered / stats.total) * 100) : 0;

  return (
    <>
      <div className={styles.statRow}>
        <div className={styles.statCard}>
          <span className={styles.statLabel}>
            <ThunderboltOutlined /> 今日已刷
          </span>
          <span className={`${styles.statValue} op-tnum`}>{stats?.today_reviews ?? 0}</span>
          <span className={styles.statHint}>本次会话 {doneToday} 道</span>
        </div>
        <div className={styles.statCard}>
          <span className={styles.statLabel}>
            <FireOutlined /> 连续打卡
          </span>
          <span className={`${styles.statValue} op-tnum`}>{stats?.streak_days ?? 0} 天</span>
          <span className={styles.statHint}>坚持就是胜利</span>
        </div>
        <div className={styles.statCard}>
          <span className={styles.statLabel}>
            <BulbOutlined /> 已掌握
          </span>
          <span className={`${styles.statValue} op-tnum`}>{masteryRate}%</span>
          <span className={styles.statHint}>
            {stats?.mastered ?? 0} / {stats?.total ?? 0} 道
          </span>
        </div>
        <div className={styles.statCard}>
          <span className={styles.statLabel}>
            <ReloadOutlined /> 待复习
          </span>
          <span className={`${styles.statValue} op-tnum`}>{stats?.due ?? 0}</span>
          <span className={styles.statHint}>到期需要巩固</span>
        </div>
      </div>

      {isLoading ? (
        <div className={styles.flash}>
          <Skeleton active paragraph={{ rows: 4 }} />
        </div>
      ) : !current ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={
            (stats?.total ?? 0) === 0 ? '题库还是空的，先去生成一些题目吧' : '太棒了，今天的复习队列已清空'
          }
          style={{ padding: '56px 0' }}
        />
      ) : (
        <div className={styles.flash}>
          <div className={styles.qMeta}>
            {current.category && <Tag color="purple">{current.category}</Tag>}
            <Tag color={DIFFICULTY_META[current.difficulty].color}>
              {DIFFICULTY_META[current.difficulty].label}
            </Tag>
            <Tag color={STATUS_META[current.status].color}>{STATUS_META[current.status].label}</Tag>
          </div>

          <div className={styles.flashStem}>{current.question}</div>

          {revealed ? (
            <div className={styles.flashAnswer}>
              {current.reference_answer || '（这道题暂无参考答案，凭记忆自评即可）'}
            </div>
          ) : (
            <Button size="large" onClick={() => setRevealed(true)} style={{ alignSelf: 'flex-start' }}>
              显示答案 <span className={styles.rateKey}>（空格）</span>
            </Button>
          )}

          <div className={styles.flashFooter}>
            <div className={styles.progressBar} aria-hidden>
              <div className={styles.progressFill} style={{ width: `${progress}%` }} />
            </div>
            {revealed && (
              <div className={styles.rateRow}>
                <button
                  className={`${styles.rateBtn} ${styles.rateAgain}`}
                  onClick={() => rate(1)}
                  disabled={reviewMutation.isPending}
                >
                  不会
                  <span className={styles.rateKey}>1 · 明天再练</span>
                </button>
                <button
                  className={`${styles.rateBtn} ${styles.rateHard}`}
                  onClick={() => rate(2)}
                  disabled={reviewMutation.isPending}
                >
                  模糊
                  <span className={styles.rateKey}>2 · 3 天后</span>
                </button>
                <button
                  className={`${styles.rateBtn} ${styles.rateGood}`}
                  onClick={() => rate(3)}
                  disabled={reviewMutation.isPending}
                >
                  掌握
                  <span className={styles.rateKey}>3 · 7 天后</span>
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </>
  );
}
