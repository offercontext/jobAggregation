import { describe, expect, it } from 'vitest';
import applicationDetailSource from './ApplicationDetail.tsx?raw';
import reviewManagementSource from './ReviewManagementView.tsx?raw';
import appShellSource from '@/layout/AppShell.tsx?raw';

describe('interview review proposal history entry', () => {
  it('keeps history access when the note has no current event binding', () => {
    expect(applicationDetailSource).not.toContain('n.application_event_id != null &&');
    expect(reviewManagementSource).not.toContain('note.application_event_id != null &&');
    expect(applicationDetailSource).toContain('eventID={eventId}');
    expect(applicationDetailSource).not.toContain('eventID={editingNote.application_event_id}');
    expect(reviewManagementSource).toContain('eventID={proposalNote.application_event_id}');
    expect(appShellSource).toContain('interviewReviewProposalAttempts');
    expect(appShellSource).toContain('onInterviewReviewProposalAttemptChange');
  });
});
