import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ActivatedRoute, Router, convertToParamMap, provideRouter } from '@angular/router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { DeploymentService } from '../../core/services/deployment.service';
import {
  Deployer,
  DeploymentOverview,
  DeploymentPlan,
  DeploymentStep,
  DeploymentTarget,
  phaseStates,
  stepStatusText,
} from '../../shared/models/deployment.model';
import { DeploymentDetail } from './deployment-detail';
import { Deployments } from './deployments';

const NOW = '2026-11-07T10:00:00.000Z';

const makeDeployer = (overrides: Partial<Deployer> = {}): Deployer => ({
  id: 'd-core',
  name: 'dev-core',
  environment: 'Dev',
  kind: 'core',
  online: true,
  last_seen_at: NOW,
  version: '0.1.0',
  created_at: NOW,
  ...overrides,
});

const makeTarget = (overrides: Partial<DeploymentTarget> = {}): DeploymentTarget => ({
  name: 'Dev',
  own: true,
  requires_confirmation: false,
  deployers: [makeDeployer()],
  running_plan_id: null,
  last_status: 'succeeded',
  can_start: true,
  blocked_reason: null,
  ...overrides,
});

const makeOverview = (overrides: Partial<DeploymentOverview> = {}): DeploymentOverview => ({
  enabled: true,
  environment: 'Dev',
  targets: [makeTarget()],
  suggested_tags: ['latest', 'pr-93'],
  ...overrides,
});

const makeStep = (overrides: Partial<DeploymentStep> = {}): DeploymentStep => ({
  id: 's1',
  position: 0,
  kind: 'core',
  deployer_name: 'dev-core',
  status: 'succeeded',
  phase: 'done',
  failure: null,
  message: 'now running tag pr-93',
  started_at: '2026-11-07T09:00:00.000Z',
  finished_at: '2026-11-07T09:03:20.000Z',
  last_activity_at: NOW,
  events: [{ at: '2026-11-07T09:00:01.000Z', phase: 'pulling', note: 'pulled the images' }],
  failure_logs: null,
  details: {},
  ...overrides,
});

const makePlan = (overrides: Partial<DeploymentPlan> = {}): DeploymentPlan => ({
  id: 'p1',
  number: 7,
  environment: 'Dev',
  tag: 'pr-93',
  status: 'succeeded',
  origin: 'local',
  initiator: 'Tobias Wasner',
  reason: null,
  abandoned_by: null,
  created_at: '2026-11-07T09:00:00.000Z',
  finished_at: '2026-11-07T09:05:00.000Z',
  last_activity_at: NOW,
  steps: [{ name: 'dev-core', kind: 'core', status: 'succeeded' }],
  step_details: null,
  ...overrides,
});

