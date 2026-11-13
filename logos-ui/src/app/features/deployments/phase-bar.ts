import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { PHASE_NAMES, PhaseState } from '../../shared/models/deployment.model';

/** Pull, Drain, Swap, Verify: four segments that show how far a node is. */
@Component({
  selector: 'app-phase-bar',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="bar" role="img" [attr.aria-label]="summary()">
      <div class="segments">
        @for (state of states(); track $index) {
          <div class="seg" [class]="'seg seg--' + state"></div>
        }
      </div>
      <div class="segments">
        @for (name of names; track name; let i = $index) {
          <span class="label" [class]="'label label--' + states()[i]">{{ name }}</span>
        }
      </div>
    </div>
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }

    .bar {
      display: flex;
      flex-direction: column;
      gap: 6px;
      width: 100%;
      min-width: 0;
    }

    .segments {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 6px;
    }

    .seg {
      height: 6px;
      border-radius: 9999px;
      background: rgb(var(--color-typography-500) / 0.25);
    }
    .seg--done {
      background: rgb(var(--color-success));
    }
    .seg--active {
      background: rgb(var(--color-info));
      animation: breathe 1.8s ease-in-out infinite;
    }
    .seg--failed {
      background: rgb(var(--color-error));
    }
    .seg--rolled_back {
      background: rgb(var(--color-warning));
    }

    .label {
      font-size: var(--font-size-xs);
      color: rgb(var(--color-typography-500));
      white-space: nowrap;
    }
    .label--active {
      color: rgb(var(--color-typography-100));
      font-weight: var(--font-weight-semibold);
    }
    .label--failed {
      color: rgb(178 56 65);
      font-weight: var(--font-weight-semibold);
    }
    .label--rolled_back {
      color: rgb(126 102 43);
      font-weight: var(--font-weight-semibold);
    }

    :host-context([data-theme='dark']) {
      .label--failed {
        color: rgb(var(--color-error));
      }
      .label--rolled_back {
        color: rgb(var(--color-warning));
      }
    }

    @keyframes breathe {
      50% {
        opacity: 0.55;
      }
    }

    @media (prefers-reduced-motion: reduce) {
      .seg--active {
        animation: none;
      }
    }
  `,
})
export class PhaseBar {
  readonly states = input.required<PhaseState[]>();
  readonly names = PHASE_NAMES;
  readonly summary = computed(() =>
    PHASE_NAMES.map((name, i) => `${name} ${(this.states()[i] ?? 'pending').replace('_', ' ')}`).join(', '),
  );
}
