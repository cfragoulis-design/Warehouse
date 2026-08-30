# Warehouse behind One Upgrade Shield

## Browser links

Warehouse browser navigation must use relative paths. In particular, movement
pagination is built from the known filter fields and never from
`request.url`, so the Railway origin cannot be embedded in the HTML response.
The Shield must continue rewriting same-upstream absolute `Location` headers
created by Starlette slash normalization.

No generic trusted-proxy middleware is required for this pagination fix. The
existing same-origin POST guard assumes the Shield's `rewrite_to_origin`
browser-origin policy; changing that policy requires a separate security review.

## HPRT download channel

The Label Center download is selected only by
`WAREHOUSE_HPRT_AGENT_RELEASE_CHANNEL`:

- Production: `production`
- Staging: `staging`
- Hide/disable the download: `disabled`

If the variable is absent or has any other value, the download is disabled.
Both Production and Staging therefore require an explicit channel. Request
hostnames, including the Railway and future Shield hostnames, never choose a
release package.

Set the Production value explicitly in Railway before releasing this change and
confirm the Label Center text before traffic cutover. This setting is not a
credential and no token belongs in it.

This change does not repackage the HPRT agent. The existing Production package
still calls `https://sklavounoswh.up.railway.app`, so closing direct Railway
origin access remains blocked until the installed/package agent endpoint is
deliberately migrated or exempted.
