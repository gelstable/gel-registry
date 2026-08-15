# Hosting observations

This file records measured response headers from the static deployment. The
cache settings in `vercel.json` are the requested policy; they are not a
measurement and do not promise that a provider will retain or serve stale
content during an outage.

## Requested policy

| Path | Requested `Cache-Control` | Content role |
| --- | --- | --- |
| `/s/*` | `public, max-age=31536000, s-maxage=31536000, immutable` | Pinned snapshot and index bytes |
| `/registry.json` | `public, max-age=0, s-maxage=60, stale-while-revalidate=600, stale-if-error=86400` | Moving selected root |
| `/v1/*` | `public, max-age=0, s-maxage=60, stale-while-revalidate=600, stale-if-error=86400` | Moving listing and public schemas |

`stale-if-error` is requested policy, not an availability guarantee. Vercel,
an intermediary cache, or a failover provider may decline to serve stale data;
an outage must still follow the provider failover runbook in
`docs/operations.md`.

## Reproducible probes

Run the following from a network that is not bypassing the production CDN. Run
the moving and pinned probes separately so that a cached moving response cannot
be mistaken for an immutable response:

```text
curl -I https://registry.gelstable.org/registry.json
curl -I https://registry.gelstable.org/v1/snapshots.json
curl -I https://registry.gelstable.org/s/<SNAPSHOT_ID>/registry.json
curl -I https://registry.gelstable.org/s/<SNAPSHOT_ID>/index/<INDEX>.json
```

Record the response without changing the case of header names. Header names are
case-insensitive, but retaining the provider spelling makes later comparisons
with the deployment dashboard easier. If a response has no `Age`,
`x-vercel-cache`, or `x-vercel-id` header, record `absent` rather than inferring
the cache state.

## Dated result template

Copy one row per URL and observation time. Use an RFC3339 UTC timestamp and
include the deployment commit or preview identifier in `notes`.

| observed_at_utc | URL | status | Cache-Control | Age | x-vercel-cache | x-vercel-id | notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `<YYYY-MM-DDTHH:MM:SSZ>` | `https://registry.gelstable.org/registry.json` | `<HTTP status>` | `<value or absent>` | `<value or absent>` | `<value or absent>` | `<value or absent>` | `<commit/deployment>` |
| `<YYYY-MM-DDTHH:MM:SSZ>` | `https://registry.gelstable.org/v1/snapshots.json` | `<HTTP status>` | `<value or absent>` | `<value or absent>` | `<value or absent>` | `<value or absent>` | `<commit/deployment>` |
| `<YYYY-MM-DDTHH:MM:SSZ>` | `https://registry.gelstable.org/s/<SNAPSHOT_ID>/registry.json` | `<HTTP status>` | `<value or absent>` | `<value or absent>` | `<value or absent>` | `<value or absent>` | `<commit/deployment>` |
| `<YYYY-MM-DDTHH:MM:SSZ>` | `https://registry.gelstable.org/s/<SNAPSHOT_ID>/index/<INDEX>.json` | `<HTTP status>` | `<value or absent>` | `<value or absent>` | `<value or absent>` | `<value or absent>` | `<commit/deployment>` |

No production observation is claimed until the first nonempty bootstrap
selection has been reviewed and `registry.gelstable.org` has been attached.
Preview observations should identify their preview hostname and must not be
presented as production availability evidence.
