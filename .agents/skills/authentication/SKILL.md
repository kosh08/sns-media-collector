---
name: sns-authentication
description: Handle or modify X/Twitter or pixiv authentication, account login, cookies, OAuth, stored credentials, login-state UI, or authentication tests in SNS Media Collector. Use when work touches login, account separation, cookies.txt, browser cookies, embedded login, pixiv OAuth, refresh tokens, or auth failures.
---

# Authentication

Preserve the application's account-isolated authentication model.

## Security requirements

- Never print, log, commit, paste, expose, or add test fixtures containing real:
  - X cookies
  - pixiv refresh tokens
  - OAuth authorization codes
  - session credentials
- Do not store secret values in `accounts.json`.
- Tests must not require a real user's credentials.

## X

The preferred authentication flow is the application's account-specific embedded X login.

Each account must remain isolated from other accounts.

Important behavior:

- Authentication should not depend on the user's default Chrome/Brave login state.
- The preferred stored cookie location is account-specific.
- A logout state must not be saved back as if it were valid authentication.
- After importing or saving authentication, perform an authentication check where practical.
- Keep `cookies.txt` and browser-cookie import only as compatibility/fallback paths unless explicitly removing them is requested.

When changing X authentication, verify that one profile cannot accidentally read or overwrite another profile's authentication state.

## pixiv

The preferred flow is the application's embedded pixiv OAuth flow.

Important behavior:

- The application receives the OAuth callback.
- The refresh token is stored in the account-specific gallery-dl SQLite cache.
- Users should not normally need to copy OAuth codes from browser developer tools or manually paste refresh tokens.
- Multiple pixiv accounts must remain isolated.

When changing pixiv authentication, preserve compatibility with existing account data unless the task explicitly requires migration.

## UI

Authentication status should remain easy to understand.

Existing concepts include:

- authenticated / saved state
- login required state
- quick login or authentication refresh

Do not display secret credential values in the UI.

## Validation

After authentication-related changes:

1. Run relevant automated authentication/configuration tests.
2. Verify profile isolation.
3. Verify no credentials are emitted into logs or configuration files.
4. Verify failed or logged-out authentication cannot overwrite a previously valid state incorrectly.
5. If embedded browser behavior changed, include the relevant Qt/WebEngine regression or smoke tests.
