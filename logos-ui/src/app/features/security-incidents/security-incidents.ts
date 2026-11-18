import { ChangeDetectionStrategy, Component, DestroyRef, OnInit, computed, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';

interface Incident {
  id: number; advisory_id: string; title: string; severity: string; known_exploited: boolean;
  source_url: string; summary: string; importance: string;
}
interface Report {
  rationale?: string; evidence?: { path: string; finding: string }[]; recommended_action?: string;
  action?: string; affected_components?: string[]; commit_sha?: string; patched_version?: string;
}
interface Assessment {
  id: number; incident_id: number; repo_slug: string; branch: string; status: string;
  report: Report; pr_url: string | null; error: string | null; session_id: number | null;
  updated_at: string; next_check_at: string; session_status: string | null;
}
interface Overview {
  enabled: boolean; feeds_enabled: boolean; poll_error: string;
  incidents: Incident[]; assessments: Assessment[];
  repositories: { id: number; repo_slug: string; branch: string }[];
  feeds: { name: string; last_success: string | null; error: string | null }[];
}
interface HistoryEntry { id: number; status: string; created_at: string; created_by: string; detail: { reason?: string; error?: string } }
const BASE = '/api/agent/security';
export const STATUS_LABELS: Record<string, string> = {
  queued: 'Queued', analyzing: 'Analyzing', not_affected: 'Not affected', awaiting_upstream: 'Awaiting upstream',
  fix_available: 'Fix available', remediating: 'Preparing fix', action_required: 'Action required',
  pr_open: 'PR open', awaiting_deployment: 'Awaiting deployment', resolved: 'Resolved', failed: 'Analysis failed',
};

@Component({
  selector: 'app-security-incidents', imports: [DatePipe, FormsModule, RouterLink],
  templateUrl: './security-incidents.html', styleUrl: './security-incidents.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class SecurityIncidents implements OnInit {
  private readonly http = inject(HttpClient);
  private readonly destroyRef = inject(DestroyRef);
  readonly data = signal<Overview | null>(null);
  readonly error = signal('');
  readonly busy = signal(false);
  readonly loading = signal(true);
  readonly filter = signal('open');
  readonly labels = STATUS_LABELS;
  readonly expanded = signal<number | null>(null);
  readonly history = signal<HistoryEntry[]>([]);
  readonly historyError = signal('');
  readonly actionTarget = signal<Assessment | null>(null);
  actionKind: 'recheck' | 'resolve' = 'recheck';
  actionReason = '';
  repoSlug = ''; branch = 'main';
  incident = { advisory_id: '', title: '', severity: 'critical', known_exploited: false,
    source_url: '', summary: '', importance: '', upstream: '' };
  readonly counts = computed(() => {
    const rows = this.data()?.assessments ?? [];
    return {
      action: rows.filter(r => ['action_required', 'failed', 'awaiting_deployment'].includes(r.status)).length,
      waiting: rows.filter(r => r.status === 'awaiting_upstream').length,
      prs: rows.filter(r => ['pr_open', 'remediating'].includes(r.status)).length,
      unaffected: rows.filter(r => r.status === 'not_affected').length,
    };
  });
  readonly visible = computed(() => (this.data()?.incidents ?? []).map(incident => ({
    ...incident, assessments: (this.data()?.assessments ?? []).filter(a => a.incident_id === incident.id &&
      (this.filter() === 'all' || (this.filter() === 'open' ? !['not_affected', 'resolved'].includes(a.status) : a.status === this.filter()))),
  })).filter(i => i.assessments.length || (this.filter() === 'all' && !this.data()?.repositories.length)));

  ngOnInit(): void {
    void this.refresh();
    const timer = setInterval(() => void this.refresh(), 15_000);
    this.destroyRef.onDestroy(() => clearInterval(timer));
  }
  label(status: string): string { return this.labels[status] ?? status; }
  needsAction(a: Assessment): boolean { return ['action_required', 'failed', 'awaiting_deployment'].includes(a.status); }
  canAct(a: Assessment): boolean { return !['analyzing', 'remediating', 'pr_open'].includes(a.status); }
  async refresh(): Promise<void> {
    try { this.data.set(await firstValueFrom(this.http.get<Overview>(BASE))); this.error.set(''); }
    catch (e) { this.error.set(this.message(e)); }
    finally { this.loading.set(false); }
  }
  private message(e: unknown): string {
    if (e instanceof HttpErrorResponse && typeof e.error?.detail === 'string') return e.error.detail;
    return 'The request failed. Check the fields and the agent runner, then try again.';
  }
  private async submit(path: string, body: unknown): Promise<boolean> {
    this.busy.set(true); this.error.set('');
    try { await firstValueFrom(this.http.post(BASE + path, body)); await this.refresh(); return true; }
    catch (e) { this.error.set(this.message(e)); return false; }
    finally { this.busy.set(false); }
  }
  async enroll(): Promise<void> {
    if (await this.submit('/repositories', { repo_slug: this.repoSlug.trim(), branch: this.branch.trim() })) this.repoSlug = '';
  }
  async reportIncident(): Promise<void> {
    const { upstream, ...body } = this.incident;
    if (await this.submit('/incidents', { ...body, upstream_repositories: upstream.split(',').map(v => v.trim()).filter(Boolean) })) {
      this.incident = { advisory_id: '', title: '', severity: 'critical', known_exploited: false,
        source_url: '', summary: '', importance: '', upstream: '' };
    }
  }
  async toggleEvidence(a: Assessment): Promise<void> {
    const id = this.expanded() === a.id ? null : a.id;
    this.expanded.set(id); this.history.set([]); this.historyError.set('');
    if (id !== null) {
      try {
        const events = await firstValueFrom(this.http.get<HistoryEntry[]>(`${BASE}/assessments/${id}/events`));
        if (this.expanded() === id) this.history.set(events);
      } catch { if (this.expanded() === id) this.historyError.set('Could not load the status history.'); }
    }
  }
  prepareAction(a: Assessment, kind: 'recheck' | 'resolve'): void {
    this.actionKind = kind; this.actionReason = ''; this.actionTarget.set(a);
  }
  async applyAction(): Promise<void> {
    const target = this.actionTarget();
    if (target && await this.submit(`/assessments/${target.id}/actions`, { action: this.actionKind, reason: this.actionReason })) {
      this.actionTarget.set(null);
    }
  }
}
