// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  createLive2dPilotMascotRuntime,
  serializePilotMascotRuntime,
  type Live2dRuntimeDependencies,
  type PilotMascotRuntime,
  type PilotMascotRuntimeController,
} from './live2dRuntime';

function createController(dispose: () => void = vi.fn()): PilotMascotRuntimeController {
  return {
    setActivity: vi.fn(),
    setZoom: vi.fn(),
    dispose,
  };
}

describe('serializePilotMascotRuntime', () => {
  it('never initializes two runtimes concurrently on the same canvas', async () => {
    const canvas = document.createElement('canvas');
    const firstController = new AbortController();
    const secondController = new AbortController();
    let resolveFirst!: (controller: PilotMascotRuntimeController) => void;
    let active = 0;
    let maximumActive = 0;
    const firstDispose = vi.fn(() => {
      active -= 1;
    });
    const secondDispose = vi.fn(() => {
      active -= 1;
    });
    const underlying: PilotMascotRuntime = {
      mount: vi.fn()
        .mockImplementationOnce(() => {
          active += 1;
          maximumActive = Math.max(maximumActive, active);
          return new Promise<PilotMascotRuntimeController>((resolve) => {
            resolveFirst = resolve;
          });
        })
        .mockImplementationOnce(async () => {
          active += 1;
          maximumActive = Math.max(maximumActive, active);
          return createController(secondDispose);
        }),
    };
    const runtime = serializePilotMascotRuntime(underlying);

    const firstMount = runtime.mount(canvas, firstController.signal);
    const firstRejected = firstMount.catch((error: unknown) => error);
    await Promise.resolve();
    await Promise.resolve();
    expect(underlying.mount).toHaveBeenCalledTimes(1);
    firstController.abort();
    const secondMount = runtime.mount(canvas, secondController.signal);
    await Promise.resolve();
    expect(underlying.mount).toHaveBeenCalledTimes(1);

    resolveFirst(createController(firstDispose));
    await expect(firstRejected).resolves.toMatchObject({ name: 'AbortError' });
    const second = await secondMount;

    expect(underlying.mount).toHaveBeenCalledTimes(2);
    expect(maximumActive).toBe(1);
    expect(firstDispose).toHaveBeenCalledTimes(1);
    second.dispose();
    expect(secondDispose).toHaveBeenCalledTimes(1);
  });

  it('does not start an aborted mount after a queued runtime releases', async () => {
    const canvas = document.createElement('canvas');
    let releaseFirst!: () => void;
    const underlying: PilotMascotRuntime = {
      mount: vi.fn()
        .mockResolvedValueOnce(createController(() => releaseFirst?.()))
        .mockResolvedValueOnce(createController()),
    };
    const runtime = serializePilotMascotRuntime(underlying);
    const firstController = await runtime.mount(canvas);
    const controller = new AbortController();
    const queued = runtime.mount(canvas, controller.signal);
    controller.abort();
    firstController.dispose();

    await expect(queued).rejects.toMatchObject({ name: 'AbortError' });
    expect(underlying.mount).toHaveBeenCalledTimes(1);
  });
});

function runtimeDependencies(overrides: Partial<Live2dRuntimeDependencies> = {}) {
  const application = {
    stage: { addChild: vi.fn() },
    render: vi.fn(),
    destroy: vi.fn(),
  };
  const model = {
    width: 500,
    height: 800,
    x: 0,
    y: 0,
    anchor: { set: vi.fn() },
    scale: { set: vi.fn() },
    motion: vi.fn().mockResolvedValue(true),
    expression: vi.fn().mockResolvedValue(true),
    destroy: vi.fn(),
  };
  const Application = vi.fn(function Application() {
    return application;
  });
  const Live2DModel = {
    registerTicker: vi.fn(),
    from: vi.fn().mockResolvedValue(model),
  };
  const observer = { observe: vi.fn(), disconnect: vi.fn() };
  const dependencies: Live2dRuntimeDependencies = {
    loadModules: vi.fn().mockResolvedValue({ Application, Ticker: {}, Live2DModel }),
    prefersReducedMotion: () => false,
    createResizeObserver: vi.fn(() => observer),
    ...overrides,
  };
  return { dependencies, Application, Live2DModel, application, model, observer };
}

