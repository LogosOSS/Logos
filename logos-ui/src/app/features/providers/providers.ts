import {
  Component,
  computed,
  inject,
  signal,
  OnInit,
  OnDestroy,
  ChangeDetectionStrategy,
} from '@angular/core';
import { NgTemplateOutlet } from '@angular/common';
import { ModalFormComponent } from '../../shared/components/modal/modal-form/modal-form';
import { ModalConfirmComponent } from '../../shared/components/modal/modal-confirm/modal-confirm';
import { ProviderManagementService } from '../../core/services/provider-management.service';
import { ModelManagementService } from '../../core/services/model-management.service';
import {
  Provider,
  ModelConnection,
  AddProviderPayload,
  UpdateProviderPayload,
  ProviderType,
  CloudProviderType,
  PrivacyLevel,
  PRIVACY_LEVEL_LABELS,
} from '../../shared/models/provider.model';
import { Model } from '../../shared/models/model.model';
import { isInteractiveClick } from '../../shared/utils/interactive-click';
import { SearchInputComponent } from '../../shared/components/search-input/search-input';
import { DataTableComponent } from '../../shared/components/data-table/data-table';
import { ErrorMessageComponent } from '../../shared/components/error-message/error-message';
import { SelectComponent, AppSelectOption } from '../../shared/components/select/select';
import { NodeModels } from './node-models/node-models';

@Component({
  selector: 'app-providers',
  standalone: true,
  imports: [
    NgTemplateOutlet,
    ModalFormComponent,
    ModalConfirmComponent,
    SearchInputComponent,
    DataTableComponent,
    ErrorMessageComponent,
    SelectComponent,
    NodeModels,
  ],
  templateUrl: './providers.html',
  host: { '(window:pagehide)': 'discardWorkerKey()' },
  changeDetection: ChangeDetectionStrategy.Eager,
  styleUrl: './providers.scss',
})
export class Providers implements OnInit, OnDestroy {
  private providerService = inject(ProviderManagementService);
  private modelService = inject(ModelManagementService);

  readonly providerTypes: ProviderType[] = ['logosnode', 'cloud'];
  readonly cloudProviderTypes: CloudProviderType[] = [
    'azure',
    'openai',
    'anthropic',
    'gemini',
    'bedrock',
    'deepseek',
    'groq',
    'logos',
    'none',
  ];
  // Most private first within each provider type.
  readonly privacyLevels: PrivacyLevel[] = [
    'LOCAL',
    'CLOUD_IN_EU_BY_EU_PROVIDER',
    'CLOUD_IN_EU_BY_US_PROVIDER',
    'CLOUD_NOT_IN_EU_BY_US_PROVIDER',
    // Hardware outside operator control — use it for a personal Mac MLX
    // worker so that LOCAL-threshold requests never route onto a machine
    // whose owner can inspect the running processes.
    'THIRD_PARTY_HARDWARE',
  ];

  readonly String = String;

  readonly providerTypeOptions: AppSelectOption[] = this.providerTypes.map((t) => ({ value: t, label: t }));

  // A cloud provider that is none of the named vendors — Hetzner, a vLLM
  // gateway, any other OpenAI-compatible endpoint — is left untyped. That is
  // not an omission: an untyped cloud provider is exactly the one the generic
  // machinery handles, addressed at /v1/chat/completions with a plain
  // "Authorization: Bearer" and discovered over GET /v1/models.
  private readonly untypedCloudLabel = 'other (OpenAI-compatible)';

  // Deliberately not 'azure'. Azure is the one type excluded from the generic
  // /v1/models sync — it has its own control-plane discovery — so defaulting to
  // it meant a provider saved without touching the field was scraped by
  // neither path and silently stayed empty.
  private readonly defaultCloudProviderType: CloudProviderType = 'none';
  private readonly defaultCloudPrivacyLevel: PrivacyLevel = 'CLOUD_IN_EU_BY_US_PROVIDER';

