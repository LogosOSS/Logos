import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import {
  Provider,
  ModelConnection,
  AddProviderPayload,
  UpdateProviderPayload,
  ProviderSpendStatus,
  ProviderSpendChange,
} from '../../shared/models/provider.model';
import {
  NodeConfig,
  NodeConfigEntry,
  NodeInsights,
  NodeSettings,
  NodeSettingsSaved,
  NodeSettingsValues,
} from '../../shared/models/node-config.model';

@Injectable({ providedIn: 'root' })
export class ProviderManagementService {
  private http = inject(HttpClient);

  getProviders(): Promise<Provider[]> {
    return firstValueFrom(this.http.post<Provider[]>('/api/logosdb/get_providers', {}));
  }

  getMonthlySpendCap(id: number): Promise<ProviderSpendStatus> {
    return firstValueFrom(this.http.get<ProviderSpendStatus>(
      `/api/logosdb/providers/${id}/monthly-spend-cap`,
    ));
  }

  scheduleMonthlySpendCap(id: number, change: ProviderSpendChange): Promise<ProviderSpendStatus> {
    return firstValueFrom(this.http.put<ProviderSpendStatus>(
      `/api/logosdb/providers/${id}/monthly-spend-cap`, change,
    ));
  }

  addProvider(payload: AddProviderPayload): Promise<any> {
    return firstValueFrom(this.http.post<any>('/api/logosdb/add_provider', {
      provider_name:      payload.name,
      base_url:           payload.base_url ?? null,
      api_key:            payload.api_key ?? null,
      auth_name:          payload.auth_name ?? null,
      auth_format:        payload.auth_format ?? null,
      provider_type:      payload.provider_type,
      cloud_provider_type: payload.cloud_provider_type ?? null,
      eu_processing_verified: payload.eu_processing_verified,
      privacy_level:      payload.privacy_level,
    }));
  }

  updateProvider(payload: UpdateProviderPayload): Promise<any> {
    return firstValueFrom(this.http.post<any>('/api/logosdb/update_provider', {
      provider_id:        payload.provider_id,
      provider_name:      payload.name ?? null,
      base_url:           payload.base_url ?? null,
      api_key:            payload.api_key,
      auth_name:          payload.auth_name,
      auth_format:        payload.auth_format,
      provider_type:      payload.provider_type ?? null,
      cloud_provider_type: payload.cloud_provider_type ?? null,
      eu_processing_verified: payload.eu_processing_verified,
      privacy_level:      payload.privacy_level ?? null,
    }));
  }

  rotateWorkerKey(id: number): Promise<{ api_key: string }> {
    return firstValueFrom(this.http.post<{ api_key: string }>(
      `/api/logosdb/providers/${id}/rotate_worker_key`, {},
    ));
  }

  replaceCloudKey(id: number, apiKey: string | null, modelId?: number): Promise<{ api_key_hint: string | null }> {
    return firstValueFrom(this.http.post<{ api_key_hint: string | null }>(
      `/api/logosdb/providers/${id}/replace_cloud_key`,
      apiKey === null ? { clear: true, model_id: modelId } : { api_key: apiKey, model_id: modelId },
    ));
  }

  deleteProvider(id: number): Promise<void> {
    return firstValueFrom(this.http.post<void>('/api/logosdb/delete_provider', { provider_id: id }));
  }

  /**
   * Trigger the orchestrator to re-read every cloud provider's /v1/models
   * listing now. Resolves only once the orchestrator has accepted the
   * refresh (503 when it could not be reached); the sync pass itself runs
   * asynchronously there — see modelSyncStatus() for its completion state.
   */
  refreshModels(): Promise<{ result: string }> {
    return firstValueFrom(this.http.post<{ result: string }>('/api/logosdb/refresh_models', {}));
  }

  /**
   * A cloud model sync's in-flight state: `true` while a pass is running or
   * queued, `false` once the orchestrator explicitly reports none in flight,
   * and `null` when its status could not be read (timeout, rolling deploy).
   * Only an explicit `false` ends the refresh wait — a `null` is unknown,
   * not done.
   */
  modelSyncStatus(): Promise<{ running: boolean | null }> {
    return firstValueFrom(this.http.post<{ running: boolean | null }>('/api/logosdb/model_sync_status', {}));
  }

  /** The stored model list of a logosnode and its revision. */
  getNodeConfig(providerId: number): Promise<NodeConfig> {
    return firstValueFrom(this.http.post<NodeConfig>('/api/logosdb/node_config/get', { provider_id: providerId }));
  }

  /**
   * Replaces the whole list. `expectedRevision` is the revision that was read:
   * another save in between answers 409, a worker that refuses the list 400.
   */
  replaceNodeConfig(
    providerId: number,
    expectedRevision: number,
    entries: NodeConfigEntry[],
  ): Promise<{ provider_id: number; config_revision: number }> {
    return firstValueFrom(
      this.http.post<{ provider_id: number; config_revision: number }>('/api/logosdb/node_config/replace', {
        provider_id: providerId,
        expected_revision: expectedRevision,
        entries,
      }),
    );
  }

  /** Measured and benchmarked values next to the models of a logosnode. */
  getNodeInsights(providerId: number): Promise<NodeInsights> {
    return firstValueFrom(this.http.post<NodeInsights>('/api/logosdb/node_config/insights', { provider_id: providerId }));
  }

  getNodeSettings(providerId: number): Promise<NodeSettings> {
    return firstValueFrom(this.http.post<NodeSettings>('/api/logosdb/node_settings/get', { provider_id: providerId }));
  }

  /** Saves the whole settings document; expectedRevision is the one the admin read. */
  replaceNodeSettings(
    providerId: number,
    expectedRevision: number,
    settings: NodeSettingsValues,
  ): Promise<NodeSettingsSaved> {
    return firstValueFrom(
      this.http.post<NodeSettingsSaved>('/api/logosdb/node_settings/replace', {
        provider_id: providerId,
        expected_revision: expectedRevision,
        settings,
      }),
    );
  }

  /** Starts a calibration of one model; the worker measures it right away. */
  calibrateModel(providerId: number, model: string): Promise<{ message?: string; count?: number }> {
    return firstValueFrom(
      this.http.post<{ message?: string; count?: number }>('/api/logosdb/providers/logosnode/calibrate_uncalibrated', {
        provider_id: providerId,
        model,
      }),
    );
  }

  /** Stops the calibration session of the node; the model in progress stays uncalibrated. */
  stopCalibration(providerId: number): Promise<{ message?: string; was_active?: boolean; current_model?: string }> {
    return firstValueFrom(
      this.http.post<{ message?: string; was_active?: boolean; current_model?: string }>(
        '/api/logosdb/providers/logosnode/stop_calibration',
        { provider_id: providerId },
      ),
    );
  }

  getProviderModels(providerId: number): Promise<ModelConnection[]> {
    return firstValueFrom(this.http.post<ModelConnection[]>('/api/logosdb/get_provider_models', { provider_id: providerId }));
  }

  connectModel(providerId: number, modelId: number, endpoint?: string, apiKey?: string): Promise<void> {
    return firstValueFrom(this.http.post<void>('/api/logosdb/connect_model_provider', {
      provider_id: providerId,
      model_id:    modelId,
      endpoint:    endpoint ?? null,
      api_key:     apiKey   ?? null,
    }));
  }

  disconnectModel(providerId: number, modelId: number): Promise<void> {
    return firstValueFrom(this.http.post<void>('/api/logosdb/disconnect_model_provider', {
      provider_id: providerId,
      model_id:    modelId,
    }));
  }
}
