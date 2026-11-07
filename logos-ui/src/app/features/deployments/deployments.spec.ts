import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap } from '@angular/router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { DeploymentService } from '../../core/services/deployment.service';
import {
  Deployer,
  DeploymentOverview,
  DeploymentPlan,
  DeploymentStep,
  DeploymentTarget,
} from '../../shared/models/deployment.model';
import { Deployments, formatDuration } from './deployments';

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
  environment: 'Dev',
  tag: 'pr-93',
  status: 'succeeded',
  origin: 'local',
  initiator: 'Jane Doe',
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

  /** The text of the whole page, dialogs included: the dialogs of the design system are drawn outside the component. */
  const pageText = (): string => document.body.textContent ?? '';

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
      createDeployer: vi.fn().mockResolvedValue({
        deployer: makeDeployer({ id: 'd-new', name: 'deioma', kind: 'worker', online: false }),
        key: 'the-secret-key-shown-once',
      }),
      replaceDeployerKey: vi.fn().mockResolvedValue({ deployer: makeDeployer(), key: 'another-key' }),
      deleteDeployer: vi.fn().mockResolvedValue(undefined),
    };
    await TestBed.configureTestingModule({
      imports: [Deployments],
      providers: [
        { provide: DeploymentService, useValue: service },
        { provide: ActivatedRoute, useValue: { snapshot: { queryParamMap: convertToParamMap({}) } } },
      ],
    }).compileComponents();
  });

  describe('a Logos without deployers', () => {
    it('says that deployments are not set up and offers nothing to start', async () => {
      overview = makeOverview({ enabled: false, targets: [] });

      await create();

      expect(text()).toContain('Deployments are not set up on this Logos');
      expect(text()).not.toContain('New deployment');
      expect(service['getPlans']).not.toHaveBeenCalled();
    });
  });

  describe('the environments', () => {
    it('opens the environment of this Logos and loads its history', async () => {
      overview = makeOverview({
        environment: 'Prod',
        targets: [makeTarget({ name: 'Prod', requires_confirmation: true }), makeTarget({ name: 'Dev', own: false })],
      });

      await create();

      expect(component.selectedEnv()).toBe('Prod');
      expect(service['getPlans']).toHaveBeenCalledWith('Prod', 50);
      expect(text()).toContain('Prod');
      expect(text()).toContain('Dev');
    });

    it('reads the history of another environment when it is chosen', async () => {
      overview = makeOverview({
        targets: [makeTarget(), makeTarget({ name: 'Test', own: false })],
      });
      await create();

      await component.selectEnv('Test');

      expect(component.selectedEnv()).toBe('Test');
      expect(service['getPlans']).toHaveBeenLastCalledWith('Test', 50);
    });

    it('shows a warning dot when a core deployer is offline', async () => {
      overview = makeOverview({
        targets: [makeTarget({ deployers: [makeDeployer({ online: false })] })],
      });

      await create();

      expect(component.envState('Dev')).toBe('warn');
    });

    it('shows no warning for an environment without deployers', async () => {
      overview = makeOverview({ targets: [makeTarget({ deployers: [] })] });

      await create();

      expect(component.envState('Dev')).toBe('idle');
    });
  });

  describe('starting a deployment', () => {
    it('starts at once in an environment that needs no confirmation', async () => {
      await create();
      component.tag.set(' pr-93 ');

      component.deploy();
      await vi.waitFor(() => expect(component.starting()).toBe(false));

      expect(component.confirmOpen()).toBe(false);
      expect(service['startPlan']).toHaveBeenCalledWith('Dev', 'pr-93', undefined);
    });

    it('opens the plan it started', async () => {
      await create();

      component.deploy();
      await vi.waitFor(() => expect(component.openPlan()?.id).toBe('new'));
    });

    it('asks a protected environment for its typed name first', async () => {
      overview = makeOverview({
        environment: 'Prod',
        targets: [makeTarget({ name: 'Prod', requires_confirmation: true })],
      });
      await create();
      component.tag.set('latest');

      component.deploy();

      expect(component.confirmOpen()).toBe(true);
      expect(service['startPlan']).not.toHaveBeenCalled();
    });

    it('does not start a protected environment on the wrong word', async () => {
      overview = makeOverview({
        environment: 'Prod',
        targets: [makeTarget({ name: 'Prod', requires_confirmation: true })],
      });
      await create();
      component.deploy();
      component.confirmation.set('dev');

      component.confirmStart();

      expect(service['startPlan']).not.toHaveBeenCalled();
      expect(component.startError()).toBe('Type prod to confirm.');
    });

    it('starts a protected environment on its typed name, in any case', async () => {
      overview = makeOverview({
        environment: 'Prod',
        targets: [makeTarget({ name: 'Prod', requires_confirmation: true })],
      });
      await create();
      component.tag.set('v2026.10.4');
      component.deploy();
      component.confirmation.set(' PROD ');

      component.confirmStart();
      await vi.waitFor(() => expect(component.starting()).toBe(false));

      expect(service['startPlan']).toHaveBeenCalledWith('Prod', 'v2026.10.4', 'PROD');
      expect(component.confirmOpen()).toBe(false);
    });

    it('asks for a tag when it is empty', async () => {
      await create();
      component.tag.set('  ');

      component.deploy();

      expect(component.startError()).toBe('Enter an image tag.');
      expect(service['startPlan']).not.toHaveBeenCalled();
    });

    it('shows the reason of the server when the start is refused', async () => {
      service['startPlan'].mockRejectedValue({ error: { detail: 'A deployment of Dev is running.' } });
      await create();

      component.deploy();
      await vi.waitFor(() => expect(component.startError()).toBe('A deployment of Dev is running.'));
      fixture.detectChanges();

      expect(text()).toContain('A deployment of Dev is running.');
    });

    it('turns the button off and says why when the environment is blocked', async () => {
      overview = makeOverview({
        targets: [
          makeTarget({ can_start: false, blocked_reason: 'The core deployer dev-core was last seen 12 min ago.' }),
        ],
      });

      await create();

      const button = (fixture.nativeElement as HTMLElement).querySelector<HTMLButtonElement>('.new__deploy');
      expect(button?.disabled).toBe(true);
      expect(text()).toContain('The core deployer dev-core was last seen 12 min ago.');
    });
  });

  describe('what runs now', () => {
    const running = makePlan({
      id: 'run',
      status: 'running',
      finished_at: null,
      steps: [
        { name: 'dev-core', kind: 'core', status: 'succeeded' },
        { name: 'vm-2', kind: 'worker', status: 'waiting' },
      ],
    });

    it('shows the running plan with a pill for each step', async () => {
      plans = [running];

      await create();

      expect(text()).toContain('started by Jane Doe');
      expect(text()).toContain('Waiting for the core');
    });

    it('lets an admin abandon a plan that this Logos started', async () => {
      plans = [running];
      await create();

      component.askAbandon();
      await component.confirmAbandon();

      expect(service['abandonPlan']).toHaveBeenCalledWith('run');
      expect(component.abandonOpen()).toBe(false);
    });

    it('does not offer to abandon a plan that another Logos ordered', async () => {
      plans = [{ ...running, origin: 'observed' }];

      await create();

      expect(component.canAbandon(component.currentPlan())).toBe(false);
      expect(text()).toContain('ordered on another Logos');
    });

    it('says that nothing runs when nothing does', async () => {
      await create();

      expect(text()).toContain('Nothing is running in Dev.');
    });
  });

  describe('the history', () => {
    it('lists the plans with who started them and why they failed', async () => {
      plans = [
        makePlan({ id: 'a', tag: 'v1' }),
        makePlan({ id: 'b', tag: 'v2', status: 'failed', reason: 'vm-2: the node did not drain within 900 s' }),
      ];

      await create();

      expect(text()).toContain('v1');
      expect(text()).toContain('v2');
      expect(text()).toContain('vm-2: the node did not drain within 900 s');
    });

    it('filters the plans by status', async () => {
      plans = [makePlan({ id: 'a' }), makePlan({ id: 'b', status: 'failed' }), makePlan({ id: 'c', status: 'rolled_back' })];
      await create();

      component.statusFilter.set('failed');

      expect(component.history().map((p) => p.id)).toEqual(['b']);
      component.statusFilter.set('all');
      expect(component.history()).toHaveLength(3);
    });

    it('labels a plan that came from another Logos', async () => {
      plans = [makePlan({ origin: 'observed' })];

      await create();

      expect(text()).toContain('via another Logos');
    });

    it('shows the steps, the events and the log of a plan when it is opened', async () => {
      plans = [makePlan({ status: 'failed' })];
      service['getPlan'].mockResolvedValue(
        makePlan({
          status: 'failed',
          step_details: [
            makeStep({
              status: 'failed',
              message: 'docker compose up failed',
              failure_logs: 'Error response from daemon',
              details: { drained_requests: 3, drain_seconds: 42 },
            }),
          ],
        }),
      );
      await create();

      await component.open(plans[0]);
      fixture.detectChanges();

      expect(text()).toContain('dev-core');
      expect(text()).toContain('docker compose up failed');
      expect(text()).toContain('Error response from daemon');
      expect(text()).toContain('3 request(s) were running');
      expect(text()).toContain('took 42 s');
      expect(text()).toContain('3 min 20 s');
    });

    it('opens the plan that a notification links to', async () => {
      const id = '3f2b6c1e-8d4a-4b7e-9c11-0a1b2c3d4e5f';
      plans = [makePlan({ id })];
      TestBed.resetTestingModule();
      await TestBed.configureTestingModule({
        imports: [Deployments],
        providers: [
          { provide: DeploymentService, useValue: service },
          { provide: ActivatedRoute, useValue: { snapshot: { queryParamMap: convertToParamMap({ plan: id }) } } },
        ],
      }).compileComponents();

      await create();

      expect(service['getPlan']).toHaveBeenCalledWith(id);
      expect(component.openPlan()?.id).toBe(id);
    });
  });

  describe('the deployers', () => {
    it('shows the key once, after the deployer is made', async () => {
      await create();
      component.newName.set(' deioma ');
      component.newKind.set('worker');

      await component.createDeployer();
      fixture.detectChanges();

      expect(service['createDeployer']).toHaveBeenCalledWith('deioma', 'Dev', 'worker');
      expect(component.createdKey()?.key).toBe('the-secret-key-shown-once');
      expect(pageText()).toContain('the-secret-key-shown-once');

      component.closeKey();
      fixture.detectChanges();

      expect(component.createdKey()).toBeNull();
      expect(pageText()).not.toContain('the-secret-key-shown-once');
    });

    it('asks for a name', async () => {
      await create();
      component.newName.set('   ');

      await component.createDeployer();

      expect(service['createDeployer']).not.toHaveBeenCalled();
      expect(component.createError()).toContain('Enter a name');
    });

    it('shows why a deployer could not be made', async () => {
      service['createDeployer'].mockRejectedValue({ error: { detail: "The environment Dev has a deployer called 'deioma' already." } });
      await create();
      component.newName.set('deioma');

      await component.createDeployer();

      expect(component.createError()).toBe("The environment Dev has a deployer called 'deioma' already.");
    });

    it('gives a deployer a new key and shows it once', async () => {
      await create();
      component.replaceTarget.set(makeDeployer());

      await component.confirmReplaceKey();

      expect(service['replaceDeployerKey']).toHaveBeenCalledWith('d-core');
      expect(component.createdKey()?.key).toBe('another-key');
      expect(component.replaceTarget()).toBeNull();
    });

    it('removes a deployer', async () => {
      await create();
      component.removeTarget.set(makeDeployer());

      await component.confirmRemove();

      expect(service['deleteDeployer']).toHaveBeenCalledWith('d-core');
      expect(component.removeTarget()).toBeNull();
    });

    it('lists the deployers of the environment with their last sight', async () => {
      overview = makeOverview({
        targets: [
          makeTarget({
            deployers: [makeDeployer(), makeDeployer({ id: 'd2', name: 'vm-2', kind: 'worker', online: false, last_seen_at: null })],
          }),
        ],
      });

      await create();

      expect(text()).toContain('dev-core');
      expect(text()).toContain('vm-2');
      expect(text()).toContain('never');
    });
  });

  describe('formatDuration', () => {
    it.each([
      [0, '0 s'],
      [45_000, '45 s'],
      [200_000, '3 min 20 s'],
      [7_500_000, '2 h 5 min'],
      [-5, '0 s'],
    ])('writes %d ms as %s', (ms, expected) => {
      expect(formatDuration(ms)).toBe(expected);
    });
  });
});
