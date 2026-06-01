import csv
import hashlib
import json
import os
import re
import sqlite3
import time
import webbrowser
from datetime import datetime
from functools import wraps
from pathlib import Path
from urllib import error, request

from flask import Flask, jsonify, request as flask_request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash


PORT = int(os.environ.get("PORT", "9877"))
BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"
TRADES_FILE = BASE_DIR / "trades.json"
SETTINGS_FILE = BASE_DIR / "api_settings.json"
DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", BASE_DIR / "futures_platform.db"))
DATABASE_URL = os.environ.get("DATABASE_URL", "")
USING_POSTGRES = bool(DATABASE_URL)

FUTURES = {
    "MGC": {"tickSize": 0.10, "tickValue": 1.00},
    "MES": {"tickSize": 0.25, "tickValue": 1.25},
    "MNQ": {"tickSize": 0.25, "tickValue": 0.50},
    "MYM": {"tickSize": 1.00, "tickValue": 0.50},
}

app = Flask(__name__, static_folder=None)
app.secret_key = os.environ.get("SECRET_KEY", "dev-change-me-before-hosting")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("FLASK_ENV") == "production",
)


def default_settings():
    return {
        "tradovate": {
            "enabled": False,
            "environment": "demo",
            "name": "",
            "password": "",
            "appId": "FuturesPlatform",
            "appVersion": "1.0",
            "cid": "",
            "secret": "",
            "deviceId": "",
        },
        "brokers": [],
    }


def default_snapshot():
    return {
        "trades": [],
        "rules": [],
        "tradeConditions": [],
        "settings": default_settings(),
    }


def db():
    if USING_POSTGRES:
        import psycopg2
        import psycopg2.extras

        return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def sql(query):
    return query.replace("?", "%s") if USING_POSTGRES else query


def fetch_one(query, params=()):
    with db() as conn:
        cur = conn.cursor()
        cur.execute(sql(query), params)
        return cur.fetchone()


def execute_write(query, params=()):
    with db() as conn:
        cur = conn.cursor()
        cur.execute(sql(query), params)
        return cur


