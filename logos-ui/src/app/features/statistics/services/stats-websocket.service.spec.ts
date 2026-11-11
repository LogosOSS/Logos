import { TestBed } from '@angular/core/testing';
import { vi } from 'vitest';
import { AuthService } from '../../../core/auth/services/auth.service';
import { StatsWebsocketService, StatsWsConnectOptions } from './stats-websocket.service';

class Socket {
  static readonly OPEN = 1;
  static instances: Socket[] = [];
  readonly readyState = 1;
  readonly send = vi.fn();
  readonly close = vi.fn();
  onopen: (() => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onmessage: unknown;
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
