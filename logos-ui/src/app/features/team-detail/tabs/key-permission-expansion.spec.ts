import { beforeEach, describe, expect, it, vi } from 'vitest';

import { TeamApiKey } from '../../../shared/models/team.model';
import { KeyPermissionExpansion } from './key-permission-expansion';

function key(id: number, useCustom = false): TeamApiKey {
  return { id, use_custom_permissions: useCustom } as TeamApiKey;
}

describe('KeyPermissionExpansion', () => {
  const source = { getKeyEffectiveModelDeployments: vi.fn() };
  let expansion: KeyPermissionExpansion;

  beforeEach(() => {
    source.getKeyEffectiveModelDeployments.mockReset().mockImplementation((id: number) =>
      Promise.resolve(id === 1 ? [
        { model_id: 10, model_name: 'synced-azure', provider_id: 1, provider_name: 'Azure', provider_type: 'cloud' },
        { model_id: 20, model_name: 'manual', provider_id: 3, provider_name: 'Node', provider_type: 'logosnode' },
        { model_id: 20, model_name: 'manual', provider_id: 2, provider_name: 'Other', provider_type: 'cloud' },
      ] : []),
    );
    expansion = new KeyPermissionExpansion(source);
  });

  it('shows automatic-only defaults on the exact provider without inheriting them on custom keys', async () => {
    const defaults = key(1);
    const custom = key(2, true);
    expansion.toggleExpand(defaults);
    expansion.toggleExpand(custom);
    expect(expansion.isLoadingExpand(1)).toBe(true);
    await vi.waitFor(() => expect(expansion.isLoadingExpand(1)).toBe(false));

    expect(source.getKeyEffectiveModelDeployments.mock.calls).toEqual([[1], [2]]);
    expect(expansion.getModelGroups(defaults)).toEqual([
      { model_name: 'manual', providers: ['Node', 'Other'], hasCloud: true, hasLocal: true },
      { model_name: 'synced-azure', providers: ['Azure'], hasCloud: true, hasLocal: false },
    ]);
    expect(expansion.getModelGroups(custom)).toEqual([]);
  });

  it('refreshes access on reopening after an opt-out', async () => {
    const k = key(1);
    expansion.toggleExpand(k);
    await vi.waitFor(() => expect(expansion.getModelGroups(k)).toHaveLength(2));
    expansion.toggleExpand(k);
    expect(expansion.isExpanded(1)).toBe(false);
    expect(source.getKeyEffectiveModelDeployments).toHaveBeenCalledTimes(1);

    source.getKeyEffectiveModelDeployments.mockResolvedValueOnce([]);
    expansion.toggleExpand(k);
    await vi.waitFor(() => expect(expansion.getModelGroups(k)).toEqual([]));
    expect(source.getKeyEffectiveModelDeployments).toHaveBeenCalledTimes(2);
  });

  it('distinguishes failed loading from empty access and retries on reopen', async () => {
    const k = key(1);
    source.getKeyEffectiveModelDeployments.mockRejectedValueOnce(new Error('offline'));
    expansion.toggleExpand(k);
    await vi.waitFor(() => expect(expansion.hasExpandError(1)).toBe(true));
    expect(expansion.isLoadingExpand(1)).toBe(false);

    expansion.toggleExpand(k);
    expansion.toggleExpand(k);
    await vi.waitFor(() => expect(expansion.getModelGroups(k)).toHaveLength(2));
    expect(expansion.hasExpandError(1)).toBe(false);
  });
});
