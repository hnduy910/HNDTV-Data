#!/usr/bin/env python3
"""Append-only HND TV community discovery and shared-manifest publisher.

The production M3U is an accumulated source vault.  Existing entries are
never removed by this updater, even when a later probe fails.  Only a new,
policy-eligible candidate that passes the playback probe may be appended.

This module deliberately keeps discovery, policy classification, probing,
normalisation and publication separate so the tests can exercise the
append-only contract without network access.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
from typing import Callable, Iterable
from urllib.parse import parse_qsl, urlsplit, urlunsplit
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
M3U_PATH = ROOT / "hndtv.m3u"
MANIFEST_PATH = ROOT / "channels.json"
SOURCES_PATH = ROOT / "sources.json"
STATUS_PATH = ROOT / "refresh-status.json"

ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"|([\w-]+)=([^\s,]+)')
NATURAL_RE = re.compile(r"\d+|\D+")
RESTRICTED_QUERY_KEYS = {
    "access_token", "auth", "authorization", "cookie", "device_id", "deviceid",
    "drm", "exp", "expires", "hdnea", "hdnts", "jwt", "key", "license",
    "referer", "session", "session_id", "sessionid", "sig", "signature", "token",
    "user_id", "userid", "widevine",
}
RESTRICTED_MARKERS = ("clearkey", "drm", "widevine", "license")
MOVIE_WORDS = {"movie", "movies", "cinema", "film", "films", "phim"}
FOOTBALL_WORDS = {"football", "soccer", "futbol", "bong", "da", "bongda"}
VIETNAM_BRANDS = {
    "antv", "htv", "qpvn", "sctv", "thvl", "vtc", "vtv", "vietnam", "viet nam",
    "vinhlong", "cantho", "danang", "dongnai", "hanoi", "haiphong", "hue",
    "laocai", "quangninh", "thainguyen", "tayninh",
}


@dataclass(frozen=True)
class PlaylistEntry:
    info_line: str
    option_lines: tuple[str, ...]
    url: str
    name: str
    attrs: dict[str, str]
    source_id: str = "existing"
    priority: int = 0
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def group(self) -> str:
        return str(self.attrs.get("group-title") or "Khác").strip() or "Khác"

    @property
    def logo(self) -> str | None:
        value = str(self.attrs.get("tvg-logo") or "").strip()
        return value or None

    @property
    def tvg_id(self) -> str | None:
        value = str(self.attrs.get("tvg-id") or "").strip()
        return value or None

    @property
    def epg_id(self) -> str | None:
        value = str(self.attrs.get("epg-id") or "").strip()
        return value or None

    def render(self) -> str:
        lines = [self.info_line, *self.option_lines, self.url]
        return "\n".join(lines)


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def parse_attributes(line: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for match in ATTR_RE.finditer(line):
        key = match.group(1) or match.group(3)
        value = match.group(2) if match.group(2) is not None else match.group(4)
        result[key] = value
    return result


def info_name(info_line: str, attrs: dict[str, str]) -> str:
    value = info_line.rsplit(",", 1)[-1].strip() if "," in info_line else ""
    return value or str(attrs.get("tvg-name") or "Kênh").strip() or "Kênh"


def parse_m3u(text: str, source_id: str = "existing", priority: int = 0) -> list[PlaylistEntry]:
    """Parse entries while retaining option lines and the original info line."""
    entries: list[PlaylistEntry] = []
    pending_info: str | None = None
    pending_options: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF:"):
            pending_info = line
            pending_options = []
            continue
        if pending_info is None:
            continue
        if line.startswith("#"):
            pending_options.append(line)
            continue
        if not line:
            continue
        attrs = parse_attributes(pending_info)
        entries.append(
            PlaylistEntry(
                info_line=pending_info,
                option_lines=tuple(pending_options),
                url=line,
                name=info_name(pending_info, attrs),
                attrs=attrs,
                source_id=source_id,
                priority=priority,
            )
        )
        pending_info = None
        pending_options = []
    return entries


def normalise_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", str(value))
    without_marks = "".join(char for char in decomposed if unicodedata.category(char) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", without_marks.lower()).strip()


def natural_key(value: str) -> tuple[tuple[int, object], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in NATURAL_RE.findall(normalise_text(value))
    )


def normalise_url(value: str) -> str:
    """Return a comparison key without changing the URL kept in the M3U."""
    raw = str(value).strip()
    try:
        parsed = urlsplit(raw)
        if not parsed.scheme or not parsed.netloc:
            return raw.casefold().split("#", 1)[0]
        host = (parsed.hostname or "").casefold().rstrip(".")
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme.casefold(), host, parsed.path.rstrip("/"), parsed.query, ""))
    except Exception:
        return raw.casefold().split("#", 1)[0]


def is_restricted_url(url: str, name: str = "") -> bool:
    try:
        parsed = urlsplit(url)
    except Exception:
        return True
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return True
    if parsed.username or parsed.password:
        return True
    if "geo-blocked" in normalise_text(name) or "geoblocked" in normalise_text(name):
        return True
    if any(marker in f"{parsed.hostname}{parsed.path}".casefold() for marker in RESTRICTED_MARKERS):
        return True
    query_keys = {key.casefold() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    return bool(query_keys.intersection(RESTRICTED_QUERY_KEYS))


def channel_scope(entry: PlaylistEntry) -> str | None:
    """Return a permitted scope for a *new* candidate, or None."""
    id_text = normalise_text(entry.tvg_id or entry.epg_id or "")
    name_text = normalise_text(entry.name)
    group_text = normalise_text(entry.group)
    country_text = normalise_text(entry.attrs.get("tvg-country", ""))
    combined = " ".join((id_text, name_text, group_text, country_text))
    compact = combined.replace(" ", "")
    if country_text in {"vn", "vnm", "vietnam"} or compact.endswith("vn"):
        return "vietnam"
    if any(brand in compact for brand in VIETNAM_BRANDS):
        return "vietnam"
    tokens = set(combined.split())
    if tokens.intersection(MOVIE_WORDS):
        return "international-movies"
    if tokens.intersection(FOOTBALL_WORDS) or "football" in compact or "soccer" in compact:
        return "international-football"
    return None


def source_is_authorized(source: dict) -> bool:
    """Public output requires an explicit permission signal."""
    return bool(source.get("public", False)) and bool(
        source.get("productionEligible", source.get("production_eligible", False))
    ) and bool(str(source.get("permission", "")).strip())


def source_location(source: dict) -> str:
    return str(source.get("path") or source.get("url") or "").strip()


def source_id(source: dict) -> str:
    explicit = str(source.get("id", "")).strip()
    if explicit:
        return explicit
    return hashlib.sha1(source_location(source).encode("utf-8")).hexdigest()[:12]


def fetch_source(source: dict, root: Path) -> str:
    path_value = str(source.get("path", "")).strip()
    if path_value:
        path = (root / path_value).resolve()
        path.relative_to(root.resolve())
        return path.read_text("utf-8")
    url = str(source.get("url", "")).strip()
    if not url.startswith("https://"):
        raise ValueError("community catalog must use HTTPS")
    request = Request(
        url,
        headers={
            "Accept": "audio/x-mpegurl, application/vnd.apple.mpegurl, text/plain, */*",
            "User-Agent": "HNDTV-Data-AppendOnly/1.0",
        },
    )
    with urlopen(request, timeout=12) as response:
        payload = response.read(12 * 1024 * 1024 + 1)
    if len(payload) > 12 * 1024 * 1024:
        raise ValueError("playlist exceeds size limit")
    return payload.decode("utf-8", "replace")


def default_probe(entry: PlaylistEntry, _source: dict) -> dict:
    """Probe a new direct media URL with ffprobe; fail closed if unavailable."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {"live": False, "reason": "ffprobe unavailable"}
    command = [
        ffprobe,
        "-v", "error",
        "-rw_timeout", "10000000",
        "-analyzeduration", "5000000",
        "-probesize", "5000000",
        "-show_entries",
        "format=bit_rate:stream=codec_type,width,height,r_frame_rate,bit_rate",
        "-of", "json",
        entry.url,
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=13)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"live": False, "reason": str(error)[:180]}
    if completed.returncode != 0:
        return {"live": False, "reason": (completed.stderr or "ffprobe failed")[:180]}
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return {"live": False, "reason": "invalid ffprobe JSON"}
    streams = payload.get("streams", [])
    if not any(item.get("codec_type") in {"audio", "video"} for item in streams):
        return {"live": False, "reason": "no audio/video track"}

    def integer(value: object) -> int:
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            return 0

    video = max(
        (item for item in streams if item.get("codec_type") == "video"),
        key=lambda item: integer(item.get("height")),
        default={},
    )
    bitrate = max(
        [integer(item.get("bit_rate")) for item in streams]
        + [integer((payload.get("format") or {}).get("bit_rate"))],
        default=0,
    ) // 1000
    fps = 0.0
    rate = str(video.get("r_frame_rate", "0/1"))
    if "/" in rate:
        numerator, denominator = rate.split("/", 1)
        try:
            fps = round(float(numerator) / float(denominator), 2) if float(denominator) else 0.0
        except (ValueError, ZeroDivisionError):
            fps = 0.0
    return {
        "live": True,
        "health": 100,
        "resolution": integer(video.get("height")),
        "bitrateKbps": bitrate,
        "fps": fps,
        "adaptive": entry.url.casefold().endswith((".m3u8", ".mpd")),
        "latencyMs": 0,
    }


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: Path, value: dict) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def validate_m3u(text: str) -> None:
    lines = text.splitlines()
    if not lines or not lines[0].strip().casefold().startswith("#extm3u"):
        raise ValueError("M3U must start with #EXTM3U")
    entries = parse_m3u(text)
    for entry in entries:
        if not entry.url.startswith(("https://", "http://")):
            raise ValueError(f"invalid stream URL: {entry.url}")


