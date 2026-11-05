import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthService } from '../../../core/auth/services/auth.service';
import { TeamManagementService } from '../../../core/services/team-management.service';
import { TeamApiKey } from '../../../shared/models/team.model';
import { AppKeysTabComponent } from './app-keys/app-keys-tab';
import { MembersTabComponent } from './members/members-tab';

/** Clicking a key row expands its permissions unless the click hit a control. */
describe('key permission rows', () => {
  let teamService: Record<string, ReturnType<typeof vi.fn>>;

  const rowClick = (target: HTMLElement): Event => ({ target }) as unknown as Event;
  const plainCell = () => document.createElement('div');
  const button = () => document.createElement('button');

  beforeEach(() => {
    teamService = {
      getAllProviders: vi.fn().mockResolvedValue([]),
      getTeamProviderPermissions: vi.fn().mockResolvedValue([]),
      getTeamModelPermissions: vi.fn().mockResolvedValue([]),
      getProviderModels: vi.fn().mockResolvedValue([]),
      getApiKeyProviderPermissions: vi.fn().mockResolvedValue([]),
      getApiKeyModelPermissions: vi.fn().mockResolvedValue([]),
    };
    TestBed.configureTestingModule({
      providers: [
        { provide: TeamManagementService, useValue: teamService },
        { provide: AuthService, useValue: { currentUser: signal(null) } },
      ],
    });
  });

  it('expands an application key and loads the catalogue of its team', async () => {
    const component = TestBed.runInInjectionContext(() => new AppKeysTabComponent());
    component.teamId = 7;
    const key = { id: 3, key_type: 'application' } as TeamApiKey;

    component.onRowClick(rowClick(button()), key);
    expect(component.keyPermissions.isExpanded(3)).toBe(false);

    component.onRowClick(rowClick(plainCell()), key);
    expect(component.keyPermissions.isExpanded(3)).toBe(true);
    await vi.waitFor(() => expect(teamService['getTeamModelPermissions']).toHaveBeenCalledWith(7));
  });

  it('expands the developer key of a member and ignores members without one', async () => {
    const component = TestBed.runInInjectionContext(() => new MembersTabComponent());
    component.teamId = 9;
    component.apiKeys = [{ id: 4, user_id: 21, key_type: 'developer' } as TeamApiKey];

    component.onRowClick(rowClick(plainCell()), 22);
    expect(component.keyPermissions.expandedKeyIds().size).toBe(0);

    component.onRowClick(rowClick(button()), 21);
    expect(component.keyPermissions.isExpanded(4)).toBe(false);

    component.onRowClick(rowClick(plainCell()), 21);
    expect(component.keyPermissions.isExpanded(4)).toBe(true);
    await vi.waitFor(() => expect(teamService['getTeamProviderPermissions']).toHaveBeenCalledWith(9));
  });
});
