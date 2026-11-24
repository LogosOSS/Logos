import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, Router, convertToParamMap } from '@angular/router';
import { AuthService } from '../../services/auth.service';
import { LoggingOnboarding } from './logging-onboarding';

describe('LoggingOnboarding', () => {
  function setup(save: (fullLogging: boolean) => Promise<boolean>) {
    const navigateByUrl = vi.fn().mockResolvedValue(true);
    TestBed.configureTestingModule({
      providers: [
        { provide: AuthService, useValue: { completeLoggingOnboarding: save } },
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
    expect(save).toHaveBeenNthCalledWith(2, true);
    expect(component.error()).toBe('');
    expect(navigateByUrl).toHaveBeenCalledWith('/my-workspace', { replaceUrl: true });
  });

  it.each([true, false])('shows a conflicting saved choice (%s) before continuing', async (savedChoice) => {
    const { component, navigateByUrl } = setup(vi.fn().mockResolvedValue(savedChoice));

    await component.complete(!savedChoice);

    expect(component.earlierChoice()).toBe(savedChoice);
    expect(component.savingChoice()).toBeNull();
    expect(component.error()).toBe('');
    expect(navigateByUrl).not.toHaveBeenCalled();
  });
});