describe('Deployments', () => {
  let fixture: ComponentFixture<Deployments>;
  let component: Deployments;
  let service: Record<string, ReturnType<typeof vi.fn>>;
  let overview: DeploymentOverview;
  let plans: DeploymentPlan[];

  const text = (): string => (fixture.nativeElement as HTMLElement).textContent ?? '';

  async function create(): Promise<void> {
    fixture = TestBed.createComponent(Deployments);
    component = fixture.componentInstance;
    fixture.detectChanges();
    await vi.waitFor(() => expect(component.loading()).toBe(false));
    fixture.detectChanges();
  }

  beforeEach(async () => {
    overview = makeOverview();
    plans = [makePlan()];
    service = {
      getOverview: vi.fn().mockImplementation(async () => overview),
      getPlans: vi.fn().mockImplementation(async () => plans),
      getPlan: vi.fn().mockImplementation(async (id: string) => ({
        ...(plans.find((p) => p.id === id) ?? makePlan({ id })),
        step_details: [makeStep()],
      })),
      startPlan: vi.fn().mockResolvedValue(makePlan({ id: 'new', status: 'running' })),
      abandonPlan: vi.fn().mockResolvedValue(makePlan({ status: 'failed' })),
      createDeployer: vi.fn(),
      replaceDeployerKey: vi.fn(),
      deleteDeployer: vi.fn(),
    };
    await TestBed.configureTestingModule({
      imports: [Deployments],
      providers: [
        provideRouter([]),
        { provide: DeploymentService, useValue: service },
        { provide: ActivatedRoute, useValue: { snapshot: { paramMap: convertToParamMap({}) } } },
      ],
    }).compileComponents();
  });

  it('says that deployments are not set up on a Logos without deployers', async () => {
    overview = makeOverview({ enabled: false, targets: [] });
    await create();
    expect(text()).toContain('Deployments are not set up on this Logos');
    expect(service['getPlans']).not.toHaveBeenCalled();
  });

  it('shows the switch in the order Prod, Dev, Test and never says "this Logos" next to an environment', async () => {
    overview = makeOverview({
      environment: 'Prod',
      targets: [
        makeTarget({ name: 'Prod', requires_confirmation: true }),
        makeTarget({ name: 'Dev' }),
        makeTarget({ name: 'Test' }),
      ],
    });
    await create();
    const pills = Array.from(fixture.nativeElement.querySelectorAll('.lg-pill')).map((e) =>
      (e as HTMLElement).textContent?.trim(),
    );
    expect(pills).toEqual(['Prod', 'Dev', 'Test']);
    expect(text()).not.toContain('this Logos');
    expect(text()).toContain('Deployments on Prod affect live users');
  });

  it('lists the history with numbers and links each row to its page', async () => {
    plans = [makePlan({ id: 'a', number: 9 }), makePlan({ id: 'b', number: 8, status: 'failed' })];
    await create();
    const rows = fixture.nativeElement.querySelectorAll('a.lg-trow') as NodeListOf<HTMLAnchorElement>;
    expect(rows.length).toBe(2);
    expect(rows[0].getAttribute('href')).toBe('/deployments/a');
    expect(text()).toContain('#9');
    expect(text()).toContain('Tobias Wasner');
    expect(fixture.nativeElement.querySelector('.avatar')).toBeNull();
  });

  it('filters the history by status', async () => {
    plans = [makePlan({ id: 'a', number: 9 }), makePlan({ id: 'b', number: 8, status: 'failed' })];
    await create();
    component.statusFilter.set('failed');
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelectorAll('a.lg-trow').length).toBe(1);
  });

  it('shows the running deployment with its nodes and offers to abandon it', async () => {
    plans = [makePlan({ id: 'run', number: 12, status: 'running', finished_at: null })];
    await create();
    expect(text()).toContain('Deployment #12');
    expect(text()).toContain('View details');
    expect(text()).not.toContain('Only logos admins');
    expect(text()).toContain('Abandon deployment');
    expect(fixture.nativeElement.querySelectorAll('app-phase-bar').length).toBe(1);
  });

  it('keeps Deploy off on a protected environment until its name is typed, then opens the page of the plan', async () => {
    overview = makeOverview({ environment: 'Prod', targets: [makeTarget({ name: 'Prod', requires_confirmation: true })] });
    plans = [];
    await create();
    const router = TestBed.inject(Router);
    const navigate = vi.spyOn(router, 'navigate').mockResolvedValue(true);
    expect(component.confirmed()).toBe(false);
    expect(text()).toContain('Type prod to confirm');

    component.confirmation.set('prod');
    await component.deploy();

    expect(service['startPlan']).toHaveBeenCalledWith('Prod', 'latest', 'prod');
    expect(navigate).toHaveBeenCalledWith(['/deployments', 'new']);
  });

  it('does not start without the typed name', async () => {
    overview = makeOverview({ environment: 'Prod', targets: [makeTarget({ name: 'Prod', requires_confirmation: true })] });
    await create();
    await component.deploy();
    expect(service['startPlan']).not.toHaveBeenCalled();
    expect(component.startError()).toContain('Type prod to confirm');
  });

  it('shows why the page could not load', async () => {
    service['getOverview'].mockRejectedValueOnce(new Error('down'));
    await create();
    expect(component.loadError()).not.toBeNull();
    expect(text()).toContain('could not be loaded');
  });

  it('loads the plans of another environment when it is chosen, and does nothing for the chosen one', async () => {
    overview = makeOverview({ targets: [makeTarget({ name: 'Dev' }), makeTarget({ name: 'Test' })] });
    await create();
    service['getPlans'].mockClear();

    await component.selectEnv('Dev');
    expect(service['getPlans']).not.toHaveBeenCalled();

    component.confirmation.set('typed');
    await component.selectEnv('Test');
    expect(component.selectedEnv()).toBe('Test');
    expect(component.confirmation()).toBe('');
    expect(service['getPlans']).toHaveBeenCalledWith('Test', 50);
  });

  it('shows an error when the plans of the chosen environment cannot be loaded', async () => {
    overview = makeOverview({ targets: [makeTarget({ name: 'Dev' }), makeTarget({ name: 'Test' })] });
    await create();
    service['getPlans'].mockRejectedValueOnce(new Error('down'));
    await component.selectEnv('Test');
    expect(component.loadError()).not.toBeNull();
  });

  it('keeps the page when a later reload fails', async () => {
    await create();
    service['getOverview'].mockRejectedValueOnce(new Error('down'));
    await component.load(false);
    expect(component.loadError()).toBeNull();
    expect(component.overview()).not.toBeNull();
  });

  it('shows the dot of an environment: the end of its last deployment, or an empty ring', async () => {
    await create();
    expect(component.envDot(makeTarget({ last_status: 'rolled_back' }))).toBe('rolled-back');
    expect(component.envDot(makeTarget({ last_status: null }))).toBe('none');
  });

  it('refuses to start without a tag', async () => {
    await create();
    component.tag.set('  ');
    await component.deploy();
    expect(service['startPlan']).not.toHaveBeenCalled();
    expect(component.startError()).toContain('image tag');
  });

  it('shows the error when a deployment cannot be started', async () => {
    await create();
    service['startPlan'].mockRejectedValueOnce(new Error('refused'));
    await component.deploy();
    expect(component.startError()).not.toBeNull();
    expect(component.starting()).toBe(false);
  });

  describe('abandoning', () => {
    beforeEach(() => {
      plans = [makePlan({ id: 'run', number: 12, status: 'running', finished_at: null })];
    });

    it('asks first and abandons the running deployment', async () => {
      await create();
      component.askAbandon();
      expect(component.abandonOpen()).toBe(true);

      await component.confirmAbandon();

      expect(service['abandonPlan']).toHaveBeenCalledWith('run');
      expect(component.abandonOpen()).toBe(false);
    });

    it('keeps the dialog open and says why when it fails', async () => {
      await create();
      service['abandonPlan'].mockRejectedValueOnce(new Error('no'));
      component.askAbandon();
      await component.confirmAbandon();
      expect(component.abandonOpen()).toBe(true);
      expect(component.abandonError()).not.toBeNull();
      component.closeAbandon();
      expect(component.abandonOpen()).toBe(false);
    });

    it('does nothing when no deployment runs', async () => {
      plans = [makePlan()];
      await create();
      await component.confirmAbandon();
      expect(service['abandonPlan']).not.toHaveBeenCalled();
    });
  });

  describe('deployers', () => {
    const created = { deployer: makeDeployer({ id: 'd-new', name: 'deioma', kind: 'worker' }), key: 'secret-key' };

    it('makes a deployer and shows its key once', async () => {
      service['createDeployer'].mockResolvedValue(created);
      await create();
      component.toggleAdd();
      expect(component.addOpen()).toBe(true);
      component.newName.set('  deioma ');
      component.newKind.set('worker');

      await component.createDeployer();

      expect(service['createDeployer']).toHaveBeenCalledWith('deioma', 'Dev', 'worker');
      expect(component.createdKey()).toEqual(created);
      expect(component.addOpen()).toBe(false);
      expect(component.newName()).toBe('');
      component.closeKey();
      expect(component.createdKey()).toBeNull();
    });

    it('asks for a name', async () => {
      await create();
      await component.createDeployer();
      expect(service['createDeployer']).not.toHaveBeenCalled();
      expect(component.createError()).toContain('name');
    });

    it('shows the error when a deployer cannot be made', async () => {
      service['createDeployer'].mockRejectedValue(new Error('exists'));
      await create();
      component.newName.set('deioma');
      await component.createDeployer();
      expect(component.createError()).not.toBeNull();
      expect(component.creating()).toBe(false);
    });

    it('gives a deployer a new key and shows it', async () => {
      service['replaceDeployerKey'].mockResolvedValue({ deployer: makeDeployer(), key: 'another' });
      await create();
      component.replaceTarget.set(makeDeployer());
      await component.confirmReplaceKey();
      expect(service['replaceDeployerKey']).toHaveBeenCalledWith('d-core');
      expect(component.createdKey()?.key).toBe('another');
      expect(component.replaceTarget()).toBeNull();
    });

    it('keeps the dialog of a new key and says why when it fails', async () => {
      service['replaceDeployerKey'].mockRejectedValue(new Error('no'));
      await create();
      component.replaceTarget.set(makeDeployer());
      await component.confirmReplaceKey();
      expect(component.deployerError()).not.toBeNull();
      expect(component.replaceTarget()).not.toBeNull();
    });

    it('removes a deployer', async () => {
      service['deleteDeployer'].mockResolvedValue(undefined);
      await create();
      component.removeTarget.set(makeDeployer());
      await component.confirmRemove();
      expect(service['deleteDeployer']).toHaveBeenCalledWith('d-core');
      expect(component.removeTarget()).toBeNull();
    });

    it('keeps the dialog of a removal and says why when it fails', async () => {
      service['deleteDeployer'].mockRejectedValue(new Error('no'));
      await create();
      component.removeTarget.set(makeDeployer());
      await component.confirmRemove();
      expect(component.deployerError()).not.toBeNull();
      expect(component.removeTarget()).not.toBeNull();
    });

    it('does nothing for a key or a removal that nobody chose', async () => {
      await create();
      await component.confirmReplaceKey();
      await component.confirmRemove();
      expect(service['replaceDeployerKey']).not.toHaveBeenCalled();
      expect(service['deleteDeployer']).not.toHaveBeenCalled();
    });

    it('copies the key to the clipboard', async () => {
      const writeText = vi.fn().mockResolvedValue(undefined);
      Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
      try {
        await create();
        component.createdKey.set(created);
        await component.copyKey();
        expect(writeText).toHaveBeenCalledWith('secret-key');
        expect(component.keyCopied()).toBe(true);

        writeText.mockRejectedValueOnce(new Error('denied'));
        await component.copyKey();
        expect(component.keyCopied()).toBe(false);
      } finally {
        Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true });
      }
    });

    it('copies nothing when there is no key', async () => {
      await create();
      await component.copyKey();
      expect(component.keyCopied()).toBe(false);
    });
  });
});