  // Where each vendor serves its public API, proposed as the base URL once the
  // operator picks the vendor. Only a proposal: a proxy or regional endpoint
  // is typed over it. Vendors whose endpoint names the operator's own resource
  // or instance (azure, bedrock, logos) have none to propose, and neither does
  // gemini, whose OpenAI-compatible root does not end in an API version and
  // so cannot be addressed by the model sync's /v1/models rule.
  private readonly suggestedBaseUrls: Partial<Record<CloudProviderType, string>> = {
    openai: 'https://api.openai.com/v1',
    anthropic: 'https://api.anthropic.com',
    deepseek: 'https://api.deepseek.com',
    groq: 'https://api.groq.com/openai/v1',
  };

  // What the orchestrator and the gateway send when the auth fields are left
  // empty. Anthropic reads x-api-key and ignores an Authorization header, so
  // advertising "Authorization: Bearer {}" for it invites a wrong entry.
  private readonly defaultAuthHeaders: Partial<Record<CloudProviderType, { name: string; format: string }>> = {
    anthropic: { name: 'x-api-key', format: '{}' },
  };

  suggestedBaseUrl(type: ProviderType, cloud: CloudProviderType): string {
    return type === 'cloud' ? (this.suggestedBaseUrls[cloud] ?? '') : '';
  }

  /**
   * The base URL after the vendor changed. An empty field, or one still
   * holding the previous vendor's proposal, takes the new vendor's proposal;
   * a URL the operator typed is never replaced.
   */
  private rebasedUrl(current: string, previous: string, next: string): string {
    const trimmed = current.trim();
    return trimmed === '' || trimmed === previous ? next : current;
  }

  private baseUrlPlaceholder(type: ProviderType, cloud: CloudProviderType): string {
    return this.suggestedBaseUrl(type, cloud) || 'https://...';
  }

  private authPlaceholders(type: ProviderType, cloud: CloudProviderType): { name: string; format: string } {
    return (type === 'cloud' ? this.defaultAuthHeaders[cloud] : undefined) ?? {
      name: 'Authorization',
      format: 'Bearer {}',
    };
  }

  private cloudProviderTypeOptionsFor(type: ProviderType): AppSelectOption[] {
    if (type === 'logosnode') {
      // Never rendered — the field is hidden for a logosnode — but the value
      // still has to resolve to something the application server maps to NULL.
      return [{ value: 'none', label: 'none' }];
    }
    return [
      { value: 'none', label: this.untypedCloudLabel },
      ...this.cloudProviderTypes.filter((t) => t !== 'none').map((t) => ({ value: t, label: t })),
    ];
  }

  private privacyLevelOptionsFor(type: ProviderType): AppSelectOption[] {
    // A logosnode is self-hosted hardware: either the operator's own box
    // (LOCAL) or hardware outside operator control, e.g. a personal Mac MLX
    // worker (THIRD_PARTY_HARDWARE). The CLOUD_* tiers describe jurisdiction
    // of a cloud provider and never apply to a logosnode.
    const levels =
      type === 'logosnode'
        ? this.privacyLevels.filter((l) => l === 'LOCAL' || l === 'THIRD_PARTY_HARDWARE')
        : this.privacyLevels.filter((l) => l !== 'LOCAL' && l !== 'THIRD_PARTY_HARDWARE');
    return levels.map((l) => ({ value: l, label: PRIVACY_LEVEL_LABELS[l] }));
  }

  readonly addCloudProviderTypeOptions = computed(() =>
    this.cloudProviderTypeOptionsFor(this.addProviderType()),
  );
  readonly addPrivacyLevelOptions = computed(() =>
    this.privacyLevelOptionsFor(this.addProviderType()),
  );
  readonly editCloudProviderTypeOptions = computed(() =>
    this.cloudProviderTypeOptionsFor(this.editProviderType()),
  );
  readonly editPrivacyLevelOptions = computed(() =>
    this.privacyLevelOptionsFor(this.editProviderType()),
  );
  readonly addBaseUrlPlaceholder = computed(() =>
    this.baseUrlPlaceholder(this.addProviderType(), this.addCloudProviderType()),
  );
  readonly editBaseUrlPlaceholder = computed(() =>
    this.baseUrlPlaceholder(this.editProviderType(), this.editCloudProviderType()),
  );
  readonly addAuthPlaceholders = computed(() =>
    this.authPlaceholders(this.addProviderType(), this.addCloudProviderType()),
  );
  readonly editAuthPlaceholders = computed(() =>
    this.authPlaceholders(this.editProviderType(), this.editCloudProviderType()),
  );
  // Shown under the field while it holds the proposal, so the operator knows
  // the value came from the vendor choice and may need adjusting.
  readonly addBaseUrlIsSuggestion = computed(() => {
    const suggestion = this.suggestedBaseUrl(this.addProviderType(), this.addCloudProviderType());
    return suggestion !== '' && this.addBaseUrl().trim() === suggestion;
  });
  readonly editBaseUrlIsSuggestion = computed(() => {
    const suggestion = this.suggestedBaseUrl(this.editProviderType(), this.editCloudProviderType());
    return suggestion !== '' && this.editBaseUrl().trim() === suggestion;
  });

