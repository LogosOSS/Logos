import { HttpClient, HttpErrorResponse, HttpStatusCode } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { firstValueFrom } from 'rxjs';

export type DeveloperKeyLog = 'BILLING' | 'FULL';

@Injectable({ providedIn: 'root' })
export class DeveloperKeyLoggingService {
  private http = inject(HttpClient);

  async needsChoice(teamId: number): Promise<boolean> {
    const status = await firstValueFrom(
      this.http.get<{ pending: boolean }>(`/api/teams/${teamId}/logging-onboarding`),
    );
    return status.pending;
  }

  async saveChoice(teamId: number, choice: DeveloperKeyLog): Promise<DeveloperKeyLog> {
    try {
      await firstValueFrom(this.http.post<void>(`/api/teams/${teamId}/logging-onboarding`, {
        full_logging: choice === 'FULL',
      }));
      return choice;
    } catch (error) {
      if (error instanceof HttpErrorResponse && error.status === HttpStatusCode.Conflict
          && typeof error.error?.full_logging === 'boolean') {
        return error.error.full_logging ? 'FULL' : 'BILLING';
      }
      throw error;
    }
  }
}
