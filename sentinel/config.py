"""Configuration: secrets from the environment, sites from config.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

import yaml

DEFAULT_UA = (
    "CA-Sentinel/1.0 (+public-page change monitor; conditional GET; "
    "contact: set CONTACT_EMAIL)"
)

DEFAULT_WORDLIST = [
    "secret", "secretarea", "secret-area", "secrets", "alpha", "gems", "gem",
    "vip", "calls", "picks", "drops", "new", "latest", "private", "members",
    "insider", "insiders", "early", "next", "signals", "research", "premium",
]


@dataclass(slots=True)
class TargetConfig:
    path: str
    interval: float = 5.0
    priority: str = "normal"


@dataclass(slots=True)
class DiscoveryConfig:
    crawl_links: bool = True
    probe_routes: bool = False
    probe_interval: float = 600.0
    route_wordlist: list[str] = field(default_factory=lambda: list(DEFAULT_WORDLIST))
    ct_logs: bool = False
    ct_interval: float = 3600.0
    #: Cap on auto-added targets, so a link explosion cannot melt the poller.
    max_watched: int = 50
    #: Interval assigned to targets that discovery adds.
    discovered_interval: float = 15.0
    #: Seconds between individual probe requests. Keeps discovery polite.
    probe_delay: float = 0.4


@dataclass(slots=True)
class SiteConfig:
    id: str
    base_url: str
    parser: str = "generic"
    enabled: bool = True
    targets: list[TargetConfig] = field(default_factory=list)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)

    def url_for(self, path: str) -> str:
        return urljoin(self.base_url.rstrip("/") + "/", path.lstrip("/"))


@dataclass(slots=True)
class AppConfig:
    telegram_token: str
    telegram_chat_id: str
    db_path: str = "data/sentinel.db"
    log_dir: str = "logs"
    log_level: str = "INFO"
    user_agent: str = DEFAULT_UA
    http_timeout: float = 10.0
    max_connections: int = 32
    #: Candidates below this score are stored for audit but never alerted.
    min_confidence: float = 0.6
    #: "seed" records everything already on the site silently on first run;
    #: "alert" announces the current state too.
    bootstrap: str = "seed"
    #: Alert when discovery finds a previously unknown route.
    alert_on_new_route: bool = True
    #: Random +/- fraction applied to every poll interval, so targets do not
    #: line up into synchronised bursts.
    jitter: float = 0.15
    sites: list[SiteConfig] = field(default_factory=list)


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader; real environment variables always win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def load_config(
    config_path: str | Path = "config.yaml", env_path: str | Path = ".env"
) -> AppConfig:
    _load_dotenv(Path(env_path))

    raw: dict = {}
    cfg_file = Path(config_path)
    if cfg_file.exists():
        raw = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set "
            "(copy .env.example to .env and fill them in)."
        )

    defaults = raw.get("defaults", {}) or {}
    contact = os.environ.get("CONTACT_EMAIL", "").strip()
    user_agent = defaults.get("user_agent") or DEFAULT_UA
    if contact:
        user_agent = user_agent.replace("set CONTACT_EMAIL", contact)

    sites: list[SiteConfig] = []
    for entry in raw.get("sites", []) or []:
        if not entry.get("enabled", True):
            continue

        disc_raw = entry.get("discovery", {}) or {}
        discovery = DiscoveryConfig(
            crawl_links=disc_raw.get("crawl_links", True),
            probe_routes=disc_raw.get("probe_routes", False),
            probe_interval=float(disc_raw.get("probe_interval", 600)),
            route_wordlist=list(disc_raw.get("route_wordlist") or DEFAULT_WORDLIST),
            ct_logs=disc_raw.get("ct_logs", False),
            ct_interval=float(disc_raw.get("ct_interval", 3600)),
            max_watched=int(disc_raw.get("max_watched", 50)),
            discovered_interval=float(disc_raw.get("discovered_interval", 15)),
            probe_delay=float(disc_raw.get("probe_delay", 0.4)),
        )

        targets = [
            TargetConfig(
                path=t.get("path", "/"),
                interval=float(t.get("interval", defaults.get("interval", 5))),
                priority=t.get("priority", "normal"),
            )
            for t in (entry.get("targets") or [{"path": "/"}])
        ]

        sites.append(
            SiteConfig(
                id=entry["id"],
                base_url=entry["base_url"],
                parser=entry.get("parser", "generic"),
                enabled=True,
                targets=targets,
                discovery=discovery,
            )
        )

    return AppConfig(
        telegram_token=token,
        telegram_chat_id=chat_id,
        db_path=os.environ.get("DB_PATH") or defaults.get("db_path", "data/sentinel.db"),
        log_dir=os.environ.get("LOG_DIR") or defaults.get("log_dir", "logs"),
        log_level=os.environ.get("LOG_LEVEL") or defaults.get("log_level", "INFO"),
        user_agent=user_agent,
        http_timeout=float(defaults.get("http_timeout", 10)),
        max_connections=int(defaults.get("max_connections", 32)),
        min_confidence=float(defaults.get("min_confidence", 0.6)),
        bootstrap=(os.environ.get("BOOTSTRAP") or defaults.get("bootstrap", "seed")),
        alert_on_new_route=bool(defaults.get("alert_on_new_route", True)),
        jitter=float(defaults.get("jitter", 0.15)),
        sites=sites,
    )
