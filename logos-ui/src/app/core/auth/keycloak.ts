import Keycloak from 'keycloak-js';
import { InjectionToken } from '@angular/core';

export const KEYCLOAK = new InjectionToken<Keycloak>('KEYCLOAK');

let instance: Keycloak | null = null;

/** Passkey config served by /api/info. Empty => fall back to hostname/"Logos".*/
export interface PasskeyConfig { rpId?: string; rpName?: string; }
let passkeyConfig: PasskeyConfig = {};

/** Returns the initialized singleton; throws if init has not run. */
export function getKeycloak(): Keycloak {
  if (!instance) throw new Error('Keycloak not initialized');
  return instance;
}

/** WebAuthn RP id/name from /api/info; both optional.*/
export function getPasskeyConfig(): PasskeyConfig {
  return passkeyConfig;
}

/** Splits a Keycloak issuer `{url}/realms/{realm}` into `[url, realm]`. */
export function splitIssuer(issuer: string): [string, string] {
  const marker = '/realms/';
  const i = issuer.indexOf(marker);
  if (i < 0) throw new Error(`Issuer is not in {url}/realms/{realm} form: ${issuer}`);
  const url = issuer.slice(0, i);
  const realm = issuer.slice(i + marker.length).replace(/\/+$/, '');
  if (!realm) throw new Error(`Issuer has no realm: ${issuer}`);
  return [url, realm];
}

/** Reconstructs the issuer URL from a configured keycloak-js instance. */
export function keycloakIssuer(kc: Keycloak): string {
  const url = (kc.authServerUrl ?? '').replace(/\/+$/, '');
  return `${url}/realms/${kc.realm}`;
}

/**
 * Fetches runtime config from /api/info and initializes the keycloak-js
 * singleton with a redirect SSO check and PKCE before the app renders.
 */
export async function initKeycloak(): Promise<Keycloak> {
  const res = await fetch('/api/info');
  if (!res.ok) throw new Error(`Failed to load runtime config from /api/info: ${res.status}`);
  const data = (await res.json()) as {
    keycloak?: { issuer?: string; client_id?: string; passkey_rp_id?: string; passkey_rp_name?: string };
  };
  const issuer = data.keycloak?.issuer;
  const clientId = data.keycloak?.client_id;
  if (!issuer || !clientId) {
    throw new Error('Server /api/info response missing keycloak.issuer or keycloak.client_id');
  }
  // Shared TUM Keycloak serves the parent-domain RP id so passkeys work
  // across *.aet.cit.tum.de. Blank => the ceremony falls back to the hostname.
  passkeyConfig = {
    rpId: data.keycloak?.passkey_rp_id || undefined,
    rpName: data.keycloak?.passkey_rp_name || undefined,
  };
  const [url, realm] = splitIssuer(issuer);
  const kc = new Keycloak({ url, realm, clientId });
  // Use the top-level SSO redirect. A blocked third-party cookie iframe must not
  // prevent application startup; PKCE and callback verification stay with Keycloak.
  await kc.init({
    onLoad: 'check-sso',
    pkceMethod: 'S256',
    checkLoginIframe: false,
  });
  kc.onTokenExpired = () => { void kc.updateToken(30); };
  instance = kc;
  return kc;
}
