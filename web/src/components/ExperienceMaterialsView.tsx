import type { ReactNode } from 'react';
import InterviewStoryLibraryView, { type InterviewStoryOpenDraft } from './InterviewStoryLibraryView';
import {
  projectExperienceMaterials,
  type MaterialProjectionState,
  type MaterialSourceEnvelope,
  type MaterialSourceState,
} from '@/features/materialSurfaces/materialClassification';

export interface ExperienceMaterialsViewProps {
  readonly onOpenDraft?: (input: InterviewStoryOpenDraft) => void;
  readonly onBack?: () => void;
  /** AppShell owns this read-only query; the view never fetches it itself. */
  readonly confirmedCaptures?: readonly object[] | MaterialSourceEnvelope<readonly object[]>;
  readonly confirmedCapturesLoading?: boolean;
  readonly confirmedCapturesError?: boolean;
  readonly confirmedCapturesState?: MaterialSourceState;
}

const EMPTY_CAPTURE_RECORDS: readonly object[] = Object.freeze([]);

function stateForProps(props: ExperienceMaterialsViewProps): MaterialSourceEnvelope<readonly object[]> | readonly object[] {
  if (props.confirmedCapturesLoading) return { status: 'loading' };
  if (props.confirmedCapturesError) return { status: 'error' };
  if (props.confirmedCapturesState === 'absent' || props.confirmedCapturesState === 'unknown') {
    return { status: props.confirmedCapturesState };
  }
  if (props.confirmedCapturesState) {
    if (props.confirmedCapturesState === 'ready' || props.confirmedCapturesState === 'empty' || props.confirmedCapturesState === 'partial') {
      const raw = props.confirmedCaptures;
      if (raw !== null && raw !== undefined) {
        try {
          if (!Array.isArray(raw)) return raw;
          return { status: props.confirmedCapturesState, value: raw };
        } catch {
          return { status: props.confirmedCapturesState, value: null };
        }
      }
      // An explicit ready/empty/partial state without its collection is a
      // malformed source envelope, not an empty successful result.
      return { status: props.confirmedCapturesState, value: null };
    }
    return { status: props.confirmedCapturesState };
  }
  return props.confirmedCaptures ?? EMPTY_CAPTURE_RECORDS;
}

function captureStateLabel(state: MaterialProjectionState): ReactNode {
  if (state === 'loading') return <div role="status">正在加载经历素材</div>;
  if (state === 'error' || state === 'unavailable') return <div role="alert">面试片段暂时不可用，请稍后重试。</div>;
  if (state === 'empty') return <div role="status">还没有已确认的面试片段</div>;
  if (state === 'partial') return <div role="status">部分面试片段暂时不可用，已展示可用内容。</div>;
  return null;
}

/**
 * Canonical reviews surface. Stories retain their existing read/manage owner;
 * confirmed captures are a separate, read-only projection.  This component
 * deliberately receives AppShell's query result instead of creating another
 * request owner for the same capture collection.
 */
export default function ExperienceMaterialsView({
  onOpenDraft = () => undefined,
  onBack,
  ...props
}: ExperienceMaterialsViewProps) {
  const captureProjection = projectExperienceMaterials({
    captures: stateForProps(props),
  });

  return (
    <section aria-label="经历素材" data-testid="experience-materials-view">
      <header>
        <h2>经历素材</h2>
        <p>已确认的经历故事和面试片段会保留在这里，来源变化时仍保留当时确认的内容。</p>
      </header>

      <section aria-label="已确认面试片段" data-testid="confirmed-capture-list">
        <h3>已确认面试片段</h3>
        {captureStateLabel(captureProjection.state)}
        {captureProjection.items.length > 0 ? (
          <ul>
            {captureProjection.items.map((item) => (
              <li key={item.internalKey}>
                <strong>{item.title}</strong>
                <p>{item.summary}</p>
                {item.sourceState === 'source_changed' ? (
                  <span>来源已更新，已确认内容仍保留</span>
                ) : item.sourceState === 'unavailable' ? (
                  <span>部分来源暂时不可用</span>
                ) : null}
              </li>
            ))}
          </ul>
        ) : null}
      </section>

      <section aria-label="经历故事" data-testid="experience-story-library">
        <InterviewStoryLibraryView onBack={onBack} onOpenDraft={onOpenDraft} />
      </section>
    </section>
  );
}
