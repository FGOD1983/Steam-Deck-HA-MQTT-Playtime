#!/usr/bin/env python3
"""
Steam Deck Queue Processor
A persistent background service that handles game session tracking,
playtime calculation and Steam API sync for the Steam Deck HA integration.

Runs as a background process on Home Assistant OS, started by a HA automation
on homeassistant_start and monitored by a watchdog automation every 5 minutes.
"""

import json
import logging
import os
import queue
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

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
    with open(CONFIG_FILE, 'r') as f:
        config = json.load(f)
    log.info('Config loaded')

# ── Queue file helpers ─────────────────────────────────────────────────────────
queue_file_lock = threading.Lock()

def read_queue_file():
    """Read the queue file, return list of entries. Returns [] if file missing or empty."""
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
    """Write entries to queue file atomically using a temp file."""
    path = config['queue_file']
    tmp_path = path + '.tmp'
    with open(tmp_path, 'w') as f:
        json.dump({'queue': entries}, f, indent=2)
    os.replace(tmp_path, path)

def append_to_queue_file(entry):
    """Append a new entry to the queue file under lock."""
    with queue_file_lock:
        entries = read_queue_file()
        entries.append(entry)
        write_queue_file(entries)
    log.info(f'Appended entry to queue file: {entry["game_name"]} [{entry["state"]}]')

def remove_from_queue_file(entry_id):
    """Remove a processed entry from the queue file under lock."""
    with queue_file_lock:
        entries = read_queue_file()
        entries = [e for e in entries if e.get('entry_id') != entry_id]
        write_queue_file(entries)
    log.info(f'Removed entry {entry_id} from queue file')

