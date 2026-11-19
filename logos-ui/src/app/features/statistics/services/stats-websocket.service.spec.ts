import { TestBed } from '@angular/core/testing';
import { vi } from 'vitest';
import { AuthService } from '../../../core/auth/services/auth.service';
import { StatsWebsocketService, StatsWsConnectOptions } from './stats-websocket.service';
import { StatsClockService } from './stats-clock.service';

class Socket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static instances: Socket[] = [];
  readyState = 1;
  readonly send = vi.fn();
  readonly close = vi.fn();
  onopen: (() => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: unknown;
  constructor(readonly url: string) {
    Socket.instances.push(this);
  }
}

describe('statistics websocket feed filters', () => {
  let service: StatsWebsocketService;
  beforeEach(() => {
    vi.useFakeTimers();
    Socket.instances = [];
    vi.stubGlobal('WebSocket', Socket);
    TestBed.configureTestingModule({
      providers: [
        { provide: AuthService, useValue: { freshToken: vi.fn().mockResolvedValue('test-token') } },
      ],
    });
    service = TestBed.inject(StatsWebsocketService);
  });
  afterEach(() => {
    service.disconnect();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  const options = (): StatsWsConnectOptions => ({
    vramDayOffset: -1,
    timeline: { start: '2026-11-01T00:00:00Z', end: '2026-11-13T12:00:00Z', targetBuckets: 60, live: true },
    scope: { userId: 3, teamId: 7, providerId: 9, apiKeyId: null, errorsOnly: false },
    feedStatus: 'running', feedModelIds: [12], feedProviderIds: [9], interest: 'requests',
    handlers: { onVramInit: vi.fn(), onVramDelta: vi.fn(), onTimelineInit: vi.fn(),
      onStats: vi.fn(), onRequestsData: vi.fn() },
  });

  async function open(opts = options()): Promise<Socket> {
    service.connect(opts);
    await Promise.resolve();
    const socket = Socket.instances.at(-1)!;
    socket.onopen!();
    return socket;
  }

  it('detects a silent OPEN socket without any browser wake event and restores the full live subscription', async () => {
    const opts = options();
    const socket = await open(opts);
    // A long suspension does not change readyState and may emit no visibility event.
    vi.setSystemTime(new Date('2026-11-13T14:00:00Z'));
    await vi.advanceTimersByTimeAsync(25_000);
    expect(JSON.parse(socket.send.mock.calls.at(-1)![0])).toEqual({ action: 'ping' });
    await vi.advanceTimersByTimeAsync(3000);
    expect(socket.close).toHaveBeenCalledOnce();
    const replacement = Socket.instances.at(-1)!;
    replacement.onopen!();
    expect(JSON.parse(replacement.send.mock.calls[0][0])).toMatchObject({
      action: 'init', timeline: { ...{ start: opts.timeline.start, end: opts.timeline.end }, live: true },
      user_id: 3, team_id: 7, provider_id: 9, status: 'running', model_ids: [12], provider_ids: [9], interest: 'requests',
    });
    replacement.onmessage!({ data: JSON.stringify({ type: 'requests', payload: { requests: [{ request_id: 'after-wake' }] } }) } as MessageEvent);
    expect(opts.handlers.onRequestsData).toHaveBeenCalledWith({ requests: [{ request_id: 'after-wake' }] });
  });

  it('accepts pong and samples server time without reconnecting a healthy socket', async () => {
    const socket = await open();
    await vi.advanceTimersByTimeAsync(25_000);
    socket.onmessage!({ data: JSON.stringify({ type: 'pong', server_now: '2026-11-13T12:00:25Z' }) } as MessageEvent);
    expect(TestBed.inject(StatsClockService).now()).toBe(Date.parse('2026-11-13T12:00:25Z'));
    await vi.advanceTimersByTimeAsync(3000);
    expect(Socket.instances).toHaveLength(1);
  });

  it('probes on online and retries a hung CONNECTING socket', async () => {
    const socket = await open();
    window.dispatchEvent(new Event('online'));
    await vi.advanceTimersByTimeAsync(3000);
    const connecting = Socket.instances.at(-1)!;
    connecting.readyState = Socket.CONNECTING;
    window.dispatchEvent(new Event('online'));
    await vi.advanceTimersByTimeAsync(10_000);
    expect(connecting.close).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(2000);
    expect(Socket.instances).toHaveLength(3);
    expect(socket.close).toHaveBeenCalledOnce();
  });

  it('preserves an explicitly historical range across reconnect', async () => {
    const opts = options();
    opts.timeline.live = false;
    await open(opts);
    service.reconnect();
    await Promise.resolve();
    const socket = Socket.instances.at(-1)!;
    socket.onopen!();
    expect(JSON.parse(socket.send.mock.calls[0][0]).timeline).toMatchObject({ end: opts.timeline.end, live: false });
  });

  it('ignores stale auth completion when a reconnect supersedes it and coalesces wake events', async () => {
    let resolveOld!: (token: string) => void;
    const freshToken = vi.mocked(TestBed.inject(AuthService).freshToken);
    freshToken.mockReturnValueOnce(new Promise(resolve => resolveOld = resolve));
    service.connect(options());
    window.dispatchEvent(new Event('online'));
    window.dispatchEvent(new Event('online'));
    expect(freshToken).toHaveBeenCalledOnce();
    service.reconnect();
    await Promise.resolve();
    resolveOld('stale-token');
    await Promise.resolve();
    expect(Socket.instances).toHaveLength(1);
    expect(Socket.instances[0].url).toContain('key=test-token');
  });

  it('does not keep a watchdog or accept callbacks after disconnect', async () => {
    const opts = options();
    const socket = await open(opts);
    const message = socket.onmessage!;
    await vi.advanceTimersByTimeAsync(25_000);
    service.disconnect();
    message({ data: JSON.stringify({ type: 'requests', payload: { requests: [{}] } }) } as MessageEvent);
    await vi.advanceTimersByTimeAsync(60_000);
    window.dispatchEvent(new Event('online'));
    expect(Socket.instances).toHaveLength(1);
    expect(opts.handlers.onRequestsData).not.toHaveBeenCalled();
  });

  it('expires an auth attempt and ignores its late result after a later attempt succeeds', async () => {
    let resolveOld!: (token: string) => void;
    vi.mocked(TestBed.inject(AuthService).freshToken).mockReturnValueOnce(new Promise(resolve => resolveOld = resolve));
    service.connect(options());
    await vi.advanceTimersByTimeAsync(12_000);
    expect(Socket.instances).toHaveLength(1);
    resolveOld('expired-attempt');
    await Promise.resolve();
    expect(Socket.instances).toHaveLength(1);
    expect(Socket.instances[0].url).toContain('test-token');
  });

  it('keeps the auth deadline when a previous CLOSING socket delivers a late close event', async () => {
    const old = await open();
    old.readyState = 2;
    const lateClose = old.onclose!;
    vi.mocked(TestBed.inject(AuthService).freshToken).mockReturnValueOnce(new Promise(() => {}));
    window.dispatchEvent(new Event('online'));
    lateClose({ code: 1006 } as CloseEvent);
    await vi.advanceTimersByTimeAsync(12_000);
    expect(Socket.instances).toHaveLength(2);
    expect(Socket.instances[1].url).toContain('test-token');
  });

  it('sends selections and restores the latest filter after reconnect', async () => {
    const opts: StatsWsConnectOptions = {
      vramDayOffset: -1,
      timeline: { start: '2026-09-01T00:00:00Z', end: '2026-10-01T00:00:00Z', targetBuckets: 60 },
      handlers: {
        onVramInit: vi.fn(),
        onVramDelta: vi.fn(),
        onTimelineInit: vi.fn(),
        onStats: vi.fn(),
        onRequestsData: vi.fn(),
      },
    };
    service.connect(opts);
    await Promise.resolve();
    const socket = Socket.instances[0];
    socket.onopen!();
    service.setFeedFilters('running', [1, 2], [3, 4]);
    expect(JSON.parse(socket.send.mock.calls.at(-1)![0])).toEqual({
      action: 'set_feed_filters',
      status: 'running',
      model_ids: [1, 2],
      provider_ids: [3, 4],
    });
    service.setScope({ userId: null, teamId: 7, providerId: null, apiKeyId: 41, errorsOnly: false });
    expect(JSON.parse(socket.send.mock.calls.at(-1)![0])).toMatchObject({
      action: 'set_scope',
      team_id: 7,
      api_key_id: 41,
    });
    socket.onclose!({ code: 1006 } as CloseEvent);
    service.setFeedFilters('finished', [2], [4]);
    await vi.advanceTimersByTimeAsync(2000);
    const reconnected = Socket.instances[1];
    reconnected.onopen!();
    expect(JSON.parse(reconnected.send.mock.calls[0][0])).toMatchObject({
      action: 'init',
      // The page scope survives the reconnect, the API key included.
      team_id: 7,
      api_key_id: 41,
      status: 'finished',
      model_ids: [2],
      provider_ids: [4],
    });
  });

  it('forces a fresh token after expiry and retries refresh failures without reusing the old token', async () => {
    const freshToken = vi.mocked(TestBed.inject(AuthService).freshToken);
    freshToken.mockResolvedValueOnce('old-token').mockResolvedValueOnce('').mockResolvedValueOnce('fresh-token');
    service.connect({
      vramDayOffset: -1,
      timeline: { start: '2026-09-01T00:00:00Z', end: '2026-10-01T00:00:00Z', targetBuckets: 60 },
      handlers: {
        onVramInit: vi.fn(), onVramDelta: vi.fn(), onTimelineInit: vi.fn(),
        onStats: vi.fn(), onRequestsData: vi.fn(),
      },
    });
    await Promise.resolve();
    const socket = Socket.instances[0];
    expect(socket.url).toContain('key=old-token');
    socket.onclose!({ code: 4001 } as CloseEvent);
    await vi.advanceTimersByTimeAsync(2000);
    expect(freshToken).toHaveBeenLastCalledWith(true);
    expect(Socket.instances).toHaveLength(1);

    await vi.advanceTimersByTimeAsync(3000);
    expect(freshToken).toHaveBeenLastCalledWith(true);
    expect(Socket.instances).toHaveLength(2);
    expect(Socket.instances[1].url).toContain('key=fresh-token');
  });

  it('does not open a socket if disconnected during token refresh', async () => {
    let resolveToken!: (token: string) => void;
    vi.mocked(TestBed.inject(AuthService).freshToken).mockReturnValue(new Promise(resolve => resolveToken = resolve));
    service.connect({
      vramDayOffset: -1,
      timeline: { start: '', end: '', targetBuckets: 60 },
      handlers: {
        onVramInit: vi.fn(), onVramDelta: vi.fn(), onTimelineInit: vi.fn(),
        onStats: vi.fn(), onRequestsData: vi.fn(),
      },
    });
    service.disconnect();
    resolveToken('fresh-token');
    await Promise.resolve();
    expect(Socket.instances).toHaveLength(0);
  });

});
