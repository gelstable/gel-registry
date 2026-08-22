# Hosting observations

Log of measured response headers from static deployments. Cache policies defined in `vercel.json` are requested directives, not guarantees of stale serving during upstream outages.

## Requested cache policy

| Path | Requested `Cache-Control` | Content role |
| --- | --- | --- |
| `/s/*` | `public, max-age=31536000, s-maxage=31536000, immutable` | Pinned snapshot manifests |
| `/i/*` | `public, max-age=31536000, s-maxage=31536000, immutable` | Content-addressed index blobs |
| `/registry.json` | `public, max-age=0, s-maxage=60, stale-while-revalidate=600, stale-if-error=86400` | Moving root pointer |
| `/v1/*` | `public, max-age=0, s-maxage=60, stale-while-revalidate=600, stale-if-error=86400` | Moving snapshot listing & schemas |

Note: `stale-if-error` is requested policy; edge caches or failover hosts may decline to serve stale content. Follow failover runbooks in `docs/operations.md` during outages.

## Verification probes

Run from an external network (not bypassing the production CDN). Probe moving and pinned paths separately:

```text
curl -I https://registry.gelstable.com/registry.json
curl -I https://registry.gelstable.com/v1/snapshots.json
curl -I https://registry.gelstable.com/s/<SNAPSHOT_ID>/registry.json
curl -I https://registry.gelstable.com/i/<BLOB_ID>.json
```

Log headers verbatim. If `Age`, `x-vercel-cache`, or `x-vercel-id` are missing, record `absent`.

## Observation log template

Record one row per probe with RFC3339 UTC timestamps and deployment/commit IDs:

| observed_at_utc | URL | status | Cache-Control | Age | x-vercel-cache | x-vercel-id | notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `<YYYY-MM-DDTHH:MM:SSZ>` | `https://registry.gelstable.com/registry.json` | `<status>` | `<value/absent>` | `<value/absent>` | `<value/absent>` | `<value/absent>` | `<commit/deployment>` |
| `<YYYY-MM-DDTHH:MM:SSZ>` | `https://registry.gelstable.com/v1/snapshots.json` | `<status>` | `<value/absent>` | `<value/absent>` | `<value/absent>` | `<value/absent>` | `<commit/deployment>` |
| `<YYYY-MM-DDTHH:MM:SSZ>` | `https://registry.gelstable.com/s/<SNAPSHOT_ID>/registry.json` | `<status>` | `<value/absent>` | `<value/absent>` | `<value/absent>` | `<value/absent>` | `<commit/deployment>` |
| `<YYYY-MM-DDTHH:MM:SSZ>` | `https://registry.gelstable.com/i/<BLOB_ID>.json` | `<status>` | `<value/absent>` | `<value/absent>` | `<value/absent>` | `<value/absent>` | `<commit/deployment>` |

Preview environment observations must explicitly state preview hostnames in `notes` and do not count toward production SLA validation.
