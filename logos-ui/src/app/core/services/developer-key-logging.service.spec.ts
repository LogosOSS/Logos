import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { DeveloperKeyLoggingService } from './developer-key-logging.service';

describe('DeveloperKeyLoggingService', () => {
  let service: DeveloperKeyLoggingService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
    service = TestBed.inject(DeveloperKeyLoggingService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('reads each opened team separately and reflects choices made after an earlier read', async () => {
    const firstTeam = service.needsChoice(10);
    http.expectOne({ method: 'GET', url: '/api/teams/10/logging-onboarding' }).flush({ pending: true });
    expect(await firstTeam).toBe(true);

    const secondTeam = service.needsChoice(20);
    http.expectOne({ method: 'GET', url: '/api/teams/20/logging-onboarding' }).flush({ pending: true });
    expect(await secondTeam).toBe(true);

    const reopenedTeam = service.needsChoice(10);
    http.expectOne({ method: 'GET', url: '/api/teams/10/logging-onboarding' }).flush({ pending: false });
    expect(await reopenedTeam).toBe(false);
  });

  it.each(['FULL', 'BILLING'] as const)('saves %s for only the chosen team', async (choice) => {
    const result = service.saveChoice(20, choice);
    const request = http.expectOne({ method: 'POST', url: '/api/teams/20/logging-onboarding' });
    expect(request.request.body).toEqual({ full_logging: choice === 'FULL' });
    request.flush(null, { status: 204, statusText: 'No Content' });

    expect(await result).toBe(choice);
  });

  it('returns the canonical choice on a conflict', async () => {
    const result = service.saveChoice(20, 'FULL');
    http.expectOne('/api/teams/20/logging-onboarding').flush(
      { full_logging: false },
      { status: 409, statusText: 'Conflict' },
    );
    expect(await result).toBe('BILLING');
  });
});
