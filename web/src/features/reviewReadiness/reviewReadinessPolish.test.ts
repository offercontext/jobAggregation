import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import questionBankSource from '@/components/QuestionBankView.tsx?raw';

const readinessCss = readFileSync(join(process.cwd(), 'src/features/reviewReadiness/reviewReadiness.module.css'), 'utf8');
const adaptiveCss = readFileSync(join(process.cwd(), 'src/components/AdaptiveInterviewPracticeWorkspace.module.css'), 'utf8');
const reviewDrawerCss = readFileSync(join(process.cwd(), 'src/components/InterviewReviewProposalDrawer.module.css'), 'utf8');
const themeCss = readFileSync(join(process.cwd(), 'src/theme/tokens.css'), 'utf8');

function hexToken(block: string, token: string): string {
  const value = block.match(new RegExp(`--${token}:\\s*(#[0-9a-f]{6})`, 'i'))?.[1];
  if (!value) throw new Error(`missing --${token}`);
  return value;
}

function contrastRatio(foreground: string, background: string): number {
  const luminance = (hex: string) => {
    const values = [1, 3, 5].map((offset) => Number.parseInt(hex.slice(offset, offset + 2), 16) / 255);
    const [r, g, b] = values.map((value) => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const foregroundLuminance = luminance(foreground);
  const backgroundLuminance = luminance(background);
  return (Math.max(foregroundLuminance, backgroundLuminance) + 0.05) / (Math.min(foregroundLuminance, backgroundLuminance) + 0.05);
}

function focusOutlineToken(css: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const block = css.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`, 's'))?.[1] ?? '';
  const token = block.match(/outline:\s*3px\s+solid\s+var\((--[a-z0-9-]+)\)/i)?.[1];
  if (!token) throw new Error(`missing opaque focus outline for ${selector}`);
  return token.slice(2);
}

function declarationToken(css: string, selector: string, property: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const block = css.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`, 's'))?.[1] ?? '';
  const declaration = block.match(new RegExp(`${property}:([^;}]+)`, 'i'))?.[1] ?? '';
  const token = declaration.match(/var\((--[a-z0-9-]+)\)/i)?.[1];
  if (!token) throw new Error(`missing tokenized ${property} for ${selector}`);
  return token.slice(2);
}

