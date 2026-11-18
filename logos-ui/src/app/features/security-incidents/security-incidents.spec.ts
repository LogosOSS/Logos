import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { SecurityIncidents } from './security-incidents';
import { routes } from '../../app.routes';
import { roleGuard } from '../../core/auth/guards/role.guard';

describe('Security incident dashboard', () => {
  afterEach(() => TestBed.resetTestingModule());
  it('restricts direct navigation to Logos Admin', () => {
    const route = routes.flatMap(r => r.children ?? []).find(r => r.path === 'agents/security');
    expect(route?.data?.['roles']).toEqual(['logos_admin']);
    expect(route?.canActivate).toContain(roleGuard);
  });
  it('keeps not affected visible in the all-status view and separates deployment from resolution', async () => {
    TestBed.configureTestingModule({ imports: [SecurityIncidents], providers: [provideRouter([]), provideHttpClient(), provideHttpClientTesting()] });
    const fixture = TestBed.createComponent(SecurityIncidents);
    const http = TestBed.inject(HttpTestingController);
    http.expectNone('/api/agent/security');
    fixture.detectChanges();
    http.expectOne('/api/agent/security').flush({ enabled: true, feeds_enabled: true, poll_error: '', feeds: [],
      repositories: [{ id: 1, repo_slug: 'org/repo', branch: 'main' }],
      incidents: [{ id: 1, advisory_id: 'CVE-2026-12345', title: 'An important vulnerability', severity: 'critical', source_url: 'https://example.org', importance: 'Remote exploitation is possible.' }],
      assessments: [
        { id: 1, incident_id: 1, repo_slug: 'org/repo', status: 'not_affected', report: {} },
        { id: 2, incident_id: 1, repo_slug: 'org/other', status: 'awaiting_deployment', report: {} },
      ] });
    await fixture.whenStable();
    expect(fixture.componentInstance.visible()[0].assessments.map(a => a.id)).toEqual([2]);
    expect(fixture.componentInstance.counts().action).toBe(1);
    fixture.componentInstance.filter.set('all');
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('Not affected');
    expect(fixture.nativeElement.textContent).toContain('Awaiting deployment');
    expect(fixture.componentInstance.visible()[0].assessments.length).toBe(2);
    http.verify();
    fixture.destroy();
  });
});
