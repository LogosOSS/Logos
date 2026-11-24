import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, Router, convertToParamMap } from '@angular/router';
import { Team } from '../../models/user.model';
import { AuthService } from '../../services/auth.service';
import { LoggingOnboarding } from './logging-onboarding';

describe('LoggingOnboarding', () => {
  const firstTeam: Team = { id: 10, name: 'First team' };
  const secondTeam: Team = { id: 20, name: 'Second team' };

  function setup(save: (teamId: number, fullLogging: boolean) => Promise<boolean>, teams = [firstTeam]) {
    const pendingLoggingTeams = signal(teams);
    const navigateByUrl = vi.fn().mockResolvedValue(true);
    TestBed.configureTestingModule({
      providers: [
        { provide: AuthService, useValue: {
          pendingLoggingTeams,
          async completeLoggingOnboarding(teamId: number, fullLogging: boolean) {
            const choice = await save(teamId, fullLogging);
            pendingLoggingTeams.update(values => values.filter(team => team.id !== teamId));
            return choice;
          },
        } },
        { provide: Router, useValue: { navigateByUrl } },
        { provide: ActivatedRoute, useValue: { snapshot: { queryParamMap: convertToParamMap({ returnUrl: '/my-workspace' }) } } },
      ],
    });
    const component = TestBed.runInInjectionContext(() => new LoggingOnboarding());
    return { component, navigateByUrl };
  }

  it('lets the user retry after a failed save', async () => {
    const save = vi.fn().mockRejectedValueOnce(new Error('Save failed')).mockResolvedValueOnce(true);
    const { component, navigateByUrl } = setup(save);

    await component.complete(true);
    expect(component.error()).toContain('Please try again.');
    expect(component.savingChoice()).toBeNull();
    expect(navigateByUrl).not.toHaveBeenCalled();

    await component.complete(true);
    expect(save).toHaveBeenNthCalledWith(2, firstTeam.id, true);
    expect(component.error()).toBe('');
    expect(navigateByUrl).toHaveBeenCalledWith('/my-workspace', { replaceUrl: true });
  });

  it('accepts an independent choice for each team before restoring the destination', async () => {
    const save = vi.fn()
      .mockResolvedValueOnce(true)
      .mockResolvedValueOnce(false);
    const { component, navigateByUrl } = setup(save, [firstTeam, secondTeam]);

    await component.complete(true);
    expect(component.team()).toEqual(secondTeam);
    expect(navigateByUrl).not.toHaveBeenCalled();

    await component.complete(false);
    expect(save.mock.calls).toEqual([[firstTeam.id, true], [secondTeam.id, false]]);
    expect(navigateByUrl).toHaveBeenCalledWith('/my-workspace', { replaceUrl: true });
  });

  it.each([true, false])('shows a conflicting saved choice (%s) before continuing', async (savedChoice) => {
    const { component, navigateByUrl } = setup(vi.fn().mockResolvedValue(savedChoice));

    await component.complete(!savedChoice);

    expect(component.earlierChoice()).toBe(savedChoice);
    expect(component.team()).toEqual(firstTeam);
    expect(component.savingChoice()).toBeNull();
    expect(component.error()).toBe('');
    expect(navigateByUrl).not.toHaveBeenCalled();
  });
});
