import { useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import CalendarHaruPresentation from './CalendarHaruPresentation';
import PilotMascot, {
  type PilotMascotActivity,
  type PilotMascotAnimationLevel,
} from '@/features/pilotMascot/PilotMascot';
import type { PilotMascotRect } from '@/features/pilotMascot/pilotMascotPreference';
import { useAssistantSurface } from './AssistantSurfaceProvider';
import HaruChatWindow from './HaruChatWindow';

interface Props {
  visible: boolean;
  activity: PilotMascotActivity;
  zoom: number;
  onZoomChange: (zoom: number) => void;
  animationLevel?: PilotMascotAnimationLevel;
  positionResetToken?: number;
  onHide: () => void;
  onOpen?: () => void;
  onExpand?: () => void;
  calendarHost?: HTMLElement | null;
  calendarActive?: boolean;
}

export default function HaruDock({
  visible,
  activity,
  zoom,
  onZoomChange,
  animationLevel,
  positionResetToken,
  onHide,
  onOpen,
  onExpand,
  calendarHost,
  calendarActive = false,
}: Props) {
  const surface = useAssistantSurface();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const [anchorRect, setAnchorRect] = useState<PilotMascotRect>();

  if (!visible) return null;

  const notification = surface.completionNotice
    ? {
        status: surface.completionNotice.status === 'completed' ? 'success' as const : 'error' as const,
        conversationId: surface.completionNotice.conversationId,
      }
    : null;

  return (
    <>
      {calendarActive ? (calendarHost ? createPortal(<CalendarHaruPresentation
        activity={notification?.status ?? activity}
        panelOpen={surface.surface === 'haru_chat'}
        onToggle={() => {
          if (notification) surface.openCompletionNotice();
          else if (surface.surface === 'haru_chat') surface.closeSurface();
          else if (onOpen) onOpen();
          else surface.openHaru();
        }}
        triggerRef={triggerRef}
        onAnchorRectChange={setAnchorRect}
        animationLevel={animationLevel}
      />, calendarHost) : null) : <PilotMascot
        activity={notification?.status ?? activity}
        panelOpen={surface.surface === 'haru_chat'}
        onTogglePilot={() => {
          if (notification) surface.openCompletionNotice();
          else if (surface.surface === 'haru_chat') surface.closeSurface();
          else if (onOpen) onOpen();
          else surface.openHaru();
        }}
        onHide={() => {
          surface.closeSurface();
          onHide();
        }}
        zoom={zoom}
        onZoomChange={onZoomChange}
        animationLevel={animationLevel}
        positionResetToken={positionResetToken}
        onAnchorRectChange={setAnchorRect}
        notification={notification}
        placement="contextual"
        triggerRef={triggerRef}
      />}
      <HaruChatWindow returnFocusRef={triggerRef} onExpand={onExpand} anchorRect={anchorRect} />
    </>
  );
}
