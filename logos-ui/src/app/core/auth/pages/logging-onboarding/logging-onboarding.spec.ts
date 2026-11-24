import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, Router, convertToParamMap } from '@angular/router';
import { AuthService } from '../../services/auth.service';
import { LoggingOnboarding } from './logging-onboarding';

describe('LoggingOnboarding', () => {
  it('lets the user retry after a failed save', async () => {
    const save = vi.fn().mockRejectedValueOnce(new Error('Save failed')).mockResolvedValueOnce(undefined);
    const navigateByUrl = vi.fn().mockResolvedValue(true);
    TestBed.configureTestingModule({
      providers: [
        { provide: AuthService, useValue: { completeLoggingOnboarding: save } },
        { provide: Router, useValue: { navigateByUrl } },
        { provide: ActivatedRoute, useValue: { snapshot: { queryParamMap: convertToParamMap({ returnUrl: '/my-workspace' }) } } },
      ],
    });
    const component = TestBed.runInInjectionContext(() => new LoggingOnboarding());

    await component.complete(true);
    expect(component.error()).toContain('Please try again.');
    expect(component.savingChoice()).toBeNull();
    expect(navigateByUrl).not.toHaveBeenCalled();

    await component.complete(true);
    expect(save).toHaveBeenNthCalledWith(2, true);
    expect(component.error()).toBe('');
    expect(navigateByUrl).toHaveBeenCalledWith('/my-workspace', { replaceUrl: true });
  });
});
