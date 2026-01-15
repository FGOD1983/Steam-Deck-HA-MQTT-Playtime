# Steam-Deck-HA-MQTT-Playtime
## Steam Deck Game Tracker for Home Assistant

This project provides a robust way to track playtime for your entire Steam Deck library within Home Assistant. It is specifically designed to work with:

✅ Steam Games  
✅ Non-Steam Games (Heroic, Unifideck)  
✅ Emulation & ROMs (EmuDeck, RetroArch)  
✅ ExoDOS  

The system features a Smart Lookup engine that cleans up folder names and fetches official game titles via the Steam API, storing them in a local cache on your Deck. If needed the cache file can be edited so it will reflect the correct name in Home Assistant.

## ✨ Features 
**🚀 Universal Detection:** Automatic detection of ROMs, eXoDOS, and Windows (.exe) games.  
**🧠 Smart Title Resolver:** Converts mdk_v1.0_clean to MDK using the Steam Store API.  
**🔋 System Stats:** Monitors battery percentage, charging state, and SteamOS Mode (Game vs. Desktop).  
**🔐 Reboot-Resilient:** Home Assistant logic ensures playtime is saved even if HA restarts mid-session.  
**📡 Secure MQTT:** Supports TLS/SSL connections for remote tracking.  

By using a Python script on the Steam Deck and a "Session Lock" logic in Home Assistant, your playtime data remains accurate even if Home Assistant reboots during a gaming session.

*Note: This system requires an active local network connection between your Steam Deck and your MQTT broker to function.*

## 🛠 Prerequisites
* MQTT Broker: A running broker (like Mosquitto) integrated with Home Assistant.  
* Steam Deck: Access to Desktop Mode and a terminal (Konsole).  
* Home Assistant Helpers: Three helpers (Boolean, Datetime, and Text) to manage the session state.

## 📦 Step 1: Steam Deck Setup
1. Install dependencies

Open Konsole in Desktop Mode and run the following to prepare the environment:

```Bash

# Create a folder for the scripts
mkdir -p ~/scripts

# Set up a Python Virtual Environment to keep the system clean
python -m venv ~/mqtt-env
~/mqtt-env/bin/pip install paho-mqtt requests
```
2. Create the Script

Create a file called `/home/deck/scripts/steamdeck_mqtt_sensors.py` and paste the provided Python script.

```python
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
```

3. Auto-start with Systemd
Create a user service file to ensure the script runs in the background: `.config/systemd/user/steamdeck_mqtt_update.service`

```
[Unit]
Description=Periodic Steam Deck MQTT sensor update
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=/home/deck/mqtt-env/bin/python /home/deck/scripts/steamdeck_mqtt_sensors.py
StandardOutput=journal
StandardError=journal
```
Also create the timer service: `.config/systemd/user/steamdeck_mqtt_update.timer`
```
[Unit]
Description=Run Steam Deck MQTT sensor update every 10 seconds 

[Timer]
OnBootSec=30s
OnUnitActiveSec=20s
AccuracySec=1s
Persistent=true

[Install]
WantedBy=timers.target
```
and the service to respond to boot and sleep or to when it looses the network connection: 
`.config/systemd/user/steamdeck_mqtt_boot.service `:
```
[Unit]
Description=Steam Deck MQTT update at boot or resume
After=default.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/home/deck/mqtt-env/bin/python /home/deck/scripts/steamdeck_mqtt_sensors.py --boot
StandardOutput=journal
StandardError=journal
```
`.config/systemd/user/steamdeck_mqtt_offline.service`:
```
[Unit]
Description=Set Steam Deck MQTT sensors offline before sleep
Before=sleep.target
Wants=network.target

[Service]
Type=oneshot
ExecStart=/home/deck/mqtt-env/bin/python /home/deck/scripts/steamdeck_mqtt_sensors.py --offline
StandardOutput=journal
StandardError=journal
```

## 🏠 Step 2: Home Assistant Setup
This part of the setup handles the incoming data, manages the session logic, and ensures everything is saved correctly to a local JSON database.

