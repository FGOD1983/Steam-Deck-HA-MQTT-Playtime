#!/home/deck/mqtt-env/bin/python
import subprocess
import paho.mqtt.client as mqtt
import sys
import os
import re
import glob
import datetime
import time
import ssl
import json
import math
import requests
import psutil
import vdf
from urllib.parse import quote

# ===========================
# Configuration
# ===========================

MQTT_HOST = "YOUR_HA_RUNNING_MQTT"
MQTT_PORT = 8883
MQTT_USER = "YOUR_MQTT_USER_FOR_HA"
MQTT_PASS = "YOUR_MQTT_PASSWORD_FOR_HA"
BASE_TOPIC = "steamdeck"
CACHE_PATH = "/home/deck/scripts/game_cache.json"
TRACE_LOG_PATH = "/home/deck/scripts/game_trace.log"
STEAM_APPS_PATH = "/home/deck/.steam/steam/steamapps"
QUEUE_PATH       = "/home/deck/scripts/playtime_queue.json"
STEAM_USER_PATH  = os.path.expanduser("~/.local/share/Steam/userdata")

os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)

# ===========================
# Helpers
# ===========================

def print_log(message):
    timestamp = datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
    print(f"{timestamp} [DEBUG] {message}")

def write_trace(game_name, game_type, cpu, status="Detection"):
    try:
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(TRACE_LOG_PATH, "a") as f:
            f.write(f"{timestamp} | {status} | Found: {game_name} | Type: {game_type} | CPU: {cpu}%\n")
        if os.path.exists(TRACE_LOG_PATH):
            with open(TRACE_LOG_PATH, "r") as f:
                lines = f.readlines()
            if len(lines) > 100:
                with open(TRACE_LOG_PATH, "w") as f:
                    f.writelines(lines[-100:])
    except:
        pass

def get_output(cmd):
    try:
        return subprocess.check_output(cmd, shell=True, stderr=subprocess.PIPE).decode("utf-8").strip()
    except:
        return ""

# ===========================
# localconfig.vdf helpers
# ===========================

def find_steam_user_id():
    """Return the first numeric Steam userdata directory found."""
    try:
        ids = [d for d in os.listdir(STEAM_USER_PATH)
               if os.path.isdir(os.path.join(STEAM_USER_PATH, d)) and d.isdigit()]
        return ids[0] if ids else None
    except:
        return None

def get_localconfig_playtime(appid):
    """
    Read the current total Playtime (minutes) for a given appid from localconfig.vdf.
    appid can be a string in any format — we try signed int32 str (non-Steam)
    and plain numeric str (Steam Native).
    Returns integer minutes, or 0 if not found.
    """
    uid = find_steam_user_id()
    if not uid:
        return 0
    lc_path = os.path.join(STEAM_USER_PATH, uid, "config", "localconfig.vdf")
    if not os.path.exists(lc_path):
        return 0
    try:
        with open(lc_path, "r", encoding="utf-8") as f:
            lc = vdf.loads(f.read())
        apps = (lc["UserLocalConfigStore"]
                   ["Software"]
                   ["Valve"]
                   ["Steam"]
                   ["apps"])
        # Try the appid as-is first, then as signed int32 string
        entry = apps.get(str(appid))
        if not entry and appid:
            try:
                signed = str(int(appid) if int(appid) < 2**31 else int(appid) - 2**32)
                entry = apps.get(signed)
            except:
                pass
        if entry:
            return int(entry.get("Playtime", 0))
    except Exception as e:
        print_log(f"localconfig.vdf read error: {e}")
    return 0

# ===========================
# Playtime Queue
# ===========================

def load_queue():
    """Load playtime_queue.json, return dict with 'active_sessions' list."""
    if not os.path.exists(QUEUE_PATH):
        return {"active_sessions": []}
    try:
        with open(QUEUE_PATH, "r") as f:
            data = json.load(f)
        if "active_sessions" not in data:
            data["active_sessions"] = []
        return data
    except Exception as e:
        print_log(f"Queue load error: {e}")
        return {"active_sessions": []}