IDENTITY_DECORATION_TOKENS = {
    "hd", "hdtv", "fhd", "fullhd", "uhd", "sd", "4k", "8k",
    "360p", "480p", "576p", "720p", "1080p", "1440p", "2160p", "4320p",
    "360", "480", "576", "720", "1080", "1440", "2160", "4320",
    "tv", "vn", "vietnam",
}


def trim_identity_suffix(tokens: list[str]) -> list[str]:
    result = list(tokens)
    while result:
        if len(result) >= 2 and result[-2:] == ["geo", "blocked"]:
            result = result[:-2]
            continue
        if len(result) >= 3 and result[-3:] == ["not", "24", "7"]:
            result = result[:-3]
            continue
        if result[-1] in IDENTITY_DECORATION_TOKENS:
            result.pop()
            continue
        if len(result) >= 2 and result[-2:] == ["viet", "nam"]:
            result = result[:-2]
            continue
        break
    return result


def split_number_and_variant(value: str) -> tuple[str, str | None] | None:
    if not value:
        return None
    match = re.match(r"^(\d+)(.*)$", value)
    if not match:
        return None
    number = str(int(match.group(1)))
    variant = match.group(2) or None
    return number, variant


def broadcast_identity_key(tokens: list[str]) -> str | None:
    values = [token for token in tokens if token != "tv"]
    brands = ("vtv", "htv", "sctv")
    for index, token in enumerate(values):
        trailing = values[index + 1:]
        for brand in brands:
            if token == brand and trailing:
                parsed = split_number_and_variant(trailing[0])
                if parsed:
                    number, variant = parsed
                    return make_broadcast_key(brand, number, ([variant] if variant else []) + trailing[1:])
            if token.startswith(brand):
                parsed = split_number_and_variant(token[len(brand):])
                if parsed:
                    number, variant = parsed
                    return make_broadcast_key(brand, number, ([variant] if variant else []) + trailing)
        if token == "thvl" and trailing:
            parsed = split_number_and_variant(trailing[0])
            if parsed:
                number, variant = parsed
                return make_broadcast_key("vinhlong", number, ([variant] if variant else []) + trailing[1:])
        if token.startswith("thvl"):
            parsed = split_number_and_variant(token[4:])
            if parsed:
                number, variant = parsed
                return make_broadcast_key("vinhlong", number, ([variant] if variant else []) + trailing)
        if token.startswith("vinhlong"):
            parsed = split_number_and_variant(token[len("vinhlong"):])
            if parsed:
                number, variant = parsed
                return make_broadcast_key("vinhlong", number, ([variant] if variant else []) + trailing)
        if token == "vinh" and len(values) > index + 2 and values[index + 1] == "long":
            parsed = split_number_and_variant(values[index + 2])
            if parsed:
                number, variant = parsed
                return make_broadcast_key(
                    "vinhlong", number,
                    ([variant] if variant else []) + values[index + 3:],
                )
    return None