1. Enable File Writing
To allow Home Assistant to write to your local storage, add the following to your `configuration.yaml`. This is required for the `shell_command` to work.

```YAML
homeassistant:
  allowlist_external_dirs:
    - "/config/www"
    - "/config/www/"
```
2. Create Helpers (UI)
Go to **Settings > Devices & Services > Helpers** and create these three entities:

* **Input Boolean**: `input_boolean.steam_deck_sessie_actief` (Session Lock)
* **Input Datetime**: `input_datetime.steam_deck_sessie_starttijd` (Start Timestamp)
* **Input Text**: `input_text.steam_deck_huidige_game` (Game Name Memory)

3. Sensors & Shell Command
Add these definitions to your `configuration.yaml`. They define how Home Assistant talks to your Deck and how it saves the data.

```YAML
mqtt:
  sensor:
# Steam Deck
    - name: "Steam Deck Battery"
      unique_id: 'steam_deck_battery_percentage'
      state_topic: "steamdeck/battery"
      unit_of_measurement: "%"
      icon: "mdi:battery"
      device_class: battery 
      state_class: measurement
      availability_topic: "steamdeck/availability"
      payload_available: "online"
      payload_not_available: "offline"
      expire_after: 90 # 1,5 minutes without update = unavailable
      
    - name: "Steam Deck Charging"
      unique_id: 'steam_deck_charging'
      state_topic: "steamdeck/charging"
      icon: "mdi:battery-charging"
      availability_topic: "steamdeck/availability"
      payload_available: "online"
      payload_not_available: "offline"
      expire_after: 90  # 1,5 minutes without update = unavailable
    
    - name: "Steam Deck Mode"
      unique_id: 'steam_deck_mode'
      state_topic: "steamdeck/mode"
      icon: "mdi:monitor"
      availability_topic: "steamdeck/availability"
      payload_available: "online"
      payload_not_available: "offline"
      expire_after: 90  # 1,5 minutes without update = unavailable
    
    - name: "Steam Deck Network"
      unique_id: 'steam_deck_network'
      state_topic: "steamdeck/network"
      icon: "mdi:wifi"
      availability_topic: "steamdeck/availability"
      payload_available: "online"
      payload_not_available: "offline"
      expire_after: 90  # 1,5 minutes without update = unavailable
  
    - name: "Steam Deck Active Game"
      unique_id: 'steam_deck_active_game'
      state_topic: "steamdeck/game"
      icon: "mdi:gamepad-variant"
      availability_topic: "steamdeck/availability"
      payload_available: "online"
      payload_not_available: "offline"
      expire_after: 90  # 1,5 minutes without update = unavailable

    - name: "Steam Deck Dock Status"
      state_topic: "steamdeck/docked"
      unique_id: "steamdeck_docked_status"
      icon: "mdi:dock-bottom"
      availability_topic: "steamdeck/availability"
      payload_available: "online"
      payload_not_available: "offline"
      expire_after: 90 # 1,5 minutes without update = unavailable       
```

shell_command:
```yaml
shell_command:
  # Steam Deck
  update_steam_library: "/bin/bash -c 'echo \"{{ json_data }}\" | base64 -d > /config/www/steam_library.json'"
```

