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

## Shared manifest contract

`channels.json` uses schema `2` for generated production snapshots. The
`schema` field remains backward-compatible and consumers must continue to
accept schema `1` snapshots. Each channel keeps its existing `id`, `name`,
`group`, `logo`, `epgId`, and `streams` fields and may additionally provide an
integer `order`. When present, `order` is the authoritative absolute display
order; clients fall back to their established group and natural-number sort
when it is absent.

An empty sentinel manifest is intentionally allowed to remain at the legacy
schema while no upstream has documented public redistribution permission; it
is not a production catalog and must not replace a client's last-known-good
snapshot.

Every `streams[]` entry is a distinct direct media URL for that channel and
may carry `priority`, `health`, `resolution`, `bitrateKbps`, `fps`,
`stability`, and `adaptive` metadata. Clients should rank healthy sources by
backend priority, health/reliability, quality, bitrate, FPS, latency and
adaptive support, then fail over through every remaining URL of the same
channel before moving to another channel. The generated M3U must contain only
the same validated, live, unique, authorized production URLs.

If no upstream has documented public redistribution permission, the publisher
leaves the public data unchanged. The app then uses its cached or bundled
manifest instead.

The private HNDTV repository publishes through GitHub Actions using the minimum
cross-repository secret required by the workflow: `HNDTV_DATA_TOKEN`. The
secret must be configured in `hnduy910/HNDTV`; it is never committed here.
