import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ModelManagementService } from '../../core/services/model-management.service';
import { ProviderManagementService } from '../../core/services/provider-management.service';
import {
  AddProviderPayload,
  ModelConnection,
  Provider,
  UpdateProviderPayload,
} from '../../shared/models/provider.model';
import { Model } from '../../shared/models/model.model';
import { Providers } from './providers';

/**
 * A cloud provider that is none of the named vendors — Hetzner, a vLLM
 * gateway — is configured by leaving the cloud type unset. That is the
 * configuration the generic machinery is built for: addressed at
 * /v1/chat/completions with a plain bearer token, and discovered over
 * GET /v1/models. The form used to make it unreachable: 'none' was filtered
 * out of the cloud list, the field defaulted to 'azure', and 'azure' is the
 * one type the /v1/models sync skips.
 */

const makeProvider = (overrides: Partial<Provider> = {}): Provider => ({
  id: 7,
  name: 'Hetzner',
  base_url: 'https://inference.hetzner.com/api/v1',
  api_key_hint: '••••ret',
  auth_name: '',
  auth_format: '',
  provider_type: 'cloud',
  cloud_provider_type: null,
  privacy_level: 'CLOUD_IN_EU_BY_EU_PROVIDER',
  ...overrides,
});

const makeModel = (id: number, name = `model-${id}`): Model => ({
  id,
  name,
  description: null,
  tags: null,
  aliases: null,
  weight_latency: null,
  weight_accuracy: null,
  weight_cost: null,
  weight_quality: null,
});

