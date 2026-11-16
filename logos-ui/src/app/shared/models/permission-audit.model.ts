export interface PermissionAuditEntry {
  id: number;
  occurred_at: string;
  actor_user_id: number | null;
  actor_role: string;
  team_id: number | null;
  team_name: string | null;
  api_key_id: number | null;
  scope_type: 'team' | 'api_key';
  scope_id: number;
  scope_name: string | null;
  target_type: 'model' | 'provider';
  target_id: number;
  target_name: string | null;
  action: 'grant' | 'revoke';
  cause: 'direct' | 'cascade' | 'migration' | 'repair';
}

export interface PermissionAuditFilters {
  team_id?: number;
  api_key_id?: number;
  model_id?: number;
  provider_id?: number;
  from?: string;
  to?: string;
}
