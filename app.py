"""
RiotStalker – watch the Riot/League client friends list and run a callback
when a name shows up (comes online or appears in the list).

Supports:
- Riot Client (launcher): uses lockfile at %LocalAppData%\\Riot Games\\Riot Client\\Config\\lockfile
  and GET https://127.0.0.1:{port}/chat/v4/presences (online friends only).
- League Client (LCU): uses League lockfile or LeagueClientUx.exe and /lol-chat/v1/friends.

Ref: https://valapidocs.techchrism.me/endpoint/presence
"""

import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import ssl
import threading

# Unbuffer stdout/stderr so each line appears immediately (no need to press Ctrl+C to see output)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(line_buffering=True)

# Optional: use requests if available for cleaner HTTP
try:
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# Optional: use websocket-client for real-time events (pip install websocket-client)
try:
    import websocket  # type: ignore[import-untyped]
    HAS_WEBSOCKET = True
except ImportError:
    HAS_WEBSOCKET = False


def _read_lockfile(lockfile_path):
    """Read port and password from a lockfile. Returns (port, password) or (None, None)."""
    if not os.path.isfile(lockfile_path):
        return None, None
    try:
        with open(lockfile_path, "r") as f:
            line = f.read().strip()
        parts = line.split(":")
        if len(parts) >= 5:
            return int(parts[2]), parts[3]
    except Exception:
        pass
    return None, None


def _get_connection_from_lockfile():
    """Find port and password from lockfile. Prefer Riot Client, then League."""
    # Riot Client lockfile (Valorant API docs: used when game/launcher is running)
    riot_config = os.path.expandvars(r"%LOCALAPPDATA%\Riot Games\Riot Client\Config")
    port, password = _read_lockfile(os.path.join(riot_config, "lockfile"))
    if port is not None:
        return port, password
    # League / other locations
    possible_dirs = [
        os.path.expandvars(r"%LOCALAPPDATA%\Riot Games\Riot Client"),
        os.path.expandvars(r"%PROGRAMDATA%\Riot Games\League of Legends"),
        r"C:\Riot Games\League of Legends",
        r"C:\Riot Games\Riot Client",
    ]
    for directory in possible_dirs:
        port, password = _read_lockfile(os.path.join(directory, "lockfile"))
        if port is not None:
            return port, password
    return None, None


def _get_connection_from_process():
    """Get LCU port and auth token from LeagueClientUx.exe command line (Windows)."""
    try:
        out = subprocess.run(
            [
                "wmic", "PROCESS", "WHERE", "name='LeagueClientUx.exe'",
                "GET", "commandline", "/FORMAT:VALUE"
            ],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if out.returncode != 0 or not out.stdout:
            return None, None
        cmd = out.stdout
        port_m = re.search(r"--app-port=(\d+)", cmd)
        token_m = re.search(r"--remoting-auth-token=([\w-]+)", cmd)
        if port_m and token_m:
            return int(port_m.group(1)), token_m.group(1)
    except Exception:
        pass
    return None, None


def get_lcu_connection():
    """Return (port, password) for the running League client, or (None, None)."""
    port, password = _get_connection_from_lockfile()
    if port is not None:
        return port, password
    return _get_connection_from_process()


def _auth_header(password):
    raw = f"riot:{password}"
    b64 = base64.b64encode(raw.encode()).decode()
    return f"Basic {b64}"


def _ssl_no_verify():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def fetch_presences(port, password):
    """
    GET /chat/v4/presences (Riot Client API).
    Returns only online friends; each has name, game_name, game_tag, product, state, etc.
    Dedupes by puuid and prefers presence with product valorant/league_of_legends over riot_client.
    Ref: https://valapidocs.techchrism.me/endpoint/presence
    """
    url = f"https://127.0.0.1:{port}/chat/v4/presences"
    headers = {"Authorization": _auth_header(password)}

    if HAS_REQUESTS:
        r = requests.get(url, headers=headers, verify=False, timeout=5)
        r.raise_for_status()
        data = r.json()
        raw = (data.get("presences") or []) if isinstance(data, dict) else []
    else:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=5, context=_ssl_no_verify()) as resp:
            data = json.loads(resp.read().decode())
            raw = (data.get("presences") or []) if isinstance(data, dict) else []

    # API can return multiple presences per user (e.g. one "valorant", one "riot_client"); keep the game one
    by_puuid = {}
    for p in raw:
        puuid = p.get("puuid")
        if not puuid:
            continue
        prod = (p.get("product") or p.get("Product") or "").strip().lower()
        existing = by_puuid.get(puuid)
        if not existing:
            by_puuid[puuid] = p
            continue
        existing_prod = (existing.get("product") or existing.get("Product") or "").strip().lower()
        if prod in ("valorant", "league_of_legends") and existing_prod not in ("valorant", "league_of_legends"):
            by_puuid[puuid] = p
        elif prod in ("valorant", "league_of_legends") and existing_prod in ("valorant", "league_of_legends"):
            pass
        elif existing_prod not in ("valorant", "league_of_legends") and p.get("private"):
            by_puuid[puuid] = p
    return list(by_puuid.values())