describe('Providers', () => {
  let fixture: ComponentFixture<Providers>;
  let component: Providers;
  let addProvider: ReturnType<typeof vi.fn>;
  let updateProvider: ReturnType<typeof vi.fn>;
  let replaceCloudKey: ReturnType<typeof vi.fn>;
  let refreshModels: ReturnType<typeof vi.fn>;
  let modelSyncStatus: ReturnType<typeof vi.fn>;
  let getModels: ReturnType<typeof vi.fn>;
  let getProviderModels: ReturnType<typeof vi.fn>;

  const optionValues = (options: { value: string }[]): string[] => options.map((o) => o.value);

  beforeEach(async () => {
    addProvider = vi.fn().mockResolvedValue({});
    updateProvider = vi.fn().mockResolvedValue({});
    refreshModels = vi.fn().mockResolvedValue({ result: 'Model refresh triggered.' });
    modelSyncStatus = vi.fn().mockResolvedValue({ running: false });
    getModels = vi.fn().mockResolvedValue([]);
    getProviderModels = vi.fn().mockResolvedValue([]);
    replaceCloudKey = vi.fn().mockResolvedValue({ api_key_hint: '••••new' });
    const providerService = {
      replaceCloudKey,
      getProviders: vi.fn().mockResolvedValue([makeProvider()]),
      rotateWorkerKey: vi.fn().mockResolvedValue({ api_key: 'synthetic-worker-replacement' }),
      addProvider,
      updateProvider,
      refreshModels,
      modelSyncStatus,
      getProviderModels,
      getNodeInsights: vi.fn().mockResolvedValue({
        provider_id: 4,
        worker: { online: true, supports_actions: {}, config_revision: 1 },
        gpus: [],
        models: [],
      }),
    };

    await TestBed.configureTestingModule({
      imports: [Providers],
      providers: [
        provideRouter([]),
        { provide: ProviderManagementService, useValue: providerService },
        { provide: ModelManagementService, useValue: { getModels } },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(Providers);
    component = fixture.componentInstance;
    await component.fetchProviders();
  });

  it('shows the cloud key hint as text and saves other settings independently', async () => {
    component.openEditDialog(makeProvider());
    fixture.detectChanges();
    await fixture.whenStable();
    expect(document.querySelector('.key-value-row code')?.textContent).toBe('••••ret');
    expect(document.querySelector('#replacement-key')).toBeNull();
    await component.submitEdit();
    expect(updateProvider.mock.calls[0][0].api_key).toBeUndefined();
    expect(replaceCloudKey).not.toHaveBeenCalled();
  });

  it('only replaces a cloud key on explicit confirmation and preserves pending edits', async () => {
    const provider = makeProvider();
    component.openEditDialog(provider);
    component.editName.set('Unsaved name');
    component.requestRotation(provider);
    await component.confirmRotation();
    component.replacementKey.set('synthetic-cancelled');
    component.closeRotation();
    expect(replaceCloudKey).not.toHaveBeenCalled();
    component.requestRotation(provider);
    expect(component.replacementKey()).toBe('');
    component.replacementKey.set('synthetic-new');
    await component.confirmRotation();
    expect(replaceCloudKey).toHaveBeenCalledWith(provider.id, 'synthetic-new', undefined);
    expect(updateProvider).not.toHaveBeenCalled();
    expect(component.editName()).toBe('Unsaved name');
    expect(component.editTarget()?.api_key_hint).toBe('••••new');
    expect(component.replacementKey()).toBe('');
  });

  it('discards the rotated worker key when the dialog closes', async () => {
    component.openEditDialog(makeProvider({ provider_type: 'logosnode' }));
    component.requestRotation(component.editTarget()!);
    await component.confirmRotation();
    fixture.detectChanges();
    await fixture.whenStable();
    expect(document.querySelector<HTMLInputElement>('#rotated-key')?.value).toBe('synthetic-worker-replacement');
    component.discardWorkerKey();
    fixture.detectChanges();
    expect(component.rotatedKey()).toBeNull();
    await fixture.whenStable();
    expect(document.querySelector('#rotated-key')).toBeNull();
    component.openEditDialog(makeProvider({ provider_type: 'logosnode' }));
    expect(component.rotatedKey()).toBeNull();
  });

  describe('the cloud provider type field', () => {
    it('offers an untyped option for a vendor that is not on the list', () => {
      component.onAddProviderTypeChange('cloud');
      expect(optionValues(component.addCloudProviderTypeOptions())).toContain('none');
    });

    it('does not default a new cloud provider to azure', async () => {
      // Azure has its own control-plane discovery and is excluded from the
      // /v1/models sync, so a provider saved on this default was scraped by
      // neither path and stayed empty.
      component.openAddDialog();
      component.addName.set('Hetzner');
      await component.submitAdd();

      const payload = addProvider.mock.calls[0][0] as AddProviderPayload;
      expect(payload.provider_type).toBe('cloud');
      expect(payload.cloud_provider_type).toBeUndefined();
    });

    it('keeps a named vendor the operator picked', async () => {
      component.openAddDialog();
      component.addName.set('OpenAI');
      component.addCloudProviderType.set('openai');
      await component.submitAdd();

      expect((addProvider.mock.calls[0][0] as AddProviderPayload).cloud_provider_type).toBe(
        'openai',
      );
    });

    it('does not relabel an untyped cloud provider as azure on edit', async () => {
      // Merely opening the dialog and saving used to move the provider onto
      // the Azure path, silently dropping it out of the sync it relied on.
      component.openEditDialog(makeProvider());
      expect(component.editCloudProviderType()).toBe('none');

      await component.submitEdit();
      expect((updateProvider.mock.calls[0][0] as UpdateProviderPayload).cloud_provider_type).toBe(
        'none',
      );
    });

    it('stays out of the way for a logosnode', () => {
      component.onAddProviderTypeChange('logosnode');
      expect(component.addProviderType()).toBe('logosnode');
      expect(component.addCloudProviderType()).toBe('none');
    });
  });

  describe('the provider overview', () => {
    it('lists cloud and local providers apart', async () => {
      component.providers.set([
        makeProvider({ id: 1, name: 'Anthropic', cloud_provider_type: 'anthropic' }),
        makeProvider({ id: 2, name: 'GPU box', provider_type: 'logosnode', privacy_level: 'LOCAL' }),
        makeProvider({ id: 3, name: 'Hetzner' }),
      ]);

      expect(component.cloudProviders().map((p) => p.name)).toEqual(['Anthropic', 'Hetzner']);
      expect(component.localProviders().map((p) => p.name)).toEqual(['GPU box']);
    });

    it('keeps the search across both lists', () => {
      component.providers.set([
        makeProvider({ id: 1, name: 'Anthropic' }),
        makeProvider({ id: 2, name: 'GPU box', provider_type: 'logosnode', privacy_level: 'LOCAL' }),
      ]);
      component.search.set('gpu');

      expect(component.cloudProviders()).toEqual([]);
      expect(component.localProviders().map((p) => p.name)).toEqual(['GPU box']);
    });

    it('names the vendor of a cloud provider', () => {
      expect(component.typeLabel(makeProvider({ cloud_provider_type: 'anthropic' }))).toBe('anthropic');
      expect(component.typeLabel(makeProvider())).toBe('other (OpenAI-compatible)');
    });
  });

  describe('the privacy level', () => {
    it('offers readable choices instead of the stored values', () => {
      component.openAddDialog();
      expect(component.addPrivacyLevelOptions().map((o) => o.label)).toEqual([
        'Cloud in the EU, EU provider',
        'Cloud in the EU, US provider',
        'Cloud outside the EU, US provider',
      ]);

      component.onAddProviderTypeChange('logosnode');
      expect(component.addPrivacyLevelOptions().map((o) => o.label)).toEqual([
        'Local hardware',
        'Third-party hardware',
      ]);
    });

    it('still saves the stored value', async () => {
      component.openAddDialog();
      component.addName.set('Anthropic');
      component.addPrivacyLevel.set('CLOUD_NOT_IN_EU_BY_US_PROVIDER');
      await component.submitAdd();

      expect((addProvider.mock.calls[0][0] as AddProviderPayload).privacy_level).toBe(
        'CLOUD_NOT_IN_EU_BY_US_PROVIDER',
      );
    });
  });

  describe('the base URL proposal', () => {
    it('proposes the vendor endpoint once the cloud provider is picked', () => {
      component.openAddDialog();
      expect(component.addBaseUrl()).toBe('');

      component.onAddCloudProviderTypeChange('anthropic');
      expect(component.addBaseUrl()).toBe('https://api.anthropic.com');
      expect(component.addBaseUrlIsSuggestion()).toBe(true);
    });

    it('follows a vendor change while the field still holds the proposal', () => {
      component.openAddDialog();
      component.onAddCloudProviderTypeChange('anthropic');
      component.onAddCloudProviderTypeChange('openai');
      expect(component.addBaseUrl()).toBe('https://api.openai.com/v1');

      component.onAddCloudProviderTypeChange('azure');
      expect(component.addBaseUrl()).toBe('');
    });

    it('never replaces a URL the operator typed', () => {
      component.openAddDialog();
      component.onAddCloudProviderTypeChange('anthropic');
      component.addBaseUrl.set('https://proxy.example.org/anthropic');
      component.onAddCloudProviderTypeChange('openai');

      expect(component.addBaseUrl()).toBe('https://proxy.example.org/anthropic');
      expect(component.addBaseUrlIsSuggestion()).toBe(false);
    });

    it('drops the proposal when the provider becomes a logosnode', () => {
      component.openAddDialog();
      component.onAddCloudProviderTypeChange('anthropic');
      component.onAddProviderTypeChange('logosnode');
      expect(component.addBaseUrl()).toBe('');
    });

    it('keeps the stored URL of an existing provider when the vendor changes', () => {
      component.openEditDialog(makeProvider({ base_url: 'https://gateway.example.org/v1' }));
      component.onEditCloudProviderTypeChange('anthropic');
      expect(component.editBaseUrl()).toBe('https://gateway.example.org/v1');
    });

    it('fills an empty URL of an existing provider when the vendor changes', () => {
      component.openEditDialog(makeProvider({ base_url: null }));
      component.onEditCloudProviderTypeChange('anthropic');
      expect(component.editBaseUrl()).toBe('https://api.anthropic.com');
    });

    it('sends the proposed URL with the new provider', async () => {
      component.openAddDialog();
      component.addName.set('Anthropic');
      component.onAddCloudProviderTypeChange('anthropic');
      await component.submitAdd();

      const payload = addProvider.mock.calls[0][0] as AddProviderPayload;
      expect(payload.cloud_provider_type).toBe('anthropic');
      expect(payload.base_url).toBe('https://api.anthropic.com');
    });

    it('advertises the auth header the vendor is actually sent', () => {
      component.openAddDialog();
      expect(component.addAuthPlaceholders()).toEqual({ name: 'Authorization', format: 'Bearer {}' });

      component.onAddCloudProviderTypeChange('anthropic');
      expect(component.addAuthPlaceholders()).toEqual({ name: 'x-api-key', format: '{}' });
    });
  });

  describe('the provider type field', () => {
    it('opens on cloud for a new provider', () => {
      component.openAddDialog();
      expect(component.addProviderType()).toBe('cloud');
    });

    it('opens on the type the provider actually has', () => {
      component.openEditDialog(
        makeProvider({ provider_type: 'logosnode', privacy_level: 'LOCAL' }),
      );
      expect(component.editProviderType()).toBe('logosnode');
    });

    it('renders the dialog on the bound type, not on the first option', async () => {
      // Regression for the app-select value binding: the dropdown read
      // "logosnode" while the component state said "cloud", so the operator
      // saw a provider type that was not the one being saved.
      component.openAddDialog();
      fixture.detectChanges();
      await fixture.whenStable();

      // The dialog's content renders into a CDK overlay appended to
      // document.body, outside the fixture's own DOM subtree.
      const selects = document.querySelectorAll('select') as NodeListOf<HTMLSelectElement>;
      const typeSelect = Array.from(selects).find((s) =>
        Array.from(s.options).some((o) => o.value === 'logosnode'),
      );
      expect(typeSelect?.value).toBe('cloud');
    });
  });

  describe('the model refresh', () => {
    beforeEach(() => {
      // The first change detection (and with it ngOnInit, which fetches the
      // model list) is otherwise scheduled outside the test's control and can
      // land inside the fake-timer window, skewing the call counts.
      fixture.detectChanges();
      getModels.mockClear();
    });

    it('triggers the backend sync and stops once the pass reports done', async () => {
      vi.useFakeTimers();
      try {
        const refresh = component.refreshModels();
        await vi.advanceTimersByTimeAsync(2000);
        await refresh;

        expect(refreshModels).toHaveBeenCalledTimes(1);
        // The pass reports idle on the first poll: one refetch, no rounds.
        expect(modelSyncStatus).toHaveBeenCalledTimes(1);
        expect(getModels).toHaveBeenCalledTimes(1);
        expect(component.refreshing()).toBe(false);
      } finally {
        vi.useRealTimers();
      }
    });

    it('keeps polling while the pass is still running', async () => {
      vi.useFakeTimers();
      try {
        modelSyncStatus
          .mockResolvedValueOnce({ running: true })
          .mockResolvedValueOnce({ running: true })
          .mockResolvedValueOnce({ running: false });
        getModels
          .mockResolvedValueOnce([])
          .mockResolvedValueOnce([makeModel(1)])
          .mockResolvedValueOnce([makeModel(1), makeModel(2)]);
        const refresh = component.refreshModels();
        await vi.advanceTimersByTimeAsync(2000 * 2);
        await refresh;

        expect(modelSyncStatus).toHaveBeenCalledTimes(3);
        expect(getModels).toHaveBeenCalledTimes(3);
        expect(component.allModels().map((m) => m.id)).toEqual([1, 2]);
      } finally {
        vi.useRealTimers();
      }
    });

    it('refetches the connections of an expanded provider', async () => {
      vi.useFakeTimers();
      try {
        component.toggleExpand(component.providers()[0]!);
        const refresh = component.refreshModels();
        await vi.advanceTimersByTimeAsync(2000);
        await refresh;

        // One from the expansion itself, one from the refresh refetch.
        expect(getProviderModels).toHaveBeenCalledTimes(2);
      } finally {
        vi.useRealTimers();
      }
    });

    it('does not start a second refresh while one is running', async () => {
      vi.useFakeTimers();
      try {
        const first = component.refreshModels();
        const second = component.refreshModels();
        await vi.advanceTimersByTimeAsync(2000 * 2);
        await Promise.all([first, second]);

        expect(refreshModels).toHaveBeenCalledTimes(1);
      } finally {
        vi.useRealTimers();
      }
    });

    it('surfaces an error when the trigger fails', async () => {
      refreshModels.mockRejectedValueOnce(new Error('orchestrator unreachable'));
      await component.refreshModels();

      expect(component.refreshError()).toBe(true);
      expect(component.refreshing()).toBe(false);
      expect(modelSyncStatus).not.toHaveBeenCalled();
      expect(getModels).not.toHaveBeenCalled();
    });

    it('retries an unreadable status within the rounds cap', async () => {
      vi.useFakeTimers();
      try {
        // A failed status read is unknown, not done: the accepted sync may
        // still be writing, so the UI polls until the cap instead of
        // reporting completion on a transient failure (timeout, rolling
        // deploy).
        modelSyncStatus.mockRejectedValue(new Error('unknown endpoint'));
        const refresh = component.refreshModels();
        await vi.advanceTimersByTimeAsync(45 * 2000 + 1000);
        await refresh;

        expect(modelSyncStatus).toHaveBeenCalledTimes(45);
        expect(getModels).toHaveBeenCalledTimes(45);
        expect(component.refreshing()).toBe(false);
        expect(component.refreshError()).toBe(false);
      } finally {
        vi.useRealTimers();
      }
    });

    it('keeps polling through unreadable statuses and stops on an explicit idle', async () => {
      vi.useFakeTimers();
      try {
        modelSyncStatus
          .mockRejectedValueOnce(new Error('timeout'))
          .mockResolvedValueOnce({ running: null })
          .mockResolvedValueOnce({ running: false });
        const refresh = component.refreshModels();
        await vi.advanceTimersByTimeAsync(2000 * 2);
        await refresh;

        expect(modelSyncStatus).toHaveBeenCalledTimes(3);
        expect(getModels).toHaveBeenCalledTimes(3);
        expect(component.refreshing()).toBe(false);
      } finally {
        vi.useRealTimers();
      }
    });

    it('gives up at the rounds cap if the pass never reports done', async () => {
      vi.useFakeTimers();
      try {
        modelSyncStatus.mockResolvedValue({ running: true });
        const refresh = component.refreshModels();
        // 45 rounds x 2 s is the component cap; the margin covers the
        // round work after the last timer.
        await vi.advanceTimersByTimeAsync(45 * 2000 + 1000);
        await refresh;

        expect(component.refreshing()).toBe(false);
        expect(component.refreshError()).toBe(false);
      } finally {
        vi.useRealTimers();
      }
    });
  });
  describe('the batch support of a connected model', () => {
    const connection = (overrides: Partial<ModelConnection> = {}): ModelConnection => ({
      model_id: 92,
      model_name: 'gpt-5.6-luna',
      endpoint: 'https://res.openai.azure.com/openai/deployments/gpt-5.6-luna/responses',
      api_key_hint: null,
      ...overrides,
    });

    const badges = (): HTMLElement[] =>
      Array.from(fixture.nativeElement.querySelectorAll('[data-testid="batch-support"]'));

    it('shows for each connection whether its batches reach the provider', async () => {
      getProviderModels.mockResolvedValue([
        connection({ batch_support: 'supported', batch_deployment: 'gpt-56-luna-batch', model_id: 1 }),
        connection({ batch_support: 'unsupported', batch_detail: "SKU 'GlobalStandard'", model_id: 2 }),
        connection({ batch_support: 'unknown', model_id: 3 }),
      ]);
      component.toggleExpand(component.providers()[0]!);
      await fixture.whenStable();
      fixture.detectChanges();

      expect(badges().map((b) => b.textContent?.trim())).toEqual(['Batch', 'No batch', 'Batch unchecked']);
      expect(badges()[0].classList).toContain('batch-badge--supported');
      expect(badges()[0].getAttribute('title')).toContain('gpt-56-luna-batch');
      expect(badges()[1].classList).toContain('batch-badge--unsupported');
      expect(badges()[1].getAttribute('title')).toContain('GlobalStandard');
    });

    it('treats a connection without batch information as unchecked', () => {
      expect(component.batchLabel(connection())).toBe('Batch unchecked');
      expect(component.batchLabel(connection({ batch_support: 'logos' }))).toBe('Batch in Logos');
    });
  });

  describe('the expanded row of a logosnode', () => {
    const expand = async (provider: Provider): Promise<HTMLElement> => {
      // The first change detection starts the component's own fetch; let it finish first.
      fixture.detectChanges();
      await fixture.whenStable();
      component.providers.set([provider]);
      fixture.detectChanges();
      component.toggleExpand(provider);
      fixture.detectChanges();
      await fixture.whenStable();
      fixture.detectChanges();
      return fixture.nativeElement as HTMLElement;
    };

    it('lists the models of the node instead of connected models', async () => {
      const root = await expand(makeProvider({ id: 4, name: 'hochbruegge', provider_type: 'logosnode' }));

      expect(root.querySelector('app-node-models')).not.toBeNull();
      expect(root.textContent).toContain('Node models');
      expect(root.textContent).not.toContain('Connected Models');
    });

    it('keeps two providers open at once', async () => {
      const node = makeProvider({ id: 4, name: 'hochbruegge', provider_type: 'logosnode' });
      const cloud = makeProvider({ id: 5, name: 'azure' });
      const root = await expand(node);
      component.providers.set([node, cloud]);
      component.toggleExpand(cloud);
      fixture.detectChanges();
      await fixture.whenStable();
      fixture.detectChanges();

      expect(root.querySelector('app-node-models')).not.toBeNull();
      expect(root.textContent).toContain('Connected Models');

      component.toggleExpand(node);
      fixture.detectChanges();

      expect(root.querySelector('app-node-models')).toBeNull();
      expect(root.textContent).toContain('Connected Models');
    });

    it('keeps the connected models of a cloud provider', async () => {
      const root = await expand(makeProvider());

      expect(root.querySelector('app-node-models')).toBeNull();
      expect(root.textContent).toContain('Connected Models');
    });
  });
});
