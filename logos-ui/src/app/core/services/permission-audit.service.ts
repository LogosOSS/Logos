import { inject, Injectable } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { PermissionAuditEntry, PermissionAuditFilters } from '../../shared/models/permission-audit.model';

@Injectable({ providedIn: 'root' })
export class PermissionAuditService {
  private readonly http = inject(HttpClient);

  list(filters: PermissionAuditFilters, beforeId?: number, limit = 26): Promise<PermissionAuditEntry[]> {
    let params = new HttpParams().set('limit', limit);
    for (const [key, value] of Object.entries(filters)) {
      if (value !== undefined) params = params.set(key, value);
    }
    if (beforeId !== undefined) params = params.set('before_id', beforeId);
    return firstValueFrom(this.http.get<PermissionAuditEntry[]>('/api/admin/permission-audit-log', { params }));
  }
}
