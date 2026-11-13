# Passkey browser tests

The regular suite uses the local Logos E2E stack described in
[`../../e2e/README.md`](../../e2e/README.md). Run from `logos-ui/`:

```sh
npm run e2e -- passkeys.spec.ts
```

`passkeys.spec.ts` signs in through the real local Keycloak, then fixtures the
account and passkey-provider responses. It exercises the settings UI, a real
browser WebAuthn ceremony using a virtual authenticator, keyboard interaction,
375px mobile layout, account errors, and removal confirmation. It runs in the
standard CI stack without the custom passkey provider.

`passkeys-live.spec.ts` is an additional opt-in integration test. Run it only
against a disposable local realm with the redirectless passkey provider,
account roles, token audience, CORS origins, and passwordless authentication
flow configured as described in
[`../../docs/passkey-login.md`](../../docs/passkey-login.md).
The UI and Keycloak issuer must both use the hostname `localhost`, and the
WebAuthn RP ID must be `localhost`. The regular authentication setup uses the
seeded local admin account.

```sh
E2E_UI_URL=http://localhost:4300 E2E_PASSKEYS_LIVE=1 npm run e2e -- passkeys-live.spec.ts
```

The live test creates two randomly named credentials with separate virtual
authenticators, reloads their list, checks cancellation, removes one, verifies
that the removed passkey cannot sign in and the retained passkey can, then
removes its remaining test credential. It does not select pre-existing
credentials for deletion or store virtual-authenticator secrets on disk.
Without `E2E_PASSKEYS_LIVE=1`, this test is skipped.
