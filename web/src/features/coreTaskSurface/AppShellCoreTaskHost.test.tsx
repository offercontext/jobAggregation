import { describe, expect, it, vi } from 'vitest';
import appShellSource from '@/layout/AppShell.tsx?raw';
import applicationDetailSource from '@/components/ApplicationDetail.tsx?raw';
import type { TaskLaunchRequest } from './contracts';
import { createCoreTaskSurfaceController } from './controller';

const materialRequest = (
  applicationId: number,
  source: TaskLaunchRequest['source'],
  suggestedResumeId?: number,
): TaskLaunchRequest => ({
  ref: { taskId: 'application.material_kit', applicationId },
  source,
  hints: suggestedResumeId === undefined ? undefined : { suggestedResumeId },
});

const offerRequest = (
  applicationId: number,
  source: TaskLaunchRequest['source'],
  suggestedOfferId?: number,
): TaskLaunchRequest => ({
  ref: { taskId: 'application.offer_review', applicationId },
  source,
  hints: suggestedOfferId === undefined ? undefined : { suggestedOfferId },
});

describe('AppShell application task composition', () => {
  it('creates one stable controller and injects one host into ApplicationDetail', () => {
    expect(appShellSource).toContain('const coreTaskControllerRef = useRef<CoreTaskSurfaceController | null>(null);');
    expect(appShellSource).toContain('if (!coreTaskControllerRef.current) {');
    expect((appShellSource.match(/createCoreTaskSurfaceController\(/g) ?? []).length).toBe(1);
    expect(appShellSource).toContain('launchCoreTask');
    expect(appShellSource).toContain('taskController={coreTaskController}');
    expect(appShellSource).toContain('onLaunchTask={launchTaskFromApplicationDetail}');
    expect(appShellSource).toContain("request.ref.taskId === 'application.interview_prepare'");
    expect(appShellSource).toContain('? openExactInterviewTask(request)');
    expect(appShellSource).toContain(': launchCoreTask(request)');
    expect(applicationDetailSource).toContain('CoreTaskSurfaceHost');
    expect((applicationDetailSource.match(/<CoreTaskSurfaceHost/g) ?? []).length).toBe(1);
    expect(applicationDetailSource).toContain('revealOnOpen');
    expect(applicationDetailSource).toContain('resolveApplicationTasks');
    expect(applicationDetailSource).not.toContain('const [opportunityFitOpen');
    expect(applicationDetailSource).not.toContain('const [materialKitOpen');
    expect(applicationDetailSource).not.toContain('const [preparationOpen');
  });

  it('uses one canonical key for header, task card, Pilot and deep-link requests', () => {
    const controller = createCoreTaskSurfaceController();
    const header = controller.launch(materialRequest(7, 'application_header'));
    expect(header.kind).toBe('launched');
    const generation = header.kind === 'launched' ? header.generation : -1;

    for (const source of ['application_task_card', 'haru', 'pilot', 'deep_link'] as const) {
      expect(controller.launch(materialRequest(7, source, 99))).toMatchObject({
        kind: 'focused_existing',
        generation,
        key: 'application.material_kit:applicationId=7',
      });
    }
    expect(controller.getState().generation).toBe(generation);
  });

  it('keeps one application-level Offer key for zero, one, or many Offers', () => {
    const controller = createCoreTaskSurfaceController();
    const zero = controller.launch(offerRequest(7, 'application_header'));
    expect(zero).toMatchObject({
      kind: 'launched',
      key: 'application.offer_review:applicationId=7',
    });
    expect(controller.getState().active?.ref).not.toHaveProperty('offerId');
    expect(controller.launch(offerRequest(7, 'pilot', 91))).toMatchObject({
      kind: 'focused_existing',
      generation: zero.kind === 'launched' ? zero.generation : -1,
    });
    expect(controller.launch(offerRequest(7, 'deep_link', 92)).kind).toBe('focused_existing');
    expect(appShellSource).toContain('listOfferBindingState(offer)');
    expect(appShellSource).toContain('hints: { suggestedOfferId: offer.id }');
    expect(appShellSource).not.toContain("taskId: 'application.offer_review', applicationId: application.id, offerId");
  });

  it('blocks replacement while a current owner has pending or unsaved work', () => {
    let pending = false;
    let unsaved = false;
    const controller = createCoreTaskSurfaceController({
      hasPending: () => pending,
      hasUnsavedChanges: () => unsaved,
    });
    const first = controller.launch(materialRequest(7, 'application_header'));
    expect(first.kind).toBe('launched');
    pending = true;
    expect(controller.launch({
      ref: { taskId: 'application.interview_review', applicationId: 7, eventId: 31 },
      source: 'application_task_card',
    })).toMatchObject({ kind: 'replacement_denied' });
    pending = false;
    unsaved = true;
    expect(controller.launch({
      ref: { taskId: 'application.interview_review', applicationId: 7, eventId: 31 },
      source: 'application_task_card',
    })).toMatchObject({ kind: 'replacement_denied' });
    expect(controller.getState().active?.key).toBe('application.material_kit:applicationId=7');
  });

  it('allows only the narrow confirmed Fit to Material handoff through the unsaved guard', () => {
    let transition: { applicationId: number; generation: number } | null = null;
    const controller = createCoreTaskSurfaceController({
      hasUnsavedChanges: (active) => !(
        transition
        && transition.applicationId === active.ref.applicationId
        && transition.generation === active.generation
        && active.ref.taskId === 'application.opportunity_fit'
      ),
    });
    const fit = controller.launch({
      ref: { taskId: 'application.opportunity_fit', applicationId: 7 },
      source: 'pilot',
    });
    expect(fit.kind).toBe('launched');
    if (fit.kind !== 'launched') return;
    transition = { applicationId: 7, generation: fit.generation };
    expect(controller.launch(materialRequest(7, 'pilot'))).toMatchObject({
      kind: 'launched',
      generation: fit.generation + 1,
    });
    transition = null;
    expect(controller.launch({
      ref: { taskId: 'application.opportunity_fit', applicationId: 7 },
      source: 'application_task_card',
    })).toMatchObject({ kind: 'replacement_denied' });
  });

  it('does not duplicate focus side effects or let stale generation callbacks replace the owner', () => {
    const onFocus = vi.fn();
    const controller = createCoreTaskSurfaceController({ onFocus, canReplace: () => true });
    const first = controller.launch(materialRequest(7, 'application_header'));
    expect(first.kind).toBe('launched');
    const duplicate = controller.launch(materialRequest(7, 'pilot'));
    expect(duplicate.kind).toBe('focused_existing');
    expect(onFocus).toHaveBeenCalledTimes(1);
    const replacement = controller.launch({
      ref: { taskId: 'application.general_review', applicationId: 7 },
      source: 'deep_link',
    });
    expect(replacement.kind).toBe('launched');
    if (first.kind === 'launched') {
      controller.close(first.generation);
      controller.markClosed(first.generation);
    }
    expect(controller.getState().active?.ref).toEqual({
      taskId: 'application.general_review',
      applicationId: 7,
    });
  });

  it('keeps event review and general review keys distinct and guards stale callbacks', () => {
    const controller = createCoreTaskSurfaceController({ canReplace: () => true });
    const event = controller.launch({
      ref: { taskId: 'application.interview_review', applicationId: 7, eventId: 31 },
      source: 'interview_event_card',
    });
    expect(event.kind).toBe('launched');
    const eventGeneration = event.kind === 'launched' ? event.generation : -1;
    expect(controller.launch({
      ref: { taskId: 'application.general_review', applicationId: 7 },
      source: 'pilot',
    })).toMatchObject({ kind: 'launched', generation: eventGeneration + 1 });
    controller.close(eventGeneration);
    controller.markClosed(eventGeneration);
    expect(controller.getState().active?.ref).toEqual({
      taskId: 'application.general_review',
      applicationId: 7,
    });
  });

  it('does not guess an event for application-only interview preparation', () => {
    expect(appShellSource).toContain('pilotInterviewPreparationEventId');
    expect(applicationDetailSource).toContain('pilotInterviewPreparationEventId == null');
    expect(applicationDetailSource).toContain('选择要准备的面试');
    expect(applicationDetailSource).not.toContain('interviewEvents.length === 1');
  });

  it('keeps More actions limited to record management', () => {
    const start = applicationDetailSource.indexOf('const moreActionItems');
    const end = applicationDetailSource.indexOf('const linkedOffers', start);
    const source = applicationDetailSource.slice(start, end);
    expect(source).toContain('让 Haru 帮我');
    expect(source).not.toContain('评估岗位匹配');
    expect(source).not.toContain('投递材料');
    expect(source).not.toContain('岗位决策');
    expect(source).not.toContain('面试');
    expect(source).not.toContain('Offer');
    expect(source).not.toContain('结果');
  });
});