sensors.yaml:
```yaml
  - platform: rest
    name: "Steam Deck Game Playtime"
    unique_id: steam_deck_game_playtime
    # 127.0.0.1 is altijd de HA server zelf, ongeacht je URL
    resource: "http://homeassistant.local:8123/local/steam_library.json"
    value_template: >
      {% if value_json is defined and value_json.games is defined %}
        {{ value_json.games | length }}
      {% else %}
        {{ states('sensor.steam_deck_game_playtime') }}
      {% endif %}
    json_attributes:
      - games
    verify_ssl: false
    timeout: 10
    scan_interval: 20
```
templates.yaml:
```yaml
#Steam Deck
- sensor:
    # 🔋 Battery Sensor
    - name: "Steam Deck Battery Display"
      unique_id: steam_deck_battery_display
      state: >
        {% set val = states('sensor.steam_deck_battery') %}
        {% if val in ['unavailable', 'unknown', 'none'] %}
          {{''}}
        {% else %}
          {{ val }}%
        {% endif %}
      icon: >
        {% set val = states('sensor.steam_deck_battery') %}
        {% if val in ['unavailable', 'unknown', 'none'] %}
          mdi:battery-alert
        {% else %}
          {% set pct = val | int(0) %}
          {% if pct > 90 %}
            mdi:battery
          {% elif pct > 80 %}
            mdi:battery-90
          {% elif pct > 70 %}
            mdi:battery-80
          {% elif pct > 60 %}
            mdi:battery-70
          {% elif pct > 50 %}
            mdi:battery-60
          {% elif pct > 40 %}
            mdi:battery-50
          {% elif pct > 30 %}
            mdi:battery-40
          {% elif pct > 20 %}
            mdi:battery-30
          {% elif pct > 10 %}
            mdi:battery-20
          {% elif pct > 0 %}
            mdi:battery-10
          {% else %}
            mdi:battery-outline
          {% endif %}
        {% endif %}
    
    # 📡 Network Sensor
    - name: "Steam Deck Network Display"
      unique_id: steam_deck_network_display
      state: >
        {% set val = states('sensor.steam_deck_network') %}
        {% if val in ['unavailable', 'unknown', 'none'] %}
          {{''}}
        {% else %}
          {{ val }}
        {% endif %}
      icon: >
        {% set val = states('sensor.steam_deck_network') %}
        {% if val in ['unavailable', 'unknown', 'none'] %}
          mdi:lan-disconnect
        {% elif val == 'Ethernet' %}
          mdi:ethernet
        {% else %}
          mdi:wifi
        {% endif %}
        
      # 🎮 Mode Sensor
    - name: "Steam Deck Mode Display"
      unique_id: steam_deck_mode_display
      state: >
        {% set val = states('sensor.steam_deck_mode') %}
        {% if val in ['unavailable', 'unknown', 'none'] %}
          {{''}}
        {% else %}
          {{ val }}
        {% endif %}
      icon: >
        {% set val = states('sensor.steam_deck_mode') %}
        {% if val in ['unavailable', 'unknown', 'none'] %}
          mdi:alert-circle-outline
        {% elif val == 'Game Mode' %}
          mdi:gamepad-variant-outline
        {% elif val == 'Desktop Mode' %}
          mdi:monitor
        {% else %}
          mdi:alert-circle-outline
        {% endif %}
        
    # 🔌 Charging Sensor
    - name: "Steam Deck Charging Display"
      unique_id: steam_deck_charging_display
      state: >
        {% set val = states('sensor.steam_deck_charging') %}
        {% if val in ['unavailable', 'unknown', 'none'] %}
          {{''}}
        {% elif val == 'Pending-charge' %}
          Charged
        {% else %}
          {{ val }}
        {% endif %}
      icon: >
        {% set val = states('sensor.steam_deck_charging') %}
        {% if val in ['unavailable', 'unknown', 'none'] %}
          mdi:battery-remove-outline
        {% elif val == 'Charging' %}
          mdi:battery-charging
        {% elif val == 'Pending-charge' %}
          mdi:power-plug-battery-outline
        {% else %}
          mdi:battery-arrow-down-outline
        {% endif %}

    # 🎮 Active Game Sensor
    - name: "Steam Deck Active Game Display"
      unique_id: steam_deck_active_game_display
      state: >
        {% set val = states('sensor.steam_deck_active_game') %}
        {% if val in ['unavailable', 'unknown', 'none'] %}
          Offline
        {% else %}
          {{ val }}
        {% endif %}
      icon: >
        {% set val = states('sensor.steam_deck_active_game') %}
        {% if val in ['unavailable', 'unknown', 'none'] %}
          mdi:controller-off
        {% else %}
          mdi:controller
        {% endif %}
        
    # 🎮 Dock Status Sensor
    - name: "Steam Deck Dock Status Display"
      unique_id: steam_deck_dock_status_display
      state: >
        {% set val = states('sensor.steam_deck_dock_status') %}
        {% if val in ['unavailable', 'unknown', 'none'] %}
          {{''}}
        {% else %}
          {{ val }}
        {% endif %}
      icon: >
        {% set val = states('sensor.steam_deck_dock_status') %}
        {% if val in ['unavailable', 'unknown', 'none'] %}
          {{ state_attr('sensor.steam_deck_dock_status_display', 'icon')
             if state_attr('sensor.steam_deck_dock_status_display', 'icon') else 'mdi:controller-off' }}
        {% elif val == 'Docked' %}
          fgod:steam-deck-docked-variant
        {% else %}
          fgod:steam-deck-variant-1
        {% endif %}
```

