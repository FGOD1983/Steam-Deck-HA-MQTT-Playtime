#!/usr/bin/env python3
"""
Steam Deck Queue Processor
A persistent background service that handles game session tracking,
playtime calculation and InfluxDB recording for the Steam Deck HA integration.

Runs as a background process on Home Assistant OS, started by a HA automation
on homeassistant_start and monitored by a watchdog automation every 5 minutes.

Playtime flow:
  - Properly closed sessions (any game type) are handled exclusively via the
    Deck's local playtime queue, received over MQTT → /process_deck_queue.
    localconfig.vdf is the source of truth:
      total_seconds   = end_playtime * 60
      session_seconds = (end_playtime - start_playtime) * 60
    An MQTT ACK is published per session_id after successful processing so the
    Deck can clean up its local queue.

  - Improperly closed sessions (game → standby/offline) are handled by the
    existing /game_stop flow from the HA automation. These have no localconfig
    data so playtime is calculated as: existing_seconds + session_duration.
    The automation sends start_time from the MQTT queue sensor so the processor
    uses the correct session start rather than the stale HA queue file entry.

  - Sessions marked ha_processed=True in the deck queue were already recorded
    by the HA game_stop automation (standby case). The processor skips the
    library and InfluxDB write but still sends an ACK to clean up the deck queue.

  - The Steam API is no longer used for playtime — localconfig.vdf is faster,
    works offline, and is equally accurate for games played on the Deck.
"""

import json
import logging
import os
import queue
import ssl
import threading
import time as time_module
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta
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
in_flight_lock = threading.Lock()
in_flight_sessions = set()
recently_stopped_games = {}  # game_name_lower → timestamp of game_stop processing

def is_in_flight(session_id):
    with in_flight_lock:
        return session_id in in_flight_sessions

def mark_in_flight(session_id):
    with in_flight_lock:
        in_flight_sessions.add(session_id)

def unmark_in_flight(session_id):
    with in_flight_lock:
        in_flight_sessions.discard(session_id)

def mark_recently_stopped(game_name):
    with in_flight_lock:
        recently_stopped_games[game_name.lower()] = datetime.now().timestamp()

def was_recently_stopped(game_name, within_seconds=120):
    with in_flight_lock:
        ts = recently_stopped_games.get(game_name.lower())
        if ts is None:
            return False
        return (datetime.now().timestamp() - ts) < within_seconds

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
            'seconds':       round(new_seconds, 2),
            'session_count': session_count,
            'first_played':  first_played,
            'last_played':   start_time
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

