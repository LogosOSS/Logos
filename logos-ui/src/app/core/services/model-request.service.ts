import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';

/**
 * The license state of a requested model: not fetched yet (pending — while a
 * model is gated the license is not readable, and a failed fetch retries on
 * the recheck), a license exists to accept (available), or the repository
 * carries no license information at all (no_license).
 */
export type LicenseStatus = 'pending' | 'available' | 'no_license';

/**
 * One requested model, as the webservice reports it: the registry row id, the
 * model name, how many users have voted for it, and whether the caller has.
 * The keys mirror the columns of the `requested_models` / `requested_model_votes`
 * tables (the webservice serializes in snake_case).
 */
export interface ModelRequest {
  id: number;
  name: string;
  request_count: number;
  has_voted: boolean;
  is_gated: boolean | null;
  /** The team that first requested the model; null until a vote named one. */
  first_requesting_team: number | null;
  license_status: LicenseStatus;
  /** The Logos admin's acceptance of the model's license. */
  license_approved: boolean;
}

/** The stored license of a requested model, as the acceptance dialog renders it. */
export interface ModelLicense {
  status: LicenseStatus;
  /** The SPDX expression the model card declares; null when there is none. */
  license_id: string | null;
  /** The license file's text; null when the repository carries no file. */
  license_text: string | null;
  approved: boolean;
  approved_at: string | null;
}

/**
 * Data access for the model-request voting section (embedded in the Models
 * page).
 *
 * A model request is a vote for a model Logos does not serve yet — a demand
 * signal, not a configuration change. Each user gets one vote per model; they
 * can take it back (undo) and cast it again. The listing shows what everyone
 * has voted for, with the caller's own vote state.
 */
@Injectable({ providedIn: 'root' })
export class ModelRequestService {
  private http = inject(HttpClient);

  list(): Promise<ModelRequest[]> {
    return firstValueFrom(this.http.post<ModelRequest[]>('/api/logosdb/get_model_requests', {}));
  }

  /**
   * Re-verify every flagged model against Hugging Face so a license accepted
   * since the flag was set clears it. Call it before {@link list} on a
   * refresh; a failure there must not prevent the list from loading.
   */
  recheckGated(): Promise<void> {
    return firstValueFrom(this.http.post<void>('/api/logosdb/recheck_model_requests', {}));
  }

  /**
   * Cast the caller's vote for a model, naming the team it is wanted for (a
   * no-op if they already voted).
   */
  vote(name: string, teamId: number): Promise<ModelRequest> {
    return firstValueFrom(
      this.http.post<ModelRequest>('/api/logosdb/add_model_request', { name, team_id: teamId }),
    );
  }

  /** Take the caller's vote back for a model (idempotent). */
  undoVote(name: string): Promise<ModelRequest> {
    return firstValueFrom(
      this.http.post<ModelRequest>('/api/logosdb/remove_model_request', { name }),
    );
  }

  /** The model's license — state, SPDX id and text — for the acceptance dialog. */
  getLicense(modelId: number): Promise<ModelLicense> {
    return firstValueFrom(this.http.post<ModelLicense>('/api/logosdb/get_model_license', { model_id: modelId }));
  }

  /**
   * Record the Logos admin's global acceptance of the model's license. Only a
   * model whose license is available can be accepted (400 otherwise);
   * accepting again refreshes who and when.
   */
  approveLicense(modelId: number): Promise<ModelLicense> {
    return firstValueFrom(
      this.http.post<ModelLicense>('/api/logosdb/approve_model_license', { model_id: modelId }),
    );
  }

  /**
   * Revoke the Logos admin's acceptance of the model's license — and the team
   * acceptances that depend on it. Idempotent.
   */
  revokeLicense(modelId: number): Promise<ModelLicense> {
    return firstValueFrom(
      this.http.post<ModelLicense>('/api/logosdb/revoke_model_license', { model_id: modelId }),
    );
  }
}
