import { afterNextRender, ChangeDetectionStrategy, Component, ElementRef, inject, Injector, signal, viewChild } from '@angular/core';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { ErrorMessageComponent } from '../../../../shared/components/error-message/error-message';
import { Logo } from '../../../../shared/components/logo/logo';
import { ThemeToggle } from '../../../../shared/components/theme-toggle/theme-toggle';
import { AuthService } from '../../services/auth.service';

@Component({
  selector: 'app-logging-onboarding',
  imports: [ErrorMessageComponent, Logo, RouterLink, ThemeToggle],
  templateUrl: './logging-onboarding.html',
  styleUrl: './logging-onboarding.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class LoggingOnboarding {
  private auth = inject(AuthService);
  private router = inject(Router);
  private route = inject(ActivatedRoute);
  private injector = inject(Injector);
  private savedChoiceHeading = viewChild<ElementRef<HTMLHeadingElement>>('savedChoiceHeading');

  savingChoice = signal<boolean | null>(null);
  earlierChoice = signal<boolean | null>(null);
  error = signal('');

  async complete(fullLogging: boolean): Promise<void> {
    if (this.savingChoice() !== null || this.earlierChoice() !== null) return;

    this.savingChoice.set(fullLogging);
    this.error.set('');
    try {
      let savedChoice: boolean;
      try {
        savedChoice = await this.auth.completeLoggingOnboarding(fullLogging);
      } catch {
        this.error.set('We could not confirm your choice. Please try again.');
        return;
      }

      if (savedChoice !== fullLogging) {
        this.earlierChoice.set(savedChoice);
        afterNextRender(() => this.savedChoiceHeading()?.nativeElement.focus(), { injector: this.injector });
        return;
      }

      const returnUrl = this.route.snapshot.queryParamMap.get('returnUrl');
      const destination = returnUrl?.startsWith('/') && !returnUrl.startsWith('//') ? returnUrl : '/';
      await this.router.navigateByUrl(destination, { replaceUrl: true });
    } finally {
      this.savingChoice.set(null);
    }
  }
}
