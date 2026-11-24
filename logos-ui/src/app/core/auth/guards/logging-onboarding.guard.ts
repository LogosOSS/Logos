import { inject } from '@angular/core';
import { CanActivateChildFn, CanActivateFn, Router } from '@angular/router';
import { AuthService } from '../services/auth.service';

export const requireLoggingOnboarding: CanActivateChildFn = (_route, state) => {
  const auth = inject(AuthService);
  const router = inject(Router);
  return auth.pendingLoggingTeams().length === 0
    || router.createUrlTree(['/onboarding'], { queryParams: { returnUrl: state.url } });
};

// Runs after authGuard, which waits for /api/me before allowing activation.
export const hasLoggingOnboarding: CanActivateFn = () =>
  inject(AuthService).pendingLoggingTeams().length > 0
  || inject(Router).createUrlTree(['/']);
