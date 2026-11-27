import { ChangeDetectionStrategy, Component, OnInit, computed, inject, signal } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { FormsModule } from '@angular/forms';
import { firstValueFrom } from 'rxjs';
import { benchmarkErrorMessage } from './benchmark-settings';

type Provider = 'local' | 'logos_prod';

interface Selection {
  provider: Provider;
  model: string;
  revision: number | null;
}

interface ProposerSettings {
  selection: Selection;
  local_models: string[];
  logos_prod: { available: boolean; models: string[] } | null;
}

const settingsUrl = '/api/logosdb/model_benchmarks/nightly/proposer';

@Component({
  selector: 'app-nightly-proposer-model',
  imports: [FormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './nightly-proposer-model.html',
  styleUrl: './nightly-proposer-model.scss',
})
export class NightlyProposerModel implements OnInit {
  private readonly http = inject(HttpClient);
  readonly settings = signal<ProposerSettings | null>(null);
  readonly provider = signal<Provider>('local');
  readonly model = signal('');
  readonly loading = signal(false);
  readonly saving = signal(false);
  readonly error = signal('');
  readonly saved = signal(false);
  readonly models = computed(() => {
    const settings = this.settings();
    return (this.provider() === 'local' ? settings?.local_models : settings?.logos_prod?.models) ?? [];
  });
  readonly canSave = computed(() => {
    const selection = this.settings()?.selection;
    return !!selection && !this.loading() && !this.saving() && this.models().includes(this.model()) &&
      (selection.provider !== this.provider() || selection.model !== this.model());
  });

  ngOnInit(): void {
    void this.load();
  }

  async load(): Promise<void> {
    this.loading.set(true);
    this.error.set('');
    this.saved.set(false);
    try {
      const settings = await firstValueFrom(this.http.post<ProposerSettings>(settingsUrl, {}));
      this.settings.set(settings);
      this.provider.set(settings.selection.provider);
      this.model.set(settings.selection.model);
    } catch {
      this.settings.set(null);
      this.error.set('Could not load the model for parameter suggestions.');
    } finally {
      this.loading.set(false);
    }
  }

  selectProvider(value: string): void {
    const provider = value === 'logos_prod' ? 'logos_prod' : 'local';
    const selection = this.settings()?.selection;
    this.provider.set(provider);
    this.selectModel(selection?.provider === provider ? selection.model : '');
  }

  selectModel(model: string): void {
    this.model.set(model);
    this.error.set('');
    this.saved.set(false);
  }

  async save(): Promise<void> {
    const settings = this.settings();
    if (!settings || !this.canSave()) return;
    this.saving.set(true);
    this.error.set('');
    try {
      const selection = await firstValueFrom(this.http.post<Selection>(settingsUrl + '/configure', {
        provider: this.provider(),
        model: this.model(),
        revision: settings.selection.revision,
      }));
      this.settings.set({ ...settings, selection });
      this.saved.set(true);
    } catch (error) {
      if (error instanceof HttpErrorResponse && error.status === 409) {
        await this.load();
        if (!this.error()) this.error.set('Another admin changed the selection. The current settings are shown.');
      } else {
        this.error.set(benchmarkErrorMessage(error, 'Could not save the model for parameter suggestions.'));
      }
    } finally {
      this.saving.set(false);
    }
  }
}
