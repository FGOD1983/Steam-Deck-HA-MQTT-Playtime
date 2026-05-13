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

MQTT_HOST        = "YOUR_HA_RUNNING_MQTT"
MQTT_PORT        = 8883
MQTT_USER        = "YOUR_MQTT_USER_FOR_HA"
MQTT_PASS        = "YOUR_MQTT_PASSWORD_FOR_HA"
BASE_TOPIC       = "steamdeck"
CACHE_PATH       = "/home/deck/scripts/game_cache.json"
TRACE_LOG_PATH   = "/home/deck/scripts/game_trace.log"
STEAM_APPS_PATH  = "/home/deck/.steam/steam/steamapps"
QUEUE_PATH       = "/home/deck/scripts/playtime_queue.json"
LAST_RUN_PATH    = "/home/deck/scripts/last_run.json"
STEAM_USER_PATH  = os.path.expanduser("~/.local/share/Steam/userdata")


# Gap threshold in seconds — if the script hasn't run in this long,
# the Deck was assumed to be in standby.
GAP_THRESHOLD_SECONDS = 30

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
# Last Run helpers
# ===========================

def load_last_run():
    """
    Load last_run.json.
    Returns a list of run entries (most recent last).
    When online, only the last entry matters.
    When offline, entries accumulate until back online.
    """
    if not os.path.exists(LAST_RUN_PATH):
        return []
    try:
        with open(LAST_RUN_PATH, "r") as f:
            data = json.load(f)
        # Support both single dict (legacy) and list format
        if isinstance(data, dict):
            return [data]
        return data if isinstance(data, list) else []
    except Exception as e:
        print_log(f"last_run.json load error: {e}")
        return []

def get_last_run(runs):
    """Return the most recent run entry, or empty dict if none."""
    return runs[-1] if runs else {}

def save_last_run(runs, online):
    """
    Save last_run.json.
    If online: overwrite with just the current (last) entry — clean slate.
    If offline: keep all accumulated offline entries.
    """
    tmp = LAST_RUN_PATH + ".tmp"
    try:
        data = [runs[-1]] if (online and runs) else runs
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, LAST_RUN_PATH)
    except Exception as e:
        print_log(f"last_run.json save error: {e}")

def build_run_entry(timestamp, online, detected_game, detected_appid, detected_type,
                    battery, charging, mode, network_name, is_docked, open_session):
    """Build a full run entry dict for last_run.json."""
    session_data = None
    if open_session:
        session_data = {
            "session_id":     open_session.get("session_id"),
            "game_state":     open_session.get("game_state"),
            "start_time":     open_session.get("start_time"),
            "end_time":       open_session.get("end_time"),
            "start_playtime": open_session.get("start_playtime"),
            "end_playtime":   open_session.get("end_playtime"),
            "ha_processed":   open_session.get("ha_processed"),
        }
    return {
        "timestamp":  timestamp,
        "online":     online,
        "game":       detected_game,
        "appid":      detected_appid or "",
        "game_type":  detected_type,
        "session_id": open_session.get("session_id") if open_session else None,
        "battery":    battery,
        "charging":   charging,
        "mode":       mode,
        "network":    network_name,
        "docked":     is_docked,
        "session":    session_data,
    }

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

    Non-Steam games can appear under both their unsigned uint32 key AND their
    signed int32 key. Steam stores the actual playtime under the signed key.
    We check both and return the one with the higher playtime value, since a
    zero entry under the unsigned key should not override real data under the
    signed key.

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

        candidates = []

        # Try unsigned key (as-is string)
        entry_unsigned = apps.get(str(appid))
        if entry_unsigned:
            candidates.append(int(entry_unsigned.get("Playtime", 0)))

        # Try signed int32 key
        try:
            raw = int(appid)
            # Convert to signed int32 if needed
            if raw >= 2**31:
                signed_key = str(raw - 2**32)
            elif raw < 0:
                signed_key = str(raw)
            else:
                signed_key = None

            if signed_key and signed_key != str(appid):
                entry_signed = apps.get(signed_key)
                if entry_signed:
                    candidates.append(int(entry_signed.get("Playtime", 0)))
        except (ValueError, TypeError):
            pass

        # Return the highest value found — real playtime beats a zero placeholder
        if candidates:
            return max(candidates)

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

def save_queue(q):
    """Atomically write playtime_queue.json."""
    tmp = QUEUE_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(q, f, indent=2)
        os.replace(tmp, QUEUE_PATH)
    except Exception as e:
        print_log(f"Queue save error: {e}")

