import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { StatusTone } from '../../shared/models/deployment.model';

/** The state of a deployment or of one node: a coloured dot and a word. The dot of a running one pulses. */
@Component({
  selector: 'app-status-pill',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<span class="pill" [class]="'pill pill--' + tone()">
    <span class="dot" [class.dot--ping]="tone() === 'running'" aria-hidden="true"></span>{{ label() }}
  </span>`,
  styles: `
    :host {
      display: inline-flex;
    }

    .pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 2px 10px 2px 8px;
      border-radius: 999px;
      border: 1px solid rgb(var(--tone) / var(--opacity-border));
      background: rgb(var(--tone) / var(--opacity-soft));
      color: rgb(var(--tone-text));
      font-size: var(--font-size-xs);
      font-weight: var(--font-weight-semibold);
      line-height: 1.5;
      white-space: nowrap;
    }

    .pill--running {
      --tone: var(--color-info);
      --tone-text: 45 100 144;
    }
    .pill--succeeded {
      --tone: var(--color-success);
      --tone-text: 43 110 101;
    }
    .pill--failed {
      --tone: var(--color-error);
      --tone-text: 178 56 65;
    }
    .pill--rolled-back {
      --tone: var(--color-warning);
      --tone-text: 126 102 43;
    }
    .pill--idle {
      --tone: var(--color-typography-500);
      --tone-text: var(--color-typography-500);
    }

    :host-context([data-theme='dark']) {
      .pill--running {
        --tone-text: var(--color-info);
      }
      .pill--succeeded {
        --tone-text: var(--color-success);
      }
      .pill--failed {
        --tone-text: var(--color-error);
      }
      .pill--rolled-back {
        --tone-text: var(--color-warning);
      }
    }

    .dot {
      position: relative;
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: rgb(var(--tone));
    }

    .dot--ping::after {
      content: '';
      position: absolute;
      inset: 0;
      border-radius: 50%;
      background: inherit;
      opacity: 0.6;
      animation: ping 1.8s cubic-bezier(0, 0, 0.2, 1) infinite;
    }

    @keyframes ping {
      0% {
        transform: scale(1);
        opacity: 0.6;
      }
      75%,
      100% {
        transform: scale(2.6);
        opacity: 0;
      }
    }

    @media (prefers-reduced-motion: reduce) {
      .dot--ping::after {
        animation: none;
        opacity: 0;
      }
    }
  `,
})
export class StatusPill {
  readonly tone = input.required<StatusTone>();
  readonly label = input.required<string>();
}
