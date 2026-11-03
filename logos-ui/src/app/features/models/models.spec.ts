import { signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideRouter, Router } from '@angular/router';

import { Models } from './models';
import { ModelManagementService } from '../../core/services/model-management.service';
import { ModelRequestService } from '../../core/services/model-request.service';
import { AuthService } from '../../core/auth/services/auth.service';
import { User } from '../../core/auth/models/user.model';

/**
 * The page is split into the Active models and Model requests tabs, and the
 * active one is read from and written to ?tab= — the same pattern as the
 * statistics page — so these tests boot the page with a real router and check
 * that the URL and the rendered section cannot disagree.
 *
 * The role is deliberately not logos_admin: that branch starts the nightly
 * benchmark poller, which these tests do not care about.
 */

const USER: User = {
  user_id: 1,
  username: 'operator',
  prename: 'Op',
  name: 'Erator',
  email: 'operator@example.com',
  role: 'app_developer',
  teams: [{ id: 1, name: 'Test team' }],
};

/** Flush the microtask queue so an in-flight navigation settles. */
const tick = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));

async function boot() {
  await TestBed.configureTestingModule({
    imports: [Models],
    providers: [
      provideHttpClient(),
      {
        provide: ModelManagementService,
        useValue: {
          getModels: vi.fn().mockResolvedValue([]),
          getModelCapabilities: vi.fn().mockResolvedValue({}),
        },
      },
      { provide: ModelRequestService, useValue: { list: vi.fn().mockResolvedValue([]) } },
      {
        provide: AuthService,
        useValue: { role: signal('app_developer'), currentUser: signal(USER) },
      },
      // The page reads ?tab= and navigates on a tab switch, so it needs a
      // real router: without one construction fails before any test body runs.
      provideRouter([]),
    ],
  }).compileComponents();
  const fx = TestBed.createComponent(Models);
  // detectChanges runs ngOnInit through Angular's hook machinery so a later
  // signal-driven CD pass does not re-fire it.
  fx.detectChanges();
  return { fx, router: TestBed.inject(Router) };
}

describe('Models tabs', () => {
  let fixture: ComponentFixture<Models> | undefined;

  afterEach(() => {
    fixture?.destroy();
    fixture = undefined;
    TestBed.resetTestingModule();
  });

  it('shows the catalogue by default and leaves the URL undecorated', async () => {
    const page = await boot();
    fixture = page.fx;

    expect(page.router.url).toBe('/');
    const tabs = page.fx.nativeElement.querySelectorAll('.models-tabs__tab') as HTMLButtonElement[];
    expect([...tabs].map((tab) => tab.textContent?.trim())).toEqual(['Active models', 'Model requests']);
    expect(page.fx.nativeElement.querySelector('app-data-table')).not.toBeNull();
    expect(page.fx.nativeElement.querySelector('app-model-requests')).toBeNull();
  });

  it('switches to Model requests on tab click and mirrors it into ?tab=', async () => {
    const page = await boot();
    fixture = page.fx;
    const tabs = page.fx.nativeElement.querySelectorAll('.models-tabs__tab') as HTMLButtonElement[];

    tabs[1].click();
    await tick();
    page.fx.detectChanges();

    expect(page.router.url).toBe('/?tab=requests');
    expect(page.fx.nativeElement.querySelector('app-model-requests')).not.toBeNull();
    expect(page.fx.nativeElement.querySelector('app-data-table')).toBeNull();
  });

  it('renders the Model requests section when the URL asks for it', async () => {
    const page = await boot();
    fixture = page.fx;
    await page.router.navigateByUrl('/?tab=requests');
    page.fx.detectChanges();

    expect(page.fx.nativeElement.querySelector('app-model-requests')).not.toBeNull();
    expect(page.fx.nativeElement.querySelector('app-data-table')).toBeNull();
  });

  it('falls back to Active models for an unknown ?tab= value', async () => {
    const page = await boot();
    fixture = page.fx;
    await page.router.navigateByUrl('/?tab=bogus');
    page.fx.detectChanges();

    expect(page.fx.nativeElement.querySelector('app-data-table')).not.toBeNull();
    expect(page.fx.nativeElement.querySelector('app-model-requests')).toBeNull();
  });
});
