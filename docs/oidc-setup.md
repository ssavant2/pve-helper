# OIDC authentication

pve-helper can authenticate against an OpenID Connect provider. Authentik is one
tested option, but it is not required; the application and its external reverse
proxy do not delegate authorization to a particular vendor.

The provider must:

- support the OIDC authorization-code flow and RS256-signed tokens;
- emit a stable, non-empty `sub` claim;
- expose the operator's groups in a `groups` claim, unless the provider's own
  application assignment is the gate (see below);
- provide authorization, token, userinfo and JWKS endpoints;
- allow the exact callback URI
  `https://pve-helper.example.com/auth/oidc/callback` (substitute your hostname).

pve-helper creates or updates the local user identified by `(issuer, sub)` and
requires membership in `OIDC_REQUIRED_GROUP`. The username and email are display
attributes, not durable identity keys.

## The two gates

Access passes through two independent gates, and only the second is pve-helper's:

1. **The provider decides who may complete the login flow at all** — an
   application binding, assignment or policy in Authentik, Entra, Okta or
   Authelia. pve-helper cannot see this gate and cannot verify that it exists.
2. **pve-helper matches `OIDC_REQUIRED_GROUP` against the `groups` claim.**

What providers put in that claim differs. Authentik, Authelia and Keycloak emit
group names — including the names of groups synchronised from an on-premises
directory, so an AD group name is the usual value there. Entra emits group object
GUIDs, so the configured value is a GUID rather than a name. Some deployments emit
no usable group claim at all because gate 1 is the whole authorization decision.

That last case is configured explicitly:

```env
OIDC_REQUIRED_GROUP=any-authenticated-user
```

Every account the provider authenticates is then admitted, and each such login is
logged as running without the group check.

**Leaving `OIDC_REQUIRED_GROUP` empty is refused at startup** (`pve_helper.E011`)
when `APP_REQUIRE_LOGIN=true`. An empty value cannot be told apart from a
configuration line that was dropped during an upgrade, and it would silently
disable gate 2 — while login grants full administrative access. Opting out must be
something someone wrote down, not something that happened.

## Configuration

Set the browser-facing application URL, provider identity and every provider
endpoint in `.env`:

```env
APP_REQUIRE_LOGIN=true
APP_BASE_URL=https://pve-helper.example.com
OIDC_ISSUER_URL=https://auth.example.com/realms/infrastructure
OIDC_CLIENT_ID=pve-helper
OIDC_CLIENT_SECRET=<provider-generated-secret>
OIDC_REQUIRED_GROUP=pve-helper-admins
OIDC_SCOPES=openid profile email groups
OIDC_OP_AUTHORIZATION_ENDPOINT=https://auth.example.com/oauth2/authorize
OIDC_OP_TOKEN_ENDPOINT=https://auth.example.com/oauth2/token
OIDC_OP_USER_ENDPOINT=https://auth.example.com/oauth2/userinfo
OIDC_OP_JWKS_ENDPOINT=https://auth.example.com/.well-known/jwks.json
OIDC_OP_END_SESSION_ENDPOINT=https://auth.example.com/oauth2/logout
```

The application does not perform OIDC discovery. Copy the endpoint values from
the provider's discovery document or administration UI. The endpoint defaults in
the application follow Authentik's URL layout only to support the accompanying
Authentik recipe; do not rely on them with another provider.

Set `OIDC_OP_END_SESSION_ENDPOINT=local` if the provider does not support
RP-initiated logout; pve-helper will still end its local session.

If the provider uses an internal CA, mount a CA bundle into the web container and
set `REQUESTS_CA_BUNDLE` to its container path. The browser must also trust that CA.

## Reverse proxy

pve-helper serves HTTP itself. A separate reverse proxy may terminate TLS; no
specific proxy product is required. It must preserve `Host`,
`X-Forwarded-For`, and `X-Forwarded-Proto`, pass the application's security
headers unchanged, and support WebSocket upgrades when the integrated console is
enabled. Trust only the proxy's actual IP or subnet in `NGINX_TRUSTED_PROXY`.

See `docs/deployment-runbook.md` for the complete proxy and large-transfer
settings. For an Authentik-specific walkthrough, see
`docs/authentik-oidc-setup.md`.

## Verification

1. Start pve-helper and visit its external URL.
2. Confirm the browser returns through the exact callback URI.
3. Confirm a member of `OIDC_REQUIRED_GROUP` can sign in.
4. Confirm a user outside that group is denied.
5. Sign out and verify both the local session and, when configured, the provider
   session end.
