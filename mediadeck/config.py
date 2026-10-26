"""Chargement et validation de la configuration TOML."""

from __future__ import annotations

import os
import re
import secrets
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Emplacement par défaut, conforme XDG
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "mediadeck"
CONFIG_PATH = CONFIG_DIR / "config.toml"

MIN_TOKEN_LENGTH = 16
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
# Nom d'hôte ou IPv4, sans schéma, port ni chemin (inséré tel quel dans l'URL tablette)
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


class ConfigError(Exception):
    """Erreur de configuration lisible par l'utilisateur."""


@dataclass(frozen=True)
class Launcher:
    """Commande lançable depuis la tablette (argv, jamais de shell)."""

    id: str
    label: str
    command: tuple[str, ...]
    icon: str = "app"


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    token: str
    max_volume: float
    enable_input: bool
    enable_power: bool
    launchers: tuple[Launcher, ...]
    # Adresse annoncée dans l'URL/QR code ; vide = détection automatique
    advertise_host: str = ""
    # HTTPS : certificat + clé du serveur ; tls_ca (public) est servi sur /ca.crt pour l'installer sur la tablette
    tls_cert: Path | None = None
    tls_key: Path | None = None
    tls_ca: Path | None = None
    # Port HTTP en clair ouvert en plus du HTTPS (0 = désactivé) : au choix de la tablette, sans HTTPS ni CA à installer
    http_port: int = 0
    # Page d'administration des lanceurs ; les commandes brutes restent réservées au fichier de configuration
    admin_enabled: bool = True
    allow_custom_command: bool = False
    # Saisie de texte depuis la tablette : équivaut à un clavier complet, donc désactivée par défaut
    enable_text_input: bool = False
    # Dossiers parcourus par l'onglet Médias ; None = dossiers Musique / Vidéos / Images de l'utilisateur, () = onglet désactivé
    media_roots: tuple[Path, ...] | None = None
    # Miniatures et images d'illustration (ffmpeg) ; désactivables
    media_thumbnails: bool = True
    # Liens symboliques suivis où qu'ils pointent ; faux = seuls ceux dont la cible reste dans le dossier configuré
    media_follow_symlinks: bool = True

    @property
    def tls_enabled(self) -> bool:
        return self.tls_cert is not None


def generate_token() -> str:
    """Jeton aléatoire URL-safe (~256 bits)."""
    return secrets.token_urlsafe(32)


def _parse_path(server: dict[str, Any], key: str) -> Path | None:
    value = server.get(key, "")
    if not isinstance(value, str):
        raise ConfigError(f"server.{key} must be a string")
    return Path(value).expanduser() if value else None


def _parse_media_roots(media: dict[str, Any]) -> tuple[Path, ...] | None:
    """Dossiers de médias : chemins absolus (après ~), sans doublon ; absent = valeur par défaut, [] = désactivé."""
    raw = media.get("roots")
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(item, str) and item for item in raw):
        raise ConfigError("media.roots must be a list of folder paths")
    roots = [Path(item).expanduser() for item in raw]
    for root in roots:
        if not root.is_absolute():
            raise ConfigError(f"media.roots: '{root}' must be an absolute path (or start with ~)")
    return tuple(dict.fromkeys(roots))


def _parse_media_thumbnails(media: dict[str, Any]) -> bool:
    thumbnails = media.get("thumbnails", True)
    if not isinstance(thumbnails, bool):
        raise ConfigError("media.thumbnails must be a boolean")
    return thumbnails


def _parse_media_follow_symlinks(media: dict[str, Any]) -> bool:
    follow = media.get("follow_symlinks", True)
    if not isinstance(follow, bool):
        raise ConfigError("media.follow_symlinks must be a boolean")
    return follow


def _parse_launchers(raw: Any) -> tuple[Launcher, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("'launchers' must be an array of tables")
    launchers: list[Launcher] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ConfigError(f"launchers[{index}] must be a table")
        launcher_id = item.get("id")
        label = item.get("label")
        command = item.get("command")
        icon = item.get("icon", "app")
        if not isinstance(launcher_id, str) or not _ID_RE.match(launcher_id):
            raise ConfigError(f"launchers[{index}].id must match {_ID_RE.pattern}")
        if launcher_id in seen:
            raise ConfigError(f"duplicate launcher id '{launcher_id}'")
        if not isinstance(label, str) or not label:
            raise ConfigError(f"launchers[{index}].label must be a non-empty string")
        # La commande est une liste argv : aucun passage par un shell
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(arg, str) and arg for arg in command)
        ):
            raise ConfigError(f"launchers[{index}].command must be a non-empty list of strings")
        if not isinstance(icon, str):
            raise ConfigError(f"launchers[{index}].icon must be a string")
        seen.add(launcher_id)
        launchers.append(Launcher(launcher_id, label, tuple(command), icon))
    return tuple(launchers)


