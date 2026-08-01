from __future__ import annotations

"""Typed config loader for ``config.yaml``.

Approach: ``pyyaml.safe_load`` -> validate -> build a tree of frozen
dataclasses. ``yaml.safe_load`` returns ``Any`` at exactly one boundary; it is
narrowed to ``object`` immediately and routed through ``_as_dict``. Every field
is validated in helpers that raise ``ConfigError`` on any missing / mistyped /
unknown key.

Dataclasses are ``frozen=True`` (shallow-frozen -- a ``dict`` field is itself
mutable per its own type; we do not deep-freeze nested dicts).
"""

from dataclasses import dataclass
from pathlib import Path

import yaml

import constants
from errors import ConfigError
from models import AlertChannel, EventType


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EntityConfig:
    key: str
    name: str
    person: str
    cik: str
    filing_types: tuple[str, ...]


@dataclass(frozen=True)
class YouTubeQuerySet:
    broad_queries: tuple[str, ...]
    sweep_queries: tuple[str, ...]


@dataclass(frozen=True)
class YouTubeConfig:
    queries_by_entity: dict[str, YouTubeQuerySet]  # keyed by entity.key
    max_results_per_query: int
    master_manifest_path: Path  # resolved absolute
    known_channels: tuple[str, ...]  # confidence heuristic (optional; default ())
    framing_keywords: tuple[str, ...]  # confidence heuristic (optional; default ())


@dataclass(frozen=True)
class PodcastFeed:
    show: str
    url: str
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class PodcastRSSConfig:
    feeds: tuple[PodcastFeed, ...]


@dataclass(frozen=True)
class GoogleNewsConfig:
    queries: tuple[str, ...]


@dataclass(frozen=True)
class CNBCConfig:
    queries: tuple[str, ...]


@dataclass(frozen=True)
class ConferencePage:
    key: str
    conference: str
    url: str
    season_months: tuple[int, ...]
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class WebsiteDiffSite:
    key: str
    url: str
    keywords: tuple[str, ...]
    check_rss: bool


@dataclass(frozen=True)
class AlertRecipients:
    email_env: str
    phone_env: str


@dataclass(frozen=True)
class Paths:
    state_dir: Path  # resolved absolute
    reference_dir: Path  # resolved absolute


@dataclass(frozen=True)
class DispatchBridgeConfig:
    enabled: bool
    repo: str  # always a str "owner/name"; owner/name-validated ONLY when enabled
    event_type: str  # non-empty; defaults to DEFAULT_DISPATCH_EVENT_TYPE; capped length


@dataclass(frozen=True)
class AppConfig:
    entities: tuple[EntityConfig, ...]
    monitor_intervals: dict[str, int]
    youtube: YouTubeConfig
    podcast_rss: PodcastRSSConfig
    google_news: GoogleNewsConfig
    cnbc: CNBCConfig
    conference_pages: tuple[ConferencePage, ...]
    website_diff: tuple[WebsiteDiffSite, ...]
    alert_routing: dict[EventType, tuple[AlertChannel, ...]]
    alert_recipients: AlertRecipients
    paths: Paths
    dispatch_bridge: DispatchBridgeConfig


# --------------------------------------------------------------------------- #
# Typing helpers -- keep Any out; each raises ConfigError with a path-qualified
# message.
# --------------------------------------------------------------------------- #


def _require(d: dict[str, object], key: str, path: str) -> object:
    if key not in d:
        raise ConfigError(f"{path}: missing required key '{key}'")
    return d[key]


def _reject_unknown_keys(
    d: dict[str, object], allowed: frozenset[str], path: str
) -> None:
    unknown = set(d.keys()) - allowed
    if unknown:
        joined = ", ".join(sorted(unknown))
        raise ConfigError(f"{path}: unknown key(s): {joined}")


def _as_dict(v: object, path: str) -> dict[str, object]:
    if not isinstance(v, dict):
        raise ConfigError(f"{path}: expected a mapping, got {type(v).__name__}")
    result: dict[str, object] = {}
    for k, val in v.items():
        if not isinstance(k, str):
            raise ConfigError(
                f"{path}: mapping keys must be strings, got "
                f"{type(k).__name__} key {k!r}"
            )
        result[k] = val
    return result


def _as_list(v: object, path: str) -> list[object]:
    if not isinstance(v, list):
        raise ConfigError(f"{path}: expected a list, got {type(v).__name__}")
    return list(v)


