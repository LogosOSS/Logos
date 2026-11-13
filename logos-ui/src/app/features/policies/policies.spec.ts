import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthService } from '../../core/auth/services/auth.service';
import { PolicyService } from '../../core/services/policy.service';
import { TeamManagementService } from '../../core/services/team-management.service';
import { Policy } from '../../shared/models/policy.model';
import { Policies } from './policies';

describe('Policies', () => {
  let component: Policies;
  let getPolicies: ReturnType<typeof vi.fn>;
  let getTeams: ReturnType<typeof vi.fn>;

  beforeEach(async () => {
    getPolicies = vi.fn().mockResolvedValue([]);
    getTeams = vi.fn().mockResolvedValue([]);
    await TestBed.configureTestingModule({
      imports: [Policies],
      providers: [
        { provide: AuthService, useValue: { currentUser: signal(null) } },
        { provide: PolicyService, useValue: { getPolicies } },
        { provide: TeamManagementService, useValue: { getTeams, getTeamApiKeys: vi.fn().mockResolvedValue([]) } },
      ],
    })
      .overrideComponent(Policies, { set: { template: '' } })
      .compileComponents();
    component = TestBed.createComponent(Policies).componentInstance;
  });

  it('loads the policies on start', async () => {
    component.ngOnInit();
    await vi.waitFor(() => expect(getPolicies).toHaveBeenCalled());
  });

  it('loads the teams and keys to choose from when the add dialog opens', async () => {
    component.openAddDialog();
    await vi.waitFor(() => expect(getTeams).toHaveBeenCalled());
  });

  it('loads the teams and keys to choose from when the edit dialog opens', async () => {
    component.openEditDialog({ name: 'p', threshold_privacy: 'LOCAL', threshold_latency: 0, threshold_accuracy: 0, threshold_cost: 0, threshold_quality: 0, priority: 0, topic: null, api_key_id: null, team_id: null } as unknown as Policy);
    await vi.waitFor(() => expect(getTeams).toHaveBeenCalled());
  });
});
