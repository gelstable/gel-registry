# Hosting observations

Log of measured response headers from static deployments. Cache policies defined in `vercel.toml` are requested directives, not guarantees of stale serving during upstream outages.

## Requested cache policy

| Path | Requested `Cache-Control` | Content role |
| --- | --- | --- |
| `/s/*` | `public, max-age=31536000, s-maxage=31536000, immutable` | Pinned snapshot manifests |
| `/i/*` | `public, max-age=31536000, s-maxage=31536000, immutable` | Content-addressed index blobs |
| `/registry.json` | `public, max-age=0, s-maxage=60, stale-while-revalidate=600, stale-if-error=86400` | Moving root pointer |
| `/v1/*` | `public, max-age=0, s-maxage=60, stale-while-revalidate=600, stale-if-error=86400` | Moving snapshot listing & schemas |
| `/apt/dists/*` | `public, max-age=0, must-revalidate` | APT `InRelease`, `Release`, `Release.gpg`, `Packages`, `Packages.gz` (replaced in place; no by-hash) |
| `/rpm/<channel>/<arch>/repodata/repomd.xml{,.asc}` | `public, max-age=0, must-revalidate` | Signed RPM repository pointer |
| `/rpm/<channel>/<arch>/repodata/<sha256>-*` | `public, max-age=31536000, s-maxage=31536000, immutable` | Hash-named RPM repodata |
| `/keys/*` | `public, max-age=0, s-maxage=60` | Public signing certificate and fingerprint |
| `/apt/pool/*`, `/rpm/pool/*` | `public, max-age=300` | Redirects to GitHub release assets |

Repository metadata that a deployment replaces in place is requested uncached
(`public, max-age=0, must-revalidate` is Vercel's static default, documented as
uncached at both the CDN and the browser). An edge copy that outlived a deploy
would pair a new signature with an old index, or a new `repomd.xml` with
repodata that no longer exists, and clients fail with `Hash Sum mismatch` or
404. RPM data files are named by their SHA-256, so they are never replaced and
are cached like `/i/*`. The header sources do not overlap: Vercel applies every
matching rule and a later value overwrites an earlier one.

Note: `stale-if-error` is requested policy; edge caches or failover hosts may decline to serve stale content. Follow failover runbooks in `docs/operations.md` during outages.

## Verification probes

Run from an external network (not bypassing the production CDN). Probe moving and pinned paths separately:

```text
curl -I https://registry.gelstable.com/registry.json
curl -I https://registry.gelstable.com/v1/snapshots.json
curl -I https://registry.gelstable.com/s/<SNAPSHOT_ID>/registry.json
curl -I https://registry.gelstable.com/i/<BLOB_ID>.json
curl -I https://registry.gelstable.com/apt/dists/stable/InRelease
curl -I https://registry.gelstable.com/rpm/stable/x86_64/repodata/repomd.xml
curl -I https://registry.gelstable.com/rpm/stable/x86_64/repodata/<SHA256>-primary.xml.gz
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
