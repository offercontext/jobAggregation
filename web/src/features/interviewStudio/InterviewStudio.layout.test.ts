import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const styles = readFileSync(new URL('./InterviewStudio.module.css', import.meta.url), 'utf8');

describe('InterviewStudio viewport layout', () => {
  it('keeps the conversation and answer workspace in one bounded two-column viewport', () => {
    expect(styles).toMatch(/\.studio\s*\{[^}]*grid-template-rows:\s*auto minmax\(0, 1fr\)[^}]*overflow:\s*hidden/s);
    expect(styles).toMatch(/\.main\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1\.75fr\) minmax\(360px, 1fr\)[^}]*overflow:\s*hidden/s);
    expect(styles).toMatch(/\.conversationPane\s*\{[^}]*min-height:\s*0[^}]*overflow:\s*hidden/s);
    expect(styles).toMatch(/\.conversationScroll\s*\{[^}]*overflow-y:\s*auto/s);
    expect(styles).toMatch(/\.turn h3\s*\{[^}]*overflow-wrap:\s*anywhere/s);
    expect(styles).toMatch(/\[data-interview-studio-question\]:focus\s*\{[^}]*outline:\s*none/s);
    expect(styles).toMatch(/\.feedbackCard:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--op-focus-ring-color\)/s);
  });

  it('keeps a tall answer workspace scrollable with sticky confirmation actions', () => {
    expect(styles).toMatch(/\.answerWorkspace\s*\{[^}]*min-height:\s*0[^}]*overflow:\s*hidden/s);
    expect(styles).toMatch(/\.answerWorkspaceBody\s*\{[^}]*overflow-y:\s*auto/s);
    expect(styles).toMatch(/\.workspaceActions\s*\{[^}]*position:\s*sticky[^}]*bottom:\s*0/s);
  });

  it('turns the answer workspace into a bounded bottom drawer on narrow screens', () => {
    expect(styles).toMatch(/@media\s*\(max-width:\s*1179px\)[\s\S]*\.answerWorkspace\s*\{[^}]*position:\s*fixed[^}]*bottom:\s*0/s);
    expect(styles).toMatch(/\.mobileWorkspaceOpen\s*\{[^}]*transform:\s*translateY\(0\)/s);
  });
});
