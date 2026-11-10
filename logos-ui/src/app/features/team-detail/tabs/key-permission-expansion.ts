// Expand-row state for the key-permissions view shared by the members and
// application-keys tabs: which keys are expanded, the team-wide provider and
// model catalogue (loaded once, on the first expand) and the per-key custom
// permissions (loaded lazily, per key).

import { signal } from '@angular/core';
import { TeamApiKey } from '../../../shared/models/team.model';
import { TeamManagementService } from '../../../core/services/team-management.service';
import { buildKeyModelGroups, KeyModelGroup, ProviderInfo } from './key-model-groups';

export type KeyPermissionSource = Pick<
  TeamManagementService,
  | 'getAllProviders'
  | 'getTeamProviderPermissions'
  | 'getTeamModelPermissions'
  | 'getProviderModels'
  | 'getApiKeyProviderPermissions'
  | 'getApiKeyModelPermissions'
>;

export class KeyPermissionExpansion {
  readonly expandedKeyIds = signal<Set<number>>(new Set());
  readonly loadingKeyIds = signal<Set<number>>(new Set());

  globalDataLoaded = false;
  private globalDataLoading = false;
  private readonly keyPermCache = new Map<number, { providerIds: Set<number>; modelIds: Set<number> }>();

  readonly allProviders = signal<ProviderInfo[]>([]);
  readonly allModels = signal<{ id: number; name: string }[]>([]);
  readonly teamProviderIds = signal<Set<number>>(new Set());
  readonly teamModelIds = signal<Set<number>>(new Set());
  // providerId → set of model ids that provider serves
  private readonly providerModelMap = new Map<number, Set<number>>();

  constructor(
    private readonly source: KeyPermissionSource,
    private readonly teamId: () => number,
  ) {}

  isExpanded(keyId: number): boolean {
    return this.expandedKeyIds().has(keyId);
  }

  isLoadingExpand(keyId: number): boolean {
    return this.loadingKeyIds().has(keyId);
  }

  toggleExpand(key: TeamApiKey): void {
    const next = new Set(this.expandedKeyIds());
    if (next.has(key.id)) {
      next.delete(key.id);
      this.expandedKeyIds.set(next);
      return;
    }
    next.add(key.id);
    this.expandedKeyIds.set(next);
    if (!this.globalDataLoaded) {
      // Both loaders handle their own failures, so their promises never reject.
      if (!this.globalDataLoading) void this.loadGlobalData(key);
    } else if (key.use_custom_permissions && !this.keyPermCache.has(key.id)) {
      void this.loadKeyPerms(key.id);
    }
  }

  getDisplayProviders(key: TeamApiKey): ProviderInfo[] {
    const ids = key.use_custom_permissions
      ? (this.keyPermCache.get(key.id)?.providerIds ?? new Set<number>())
      : this.teamProviderIds();
    return this.allProviders().filter((p) => ids.has(p.id));
  }

  getDisplayModels(key: TeamApiKey): { id: number; name: string }[] {
    const ids = key.use_custom_permissions
      ? (this.keyPermCache.get(key.id)?.modelIds ?? new Set<number>())
      : this.teamModelIds();
    return this.allModels().filter((m) => ids.has(m.id));
  }

  getModelGroups(key: TeamApiKey): KeyModelGroup[] {
    return buildKeyModelGroups(
      this.getDisplayProviders(key),
      this.getDisplayModels(key),
      this.providerModelMap,
    );
  }

  private async loadGlobalData(pending: TeamApiKey): Promise<void> {
    this.globalDataLoading = true;
    try {
      const [providers, teamProviders, teamModels] = await Promise.all([
        this.source.getAllProviders(),
        this.source.getTeamProviderPermissions(this.teamId()),
        this.source.getTeamModelPermissions(this.teamId()),
      ]);

      this.allProviders.set(
        providers.map((p) => ({ id: p.id, name: p.name, isCloud: p.provider_type !== 'logosnode' })),
      );
      this.teamProviderIds.set(new Set(teamProviders));
      this.teamModelIds.set(new Set(teamModels));

      const modelById = new Map<number, string>();
      await Promise.all(
        providers.map(async (p) => {
          try {
            const ms = await this.source.getProviderModels(p.id);
            this.providerModelMap.set(p.id, new Set((ms ?? []).map((m) => m.model_id)));
            for (const m of ms ?? [])
              if (!modelById.has(m.model_id)) modelById.set(m.model_id, m.model_name);
          } catch {
            this.providerModelMap.set(p.id, new Set());
          }
        }),
      );
      this.allModels.set([...modelById.entries()].map(([id, name]) => ({ id, name })));
      this.globalDataLoaded = true;

      if (pending.use_custom_permissions && !this.keyPermCache.has(pending.id)) {
        await this.loadKeyPerms(pending.id);
      }
    } catch {
      // leave the catalogue unloaded so the next expand retries
    } finally {
      this.globalDataLoading = false;
    }
  }

  private async loadKeyPerms(keyId: number): Promise<void> {
    const l = new Set(this.loadingKeyIds());
    l.add(keyId);
    this.loadingKeyIds.set(l);
    try {
      const [providerIds, modelIds] = await Promise.all([
        this.source.getApiKeyProviderPermissions(keyId),
        this.source.getApiKeyModelPermissions(keyId),
      ]);
      this.keyPermCache.set(keyId, {
        providerIds: new Set(providerIds),
        modelIds: new Set(modelIds),
      });
    } catch {
      // leave cache empty for this key
    } finally {
      const l2 = new Set(this.loadingKeyIds());
      l2.delete(keyId);
      this.loadingKeyIds.set(l2);
    }
  }
}