def make_broadcast_key(brand: str, number: str, suffix: list[str]) -> str:
    meaningful = [item for item in suffix if item and item not in IDENTITY_DECORATION_TOKENS]
    if not meaningful:
        return f"{brand}{number}"
    compact = "".join(meaningful)
    if compact == "taynambo":
        suffix_key = "tay-nam-bo"
    elif compact == "taynguyen":
        suffix_key = "tay-nguyen"
    else:
        suffix_key = "-".join(meaningful)
    return f"{brand}{number}-{suffix_key}" if suffix_key != "channel" else f"{brand}{number}"


def identity_evidence(values: Iterable[object]) -> tuple[set[str], set[str]]:
    """Return (strong broadcast identities, generic normalized identities)."""
    strong: set[str] = set()
    generic: set[str] = set()
    for raw in values:
        normalized = normalise_text(str(raw or ""))
        if not normalized:
            continue
        tokens = trim_identity_suffix(normalized.split())
        if not tokens:
            continue
        broadcast = broadcast_identity_key(tokens)
        if broadcast:
            strong.add(broadcast)
        meaningful = [item for item in tokens if item not in IDENTITY_DECORATION_TOKENS]
        fallback = "-".join(meaningful)
        if fallback and not fallback.isdigit():
            generic.add(fallback)
    return strong, generic