def _as_str(v: object, path: str) -> str:
    if not isinstance(v, str):
        raise ConfigError(f"{path}: expected a string, got {type(v).__name__}")
    return v


def _as_nonempty_str(v: object, path: str) -> str:
    s = _as_str(v, path)
    if s == "":
        raise ConfigError(f"{path}: must be a non-empty string")
    return s


def _as_int(v: object, path: str) -> int:
    # bool is a subclass of int; reject it so True/False cannot pass as 1/0.
    if isinstance(v, bool):
        raise ConfigError(f"{path}: expected an integer, got bool")
    if not isinstance(v, int):
        raise ConfigError(f"{path}: expected an integer, got {type(v).__name__}")
    return v


def _as_bool(v: object, path: str) -> bool:
    if not isinstance(v, bool):
        raise ConfigError(f"{path}: expected a boolean, got {type(v).__name__}")
    return v


def _as_str_tuple(v: object, path: str) -> tuple[str, ...]:
    items = _as_list(v, path)
    return tuple(_as_str(item, f"{path}[{i}]") for i, item in enumerate(items))


def _as_nonempty_str_tuple(v: object, path: str) -> tuple[str, ...]:
    items = _as_list(v, path)
    return tuple(
        _as_nonempty_str(item, f"{path}[{i}]") for i, item in enumerate(items)
    )


def _as_optional_str_tuple(v: object, path: str) -> tuple[str, ...]:
    """Optional curation list: an empty list is ALLOWED (-> ``()``), but the
    value MUST be a list (``null``/scalar -> ConfigError) and every item must be
    a non-empty string. Callers pass ``d.get(key, [])`` so an absent key yields
    ``()``.
    """
    items = _as_list(v, path)  # null / scalar -> ConfigError
    return tuple(
        _as_nonempty_str(item, f"{path}[{i}]") for i, item in enumerate(items)
    )


def _as_int_tuple(v: object, path: str) -> tuple[int, ...]:
    items = _as_list(v, path)
    return tuple(_as_int(item, f"{path}[{i}]") for i, item in enumerate(items))


def _parse_cik(v: object, path: str) -> str:
    """Accept ``str`` or ``int``; reject ``bool``; reject non-digit content,
    empty / zero-length, all-zeros, and >10 digits. Left-pad a valid non-zero
    digit string to 10 digits."""
    if isinstance(v, bool):
        raise ConfigError(f"{path}: CIK must be a digit string or int, got bool")
    if isinstance(v, int):
        s = str(v)
    elif isinstance(v, str):
        s = v
    else:
        raise ConfigError(
            f"{path}: CIK must be a digit string or int, got {type(v).__name__}"
        )
    if s == "":
        raise ConfigError(f"{path}: CIK must not be empty")
    if not s.isdigit():
        raise ConfigError(
            f"{path}: expected non-zero digit string or int, got {s!r}"
        )
    if len(s) > 10:
        raise ConfigError(f"{path}: CIK has more than 10 digits: {s!r}")
    if int(s) == 0:
        raise ConfigError(f"{path}: CIK must not be all zeros, got {s!r}")
    return s.zfill(10)


# --------------------------------------------------------------------------- #
# Section builders
# --------------------------------------------------------------------------- #

_ENTITY_KEYS = frozenset({"key", "name", "person", "cik", "filing_types"})


def _build_entity(v: object, path: str) -> EntityConfig:
    d = _as_dict(v, path)
    _reject_unknown_keys(d, _ENTITY_KEYS, path)
    key = _as_nonempty_str(_require(d, "key", path), f"{path}.key")
    name = _as_nonempty_str(_require(d, "name", path), f"{path}.name")
    person = _as_nonempty_str(_require(d, "person", path), f"{path}.person")
    cik = _parse_cik(_require(d, "cik", path), f"{path}.cik")
    filing_types = _as_nonempty_str_tuple(
        _require(d, "filing_types", path), f"{path}.filing_types"
    )
    return EntityConfig(
        key=key, name=name, person=person, cik=cik, filing_types=filing_types
    )


def _build_entities(v: object, path: str) -> tuple[EntityConfig, ...]:
    items = _as_list(v, path)
    entities = tuple(
        _build_entity(item, f"{path}[{i}]") for i, item in enumerate(items)
    )
    seen_keys: set[str] = set()
    seen_ciks: set[str] = set()
    for e in entities:
        if e.key in seen_keys:
            raise ConfigError(f"{path}: duplicate entity key {e.key!r}")
        seen_keys.add(e.key)
        if e.cik in seen_ciks:
            raise ConfigError(f"{path}: duplicate entity cik {e.cik!r}")
        seen_ciks.add(e.cik)
    return entities