def fetch_friends(port, password):
    """GET /lol-chat/v1/friends (LCU) and return list of friend objects."""
    url = f"https://127.0.0.1:{port}/lol-chat/v1/friends"
    headers = {"Authorization": _auth_header(password)}

    if HAS_REQUESTS:
        r = requests.get(url, headers=headers, verify=False, timeout=5)
        r.raise_for_status()
        return r.json()

    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=5, context=_ssl_no_verify()) as resp:
        return json.loads(resp.read().decode())


def is_online(friend):
    """True if friend is considered 'online' (available or in-game)."""
    availability = (friend.get("availability") or "offline").lower()
    if availability != "offline":
        return True
    # In-game (e.g. League of Legends) shows as gameStatus or similar
    if friend.get("gameStatus") or friend.get("lol", {}).get("gameStatus"):
        return True
    return False


def get_display_name(friend):
    """Best display name for a friend (LCU or Riot presence object)."""
    return (
        friend.get("name")
        or friend.get("gameName")
        or friend.get("summonerName")
        or (f"{friend.get('game_name', '')}#{friend.get('game_tag', '')}".strip("#") or None)
        or str(friend.get("puuid", ""))[:8]
    )


def get_display_name_from_presence(presence):
    """Display name from Riot Client presence (game_name#game_tag or name)."""
    name = presence.get("name") or presence.get("game_name")
    tag = presence.get("game_tag")
    if name and tag:
        return f"{name}#{tag}"
    return name or str(presence.get("puuid", ""))[:8]


