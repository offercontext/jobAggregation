import {
  useEffect,
  useRef,
  useState,
  type MutableRefObject,
  type KeyboardEvent,
} from 'react';
import {
  ArrowUpOutlined,
  CloseOutlined,
  ExpandAltOutlined,
  SendOutlined,
  StopOutlined,
} from '@ant-design/icons';
import { useAssistantSurface, usePilotConversationController } from './AssistantSurfaceProvider';
import { recentConversationTurns } from './assistantPresentation';
import CompactMessageRenderer from './CompactMessageRenderer';
import styles from './AssistantSurface.module.css';

const TASK_COPY = {
  idle: '随时可以开始',
  running: '正在处理',
  waiting_confirmation: '等待确认',
  completed: '已完成',
  failed: '处理失败',
} as const;

interface Props {
  returnFocusRef: MutableRefObject<HTMLElement | null>;
  onExpand?: () => void;
}

export default function HaruChatWindow({ returnFocusRef, onExpand }: Props) {
  const surface = useAssistantSurface();
  const controller = usePilotConversationController();
  const [draft, setDraft] = useState('');
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (surface.surface !== 'haru_chat') return;
    inputRef.current?.focus();
  }, [surface.surface]);

  useEffect(() => {
    if (surface.surface !== 'haru_chat') return;
    if (typeof endRef.current?.scrollIntoView === 'function') {
      endRef.current.scrollIntoView({ block: 'nearest' });
    }
  }, [controller.turns, controller.pending, surface.surface]);

  if (surface.surface !== 'haru_chat') return null;

  const close = () => {
    surface.closeSurface();
    window.setTimeout(() => returnFocusRef.current?.focus(), 0);
  };
  const submit = async () => {
    if (!draft.trim() || controller.loading || controller.pending) return;
    surface.reportTaskState('running', controller.conversationId);
    const outcome = await controller.sendMessage(draft);
    if (outcome === 'sent') setDraft('');
    else if (outcome === 'failed') surface.reportTaskState('failed', controller.conversationId);
  };
  const onInputKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      close();
      return;
    }
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      void submit();
    }
  };
  const activeConversation = controller.conversations.find(
    (conversation) => conversation.id === controller.conversationId,
  );
  const visiblePageContext = controller.requestContextSnapshot
    ?? controller.pinnedContext
    ?? controller.followingContext;
  const contextLabel =
    controller.requestContextSnapshot?.entity?.label ||
    controller.requestContextSnapshot?.label ||
    (controller.conversationId === undefined ? controller.draftContext?.context_label : undefined) ||
    activeConversation?.context_label ||
    (activeConversation?.context_type === 'application' && activeConversation.context_ref
      ? `投递 #${activeConversation.context_ref}`
      : undefined) ||
    visiblePageContext?.entity?.label ||
    visiblePageContext?.label ||
    (controller.conversationId ? '工作台' : '当前页面');
  const attachmentSuffix = controller.attachments.length > 0
    ? ` · ${controller.attachments.length} 个附件`
    : '';

  return (
    <section
      className={styles.window}
      role="dialog"
      aria-modal="false"
      aria-label="Haru 轻量对话"
      onKeyDown={(event) => {
        if (event.key === 'Escape') {
          event.preventDefault();
          close();
        }
      }}
    >
      <header className={styles.header}>
        <div>
          <strong>Haru</strong>
          <span data-task-state={controller.taskState === 'idle' ? surface.taskState : controller.taskState}>
            {TASK_COPY[controller.taskState === 'idle' ? surface.taskState : controller.taskState]}
          </span>
        </div>
        <div className={styles.headerActions}>
          <button
            type="button"
            aria-label="展开到 Pilot 工作区"
            onClick={onExpand ?? surface.openPilot}
          >
            <ExpandAltOutlined />
          </button>
          <button type="button" aria-label="关闭 Haru 对话" onClick={close}>
            <CloseOutlined />
          </button>
        </div>
      </header>

      <div className={styles.context} aria-label="当前上下文">
        <span>当前上下文</span>
        <b>{contextLabel}{attachmentSuffix}</b>
      </div>

      <div className={styles.messages} aria-live="polite" aria-relevant="additions text">
        {controller.turns.length === 0 ? (
          <div className={styles.empty}>
            <strong>想先处理什么？</strong>
            <span>可以问投递进展、面试准备或下一步安排。</span>
          </div>
        ) : (
          recentConversationTurns(controller.turns).map((turn, index) => (
            <CompactMessageRenderer key={`${turn.role}-${index}`} turn={turn} />
          ))
        )}
        {controller.loading && !controller.hasStreamingAssistantContent ? (
          <div className={styles.thinking} role="status">
            {controller.loadingLabel || '正在理解你的问题'}
          </div>
        ) : null}
        <div ref={endRef} />
      </div>

      {controller.pending ? (
        <div className={styles.pending} role="status">
          <span>有一项操作等你确认</span>
          <button type="button" data-testid="haru-open-pending" onClick={surface.openPending}>
            到 Pilot 查看并确认
            <ArrowUpOutlined />
          </button>
        </div>
      ) : null}

      {controller.lastError ? (
        <div className={styles.error} role="alert">
          <span>{controller.lastError}</span>
          <button type="button" onClick={controller.retryLastMessage} disabled={controller.loading}>
            重试
          </button>
        </div>
      ) : null}

      <footer className={styles.composer}>
        <label htmlFor="haru-composer" className={styles.srOnly}>给 Haru 发消息</label>
        <textarea
          id="haru-composer"
          ref={inputRef}
          rows={2}
          value={draft}
          disabled={!controller.hasKey || Boolean(controller.pending)}
          placeholder={controller.pending ? '请先在 Pilot 中确认' : '问 Haru…'}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={onInputKeyDown}
        />
        {controller.loading ? (
          <button
            type="button"
            className={styles.stopButton}
            aria-label="停止生成"
            onClick={() => {
              controller.stopActiveRequest();
            }}
          >
            <StopOutlined />
          </button>
        ) : (
          <button
            type="button"
            className={styles.sendButton}
            aria-label="发送消息"
            disabled={!draft.trim() || !controller.hasKey || Boolean(controller.pending)}
            onClick={() => void submit()}
          >
            <SendOutlined />
          </button>
        )}
      </footer>
    </section>
  );
}
