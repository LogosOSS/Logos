import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';

import { TeamManagementService } from '../../../../core/services/team-management.service';
import { ProvidersTabComponent } from './providers-tab';

async function createFixture(enabled: number[] = []) {
  const service = {
    getTeamPermissionCatalog: vi.fn().mockResolvedValue([{ id: 4 }, { id: 5 }, { id: 6 }]),
    getAllProviders: vi.fn().mockResolvedValue([
      { id: 4, name: 'Azure Example', provider_type: 'cloud', cloud_provider_type: 'azure' },
      { id: 5, name: 'Other Cloud', provider_type: 'cloud', cloud_provider_type: 'openai' },
      { id: 6, name: 'Unassigned Azure', provider_type: 'cloud', cloud_provider_type: 'azure' },
    ]),
    getTeamProviderPermissions: vi.fn().mockResolvedValue([4, 5]),
    getTeamAzureAutoAccess: vi.fn().mockResolvedValue(enabled),
    setTeamAzureAutoAccess: vi.fn().mockResolvedValue(undefined),
  };
  await TestBed.configureTestingModule({
    imports: [ProvidersTabComponent],
    providers: [{ provide: TeamManagementService, useValue: service }],
  }).compileComponents();

  const fixture = TestBed.createComponent(ProvidersTabComponent);
  fixture.componentRef.setInput('teamId', 7);
  fixture.componentRef.setInput('canEdit', true);
  fixture.detectChanges();
  await vi.waitFor(() => expect(fixture.componentInstance.loading()).toBe(false));
  fixture.detectChanges();
  const controls = () => [...(fixture.nativeElement as HTMLElement).querySelectorAll<HTMLInputElement>('input[type="checkbox"]')];
  return { fixture, controls, service };
}

describe('Providers tab Azure model access', () => {
  it('shows a default-off toggle only on assigned Azure providers and saves and clears the preference', async () => {
    const { fixture, controls, service } = await createFixture();
    expect(controls()).toHaveLength(1);
    const control = controls()[0];
    expect(control.getAttribute('aria-label')).toContain('Azure Example');
    expect(control.checked).toBe(false);
    control.click();
    await vi.waitFor(() => expect(fixture.componentInstance.updatingAutoAccess().size).toBe(0));
    fixture.detectChanges();
    expect(service.setTeamAzureAutoAccess).toHaveBeenNthCalledWith(1, 7, 4, true);
    expect(control.checked).toBe(true);
    control.click();
    await vi.waitFor(() => expect(fixture.componentInstance.updatingAutoAccess().size).toBe(0));
    fixture.detectChanges();
    expect(service.setTeamAzureAutoAccess).toHaveBeenNthCalledWith(2, 7, 4, false);
    expect(control.checked).toBe(false);
  });

  it('retains the last confirmed preference and shows an error if saving fails', async () => {
    const { fixture, controls, service } = await createFixture([4]);
    const control = controls()[0];
    service.setTeamAzureAutoAccess.mockRejectedValueOnce(new Error('Save failed'));
    expect(control.checked).toBe(true);
    control.click();
    await vi.waitFor(() => expect(fixture.componentInstance.autoAccessError()).not.toBe(""));
    fixture.detectChanges();
    expect(control.checked).toBe(true);
    expect(fixture.nativeElement.textContent).toContain('Could not update automatic model access');
  });
});
