import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
const styles = readFileSync(new URL('./CalendarView.module.css', import.meta.url), 'utf8');

describe('calendar theme', () => {
  it('uses theme surfaces rather than a white container under dark text tokens', () => {
    expect(styles).not.toContain('background: #fff;');
    expect(styles).toContain('background: var(--op-surface)');
  });
});
