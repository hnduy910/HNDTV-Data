# HND TV public data

This repository contains generated IPTV data for the private `hnduy910/HNDTV`
Android TV client. It intentionally contains no application source code,
credentials, DRM material, authenticated URLs, tokenized URLs, or geo-bypass
configuration.

The publisher writes these files only after parsing, normalizing, de-duplicating,
health-checking, and validating an upstream that is explicitly approved for
public redistribution:

- `channels.json` — normalized channels and live stream metadata
- `hndtv.m3u` — generated M3U export of the same validated streams
- `sources.json` — auditable upstream and permission metadata

If no upstream has documented public redistribution permission, the publisher
leaves the public data unchanged. The app then uses its cached or bundled
manifest instead.

The private HNDTV repository publishes through GitHub Actions using the minimum
cross-repository secret required by the workflow: `HNDTV_DATA_TOKEN`. The
secret must be configured in `hnduy910/HNDTV`; it is never committed here.
