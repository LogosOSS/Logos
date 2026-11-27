import { DatePipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, OnInit, computed, inject, input, signal } from '@angular/core';
import { ProviderManagementService } from '../../../core/services/provider-management.service';
import { ProviderSpendChange, ProviderSpendStatus } from '../../../shared/models/provider.model';

const MAX_MICRO_CENTS = 9_223_372_036_854_775_807n;

/** Preserve every decimal digit in both validation and the request. */
function validLimit(value: string): boolean {
  if (!/^\d{1,11}(?:\.\d{1,8})?$/.test(value)) return false;
  const [whole, fraction = ''] = value.split('.');
  return BigInt(whole + fraction.padEnd(8, '0')) <= MAX_MICRO_CENTS;
}

@Component({
  selector: 'app-provider-spend-cap',
  standalone: true,
  imports: [DatePipe],
  templateUrl: './provider-spend-cap.html',
  styleUrl: './provider-spend-cap.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ProviderSpendCap implements OnInit {
  private readonly providers = inject(ProviderManagementService);

  readonly providerId = input.required<number>();
  readonly status = signal<ProviderSpendStatus | null>(null);
  readonly loading = signal(false);
  readonly loadFailed = signal(false);
  readonly editing = signal(false);
  readonly saving = signal(false);
  readonly saveFailed = signal(false);
  readonly saved = signal(false);
  readonly enabled = signal(true);
  readonly limit = signal('');
  readonly limitValid = computed(() => validLimit(this.limit().trim()));

  ngOnInit(): void {
    void this.load();
  }

  async load(): Promise<void> {
    if (this.loading()) return;
    this.loading.set(true);
    this.loadFailed.set(false);
    try {
      this.status.set(await this.providers.getMonthlySpendCap(this.providerId()));
    } catch {
      this.loadFailed.set(true);
    } finally {
      this.loading.set(false);
    }
  }

  edit(): void {
    const status = this.status();
    if (!status) return;
    const policy = status.scheduled ?? status.current;
    this.enabled.set(policy?.enabled ?? true);
    this.limit.set(policy?.limit_usd == null ? '' : this.usdDecimal(policy.limit_usd));
    this.saved.set(false);
    this.saveFailed.set(false);
    this.editing.set(true);
  }

  usdLabel(value: number | string | null | undefined): string {
    if (value == null) return 'Unavailable';
    const [whole, fraction = ''] = this.usdDecimal(value).split('.');
    return '$' + whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',') + '.' + fraction.padEnd(2, '0');
  }

  private usdDecimal(value: number | string): string {
    const decimal = typeof value === 'string' ? value : value.toFixed(8);
    return decimal.includes('.') ? decimal.replace(/0+$/, '').replace(/\.$/, '') : decimal;
  }

  cancel(): void {
    if (this.saving()) return;
    this.saveFailed.set(false);
    this.editing.set(false);
  }

  async save(): Promise<void> {
    if (!this.editing() || this.saving() || (this.enabled() && !this.limitValid())) return;
    const change: ProviderSpendChange = this.enabled()
      ? { enabled: true, limit_usd: this.limit().trim() }
      : { enabled: false };
    this.saveFailed.set(false);
    this.saving.set(true);
    try {
      this.status.set(await this.providers.scheduleMonthlySpendCap(this.providerId(), change));
      this.editing.set(false);
      this.saved.set(true);
    } catch {
      this.saveFailed.set(true);
    } finally {
      this.saving.set(false);
    }
  }
}
