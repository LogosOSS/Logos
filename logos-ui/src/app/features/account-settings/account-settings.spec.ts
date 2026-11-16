import { ComponentFixture, TestBed } from '@angular/core/testing';
import { AccountPasskey, PasskeyManagementService } from '../../core/auth/services/passkey-management.service';
import { AccountSettings } from './account-settings';

const passkey: AccountPasskey = { id: 'key-1', label: 'Personal laptop', createdAt: 1_700_000_000_000, removable: true };

describe('AccountSettings', () => {
  let fixture: ComponentFixture<AccountSettings>;
  let component: AccountSettings;
  const service = { list: vi.fn(), add: vi.fn(), remove: vi.fn() };

  beforeEach(async () => {
    vi.clearAllMocks();
    vi.stubGlobal('isSecureContext', true);
    vi.stubGlobal('PublicKeyCredential', class {});
    vi.stubGlobal('navigator', { credentials: {}, userAgent: 'Mac Chrome' });
    service.list.mockResolvedValue([passkey]);
    service.add.mockResolvedValue(undefined);
    service.remove.mockResolvedValue(undefined);
    await TestBed.configureTestingModule({
      imports: [AccountSettings], providers: [{ provide: PasskeyManagementService, useValue: service }],
    }).compileComponents();
    fixture = TestBed.createComponent(AccountSettings);
    component = fixture.componentInstance;
  });

  afterEach(() => { fixture.destroy(); vi.unstubAllGlobals(); });

  it('shows a loading state before listing passkeys', async () => {
    let finish!: (value: AccountPasskey[]) => void;
    service.list.mockReturnValue(new Promise<AccountPasskey[]>(resolve => { finish = resolve; }));
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('Loading passkeys');
    finish([passkey]);
    await fixture.whenStable();
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('Personal laptop');
    expect(fixture.nativeElement.textContent).not.toContain('Loading passkeys');
  });

  it('shows errors with retry instead of claiming there are no passkeys', async () => {
    service.list.mockRejectedValueOnce(new Error('Account access denied.'));
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('Account access denied.');
    expect(fixture.nativeElement.textContent).not.toContain('No passkeys yet');
    expect(fixture.nativeElement.querySelector('#add-passkey').disabled).toBe(true);
    service.list.mockResolvedValueOnce([]);
    await component.load();
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('No passkeys yet');
  });

  it('keeps the add form open after cancellation and can retry successfully', async () => {
    await component.load();
    component.openAdd();
    component.name.set('Backup key');
    service.add.mockRejectedValueOnce(new DOMException('Cancelled', 'NotAllowedError'));
    await component.add();
    expect(component.addOpen()).toBe(true);
    expect(component.addError()).toContain('creation was cancelled');
    await component.add();
    expect(component.addOpen()).toBe(false);
    expect(component.notice()).toContain('Passkey added');
    expect(service.add).toHaveBeenLastCalledWith('Backup key');
    expect(service.list).toHaveBeenCalledTimes(2);
  });

  it('requires confirmation and explains the central account scope before removal', async () => {
    await component.load();
    component.openRemove(passkey);
    expect(component.removalMessage()).toContain('Personal laptop');
    expect(component.removalMessage()).toContain('any other application');
    expect(service.remove).not.toHaveBeenCalled();
    component.closeRemove(false);
    await component.remove();
    expect(service.remove).not.toHaveBeenCalled();
    component.openRemove(passkey);
    service.list.mockResolvedValue([]);
    await component.remove();
    expect(service.remove).toHaveBeenCalledWith('key-1');
    expect(component.selected()).toBeNull();
    expect(component.passkeys()).toEqual([]);
  });

  it('preserves the selected passkey and displays a failed deletion without removing the row', async () => {
    await component.load();
    component.openRemove(passkey);
    service.remove.mockRejectedValueOnce(new Error('Removal denied.'));
    await component.remove();
    expect(component.selected()).toEqual(passkey);
    expect(component.passkeys()).toEqual([passkey]);
    expect(component.removeError()).toBe('Removal denied.');
  });

  it('blocks duplicate mutation requests and closing the dialog while saving', async () => {
    let finish!: () => void;
    service.add.mockReturnValueOnce(new Promise<void>(resolve => { finish = resolve; }));
    await component.load();
    component.openAdd();
    const pending = component.add();
    await component.add();
    component.closeAdd(false);
    expect(service.add).toHaveBeenCalledTimes(1);
    expect(component.addOpen()).toBe(true);
    finish();
    await pending;
  });

  it('does not claim a successful registration failed when refreshing the list fails', async () => {
    await component.load();
    component.openAdd();
    service.list.mockRejectedValueOnce(new Error('Refresh unavailable.'));
    await component.add();
    expect(component.addOpen()).toBe(false);
    expect(component.notice()).toContain('Passkey added');
    expect(component.loadError()).toBe('Refresh unavailable.');
  });
});
