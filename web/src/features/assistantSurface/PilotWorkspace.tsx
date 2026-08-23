import { useCallback, useEffect, useRef } from 'react';
import ChatPanel, { type Props as ChatPanelProps } from '@/components/ChatPanel';
import { useAssistantSurface, usePilotConversationController } from './AssistantSurfaceProvider';
import styles from './AssistantSurface.module.css';

interface Props extends Omit<ChatPanelProps, 'variant' | 'open'> {
  pageActive?: boolean;
  variant?: NonNullable<ChatPanelProps['variant']>;
  open?: boolean;
}

export default function PilotWorkspace({
  pageActive = true,
  variant = 'page',
  open = true,
  onReplyLifecycle,
  onboardingFocusToken,
  ...props
}: Props) {
  const surface = useAssistantSurface();
  const controller = usePilotConversationController();
  const lifecycleForegroundRef = useRef(open || surface.surface === 'haru_chat');
  lifecycleForegroundRef.current = open || surface.surface === 'haru_chat';
  const reportReplyLifecycle = useCallback<NonNullable<ChatPanelProps['onReplyLifecycle']>>((event) => {
    onReplyLifecycle?.({
      ...event,
      background: event.background || !lifecycleForegroundRef.current,
    });
  }, [onReplyLifecycle]);
  useEffect(() => {
    if (pageActive) surface.openPilot();
  }, [pageActive, surface.openPilot]);
  return (
    <div className={`${pageActive ? styles.pilotWorkspace : styles.pilotControllerSurface} ${
      pageActive && controller.contextChangeNotice ? styles.pilotWorkspaceWithContext : ''
    }`}>
      {pageActive && controller.contextChangeNotice ? (
        <section className={styles.pilotContextChange} aria-label="页面上下文已变化" aria-live="polite">
          <div>
            <span>当前会话</span>
            <b>{controller.contextChangeNotice.currentConversationLabel}</b>
            <span>当前页面</span>
            <b>{controller.contextChangeNotice.currentPageLabel}</b>
          </div>
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
        </section>
      ) : null}
      <ChatPanel
        {...props}
        variant={variant}
        open={open}
        onboardingFocusToken={open ? onboardingFocusToken : undefined}
        onReplyLifecycle={reportReplyLifecycle}
      />
    </div>
  );
}