def _decode_presence_private(private_b64):
    """Decode presence private payload; returns dict or None."""
    if not private_b64 or not isinstance(private_b64, str):
        return None
    try:
        raw = base64.b64decode(private_b64).decode("utf-8", errors="ignore")
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def presence_to_status(presence):
    """
    Returns status for a presence: same style as League ("Playing League of Legends") but for Valorant ("Playing Valorant" / "In Valorant (menu)" etc.).
    When we're on the presence API and product isn't League, we treat them as Valorant so it "does it just like league but for val".
    """
    product = (presence.get("product") or presence.get("Product") or presence.get("gameProduct") or "").strip().lower()
    resource = (presence.get("resource") or "").lower()
    patchline = (presence.get("patchline") or "").lower()
    basic_raw = presence.get("basic")
    basic = ""
    if isinstance(basic_raw, str):
        basic = basic_raw.lower()
        # Some Riot presence APIs use "basic" as base64-encoded JSON (e.g. {"product":"valorant",...})
        try:
            basic_decoded = base64.b64decode(basic_raw).decode("utf-8", errors="ignore")
            if basic_decoded.strip().startswith("{"):
                basic_json = json.loads(basic_decoded)
                if isinstance(basic_json, dict):
                    product = product or (basic_json.get("product") or basic_json.get("Product") or "").strip().lower()
                    if "valorant" in json.dumps(basic_json).lower():
                        basic = "valorant"
        except Exception:
            pass
    state = (presence.get("state") or "chat").lower()
    summary_raw = (presence.get("summary") or "").strip()
    summary = summary_raw.lower()
    private_b64 = presence.get("private")
    private_data = _decode_presence_private(private_b64)

    # Detect Valorant: product, resource, basic, summary, or any presence field; or private payload keys
    def _any_valorant(obj):
        if not obj or not isinstance(obj, dict):
            return False
        for k, v in obj.items():
            if isinstance(v, str) and "valorant" in v.lower():
                return True
            if isinstance(v, dict) and _any_valorant(v):
                return True
        return False

    is_valorant = (
        product == "valorant"
        or "valorant" in product
        or "valorant" in resource
        or "valorant" in patchline
        or "valorant" in basic
        or "valorant" in summary
        or (private_data and any(
            k in (private_data or {}) for k in ("sessionLoopState", "partyState", "queueId", "provisioningFlow")
        ))
        or _any_valorant(presence)
        or (
            product != "league_of_legends"
            and any(p in summary for p in ("agent select", "pregame", "in a game", "custom game"))
        )
    )

    # sessionLoopState can be at top level or inside matchPresenceData / partyPresenceData
    _priv = private_data or {}
    session_state = (
        _priv.get("sessionLoopState")
        or (_priv.get("matchPresenceData") or {}).get("sessionLoopState")
        or (_priv.get("partyPresenceData") or {}).get("partyOwnerSessionLoopState")
        or ""
    )
    session_state = str(session_state).upper()
    party_state = _priv.get("partyState") or (_priv.get("partyPresenceData") or {}).get("partyState") or ""
    queue_id = _priv.get("queueId") or (_priv.get("matchPresenceData") or {}).get("queueId") or ""

    summary_says_playing = "playing" in summary and any(c.isdigit() for c in summary_raw)
    in_game = session_state == "INGAME" or summary_says_playing
    # Only "in queue" when actually matchmaking; MENUS + queueId (e.g. "unrated") just means in client with a queue selected
    in_queue = party_state in ("MATCHMAKING", "STARTING_MATCHMAKING", "LEAVING_MATCHMAKING")
    in_pregame = session_state == "PREGAME"

    # Extract round score, party, and match info from private payload (for Valorant)
    party_data = _priv.get("partyPresenceData") or {}
    match_data = _priv.get("matchPresenceData") or {}
    player_data = _priv.get("playerPresenceData") or {}
    score_ally = _priv.get("partyOwnerMatchScoreAllyTeam") or party_data.get("partyOwnerMatchScoreAllyTeam")
    score_enemy = _priv.get("partyOwnerMatchScoreEnemyTeam") or party_data.get("partyOwnerMatchScoreEnemyTeam")
    party_size = _priv.get("partySize") or party_data.get("partySize")
    max_party_size = _priv.get("maxPartySize") or party_data.get("maxPartySize")
    match_map = match_data.get("matchMap") or ""
    is_party_owner = party_data.get("isPartyOwner")
    party_state_val = party_data.get("partyState") or party_state
    competitive_tier = player_data.get("competitiveTier")
    account_level = player_data.get("accountLevel")

    valorant_detail = {
        "round_score": f"{score_ally or 0}-{score_enemy or 0}" if (score_ally is not None or score_enemy is not None) else None,
        "score_ally": score_ally,
        "score_enemy": score_enemy,
        "party_size": party_size,
        "max_party_size": max_party_size,
        "match_map": match_map or None,
        "queue_id": queue_id or None,
        "is_party_owner": is_party_owner,
        "party_state": party_state_val or None,
        "competitive_tier": competitive_tier,
        "account_level": account_level,
        "session_loop_state": session_state or None,
    }

    # League of Legends: parse summary and private for phase (lobby, queue, champ select, in game)
    league_phase = None
    league_detail = {}
    if product == "league_of_legends":
        league_detail = {"summary": summary_raw or None, "basic": (presence.get("basic") or "") or None}
        # Common League phases in summary or basic
        s = (summary_raw or "").lower()
        b = (str((presence.get("basic") or ""))).lower()
        combined = f"{s} {b}"
        if "champ select" in combined or "champion select" in combined:
            league_phase = "champ_select"
            league_detail["phase"] = "champ_select"
        elif "in game" in combined or "in progress" in combined or "in a game" in combined:
            league_phase = "in_game"
            league_detail["phase"] = "in_game"
        elif "in queue" in combined or "matchmaking" in combined or "finding match" in combined:
            league_phase = "in_queue"
            league_detail["phase"] = "in_queue"
        elif "in lobby" in combined or "lobby" in combined:
            league_phase = "lobby"
            league_detail["phase"] = "lobby"
        elif "ready" in combined or "ready check" in combined:
            league_phase = "ready_check"
            league_detail["phase"] = "ready_check"
        # Decode League private if present (structure may vary)
        if private_data:
            q = (private_data or {}).get("queueId") or (private_data or {}).get("queue")
            if q is not None:
                league_detail["queue_id"] = q
            gp = (private_data or {}).get("gamePhase") or (private_data or {}).get("phase")
            if gp:
                league_detail["game_phase"] = gp

    # Same pattern as League: explicit game name + phase when known. Valorant gets same treatment.
    if product == "league_of_legends":
        if league_phase == "in_game":
            status = "Playing League of Legends (in game)"
        elif league_phase == "champ_select":
            status = "Playing League of Legends (champ select)"
        elif league_phase == "in_queue":
            status = "Playing League of Legends (in queue)"
        elif league_phase == "lobby":
            status = "Playing League of Legends (in lobby)"
        elif league_phase == "ready_check":
            status = "Playing League of Legends (ready check)"
        else:
            status = "Playing League of Legends" + (f" · {summary_raw}" if summary_raw else "")
    elif is_valorant or summary_says_playing:
        if in_game or summary_says_playing:
            status = "In Valorant (in game)"
        elif in_queue:
            status = "In Valorant (in queue)"
        elif in_pregame:
            status = "In Valorant (agent select)"
        elif "menu" in summary or "lobby" in summary or not summary or session_state == "MENUS":
            status = "In Valorant (menu)"
        else:
            status = "Playing Valorant"
    else:
        # Launcher only or unknown — not in a game
        status = "Online in Launcher"

    # Append round/party summary to status when in game (e.g. "In Valorant (in game) · 3-2 · Party 2/5")
    if (is_valorant or summary_says_playing) and (in_game or summary_says_playing):
        extra = []
        if valorant_detail.get("round_score"):
            extra.append(valorant_detail["round_score"])
        if party_size is not None and max_party_size is not None:
            extra.append(f"Party {party_size}/{max_party_size}")
        if match_map:
            extra.append(match_map)
        if extra:
            status = f"{status} · {' · '.join(extra)}"

    if status == "Online" and os.environ.get("RIOTSTALKER_DEBUG"):
        try:
            name = (presence.get("game_name") or "") + "#" + (presence.get("game_tag") or "")
            name = name.strip("#") or presence.get("name") or presence.get("puuid", "")[:8]
            debug_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "riotstalker_debug.log")
            safe = {k: (v if k != "private" else ("<decoded: " + json.dumps(_decode_presence_private(v)) + ">") if v else None) for k, v in presence.items()}
            with open(debug_path, "a", encoding="utf-8") as dbg:
                dbg.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} Online presence: {name} ---\n")
                dbg.write(json.dumps(safe, indent=2) + "\n")
        except Exception:
            pass

    return {
        "status": status,
        "product": product or ("valorant" if is_valorant else None),
        "state": state,
        "summary": summary_raw or None,
        "in_game": in_game,
        "valorant": valorant_detail if (is_valorant or summary_says_playing) else None,
        "league": league_detail if product == "league_of_legends" else None,
    }


