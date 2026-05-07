#!/usr/bin/env python3
"""
Steam Deck Queue Processor
A persistent background service that handles game session tracking,
playtime calculation, Steam API sync and InfluxDB recording for the
Steam Deck HA integration.

Runs as a background process on Home Assistant OS, started by a HA automation
on homeassistant_start and monitored by a watchdog automation every 5 minutes.

New in this version:
  - /process_deck_queue endpoint receives the playtime queue from the Steam Deck
    via MQTT → HA automation → HTTP POST
  - Processes closed sessions using localconfig.vdf playtime as source of truth
    (end_playtime * 60 = total seconds, (end_playtime - start_playtime) * 60 = session seconds)
  - Publishes MQTT ACK per session_id after successful processing
  - Tracks in-flight session_ids to prevent double processing
"""

import json
import logging
import os
import queue
import ssl
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import paho.mqtt.client as mqtt_client

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('/config/scripts/steam_queue_processor.log'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
CONFIG_FILE = '/config/scripts/steam_queue_config.json'
config = {}

def load_config():
    global config
    try:
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)
        log.info('Config loaded')
    except Exception as e:
        log.error(f"Failed to load config: {e}")
        exit(1)

# ── In-flight session tracking (prevents double processing) ───────────────────
# Holds session_ids currently being processed or already ACK'd this run.
# This is in-memory only — on restart it starts fresh, which is fine since
# the Deck queue will resend any unACK'd sessions and they will be reprocessed.
in_flight_lock = threading.Lock()
in_flight_sessions = set()

def is_in_flight(session_id):
    with in_flight_lock:
        return session_id in in_flight_sessions

def mark_in_flight(session_id):
    with in_flight_lock:
        in_flight_sessions.add(session_id)

def unmark_in_flight(session_id):
    with in_flight_lock:
        in_flight_sessions.discard(session_id)

# ── Queue file helpers ─────────────────────────────────────────────────────────
queue_file_lock = threading.Lock()

def read_queue_file():
    path = config['queue_file']
    if not os.path.exists(path):
        return []
    with open(path, 'r') as f:
        try:
            data = json.load(f)
            return data.get('queue', [])
        except json.JSONDecodeError:
            log.warning('Queue file is malformed, starting fresh')
            return []

def write_queue_file(entries):
    path = config['queue_file']
    tmp_path = path + '.tmp'
    with open(tmp_path, 'w') as f:
        json.dump({'queue': entries}, f, indent=2)
    os.replace(tmp_path, path)

def append_to_queue_file(entry):
    with queue_file_lock:
        entries = read_queue_file()
        entries.append(entry)
        write_queue_file(entries)
    log.info(f'Appended entry to queue file: {entry["game_name"]} [{entry["state"]}]')

def remove_from_queue_file(entry_id):
    with queue_file_lock:
        entries = read_queue_file()
        entries = [e for e in entries if e.get('entry_id') != entry_id]
        write_queue_file(entries)
    log.info(f'Removed entry {entry_id} from queue file')

