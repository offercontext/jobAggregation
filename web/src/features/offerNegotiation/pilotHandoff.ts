import type { Offer } from '@/types/offer';

export interface OfferNegotiationPilotBrief {
  goal: string;
  concerns: string;
  scenario: string;
}

function trimmed(value: string): string {
  return value.trim();
}

export function buildOfferNegotiationPilotDraft(
  offer: Pick<Offer, 'company_name' | 'position_name'>,
  brief: OfferNegotiationPilotBrief,
): string {
  const goal = trimmed(brief.goal);
  const concerns = trimmed(brief.concerns);
  const scenario = trimmed(brief.scenario);
  const lines = [
    `我想继续讨论 ${trimmed(offer.company_name)} · ${trimmed(offer.position_name)} 的谈薪策略。`,
  ];

  if (goal) lines.push(`我的目标：${goal}`);
  if (concerns) lines.push(`我的顾虑：${concerns}`);
  if (scenario) lines.push(`沟通场景：${scenario}`);

  if (!goal && !concerns && !scenario) {
    lines.push('请帮我一起梳理谈薪目标、底线和沟通方式。');
  } else {
    lines.push('请结合这份 Offer，帮我判断优先争取什么，并陪我演练表达。');
  }
  lines.push('消息发送前我会再确认和修改。');
  return lines.join('\n');
}