def init_db():
    with db() as conn:
        cur = conn.cursor()
        if USING_POSTGRES:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                  id SERIAL PRIMARY KEY,
                  email TEXT NOT NULL UNIQUE,
                  password_hash TEXT NOT NULL,
                  created_at TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_snapshots (
                  user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                  data TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                )
                """
            )
        else:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  email TEXT NOT NULL UNIQUE,
                  password_hash TEXT NOT NULL,
                  created_at TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_snapshots (
                  user_id INTEGER PRIMARY KEY,
                  data TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            )


def public_settings(settings):
    clean = json.loads(json.dumps(settings or default_settings()))
    tradovate = clean.setdefault("tradovate", {})
    if tradovate.get("password"):
        tradovate["password"] = ""
        tradovate["passwordSaved"] = True
    if tradovate.get("secret"):
        tradovate["secret"] = ""
        tradovate["secretSaved"] = True
    for broker in clean.get("brokers", []):
        if broker.get("apiSecret"):
            broker["apiSecret"] = ""
            broker["apiSecretSaved"] = True
    return clean


def merge_snapshot(data, existing=None):
    snapshot = existing or default_snapshot()
    incoming = data if isinstance(data, dict) else {}

    if isinstance(incoming.get("trades"), list):
        snapshot["trades"] = incoming["trades"]
    if isinstance(incoming.get("rules"), list):
        snapshot["rules"] = incoming["rules"]
    if isinstance(incoming.get("tradeConditions"), list):
        snapshot["tradeConditions"] = incoming["tradeConditions"]
    if isinstance(incoming.get("settings"), dict):
        snapshot["settings"] = merge_settings(incoming["settings"], snapshot.get("settings"))

    return snapshot


def load_snapshot(user_id):
    row = fetch_one("SELECT data FROM user_snapshots WHERE user_id = ?", (user_id,))
    if not row:
        return default_snapshot()
    try:
        loaded = json.loads(row["data"])
        return merge_snapshot(loaded, default_snapshot())
    except json.JSONDecodeError:
        return default_snapshot()


def save_snapshot(user_id, snapshot):
    payload = json.dumps(snapshot, separators=(",", ":"))
    now = datetime.utcnow().isoformat()
    execute_write(
        """
        INSERT INTO user_snapshots (user_id, data, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at
        """,
        (user_id, payload, now),
    )


def merge_settings(incoming, existing=None):
    current = existing or default_settings()
    settings = default_settings()
    settings["tradovate"].update(current.get("tradovate", {}))

    if isinstance(incoming.get("tradovate"), dict):
        settings["tradovate"].update(incoming["tradovate"])
        if not incoming["tradovate"].get("password") and current.get("tradovate", {}).get("password"):
            settings["tradovate"]["password"] = current["tradovate"]["password"]
        if not incoming["tradovate"].get("secret") and current.get("tradovate", {}).get("secret"):
            settings["tradovate"]["secret"] = current["tradovate"]["secret"]

    brokers = incoming.get("brokers", current.get("brokers", []))
    settings["brokers"] = brokers if isinstance(brokers, list) else []
    return settings


def current_user_id():
    return session.get("user_id")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user_id():
            return jsonify({"ok": False, "message": "Login required."}), 401
        return view(*args, **kwargs)

    return wrapped


def get_current_user():
    user_id = current_user_id()
    if not user_id:
        return None
    return fetch_one("SELECT id, email FROM users WHERE id = ?", (user_id,))


def read_json_body():
    payload = flask_request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


def tradovate_base_url(environment):
    host = "live.tradovateapi.com" if environment == "live" else "demo.tradovateapi.com"
    return f"https://{host}/v1"


def tradovate_request(path, method="GET", payload=None, token=""):
    data = None
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = request.Request(path, data=data, headers=headers, method=method)
    with request.urlopen(req, timeout=20) as response:
        body = response.read().decode("utf-8")
        return json.loads(body) if body else {}


def test_tradovate_connection(settings):
    settings = settings.get("tradovate", settings)
    missing = [label for key, label in {"name": "username", "password": "password"}.items() if not settings.get(key)]
    if missing:
        return {"ok": False, "message": f"Missing Tradovate fields: {', '.join(missing)}"}

    base_url = tradovate_base_url(settings.get("environment"))
    token_payload = {
        "name": settings["name"],
        "password": settings["password"],
    }
    for field in ("appId", "appVersion"):
        if settings.get(field):
            token_payload[field] = settings[field]
    if settings.get("cid"):
        token_payload["cid"] = int(settings["cid"]) if str(settings["cid"]).isdigit() else settings["cid"]
    if settings.get("secret"):
        token_payload["sec"] = settings["secret"]
    if settings.get("deviceId"):
        token_payload["deviceId"] = settings["deviceId"]

    try:
        token_data = tradovate_request(
            f"{base_url}/auth/accesstokenrequest",
            method="POST",
            payload=token_payload,
        )
        token = token_data.get("accessToken")
        if not token:
            detail = token_data.get("errorText") or token_data.get("message") or "No access token was returned."
            return {"ok": False, "message": f"Tradovate authentication failed: {detail}"}

        accounts = tradovate_request(f"{base_url}/account/list", token=token)
        count = len(accounts) if isinstance(accounts, list) else 0
        return {"ok": True, "message": f"Tradovate connection succeeded. Found {count} account(s).", "accounts": accounts}
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "message": f"Tradovate HTTP {exc.code}: {detail[:300]}"}
    except (error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "message": f"Tradovate connection failed: {exc}"}


def norm_header(value):
    return re.sub(r"[^a-z0-9]", "", value.lower())


def parse_number(value):
    if value is None:
        return 0.0
    text = str(value).strip().replace("\u2212", "-").replace("\u2013", "-").replace("\u2014", "-")
    text = re.sub(r"[$,%\s]", "", text)
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return 0.0


def get_value(row, aliases):
    for alias in aliases:
        key = norm_header(alias)
        if key in row and row[key] != "":
            return row[key]
        fuzzy_key = next((row_key for row_key in row if row_key and (key in row_key or row_key in key)), None)
        if fuzzy_key and row[fuzzy_key] != "":
            return row[fuzzy_key]
    return ""


def normalize_symbol(symbol):
    cleaned = re.sub(r"[^A-Z0-9]", "", str(symbol or "").split(":")[-1].upper())
    match = re.match(r"^(MGC|MES|MNQ|MYM|GC|ES|NQ|YM)", cleaned)
    if not match:
        return cleaned or "BALANCE"
    root = match.group(1)
    return {"GC": "MGC", "ES": "MES", "NQ": "MNQ", "YM": "MYM"}.get(root, root)


def symbol_from_action(action):
    text = str(action or "").upper()
    match = re.search(r"\b(MGC|MES|MNQ|MYM|GC|ES|NQ|YM)\d*!?\b", text)
    if match:
        return normalize_symbol(match.group(1))
    match = re.search(r"\b[A-Z]{1,6}\d*!?\b", text)
    return normalize_symbol(match.group(0)) if match else "BALANCE"


def trade_details_from_action(action):
    text = str(action or "")
    lower = text.lower()
    entry_match = re.search(r"position\s+avg\s+price\s+was\s+(-?\d+(?:\.\d+)?)", text, re.IGNORECASE)
    exit_match = re.search(r"\bat\s+price\s+(-?\d+(?:\.\d+)?)", text, re.IGNORECASE)
    contracts_match = re.search(r"\bfor\s+(-?\d+(?:\.\d+)?)\s+units?\b", text, re.IGNORECASE)

    if "close short" in lower:
        trade_type = "Short"
    elif "close long" in lower:
        trade_type = "Long"
    else:
        trade_type = "Short" if "short" in lower and "long" not in lower else "Long"

    return {
        "entry": parse_number(entry_match.group(1)) if entry_match else 0,
        "exit": parse_number(exit_match.group(1)) if exit_match else 0,
        "contracts": abs(parse_number(contracts_match.group(1))) if contracts_match else 1,
        "type": trade_type,
    }


def parse_time(value):
    text = str(value or "").strip()
    if not text:
        return datetime.now().isoformat()

    candidates = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%m/%d/%y %H:%M:%S",
        "%m/%d/%y %H:%M",
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%m/%d/%y",
    ]
    for fmt in candidates:
        try:
            return datetime.strptime(text, fmt).isoformat()
        except ValueError:
            pass

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return datetime.now().isoformat()


def trade_from_balance_row(row):
    action = get_value(row, ["action", "description", "operation", "event"])
    realized = get_value(
        row,
        [
            "realized pnl value",
            "realized p/l value",
            "realized pnl",
            "realized p/l",
            "profit/loss",
            "p/l",
            "pnl",
            "amount",
            "value",
        ],
    )
    if realized == "":
        return None

    symbol = normalize_symbol(get_value(row, ["symbol", "ticker", "instrument", "contract", "asset"]))
    if symbol == "BALANCE":
        symbol = symbol_from_action(action)

    timestamp = parse_time(get_value(row, ["time", "date/time", "date", "trade date"]))
    pl = parse_number(realized)
    raw_id = "|".join([timestamp, symbol, str(pl), action])
    import_id = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:24]
    tick_value = FUTURES.get(symbol, {"tickValue": 1.0})["tickValue"]
    details = trade_details_from_action(action)

    return {
        "id": import_id,
        "importId": import_id,
        "source": "tradingview-balance-history",
        "symbol": symbol,
        "entry": details["entry"],
        "exit": details["exit"],
        "contracts": details["contracts"],
        "tickValue": tick_value,
        "type": details["type"],
        "notes": f"Auto-imported from TradingView Balance History: {action}" if action else "Auto-imported from TradingView Balance History",
        "pl": round(pl, 2),
        "plSource": "csv-realized",
        "timestamp": timestamp,
        "conditions": [],
        "images": [],
    }


def read_csv_upload(file_storage):
    raw = file_storage.read().decode("utf-8-sig", errors="replace")
    sample = raw[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel

    rows = []
    for row in csv.DictReader(raw.splitlines(), dialect=dialect):
        normalized = {norm_header(key or ""): (value or "").strip() for key, value in row.items()}
        rows.append(normalized)
    return rows


@app.route("/")
def root():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/index.html")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(BASE_DIR, path)


@app.get("/api/auth/me")
def auth_me():
    user = get_current_user()
    if not user:
        return jsonify({"authenticated": False})
    return jsonify({"authenticated": True, "user": {"id": user["id"], "email": user["email"]}})


@app.post("/api/auth/signup")
def auth_signup():
    payload = read_json_body()
    email = str(payload.get("email", "")).strip().lower()
    password = str(payload.get("password", ""))
    if not email or "@" not in email or len(password) < 8:
        return jsonify({"ok": False, "message": "Use a valid email and a password with at least 8 characters."}), 400

    try:
        with db() as conn:
            cur = conn.cursor()
            if USING_POSTGRES:
                cur.execute(
                    "INSERT INTO users (email, password_hash, created_at) VALUES (%s, %s, %s) RETURNING id",
                    (email, generate_password_hash(password), datetime.utcnow().isoformat()),
                )
                user_id = cur.fetchone()["id"]
            else:
                cur.execute(
                    "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
                    (email, generate_password_hash(password), datetime.utcnow().isoformat()),
                )
                user_id = cur.lastrowid
        save_snapshot(user_id, default_snapshot())
    except sqlite3.IntegrityError:
        return jsonify({"ok": False, "message": "An account with that email already exists."}), 409
    except Exception as exc:
        if exc.__class__.__name__ == "UniqueViolation":
            return jsonify({"ok": False, "message": "An account with that email already exists."}), 409
        raise

    session.clear()
    session["user_id"] = user_id
    return jsonify({"ok": True, "user": {"id": user_id, "email": email}})


@app.post("/api/auth/login")
def auth_login():
    payload = read_json_body()
    email = str(payload.get("email", "")).strip().lower()
    password = str(payload.get("password", ""))
    user = fetch_one("SELECT id, email, password_hash FROM users WHERE email = ?", (email,))

    if not user or not check_password_hash(user["password_hash"], password):
        return jsonify({"ok": False, "message": "Invalid email or password."}), 401

    session.clear()
    session["user_id"] = user["id"]
    return jsonify({"ok": True, "user": {"id": user["id"], "email": user["email"]}})


@app.post("/api/auth/logout")
def auth_logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/snapshot")
@login_required
def api_snapshot_get():
    snapshot = load_snapshot(current_user_id())
    snapshot["settings"] = public_settings(snapshot.get("settings"))
    return jsonify({"ok": True, "snapshot": snapshot})


@app.post("/api/snapshot")
@login_required
def api_snapshot_post():
    payload = read_json_body()
    current = load_snapshot(current_user_id())
    snapshot = merge_snapshot(payload.get("snapshot", payload), current)
    save_snapshot(current_user_id(), snapshot)
    public = json.loads(json.dumps(snapshot))
    public["settings"] = public_settings(public.get("settings"))
    return jsonify({"ok": True, "snapshot": public})


@app.get("/api/trades")
@login_required
def api_trades_get():
    return jsonify({"trades": load_snapshot(current_user_id()).get("trades", [])})


@app.post("/api/import-csv")
@login_required
def api_import_csv():
    upload = flask_request.files.get("file")
    if not upload:
        return jsonify({"ok": False, "message": "Upload a CSV file."}), 400

    rows = read_csv_upload(upload)
    imported = []
    for row in rows:
        trade = trade_from_balance_row(row)
        if trade:
            imported.append(trade)

    snapshot = load_snapshot(current_user_id())
    seen = {trade.get("importId") or trade.get("id") for trade in snapshot.get("trades", [])}
    new_trades = [trade for trade in imported if (trade.get("importId") or trade.get("id")) not in seen]
    snapshot["trades"].extend(new_trades)
    save_snapshot(current_user_id(), snapshot)
    return jsonify({"ok": True, "imported": len(new_trades), "trades": snapshot["trades"]})


@app.get("/api/settings")
@login_required
def api_settings_get():
    return jsonify({"settings": public_settings(load_snapshot(current_user_id()).get("settings"))})


@app.post("/api/settings")
@login_required
def api_settings_post():
    payload = read_json_body()
    snapshot = load_snapshot(current_user_id())
    snapshot["settings"] = merge_settings(payload.get("settings", payload), snapshot.get("settings"))
    save_snapshot(current_user_id(), snapshot)
    return jsonify({"ok": True, "settings": public_settings(snapshot["settings"])})


@app.post("/api/tradovate/test")
@login_required
def api_tradovate_test():
    payload = read_json_body()
    snapshot = load_snapshot(current_user_id())
    if payload:
        snapshot["settings"] = merge_settings({"tradovate": payload.get("tradovate", payload)}, snapshot.get("settings"))
        save_snapshot(current_user_id(), snapshot)
    return jsonify(test_tradovate_connection(snapshot.get("settings", default_settings())))


def migrate_local_files_to_dev_user():
    if os.environ.get("SKIP_LOCAL_MIGRATION") == "1":
        return

    count = fetch_one("SELECT COUNT(*) AS count FROM users")["count"]
    if count:
        return

    trades = []
    settings = default_settings()
    if TRADES_FILE.exists():
        try:
            trades = json.loads(TRADES_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            trades = []
    if SETTINGS_FILE.exists():
        try:
            settings = merge_settings(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")), default_settings())
        except (json.JSONDecodeError, OSError):
            settings = default_settings()

    if not trades and settings == default_settings():
        return

    email = os.environ.get("DEV_MIGRATION_EMAIL", "owner@example.com")
    password = os.environ.get("DEV_MIGRATION_PASSWORD", "ChangeMe123!")
    with db() as conn:
        cur = conn.cursor()
        if USING_POSTGRES:
            cur.execute(
                "INSERT INTO users (email, password_hash, created_at) VALUES (%s, %s, %s) RETURNING id",
                (email, generate_password_hash(password), datetime.utcnow().isoformat()),
            )
            user_id = cur.fetchone()["id"]
        else:
            cur.execute(
                "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
                (email, generate_password_hash(password), datetime.utcnow().isoformat()),
            )
            user_id = cur.lastrowid
    save_snapshot(user_id, {**default_snapshot(), "trades": trades, "settings": settings})
    print(f"Migrated local JSON files into {email} with password {password}")


def main():
    if not INDEX_FILE.exists():
        raise SystemExit(f"Missing {INDEX_FILE}. Put index.html next to futures_platform.py.")
    init_db()
    migrate_local_files_to_dev_user()
    url = f"http://localhost:{PORT}/index.html"
    print(f"Serving {INDEX_FILE}")
    print(f"Opening {url}")
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        webbrowser.open(url)
    app.run(host="127.0.0.1", port=PORT, debug=os.environ.get("FLASK_DEBUG") == "1")


init_db()

if __name__ == "__main__":
    main()
