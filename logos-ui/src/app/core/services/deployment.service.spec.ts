import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { DeploymentService } from './deployment.service';

describe('DeploymentService', () => {
  let service: DeploymentService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
    service = TestBed.inject(DeploymentService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('reads the overview', async () => {
    const result = service.getOverview();
    http.expectOne('/api/deployments/overview').flush({ can_start: true });
    expect(await result).toEqual({ can_start: true });
  });

  it('reads the plans with a limit, 50 by default', async () => {
    const limited = service.getPlans(20);
    const request = http.expectOne((r) => r.url === '/api/deployments/plans');
    expect(request.request.params.get('limit')).toBe('20');
    request.flush([]);
    expect(await limited).toEqual([]);

    const all = service.getPlans();
    http.expectOne((r) => r.url === '/api/deployments/plans' && r.params.get('limit') === '50').flush([]);
    await all;
  });

  it('reads one plan', async () => {
    const result = service.getPlan('p1');
    http.expectOne('/api/deployments/plans/p1').flush({ id: 'p1' });
    expect((await result).id).toBe('p1');
  });

  it('starts a plan with its tag', async () => {
    const result = service.startPlan('latest');
    const request = http.expectOne('/api/deployments/plans');
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({ tag: 'latest' });
    request.flush({ id: 'new' });
    await result;
  });

  it('abandons a plan', async () => {
    const result = service.abandonPlan('p1');
    const request = http.expectOne('/api/deployments/plans/p1/abandon');
    expect(request.request.method).toBe('POST');
    request.flush({ id: 'p1' });
    await result;
  });

  it('makes a core deployer', async () => {
    const result = service.createDeployer('dev-core');
    const request = http.expectOne('/api/deployments/deployers');
    expect(request.request.body).toEqual({ name: 'dev-core' });
    request.flush({ key: 'k' });
    expect((await result).key).toBe('k');
  });

  it('replaces the key of a deployer', async () => {
    const result = service.replaceDeployerKey('d1');
    const request = http.expectOne('/api/deployments/deployers/d1/key');
    expect(request.request.method).toBe('POST');
    request.flush({ key: 'new' });
    expect((await result).key).toBe('new');
  });

  it('removes a deployer', async () => {
    const result = service.deleteDeployer('d1');
    const request = http.expectOne('/api/deployments/deployers/d1');
    expect(request.request.method).toBe('DELETE');
    request.flush(null);
    await result;
  });
});
