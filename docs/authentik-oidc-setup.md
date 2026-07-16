# Authentik OIDC setup for pve-helper

This is the intended setup for `pve-helper` when the app skeleton is ready.

The app uses native OIDC login against Authentik. A reverse proxy (e.g. Nginx Proxy
Manager) is only the TLS/reverse-proxy entrypoint, **not** the only authentication
boundary. The app validates the OIDC session and the required group itself.

This guide is written to be followed top to bottom by someone who is not fluent in
Authentik. Field names match the Authentik admin UI (date-versioned releases, e.g.
`20XX.x`). If your version differs slightly, the field is usually named the same even if
it has moved.

> Hostnames, groups and URLs below are **generic placeholders**. Substitute your own
> environment's values in `.env` (which is gitignored). Do not commit real values.

---

## Choosing the issuer URL (internal vs external)

OIDC requires three things to line up:

- The **browser** must reach the Authentik authorize + flow UI (interactive login).
- The **app server** must reach Authentik token/JWKS/userinfo (back-channel).
- The **issuer** in tokens must be consistent with the host the flow runs on (Authentik
  is host-aware and Authentik's own flow session cookie is set on that host).

For an **internal-only** app, point `OIDC_ISSUER_URL` at the **internal** Authentik URL
that fully serves the flow UI (`/if/flow/...`). Do not route internal logins through a
DMZ/external proxy that may only expose a subset of Authentik's paths. If the internal
URL uses an internal/private CA, the app container must trust that CA for the
back-channel calls — see **Part F.2**.

---

## Reference values (placeholders)

| Purpose | Value |
| --- | --- |
| Application name | `pve-helper` |
| Application slug | `pve-helper` |
| App external URL | `https://pve-helper.example.com` |
| Redirect URI | `https://pve-helper.example.com/auth/oidc/callback` |
| Required group | `pve-helper-admins` |
| Authentik URL | `https://auth.example.com` |
| Issuer URL (per-provider mode) | `https://auth.example.com/application/o/pve-helper/` |
| Discovery endpoint | `https://auth.example.com/application/o/pve-helper/.well-known/openid-configuration` |

There are **two separate group concepts** in this setup. Do not confuse them:

1. **Access binding** — controls *who is allowed to use the app at all* (enforced by
   Authentik). This is what actually gates login.
2. **Groups claim** — the list of group names Authentik puts *inside the token* so the
   app can double-check membership itself.

You need both. They are configured in different places (Part D and Part E).

---

## Prerequisites

- You can log in to Authentik as an administrator and open the **Admin interface**.
- DNS for the app and for Authentik resolves on the network the users/app are on.
- The required group exists in Authentik (see Part A).

---

## Part A — Verify the group exists

The group is often AD-backed and arrives via LDAP sync, so it may already be present.

1. Go to **Directory** -> **Groups**.
2. Search for the required group.
   - If it exists, open it and confirm your admin test user is a **member** (Members
     tab). You need a member account to test login later.
   - If it does **not** exist and is supposed to come from AD, fix the directory sync
     first, or (for testing only) create the group manually and add your user.

Do not continue until the group exists and has at least one member you can log in as.

---

## Part B — Create the Application and Provider

### B.1 Application step

| Field | Value |
| --- | --- |
| Name | `pve-helper` |
| Slug | `pve-helper` |
| **Group** | **LEAVE BLANK** — this only groups apps in the Authentik UI. It is **not** the authorization group. |
| Provider | (set in the next step) |
| Policy engine mode | `ANY` |

UI settings — **leave blank for now** (optional cosmetics): Launch URL (optionally the
app URL), Description, Publisher, Icon.

### B.2 Choose a provider type

- Select **OAuth2/OpenID Provider**.

### B.3 Configure the provider (in the wizard)

> **Important:** the *Create with Provider* wizard is simplified. It only asks for a few
> fields, and its final "Review the Application and Provider" screen shows just a summary.
> That is expected. The remaining fields (signing key, authorization/invalidation flow,
> scopes, issuer/subject mode) are set to sensible defaults and configured/verified
> afterward by editing the provider — see **B.4**.

In the wizard, set what it offers:

| Field | Value |
| --- | --- |
| Name | Leave the auto-generated name or set `pve-helper`. Cosmetic. |
| Client type | `Confidential` |
| Client ID / Secret | Auto-generated. Copy later from the provider edit view. |
| Authorization flow | If asked, `default-provider-authorization-implicit-consent`. |

**Redirect URIs / Origins:**

- Add one entry: Matching mode **Strict**, URL `https://pve-helper.example.com/auth/oidc/callback`.
- Do **not** add wildcards or extra URIs.

Finish the wizard. The summary screen showing only a subset of fields is normal.

### B.4 Verify and complete the provider (after the wizard)

Go to **Applications** -> **Providers** -> your provider -> **Edit**, then check / set:

**Flows:**

- **Authorization flow**: `default-provider-authorization-implicit-consent` (or
  explicit-consent if you want a consent prompt).
- **Invalidation flow**: `default-provider-invalidation-flow`. **Required** — handles
  logout/end-session. An empty invalidation flow makes logout error out.

**Signing & encryption:**

- **Signing Key**: a certificate must be selected (default self-signed). This makes the
  `id_token` RS256-signed and exposes a JWKS in discovery. **Do not clear this.**
- **Encryption Key**: **LEAVE BLANK**.

**Scopes:** ensure the default mappings for `openid`, `profile`, `email` are selected.
See Part E before adding any custom `groups` scope.

**Advanced protocol settings:** keep per-provider issuer mode, default subject mode, and
all other advanced fields at defaults.

The wizard does **not** create the access binding — do it in **Part D**.

---

## Part C — Authorization vs Invalidation flow (why both)

- **Authorization flow** runs at login/authorization. Without it, login fails.
- **Invalidation flow** runs on logout / end-session. Newer Authentik requires it.

Both should point at the standard built-in flows unless your instance has custom ones.

---

## Part D — Restrict access to the required group (the real gate)

An application **without** an access binding may be available to all Authentik users.
This binding is **not optional**.

1. Open **Applications** -> **Applications** -> `pve-helper`.
2. Open the **Bindings** tab.
3. Click **Bind existing policy/group/user** -> **Group**.
4. Select the required group, save.

Verify: the Bindings tab lists exactly one group binding. Do not add other policy
bindings unless you have a specific reason.

---

## Part E — Make sure the app receives a `groups` claim

> **No action is needed here in the normal case.** This part cannot be verified until the
> app can perform a login (Part H), because Authentik has no convenient "preview my token"
> view. In current Authentik versions the default `profile` scope already emits `groups`,
> and you already selected `openid profile email`. Confirm it at Part H; only if it is
> missing do **E.2**.

### E.1 What to confirm (during Part H)

When you run the login test, confirm the app received a `groups` claim containing the
required group. If it did, skip E.2.

### E.2 Fallback — only if the `groups` claim is missing at Part H

1. **Customization** -> **Property Mappings** -> **Create** -> **OAuth2/OpenID Provider — Scope Mapping**.
2. Fill in:
   - Name: `pve-helper-groups`
   - Scope name: `groups`
   - Expression:

     ```python
     return {
         "groups": [group.name for group in request.user.ak_groups.all()],
     }
     ```

   > Note the attribute is **`ak_groups`**, not `groups`. Using `request.user.groups`
   > returns nothing and the claim would always be empty.

3. Save, then add `pve-helper-groups` to the provider's selected scope mappings.
4. Ensure `OIDC_SCOPES=openid profile email groups`.

---

## Part F — App configuration

### F.1 Credentials

Open the provider (Applications -> Providers -> your provider) and copy the **Client ID**
and **Client Secret** into `.env`:

```env
OIDC_ISSUER_URL=https://auth.example.com/application/o/pve-helper/
OIDC_CLIENT_ID=<from-authentik>
OIDC_CLIENT_SECRET=<from-authentik>
OIDC_REDIRECT_URI=https://pve-helper.example.com/auth/oidc/callback
OIDC_REQUIRED_GROUP=pve-helper-admins
OIDC_SCOPES=openid profile email groups
```

The app derives the OP endpoints (authorize/token/userinfo/jwks) from `OIDC_ISSUER_URL`
automatically — mozilla-django-oidc does **not** perform `.well-known` discovery, so the
issuer must point at the right Authentik host.

### F.2 Internal CA trust (if the Authentik URL uses an internal CA)

The app makes back-channel HTTPS calls (token + JWKS) to Authentik. If the issuer host
presents an internal/private CA certificate, the container must trust it or these calls
fail with `SSLError` — which surfaces as a 500 *after* the OIDC redirect.

1. Build a PEM bundle of public roots + your internal CA and mount it read-only into
   `web` and `worker` (see `docker-compose.yml`).
2. Set `REQUESTS_CA_BUNDLE` to its container path, e.g.:

   ```env
   REQUESTS_CA_BUNDLE=/etc/ssl/pve-helper-ca-bundle.pem
   ```

`REQUESTS_CA_BUNDLE` covers the OIDC path (it uses `requests`). Leave it empty if your
Authentik URL uses a publicly-trusted certificate.

---

## Part G — Configure the reverse proxy

| Field | Value |
| --- | --- |
| Domain | `pve-helper.example.com` |
| Scheme | `http` |
| Forward host/IP | the pve-helper app target on the Docker host |
| Forward port | chosen app port (e.g. `21080`) |
| Websockets | Enable if the UI later adds live task updates |
| TLS certificate | Attach the appropriate certificate |

The proxy is the TLS/routing layer only — the app still enforces OIDC login and group
membership.

---

## Part H — Test

1. Confirm discovery is reachable:

   ```bash
   curl -s https://auth.example.com/application/o/pve-helper/.well-known/openid-configuration | jq .issuer
   ```

2. Visit the app, confirm redirect to Authentik, and that the **flow UI renders**
   (no 404 — if you get one, your issuer host is not serving `/if/flow/...`; see the
   "Choosing the issuer URL" section).
3. Log in as a member of the required group.
4. Confirm callback to `/auth/oidc/callback` and that the app logs you in.
5. Confirm the app received the username **and** the `groups` claim (Part E).
6. **Negative test:** with a user not in the group, confirm access is denied.

---

## App-side checks (the app must still enforce)

- Valid OIDC session.
- `iss` matches configured `OIDC_ISSUER_URL`.
- `aud` / client ID matches configured `OIDC_CLIENT_ID`.
- User belongs to configured `OIDC_REQUIRED_GROUP`.
- Failed authentication and failed group checks are written to the audit log.

---

## Fields to leave blank or at default (quick reference)

| Location | Field | Action |
| --- | --- | --- |
| Application | **Group** | **Leave blank** — UI grouping only, not authorization. |
| Application | Description / Publisher / Icon | Leave blank (optional cosmetics). |
| Provider | Client ID / Client Secret | Leave the auto-generated values; just copy them. |
| Provider | **Encryption Key** | **Leave blank** — do not encrypt the token. |
| Provider | Signing Key | **Keep the default certificate** — do **not** clear it. |
| Provider | Issuer mode | Keep per-provider default. |
| Provider | Subject mode | Keep default (hashed user ID). |
| Provider | Token validity / PKCE / advanced | Keep defaults. |
| Provider | Custom `groups` scope mapping | Add **only** if Part E shows the claim is missing. |
| Application | Extra policy bindings | Add **only** the one group binding from Part D. |

---

## References

- OAuth2/OIDC provider: https://docs.goauthentik.io/add-secure-apps/providers/oauth2/
- Create OAuth2 provider: https://docs.goauthentik.io/add-secure-apps/providers/oauth2/create-oauth2-provider/
- Application access bindings: https://docs.goauthentik.io/add-secure-apps/applications/manage_apps/
- Property mappings: https://docs.goauthentik.io/add-secure-apps/providers/property-mappings/