def _build_monitor_intervals(v: object, path: str) -> dict[str, int]:
    d = _as_dict(v, path)
    result: dict[str, int] = {}
    for k, val in d.items():
        interval = _as_int(val, f"{path}.{k}")
        if interval <= 0:
            raise ConfigError(f"{path}.{k}: interval must be positive")
        result[k] = interval
    # Exact-set validation against MONITOR_NAMES.
    keys = frozenset(result.keys())
    missing = constants.MONITOR_NAMES - keys
    unknown = keys - constants.MONITOR_NAMES
    if missing:
        raise ConfigError(
            f"{path}: missing monitor(s): {', '.join(sorted(missing))}"
        )
    if unknown:
        raise ConfigError(
            f"{path}: unknown monitor(s): {', '.join(sorted(unknown))}"
        )
    return result


_YOUTUBE_QUERYSET_KEYS = frozenset({"broad_queries", "sweep_queries"})
_YOUTUBE_KEYS = frozenset(
    {
        "queries_by_entity",
        "max_results_per_query",
        "master_manifest_path",
        "known_channels",
        "framing_keywords",
    }
)


def _build_youtube(
    v: object, path: str, entity_keys: frozenset[str], anchor: Path
) -> YouTubeConfig:
    d = _as_dict(v, path)
    _reject_unknown_keys(d, _YOUTUBE_KEYS, path)

    qbe_raw = _as_dict(
        _require(d, "queries_by_entity", path), f"{path}.queries_by_entity"
    )
    queries_by_entity: dict[str, YouTubeQuerySet] = {}
    for ek, qv in qbe_raw.items():
        qpath = f"{path}.queries_by_entity.{ek}"
        if ek not in entity_keys:
            raise ConfigError(
                f"{qpath}: key does not match any defined entities[].key"
            )
        qd = _as_dict(qv, qpath)
        _reject_unknown_keys(qd, _YOUTUBE_QUERYSET_KEYS, qpath)
        broad = _as_nonempty_str_tuple(
            _require(qd, "broad_queries", qpath), f"{qpath}.broad_queries"
        )
        sweep = _as_nonempty_str_tuple(
            _require(qd, "sweep_queries", qpath), f"{qpath}.sweep_queries"
        )
        queries_by_entity[ek] = YouTubeQuerySet(
            broad_queries=broad, sweep_queries=sweep
        )

    max_results = _as_int(
        _require(d, "max_results_per_query", path),
        f"{path}.max_results_per_query",
    )
    if max_results <= 0:
        raise ConfigError(f"{path}.max_results_per_query: must be > 0")

    manifest_rel = _as_nonempty_str(
        _require(d, "master_manifest_path", path),
        f"{path}.master_manifest_path",
    )
    manifest_path = (anchor / manifest_rel).resolve()

    # Optional curation lists for the confidence heuristic. Absent -> (); an
    # empty list is allowed; null/scalar/empty-item/non-str-item -> ConfigError.
    known_channels = _as_optional_str_tuple(
        d.get("known_channels", []), f"{path}.known_channels"
    )
    framing_keywords = _as_optional_str_tuple(
        d.get("framing_keywords", []), f"{path}.framing_keywords"
    )

    return YouTubeConfig(
        queries_by_entity=queries_by_entity,
        max_results_per_query=max_results,
        master_manifest_path=manifest_path,
        known_channels=known_channels,
        framing_keywords=framing_keywords,
    )


_PODCAST_FEED_KEYS = frozenset({"show", "url", "keywords"})
_PODCAST_RSS_KEYS = frozenset({"feeds"})


def _build_podcast_rss(v: object, path: str) -> PodcastRSSConfig:
    # URL slots stay allowed-empty (discovery deferred). Everything else
    # non-empty.
    d = _as_dict(v, path)
    _reject_unknown_keys(d, _PODCAST_RSS_KEYS, path)
    items = _as_list(_require(d, "feeds", path), f"{path}.feeds")
    feeds: list[PodcastFeed] = []
    for i, item in enumerate(items):
        fpath = f"{path}.feeds[{i}]"
        fd = _as_dict(item, fpath)
        _reject_unknown_keys(fd, _PODCAST_FEED_KEYS, fpath)
        show = _as_nonempty_str(_require(fd, "show", fpath), f"{fpath}.show")
        url = _as_str(_require(fd, "url", fpath), f"{fpath}.url")  # allowed-empty
        keywords = _as_nonempty_str_tuple(
            _require(fd, "keywords", fpath), f"{fpath}.keywords"
        )
        feeds.append(PodcastFeed(show=show, url=url, keywords=keywords))
    return PodcastRSSConfig(feeds=tuple(feeds))