describe('createLive2dPilotMascotRuntime', () => {
  it('destroys a partially initialized Pixi application when model loading fails', async () => {
    const fixture = runtimeDependencies();
    fixture.Live2DModel.from.mockRejectedValueOnce(new Error('model failed'));
    const host = document.createElement('div');
    const canvas = document.createElement('canvas');
    host.appendChild(canvas);

    await expect(createLive2dPilotMascotRuntime(fixture.dependencies).mount(canvas)).rejects.toThrow('model failed');
    expect(fixture.application.destroy).toHaveBeenCalledTimes(1);
    expect(fixture.model.destroy).not.toHaveBeenCalled();
  });

  it('cleans up model and application when ResizeObserver setup fails', async () => {
    const fixture = runtimeDependencies({
      createResizeObserver: () => { throw new Error('observer failed'); },
    });
    const host = document.createElement('div');
    const canvas = document.createElement('canvas');
    host.appendChild(canvas);

    await expect(createLive2dPilotMascotRuntime(fixture.dependencies).mount(canvas)).rejects.toThrow('observer failed');
    expect(fixture.model.destroy).toHaveBeenCalledTimes(1);
    expect(fixture.application.destroy).toHaveBeenCalledTimes(1);
  });

  it('renders one static frame without auto updates when reduced motion is requested', async () => {
    const fixture = runtimeDependencies({ prefersReducedMotion: () => true });
    const host = document.createElement('div');
    Object.defineProperties(host, {
      clientWidth: { configurable: true, value: 240 },
      clientHeight: { configurable: true, value: 360 },
    });
    const canvas = document.createElement('canvas');
    host.appendChild(canvas);

    const controller = await createLive2dPilotMascotRuntime(fixture.dependencies).mount(canvas);
    expect(fixture.Application).toHaveBeenCalledWith(expect.objectContaining({ autoStart: false }));
    expect(fixture.Live2DModel.from).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({ autoUpdate: false }));
    expect(fixture.application.render).toHaveBeenCalledTimes(1);
    controller.dispose();
  });

  it('treats off animation as a static runtime even without a system override', async () => {
    const fixture = runtimeDependencies();
    const host = document.createElement('div');
    Object.defineProperties(host, {
      clientWidth: { configurable: true, value: 240 },
      clientHeight: { configurable: true, value: 360 },
    });
    const canvas = document.createElement('canvas');
    host.appendChild(canvas);

    const controller = await createLive2dPilotMascotRuntime(fixture.dependencies).mount(canvas, undefined, 'off');
    expect(fixture.Application).toHaveBeenCalledWith(expect.objectContaining({ autoStart: false }));
    expect(fixture.Live2DModel.from).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({ autoUpdate: false }));
    controller.setActivity('success');
    expect(fixture.model.motion).not.toHaveBeenCalled();
    expect(fixture.model.expression).not.toHaveBeenCalled();
    controller.dispose();
  });

  it('keeps minimal animation free of the thinking loop while retaining one-shot feedback', async () => {
    const fixture = runtimeDependencies();
    const host = document.createElement('div');
    Object.defineProperties(host, {
      clientWidth: { configurable: true, value: 240 },
      clientHeight: { configurable: true, value: 360 },
    });
    const canvas = document.createElement('canvas');
    host.appendChild(canvas);

    const controller = await createLive2dPilotMascotRuntime(fixture.dependencies).mount(canvas, undefined, 'minimal');
    controller.setActivity('thinking');
    await Promise.resolve();
    expect(fixture.model.motion).not.toHaveBeenCalled();
    controller.setActivity('success');
    await Promise.resolve();
    expect(fixture.model.motion).toHaveBeenCalledWith('Tap', 0, expect.any(Number));
    controller.dispose();
  });

  it('applies zoom to the stable auto-fit scale and keeps it after resize', async () => {
    const fixture = runtimeDependencies();
    const host = document.createElement('div');
    let width = 240;
    let height = 360;
    Object.defineProperties(host, {
      clientWidth: { configurable: true, get: () => width },
      clientHeight: { configurable: true, get: () => height },
    });
    const canvas = document.createElement('canvas');
    host.appendChild(canvas);

    const controller = await createLive2dPilotMascotRuntime(fixture.dependencies).mount(canvas);
    controller.setZoom(1.2);
    expect(fixture.model.scale.set.mock.calls[fixture.model.scale.set.mock.calls.length - 1]?.[0]).toBeCloseTo(0.5184);

    width = 300;
    height = 420;
    const resizeCallback = (fixture.dependencies.createResizeObserver as ReturnType<typeof vi.fn>).mock.calls[0]?.[0];
    resizeCallback?.();
    expect(fixture.model.scale.set.mock.calls[fixture.model.scale.set.mock.calls.length - 1]?.[0]).toBeCloseTo(0.6048);
    controller.dispose();
  });

  it('plays a distinct thinking loop and one-shot success feedback', async () => {
    const fixture = runtimeDependencies();
    const host = document.createElement('div');
    Object.defineProperties(host, {
      clientWidth: { configurable: true, value: 240 },
      clientHeight: { configurable: true, value: 360 },
    });
    const canvas = document.createElement('canvas');
    host.appendChild(canvas);

    const controller = await createLive2dPilotMascotRuntime(fixture.dependencies).mount(canvas);
    controller.setActivity('thinking');
    await Promise.resolve();
    expect(fixture.model.motion).toHaveBeenCalledWith('Idle', 1, expect.any(Number));

    controller.setActivity('success');
    await Promise.resolve();
    expect(fixture.model.motion).toHaveBeenCalledWith('Tap', 0, expect.any(Number));
    expect(fixture.model.expression).toHaveBeenCalledWith('f06');
    controller.dispose();
  });

  it('maps speaking, listening and transcribing to distinct mascot feedback', async () => {
    const fixture = runtimeDependencies();
    const host = document.createElement('div');
    Object.defineProperties(host, {
      clientWidth: { configurable: true, value: 240 },
      clientHeight: { configurable: true, value: 360 },
    });
    const canvas = document.createElement('canvas');
    host.appendChild(canvas);

    const controller = await createLive2dPilotMascotRuntime(fixture.dependencies).mount(canvas);
    controller.setActivity('speaking');
    await Promise.resolve();
    expect(fixture.model.expression).toHaveBeenCalledWith('f06');
    expect(fixture.model.motion).toHaveBeenCalledWith('Tap', 0, expect.any(Number));

    controller.setActivity('listening');
    await Promise.resolve();
    expect(fixture.model.expression).toHaveBeenCalledWith('f01');
    expect(fixture.model.motion).toHaveBeenCalledWith('Idle', 0, expect.any(Number));

    controller.setActivity('waiting_for_speech');
    await Promise.resolve();
    expect(fixture.model.motion).toHaveBeenCalledWith('Idle', 0, expect.any(Number));

    controller.setActivity('speech_paused');
    await Promise.resolve();
    expect(fixture.model.expression).toHaveBeenCalledWith('f03');
    expect(fixture.model.motion).toHaveBeenCalledWith('Tap', 1, expect.any(Number));

    controller.setActivity('transcribing');
    await Promise.resolve();
    expect(fixture.model.motion).toHaveBeenCalledWith('Idle', 1, expect.any(Number));

    controller.setActivity('preparing_voice');
    await Promise.resolve();
    expect(fixture.model.motion).toHaveBeenCalledWith('Idle', 1, expect.any(Number));
    controller.dispose();
  });

  it('keeps activity changes static under reduced motion and ignores calls after dispose', async () => {
    const fixture = runtimeDependencies({ prefersReducedMotion: () => true });
    const host = document.createElement('div');
    Object.defineProperties(host, {
      clientWidth: { configurable: true, value: 240 },
      clientHeight: { configurable: true, value: 360 },
    });
    const canvas = document.createElement('canvas');
    host.appendChild(canvas);

    const controller = await createLive2dPilotMascotRuntime(fixture.dependencies).mount(canvas);
    controller.setActivity('thinking');
    controller.dispose();
    controller.setActivity('success');
    controller.setZoom(1.3);

    expect(fixture.model.motion).not.toHaveBeenCalled();
    expect(fixture.model.expression).not.toHaveBeenCalled();
    expect(fixture.model.destroy).toHaveBeenCalledTimes(1);
  });
});
