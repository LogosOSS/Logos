import { beforeEach, describe, expect, it, vi } from 'vitest';

import { TeamApiKey } from '../../../shared/models/team.model';
import { KeyPermissionExpansion, KeyPermissionSource } from './key-permission-expansion';

type MockSource = { [K in keyof KeyPermissionSource]: ReturnType<typeof vi.fn> };

function key(id: number, useCustom = false): TeamApiKey {
  return { id, use_custom_permissions: useCustom } as TeamApiKey;
}

describe('KeyPermissionExpansion', () => {
  let source: MockSource;
  let expansion: KeyPermissionExpansion;

  beforeEach(() => {
    source = {
      getAllProviders: vi.fn().mockResolvedValue([
        { id: 1, name: 'Azure', provider_type: 'azure' },
        { id: 2, name: 'Node', provider_type: 'logosnode' },
        { id: 3, name: 'Broken', provider_type: 'openai' },
      ]),
      getTeamProviderPermissions: vi.fn().mockResolvedValue([1, 2]),
      getTeamModelPermissions: vi.fn().mockResolvedValue([10]),
      getProviderModels: vi.fn().mockImplementation((id: number) => {
        if (id === 3) return Promise.reject(new Error('down'));
        if (id === 2) return Promise.resolve(null);
        return Promise.resolve([
          { model_id: 10, model_name: 'gpt' },
          { model_id: 11, model_name: 'llama' },
        ]);
      }),
      getApiKeyProviderPermissions: vi.fn().mockResolvedValue([1]),
      getApiKeyModelPermissions: vi.fn().mockResolvedValue([11]),
    };
    expansion = new KeyPermissionExpansion(source as unknown as KeyPermissionSource, () => 7);
  });

  it('loads the team catalogue on the first expand and shows the team permissions', async () => {
    const k = key(1);
    expansion.toggleExpand(k);
    expect(expansion.isExpanded(1)).toBe(true);

    await vi.waitFor(() => expect(expansion.globalDataLoaded).toBe(true));
    expect(source.getTeamProviderPermissions).toHaveBeenCalledWith(7);
    expect(source.getApiKeyProviderPermissions).not.toHaveBeenCalled();
    expect(expansion.allProviders().map((p) => p.isCloud)).toEqual([true, false, true]);
    expect(expansion.getDisplayProviders(k).map((p) => p.id)).toEqual([1, 2]);
    expect(expansion.getDisplayModels(k)).toEqual([{ id: 10, name: 'gpt' }]);
    expect(expansion.getModelGroups(k).map((g) => g.model_name)).toEqual(['gpt']);
  });

  it('collapses an expanded key without loading again', async () => {
    const k = key(1);
    expansion.toggleExpand(k);
    await vi.waitFor(() => expect(expansion.globalDataLoaded).toBe(true));
    expansion.toggleExpand(k);
    expect(expansion.isExpanded(1)).toBe(false);
    expansion.toggleExpand(k);
    expect(source.getAllProviders).toHaveBeenCalledTimes(1);
  });

  it('loads custom key permissions after the catalogue for the key that triggered it', async () => {
    const k = key(5, true);
    expansion.toggleExpand(k);
    await vi.waitFor(() => expect(expansion.getDisplayModels(k)).toEqual([{ id: 11, name: 'llama' }]));
    expect(expansion.getDisplayProviders(k).map((p) => p.id)).toEqual([1]);
    expect(expansion.isLoadingExpand(5)).toBe(false);
  });

  it('loads custom key permissions lazily once the catalogue is present', async () => {
    expansion.toggleExpand(key(1));
    await vi.waitFor(() => expect(expansion.globalDataLoaded).toBe(true));

    const custom = key(6, true);
    expansion.toggleExpand(custom);
    expect(expansion.isLoadingExpand(6)).toBe(true);
    await vi.waitFor(() => expect(expansion.isLoadingExpand(6)).toBe(false));
    expect(source.getApiKeyModelPermissions).toHaveBeenCalledWith(6);
  });

  it('shows no custom permissions when they fail to load', async () => {
    source.getApiKeyProviderPermissions.mockRejectedValue(new Error('forbidden'));
    const k = key(5, true);
    expansion.toggleExpand(k);
    await vi.waitFor(() => expect(source.getApiKeyProviderPermissions).toHaveBeenCalled());
    await vi.waitFor(() => expect(expansion.isLoadingExpand(5)).toBe(false));
    expect(expansion.getDisplayProviders(k)).toEqual([]);
    expect(expansion.getDisplayModels(k)).toEqual([]);
  });

  it('does not start a second catalogue load while one is in flight', () => {
    expansion.toggleExpand(key(1));
    expansion.toggleExpand(key(2));
    expect(source.getAllProviders).toHaveBeenCalledTimes(1);
  });

  it('retries the catalogue on the next expand after a failed load', async () => {
    source.getAllProviders.mockRejectedValueOnce(new Error('offline'));
    const k = key(1);
    expansion.toggleExpand(k);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(expansion.globalDataLoaded).toBe(false);

    expansion.toggleExpand(k);
    expansion.toggleExpand(k);
    await vi.waitFor(() => expect(expansion.globalDataLoaded).toBe(true));
    expect(source.getAllProviders).toHaveBeenCalledTimes(2);
  });
});
