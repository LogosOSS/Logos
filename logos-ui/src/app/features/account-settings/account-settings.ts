import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { getDeviceName, isPasskeyCancellation, isPasskeySupported, passkeyErrorMessage } from '../../core/auth/passkey';
import { AccountPasskey, PasskeyManagementService } from '../../core/auth/services/passkey-management.service';
import { ModalFormComponent } from '../../shared/components/modal/modal-form/modal-form';
import { ModalConfirmComponent } from '../../shared/components/modal/modal-confirm/modal-confirm';
import { ErrorMessageComponent } from '../../shared/components/error-message/error-message';

@Component({
  selector: 'app-account-settings',
  standalone: true,
  imports: [DatePipe, ModalFormComponent, ModalConfirmComponent, ErrorMessageComponent],
  templateUrl: './account-settings.html',
  styleUrl: './account-settings.scss',
})
export class AccountSettings implements OnInit {
  private readonly service = inject(PasskeyManagementService);
  readonly supported = isPasskeySupported();
  readonly passkeys = signal<AccountPasskey[]>([]);
  readonly loading = signal(true);
  readonly loadError = signal('');
  readonly notice = signal('');
  readonly addOpen = signal(false);
  readonly name = signal('');
  readonly addError = signal('');
  readonly adding = signal(false);
  readonly selected = signal<AccountPasskey | null>(null);
  readonly removeError = signal('');
  readonly removing = signal(false);
  readonly busy = computed(() => this.loading() || this.adding() || this.removing());
  readonly removalMessage = computed(() => {
    const label = this.selected()?.label ?? '';
    return `Remove “${label}” from your central account? It will no longer work in Logos or any other application using this account. This cannot be undone.`;
  });

  ngOnInit(): void {
    void this.load();
  }

  async load(): Promise<void> {
    this.loading.set(true);
    this.loadError.set('');
    try {
      this.passkeys.set(await this.service.list());
    } catch (error) {
      this.loadError.set(passkeyErrorMessage(error, 'Your passkeys could not be loaded. Please try again.'));
    } finally {
      this.loading.set(false);
    }
  }

  openAdd(): void {
    if (this.busy() || this.loadError() || !this.supported) return;
    const base = getDeviceName();
    let name = base;
    for (let suffix = 2; this.passkeys().some(passkey => passkey.label === name); suffix += 1) {
      name = `${base} (${suffix})`;
    }
    this.name.set(name);
    this.addError.set('');
    this.notice.set('');
    this.addOpen.set(true);
  }

  closeAdd(visible: boolean): void {
    if (!this.adding()) this.addOpen.set(visible);
  }

  async add(event?: Event): Promise<void> {
    event?.preventDefault();
    if (this.busy() || !this.addOpen()) return;
    this.adding.set(true);
    this.addError.set('');
    try {
      await this.service.add(this.name());
      this.addOpen.set(false);
      this.notice.set('Passkey added. You can use it the next time you sign in.');
      await this.load();
    } catch (error) {
      this.addError.set(isPasskeyCancellation(error)
        ? 'Passkey creation was cancelled. You can try again when you are ready.'
        : passkeyErrorMessage(error, 'The passkey could not be added. Please try again.'));
    } finally {
      this.adding.set(false);
    }
  }

  openRemove(passkey: AccountPasskey): void {
    if (this.busy() || this.loadError() || !passkey.removable) return;
    this.notice.set('');
    this.removeError.set('');
    this.selected.set(passkey);
  }

  closeRemove(visible: boolean): void {
    if (!visible && !this.removing()) this.selected.set(null);
  }

  async remove(): Promise<void> {
    const selected = this.selected();
    if (this.busy() || !selected) return;
    this.removing.set(true);
    this.removeError.set('');
    try {
      await this.service.remove(selected.id);
      this.selected.set(null);
      this.passkeys.update(passkeys => passkeys.filter(passkey => passkey.id !== selected.id));
      this.notice.set(`“${selected.label}” was removed from your central account.`);
      await this.load();
      // The remove button no longer exists; give keyboard users a stable target.
      setTimeout(() => document.getElementById('passkeys-title')?.focus(), 0);
    } catch (error) {
      this.removeError.set(passkeyErrorMessage(error, 'The passkey could not be removed. Please try again.'));
    } finally {
      this.removing.set(false);
    }
  }
}