describe('DeploymentDetail', () => {
  let fixture: ComponentFixture<DeploymentDetail>;
  let service: Record<string, ReturnType<typeof vi.fn>>;

  async function create(plan: DeploymentPlan): Promise<void> {
    service = { getPlan: vi.fn().mockResolvedValue(plan), abandonPlan: vi.fn() };
    await TestBed.configureTestingModule({
      imports: [DeploymentDetail],
      providers: [
        provideRouter([]),
        { provide: DeploymentService, useValue: service },
        { provide: ActivatedRoute, useValue: { snapshot: { paramMap: convertToParamMap({ id: plan.id }) } } },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(DeploymentDetail);
    fixture.detectChanges();
    await vi.waitFor(() => expect(fixture.componentInstance.loading()).toBe(false));
    fixture.detectChanges();
  }

  it('shows the node events and the container log of a rolled-back deployment', async () => {
    await create(
      makePlan({
        status: 'rolled_back',
        reason: 'verify failed on dev-core',
        step_details: [
          makeStep({ status: 'rolled_back', failure: 'verify_failed', failure_logs: 'health check failed' }),
        ],
      }),
    );
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('Deployment #7');
    expect(text).toContain('The deployment was rolled back');
    expect(text).toContain('Node events');
    expect(text).toContain('pulled the images');
    expect(text).toContain('Container log');
    expect(text).toContain('health check failed');
  });

  it('abandons a running deployment after asking', async () => {
    await create(makePlan({ id: 'run', status: 'running', finished_at: null, step_details: [makeStep()] }));
    const component = fixture.componentInstance;
    component.askAbandon();
    expect(component.abandonOpen()).toBe(true);
    service['abandonPlan'].mockResolvedValue(makePlan({ status: 'failed' }));

    await component.confirmAbandon();

    expect(service['abandonPlan']).toHaveBeenCalledWith('run');
    expect(component.abandonOpen()).toBe(false);
  });

  it('says why a deployment could not be abandoned', async () => {
    await create(makePlan({ id: 'run', status: 'running', finished_at: null, step_details: [makeStep()] }));
    const component = fixture.componentInstance;
    service['abandonPlan'].mockRejectedValue(new Error('no'));
    component.askAbandon();
    await component.confirmAbandon();
    expect(component.abandonError()).not.toBeNull();
    component.closeAbandon();
    expect(component.abandonOpen()).toBe(false);
  });

  it('copies the container log', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    try {
      await create(
        makePlan({
          status: 'failed',
          step_details: [makeStep({ status: 'failed', failure: 'swap_failed', failure_logs: 'port in use' })],
        }),
      );
      const component = fixture.componentInstance;
      await component.copyLog();
      expect(writeText).toHaveBeenCalledWith('port in use');
      expect(component.copied()).toBe(true);
      writeText.mockRejectedValueOnce(new Error('denied'));
      await component.copyLog();
      expect(component.copied()).toBe(false);
    } finally {
      Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true });
    }
  });

  it('copies nothing when the deployment has no log', async () => {
    await create(makePlan({ step_details: [makeStep()] }));
    await fixture.componentInstance.copyLog();
    expect(fixture.componentInstance.copied()).toBe(false);
  });

  it('says how long a deployment took, in seconds, minutes or hours', async () => {
    await create(makePlan({ created_at: '2026-11-07T09:00:00.000Z', finished_at: '2026-11-07T09:00:42.000Z', step_details: [] }));
    expect(fixture.componentInstance.duration()).toBe('42s');
    fixture.componentInstance.plan.set(makePlan({ finished_at: '2026-11-07T09:06:12.000Z' }));
    expect(fixture.componentInstance.duration()).toBe('6m 12s');
    fixture.componentInstance.plan.set(makePlan({ finished_at: '2026-11-07T12:04:00.000Z' }));
    expect(fixture.componentInstance.duration()).toBe('3h 4m');
  });

  it('counts the time of a running deployment up', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] });
    try {
      vi.setSystemTime(new Date('2026-11-07T09:05:00.000Z'));
      await create(makePlan({ status: 'running', finished_at: null, step_details: [] }));
      expect(fixture.componentInstance.duration()).toBe('5m 0s');
      vi.advanceTimersByTime(3000);
      expect(fixture.componentInstance.duration()).toBe('5m 3s');
    } finally {
      vi.useRealTimers();
    }
  });

  it('shows why the deployment could not be loaded', async () => {
    service = { getPlan: vi.fn().mockRejectedValue(new Error('gone')), abandonPlan: vi.fn() };
    await TestBed.configureTestingModule({
      imports: [DeploymentDetail],
      providers: [
        provideRouter([]),
        { provide: DeploymentService, useValue: service },
        { provide: ActivatedRoute, useValue: { snapshot: { paramMap: convertToParamMap({ id: 'x' }) } } },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(DeploymentDetail);
    fixture.detectChanges();
    await vi.waitFor(() => expect(fixture.componentInstance.loading()).toBe(false));
    expect(fixture.componentInstance.loadError()).not.toBeNull();
  });

  it('has no log panel and no abandon button for a deployment that succeeded', async () => {
    await create(makePlan({ step_details: [makeStep()] }));
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).not.toContain('Container log');
    expect(text).not.toContain('Abandon deployment');
  });
});