def get_open_session(q):
    """Return the first session with game_state == 'opened', or None."""
    for s in q["active_sessions"]:
        if s.get("game_state") == "opened":
            return s
    return None

def open_session(q, name, appid):
    """Add a new opened session to the queue."""
    now      = int(time.time())
    playtime = get_localconfig_playtime(appid) if appid else 0
    session  = {
        "session_id":     str(now),
        "appid":          appid or "",
        "name":           name,
        "game_state":     "opened",
        "ha_processed":   None,
        "start_playtime": playtime,
        "end_playtime":   None,
        "start_time":     now,
        "end_time":       None,
    }
    q["active_sessions"].append(session)
    save_queue(q)
    print_log(f"Queue: opened session for '{name}' (appid={appid}, start_playtime={playtime}m)")
    return session

def close_session(q, session, ha_processed, end_time_override=None):
    """
    Mark a session as closed.

    ha_processed: True only when gap detected + same game + last run was online
                  (meaning HA already registered this session via standby detection).
                  False in all other cases — let HA process it via normal flows.

    end_time_override: use a specific end_time (last run timestamp for gap closes)
                       instead of now.
    end_playtime is read from localconfig.vdf at close time.
    """
    now      = end_time_override if end_time_override is not None else int(time.time())
    playtime = get_localconfig_playtime(session.get("appid")) if session.get("appid") else 0
    session["game_state"]   = "closed"
    session["ha_processed"] = ha_processed
    session["end_time"]     = now
    session["end_playtime"] = playtime
    save_queue(q)
    print_log(
        f"Queue: closed session '{session['name']}' "
        f"(ha_processed={ha_processed}, end_playtime={playtime}m, "
        f"end_time={now}, duration={now - session['start_time']}s)"
    )

def remove_session(q, session_id):
    """Remove a session from the queue by session_id."""
    before = len(q["active_sessions"])
    q["active_sessions"] = [
        s for s in q["active_sessions"]
        if s["session_id"] != session_id
    ]
    after = len(q["active_sessions"])
    if before != after:
        save_queue(q)
        print_log(f"Queue: removed session {session_id}")

def update_queue_for_game(q, detected_game, detected_appid, last_run, online):
    """
    Compare detected game against the current open session.
    Uses last_run to detect standby gaps and set ha_processed correctly.

    Gap detection: if time since last run > GAP_THRESHOLD_SECONDS, the Deck
    was in standby. The open session is closed with end_time = last run timestamp.

    ha_processed logic — only True in ONE case:
      Gap detected + same game + last run was online
      → HA saw the sensor go offline after 90s and already registered the session.

    All other closes → ha_processed=False
      → HA automations handle processing when they receive the queue data.
    """
    now          = int(time.time())
    no_game      = (detected_game == "No game opened")
    open_sess    = get_open_session(q)
    last_ts      = last_run.get("timestamp", now)
    last_game    = last_run.get("game", "No game opened")
    last_online  = last_run.get("online", False)
    gap_detected = (now - last_ts) > GAP_THRESHOLD_SECONDS

    if gap_detected:
        print_log(f"Gap detected: {now - last_ts}s since last run (threshold={GAP_THRESHOLD_SECONDS}s)")

    # ── Gap detected with an open session ─────────────────────────────────────
    if gap_detected and open_sess:
        same_game = (open_sess["name"] == last_game)

        # Only True if: gap + same game + last run was online
        # In this case HA saw the sensor go offline and registered the session
        ha_processed = (same_game and last_online)

        print_log(
            f"Gap close: '{open_sess['name']}' | same_game={same_game} | "
            f"last_online={last_online} | ha_processed={ha_processed}"
        )
        # Close with end_time = last run timestamp (moment standby began)
        close_session(q, open_sess, ha_processed=ha_processed, end_time_override=last_ts)

        # If a different game is now detected, open a new session for it
        if not no_game:
            open_session(q, detected_game, detected_appid)
        return

    # ── No gap — normal flow ──────────────────────────────────────────────────
    if no_game:
        if open_sess:
            print_log(f"Queue: no game detected, closing '{open_sess['name']}'")
            # ha_processed=False — HA processes this via deck queue when it receives it
            close_session(q, open_sess, ha_processed=False)
        return

    if open_sess:
        if open_sess["name"] == detected_game:
            # Same game still running — nothing to do
            return
        else:
            # Game swap — close current, open new
            print_log(f"Queue: game swap '{open_sess['name']}' → '{detected_game}'")
            # ha_processed=False — deck queue handles this via end_playtime
            close_session(q, open_sess, ha_processed=False)

    open_session(q, detected_game, detected_appid)

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

