import { useEffect } from 'react';
import ChatPanel, { type Props as ChatPanelProps } from '@/components/ChatPanel';
import { useAssistantSurface } from './AssistantSurfaceProvider';

export default function PilotWorkspace(props: Omit<ChatPanelProps, 'variant' | 'open'>) {
  const surface = useAssistantSurface();
  useEffect(() => {
    surface.openPilot();
  }, [surface.openPilot]);
  return <ChatPanel {...props} variant="page" open />;
}