_GOOGLE_NEWS_KEYS = frozenset({"queries"})


def _build_google_news(v: object, path: str) -> GoogleNewsConfig:
    d = _as_dict(v, path)
    _reject_unknown_keys(d, _GOOGLE_NEWS_KEYS, path)
    queries = _as_nonempty_str_tuple(
        _require(d, "queries", path), f"{path}.queries"
    )
    return GoogleNewsConfig(queries=queries)


_CNBC_KEYS = frozenset({"queries"})


def _build_cnbc(v: object, path: str) -> CNBCConfig:
    d = _as_dict(v, path)
    _reject_unknown_keys(d, _CNBC_KEYS, path)
    queries = _as_nonempty_str_tuple(
        _require(d, "queries", path), f"{path}.queries"
    )
    return CNBCConfig(queries=queries)


_CONFERENCE_PAGE_KEYS = frozenset(
    {"key", "conference", "url", "season_months", "keywords"}
)


def _build_conference_page(v: object, path: str) -> ConferencePage:
    d = _as_dict(v, path)
    _reject_unknown_keys(d, _CONFERENCE_PAGE_KEYS, path)
    key = _as_nonempty_str(_require(d, "key", path), f"{path}.key")
    conference = _as_nonempty_str(
        _require(d, "conference", path), f"{path}.conference"
    )
    url = _as_nonempty_str(_require(d, "url", path), f"{path}.url")
    season_months = _as_int_tuple(
        _require(d, "season_months", path), f"{path}.season_months"
    )
    seen_months: set[int] = set()
    for m in season_months:
        if m < 1 or m > 12:
            raise ConfigError(
                f"{path}.season_months: month out of range 1-12: {m}"
            )
        if m in seen_months:
            raise ConfigError(
                f"{path}.season_months: duplicate month: {m}"
            )
        seen_months.add(m)
    keywords = _as_nonempty_str_tuple(
        _require(d, "keywords", path), f"{path}.keywords"
    )
    return ConferencePage(
        key=key,
        conference=conference,
        url=url,
        season_months=season_months,
        keywords=keywords,
    )


def _build_conference_pages(v: object, path: str) -> tuple[ConferencePage, ...]:
    items = _as_list(v, path)
    pages = tuple(
        _build_conference_page(item, f"{path}[{i}]")
        for i, item in enumerate(items)
    )
    seen: set[str] = set()
    for p in pages:
        if p.key in seen:
            raise ConfigError(f"{path}: duplicate conference key {p.key!r}")
        seen.add(p.key)
    return pages


_WEBSITE_DIFF_KEYS = frozenset({"key", "url", "keywords", "check_rss"})


def _build_website_diff_site(v: object, path: str) -> WebsiteDiffSite:
    d = _as_dict(v, path)
    _reject_unknown_keys(d, _WEBSITE_DIFF_KEYS, path)
    key = _as_nonempty_str(_require(d, "key", path), f"{path}.key")
    url = _as_nonempty_str(_require(d, "url", path), f"{path}.url")
    keywords = _as_str_tuple(
        _require(d, "keywords", path), f"{path}.keywords"
    )  # empty list allowed (alert on any diff); items themselves non-empty
    for i, kw in enumerate(keywords):
        if kw == "":
            raise ConfigError(f"{path}.keywords[{i}]: must be a non-empty string")
    check_rss = _as_bool(_require(d, "check_rss", path), f"{path}.check_rss")
    return WebsiteDiffSite(
        key=key, url=url, keywords=keywords, check_rss=check_rss
    )


def _build_website_diff(v: object, path: str) -> tuple[WebsiteDiffSite, ...]:
    items = _as_list(v, path)
    sites = tuple(
        _build_website_diff_site(item, f"{path}[{i}]")
        for i, item in enumerate(items)
    )
    seen: set[str] = set()
    for s in sites:
        if s.key in seen:
            raise ConfigError(f"{path}: duplicate website_diff key {s.key!r}")
        seen.add(s.key)
    return sites


