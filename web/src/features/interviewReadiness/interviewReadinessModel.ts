export type ReadinessStatus = 'ready' | 'needs_input' | 'source_changed' | 'unknown' | 'unavailable';

export interface ReadinessItem {
  key: 'application' | 'jd' | 'resume' | 'event' | 'preparation';
  label: string;
  status: ReadinessStatus;
  detail: string;
  actionLabel?: string;
}

export interface ReadinessResult {
  ready: boolean;
  items: readonly ReadinessItem[];
}

export interface RealInterviewReadinessInput {
  application: { id: number } | null;
  jd: { status: 'ready' | 'missing' | 'source_changed' | 'unknown' | 'unavailable' };
  resume: { id: number } | null;
  event: { id: number } | null;
}

export interface QuickPracticeDraft {
  positionName: string;
  jdText: string;
  jdConfirmed: boolean;
  resumeId: number | undefined;
}

export type QuickPracticeResumeSourceStatus = 'ready' | 'loading' | 'error' | 'absent' | 'unknown';

export interface QuickPracticeReadinessOptions {
  /** The source state is authoritative; rows carried by an unresolved envelope are not usable. */
  readonly resumeSourceStatus?: QuickPracticeResumeSourceStatus;
}

function isValidId(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0;
}

export function buildRealInterviewReadiness(input: RealInterviewReadinessInput): ReadinessResult {
  const applicationReady = isValidId(input.application?.id);
  const resumeReady = isValidId(input.resume?.id);
  const eventReady = isValidId(input.event?.id);
  const items: ReadinessItem[] = [
    {
      key: 'application',
      label: '投递',
      status: applicationReady ? 'ready' : 'needs_input',
      detail: applicationReady ? '已锁定当前可见的投递。' : '需要一条当前可见的投递。',
      actionLabel: applicationReady ? undefined : '选择投递',
    },
    {
      key: 'jd',
      label: '岗位资料',
      status: input.jd.status === 'ready' ? 'ready' : input.jd.status === 'missing' ? 'needs_input' : input.jd.status,
      detail: input.jd.status === 'ready'
        ? '当前 JD（只读）已确认。'
        : input.jd.status === 'source_changed'
          ? '岗位资料版本已变化，需要重新确认。'
          : input.jd.status === 'missing'
            ? '还没有当前已确认的岗位资料版本。'
            : '岗位资料状态暂时无法确认。',
      actionLabel: input.jd.status === 'ready' ? '更新岗位资料' : '补充岗位资料',
    },
    {
      key: 'resume',
      label: '简历',
      status: resumeReady ? 'ready' : 'needs_input',
      detail: resumeReady ? '已选择一份已保存简历。' : '需要显式选择一份当前可见的已保存简历。',
      actionLabel: resumeReady ? undefined : '选择简历',
    },
    {
      key: 'event',
      label: '面试安排',
      status: eventReady ? 'ready' : 'needs_input',
      detail: eventReady ? '已锁定已排期的面试事件。' : '需要一条已排期且可见的面试事件。',
      actionLabel: eventReady ? undefined : '安排面试',
    },
  ];
  return Object.freeze({
    ready: items.slice(0, 4).every((item) => item.status === 'ready'),
    items: Object.freeze(items.map((item) => Object.freeze(item))),
  });
}

export function buildQuickPracticeReadiness(
  draft: QuickPracticeDraft,
  options: QuickPracticeReadinessOptions = {},
): ReadinessResult {
  const draftStatus = validateQuickPracticeDraft(draft);
  const resumeSourceStatus = options.resumeSourceStatus ?? 'ready';
  const resumeSourceReady = resumeSourceStatus === 'ready';
  const resumeStatus: ReadinessStatus = resumeSourceReady
    ? isValidId(draft.resumeId) ? 'ready' : 'needs_input'
    : resumeSourceStatus === 'loading' ? 'unknown' : 'unavailable';
  const resumeDetail = resumeSourceReady
    ? isValidId(draft.resumeId) ? '将冻结当前已保存版本。' : '需要选择一份当前可见的已保存简历。'
    : resumeSourceStatus === 'loading'
      ? '简历列表正在加载，加载完成后才能开始。'
      : resumeSourceStatus === 'absent'
        ? '简历列表尚未加载，加载完成后才能开始。'
        : '简历列表状态暂时无法确认，请重试后再开始。';
  const items: ReadinessItem[] = [
    {
      key: 'application',
      label: '练习档案',
      status: 'ready',
      detail: '快速练习不会创建投递或日程。',
    },
    {
      key: 'jd',
      label: '岗位资料',
      status: draft.positionName.trim().length > 0 && draft.jdText.trim().length > 0 && draft.jdConfirmed ? 'ready' : 'needs_input',
      detail: draft.jdConfirmed ? '已核对，本次按此岗位资料练习。' : '粘贴 JD 后请明确勾选已核对。',
      actionLabel: '粘贴 JD',
    },
    {
      key: 'resume',
      label: '简历',
      status: resumeStatus,
      detail: resumeDetail,
      actionLabel: resumeSourceReady ? '选择简历' : undefined,
    },
    {
      key: 'event',
      label: '写入边界',
      status: 'ready',
      detail: '只创建快速练习档案，不写入投递、日历、Knowledge、Memory、Story 或 Offer。',
    },
    {
      key: 'preparation',
      label: '输入边界',
      status: 'ready',
      detail: '仅发送冻结 JD、冻结简历和本次确认的问答。',
    },
  ];
  return Object.freeze({
    ready: draftStatus.ok && resumeSourceReady && items.slice(1, 3).every((item) => item.status === 'ready'),
    items: Object.freeze(items.map((item) => Object.freeze(item))),
  });
}

export function validateQuickPracticeDraft(
  draft: QuickPracticeDraft,
): { ok: true } | { ok: false; field: 'positionName' | 'jdText' | 'jdConfirmed' | 'resumeId' } {
  if (!draft.positionName.trim() || [...draft.positionName].length > 200) return { ok: false, field: 'positionName' };
  if (!draft.jdText.trim() || draft.jdText.length > 100_000) return { ok: false, field: 'jdText' };
  if (!draft.jdConfirmed) return { ok: false, field: 'jdConfirmed' };
  if (!isValidId(draft.resumeId)) return { ok: false, field: 'resumeId' };
  return { ok: true };
}
