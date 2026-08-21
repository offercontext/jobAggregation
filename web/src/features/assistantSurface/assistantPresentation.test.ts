import { describe, expect, it } from 'vitest';
import type { UITurn } from '@/components/ChatPanel/model';
import { compactMessageText, recentConversationTurns } from './assistantPresentation';

describe('assistant compact presentation', () => {
  it('keeps only the latest eight visible user and assistant turns', () => {
    const turns = Array.from({ length: 11 }, (_, index): UITurn => ({
      role: index % 2 ? 'assistant' : 'user',
      content: `message-${index}`,
    }));

    expect(recentConversationTurns(turns).map((turn) => turn.content)).toEqual([
      'message-3',
      'message-4',
      'message-5',
      'message-6',
      'message-7',
      'message-8',
      'message-9',
      'message-10',
    ]);
  });

  it('uses existing structured presentation without generating another summary', () => {
    const turn = {
      role: 'assistant' as const,
      content: '完整正文',
      presentation: {
        conclusion: '优先准备项目案例。',
        actions: ['补充量化结果', '安排模拟面试'],
        detailMarkdown: '完整正文',
      },
    };

    expect(compactMessageText(turn)).toBe(
      '优先准备项目案例。\n\n下一步\n1. 补充量化结果\n2. 安排模拟面试',
    );
  });

  it('deterministically trims long plain content', () => {
    expect(compactMessageText({ role: 'assistant', content: 'a'.repeat(700) })).toBe(
      `${'a'.repeat(520)}…`,
    );
  });
});
