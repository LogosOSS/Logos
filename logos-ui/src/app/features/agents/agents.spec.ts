import { vi } from 'vitest';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { TeamManagementService } from '../../core/services/team-management.service';
import { AgentService } from '../../core/services/agent.service';
import {
  ACTIVE_SESSION_STATUSES,
  AgentCapacity,
  AgentControls,
  AgentInstructions,
  AgentModels,
  AgentRepository,
  AgentSession,
  AgentTriggers,
  AgentWorkspace,
  AgentWorkspaceStorage,
  CreateSessionRequest,
  SessionMessages,
  isActive,
} from '../../shared/models/agent.model';
import { Agents, scaleSlotCounts } from './agents';

/**
 * The agents page groups the list into "active" and "finished", and it only
 * keeps polling the runner while something in that list can still change.
 * Both read off the same status set as the application service, so a status the runner
 * treats as active but the page does not would sit under Finished — stale,
 * and no longer refreshed — until a manual reload.
 */

const makeSession = (overrides: Partial<AgentSession> = {}): AgentSession => ({
  id: 1,
  workspace_id: 1,
  workspace_name: 'feature-work',
  task: 'a task description for the agent',
  status: 'finalizing',
  model: null,
  branch_name: 'agent/feature-work/session-1',
  pr_url: null,
  created_by: 'tester',
  created_at: '2026-09-02T10:00:00Z',
  started_at: '2026-09-02T10:01:00Z',
  finished_at: null,
  exit_code: null,
  error: null,
  tokens_in: 0,
  tokens_out: 0,
  cost_usd: 0,
  screenshot_count: 0,
  trigger_kind: null,
  trigger_ref: null,
  priority: 50,
  priority_reason: null,
  environment_notes: null,
  ...overrides,
});

const CAPACITY: AgentCapacity = {
  load: 0.25,
  total_slots: 4,
  busy_slots: 1,
  own_slots: 1,
  other_slots: 0,
  sessions_running: 1,
  sessions_queued: 0,
  sessions_paused: 0,
  max_parallel: 2,
  may_start: true,
  reason: 'ok',
  models_local_only: true,
  models_detail: 'one local model',
};

class FakeAgentService {
  sessions: AgentSession[] = [];
  sessionCalls = 0;
  capacityCalls = 0;
  instructionBodies: Record<string, unknown>[] = [];
  repositories: AgentRepository[] = [];
  repositoriesFail = false;
  workspaces: AgentWorkspace[] = [];
  storage: AgentWorkspaceStorage = { artifact_filesystem: null, workspace_used_bytes: {} };
  createBodies: CreateSessionRequest[] = [];

  async getRepositories(): Promise<AgentRepository[]> {
    if (this.repositoriesFail) throw new Error('not found');
    return this.repositories;
  }

  async createSession(body: CreateSessionRequest): Promise<AgentSession> {
    this.createBodies.push(body);
    return makeSession({ id: 99, status: 'queued', task: body.task });
  }

  async getEvents(): Promise<[]> {
    return [];
  }

  async getMessages(_id: number): Promise<SessionMessages> {
    return { messages: [], omitted: 0 };
  }

  async *streamEvents(): AsyncGenerator<never> {
    // No events: the tests read the request, not the transcript.
  }

  async getInstructions(): Promise<AgentInstructions> {
    return {
      house_rules: 'the shipped rules',
      environment_notes: 'the shipped notes',
      house_rules_default: true,
      environment_notes_default: true,
      updated_by: '',
    };
  }

  async setInstructions(body: Record<string, unknown>): Promise<AgentInstructions> {
    this.instructionBodies.push(body);
    return {
      house_rules: 'the shipped rules',
      environment_notes: 'the shipped notes',
      house_rules_default: true,
      environment_notes_default: true,
      updated_by: 'tester',
    };
  }

  async getSessions(): Promise<AgentSession[]> {
    this.sessionCalls += 1;
    return this.sessions;
  }

  async getWorkspaces(): Promise<AgentWorkspace[]> {
    return this.workspaces;
  }

  async getWorkspaceStorage(): Promise<AgentWorkspaceStorage> {
    return this.storage;
  }

  async getCapacity(): Promise<AgentCapacity> {
    this.capacityCalls += 1;
    return CAPACITY;
  }

  async getModels(): Promise<AgentModels> {
    return {
      models: ['local-model'],
      default: 'local-model',
      local_only: true,
      detail: 'one local model',
    };
  }