  private coercedForType(
    type: ProviderType,
    cloud: CloudProviderType,
    privacy: PrivacyLevel,
  ): { cloud: CloudProviderType; privacy: PrivacyLevel } {
    if (type === 'logosnode') {
      // Preserve a valid logosnode selection instead of forcing LOCAL: a Mac
      // saved as third-party hardware must keep that tier when the edit
      // dialog reopens or the type toggle is cycled.
      return { cloud: 'none', privacy: privacy === 'THIRD_PARTY_HARDWARE' ? privacy : 'LOCAL' };
    }
    // 'none' is kept, not rewritten to a vendor. An untyped cloud provider is a
    // legitimate configuration, and coercing it to 'azure' here meant merely
    // opening the edit dialog on one and saving re-labelled it as Azure —
    // which drops it out of the /v1/models sync it was relying on.
    return {
      cloud,
      privacy: privacy === 'LOCAL' || privacy === 'THIRD_PARTY_HARDWARE' ? this.defaultCloudPrivacyLevel : privacy,
    };
  }

  onAddProviderTypeChange(type: ProviderType): void {
    const previous = this.suggestedBaseUrl(this.addProviderType(), this.addCloudProviderType());
    this.addProviderType.set(type);
    this.addApiKey.set('');
    const { cloud, privacy } = this.coercedForType(
      type,
      this.addCloudProviderType(),
      this.addPrivacyLevel(),
    );
    this.addCloudProviderType.set(cloud);
    this.addPrivacyLevel.set(privacy);
    this.addBaseUrl.set(this.rebasedUrl(this.addBaseUrl(), previous, this.suggestedBaseUrl(type, cloud)));
  }

  onAddCloudProviderTypeChange(cloud: CloudProviderType): void {
    const type = this.addProviderType();
    const previous = this.suggestedBaseUrl(type, this.addCloudProviderType());
    this.addCloudProviderType.set(cloud);
    this.addBaseUrl.set(this.rebasedUrl(this.addBaseUrl(), previous, this.suggestedBaseUrl(type, cloud)));
  }

  onEditProviderTypeChange(type: ProviderType): void {
    const previous = this.suggestedBaseUrl(this.editProviderType(), this.editCloudProviderType());
    this.editProviderType.set(type);
    const { cloud, privacy } = this.coercedForType(
      type,
      this.editCloudProviderType(),
      this.editPrivacyLevel(),
    );
    this.editCloudProviderType.set(cloud);
    this.editPrivacyLevel.set(privacy);
    this.editBaseUrl.set(this.rebasedUrl(this.editBaseUrl(), previous, this.suggestedBaseUrl(type, cloud)));
  }

  onEditCloudProviderTypeChange(cloud: CloudProviderType): void {
    const type = this.editProviderType();
    const previous = this.suggestedBaseUrl(type, this.editCloudProviderType());
    this.editCloudProviderType.set(cloud);
    this.editBaseUrl.set(this.rebasedUrl(this.editBaseUrl(), previous, this.suggestedBaseUrl(type, cloud)));
  }

  readonly connectableModelOptions = computed<AppSelectOption[]>(() => [
    { value: '', label: 'Select model…' },
    ...this.connectableModels().map((m) => ({ value: String(m.id), label: m.name })),
  ]);

