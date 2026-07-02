from __future__ import annotations

import hashlib
import json
import re
import ssl
import time
from datetime import datetime
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

def get_ssl_context():
    """Return a CA-aware SSL context for bundled macOS builds.

    PyInstaller macOS bundles can miss the system CA lookup path used by
    urllib. certifi ships a known CA bundle with the app, so ScreenScraper
    HTTPS requests keep working without disabling certificate verification.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


SCREENSCRAPER_DEV_ID = ""
SCREENSCRAPER_DEV_PASSWORD = ""
SCREENSCRAPER_SOFTNAME = "Gentleman"
SCREENSCRAPER_LANGUAGE = "en"
SCREENSCRAPER_BASE_URL = "https://api.screenscraper.fr/api2/jeuInfos.php"
SCREENSCRAPER_SEARCH_URL = "https://api.screenscraper.fr/api2/jeuRecherche.php"
SCREENSCRAPER_USER_URL = "https://api.screenscraper.fr/api2/ssuserInfos.php"

SCRAPE_MODES = [
    "All Games",
    "Unscraped Only",
    "New & Missing Data",
]

SCRAPE_REGIONS = [
    "Same as Game",
    "Prioritize USA",
    "Prioritize Europe",
    "Prioritize Japan",
]

REGION_CODES = {
    "USA": "us",
    "Europe": "eu",
    "Japan": "jp",
}

SCREENSCRAPER_SYSTEM_IDS = {
    "Genesis": 1,
    "MasterSystem": 2,
    "NES": 3,
    "SNES": 4,
    "Gameboy": 9,
    "Gameboy2P": 9,
    "GameboyColor": 10,
    "VirtualBoy": 11,
    "GBA": 12,
    "GBA2P": 12,
    "GameCube": 13,
    "N64": 14,
    "NDS": 15,
    "Wii": 16,
    "3DS": 17,
    "WiiU": 18,
    "S32X": 19,
    "Sega32X": 19,
    "MegaCD": 20,
    "GameGear": 21,
    "Saturn": 22,
    "Dreamcast": 23,
    "NeoGeoPocket": 25,
    "Atari2600": 26,
    "AtariJaguar": 27,
    "AtariLynx": 28,
    "3DO": 29,
    "TGFX16": 31,
    "Xbox": 32,
    "Xbox360": 33,
    "XboxOne": 34,
    "Atari5200": 40,
    "Atari7800": 41,
    "Astrocade": 44,
    "WonderSwan": 45,
    "WonderSwanColor": 46,
    "ColecoVision": 48,
    "CoreGrafx": 50,
    "GameNWatch": 52,
    "PSX": 57,
    "PS2": 58,
    "PS3": 59,
    "PS4": 60,
    "PSP": 61,
    "PSVita": 62,
    "NeoGeoCD": 70,
    "PCFX": 72,
    "CasioPV1000": 74,
    "AdventureVision": 78,
    "ChannelF": 80,
    "NeoGeoPocketColor": 82,
    "GamePocket": 95,
    "GP32": 101,
    "Vectrex": 102,
    "GameMaster": 103,
    "Odyssey2": 104,
    "SuperGrafx": 105,
    "FDS": 106,
    "Satellaview": 107,
    "SG1000": 109,
    "PCECD": 114,
    "Intellivision": 115,
    "GameCom": 121,
    "N64DD": 122,
    "AmigaCD32": 130,
    "CDI": 133,
    "NeoGeo": 142,
    "JaguarCD": 171,
    "PokemonMini": 211,
    "Arcade": 75,
    "Nintendo64": 14,
    "Jaguar": 27,
    "TurboGrafx16": 31,
    "TurboGrafx16CD": 114,
    "Vita": 62,
    "Atari800": 43,
    "Archimedes": 84,
    "MegaDuck": 90,
    "MSX": 113,
    "MSX1": 113,
    "MSX2": 116,
    "MSX2Plus": 117,
    "NeoGeoAES": 142,
    "NeoGeoMVS": 142,
    "GenesisMSU": 1,
    "SNESMSU1": 4,
    "CPS1": 6,
    "CPS2": 7,
    "CPS3": 8,
    "Atomiswave": 53,
    "NAOMI": 56,
    "Triforce": 68,
    "Pico8": 234,
    "Amiga": 64,
    "Amiga500": 64,
    "Amiga1200": 64,
    "Amstrad": 65,
    "C64": 66,
    "VIC20": 73,
    "SuperVision": 67,
    "ZXSpectrum": 76,
    "ZX81": 77,
    "AtariST": 42,
    "DOS": 135,
    "PC": 135,
    "PC88": 221,
    "PC98": 208,
    "X68000": 79,
}


class ScreenScraperQuotaError(RuntimeError):
    pass


class ScreenScraperDailyQuotaError(ScreenScraperQuotaError):
    pass


def build_screenscraper_daily_quota_message(details: str = "") -> str:
    message = (
        "ScreenScraper daily quota has been reached. "
        "Scraping has been stopped. The daily quota normally refreshes at midnight."
    )
    details = str(details or "").strip()
    return f"{message} Details: {details}" if details else message


def _walk_values(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_values(item)


def _first_present_int(data: dict, names: set[str]) -> int | None:
    wanted = {str(name).lower() for name in names}
    for container in _walk_values(data):
        for key, value in container.items():
            if str(key).lower() not in wanted:
                continue
            if isinstance(value, (dict, list, tuple, set)):
                continue
            try:
                text = str(value).strip()
                if not text:
                    continue
                slash = re.search(r"(\d[\d\s.,]*)\s*/\s*(\d[\d\s.,]*)", text)
                if slash:
                    return int(re.sub(r"\D", "", slash.group(1)))
                digits = re.sub(r"[^\d-]", "", text)
                if digits:
                    return int(digits)
            except Exception:
                continue
    return None


def _first_present_text(data: dict, names: set[str]) -> str:
    wanted = {str(name).lower() for name in names}
    for container in _walk_values(data):
        for key, value in container.items():
            if str(key).lower() not in wanted:
                continue
            text = str(value or "").strip()
            if text:
                return text
    return ""


def _first_quota_pair(data: dict) -> tuple[int | None, int | None]:
    if not isinstance(data, dict):
        return (None, None)

    interesting_terms = (
        "quota", "request", "requests", "scrape", "scrapes", "api", "daily", "day", "jour"
    )

    for container in _walk_values(data):
        for key, value in container.items():
            key_l = str(key or "").lower()
            if not any(term in key_l for term in interesting_terms):
                continue
            if isinstance(value, (dict, list, tuple, set)):
                continue
            text = str(value or "").strip()
            if not text:
                continue
            match = re.search(r"(\d[\d\s.,]*)\s*/\s*(\d[\d\s.,]*)", text)
            if not match:
                continue
            try:
                used = int(re.sub(r"\D", "", match.group(1)))
                limit = int(re.sub(r"\D", "", match.group(2)))
            except Exception:
                continue
            if limit > 0:
                return (used, limit)
    return (None, None)



def _safe_int(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        text = str(value).strip()
        if not text:
            return None
        if "/" in text:
            text = text.split("/", 1)[0]
        digits = re.sub(r"[^\d-]", "", text)
        if not digits or digits == "-":
            return None
        return int(digits)
    except Exception:
        return None


def _quota_from_text(value) -> tuple[int | None, int | None]:
    text = str(value or "").strip()
    if not text:
        return (None, None)
    match = re.search(r"(\d[\d\s.,]*)\s*/\s*(\d[\d\s.,]*)", text)
    if not match:
        return (None, None)
    try:
        used = int(re.sub(r"\D", "", match.group(1)))
        limit = int(re.sub(r"\D", "", match.group(2)))
    except Exception:
        return (None, None)
    return (used, limit if limit > 0 else None)


def _find_screen_scraper_user_containers(data: dict) -> list[dict]:
    containers: list[dict] = []
    if not isinstance(data, dict):
        return containers

    response = data.get("response")
    if isinstance(response, dict):
        for key in ("ssuser", "user", "joueur", "utilisateur"):
            value = response.get(key)
            if isinstance(value, dict):
                containers.append(value)
        containers.append(response)

    for key in ("ssuser", "user", "joueur", "utilisateur"):
        value = data.get(key)
        if isinstance(value, dict):
            containers.append(value)

    # Keep order, remove duplicate object identities.
    unique: list[dict] = []
    seen = set()
    for item in containers:
        item_id = id(item)
        if item_id not in seen:
            unique.append(item)
            seen.add(item_id)
    return unique


def _quota_from_known_screen_scraper_user_fields(data: dict) -> dict:
    containers = _find_screen_scraper_user_containers(data)
    if not containers:
        containers = [data] if isinstance(data, dict) else []

    used_keys = {
        "requeststoday", "requesttoday", "requestsday", "requests_today", "request_today",
        "nbscrapetoday", "nbscrapeursjour", "nbrequeststoday", "scrapestoday", "scrapetoday",
        "dailyused", "daily_used", "usedtoday", "used_day", "apiusedtoday",
    }
    limit_keys = {
        "maxrequestsperday", "maxrequestperday", "maxrequestsday", "maxrequesttoday",
        "maxrequeststoday", "requestslimitday", "requestsdaymax", "dailymax", "daily_limit",
        "maxday", "maxrequetesjour", "maxrequestsjour", "maxrequests", "maxscrapestoday",
        "maxscrapetoday", "dailymaxrequests",
    }
    remaining_keys = {
        "requestsremaining", "requestsremainingday", "remainingrequests", "remainingrequestsday",
        "requestsleft", "requestsleftday", "remainingday", "quota_remaining", "dayremaining",
        "dailyremaining", "daily_remaining",
    }

    used = limit = remaining = None
    username = ""

    for container in containers:
        for key, value in container.items():
            key_l = str(key or "").lower()
            if not username and key_l in {"pseudo", "ssid", "username", "nom", "user"}:
                username = str(value or "").strip()
            pair_used, pair_limit = _quota_from_text(value)
            if pair_used is not None and pair_limit is not None:
                if used is None and any(token in key_l for token in ("request", "quota", "scrape", "jour", "today")):
                    used = pair_used
                    limit = pair_limit
            if used is None and key_l in used_keys:
                used = _safe_int(value)
            if limit is None and key_l in limit_keys:
                limit = _safe_int(value)
            if remaining is None and key_l in remaining_keys:
                remaining = _safe_int(value)

    if used is None:
        used = _first_present_int(data, used_keys)
    if limit is None:
        limit = _first_present_int(data, limit_keys)
    if remaining is None:
        remaining = _first_present_int(data, remaining_keys)
    if not username:
        username = _first_present_text(data, {"pseudo", "ssid", "username", "nom", "user"})

    if remaining is None and used is not None and limit is not None:
        remaining = max(0, limit - used)
    if used is None and remaining is not None and limit is not None:
        used = max(0, limit - remaining)
    if limit is None and used is not None:
        limit = 20000
    if limit is None and remaining is not None:
        limit = 20000
        used = max(0, limit - remaining)

    quota = {}
    if username:
        quota["username"] = username
    if used is not None:
        quota["used"] = used
    if limit is not None:
        quota["limit"] = limit
    if remaining is not None:
        quota["remaining"] = remaining
    if quota:
        quota["updated_at"] = datetime.now().isoformat(timespec="seconds")
    return quota

def _quota_debug_keys(data: dict) -> list[str]:
    keys: list[str] = []
    if not isinstance(data, dict):
        return keys
    wanted = ("quota", "request", "scrape", "limit", "max", "jour", "today")
    for container in _walk_values(data):
        for key in container.keys():
            key_s = str(key or "")
            key_l = key_s.lower()
            if any(term in key_l for term in wanted):
                keys.append(key_s)
    return sorted(set(keys))[:20]


def _looks_like_quota_error(message: str) -> bool:
    text = str(message or "").lower()
    return any(term in text for term in (
        "quota", "rate limit", "ratelimit", "too many", "maximum", "max requests",
        "maxrequests", "limit reached", "limite", "niveau maximum", "overquota",
    ))


def _looks_like_daily_quota_error(message: str) -> bool:
    text = str(message or "").lower()
    return any(term in text for term in (
        "daily", "per day", "day quota", "daily quota", "quota journali",
        "quota journalier", "quotidien", "today", "aujourd", "requeststoday",
        "requests today", "nbscrapetoday", "nbscrapeursjour", "maxrequestsperday",
        "maxrequetesjour",
    ))


def _looks_like_login_error(text: str) -> bool:
    lower = str(text or "").lower()
    return any(token in lower for token in (
        "erreur de login",
        "verifier les identifiants",
        "vérifier les identifiants",
        "identifiants utilisateurs",
        "invalid login",
        "login rejected",
        "credentials were rejected",
        "bad credentials",
        "wrong password",
    ))


def _contains_login_error(value) -> bool:
    if isinstance(value, dict):
        return any(_contains_login_error(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_login_error(item) for item in value)
    if isinstance(value, str):
        return _looks_like_login_error(value)
    return False


def _quota_info_indicates_daily_limit(quota: dict) -> bool:
    if not isinstance(quota, dict):
        return False
    remaining = quota.get("remaining")
    used = quota.get("used")
    limit = quota.get("limit")
    try:
        if remaining is not None and int(remaining) <= 0:
            return True
    except Exception:
        pass
    try:
        if used is not None and limit is not None and int(used) >= int(limit):
            return True
    except Exception:
        pass
    return False

@dataclass
class GameMetadataIdentity:
    system: str
    launcher: str
    rom: str
    rom_name: str
    rom_stem: str

class MetadataCache:
    def __init__(self, base_dir: Path):
        self.root = base_dir / "metadata" / "screenscraper"
        self.root.mkdir(parents=True, exist_ok=True)
        self._index_cache: dict[str, dict] = {}
        self._data_cache: dict[str, dict] = {}
        self._missing_cache: set[str] = set()

    def safe_system_dir(self, system: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", system.strip())
        return cleaned or "Unknown"

    def cache_key(self, identity: GameMetadataIdentity) -> str:
        raw = "|".join([
            identity.system.strip().lower(),
            identity.launcher.strip().replace(chr(92), "/").lower(),
            identity.rom.strip().replace(chr(92), "/").lower(),
        ])
        return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()

    def system_root(self, system: str) -> Path:
        path = self.root / self.safe_system_dir(system)
        (path / "games").mkdir(parents=True, exist_ok=True)
        (path / "box2d").mkdir(parents=True, exist_ok=True)
        return path

    def index_path(self, system: str) -> Path:
        return self.system_root(system) / "index.json"

    def load_index(self, system: str) -> dict:
        cache_name = self.safe_system_dir(system)
        if cache_name in self._index_cache:
            return self._index_cache[cache_name]
        path = self.index_path(system)
        if not path.exists():
            self._index_cache[cache_name] = {}
            return self._index_cache[cache_name]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._index_cache[cache_name] = data if isinstance(data, dict) else {}
        except Exception:
            self._index_cache[cache_name] = {}
        return self._index_cache[cache_name]

    def save_index(self, system: str, index: dict):
        cache_name = self.safe_system_dir(system)
        self._index_cache[cache_name] = index
        path = self.index_path(system)
        try:
            path.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def index_entry(self, identity: GameMetadataIdentity | None) -> dict | None:
        if identity is None:
            return None
        entry = self.load_index(identity.system).get(self.cache_key(identity))
        return entry if isinstance(entry, dict) else None

    def update_index(self, identity: GameMetadataIdentity, data: dict):
        key = self.cache_key(identity)
        index = self.load_index(identity.system)
        box_path = self.resolve_box2d_path(identity, str(data.get("box2d", "")))
        index[key] = {
            "cache_key": key,
            "file_base": str(data.get("file_base", self.safe_rom_asset_stem(identity))).strip(),
            "rom": identity.rom,
            "rom_filename": identity.rom_name,
            "scrape_name": str(data.get("scrape_name", "")).strip(),
            "has_metadata": True,
            "has_boxart": bool(box_path and box_path.exists()),
            "updated_at": int(data.get("updated_at", time.time())),
        }
        self.save_index(identity.system, index)

    def safe_rom_filename_stem(self, identity: GameMetadataIdentity) -> str:
        name = Path(identity.rom_name).stem or identity.rom_stem or self.cache_key(identity)
        cleaned = re.sub(r'[<>:"/\\|?*]+', "_", name).strip().rstrip(".")
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned or self.cache_key(identity)

    def duplicate_suffix(self, identity: GameMetadataIdentity) -> str:
        return self.cache_key(identity)[:6]

    def safe_rom_asset_stem(self, identity: GameMetadataIdentity) -> str:
        key = self.cache_key(identity)
        entry = self.index_entry(identity)
        if entry is not None:
            file_base = str(entry.get("file_base", "")).strip()
            if file_base:
                return file_base

        base = self.safe_rom_filename_stem(identity)
        suffixed = f"{base}__{self.duplicate_suffix(identity)}"
        games_dir = self.system_root(identity.system) / "games"
        clean_json = games_dir / f"{base}.json"
        suffixed_json = games_dir / f"{suffixed}.json"

        if suffixed_json.exists():
            return suffixed

        if clean_json.exists():
            try:
                existing = json.loads(clean_json.read_text(encoding="utf-8"))
                existing_key = str(existing.get("cache_key", "")).strip()
                if existing_key and existing_key != key:
                    return suffixed
            except Exception:
                return suffixed

        for other_key, other_entry in self.load_index(identity.system).items():
            if other_key == key or not isinstance(other_entry, dict):
                continue
            other_base = str(other_entry.get("file_base", "")).strip()
            other_rom_name = str(other_entry.get("rom_filename", "")).strip()
            if other_base == base or (other_rom_name and other_rom_name == identity.rom_name):
                return suffixed

        return base

    def json_path(self, identity: GameMetadataIdentity) -> Path:
        return self.system_root(identity.system) / "games" / f"{self.safe_rom_asset_stem(identity)}.json"

    def safe_artwork_filename(self, identity: GameMetadataIdentity) -> str:
        return self.safe_rom_asset_stem(identity)

    def box2d_path(self, identity: GameMetadataIdentity) -> Path:
        return self.system_root(identity.system) / "box2d" / f"{self.safe_artwork_filename(identity)}.png"

    def load(self, identity: GameMetadataIdentity | None) -> dict | None:
        if identity is None:
            return None
        key = self.cache_key(identity)
        if key in self._data_cache:
            return self._data_cache[key]
        if key in self._missing_cache:
            return None
        path = self.json_path(identity)
        if not path.exists():
            self._missing_cache.add(key)
            if len(self._missing_cache) > 512:
                self._missing_cache.pop()
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._data_cache[key] = data
                if len(self._data_cache) > 128:
                    self._data_cache.pop(next(iter(self._data_cache)))
                return data
            return None
        except Exception:
            return None

    def exists(self, identity: GameMetadataIdentity | None) -> bool:
        if identity is None:
            return False
        entry = self.index_entry(identity)
        if entry is not None:
            return bool(entry.get("has_metadata", True))
        return self.json_path(identity).exists()

    def resolve_box2d_path(self, identity: GameMetadataIdentity | None, box_value: str) -> Path | None:
        if identity is None:
            return None
        box = str(box_value or "").strip()
        if not box:
            return None
        path = Path(box)
        if path.is_absolute():
            return path
        system_root = self.system_root(identity.system)
        candidates = [
            (system_root / box).resolve(),
            (self.json_path(identity).parent / box).resolve(),
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return candidates[0]

    def is_complete(self, identity: GameMetadataIdentity | None) -> bool:
        data = self.load(identity)
        if not data:
            return False
        box_path = self.resolve_box2d_path(identity, str(data.get("box2d", "")))
        has_box = bool(box_path and box_path.exists())
        fields = ["scrape_name", "description", "year", "developer", "publisher", "genre", "players"]
        return has_box and all(str(data.get(field, "")).strip() for field in fields)

    def should_scrape(self, identity: GameMetadataIdentity, mode: str) -> bool:
        if mode == "All Games":
            return True
        if mode == "Unscraped Only":
            return not self.exists(identity)
        if mode == "New & Missing Data":
            return not self.is_complete(identity)
        return not self.exists(identity)

    def clean_cached_text(self, value, allow_numeric: bool = True) -> str:
        text = str(value or "").strip()
        if not allow_numeric and re.fullmatch(r"\d+", text):
            return ""
        return text

    def clean_players_text(self, value) -> str:
        if value is None:
            return ""
        if isinstance(value, dict):
            for key in ("text", "nom", "name", "joueurs", "players", "nbre_joueurs"):
                found = self.clean_players_text(value.get(key, ""))
                if found:
                    return found
            for item in value.values():
                found = self.clean_players_text(item)
                if found:
                    return found
            return ""
        if isinstance(value, list):
            for item in value:
                found = self.clean_players_text(item)
                if found:
                    return found
            return ""
        text = str(value or "").strip()
        if not text:
            return ""
        match = re.search(r"['\"]text['\"]\s*:\s*['\"]([^'\"]+)['\"]", text)
        if match:
            text = match.group(1).strip()
        text = re.sub(r"\s+", " ", text)
        return text

    def save(self, identity: GameMetadataIdentity, metadata: dict, box2d_bytes: bytes | None = None, manual_name: str = "") -> dict:
        key = self.cache_key(identity)
        json_path = self.json_path(identity)
        file_base = json_path.stem
        box_path = self.box2d_path(identity)
        existing = self.load(identity) or {}

        if box2d_bytes:
            box_path.write_bytes(box2d_bytes)
            rel_box = str(box_path.relative_to(self.system_root(identity.system))).replace(chr(92), "/")
        else:
            rel_box = str(existing.get("box2d", ""))

        saved = {
            "cache_key": key,
            "file_base": file_base,
            "source": "screenscraper",
            "system": identity.system,
            "launcher": identity.launcher,
            "rom": identity.rom,
            "rom_filename": identity.rom_name,
            "rom_stem": identity.rom_stem,
            "manual_scrape_name": manual_name or str(existing.get("manual_scrape_name", "")),
            "scrape_name": str(metadata.get("scrape_name", existing.get("scrape_name", identity.rom_stem))).strip(),
            "screenscraper_game_id": str(metadata.get("screenscraper_game_id", existing.get("screenscraper_game_id", ""))).strip(),
            "year": str(metadata.get("year", existing.get("year", ""))).strip(),
            "developer": self.clean_cached_text(metadata.get("developer", existing.get("developer", "")), allow_numeric=False),
            "publisher": self.clean_cached_text(metadata.get("publisher", existing.get("publisher", "")), allow_numeric=False),
            "genre": str(metadata.get("genre", existing.get("genre", ""))).strip(),
            "players": self.clean_players_text(metadata.get("players", existing.get("players", ""))),
            "description": str(metadata.get("description", existing.get("description", ""))).strip(),
            "box2d": rel_box,
            "updated_at": int(time.time()),
        }
        json_path.write_text(json.dumps(saved, indent=2, ensure_ascii=False), encoding="utf-8")
        self._missing_cache.discard(key)
        self._data_cache[key] = saved
        self.update_index(identity, saved)
        return saved


def region_from_option(option: str, identity: GameMetadataIdentity | None = None) -> str:
    option = str(option or "").strip()
    if option == "Prioritize USA":
        return "USA"
    if option == "Prioritize Europe":
        return "Europe"
    if option == "Prioritize Japan":
        return "Japan"
    return detect_region_from_game(identity)

def detect_region_from_game(identity: GameMetadataIdentity | None) -> str:
    if identity is None:
        return ""
    path = str(identity.rom or "").replace(chr(92), "/")
    parts = [p.lower() for p in Path(path).parts]
    filename = identity.rom_name.lower()

    folder_patterns = (
        ("USA", {"usa", "us", "u", "north america", "united states"}),
        ("Europe", {"europe", "eu", "eur", "e"}),
        ("Japan", {"japan", "jp", "jpn", "j"}),
    )
    for region, names in folder_patterns:
        for part in parts[:-1]:
            cleaned = re.sub(r"[^a-z0-9]+", " ", part).strip()
            tokens = set(cleaned.split()) | {cleaned}
            if tokens & names:
                return region

    filename_patterns = (
        ("USA", r"(?:^|[\s_\-\(\[\{,.])(?:usa|u|us)(?:$|[\s_\-\)\]\},.])"),
        ("Europe", r"(?:^|[\s_\-\(\[\{,.])(?:europe|eur|eu|e)(?:$|[\s_\-\)\]\},.])"),
        ("Japan", r"(?:^|[\s_\-\(\[\{,.])(?:japan|jpn|jp|j)(?:$|[\s_\-\)\]\},.])"),
    )
    for region, pattern in filename_patterns:
        if re.search(pattern, filename):
            return region
    return ""

class ScreenScraperClient:
    def __init__(self, username: str = "", password: str = "", min_delay_seconds: float = 2.0, quota_callback: Callable[[dict], None] | None = None):
        self.username = username.strip()
        self.password = password.strip()
        self.min_delay_seconds = min_delay_seconds
        self.last_request_at = 0.0
        self.quota_callback = quota_callback
        self.last_quota: dict = {}

    def developer_credentials_ready(self) -> bool:
        return bool(
            SCREENSCRAPER_DEV_ID
            and SCREENSCRAPER_DEV_PASSWORD
            and not SCREENSCRAPER_DEV_ID.startswith("__")
            and not SCREENSCRAPER_DEV_PASSWORD.startswith("__")
        )

    def user_credentials_ready(self) -> bool:
        return bool(self.username and self.password)

    def credentials_ready(self) -> bool:
        return self.developer_credentials_ready() and self.user_credentials_ready()

    def credentials_error(self) -> str:
        if not self.developer_credentials_ready():
            return "ScreenScraper developer credentials are not set."
        if not self.user_credentials_ready():
            return "Enter your ScreenScraper username and password first."
        return ""

    def wait_for_limit(self):
        elapsed = time.monotonic() - self.last_request_at
        delay = self.min_delay_seconds - elapsed
        if delay > 0:
            time.sleep(delay)
        self.last_request_at = time.monotonic()

    def base_params(self, include_language: bool = True) -> dict:
        params = {
            "devid": SCREENSCRAPER_DEV_ID,
            "devpassword": SCREENSCRAPER_DEV_PASSWORD,
            "softname": SCREENSCRAPER_SOFTNAME,
            "output": "json",
            "ssid": self.username,
            "sspassword": self.password,
        }
        if include_language:
            params["langue"] = SCREENSCRAPER_LANGUAGE
        return params

    def request_json(self, base_url: str, params: dict, timeout: int = 25, raise_on_daily_quota: bool = True) -> dict:
        url = f"{base_url}?{urllib.parse.urlencode(params, doseq=True, quote_via=urllib.parse.quote)}"
        self.wait_for_limit()
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": f"{SCREENSCRAPER_SOFTNAME}/1.0",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=get_ssl_context()) as response:
                status_code = int(getattr(response, "status", 200) or 200)
                payload = response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "ignore")[:500]
            detail = self.safe_error_text(body or str(exc))
            if _looks_like_login_error(body) or _looks_like_login_error(detail):
                raise RuntimeError("ScreenScraper login rejected. Check your username and password. ScreenScraper passwords should be alphanumeric.")
            if exc.code in {403, 429} or _looks_like_quota_error(detail):
                if _looks_like_daily_quota_error(detail):
                    raise ScreenScraperDailyQuotaError(build_screenscraper_daily_quota_message(detail))
                raise ScreenScraperQuotaError("ScreenScraper quota or rate limit reached. Please wait before scraping again.")
            if exc.code == 404:
                return {}
            raise RuntimeError(detail)
        except urllib.error.URLError as exc:
            raise RuntimeError(str(exc.reason or exc))

        if status_code in {403, 429}:
            raise ScreenScraperQuotaError("ScreenScraper quota or rate limit reached. Please wait before scraping again.")

        try:
            data = json.loads(payload.decode("utf-8-sig", "ignore"))
        except Exception:
            raise RuntimeError("ScreenScraper returned invalid data.")
        if not isinstance(data, dict):
            raise RuntimeError("ScreenScraper returned unexpected data.")

        if _contains_login_error(data):
            raise RuntimeError("ScreenScraper login rejected. Check your username and password. ScreenScraper passwords should be alphanumeric.")

        quota = self.parse_quota_info(data)
        if quota:
            self.last_quota = quota
            data["_gentleman_quota"] = quota
            if callable(self.quota_callback):
                try:
                    self.quota_callback(quota)
                except Exception:
                    pass
            if raise_on_daily_quota and _quota_info_indicates_daily_limit(quota):
                raise ScreenScraperDailyQuotaError(build_screenscraper_daily_quota_message(self.format_quota_info(quota)))

        self.raise_for_api_error(data)
        return data


    def quota_info(self) -> dict:
        credentials_error = self.credentials_error()
        if credentials_error:
            raise RuntimeError(credentials_error)

        try:
            data = self.request_json(SCREENSCRAPER_USER_URL, self.base_params(include_language=False), raise_on_daily_quota=False)
        except Exception as exc:
            if self.last_quota and self.quota_has_numbers(self.last_quota):
                return self.last_quota
            raise RuntimeError(str(exc) or "ScreenScraper quota could not be refreshed.")

        # ssuserInfos.php is the live account state. Prefer its user object so the menu shows
        # the account's current quota even when requests were made from another app.
        quota = _quota_from_known_screen_scraper_user_fields(data)
        if not self.quota_has_numbers(quota):
            quota = data.get("_gentleman_quota") or self.parse_quota_info(data)

        if self.quota_has_numbers(quota):
            self.last_quota = quota
            return quota

        debug_keys = _quota_debug_keys(data)
        detail = "ScreenScraper did not report readable user quota values."
        if debug_keys:
            detail += " Keys: " + ", ".join(debug_keys)
        raise RuntimeError(detail)

    def quota_has_numbers(self, quota: dict) -> bool:
        if not isinstance(quota, dict):
            return False
        return any(quota.get(key) is not None for key in ("used", "limit", "remaining", "minute_used", "minute_limit"))

    def format_quota_info(self, quota: dict) -> str:
        if not isinstance(quota, dict) or not quota:
            return ""
        used = quota.get("used")
        limit = quota.get("limit")
        remaining = quota.get("remaining")
        if used is not None and limit is not None:
            return f"User Quota: {int(used):,} / {int(limit):,}"
        if remaining is not None:
            return f"User Quota: {int(remaining):,} remaining"
        if used is not None:
            return f"User Quota: {int(used):,} used"
        return ""

    def parse_quota_info(self, data: dict) -> dict:
        if not isinstance(data, dict):
            return {}

        known_quota = _quota_from_known_screen_scraper_user_fields(data)

        pair_used, pair_limit = _first_quota_pair(data)

        used = _first_present_int(data, {
            "requeststoday", "requestsday", "request_today", "requests_today",
            "usedrequeststoday", "usedrequestsday", "usedtoday", "used_day",
            "apiusedtoday", "nbscrapetoday", "nbscrapeursjour", "nbrequeststoday",
            "userrequeststoday", "ssuserrequeststoday", "scrapestoday", "scrapetoday",
            "requeststodaynb", "nbrequestsday", "dailyused", "daily_used",
        })
        limit = _first_present_int(data, {
            "maxrequestsperday", "maxrequestsday", "requestslimitday", "requestsdaymax",
            "dailymax", "daily_limit", "maxday", "maxrequetesjour", "maxrequests",
            "maxrequeststoday", "maxscrapestoday", "maxscrapetoday", "maxrequestsdaily",
            "maxrequestsjour", "requeststodaymax", "maxnbscrapetoday", "dailymaxrequests",
        })
        remaining = _first_present_int(data, {
            "requestsremaining", "requestsremainingday", "remainingrequests",
            "remainingrequestsday", "requestsleft", "requestsleftday", "remainingday",
            "quota_remaining", "dayremaining", "remaining", "dailyremaining", "daily_remaining",
        })
        minute_used = _first_present_int(data, {
            "requestsminute", "requestsperminute", "usedrequestsminute",
            "used_minute", "minuterequests", "requestsinminute",
        })
        minute_limit = _first_present_int(data, {
            "maxrequestsperminute", "maxrequestsminute", "requestslimitminute",
            "minute_limit", "maxminute", "maxrequestsinminute",
        })
        username = _first_present_text(data, {"pseudo", "ssid", "username", "nom", "user"})

        if known_quota:
            if used is None:
                used = known_quota.get("used")
            if limit is None:
                limit = known_quota.get("limit")
            if remaining is None:
                remaining = known_quota.get("remaining")
            if not username:
                username = str(known_quota.get("username", "")).strip()

        if used is None and pair_used is not None:
            used = pair_used
        if limit is None and pair_limit is not None:
            limit = pair_limit

        if remaining is None and used is not None and limit is not None:
            remaining = max(0, limit - used)
        if used is None and limit is not None and remaining is not None:
            used = max(0, limit - remaining)
        if limit is None and used is not None:
            limit = 20000
        if limit is None and remaining is not None:
            limit = 20000
            used = max(0, limit - remaining)

        quota = {}
        if username:
            quota["username"] = username
        if used is not None:
            quota["used"] = used
        if limit is not None:
            quota["limit"] = limit
        if remaining is not None:
            quota["remaining"] = remaining
        if minute_used is not None:
            quota["minute_used"] = minute_used
        if minute_limit is not None:
            quota["minute_limit"] = minute_limit
        if quota:
            quota["updated_at"] = datetime.now().isoformat(timespec="seconds")
        return quota

    def scrape_game(self, system_id: int, rom_name: str, search_name: str = "", preferred_region: str = "", game_id: str = "") -> tuple[dict, bytes | None]:
        credentials_error = self.credentials_error()
        if credentials_error:
            raise RuntimeError(credentials_error)

        selected_game_id = str(game_id or "").strip()
        if selected_game_id:
            data = self.fetch_game_by_id(system_id, selected_game_id, preferred_region)
        else:
            query_name = search_name or rom_name
            data = None
            if preferred_region:
                data = self.scrape_game_by_region_search(system_id, query_name, preferred_region)

            if data is None:
                params = self.base_params()
                params.update({
                    "systemeid": str(system_id),
                    "romtype": "rom",
                    "romnom": query_name,
                })
                data = self.request_json(SCREENSCRAPER_BASE_URL, params)

        metadata, image_url = self.parse_game_response(data, preferred_region)
        image_bytes = self.download_image(image_url) if image_url else None
        return metadata, image_bytes

    def fetch_game_by_id(self, system_id: int, game_id: str, preferred_region: str = "") -> dict:
        params = self.base_params()
        params.update({
            "systemeid": str(system_id),
            "gameid": str(game_id),
        })
        return self.request_json(SCREENSCRAPER_BASE_URL, params)

    def search_game_suggestions(self, system_id: int, search_name: str, preferred_region: str = "", limit: int = 12) -> list[dict]:
        credentials_error = self.credentials_error()
        if credentials_error:
            raise RuntimeError(credentials_error)
        params = self.base_params()
        params.update({
            "systemeid": str(system_id),
            "recherche": search_name,
        })
        search_data = self.request_json(SCREENSCRAPER_SEARCH_URL, params)
        candidates = self.search_candidates(search_data)
        if preferred_region:
            candidates = sorted(candidates, key=lambda item: self.item_region_score(item, preferred_region), reverse=True)
        suggestions = []
        seen = set()
        for candidate in candidates:
            game_id = self.game_id_from_candidate(candidate)
            if not game_id or game_id in seen:
                continue
            seen.add(game_id)
            title = self.game_title_from_candidate(candidate, preferred_region) or search_name
            region = self.region_label_from_candidate(candidate, preferred_region)
            year = self.extract_year(self.first_named_value_region(candidate.get("dates", candidate.get("date", [])), ("text", "date", "date_debut"), preferred_region) or str(candidate.get("date", "")))
            label_parts = [title]
            details = []
            if region:
                details.append(region)
            if year:
                details.append(year)
            label = f"{title} ({', '.join(details)})" if details else title
            suggestions.append({
                "game_id": game_id,
                "title": title,
                "label": label,
                "region": region,
                "year": year,
            })
            if len(suggestions) >= limit:
                break
        return suggestions

    def game_title_from_candidate(self, candidate: dict, preferred_region: str = "") -> str:
        names = candidate.get("noms", candidate.get("nom", []))
        return self.first_named_value_region(names, ("text", "nom", "name"), preferred_region) or str(candidate.get("nom", candidate.get("name", ""))).strip()

    def region_label_from_candidate(self, candidate: dict, preferred_region: str = "") -> str:
        if preferred_region and self.item_region_score(candidate, preferred_region) > 0:
            return preferred_region
        for region in ("USA", "Europe", "Japan"):
            if self.item_region_score(candidate, region) > 0:
                return region
        return ""

    def scrape_game_by_region_search(self, system_id: int, search_name: str, preferred_region: str) -> dict | None:
        params = self.base_params()
        params.update({
            "systemeid": str(system_id),
            "recherche": search_name,
        })
        search_data = self.request_json(SCREENSCRAPER_SEARCH_URL, params)
        candidates = self.search_candidates(search_data)
        if not candidates:
            return None
        chosen = self.choose_region_candidate(candidates, preferred_region) or candidates[0]
        game_id = self.game_id_from_candidate(chosen)
        if not game_id:
            return {"response": {"jeu": chosen}}
        params = self.base_params()
        params.update({
            "systemeid": str(system_id),
            "gameid": str(game_id),
        })
        return self.request_json(SCREENSCRAPER_BASE_URL, params)

    def search_candidates(self, data: dict) -> list[dict]:
        response = data.get("response", data) if isinstance(data, dict) else {}
        games = []
        if isinstance(response, dict):
            games = response.get("jeux", response.get("jeu", []))
        if isinstance(games, dict):
            if all(isinstance(v, dict) for v in games.values()):
                return [v for v in games.values() if isinstance(v, dict)]
            return [games]
        if isinstance(games, list):
            return [g for g in games if isinstance(g, dict)]
        return []

    def game_id_from_candidate(self, game: dict) -> str:
        for key in ("id", "idjeu", "gameid", "jeuid"):
            value = str(game.get(key, "")).strip()
            if value:
                return value
        return ""

    def choose_region_candidate(self, candidates: list[dict], preferred_region: str) -> dict | None:
        if not candidates:
            return None
        scored = sorted(candidates, key=lambda item: self.item_region_score(item, preferred_region), reverse=True)
        return scored[0] if scored else None

    def raise_for_api_error(self, data: dict):
        if _contains_login_error(data):
            raise RuntimeError("ScreenScraper login rejected. Check your username and password. ScreenScraper passwords should be alphanumeric.")
        header = data.get("header") if isinstance(data, dict) else None
        if isinstance(header, dict):
            success = str(header.get("success", "")).lower().strip()
            message = str(header.get("error") or header.get("message") or header.get("APIErreur", "") or "").strip()
            if message and message.lower() in {"0", "false", "none", "null", "ok", "aucune"}:
                message = ""
            if success in {"false", "0", "ko"} or message:
                if _looks_like_quota_error(message):
                    if _looks_like_daily_quota_error(message):
                        raise ScreenScraperDailyQuotaError(build_screenscraper_daily_quota_message(self.safe_error_text(message)))
                    raise ScreenScraperQuotaError(self.safe_error_text(message))
                raise RuntimeError(self.safe_error_text(message or "ScreenScraper request failed."))

        response = data.get("response") if isinstance(data, dict) else None
        if isinstance(response, dict):
            message = str(response.get("erreur") or response.get("error") or "").strip()
            if message and message.lower() in {"0", "false", "none", "null", "ok", "aucune"}:
                message = ""
            if message:
                if _looks_like_quota_error(message):
                    if _looks_like_daily_quota_error(message):
                        raise ScreenScraperDailyQuotaError(build_screenscraper_daily_quota_message(self.safe_error_text(message)))
                    raise ScreenScraperQuotaError(self.safe_error_text(message))
                raise RuntimeError(self.safe_error_text(message))

    def safe_error_text(self, text: str) -> str:
        text = str(text or "")
        lower = text.lower()
        if "identifiant" in lower or "password" in lower or "auth" in lower or "invalid" in lower:
            return "ScreenScraper credentials were rejected."
        if "not found" in lower or "introuvable" in lower:
            return "Game not found."
        cleaned = re.sub(r"(devid|devpassword|ssid|sspassword)=[^&\s]+", r"\1=hidden", text)[:240]
        if _looks_like_quota_error(cleaned):
            return cleaned or "ScreenScraper quota or rate limit reached."
        return cleaned

    def is_numeric_id_value(self, value: str) -> bool:
        return bool(re.fullmatch(r"\d+", str(value or "").strip()))

    def clean_named_value(self, value: str) -> str:
        text = str(value or "").strip()
        return "" if self.is_numeric_id_value(text) else text

    def first_named_value(self, values, name_keys: tuple[str, ...]) -> str:
        if isinstance(values, str):
            return self.clean_named_value(values)
        if isinstance(values, list):
            for item in values:
                if isinstance(item, str):
                    found = self.clean_named_value(item)
                    if found:
                        return found
                    continue
                if not isinstance(item, dict):
                    continue
                for key in name_keys:
                    found = self.clean_named_value(item.get(key, ""))
                    if found:
                        return found
                for value in item.values():
                    if isinstance(value, dict):
                        found = self.first_named_value(value, name_keys)
                        if found:
                            return found
                    elif isinstance(value, list):
                        found = self.first_named_value(value, name_keys)
                        if found:
                            return found
        elif isinstance(values, dict):
            for key in name_keys:
                found = self.clean_named_value(values.get(key, ""))
                if found:
                    return found
            for key, value in values.items():
                if str(key).lower() in {"id", "idcompagnie", "idcompany", "idjoueur"}:
                    continue
                if isinstance(value, dict):
                    found = self.first_named_value(value, name_keys)
                    if found:
                        return found
                elif isinstance(value, list):
                    found = self.first_named_value(value, name_keys)
                    if found:
                        return found
                elif isinstance(value, str):
                    found = self.clean_named_value(value)
                    if found:
                        return found
        return ""

    def clean_players_text(self, value) -> str:
        if value is None:
            return ""
        if isinstance(value, dict):
            for key in ("text", "nom", "name", "joueurs", "players", "nbre_joueurs"):
                found = self.clean_players_text(value.get(key, ""))
                if found:
                    return found
            for item in value.values():
                found = self.clean_players_text(item)
                if found:
                    return found
            return ""
        if isinstance(value, list):
            for item in value:
                found = self.clean_players_text(item)
                if found:
                    return found
            return ""
        text = str(value or "").strip()
        if not text:
            return ""
        match = re.search(r"['\"]text['\"]\s*:\s*['\"]([^'\"]+)['\"]", text)
        if match:
            text = match.group(1).strip()
        text = re.sub(r"\s+", " ", text)
        return text

    def region_codes_for(self, preferred_region: str) -> list[str]:
        if not preferred_region:
            return []
        if preferred_region == "USA":
            return ["us", "usa", "ame"]
        if preferred_region == "Europe":
            return ["eu", "eur", "europe"]
        if preferred_region == "Japan":
            return ["jp", "jpn", "japan"]
        return [REGION_CODES.get(preferred_region, preferred_region).lower()]

    def fallback_region_codes(self, preferred_region: str) -> list[str]:
        preferred = set(self.region_codes_for(preferred_region))
        ordered = ["wor", "ss", "us", "eu", "jp", "fr", "en"]
        return [code for code in ordered if code not in preferred]

    def region_matches(self, value, preferred_region: str) -> bool:
        if not preferred_region:
            return False
        wanted = set(self.region_codes_for(preferred_region)) | {preferred_region.lower()}
        text = str(value or "").lower()
        return any(re.search(rf"(^|[^a-z0-9]){re.escape(token)}([^a-z0-9]|$)", text) or token in text for token in wanted)

    def item_region_score(self, item, preferred_region: str) -> int:
        if not preferred_region or not isinstance(item, dict):
            return 0
        score = 0
        direct_keys = ("region", "regions", "regionshortnames", "regionshortname", "romregions", "zone", "country", "pays", "parent")
        haystack = " ".join(str(item.get(key, "")) for key in direct_keys)
        if self.region_matches(haystack, preferred_region):
            score += 6
        for key in item.keys():
            key_text = str(key).lower()
            if self.region_matches(key_text, preferred_region):
                score += 3
        for value in item.values():
            if isinstance(value, str) and self.region_matches(value, preferred_region):
                score += 2
            elif isinstance(value, dict):
                score += max(0, self.item_region_score(value, preferred_region) - 1)
            elif isinstance(value, list):
                nested_scores = [self.item_region_score(v, preferred_region) for v in value if isinstance(v, dict)]
                if nested_scores:
                    score += max(nested_scores)
        return score

    def order_by_region(self, values, preferred_region: str):
        if not preferred_region or not isinstance(values, list):
            return values
        return sorted(values, key=lambda item: self.item_region_score(item, preferred_region), reverse=True)

    def first_region_key_value(self, values, base_names: tuple[str, ...], preferred_region: str, fallback_codes: tuple[str, ...] = ("wor", "ss", "en", "us", "eu", "jp", "fr")) -> str:
        if not isinstance(values, dict):
            return ""
        prefixes = [base.lower() for base in base_names]
        region_codes = self.region_codes_for(preferred_region) if preferred_region else []
        for code in region_codes:
            for key, value in values.items():
                key_l = str(key).lower()
                if any(key_l == f"{prefix}_{code}" or key_l.endswith(f"_{code}") and key_l.startswith(prefix) for prefix in prefixes):
                    found = self.clean_named_value(value)
                    if found:
                        return found
        for code in fallback_codes:
            if code in region_codes:
                continue
            for key, value in values.items():
                key_l = str(key).lower()
                if any(key_l == f"{prefix}_{code}" or key_l.endswith(f"_{code}") and key_l.startswith(prefix) for prefix in prefixes):
                    found = self.clean_named_value(value)
                    if found:
                        return found
        return ""

    def item_language_score(self, item, language: str = SCREENSCRAPER_LANGUAGE) -> int:
        if not language or not isinstance(item, dict):
            return 0
        language = language.lower()
        score = 0
        for key, value in item.items():
            key_l = str(key).lower()
            value_l = str(value).lower() if isinstance(value, str) else ""
            if key_l in {"langue", "language", "lang"} and value_l == language:
                score += 8
            if key_l.endswith(f"_{language}") or key_l == f"nom_{language}":
                score += 5
            if isinstance(value, dict):
                score += max(0, self.item_language_score(value, language) - 1)
            elif isinstance(value, list):
                nested_scores = [self.item_language_score(v, language) for v in value if isinstance(v, dict)]
                if nested_scores:
                    score += max(nested_scores)
        return score

    def order_by_language(self, values, language: str = SCREENSCRAPER_LANGUAGE):
        if not language or not isinstance(values, list):
            return values
        return sorted(values, key=lambda item: self.item_language_score(item, language) if isinstance(item, dict) else 0, reverse=True)

    def first_language_key_value(self, values, base_names: tuple[str, ...], language: str = SCREENSCRAPER_LANGUAGE) -> str:
        if not isinstance(values, dict):
            return ""
        language = language.lower()
        prefixes = [base.lower() for base in base_names]
        for key, value in values.items():
            key_l = str(key).lower()
            if any(key_l == f"{prefix}_{language}" or (key_l.endswith(f"_{language}") and key_l.startswith(prefix)) for prefix in prefixes):
                found = self.clean_named_value(value)
                if found:
                    return found
        return ""

    def first_named_value_language(self, values, name_keys: tuple[str, ...], language: str = SCREENSCRAPER_LANGUAGE) -> str:
        if isinstance(values, dict):
            language_value = self.first_language_key_value(values, name_keys, language)
            if language_value:
                return language_value
        ordered = self.order_by_language(values, language)
        found = self.first_named_value(ordered, name_keys)
        if found:
            return found
        return ""

    def first_named_value_region(self, values, name_keys: tuple[str, ...], preferred_region: str) -> str:
        language_value = self.first_named_value_language(values, name_keys, SCREENSCRAPER_LANGUAGE)
        if language_value:
            return language_value
        if isinstance(values, dict):
            region_value = self.first_region_key_value(values, name_keys, preferred_region)
            if region_value:
                return region_value
        return self.first_named_value(self.order_by_region(values, preferred_region), name_keys)

    def media_url_from_region_keys(self, values, preferred_region: str) -> str:
        if not isinstance(values, dict):
            return ""
        region_codes = self.region_codes_for(preferred_region) if preferred_region else []
        fallback_codes = self.fallback_region_codes(preferred_region)
        key_items = [(str(k).lower(), v) for k, v in values.items()]
        def match_codes(codes: list[str]) -> str:
            for code in codes:
                for key, value in key_items:
                    if not isinstance(value, str) or not value.startswith("http"):
                        continue
                    if ("boitier_2d" in key or "box2d" in key or "box_2d" in key or "box-2d" in key) and (key.endswith(f"_{code}") or f"_{code}" in key):
                        return value.strip()
            return ""
        return match_codes(region_codes) or match_codes(fallback_codes)

    def collect_media_dicts(self, value) -> list[dict]:
        found = []
        if isinstance(value, dict):
            found.append(value)
            for child in value.values():
                found.extend(self.collect_media_dicts(child))
        elif isinstance(value, list):
            for child in value:
                found.extend(self.collect_media_dicts(child))
        return found

    def parse_game_response(self, data: dict, preferred_region: str = "") -> tuple[dict, str]:
        response = data.get("response", data)
        game = response.get("jeu", response) if isinstance(response, dict) else {}
        if not isinstance(game, dict):
            raise RuntimeError("Game not found.")

        names = game.get("noms", game.get("nom", []))
        synopsis = game.get("synopsis", [])
        genres = game.get("genres", [])
        devs = game.get("developpeur", game.get("developpeurs", []))
        pubs = game.get("editeur", game.get("editeurs", []))
        players = game.get("joueurs", game.get("nbre_joueurs", ""))
        dates = game.get("dates", game.get("date", []))
        medias = game.get("medias", [])

        scrape_name = self.first_named_value_region(names, ("text", "nom", "name"), preferred_region) or str(game.get("nom", "")).strip()
        description = self.first_named_value_region(synopsis, ("text", "synopsis", "description"), preferred_region)
        genre = self.first_named_value_region(genres, ("text", "nom", "name"), preferred_region)
        developer = self.first_named_value_language(devs, ("text", "nom", "name")) or (str(devs).strip() if isinstance(devs, str) else "")
        publisher = self.first_named_value_language(pubs, ("text", "nom", "name")) or (str(pubs).strip() if isinstance(pubs, str) else "")
        year = self.extract_year(self.first_named_value_region(dates, ("text", "date", "date_debut"), preferred_region) or str(game.get("date", "")))
        player_text = self.clean_players_text(players)
        image_url = self.find_box2d_url(medias, preferred_region)

        return {
            "scrape_name": scrape_name,
            "screenscraper_game_id": str(game.get("id", game.get("idjeu", ""))).strip(),
            "year": year,
            "developer": developer,
            "publisher": publisher,
            "genre": genre,
            "players": player_text,
            "description": description,
        }, image_url

    def extract_year(self, text: str) -> str:
        match = re.search(r"(?:19|20)\d{2}", text or "")
        return match.group(0) if match else ""

    def find_box2d_url(self, medias, preferred_region: str = "") -> str:
        for media in self.collect_media_dicts(medias):
            url = self.media_url_from_region_keys(media, preferred_region)
            if url:
                return url

        candidates = []
        if isinstance(medias, list):
            candidates = [m for m in medias if isinstance(m, dict)]
        elif isinstance(medias, dict):
            candidates = self.collect_media_dicts(medias)
        ordered = self.order_by_region(candidates, preferred_region) if preferred_region else candidates
        for media in ordered:
            text = " ".join(str(media.get(k, "")).lower() for k in ("type", "media_type", "format", "parent", "nomcourt", "nom"))
            url = str(media.get("url", media.get("media", media.get("download", "")))).strip()
            if url and ("box-2d" in text or "box2d" in text or "boitier_2d" in text or "2d" in text):
                return url
        for media in candidates:
            for key, value in media.items():
                key_l = str(key).lower()
                if isinstance(value, str) and value.startswith("http") and ("boitier_2d" in key_l or "box-2d" in key_l or "box2d" in key_l or "box_2d" in key_l):
                    return value.strip()
        return ""

    def download_image(self, url: str) -> bytes | None:
        if not url:
            return None
        self.wait_for_limit()
        request = urllib.request.Request(url, headers={"User-Agent": SCREENSCRAPER_SOFTNAME})
        try:
            with urllib.request.urlopen(request, timeout=30, context=get_ssl_context()) as response:
                return response.read()
        except Exception:
            return None

def cleaned_scrape_name(name: str) -> str:
    stem = Path(name).stem
    stem = re.sub(r"[_]+", " ", stem)
    stem = re.sub(r"\s*\([^)]*\)", "", stem)
    stem = re.sub(r"\s*\[[^]]*\]", "", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    return stem or Path(name).stem or name
