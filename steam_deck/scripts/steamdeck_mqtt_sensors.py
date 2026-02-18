#!/home/deck/mqtt-env/bin/python
import subprocess
import paho.mqtt.client as mqtt
import sys
import os
import re
import datetime
import time
import ssl
import json
import requests
import psutil
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

os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)

# ===========================
# Helpers & Smart Lookup
# ===========================

def print_log(message):
    timestamp = datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
    print(f"{timestamp} [DEBUG] {message}")

def write_trace(game_name, cpu, status="Detection"):
    try:
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(TRACE_LOG_PATH, "a") as f:
            f.write(f"{timestamp} | {status} | Found: {game_name} | CPU: {cpu}%\n")
        
        if os.path.exists(TRACE_LOG_PATH):
            with open(TRACE_LOG_PATH, "r") as f:
                lines = f.readlines()
            if len(lines) > 100:
                with open(TRACE_LOG_PATH, "w") as f:
                    f.writelines(lines[-100:])
    except: pass

def get_output(cmd):
    try:
        return subprocess.check_output(cmd, shell=True, stderr=subprocess.PIPE).decode("utf-8").strip()
    except:
        return ""

def lookup_steam_name(search_term):
    try:
        url_term = quote(search_term)
        search_url = f"https://store.steampowered.com/api/storesearch/?term={url_term}&l=dutch&cc=NL"
        response = requests.get(search_url, timeout=5)
        data = response.json()
        if data.get('total', 0) > 0:
            items = data['items']
            for item in items:
                if item['name'].lower() == search_term.lower():
                    return item['name']
            if search_term.lower() in items[0]['name'].lower():
                return items[0]['name']
    except: pass
    return None

def resolve_game_title(raw_name):
    cache_data = {}
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, 'r') as f:
                content = f.read().strip()
                if content:
                    cache_data = json.loads(content)
        except Exception as e:
            print_log(f"Cache error: {e}")
            if os.path.exists(CACHE_PATH + ".bak"):
                try:
                    with open(CACHE_PATH + ".bak", 'r') as f:
                        cache_data = json.load(f)
                except: pass

    if raw_name in cache_data:
        return cache_data[raw_name]

    print_log(f"Nieuwe titel opzoeken voor: {raw_name}")
    clean_name = re.sub(r'([a-z])([A-Z])', r'\1 \2', raw_name)
    clean_name = clean_name.replace("_", " ").replace("-", " ")
    search_term = " ".join(clean_name.split()).strip()

    final_name = lookup_steam_name(search_term)
    if not final_name:
        final_name = search_term.title()

    cache_data[raw_name] = final_name
    tmp_path = CACHE_PATH + ".tmp"
    bak_path = CACHE_PATH + ".bak"

    try:
        if os.path.exists(CACHE_PATH):
            subprocess.call(f"cp {CACHE_PATH} {bak_path}", shell=True)
        with open(tmp_path, 'w') as f:
            json.dump(cache_data, f, indent=4)
        os.replace(tmp_path, CACHE_PATH)
    except Exception as e:
        print_log(f"Fout bij opslaan cache: {e}")

    return final_name

# ===========================
# Game Detection
# ===========================