  // ── List state ──────────────────────────────────────────────────────────
  providers = signal<Provider[]>([]);
  loading = signal(true);
  search = signal('');
  loadError = signal(false);

  // ── Model refresh state ─────────────────────────────────────────────────
  refreshing = signal(false);
  refreshError = signal(false);

  // ── Expand state ─────────────────────────────────────────────────────────
  expandedIds = signal<ReadonlySet<number>>(new Set());
  providerModels = signal<Record<number, ModelConnection[]>>({});

  // ── All models (for connect picker) ──────────────────────────────────────
  allModels = signal<Model[]>([]);

  // ── Delete modal ────────────────────────────────────────────────────────
  deleteTarget = signal<Provider | null>(null);
  deleteLoading = signal(false);
  deleteError = signal(false);

  // ── Add modal ────────────────────────────────────────────────────────────
  addOpen = signal(false);
  addName = signal('');
  addBaseUrl = signal('');
  addApiKey = signal('');
  addAuthName = signal('');
  addAuthFormat = signal('');
  addProviderType = signal<ProviderType>('cloud');
  addCloudProviderType = signal<CloudProviderType>('azure');
  addPrivacyLevel = signal<PrivacyLevel>('CLOUD_IN_EU_BY_US_PROVIDER');
  addLoading = signal(false);
  addError = signal('');

  keyChange = signal<{ provider: Provider; connection?: ModelConnection; remove: boolean } | null>(null);
  replacementKey = signal('');
  rotationLoading = signal(false);
  rotationError = signal('');
  rotatedKey = signal<{ name: string; value: string } | null>(null);
  rotatedKeyCopied = signal(false);

  // ── Edit modal ────────────────────────────────────────────────────────────
  editTarget = signal<Provider | null>(null);
  editName = signal('');
  editBaseUrl = signal('');
  editAuthName = signal('');
  editAuthFormat = signal('');
  editProviderType = signal<ProviderType>('cloud');
  editCloudProviderType = signal<CloudProviderType>('azure');
  editPrivacyLevel = signal<PrivacyLevel>('CLOUD_IN_EU_BY_US_PROVIDER');
  editLoading = signal(false);
  editError = signal('');

  // ── Connect model modal ───────────────────────────────────────────────────
  connectTarget = signal<Provider | null>(null);
  connectModelId = signal<number | null>(null);
  connectEndpoint = signal('');
  connectApiKey = signal('');
  connectLoading = signal(false);
  connectError = signal('');

  // ── Edit connection modal ─────────────────────────────────────────────────
  editConnProvider = signal<Provider | null>(null);
  editConnModel = signal<ModelConnection | null>(null);
  editConnEndpoint = signal('');
  editConnLoading = signal(false);
  editConnError = signal('');

  // ── Disconnect model modal ────────────────────────────────────────────────
  disconnectProvider = signal<Provider | null>(null);
  disconnectTarget = signal<ModelConnection | null>(null);
  disconnectLoading = signal(false);
  disconnectError = signal(false);

  // ── Computed ─────────────────────────────────────────────────────────────
  filteredProviders = computed(() => {
    const q = this.search().toLowerCase().trim();
    if (!q) return this.providers();
    return this.providers().filter(
      (p) => p.name.toLowerCase().includes(q) || (p.base_url ?? '').toLowerCase().includes(q),
    );
  });
  // Listed apart: a cloud provider is configured by vendor and endpoint, a
  // local one is a worker node with its own models and GPU settings.
  cloudProviders = computed(() => this.filteredProviders().filter((p) => p.provider_type === 'cloud'));
  localProviders = computed(() => this.filteredProviders().filter((p) => p.provider_type !== 'cloud'));

  addValid = computed(() => this.addName().trim().length > 0);
  editValid = computed(() => this.editName().trim().length > 0);
  connectValid = computed(() => this.connectModelId() !== null);

  connectableModels = computed(() => {
    const target = this.connectTarget();
    if (!target) return this.allModels();
    const connected = new Set((this.providerModels()[target.id] ?? []).map((c) => c.model_id));
    return this.allModels().filter((m) => !connected.has(m.id));
  });

  async ngOnInit(): Promise<void> {
    void this.fetchProviders();
    try {
      const models = await this.modelService.getModels();
      this.allModels.set(models);
    } catch {
      // ignore
    }
  }

