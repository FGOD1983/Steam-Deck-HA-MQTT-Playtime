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

os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)

# ===========================
# s & Smart Lookup
# ===========================

def print_log(message):
    timestamp = datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
    print(f"{timestamp} [DEBUG] {message}")

def get_output(cmd):
    try:
        return subprocess.check_output(cmd, shell=True, stderr=subprocess.PIPE).decode("utf-8").strip()
    except:
        return ""

def lookup_steam_name(search_term):
    """Zoekt op Steam naar de meest exacte match (MDK/Blur fix)."""
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
    """Vertaalt map/bestandsnaam naar mooie titel via cache en API."""
    cache_data = {}
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, 'r') as f:
                cache_data = json.load(f)
        except: pass

    if raw_name in cache_data:
        return cache_data[raw_name]

    # Opschonen voor API
    clean_name = re.sub(r'([a-z])([A-Z])', r'\1 \2', raw_name)
    clean_name = clean_name.replace("_", " ").replace("-", " ")
    search_term = " ".join(clean_name.split()).strip()

    final_name = lookup_steam_name(search_term)
    if not final_name:
        final_name = search_term.title()

    cache_data[raw_name] = final_name
    try:
        with open(CACHE_PATH, 'w') as f:
            json.dump(cache_data, f, indent=4)
        print_log(f"Nieuwe entry: {raw_name} -> {final_name}")
    except: pass
    
    return final_name

# ===========================
# Game Detection
# ===========================

def detect_game():
    ps_output = get_output("ps aux")

    # 1. eXoDOS Detectie
    exo_match = re.search(r'/eXoDOS/.*\/([^/]+)\.(?:command|bsh)', ps_output, re.IGNORECASE)
    if exo_match:
        raw_exo = exo_match.group(1)
        clean_exo = re.sub(r'\(\d{4}\)', '', raw_exo).strip()
        return resolve_game_title(clean_exo)

    # 2. Universele ROM Detectie (NES, Switch, RetroArch, etc.)
    # Uitgebreid met alle gangbare emulator extensies
    rom_ext = "iso|gcm|rvz|zip|7z|cue|bin|elf|nsp|xci|wua|nes|sfc|smc|n64|gba|gbc|gb|nds"
    rom_match = re.search(rf'/roms/[^/]+/([^/]+)\.(?:{rom_ext})', ps_output, re.IGNORECASE)
    if rom_match:
        raw_rom = rom_match.group(1)
        # Strip tags zoals (U), [!], [01008C2...]
        clean_rom = re.sub(r'[\(\[][^\]\)]*[\]\)]', '', raw_rom).strip()
        clean_rom = " ".join(clean_rom.split())
        return resolve_game_title(clean_rom)

    # 3. Universele EXE Detectie (Epic, GOG, Blur, Non-Steam)
    ignore_list = [
        "steam.exe", "services.exe", "explorer.exe", "winedevice.exe", 
        "system32", "proton", "experimental", "pressure-vessel", 
        "command", "epicgameslauncher", "monitoring", "bmlauncher", 
        "launcher", "setup.exe", "install.exe", "reaper"
    ]
    
    for line in ps_output.splitlines():
        if ".exe" in line.lower() and not any(x in line.lower() for x in ignore_list):
            # Isoleer pad om ps-rommel te vermijden
            path_match = re.search(r'([A-Za-z]:[/\\]|/)(?:[\w\-. ]+[/\\])*[\w\-. ]+\.exe', line, re.IGNORECASE)
            if path_match:
                full_path = path_match.group(0).replace('\\', '/')
                parts = [p for p in full_path.split('/') if p]
                if len(parts) > 1:
                    exe_name = parts[-1].lower()
                    if "launcher" in exe_name: continue
                    
                    folder = parts[-2]
                    tech_folders = ["binaries", "win64", "win32", "shipping", "pfx", "drive_c", "core"]
                    if folder.lower() in tech_folders and len(parts) > 2:
                        folder = parts[-3]
                    
                    if folder.lower() not in ["windows", "common", "steamapps", "steam", "games"]:
                        return resolve_game_title(folder)

    # 4. Steam Native Fallback
    game_native = get_output(
        "ps aux | awk '{gsub(/\\\\/, \"/\"); if ($0 ~ /steamapps\\/common\\// && $0 !~ /gamescope|steamwebhelper|grep|[Pp]roton|[Rr]untime|[Ee]xperimental|[Ss]oldier|[Ss]niper|[Pp]ressure-vessel/) "
        "{split($0, a, \"/\"); for (i=1; i<=length(a); i++) if (a[i]==\"common\") {print a[i+1]; next}}}' | uniq"
    )
    if game_native:
        return resolve_game_title(game_native)

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
        print_log("Geen netwerk.")
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
        print_log(f"Update voltooid: {detected_game}")
    except Exception as e:
        print_log(f"MQTT Fout: {e}")

if __name__ == '__main__':
    run_update(offline_mode='--offline' in sys.argv)