def detect_game():
    possible_matches = []
    
    # Ignore list uitgebreid met Steam setup tools en launchers
    ignore_list = [
        "steam.exe", "services.exe", "explorer.exe", "winedevice.exe",
        "system32", "proton", "experimental", "pressure-vessel",
        "command", "epicgameslauncher", "monitoring", "bmlauncher",
        "launcher", "setup.exe", "install.exe", "reaper", "steamwebhelper",
        "overlay", "social", "webhelper", "crashreporter", "eosoverlay",
        "ea desktop", "eadesktop", "destager", "origin", "uplay",
        "iscriptevaluator", "legacycompat", "vivox", "easyanticheat"
    ]
    
    # Technische mappen die we skippen om de echte gamemap te vinden
    tech_folders = [
        "binaries", "win64", "win32", "win32s", "shipping", "pfx", 
        "drive_c", "core", "common", "steamapps", "dist", "scripts", "bin",
        "ea desktop", "origin", "launcher", "system", "oakgame", "engine",
        "bundled", "plugins"
    ]

    for proc in psutil.process_iter(['cmdline', 'cpu_percent', 'create_time']):
        try:
            cmdline = proc.info.get('cmdline')
            if not cmdline: continue
            
            full_cmd = " ".join(cmdline)
            full_cmd_lower = full_cmd.lower()

            # Skip processen in de ignore_list
            if any(x in full_cmd_lower for x in ignore_list):
                continue

            # 1. eXoDOS Check
            exo_match = re.search(r'/eXoDOS/.*\/([^/]+)\.(?:command|bsh)', full_cmd, re.IGNORECASE)
            if exo_match:
                title = re.sub(r'\(\d{4}\)', '', exo_match.group(1)).strip()
                possible_matches.append({'title': title, 'cpu': 100, 'time': proc.info['create_time']})
                continue

            # 2. ROM Check
            rom_ext = "iso|gcm|rvz|zip|7z|cue|bin|elf|nsp|xci|wua|nes|sfc|smc|n64|gba|gbc|gb|nds"
            rom_match = re.search(rf'/roms/[^/]+/([^/]+)\.(?:{rom_ext})', full_cmd, re.IGNORECASE)
            if rom_match:
                clean_rom = re.sub(r'[\(\[][^\]\)]*[\]\)]', '', rom_match.group(1)).strip()
                possible_matches.append({'title': clean_rom, 'cpu': 100, 'time': proc.info['create_time']})
                continue

            # 3. EXE & Path Check (Steam, Linux Native, Heroic, etc.)
            is_steam_game = "steamapps/common" in full_cmd_lower or ".exe" in full_cmd_lower
            is_linux_native = "ut2004" in full_cmd_lower or "/games/" in full_cmd_lower or "/applications/" in full_cmd_lower

            if is_steam_game or is_linux_native:
                path_match = re.search(r'([A-Za-z]:[/\\]|/)(?:[\w\-. ]+[/\\])*[\w\-. ]+(?:\.exe|\.sh)?', full_cmd, re.IGNORECASE)
                if path_match:
                    full_path = path_match.group(0).replace('\\', '/')
                    parts = [p for p in full_path.split('/') if p]
                    
                    game_folder = None
                    # Loop achterwaarts door het pad om de gamemap te vinden
                    for i in range(len(parts)-2, -1, -1):
                        folder = parts[i]
                        f_lower = folder.lower()
                        if (f_lower not in tech_folders and 
                            f_lower not in ["windows", "games", "deck", "home", "users", "usr", "bin", "local", "share"] and 
                            not folder.startswith('.') and 
                            len(folder) > 2 and
                            "launcher" not in f_lower and
                            "desktop" not in f_lower):
                            game_folder = folder
                            break
                    
                    # Fallback voor specifieke executables
                    if not game_folder and "ut2004" in full_cmd_lower:
                        game_folder = "UT2004"

                    if game_folder:
                        possible_matches.append({
                            'title': game_folder,
                            'cpu': proc.info['cpu_percent'],
                            'time': proc.info['create_time']
                        })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if possible_matches:
        # Sorteer op CPU belasting (belangrijkst) en daarna op hoe recent het proces is
        best_match = sorted(possible_matches, key=lambda x: (x['cpu'], x['time']), reverse=True)[0]
        write_trace(best_match['title'], best_match['cpu'])
        if best_match['title'].startswith('.'):
            return "No game opened"
        return resolve_game_title(best_match['title'])

    return "No game opened"

# ===========================
# MQTT & Run
# ===========================

def is_network_online():
    output = get_output("nmcli -t -f DEVICE,TYPE,STATE,CONNECTION dev status")
    return any("connected" in line and ("wifi" in line or "ethernet" in line) for line in output.splitlines())

def run_update(offline_mode=False):
    print_log("--- Starting MQTT Update ---")
    detected_game = detect_game()

    if not is_network_online():
        print_log("Netwerk offline. Overslaan.")
        return

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.tls_set(cert_reqs=ssl.CERT_NONE)
    client.tls_insecure_set(True)

    try:
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
        client.loop_start()

        if offline_mode:
            client.publish(f"{BASE_TOPIC}/availability", "offline", retain=True)
            write_trace("OFFLINE SIGNAL", 0, "Status")
        else:
            battery = get_output("upower -i /org/freedesktop/UPower/devices/battery_BAT1 | grep percentage | awk '{print $2}' | tr -d '%'") or "0"
            charging = get_output("upower -i /org/freedesktop/UPower/devices/battery_BAT1 | grep state | awk '{print $2}'").capitalize() or "Unknown"
            mode = "Game Mode" if get_output("ps -A | grep gamescope") else "Desktop Mode"

            client.publish(f"{BASE_TOPIC}/battery", battery, retain=True)
            client.publish(f"{BASE_TOPIC}/charging", charging, retain=True)
            client.publish(f"{BASE_TOPIC}/mode", mode, retain=True)
            client.publish(f"{BASE_TOPIC}/game", detected_game, retain=True)
            client.publish(f"{BASE_TOPIC}/availability", "online", retain=True)

        time.sleep(1)
        client.loop_stop()
        client.disconnect()
        print_log(f"Update succesvol: {detected_game}")
    except Exception as e:
        print_log(f"MQTT Fout: {e}")

if __name__ == '__main__':
    run_update(offline_mode='--offline' in sys.argv)