def entry_evidence(entry: PlaylistEntry) -> tuple[set[str], set[str]]:
    return identity_evidence((entry.tvg_id, entry.epg_id, entry.name))


def channel_evidence(channel: dict) -> tuple[set[str], set[str]]:
    return identity_evidence((channel.get("id"), channel.get("epgId"), channel.get("name")))


def evidence_matches(first: tuple[set[str], set[str]], second: tuple[set[str], set[str]]) -> bool:
    first_strong, first_generic = first
    second_strong, second_generic = second
    # Distinct numbered broadcast identities must never be collapsed merely
    # because a provider reused a generic name or source-local id.
    if first_strong and second_strong and not first_strong.intersection(second_strong):
        return False
    return bool(first_strong.intersection(second_strong) or first_generic.intersection(second_generic))


def channel_id(entry: PlaylistEntry) -> str:
    for value in (entry.tvg_id, entry.epg_id, entry.name):
        strong, generic = identity_evidence((value,))
        if strong:
            return sorted(strong)[0]
        if generic:
            return sorted(generic)[0]
    return "channel-" + hashlib.sha1(entry.url.encode("utf-8")).hexdigest()[:12]


def brand_priority(value: object) -> int | None:
    normalized = normalise_text(str(value or ""))
    tokens = normalized.split()
    if any(token == "vtv" or (token.startswith("vtv") and token[3:4].isdigit()) for token in tokens):
        return 0
    if "vinh long" in normalized or any(token.startswith("thvl") for token in tokens):
        return 1
    if normalized == "vtvcab":
        return 2
    if any(token == "on" or (token.startswith("on") and token[2:].isdigit()) for token in tokens):
        return 2
    if any(token == "htv" or token.startswith("htv") for token in tokens):
        return 3
    if any(token == "sctv" or token.startswith("sctv") for token in tokens):
        return 4
    return None


