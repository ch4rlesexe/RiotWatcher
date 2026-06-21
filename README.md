# RiotStalker

Watches your Riot friends list and tracks what your friends are up to in real time — when they come online, what game they're in, round scores, map, party size, queue state, and more. Prints to console, optionally sends a push notification to your phone, and logs everything with timestamps.

No Riot API key needed — it reads the lockfile the client already drops and hits the local API on localhost.

---

## Setup

Requires Python 3.8+ on Windows. Install the optional deps for best results:

```
pip install requests websocket-client
```

Then open `app.py`, scroll to the bottom, and fill in:

```python
watch_names=["FriendName"],      # who you want to watch, or None for everyone
NOTIFY_NTFY_TOPIC = "your-topic" # for phone push (see below), or None to skip
```

Run it:
```
python app.py
```

It'll wait for the client if it's not open yet and reconnect automatically if you restart Valorant.

---

## Phone notifications

### ntfy (easiest, free)

1. Install the [ntfy app](https://ntfy.sh) on your phone
2. Pick a topic name that's hard to guess (anyone who knows it can subscribe)
3. Subscribe to it in the app
4. Set `NOTIFY_NTFY_TOPIC = "your-topic"` in `app.py`

### Pushbullet

Get your API key from [pushbullet.com/#settings](https://www.pushbullet.com/#settings) and set `NOTIFY_PUSHBULLET_KEY` in `app.py`.

---

## What it tracks

More than just online/offline. It polls the presence payload the client broadcasts and pulls out whatever it can:

**Valorant**
- Game phase: menu, agent select, in game
- Round score (e.g. `5-3`)
- Map
- Party size and whether they're the party owner
- Queue (unrated, competitive, etc.) and matchmaking state
- Competitive tier and account level (when available)

**League of Legends**
- Phase: lobby, in queue, champ select, ready check, in game
- Queue type

Output looks like:
```
PlayerOne: In Valorant (in game) · 5-3 · Party 2/5 · Ascent
PlayerOne -> In Valorant (agent select)
PlayerOne went offline
```

If they're just sitting in the launcher it'll say so too.

---

## Using it as a module

```python
from app import run_when_name_shows_up

def on_online(name, status):
    print(f"{name} is on: {status['status']}")

def on_change(name, old, new):
    print(f"{name}: {old['status']} -> {new['status']}")

def on_offline(name):
    print(f"{name} logged off")

run_when_name_shows_up(
    on_online=on_online,
    on_status_change=on_change,
    on_offline=on_offline,
    watch_names=["PlayerOne"],
)
```

The `status` dict has `status` (string), `product`, `in_game` (bool), and `valorant` / `league` dicts with game-specific info when applicable.

---

## How it works

Reads the Riot Client lockfile from `%LOCALAPPDATA%\Riot Games\Riot Client\Config\lockfile` to get the local port + auth token, then polls `/chat/v4/presences` every few seconds. Falls back to the League Client `/lol-chat/v1/friends` endpoint (with WebSocket support) if the presence API isn't available.

---

## Debugging

```powershell
$env:RIOTSTALKER_DEBUG=1; python app.py
```

Dumps raw presence payloads to `riotstalker_debug.log` when someone shows up as just "Online" and you want to see what the API is actually returning.

---

## Disclaimer

This only talks to `127.0.0.1` — the same local API the Riot client itself uses. No external API calls, no key required. Use at your own risk and in line with Riot's ToS.

## License

[MIT](LICENSE)
