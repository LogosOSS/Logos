import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthService } from '../../../../core/auth/services/auth.service';
import { TeamManagementService } from '../../../../core/services/team-management.service';
import { MembersTabComponent } from './members-tab';

describe('MembersTabComponent', () => {
  let component: MembersTabComponent;
  let teamService: { getAdminUsers: ReturnType<typeof vi.fn>; getAllUsers: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    teamService = { getAdminUsers: vi.fn().mockResolvedValue([]), getAllUsers: vi.fn().mockResolvedValue([]) };
    await TestBed.configureTestingModule({
      imports: [MembersTabComponent],
      providers: [
        { provide: TeamManagementService, useValue: teamService },
        { provide: AuthService, useValue: { currentUser: signal(null) } },
      ],
    })
      .overrideComponent(MembersTabComponent, { set: { template: '' } })
      .compileComponents();
    component = TestBed.createComponent(MembersTabComponent).componentInstance;
  });

  it('loads the candidate owners when the add-owner dialog opens', async () => {
    component.openAddOwner();
    await vi.waitFor(() => expect(teamService.getAdminUsers).toHaveBeenCalled());
  });

  it('loads the candidate members when the add-member dialog opens', async () => {
    component.openAddMember();
    await vi.waitFor(() => expect(teamService.getAllUsers).toHaveBeenCalled());
  });
});
