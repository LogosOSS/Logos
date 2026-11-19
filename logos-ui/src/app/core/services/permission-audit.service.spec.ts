import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { PermissionAuditService } from './permission-audit.service';

describe('PermissionAuditService', () => {
  it('sends filters and the exclusive cursor without empty parameters', async () => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
    const http = TestBed.inject(HttpTestingController);
    const promise = TestBed.inject(PermissionAuditService).list({ team_id: 42, api_key_id: 9,
      model_id: 3, from: '2026-11-12T10:00:00Z', provider_id: undefined }, 99);
    const request = http.expectOne(req => req.url === '/api/admin/permission-audit-log');
    expect(request.request.method).toBe('GET');
    expect(request.request.params.get('team_id')).toBe('42');
    expect(request.request.params.get('api_key_id')).toBe('9');
    expect(request.request.params.get('model_id')).toBe('3');
    expect(request.request.params.get('from')).toBe('2026-11-12T10:00:00Z');
    expect(request.request.params.get('before_id')).toBe('99');
    expect(request.request.params.get('limit')).toBe('26');
    expect(request.request.params.has('provider_id')).toBe(false);
    request.flush([]);
    expect(await promise).toEqual([]);
    http.verify();
  });
});