def _build_alert_routing(
    v: object, path: str
) -> dict[EventType, tuple[AlertChannel, ...]]:
    d = _as_dict(v, path)

    valid_events = {e.value: e for e in EventType}
    valid_channels = {c.value: c for c in AlertChannel}

    routing: dict[EventType, tuple[AlertChannel, ...]] = {}
    for k, val in d.items():
        kpath = f"{path}.{k}"
        if k not in valid_events:
            raise ConfigError(f"{path}: unknown event type key {k!r}")
        event = valid_events[k]
        channel_strs = _as_list(val, kpath)
        channels: list[AlertChannel] = []
        for i, cs in enumerate(channel_strs):
            cstr = _as_str(cs, f"{kpath}[{i}]")
            if cstr not in valid_channels:
                raise ConfigError(f"{kpath}[{i}]: unknown alert channel {cstr!r}")
            channels.append(valid_channels[cstr])
        routing[event] = tuple(channels)

    # Exact-set validation over EventType.
    present = frozenset(routing.keys())
    all_events = frozenset(EventType)
    missing = all_events - present
    unknown = present - all_events  # cannot happen (guarded above) but symmetric
    if missing:
        names = ", ".join(sorted(e.value for e in missing))
        raise ConfigError(f"{path}: missing event type(s): {names}")
    if unknown:  # pragma: no cover - unreachable; keys validated above
        names = ", ".join(sorted(e.value for e in unknown))
        raise ConfigError(f"{path}: unknown event type(s): {names}")
    return routing


_ALERT_RECIPIENTS_KEYS = frozenset({"email_env", "phone_env"})


def _build_alert_recipients(v: object, path: str) -> AlertRecipients:
    d = _as_dict(v, path)
    _reject_unknown_keys(d, _ALERT_RECIPIENTS_KEYS, path)
    email_env = _as_nonempty_str(
        _require(d, "email_env", path), f"{path}.email_env"
    )
    phone_env = _as_nonempty_str(
        _require(d, "phone_env", path), f"{path}.phone_env"
    )
    return AlertRecipients(email_env=email_env, phone_env=phone_env)


_PATHS_KEYS = frozenset({"state_dir", "reference_dir"})


def _build_paths(v: object, path: str, anchor: Path) -> Paths:
    d = _as_dict(v, path)
    _reject_unknown_keys(d, _PATHS_KEYS, path)
    state_dir_rel = _as_nonempty_str(
        _require(d, "state_dir", path), f"{path}.state_dir"
    )
    reference_dir_rel = _as_nonempty_str(
        _require(d, "reference_dir", path), f"{path}.reference_dir"
    )
    return Paths(
        state_dir=(anchor / state_dir_rel).resolve(),
        reference_dir=(anchor / reference_dir_rel).resolve(),
    )


_DISPATCH_BRIDGE_KEYS = frozenset({"enabled", "repo", "event_type"})


def _build_dispatch_bridge(v: object, path: str) -> DispatchBridgeConfig:
    """Parse a PRESENT ``dispatch_bridge`` section (an absent section is handled
    in ``load_config`` and never reaches here).

    Strictness:
      - present-but-not-a-mapping (``null``/list/scalar/blank) -> ConfigError with
        a remedy, BEFORE any key access.
      - ``enabled`` REQUIRED (non-bool -> ConfigError).
      - ``repo`` must ALWAYS be a ``str`` (reject non-str even when disabled);
        owner/name-validated (exactly two non-empty, whitespace-free parts) ONLY
        when ``enabled: true``.
      - ``event_type`` OPTIONAL: absent -> DEFAULT_DISPATCH_EVENT_TYPE; present ->
        non-empty after strip, capped at DISPATCH_EVENT_TYPE_MAX_CHARS.
    """
    if not isinstance(v, dict):
        raise ConfigError(
            f"{path}: expected a mapping (remove the key entirely, or set "
            f"'enabled: false')"
        )
    d = _as_dict(v, path)
    _reject_unknown_keys(d, _DISPATCH_BRIDGE_KEYS, path)

    enabled = _as_bool(_require(d, "enabled", path), f"{path}.enabled")

    # repo must ALWAYS be a str (reject non-str even when disabled); default "".
    repo_raw = d.get("repo", "")
    repo = _as_str(repo_raw, f"{path}.repo").strip()
    if enabled:
        if repo == "":
            raise ConfigError(f"{path}.repo: required and non-empty when enabled")
        parts = repo.split("/")
        if len(parts) != 2 or any(
            part == "" or any(ch.isspace() for ch in part) for part in parts
        ):
            raise ConfigError(
                f"{path}.repo: must be 'owner/name' (two non-empty, "
                f"whitespace-free parts), got {repo!r}"
            )

    # event_type optional -> default; present must be non-empty + length-capped.
    if "event_type" in d:
        event_type = _as_str(d["event_type"], f"{path}.event_type").strip()
        if event_type == "":
            raise ConfigError(f"{path}.event_type: must be a non-empty string")
        if len(event_type) > constants.DISPATCH_EVENT_TYPE_MAX_CHARS:
            raise ConfigError(
                f"{path}.event_type: exceeds "
                f"{constants.DISPATCH_EVENT_TYPE_MAX_CHARS} characters"
            )
    else:
        event_type = constants.DEFAULT_DISPATCH_EVENT_TYPE

    return DispatchBridgeConfig(enabled=enabled, repo=repo, event_type=event_type)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

