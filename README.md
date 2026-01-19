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

By using a Python script on the Steam Deck and a "Session Lock" logic in Home Assistant, your playtime data remains accurate even if Home Assistant reboots during a gaming session.

*Note: This system requires an active local network connection between your Steam Deck and your MQTT broker to function.*

## 🛠 Prerequisites
* MQTT Broker: A running broker (like Mosquitto) integrated with Home Assistant.  
* Steam Deck: Access to Desktop Mode and a terminal (Konsole).  
* Home Assistant Helpers: Three helpers (Boolean, Datetime, and Text) to manage the session state.

## 🎮 [Step 1: Steam Deck Setup](./steam_deck/)
1. Install dependencies

Open Konsole in Desktop Mode and run the following to prepare the environment:

```Bash

# Create a folder for the scripts
mkdir -p ~/scripts

# Set up a Python Virtual Environment to keep the system clean
python -m venv ~/mqtt-env
~/mqtt-env/bin/pip install paho-mqtt requests psutil
```
2. Create the Script

Create a file called `/home/deck/scripts/steamdeck_mqtt_sensors.py` and paste the provided [Python script](./steam_deck/scripts/steamdeck_mqtt_sensors.py). Don't forget to edit variables at the beginning of the script (MQTT_HOST, MQTT_USER, MQTT_PASS).


3. Auto-start with Systemd
Create a file called `.config/systemd/user/steamdeck_mqtt_update.service` and copy the [update service code](./steam_deck/services/steamdeck_mqtt_update.service) into the file.

Also create the timer service `.config/systemd/user/steamdeck_mqtt_update.timer` and copy the [timer service code](./steam_deck/services/steamdeck_mqtt_update.timer) into the file.

In order to make it respond correctly at boot or sleep/wake we also need to create services for that.

Create a file called `.config/systemd/user/steamdeck_mqtt_boot.service` and copy the [boot service code](./steam_deck/services/steamdeck_mqtt_boot.service) into the file.
Create a file called ``.config/systemd/user/steamdeck_mqtt_offline.service`` and copy the [offline service code](./steam_deck/services/steamdeck_mqtt_offline.service) into the file.

## 🏠 Step 2: Home Assistant Setup
This part of the setup handles the incoming data, manages the session logic, and ensures everything is saved correctly to a local JSON database.

1. Enable File Writing
To allow Home Assistant to write to your local storage, add [the following code](./home_assistant/configuration.yaml) to your `configuration.yaml`. This is required for the `shell_command` to work.

2. Create Helpers (UI)
Go to **Settings > Devices & Services > Helpers** and create these three entities:

* **Input Boolean**: `input_boolean.steam_deck_sessie_actief` (Steam Deck Sessie Actief)
* **Input Datetime**: `input_datetime.steam_deck_sessie_starttijd` (Steam Deck Sessie Starttijd)
* **Input Text**: `input_text.steam_deck_huidige_game` (Steam Deck Huidige Game)

3. Sensors & Shell Command
Now we need to build the sensors and the needed shell command for the data. These can be created in the `configuration.yaml` or in their own seperate yaml file which should then be included in the `configuration.yaml`. 

Let's first do the MQTT sensors inside [`mqtt.yaml`](./home_assistant/sensors/mqtt.yaml). Copy the code inside your Home Assistant mqtt.yaml file.

Next up is the shell command. This can be put inside the `configuration.yaml`. Copy the code from [`the shell_commands.yaml`](./home_assistant/shell_commands.yaml) into the `configuration.yaml` file.

Now let's do the same for the REST Sensor [REST Sensor](./home_assistant/sensors/sensors.yaml). Copy that code into your `sensors.yaml` file

And for the last sensors, you will need to copy the [`templates.yaml`](./home_assistant/sensors/templates.yaml) file content to your `templates.yaml` file on Home Assistant.

4. The Library Database (JSON)
For the system to store your data, you need to create an initial empty library file.

Use a File Editor or SSH to go to your `/homeassistant/www/` folder (which is basically your `/config/www` folder).

Create a file there named `steam_library.json` and copy in the template data from the [`steam_library.json`](./home_assistant/www/steam_library.json) file from this repo.

5. The Core Automation

This automation is the "Brain". It manages the session, calculates the time, and triggers the save command. It is reboot-proof: if Home Assistant restarts during a session, the input_boolean ensures the session remains active, and the input_text remembers which game you were playing.

You can create a new Home Automation, switch to yaml mode and paste in the [`automations.yaml`](./home_assistant/automation.yaml) code into your new automation and save it.

## 📊 Step 3: Visualizing the Data
To read the data back into Home Assistant for your dashboard, you can use 2 cards. The first one I use is the picture card which uses [`steamdeck.png`](./home_assistant/www/steamdeck.png). you will need to upload this file into your `www` where you also created the `steam_library.json` file. 

When that is done you can go to your dashboard and create a new card with the [`picture_card.yaml`](./home_assistant/dashboard/picture_card.yaml) code.

The second card I use is the Markdown card which displays the top 5 most played games with theit total playtime and the last 5 played games with their day and time they have been played last.

You can again create a new card on your dashboard and copy the [`markdown_card.yaml`](./home_assistant/dashboard/markdown_card.yaml) code into the card yaml.
