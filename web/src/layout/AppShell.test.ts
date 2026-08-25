import { describe, expect, it } from 'vitest';
import source from './AppShell.tsx?raw';
import calendarView from '@/components/CalendarView.tsx?raw';
import offerCenterView from '@/components/OfferCenterView.tsx?raw';
import resumeLibraryView from '@/components/ResumeLibraryView.tsx?raw';
import { createOpportunityFitDraftStore } from '@/features/pilot/opportunityFitDraft';
import { runPilotTriage } from '@/features/pilot/pilotOpportunityFitLifecycle';
import { consumeMaterialKitHandoff, writeMaterialKitHandoff } from '@/features/pilot/materialKitHandoff';

describe('AppShell source contract', () => {
  it('closes stale application detail when a selected application disappears', () => {
    expect(source).toContain('!apps.some((app) => app.id === selected.id)');
    expect(source).toContain('setSelected(null)');
  });

  it('refreshes workspace data after Pilot writes complete', () => {
    expect(source).toContain('const refreshWorkspaceData = () => {');
    expect(source).toContain("queryKey: ['applications']");
    expect(source).toContain("queryKey: ['events']");
    expect(source).toContain("queryKey: ['offers']");
    expect(source).toContain("queryKey: ['questions', 'stats']");
    expect(source).toContain('onDataChanged={refreshWorkspaceData}');
  });

  it('exercises the Pilot runner with invalid provider output and frozen handoff consumption', async () => {
    const store = createOpportunityFitDraftStore(7, 'draft-1');
    store.dispatch({ type: 'set_resume', resumeID: 3 });
    store.dispatch({ type: 'set_jd', jdText: 'JD' });

    await runPilotTriage({
      store,
      applicationId: 7,
      pilotDraftKey: 'draft-1',
      draft: store.getState(),
      existingKey: 'attempt-1',
      createReview: async () => ({ invalid: true } as never),
    });

    expect(store.getState().phase).toBe('confirm_triage');
    expect(store.getState().triageAttemptKey).toBe('attempt-1');
    expect(store.getState().actionError).toBeTruthy();

    writeMaterialKitHandoff({
      applicationId: 7,
      resumeId: 3,
      jdText: 'JD',
      jdVersionId: 1,
    });
    expect(consumeMaterialKitHandoff(7)?.jdText).toBe('JD');
    expect(consumeMaterialKitHandoff(7)).toBeNull();
  });

  it('restores historical Pilot reviews through the frozen review APIs', () => {
    expect(source).toContain('listOpportunityFitV2Reviews');
    expect(source).toContain('getOpportunityFitV2Review');
    expect(source).toContain('listOpportunityFitReviews');
    expect(source).toContain('getOpportunityFitReview');
    expect(source).toContain("['opportunity-fit-v2-reviews'");
    expect(source).toContain('viewPilotV2History');
    expect(source).toContain('viewPilotLegacyHistory');
    expect(source).toContain('setPilotLegacyReview');
    expect(source).not.toContain('const latest = summaries[0]');
    expect(source).toContain('legacyHistory={pilotLegacyHistoryQuery.data ?? []}');
    expect(source).toContain('history={pilotV2HistoryQuery.data ?? []}');
    expect(source).toContain('handlePilotNotFound');
  });

  it('clears the Pilot context when the historical list is no longer visible', () => {
    expect(source).toContain('isOpportunityFitNotFoundError(pilotV2HistoryQuery.error)');
    expect(source).toContain('isOpportunityFitNotFoundError(pilotLegacyHistoryQuery.error)');
    expect(source).toContain('handlePilotNotFound();');
    expect(source).toContain('discardMaterialKitHandoff(current.applicationId);');
    expect(source).toContain('setPilotApplicationContext(null);');
  });

  it('clears the Pilot context when a historical detail is no longer visible', () => {
    expect(source).toContain('if (isOpportunityFitNotFoundError(error)) handlePilotNotFound();');
    expect(source).toContain('exitPilotContext({ preserveUnknownAttempt: false });');
  });

  it('deletes ordinary mock attempts but retains unknown results across drawer unmounts', () => {
    expect(source).toContain('discardMockInterviewAttempt');
    expect(source).toContain('if (!draft.attemptId && draft.attemptKey)');
    expect(source).toContain('if (!draft.attemptId || draft.resultUnknown)');
    expect(source).toContain('setMockInterviewContext(null);');
    expect(source).toContain('操作结果待确认，请稍后使用原尝试重试。');
  });

  it('merges mock interview patches from the keyed current draft', () => {
    expect(source).toContain('mockInterviewDraftsRef.current.get(draftKey)');
    expect(source).toContain('const next = { ...currentDraft, ...patch };');
  });

  it('routes a missing Triage application through the shared Pilot cleanup', () => {
    const triageStart = source.indexOf('const startPilotV2Triage =');
    const triageEnd = source.indexOf('const confirmPilotV2Triage =', triageStart);
    expect(source.slice(triageStart, triageEnd)).toContain('isOpportunityFitNotFoundError(error)');
  });

  it('recovers Pilot Triage confirmation uncertainty from the server stage', () => {
    const confirmStart = source.indexOf('const recoverPilotV2TriageConfirmation =');
    const confirmEnd = source.indexOf('const startPilotV2DeepReview =', confirmStart);
    const confirmSource = source.slice(confirmStart, confirmEnd);
    expect(confirmSource).toContain('getOpportunityFitV2Review');
    expect(confirmSource).toContain("current?.stage_status === 'confirmed'");
    expect(confirmSource).toContain('opportunity_fit_triage_confirmation_consumed');
    expect(confirmSource).toContain('resultUnknown: true');
  });

  it('routes a missing Deep Review application through the shared Pilot cleanup', () => {
    const deepReviewStart = source.indexOf('const startPilotV2DeepReview =');
    const deepReviewEnd = source.indexOf('const viewPilotV2History =', deepReviewStart);
    expect(source.slice(deepReviewStart, deepReviewEnd)).toContain('isOpportunityFitNotFoundError(error)');
  });

  it('retains an unknown Pilot attempt when the flow is canceled', () => {
    expect(source).toContain('const exitPilotContext = ({ preserveUnknownAttempt = true }');
    expect(source).toContain('exitPilotContext();');
    expect(source).toContain('draft.resultUnknown || requestPending');
    expect(source).toContain("error: '结果待确认，请使用原尝试重试。'");
    expect(source).toContain('pilotV2DraftsRef.current.delete(current.applicationId);');
  });

  it('keeps the old Pilot view as the expanded assistant workspace', () => {
    expect(source).toContain("view === 'pilot'");
    expect(source).toContain('<PilotWorkspace');
    expect(source).toContain('<AssistantSurfaceProvider>');
    expect(source).not.toContain('PilotHomeView');
  });

  it('keeps the contextual Pilot rail out of the Pilot tab itself', () => {
    expect(source).toContain("view !== 'pilot'");
    expect(source).toContain('shouldShowContextualPilot');
  });

  it('routes the docked Pilot expand action into the normal Pilot tab', () => {
    expect(source).toContain('handoffPilotAttachmentDraft();');
    expect(source).toContain("navigateToView('pilot');");
    expect(source).not.toContain('onExpand={() => setPilotDrawerOpen(true)}');
  });

  it('starts a fresh application-scoped Pilot draft from shared entry surfaces', () => {
    expect(source).toContain('startApplicationChat');
    expect(source).toContain("context_type: 'application'");
    expect(source).toContain('requestKey:');
    expect(source).toContain('onAskPilot={startApplicationChat}');
  });

  it('keeps UI and Pilot negotiation drafts separate for the same Offer', () => {
    expect(source).toContain('offerNegotiationPilotDraftsRef');
    expect(source).toContain("offerNegotiationEntryPoint === 'pilot'");
    expect(source).toContain('key={`${offerNegotiationEntryPoint}-${offerNegotiationOffer.id}`}');
  });

  it('derives page context from the active view, application, and coached offer', () => {
    expect(source).toContain('const coachedOffer = ofrs.find((offer) => offer.id === coachOfferId);');
    expect(source).toContain('const pageContext = useMemo(');
    expect(source).toContain('buildPilotPageContext({');
    expect(source).toContain('selectedApplication: selectedApp');
    expect(source).toContain('coachedOffer');
    expect(source).toContain('[view, selectedApp, coachedOffer]');
  });

  it('routes page context through one stable Pilot owner', () => {
    expect(source.match(/<PilotWorkspace/g)).toHaveLength(1);
    expect(source).toContain("pageActive={view === 'pilot'}");
    expect(source).toContain(
      "pageContext={view === 'pilot' ? pilotController.followingContext : pageContext}",
    );
  });

  it('shares one attachment provider across business surfaces and every Pilot panel', () => {
    expect(source).toContain("import { PilotAttachmentProvider } from '@/features/pilot/PilotAttachmentContext'");
    expect(source).toContain('<PilotAttachmentProvider>');
    expect(source).toContain('</PilotAttachmentProvider>');
    expect(source.indexOf('<PilotAttachmentProvider>')).toBeLessThan(source.indexOf('<Layout'));
  });

  it('keeps card attachments on the selected keyed draft across Pilot presentations', () => {
    expect(source).toContain('onAttachmentKeyChange={syncPilotAttachmentKey}');
    expect(source.match(/onAttachmentKeyChange=\{syncPilotAttachmentKey\}/g)).toHaveLength(1);
    expect(source).toContain('pendingAttachmentDraftKeyRef.current ?? pendingAttachmentDraftKey');
  });

  it('retains the active attachment draft while the stable Pilot owner changes presentation', () => {
    expect(source).toContain(
      'const pilotAttachmentDraftKey = pendingAttachmentDraftKey;',
    );
    expect(source).toContain(
      "import { retainPilotAttachmentKey } from '@/features/pilot/attachmentHandoff'",
    );
    expect(source).toContain('setActivePilotAttachmentKey((currentKey) => retainPilotAttachmentKey(currentKey, key));');
    expect(source).toContain('const handoffPilotAttachmentDraft = () => {');
    expect(source).toContain('handoffPilotAttachmentDraft();');
    expect(source).toContain('attachmentDraftKey={pilotAttachmentDraftKey}');
    expect(source.match(/attachmentDraftKey=\{pilotAttachmentDraftKey\}/g)).toHaveLength(1);
    expect(source).toContain("variant={view === 'pilot' ? 'page' : contextualPilotRailMode ? 'rail' : 'drawer'}");
  });

  it('registers one persistent dnd-kit target for the contextual Pilot owner', () => {
    expect(source).toContain("const contextualPilotOpen = assistantSurface.surface === 'pilot_workspace' || contextualPilotRailMode;");
    expect(source).toContain('controllerActive');
    expect(source.match(/pilotDropTarget/g)).toHaveLength(1);
  });

  it('keeps the stable Pilot owner scope and focus target while presentations change', () => {
    expect(source).toContain('data-pilot-surface-host');
    expect(source).toContain(
      "'[data-pilot-surface-host] [role=\"group\"][aria-label=\"AI 修改提议\"]'",
    );
    expect(source).toContain('offerId={coachOfferId}');
    expect(source).not.toContain("offerId={view === 'pilot' ? undefined : coachOfferId}");
    expect(source).toContain('nextPilotOnboardingFocusToken.current += 1;');
  });

  it('only clears an idle Offer scope and preserves it around active work', () => {
    const workspaceStart = source.indexOf('<PilotWorkspace');
    const closeStart = source.indexOf('onClose={() => {', workspaceStart);
    const closeEnd = source.indexOf('offerId={coachOfferId}', closeStart);
    const closeSource = source.slice(closeStart, closeEnd);
    expect(closeSource).toContain("if (view !== 'pilot') assistantSurface.closeSurface();");
    expect(closeSource).toContain('!pilotController.activeRequestRef.current');
    expect(closeSource).toContain('!pilotController.pending');
    expect(closeSource).toContain('!pilotController.activePendingRef.current');
    expect(closeSource).toContain('setCoachOfferId(undefined);');
  });

  it('keeps a visible Pilot open state unchanged when a card is attached', () => {
    const attachStart = source.indexOf('const attachToPilot =');
    const attachEnd = source.indexOf('const syncPilotAttachmentKey =', attachStart);
    const attachSource = source.slice(attachStart, attachEnd);

    expect(attachSource).not.toContain('openChat();');
    expect(attachSource).toContain('addAttachmentToKey(attachmentKey, attachment);');
  });

  it('keeps one application filter state while switching board and list views', () => {
    expect(source).toContain('const [applicationViewState, setApplicationViewState] = useState');
    expect(source.match(/viewState=\{applicationViewState\}/g)).toHaveLength(2);
    expect(source).toContain('onViewStateChange={setApplicationViewState}');
  });

  it('routes evidence through one-shot destination focus without changing Pilot visibility', () => {
    expect(source).toContain("import type { EvidenceTarget } from '@/components/ChatPanel/model'");
    expect(source).toContain(
      "const [evidenceFocus, setEvidenceFocus] = useState<Exclude<EvidenceTarget, { kind: 'application' }> | null>(null);",
    );
    expect(source).toContain('const clearEvidenceFocus = (target: EvidenceTarget) => {');
    expect(source).toContain('setEvidenceFocus((current) => (current === target ? null : current));');
    expect(source).toContain('const openEvidence = (target: EvidenceTarget) => {');
    expect(source).toContain("if (view === 'pilot' && !pilotRailAvailable) {");
    expect(source).toContain('assistantSurface.closeSurface();');
    expect(source).toContain('onOpenEvidence={openEvidence}');
    expect(source.match(/onOpenEvidence=\{openEvidence\}/g)).toHaveLength(1);
  });

  it('passes exact evidence focus targets to their destination views', () => {
    expect(source).toContain("const calendarEvidenceFocus = evidenceFocus?.kind === 'event' ? evidenceFocus : undefined;");
    expect(source).toContain("const offerEvidenceFocus = evidenceFocus?.kind === 'offer' ? evidenceFocus : undefined;");
    expect(source).toContain("const resumeEvidenceFocus = evidenceFocus?.kind === 'resume' ? evidenceFocus : undefined;");
    expect(source).toContain('focusOfferId={offerEvidenceFocus?.id}');
    expect(source).toContain('focusResumeId={resumeEvidenceFocus?.id}');
    expect(source).toContain('focusEvent={calendarEvidenceFocus}');
    expect(source).toContain('onEvidenceFocusConsumed={offerEvidenceFocus ? () => clearEvidenceFocus(offerEvidenceFocus) : undefined}');
    expect(source).toContain('onEvidenceFocusConsumed={resumeEvidenceFocus ? () => clearEvidenceFocus(resumeEvidenceFocus) : undefined}');
    expect(source).toContain('onEvidenceFocusConsumed={calendarEvidenceFocus ? () => clearEvidenceFocus(calendarEvidenceFocus) : undefined}');
  });

  it('closes offer comparison before opening focused evidence in the editor', () => {
    const focusStart = offerCenterView.indexOf('const offer = findEvidenceFocusRecord(offers, focusOfferId);');
    const focusEnd = offerCenterView.indexOf('onEvidenceFocusConsumed?.();', focusStart);
    const focusEffect = offerCenterView.slice(focusStart, focusEnd);

    expect(focusEffect).toContain('setCompareOpen(false);');
    expect(focusEffect.indexOf('setCompareOpen(false);')).toBeLessThan(focusEffect.indexOf('setEditing(offer);'));
    expect(focusEffect.indexOf('setEditing(offer);')).toBeLessThan(focusEffect.indexOf('setAddOpen(true);'));
  });

  it('retains destination focus while its queried data is in an error state', () => {
    expect(offerCenterView).toContain('const { data: offers = [], isLoading, isError, isFetching, refetch } = useQuery({');
    expect(offerCenterView).toContain('if (focusOfferId === undefined || isLoading || isError || isFetching) return;');
    expect(resumeLibraryView).toContain('resumesQuery.isFetching');
    expect(calendarView).toContain('const { data: rawEntries, isLoading, isError, isFetching, refetch } = useQuery({');
    expect(calendarView).toContain('if (focusedEventId === null || !selectedDate || isLoading || isError || isFetching) return;');
  });

  it('keeps missing calendar-event cleanup local after handing off valid focus', () => {
    const initialFocusStart = calendarView.indexOf('const date = eventFocusDate(focusEvent.scheduledAt);');
    const verificationStart = calendarView.indexOf('if (focusedEventId === null', initialFocusStart);
    const verificationEnd = calendarView.indexOf('const deleteMutation', verificationStart);
    const initialFocus = calendarView.slice(initialFocusStart, verificationStart);
    const verification = calendarView.slice(verificationStart, verificationEnd);

    expect(initialFocus).toContain('setFocusedEventId(focusEvent.id);');
    expect(initialFocus).toContain('if (isError || isFetching || consumedEvidenceTarget.current === focusEvent) return;');
    expect(verification).toContain('setFocusedEventId(null);');
    expect(verification).not.toContain('onEvidenceFocusConsumed?.();');
  });

  it('routes onboarding setup actions through their declared intents', () => {
    expect(source).toContain('const handleOnboardingAction = (action: OnboardingAction) => {');
    expect(source).toContain('const intent = onboardingActionIntent(action, pilotRailAvailable);');
    expect(source).toContain('navigateToView(intent.view);');
    expect(source).not.toContain('setAISettingsOpen');
    expect(source).toContain('setAddOpen(true);');
    expect(source).toContain('setResumeOnboardingFocusToken((token) => token + 1);');
    expect(source).toContain('const nextPilotOnboardingFocusToken = useRef(0);');
    expect(source).toContain('nextPilotOnboardingFocusToken.current += 1;');
    expect(source).toContain('const consumePilotOnboardingFocus = (token: number) => {');
    expect(source).toContain('setPilotOnboardingFocusToken((current) => (current === token ? 0 : current));');
    expect(source).toContain('onOnboardingFocusConsumed={consumePilotOnboardingFocus}');
    expect(source).toContain('onOnboardingAction={handleOnboardingAction}');
  });

  it('keeps five page-aware TopBar action classes and hides the global primary in detail', () => {
    const topBarSource = source.slice(
      source.indexOf('let topBarPrimaryAction'),
      source.indexOf('return (', source.indexOf('let topBarPrimaryAction')),
    );
    for (const label of ['添加投递', '开始练习', '上传简历', '添加经历']) {
      expect(topBarSource).toContain(`label: '${label}'`);
    }
    expect(topBarSource).toContain("? '添加投递' : '录入 Offer'");
    expect(topBarSource).toContain('setResumeUploadRequestToken((token) => token + 1)');
    expect(topBarSource).toContain("['dashboard', 'reminders', 'board', 'applications-list']");
    expect(topBarSource).not.toContain("'calendar', 'board'");
    expect(topBarSource).toContain('if (!selectedApp) {');
    expect(source).toContain('primaryAction={topBarPrimaryAction}');
    expect(source).toContain('uploadRequestToken={resumeUploadRequestToken}');
    expect(source).not.toContain("import ResumeUploadModal from '@/components/ResumeUploadModal'");
    expect(source).not.toContain('const uploadResumeMut = useMutation');
  });

  it('hydrates and synchronizes canonical and legacy view deep links through history', () => {
    expect(source).toContain("from './viewRoute'");
    expect(source).toContain('pushWorkspaceView');
    expect(source).toContain('readInitialWorkspaceView');
    expect(source).toContain('subscribeToWorkspaceView');
    expect(source).toContain('useState<ViewMode>(readInitialWorkspaceView)');
    expect(source).toContain('skipNextHistorySyncRef.current = true;');
    expect(source).toContain('pushWorkspaceView(view);');
  });

  it('restores main-content focus on view/detail changes and keeps the desktop shell at 100dvh', () => {
    expect(source).toContain('const contentRef = useRef<HTMLElement | null>(null);');
    expect(source).toContain('tabIndex={-1}');
    expect(source).toContain('contentRef.current?.focus({ preventScroll: true })');
    expect(source).toContain("minHeight: '100dvh'");
  });

  it('passes the shared Offer collection into ApplicationDetail', () => {
    const detailStart = source.indexOf('<ApplicationDetail');
    const detailEnd = source.indexOf('/>', detailStart);
    expect(detailStart).toBeGreaterThanOrEqual(0);
    expect(source.slice(detailStart, detailEnd)).toContain('offers={ofrs}');
  });

  it('keeps Haru to Pilot expansion as a surface change without a second request owner', () => {
    expect((source.match(/<AssistantSurfaceProvider>/g) ?? [])).toHaveLength(1);
    expect((source.match(/usePilotConversationController\(\)/g) ?? [])).toHaveLength(1);
    expect(source).not.toContain('new EventSource');
    expect(source).not.toContain('streamChat(');

    const applicationChatStart = source.indexOf('const startApplicationChat =');
    const applicationChatEnd = source.indexOf('const claimChatStartRequest =', applicationChatStart);
    const applicationChatSource = source.slice(applicationChatStart, applicationChatEnd);
    expect(applicationChatSource).toContain('assistantSurface.openHaru();');
    expect(applicationChatSource).not.toContain('streamChat');
    expect(applicationChatSource).not.toContain('sendMessage');
  });
});