def to_utc_string(dt_str):
    """
    Convert a datetime string to a UTC ISO string for InfluxDB string fields.

    Grafana interprets string fields without timezone info as UTC and then
    converts to local time on display, adding the local offset. To avoid
    displaying times 2 hours ahead, we store string fields in UTC.

    - If the string has timezone info (e.g. +02:00): convert to UTC directly.
    - If the string is naive (no timezone): assume local time, convert to UTC
      using the system's current UTC offset.
    """
    try:
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is not None:
            # Timezone-aware: convert to UTC
            dt_utc = dt.astimezone(timezone.utc)
        else:
            # Naive: assume local time, apply system UTC offset to convert to UTC
            local_offset_seconds = -(time_module.timezone
                                     if not time_module.daylight
                                     else time_module.altzone)
            local_offset = timedelta(seconds=local_offset_seconds)
            dt_utc = dt - local_offset
        return dt_utc.strftime('%Y-%m-%dT%H:%M:%S')
    except Exception:
        return dt_str  # return as-is if parsing fails

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

    # Build tags dynamically — skip empty appid since InfluxDB rejects empty tag values
    tags = [
        f'game={escape_influx_tag(game_name)}',
        f'game_type={escape_influx_tag(game_type)}',
    ]
    if appid:
        tags.append(f'appid={escape_influx_tag(appid)}')
    tag_str = ','.join(tags)

    # Convert string fields to UTC so Grafana displays correct local time
    field_first_played = escape_influx_string_field(to_utc_string(first_played))
    field_last_played  = escape_influx_string_field(to_utc_string(last_played))

    line = (
        f'playtime,{tag_str} '
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
        log.info(f'InfluxDB line: {line}')
        url = f'{influxdb_url}/write?{params}'
        req = urllib.request.Request(url, data=line.encode('utf-8'), method='POST')
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 204:
                log.info(f'InfluxDB write successful for {game_name}')
            else:
                log.warning(f'InfluxDB write returned unexpected status {resp.status}')
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8')
        log.error(f'InfluxDB write failed for {game_name}: {e} | Body: {body}')
    except Exception as e:
        log.error(f'InfluxDB write failed for {game_name}: {e}')

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
    """
    Process an improperly closed session from the HA automation flow.

    Called when properly_closed=False — the game sensor went to offline/standby.
    The automation sends start_time from the MQTT queue sensor (the actual
    session start on the Deck) and stop_time as the automation trigger time.
    session_seconds = stop_time - start_time (excludes standby periods).
    total_seconds   = existing_seconds + session_seconds.
    """
    game_name       = entry['game_name']
    appid           = entry.get('appid', '')
    game_type       = entry.get('game_type', '')
    properly_closed = entry.get('properly_closed', False)
    entry_id        = entry['entry_id']

    start_time_str = entry.get('start_time')
    stop_time_str  = entry.get('stop_time')

    if not start_time_str or not stop_time_str:
        log.error(f'Missing start_time or stop_time for {game_name}, skipping')
        remove_from_queue_file(entry_id)
        return

    try:
        start_time = datetime.fromisoformat(start_time_str)
        stop_time  = datetime.fromisoformat(stop_time_str)
        # Normalize both to naive datetime to avoid offset-naive vs offset-aware errors.
        # start_time from deck queue is Unix timestamp converted to naive local datetime.
        # stop_time from HA automation may be timezone-aware (e.g. +02:00).
        if start_time.tzinfo is not None:
            start_time = start_time.replace(tzinfo=None)
        if stop_time.tzinfo is not None:
            stop_time = stop_time.replace(tzinfo=None)
    except ValueError as e:
        log.error(f'Invalid datetime format for {game_name}: {e}, skipping')
        remove_from_queue_file(entry_id)
        return

    log.info(f'Processing stop: {game_name} | type={game_type} | properly_closed={properly_closed}')

    if properly_closed:
        log.warning(
            f'{game_name} arrived at process_stop_entry with properly_closed=True — '
            f'expected via deck queue. Falling back to session calculation.'
        )

    session_seconds  = (stop_time - start_time).total_seconds()
    existing_seconds = get_existing_seconds(game_name)
    new_seconds      = existing_seconds + session_seconds

    updated = update_library_entry(
        game_name, new_seconds,
        start_time=start_time.isoformat(),
        stop_time=stop_time.isoformat()
    )
    log.info(f'Improperly closed: {game_name} — {session_seconds}s added to {existing_seconds}s existing')

    write_to_influxdb(
        game_name=game_name,
        appid=appid,
        game_type=game_type,
        total_seconds=new_seconds,
        session_seconds=session_seconds,
        session_count=updated['session_count'],
        first_played=updated['first_played'],
        last_played=updated['last_played'],
        start_time_dt=start_time
    )

    mark_recently_stopped(game_name)
    remove_from_queue_file(entry_id)

def process_deck_session(session):
    """
    Process a single closed session from the Steam Deck local queue.

    ha_processed=True: session was already recorded by the HA game_stop
    automation (standby case). Skip library and InfluxDB write, just ACK.

    ha_processed=False: normal properly closed session.
    Playtime source of truth is localconfig.vdf (values in minutes):
      total_seconds   = end_playtime * 60
      session_seconds = (end_playtime - start_playtime) * 60
    """
    session_id     = session['session_id']
    game_name      = session['name']
    appid          = session.get('appid', '')
    ha_processed   = session.get('ha_processed', False)
    start_playtime = int(session.get('start_playtime', 0))
    end_playtime   = int(session.get('end_playtime', 0))
    start_time     = datetime.fromtimestamp(int(session['start_time']))
    end_time       = datetime.fromtimestamp(int(session['end_time']))

    if ha_processed:
        log.info(
            f'Deck session already processed by HA, skipping write: '
            f'{game_name} [{session_id}]'
        )
        publish_ack(session_id)
        unmark_in_flight(session_id)
        return

    if was_recently_stopped(game_name):
        log.warning(
            f'Deck session for {game_name} [{session_id}] skipped — '
            f'game was recently processed via game_stop (possible duplicate)'
        )
        publish_ack(session_id)
        unmark_in_flight(session_id)
        return

    total_seconds   = end_playtime * 60
    session_seconds = (end_playtime - start_playtime) * 60

    log.info(
        f'Processing deck session: {game_name} | session_id={session_id} | '
        f'start_playtime={start_playtime}m end_playtime={end_playtime}m | '
        f'session={session_seconds}s total={total_seconds}s'
    )

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
    """Single worker thread — processes one queue entry at a time in order."""
    log.info('Worker thread started')
    while True:
        entry = memory_queue.get()
        try:
            if entry.get('_type') == 'deck_session':
                process_deck_session(entry['session'])
            else:
                process_queue_entry(entry)
        except Exception as e:
            session_id = entry.get('session', {}).get('session_id') or entry.get('entry_id')
            log.error(f'Error processing entry {session_id}: {e}')
            if entry.get('_type') == 'deck_session':
                unmark_in_flight(entry['session'].get('session_id', ''))
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
                'queue':              entries,
                'memory_queue_size':  memory_queue.qsize(),
                'in_flight_sessions': list(in_flight_sessions),
                'recently_stopped':   list(recently_stopped_games.keys())
            })
        else:
            self.send_json(404, {'error': 'Not found'})

    def handle_deck_queue(self, data):
        """
        Receives the full playtime queue payload from the Steam Deck via MQTT.

        Closed sessions are processed unless:
          - ha_processed=True: HA already recorded via game_stop, just ACK
          - already in-flight: duplicate, skip
          - recently stopped via game_stop: safety guard, ACK and skip

        Opened sessions are skipped — handled by game_stop or standby flow.
        """
        sessions = data.get('active_sessions', [])
        if not sessions:
            self.send_json(200, {'status': 'ok', 'processed': 0, 'skipped': 0})
            return

        queued  = 0
        skipped = 0
        opened  = 0
        ha_skip = 0

        for session in sessions:
            session_id   = session.get('session_id')
            game_state   = session.get('game_state')
            game_name    = session.get('name', 'unknown')
            ha_processed = session.get('ha_processed', False)

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

            if session.get('end_playtime') is None or session.get('end_time') is None:
                log.warning(f'Closed session {session_id} missing end data, skipping')
                skipped += 1
                continue

            if is_in_flight(session_id):
                log.info(f'Session {session_id} already in-flight, skipping duplicate')
                skipped += 1
                continue

            mark_in_flight(session_id)
            memory_queue.put({'_type': 'deck_session', 'session': session})

            if ha_processed:
                log.info(f'Queued ha_processed session for ACK-only: {game_name} [{session_id}]')
                ha_skip += 1
            else:
                log.info(f'Queued deck session for processing: {game_name} [{session_id}]')
                queued += 1

        log.info(
            f'Deck queue received: {queued} queued, {ha_skip} ha_processed (ACK only), '
            f'{skipped} skipped, {opened} still open'
        )
        self.send_json(200, {
            'status':     'ok',
            'processed':  queued,
            'ha_skip':    ha_skip,
            'skipped':    skipped,
            'still_open': opened
        })

    def handle_game_start(self, data):
        required = ['game_name', 'appid', 'game_type', 'start_time']
        if not all(k in data for k in required):
            self.send_json(400, {'error': f'Missing fields: {required}'})
            return

        entry_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{data['appid']}"

        entry = {
            'entry_id':        entry_id,
            'game_name':       data['game_name'],
            'appid':           data['appid'],
            'game_type':       data['game_type'],
            'state':           'start',
            'start_time':      data['start_time'],
            'stop_time':       None,
            'properly_closed': None
        }

        append_to_queue_file(entry)
        memory_queue.put(entry)

        log.info(f'Game start received: {data["game_name"]} [{entry_id}]')
        self.send_json(200, {'status': 'ok', 'entry_id': entry_id})

    def handle_game_stop(self, data):
        """
        Handles improperly closed sessions (properly_closed=False) from the
        HA game closed automation.

        The automation sends start_time sourced from the MQTT queue sensor
        (the actual session start on the Deck), so we use that directly rather
        than relying on the potentially stale HA queue file entry's start_time.
        """
        required = ['game_name', 'stop_time', 'properly_closed']
        if not all(k in data for k in required):
            self.send_json(400, {'error': f'Missing fields: {required}'})
            return

        game_name       = data['game_name']
        stop_time       = data['stop_time']
        properly_closed = data['properly_closed']
        start_time      = data.get('start_time', stop_time)

        with queue_file_lock:
            entries = read_queue_file()
            matched = None
            for i, e in enumerate(entries):
                if (e.get('state') == 'start'
                        and e.get('game_name', '').lower() == game_name.lower()):
                    entries[i]['state']          = 'stop'
                    entries[i]['stop_time']       = stop_time
                    entries[i]['start_time']      = start_time
                    entries[i]['properly_closed'] = properly_closed
                    matched = entries[i]
                    break

            if matched:
                write_queue_file(entries)
                memory_queue.put(matched)
                log.info(
                    f'Game stop received: {game_name} | properly_closed={properly_closed} | '
                    f'start={start_time} stop={stop_time}'
                )
                self.send_json(200, {'status': 'ok', 'entry_id': matched['entry_id']})
            else:
                log.warning(
                    f'No matching start entry for stop: {game_name} — '
                    f'creating minimal stop entry'
                )
                entry_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_nostartmatch"
                fallback_entry = {
                    'entry_id':        entry_id,
                    'game_name':       game_name,
                    'appid':           data.get('appid', ''),
                    'game_type':       data.get('game_type', ''),
                    'state':           'stop',
                    'start_time':      start_time,
                    'stop_time':       stop_time,
                    'properly_closed': properly_closed
                }
                entries.append(fallback_entry)
                write_queue_file(entries)
                memory_queue.put(fallback_entry)
                self.send_json(200, {'status': 'ok', 'entry_id': entry_id})

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
