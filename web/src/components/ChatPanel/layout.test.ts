import { describe, expect, it } from 'vitest';
import component from './index.tsx?raw';
import proposalCard from './ProposalCard.tsx?raw';
import thinking from './ThinkingIndicator.tsx?raw';
import messageBubble from './MessageBubble.tsx?raw';
import contextPanel from './ContextPanel.tsx?raw';
import evidenceList from './EvidenceList.tsx?raw';
import processTimeline from './ProcessTimeline.tsx?raw';
import threadRail from './ThreadRail.tsx?raw';
import composer from './Composer.tsx?raw';
import controller from '../../features/assistantSurface/usePilotConversationController.ts?raw';

async function loadCss(): Promise<string> {
  const fsModule = 'node:fs';
  const { readFileSync } = (await import(fsModule)) as {
    readFileSync: (path: URL, encoding: string) => string;
  };
  return readFileSync(new URL('./ChatPanel.module.css', import.meta.url), 'utf8');
}

async function loadThemeCss(): Promise<string> {
  const fsModule = 'node:fs';
  const { readFileSync } = (await import(fsModule)) as {
    readFileSync: (path: URL, encoding: string) => string;
  };
  return readFileSync(new URL('../../theme/tokens.css', import.meta.url), 'utf8');
}

describe('ChatPanel docked layout contract', () => {
  it('keeps a new-chat control visible when the docked layout hides the thread rail', () => {
    expect(component).toContain('styles.workspaceDocked');
    expect(component).toContain('<ThreadRail');
    expect(component).toContain('PlusOutlined');
    expect(component).toContain('aria-label="新建对话"');
    expect(component).toContain('docked &&');
  });

  it('lets the docked Pilot expose an action that opens the Pilot tab', () => {
    expect(component).toContain('onExpand');
    expect(component).toContain('ExpandAltOutlined');
    expect(component).toContain('aria-label="打开 Pilot tab"');
  });

  it('supports a normal Pilot tab page variant that reuses the expanded workspace', () => {
    expect(component).toContain("variant?: 'drawer' | 'rail' | 'page'");
    expect(component).toContain("const inlinePage = variant === 'page'");
    expect(component).toContain('if (docked || inlinePage) return panelWorkspace');
  });

  it('keeps the immersive exit action inside the existing page header', async () => {
    const css = await loadCss();

    expect(component).toContain('onExitPage?: () => void');
    expect(component).toContain('inlinePage && onExitPage');
    expect(component).toContain('data-testid="pilot-exit-immersive"');
    expect(component).toContain('退出沉浸模式，返回原页面');
    expect(component).toContain('styles.pageExit');
    expect(css).toContain('.pageExit');
    expect(css).toContain('min-height: 40px;');
    expect(css).toContain('.pageExit:focus-visible');
  });

  it('gives the immersive workspace a calm inset frame instead of touching viewport edges', async () => {
    const css = await loadCss();
    const themeCss = await loadThemeCss();

    expect(themeCss).toMatch(
      /\.op-pilot-page-host\s*\{[^}]*box-sizing:\s*border-box;[^}]*padding:\s*clamp\(10px, 1vw, 16px\);/s,
    );
    expect(css).toMatch(
      /\.workspacePage\s*\{[^}]*border:\s*1px solid var\(--op-border\);[^}]*border-radius:\s*16px;[^}]*overflow:\s*hidden;/s,
    );
    expect(css).toMatch(/\.workspacePage \.header\s*\{[^}]*padding:\s*12px 16px;/s);
    expect(css).toMatch(/\.workspacePage \.rail\s*\{[^}]*padding:\s*14px 12px 12px;/s);
    expect(css).toMatch(/\.workspacePage \.context\s*\{[^}]*padding:\s*16px 12px 12px 16px;/s);
    expect(css).toMatch(/\.workspacePage \.composer\s*\{[^}]*padding:\s*12px 18px 16px;/s);
  });

  it('mounts Kanban drop targeting inside the visible rail or drawer surface', () => {
    expect(component).toContain("from '@/components/KanbanBoard/PilotContextDropTarget'");
    expect(component).toContain('pilotDropTarget?: boolean');
    expect(component).toContain('const panelWorkspace = pilotDropTarget ? (');
    expect(component).toContain('<NativePilotAttachmentDropSurface');
    expect(component).toContain('<PilotContextDropTarget disabled={composerDisabled} onNativeDrop={addAttachment}>');
    expect(component).toContain('if (docked || inlinePage) return panelWorkspace');
    expect(component).toContain('{panelWorkspace}');
  });

  it('limits confirmation cards in the full Pilot page without narrowing the rail drawer', async () => {
    const css = await loadCss();

    expect(component).toContain('inlinePage ? styles.workspacePage');
    expect(css).toContain('.workspacePage .pendingDock');
    expect(css).toContain('max-width: 720px;');
    expect(css).toContain('.workspaceDocked .pendingDock .proposal');
    expect(css).toContain('max-width: 100%;');
  });

  it('shows an inline API-key setup notice when the docked context panel is hidden', () => {
    expect(component).toContain('styles.inlineKeyNotice');
    expect(component).toContain('!hasKey &&');
    expect(component).toContain('onOpenSettings');
  });

  it('keeps failed user drafts retryable instead of dropping them', () => {
    expect(component).toContain('lastFailedText');
    expect(component).toContain('retryLastMessage');
    expect(component).toContain('clearLastFailure');
    expect(component).toContain('关闭提示');
    expect(component).toContain('disabledReason={composerDisabledReason}');
  });

  it('uses concrete waiting states while AI is working', () => {
    expect(thinking).toContain('WAITING_STEPS');
    expect(thinking).toContain('label?: string');
    expect(thinking).toContain('正在理解你的问题');
    expect(thinking).toContain('正在调用工具读取上下文');
    expect(thinking).toContain('正在等待模型返回结果');
    expect(thinking).toContain('正在整理结论和下一步建议');
    expect(component).toContain('loadingLabel');
    expect(component).toContain('<ThinkingIndicator label={loadingLabel}');
  });

  it('lets users stop an in-flight assistant response', () => {
    expect(component).toContain('activeRequestRef');
    expect(component).toContain('stopActiveRequest');
    expect(controller).toContain('activeRequest.controller.abort()');
    expect(component).toContain('isAbortError');
    expect(component).toContain('aria-label="停止当前回复"');
    expect(component).toContain('已停止当前回复');
  });

  it('uses Pilot SSE stream services for chat and confirmation flows', () => {
    expect(component).toContain('streamChat');
    expect(component).toContain('streamConfirmationRequest');
    expect(component).toContain('streamLoadingLabel');
    expect(component).toContain('appendAssistantDelta');
    expect(component).toContain("event.event === 'assistant_delta'");
    expect(component).not.toContain('sendChat,');
    expect(component).not.toContain('confirmAction,');
  });

  it('snapshots current context attachments into the chat stream request', () => {
    expect(controller).toContain('mergePilotRequestAttachments(');
    expect(controller).toContain('input.draftContext?.attachments');
    expect(component).toContain('const requestContext');
    expect(component).toContain('streamChatRequest(requestLease, trimmed, convID, requestContext');
  });

  it('hides the thinking indicator once assistant text is streaming', () => {
    expect(component).toContain('hasStreamingAssistantContent');
    expect(component).toContain('setHasStreamingAssistantContent(true)');
    expect(component).toContain('loading && !activePending && !hasStreamingAssistantContent');
  });

  it('resynchronizes real conversation state after users abort a stream', () => {
    expect(component).toContain('syncConversationAfterAbort');
    expect(component).toContain('await syncConversationAfterAbort(streamConversationId, visibleRequestGeneration)');
    expect(component).toContain('await syncConversationAfterAbort(convID, visibleRequestGeneration)');
    expect(component).toContain('const selectedPending = pendingActionForConversation');
    expect(component).toContain('setPending(selectedPending);');
  });

  it('cleans up partial assistant bubbles on stream failure and completed fallback', () => {
    expect(component).toContain('if (streamingAssistantActiveRef.current)');
    expect(component).toContain('await syncConversationAfterAbort(streamConversationId, visibleRequestGeneration)');
    expect(component).toContain('return [...t.slice(0, -1), { ...last, content: resp.message }];');
  });

  it('notifies owners after Pilot write flows can change application data', () => {
    expect(component).toContain('onDataChanged?: () => void');
    expect(component).toContain('if (applied && autoApprove) onDataChanged?.();');
    expect(component).toContain('if (approved && applied) onDataChanged?.();');
    expect(component).toContain("resp.write_status === 'success'");
  });

  it('keeps write confirmation cards localized for Chinese users', () => {
    expect(proposalCard).not.toContain('<span>Target</span>');
    expect(proposalCard).not.toContain('<span>Proposed</span>');
    expect(proposalCard).not.toContain('Status ->');
    expect(proposalCard).not.toContain('Evidence is limited');
    expect(proposalCard).toContain('FIELD_LABELS');
    expect(proposalCard).toContain('STATUS_LABELS');
    expect(proposalCard).toContain('EVENT_TYPE_LABELS');
    expect(proposalCard).toContain("duration_minutes: '时长'");
    expect(proposalCard).toContain('参考依据较少');
  });

  it('renders pending write confirmations in a visible dock above the composer', () => {
    expect(component).toContain('styles.pendingDock');
    expect(component).toContain('{activePending && (');
    expect(component.indexOf('styles.pendingDock')).toBeGreaterThan(component.indexOf('</div>'));
    expect(component.indexOf('styles.pendingDock')).toBeLessThan(component.indexOf('<Composer'));
  });

  it('caps oversized confirmation cards and truncates long field values', async () => {
    const css = await loadCss();

    expect(css).toContain('max-height: min(560px, calc(100vh - 140px));');
    expect(css).toContain('overflow-y: auto;');
    expect(css).toContain('-webkit-line-clamp: 4;');
    expect(css).toContain('text-overflow: ellipsis;');
    expect(proposalCard).toContain('className={styles.changeValue}');
    expect(proposalCard).toContain('title={rawAfterText}');
  });

  it('summarizes multi-step write cards and long review fields', () => {
    expect(proposalCard).toContain('action.workflow');
    expect(proposalCard).toContain('第 {action.workflow.current_step} / {action.workflow.total_steps} 步');
    expect(proposalCard).toContain('summarizeLongValue');
    expect(proposalCard).toContain('longDraftFields');
    expect(proposalCard).toContain('action.draft_summary');
    expect(proposalCard).toContain('草稿审阅');
    expect(proposalCard).toContain('长内容已按摘要展示，确认后会完整保存。');
    expect(proposalCard).toContain('action.risk_hint');
    expect(component).toContain('pendingComposerDisabledReason(activePending)');
  });

  it('surfaces pending confirmation recovery and the active conversation context', () => {
    expect(component).toContain('confirmError');
    expect(component).toContain('retryConfirmAction');
    expect(component).toContain('如需放弃，请使用下方审核卡片');
    expect(component).toContain('contextBadge');
    expect(component).toContain('当前上下文');
    expect(component).toContain('contextLabel');
    expect(component).toContain('lastConfirmationInputRef');
    expect(component).toContain('confirmationInputForRetry(lastConfirmationInputRef.current)');
    expect(component).toContain('void handleConfirm(retryInput)');
    expect(component).toContain('confirmationErrorRequiresSync');
    expect(component).toContain('lastConfirmationInputRef.current = null');
    expect(component).toContain('await syncConversationAfterAbort(convID, visibleRequestGeneration)');
    expect(component).not.toContain('void handleConfirm(true)');
  });

  it('keeps rejection deliberate when confirmation recovery is visible', () => {
    const recoveryStart = component.indexOf('{confirmError ? (');
    const proposalStart = component.indexOf('<ProposalCard', recoveryStart);
    const recoverySource = component.slice(recoveryStart, proposalStart);

    expect(recoveryStart).toBeGreaterThan(-1);
    expect(proposalStart).toBeGreaterThan(recoveryStart);
    expect(recoverySource).toContain('retryConfirmAction');
    expect(recoverySource).toContain('role="alert"');
    expect(recoverySource).toContain('lastConfirmationInputRef.current?.approved === false');
    expect(recoverySource).toContain('重试拒绝');
    expect(recoverySource).toContain('重试会继续提交拒绝');
    expect(recoverySource).not.toContain('handleConfirm({');
    expect(recoverySource).not.toContain('approved: false');
    expect(controller.match(/approved: false/g)).toHaveLength(1);
    expect(component.match(/onCancel=\{rejectPending\}/g)).toHaveLength(1);
  });

  it('gives confirmation recovery a compact accessible retry target', async () => {
    const css = await loadCss();
    const buttonStart = css.indexOf('.confirmRecovery button {');
    const disabledStart = css.indexOf('.confirmRecovery button:disabled', buttonStart);
    const buttonRule = css.slice(buttonStart, disabledStart);
    const reducedMotionStart = css.indexOf('@media (prefers-reduced-motion: reduce)');
    const reducedMotionRule = css.slice(reducedMotionStart);

    expect(buttonStart).toBeGreaterThan(-1);
    expect(buttonRule).toContain('min-height: 40px;');
    expect(buttonRule).toContain('padding: 0 12px;');
    expect(buttonRule).toContain('transition-property: color, background-color, transform;');
    expect(css).toContain('.confirmRecovery button:active:not(:disabled)');
    expect(css).toContain('transform: scale(0.96);');
    expect(css).toContain('flex-wrap: wrap;');
    expect(reducedMotionRule).toContain('.confirmRecovery button');
    expect(buttonRule).not.toContain('transition: all');
  });

  it('renders typed accessible proposal controls without an arbitrary JSON editor', () => {
    expect(proposalCard).toContain("Select");
    expect(proposalCard).toContain("Switch");
    expect(proposalCard).toContain("InputNumber");
    expect(proposalCard).toContain("DatePicker");
    expect(proposalCard).toContain("showTime");
    expect(proposalCard).toContain("allowClear={descriptor.clearable === true}");
    expect(proposalCard).toContain("descriptor.clear_value");
    expect(proposalCard).toContain("precision={0}");
    expect(proposalCard).toContain("action.args?.[descriptor.field]");
    expect(proposalCard).toContain("Input.TextArea");
    expect(proposalCard).toContain("<Input");
    expect(proposalCard).toContain("htmlFor={controlId}");
    expect(proposalCard).toContain("const common = { id: controlId");
    expect(proposalCard.match(/size="large"/g)?.length ?? 0).toBeGreaterThanOrEqual(5);
    expect(proposalCard).toContain("styles.switchHitArea");
    expect(proposalCard).toContain('<label className={styles.switchHitArea} htmlFor={controlId}>');
    expect(proposalCard).toContain("编辑建议");
    expect(proposalCard).not.toContain("JSON.stringify(action.args");
    expect(proposalCard).not.toContain("Monaco");
  });

  it('uses a two-step rejection with bounded optional feedback', () => {
    expect(proposalCard).toContain("setRejectOpen(true)");
    expect(proposalCard).toContain("maxLength={500}");
    expect(proposalCard).toContain("feedback.trim() || undefined");
    expect(proposalCard).toContain("返回审核");
    expect(proposalCard).toContain("最终拒绝");
    expect(proposalCard).toContain("pendingFocusTargetRef");
    expect(proposalCard).toContain("document.getElementById(targetId)?.focus()");
    expect(proposalCard).toContain('role="region"');
    expect(proposalCard).not.toContain("onConfirm(rejectionFeedback");
  });

  it('keeps proposal editing compact, tactile, and overflow-safe', async () => {
    const css = await loadCss();

    expect(css).toContain('.proposalEditor');
    expect(css).toContain('.editorGrid');
    expect(css).toContain('.editorDisclosure');
    expect(css).toContain('.reviewBack');
    expect(css).toContain('min-height: 40px;');
    expect(css).toContain('transform: scale(0.96);');
    expect(css).toContain('transition-property: background-color, color, box-shadow, transform;');
    expect(css).toContain('overflow-wrap: anywhere;');
    expect(css).toContain('.switchHitArea');
    expect(css).not.toContain('.editorControl:global(.ant-switch)');
    expect(css).not.toContain('.editorField :global(.ant-select-selector)');
    expect(css).not.toContain('transition: all');
  });

  it('attaches the current pending token to edited approval and rejection intents', () => {
    expect(proposalCard).toContain("onConfirm: (editedArgs?: Record<string, unknown>) => void");
    expect(proposalCard).toContain("onCancel: (rejectionFeedback?: string) => void");
    expect(controller).toContain("...(editedArgs ? { edited_args: editedArgs } : {})");
    expect(controller).toContain("...(rejectionFeedback ? { rejection_feedback: rejectionFeedback } : {})");
    expect(controller).toContain("confirmation_token: action.confirmation_token");
    expect(component).toContain("key={`${convID}:${activePending.confirmation_token}`}");
    expect(component).toContain("retryInput.confirmation_token !== activePending.confirmation_token");
    expect(component).toContain("input.confirmation_token !== activePendingRef.current?.confirmation_token");
  });

  it('keeps recovery mounted and restores retry focus after a repeated failure', () => {
    const confirmStart = component.indexOf('async function handleConfirm(input: ConfirmationInput)');
    const retryStart = component.indexOf('function retryConfirmAction()', confirmStart);
    const confirmSource = component.slice(confirmStart, retryStart);
    const requestStartSource = confirmSource.slice(0, confirmSource.indexOf('setConfirmPhase('));

    expect(requestStartSource).not.toContain('setConfirmError(null);');
    expect(component).toContain('confirmRetryButtonRef');
    expect(component).toContain('restoreConfirmationRetryFocusRef');
    expect(component).toContain('shouldRestoreConfirmationRetryFocus(');
    expect(component).toContain('confirmRetryButtonRef.current?.focus()');
    expect(component).toContain('ref={confirmRetryButtonRef}');
    expect(component).toMatch(/setConfirmError\(null\);\s+setConvID\(resp\.conversation_id\)/);
  });

  it('keeps removable request page context separate from durable conversation context', async () => {
    const css = await loadCss();

    expect(component).toContain('pageContext?: PilotPageContext');
    expect(component).not.toContain('useState<PilotPageContext | undefined>(() => pageContext)');
    expect(component).not.toContain('setActivePageContext');
    expect(component).toContain('pageContextKey(pageContext)');
    expect(component).toContain('pilotPageContextRemovalReducer,');
    expect(component).toContain('deriveActivePageContext(pageContext, pageContextRemovalState)');
    expect(component).toContain("type: 'sync', contextKey: incomingPageContextKey");
    expect(component).toContain('}, [incomingPageContextKey]);');
    expect(component).toContain('buildRequestContext({');
    expect(controller).toContain('buildChatRequestContext({');
    expect(component).toContain('const requestPageContext = convID === undefined ? activePageContext : pinnedContext');
    expect(component).toContain('pageContext: requestPageContext');
    expect(component).toContain('pageContextChips(activePageContext)');
    expect(component).toContain("type: 'remove', contextKey: incomingPageContextKey, chipKey");
    expect(component).toContain('aria-label={`\u79fb\u9664${chip.label}`}');
    expect(component).toContain('disabled={loading}');
    expect(component).toContain('styles.requestContextRow');
    expect(component).toContain('styles.contextBadge');
    expect(css).toContain('.requestContextRow');
    expect(css).toContain('flex-wrap: wrap;');
    expect(css).toContain('min-width: 0;');
  });

  it('does not mix persistent context fields into existing conversation requests', () => {
    expect(component).toContain('conversationId: convID');
    expect(component).toContain('buildRequestContext({');
    expect(component).toContain('streamChatRequest(requestLease, trimmed, convID, requestContext');
  });

  it('scopes attachment drafts to the displayed conversation or fresh request', () => {
    expect(component).toContain("import { usePilotAttachments } from '@/features/pilot/PilotAttachmentContext'");
    expect(component).toContain('setActiveConversationKey(`conversation:${convID}`)');
    expect(component).toContain('setActiveConversationKey(`new:${draftContext.requestKey}`)');
    expect(component).toContain('ensureNewAttachmentDraft()');
    expect(component).toContain('beginNewAttachmentDraft()');
    expect(component).toMatch(
      /handoffAttachmentKeyRef\.current !== undefined\s*&&\s*handoffAttachmentKeyRef\.current === activeAttachmentKey/,
    );
  });

  it('clears only the send-time attachment draft after a successful send', () => {
    expect(component).toContain('activeKey: activeAttachmentKey');
    expect(component).toContain('const attachmentDraftKeyAtSend =');
    expect(component).toContain('clearAttachmentsByKey(attachmentDraftKeyAtSend)');
    expect(component).not.toContain('clearAttachments()');
  });

  it('keeps attachment controls in the draft UI without changing the chat stream payload', async () => {
    const css = await loadCss();

    expect(component).toContain('ContextAttachmentRail');
    expect(component).toContain('attachments={attachments}');
    expect(component).toContain('onRemove={removeAttachment}');
    expect(component).toContain('onNativeDrop={addAttachment}');
    expect(component).toContain('pilotQuickQuestions(attachments)');
    expect(component).toContain('suggestions={attachmentSuggestions}');
    expect(css).toContain('.contextAttachmentRail');
    expect(css).toContain('.contextAttachmentRemove');
    expect(css).toContain('.quickQuestion');
    expect(css).toContain('min-height: 40px;');
    expect(component).toContain('streamChatRequest(requestLease, trimmed, convID, requestContext');
    expect(component).not.toContain('streamChatRequest(requestLease, trimmed, convID, requestContext, attachments');
  });

  it('lets users manage conversations and remove active context from the Pilot UI', () => {
    expect(threadRail).toContain('PushpinOutlined');
    expect(threadRail).toContain('EditOutlined');
    expect(threadRail).toContain('InboxOutlined');
    expect(threadRail).toContain('onUpdate');
    expect(threadRail).toContain('stopActionPropagation');
    expect(threadRail).toContain('onKeyDown={stopActionPropagation}');
    expect(component).toContain('updateConversation');
    expect(component).toContain('clearActiveContext');
    expect(component).toContain('aria-label="移除当前上下文"');
  });

  it('supports searchable active and archive conversation views with recovery actions', async () => {
    const css = await loadCss();

    expect(threadRail).toContain('aria-label="搜索对话"');
    expect(threadRail).toContain("view === 'archived'");
    expect(threadRail).toContain('待确认');
    expect(threadRail).toContain('该对话有待确认操作，完成或取消后才能归档');
    expect(threadRail).toContain("onUpdate(conversation.id, { archived: false })");
    expect(threadRail).toContain('恢复对话');
    expect(threadRail).toContain('GROUP_LABELS');
    expect(component).toContain('refreshConversations(showArchived)');
    expect(component).toContain('listConversations(includeArchived)');
    expect(component).toContain('showArchived={showArchived}');
    expect(component).toContain('onViewChange={setShowArchived}');
    expect(css).toContain('min-height: 40px;');
    expect(css).toContain('overflow-x: hidden;');
    expect(css).toContain('@media (prefers-reduced-motion: reduce)');
  });

  it('suppresses pending auto-selection after an explicit new chat and locks only the active thread', () => {
    expect(component).toContain('pendingAutoSelectSuppressedRef');
    expect(component).toContain('conversationSelectionRequestRef');
    expect(component).toContain('shouldApplyConversationRequest(');
    expect(component).toContain('cancelConversationSelection();');
    expect(component).toContain('visibleRequestGeneration: number,');
    expect(component).toContain('!isCurrentVisibleRequest(visibleRequestGeneration)');
    expect(component).toContain("markPendingAutoSelect('suppress')");
    expect(component).toContain("markPendingAutoSelect('allow')");
    expect(component).toContain('if (pendingAutoSelectSuppressedRef.current) return;');
    expect(component).toContain('resolveActivePendingAction(pending, conversations, convID)');
    expect(component).toContain('const composerDisabled = loading || !!activePending || !hasKey;');
    expect(component).not.toContain('conversations.some((conversation) => conversation.pending_action)');
  });

  it('isolates visible chat and confirmation mutations by request generation', () => {
    expect(component).toContain('visibleRequestGenerationRef');
    expect(component).toContain('const visibleRequestGeneration = ++visibleRequestGenerationRef.current;');
    expect(component).toContain('isCurrentVisibleConversationRequest(');
    expect(component).toContain('if (!isCurrentVisibleRequest(visibleRequestGeneration))');
    expect(component).toContain('finishMessage(resp, visibleRequestGeneration)');
  });

  it('keeps active requests on close while durable context resets and explicit replacement stay fenced', () => {
    const contextStart = component.indexOf('if (offerId !== threadOfferId.current) {');
    const contextEnd = component.indexOf('threadOfferId.current = offerId;', contextStart);
    const contextReset = component.slice(contextStart, contextEnd);

    expect(component).toContain('const conversationSurfaceActive = open || controllerActive;');
    expect(component).toContain('openRef.current = conversationSurfaceActive;');
    expect(component).toContain('background: !openRef.current');
    expect(component).toContain('confirmationLocksRef.current.set(convID, confirmationExecution);');
    expect(component).toContain("beginActiveRequest('confirmation'");
    expect(component).toContain('input.confirmation_token');
    expect(controller).toContain('...(confirmationToken ? { confirmationToken } : {})');
    expect(component).toContain(
      'clearOwnedConfirmationLock(',
    );
    expect(component).toContain('if (confirmationLocksRef.current.has(convID)) return;');
    expect(component).toContain('lockedConfirmationRef.current = confirmationExecution;');
    expect(component).toContain(
      'const confirmationSettled = hasConfirmationSettled(',
    );
    expect(component).toContain("setConfirmPhase('saving');");
    expect(component).toContain('const selectedConfirmationLock = confirmationLocksRef.current.get(id);');
    expect(component).toContain('if (selectedConfirmationLock) {');
    const newChatStart = component.indexOf('function canStartNewChat()');
    const newChatEnd = component.indexOf('async function selectConversation', newChatStart);
    const newChat = component.slice(newChatStart, newChatEnd);
    expect(newChat).toContain("if (activeRequestRef.current?.kind === 'confirmation')");
    expect(newChat).toContain('操作确认仍在处理中，请稍候');
    expect(newChat).toContain('return false;');
    expect(newChat).toContain('return true;');
    expect(newChat).toContain('shouldAbortActiveRequestOnReplacement(activeRequestRef.current)');
    expect(component).not.toContain('confirmationExecution.settled = true;');
    expect(component).toContain('CONFIRMATION_RECONCILE_MAX_POLLS = 240');
    expect(component).toContain('确认操作仍在处理中，请刷新状态后再继续。');
    expect(component).toContain('onClick={refreshConfirmationStatus}');
    expect(contextReset).toContain('visibleRequestGenerationRef.current += 1;');
    expect(contextReset).toContain('setLoading(false);');
    expect(contextReset).toContain('setLoadingLabel(undefined);');

    const confirmationResponseStart = component.indexOf(
      'const resp = await streamConfirmationRequest',
    );
    const staleCompletionStart = component.indexOf(
      'if (!isCurrentVisibleRequest(visibleRequestGeneration)) {',
      confirmationResponseStart,
    );
    const staleCompletionEnd = component.indexOf(
      'clearOwnedConfirmationLock(confirmationLocksRef.current, convID',
      staleCompletionStart,
    );
    const staleCompletion = component.slice(staleCompletionStart, staleCompletionEnd);
    expect(staleCompletion).toContain('refreshConversations();');
    expect(staleCompletion).not.toContain('clearOwnedConfirmationLock(');
  });

  it('fences stale new-conversation completions before mutating the visible conversation', () => {
    const responseStart = component.indexOf('const resp = await streamChatRequest');
    const staleFence = component.indexOf(
      'if (!isCurrentVisibleRequest(visibleRequestGeneration)) {',
      responseStart,
    );
    const conversationMutation = component.indexOf('setConvID(resp.conversation_id);', staleFence);
    expect(staleFence).toBeGreaterThan(responseStart);
    expect(conversationMutation).toBeGreaterThan(staleFence);
  });

  it('clears draft context before activating an existing conversation', () => {
    const selectionStart = component.indexOf('async function selectConversation(id: number)');
    const draftReset = component.indexOf('setDraftContext(null);', selectionStart);
    const composerReset = component.indexOf("setComposerDraft('');", selectionStart);
    const contextActivation = component.indexOf('activateConversationContext(id);', selectionStart);
    expect(draftReset).toBeGreaterThan(selectionStart);
    expect(composerReset).toBeGreaterThan(draftReset);
    expect(contextActivation).toBeGreaterThan(composerReset);
  });

  it('keeps the shared busy state while a background confirmation owns the request lease', () => {
    const selectionStart = component.indexOf('async function selectConversation(id: number)');
    const selectionEnd = component.indexOf('async function removeConversation', selectionStart);
    const selection = component.slice(selectionStart, selectionEnd);
    expect(selection).toContain('const requestId = beginConversationSelection();');
    expect(selection).toContain('finishConversationSelection(requestId);');
    expect(controller).toContain('activeConversationSelectionRef.current !== null');
    expect(controller).toContain('setLoading(activeConversationSelectionRef.current !== null);');
    expect(controller).toContain('setLoading(activeRequestRef.current !== null);');
    expect(component).toContain('if (!canStartNewChat()) return;');
    expect(component).toContain('if (onStartRequestConsumed && !onStartRequestConsumed(startRequest.requestKey))');
    expect(component).toContain('resetForNewChat();');
    expect(component).toContain('acceptedStartRequestKeyRef.current === startRequest.requestKey');
    expect(component).toContain('[startRequest?.requestKey, loading, onStartRequestConsumed]');
  });

  it('guards conversation list refreshes against stale view responses', () => {
    expect(component).toContain('conversationListRequestRef');
    expect(component).toContain('showArchivedRef.current = showArchived;');
    expect(component).toContain('const includeArchived = showArchivedRef.current;');
    expect(component).toContain('const requestId = ++conversationListRequestRef.current;');
    expect(component).toContain('if (requestId === conversationListRequestRef.current)');
  });

  it('keeps thread selection separate from row actions for accessible keyboard semantics', () => {
    expect(threadRail).toContain('className={styles.threadSelect}');
    expect(threadRail).not.toContain('role="button"');
    expect(threadRail).not.toContain('tabIndex={0}');
  });

  it('shows confirmation status and lets users undo the latest AI write', () => {
    expect(component).toContain('confirmPhase');
    expect(component).toContain('保存成功');
    expect(component).toContain('lastUndo');
    expect(component).toContain('undoLastWrite');
    expect(component).toContain('撤销最近一次 AI 写入');
    expect(component).toContain("confirmError || '保存失败'");
    expect(component).toContain('当前记录已被修改，无法安全撤销。现有内容已保留。');
  });

  it('keeps the context evidence panel fully localized for Chinese users', () => {
    expect(contextPanel).toContain('当前参考依据');
    expect(contextPanel).toContain('暂无参考依据');
    expect(evidenceList).toContain('aria-label="参考依据"');
    expect(contextPanel).not.toContain('Current evidence');
    expect(contextPanel).not.toContain('No evidence collected yet');
    expect(evidenceList).not.toContain('Evidence sources');
  });

  it('refreshes a new conversation after background title generation', () => {
    expect(component).toContain('titleRefreshTimeoutsRef');
    expect(component).toContain('scheduleTitleRefresh');
    expect(component).toContain('window.setTimeout');
  });

  it('resets the active conversation for every application start request', () => {
    expect(component).toContain('startRequest?.requestKey');
    expect(component).toContain('setConvID(undefined)');
    expect(component).toContain('setTurns([])');
    expect(component).toContain('draftContext');
  });

  it('does not auto-resume an older pending thread after an application start request', () => {
    expect(component).toContain("markPendingAutoSelect('suppress')");
    expect(component).toContain('pendingAutoSelectSuppressedRef.current');
  });

  it('clears unsent composer text when an application start request opens a new draft', () => {
    expect(component).toContain('composerResetKey');
    expect(component).toContain('resetKey={composerResetKey}');
  });

  it('uses structured write status instead of treating every message response as success', () => {
    expect(component).toContain('resp.write_status === \'success\'');
    expect(component).toContain('resp.write_status === \'failed\'');
    expect(component).toContain('resp.write_error');
  });

  it('bounds and diversifies context and proposal evidence without merging record ids', () => {
    expect(contextPanel).toContain('selectEvidence(evidence, 5)');
    expect(contextPanel).toContain('similar={evidenceSelection.similar}');
    expect(contextPanel).toContain('remainingCount={evidenceSelection.remainingCount}');
    expect(proposalCard).toContain('selectEvidence');
    expect(proposalCard).toContain('visibleEvidence.slice(0, 3)');
  });

  it('renders expandable evidence and timeline limits with native controls', async () => {
    const css = await loadCss();

    expect(evidenceList).toContain('`另有 ${similar.length} 条同类依据`');
    expect(evidenceList).toContain('aria-expanded={expanded}');
    expect(evidenceList).toContain('...remaining');
    expect(evidenceList).toContain('formatEvidenceMeta(item.meta)');
    expect(evidenceList).toContain('title={item.meta}');
    expect(evidenceList).toContain('evidenceSetIdentity(items, similar, remaining)');
    expect(evidenceList).toContain('remaining = similar');
    expect(evidenceList).toContain("{expanded ? '收起依据' : '展开依据'}");
    expect(processTimeline).toContain('const visibleSteps = expandedSteps ? steps : steps.slice(0, 8);');
    expect(processTimeline).toContain('const evidenceSelection = selectEvidence(step.evidence ?? [], 8);');
    expect(processTimeline).toContain('similar={evidenceSelection.similar}');
    expect(processTimeline).toContain('remainingCount={evidenceSelection.remainingCount}');
    expect(processTimeline).toContain('remaining={remainingEvidence(step.evidence ?? [], evidenceSelection.visible)}');
    expect(processTimeline).toContain('open ? (');
    expect(processTimeline).toContain('const stepSetIdentity = toolStepSetIdentity(steps);');
    expect(processTimeline).toContain('useLayoutEffect(() => {');
    expect(processTimeline).toContain('}, [stepSetIdentity]);');
    expect(processTimeline).toContain('`还有 ${remainingSteps} 步`');
    expect(processTimeline).toContain('aria-expanded={expandedSteps}');
    expect(processTimeline).toContain('<button');
    expect(processTimeline).not.toContain('role="button"');
    expect(css).toContain('.evidenceExpand');
    expect(css).toContain('.timelineExpand');
    expect(css).toContain('min-height: 40px;');
    expect(css).toMatch(/\.evidenceControlsCompact\s*\{\s*margin-left:\s*0;/);
    expect(css).toMatch(/\.tlHead,\s*\.timelineExpand,\s*\.evidenceExpand,/);
    expect(css).toContain('@media (prefers-reduced-motion: reduce)');
  });

  it('routes Pilot task actions through the guarded chat send flow', () => {
    expect(messageBubble).toContain('PilotTaskCard');
    expect(messageBubble).toContain('actionsDisabled');
    expect(messageBubble).toContain('taskCardsEnabled');
    expect(component).toContain('actionsDisabled={composerDisabled}');
    expect(component).toContain('继续处理：${action}');
  });

  it('focuses and highlights the Pilot composer from an onboarding token without sending a message', async () => {
    const css = await loadCss();

    expect(component).toContain('onboardingFocusToken?: number;');
    expect(component).toContain('onOnboardingFocusConsumed?: (token: number) => void;');
    expect(component).toContain('data-onboarding-target="pilot"');
    expect(component).toContain('styles.onboardingFocus');
    expect(component).toContain('onboardingFocusEventToken');
    expect(component).toContain('onboardingFocusToken={onboardingFocusEventToken}');
    expect(component).toContain('onOnboardingFocusConsumed?.(onboardingFocusToken);');
    expect(component).toContain('}, [onboardingFocusEventToken]);');
    expect(component).toContain('role="status"');
    expect(component).toContain('aria-live="polite"');
    expect(component).toContain('Pilot 输入框已就绪');
    expect(composer).toContain('onboardingFocusToken?: number;');
    expect(composer).toContain('styles.composerOnboardingFocus');
    expect(composer).toContain("composerRef.current?.querySelector('textarea')?.focus()");
    expect(composer).not.toContain('onSend(onboardingFocusToken');
    expect(css).toContain('@keyframes onboardingPilotPulse');
    expect(css).toContain('.onboardingFocus');
    expect(css).toContain('.composerOnboardingFocus');
    expect(css).toContain('@media (prefers-reduced-motion: reduce)');
  });
});
