import { describe, expect, it } from 'vitest';

async function source(relativePath: string): Promise<string> {
  const fsModule = 'node:fs';
  const { readFileSync } = (await import(fsModule)) as {
    readFileSync: (path: URL, encoding: string) => string;
  };
  return readFileSync(new URL(relativePath, import.meta.url), 'utf8');
}

describe('assistant surface frontend golden', () => {
  it('preserves all three existing Pilot presentation modes', async () => {
    const chatPanel = await source('../../components/ChatPanel/index.tsx');
    expect(chatPanel).toContain("variant = 'drawer'");
    expect(chatPanel).toContain("variant === 'rail'");
    expect(chatPanel).toContain("variant === 'page'");
    expect(chatPanel).not.toContain('variant="haru"');
  });

  it('keeps one Chat service and one SSE parser contract', async () => {
    const service = await source('../../services/chat.ts');
    expect(service.match(/export async function streamChat/g)).toHaveLength(1);
    expect(service.match(/export async function streamConfirmAction/g)).toHaveLength(1);
    expect(service.match(/function parseFrame/g)).toHaveLength(1);
  });

  it('keeps explicit stop and the existing approve and reject payload path', async () => {
    const controller = await source('./usePilotConversationController.ts');
    const chatPanel = await source('../../components/ChatPanel/index.tsx');
    expect(controller).toContain('beginActiveRequest');
    expect(controller).toContain('finishActiveRequest');
    expect(controller).toContain('activeRequest.controller.abort()');
    expect(controller).toContain('approved: true');
    expect(controller).toContain('approved: false');
    expect(controller).toContain('rejection_feedback');
    expect(chatPanel).not.toContain('new AbortController()');
    expect(chatPanel).not.toContain('activeRequestRef.current = {');
    expect(chatPanel).toContain("return 'stopped'");
  });

  it('does not turn an explicit stop into a Haru failure', async () => {
    const haru = await source('./HaruChatWindow.tsx');
    expect(haru).toContain("outcome === 'failed'");
    expect(haru).not.toContain('else if (!controller.loading)');
  });

  it('does not use Journal or persistent run recovery as visible assistant state', async () => {
    const feature = (await Promise.all([
      source('./assistantSurfaceReducer.ts'),
      source('./usePilotConversationController.ts'),
      source('./AssistantSurfaceProvider.tsx'),
    ])).join('\n');
    expect(feature).not.toMatch(/journal|localStorage[^\n]*run[_-]?id/i);
  });

  it('keeps old Pilot routes while removing Pilot from primary navigation', async () => {
    const navigation = await source('../../layout/navigation.ts');
    expect(navigation).toContain("| 'pilot'");
    expect(navigation).toContain("pilot: 'pilot'");
    expect(navigation).not.toMatch(/MODULE_NAV[\s\S]*label: 'Pilot'/);
  });

  it('keeps Haru as the character identity and routes assistant settings through Settings', async () => {
    const appShell = await source('../../layout/AppShell.tsx');
    const mascot = await source('../pilotMascot/PilotMascot.tsx');
    const settings = await source('../../components/SettingsView.tsx');
    const chatPanel = await source('../../components/ChatPanel/index.tsx');
    const contextPanel = await source('../../components/ChatPanel/ContextPanel.tsx');
    expect(appShell).not.toContain('onOpenSettings={() => setAISettingsOpen(true)}');
    expect(mascot).not.toMatch(/Haru · Pilot|Pilot 看板娘/);
    expect(settings).not.toContain('Pilot 看板娘');
    expect(chatPanel).toContain('打开设置');
    expect(chatPanel).not.toContain('打开 AI 设置');
    expect(contextPanel).toContain('打开设置');
    expect(contextPanel).not.toContain('AI 设置');
  });

  it('shows draft context only before a persisted conversation becomes active', async () => {
    const haru = await source('./HaruChatWindow.tsx');
    expect(haru).toContain(
      'controller.conversationId === undefined ? controller.draftContext?.context_label : undefined',
    );
  });
});