def save_queue(queue):
    """Atomically write playtime_queue.json."""
    tmp = QUEUE_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(queue, f, indent=2)
        os.replace(tmp, QUEUE_PATH)
    except Exception as e:
        print_log(f"Queue save error: {e}")

def get_open_session(queue):
    """Return the first session with game_state == 'opened', or None."""
    for s in queue["active_sessions"]:
        if s.get("game_state") == "opened":
            return s
    return None

def open_session(queue, name, appid):
    """Add a new opened session to the queue."""
    now         = int(time.time())
    playtime    = get_localconfig_playtime(appid) if appid else 0
    session = {
        "session_id":     str(now),
        "appid":          appid or "",
        "name":           name,
        "game_state":     "opened",
        "start_playtime": playtime,
        "end_playtime":   None,
        "start_time":     now,
        "end_time":       None,
    }
    queue["active_sessions"].append(session)
    save_queue(queue)
    print_log(f"Queue: opened session for '{name}' (appid={appid}, start_playtime={playtime}m)")
    return session

def close_session(queue, session):
    """Mark a session as closed, read end_playtime from localconfig.vdf."""
    now      = int(time.time())
    playtime = get_localconfig_playtime(session.get("appid")) if session.get("appid") else 0
    session["game_state"]   = "closed"
    session["end_time"]     = now
    session["end_playtime"] = playtime
    save_queue(queue)
    print_log(
        f"Queue: closed session '{session['name']}' "
        f"(end_playtime={playtime}m, duration={now - session['start_time']}s)"
    )

def remove_session(queue, session_id):
    """Remove a session from the queue by session_id."""
    before = len(queue["active_sessions"])
    queue["active_sessions"] = [
        s for s in queue["active_sessions"]
        if s["session_id"] != session_id
    ]
    after = len(queue["active_sessions"])
    if before != after:
        save_queue(queue)
        print_log(f"Queue: removed session {session_id}")

def update_queue_for_game(queue, detected_game, detected_appid):
    """
    Compare detected game against the current open session.
    - No game → close open session if any
    - Same game as open session → no change
    - Different game → close open session, open new one
    - New game, nothing open → open new session
    """
    no_game     = (detected_game == "No game opened")
    open_sess   = get_open_session(queue)

    if no_game:
        if open_sess:
            print_log(f"Queue: no game detected, closing '{open_sess['name']}'")
            close_session(queue, open_sess)
        return

    # Game is running — check if it matches the open session
    if open_sess:
        if open_sess["name"] == detected_game:
            # Same game still running — nothing to do
            return
        else:
            # Game swap — close current, open new
            print_log(f"Queue: game swap '{open_sess['name']}' → '{detected_game}'")
            close_session(queue, open_sess)

    # Open a new session
    open_session(queue, detected_game, detected_appid)

# ===========================
# ACF Manifest Cache (Steam native games)
# ===========================