def group_priority(channel: dict) -> int:
    for value in (channel.get("name"), channel.get("id"), channel.get("group")):
        priority = brand_priority(value)
        if priority is not None:
            return priority
    return 5


def stream_from_entry(entry: PlaylistEntry) -> dict:
    metadata = entry.metadata
    return {
        "url": entry.url,
        "label": metadata.get("label") or "Auto",
        "sourceId": entry.source_id,
        "resolution": int(metadata.get("resolution", 0) or 0),
        "bitrateKbps": int(metadata.get("bitrateKbps", 0) or 0),
        "fps": float(metadata.get("fps", 0) or 0),
        "priority": entry.priority,
        "health": int(metadata.get("health", 100 if metadata.get("live") else 0) or 0),
        "stability": int(metadata.get("stability", 0) or 0),
        "adaptive": bool(metadata.get("adaptive", entry.url.casefold().endswith((".m3u8", ".mpd")))),
        "latencyMs": int(metadata.get("latencyMs", 0) or 0),
        "isLive": metadata.get("live") if "live" in metadata else None,
    }


def merge_stream(existing: dict, incoming: dict) -> dict:
    merged = dict(existing)
    for key, value in incoming.items():
        if key == "url":
            continue
        if value not in (None, "", 0, 0.0, False) or merged.get(key) in (None, "", 0, 0.0, False):
            merged[key] = value
    return merged


def build_manifest(entries: Iterable[PlaylistEntry], previous: dict | None) -> dict:
    channels: list[dict] = []

    def add_entry(entry: PlaylistEntry) -> None:
        nonlocal channels
        match_index = next(
            (index for index, channel in enumerate(channels)
             if evidence_matches(channel_evidence(channel), entry_evidence(entry))),
            None,
        )
        if match_index is None:
            key = channel_id(entry)
            used_ids = {str(item.get("id", "")) for item in channels}
            if key in used_ids:
                suffix = 2
                candidate = f"{key}-{suffix}"
                while candidate in used_ids:
                    suffix += 1
                    candidate = f"{key}-{suffix}"
                key = candidate
            channel = {
                "id": key,
                "name": entry.name,
                "group": entry.group,
                "logo": entry.logo,
                "epgId": entry.epg_id or entry.tvg_id,
                "streams": [],
            }
            channels.append(channel)
            match_index = len(channels) - 1
        channel = channels[match_index]
        if not channel.get("logo") and entry.logo:
            channel["logo"] = entry.logo
        if not channel.get("epgId") and (entry.epg_id or entry.tvg_id):
            channel["epgId"] = entry.epg_id or entry.tvg_id
        incoming_stream = stream_from_entry(entry)
        stream_key = normalise_url(entry.url)
        for index, stream in enumerate(channel["streams"]):
            if normalise_url(str(stream.get("url", ""))) == stream_key:
                channel["streams"][index] = merge_stream(stream, incoming_stream)
                break
        else:
            channel["streams"].append(incoming_stream)

    for entry in entries:
        add_entry(entry)

    # The manifest is also a last-known-good view. Preserve metadata/URLs from
    # a previous manifest even if the M3U was externally shortened.
    for previous_channel in (previous or {}).get("channels", []):
        if not isinstance(previous_channel, dict):
            continue
        match_index = next(
            (index for index, channel in enumerate(channels)
             if evidence_matches(channel_evidence(channel), channel_evidence(previous_channel))),
            None,
        )
        if match_index is None:
            previous_copy = {
                "id": str(previous_channel.get("id") or "channel"),
                "name": previous_channel.get("name", "Kênh"),
                "group": previous_channel.get("group", "Khác"),
                "logo": previous_channel.get("logo"),
                "epgId": previous_channel.get("epgId"),
                "streams": [],
            }
            channels.append(previous_copy)
            match_index = len(channels) - 1
        channel = channels[match_index]
        if not channel.get("logo") and previous_channel.get("logo"):
            channel["logo"] = previous_channel["logo"]
        existing_urls = {normalise_url(str(item.get("url", ""))) for item in channel["streams"]}
        for stream in previous_channel.get("streams", []):
            if not isinstance(stream, dict) or not stream.get("url"):
                continue
            key = normalise_url(str(stream["url"]))
            if key in existing_urls:
                stream_index = next(
                    index for index, item in enumerate(channel["streams"])
                    if normalise_url(str(item.get("url", ""))) == key
                )
                channel["streams"][stream_index] = merge_stream(channel["streams"][stream_index], stream)
            else:
                channel["streams"].append(dict(stream))
                existing_urls.add(key)

    ordered = sorted(
        channels,
        key=lambda item: (group_priority(item), natural_key(str(item.get("name", ""))), str(item.get("id", ""))),
    )
    for index, channel in enumerate(ordered, start=1):
        channel["order"] = index
        channel["streams"].sort(
            key=lambda stream: (
                int(bool(stream.get("isLive"))),
                int(stream.get("priority", 0) or 0),
                int(stream.get("health", 0) or 0),
                int(stream.get("resolution", 0) or 0),
                int(stream.get("bitrateKbps", 0) or 0),
                float(stream.get("fps", 0) or 0),
                int(bool(stream.get("adaptive"))),
                -int(stream.get("latencyMs", 0) or 0),
            ),
            reverse=True,
        )
    return {"schema": 2, "generatedAt": utc_now(), "channels": ordered}


