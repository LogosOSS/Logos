import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { NodeConfigEntry } from '../../shared/models/node-config.model';
import { ProviderManagementService } from './provider-management.service';

describe('ProviderManagementService node configuration', () => {
  let service: ProviderManagementService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
    service = TestBed.inject(ProviderManagementService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('reads the stored model list of a node', async () => {
    const result = service.getNodeConfig(4);
    const request = http.expectOne('/api/logosdb/node_config/get');
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({ provider_id: 4 });
    request.flush({ provider_id: 4, revision: 2, yaml_imported: true, entries: [] });

    expect((await result).revision).toBe(2);
  });

  it('saves the whole list with the revision it was read at', async () => {
    const entries: NodeConfigEntry[] = [{ model_name: 'org/a', capability: { tensor_parallel_size: 2 }, overrides: {} }];
    const result = service.replaceNodeConfig(4, 2, entries);
    const request = http.expectOne('/api/logosdb/node_config/replace');
    expect(request.request.body).toEqual({ provider_id: 4, expected_revision: 2, entries });
    request.flush({ provider_id: 4, config_revision: 3 });

    expect(await result).toEqual({ provider_id: 4, config_revision: 3 });
  });

  it('passes a refusal of the orchestrator on, status and text included', async () => {
    const result = service.replaceNodeConfig(4, 1, []);
    http
      .expectOne('/api/logosdb/node_config/replace')
      .flush({ error: 'Configuration changed meanwhile (revision 2); reload it' }, { status: 409, statusText: 'Conflict' });

    await expect(result).rejects.toMatchObject({ status: 409, error: { error: expect.stringContaining('revision 2') } });
  });

  it('asks for the insights of a node', async () => {
    const result = service.getNodeInsights(4);
    const request = http.expectOne('/api/logosdb/node_config/insights');
    expect(request.request.body).toEqual({ provider_id: 4 });
    request.flush({ provider_id: 4, worker: { online: true, supports_actions: {}, config_revision: 1 }, gpus: [], models: [] });

    expect((await result).worker.online).toBe(true);
  });

  it('starts the calibration of one model of a node', async () => {
    const result = service.calibrateModel(4, 'org/a');
    const request = http.expectOne('/api/logosdb/providers/logosnode/calibrate_uncalibrated');
    expect(request.request.body).toEqual({ provider_id: 4, model: 'org/a' });
    request.flush({ message: 'started', count: 1 });

    expect(await result).toEqual({ message: 'started', count: 1 });
  });

  it('reads the node-wide settings of a node', async () => {
    const result = service.getNodeSettings(4);
    const request = http.expectOne('/api/logosdb/node_settings/get');
    expect(request.request.body).toEqual({ provider_id: 4 });
    request.flush({ provider_id: 4, revision: 2, yaml_imported: true, settings: { gpu_devices: 'all' } });

    expect((await result).settings).toEqual({ gpu_devices: 'all' });
  });

  it('saves the node-wide settings at the revision they were read at', async () => {
    const result = service.replaceNodeSettings(4, 2, { gpu_poll_interval: 9 });
    const request = http.expectOne('/api/logosdb/node_settings/replace');
    expect(request.request.body).toEqual({ provider_id: 4, expected_revision: 2, settings: { gpu_poll_interval: 9 } });
    request.flush({ provider_id: 4, settings_revision: 3 });

    expect(await result).toEqual({ provider_id: 4, settings_revision: 3 });
  });
});
