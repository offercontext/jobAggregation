import {
  useEffect,
  useLayoutEffect,
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
import { positionHaruWindow, type HaruRect } from './haruWindowPosition';
import styles from './AssistantSurface.module.css';

const TASK_COPY = {
  idle: '随时可以开始',
  running: '正在处理',
  waiting_confirmation: '等待确认',
  completed: '已完成',
  failed: '处理失败',
} as const;

function measureDesktopViewport() {
  const width = typeof document === 'undefined'
    ? window.innerWidth
    : document.documentElement.clientWidth || window.innerWidth;
  const height = typeof document === 'undefined'
    ? window.innerHeight
    : document.documentElement.clientHeight || window.innerHeight;
  const navigation = typeof document === 'undefined'
    ? null
    : document.querySelector<HTMLElement>('.op-sidebar');
  const navigationRect = navigation?.getBoundingClientRect();
  const navigationRight = navigationRect
    && navigationRect.height > height / 2
    && navigationRect.right < width
      ? navigationRect.right + 12
      : 12;
  return { width, height, navigationRight };
}

interface Props {
  returnFocusRef: MutableRefObject<HTMLElement | null>;
  onExpand?: () => void;
  anchorRect?: HaruRect;
}

export default function HaruChatWindow({ returnFocusRef, onExpand, anchorRect }: Props) {
  const surface = useAssistantSurface();
  const controller = usePilotConversationController();
  const draft = controller.composerDraft;
  const setDraft = controller.setComposerDraft;
  const [viewport, setViewport] = useState(() => (
    typeof window === 'undefined'
      ? { width: 1440, height: 900, navigationRight: 12 }
      : measureDesktopViewport()
  ));
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const dialogRef = useRef<HTMLElement>(null);
  const closingRef = useRef(false);

  useEffect(() => {
    if (surface.surface !== 'haru_chat') return;
    closingRef.current = false;
    if (controller.hasKey && !controller.pending) inputRef.current?.focus();
    else dialogRef.current?.focus();
  }, [controller.hasKey, controller.pending, surface.surface]);

  useLayoutEffect(() => {
    const syncViewport = () => setViewport(measureDesktopViewport());
    syncViewport();
    window.addEventListener('resize', syncViewport);
    window.visualViewport?.addEventListener('resize', syncViewport);
    return () => {
      window.removeEventListener('resize', syncViewport);
      window.visualViewport?.removeEventListener('resize', syncViewport);
    };
  }, []);

  useEffect(() => {
    if (surface.surface !== 'haru_chat') return;
    if (typeof endRef.current?.scrollIntoView === 'function') {
      endRef.current.scrollIntoView({ block: 'nearest' });
    }
  }, [controller.turns, controller.pending, surface.surface]);

  if (surface.surface !== 'haru_chat') return null;

  const close = () => {
    if (closingRef.current) return;
    closingRef.current = true;
    surface.closeSurface();
    window.setTimeout(() => returnFocusRef.current?.focus(), 0);
  };
  const submit = async () => {
    if (!draft.trim() || controller.loading || controller.pending) return;
    const outcome = await controller.sendMessage(draft);
    if (outcome === 'sent') setDraft('');
  };
  const onInputKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
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
  const anchorPrefersLeft = anchorRect
    ? (anchorRect.left + anchorRect.right) / 2 >= viewport.width / 2
    : true;
  const horizontalAnchorSpace = anchorRect
    ? anchorPrefersLeft
      ? anchorRect.left - 12 - viewport.navigationRight
      : viewport.width - 24 - anchorRect.right
    : viewport.width - viewport.navigationRight - 12;
  const surfaceSize = {
    width: Math.min(
      392,
      Math.max(0, viewport.width - viewport.navigationRight - 12),
      Math.max(280, horizontalAnchorSpace),
    ),
    height: Math.min(620, Math.max(0, viewport.height - 24)),
  };
  const fallbackGap = viewport.width < 768 ? 16 : 32;
  const windowPosition = anchorRect
    ? positionHaruWindow({
        anchor: anchorRect,
        viewport,
        surface: surfaceSize,
        bounds: { left: viewport.navigationRight },
      })
    : {
        left: Math.max(fallbackGap, viewport.width - surfaceSize.width - fallbackGap),
        top: Math.max(fallbackGap, viewport.height - surfaceSize.height - fallbackGap),
        direction: 'left-up' as const,
      };

  return (
    <section
      id="haru-chat-window"
      ref={dialogRef}
      className={styles.window}
      role="dialog"
      aria-modal="false"
      aria-label="Haru 轻量对话"
      tabIndex={-1}
      data-expand-direction={windowPosition.direction}
      style={{ left: windowPosition.left, top: windowPosition.top, width: surfaceSize.width }}
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
          <span
            role="status"
            aria-live="polite"
            aria-atomic="true"
            data-task-state={controller.taskState === 'idle' ? surface.taskState : controller.taskState}
          >
            {TASK_COPY[controller.taskState === 'idle' ? surface.taskState : controller.taskState]}
          </span>
        </div>
        <div className={styles.headerActions}>
          <button
            type="button"
            aria-label="展开到 Pilot 工作区"
            onClick={() => {
              surface.openPilot();
              onExpand?.();
            }}
          >
            <ExpandAltOutlined />
          </button>
          <button type="button" aria-label="关闭 Haru 对话" onClick={close}>
            <CloseOutlined />
          </button>
        </div>
      </header>

      <div className={styles.contextStack}>
        <div className={styles.context} aria-label="当前上下文">
          <span>当前上下文</span>
          <b>{contextLabel}{attachmentSuffix}</b>
        </div>

        {controller.contextChangeNotice ? (
          <div className={styles.contextChange} role="status" aria-label="页面上下文已变化">
            <div className={styles.contextRows}>
              <span>当前会话</span>
              <b>{controller.contextChangeNotice.currentConversationLabel}</b>
              <span>当前页面</span>
              <b>{controller.contextChangeNotice.currentPageLabel}</b>
            </div>
            <div className={styles.contextActions}>
              <button
                type="button"
                onClick={controller.switchToFollowingContext}
                disabled={controller.loading || Boolean(controller.pending)}
              >
                切换到当前页面
              </button>
              <button type="button" onClick={controller.dismissContextChangeNotice}>
                保持原上下文
              </button>
            </div>
          </div>
        ) : null}
      </div>

      <div className={styles.messages} aria-live="polite" aria-relevant="additions text">
        {controller.turns.length === 0 ? (
          <div className={styles.empty}>
            <strong>Haru 是 Pilot 的轻量窗口</strong>
            <span>
              简单问题可以在这里完成；需要查看资料、编辑内容或确认操作时，会展开到 Pilot。对话不会丢失。
            </span>
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
          <span>这一步会修改「{contextLabel}」的内容，需要在完整工作区确认。</span>
          <button
            type="button"
            data-testid="haru-open-pending"
            onClick={() => {
              surface.openPending();
              onExpand?.();
            }}
          >
            查看修改内容
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
