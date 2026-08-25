import dayjs, { type ConfigType } from 'dayjs';
import type { Application, ApplicationStatus } from '@/types/application';
import { EVENT_TYPE_LABELS, type ScheduleEvent } from '@/types/event';
import type { Offer } from '@/types/offer';
import type { PracticeStats } from '@/types/question';

export type ActionItemPriority = 'p0' | 'p1' | 'p2';
export type ActionItemKind =
  | 'offer_deadline'
  | 'interview_soon'
  | 'stale_application'
  | 'no_next_event'
  | 'material_kit_incomplete'
  | 'question_due';
export type ActionItemTarget = 'board' | 'calendar' | 'offers' | 'questions';

export interface ActionItem {
  id: string;
  kind: ActionItemKind;
  priority: ActionItemPriority;
  title: string;
  detail: string;
  primaryActionLabel: string;
  target: ActionItemTarget;
  sortKey: number;
  appId?: number;
  offerId?: number;
  eventId?: number;
  questionCount?: number;
}

export interface ActionItemSummary {
  p0: number;
  interviewSoon: number;
  stale: number;
  dueQuestions: number;
}

interface MaterialKitActionState {
  application_id: number;
  complete: boolean;
}

interface DeriveActionItemsInput {
  apps: Application[];
  events: ScheduleEvent[];
  offers: Offer[];
  materialKits?: MaterialKitActionState[];
  practiceStats?: PracticeStats | null;
  now?: ConfigType;
}

const WAITING_STATUSES: ApplicationStatus[] = ['pending', 'applied', 'written_test'];
const PRIORITY_RANK: Record<ActionItemPriority, number> = { p0: 0, p1: 1, p2: 2 };

const DATE_ONLY_PATTERN = /^(\d{4})-(\d{2})-(\d{2})$/;
const DATE_TIME_PATTERN = /^(\d{4})-(\d{2})-(\d{2})[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:Z|[+-]\d{2}:?\d{2})?$/;

function hasExactMatch(match: RegExpExecArray | null, value: string): match is RegExpExecArray {
  return Boolean(match && match[0] === value);
}

function isRealCalendarDate(yearText: string, monthText: string, dayText: string): boolean {
  const year = Number(yearText);
  const month = Number(monthText);
  const day = Number(dayText);
  const utc = new Date(Date.UTC(year, month - 1, day));

  return utc.getUTCFullYear() === year && utc.getUTCMonth() === month - 1 && utc.getUTCDate() === day;
}

function hasRealEmbeddedDate(match: RegExpExecArray | null, value: string): boolean {
  if (!hasExactMatch(match, value)) return false;

  const [, year, month, day] = match;
  if (!year || !month || !day) return false;

  return isRealCalendarDate(year, month, day);
}

function isDateOnly(value: string): boolean {
  return hasExactMatch(DATE_ONLY_PATTERN.exec(value), value);
}

function isStrictDateOnly(value: string): boolean {
  return hasRealEmbeddedDate(DATE_ONLY_PATTERN.exec(value), value);
}

function isStrictDateTimeLike(value: string): boolean {
  return hasRealEmbeddedDate(DATE_TIME_PATTERN.exec(value), value);
}

function parseStrictDateTime(value: string): dayjs.Dayjs | null {
  if (!isStrictDateOnly(value) && !isStrictDateTimeLike(value)) return null;

  const parsed = dayjs(value);
  if (!parsed.isValid()) return null;

  return parsed;
}

function parseOfferDeadline(value: string): dayjs.Dayjs | null {
  if (isDateOnly(value)) {
    if (!isStrictDateOnly(value)) return null;
    return dayjs(value).endOf('day');
  }

  return parseStrictDateTime(value);
}

function getOfferDeadlineDays(deadlineValue: string, current: dayjs.Dayjs): number {
  const deadline = parseOfferDeadline(deadlineValue);
  if (!deadline) return Number.NaN;

  if (isDateOnly(deadlineValue)) {
    return deadline.startOf('day').diff(current.startOf('day'), 'day');
  }

  return deadline.diff(current, 'day', true);
}

function formatOfferDeadlineLabel(deadlineValue: string, current: dayjs.Dayjs, days: number): string {
  const deadline = parseOfferDeadline(deadlineValue);
  if (deadline?.isSame(current, 'day')) return '答复期今天到期';

  const rounded = Math.max(0, Math.ceil(days));
  if (rounded === 0) return '答复期今天到期';
  return `答复期还剩 ${rounded} 天`;
}

function companyPosition(company?: string, position?: string): string {
  return [company, position].filter(Boolean).join(' · ');
}

