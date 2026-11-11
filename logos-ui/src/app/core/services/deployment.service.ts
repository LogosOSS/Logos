import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import {
  DeployerCreated,
  DeployerKind,
  DeploymentOverview,
  DeploymentPlan,
} from '../../shared/models/deployment.model';

/** The page Deployments: history, starting and abandoning a deployment, and the deployers of this Logos. */
@Injectable({ providedIn: 'root' })
export class DeploymentService {
  private http = inject(HttpClient);
  private static readonly BASE = '/api/deployments';

  getOverview(): Promise<DeploymentOverview> {
    return firstValueFrom(this.http.get<DeploymentOverview>(`${DeploymentService.BASE}/overview`));
  }

  getPlans(limit = 50): Promise<DeploymentPlan[]> {
    const params = new HttpParams().set('limit', limit);
    return firstValueFrom(
      this.http.get<DeploymentPlan[]>(`${DeploymentService.BASE}/plans`, { params }),
    );
  }

  getPlan(id: string): Promise<DeploymentPlan> {
    return firstValueFrom(this.http.get<DeploymentPlan>(`${DeploymentService.BASE}/plans/${id}`));
  }

  startPlan(tag: string): Promise<DeploymentPlan> {
    return firstValueFrom(this.http.post<DeploymentPlan>(`${DeploymentService.BASE}/plans`, { tag }));
  }

  abandonPlan(id: string): Promise<DeploymentPlan> {
    return firstValueFrom(
      this.http.post<DeploymentPlan>(`${DeploymentService.BASE}/plans/${id}/abandon`, {}),
    );
  }

  createDeployer(name: string, kind: DeployerKind): Promise<DeployerCreated> {
    return firstValueFrom(
      this.http.post<DeployerCreated>(`${DeploymentService.BASE}/deployers`, { name, kind }),
    );
  }

  replaceDeployerKey(id: string): Promise<DeployerCreated> {
    return firstValueFrom(
      this.http.post<DeployerCreated>(`${DeploymentService.BASE}/deployers/${id}/key`, {}),
    );
  }

  deleteDeployer(id: string): Promise<void> {
    return firstValueFrom(this.http.delete<void>(`${DeploymentService.BASE}/deployers/${id}`));
  }
}