class AppendOnlyPipeline:
    def __init__(
        self,
        root: Path = ROOT,
        fetcher: Callable[[dict], str] | None = None,
        prober: Callable[[PlaylistEntry, dict], dict] | None = None,
    ) -> None:
        self.root = Path(root)
        self.m3u_path = self.root / "hndtv.m3u"
        self.manifest_path = self.root / "channels.json"
        self.sources_path = self.root / "sources.json"
        self.status_path = self.root / "refresh-status.json"
        self.fetcher = fetcher or (lambda source: fetch_source(source, self.root))
        self.prober = prober or default_probe

    def _read_sources(self) -> list[dict]:
        if not self.sources_path.is_file():
            return []
        payload = json.loads(self.sources_path.read_text("utf-8"))
        return [
            item for item in payload.get("upstreams", [])
            if isinstance(item, dict) and item.get("enabled", True)
        ]

    def run(self, *, write: bool = True) -> dict:
        old_text = self.m3u_path.read_text("utf-8") if self.m3u_path.is_file() else "#EXTM3U\n"
        if not old_text.strip():
            old_text = "#EXTM3U\n"
        validate_m3u(old_text)
        old_entries = parse_m3u(old_text)
        old_keys = {normalise_url(entry.url) for entry in old_entries}
        previous_manifest = None
        if self.manifest_path.is_file():
            try:
                previous_manifest = json.loads(self.manifest_path.read_text("utf-8"))
            except json.JSONDecodeError:
                previous_manifest = None

        sources = self._read_sources()
        all_candidates: list[tuple[PlaylistEntry, dict]] = []
        source_statuses: list[dict] = []
        for source in sources:
            sid = source_id(source)
            status = {
                "sourceId": sid,
                "name": source.get("name") or sid,
                "location": source_location(source),
                "state": "not_checked",
                "entryCount": 0,
                "error": None,
            }
            try:
                text = self.fetcher(source)
                parsed = parse_m3u(text, source_id=sid, priority=int(source.get("priority", 0) or 0))
                status["state"] = "fetched"
                status["entryCount"] = len(parsed)
                all_candidates.extend((entry, source) for entry in parsed)
            except Exception as error:
                status["state"] = "error"
                status["error"] = str(error)[:180]
            source_statuses.append(status)

        appended: list[PlaylistEntry] = []
        seen_candidate_keys: set[str] = set()
        counts = {
            "candidateCount": len(all_candidates),
            "candidateVietnamese": 0,
            "candidateInternationalMovies": 0,
            "candidateInternationalFootball": 0,
            "newHealthChecked": 0,
            "newPassed": 0,
            "duplicateSkipped": 0,
            "filteredOut": 0,
            "unauthorizedSkipped": 0,
            "probeFailed": 0,
        }
        probe_metadata: dict[str, dict[str, object]] = {}
        for entry, source in all_candidates:
            key = normalise_url(entry.url)
            if key in old_keys or key in seen_candidate_keys:
                counts["duplicateSkipped"] += 1
                continue
            seen_candidate_keys.add(key)
            scope = channel_scope(entry)
            if scope == "vietnam":
                counts["candidateVietnamese"] += 1
            elif scope == "international-movies":
                counts["candidateInternationalMovies"] += 1
            elif scope == "international-football":
                counts["candidateInternationalFootball"] += 1
            else:
                counts["filteredOut"] += 1
                continue
            if not source_is_authorized(source) or is_restricted_url(entry.url, entry.name):
                counts["unauthorizedSkipped"] += 1
                continue
            counts["newHealthChecked"] += 1
            try:
                probe = self.prober(entry, source) or {}
            except Exception:
                counts["probeFailed"] += 1
                continue
            if not bool(probe.get("live")):
                continue
            counts["newPassed"] += 1
            appended.append(entry)
            probe_metadata[key] = dict(probe)

        new_text = old_text
        if appended:
            separator = "" if old_text.endswith("\n") else "\n"
            new_text = old_text + separator + "\n".join(entry.render() for entry in appended) + "\n"
        validate_m3u(new_text)
        new_entries = parse_m3u(new_text)
        new_keys = {normalise_url(entry.url) for entry in new_entries}
        if len(new_keys) < len(old_keys):
            raise RuntimeError("append-only regression guard: unique URL count decreased")
        old_removed = old_keys.difference(new_keys)
        if old_removed:
            raise RuntimeError("append-only regression guard: an existing URL was removed")
        if not new_text.startswith(old_text.rstrip("\n") if old_text.endswith("\n") else old_text):
            raise RuntimeError("append-only regression guard: existing M3U bytes changed")

        manifest_entries = [
            replace(entry, metadata=probe_metadata.get(normalise_url(entry.url), {}))
            for entry in new_entries
        ]
        manifest = build_manifest(manifest_entries, previous_manifest)

        # Build and validate all dependent outputs before replacing the
        # production M3U. A formatting/manifest error must never leave a
        # partially updated data snapshot.
        if write and appended:
            atomic_write(self.m3u_path, new_text)
        previous_channels = (previous_manifest or {}).get("channels", [])
        manifest_needs_initialisation = bool(manifest["channels"]) and not previous_channels
        if write and (appended or manifest_needs_initialisation or not self.manifest_path.is_file()):
            atomic_write_json(self.manifest_path, manifest)

        report = {
            "schema": 1,
            "generatedAt": utc_now(),
            "appendOnly": True,
            "oldUniqueUrlCount": len(old_keys),
            "sourceCount": len(sources),
            "candidateCount": counts["candidateCount"],
            "candidateVietnamese": counts["candidateVietnamese"],
            "candidateInternationalMovies": counts["candidateInternationalMovies"],
            "candidateInternationalFootball": counts["candidateInternationalFootball"],
            "newHealthChecked": counts["newHealthChecked"],
            "newPassed": counts["newPassed"],
            "urlAdded": len(appended),
            "duplicateSkipped": counts["duplicateSkipped"],
            "filteredOut": counts["filteredOut"],
            "unauthorizedSkipped": counts["unauthorizedSkipped"],
            "probeFailed": counts["probeFailed"],
            "oldUrlRemoved": len(old_removed),
            "totalUniqueUrlCount": len(new_keys),
            "sources": source_statuses,
        }
        if write:
            atomic_write_json(self.status_path, report)
        return report


def main() -> int:
    report = AppendOnlyPipeline().run()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