  async fetchProviders(): Promise<void> {
    this.loading.set(true);
    this.loadError.set(false);
    try {
      const p = await this.providerService.getProviders();
      this.providers.set(p);
    } catch {
      this.loadError.set(true);
    } finally {
      this.loading.set(false);
    }
  }

  // ── Model refresh ─────────────────────────────────────────────────────────
  // The orchestrator's cloud model sync is scheduled, not awaited: after the
  // trigger returns it scrapes every cloud upstream in turn, so the trigger
  // answer is not completion. Neither is an unchanged model list — the
  // first write of a pass can land at any moment, and a mid-pass snapshot
  // can look stable while a later provider is still ahead. The explicit
  // signal is the orchestrator's pass-in-flight status: poll it while
  // refetching the model lists, and stop only once it explicitly reports
  // done — a status that cannot be read (timeout, rolling-deploy 404) is
  // unknown, not done, and is retried on the next round. The rounds cap
  // bounds both a stuck upstream (one provider may hold the pass for its
  // full 30 s request timeout) and a status that never becomes readable.
  private static readonly REFRESH_POLL_INTERVAL_MS = 2000;
  private static readonly REFRESH_POLL_MAX_ROUNDS = 45;

  private refreshPollTimer: ReturnType<typeof setTimeout> | null = null;

  async refreshModels(): Promise<void> {
    if (this.refreshing()) return;
    this.refreshing.set(true);
    this.refreshError.set(false);
    try {
      await this.providerService.refreshModels();
      await this.waitForSyncToFinish();
    } catch {
      this.refreshError.set(true);
    } finally {
      this.refreshPollTimer = null;
      this.refreshing.set(false);
    }
  }

  ngOnDestroy(): void {
    this.discardWorkerKey();
    if (this.refreshPollTimer !== null) clearTimeout(this.refreshPollTimer);
  }

  private async waitForSyncToFinish(): Promise<void> {
    for (let round = 0; round < Providers.REFRESH_POLL_MAX_ROUNDS; round++) {
      // Status first: when the pass is already done (e.g. there are no
      // cloud providers at all), the refetch below is the final state and
      // no further rounds are needed.
      const running = await this.syncStatus();
      await this.refetchModelLists();
      // Only an explicit "not running" from the orchestrator ends the wait.
      // A status that could not be read is unknown, not done — it must not
      // report an accepted sync as settled while the pass may still be
      // writing — so it is retried on the next round.
      if (running === false) return;
      await new Promise<void>((resolve) => {
        this.refreshPollTimer = setTimeout(resolve, Providers.REFRESH_POLL_INTERVAL_MS);
      });
    }
    // The cap ran out with the pass still running, or its status still
    // unreadable (a stuck upstream, a rolling deploy): stop waiting and
    // release the button — the next interval pass catches up.
  }

  /**
   * true/false when the orchestrator answered, null when its status could
   * not be read (network failure, or a response without a usable
   * `running` field). Only an explicit false ends the refresh wait.
   */
  private async syncStatus(): Promise<boolean | null> {
    try {
      const { running } = await this.providerService.modelSyncStatus();
      return running === true || running === false ? running : null;
    } catch {
      return null;
    }
  }

  /**
   * Refetch the model lists this page shows — the global model catalogue and,
   * for each expanded row, that provider's connections. Both move
   * independently: the sync writes per provider, preserving the links the
   * operator configured by hand.
   */
  private async refetchModelLists(): Promise<void> {
    const models = await this.modelService.getModels();
    this.allModels.set(models);
    const expanded = [...this.expandedIds()];
    const lists = await Promise.all(expanded.map((id) => this.providerService.getProviderModels(id)));
    this.providerModels.update((m) => ({ ...m, ...Object.fromEntries(expanded.map((id, i) => [id, lists[i]])) }));
  }

  formatPrivacy(level: PrivacyLevel): string {
    return PRIVACY_LEVEL_LABELS[level] ?? level;
  }

