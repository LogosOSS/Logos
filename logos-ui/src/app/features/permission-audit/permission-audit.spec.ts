import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap, Router } from '@angular/router';
import { BehaviorSubject } from 'rxjs';
import { PermissionAudit } from './permission-audit';
import { PermissionAuditService } from '../../core/services/permission-audit.service';
import { PermissionAuditEntry } from '../../shared/models/permission-audit.model';
import { routes } from '../../app.routes';
import { MENU_ITEMS } from '../../shared/constants/nav-items';
import { roleGuard } from '../../core/auth/guards/role.guard';
import { AuthService } from '../../core/auth/services/auth.service';

function entry(id: number): PermissionAuditEntry {
  return { id, occurred_at: '2026-11-12T10:00:00Z', actor_user_id: 8, actor_role: 'logos_admin',
    team_id: 42, team_name: 'Retired team', api_key_id: 9, scope_type: 'api_key', scope_id: 9,
    scope_name: 'Retired key', target_type: 'model', target_id: 3, target_name: 'example-model',
    action: 'revoke', cause: 'cascade' };
}

describe('PermissionAudit', () => {
  let list: ReturnType<typeof vi.fn>;
  let params: BehaviorSubject<ReturnType<typeof convertToParamMap>>;
  let navigate: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    list = vi.fn().mockResolvedValue([]);
    params = new BehaviorSubject(convertToParamMap({}));
    navigate = vi.fn().mockResolvedValue(false);
    TestBed.configureTestingModule({ imports: [PermissionAudit], providers: [
      { provide: PermissionAuditService, useValue: { list } },
      { provide: ActivatedRoute, useValue: { queryParamMap: params } },
      { provide: Router, useValue: { navigate } },
    ] });
  });

  async function create() {
    const fixture = TestBed.createComponent(PermissionAudit);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture;
  }

  it('opens team links with their filter and retains deleted names in the rendered history', async () => {
    params.next(convertToParamMap({ team_id: '42' }));
    list.mockResolvedValue([entry(1)]);
    const fixture = await create();
    expect(list).toHaveBeenCalledWith({ team_id: 42 }, undefined, 26);
    const text = fixture.nativeElement.textContent;
    expect(text).toContain('Retired team');
    expect(text).toContain('Retired key');
    expect(text).toContain('Cascade');
    expect(text).toContain('Revoked');
  });

  it('uses an extra row to detect older pages and returns to the previous cursor', async () => {
    list.mockResolvedValueOnce(Array.from({ length: 26 }, (_, i) => entry(100 - i)));
    const fixture = await create();
    const page = fixture.componentInstance;
    expect(page.entries()).toHaveLength(25);
    expect(page.hasMore()).toBe(true);
    list.mockResolvedValueOnce([entry(75)]);
    page.next();
    await fixture.whenStable();
    expect(list).toHaveBeenLastCalledWith({}, 76, 26);
    expect(page.pageIndex()).toBe(1);
    expect(page.hasMore()).toBe(false);
    page.previous();
    await fixture.whenStable();
    expect(list).toHaveBeenLastCalledWith({}, undefined, 26);
    expect(page.pageIndex()).toBe(0);
  });

  it('validates IDs and time ranges before sending a request', async () => {
    const page = (await create()).componentInstance;
    list.mockClear();
    page.draft.team_id = '-1';
    await page.applyFilters();
    expect(page.validationError()).toContain('positive');
    page.draft.team_id = '42';
    page.draft.from = '2026-11-13T10:00';
    page.draft.to = '2026-11-12T10:00';
    await page.applyFilters();
    expect(page.validationError()).toContain('after');
    expect(list).not.toHaveBeenCalled();
  });

  it('converts local dates to ISO timestamps and resets paging on a filter change', async () => {
    const page = (await create()).componentInstance;
    page.draft.team_id = '42';
    page.draft.from = '2026-11-12T09:00';
    page.draft.to = '2026-11-12T10:00';
    await page.applyFilters();
    expect(list).toHaveBeenLastCalledWith({ team_id: 42,
      from: new Date('2026-11-12T09:00').toISOString(), to: new Date('2026-11-12T10:00').toISOString() }, undefined, 26);
    expect(navigate).toHaveBeenCalled();
    expect(page.pageIndex()).toBe(0);
  });

  it('discards an earlier response after the filters change', async () => {
    let resolveOld!: (rows: PermissionAuditEntry[]) => void;
    list.mockReturnValueOnce(new Promise<PermissionAuditEntry[]>(resolve => { resolveOld = resolve; }));
    const fixture = TestBed.createComponent(PermissionAudit);
    list.mockResolvedValueOnce([entry(2)]);
    params.next(convertToParamMap({ team_id: '42' }));
    await fixture.whenStable();
    resolveOld([entry(1)]);
    await fixture.whenStable();
    expect(fixture.componentInstance.entries().map(row => row.id)).toEqual([2]);
  });

  it('shows a retryable error rather than claiming the history is empty', async () => {
    list.mockRejectedValueOnce(new Error('offline'));
    const fixture = await create();
    expect(fixture.nativeElement.textContent).toContain('Could not load permission history');
    expect(fixture.nativeElement.textContent).not.toContain('No permission changes match');
    list.mockResolvedValueOnce([entry(4)]);
    fixture.componentInstance.retry();
    await fixture.whenStable();
    expect(fixture.componentInstance.error()).toBe('');
    expect(fixture.componentInstance.entries()).toHaveLength(1);
  });

  it('does not page through the previous team after a failed filter change', async () => {
    params.next(convertToParamMap({ team_id: '1' }));
    list.mockResolvedValueOnce(Array.from({ length: 26 }, (_, i) => entry(100 - i)));
    const fixture = await create();
    const page = fixture.componentInstance;
    list.mockResolvedValueOnce(Array.from({ length: 26 }, (_, i) => entry(74 - i)));
    page.next();
    await fixture.whenStable();
    expect(page.pageIndex()).toBe(1);

    list.mockRejectedValueOnce(new Error('offline'));
    page.draft.team_id = '2';
    await page.applyFilters();
    fixture.detectChanges();
    expect(page.error()).not.toBe('');

    const buttons = Array.from(fixture.nativeElement.querySelectorAll('button')) as HTMLButtonElement[];
    const newer = buttons.find(b => b.textContent?.trim() === 'Newer')!;
    const older = buttons.find(b => b.textContent?.trim() === 'Older')!;
    expect(newer.disabled).toBe(true);
    expect(older.disabled).toBe(true);

    list.mockClear();
    page.previous();
    page.next();
    await fixture.whenStable();
    expect(list).not.toHaveBeenCalled();

    list.mockResolvedValueOnce([entry(5)]);
    page.retry();
    await fixture.whenStable();
    expect(list).toHaveBeenLastCalledWith({ team_id: 2 }, undefined, 26);
    expect(page.error()).toBe('');
  });

  it('clears target filters when following a different kind of target', async () => {
    const fixture = await create();
    const page = fixture.componentInstance;
    page.draft.model_id = '3';
    page.filterBy('provider_id', 7);
    await fixture.whenStable();
    expect(list).toHaveBeenLastCalledWith({ provider_id: 7 }, undefined, 26);
    page.reset();
    await fixture.whenStable();
    expect(list).toHaveBeenLastCalledWith({}, undefined, 26);
  });
});

describe('Permission audit access', () => {
  // Found by path rather than position: public routes may precede the shell.
  const auditRoute = routes
    .flatMap(route => route.children ?? [])
    .find(route => route.path === 'permission-audit-log')!;
  it('exposes navigation and the route only to Logos admins', () => {
    expect(MENU_ITEMS.find(item => item.path === '/permission-audit-log')?.roles).toEqual(['logos_admin']);
    expect(auditRoute.data?.['roles']).toEqual(['logos_admin']);
    expect(auditRoute.canActivate).toContain(roleGuard);
  });
  for (const role of ['app_admin', 'app_developer', 'logos_admin']) {
    it(`enforces the route for ${role}`, () => {
      TestBed.configureTestingModule({ providers: [
        { provide: AuthService, useValue: { status: signal('authenticated'), role: signal(role) } },
        { provide: Router, useValue: { parseUrl: (url: string) => url } },
      ] });
      const result = TestBed.runInInjectionContext(() => roleGuard({ data: auditRoute.data } as never, {} as never));
      expect(result === true).toBe(role === 'logos_admin');
    });
  }
});
