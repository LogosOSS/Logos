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

  savingChoice = signal<boolean | null>(null);
  error = signal('');

  async complete(fullLogging: boolean): Promise<void> {
    if (this.savingChoice() !== null) return;

    this.savingChoice.set(fullLogging);
    this.error.set('');
    try {
      await this.auth.completeLoggingOnboarding(fullLogging);
    } catch {
      this.error.set('We could not save your choice. Please try again.');
      this.savingChoice.set(null);
      return;
    }

    const returnUrl = this.route.snapshot.queryParamMap.get('returnUrl');
    const destination = returnUrl?.startsWith('/') && !returnUrl.startsWith('//') ? returnUrl : '/';
    try {
      await this.router.navigateByUrl(destination, { replaceUrl: true });
    } finally {
      this.savingChoice.set(null);
    }
  }
}
