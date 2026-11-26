/**
 * Response of GET /api/admin/models/{id}/access — the read-only access
 * matrix of a single model. Manual model and hosting-provider grants are
 * distinct from effective access, which can also be granted automatically.
 */
export interface ModelAccessModel {
  id: number;
  name: string;
  description: string | null;
  tags: string | null;
  aliases: string[];
  input_usd_per_million: number | null;
  output_usd_per_million: number | null;
  max_input_tokens: number | null;
  last_used_at: string | null;
}

export interface HostingProvider {
  provider_id: number;
  name: string;
  provider_type: string;
  privacy_level: string;
  request_count: number;
  last_request_at: string | null;
}

export interface ProviderGrant {
  provider_id: number;
  granted: boolean;
}

export interface TeamAccess {
  team_id: number;
  team_name: string;
  model_grant: boolean;
  provider_grants: ProviderGrant[];
  automatic_provider_ids: number[];
  effective_access: boolean;
}

export interface KeyAccess {
  key_id: number;
  key_name: string;
  is_active: boolean;
  team_id: number | null;
  team_name: string | null;
  model_grant: boolean;
  provider_grants: ProviderGrant[];
  effective_access: boolean;
}

export interface ModelAccessResponse {
  model: ModelAccessModel;
  providers: HostingProvider[];
  teams: TeamAccess[];
  api_keys: KeyAccess[];
}