def build_acf_cache():
    acf_cache = {}
    search_paths = [
        STEAM_APPS_PATH,
        "/run/media/mmcblk0p1/steamapps",
        "/run/media/deck/steamapps",
    ]
    for base in search_paths:
        if not os.path.isdir(base):
            continue
        for fname in os.listdir(base):
            if not fname.endswith(".acf"):
                continue
            try:
                with open(os.path.join(base, fname), "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                appid_m  = re.search(r'"appid"\s+"(\d+)"', content)
                name_m   = re.search(r'"name"\s+"([^"]+)"', content)
                folder_m = re.search(r'"installdir"\s+"([^"]+)"', content)
                if appid_m and name_m:
                    appid = appid_m.group(1)
                    name  = name_m.group(1)
                    acf_cache[appid] = name
                    if folder_m:
                        acf_cache[folder_m.group(1).lower()] = name
            except:
                pass
    print_log(f"ACF cache built: {len(acf_cache)} entries")
    return acf_cache

# ===========================
# Shortcuts VDF Cache (non-Steam shortcuts)
# ===========================

def parse_shortcuts_vdf(vdf_path):
    shortcuts = {}
    try:
        with open(vdf_path, "rb") as f:
            data = f.read()

        i = 0
        current_runtime_id = None

        while i < len(data):
            if data[i] == 0x02:
                j = i + 1
                while j < len(data) and data[j] != 0x00:
                    j += 1
                field_name = data[i+1:j].decode("utf-8", errors="replace").lower()
                if field_name == "appid" and j + 5 <= len(data):
                    raw = int.from_bytes(data[j+1:j+5], "little")
                    current_runtime_id = str(raw | 0x80000000)
                i = j + 5 if field_name == "appid" else j + 1
                continue

            if data[i] == 0x01:
                j = i + 1
                while j < len(data) and data[j] != 0x00:
                    j += 1
                field_name = data[i+1:j].decode("utf-8", errors="replace").lower()
                k = j + 1
                while k < len(data) and data[k] != 0x00:
                    k += 1
                value = data[j+1:k].decode("utf-8", errors="replace")

                if field_name == "appname" and value.strip():
                    name = value.strip()
                    if current_runtime_id:
                        shortcuts[current_runtime_id] = name
                    shortcuts[name.lower()] = name
                    print_log(f"Shortcut: '{name}' → runtime id {current_runtime_id}")

                i = k + 1
                continue

            i += 1

    except Exception as e:
        print_log(f"shortcuts.vdf parse error ({vdf_path}): {e}")

    return shortcuts

def build_shortcuts_cache():
    combined = {}
    for vdf_path in glob.glob(os.path.expanduser("~/.steam/steam/userdata/*/config/shortcuts.vdf")):
        print_log(f"Reading shortcuts: {vdf_path}")
        combined.update(parse_shortcuts_vdf(vdf_path))
    print_log(f"Shortcuts cache: {len(combined)} entries")
    return combined

# Build caches once at startup
ACF_CACHE       = build_acf_cache()
SHORTCUTS_CACHE = build_shortcuts_cache()

EMULATOR_SUFFIXES = re.compile(
    r'\s*\((?:Ryujinx|Yuzu|RPCS3|PCSX2|Dolphin|Citra|mGBA|melonDS|DuckStation|'
    r'PPSSPP|Xemu|Xenia|MAME|RetroArch|Cemu|Lime3DS|Sudachi|Citron|'
    r'ScummVM|DOSBox|Flycast|VICE|Redream|BigPEmu|azahar)\)',
    re.IGNORECASE
)

def strip_emulator_suffix(name):
    if not name:
        return name
    name = EMULATOR_SUFFIXES.sub('', name)
    name = re.sub(r'\s*[\[\(].*?[\]\)]\s*$', '', name)
    name = re.sub(r'[\s\(\)\[\]]+$', '', name)
    return name.strip()

# ===========================
# AppID helpers
# ===========================

def get_steam_appid_from_env(pid):
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            env = f.read().decode("utf-8", errors="replace")
        for var in env.split("\x00"):
            if var.startswith("SteamGameId="):
                val = var.split("=", 1)[1].strip()
                if val.isdigit():
                    return val
            if var.startswith("SteamAppId="):
                val = var.split("=", 1)[1].strip()
                if val.isdigit() and val != "0":
                    return val
    except:
        pass
    return None

def get_steam_appid_from_cmdline(cmdline_str):
    match = re.search(r'SteamGameId[=\s]+(\d{4,})', cmdline_str)
    if match:
        return match.group(1)
    match = re.search(r'(?:^|\s)-?(?:AppId|appid|gameid)[=\s]+(\d{4,})', cmdline_str, re.IGNORECASE)
    if match:
        return match.group(1)
    return None

def is_steam_native_appid(appid):
    if not appid:
        return False
    try:
        return int(appid) < 0x80000000
    except:
        return False

# ===========================
# Online Lookup
# ===========================

def _similarity(a, b):
    sa = set(a.lower().split())
    sb = set(b.lower().split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / max(len(sa), len(sb))

def lookup_steam_name_by_appid(appid):
    try:
        url = f"https://store.steampowered.com/api/appdetails?appids={appid}&filters=basic"
        r = requests.get(url, timeout=6)
        data = r.json().get(str(appid), {})
        if data.get("success"):
            return data.get("data", {}).get("name")
    except:
        pass
    return None

def lookup_steam_search(search_term):
    try:
        url = f"https://store.steampowered.com/api/storesearch/?term={quote(search_term)}&l=english&cc=US"
        r = requests.get(url, timeout=6)
        items = r.json().get("items", [])
        if not items:
            return None
        term_lower = search_term.lower()
        for item in items:
            if item["name"].lower() == term_lower:
                return item["name"]
        for item in items:
            if item["name"].lower().startswith(term_lower):
                return item["name"]
        for item in items:
            if term_lower in item["name"].lower():
                return item["name"]
        if _similarity(term_lower, items[0]["name"].lower()) > 0.6:
            return items[0]["name"]
    except:
        pass
    return None

def lookup_rawg(search_term):
    try:
        url = f"https://api.rawg.io/api/games?search={quote(search_term)}&page_size=5"
        r = requests.get(url, timeout=6)
        results = r.json().get("results", [])
        term_lower = search_term.lower()
        for game in results:
            if game["name"].lower() == term_lower:
                return game["name"]
        for game in results:
            if _similarity(term_lower, game["name"].lower()) > 0.65:
                return game["name"]
    except:
        pass
    return None

def clean_raw_name(raw_name):
    name = raw_name
    name = re.sub(
        r'[-_.]?(x64|x86|win64|win32|linux|linux64|dx11|dx12|vk|vulkan|'
        r'retail|gold|goty|remaster|enhanced|definitive|complete)$',
        '', name, flags=re.IGNORECASE
    )
    name = re.sub(r'\s*\(\d{4}\)\s*$', '', name)
    name = re.sub(r'([a-z])([A-Z])', r'\1 \2', name)
    name = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', name)
    name = name.replace("_", " ").replace("-", " ").replace(".", " ")
    return " ".join(name.split()).strip()

# ===========================
# Game Title Resolver
# ===========================

def resolve_game_title(raw_name, appid=None):
    cache_data = {}
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, "r") as f:
                content = f.read().strip()
                if content:
                    cache_data = json.loads(content)
        except Exception as e:
            print_log(f"Cache read error: {e}")
            if os.path.exists(CACHE_PATH + ".bak"):
                try:
                    with open(CACHE_PATH + ".bak", "r") as f:
                        cache_data = json.load(f)
                except:
                    pass

    for key in ([appid, raw_name] if appid else [raw_name]):
        if key and key in cache_data:
            return cache_data[key]

    print_log(f"Resolving: '{raw_name}' (appid={appid})")
    final_name = None

    if appid and is_steam_native_appid(appid):
        final_name = lookup_steam_name_by_appid(appid)
        if final_name:
            print_log(f"→ Steam API by appid: {final_name}")

    if not final_name and appid and appid in ACF_CACHE:
        final_name = ACF_CACHE[appid]
        print_log(f"→ ACF by appid: {final_name}")

    if not final_name and raw_name.lower() in ACF_CACHE:
        final_name = ACF_CACHE[raw_name.lower()]
        print_log(f"→ ACF by folder: {final_name}")

    if not final_name:
        shortcut_hit = (SHORTCUTS_CACHE.get(appid) if appid else None) or SHORTCUTS_CACHE.get(raw_name.lower())
        if shortcut_hit:
            final_name = shortcut_hit
            print_log(f"→ shortcuts.vdf: {final_name}")

    if not final_name:
        search_term = clean_raw_name(raw_name)
        final_name = lookup_steam_search(search_term)
        if final_name:
            print_log(f"→ Steam search: {final_name}")

    if not final_name:
        search_term = clean_raw_name(raw_name)
        final_name = lookup_rawg(search_term)
        if final_name:
            print_log(f"→ RAWG: {final_name}")

    if not final_name:
        final_name = clean_raw_name(raw_name).title()
        print_log(f"→ Fallback: {final_name}")

    for key in ([appid, raw_name] if appid else [raw_name]):
        if key:
            cache_data[key] = final_name

    tmp_path = CACHE_PATH + ".tmp"
    bak_path = CACHE_PATH + ".bak"
    try:
        if os.path.exists(CACHE_PATH):
            subprocess.call(f"cp {CACHE_PATH} {bak_path}", shell=True)
        with open(tmp_path, "w") as f:
            json.dump(cache_data, f, indent=4)
        os.replace(tmp_path, CACHE_PATH)
    except Exception as e:
        print_log(f"Cache write error: {e}")

    return final_name

# ===========================
# Game Detection
# ===========================

def detect_game():
    possible_matches = []
    is_desktop_mode = not get_output("ps -A | grep gamescope")

    ignore_list = [
        "steam.exe", "services.exe", "explorer.exe", "winedevice.exe",
        "system32", "proton", "experimental", "pressure-vessel",
        "epicgameslauncher", "monitoring", "bmlauncher",
        "setup.exe", "install.exe", "steamwebhelper",
        "overlay", "social", "webhelper", "crashreporter", "eosoverlay",
        "ea desktop", "eadesktop", "destager", "origin", "uplay",
        "iscriptevaluator", "legacycompat", "vivox", "easyanticheat",
        "python", "bash", "/bin/sh", "systemd", "dbus", "pipewire",
        "gamescope", "xdg-", "kwin", "plasmashell",
        "exogui",
    ]

    tech_folders = {
        "binaries", "win64", "win32", "win32s", "shipping", "pfx",
        "drive_c", "core", "common", "steamapps", "dist", "scripts", "bin",
        "ea desktop", "origin", "launcher", "system", "oakgame", "engine",
        "bundled", "plugins", "redist", "vcredist", "directx", "support",
        "prerequisites", "_commonredist", "dotnet", "physx",
    }

    skip_folders = {
        "windows", "games", "deck", "home", "users", "usr", "bin",
        "local", "share", "run", "media", "mnt", "opt", "lib",
        "lib64", "proc", "sys", "dev", "tmp", "var", "etc",
    }

    for proc in psutil.process_iter(['pid', 'cmdline', 'cpu_percent', 'create_time']):
        try:
            cmdline = proc.info.get('cmdline')
            if not cmdline:
                continue

            full_cmd       = " ".join(cmdline)
            full_cmd_lower = full_cmd.lower()
            pid            = proc.info.get('pid')

            reaper_match = re.search(r'reaper.*AppId=(\d+)', full_cmd, re.IGNORECASE)
            if reaper_match:
                appid = reaper_match.group(1)
                if appid in SHORTCUTS_CACHE:
                    shortcut_name = SHORTCUTS_CACHE[appid].lower()
                    if "exogui" not in shortcut_name and "exodos" not in shortcut_name:
                        cache_data = {}
                        if os.path.exists(CACHE_PATH):
                            try:
                                with open(CACHE_PATH, 'r') as f:
                                    content_cache = f.read().strip()
                                    if content_cache:
                                        cache_data = json.loads(content_cache)
                            except Exception as e:
                                print_log(f"Cache read error (Reaper override): {e}")
                        raw_title = cache_data.get(appid) or SHORTCUTS_CACHE[appid]
                        title = strip_emulator_suffix(raw_title)
                        possible_matches.append({
                            'title':     title,
                            'appid':     appid,
                            'game_type': "Non-Steam",
                            'resolved':  True,
                            'cpu':       proc.info['cpu_percent'],
                            'time':      proc.info['create_time'],
                        })
                continue

            exo_match = re.search(
                r'/eXo[^/]*/(?:[^/]+/)*([^/]+)\.(?:command|bsh)',
                full_cmd, re.IGNORECASE
            )
            if exo_match:
                raw_title = exo_match.group(1)
                if "exogui" not in raw_title.lower():
                    title = re.sub(r'\s*\(\d{4}\)\s*', ' ', raw_title).strip()
                    possible_matches.append({
                        'title':     title,
                        'appid':     None,
                        'game_type': "ExoDOS",
                        'resolved':  False,
                        'cpu':       proc.info['cpu_percent'],
                        'time':      proc.info['create_time'],
                    })
                continue

            if any(x in full_cmd_lower for x in ignore_list):
                continue

            appid = get_steam_appid_from_env(pid) or get_steam_appid_from_cmdline(full_cmd)

            if is_desktop_mode:
                is_known_steam = appid and (appid in ACF_CACHE or appid in SHORTCUTS_CACHE)
                if not is_known_steam:
                    if not re.search(r'/roms/', full_cmd_lower):
                        continue

            if appid and appid in ACF_CACHE and is_steam_native_appid(appid):
                cache_data = {}
                if os.path.exists(CACHE_PATH):
                    try:
                        with open(CACHE_PATH, 'r') as f:
                            content_cache = f.read().strip()
                            if content_cache:
                                cache_data = json.loads(content_cache)
                    except Exception as e:
                        print_log(f"Cache read error (Steam Native override): {e}")
                title = cache_data.get(appid) or ACF_CACHE[appid]
                possible_matches.append({
                    'title':     title,
                    'appid':     appid,
                    'game_type': "Steam Native",
                    'resolved':  True,
                    'cpu':       proc.info['cpu_percent'],
                    'time':      proc.info['create_time'],
                })
                continue

            if appid and appid in SHORTCUTS_CACHE:
                cache_data = {}
                if os.path.exists(CACHE_PATH):
                    try:
                        with open(CACHE_PATH, 'r') as f:
                            content_cache = f.read().strip()
                            if content_cache:
                                cache_data = json.loads(content_cache)
                    except Exception as e:
                        print_log(f"Cache read error (Non-Steam override): {e}")
                raw_title = cache_data.get(appid) or SHORTCUTS_CACHE[appid]
                title = strip_emulator_suffix(raw_title)
                possible_matches.append({
                    'title':     title,
                    'appid':     appid,
                    'game_type': "Non-Steam",
                    'resolved':  True,
                    'cpu':       proc.info['cpu_percent'],
                    'time':      proc.info['create_time'],
                })
                continue

            rom_ext = (
                r'iso|gcm|rvz|zip|7z|cue|bin|elf|nsp|xci|wua|'
                r'nes|sfc|smc|n64|gba|gbc|gb|nds|z64|v64|chd|pbp|cso'
            )
            rom_match = re.search(rf'/roms/[^/]+/([^/]+)\.(?:{rom_ext})', full_cmd, re.IGNORECASE)
            if rom_match:
                raw_rom   = rom_match.group(1)
                clean_rom = re.sub(r'[\(\[][^\]\)]*[\]\)]', '', raw_rom).strip()
                clean_rom = re.sub(r'\s+', ' ', clean_rom).strip()
                possible_matches.append({
                    'title':     clean_rom,
                    'appid':     None,
                    'game_type': "ROM",
                    'resolved':  False,
                    'cpu':       proc.info['cpu_percent'],
                    'time':      proc.info['create_time'],
                })
                continue

            if not is_desktop_mode:
                is_steam_or_exe = "steamapps/common" in full_cmd_lower or ".exe" in full_cmd_lower
                is_linux_native = not is_steam_or_exe and any(
                    p in full_cmd_lower for p in ["/games/", "/applications/", "/run/media/"]
                )

                if (is_steam_or_exe or is_linux_native):
                    path_match = re.search(
                        r'(?:/home/[^/\s]+|/run/media/[^/\s]+/[^/\s]+|/mnt/[^/\s]+)'
                        r'(?:/[^/\s\'"]+)+',
                        full_cmd
                    )
                    if path_match:
                        full_path   = path_match.group(0).replace("\\", "/")
                        parts       = [p for p in full_path.split("/") if p]
                        parts_lower = [p.lower() for p in parts]
                        game_folder = None

                        if "common" in parts_lower:
                            try:
                                common_idx = parts_lower.index("common")
                                if common_idx + 1 < len(parts):
                                    candidate = parts[common_idx + 1]
                                    if candidate.lower() not in tech_folders:
                                        acf_hit     = ACF_CACHE.get(candidate.lower())
                                        game_folder = acf_hit if acf_hit else candidate
                            except ValueError:
                                pass

                        if not game_folder:
                            for i in range(len(parts) - 2, -1, -1):
                                folder  = parts[i]
                                f_lower = folder.lower()
                                if (f_lower not in tech_folders
                                        and f_lower not in skip_folders
                                        and not folder.startswith(".")
                                        and len(folder) > 2
                                        and not re.match(r'^v?\d[\d.]+$', folder)
                                        and "launcher" not in f_lower
                                        and "desktop"  not in f_lower):
                                    game_folder = folder
                                    break

                        if game_folder and not game_folder.startswith("."):
                            acf_hit   = ACF_CACHE.get(game_folder.lower())
                            game_type = "Steam Native" if (acf_hit and is_steam_native_appid(appid)) else "Non-Steam"
                            possible_matches.append({
                                'title':     acf_hit if acf_hit else game_folder,
                                'appid':     appid,
                                'game_type': game_type,
                                'resolved':  bool(acf_hit),
                                'cpu':       proc.info['cpu_percent'],
                                'time':      proc.info['create_time'],
                            })

        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not possible_matches:
        return "No game opened", None, "None"

    best = sorted(
        possible_matches,
        key=lambda x: (x.get('resolved', False), x['cpu'], x['time']),
        reverse=True
    )[0]

    write_trace(best['title'], best['game_type'], best['cpu'])

    if best.get('resolved'):
        return best['title'], best.get('appid'), best['game_type']

    resolved_title = resolve_game_title(best['title'], appid=best.get('appid'))
    return resolved_title, best.get('appid'), best['game_type']

# ===========================
# Network
# ===========================

def is_network_online():
    output = get_output("nmcli -t -f DEVICE,TYPE,STATE,CONNECTION dev status")
    return any(
        "connected" in line and ("wifi" in line or "ethernet" in line)
        for line in output.splitlines()
    )

# ===========================
# MQTT ACK handling
# ===========================

def process_acks(client, queue):
    """
    Read retained ACK messages from steamdeck/playtime/ack/#.
    For each ACK:
      - Remove the session from the local queue
      - Clear the retained ACK topic on MQTT
    Returns the updated queue.
    """
    acked_session_ids = []

    def on_message(c, userdata, msg):
        topic   = msg.topic
        payload = msg.payload.decode("utf-8", errors="replace").strip()
        if not payload:
            return  # already cleared, ignore
        # Topic format: steamdeck/playtime/ack/<session_id>
        parts = topic.split("/")
        if len(parts) == 4 and parts[3]:
            session_id = parts[3]
            print_log(f"ACK received for session {session_id}")
            acked_session_ids.append(session_id)

    client.on_message = on_message
    client.subscribe(f"{BASE_TOPIC}/playtime/ack/#")

    # Give retained messages a moment to arrive
    client.loop_start()
    time.sleep(0.5)
    client.loop_stop()

    for session_id in acked_session_ids:
        remove_session(queue, session_id)
        # Clear the retained ACK topic
        client.publish(
            f"{BASE_TOPIC}/playtime/ack/{session_id}",
            payload="",
            retain=True
        )
        print_log(f"Cleared ACK topic for session {session_id}")

    return queue

# ===========================
# MQTT & Run
# ===========================

def run_update(offline_mode=False):
    print_log("--- Starting MQTT Update ---")
    detected_game, detected_appid, detected_type = detect_game()

    # Always update the local queue regardless of network state
    queue = load_queue()
    update_queue_for_game(queue, detected_game, detected_appid)
    # Reload after potential saves inside update_queue_for_game
    queue = load_queue()

    if not is_network_online():
        print_log("Network offline. Skipping MQTT publish.")
        return

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.tls_set(cert_reqs=ssl.CERT_NONE)
    client.tls_insecure_set(True)

    try:
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)

        # ── Step 1: Process ACKs from HA ──────────────────────────────────────
        queue = process_acks(client, queue)
        # Reload after ACK removals
        queue = load_queue()

        if offline_mode:
            client.loop_start()
            client.publish(f"{BASE_TOPIC}/availability", "offline", retain=True)
            time.sleep(1)
            client.loop_stop()
            client.disconnect()
            write_trace("OFFLINE SIGNAL", "None", 0, "Status")
            return

        # ── Step 2: Collect sensor data ───────────────────────────────────────
        eth_check    = get_output("nmcli -t -f TYPE,STATE dev | grep 'ethernet:connected'")
        is_docked    = "Docked" if eth_check else "Undocked"

        if eth_check:
            network_name = "Ethernet"
        else:
            network_name = get_output("nmcli -t -f ACTIVE,SSID dev wifi | grep '^yes' | cut -d':' -f2")
            if not network_name:
                network_name = "Disconnected"

        battery = get_output(
            "upower -i /org/freedesktop/UPower/devices/battery_BAT1 "
            "| grep percentage | awk '{print $2}' | tr -d '%'"
        ) or "0"
        charging = get_output(
            "upower -i /org/freedesktop/UPower/devices/battery_BAT1 "
            "| grep state | awk '{print $2}'"
        ).capitalize() or "Unknown"
        mode = "Game Mode" if get_output("ps -A | grep gamescope") else "Desktop Mode"

        # ── Step 3: Publish all sensors ───────────────────────────────────────
        client.loop_start()

        client.publish(f"{BASE_TOPIC}/battery",      battery,       retain=True)
        client.publish(f"{BASE_TOPIC}/charging",     charging,      retain=True)
        client.publish(f"{BASE_TOPIC}/mode",         mode,          retain=True)
        client.publish(f"{BASE_TOPIC}/network",      network_name,  retain=True)
        client.publish(f"{BASE_TOPIC}/docked",       is_docked,     retain=True)
        client.publish(f"{BASE_TOPIC}/game",         detected_game, retain=True)
        client.publish(f"{BASE_TOPIC}/game_type",    detected_type, retain=True)
        client.publish(f"{BASE_TOPIC}/appid",        detected_appid or "", retain=True)
        client.publish(f"{BASE_TOPIC}/availability", "online",      retain=True)

        # ── Step 4: Publish playtime queue ────────────────────────────────────
        queue_payload = json.dumps(queue, indent=2)
        client.publish(f"{BASE_TOPIC}/playtime/queue", queue_payload, retain=True)
        print_log(f"Queue published: {len(queue['active_sessions'])} session(s)")

        time.sleep(1)
        client.loop_stop()
        client.disconnect()
        print_log(f"Update successful: {detected_game} [{detected_type}] (appid={detected_appid})")

    except Exception as e:
        print_log(f"MQTT Error: {e}")

if __name__ == "__main__":
    run_update(offline_mode="--offline" in sys.argv)