# ── HA REST API helpers ────────────────────────────────────────────────────────
def ha_get(path):
    url = config['ha_url'] + '/api/' + path
    req = urllib.request.Request(url, headers={
        'Authorization': 'Bearer ' + config['ha_token'],
        'Content-Type': 'application/json'
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())

def get_ha_state(entity_id):
    result = ha_get(f'states/{entity_id}')
    return result.get('state', '')

def get_steam_api_key():
    return get_ha_state('input_text.steam_api_key')

def get_steam_user_id():
    return get_ha_state('input_text.steam_user_id')

# ── Library file helpers ───────────────────────────────────────────────────────
library_file_lock = threading.Lock()

def read_library():
    path = config['library_file']
    if not os.path.exists(path):
        return {}
    with open(path, 'r') as f:
        try:
            data = json.load(f)
            return data.get('games', {})
        except json.JSONDecodeError:
            log.error('Library file is malformed')
            return {}

def write_library(games):
    path = config['library_file']
    tmp_path = path + '.tmp'
    with open(tmp_path, 'w') as f:
        json.dump({'games': games}, f, indent=2)
    os.replace(tmp_path, path)
    log.info('Library file updated')

def update_library_entry(game_name, new_seconds, start_time, stop_time):
    """
    Update a single game entry in the library under lock.
    Returns the updated entry dict for use in InfluxDB write.
    """
    with library_file_lock:
        games = read_library()

        matched_key = game_name
        existing_entry = {}
        for key in games.keys():
            if key.lower() == game_name.lower():
                matched_key = key
                existing_entry = games[key] if isinstance(games[key], dict) else {}
                break

        first_played = existing_entry.get('first_played')
        if not first_played:
            first_played = existing_entry.get('last_played', start_time)

        session_count = existing_entry.get('session_count', 0) + 1

        updated_entry = {
            'seconds': round(new_seconds, 2),
            'session_count': session_count,
            'first_played': first_played,
            'last_played': start_time
        }
        games[matched_key] = updated_entry
        write_library(games)
        log.info(f'Updated library entry for {matched_key}: {round(new_seconds)}s | sessions={session_count}')
        return updated_entry

def get_existing_seconds(game_name):
    games = read_library()
    for key, value in games.items():
        if key.lower() == game_name.lower():
            if isinstance(value, dict):
                return float(value.get('seconds', 0))
            return float(value)
    return 0.0

# ── InfluxDB helpers ───────────────────────────────────────────────────────────
def escape_influx_tag(value):
    return str(value).replace(',', r'\,').replace(' ', r'\ ').replace('=', r'\=')

def escape_influx_string_field(value):
    return str(value).replace('"', r'\"')

def write_to_influxdb(game_name, appid, game_type, total_seconds, session_seconds,
                       session_count, first_played, last_played, start_time_dt):
    influxdb_url      = config.get('influxdb_url')
    influxdb_db       = config.get('influxdb_db')
    influxdb_user     = config.get('influxdb_user', '')
    influxdb_password = config.get('influxdb_password', '')

    if not influxdb_url or not influxdb_db:
        log.warning('InfluxDB not configured, skipping write')
        return

    timestamp_ns = int(start_time_dt.timestamp() * 1_000_000_000)

    tag_game      = escape_influx_tag(game_name)
    tag_game_type = escape_influx_tag(game_type)
    tag_appid     = escape_influx_tag(appid if appid else '')

    field_first_played = escape_influx_string_field(first_played)
    field_last_played  = escape_influx_string_field(last_played)

    line = (
        f'playtime,'
        f'game={tag_game},'
        f'game_type={tag_game_type},'
        f'appid={tag_appid} '
        f'total_seconds={round(total_seconds, 2)},'
        f'session_seconds={round(session_seconds, 2)},'
        f'session_count={session_count}i,'
        f'first_played="{field_first_played}",'
        f'last_played="{field_last_played}" '
        f'{timestamp_ns}'
    )

    try:
        params = urllib.parse.urlencode({
            'db': influxdb_db,
            'u': influxdb_user,
            'p': influxdb_password
        })
        url = f'{influxdb_url}/write?{params}'
        req = urllib.request.Request(url, data=line.encode('utf-8'), method='POST')
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 204:
                log.info(f'InfluxDB write successful for {game_name}')
            else:
                log.warning(f'InfluxDB write returned unexpected status {resp.status}')
    except Exception as e:
        log.error(f'InfluxDB write failed for {game_name}: {e}')

# ── Steam API helpers ──────────────────────────────────────────────────────────
def fetch_steam_playtime(appid):
    api_key  = get_steam_api_key()
    steam_id = get_steam_user_id()

    if not api_key or not steam_id:
        log.error('Steam API key or user ID not set in HA helpers')
        return None

    url = (
        f'https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/'
        f'?key={api_key}&steamid={steam_id}&include_appinfo=1'
        f'&include_played_free_games=1&format=json'
    )
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data   = json.loads(resp.read())
            games  = data.get('response', {}).get('games', [])
            for game in games:
                if game.get('appid') == int(appid):
                    minutes = game.get('playtime_forever', 0)
                    return minutes * 60
    except Exception as e:
        log.error(f'Error fetching Steam playtime: {e}')
    return None

# ── MQTT ACK publisher ─────────────────────────────────────────────────────────
def publish_ack(session_id):
    """
    Publish a retained ACK message to steamdeck/playtime/ack/<session_id>
    so the Steam Deck script knows this session has been processed.
    """
    mqtt_host = config.get('mqtt_host')
    mqtt_port = int(config.get('mqtt_port', 8883))
    mqtt_user = config.get('mqtt_user')
    mqtt_pass = config.get('mqtt_pass')

    if not mqtt_host:
        log.warning('MQTT not configured in config, skipping ACK publish')
        return

    topic = f"steamdeck/playtime/ack/{session_id}"
    try:
        client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2)
        client.username_pw_set(mqtt_user, mqtt_pass)
        client.tls_set(cert_reqs=ssl.CERT_NONE)
        client.tls_insecure_set(True)
        client.connect(mqtt_host, mqtt_port, keepalive=30)
        client.loop_start()
        result = client.publish(topic, payload=session_id, retain=True)
        result.wait_for_publish(timeout=5)
        client.loop_stop()
        client.disconnect()
        log.info(f'ACK published for session {session_id} → {topic}')
    except Exception as e:
        log.error(f'Failed to publish ACK for session {session_id}: {e}')