export function deriveActionItems({
  apps,
  events,
  offers,
  materialKits,
  practiceStats,
  now = dayjs(),
}: DeriveActionItemsInput): ActionItem[] {
  const current = dayjs(now);
  const items: ActionItem[] = [];

  for (const offer of offers) {
    if (!['pending', 'negotiating'].includes(offer.status)) continue;
    if (!offer.deadline) continue;

    const days = getOfferDeadlineDays(offer.deadline, current);
    if (!Number.isFinite(days) || days < 0 || days > 7) continue;

    const priority: ActionItemPriority = days <= 2 ? 'p0' : 'p1';
    const label = formatOfferDeadlineLabel(offer.deadline, current, days);
    items.push({
      id: `offer-${offer.id}`,
      kind: 'offer_deadline',
      priority,
      title: `${offer.company_name} Offer ${label}`,
      detail: '建议确认接受、谈判或拒绝策略，必要时打开谈薪教练。',
      primaryActionLabel: '处理 Offer',
      target: 'offers',
      offerId: offer.id,
      appId: offer.application_id,
      sortKey: days,
    });
  }

  for (const event of events) {
    if (!event.scheduled_at) continue;

    const when = parseStrictDateTime(event.scheduled_at);
    if (!when) continue;

    const hours = when.diff(current, 'hour', true);
    if (!Number.isFinite(hours)) continue;
    if (hours < 0 || hours > 72) continue;

    const priority: ActionItemPriority = hours <= 24 ? 'p0' : 'p1';
    const titleBase = companyPosition(event.company_name, event.position_name) || '面试安排';
    items.push({
      id: `interview-${event.id}`,
      kind: 'interview_soon',
      priority,
      title: `${titleBase} ${EVENT_TYPE_LABELS[event.event_type]}：${when.format('M月D日 HH:mm')}`,
      detail: '建议复习到期题目，查看岗位 JD 分析和历史薄弱点。',
      primaryActionLabel: '准备面试',
      target: 'calendar',
      eventId: event.id,
      appId: event.application_id,
      sortKey: 100 + hours,
    });
  }

  for (const application of apps) {
    if (!WAITING_STATUSES.includes(application.status)) continue;

    const base = application.updated_at || application.applied_at;
    if (!base) continue;

    const parsedBase = parseStrictDateTime(base);
    if (!parsedBase) continue;

    const days = current.diff(parsedBase, 'day');
    if (!Number.isFinite(days)) continue;
    if (days <= 7) continue;

    items.push({
      id: `stale-${application.id}`,
      kind: 'stale_application',
      priority: days >= 14 ? 'p1' : 'p2',
      title: `${application.company_name} · ${application.position_name} 已 ${days} 天无更新`,
      detail: '建议跟进、补充备注或更新投递状态，避免看板长期停滞。',
      primaryActionLabel: '打开投递',
      target: 'board',
      appId: application.id,
      sortKey: 1000 - days,
    });
  }

  const appsWithFutureEvents = new Set(
    events
      .filter((event) => {
        if (!event.scheduled_at) return false;
        const scheduledAt = parseStrictDateTime(event.scheduled_at);
        return Boolean(scheduledAt?.isAfter(current));
      })
      .map((event) => event.application_id),
  );

  for (const application of apps) {
    if (application.status !== 'interview') continue;
    if (appsWithFutureEvents.has(application.id)) continue;

    items.push({
      id: `no-next-${application.id}`,
      kind: 'no_next_event',
      priority: 'p2',
      title: `${application.company_name} · ${application.position_name} 未安排下一场`,
      detail: '当前处于面试阶段，建议补充下一场安排或更新投递状态。',
      primaryActionLabel: '安排下一步',
      target: 'calendar',
      appId: application.id,
      sortKey: 2500,
    });
  }

  if (materialKits) {
    const materialKitByApp = new Map(materialKits.map((kit) => [kit.application_id, kit]));
    for (const application of apps) {
      if (!WAITING_STATUSES.includes(application.status)) continue;
      const kit = materialKitByApp.get(application.id);
      if (kit?.complete) continue;

      items.push({
        id: `material-kit-${application.id}`,
        kind: 'material_kit_incomplete',
        priority: 'p2',
        title: `${application.company_name} · ${application.position_name} 投递准备待完善`,
        detail: '简历建议、沟通话术或投递清单尚未完成。',
        primaryActionLabel: '打开材料包',
        target: 'board',
        appId: application.id,
        sortKey: 2200,
      });
    }
  }

  const due = practiceStats?.due ?? 0;
  if (due > 0) {
    items.push({
      id: 'questions-due',
      kind: 'question_due',
      priority: due >= 10 ? 'p1' : 'p2',
      title: `${due} 道题目到期复习`,
      detail: '建议安排一次 20 分钟练习，保持面试手感。',
      primaryActionLabel: '开始刷题',
      target: 'questions',
      questionCount: due,
      sortKey: 3000 - Math.min(due, 50),
    });
  }

  return items.sort(
    (a, b) => PRIORITY_RANK[a.priority] - PRIORITY_RANK[b.priority] || a.sortKey - b.sortKey,
  );
}

export function summarizeActionItems(items: ActionItem[]): ActionItemSummary {
  return items.reduce<ActionItemSummary>(
    (summary, item) => {
      if (item.priority === 'p0') summary.p0 += 1;
      if (item.kind === 'interview_soon') summary.interviewSoon += 1;
      if (item.kind === 'stale_application') summary.stale += 1;
      if (item.kind === 'question_due') summary.dueQuestions += item.questionCount ?? 0;
      return summary;
    },
    { p0: 0, interviewSoon: 0, stale: 0, dueQuestions: 0 },
  );
}
