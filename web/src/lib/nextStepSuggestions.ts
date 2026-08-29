import type { Application } from '@/types/application';
import type { ScheduleEvent } from '@/types/event';
import type { Offer } from '@/types/offer';
import type { PracticeStats } from '@/types/question';
import type { Resume } from '@/types/resume';
import { classifyEventLifecycleV1 } from '@/features/interviewEvents/eventLifecycle';

export type FactState<T> =
  | { status: 'known'; value: T; version?: string }
  | { status: 'unknown'; reason: 'not_loaded' | 'not_supported' | 'not_visible' };

export type ResumeFact = Resume & {
  version?: string | null;
  sha256?: string | null;
};

export interface NextStepFacts {
  application: FactState<Application>;
  availableResumes: FactState<ResumeFact[]>;
  events: FactState<ScheduleEvent[]>;
  offers: FactState<Offer[]>;
  confirmedKnowledge: FactState<unknown[]>;
  practiceStats: FactState<PracticeStats>;
  jd: FactState<string>;
  fitReview: FactState<unknown>;
  materialKit: FactState<unknown>;
  interviewPreparationHistory: FactState<unknown>;
  mockInterviewHistory: FactState<unknown>;
  sourceRisks?: SourceRiskNotice[];
}

export type ApplicationDestination = {
  kind: 'application_detail';
  applicationId: number;
};

export type OpportunityFitDestination = {
  kind: 'pilot_opportunity_fit';
  applicationId: number;
};

export type MaterialKitDestination = {
  kind: 'material_kit_entry';
  applicationId: number;
};

export type InterviewEventDestination = {
  kind: 'interview_event';
  applicationId: number;
  eventId: number;
};

export type InterviewEventSelectionDestination = {
  kind: 'interview_event_selection';
  applicationId: number;
};

export type InterviewReviewDestination = {
  kind: 'interview_review';
  applicationId: number;
  eventId: number;
};

export type InterviewReviewHistoryDestination = {
  kind: 'interview_review_history';
  applicationId: number;
  eventId: number;
  reviewId: number;
};

export type InterviewReviewSelectionDestination = {
  kind: 'interview_review_selection';
  applicationId: number;
};

export type OpportunityFitHistoryDestination = {
  kind: 'opportunity_fit_history';
  applicationId: number;
  reviewId: number;
};

export type NextStepDestination =
  | ApplicationDestination
  | OpportunityFitDestination
  | MaterialKitDestination
  | InterviewEventDestination
  | InterviewEventSelectionDestination
  | InterviewReviewDestination
  | InterviewReviewHistoryDestination
  | InterviewReviewSelectionDestination
  | OpportunityFitHistoryDestination;

export type ReadonlyDestination =
  | ApplicationDestination
  | InterviewEventDestination
  | InterviewEventSelectionDestination
  | InterviewReviewDestination
  | InterviewReviewHistoryDestination
  | InterviewReviewSelectionDestination
  | OpportunityFitHistoryDestination;

export type NextStepSource = {
  label: string;
  status: 'current' | 'frozen' | 'changed' | 'unknown';
  readonlyDestination?: ReadonlyDestination;
};

export type NextStepCandidate = {
  id: string;
  stateKey: string;
  title: string;
  reason: string;
  destination: NextStepDestination;
  sources: NextStepSource[];
};

export type SourceRiskNotice = {
  id: string;
  stateKey: string;
  title: string;
  reason: string;
  sources: NextStepSource[];
  readonlyDestination?: ReadonlyDestination;
};

export type NextStepSuggestions = {
  candidates: NextStepCandidate[];
  sourceRisks: SourceRiskNotice[];
};

export type SuggestionSessionState = {
  stateKey: string;
  disposition: 'snoozed' | 'ignored';
};

export type SuggestionContext = 'workbench' | 'detail';

type InterviewEventWithDeletedAt = ScheduleEvent & { deleted_at?: string | null };

function lifecycleForEvent(event: ScheduleEvent): ReturnType<typeof classifyEventLifecycleV1> {
  const projected = (event as ScheduleEvent & { lifecycle?: unknown }).lifecycle;
  if (projected === 'scheduled' || projected === 'in_progress' || projected === 'completed' || projected === 'cancelled' || projected === 'unknown') {
    return projected;
  }
  return classifyEventLifecycleV1(event.status);
}

function stableStringify(value: unknown): string {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(stableStringify).join(',') + ']';
  return '{' + Object.keys(value as Record<string, unknown>).sort().map((key) => (
    JSON.stringify(key) + ':' + stableStringify((value as Record<string, unknown>)[key])
  )).join(',') + '}';
}

function opaqueDigest(value: string): string {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(16).padStart(8, '0');
}

