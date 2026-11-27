import { EffectiveModelDeployment } from '../../../shared/models/team.model';

// Shared conditions and grouping for the member and application key expand rows.
export interface KeyModelGroup {
  model_name: string;
  providers: string[];
  hasCloud: boolean;
  hasLocal: boolean;
}

/** Group the exact model/provider pairs returned by the server; never rebuild a cross-product. */
export function buildKeyModelGroups(deployments: EffectiveModelDeployment[]): KeyModelGroup[] {
  const byModel = new Map<number, KeyModelGroup>();
  for (const deployment of deployments) {
    let group = byModel.get(deployment.model_id);
    if (!group) {
      group = { model_name: deployment.model_name, providers: [], hasCloud: false, hasLocal: false };
      byModel.set(deployment.model_id, group);
    }
    group.providers.push(deployment.provider_name);
    group.hasCloud ||= deployment.provider_type === 'cloud';
    group.hasLocal ||= deployment.provider_type === 'logosnode';
  }
  const groups = [...byModel.values()];
  for (const group of groups) group.providers.sort((a, b) => a.localeCompare(b));
  return groups.sort((a, b) => {
    if (a.hasCloud !== b.hasCloud) return a.hasCloud ? -1 : 1;
    return a.model_name.localeCompare(b.model_name);
  });
}
