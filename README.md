# HND TV public data

This repository contains the shared IPTV data contract for HND TV clients. It
intentionally contains no application source code, credentials, DRM material,
authenticated URLs, tokenized URLs, or geo-bypass configuration.

`hndtv.m3u` is an append-only source vault. It is cumulative: an URL already
present is retained even when a later health-check, timeout, HTTP failure,
offline stream, ffprobe failure, or temporary source outage occurs. Only a new
candidate that is in scope, explicitly authorized for public redistribution,
and passes the playback probe may be appended. A failed refresh therefore
leaves the existing file byte-for-byte unchanged.

The publisher writes these files through `backend/update_data.py`:

- `channels.json` — normalized runtime channels and ranked stream metadata
- `hndtv.m3u` — cumulative M3U source vault; old URLs are never pruned by CI
- `sources.json` — auditable upstream and permission metadata
- `refresh-status.json` — counts and per-source status from the last refresh

The updater scans every enabled upstream in `sources.json`. New Vietnamese
channels are eligible in all categories. New international candidates are
limited to movies/film/cinema and football/soccer streams. Restricted,
authenticated, tokenized, DRM, paywalled, or geo-circumvention sources are not
published. An empty `upstreams` list is a deliberate no-op configuration.

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
`stability`, `latencyMs`, and `adaptive` metadata. Clients should rank healthy
sources by backend priority, health/reliability, quality, bitrate, FPS,
latency and adaptive support, then fail over through every remaining URL of the
same channel before moving to another channel. A stream with health `0` remains
available as a last-known-good fallback; ranking never removes it from
`hndtv.m3u`.

The updater validates an atomic candidate before replacing the production M3U.
Its regression guard requires the new unique URL count to be at least the old
count and requires `oldUrlRemoved == 0`; otherwise the workflow fails without
writing or committing the candidate. Duplicate URLs are skipped without
deleting the existing entry. The workflow runs every 12 hours at minute 17,
supports manual dispatch, and uses a concurrency guard so two refreshes cannot
publish concurrently.

If there are no approved upstreams, `channels.json` remains an empty sentinel
and `hndtv.m3u` contains only `#EXTM3U`. This is a safe no-op: clients keep
using their local cached or bundled last-known-good manifest.

The private HNDTV repository publishes through GitHub Actions using the minimum
cross-repository secret required by the workflow: `HNDTV_DATA_TOKEN`. The
secret must be configured in `hnduy910/HNDTV`; it is never committed here.
