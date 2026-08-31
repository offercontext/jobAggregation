// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const service = vi.hoisted(() => ({ advisory: vi.fn() }));
vi.mock('./service', () => ({ getEventReadinessFeedback: service.advisory }));

const { ReadinessFeedbackAdvisory } = await import('./ReadinessFeedbackAdvisory');

let root: Root;
let host: HTMLDivElement;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  service.advisory.mockReset();
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => { act(() => root.unmount()); host.remove(); });

describe('ReadinessFeedbackAdvisory', () => {
  it('defaults to zero selected, caps at eight, and disables stale/retracted items', async () => {
    const available = Array.from({ length: 9 }, (_, index) => ({
      signalId: index + 1,
      versionId: index + 10,
      practiceSourceFingerprint: `sha256:${String(index).repeat(64).slice(0, 64)}`,
      practiceTargetFingerprint: `sha256:${String(index + 1).repeat(64).slice(0, 64)}`,
      state: 'available' as const,
      practiceState: 'not_started' as const,
      selected: false,
      title: `重点 ${index + 1}`,
      sourceLabel: '第 1 轮面试复盘',
    }));
    service.advisory.mockResolvedValue({ schema_version: 1, application_id: 3, event_id: 20, items: [...available, { ...available[0], signalId: 99, versionId: 99, title: '已撤销', state: 'retracted' }] });
    const changes: number[][] = [];
    let selected: number[] = [];
    const render = () => root.render(<ReadinessFeedbackAdvisory applicationId={3} eventId={20} selectedVersionIds={selected} onSelectionChange={(ids) => { changes.push(ids); selected = ids; render(); }} />);
    act(render);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(changes).toEqual([]);
    const boxes = [...host.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')];
    expect(boxes).toHaveLength(10);
    expect(boxes[9]!.disabled).toBe(true);
    for (const box of boxes.slice(0, 9)) act(() => box.click());
    expect(changes[changes.length - 1]).toHaveLength(8);
    expect(host.textContent).toContain('最多选择 8 个');
  });

  it('surfaces read errors and never converts them to an empty result', async () => {
    service.advisory.mockRejectedValue(new Error('network'));
    act(() => root.render(<ReadinessFeedbackAdvisory applicationId={3} eventId={20} selectedVersionIds={[]} onSelectionChange={() => undefined} />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(host.querySelector('[role="alert"]')).not.toBeNull();
    expect(host.textContent).toContain('暂时无法读取准备重点');
  });

  it('keeps a practiced signal selectable for preparation while completed practice stays non-launchable', async () => {
    const practiced = {
      signalId: 5,
      versionId: 15,
      practiceSourceFingerprint: `sha256:${'a'.repeat(64)}`,
      practiceTargetFingerprint: `sha256:${'b'.repeat(64)}`,
      state: 'practiced' as const,
      practiceState: 'completed' as const,
      selected: false,
      title: '已经练过的准备重点',
      sourceLabel: '第 1 轮面试复盘',
    };
    service.advisory.mockResolvedValue({ schema_version: 1, application_id: 3, event_id: 20, items: [practiced] });
    const change = vi.fn();
    const openPractice = vi.fn();
    act(() => root.render(<ReadinessFeedbackAdvisory applicationId={3} eventId={20} selectedVersionIds={[]} onSelectionChange={change} onOpenPractice={openPractice} />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    const box = host.querySelector<HTMLInputElement>('input[type="checkbox"]')!;
    expect(box.disabled).toBe(false);
    act(() => box.click());
    expect(change).toHaveBeenCalledWith([15]);
    expect(host.textContent).not.toContain('用这个重点练习');
  });
});