describe('phaseStates', () => {
  it('marks the segments before the running phase as done', () => {
    expect(phaseStates({ status: 'running', phase: 'swapping', failure: null })).toEqual([
      'done',
      'done',
      'active',
      'pending',
    ]);
  });

  it('marks the segment of a failure', () => {
    expect(phaseStates({ status: 'failed', phase: 'pulling', failure: 'swap_failed' })).toEqual([
      'done',
      'done',
      'failed',
      'pending',
    ]);
  });

  it('marks every segment of a node that ended well as done', () => {
    expect(phaseStates({ status: 'succeeded', phase: 'done', failure: null })).toEqual(['done', 'done', 'done', 'done']);
  });
});

describe('stepStatusText', () => {
  it('cuts the drain message to the number of requests that are left', () => {
    const step = makeStep({
      status: 'running',
      events: [
        {
          at: NOW,
          phase: 'draining',
          note: 'waiting for the node to drain: 128 request(s) (100 tracked by the orchestrator, 28 on the worker) running',
        },
      ],
    });
    expect(stepStatusText(step, 'pr-93')).toBe('Waiting for the node to drain:\n128 request(s)');
  });

  it('keeps other messages as they are', () => {
    const step = makeStep({ status: 'running', events: [{ at: NOW, phase: 'pulling', note: 'pulled the images' }] });
    expect(stepStatusText(step, 'pr-93')).toBe('pulled the images');
  });
});