function resumeVersion(resume: ResumeFact): string {
  if (typeof resume.version === 'string' && resume.version.length > 0) return resume.version;
  if (typeof resume.sha256 === 'string' && resume.sha256.length > 0) return resume.sha256;
  return opaqueDigest(stableStringify({
    content_json: resume.content_json,
    name: resume.name,
    source: resume.source,
    title: resume.title,
  }));
}

function resumeSetKey(facts: NextStepFacts): string {
  if (facts.availableResumes.status === 'unknown') return 'unknown';
  return facts.availableResumes.value
    .map((resume) => String(resume.id) + ':' + resumeVersion(resume))
    .sort()
    .join(',');
}

function makeStateKey(
  facts: NextStepFacts,
  candidateId: string,
  context: SuggestionContext,
  destination: NextStepDestination,
  eventIdentity = 'none',
): string {
  const applicationId = facts.application.status === 'known' ? facts.application.value.id : 'unknown';
  return [
    applicationId,
    candidateId,
    context,
    resumeSetKey(facts),
    facts.jd.status,
    (facts.sourceRisks ?? []).map((risk) => risk.stateKey).sort().join(','),
    eventIdentity,
    stableStringify(destination),
  ].join('|');
}

export type MaterialKitQueryFact = {
  applicationId: number;
  status: 'loading' | 'error' | 'success';
  complete: boolean;
  kits?: Array<{ application_id?: number }>;
};

export function deriveMaterialKitFact(input: MaterialKitQueryFact): FactState<unknown> {
  if (input.status !== 'success' || !input.complete) {
    return { status: 'unknown', reason: 'not_loaded' };
  }
  return {
    status: 'known',
    value: input.kits?.find((kit) => kit.application_id === input.applicationId) ?? null,
  };
}

function isValidDuration(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && Number.isInteger(value) && value > 0;
}

function parseScheduledAt(value: string): number | null {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return null;
  const datePart = value.slice(0, 10);
  if (/^\d{4}-\d{2}-\d{2}$/.test(datePart)) {
    const [year, month, day] = datePart.split('-').map(Number);
    const utc = new Date(Date.UTC(year, month - 1, day));
    if (utc.getUTCFullYear() !== year || utc.getUTCMonth() !== month - 1 || utc.getUTCDate() !== day) {
      return null;
    }
  }
  return timestamp;
}

function classifyInterviewEvents(facts: NextStepFacts, now: Date) {
  if (facts.events.status === 'unknown' || facts.application.status === 'unknown') {
    return { currentOrFuture: [], ended: [] as InterviewEventWithDeletedAt[] };
  }
  const applicationId = facts.application.value.id;

  const valid = facts.events.value.filter((event): event is InterviewEventWithDeletedAt => {
    if (event.application_id !== applicationId || event.event_type !== 'interview') return false;
    if ('deleted_at' in event && event.deleted_at) return false;
    const lifecycle = lifecycleForEvent(event);
    if (lifecycle === 'cancelled' || lifecycle === 'unknown') return false;
    if (lifecycle === 'completed') return true;
    const scheduleState = (event as ScheduleEvent & { scheduled_at_state?: unknown }).scheduled_at_state;
    if (scheduleState !== undefined && scheduleState !== 'present') return false;
    if (!isValidDuration(event.duration_minutes)) return false;
    return parseScheduledAt(event.scheduled_at) !== null;
  }).map((event) => {
    const parsedStart = parseScheduledAt(event.scheduled_at);
    const start = parsedStart ?? Number.POSITIVE_INFINITY;
    const end = parsedStart === null ? Number.POSITIVE_INFINITY : start + event.duration_minutes * 60_000;
    return { event, start, end, lifecycle: lifecycleForEvent(event) };
  });

  const timestamp = now.getTime();
  const sortAscending = (left: typeof valid[number], right: typeof valid[number]) => (
    left.start - right.start
    || right.event.created_at.localeCompare(left.event.created_at)
    || right.event.id - left.event.id
  );
  const sortDescending = (left: typeof valid[number], right: typeof valid[number]) => (
    right.start - left.start
    || right.event.created_at.localeCompare(left.event.created_at)
    || right.event.id - left.event.id
  );

  return {
    currentOrFuture: valid
      .filter(({ lifecycle, start, end }) => (lifecycle === 'scheduled' && start > timestamp) || (lifecycle === 'in_progress' && timestamp <= end))
      .sort(sortAscending)
      .map(({ event }) => event),
    ended: valid.filter(({ lifecycle }) => lifecycle === 'completed').sort(sortDescending).map(({ event }) => event),
  };
}

