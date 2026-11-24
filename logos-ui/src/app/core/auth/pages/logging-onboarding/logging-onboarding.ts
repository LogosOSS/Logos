import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { ActivatedRoute, Router } from '@angular/router';
import { ErrorMessageComponent } from '../../../../shared/components/error-message/error-message';
import { Logo } from '../../../../shared/components/logo/logo';
import { ThemeToggle } from '../../../../shared/components/theme-toggle/theme-toggle';
import { AuthService } from '../../services/auth.service';

@Component({
  selector: 'app-logging-onboarding',
  imports: [ErrorMessageComponent, Logo, ThemeToggle],
  templateUrl: './logging-onboarding.html',
  styleUrl: './logging-onboarding.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class LoggingOnboarding {
  private auth = inject(AuthService);
  private router = inject(Router);
  private route = inject(ActivatedRoute);

  choice = signal<boolean | null>(null);
  saving = signal(false);
  error = signal('');

  async complete(): Promise<void> {
    const fullLogging = this.choice();
    if (fullLogging === null || this.saving()) return;

    this.saving.set(true);
    this.error.set('');
    try {
      await this.auth.completeLoggingOnboarding(fullLogging);
    } catch {
      this.error.set('We could not save your choice. Please try again.');
      return;
    } finally {
      this.saving.set(false);
    }

    const returnUrl = this.route.snapshot.queryParamMap.get('returnUrl');
    const destination = returnUrl?.startsWith('/') && !returnUrl.startsWith('//') ? returnUrl : '/';
    await this.router.navigateByUrl(destination, { replaceUrl: true });
  }
}