  typeLabel(provider: Provider): string {
    if (provider.provider_type !== 'cloud') return provider.provider_type;
    return provider.cloud_provider_type && provider.cloud_provider_type !== 'none'
      ? provider.cloud_provider_type
      : this.untypedCloudLabel;
  }

  // ── Expand ────────────────────────────────────────────────────────────────
  onRowClick(event: Event, provider: Provider): void {
    if (isInteractiveClick(event)) return;
    this.toggleExpand(provider);
  }

  isExpanded(providerId: number): boolean {
    return this.expandedIds().has(providerId);
  }

  /** Any number of rows may be open at once; a click opens a closed row and closes an open one. */
  toggleExpand(provider: Provider): void {
    if (this.isExpanded(provider.id)) {
      this.expandedIds.update((ids) => new Set([...ids].filter((id) => id !== provider.id)));
      return;
    }
    this.expandedIds.update((ids) => new Set(ids).add(provider.id));
    if (!this.providerModels()[provider.id]) {
      void this.loadProviderModels(provider.id);
    }
  }

  /** Short label of a connection's Batch support, shown next to its endpoint. */
  batchLabel(conn: ModelConnection): string {
    switch (conn.batch_support) {
      case 'supported':
        return 'Batch';
      case 'unsupported':
        return 'No batch';
      case 'logos':
        return 'Batch in Logos';
      default:
        return 'Batch unchecked';
    }
  }

  /** What the Batch label means for this connection, and where the answer came from. */
  batchTooltip(conn: ModelConnection): string {
    const checked = conn.batch_checked_at
      ? ` Checked ${new Date(conn.batch_checked_at).toLocaleString()}${conn.batch_source === 'refusal' ? ' (refused batch)' : ''}.`
      : '';
    switch (conn.batch_support) {
      case 'supported':
        return `Batches are sent to the provider's Batch API on deployment ${conn.batch_deployment ?? '-'}.${checked}`;
      case 'unsupported':
        return `The provider's Batch API refuses this model; Logos runs its batches itself.${checked}${conn.batch_detail ? ' ' + conn.batch_detail : ''}`;
      case 'logos':
        return 'Worker nodes have no Batch API; Logos runs batches for this model itself.';
      default:
        return (
          'Not probed yet. On Azure, Logos runs this model\'s batches itself until a Batch deployment is confirmed; ' +
          'other providers receive the batch and a refusal is remembered.'
        );
    }
  }

  async loadProviderModels(providerId: number): Promise<void> {
    try {
      const conns = await this.providerService.getProviderModels(providerId);
      this.providerModels.update((m) => ({ ...m, [providerId]: conns }));
    } catch {
      this.providerModels.update((m) => ({ ...m, [providerId]: [] }));
    }
  }

  // ── Delete flow ───────────────────────────────────────────────────────────
  openDeleteDialog(provider: Provider): void {
    this.deleteTarget.set(provider);
    this.deleteError.set(false);
  }

  closeDeleteDialog(): void {
    if (this.deleteLoading()) return;
    this.deleteTarget.set(null);
  }

  async confirmDelete(): Promise<void> {
    const target = this.deleteTarget();
    if (!target || this.deleteLoading()) return;
    this.deleteLoading.set(true);
    this.deleteError.set(false);
    try {
      await this.providerService.deleteProvider(target.id);
      this.providers.update((list) => list.filter((p) => p.id !== target.id));
      this.deleteTarget.set(null);
    } catch {
      this.deleteError.set(true);
    } finally {
      this.deleteLoading.set(false);
    }
  }

  // ── Add flow ──────────────────────────────────────────────────────────────
  openAddDialog(): void {
    this.addName.set('');
    this.addBaseUrl.set('');
    this.addApiKey.set('');
    this.addAuthName.set('');
    this.addAuthFormat.set('');
    this.addProviderType.set('cloud');
    this.addCloudProviderType.set(this.defaultCloudProviderType);
    this.addPrivacyLevel.set(this.defaultCloudPrivacyLevel);
    this.addError.set('');
    this.addOpen.set(true);
  }

  closeAddDialog(): void {
    if (this.addLoading()) return;
    this.addOpen.set(false);
  }

