import { ChangeDetectionStrategy, Component, DestroyRef, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router } from '@angular/router';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { PermissionAuditService } from '../../core/services/permission-audit.service';
import { PermissionAuditEntry, PermissionAuditFilters } from '../../shared/models/permission-audit.model';
import { DataTableComponent } from '../../shared/components/data-table/data-table';
import { ErrorMessageComponent } from '../../shared/components/error-message/error-message';

const ID_FIELDS = ['team_id', 'api_key_id', 'model_id', 'provider_id'] as const;
type IdField = typeof ID_FIELDS[number];
type Draft = Record<IdField | 'from' | 'to', string>;
const emptyDraft = (): Draft => ({ team_id: '', api_key_id: '', model_id: '', provider_id: '', from: '', to: '' });

@Component({
  selector: 'app-permission-audit',
  imports: [DatePipe, FormsModule, DataTableComponent, ErrorMessageComponent],
  templateUrl: './permission-audit.html',
  styleUrl: './permission-audit.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class PermissionAudit {
  private readonly service = inject(PermissionAuditService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly destroyRef = inject(DestroyRef);
  private requestVersion = 0;
  private applied: PermissionAuditFilters = {};
  private cursors: (number | undefined)[] = [undefined];
  private retryPage = 0;
  private retryFilters: PermissionAuditFilters = {};

  readonly pageSize = 25;
  readonly idFields: { key: IdField; label: string }[] = [
    { key: 'team_id', label: 'Team ID' }, { key: 'api_key_id', label: 'API key ID' },
    { key: 'model_id', label: 'Model ID' }, { key: 'provider_id', label: 'Provider ID' },
  ];
  readonly causes = { direct: 'Direct edit', cascade: 'Cascade', migration: 'Migration', repair: 'One-click repair' };
  readonly roles: Record<string, string> = { logos_admin: 'Logos Admin', app_admin: 'App Admin', app_developer: 'App Developer' };
  readonly entries = signal<PermissionAuditEntry[]>([]);
  readonly loading = signal(true);
  readonly error = signal('');
  readonly validationError = signal('');
  readonly pageIndex = signal(0);
  readonly hasMore = signal(false);
  draft = emptyDraft();

  constructor() {
    this.destroyRef.onDestroy(() => this.requestVersion++);
    this.route.queryParamMap.pipe(takeUntilDestroyed()).subscribe(params => {
      this.draft = emptyDraft();
      for (const key of ID_FIELDS) this.draft[key] = params.get(key) ?? '';
      for (const key of ['from', 'to'] as const) {
        const iso = params.get(key);
        if (iso) this.draft[key] = this.localDateTime(iso);
      }
      void this.applyFilters(false);
    });
  }

  async applyFilters(updateUrl = true): Promise<void> {
    ++this.requestVersion;
    const filters: PermissionAuditFilters = {};
    this.validationError.set('');
    for (const key of ID_FIELDS) {
      const value = this.draft[key].trim();
      if (!value) continue;
      if (!/^\d+$/.test(value) || !Number.isSafeInteger(Number(value)) || Number(value) < 1 || Number(value) > 2147483647) {
        this.validationError.set('Enter positive whole-number IDs.');
        this.loading.set(false);
        return;
      }
      filters[key] = Number(value);
    }
    if (filters.model_id && filters.provider_id) {
      this.validationError.set('Filter by one target: a model or a provider.');
      this.loading.set(false);
      return;
    }
    for (const key of ['from', 'to'] as const) {
      if (!this.draft[key]) continue;
      const date = new Date(this.draft[key]);
      if (Number.isNaN(date.getTime())) {
        this.validationError.set('Enter a valid date and time.');
        this.loading.set(false);
        return;
      }
      filters[key] = date.toISOString();
    }
    if (filters.from && filters.to && filters.from >= filters.to) {
      this.validationError.set('The end of the time range must be after its start.');
      this.loading.set(false);
      return;
    }
    if (updateUrl) {
      // The route subscription loads the new filters, including browser back/forward.
      const changed = await this.router.navigate([], { relativeTo: this.route, queryParams: filters });
      if (changed) return;
    }
    await this.load(filters, 0);
  }

  reset(): void {
    this.draft = emptyDraft();
    void this.applyFilters();
  }

  filterBy(key: IdField, id: number): void {
    this.draft[key] = String(id);
    if (key === 'model_id') this.draft.provider_id = '';
    if (key === 'provider_id') this.draft.model_id = '';
    void this.applyFilters();
  }

  next(): void {
    if (this.loading() || !this.hasMore()) return;
    this.cursors[this.pageIndex() + 1] = this.entries().at(-1)?.id;
    void this.load(this.applied, this.pageIndex() + 1);
  }

  previous(): void {
    if (!this.loading() && this.pageIndex() > 0) void this.load(this.applied, this.pageIndex() - 1);
  }

  retry(): void { void this.load(this.retryFilters, this.retryPage); }

  private async load(filters: PermissionAuditFilters, page: number): Promise<void> {
    const version = ++this.requestVersion;
    this.retryFilters = filters;
    this.retryPage = page;
    this.error.set('');
    this.loading.set(true);
    if (page === 0 && JSON.stringify(filters) !== JSON.stringify(this.applied)) {
      this.entries.set([]);
      this.hasMore.set(false);
    }
    try {
      const rows = await this.service.list(filters, page === 0 ? undefined : this.cursors[page], this.pageSize + 1);
      if (version !== this.requestVersion) return;
      this.entries.set(rows.slice(0, this.pageSize));
      this.hasMore.set(rows.length > this.pageSize);
      this.pageIndex.set(page);
      this.applied = filters;
      if (page === 0) this.cursors = [undefined];
    } catch {
      if (version === this.requestVersion) this.error.set('Could not load permission history. Please try again.');
    } finally {
      if (version === this.requestVersion) this.loading.set(false);
    }
  }

  private localDateTime(iso: string): string {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return iso;
    const pad = (value: number) => String(value).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }
}
