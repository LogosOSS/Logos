import { afterNextRender, ChangeDetectionStrategy, Component, ElementRef, inject, Injector, signal, viewChild } from '@angular/core';
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
  private injector = inject(Injector);
  private heading = viewChild<ElementRef<HTMLHeadingElement>>('heading');

  readonly pendingTeams = this.auth.pendingLoggingTeams;
  team = signal(this.pendingTeams()[0] ?? null);
  savingChoice = signal<boolean | null>(null);
  earlierChoice = signal<boolean | null>(null);
  error = signal('');

  async complete(fullLogging: boolean): Promise<void> {
    const team = this.team();
    if (!team || this.savingChoice() !== null || this.earlierChoice() !== null) return;

    this.savingChoice.set(fullLogging);
    this.error.set('');
    try {
      let savedChoice: boolean;
      try {
        savedChoice = await this.auth.completeLoggingOnboarding(team.id, fullLogging);
      } catch {
        this.error.set('We could not confirm your choice. Please try again.');
        return;
      }

      if (savedChoice !== fullLogging) {
        this.earlierChoice.set(savedChoice);
        this.focusHeading();
        return;
      }

      const returnUrl = this.route.snapshot.queryParamMap.get('returnUrl');
      const destination = returnUrl?.startsWith('/') && !returnUrl.startsWith('//') ? returnUrl : '/';
      await this.advance(destination);
    } finally {
      this.savingChoice.set(null);
    }
  }

  async continueAfterConflict(): Promise<void> {
    await this.advance('/my-workspace');
  }

  private async advance(destination: string): Promise<void> {
    const nextTeam = this.pendingTeams()[0];
    if (nextTeam) {
      this.team.set(nextTeam);
      this.earlierChoice.set(null);
      this.focusHeading();
    } else {
      await this.router.navigateByUrl(destination, { replaceUrl: true });
    }
  }

  private focusHeading(): void {
    afterNextRender(() => this.heading()?.nativeElement.focus(), { injector: this.injector });
  }
}