  async submitAdd(): Promise<void> {
    if (!this.addValid() || this.addLoading()) return;
    this.addLoading.set(true);
    this.addError.set('');
    const payload: AddProviderPayload = {
      name: this.addName().trim(),
      base_url: this.addBaseUrl().trim() || undefined,
      api_key: this.addApiKey().trim() || undefined,
      auth_name: this.addAuthName().trim() || undefined,
      auth_format: this.addAuthFormat().trim() || undefined,
      provider_type: this.addProviderType(),
      cloud_provider_type:
        this.addCloudProviderType() === 'none' ? undefined : this.addCloudProviderType(),
      privacy_level: this.addPrivacyLevel(),
    };
    try {
      await this.providerService.addProvider(payload);
      this.addApiKey.set('');
      await this.fetchProviders();
      this.addOpen.set(false);
    } catch {
      this.addError.set('Failed to add provider, please try again.');
    } finally {
      this.addLoading.set(false);
    }
  }

  requestKeyChange(provider: Provider, connection?: ModelConnection, remove = false): void {
    this.replacementKey.set('');
    this.rotationError.set('');
    this.keyChange.set({ provider, connection, remove });
  }

  closeKeyChange(): void {
    if (this.rotationLoading()) return;
    this.replacementKey.set('');
    this.keyChange.set(null);
  }

  async confirmKeyChange(): Promise<void> {
    const target = this.keyChange();
    if (!target || this.rotationLoading()) return;
    const { provider, connection } = target;
    const worker = provider.provider_type === 'logosnode';
    const replacement = this.replacementKey().trim();
    if (!worker && !target.remove && !replacement) return;
    this.rotationLoading.set(true);
    this.rotationError.set('');
    try {
      if (worker) {
        const response = await this.providerService.rotateWorkerKey(provider.id);
        if (this.keyChange() !== target) return;
        this.editTarget.set(null);
        this.rotatedKeyCopied.set(false);
        this.rotatedKey.set({ name: provider.name, value: response.api_key });
      } else {
        const { api_key_hint } = await this.providerService.replaceCloudKey(provider.id, target.remove ? null : replacement, connection?.model_id);
        if (this.keyChange() !== target) return;
        if (connection) {
          this.editConnModel.set({ ...connection, api_key_hint });
        } else {
          this.editTarget.set({ ...provider, api_key_hint });
        }
      }
      this.keyChange.set(null);
      this.replacementKey.set('');
      if (connection) await this.loadProviderModels(provider.id);
      else await this.fetchProviders();
    } catch {
      this.rotationError.set(worker
        ? 'Could not confirm rotation. Rotate again if the replacement key was lost.'
        : 'Could not update the key. Please try again.');
    } finally {
      this.rotationLoading.set(false);
    }
  }

  async copyRotatedKey(): Promise<void> {
    const key = this.rotatedKey();
    if (!key) return;
    try {
      await navigator.clipboard.writeText(key.value);
      if (this.rotatedKey() === key) this.rotatedKeyCopied.set(true);
    } catch {
      // The readonly field also supports manual copying.
    }
  }

  discardWorkerKey(): void {
    this.keyChange.set(null);
    this.replacementKey.set('');
    this.rotatedKey.set(null);
    this.rotatedKeyCopied.set(false);
    this.editTarget.set(null);
  }

  // ── Edit flow ─────────────────────────────────────────────────────────────
  openEditDialog(provider: Provider): void {
    this.editTarget.set(provider);
    this.editName.set(provider.name);
    this.editBaseUrl.set(provider.base_url ?? '');
    this.editAuthName.set(provider.auth_name ?? '');
    this.editAuthFormat.set(provider.auth_format ?? '');
    this.editProviderType.set(provider.provider_type);
    const { cloud, privacy } = this.coercedForType(
      provider.provider_type,
      provider.cloud_provider_type ?? 'none',
      provider.privacy_level,
    );
    this.editCloudProviderType.set(cloud);
    this.editPrivacyLevel.set(privacy);
    this.editError.set('');
  }

  closeEditDialog(): void {
    if (this.editLoading()) return;
    this.editTarget.set(null);
  }

