import { signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap, Router } from '@angular/router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthService } from '../../core/auth/services/auth.service';
import { TeamManagementService } from '../../core/services/team-management.service';
import { TeamDetail as TeamDetailModel } from '../../shared/models/team.model';
import { TeamDetail } from './team-detail';

describe('TeamDetail', () => {
  let component: TeamDetail;
  let fixture: ComponentFixture<TeamDetail>;
  let getTeamWithMembers: ReturnType<typeof vi.fn>;
  let getDeprecatedModelWarnings: ReturnType<typeof vi.fn>;

  beforeEach(async () => {
    getTeamWithMembers = vi.fn().mockRejectedValue(new Error('offline'));
    getDeprecatedModelWarnings = vi.fn().mockResolvedValue([]);
    await TestBed.configureTestingModule({
      imports: [TeamDetail],
      providers: [
        { provide: ActivatedRoute, useValue: { snapshot: { paramMap: convertToParamMap({ id: '5' }) } } },
        { provide: Router, useValue: {} },
        { provide: AuthService, useValue: { currentUser: signal(null) } },
        { provide: TeamManagementService, useValue: {
          getTeamWithMembers, getDeprecatedModelWarnings,
          getTeamApiKeys: vi.fn().mockResolvedValue([]),
          getTeamModelPermissions: vi.fn().mockResolvedValue([]),
        } },
      ],
    })
      .overrideComponent(TeamDetail, { set: { template: '' } })
      .compileComponents();
    fixture = TestBed.createComponent(TeamDetail);
    component = fixture.componentInstance;
  });

  it('loads the team named in the route on start', async () => {
    component.ngOnInit();
    await vi.waitFor(() => expect(getTeamWithMembers).toHaveBeenCalledWith(5));
  });

  it('keeps saved public stats choices in the team model for later tab visits', () => {
    component.team.set({
      id: 5, name: 'Research team', show_on_public_stats: false, public_category: null,
    } as TeamDetailModel);
    component.applyPublicStats({ show_on_public_stats: true, public_category: 'Research' });
    expect(component.team()).toMatchObject({
      id: 5, name: 'Research team', show_on_public_stats: true, public_category: 'Research',
    });
    expect(getTeamWithMembers).not.toHaveBeenCalled();
  });

  it('loads warnings only after confirming ownership', async () => {
    getTeamWithMembers.mockResolvedValue({ team: { id: 5, is_caller_owner: false }, members: [] });
    component.ngOnInit();
    await vi.waitFor(() => expect(component.loading()).toBe(false));
    expect(getDeprecatedModelWarnings).not.toHaveBeenCalled();

    const warnings = [{ model_id: 2, model_name: 'deprecated', last_used_at: '2026-11-24T00:00:00Z' }];
    getTeamWithMembers.mockResolvedValue({ team: { id: 5, is_caller_owner: true }, members: [] });
    getDeprecatedModelWarnings.mockResolvedValue(warnings);
    component.refresh();
    await vi.waitFor(() => expect(component.deprecatedModelWarnings()).toEqual(warnings));
    expect(getDeprecatedModelWarnings).toHaveBeenCalledExactlyOnceWith(5);
  });

  it('does not restore warnings from a superseded refresh', async () => {
    getTeamWithMembers.mockResolvedValue({ team: { id: 5, is_caller_owner: true }, members: [] });
    const warnings = [{ model_id: 2, model_name: 'deprecated', last_used_at: '2026-11-24T00:00:00Z' }];
    let settleOlder = () => {};
    getDeprecatedModelWarnings.mockReturnValueOnce(new Promise(resolve => {
      settleOlder = () => resolve(warnings);
    }));
    fixture.detectChanges();
    await vi.waitFor(() => expect(getDeprecatedModelWarnings).toHaveBeenCalledTimes(1));
    component.refresh();
    await vi.waitFor(() => expect(getDeprecatedModelWarnings).toHaveBeenCalledTimes(2));
    settleOlder();
    await Promise.resolve();
    expect(component.deprecatedModelWarnings()).toEqual([]);
  });

  it('reloads the same team on refresh', async () => {
    component.ngOnInit();
    component.refresh();
    await vi.waitFor(() => expect(getTeamWithMembers).toHaveBeenCalledTimes(2));
    expect(getTeamWithMembers).toHaveBeenLastCalledWith(5);
  });
});
