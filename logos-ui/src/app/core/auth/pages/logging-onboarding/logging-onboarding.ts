import {
  afterNextRender, ChangeDetectionStrategy, Component, ElementRef, inject, Injector, input, output, signal, viewChild,
} from '@angular/core';
import { TumAetUiDialogComponent } from '@tumaet/ui-angular';
import { ErrorMessageComponent } from '../../../../shared/components/error-message/error-message';
import { Logo } from '../../../../shared/components/logo/logo';
import { DeveloperKeyLog, DeveloperKeyLoggingService } from '../../../services/developer-key-logging.service';
import { Team } from '../../models/user.model';

@Component({
  selector: 'app-logging-onboarding',
  imports: [TumAetUiDialogComponent, ErrorMessageComponent, Logo],
  templateUrl: './logging-onboarding.html',
  styleUrl: './logging-onboarding.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class LoggingOnboarding {
  private logging = inject(DeveloperKeyLoggingService);
  private injector = inject(Injector);
  private heading = viewChild<ElementRef<HTMLHeadingElement>>('heading');

  team = input.required<Team>();
  completed = output<DeveloperKeyLog>();
  dismissed = output<void>();

  savingChoice = signal<DeveloperKeyLog | null>(null);
  earlierChoice = signal<DeveloperKeyLog | null>(null);
  error = signal('');

  async complete(choice: DeveloperKeyLog): Promise<void> {
    if (this.savingChoice() !== null || this.earlierChoice() !== null) return;

    this.savingChoice.set(choice);
    this.error.set('');
    let saved: DeveloperKeyLog;
    try {
      saved = await this.logging.saveChoice(this.team().id, choice);
    } catch {
      this.error.set('We could not confirm your choice. Please try again.');
      return;
    } finally {
      this.savingChoice.set(null);
    }

    if (saved !== choice) {
      this.earlierChoice.set(saved);
      afterNextRender(() => this.heading()?.nativeElement.focus(), { injector: this.injector });
    } else {
      this.completed.emit(saved);
    }
  }

  close(): void {
    if (this.savingChoice() !== null) return;
    const saved = this.earlierChoice();
    if (saved) this.completed.emit(saved);
    else this.dismissed.emit();
  }
}
