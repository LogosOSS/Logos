import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap, Router } from '@angular/router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthService } from '../../core/auth/services/auth.service';
import { TeamManagementService } from '../../core/services/team-management.service';
import { TeamDetail as TeamDetailModel } from '../../shared/models/team.model';
import { TeamDetail } from './team-detail';

describe('TeamDetail', () => {
  let component: TeamDetail;
  let getTeamWithMembers: ReturnType<typeof vi.fn>;

  beforeEach(async () => {
    getTeamWithMembers = vi.fn().mockRejectedValue(new Error('offline'));
    await TestBed.configureTestingModule({
      imports: [TeamDetail],
      providers: [
        { provide: ActivatedRoute, useValue: { snapshot: { paramMap: convertToParamMap({ id: '5' }) } } },
        { provide: Router, useValue: {} },
        { provide: AuthService, useValue: { currentUser: signal(null) } },
        { provide: TeamManagementService, useValue: { getTeamWithMembers } },
      ],
    })
      .overrideComponent(TeamDetail, { set: { template: '' } })
      .compileComponents();
    component = TestBed.createComponent(TeamDetail).componentInstance;
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

  it('reloads the same team on refresh', async () => {
    component.ngOnInit();
    component.refresh();
    await vi.waitFor(() => expect(getTeamWithMembers).toHaveBeenCalledTimes(2));
    expect(getTeamWithMembers).toHaveBeenLastCalledWith(5);
  });
});
