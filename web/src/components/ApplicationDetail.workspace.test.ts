import { describe, expect, it } from 'vitest';
import source from './ApplicationDetail.tsx?raw';

describe('ApplicationDetail staged workspace', () => {
  it('keeps one stage action and moves low-frequency actions into more', () => {
    expect(source).toContain('getApplicationWorkspaceStage');
    expect(source).toContain('stage.primaryActionLabel');
    expect(source).toContain('更多操作');
    expect(source).toContain('menu={{ items: moreActionItems');
  });

  it('uses stable business sections and the unified Haru entry copy', () => {
    expect(source).toContain('概览');
    expect(source).toContain('岗位与材料');
    expect(source).toContain('日程与沟通');
    expect(source).toContain('面试');
    expect(source).toContain('结果');
    expect(source).toContain('让 Haru 帮我');
    expect(source).not.toContain('问 Pilot');
    expect(source).not.toContain('在 Pilot 中评估');
  });

  it('exposes a keyboard-reachable three-part detail workspace', () => {
    expect(source).toContain('role="tablist"');
    expect(source).toContain('role="tab"');
    expect(source).toContain('aria-selected');
    expect(source).toContain('aria-controls');
    expect(source).toContain('onKeyDown');
    expect(source).toContain('role="tabpanel"');
    expect(source).toContain('最近变化');
    expect(source).toContain('JD 摘要');
    expect(source).toContain('准备');
    expect(source).toContain('进展');
  });

  it('keeps progress read-only while accepting optional application-linked offers', () => {
    expect(source).toContain('offers?: Offer[]');
    expect(source).toContain('linkedOffers');
    expect(source).toContain('进展时间线');
    expect(source).toContain('OFFER_STATUS_LABELS');
    expect(source).toContain('offersError?: boolean');
    expect(source).toContain('部分日程进展暂时无法读取');
    expect(source).toContain('eventSubtypeLabel');
    expect(source).toContain('eventStatusLabel');
  });

  it('generation-fences the Offer negotiation handoff before closing the task owner', () => {
    const handoffStart = source.indexOf('onOpenPilotChat={onOpenOfferNegotiationPilot');
    const handoffEnd = source.indexOf('onClose={close}', handoffStart);
    const handoffSource = source.slice(handoffStart, handoffEnd);

    expect(handoffStart).toBeGreaterThanOrEqual(0);
    expect(handoffSource).toContain('if (!isCurrent()) return;');
    expect(handoffSource).toContain('if (!onOpenOfferNegotiationPilot(currentOffer, brief)) return;');
    expect(handoffSource).toContain('close();');
  });
});