def run_when_name_shows_up(
    on_online=None,
    on_status_change=None,
    on_offline=None,
    watch_names=None,
    poll_interval=3,
    use_websocket=True,
    log_file=None,
    notify_cooldown_seconds=90,
):
    """
    Watch the friends list and call callbacks when someone comes online, goes offline, or their status changes.

    You don't need to provide user id, tag, or name - the API returns everyone who is online
    with their game_name, game_tag, puuid, and activity (Valorant in menu, in game, League, etc.).
    Use watch_names only to filter to specific people (e.g. ["ImClem", "Sith"]).

    - on_online: callback(name, status_dict) when a friend comes online. status_dict has
      "status", "product", "state", "summary", "in_game", "valorant" (when in Valorant: round score, party, map, etc.),
      and "league" (when in League: phase, summary, queue_id, game_phase).
    - on_status_change: optional callback(name, old_status_dict, new_status_dict) when their
      activity changes (e.g. launcher -> Valorant, or Valorant menu -> in game).
    - on_offline: optional callback(name) when a watched friend disappears from the presence list (went offline).
    - watch_names: if set, only trigger for these names (case-insensitive). None = all friends.
    - poll_interval: seconds between polls.
    - use_websocket: if True and websocket-client installed, use LCU events (League only).
    - log_file: if set, append all status messages to this file (with timestamp). Same as console.
    - notify_cooldown_seconds: after notifying for a person, ignore further status changes for this many seconds (stops API flicker spam). Default 90.
    """
    def _log(msg):
        print(msg, flush=True)
        if log_file:
            try:
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
            except Exception as e:
                print(f"(log write failed: {e})", file=sys.stderr, flush=True)

    if on_online is None:
        def _default_on_online(name, status=None):
            s = (status or {}).get("status", "online")
            _log(f"[RiotStalker] {name}: {s}.")
        on_online = _default_on_online
    # Wait for client (so if you start script then open Valorant, or close and reopen, we connect)
    while True:
        port, password = get_lcu_connection()
        if port is not None:
            break
        _log("[RiotStalker] Waiting for Riot/Valorant client... (open the client and log in)")
        time.sleep(5)

    connection = {"port": port, "password": password}
    watch_set = None
    if watch_names is not None:
        watch_set = {n.strip().lower() for n in watch_names if n}

    # Prefer Riot Client presence API (GET /chat/v4/presences) when available
    use_presences = False
    try:
        presences = fetch_presences(connection["port"], connection["password"])
        use_presences = True
    except Exception:
        pass

    if use_presences:
        # Riot Client: presences = online friends only. Report status: online, Valorant menu, Valorant in game, League.
        watch_str = ", ".join(watch_names) if watch_names else "all friends"
        _log(f"[RiotStalker] Started. Watching for: {watch_str}. (Polling every {poll_interval}s)")

        seen_puuids = set()
        last_status_by_puuid = {}
        last_emitted_status_by_puuid = {}
        consecutive_status_by_puuid = {}  # puuid -> (status_str, count) for stability
        last_notify_time_by_puuid = {}  # puuid -> time; cooldown to avoid flicker spam
        last_seen_online_puuids = set()  # watched puuids we saw online last poll (for offline detection)
        puuid_to_name = {}  # puuid -> display name for watched users (so we can say who went offline)
        for p in presences:
            puuid = p.get("puuid")
            if puuid:
                seen_puuids.add(puuid)
                last_status_by_puuid[puuid] = presence_to_status(p)

        # Print current status for watched users (once per puuid) and notify so you get a push when they're already online
        seen_current = set()
        for p in presences:
            puuid = p.get("puuid")
            if not puuid or puuid in seen_current:
                continue
            seen_current.add(puuid)
            name = get_display_name_from_presence(p)
            game_name = (p.get("game_name") or "").lower()
            name_lower = (name or "").lower()
            allowed = watch_set is None or (name_lower in watch_set or game_name in watch_set)
            if allowed:
                last_seen_online_puuids.add(puuid)
                puuid_to_name[puuid] = name
                status_dict = last_status_by_puuid.get(puuid) or presence_to_status(p)
                last_emitted_status_by_puuid[puuid] = status_dict.get("status")
                consecutive_status_by_puuid[puuid] = (status_dict.get("status"), 2)
                # Debug: when status shows Online but they might be in Valorant, log raw presence once
                if os.environ.get("RIOTSTALKER_DEBUG") and status_dict.get("status") == "Online":
                    _debug_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "riotstalker_debug.log")
                    try:
                        with open(_debug_path, "a", encoding="utf-8") as dbg:
                            dbg.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} presence for {name} (status=Online) ---\n")
                            safe = {k: (v if k != "private" else ("<decoded: " + json.dumps(_decode_presence_private(v)) + ">") if v else None) for k, v in p.items()}
                            dbg.write(json.dumps(safe, indent=2) + "\n")
                    except Exception:
                        pass
                try:
                    on_online(name, status_dict)
                except TypeError:
                    on_online(name)
                except Exception as e:
                    print(f"Callback error for {name}: {e}", file=sys.stderr, flush=True)

        _last_reconnect_log = [0.0]  # throttle "client closed" message

        def check_presences():
            nonlocal seen_puuids, last_status_by_puuid, last_emitted_status_by_puuid, consecutive_status_by_puuid, last_notify_time_by_puuid, last_seen_online_puuids, puuid_to_name
            changed = False
            now = time.time()
            try:
                presences_now = fetch_presences(connection["port"], connection["password"])
            except Exception as e:
                # Client may have closed; try to reconnect so we resume when they open Valorant again
                new_port, new_password = get_lcu_connection()
                if new_port is not None and (new_port != connection["port"] or new_password != connection["password"]):
                    connection["port"], connection["password"] = new_port, new_password
                    _log("[RiotStalker] Reconnected to client.")
                    return changed
                if now - _last_reconnect_log[0] > 30:
                    _last_reconnect_log[0] = now
                    _log("[RiotStalker] Client not responding. Will keep retrying when you open Valorant/Riot client.")
                return changed

            prev_online_watched = last_seen_online_puuids.copy()
            current_online_watched = set()

            for p in presences_now:
                puuid = p.get("puuid")
                if not puuid:
                    continue
                name = get_display_name_from_presence(p)
                game_name = (p.get("game_name") or "").lower()
                name_lower = (name or "").lower()
                allowed = (
                    watch_set is None
                    or (name_lower in watch_set or game_name in watch_set)
                )
                if not allowed:
                    continue
                current_online_watched.add(puuid)
                puuid_to_name[puuid] = name
                status_dict = presence_to_status(p)
                new_status = status_dict.get("status")
                if puuid not in seen_puuids:
                    seen_puuids.add(puuid)
                    last_status_by_puuid[puuid] = status_dict
                    last_emitted_status_by_puuid[puuid] = new_status
                    consecutive_status_by_puuid[puuid] = (new_status, 2)
                    changed = True
                    try:
                        on_online(name, status_dict)
                    except TypeError:
                        on_online(name)
                    except Exception as e:
                        print(f"Callback error for {name}: {e}", file=sys.stderr, flush=True)
                else:
                    old = last_status_by_puuid.get(puuid)
                    last_emitted = last_emitted_status_by_puuid.get(puuid)
                    # Emit as soon as status changes (every 3s poll), with short cooldown to avoid API flicker
                    on_cooldown = (now - last_notify_time_by_puuid.get(puuid, 0)) < 5  # 5s to avoid flicker, still update every 3s when change persists
                    if (
                        new_status != last_emitted
                        and not on_cooldown
                        and on_status_change
                    ):
                        changed = True
                        last_notify_time_by_puuid[puuid] = now
                        last_emitted_status_by_puuid[puuid] = new_status
                        try:
                            on_status_change(name, old or {}, status_dict)
                        except Exception as e:
                            print(f"Status change callback error for {name}: {e}", file=sys.stderr, flush=True)
                    elif new_status != last_emitted:
                        last_emitted_status_by_puuid[puuid] = new_status
                    last_status_by_puuid[puuid] = status_dict

            # Offline: were in previous poll but not in this one
            went_offline = prev_online_watched - current_online_watched
            if went_offline and on_offline:
                for puuid in went_offline:
                    offline_name = puuid_to_name.get(puuid) or puuid[:8]
                    changed = True
                    try:
                        on_offline(offline_name)
                    except Exception as e:
                        print(f"Offline callback error for {offline_name}: {e}", file=sys.stderr, flush=True)
                    seen_puuids.discard(puuid)
                    last_status_by_puuid.pop(puuid, None)
                    last_emitted_status_by_puuid.pop(puuid, None)
                    consecutive_status_by_puuid.pop(puuid, None)
                    last_notify_time_by_puuid.pop(puuid, None)

            last_seen_online_puuids = current_online_watched
            return changed

        # Poll every poll_interval (3s) so changes show within one poll
        while True:
            time.sleep(poll_interval)
            check_presences()
        return

    # League Client (LCU): full friends list + availability
    watch_str = ", ".join(watch_names) if watch_names else "all friends"
    _log(f"[RiotStalker] Started (League client). Watching for: {watch_str}. (Polling every {poll_interval}s)")

    last_online = {}  # name -> was online last time we saw

    def check_friends_list():
        nonlocal last_online
        try:
            friends = fetch_friends(port, password)
        except Exception as e:
            print(f"Failed to fetch friends: {e}", file=sys.stderr, flush=True)
            return
        for f in friends:
            name = get_display_name(f)
            if not name:
                continue
            now_online = is_online(f)
            was_online = last_online.get(name, False)
            last_online[name] = now_online
            if watch_set is not None and name.lower() not in watch_set:
                continue
            if now_online and not was_online:
                try:
                    on_online(name, {"status": "Online"})
                except TypeError:
                    on_online(name)
                except Exception as e:
                    print(f"Callback error for {name}: {e}", file=sys.stderr, flush=True)
            elif not now_online and was_online and on_offline:
                try:
                    on_offline(name)
                except Exception as e:
                    print(f"Offline callback error for {name}: {e}", file=sys.stderr, flush=True)

    # Initial snapshot so we don't fire for everyone already online
    try:
        friends = fetch_friends(port, password)
        for f in friends:
            name = get_display_name(f)
            if name:
                last_online[name] = is_online(f)
    except Exception as e:
        print(
            f"Connected to client on port {port} but the friends API failed: {e}\n"
            "Make sure League of Legends (or Valorant) is open and you're logged in, then run again.",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)

    if use_websocket and HAS_WEBSOCKET:
        def on_ws_message(ws, message):
            try:
                msg = json.loads(message)
                if isinstance(msg, list) and len(msg) >= 3 and msg[0] == 8:
                    uri = (msg[2] or {}).get("uri") or ""
                    if "lol-chat" in uri and "friend" in uri.lower():
                        check_friends_list()
            except Exception:
                pass

        def run_ws():
            url = f"wss://127.0.0.1:{port}/"
            ws = websocket.WebSocketApp(
                url,
                header=["Authorization: " + _auth_header(password)],
                on_message=on_ws_message,
            )
            ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE}, skip_utf8_validation=True)

        def send_subscribe(ws):
            ws.send(json.dumps([5, "OnJsonApiEvent"]))

        # We need to subscribe on open; run_forever doesn't give us the open event easily from here.
        # So we run websocket in a thread and poll as fallback, or subscribe in thread.
        class Subscriber(websocket.WebSocketApp):
            def on_open(self, ws):
                ws.send(json.dumps([5, "OnJsonApiEvent"]))

        ws_app = Subscriber(
            f"wss://127.0.0.1:{port}/",
            header=["Authorization: " + _auth_header(password)],
            on_message=on_ws_message,
        )
        th = threading.Thread(
            target=lambda: ws_app.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE}, skip_utf8_validation=True),
            daemon=True,
        )
        th.start()
        time.sleep(1)
        # Fallback poll in main thread
        while True:
            time.sleep(poll_interval)
            check_friends_list()
    else:
        while True:
            time.sleep(poll_interval)
            check_friends_list()