_TOP_LEVEL_KEYS = frozenset(
    {
        "entities",
        "monitor_intervals",
        "youtube",
        "podcast_rss",
        "google_news",
        "cnbc",
        "conference_pages",
        "website_diff",
        "alert_routing",
        "alert_recipients",
        "paths",
        "dispatch_bridge",
    }
)


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load, validate, and return the application config.

    When ``path`` is None, ``config.yaml`` is resolved relative to THIS module's
    directory (the repo root under the flat layout), NOT the CWD, so a default
    ``load_config()`` is deterministic regardless of the launch directory.

    All file-access and parse failures are wrapped as ``ConfigError``.
    """
    if path is None:
        effective = (Path(__file__).parent / "config.yaml").resolve()
    else:
        effective = Path(path).resolve()

    try:
        text = effective.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {effective}") from exc
    except OSError as exc:
        raise ConfigError(f"config file unreadable: {effective}: {exc}") from exc

    try:
        # yaml.safe_load returns Any; narrowed immediately below.
        raw: object = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"config file has invalid YAML: {effective}: {exc}") from exc

    if raw is None:
        raise ConfigError(f"config file is empty: {effective}")

    anchor = effective.parent
    root = _as_dict(raw, "config")
    _reject_unknown_keys(root, _TOP_LEVEL_KEYS, "config")

    entities = _build_entities(
        _require(root, "entities", "config"), "config.entities"
    )
    entity_keys = frozenset(e.key for e in entities)

    monitor_intervals = _build_monitor_intervals(
        _require(root, "monitor_intervals", "config"), "config.monitor_intervals"
    )
    youtube = _build_youtube(
        _require(root, "youtube", "config"),
        "config.youtube",
        entity_keys,
        anchor,
    )
    podcast_rss = _build_podcast_rss(
        _require(root, "podcast_rss", "config"), "config.podcast_rss"
    )
    google_news = _build_google_news(
        _require(root, "google_news", "config"), "config.google_news"
    )
    cnbc = _build_cnbc(_require(root, "cnbc", "config"), "config.cnbc")
    conference_pages = _build_conference_pages(
        _require(root, "conference_pages", "config"), "config.conference_pages"
    )
    website_diff = _build_website_diff(
        _require(root, "website_diff", "config"), "config.website_diff"
    )
    alert_routing = _build_alert_routing(
        _require(root, "alert_routing", "config"), "config.alert_routing"
    )
    alert_recipients = _build_alert_recipients(
        _require(root, "alert_recipients", "config"), "config.alert_recipients"
    )
    paths = _build_paths(
        _require(root, "paths", "config"), "config.paths", anchor
    )

    # Optional section. Distinguish ABSENT (omitted key) from present-null via
    # `"dispatch_bridge" in root` -- `.get(...)` cannot tell them apart. Absent ->
    # inert disabled default (no validation); present (incl. explicit null) ->
    # run the strict builder, which rejects a non-mapping value with a remedy.
    if "dispatch_bridge" in root:
        dispatch_bridge = _build_dispatch_bridge(
            root["dispatch_bridge"], "config.dispatch_bridge"
        )
    else:
        dispatch_bridge = DispatchBridgeConfig(
            enabled=False,
            repo="",
            event_type=constants.DEFAULT_DISPATCH_EVENT_TYPE,
        )

    return AppConfig(
        entities=entities,
        monitor_intervals=monitor_intervals,
        youtube=youtube,
        podcast_rss=podcast_rss,
        google_news=google_news,
        cnbc=cnbc,
        conference_pages=conference_pages,
        website_diff=website_diff,
        alert_routing=alert_routing,
        alert_recipients=alert_recipients,
        paths=paths,
        dispatch_bridge=dispatch_bridge,
    )
