# Next

Ideas left out of v1 on purpose. The v1 scope is fixed: four read-only tools.

## Safety

- **Refuse write permissions at startup.** Read the token's `roles` claim and refuse to run if any role is not a `.Read.All` permission. This would catch someone adding a write permission to the app registration later.
- **Certificate instead of a client secret.** A certificate's private key is harder to leak than a password in a file.
- **Audit log rotation.** `audit.log` grows forever today.

## Tools

- **Password reset, with human approval.** The first write tool. It needs its own app registration with one narrow write permission, an approval step before anything runs, and an audit line recording who approved.
- **Option to include users who have never signed in** in `find_inactive_users`.
- **Option to skip disabled accounts** in the MFA and inactive lists.
- **Friendly license names** (show "Microsoft 365 E3" beside `SPE_E3`).

## Reliability

- **Retry on 429.** Wait for the `Retry-After` time Graph sends, then try once more.
- **Smaller pages.** Ask Graph for 26 rows with `$top` where the endpoint supports it, instead of a full page.