# --- Phone notifications (optional): set one of these to get a push when someone logs on ---
def notify_ntfy(topic: str, title: str, message: str) -> None:
    """Send push to your phone via ntfy.sh (no account). Pick a secret topic, subscribe in the ntfy app."""
    url = f"https://ntfy.sh/{topic}"
    try:
        req = urllib.request.Request(url, data=message.encode("utf-8"), method="POST")
        req.add_header("Title", title)
        req.add_header("Content-Type", "text/plain")
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"(ntfy failed: {e})", file=sys.stderr, flush=True)


def notify_pushbullet(api_key: str, title: str, message: str) -> None:
    """Send push to your phone via Pushbullet. Get API key from https://www.pushbullet.com/#settings."""
    url = "https://api.pushbullet.com/v2/pushes"
    data = json.dumps({"type": "note", "title": title, "body": message}).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {api_key}")
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"(Pushbullet failed: {e})", file=sys.stderr, flush=True)


if __name__ == "__main__":
    # Log file: same directory as this script. All status messages are appended with a timestamp.
    _LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "riotstalker.log")

    # --- Phone push when they log on: set ONE of these (or leave both None for no push) ---
    # Option A: ntfy (easiest, no account). Install "ntfy" app, subscribe to the topic below.
    NOTIFY_NTFY_TOPIC = "your-secret-topic"  # change to a unique, hard-to-guess topic; subscribe to it in the ntfy app
    # Option B: Pushbullet. Get key from https://www.pushbullet.com/#settings
    NOTIFY_PUSHBULLET_KEY = None  # e.g. "o.xxxxxxxxxxxxxxxxxxxxxxxx"

    def _send_push(title: str, body: str) -> None:
        if NOTIFY_NTFY_TOPIC:
            notify_ntfy(NOTIFY_NTFY_TOPIC, title, body)
        if NOTIFY_PUSHBULLET_KEY:
            notify_pushbullet(NOTIFY_PUSHBULLET_KEY, title, body)

    def _push_body(name: str, status: str) -> str:
        """Notification body: explicit for launcher, otherwise use status."""
        s = (status or "online").lower()
        if s in ("online", "online in launcher", ""):
            return f"{name} is online in the launcher"
        return f"{name}: {status}"

    def on_presence(name, status_dict):
        status = status_dict.get("status", "online")
        msg = f"[RiotStalker] {name}: {status}"
        print(msg, flush=True)
        if _LOG_PATH:
            try:
                with open(_LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
            except Exception:
                pass
        _send_push("RiotStalker", _push_body(name, status))

    def on_change(name, old, new):
        status = new.get("status", "?")
        msg = f"[RiotStalker] {name} -> {status}"
        print(msg, flush=True)
        if _LOG_PATH:
            try:
                with open(_LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
            except Exception:
                pass
        _send_push("RiotStalker", _push_body(name, status))

    def on_offline(name):
        msg = f"[RiotStalker] {name} went offline"
        print(msg, flush=True)
        if _LOG_PATH:
            try:
                with open(_LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
            except Exception:
                pass
        _send_push("RiotStalker", f"{name} went offline")

    run_when_name_shows_up(
        on_online=on_presence,
        on_status_change=on_change,
        on_offline=on_offline,
        watch_names=["FriendName"],  # replace with the Riot/Valorant display name(s) you want to watch (or None for all friends)
        poll_interval=3,
        use_websocket=True,
        log_file=_LOG_PATH,
    )