  async submitEdit(): Promise<void> {
    const target = this.editTarget();
    if (!target || !this.editValid() || this.editLoading()) return;
    this.editLoading.set(true);
    this.editError.set('');
    const payload: UpdateProviderPayload = {
      provider_id: target.id,
      name: this.editName().trim() || undefined,
      base_url: this.editBaseUrl().trim(),
      auth_name: this.editAuthName().trim(),
      auth_format: this.editAuthFormat().trim(),
      provider_type: this.editProviderType(),
      // Send 'none' literally — the application server treats null as "leave unchanged",
      // so mapping it to null makes resetting the cloud type a silent no-op.
      cloud_provider_type: this.editCloudProviderType(),
      privacy_level: this.editPrivacyLevel(),
    };
    try {
      await this.providerService.updateProvider(payload);
      await this.fetchProviders();
      this.editTarget.set(null);
    } catch {
      this.editError.set('Failed to save changes, please try again.');
    } finally {
      this.editLoading.set(false);
    }
  }

  // ── Connect model flow ────────────────────────────────────────────────────
  openConnectDialog(provider: Provider): void {
    this.connectTarget.set(provider);
    this.connectModelId.set(null);
    this.connectEndpoint.set('');
    this.connectApiKey.set('');
    this.connectError.set('');
  }

  closeConnectDialog(): void {
    if (this.connectLoading()) return;
    this.connectTarget.set(null);
  }

  async submitConnect(): Promise<void> {
    const target = this.connectTarget();
    const modelId = this.connectModelId();
    if (!target || modelId === null || this.connectLoading()) return;
    this.connectLoading.set(true);
    this.connectError.set('');
    try {
      await this.providerService.connectModel(
        target.id,
        modelId,
        this.connectEndpoint().trim() || undefined,
        this.connectApiKey().trim() || undefined,
      );
      await this.loadProviderModels(target.id);
      this.connectTarget.set(null);
    } catch {
      this.connectError.set('Failed to connect model, please try again.');
    } finally {
      this.connectLoading.set(false);
    }
  }

  // ── Edit connection flow ──────────────────────────────────────────────────
  openEditConnDialog(provider: Provider, conn: ModelConnection): void {
    this.editConnProvider.set(provider);
    this.editConnModel.set(conn);
    this.editConnEndpoint.set(conn.endpoint ?? '');
    this.editConnError.set('');
  }

  closeEditConnDialog(): void {
    if (this.editConnLoading()) return;
    this.editConnProvider.set(null);
    this.editConnModel.set(null);
  }

  async submitEditConn(): Promise<void> {
    const provider = this.editConnProvider();
    const conn = this.editConnModel();
    if (!provider || !conn || this.editConnLoading()) return;
    this.editConnLoading.set(true);
    this.editConnError.set('');
    try {
      await this.providerService.connectModel(
        provider.id,
        conn.model_id,
        this.editConnEndpoint().trim() || undefined,
      );
      await this.loadProviderModels(provider.id);
      this.editConnProvider.set(null);
      this.editConnModel.set(null);
    } catch {
      this.editConnError.set('Failed to save, please try again.');
    } finally {
      this.editConnLoading.set(false);
    }
  }

  openDisconnectDialog(provider: Provider, conn: ModelConnection): void {
    this.disconnectProvider.set(provider);
    this.disconnectTarget.set(conn);
    this.disconnectError.set(false);
  }

  closeDisconnectDialog(): void {
    if (this.disconnectLoading()) return;
    this.disconnectProvider.set(null);
    this.disconnectTarget.set(null);
  }

  async confirmDisconnect(): Promise<void> {
    const provider = this.disconnectProvider();
    const conn = this.disconnectTarget();
    if (!provider || !conn || this.disconnectLoading()) return;
    this.disconnectLoading.set(true);
    this.disconnectError.set(false);
    try {
      await this.providerService.disconnectModel(provider.id, conn.model_id);
      this.providerModels.update((m) => ({
        ...m,
        [provider.id]: (m[provider.id] ?? []).filter((c) => c.model_id !== conn.model_id),
      }));
      this.disconnectProvider.set(null);
      this.disconnectTarget.set(null);
    } catch {
      this.disconnectError.set(true);
    } finally {
      this.disconnectLoading.set(false);
    }
  }
}
