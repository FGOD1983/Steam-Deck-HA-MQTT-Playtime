# Steam-Deck-HA-MQTT-Playtime

# Support
Hey if you like what I did with this, :beers: or a :pizza: would be nice :D

[![coffee](https://www.buymeacoffee.com/assets/img/custom_images/black_img.png)](https://buymeacoffee.com/fgod)

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
**🎨 Game Cover Art:** Automatically fetches game cover art from IGDB and displays it on your dashboard.  
**🔄 Auto Token Refresh:** IGDB Bearer token is automatically refreshed before it expires.  
**🕹️ Steam Native Playtime Sync:** Official Steam playtime is fetched from the Steam API after each session and used to keep your library accurate.  
**⚡ Queue-Based Processing:** A persistent Python service handles all playtime calculations in order, preventing data corruption when switching games rapidly.  

By using a Python script on the Steam Deck and a persistent queue processor service in Home Assistant, your playtime data remains accurate even if Home Assistant reboots during a gaming session or you switch games rapidly.

*Note: This system requires an active local network connection between your Steam Deck and your MQTT broker to function.*

## 🛠 Prerequisites
* MQTT Broker: A running broker (like Mosquitto) integrated with Home Assistant.  
* Steam Deck: Access to Desktop Mode and a terminal (Konsole).  
* Home Assistant Helpers: Several helpers (Boolean, Datetime, Text) to manage the session state, IGDB tokens, cover art and Steam playtime sync.

## 🎮 [Step 1: Steam Deck Setup](./steam_deck/)

### 1.1 Install dependencies

Open Konsole in Desktop Mode and run the following to prepare the environment:

```bash
# Create a folder for the scripts
mkdir -p ~/scripts

# Set up a Python Virtual Environment to keep the system clean
python -m venv ~/mqtt-env
~/mqtt-env/bin/pip install paho-mqtt requests psutil
```

### 1.2 Create the Script

Create a file called `/home/deck/scripts/steamdeck_mqtt_sensors.py` and paste the provided [Python script](./steam_deck/scripts/steamdeck_mqtt_sensors.py). Don't forget to edit variables at the beginning of the script (MQTT_HOST, MQTT_USER, MQTT_PASS).

### 1.3 Auto-start with Systemd

Create a file called `.config/systemd/user/steamdeck_mqtt_update.service` and copy the [update service code](./steam_deck/services/steamdeck_mqtt_update.service) into the file.

Also create the timer service `.config/systemd/user/steamdeck_mqtt_update.timer` and copy the [timer service code](./steam_deck/services/steamdeck_mqtt_update.timer) into the file.

In order to make it respond correctly at boot or sleep/wake we also need to create services for that.

Create a file called `.config/systemd/user/steamdeck_mqtt_boot.service` and copy the [boot service code](./steam_deck/services/steamdeck_mqtt_boot.service) into the file.
Create a file called `.config/systemd/user/steamdeck_mqtt_offline.service` and copy the [offline service code](./steam_deck/services/steamdeck_mqtt_offline.service) into the file.

After creating the files you can enable the services:

```
systemctl --user enable steamdeck_mqtt_offline.service
systemctl --user enable steamdeck_mqtt_boot.service
systemctl --user enable --now steamdeck_mqtt_update.timer
```

## 🏠 Step 2: Home Assistant Setup

This part of the setup handles the incoming data, manages the session logic, and ensures everything is saved correctly to a local JSON database.

### 2.1 Enable File Writing

To allow Home Assistant to write to your local storage, add [the following code](./home_assistant/configuration.yaml) to your `configuration.yaml`. This is required for the `shell_command` to work.

### 2.2 Create Helpers (UI)

Go to **Settings > Devices & Services > Helpers** and create these entities:

**Session tracking:**
* **Input Boolean**: `input_boolean.steam_deck_sessie_actief` (Steam Deck Sessie Actief)
* **Input Datetime**: `input_datetime.steam_deck_sessie_starttijd` (Steam Deck Sessie Starttijd)
* **Input Text**: `input_text.steam_deck_huidige_game` (Steam Deck Huidige Game)
* **Input Text**: `input_text.steam_deck_game_cover_url` (Steam Deck Game Cover URL) — stores the cover art URL for the currently active game

**IGDB API (for cover art):**
* **Input Text**: `input_text.igdb_client_id` — your Twitch/IGDB Client ID
* **Input Text**: `input_text.igdb_client_secret` — your Twitch/IGDB Client Secret
* **Input Text**: `input_text.igdb_bearer_token` — managed automatically, stores the current Bearer token
* **Input Text**: `input_text.igdb_token_expiry` — managed automatically, stores the token expiry timestamp

> ℹ️ The `igdb_bearer_token` and `igdb_token_expiry` helpers are automatically updated by the cover art automation whenever the token is close to expiring. You only need to set them manually on first setup (see Step 3.3).

**Steam Native playtime sync & cover art:**
* **Input Text**: `input_text.steam_api_key` — your Steam Web API key (see Step 4.1)
* **Input Text**: `input_text.steam_user_id` — your 64-bit Steam ID (see Step 4.1)
* **Input Text**: `input_text.steam_pending_appid` — managed automatically, holds the appid between game start and close
* **Input Text**: `input_text.steam_pending_game_type` — managed automatically, holds the game type between game start and close
* **Input Text**: `input_text.steam_pending_image_type` — managed automatically, holds `capsule` or `header` depending on which Steam CDN image is available for the current game

> ℹ️ The `steam_pending_appid`, `steam_pending_game_type` and `steam_pending_image_type` are all managed automatically. You only need to create them — the automations handle the rest.

### 2.3 Sensors & Shell Commands

Now we need to build the sensors and the needed shell commands for the data. These can be created in the `configuration.yaml` or in their own separate yaml file which should then be included in the `configuration.yaml`.

Let's first do the MQTT sensors inside [`mqtt.yaml`](./home_assistant/sensors/mqtt.yaml). Copy the code inside your Home Assistant mqtt.yaml file.

Next up are the shell commands. Copy the code from [`shell_commands.yaml`](./home_assistant/shell_commands.yaml) into your `configuration.yaml` or separate shell commands yaml file.

Now let's do the same for the REST Sensor [REST Sensor](./home_assistant/sensors/sensors.yaml). Copy that code into your `sensors.yaml` file.

And for the last sensors, you will need to copy the [`templates.yaml`](./home_assistant/sensors/templates.yaml) file content to your `templates.yaml` file on Home Assistant.

### 2.4 The Library Database (JSON)

For the system to store your data, you need to create an initial empty library file.

Use a File Editor or SSH to go to your `/homeassistant/www/` folder (which is basically your `/config/www` folder).

Create a file there named `steam_library.json` and copy in the template data from the [`steam_library.json`](./home_assistant/www/steam_library.json) file from this repo.

### 2.5 The Queue Processor Service

The queue processor is a persistent Python service that runs in the background on your Home Assistant server. It receives game open and close events from HA automations via a local HTTP server, manages a queue file for crash recovery, and handles all playtime calculations and Steam API calls.

> ℹ️ **Why a queue processor?** When you switch games rapidly on the Steam Deck, the script on the Deck runs every 90 seconds which means it can miss the `No game opened` state between two games. The queue processor detects these swaps, serializes all events and processes them one by one in order, preventing data corruption and ensuring every session is recorded correctly.

#### 2.5.1 Create the Scripts Folder

Use File Editor or SSH to create the folder `/config/scripts/` if it does not already exist.

#### 2.5.2 Add the Queue Processor Script

Copy [`steam_queue_processor.py`](./home_assistant/scripts/steam_queue_processor.py) into `/config/scripts/steam_queue_processor.py`.

#### 2.5.3 Add the Config File

Copy [`steam_queue_config.json`](./home_assistant/scripts/steam_queue_config_without_InfluxDB.json) into `/config/scripts/steam_queue_config.json` and fill in your values for ha_token and ha_url.

If you want to use the setup with InfluxDB and Grafana, you will need to copy [`steam_queue_config.json`](./home_assistant/scripts/steam_queue_config_with_InfluxDB.json) into `/config/scripts/steam_queue_config.json` instead and fill in ha_roken, ha_url, influxdb_db (if you created it with another name), influxdb_user and influxdb_password.

To generate a long-lived access token go to your HA **Profile → Security → Long-lived access tokens** and click **Create Token**.

> ℹ️ The service listens on `http://127.0.0.1:8098` by default — only accessible locally, not externally. If port 8098 is already in use on your system you can change it to any free port here and in `shell_commands.yaml`.

#### 2.5.4 Add the Core Automations

**Game opened automation** — triggers when a game starts on the Steam Deck. Waits 2 seconds for MQTT sensors to settle, then posts the game details to the queue processor service and updates the session helpers.

Create a new automation, switch to YAML mode and paste in the [`steam_deck_game_opened.yaml`](./home_assistant/automations/steam_deck_game_opened.yaml) code.

**Game closed automation** — triggers when a game stops. Posts the stop event to the queue processor service with a `properly_closed` flag. This flag is `false` when the sensor reports `offline`, `unavailable` or `unknown` — indicating the Deck went to standby or the script crashed — and `true` for all normal closes. Game swaps (switching directly from one game to another) are detected by the queue processor itself and always treated as properly closed for Steam Native games.

Create a second new automation, switch to YAML mode and paste in the [`steam_deck_game_closed.yaml`](./home_assistant/automations/steam_deck_game_closed.yaml) code.

**Queue processor startup and watchdog automations** — the startup automation launches the queue processor service when HA starts. The watchdog automation checks every 5 minutes if the service is still running and restarts it automatically if it has crashed, sending a persistent notification.

Create two more automations by pasting the [`steam_queue_processor_automations.yaml`](./home_assistant/automations/steam_queue_processor_automations.yaml) code — this file contains both automations, so paste each one separately in YAML mode.

After adding all automations and shell commands do a **full Home Assistant restart**. The startup automation will launch the queue processor automatically.

> ℹ️ You can verify the queue processor is running and inspect the current queue at any time by visiting `http://your-ha-ip:8098/status` or by checking the log at `/config/scripts/steam_queue_processor.log`.

## 🎨 Step 3: IGDB Game Cover Art Setup

To display game cover art on your dashboard, you need a free IGDB API account. IGDB is owned by Twitch, so authentication goes through the Twitch Developer portal.

> ℹ️ **Cover art sources:** Steam Native games use Steam's own images fetched directly from the Steam CDN using the appid — the automation checks if a high quality capsule image (616x353) is available and falls back to the standard header image (460x215) if not. All other game types (ROMs, non-Steam, ExoDOS) use IGDB for cover art lookup by game name.

### 3.1 Create a Twitch Developer Application

1. Go to [https://dev.twitch.tv/console](https://dev.twitch.tv/console) and log in with your Twitch account (or create a free one if you don't have one).
2. Click **Register Your Application**.
3. Fill in the form:
   - **Name**: anything you like, e.g. `HomeAssistant IGDB`
   - **OAuth Redirect URLs**: `https://my.home-assistant.io/redirect/oauth`
   - **Category**: select `Application Integration`
4. Click **Create**.
5. On the next screen click **Manage** next to your new application.
6. Note down your **Client ID** — paste this into the `input_text.igdb_client_id` helper you created in Step 2.
7. Click **New Secret** to generate a **Client Secret** — copy and paste this into the `input_text.igdb_client_secret` helper. Save it somewhere safe as it is only shown once.

### 3.2 Add the Shell Commands

Copy the code from [`shell_commands.yaml`](./home_assistant/shell_commands.yaml) into your `shell_commands.yaml` file. This adds the following commands:

**Cover art:**
- `fetch_igdb_cover` — searches IGDB for a game cover by name
- `refresh_igdb_token` — calls Twitch to get a new Bearer token when the current one is about to expire
- `check_steam_image` — checks whether a Steam CDN image URL exists, used to determine whether to use the capsule or header image for Steam Native games

**Queue processor:**
- `post_game_start` — posts a game start event to the queue processor service
- `post_game_stop` — posts a game stop event to the queue processor service
- `start_queue_processor` — starts the queue processor service in the background
- `check_queue_processor` — checks if the queue processor service is running, used by the watchdog automation

All commands receive their parameters as variables from the automation at runtime, so no credentials are hardcoded in the config files.

After adding the shell commands, do a **full Home Assistant restart** — shell commands require a full restart to register.

### 3.3 Generate the Initial Bearer Token

The automation handles token renewal automatically, but you need to set up the initial token manually once.

Run the following command in your terminal, replacing the placeholders with your actual Client ID and Client Secret:

```bash
curl -s -X POST 'https://id.twitch.tv/oauth2/token' \
  -d 'client_id=YOUR_CLIENT_ID' \
  -d 'client_secret=YOUR_CLIENT_SECRET' \
  -d 'grant_type=client_credentials'
```

The response will look like this:

```json
{
  "access_token": "your_bearer_token_here",
  "expires_in": 5183944,
  "token_type": "bearer"
}
```

Now calculate the expiry date by adding `expires_in` seconds to the current time:

```bash
# Linux
date -d "+5183944 seconds" --iso-8601=seconds

# Mac
date -v +5183944S +"%Y-%m-%dT%H:%M:%S"
```

Then set the values in your helpers:
- `input_text.igdb_bearer_token` → paste the `access_token` value
- `input_text.igdb_token_expiry` → paste the calculated expiry date (e.g. `2026-04-28T22:15:00`)

> ✅ After this one-time setup the automation will automatically refresh the token whenever it is within 7 days of expiring. You will never need to manually update it again.

### 3.4 Add the Cover Art Automation

This automation triggers whenever the active game sensor changes. For Steam Native games it checks whether a high quality capsule image is available on the Steam CDN and falls back to the header image if not. For all other game types it checks if the Bearer token is still valid, refreshes it if needed, and fetches the cover from IGDB. When a game stops it clears the cover.

Create a new automation, switch to YAML mode and paste in the [`steam_deck_game_cover.yaml`](./home_assistant/automations/steam_deck_game_cover.yaml) code.

### 3.5 Add the Image Entity

The cover URL template sensor and image entity are already included in:

[`templates.yaml`](./home_assistant/sensors/templates.yaml)

You can copy them from the templates.yaml file into your own Home Assistant configuration.

## 🕹️ Step 4: Steam Native Playtime Sync Setup

For Steam Native games, the system fetches the official total playtime directly from Steam after each session and uses that to keep your library accurate. This means your playtime in Home Assistant will always match what Steam reports, including any time played on other devices.

> ℹ️ **How it works:** When a Steam Native game is properly closed the queue processor waits 3 minutes to give Steam time to update its servers, then calls the Steam API, finds the game by appid, and overwrites the session-tracked playtime with the official Steam total. This applies to both normal closes and game swaps — if you switch directly from one Steam Native game to another the swap is detected and each game gets its own 3 minute Steam API wait. If the Deck goes to standby with a game open, the session time is calculated from the start and stop timestamps and added to the existing playtime instead.

### 4.1 Get Your Steam API Key and Steam ID

**Steam API Key:**
1. Go to [https://steamcommunity.com/dev/apikey](https://steamcommunity.com/dev/apikey) and log in with your Steam account.
2. Enter any domain name in the field (e.g. `localhost`) — it does not matter for personal use.
3. Click **Register** and copy the API key shown on the page.
4. Paste it into the `input_text.steam_api_key` helper you created in Step 2.

**Steam ID (64-bit):**
1. Go to [https://steamid.io](https://steamid.io) and enter your Steam profile URL or username.
2. Copy the **steamID64** value — a 17-digit number like `76561198012345678`.
3. Paste it into the `input_text.steam_user_id` helper you created in Step 2.

> ⚠️ Make sure your Steam profile's **Game details** privacy setting is set to **Public**, otherwise the API will return no data.

### 4.2 Shell Commands

The Steam API calls are handled entirely by the queue processor service — no additional shell commands are needed beyond what was added in Step 3.2. The queue processor reads the Steam API key and Steam user ID directly from the HA helpers via the REST API using the token you configured in the queue processor config file.

## 📊 Step 5: Visualizing the Data

The dashboard card uses a custom Steam Deck icon set for the dock status. You need to install this before adding the card.

### 5.1 Install the Custom Icon Set

1. Copy [`hass-fgod-icons.js`](./home_assistant/www/hass-fgod-icons.js) into your `/config/www/` folder.
2. Add the [`configuration.yaml`](./home_assistant/configuration.yaml) code to your configuration.yaml file.
3. Do a **full Home Assistant restart** after adding this.

> ℹ️ Without the icon set the dock status icons will not display correctly on the dashboard card.

### 5.2 Add the Dashboard Cards

The first card uses [`steamdeck.png`](./home_assistant/www/steamdeck.png) as the background image. Copy this file into your `/config/www/` folder alongside `steam_library.json`.

When that is done you can go to your dashboard and create a new card with the [`picture_card.yaml`](./home_assistant/dashboard/picture_card.yaml) code.

The second card is a Markdown card which displays the top 5 most played games with their total playtime and the last 5 played games with their day and time they were last played.

Create a new card on your dashboard and copy the [`markdown_card.yaml`](./home_assistant/dashboard/markdown_card.yaml) code into the card yaml.

## 📈 Step 6: InfluxDB & Grafana (Optional)

This step is fully optional. If you skip it the rest of the system works exactly as described above. InfluxDB enables long-term time-series storage of your playtime data, and Grafana provides rich dashboards for visualizing it — top games, weekly playtime, session counts and more.

> ℹ️ The queue processor will silently skip InfluxDB writes if the InfluxDB settings are not present in `steam_queue_config.json`. No errors, no impact on normal operation.

### 6.1 Install the Apps

Both add-ons are available directly from the Home Assistant add-on store.

1. Go to **Settings → Apps → Install apps**
2. Search for and install **InfluxDB**
3. Search for and install **Grafana**
4. Start both add-ons and enable **Start on boot** and **Watchdog** for each

### 6.2 Set Up InfluxDB

1. On the InfluxDB apps page click **Open Web UI**
2. Click the **InfluxDB Admin** icon (crown) in the left menu
3. On the **Databases** tab create a database called `steamdeck`
4. On the **Users** tab create a user (e.g. `steamdeck`) and give it **ALL** permissions on the `steamdeck` database

Alternatively create the database via the terminal:

```bash
curl -s -X POST "http://localhost:8086/query?u=YOUR_USER&p=YOUR_PASSWORD" \
  --data-urlencode "q=CREATE DATABASE steamdeck"
```

Verify it was created:

```bash
curl -s -X POST "http://localhost:8086/query?u=YOUR_USER&p=YOUR_PASSWORD" \
  --data-urlencode "q=SHOW DATABASES"
```

### 6.3 Update the Queue Processor Config

As mentioned in 2.5.3, use the config file with the InfluxDB settings included and fill in the needed values for the variables.

Restart the queue processor after saving — either via the watchdog automation or by triggering the startup automation from Developer Tools.

### 6.4 What Gets Stored in InfluxDB

After each gaming session the queue processor writes a data point to the `playtime` measurement with the following data:

**Tags** (indexed, used for filtering):
- `game` — game name
- `game_type` — Steam Native, Non-Steam, ROM, ExoDOS
- `appid` — Steam appid (empty for non-Steam games)

**Fields** (the actual values):
- `total_seconds` — total playtime in seconds after this session
- `session_seconds` — duration of this session in seconds
- `session_count` — running total of how many times this game has been played
- `first_played` — timestamp of the first ever recorded session
- `last_played` — timestamp of when this session ended

**Timestamp** — set to the start time of the session.

### 6.5 Set Up Grafana

1. On the Grafana add-on page click **Open Web UI**
2. Log in with the default credentials (`admin` / `admin`) and set a new password
3. Go to **Connections → Data Sources → Add data source**
4. Select **InfluxDB**
5. Set the URL to `http://a0d7b954-influxdb:8086`
6. Set the database to `steamdeck`, add your InfluxDB username and password and HTTP Method to GET
7. Click **Save & Test** — it should confirm the connection is working

### 6.6 Create the Dashboard

In Grafana go to **Dashboards → New → New Dashboard** and create your own panels. Here are some examples of what I made:


[`**Panel 1 — Top 10 Total Playtime:**`](./home_assistant/Grafana/panel_1.json)

[`**Panel 2 — Time Played Past 7 Days:**`](./home_assistant/Grafana/panel_2.json)

[`**Panel 3 — Playtime Increase Past 7 Days:**`](./home_assistant/Grafana/panel_3.json)

[`**Panel 4 — Top 10 games past 7 days:**`](./home_assistant/Grafana/panel_4.json)

[`**Panel 5 — Top 10 Games All Time:**`](./home_assistant/Grafana/panel_5.json)

