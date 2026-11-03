import { inject } from '@angular/core';
import { CanActivateFn, Router, UrlTree } from '@angular/router';
import { AuthService } from '../services/auth.service';
import { HOME_ROUTE } from '../../../shared/constants/nav-items';

/**
 * Requires current team membership — a teamless caller has no models to
 * request or vote on. logos_admin and app_admin are exempt.
 *
 * Unlike hasKeysGuard, an active key is not required: a team member without a
 * key still sees the model catalogue and can vote on model requests.
 *
 * Only an app_developer reaches the fallback (the other roles are exempt
 * above), and their remaining menu items are all team-gated — so they go to
 * /no-access. The redirect to a role's home route is kept as a backstop for
 * any future non-developer role.
 */
export const hasTeamGuard: CanActivateFn = (): boolean | UrlTree => {
  const auth = inject(AuthService);
  const router = inject(Router);

  const role = auth.role();
  const hasTeams = (auth.currentUser()?.teams.length ?? 0) > 0;
  if (role === 'logos_admin' || role === 'app_admin' || hasTeams) return true;

  if (role && role !== 'app_developer') return router.parseUrl(HOME_ROUTE[role]);
  return router.parseUrl('/no-access');
};
