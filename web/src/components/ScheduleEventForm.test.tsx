import { describe, expect, it } from 'vitest';
import source from './ScheduleEventForm.tsx?raw';

describe('ScheduleEventForm presentation', () => {
  it('exposes a stable responsive form surface without changing submission fields', () => {
    expect(source).toContain('data-testid="schedule-event-form"');
    expect(source).toContain('workflowStyles.formGridCompact');
    expect(source).toContain('workflowStyles.formGridStatus');
    expect(source).toContain('name="scheduled_at"');
    expect(source).toContain('name="duration_minutes"');
    expect(source).toContain('handleFinish');
    expect(source).toContain('headingRef');
    expect(source).toContain('onSuccess?.(savedEvent)');
  });
});
