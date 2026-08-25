import { normalizePilotMascotZoom, type PilotMascotAnimationLevel } from './pilotMascotPreference';

export type PilotMascotActivity =
  | 'idle'
  | 'thinking'
  | 'preparing_voice'
  | 'speaking'
  | 'waiting_for_speech'
  | 'listening'
  | 'speech_paused'
  | 'transcribing'
  | 'reviewing_voice'
  | 'waiting_confirmation'
  | 'success'
  | 'error';

export interface PilotMascotRuntimeController {
  setActivity(activity: PilotMascotActivity): void;
  setZoom(zoom: number): void;
  dispose(): void;
}

export interface PilotMascotRuntime {
  mount(
    canvas: HTMLCanvasElement,
    signal?: AbortSignal,
    animationLevel?: PilotMascotAnimationLevel,
  ): Promise<PilotMascotRuntimeController>;
}

export function serializePilotMascotRuntime(runtime: PilotMascotRuntime): PilotMascotRuntime {
  const canvasQueues = new WeakMap<HTMLCanvasElement, Promise<void>>();

  return {
    async mount(canvas, signal, animationLevel) {
      const previous = canvasQueues.get(canvas) ?? Promise.resolve();
      let releaseQueue!: () => void;
      const current = new Promise<void>((resolve) => {
        releaseQueue = resolve;
      });
      canvasQueues.set(canvas, current);
      await previous.catch(() => undefined);

      let mountedController: PilotMascotRuntimeController | undefined;
      const release = () => {
        if (mountedController) {
          mountedController.dispose();
          mountedController = undefined;
        }
        if (canvasQueues.get(canvas) === current) canvasQueues.delete(canvas);
        releaseQueue();
      };

      try {
        if (signal?.aborted) throw new DOMException('Mascot mount aborted', 'AbortError');
        mountedController = await runtime.mount(canvas, signal, animationLevel);
        if (signal?.aborted) throw new DOMException('Mascot mount aborted', 'AbortError');
        return {
          setActivity: (activity) => mountedController?.setActivity(activity),
          setZoom: (zoom) => mountedController?.setZoom(zoom),
          dispose: release,
        };
      } catch (error) {
        release();
        throw error;
      }
    },
  };
}

const MODEL_URL = '/live2d/haru-receptionist/haru_greeter_t03.model3.json';
const CUBISM_CORE_URL = '/live2d/live2dcubismcore.min.js';
const CUBISM_CORE_SCRIPT_ID = 'offerpilot-live2d-cubism-core';

interface Live2dApplication {
  stage: { addChild: (model: Live2dModelInstance) => void };
  render: () => void;
  destroy: (removeView: boolean, options: Record<string, boolean>) => void;
}

interface Live2dModelInstance {
  width: number;
  height: number;
  x: number;
  y: number;
  anchor: { set: (x: number, y: number) => void };
  scale: { set: (scale: number) => void };
  motion?: (group: string, index: number, priority: number) => Promise<boolean> | boolean;
  expression?: (name: string) => Promise<boolean> | boolean;
  destroy: (options: { children: boolean }) => void;
}

interface Live2dRuntimeModules {
  Application: new (options: Record<string, unknown>) => Live2dApplication;
  Ticker: unknown;
  Live2DModel: {
    registerTicker: (ticker: unknown) => void;
    from: (url: string, options: { autoInteract: boolean; autoUpdate: boolean }) => Promise<Live2dModelInstance>;
  };
}

export interface Live2dRuntimeDependencies {
  loadModules: () => Promise<Live2dRuntimeModules>;
  prefersReducedMotion: () => boolean;
  createResizeObserver: (callback: () => void) => Pick<ResizeObserver, 'observe' | 'disconnect'>;
}

let cubismCorePromise: Promise<void> | undefined;

function ensureCubismCore(): Promise<void> {
  if ('Live2DCubismCore' in globalThis) return Promise.resolve();
  if (cubismCorePromise) return cubismCorePromise;
  cubismCorePromise = new Promise((resolve, reject) => {
    const existing = document.getElementById(CUBISM_CORE_SCRIPT_ID) as HTMLScriptElement | null;
    const script = existing ?? document.createElement('script');
    const onReady = () => resolve();
    const onError = () => {
      cubismCorePromise = undefined;
      script.remove();
      reject(new Error('Cubism Core failed to load'));
    };
    script.addEventListener('load', onReady, { once: true });
    script.addEventListener('error', onError, { once: true });
    if (!existing) {
      script.id = CUBISM_CORE_SCRIPT_ID;
      script.src = CUBISM_CORE_URL;
      script.async = true;
      document.head.appendChild(script);
    }
  });
  return cubismCorePromise;
}

