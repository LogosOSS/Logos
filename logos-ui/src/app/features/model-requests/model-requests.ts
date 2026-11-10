import { ChangeDetectionStrategy, Component, OnInit, computed, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import {
  ModelLicense,
  ModelRequest,
  ModelRequestService,
} from '../../core/services/model-request.service';
import { ModalConfirmComponent } from '../../shared/components/modal/modal-confirm/modal-confirm';
import { ModalFormComponent } from '../../shared/components/modal/modal-form/modal-form';
import { ErrorMessageComponent } from '../../shared/components/error-message/error-message';
import { AuthService } from '../../core/auth/services/auth.service';

/**
 * The model-request voting section, embedded in the Models page.
 *
 * Ask for a model Logos does not serve yet by voting for it, and see the
 * running list of what everyone has voted for, most-voted first. Each user gets
 * one vote per model and can take it back — the Vote/Undo control on each row
 * reflects the caller's own vote state. Submitting is a demand signal, not a
 * configuration change: it records demand so operators can see what to add next.
 * Every request names one of the caller's teams (preselected to the first
 * one); the first team to ask for a model is recorded on it. A failed vote
 * (model already available, not found on Hugging Face, precheck timeout) is
 * surfaced in a modal; the typed name is kept so it can be corrected instead
 * of retyped.
 *
 * The Logos admin additionally sees each row's license agreement status: the
 * license is fetched through the central Hugging Face token (a gated model's
 * only once access is granted there) and the admin accepts it in a dialog —
 * or revokes an acceptance that turned out wrong. Team-level acceptances
 * (app admins, per team) build on the global one and live elsewhere.
 */
@Component({
  selector: 'app-model-requests',
  standalone: true,
  imports: [CommonModule, ModalConfirmComponent, ModalFormComponent, ErrorMessageComponent],
  templateUrl: './model-requests.html',
  styleUrl: './model-requests.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ModelRequests implements OnInit {
  private service = inject(ModelRequestService);
  private auth = inject(AuthService);

  requests = signal<ModelRequest[]>([]);
  loading = signal(false);
  /** True while the (slow, remote) recheck of a refresh is running. */
  rechecking = signal(false);
  submitting = signal(false);
  /** Load failure for the list, shown inline (load errors are not vote outcomes). */
  error = signal<string | null>(null);
  /** A failed vote, shown in a modal; null while no modal is open. */
  voteError = signal<string | null>(null);
  notice = signal<string | null>(null);
  name = signal('');
  showHelpImage = signal(false);
  /** The caller's teams — the only ones a request may name. */
  teams = computed(() => this.auth.currentUser()?.teams ?? []);
  /** The team the next request is for; null while none is (or can be) chosen. */
  teamId = signal<number | null>(null);
  /** The gated model whose Hugging Face dialog is open; null while closed. */
  gatedTarget = signal<ModelRequest | null>(null);
  // The license agreement state — the stored license and the Logos admin's
  // acceptance of it — is a Logos admin concern, so the license column and
  // its dialog are only offered to that role.
  readonly isLogosAdmin = computed(() => this.auth.role() === 'logos_admin');

  /** The row whose license dialog is open; null while it is closed. */
  licenseTarget = signal<ModelRequest | null>(null);
  /**
   * Bumped every time the dialog opens or closes; an in-flight license load
   * applies its result only for the generation it started in, so a slow
   * answer can never display one model's agreement while another is open.
   */
  private licenseGeneration = 0;
  /** The stored license for the open dialog's row; null until loaded. */
  license = signal<ModelLicense | null>(null);
  licenseLoading = signal(false);
  licenseError = signal<string | null>(null);
  /** The "I accept…" checkbox of the open dialog. */
  acceptChecked = signal(false);
  accepting = signal(false);
  revoking = signal(false);

  async ngOnInit(): Promise<void> {
    // Preselect the first team: a member of exactly one team should not have
    // to make the choice; a member of several can still switch it.
    const first = this.teams()[0];
    if (first) this.teamId.set(first.id);
    await this.refresh();
  }

  /**
   * Refresh = render what is stored, then re-verify, then render the fresh
   * verdicts. The recheck is a slow remote scan, so the (local) list is
   * loaded and rendered first instead of waiting behind it.
   */
  async refresh(): Promise<void> {
    await this.loadRequests();
    this.rechecking.set(true);
    try {
      // Best effort: a license accepted since the last check unflags the row,
      // and a precheck failure must not keep the list from re-rendering.
      await this.service.recheckGated().catch(() => {});
    } finally {
      this.rechecking.set(false);
    }
    await this.loadRequests();
  }

  /** Load the stored list (no recheck) and render it, or report the failure. */
  private async loadRequests(): Promise<void> {
    this.loading.set(true);
    try {
      this.requests.set(await this.service.list());
      this.error.set(null);
    } catch {
      this.error.set('Could not load model requests.');
    } finally {
      this.loading.set(false);
    }
  }

  /** Vote for a model typed into the form (a new request). */
  async submit(): Promise<void> {
    const name = this.name().trim();
    if (!name || this.teamId() === null || this.submitting()) return;
    await this.castVote(name, this.teamId(), () => this.name.set(''));
  }

  /** Toggle the caller's vote on a listed model. */
  async toggle(request: ModelRequest): Promise<void> {
    if (this.submitting()) return;
    if (request.has_voted) {
      // Undoing a vote is the caller's own, so it needs no team.
      await this.castVote(request.name, null, null, { undo: true });
    } else if (this.teamId() !== null) {
      await this.castVote(request.name, this.teamId(), null);
    }
  }

  private async castVote(
    name: string,
    teamId: number | null,
    onSuccess: (() => void) | null,
    opts: { undo?: boolean } = {},
  ): Promise<void> {
    this.submitting.set(true);
    this.error.set(null);
    this.notice.set(null);
    try {
      if (opts.undo) {
        await this.service.undoVote(name);
        this.notice.set(`Vote removed for "${name}".`);
      } else if (teamId === null) {
        // The Vote control is disabled without a selected team; backstop.
        return;
      } else {
        const updated = await this.service.vote(name, teamId);
        this.notice.set(
          `Thanks — "${updated.name}" now has ${updated.request_count} vote${
            updated.request_count === 1 ? '' : 's'
          }.`,
        );
      }
      onSuccess?.();
      // The vote just updated the server state for this row; a plain list
      // reload picks it up without re-running the remote recheck.
      await this.loadRequests();
    } catch (err: unknown) {
      // Vote outcomes are surfaced in a modal, not inline: the message needs
      // to be seen even though the user's attention is on the button.
      this.voteError.set(this.messageOf(err) ?? 'Something went wrong with the model request.');
    } finally {
      this.submitting.set(false);
    }
  }

  /** Close the failed-vote modal. */
  closeVoteError(): void {
    this.voteError.set(null);
  }

  /** Open the Hugging Face dialog for a gated model. */
  openGatedDialog(request: ModelRequest): void {
    this.gatedTarget.set(request);
  }

  /** Close the Hugging Face dialog (cancel, the backdrop or the close button). */
  closeGatedDialog(): void {
    this.gatedTarget.set(null);
  }

  /**
   * Open the model's Hugging Face page — where the license agreement is
   * accepted — in a new tab, then dismiss the dialog.
   */
  goToHuggingFace(): void {
    const target = this.gatedTarget();
    if (target) {
      this.openHuggingFacePage(target.name);
    }
    this.gatedTarget.set(null);
  }

  /** The row's license flag as its hover/aria text. */
  licenseFlagTitle(request: ModelRequest): string {
    if (request.license_status === 'available') {
      return 'License agreement not accepted yet';
    }
    if (request.license_status === 'no_license') {
      return 'No license information found on Hugging Face';
    }
    return request.is_gated
      ? 'The model is gated — its license can only be fetched once Hugging Face grants access'
      : 'The license has not been fetched yet';
  }

  /** Open the license dialog for a row and load its stored license. */
  openLicenseDialog(request: ModelRequest): void {
    this.licenseGeneration++;
    this.licenseTarget.set(request);
    this.license.set(null);
    this.licenseError.set(null);
    this.acceptChecked.set(false);
    void this.loadLicense();
  }

  /** Close the license dialog (cancel, the backdrop or the close button). */
  closeLicenseDialog(): void {
    this.licenseGeneration++;
    this.licenseTarget.set(null);
    // An in-flight load will not clear the flag for the closed session
    // (its generation no longer matches), so the close does.
    this.licenseLoading.set(false);
  }

  /**
   * Open the model's Hugging Face page — where a gated model's agreement is
   * accepted — in a new tab, then dismiss the license dialog.
   */
  goToHuggingFaceFromLicense(): void {
    const target = this.licenseTarget();
    if (target) {
      this.openHuggingFacePage(target.name);
    }
    this.licenseTarget.set(null);
  }

  /**
   * Record the Logos admin's acceptance of the open model's license. Refused
   * while the checkbox is unchecked — an unchecked box is a "not yet", and
   * only an explicit revocation clears an existing acceptance.
   */
  async acceptLicense(): Promise<void> {
    const target = this.licenseTarget();
    if (!target || !this.acceptChecked() || this.accepting()) return;
    this.accepting.set(true);
    this.licenseError.set(null);
    try {
      await this.service.approveLicense(target.id);
      this.licenseTarget.set(null);
      this.notice.set(`License agreement for "${target.name}" accepted.`);
      await this.loadRequests();
    } catch (err: unknown) {
      this.licenseError.set(this.messageOf(err) ?? 'Could not record the acceptance.');
    } finally {
      this.accepting.set(false);
    }
  }

  /** Revoke the Logos admin's acceptance of the open model's license. */
  async revokeLicense(): Promise<void> {
    const target = this.licenseTarget();
    if (!target || this.revoking()) return;
    this.revoking.set(true);
    this.licenseError.set(null);
    try {
      await this.service.revokeLicense(target.id);
      this.licenseTarget.set(null);
      this.notice.set(`License agreement for "${target.name}" revoked.`);
      await this.loadRequests();
    } catch (err: unknown) {
      this.licenseError.set(this.messageOf(err) ?? 'Could not revoke the acceptance.');
    } finally {
      this.revoking.set(false);
    }
  }

  private async loadLicense(): Promise<void> {
    const target = this.licenseTarget();
    const generation = this.licenseGeneration;
    if (!target) return;
    this.licenseLoading.set(true);
    this.licenseError.set(null);
    try {
      const license = await this.service.getLicense(target.id);
      // The dialog may have closed or switched to another model in the
      // meantime: a stale answer must not render then.
      if (generation !== this.licenseGeneration) return;
      this.license.set(license);
    } catch (err: unknown) {
      if (generation !== this.licenseGeneration) return;
      this.licenseError.set(this.messageOf(err) ?? 'Could not load the license.');
    } finally {
      if (generation === this.licenseGeneration) {
        this.licenseLoading.set(false);
      }
    }
  }

  private openHuggingFacePage(name: string): void {
    window.open('https://huggingface.co/' + name, '_blank', 'noopener,noreferrer');
  }

  /** Team selector change; the empty option clears the selection. */
  onTeamChange(value: string): void {
    this.teamId.set(value === '' ? null : Number(value));
  }

  private messageOf(err: unknown): string | null {
    const body = (err as { error?: { error?: string; detail?: string } })?.error;
    return body?.error ?? body?.detail ?? null;
  }
}