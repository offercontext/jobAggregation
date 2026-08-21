import { useRef } from 'react';
import PilotMascot, {
  type PilotMascotActivity,
  type PilotMascotNotification,
} from '@/features/pilotMascot/PilotMascot';
import { useAssistantSurface } from './AssistantSurfaceProvider';
import HaruChatWindow from './HaruChatWindow';

interface Props {
  visible: boolean;
  activity: PilotMascotActivity;
  notification?: PilotMascotNotification | null;
  zoom: number;
  onZoomChange: (zoom: number) => void;
  onHide: () => void;
  onOpen?: () => void;
  onExpand?: () => void;
}

export default function HaruDock({
  visible,
  activity,
  notification,
  zoom,
  onZoomChange,
  onHide,
  onOpen,
  onExpand,
}: Props) {
  const surface = useAssistantSurface();
  const triggerRef = useRef<HTMLButtonElement>(null);

  if (!visible) return null;

  return (
    <>
      <PilotMascot
        activity={notification?.status ?? activity}
        panelOpen={surface.surface === 'haru_chat'}
        onTogglePilot={() => {
          if (surface.surface === 'haru_chat') surface.closeSurface();
          else if (onOpen) onOpen();
          else surface.openHaru();
        }}
        onHide={() => {
          surface.closeSurface();
          onHide();
        }}
        zoom={zoom}
        onZoomChange={onZoomChange}
        notification={notification}
        placement="contextual"
        triggerRef={triggerRef}
      />
      <HaruChatWindow returnFocusRef={triggerRef} onExpand={onExpand} />
    </>
  );
}
