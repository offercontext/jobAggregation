import { useEffect, useState } from 'react';
import { Segmented, Typography } from 'antd';
import AdaptiveInterviewPracticeWorkspace from './AdaptiveInterviewPracticeWorkspace';
import type { AdaptivePracticeFocus, AdaptivePracticeOwnerDraft } from '@/types/adaptiveInterviewPractice';
import InterviewReadinessCenter, {
  type QuickPracticeStudioContext,
  type ResumeInput,
} from '@/features/interviewReadiness/InterviewReadinessCenter';

const { Paragraph, Title } = Typography;

type InterviewPracticeMode = 'quick' | 'review';

export interface InterviewPracticeViewProps {
  adaptiveFocus?: AdaptivePracticeFocus;
  adaptiveOwnerGeneration?: number;
  recoveryOwnerGeneration?: number | null;
  adaptivePracticeDrafts?: Readonly<Record<string, AdaptivePracticeOwnerDraft>>;
  onAdaptivePracticeDraftChange?: (
    key: string,
    draft: AdaptivePracticeOwnerDraft | null,
    retireOwnerKey?: string,
  ) => boolean | void;
  onAdaptivePracticeGuardChange?: (guard: { pending: boolean; unsaved: boolean }) => void;
  quickPracticeResumes?: ResumeInput;
  onOpenStudio?: (context: QuickPracticeStudioContext) => void;
}

export default function InterviewPracticeView({
  adaptiveFocus,
  adaptiveOwnerGeneration,
  recoveryOwnerGeneration,
  adaptivePracticeDrafts,
  onAdaptivePracticeDraftChange,
  onAdaptivePracticeGuardChange,
  quickPracticeResumes,
  onOpenStudio,
}: InterviewPracticeViewProps) {
  const [mode, setMode] = useState<InterviewPracticeMode>(adaptiveFocus ? 'review' : 'quick');

  useEffect(() => {
    setMode(adaptiveFocus ? 'review' : 'quick');
  }, [adaptiveFocus?.ownerGeneration, adaptiveFocus?.signalVersionId, adaptiveFocus?.targetEventId]);

  return (
    <section
      data-testid="interview-practice-surface"
      className="op-view-enter"
      style={{ padding: 24 }}
      aria-labelledby="interview-practice-title"
    >
      <div className="op-section-heading" style={{ marginBottom: 18 }}>
        <div>
          <Title id="interview-practice-title" level={2} style={{ margin: 0 }}>面试练习</Title>
          <Paragraph type="secondary" style={{ margin: '6px 0 0' }}>
            选择快速模拟，或围绕已确认的复盘重点练习回答。
          </Paragraph>
        </div>
        <Segmented
          aria-label="面试练习类型"
          value={mode}
          onChange={(value) => setMode(value as InterviewPracticeMode)}
          options={[
            { label: '快速模拟', value: 'quick' },
            { label: '复盘重点练习', value: 'review' },
          ]}
        />
      </div>

      <div data-testid="quick-interview-practice" hidden={mode !== 'quick'}>
        <InterviewReadinessCenter
          initialMode="quick"
          fixedMode="quick"
          actionEmphasis="primary"
          resumes={quickPracticeResumes}
          onOpenStudio={(context) => {
            if (context.kind === 'quick_practice') onOpenStudio?.(context);
          }}
        />
      </div>
      <div data-testid="review-focus-practice" hidden={mode !== 'review'}>
        <AdaptiveInterviewPracticeWorkspace
          focus={adaptiveFocus}
          ownerGeneration={adaptiveOwnerGeneration}
          recoveryOwnerGeneration={recoveryOwnerGeneration}
          drafts={adaptivePracticeDrafts}
          onDraftChange={onAdaptivePracticeDraftChange}
          onGuardChange={onAdaptivePracticeGuardChange}
        />
      </div>
    </section>
  );
}
