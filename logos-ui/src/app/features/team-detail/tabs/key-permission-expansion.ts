import { signal } from '@angular/core';
import { TeamApiKey } from '../../../shared/models/team.model';
import { TeamManagementService } from '../../../core/services/team-management.service';
import { buildKeyModelGroups, KeyModelGroup } from './key-model-groups';

export type KeyPermissionSource = Pick<TeamManagementService, 'getKeyEffectiveModelDeployments'>;

export class KeyPermissionExpansion {
  readonly expandedKeyIds = signal<Set<number>>(new Set());
  private readonly groups = signal<Map<number, KeyModelGroup[]>>(new Map());
  private readonly loading = signal<Set<number>>(new Set());
  private readonly failed = signal<Set<number>>(new Set());

  constructor(private readonly source: KeyPermissionSource) {}

  isExpanded(keyId: number): boolean {
    return this.expandedKeyIds().has(keyId);
  }

  isLoadingExpand(keyId: number): boolean {
    return this.loading().has(keyId);
  }

  hasExpandError(keyId: number): boolean {
    return this.failed().has(keyId);
  }

  toggleExpand(key: TeamApiKey): void {
    const next = new Set(this.expandedKeyIds());
    if (next.delete(key.id)) {
      this.expandedKeyIds.set(next);
      return;
    }
    next.add(key.id);
    this.expandedKeyIds.set(next);
    // Re-opening reflects grants, opt-ins, and key status as they are now.
    if (!this.isLoadingExpand(key.id)) void this.load(key.id);
  }

  getModelGroups(key: TeamApiKey): KeyModelGroup[] {
    return this.groups().get(key.id) ?? [];
  }

  private async load(keyId: number): Promise<void> {
    this.loading.update((ids) => new Set(ids).add(keyId));
    this.failed.update((ids) => without(ids, keyId));
    try {
      const deployments = await this.source.getKeyEffectiveModelDeployments(keyId);
      this.groups.update((rows) => new Map(rows).set(keyId, buildKeyModelGroups(deployments)));
    } catch {
      this.failed.update((ids) => new Set(ids).add(keyId));
    } finally {
      this.loading.update((ids) => without(ids, keyId));
    }
  }
}

function without(ids: Set<number>, keyId: number): Set<number> {
  const next = new Set(ids);
  next.delete(keyId);
  return next;
}