  async getControls(): Promise<AgentControls> {
    return {
      mode: 'running',
      mode_reason: '',
      paused: false,
      admits_new_sessions: true,
      max_parallel: 2,
      max_parallel_override: null,
      max_parallel_configured: 10,
      max_parallel_available: 2,
      session_memory_mb: 4096,
      session_memory_override: null,
      session_memory_configured: 4096,
      memory_budget_mb: 8192,
      host_memory_mb: 16384,
      memory_reserved_mb: 0,
      updated_by: '',
    };
  }

  controlBodies: unknown[] = [];

  async setControls(body: unknown): Promise<AgentControls> {
    this.controlBodies.push(body);
    return this.getControls();
  }

  async getTriggers(): Promise<AgentTriggers> {
    return {
      enabled: false,
      polling: false,
      account: 'LogosOSSAgent',
      poll_interval_s: 120,
      max_active_sessions: 5,
      active_sessions: 0,
      last_pass: null,
      queued_total: 0,
      last_error: '',
    };
  }
}

describe('Agents', () => {
  let fixture: ComponentFixture<Agents>;
  let component: Agents;
  let agentService: FakeAgentService;
  const teamService = { analyzeAllRepositories: vi.fn() };

  beforeEach(async () => {
    agentService = new FakeAgentService();
    await TestBed.configureTestingModule({
      imports: [Agents],
      providers: [
        provideRouter([]),
        { provide: AgentService, useValue: agentService },
        { provide: TeamManagementService, useValue: teamService },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(Agents);
    component = fixture.componentInstance;
    // Settle the load ngOnInit kicked off, so each test starts from the
    // state the runner actually reported.
    await component.refresh();
  });

  afterEach(() => {
    // ngOnInit opened a polling interval; dropping it here keeps the event
    // loop empty for the next test.
    fixture.destroy();
    vi.useRealTimers();
  });

  it('renders typed messages with safe text, lists, code, and a separate diagnostic log', async () => {
    const session = makeSession({ status: 'running' });
    agentService.sessions = [session];
    const source = { id: 'message-a', actor: 'main', truncated: false, text: 'Summary\n\n- One\n- Two\n\n```html\n<img src=x>\n```' };
    const read = vi.spyOn(agentService, 'getMessages').mockResolvedValue({ messages: [source], omitted: 0 });
    await component.refresh();
    await component.select(session);
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;
    expect(root.querySelector('.agent-message p')?.textContent).toBe('Summary');
    expect(root.querySelectorAll('.agent-message li').length).toBe(2);
    expect(root.querySelector('.agent-message code')?.textContent).toBe('<img src=x>');
    expect(root.querySelector('.agent-message img')).toBeNull();
    expect(root.querySelector('details.diagnostics')?.hasAttribute('open')).toBe(false);
    read.mockResolvedValue({ messages: [source, { ...source, id: 'message-b', actor: 'child-12345678', text: '2. Verify' }], omitted: 0 });
    await component.refresh();
    fixture.detectChanges();
    expect(root.querySelectorAll('.agent-message').length).toBe(2);
    expect(root.querySelectorAll('.agent-message header')[1].textContent).toContain('Subagent');
    expect(root.querySelector('.agent-message ol')?.getAttribute('start')).toBe('2');
  });

  it('keeps long messages compact and expandable without discarding their body', async () => {
    const session = makeSession({ status: 'running' });
    agentService.sessions = [session];
    vi.spyOn(agentService, 'getMessages').mockResolvedValue({ messages: [{
      id: 'long', actor: 'main', truncated: false, text: 'Proving the estimate. '.repeat(30) + '\n\nFinal detail.',
    }], omitted: 0 });
    await component.refresh();
    await component.select(session);
    fixture.detectChanges();
    const details = fixture.nativeElement.querySelector('.agent-message details') as HTMLDetailsElement;
    expect(details.open).toBe(false);
    expect(details.querySelector('summary')?.textContent).toContain('Proving the estimate');
    details.open = true;
    expect(details.querySelectorAll('p')[1].textContent).toBe('Final detail.');
  });

  it('queues one manual RAM override and rejects invalid values', async () => {
    await component.refresh();
    component.openForm();
    component.formWorkspaceId.set(1);
    component.formTask.set('Fix the flaky test');
    component.formRam.set('9000');
    expect(component.canSubmit()).toBe(false);
    component.formRam.set('6144.5');
    expect(component.canSubmit()).toBe(false);
    component.formRam.set('6144');
    expect(component.canSubmit()).toBe(true);
    await component.submit();
    expect(agentService.createBodies[0].memory_override_mb).toBe(6144);
    expect(component.formRam()).toBe('');
  });

  it('keeps legacy output in diagnostics and discards message reads from a previous selection', async () => {
    const [first, second] = [makeSession({ id: 1 }), makeSession({ id: 2 })];
    agentService.sessions = [first, second];
    await component.refresh();
    let finish!: (data: SessionMessages) => void;
    vi.spyOn(agentService, 'getMessages').mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const pending = component.select(first);
    await component.select(second);
    finish({ messages: [{ id: 'old', actor: 'main', text: 'Previous', truncated: false }], omitted: 0 });
    await pending;
    component.events.set([{ id: 1, session_id: 2, ts: '', kind: 'log', payload: { lines: ['legacy stdout'] } }]);
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.agent-message')).toBeNull();
    expect(fixture.nativeElement.querySelector('.diagnostics pre').textContent).toContain('legacy stdout');
  });

  it('reports free artifact space and distinguishes measured zero from missing workspace sizes', async () => {
    agentService.workspaces = [1, 2, 3].map((id) => ({
      id,
      name: `fixture-${id}`,
      volume_name: `volume-${id}`,
      base_branch: 'main',
      created_by: 'tester',
      created_at: '2026-09-02T10:00:00Z',
      active_sessions: 0,
    }));
    const gib = 1024 ** 3;
    agentService.storage = {
      artifact_filesystem: { total_bytes: 250 * gib, available_bytes: 10 * gib },
      workspace_used_bytes: { 1: 2.5 * gib, 2: 0 },
    };
    await component.refresh();
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();

    const root = fixture.nativeElement as HTMLElement;
    expect(root.querySelector('.workspace-storage')?.textContent).toContain('10 GiB free of 250 GiB');
    expect(root.querySelector('.workspace-storage .pi-exclamation-triangle')).not.toBeNull();
    expect(root.querySelector('[aria-label="Artifact disk available"]')?.getAttribute('aria-valuenow')).toBe('4');
    expect([...root.querySelectorAll('.workspace__disk')].map((el) => el.textContent?.trim())).toEqual([
      '2.5 GiB used',
      '0 B used',
      'Usage unavailable',
    ]);
    expect(root.querySelector('.workspace-storage input')).toBeNull();
  });

  it('does not make the agent page unavailable if disk telemetry fails', async () => {
    vi.spyOn(agentService, 'getWorkspaceStorage').mockRejectedValue(new Error('telemetry unavailable'));
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;
    expect(root.querySelector('.workspace-storage')?.textContent).toContain('Runner artifact disk: unavailable');
    expect(component.error()).toBeNull();
  });

  describe('session timings', () => {
    beforeEach(async () => {
      vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] });
      vi.setSystemTime(new Date('2026-09-02T10:02:09Z'));
      fixture.detectChanges();
      await component.refresh();
    });

    const timings = (): string[] =>
      Array.from(
        fixture.nativeElement.querySelectorAll('.session__timing') as NodeListOf<HTMLElement>,
      ).map((element) => element.textContent!.trim());

    it('refreshes RAM on polls and keeps only the observed peak for finished sessions', async () => {
      const row = makeSession({ status: 'starting', memory_mb: 2048, memory_current_bytes: 1048576,
        memory_peak_bytes: 2 * 1048576 });
      agentService.sessions = [row, makeSession({ id: 22, status: 'failed' })];
      await component.refresh();
      fixture.detectChanges();
      const readings = () => [...fixture.nativeElement.querySelectorAll('.session__memory')]
        .map((el: HTMLElement) => el.textContent!.replace(/\s+/g, ' ').trim());
      expect(readings()[0]).toContain('RAM now 1 MiB');
      expect(readings()[1]).toBe('Observed peak Unavailable');

      agentService.sessions = [{ ...row, status: 'paused', memory_current_bytes: 0 }];
      await vi.advanceTimersByTimeAsync(4000);
      fixture.detectChanges();
      expect(readings()[0]).toContain('RAM now 0 MiB');

      agentService.sessions = [{ ...row, status: 'succeeded' }];
      await vi.advanceTimersByTimeAsync(4000);
      fixture.detectChanges();
      expect(readings()[0]).not.toContain('RAM now');
      expect(readings()[0]).toContain('Observed peak 2 MiB');
      expect(readings()[0]).toContain('Limit 2,048 MiB');
    });

    it('counts distinct agents and shows a self-reported countdown in active minutes', async () => {
      const row = makeSession({ status: 'running', active_seconds: 1200, active_since: new Date().toISOString(),
        progress_percent: 40, progress_active_s: 1200, progress_reported_at: new Date().toISOString(), subagent_count: 1 });
      agentService.sessions = [row];
      await component.refresh();
      fixture.detectChanges();
      const list = () => fixture.nativeElement.querySelector('.session__head').textContent;
      expect(list()).toContain('2x');
      expect(list()).toContain('About 30 min of active work left');
      await vi.advanceTimersByTimeAsync(61000);
      expect(component.progressEta(row)).toBe('About 29 min of active work left');
      const paused = { ...row, status: 'paused' as const, active_seconds: 1320, active_since: null };
      expect(component.progressEta(paused)).toBe('About 28 min of active work left');
      await vi.advanceTimersByTimeAsync(61000);
      expect(component.progressEta(paused)).toBe('About 28 min of active work left');
      expect(component.progressEta({ ...paused, active_seconds: 2400 })).toBe('Awaiting updated estimate');
      expect(component.progressEta({ ...paused, progress_percent: 99 })).toBe('Awaiting updated estimate');
      for (const progress_percent of [null, 0, 100]) expect(component.progressEta({ ...row, progress_percent })).toBe('');
      agentService.sessions = [{ ...row, subagent_count: 2, status: 'succeeded' }];
      await component.refresh();
      fixture.detectChanges();
      expect(list()).toContain('3x');
      expect(list()).not.toContain('active work left');
      agentService.sessions = [makeSession({ subagent_count: null })];
      await component.refresh();
      fixture.detectChanges();
      expect(list()).not.toContain('Agent estimate');
      expect(list()).not.toContain('2x');
    });

    it('expires RAM now while the session list cannot refresh', async () => {
      agentService.sessions = [makeSession({ status: 'running', memory_current_bytes: 1048576, memory_peak_bytes: 1048576 })];
      await component.refresh();
      vi.spyOn(agentService, 'getSessions').mockRejectedValue(new Error('offline'));
      await vi.advanceTimersByTimeAsync(12000);
      fixture.detectChanges();
      const memory = fixture.nativeElement.querySelector('.session__memory').textContent;
      expect(memory).toContain('RAM now Unavailable');
      expect(memory).toContain('Observed peak 1 MiB');
    });

    it('updates queue waits and running durations every second between server polls', async () => {
      agentService.sessions = [
        makeSession({
          id: 1,
          status: 'queued',
          created_at: '2026-09-02T10:02:00Z',
          started_at: null,
        }),
        makeSession({ id: 2, status: 'running', started_at: '2026-09-02T10:02:00Z' }),
      ];
      await component.refresh();
      fixture.detectChanges();
      expect(timings()).toEqual(['Queued 2m 0s', 'Run 9s', 'Queued 9s']);
      agentService.sessionCalls = 0;

      await vi.advanceTimersByTimeAsync(1000);
      fixture.detectChanges();

      expect(timings()).toEqual(['Queued 2m 0s', 'Run 10s', 'Queued 10s']);
      expect(agentService.sessionCalls).toBe(0);
      expect(fixture.nativeElement.querySelector('.status--running')).toBeTruthy();
    });

    it.each(['succeeded', 'failed', 'cancelled'] as const)(
      'updates the finish age of a %s session while queue and runtime stay fixed',
      async (status) => {
        agentService.sessions = [makeSession({ status, finished_at: '2026-09-02T10:02:00Z' })];
        await component.refresh();
        fixture.detectChanges();
        expect(timings()).toEqual(['Queued 1m 0s', 'Run 1m 0s', 'Finished 9s ago']);
        expect(
          fixture.nativeElement.querySelector('.session__timing[title^="Finished "]'),
        ).toBeTruthy();
        agentService.sessionCalls = 0;

        await vi.advanceTimersByTimeAsync(1000);
        fixture.detectChanges();

        expect(timings()).toEqual(['Queued 1m 0s', 'Run 1m 0s', 'Finished 10s ago']);
        expect(agentService.sessionCalls).toBe(0);
      },
    );

    it('freezes the queue wait when a session is cancelled before it starts', async () => {
      const session = makeSession({
        status: 'cancelled',
        started_at: null,
        finished_at: '2026-09-02T10:01:00Z',
      });
      agentService.sessions = [session];
      await component.refresh();
      fixture.detectChanges();
      expect(timings()).toEqual(['Queued 1m 0s', 'Finished 1m 9s ago']);
      expect(component.duration(session)).toBe('—');

      await vi.advanceTimersByTimeAsync(1000);
      fixture.detectChanges();

      expect(timings()).toEqual(['Queued 1m 0s', 'Finished 1m 10s ago']);
    });

    it.each([
      ['2026-09-02T10:01:09.001Z', '59s'],
      ['2026-09-02T10:01:09Z', '1m 0s'],
      ['2026-09-02T09:02:09.001Z', '59m 59s'],
      ['2026-09-02T09:02:09Z', '1h 0m'],
      ['2026-09-02T10:02:10Z', '0s'],
      ['invalid', '—'],
    ])('formats a start at %s as %s without overflowing seconds', (started_at, expected) => {
      expect(component.duration(makeSession({ started_at }))).toBe(expected);
    });

    it('stops both local timing updates and polling when the page is destroyed', async () => {
      const session = makeSession({ status: 'running', started_at: '2026-09-02T10:02:00Z' });
      const elapsed = component.duration(session);
      fixture.destroy();
      agentService.sessionCalls = 0;
      agentService.capacityCalls = 0;

      await vi.advanceTimersByTimeAsync(5000);

      expect(component.duration(session)).toBe(elapsed);
      expect(agentService.sessionCalls).toBe(0);
      expect(agentService.capacityCalls).toBe(0);
      expect(vi.getTimerCount()).toBe(0);
    });
  });

  describe('slot strip', () => {
    it('scales categories proportionally when total exceeds the cell limit', () => {
      // Truncating in category order would render 60 agent / 4 other / 0 free
      // and contradict the legend. Proportional scaling keeps every non-zero
      // category visible.
      expect(scaleSlotCounts([60, 20, 20], 64)).toEqual([38, 13, 13]);
      expect(scaleSlotCounts([60, 20, 20], 64).reduce((a, b) => a + b, 0)).toBe(64);
    });

    it('keeps every non-zero category at least one cell', () => {
      expect(scaleSlotCounts([100, 1, 1], 64)).toEqual([62, 1, 1]);
    });

    it('passes counts through when they already fit', () => {
      expect(scaleSlotCounts([2, 1, 1], 64)).toEqual([2, 1, 1]);
    });

    it('renders a proportional strip for a capacity above the cell limit', async () => {
      agentService.getCapacity = async () => ({
        ...CAPACITY,
        total_slots: 100,
        busy_slots: 80,
        own_slots: 60,
        other_slots: 20,
      });
      await component.refresh();
      const cells = component.slotCells();
      expect(cells.length).toBe(64);
      expect(cells.filter((c) => c === 'agent').length).toBe(38);
      expect(cells.filter((c) => c === 'other').length).toBe(13);
      expect(cells.filter((c) => c === 'free').length).toBe(13);
    });
  });

  describe('grouping', () => {
    it('keeps a finalizing session in the active group', async () => {
      // the application server counts finalizing as active (it still occupies the
      // workspace); the page must too, or the row renders under Finished
      // while the runner is still pushing its work.
      expect(ACTIVE_SESSION_STATUSES).toContain('finalizing');
      expect(isActive('finalizing')).toBe(true);

      agentService.sessions = [
        makeSession({ id: 1, status: 'finalizing' }),
        makeSession({ id: 2, status: 'succeeded' }),
      ];
      await component.refresh();

      expect(component.activeSessions().map((s) => s.id)).toEqual([1]);
      expect(component.finishedSessions().map((s) => s.id)).toEqual([2]);
    });
  });

  describe('analyze all repositories', () => {
    it('queues every repository and says how many', async () => {
      teamService.analyzeAllRepositories.mockResolvedValue({
        queued: 14,
        already_in_flight: 1,
        message: '14 analyses queued, 1 already in flight',
      });
      await component.analyzeAllRepositories();
      expect(teamService.analyzeAllRepositories).toHaveBeenCalledTimes(1);
      expect(component.analyzeAllMessage()).toBe('14 analyses queued, 1 already in flight');
      expect(component.analyzingAll()).toBe(false);
    });

    it('reports a failure instead of a count', async () => {
      teamService.analyzeAllRepositories.mockRejectedValue(new Error('boom'));
      await component.analyzeAllRepositories();
      expect(component.analyzeAllMessage()).toBeNull();
      expect(component.error()).toBeTruthy();
    });
  });

  describe('repository analysis sessions', () => {
    it('names the team and repository being analysed', () => {
      const analysis = makeSession({
        trigger_kind: 'analysis',
        repo_slug: 'ls1intum/hestia',
        team_name: 'hestia',
      });
      expect(component.analysisTarget(analysis)).toBe('hestia · ls1intum/hestia');
      expect(component.originOf(analysis)).toBe('repository analysis');
    });

    it('falls back to the slug once the link is gone, and stays out of other sessions', () => {
      expect(
        component.analysisTarget(
          makeSession({ trigger_kind: 'analysis', repo_slug: 'ls1intum/artemis', team_name: null }),
        ),
      ).toBe('ls1intum/artemis');
      expect(
        component.analysisTarget(
          makeSession({ trigger_kind: 'issue', repo_slug: 'LogosOSS/Logos' }),
        ),
      ).toBeNull();
    });
  });

  describe('the repository of a new session', () => {
    const linked: AgentRepository = {
      id: 5,
      team_id: 2,
      repo_slug: 'ls1intum/artemis',
      branch: 'develop',
      verification_command: './gradlew test',
      agent_egress_hosts: ['github.com'],
      agent_egress_unrestricted: false,
      agent_may_comment: false,
    };
    const workspace: AgentWorkspace = {
      id: 3,
      name: 'feature-work',
      base_branch: 'main',
      volume_name: 'agent-ws-3',
      created_by: 'tester',
      created_at: '2026-09-02T10:00:00Z',
      active_sessions: 0,
    };

    beforeEach(async () => {
      agentService.repositories = [linked];
      agentService.workspaces = [workspace];
      await component.refresh();
      component.formTask.set('Fix the failing build on develop');
    });

    it('offers the platform repository first, then the enabled linked repositories', () => {
      expect(component.repositoryOptions()).toEqual([
        { value: '', label: 'LogosOSS/Logos (platform repository)' },
        { value: '5', label: 'ls1intum/artemis (develop)' },
      ]);
      expect(component.formRepositoryId()).toBeNull();
    });

    it('keeps only the platform repository when the list cannot be read', async () => {
      agentService.repositoriesFail = true;
      await component.refresh();
      expect(component.repositoryOptions()).toEqual([
        { value: '', label: 'LogosOSS/Logos (platform repository)' },
      ]);
      expect(component.error()).toBeNull();
    });

    it('sends no linked repository for the platform repository', async () => {
      component.formDeploy.set(true);
      component.formScreenshots.set('/dashboard /models');
      await component.submit();
      expect(agentService.createBodies[0]).toEqual(expect.objectContaining({
        workspace_id: 3,
        team_repository_id: null,
        deploy_to_dev: true,
        screenshot_paths: ['/dashboard', '/models'],
      }));
    });

    it('turns off deploy and screenshots for a linked repository', async () => {
      component.formDeploy.set(true);
      component.formScreenshots.set('/dashboard');
      component.selectRepositoryById('5');
      expect(component.linkedRepositorySelected()).toBe(true);
      expect(component.formDeploy()).toBe(false);
      expect(component.formScreenshots()).toBe('');
      await component.submit();
      expect(agentService.createBodies[0]).toEqual(expect.objectContaining({
        team_repository_id: 5,
        deploy_to_dev: false,
        screenshot_paths: [],
      }));
    });

    it('falls back to the platform repository when the selected one is gone', async () => {
      component.selectRepositoryById('5');
      agentService.repositories = [];
      await component.refresh();
      expect(component.formRepositoryId()).toBeNull();
    });

    it('shows the repository of a session on a linked repository', async () => {
      agentService.sessions = [
        makeSession({ id: 7, status: 'running', repo_slug: 'ls1intum/artemis' }),
        makeSession({ id: 8, status: 'running', workspace_name: 'other-work', repo_slug: null }),
      ];
      await component.refresh();
      fixture.detectChanges();
      const rows = Array.from(
        (fixture.nativeElement as HTMLElement).querySelectorAll('.session'),
      ).map((row) => row.textContent ?? '');
      expect(rows[0]).toContain('ls1intum/artemis');
      expect(rows[1]).not.toContain('ls1intum/artemis');
    });
  });

  describe('the memory per session', () => {
    it('ends the session slider at what the memory budget holds', () => {
      expect(component.limitCeiling()).toBe(2);
      expect(component.limitShown()).toBe(2);
      expect(component.memoryShown()).toBe('4096');
    });

    it('sets the memory per session and goes back to the default on an empty field', async () => {
      await component.applyMemory('2048');
      await component.applyMemory('');

      expect(agentService.controlBodies).toEqual([
        { session_memory_mb: 2048 },
        { clear_session_memory_mb: true },
      ]);
    });

    it('shows the reserved share of the memory budget, capped at the budget', async () => {
      expect(component.memoryPercent()).toBe(0);

      const controls = await agentService.getControls();
      agentService.getControls = async () => ({ ...controls, memory_reserved_mb: 4096 });
      await component.refresh();
      expect(component.memoryPercent()).toBe(50);

      agentService.getControls = async () => ({ ...controls, memory_reserved_mb: 9000 });
      await component.refresh();
      expect(component.memoryPercent()).toBe(100);
    });

    it('refuses less than 512 MiB without sending anything', async () => {
      await component.applyMemory('100');

      expect(agentService.controlBodies).toEqual([]);
      expect(component.error()).toContain('512 MiB');
    });

    it('shows the ceiling in force, not a stored override the budget clamps', async () => {
      agentService.getControls = async () => ({
        ...(await new FakeAgentService().getControls()),
        max_parallel: 1,
        max_parallel_override: 4,
        max_parallel_available: 1,
      });
      await component.refresh();

      expect(component.limitShown()).toBe(1);
      expect(component.limitCeiling()).toBe(1);
    });

    it('lowers the ceiling below the maximum and clears it at the maximum', async () => {
      await component.applyLimit('1');
      await component.applyLimit('5');

      expect(agentService.controlBodies).toEqual([{ max_parallel: 1 }, { clear_max_parallel: true }]);
    });
  });

  describe('the standing instructions', () => {
    /**
     * Reset is a statement about one box. The other one may hold an edit
     * nobody has saved yet, and sending it along would save it behind the
     * operator's back — and refilling it from the answer would throw it
     * away on screen as well.
     */
    it("resets one half without submitting the other half's draft", async () => {
      component.houseRulesDraft.set('rules nobody saved');
      component.environmentNotesDraft.set('notes nobody saved');

      await component.resetInstructions('house_rules');

      expect(agentService.instructionBodies).toEqual([{ reset_house_rules: true }]);
      expect(component.environmentNotesDraft()).toBe('notes nobody saved');
      expect(component.houseRulesDraft()).toBe('the shipped rules');
    });

    it('saves both halves when both are being saved', async () => {
      component.houseRulesDraft.set('be brief');
      component.environmentNotesDraft.set('a container');

      await component.saveInstructions();

      expect(agentService.instructionBodies).toEqual([
        { house_rules: 'be brief', environment_notes: 'a container' },
      ]);
    });
  });

  describe('polling', () => {
    // tick() is private: the poll runs on an interval, and the tests drive
    // one tick directly instead of waiting for the timer.
    const tick = (): Promise<void> => (component as unknown as { tick(): Promise<void> }).tick();

    it('keeps refreshing while a finalizing session is the only live one', async () => {
      // With nothing selected, the page only refreshes while the active
      // group is non-empty. A finalizing-only list must count as live, or
      // the row would never leave finalizing on screen.
      agentService.sessions = [makeSession({ id: 1, status: 'finalizing' })];
      await component.refresh();
      agentService.sessionCalls = 0;

      await tick();

      expect(agentService.sessionCalls).toBeGreaterThan(0);
      expect(component.activeSessions().map((s) => s.id)).toEqual([1]);
    });

    it('drops to capacity-only once nothing is live and nothing is selected', async () => {
      // The other side of the same gate: an all-finished list with no
      // selection must not keep the runner answering session polls.
      agentService.sessions = [makeSession({ id: 1, status: 'succeeded' })];
      await component.refresh();
      agentService.sessionCalls = 0;

      await tick();

      expect(agentService.sessionCalls).toBe(0);
      expect(agentService.capacityCalls).toBeGreaterThan(0);
    });
  });
});