4. The Library Database (JSON)
For the system to store your data, you need to create an initial empty library file.

Use a File Editor or SSH to go to your `/config/www/` folder.

Create a file named `steam_library.json`.

Paste the following:

```JSON
{
  "games": {}
}
```
5. The Core Automation

This automation is the "Brain". It manages the session, calculates the time, and triggers the save command. It is reboot-proof: if Home Assistant restarts during a session, the input_boolean ensures the session remains active, and the input_text remembers which game you were playing.

```YAML
alias: Steam Deck Library
description: Save Steam Deck playtime of games into a readable JSON library.
triggers:
  - trigger: state
    entity_id: sensor.steam_deck_active_game_display
    id: game_state_change
conditions:
  - condition: template
    value_template: >
      {% set sensor = states.sensor.steam_deck_game_playtime %} {{ sensor is not
      none and sensor.state != 'unavailable' and 
      state_attr('sensor.steam_deck_game_playtime', 'games') is not none }}
actions:
  - choose:
      - conditions:
          - condition: trigger
            id: game_state_change
          - condition: state
            entity_id: input_boolean.steam_deck_sessie_actief
            state: "off"
          - condition: template
            value_template: >
              {% set negeren = ['unknown', 'unavailable', 'none', 'idle',
              'offline', 'not playing', 'no game opened'] %} {{
              trigger.to_state.state | lower | trim not in negeren }}
        sequence:
          - action: input_datetime.set_datetime
            target:
              entity_id: input_datetime.steam_deck_sessie_starttijd
            data:
              datetime: "{{ now().strftime('%Y-%m-%d %H:%M:%S') }}"
          - action: input_text.set_value
            target:
              entity_id: input_text.steam_deck_huidige_game
            data:
              value: "{{ trigger.to_state.state | trim }}"
          - action: input_boolean.turn_on
            target:
              entity_id: input_boolean.steam_deck_sessie_actief
      - conditions:
          - condition: trigger
            id: game_state_change
          - condition: state
            entity_id: input_boolean.steam_deck_sessie_actief
            state: "on"
          - condition: template
            value_template: >
              {% set negeren = ['unknown', 'unavailable', 'none', 'idle',
              'offline', 'not playing', 'no game opened'] %} {{
              trigger.to_state.state | lower | trim in negeren }}
        sequence:
          - action: shell_command.update_steam_library
            data:
              json_data: >-
                {% set current_library =
                state_attr('sensor.steam_deck_game_playtime', 'games') or {} %}

                {# 1. TIJD BEREKENING (Rekent tot het moment van nu/failsafe) #}
                {% set start_helper =
                states('input_datetime.steam_deck_sessie_starttijd') |
                as_datetime %} {% set nu_zonder_zone =
                now().replace(tzinfo=None) %} {% set duration = (nu_zonder_zone
                - start_helper).total_seconds() | float(0) %}

                {# 2. GAME NAAM TERUGHALEN (Altijd uit de text-helper voor
                reboot-veiligheid) #} {% set game_naam_opgeslagen =
                states('input_text.steam_deck_huidige_game') | trim %}

                {# Match de naam met de bestaande keys in de library
                (case-insensitive) #} {% set gevonden_naam =
                namespace(key=game_naam_opgeslagen) %} {% for bestaande_naam in
                current_library.keys() %}
                  {% if bestaande_naam | lower == game_naam_opgeslagen | lower %}
                    {% set gevonden_naam.key = bestaande_naam %}
                  {% endif %}
                {% endfor %} {% set game_naam = gevonden_naam.key %}

                {# 3. BESTAANDE DATA OPHALEN #} {% set bestaande_entry =
                current_library.get(game_naam, 0) %} {% if bestaande_entry is
                mapping %}
                  {% set oude_tijd = bestaande_entry.seconds | float(0) %}
                {% else %}
                  {% set oude_tijd = bestaande_entry | float(0) %}
                {% endif %}

                {# 4. NIEUWE JSON ENTRY SAMENSTELLEN #} {% set updated_entry = {
                  "seconds": oude_tijd + duration,
                  "last_played": start_helper.isoformat()
                } %}

                {# 5. VOLLEDIGE LIBRARY UPDATEN EN ENCODEN #} {% set
                updated_library = dict(current_library, **{game_naam:
                updated_entry}) %} {{ ({"games": updated_library} |
                to_json(pretty_print=True)) | base64_encode }}
          - action: input_boolean.turn_off
            target:
              entity_id: input_boolean.steam_deck_sessie_actief
          - action: input_text.set_value
            target:
              entity_id: input_text.steam_deck_huidige_game
            data:
              value: Geen game
          - delay: "00:00:02"
          - action: homeassistant.update_entity
            target:
              entity_id: sensor.steam_deck_game_playtime
mode: single
```
## 📊 Step 3: Visualizing the Data
To read the data back into Home Assistant for your dashboard, use the following card code:

Picture card (save/upload the picture into your www folder on Home Assistant):
```YAML
type: vertical-stack
cards:
  - type: conditional
    conditions:
      - condition: screen
        media_query: (pointer:coarse)
    card:
      type: vertical-stack
      cards:
        - type: picture-elements
          image: /local/steamdeck.png
          elements:
            - type: state-icon
              entity: sensor.steam_deck_network_display
              style:
                top: 12%
                left: 34%
                transform: translateX(-50%)
            - type: state-label
              entity: sensor.steam_deck_network_display
              style:
                top: 24%
                left: 34%
                transform: translateX(-50%)
                text-align: center
                display: inline-block
                min-width: 0
            - type: state-icon
              entity: sensor.steam_deck_battery_display
              style:
                top: 12%
                left: 67%
                transform: translateX(-50%)
              tap_action:
                action: more-info
                entity: sensor.steam_deck_battery
              hold_action:
                action: more-info
                entity: sensor.steam_deck_battery
            - type: state-label
              entity: sensor.steam_deck_battery_display
              style:
                top: 24%
                left: 67%
                transform: translateX(-50%)
                text-align: center
                display: inline-block
                min-width: 0
              tap_action:
                action: more-info
                entity: sensor.steam_deck_battery
              hold_action:
                action: more-info
                entity: sensor.steam_deck_battery
            - type: state-icon
              entity: sensor.steam_deck_dock_status_display
              style:
                top: 55%
                left: 34%
                transform: translateX(-50%)
            - type: state-icon
              entity: sensor.steam_deck_charging_display
              style:
                top: 54%
                left: 67%
                transform: translateX(-50%)
            - type: state-icon
              entity: sensor.steam_deck_active_game_display
              style:
                top: 45%
                left: 50%
                transform: translate(-50%, -100%)
            - type: state-label
              entity: sensor.steam_deck_active_game_display
              style:
                top: 37%
                left: 50%
                transform: translate(-50%, -0%)
                text-align: center
                color: white
                display: inline-block
                min-width: 0
                white-space: normal
                width: 180px
              card_mod:
                style: |
                  div {
                    white-space: unset !important;
                    line-height: 1.1;
                    padding-top: 0.3em;
                      }
  - type: conditional
    conditions:
      - condition: screen
        media_query: (pointer:fine)
    card:
      type: vertical-stack
      cards:
        - type: picture-elements
          image: /local/steamdeck.png
          elements:
            - type: state-icon
              entity: sensor.steam_deck_network_display
              style:
                top: 12%
                left: 34%
                transform: translateX(-50%)
            - type: state-label
              entity: sensor.steam_deck_network_display
              style:
                top: 24%
                left: 34%
                transform: translateX(-50%)
                text-align: center
                display: inline-block
                min-width: 0
            - type: state-icon
              entity: sensor.steam_deck_battery_display
              style:
                top: 12%
                left: 67%
                transform: translateX(-50%)
              tap_action:
                action: more-info
                entity: sensor.steam_deck_battery
              hold_action:
                action: more-info
                entity: sensor.steam_deck_battery
            - type: state-label
              entity: sensor.steam_deck_battery_display
              style:
                top: 24%
                left: 67%
                transform: translateX(-50%)
                text-align: center
                display: inline-block
                min-width: 0
              tap_action:
                action: more-info
                entity: sensor.steam_deck_battery
              hold_action:
                action: more-info
                entity: sensor.steam_deck_battery
            - type: state-icon
              entity: sensor.steam_deck_dock_status_display
              style:
                top: 55%
                left: 34%
                transform: translateX(-50%)
            - type: state-icon
              entity: sensor.steam_deck_charging_display
              style:
                top: 55%
                left: 67%
                transform: translateX(-50%)
            - type: state-icon
              entity: sensor.steam_deck_active_game_display
              style:
                top: 45%
                left: 50%
                transform: translate(-50%, -100%)
            - type: state-label
              entity: sensor.steam_deck_active_game_display
              style:
                top: 39%
                left: 50%
                transform: translate(-50%, -0%)
                text-align: center
                color: white
                display: inline-block
                min-width: 0
                white-space: normal
                width: 180px
              card_mod:
                style: |
                  div {
                    white-space: unset !important;
                    line-height: 1.1;
                    padding-top: 0.3em;
                      }
```
Markdown card:
```yaml
type: markdown
content: >-
  <center> <b> 🎮 Steam Deck Bibliotheek<br> Totaal aantal games gespeeld: {{
  states('sensor.steam_deck_game_playtime') }} <br><br> 🏆 Top 5 Meest
  Gespeeld<br><br> </b>


  | Game | Speeltijd | 

  | :--- | :--- | 

  {%- set attr = state_attr('sensor.steam_deck_game_playtime', 'games') -%} {%-
  if attr is not none -%}
    {%- set library = attr if attr is mapping else attr | from_json -%}
    {%- set ns = namespace(clean_list=[]) -%}
    {%- for game, data in library.items() -%}
      {%- set s = data.seconds if data is mapping else data -%}
      {%- set lp = data.last_played if data is mapping else '2000-01-01T00:00:00' -%}
      {%- set ns.clean_list = ns.clean_list + [{'name': game, 'seconds': s | float, 'last_played': lp}] -%}
    {%- endfor -%}
    {%- for item in (ns.clean_list | sort(attribute='seconds', reverse=True))[:5] -%}
      {%- set uren = (item.seconds / 3600) | int -%}
      {%- set minuten = ((item.seconds % 3600) / 60) | int %}
  | {{ item.name }} | {{ uren }}u {{ minuten }}m |
    {%- endfor %}
  {%- endif %}


  <b>🕒 Laatst Gespeeld<br></b> 


  | Game | Datum & Tijd | 

  | :--- | :--- | 

  {%- if attr is not none -%}
    {%- for item in (ns.clean_list | sort(attribute='last_played', reverse=True))[:5] -%}
      {# Hier passen we het formaat aan naar Dag-Maand om Uur:Minuut #}
      {%- set dt = as_timestamp(item.last_played) | timestamp_custom('%d-%m %H:%M', true) %}
  | {{ item.name }} | {{ dt if item.last_played != '2000-01-01T00:00:00' else
  '---' }} |
    {%- endfor %}
    
    ---
    {%- set totaal_sec = ns.clean_list | map(attribute='seconds') | sum -%}
    {%- set tot_u = (totaal_sec / 3600) | int -%}
    {%- set tot_m = ((totaal_sec % 3600) / 60) | int %}
  **TOTAAL: {{ tot_u }}u {{ tot_m }}m** {%- endif %} </center>
card_mod:
  style: |
    ha-card {
      background: rgba(0,0,0,0.95);
    }
```
