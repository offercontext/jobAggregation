import type { UITurn } from '@/components/ChatPanel/model';

const HARU_MESSAGE_LIMIT = 8;
const HARU_TEXT_LIMIT = 520;

export function recentConversationTurns(turns: UITurn[]): UITurn[] {
  return turns
    .filter((turn) => turn.role === 'user' || turn.role === 'assistant')
    .slice(-HARU_MESSAGE_LIMIT);
}
export function compactMessageText(turn: UITurn): string {
  if (turn.presentation) {
    const actions = turn.presentation.actions
      .map((action, index) => `${index + 1}. ${action}`)
      .join('\n');
    return actions
      ? `${turn.presentation.conclusion}\n\n下一步\n${actions}`
      : turn.presentation.conclusion;
  }
  const text = turn.content.trim();
  const characters = Array.from(text);
  return characters.length > HARU_TEXT_LIMIT
    ? `${characters.slice(0, HARU_TEXT_LIMIT).join('')}…`
    : text;
}
