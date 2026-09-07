import { useEffect, useLayoutEffect, useRef, useState, type RefObject } from 'react';
import { Dropdown } from 'antd';
import { CheckOutlined, EllipsisOutlined, MessageOutlined } from '@ant-design/icons';
import { live2dPilotMascotRuntime, type PilotMascotActivity, type PilotMascotRuntimeController } from '@/features/pilotMascot/live2dRuntime';
import type { PilotMascotAnimationLevel, PilotMascotRect } from '@/features/pilotMascot/pilotMascotPreference';
import { effectiveHaruPresentation, readHaruPresentation, writeHaruPresentation, type HaruPresentationMode } from './haruPresentationPreference';
import styles from './CalendarHaruPresentation.module.css';

interface Props {
  activity: PilotMascotActivity;
  panelOpen: boolean;
  onToggle: () => void;
  triggerRef: RefObject<HTMLButtonElement>;
  onAnchorRectChange: (rect: PilotMascotRect) => void;
  animationLevel?: PilotMascotAnimationLevel;
}
const ACTIVITY_LABELS: Record<PilotMascotActivity, string> = {
  idle: '随时待命', thinking: '正在处理', preparing_voice: '准备语音', speaking: '正在回答',
  waiting_for_speech: '等待说话', listening: '正在聆听', speech_paused: '语音已暂停',
  transcribing: '正在转写', reviewing_voice: '等待检查语音', waiting_confirmation: '等待确认',
  success: '处理完成', error: '需要查看',
};
export default function CalendarHaruPresentation({ activity, panelOpen, onToggle, triggerRef, onAnchorRectChange, animationLevel = 'minimal' }: Props) {
  const [mode, setMode] = useState(readHaruPresentation);
  const [viewport, setViewport] = useState(() => ({ width: typeof window === 'undefined' ? 1440 : window.innerWidth, height: typeof window === 'undefined' ? 900 : window.innerHeight }));
  const [menuOwner, setMenuOwner] = useState<'context' | 'more' | null>(null);
  const [failed, setFailed] = useState(false);
  const [motionRevision, setMotionRevision] = useState(0);
  const canvas = useRef<HTMLCanvasElement>(null);
  const controller = useRef<PilotMascotRuntimeController>();
  const latestActivity = useRef(activity); latestActivity.current = activity;
  const effective = effectiveHaruPresentation(mode, viewport.width, viewport.height);
  const status = ACTIVITY_LABELS[activity];
  useEffect(() => {
    const resize = () => { setViewport({ width: window.innerWidth, height: window.innerHeight }); setMenuOwner(null); };
    window.addEventListener('resize', resize);
    const motion = typeof window.matchMedia === 'function' ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
    const changed = () => setMotionRevision((value) => value + 1);
    motion?.addEventListener('change', changed);
    return () => { window.removeEventListener('resize', resize); motion?.removeEventListener('change', changed); };
  }, []);
  useEffect(() => {
    if (!menuOwner) return;
    const close = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault(); event.stopPropagation();
      setMenuOwner(null); triggerRef.current?.focus({ preventScroll: true });
    };
    document.addEventListener('keydown', close, true);
    return () => document.removeEventListener('keydown', close, true);
  }, [menuOwner, triggerRef]);
  useEffect(() => {
    if (!canvas.current) return;
    const abort = new AbortController(); let disposed = false; setFailed(false);
    void live2dPilotMascotRuntime.mount(canvas.current, abort.signal, animationLevel).then((runtime) => {
      if (disposed) { runtime.dispose(); return; }
      controller.current = runtime; runtime.setZoom(1); runtime.setActivity(latestActivity.current);
    }).catch(() => { if (!disposed) setFailed(true); });
    return () => { disposed = true; abort.abort(); controller.current?.dispose(); controller.current = undefined; };
  }, [animationLevel, motionRevision]);
  useEffect(() => { controller.current?.setActivity(activity); }, [activity]);
  useLayoutEffect(() => {
    const report = () => { const rect = triggerRef.current?.getBoundingClientRect(); if (rect && rect.width > 0) onAnchorRectChange({ left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom }); };
    report();
    if (!triggerRef.current || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(report); observer.observe(triggerRef.current); return () => observer.disconnect();
  }, [effective, viewport, onAnchorRectChange, triggerRef]);
  const choose = (value: HaruPresentationMode) => { setMode(value); writeHaruPresentation(value); setMenuOwner(null); triggerRef.current?.focus({ preventScroll: true }); };
  const items = (['compact', 'character'] as const).map((value) => ({ key: value, role: 'menuitemradio', 'aria-checked': mode === value, icon: mode === value ? <CheckOutlined /> : <span className={styles.blankCheck} />, label: value === 'compact' ? '小窗展示' : '人物展示', onClick: () => choose(value) }));
  return (
    <div className={styles.presentation} data-haru-mode={effective} data-haru-preference={mode} data-haru-status={activity}>
      <Dropdown trigger={['contextMenu']} open={menuOwner === 'context'} onOpenChange={(open) => setMenuOwner((owner) => open ? 'context' : owner === 'context' ? null : owner)} autoFocus autoAdjustOverflow menu={{ items, 'aria-label': 'Haru 展示' }}>
        <button ref={triggerRef} className={styles.launcher} aria-label={`Haru · ${status} · ${panelOpen ? '收起' : '打开'}对话`} aria-expanded={panelOpen} aria-controls="haru-chat-window" onClick={onToggle} onKeyDown={(event) => { if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) { event.preventDefault(); setMenuOwner('context'); } }}>
          <span className={styles.portrait} aria-hidden="true"><span className={styles.modelFrame}><canvas ref={canvas} /></span>{failed && <span className={styles.fallback}><MessageOutlined /></span>}</span>
          <span className={styles.caption}><span className={styles.dot} />Haru{status !== '随时待命' && <small>{status}</small>}</span>
        </button>
      </Dropdown>
      <Dropdown trigger={['click']} open={menuOwner === 'more'} onOpenChange={(open) => setMenuOwner((owner) => open ? 'more' : owner === 'more' ? null : owner)} autoFocus menu={{ items, 'aria-label': 'Haru 展示' }} autoAdjustOverflow><button className={styles.more} aria-label="Haru 展示方式"><EllipsisOutlined /></button></Dropdown>
    </div>
  );
}