# ── Processing logic ───────────────────────────────────────────────────────────
def process_stop_entry(entry):
    """Process a stop entry from the existing HA automation flow."""
    game_name       = entry['game_name']
    appid           = entry.get('appid', '')
    game_type       = entry.get('game_type', '')
    properly_closed = entry.get('properly_closed', False)
    start_time      = datetime.fromisoformat(entry['start_time'])
    stop_time       = datetime.fromisoformat(entry['stop_time'])
    entry_id        = entry['entry_id']

    log.info(f'Processing stop: {game_name} | type={game_type} | properly_closed={properly_closed}')

    session_seconds = (stop_time - start_time).total_seconds()

    if game_type == 'Steam Native' and properly_closed:
        delay = config.get('steam_api_delay', 180)
        log.info(f'Waiting {delay}s for Steam API to update for {game_name}...')
        time.sleep(delay)

        steam_seconds = fetch_steam_playtime(appid)
        if steam_seconds is not None:
            updated = update_library_entry(
                game_name, steam_seconds,
                start_time=start_time.isoformat(),
                stop_time=stop_time.isoformat()
            )
            log.info(f'Steam API playtime for {game_name}: {steam_seconds}s')
        else:
            log.warning(f'Steam API fetch failed for {game_name}, falling back to session calculation')
            existing_seconds = get_existing_seconds(game_name)
            new_seconds      = existing_seconds + session_seconds
            updated = update_library_entry(
                game_name, new_seconds,
                start_time=start_time.isoformat(),
                stop_time=stop_time.isoformat()
            )
    else:
        existing_seconds = get_existing_seconds(game_name)
        new_seconds      = existing_seconds + session_seconds
        updated = update_library_entry(
            game_name, new_seconds,
            start_time=start_time.isoformat(),
            stop_time=stop_time.isoformat()
        )
        log.info(f'Session playtime for {game_name}: {session_seconds}s added to {existing_seconds}s existing')

    write_to_influxdb(
        game_name=game_name,
        appid=appid,
        game_type=game_type,
        total_seconds=updated['seconds'],
        session_seconds=session_seconds,
        session_count=updated['session_count'],
        first_played=updated['first_played'],
        last_played=updated['last_played'],
        start_time_dt=start_time
    )

    remove_from_queue_file(entry_id)

def process_deck_session(session):
    """
    Process a single closed session from the Steam Deck local queue.

    Playtime source of truth is localconfig.vdf (values in minutes):
      total_seconds   = end_playtime * 60
      session_seconds = (end_playtime - start_playtime) * 60

    After successful processing, publishes an MQTT ACK so the Deck
    can remove this session from its local queue.
    """
    session_id    = session['session_id']
    game_name     = session['name']
    appid         = session.get('appid', '')
    start_playtime = int(session.get('start_playtime', 0))
    end_playtime   = int(session.get('end_playtime', 0))
    start_time     = datetime.fromtimestamp(int(session['start_time']))
    end_time       = datetime.fromtimestamp(int(session['end_time']))

    total_seconds   = end_playtime * 60
    session_seconds = (end_playtime - start_playtime) * 60

    log.info(
        f'Processing deck session: {game_name} | session_id={session_id} | '
        f'start_playtime={start_playtime}m end_playtime={end_playtime}m | '
        f'session={session_seconds}s total={total_seconds}s'
    )

    # Determine game type — appid < 0x80000000 is Steam Native
    try:
        game_type = 'Steam Native' if appid and int(appid) < 0x80000000 else 'Non-Steam'
    except (ValueError, TypeError):
        game_type = 'Non-Steam'

    updated = update_library_entry(
        game_name, total_seconds,
        start_time=start_time.isoformat(),
        stop_time=end_time.isoformat()
    )

    write_to_influxdb(
        game_name=game_name,
        appid=appid,
        game_type=game_type,
        total_seconds=total_seconds,
        session_seconds=session_seconds,
        session_count=updated['session_count'],
        first_played=updated['first_played'],
        last_played=updated['last_played'],
        start_time_dt=start_time
    )

    # Publish MQTT ACK — Deck will remove this session from its local queue
    publish_ack(session_id)
    unmark_in_flight(session_id)
    log.info(f'Deck session processed and ACK sent: {game_name} [{session_id}]')

