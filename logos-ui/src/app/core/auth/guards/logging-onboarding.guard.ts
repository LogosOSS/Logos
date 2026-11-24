import { inject } from '@angular/core';
import { CanActivateChildFn, CanActivateFn, Router } from '@angular/router';
import { AuthService } from '../services/auth.service';

export const requireLoggingOnboarding: CanActivateChildFn = (_route, state) => {
  const auth = inject(AuthService);
  const router = inject(Router);
  return auth.currentUser()?.logging_onboarding_pending !== true
    || router.createUrlTree(['/onboarding'], { queryParams: { returnUrl: state.url } });
};

// Runs after authGuard, which waits for /api/me before allowing activation.
export const hasLoggingOnboarding: CanActivateFn = () =>
  inject(AuthService).currentUser()?.logging_onboarding_pending === true
  || inject(Router).createUrlTree(['/']);