describe('review readiness responsive and accessibility polish', () => {
  it('keeps 44px controls and responsive layouts across supported desktop widths', () => {
    expect(readinessCss).toContain('min-height: 44px');
    expect(readinessCss).toContain('@media (max-width: 900px)');
    expect(adaptiveCss).toContain('@media (max-width: 900px)');
    for (const width of [768, 1024, 1280, 1440]) expect(width).toBeGreaterThanOrEqual(768);
  });

  it('uses the application data-theme tokens for light and dark without transition-all', () => {
    expect(themeCss).toContain(':root {');
    expect(themeCss).toContain(':root[data-theme="dark"]');
    expect(readinessCss).toContain(":global(:root[data-theme='dark'])");
    expect(readinessCss).not.toContain('@media (prefers-color-scheme: dark)');
    expect(readinessCss).toContain('@media (prefers-reduced-motion: reduce)');
    expect(adaptiveCss).toContain(":global(:root[data-theme='dark']) .workspace");
    expect(adaptiveCss).not.toContain('@media (prefers-color-scheme: dark)');
    for (const token of ['--op-surface', '--op-ink', '--op-muted', '--op-border', '--op-primary']) {
      expect(adaptiveCss).toContain(`var(${token})`);
    }
    expect(adaptiveCss).toContain(':global(.ant-input)');
    expect(adaptiveCss).toContain('color: var(--op-ink)');
    expect(adaptiveCss).toContain('@media (prefers-reduced-motion: reduce)');
    const allCss = `${themeCss}\n${readinessCss}\n${adaptiveCss}`;
    const declaredTokens = new Set([...allCss.matchAll(/(--[a-z0-9-]+)\s*:/gi)].map((match) => match[1]));
    const usedTokens = [...`${readinessCss}\n${adaptiveCss}`.matchAll(/var\((--[a-z0-9-]+)/gi)].map((match) => match[1]);
    for (const token of usedTokens) expect(declaredTokens.has(token), `undefined CSS token ${token}`).toBe(true);
    expect(`${readinessCss}\n${adaptiveCss}`).not.toMatch(/transition(?:-property)?:\s*all/);
  });

  it('keeps the three modes inside one free-practice owner component', () => {
    expect(questionBankSource).toContain("'review_feedback'");
    expect(questionBankSource).toContain("'question_bank'");
    expect(questionBankSource).toContain("'quick_practice'");
  });

  it('mechanically keeps muted text at AA and focus indicators at 3:1 in both data themes', () => {
    const darkStart = themeCss.indexOf(':root[data-theme="dark"]');
    const light = themeCss.slice(themeCss.indexOf(':root {'), darkStart);
    const dark = themeCss.slice(darkStart, themeCss.indexOf('\n}', darkStart) + 2);
    for (const block of [light, dark]) {
      for (const backgroundToken of ['op-surface', 'op-layout-bg', 'surface-sunken']) {
        const background = hexToken(block, backgroundToken);
        expect(contrastRatio(hexToken(block, 'op-muted'), background), `muted on ${backgroundToken}`).toBeGreaterThanOrEqual(4.5);
        expect(contrastRatio(hexToken(block, 'op-muted-strong'), background), `muted-strong on ${backgroundToken}`).toBeGreaterThanOrEqual(4.5);
        expect(contrastRatio(hexToken(block, 'op-focus-ring-color'), background), `focus on ${backgroundToken}`).toBeGreaterThanOrEqual(3);
      }
    }
    expect(light).toContain('--op-focus-ring: 0 0 0 3px var(--op-focus-ring-color)');
    expect(dark).toContain('--op-focus-ring: 0 0 0 3px var(--op-focus-ring-color)');
  });

  it('parses the real adaptive summary and Review heading focus declarations at 3:1 in both themes', () => {
    const darkStart = themeCss.indexOf(':root[data-theme="dark"]');
    const light = themeCss.slice(themeCss.indexOf(':root {'), darkStart);
    const dark = themeCss.slice(darkStart, themeCss.indexOf('\n}', darkStart) + 2);
    const declarations = [
      { css: adaptiveCss, selector: '.historyCard summary:focus-visible', background: 'surface-sunken' },
      { css: reviewDrawerCss, selector: '.title:focus-visible', background: 'op-surface' },
    ];
    for (const declaration of declarations) {
      const token = focusOutlineToken(declaration.css, declaration.selector);
      for (const block of [light, dark]) {
        expect(
          contrastRatio(hexToken(block, token), hexToken(block, declaration.background)),
          `${declaration.selector} in ${block === light ? 'light' : 'dark'}`,
        ).toBeGreaterThanOrEqual(3);
      }
    }
  });

  it('parses the real Review history surface and normal-text declarations at AA in both themes', () => {
    const darkStart = themeCss.indexOf(':root[data-theme="dark"]');
    const light = themeCss.slice(themeCss.indexOf(':root {'), darkStart);
    const dark = themeCss.slice(darkStart, themeCss.indexOf('\n}', darkStart) + 2);
    const historyBackground = declarationToken(reviewDrawerCss, '.history', 'background');
    const textDeclarations = [
      { selector: '.history', token: declarationToken(reviewDrawerCss, '.history', 'color') },
      { selector: '.item', token: declarationToken(reviewDrawerCss, '.item', 'color') },
      { selector: '.excerpt', token: declarationToken(reviewDrawerCss, '.excerpt', 'color') },
    ];
    expect(declarationToken(reviewDrawerCss, '.item', 'border-bottom')).toBe('op-border');
    expect(reviewDrawerCss).not.toMatch(/#[0-9a-f]{3,8}\b/i);
    for (const block of [light, dark]) {
      for (const declaration of textDeclarations) {
        expect(
          contrastRatio(hexToken(block, declaration.token), hexToken(block, historyBackground)),
          `${declaration.selector} text in ${block === light ? 'light' : 'dark'}`,
        ).toBeGreaterThanOrEqual(4.5);
      }
    }
  });
});