function reviewIdsForEvent(facts: NextStepFacts, eventId: number): number[] {
  if (facts.fitReview.status === 'unknown' || !facts.fitReview.value || typeof facts.fitReview.value !== 'object') return [];
  const value = facts.fitReview.value as Record<string, unknown>;
  const idsByEvent = value.reviewIdsByEvent;
  if (idsByEvent && typeof idsByEvent === 'object') {
    const ids = (idsByEvent as Record<string, unknown>)[String(eventId)];
    if (Array.isArray(ids)) return ids.filter((id): id is number => typeof id === 'number');
  }
  const singleIds = value.reviewIdByEvent;
  const single = singleIds && typeof singleIds === 'object'
    ? (singleIds as Record<string, unknown>)[String(eventId)]
    : undefined;
  return typeof single === 'number' ? [single] : [];
}

function candidate(
  facts: NextStepFacts,
  id: string,
  context: SuggestionContext,
  title: string,
  reason: string,
  destination: NextStepDestination,
  sources: NextStepSource[],
  eventIdentity = 'none',
): NextStepCandidate {
  return {
    id,
    stateKey: makeStateKey(facts, id, context, destination, eventIdentity),
    title,
    reason,
    destination,
    sources,
  };
}

export function deriveNextStepSuggestions(
  facts: NextStepFacts,
  context: SuggestionContext,
  now: Date,
): NextStepSuggestions {
  if (facts.application.status === 'unknown') return { candidates: [], sourceRisks: facts.sourceRisks ?? [] };
  const applicationId = facts.application.value.id;

  if (
    context === 'workbench'
    && [facts.jd, facts.fitReview, facts.materialKit, facts.interviewPreparationHistory, facts.mockInterviewHistory]
      .some((fact) => fact.status === 'unknown')
  ) {
    return {
      candidates: [candidate(
        facts,
        'application_detail',
        context,
        '查看投递详情以确认下一步',
        '查看投递详情以确认下一步',
        { kind: 'application_detail', applicationId },
        [],
      )],
      sourceRisks: facts.sourceRisks ?? [],
    };
  }

  const candidates: NextStepCandidate[] = [];

  if (facts.availableResumes.status === 'known' && facts.availableResumes.value.length === 0) {
    candidates.push(candidate(
      facts,
      'choose_resume',
      context,
      '选择简历',
      '先确认可用于本次投递的简历',
      { kind: 'application_detail', applicationId },
      [{ label: '已检查简历库', status: 'unknown' }],
    ));
  }

  if (facts.fitReview.status === 'known' && !facts.fitReview.value) {
    candidates.push(candidate(
      facts,
      'start_fit_review',
      context,
      '开始岗位评估',
      '已有投递事实可供确认',
      { kind: 'pilot_opportunity_fit', applicationId },
      [],
    ));
  }

  if (facts.materialKit.status === 'known' && !facts.materialKit.value) {
    candidates.push(candidate(
      facts,
      'prepare_materials',
      context,
      '准备投递材料',
      '当前投递还没有已知材料包',
      { kind: 'material_kit_entry', applicationId },
      [],
    ));
  }

  const { currentOrFuture, ended } = classifyInterviewEvents(facts, now);
  if (currentOrFuture.length === 1) {
    const event = currentOrFuture[0];
    candidates.push(candidate(
      facts,
      'prepare_interview',
      context,
      '为这场面试做准备',
      '已有一场当前或未来的面试事件',
      { kind: 'interview_event', applicationId, eventId: event.id },
      [{ label: '当前使用事件', status: 'current', readonlyDestination: { kind: 'interview_event', applicationId, eventId: event.id } }],
      'event:' + event.id,
    ));
  } else if (currentOrFuture.length > 1) {
    candidates.push(candidate(
      facts,
      'prepare_interview',
      context,
      '为这场面试做准备',
      '有多场当前或未来的面试，请先选择事件',
      { kind: 'interview_event_selection', applicationId },
      [],
      'event:selection',
    ));
  }

  if (ended.length === 1) {
    const event = ended[0];
    const reviewIds = reviewIdsForEvent(facts, event.id);
    const destination: NextStepDestination = reviewIds.length === 1
      ? { kind: 'interview_review_history', applicationId, eventId: event.id, reviewId: reviewIds[0] }
      : { kind: 'interview_review', applicationId, eventId: event.id };
    candidates.push(candidate(
      facts,
      'review_interview',
      context,
      '查看面试复盘',
      reviewIds.length === 1 ? '已有一份历史复盘可查看' : '这场面试已结束，可以开始复盘',
      destination,
      [],
      'event:' + event.id,
    ));
  } else if (ended.length > 1) {
    candidates.push(candidate(
      facts,
      'review_interview',
      context,
      '查看面试复盘',
      '有多场已结束的面试，请先选择事件',
      { kind: 'interview_review_selection', applicationId },
      [],
      'event:selection',
    ));
  }

  return { candidates, sourceRisks: facts.sourceRisks ?? [] };
}
