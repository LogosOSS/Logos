import { Injectable, inject } from '@angular/core';
import { AuthService } from './auth.service';
import { getPasskeyConfig, keycloakIssuer } from '../keycloak';
import { registerPasskey } from '../passkey';

const PASSKEY_TYPE = 'webauthn-passwordless';
const SESSION_EXPIRED = 'Your session has expired. Sign in again to manage your passkeys.';

export interface AccountPasskey {
  id: string;
  label: string;
  createdAt: number | null;
  removable: boolean;
  credentialId?: string;
}

interface CredentialContainer {
  type: string;
  removeable?: boolean;
  userCredentialMetadatas?: {
    credential?: {
      id?: string;
      type?: string;
      userLabel?: string;
      createdDate?: number;
      credentialData?: string;
    };
  }[];
}

/** Extract only passwordless WebAuthn credentials from Keycloak's account response. */
export function accountPasskeys(value: unknown): AccountPasskey[] {
  if (!Array.isArray(value)) throw new Error('The account service returned an unexpected response. Try again later.');
  const containers = (value as CredentialContainer[]).filter(item => item?.type === PASSKEY_TYPE);
  if (!containers.length) {
    throw new Error('Passkey management is not enabled for this account. Contact your Logos administrator.');
  }
  return containers.flatMap(container => {
    if (!Array.isArray(container.userCredentialMetadatas)) {
      throw new Error('The account service did not return your passkeys. Try again later.');
    }
    return container.userCredentialMetadatas.flatMap(metadata => {
      const credential = metadata?.credential;
      if (credential?.type !== PASSKEY_TYPE || typeof credential.id !== 'string' || !credential.id) return [];
      let credentialId: string | undefined;
      try {
        const data = JSON.parse(credential.credentialData ?? '{}') as { credentialId?: unknown };
        if (typeof data.credentialId === 'string' && /^[A-Za-z0-9+/_-]+={0,2}$/.test(data.credentialId)) {
          credentialId = data.credentialId;
        }
      } catch {
        // Older credentials can omit WebAuthn metadata; they remain manageable.
      }
      return [{
        id: credential.id,
        label: typeof credential.userLabel === 'string' && credential.userLabel.trim()
          ? credential.userLabel.trim() : 'Unnamed passkey',
        createdAt: typeof credential.createdDate === 'number' && Number.isFinite(credential.createdDate)
          ? credential.createdDate : null,
        removable: container.removeable === true,
        credentialId,
      }];
    });
  });
}

@Injectable({ providedIn: 'root' })
export class PasskeyManagementService {
  private readonly auth = inject(AuthService);

  private get endpoint(): string {
    return `${keycloakIssuer(this.auth.keycloak)}/account/credentials`;
  }

  private async token(forceRefresh = false): Promise<string> {
    const token = await this.auth.freshToken(forceRefresh);
    if (!token) throw new Error(SESSION_EXPIRED);
    return token;
  }

  private async request(url: string, method: 'GET' | 'DELETE'): Promise<Response> {
    let response: Response;
    for (let attempt = 0; ; attempt += 1) {
      const token = await this.token(attempt > 0);
      try {
        response = await fetch(url, {
          method,
          headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' },
          credentials: 'omit',
          cache: 'no-store',
          redirect: 'error',
        });
      } catch {
        throw new Error('The account service could not be reached. Try again or contact your Logos administrator.');
      }
      if (response.status !== 401 || attempt > 0) break;
    }
    if (response.status === 401) throw new Error(SESSION_EXPIRED);
    if (response.status === 403) {
      throw new Error('Your account did not allow this action. Sign in again, then contact your Logos administrator if it persists.');
    }
    if (!response.ok) throw new Error('The account service could not complete this action. Please try again.');
    return response;
  }

  async list(): Promise<AccountPasskey[]> {
    const response = await this.request(`${this.endpoint}?type=${PASSKEY_TYPE}`, 'GET');
    let data: unknown;
    try {
      data = await response.json();
    } catch {
      throw new Error('The account service returned an unexpected response. Try again later.');
    }
    return accountPasskeys(data);
  }

  async add(label: string): Promise<void> {
    const trimmed = label.trim();
    if (!trimmed || trimmed.length > 64) throw new Error('Enter a passkey name between 1 and 64 characters.');
    const existing = await this.list();
    if (existing.some(passkey => passkey.label === trimmed)) {
      throw new Error('A passkey already has this name. Choose a different name.');
    }
    const config = getPasskeyConfig();
    await registerPasskey(
      keycloakIssuer(this.auth.keycloak), this.auth.keycloak.clientId ?? '', await this.token(),
      config.rpId, config.rpName,
      {
        label: trimmed,
        excludeCredentialIds: existing.flatMap(passkey => passkey.credentialId ? [passkey.credentialId] : []),
        freshToken: () => this.token(),
      },
    );
  }

  async remove(id: string): Promise<void> {
    // Recheck both type and server policy before a destructive request. Never
    // forward arbitrary credential IDs (passwords, OTP, or security keys).
    const passkey = (await this.list()).find(candidate => candidate.id === id);
    if (!passkey?.removable) {
      throw new Error('This passkey is no longer available for removal. Refresh the list and try again.');
    }
    await this.request(`${this.endpoint}/${encodeURIComponent(passkey.id)}`, 'DELETE');
  }
}