def process_queue_entry(entry):
    """Route a queue entry to the correct handler based on state."""
    state = entry.get('state')
    if state == 'stop':
        process_stop_entry(entry)
    elif state == 'start':
        log.info(f'Start entry for {entry["game_name"]} acknowledged, no processing needed')
    else:
        log.warning(f'Unknown entry state: {state}')

# ── Worker thread ──────────────────────────────────────────────────────────────
memory_queue = queue.Queue()

def worker():
    log.info('Worker thread started')
    while True:
        entry = memory_queue.get()
        try:
            process_queue_entry(entry)
        except Exception as e:
            log.error(f'Error processing entry {entry.get("entry_id")}: {e}')
        finally:
            memory_queue.task_done()

# ── HTTP request handler ───────────────────────────────────────────────────────
class RequestHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        log.info(f'HTTP {format % args}')

    def send_json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        length = int(self.headers.get('Content-Length', 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def do_POST(self):
        data = self.read_body()
        if self.path == '/game_start':
            self.handle_game_start(data)
        elif self.path == '/game_stop':
            self.handle_game_stop(data)
        elif self.path == '/process_deck_queue':
            self.handle_deck_queue(data)
        else:
            self.send_json(404, {'error': 'Not found'})

    def do_GET(self):
        if self.path == '/status':
            entries = read_queue_file()
            self.send_json(200, {
                'queue': entries,
                'memory_queue_size': memory_queue.qsize(),
                'in_flight_sessions': list(in_flight_sessions)
            })
        else:
            self.send_json(404, {'error': 'Not found'})

    def handle_deck_queue(self, data):
        """
        Receives the full playtime queue payload from the Steam Deck via MQTT.
        Processes all closed sessions that are not already in-flight.
        Opened sessions are logged but not processed — they will be handled
        by the existing game_stop flow or by the standby/offline fallback.
        """
        sessions = data.get('active_sessions', [])
        if not sessions:
            self.send_json(200, {'status': 'ok', 'processed': 0, 'skipped': 0})
            return

        queued    = 0
        skipped   = 0
        opened    = 0

        for session in sessions:
            session_id  = session.get('session_id')
            game_state  = session.get('game_state')
            game_name   = session.get('name', 'unknown')

            if not session_id:
                log.warning('Session missing session_id, skipping')
                skipped += 1
                continue

            if game_state == 'opened':
                log.info(f'Deck session still open, skipping: {game_name} [{session_id}]')
                opened += 1
                continue

            if game_state != 'closed':
                log.warning(f'Unknown game_state "{game_state}" for session {session_id}, skipping')
                skipped += 1
                continue

            # Validate required fields for a closed session
            if session.get('end_playtime') is None or session.get('end_time') is None:
                log.warning(f'Closed session {session_id} missing end data, skipping')
                skipped += 1
                continue

            # Skip if already being processed or already processed this run
            if is_in_flight(session_id):
                log.info(f'Session {session_id} already in-flight, skipping duplicate')
                skipped += 1
                continue

            # Mark as in-flight and queue for processing
            mark_in_flight(session_id)
            memory_queue.put({'_type': 'deck_session', 'session': session})
            log.info(f'Queued deck session for processing: {game_name} [{session_id}]')
            queued += 1

        log.info(f'Deck queue received: {queued} queued, {skipped} skipped, {opened} still open')
        self.send_json(200, {
            'status': 'ok',
            'processed': queued,
            'skipped': skipped,
            'still_open': opened
        })

    def handle_game_start(self, data):
        required = ['game_name', 'appid', 'game_type', 'start_time']
        if not all(k in data for k in required):
            self.send_json(400, {'error': f'Missing fields: {required}'})
            return

        entry_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{data['appid']}"

        with queue_file_lock:
            entries = read_queue_file()
            for existing in entries:
                if existing.get('state') == 'start' and existing.get('stop_time') is None:
                    log.info(f'Game swap detected — synthesizing stop for {existing["game_name"]}')
                    stop_entry = dict(existing)
                    stop_entry['state'] = 'stop'
                    stop_entry['stop_time'] = data['start_time']
                    stop_entry['properly_closed'] = existing.get('game_type') == 'Steam Native'
                    entries = [e for e in entries if e.get('entry_id') != existing['entry_id']]
                    entries.append(stop_entry)
                    write_queue_file(entries)
                    memory_queue.put(stop_entry)
                    break

        entry = {
            'entry_id':       entry_id,
            'game_name':      data['game_name'],
            'appid':          data['appid'],
            'game_type':      data['game_type'],
            'state':          'start',
            'start_time':     data['start_time'],
            'stop_time':      None,
            'properly_closed': None
        }

        append_to_queue_file(entry)
        memory_queue.put(entry)

        log.info(f'Game start received: {data["game_name"]} [{entry_id}]')
        self.send_json(200, {'status': 'ok', 'entry_id': entry_id})

    def handle_game_stop(self, data):
        required = ['game_name', 'stop_time', 'properly_closed']
        if not all(k in data for k in required):
            self.send_json(400, {'error': f'Missing fields: {required}'})
            return

        with queue_file_lock:
            entries = read_queue_file()
            matched = None
            for i, e in enumerate(entries):
                if (e.get('state') == 'start'
                        and e.get('game_name', '').lower() == data['game_name'].lower()):
                    entries[i]['state']          = 'stop'
                    entries[i]['stop_time']       = data['stop_time']
                    entries[i]['properly_closed'] = data['properly_closed']
                    matched = entries[i]
                    break
            if matched:
                write_queue_file(entries)
                memory_queue.put(matched)
                log.info(f'Game stop received: {data["game_name"]} | properly_closed={data["properly_closed"]}')
                self.send_json(200, {'status': 'ok', 'entry_id': matched['entry_id']})
            else:
                log.warning(f'No matching start entry found for stop: {data["game_name"]}')
                self.send_json(404, {'error': 'No matching start entry found'})

# ── Updated worker to handle deck sessions ─────────────────────────────────────
def worker():
    """Single worker thread — processes one queue entry at a time in order."""
    log.info('Worker thread started')
    while True:
        entry = memory_queue.get()
        try:
            # Deck session entries are wrapped with _type key
            if entry.get('_type') == 'deck_session':
                process_deck_session(entry['session'])
            else:
                process_queue_entry(entry)
        except Exception as e:
            session_id = entry.get('session', {}).get('session_id') or entry.get('entry_id')
            log.error(f'Error processing entry {session_id}: {e}')
            # Unmark in-flight on error so it can be retried on next Deck push
            if entry.get('_type') == 'deck_session':
                unmark_in_flight(entry['session'].get('session_id', ''))
        finally:
            memory_queue.task_done()

# ── Startup ────────────────────────────────────────────────────────────────────
def recover_unprocessed_entries():
    entries      = read_queue_file()
    stop_entries = [e for e in entries if e.get('state') == 'stop']
    if stop_entries:
        log.info(f'Recovering {len(stop_entries)} unprocessed stop entries from queue file')
        for entry in stop_entries:
            memory_queue.put(entry)
    start_entries = [e for e in entries if e.get('state') == 'start']
    if start_entries:
        log.info(f'{len(start_entries)} unfinished start entries found (games open at shutdown)')

def main():
    load_config()

    os.makedirs(os.path.dirname(config['queue_file']), exist_ok=True)

    if not os.path.exists(config['queue_file']):
        write_queue_file([])
        log.info('Created empty queue file')

    recover_unprocessed_entries()

    worker_thread = threading.Thread(target=worker, daemon=True)
    worker_thread.start()

    class ReusableHTTPServer(HTTPServer):
        allow_reuse_address = True

    port   = config.get('port', 8098)
    server = ReusableHTTPServer(('127.0.0.1', port), RequestHandler)
    log.info(f'Steam queue processor listening on http://127.0.0.1:{port}')

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info('Shutting down')
        server.shutdown()

if __name__ == '__main__':
    main()
