import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { User } from '../../../../core/auth/models/user.model';
import { AuthService } from '../../../../core/auth/services/auth.service';
import { DeveloperKeyLoggingService } from '../../../../core/services/developer-key-logging.service';
import { TeamManagementService } from '../../../../core/services/team-management.service';
import { TeamApiKey, TeamDetail } from '../../../../shared/models/team.model';
import { MembersTabComponent } from './members-tab';

describe('MembersTabComponent', () => {
  let component: MembersTabComponent;
  let teamService: Record<string, ReturnType<typeof vi.fn>>;
  const currentUser = signal<User | null>(null);
  const needsChoice = vi.fn<(teamId: number) => Promise<boolean>>();
  const ownKey = { id: 101, user_id: 21, key_type: 'developer', log: 'BILLING' } as TeamApiKey;
  const otherKey = { ...ownKey, id: 102, user_id: 22 };

  beforeEach(async () => {
    currentUser.set({ user_id: 21, role: 'app_admin' } as User);
    needsChoice.mockReset();
    needsChoice.mockResolvedValue(true);
    teamService = {
      getAdminUsers: vi.fn().mockResolvedValue([]),
      getAllUsers: vi.fn().mockResolvedValue([]),
      getAllProviders: vi.fn().mockResolvedValue([]),
      getTeamProviderPermissions: vi.fn().mockResolvedValue([]),
      getTeamModelPermissions: vi.fn().mockResolvedValue([]),
      getTeamApiKeys: vi.fn().mockResolvedValue([{ ...ownKey, log: 'FULL' }, otherKey]),
    };
    await TestBed.configureTestingModule({
      imports: [MembersTabComponent],
      providers: [
        { provide: TeamManagementService, useValue: teamService },
        { provide: DeveloperKeyLoggingService, useValue: { needsChoice } },
        { provide: AuthService, useValue: { currentUser } },
      ],
    })
      .overrideComponent(MembersTabComponent, { set: { template: '' } })
      .compileComponents();
    component = TestBed.createComponent(MembersTabComponent).componentInstance;
    component.teamId = 9;
    component.team = { id: 9, name: 'Research' } as TeamDetail;
    component.apiKeys = [ownKey, otherKey];
  });

  it('loads the candidate owners when the add-owner dialog opens', async () => {
    component.openAddOwner();
    await vi.waitFor(() => expect(teamService['getAdminUsers']).toHaveBeenCalled());
  });

  it('loads the candidate members when the add-member dialog opens', async () => {
    component.openAddMember();
    await vi.waitFor(() => expect(teamService['getAllUsers']).toHaveBeenCalled());
  });

  it('asks about the current team when the caller expands their own key', async () => {
    component.toggleExpand(ownKey);

    await vi.waitFor(() => expect(component.loggingPrompt()?.team.name).toBe('Research'));
    expect(needsChoice).toHaveBeenCalledExactlyOnceWith(9);
    await component.finishLoggingOnboarding(false);
    expect(component.isExpanded(ownKey.id)).toBe(true);
  });

  it('opens another member’s key without asking the caller to choose logging', () => {
    component.toggleExpand(otherKey);
    component.openKeyModal(otherKey);

    expect(needsChoice).not.toHaveBeenCalled();
    expect(component.loggingPrompt()).toBeNull();
    expect(component.selectedKey()).toBe(otherKey);
  });

  it('reopens the caller’s settings with the updated key after the choice', async () => {
    component.openKeyModal(ownKey);

    await vi.waitFor(() => expect(component.loggingPrompt()).not.toBeNull());
    expect(component.keyModalOpen()).toBe(false);
    const keysReloaded = vi.fn();
    component.keysReloaded.subscribe(keysReloaded);
    await component.finishLoggingOnboarding(true);

    expect(component.loggingPrompt()).toBeNull();
    expect(component.selectedKey()?.log).toBe('FULL');
    expect(component.keyModalOpen()).toBe(true);
    expect(keysReloaded).toHaveBeenCalledExactlyOnceWith([{ ...ownKey, log: 'FULL' }, otherKey]);
  });

  it('keeps key settings available when the team logging check fails', async () => {
    needsChoice.mockRejectedValue(new Error('Offline'));
    component.openKeyModal(ownKey);

    await vi.waitFor(() => expect(component.selectedKey()).toBe(ownKey));
    expect(component.loggingPrompt()).toBeNull();
  });

  it('refreshes the team instead of opening settings with an outdated logging level', async () => {
    teamService['getTeamApiKeys'].mockRejectedValueOnce(new Error('Offline'));
    component.openKeyModal(ownKey);
    await vi.waitFor(() => expect(component.loggingPrompt()).not.toBeNull());

    const refresh = vi.fn();
    component.refresh.subscribe(refresh);
    await component.finishLoggingOnboarding(true);

    expect(component.keyModalOpen()).toBe(false);
    expect(refresh).toHaveBeenCalledOnce();
  });

  it('ignores a late check after the caller collapses their own key', async () => {
    let answer!: (pending: boolean) => void;
    needsChoice.mockImplementation(() => new Promise((resolve) => (answer = resolve)));
    component.toggleExpand(ownKey);
    component.toggleExpand(ownKey);
    answer(true);
    await Promise.resolve();
    await Promise.resolve();

    expect(component.loggingPrompt()).toBeNull();
    expect(component.isExpanded(ownKey.id)).toBe(false);
  });
});