# ── HA REST API helpers ────────────────────────────────────────────────────────
def ha_get(path):
    """GET request to HA REST API."""
    url = config['ha_url'] + '/api/' + path
    req = urllib.request.Request(url, headers={
        'Authorization': 'Bearer ' + config['ha_token'],
        'Content-Type': 'application/json'
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())

def get_ha_state(entity_id):
    """Get the state of a HA entity."""
    result = ha_get(f'states/{entity_id}')
    return result.get('state', '')

def get_steam_api_key():
    return get_ha_state('input_text.steam_api_key')

def get_steam_user_id():
    return get_ha_state('input_text.steam_user_id')

# ── Library file helpers ───────────────────────────────────────────────────────
library_file_lock = threading.Lock()

def read_library():
    """Read steam_library.json, return games dict."""
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
    """Write games dict to steam_library.json atomically."""
    path = config['library_file']
    tmp_path = path + '.tmp'
    with open(tmp_path, 'w') as f:
        json.dump({'games': games}, f, indent=2)
    os.replace(tmp_path, path)
    log.info('Library file updated')

def update_library_entry(game_name, new_seconds, last_played):
    """Update a single game entry in the library under lock."""
    with library_file_lock:
        games = read_library()

        # Case-insensitive match for existing entry
        matched_key = game_name
        for key in games.keys():
            if key.lower() == game_name.lower():
                matched_key = key
                break

        games[matched_key] = {
            'seconds': round(new_seconds, 2),
            'last_played': last_played
        }
        write_library(games)
        log.info(f'Updated library entry for {matched_key}: {round(new_seconds)}s')

def get_existing_seconds(game_name):
    """Get existing playtime seconds for a game, 0 if not found."""
    games = read_library()
    for key, value in games.items():
        if key.lower() == game_name.lower():
            if isinstance(value, dict):
                return float(value.get('seconds', 0))
            return float(value)
    return 0.0

# ── Steam API helpers ──────────────────────────────────────────────────────────
def fetch_steam_playtime(appid):
    """
    Fetch official total playtime in seconds from Steam API for a given appid.
    Returns None if not found or on error.
    """
    api_key = get_steam_api_key()
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
            data = json.loads(resp.read())
            games = data.get('response', {}).get('games', [])
            for game in games:
                if game.get('appid') == int(appid):
                    minutes = game.get('playtime_forever', 0)
                    return minutes * 60  # convert to seconds
    except Exception as e:
        log.error(f'Error fetching Steam playtime: {e}')
    return None

# ── Processing logic ───────────────────────────────────────────────────────────
def process_stop_entry(entry):
    """Process a stop entry — calculate or fetch playtime and update library."""
    game_name = entry['game_name']
    appid = entry.get('appid', '')
    game_type = entry.get('game_type', '')
    properly_closed = entry.get('properly_closed', False)
    start_time = datetime.fromisoformat(entry['start_time'])
    stop_time = datetime.fromisoformat(entry['stop_time'])
    entry_id = entry['entry_id']

    log.info(f'Processing stop: {game_name} | type={game_type} | properly_closed={properly_closed}')

    if game_type == 'Steam Native' and properly_closed:
        # Properly closed Steam Native game — wait for Steam servers to update
        # then fetch official playtime. This covers both normal closes and game
        # swaps, since a swap means the game was properly closed on the Deck but
        # the script missed the No game opened state in between.
        delay = config.get('steam_api_delay', 180)
        log.info(f'Waiting {delay}s for Steam API to update for {game_name}...')
        time.sleep(delay)

        steam_seconds = fetch_steam_playtime(appid)
        if steam_seconds is not None:
            update_library_entry(game_name, steam_seconds, start_time.isoformat())
            log.info(f'Steam API playtime for {game_name}: {steam_seconds}s')
        else:
            # Fall back to session calculation if Steam API fails
            log.warning(f'Steam API fetch failed for {game_name}, falling back to session calculation')
            session_seconds = (stop_time - start_time).total_seconds()
            existing_seconds = get_existing_seconds(game_name)
            update_library_entry(game_name, existing_seconds + session_seconds, start_time.isoformat())
    else:
        # Non-Steam Native or went to standby/offline — calculate from timestamps.
        # Steam Native games only reach here if properly_closed=False, meaning the
        # Deck went to standby or the script crashed with the game open.
        session_seconds = (stop_time - start_time).total_seconds()
        existing_seconds = get_existing_seconds(game_name)
        new_seconds = existing_seconds + session_seconds
        update_library_entry(game_name, new_seconds, start_time.isoformat())
        log.info(f'Session playtime for {game_name}: {session_seconds}s added to {existing_seconds}s existing')

    remove_from_queue_file(entry_id)

def process_queue_entry(entry):
    """Route a queue entry to the correct handler based on state."""
    state = entry.get('state')
    if state == 'stop':
        process_stop_entry(entry)
    elif state == 'start':
        # Start entries are persisted for crash recovery but need no processing
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
        else:
            self.send_json(404, {'error': 'Not found'})

    def do_GET(self):
        if self.path == '/status':
            entries = read_queue_file()
            self.send_json(200, {
                'queue': entries,
                'memory_queue_size': memory_queue.qsize()
            })
        else:
            self.send_json(404, {'error': 'Not found'})

    def handle_game_start(self, data):
        required = ['game_name', 'appid', 'game_type', 'start_time']
        if not all(k in data for k in required):
            self.send_json(400, {'error': f'Missing fields: {required}'})
            return

        entry_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{data['appid']}"

        # Check for unmatched start entry — means game was swapped without
        # a No game opened state in between. Synthesize a stop for the previous
        # game. For Steam Native games set properly_closed=True since the game
        # was closed on the Deck, the script just missed the transition.
        # For non-Steam games set properly_closed=False and use session calculation.
        with queue_file_lock:
            entries = read_queue_file()
            for existing in entries:
                if existing.get('state') == 'start' and existing.get('stop_time') is None:
                    log.info(f'Game swap detected — synthesizing stop for {existing["game_name"]}')
                    log.debug(f'Swap entry full dump: {json.dumps(existing)}')
                    log.info(f'Swap entry game_type: "{existing.get("game_type")}" | repr: {repr(existing.get("game_type"))} | properly_closed will be: {existing.get("game_type") == "Steam Native"}')
                    stop_entry = dict(existing)
                    stop_entry['state'] = 'stop'
                    stop_entry['stop_time'] = data['start_time']
                    # Steam Native: properly closed, script just missed the transition
                    # Non-Steam: use session calculation
                    stop_entry['properly_closed'] = existing.get('game_type') == 'Steam Native'
                    log.info(f'Stop entry properly_closed set to: {stop_entry["properly_closed"]}')
                    entries = [e for e in entries if e.get('entry_id') != existing['entry_id']]
                    entries.append(stop_entry)
                    write_queue_file(entries)
                    memory_queue.put(stop_entry)
                    break

        entry = {
            'entry_id': entry_id,
            'game_name': data['game_name'],
            'appid': data['appid'],
            'game_type': data['game_type'],
            'state': 'start',
            'start_time': data['start_time'],
            'stop_time': None,
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

        # Find the matching start entry and update it to stop
        with queue_file_lock:
            entries = read_queue_file()
            matched = None
            for i, e in enumerate(entries):
                if (e.get('state') == 'start'
                        and e.get('game_name', '').lower() == data['game_name'].lower()):
                    entries[i]['state'] = 'stop'
                    entries[i]['stop_time'] = data['stop_time']
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

# ── Startup ────────────────────────────────────────────────────────────────────
def recover_unprocessed_entries():
    """On startup push any existing stop entries to the memory queue for processing."""
    entries = read_queue_file()
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

    # Ensure scripts directory exists
    os.makedirs(os.path.dirname(config['queue_file']), exist_ok=True)

    # Create empty queue file if it does not exist
    if not os.path.exists(config['queue_file']):
        write_queue_file([])
        log.info('Created empty queue file')

    # Recover any unprocessed entries from before restart
    recover_unprocessed_entries()

    # Start single worker thread
    worker_thread = threading.Thread(target=worker, daemon=True)
    worker_thread.start()

    # Start HTTP server on localhost only — not accessible externally
    # ReusableHTTPServer prevents "Address in use" errors on restart
    class ReusableHTTPServer(HTTPServer):
        allow_reuse_address = True

    port = config.get('port', 8099)
    server = ReusableHTTPServer(('127.0.0.1', port), RequestHandler)
    log.info(f'Steam queue processor listening on http://127.0.0.1:{port}')

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info('Shutting down')
        server.shutdown()

if __name__ == '__main__':
    main()

