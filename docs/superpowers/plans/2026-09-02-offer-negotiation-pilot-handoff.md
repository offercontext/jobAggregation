# Offer Negotiation Pilot Handoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore a safe, non-sending handoff from structured Offer negotiation preparation into the existing Pilot negotiation conversation.

**Architecture:** The Drawer emits a typed handoff intent, ApplicationDetail fences it to the active Core Task generation, and AppShell creates an application-scoped `nego_coach` start request. A transient composer draft lives in the existing shared Pilot controller so Haru and the expanded Pilot surface show the same unsent text.

**Tech Stack:** React 19, TypeScript, Ant Design, Vitest, CSS Modules.

---

### Task 1: Freeze the handoff contract

**Files:**
- Create: `web/src/features/offerNegotiation/pilotHandoff.ts`
- Create: `web/src/features/offerNegotiation/pilotHandoff.test.ts`
- Modify: `web/src/types/chat.ts`

- [x] Write a failing test for deterministic, editable handoff copy that omits blank brief fields and contains no internal IDs.
- [x] Run `npm test -- --run src/features/offerNegotiation/pilotHandoff.test.ts` and confirm the missing module fails.
- [x] Implement `buildOfferNegotiationPilotDraft(offer, brief)` and extend `ChatStartRequest` with `mode: 'general' | 'nego_coach'` plus mutually non-sending `composerDraft`.
- [x] Re-run the focused test and confirm it passes.

### Task 2: Share the unsent composer draft

**Files:**
- Modify: `web/src/features/assistantSurface/usePilotConversationController.ts`
- Modify: `web/src/features/assistantSurface/HaruChatWindow.tsx`
- Modify: `web/src/components/ChatPanel/Composer.tsx`
- Modify: `web/src/components/ChatPanel/index.tsx`
- Test: `web/src/components/ChatPanel/Composer.test.tsx`
- Test: `web/src/components/ChatPanel/deterministicPilotConfirmation.test.tsx`
- Test: `web/src/features/assistantSurface/HaruChatWindow.test.tsx`

- [x] Add failing tests proving a `composerDraft` start request does not call `streamChat`, and the same draft is visible in Haru and Pilot.
- [x] Run the three focused test files and confirm the new assertions fail for the missing shared draft.
- [x] Add transient, Offer-scoped `composerDraft` state to the existing controller, make both composers use it, and consume start requests exactly once without reusing `initialMessage`.
- [x] Re-run the focused tests and confirm success, failure retention, new-chat clearing, and surface-switch preservation.

### Task 3: Wire the structured task handoff

**Files:**
- Modify: `web/src/components/OfferNegotiationDrawer.tsx`
- Modify: `web/src/components/OfferNegotiationDrawer.module.css`
- Modify: `web/src/components/OfferNegotiationDrawer.test.tsx`
- Modify: `web/src/components/ApplicationDetail.tsx`
- Modify: `web/src/layout/AppShell.tsx`
- Modify: `web/src/layout/AppShell.test.ts`

- [x] Add failing tests for one secondary CTA, exact brief payload, hidden unsafe states, and zero service calls on click.
- [x] Run the Drawer/AppShell tests and confirm the missing callback and handler fail.
- [x] Add the typed callback, current-generation fence, AppShell binding checks, exact Offer attachment, non-sending start request, and existing Core Task recovery close.
- [x] Re-run the focused tests and confirm the Offer card still has one primary action.

### Task 4: Verify and review

**Files:**
- Modify only files required by review findings.

- [x] Run focused Vitest files for Offer negotiation, ChatPanel, Assistant Surface, ApplicationDetail, and AppShell.
- [x] Run `npm run build`, `uv run ruff check .`, and `uv run mypy src`.
- [x] Walk the real browser path: Offer → 准备谈薪 → 和 Pilot 深聊 → prefilled composer → Haru/Pilot expand; keep the final send under automated test control.
- [x] Confirm the click alone creates no Chat/SSE request and the shared draft survives Haru/Pilot expansion.
- [x] Confirm a second Offer in the same Application cannot replace the clicked Offer attachment, and cross-scope reopen clears the old draft.
- [x] Request an independent code review and close every P0/P1/P2 finding.
- [x] Run `git diff --check`, stage files, and commit once using the repository Conventional Commit format.