def parse_config(data: dict[str, Any]) -> Config:
    """Valide un dictionnaire issu du TOML et construit la Config."""
    server = data.get("server", {})
    features = data.get("features", {})
    audio = data.get("audio", {})
    admin = data.get("admin", {})
    media = data.get("media", {})
    if not all(isinstance(table, dict) for table in (server, features, audio, admin, media)):
        raise ConfigError("[server], [features], [audio], [admin] and [media] must be tables")

    host = server.get("host", "0.0.0.0")
    port = server.get("port", 8765)
    token = server.get("token", "")
    advertise_host = server.get("advertise_host", "")
    if not isinstance(host, str) or not host:
        raise ConfigError("server.host must be a non-empty string")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError("server.port must be an integer between 1 and 65535")
    if not isinstance(token, str) or len(token) < MIN_TOKEN_LENGTH:
        raise ConfigError(
            f"server.token must be at least {MIN_TOKEN_LENGTH} characters (run 'make token')"
        )

    if not isinstance(advertise_host, str) or (advertise_host and not _HOSTNAME_RE.match(advertise_host)):
        raise ConfigError("server.advertise_host must be a hostname or IPv4 address (no scheme, port or path)")

    tls_cert = _parse_path(server, "tls_cert")
    tls_key = _parse_path(server, "tls_key")
    tls_ca = _parse_path(server, "tls_ca")
    if (tls_cert is None) != (tls_key is None):
        raise ConfigError("server.tls_cert and server.tls_key must be set together")
    if tls_ca is not None and tls_cert is None:
        raise ConfigError("server.tls_ca requires server.tls_cert and server.tls_key")

    http_port = server.get("http_port", 0)
    if not isinstance(http_port, int) or isinstance(http_port, bool) or not 0 <= http_port <= 65535:
        raise ConfigError("server.http_port must be an integer between 0 (disabled) and 65535")
    if http_port and tls_cert is None:
        raise ConfigError("server.http_port requires TLS (without it, server.port already serves plain HTTP)")
    if http_port == port:
        raise ConfigError("server.http_port must differ from server.port")

    max_volume = audio.get("max_volume", 1.0)
    if not isinstance(max_volume, (int, float)) or not 0.1 <= float(max_volume) <= 1.5:
        raise ConfigError("audio.max_volume must be between 0.1 and 1.5")

    enable_input = features.get("input", True)
    enable_power = features.get("power", True)
    enable_text_input = features.get("text_input", False)
    if not all(isinstance(flag, bool) for flag in (enable_input, enable_power, enable_text_input)):
        raise ConfigError("features.input, features.power and features.text_input must be booleans")

    admin_enabled = admin.get("enabled", True)
    allow_custom_command = admin.get("allow_custom_command", False)
    if not isinstance(admin_enabled, bool) or not isinstance(allow_custom_command, bool):
        raise ConfigError("admin.enabled and admin.allow_custom_command must be booleans")

    return Config(
        host=host,
        port=port,
        token=token,
        max_volume=float(max_volume),
        enable_input=enable_input,
        enable_power=enable_power,
        launchers=_parse_launchers(data.get("launchers")),
        advertise_host=advertise_host,
        tls_cert=tls_cert,
        tls_key=tls_key,
        tls_ca=tls_ca,
        http_port=http_port,
        admin_enabled=admin_enabled,
        allow_custom_command=allow_custom_command,
        enable_text_input=enable_text_input,
        media_roots=_parse_media_roots(media),
        media_thumbnails=_parse_media_thumbnails(media),
        media_follow_symlinks=_parse_media_follow_symlinks(media),
    )


def load_config(path: Path | None = None) -> Config:
    """Lit le fichier TOML ; lève ConfigError avec un message explicite."""
    config_path = path or CONFIG_PATH
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {config_path} (run 'make config')") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc

    # Le fichier contient le jeton : on refuse s'il est lisible par les autres
    mode = config_path.stat().st_mode
    if mode & 0o077:
        raise ConfigError(f"{config_path} must not be readable by group/others (chmod 600)")

    return parse_config(data)