export function createLive2dPilotMascotRuntime(
  dependencies: Live2dRuntimeDependencies,
): PilotMascotRuntime {
  return {
  async mount(canvas, signal, animationLevel = 'full') {
    const { Application, Ticker, Live2DModel } = await dependencies.loadModules();
    if (signal?.aborted) throw new DOMException('Mascot mount aborted', 'AbortError');
    Live2DModel.registerTicker(Ticker);
    const host = canvas.parentElement;
    if (!host) throw new Error('Pilot mascot host is unavailable');

    const reduceMotion = dependencies.prefersReducedMotion();
    const staticRender = reduceMotion || animationLevel === 'off';
    let application: Live2dApplication | undefined;
    let model: Live2dModelInstance | undefined;
    let observer: Pick<ResizeObserver, 'observe' | 'disconnect'> | undefined;
    let disposed = false;
    let zoom = 1;
    let activity: PilotMascotActivity = 'idle';
    let thinkingTimer: number | undefined;
    let fit: (() => void) | undefined;
    const stopThinkingLoop = () => {
      if (thinkingTimer !== undefined) window.clearTimeout(thinkingTimer);
      thinkingTimer = undefined;
    };
    const dispose = () => {
      if (disposed) return;
      disposed = true;
      stopThinkingLoop();
      observer?.disconnect();
      model?.destroy({ children: true });
      application?.destroy(false, { children: true, texture: false, baseTexture: false });
      observer = undefined;
      model = undefined;
      application = undefined;
      fit = undefined;
    };

    try {
      application = new Application({
        view: canvas,
        autoStart: !staticRender,
        backgroundAlpha: 0,
        antialias: true,
        resolution: Math.min(window.devicePixelRatio || 1, 2),
        autoDensity: true,
        resizeTo: host,
      });
      model = await Live2DModel.from(MODEL_URL, {
        autoInteract: false,
        autoUpdate: !staticRender,
      });
      if (signal?.aborted) throw new DOMException('Mascot mount aborted', 'AbortError');
      application.stage.addChild(model);
      model.anchor.set(0.5, 0.5);
      const naturalWidth = model.width;
      const naturalHeight = model.height;

      fit = () => {
        const width = Math.max(host.clientWidth, 1);
        const height = Math.max(host.clientHeight, 1);
        const baseScale = Math.min((width * 0.94) / naturalWidth, (height * 0.96) / naturalHeight);
        const scale = baseScale * zoom;
        model!.scale.set(scale);
        model!.x = width * 0.5;
        model!.y = height * 0.5;
        if (staticRender) application!.render();
      };
      fit();
      observer = dependencies.createResizeObserver(fit);
      observer.observe(host);

      const runMotion = (group: string, index: number) => {
        if (disposed || staticRender || !model?.motion) return Promise.resolve(false);
        try {
          return Promise.resolve(model.motion(group, index, 3)).catch(() => false);
        } catch {
          return Promise.resolve(false);
        }
      };
      const setExpression = (name: string) => {
        if (disposed || staticRender || !model?.expression) return;
        try {
          void Promise.resolve(model.expression(name)).catch(() => false);
        } catch {
          // Motion feedback is decorative; text remains the source of truth.
        }
      };
      const isThinkingLoop = () => activity === 'thinking' || activity === 'preparing_voice' || activity === 'transcribing';
      const playThinking = () => {
        if (disposed || !isThinkingLoop() || staticRender || animationLevel !== 'full') return;
        void runMotion('Idle', 1).finally(() => {
          if (disposed || !isThinkingLoop()) return;
          thinkingTimer = window.setTimeout(playThinking, 160);
        });
      };

      return {
        setActivity(nextActivity) {
          if (disposed || nextActivity === activity) return;
          activity = nextActivity;
          stopThinkingLoop();
          if (staticRender) return;
          if (activity === 'thinking' || activity === 'preparing_voice' || activity === 'transcribing') {
            playThinking();
            return;
          }
          if (activity === 'speaking') {
            setExpression('f06');
            void runMotion('Tap', 0);
            return;
          }
          if (activity === 'listening' || activity === 'waiting_for_speech') {
            setExpression('f01');
            void runMotion('Idle', 0);
            return;
          }
          if (activity === 'speech_paused' || activity === 'waiting_confirmation') {
            setExpression('f03');
            void runMotion('Tap', 1);
            return;
          }
          if (activity === 'success') {
            setExpression('f06');
            void runMotion('Tap', 0);
            return;
          }
          if (activity === 'error') {
            setExpression('f02');
            void runMotion('Tap', 1);
          }
        },
        setZoom(nextZoom) {
          if (disposed) return;
          zoom = normalizePilotMascotZoom(nextZoom);
          fit?.();
        },
        dispose,
      };
    } catch (error) {
      dispose();
      throw error;
    }
  },
  };
}

const unsharedLive2dPilotMascotRuntime = createLive2dPilotMascotRuntime({
  async loadModules() {
    await ensureCubismCore();
    const [{ Application, Ticker }, { Live2DModel }] = await Promise.all([
      import('pixi.js'),
      import('pixi-live2d-display/cubism4'),
    ]);
    return {
      Application: Application as unknown as Live2dRuntimeModules['Application'],
      Ticker,
      Live2DModel: Live2DModel as unknown as Live2dRuntimeModules['Live2DModel'],
    };
  },
  prefersReducedMotion: () => window.matchMedia('(prefers-reduced-motion: reduce)').matches,
  createResizeObserver: (callback) => new ResizeObserver(callback),
});

export const live2dPilotMascotRuntime = serializePilotMascotRuntime(unsharedLive2dPilotMascotRuntime);
