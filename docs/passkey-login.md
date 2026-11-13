# Passkey Login

Logos supports **in-page passkey (WebAuthn) login**. The user does not go to
a hosted login page. The "Sign in with a passkey" button on the login screen
runs the WebAuthn ceremony in the app and signs the user in.

## Implementation in `logos-ui`

The implementation is in `logos-ui/src/app/core/auth/` (Angular). These files
do the work:

- `passkey.ts` runs the WebAuthn ceremony and the silent token exchange.
- `keycloak.ts` manages the keycloak-js singleton. The singleton starts with
  check-sso and PKCE.
- `services/auth.service.ts` completes the login.

The flow has these steps:

1. `GET {issuer}/passkey/{clientId}/challenge` gets a WebAuthn challenge.
2. `navigator.credentials.get(...)` asks the browser for a discoverable
   (usernameless) passkey. The browser signs the challenge
   (`userVerification: "required"`).
3. `POST {issuer}/passkey/{clientId}/authenticate` sends the assertion. The
   Keycloak passkey provider verifies it and creates a Keycloak SSO session.
4. **Silent token retrieval**: a hidden `prompt=none` iframe completes
   authorization-code + PKCE against the new session
   (`silent-check-sso.html`). The app gets the tokens and the user sees no
   login page.
5. **Install into keycloak-js**: `AuthService.completeLogin()` puts the tokens
   into the keycloak-js instance of the app. The auth interceptor (`kc.token`)
   and its `updateToken()` refresh continue to work with no redirect.

Registration (`registerPasskey` in `passkey.ts`) uses the same flow. The steps
are `challenge`, then `navigator.credentials.create(...)`, then
`POST .../save`.

## Manage passkeys

Open the user menu at the bottom of the navigation. Select **Account settings**.
The `/settings` page is available to every authenticated user, including users without a team or API key.
See the [account settings guide](roles/app-developer.md#account-settings) for the complete procedure.

The page lists only `webauthn-passwordless` credentials from the Keycloak Account API.
It does not manage passwords, OTP credentials, or second-factor WebAuthn credentials.
The browser sends a fresh user access token with each request:

- `GET {issuer}/account/credentials?type=webauthn-passwordless` lists the credentials.
- `DELETE {issuer}/account/credentials/{id}` removes one credential.

Before removal, Logos checks that the credential is a passkey and that the account policy permits removal.
Keycloak checks account ownership with the user token. Logos does not use admin credentials or a privileged proxy.
The credential ID in the DELETE path is the Keycloak credential ID, not the WebAuthn credential ID.

**Add passkey** uses the existing registration provider with a chosen name.
It excludes existing WebAuthn credential IDs when Keycloak supplies them.
It refreshes the user token again after the browser prompt, before it saves the passkey.

**CAUTION:** A passkey belongs to the central Keycloak account.
Removal also stops that passkey from working in other applications that use the same account.
The confirmation identifies the passkey and explains this effect.

## Keycloak Prerequisites

These items are on the Keycloak side. They are not in this repository.

- Enable the **custom passkey provider** for the `logos` client. The provider
  exposes `{issuer}/passkey/{logos}/{health|challenge|authenticate|save}`.
- Configure the `logos` client for the silent flow. Add the UI origin to
  **Web Origins**. Add `{origin}/silent-check-sso.html` to **Valid Redirect
  URIs**.
- Configure the WebAuthn **passwordless policy** (resident key and user
  verification). The passkey **rpId** must be the same as the rpId of the
  registered credential.
- On the shared TUM Keycloak, passkeys are scoped to the parent domain. The
  rpId must be `aet.cit.tum.de` (NOT `logos.aet.cit.tum.de`). A page on
  `logos.aet.cit.tum.de` can use the parent as rpId. Then all
  `*.aet.cit.tum.de` apps share the credential.

### Account API access

Passkey login and Account API access have separate requirements.
A successful passkey login does not prove that account management is available.

1. Enable the built-in `account` client in the realm.
2. Give users the `account` client role `manage-account` for removal.
3. Include that role in the scope of the `logos` client.
4. Include the `roles` client scope and its client-role and audience mappers in issued access tokens.
5. Check that the token contains the `account` audience and `resource_access.account.roles` with `manage-account`.
6. Add the exact Logos origin to the `logos` client **Web Origins**.
7. Enable `webauthn-authenticator-passwordless` in an active authentication flow.

The built-in `manage-account` role permits a user to manage that user's account.
Do not give the application realm administration roles or a service-account secret.
`view-profile` can permit listing, but it does not permit removal.
If the realm requires a higher authentication level for removal, the current user session must satisfy that policy.

Keycloak lists credential types from active authentication flows.
If the passwordless flow is absent, stored passkeys can be absent from the Account API response.
Logos shows a configuration error in that case, rather than an empty passkey list.
If Keycloak marks a credential type as non-removable, Logos disables its remove buttons.

Check these settings in a test realm first. Use a normal user token to test listing and removal.
Use the browser Network panel to check CORS with the actual Logos origin.
HTTP 401 can indicate a missing account audience or an expired session.
HTTP 403 can indicate missing account roles or an authentication-level policy.
A browser network error can indicate an incorrect **Web Origins** entry.

The local development realm does not install the custom passkey provider by default.
Its seeded users and client scopes can also need the account settings above.
Production settings are external to this repository. Local tests do not verify the production client or the installed provider version.

The server sets the rpId with `KEYCLOAK_PASSKEY_RP_ID`. The server sends the
value to the UI through `/api/info`. The default in the production compose
file is `aet.cit.tum.de`. If the value is blank (dev), then `passkey.ts` uses
the current hostname (for example `localhost`).

## Status

- WebAuthn needs a secure context. `localhost` is a secure context for dev.
  All other hosts need HTTPS (prod is behind Traefik TLS).
- The silent `prompt=none` iframe needs to read the Keycloak session cookie.
  Verify this against the real Keycloak, because the handling of third-party
  cookies is different in different browsers.