def process_acks(client, q):
    """
    Read retained ACK messages from steamdeck/playtime/ack/#.
    For each ACK:
      - Remove the session from the local queue
      - Clear the retained ACK topic on MQTT
    """
    acked_session_ids = []

    def on_message(c, userdata, msg):
        topic   = msg.topic
        payload = msg.payload.decode("utf-8", errors="replace").strip()
        if not payload:
            return
        parts = topic.split("/")
        if len(parts) == 4 and parts[3]:
            session_id = parts[3]
            print_log(f"ACK received for session {session_id}")
            acked_session_ids.append(session_id)

    client.on_message = on_message
    client.subscribe(f"{BASE_TOPIC}/playtime/ack/#")

    client.loop_start()
    time.sleep(0.5)
    client.loop_stop()

    for session_id in acked_session_ids:
        remove_session(q, session_id)
        client.publish(
            f"{BASE_TOPIC}/playtime/ack/{session_id}",
            payload="",
            retain=True
        )
        print_log(f"Cleared ACK topic for session {session_id}")

    return q

# ===========================
# MQTT & Run
# ===========================

def run_update(offline_mode=False):
    print_log("--- Starting MQTT Update ---")
    now    = int(time.time())
    online = is_network_online()

    detected_game, detected_appid, detected_type = detect_game()

    # ── Load last run data ────────────────────────────────────────────────────
    runs     = load_last_run()
    last_run = get_last_run(runs)

    # ── Update local queue with gap detection and ha_processed logic ──────────
    q = load_queue()
    update_queue_for_game(q, detected_game, detected_appid, last_run, online)
    q = load_queue()

    # ── Collect sensor data (needed for last_run.json even when offline) ──────
    eth_check    = get_output("nmcli -t -f TYPE,STATE dev | grep 'ethernet:connected'")
    is_docked    = "Docked" if eth_check else "Undocked"
    if eth_check:
        network_name = "Ethernet"
    else:
        network_name = get_output("nmcli -t -f ACTIVE,SSID dev wifi | grep '^yes' | cut -d':' -f2")
        if not network_name:
            network_name = "Disconnected"
    battery  = get_output(
        "upower -i /org/freedesktop/UPower/devices/battery_BAT1 "
        "| grep percentage | awk '{print $2}' | tr -d '%'"
    ) or "0"
    charging = get_output(
        "upower -i /org/freedesktop/UPower/devices/battery_BAT1 "
        "| grep state | awk '{print $2}'"
    ).capitalize() or "Unknown"
    mode = "Game Mode" if get_output("ps -A | grep gamescope") else "Desktop Mode"

    if not online:
        print_log("Network offline. Skipping MQTT publish.")
        open_sess = get_open_session(q)
        run_entry = build_run_entry(
            now, False, detected_game, detected_appid, detected_type,
            battery, charging, mode, network_name, is_docked, open_sess
        )
        runs.append(run_entry)
        save_last_run(runs, online=False)
        return

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.tls_set(cert_reqs=ssl.CERT_NONE)
    client.tls_insecure_set(True)

    try:
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)

        # ── Step 1: Process ACKs from HA ──────────────────────────────────────
        q = process_acks(client, q)
        q = load_queue()

        if offline_mode:
            client.loop_start()
            client.publish(f"{BASE_TOPIC}/availability", "offline", retain=True)
            time.sleep(1)
            client.loop_stop()
            client.disconnect()
            write_trace("OFFLINE SIGNAL", "None", 0, "Status")
            return

        # ── Step 2: Publish all sensors ───────────────────────────────────────
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

        # ── Step 3: Publish playtime queue ────────────────────────────────────
        queue_payload = json.dumps(q, indent=2)
        client.publish(f"{BASE_TOPIC}/playtime/queue", queue_payload, retain=True)
        print_log(f"Queue published: {len(q['active_sessions'])} session(s)")

        time.sleep(1)
        client.loop_stop()
        client.disconnect()
        print_log(f"Update successful: {detected_game} [{detected_type}] (appid={detected_appid})")

        # ── Step 4: Save last run (online — overwrite with single entry) ──────
        open_sess = get_open_session(q)
        run_entry = build_run_entry(
            now, True, detected_game, detected_appid, detected_type,
            battery, charging, mode, network_name, is_docked, open_sess
        )
        runs.append(run_entry)
        save_last_run(runs, online=True)

    except Exception as e:
        print_log(f"MQTT Error: {e}")

if __name__ == "__main__":
    run_update(offline_mode="--offline" in sys.argv)
