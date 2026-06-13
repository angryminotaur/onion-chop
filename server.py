#!/usr/bin/env python3
import argparse
import datetime
import hashlib
import hmac
import json
import os
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")


ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "tournament-state.json"

ENTRY_ONIONS = int(os.environ.get("ONION_CHOP_ENTRY", "5"))
HOUSE_USERNAME = os.environ.get("ONION_CHOP_HOUSE_USERNAME", "Caleb Martin")
REQUESTER = os.environ.get("ONION_CHOP_REQUESTER", "onion-chop")
API_BASE = os.environ.get("ONION_API_BASE", "https://oniondao.dev").rstrip("/")
API_KEY = os.environ.get("ONION_EXTERNAL_API_KEY", "")
CALLBACK_URL = os.environ.get("ONION_CHOP_CALLBACK_URL", "")
CALLBACK_SECRET = os.environ.get("ONION_CHOP_CALLBACK_SECRET", "")
ADMIN_PIN = os.environ.get("ONION_CHOP_ADMIN_PIN", "")
ALLOW_INSECURE_TLS = os.environ.get("ONION_CHOP_ALLOW_INSECURE_TLS", "") == "1"
START_ON_APPROVAL = os.environ.get("ONION_CHOP_START_ON_APPROVAL", "1") == "1"


def now_ms():
    return int(time.time() * 1000)


def new_id(prefix):
    return f"{prefix}_{secrets.token_hex(8)}"


def clean_name(value):
    allowed = set(" _-@.")
    name = "".join(ch for ch in str(value or "") if ch.isalnum() or ch in allowed)
    return name.strip()[:64] or "YOU"


def default_state():
    return {
        "pool": 0,
        "houseVault": 0,
        "entries": [],
        "settlements": [],
        "mode": "real" if API_KEY else "demo",
    }


def load_state():
    if not DATA_FILE.exists():
        return default_state()
    with DATA_FILE.open("r", encoding="utf-8") as handle:
        state = json.load(handle)
    base = default_state()
    base.update(state)
    base["mode"] = "real" if API_KEY else "demo"
    return base


def save_state(state):
    tmp = DATA_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
    tmp.replace(DATA_FILE)


def json_response(handler, payload, status=200):
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def read_json(handler):
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    return json.loads(raw.decode("utf-8") or "{}")


def public_entry(entry, include_token=False):
    data = {
        "id": entry["id"],
        "username": entry["username"],
        "amount": entry["amount"],
        "status": entry["status"],
        "score": entry.get("score"),
        "onionRequestId": entry.get("onionRequestId"),
        "onionStatus": entry.get("lastOnionStatus"),
        "createdAt": entry["createdAt"],
        "paidAt": entry.get("paidAt"),
        "scoredAt": entry.get("scoredAt"),
    }
    if entry.get("onionRequestId") and not str(entry["onionRequestId"]).startswith("demo_"):
        data["approvalUrl"] = f"{API_BASE}/portal/onions"
    if include_token and entry.get("runToken"):
        data["runToken"] = entry["runToken"]
    return data


def score_rows(state):
    scored = [entry for entry in state["entries"] if isinstance(entry.get("score"), int)]
    scored.sort(key=lambda item: item["score"], reverse=True)
    return [{"name": row["username"], "score": row["score"]} for row in scored[:12]]


def payout_preview(pool):
    first = int(pool * 0.6)
    second = int(pool * 0.2)
    third = int(pool * 0.1)
    house = max(0, pool - first - second - third)
    return {"first": first, "second": second, "third": third, "house": house}


def tournament_payload(state):
    payouts = payout_preview(state["pool"])
    return {
        "entryAmount": ENTRY_ONIONS,
        "houseUsername": HOUSE_USERNAME,
        "mode": state["mode"],
        "pool": state["pool"],
        "houseVault": state.get("houseVault", 0),
        "leaderboard": score_rows(state),
        "players": len(state["entries"]),
        "paidPlayers": len([entry for entry in state["entries"] if entry["status"] == "completed"]),
        "approvedPlayers": len([entry for entry in state["entries"] if entry["status"] in ("approved", "completed")]),
        "payouts": payouts,
        "lastSettlement": state["settlements"][-1] if state["settlements"] else None,
    }


def onion_headers():
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    return headers


def onion_request(path, method="GET", payload=None):
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{API_BASE}{path}",
        data=body,
        method=method,
        headers=onion_headers(),
    )
    try:
        context = ssl._create_unverified_context() if ALLOW_INSECURE_TLS else None
        with urllib.request.urlopen(request, timeout=15, context=context) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Onion API {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Onion API connection failed: {exc.reason}") from exc


def create_transfer(username, recipient, amount, note, external_id, metadata=None):
    payload = {
        "type": "transfer",
        "username": username,
        "recipientUsername": recipient,
        "amount": amount,
        "requester": REQUESTER,
        "externalId": external_id,
        "note": note,
    }
    if CALLBACK_URL:
        payload["callbackUrl"] = CALLBACK_URL
    if CALLBACK_SECRET:
        payload["callbackSecret"] = CALLBACK_SECRET
    if metadata:
        payload["metadata"] = metadata
    if not API_KEY:
        return {"id": f"demo_{external_id}", "status": "completed", "demo": True}
    return onion_request("/api/public/onions/requests", "POST", payload)


def refresh_entry_payment(state, entry):
    if entry["status"] not in ("pending", "approved") or not entry.get("onionRequestId") or not API_KEY:
        return entry
    try:
        result = onion_request(f"/api/public/onions/requests/{entry['onionRequestId']}")
    except RuntimeError as exc:
        entry["lastSyncError"] = str(exc)
        save_state(state)
        return entry
    entry.pop("lastSyncError", None)
    entry["lastOnionStatus"] = result.get("status")
    if result.get("status") == "completed":
        entry["status"] = "completed"
        entry["paidAt"] = now_ms()
        entry["runToken"] = entry.get("runToken") or new_id("run")
        if not entry.get("escrowCredited"):
            state["pool"] += entry["amount"]
            entry["escrowCredited"] = True
    elif result.get("status") == "awaiting_badge_signature" and START_ON_APPROVAL:
        entry["status"] = "approved"
        entry["approvedAt"] = entry.get("approvedAt") or now_ms()
        entry["runToken"] = entry.get("runToken") or new_id("run")
    elif result.get("status") in ("denied", "failed"):
        entry["status"] = result["status"]
        entry["error"] = result.get("error")
    save_state(state)
    return entry


def find_entry(state, entry_id):
    for entry in state["entries"]:
        if entry["id"] == entry_id:
            return entry
    return None


def reusable_entry_for_username(state, username):
    candidates = [
        entry for entry in state["entries"]
        if entry.get("username") == username
        and entry.get("score") is None
        and entry.get("status") not in ("abandoned", "denied", "failed")
    ]
    for entry in candidates:
        refresh_entry_payment(state, entry)
    for entry in candidates:
        if entry.get("status") == "completed":
            return entry
    for entry in candidates:
        if entry.get("status") == "approved":
            return entry
    for entry in candidates:
        if entry.get("status") == "pending":
            return entry
    return None


def settle_tournament(state):
    if state["pool"] <= 0:
        raise ValueError("pool_empty")
    leaders = [entry for entry in state["entries"] if isinstance(entry.get("score"), int)]
    leaders.sort(key=lambda item: item["score"], reverse=True)
    if len(leaders) < 3:
        raise ValueError("not_enough_scored_players")
    payouts = payout_preview(state["pool"])
    rows = [
        {"place": "1st", "recipient": leaders[0]["username"] if len(leaders) > 0 else "UNCLAIMED", "amount": payouts["first"]},
        {"place": "2nd", "recipient": leaders[1]["username"] if len(leaders) > 1 else "UNCLAIMED", "amount": payouts["second"]},
        {"place": "3rd", "recipient": leaders[2]["username"] if len(leaders) > 2 else "UNCLAIMED", "amount": payouts["third"]},
        {"place": "House", "recipient": HOUSE_USERNAME, "amount": payouts["house"]},
    ]
    settlement = {
        "id": new_id("settle"),
        "pool": state["pool"],
        "createdAt": now_ms(),
        "payouts": rows,
        "transferRequests": [],
    }
    for payout in rows:
        if payout["recipient"] in ("UNCLAIMED", HOUSE_USERNAME) or payout["amount"] <= 0:
            continue
        request = create_transfer(
            HOUSE_USERNAME,
            payout["recipient"],
            payout["amount"],
            f"Onion Chop {payout['place']} payout",
            f"{settlement['id']}_{payout['place']}_{payout['recipient']}",
            {"settlementId": settlement["id"], "place": payout["place"]},
        )
        settlement["transferRequests"].append({
            "place": payout["place"],
            "recipient": payout["recipient"],
            "amount": payout["amount"],
            "requestId": request.get("id"),
            "status": request.get("status"),
        })
    state["houseVault"] = state.get("houseVault", 0) + payouts["house"]
    state["pool"] = 0
    state["settlements"].append(settlement)
    save_state(state)
    return settlement


class OnionChopHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, format, *args):
        print(f"[onion-chop] {self.address_string()} {format % args}")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/tournament":
            state = load_state()
            for entry in state["entries"]:
                refresh_entry_payment(state, entry)
            json_response(self, tournament_payload(load_state()))
            return
        if parsed.path.startswith("/api/entry/"):
            entry_id = parsed.path.rsplit("/", 1)[-1]
            state = load_state()
            entry = find_entry(state, entry_id)
            if not entry:
                json_response(self, {"error": "entry_not_found"}, 404)
                return
            refresh_entry_payment(state, entry)
            json_response(self, {"entry": public_entry(entry, include_token=entry["status"] in ("approved", "completed"))})
            return
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/enter":
                self.handle_enter()
            elif parsed.path == "/api/score":
                self.handle_score()
            elif parsed.path.startswith("/api/entry/") and parsed.path.endswith("/abandon"):
                self.handle_abandon_entry(parsed.path.split("/")[-2])
            elif parsed.path == "/api/admin/settle":
                self.handle_settle()
            elif parsed.path == "/api/onion-callback":
                self.handle_callback()
            else:
                json_response(self, {"error": "not_found"}, 404)
        except ValueError as exc:
            json_response(self, {"error": str(exc)}, 400)
        except RuntimeError as exc:
            json_response(self, {"error": "onion_api_error", "detail": str(exc)}, 502)
        except Exception as exc:
            json_response(self, {"error": "server_error", "detail": str(exc)}, 500)

    def handle_enter(self):
        payload = read_json(self)
        username = clean_name(payload.get("username"))
        state = load_state()
        existing = reusable_entry_for_username(state, username)
        if existing:
            json_response(self, {
                "entry": public_entry(existing, include_token=existing["status"] in ("approved", "completed")),
                "tournament": tournament_payload(state),
            })
            return
        entry_id = new_id("entry")
        external_id = f"{entry_id}_{username}"
        request = create_transfer(
            username,
            HOUSE_USERNAME,
            ENTRY_ONIONS,
            "Onion Chop entry",
            external_id,
            {"entryId": entry_id},
        )
        completed = request.get("status") == "completed"
        entry = {
            "id": entry_id,
            "username": username,
            "amount": ENTRY_ONIONS,
            "status": "completed" if completed else "pending",
            "onionRequestId": request.get("id"),
            "lastOnionStatus": request.get("status"),
            "runToken": new_id("run") if completed else None,
            "createdAt": now_ms(),
            "paidAt": now_ms() if completed else None,
        }
        state["entries"].append(entry)
        if completed:
            state["pool"] += ENTRY_ONIONS
        save_state(state)
        json_response(self, {"entry": public_entry(entry, include_token=completed), "tournament": tournament_payload(state)})

    def handle_abandon_entry(self, entry_id):
        state = load_state()
        entry = find_entry(state, entry_id)
        if not entry:
            raise ValueError("entry_not_found")
        if entry["status"] == "completed":
            raise ValueError("entry_already_paid")
        username = entry.get("username")
        abandoned_at = now_ms()
        for candidate in state["entries"]:
            if candidate.get("username") != username:
                continue
            if candidate.get("score") is not None or candidate.get("status") == "completed":
                continue
            candidate["status"] = "abandoned"
            candidate["abandonedAt"] = abandoned_at
        save_state(state)
        json_response(self, {"entry": public_entry(entry), "tournament": tournament_payload(state)})

    def handle_score(self):
        payload = read_json(self)
        state = load_state()
        entry = find_entry(state, payload.get("entryId"))
        if not entry:
            raise ValueError("entry_not_found")
        refresh_entry_payment(state, entry)
        if entry["status"] not in ("approved", "completed"):
            raise ValueError("entry_not_paid")
        if payload.get("runToken") != entry.get("runToken"):
            raise ValueError("bad_run_token")
        if entry.get("score") is not None:
            raise ValueError("score_already_submitted")
        score = int(payload.get("score", -1))
        chops = int(payload.get("chopsUsed", -1))
        if score < 0 or chops != 20:
            raise ValueError("invalid_score")
        entry["score"] = score
        entry["chopsUsed"] = chops
        entry["scoredAt"] = now_ms()
        save_state(state)
        json_response(self, {"entry": public_entry(entry), "tournament": tournament_payload(state)})

    def handle_settle(self):
        payload = read_json(self)
        if ADMIN_PIN and payload.get("adminPin") != ADMIN_PIN:
            json_response(self, {"error": "admin_pin_required"}, 401)
            return
        state = load_state()
        settlement = settle_tournament(state)
        json_response(self, {"settlement": settlement, "tournament": tournament_payload(load_state())})

    def handle_callback(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length)
        if CALLBACK_SECRET:
            signature = self.headers.get("X-Onion-Signature", "")
            expected = hmac.new(CALLBACK_SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                json_response(self, {"error": "bad_signature"}, 401)
                return
        payload = json.loads(raw.decode("utf-8") or "{}")
        request_id = payload.get("id") or self.headers.get("X-Onion-Request-Id")
        state = load_state()
        changed = False
        for entry in state["entries"]:
            if entry.get("onionRequestId") != request_id:
                continue
            entry["lastOnionStatus"] = payload.get("status")
            if payload.get("status") == "completed" and entry["status"] in ("pending", "approved"):
                entry["status"] = "completed"
                entry["paidAt"] = now_ms()
                entry["runToken"] = entry.get("runToken") or new_id("run")
                if not entry.get("escrowCredited"):
                    state["pool"] += entry["amount"]
                    entry["escrowCredited"] = True
                changed = True
            elif payload.get("status") == "awaiting_badge_signature" and entry["status"] == "pending" and START_ON_APPROVAL:
                entry["status"] = "approved"
                entry["approvedAt"] = entry.get("approvedAt") or now_ms()
                entry["runToken"] = entry.get("runToken") or new_id("run")
                changed = True
            elif payload.get("status") in ("denied", "failed"):
                entry["status"] = payload["status"]
                entry["error"] = payload.get("error")
                changed = True
        if changed:
            save_state(state)
        json_response(self, {"ok": True})


def auto_settle_loop():
    last_settled_date = None
    while True:
        now = datetime.datetime.now(CENTRAL)
        today = now.date()
        if now.hour == 23 and now.minute == 50 and last_settled_date != today:
            last_settled_date = today
            try:
                state = load_state()
                settle_tournament(state)
                print(f"[onion-chop] Auto-settled at {now.isoformat()}")
            except ValueError as exc:
                print(f"[onion-chop] Auto-settle skipped: {exc}")
            except Exception as exc:
                print(f"[onion-chop] Auto-settle error: {exc}")
        time.sleep(30)


def main():
    parser = argparse.ArgumentParser(description="Onion Chop webapp and escrow server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    t = threading.Thread(target=auto_settle_loop, daemon=True)
    t.start()
    server = ThreadingHTTPServer((args.host, args.port), OnionChopHandler)
    mode = "real Onion API" if API_KEY else "demo local onions"
    print(f"Onion Chop running at http://{args.host}:{args.port}/ ({mode})")
    print(f"Auto-settle scheduled daily at 11:50 PM CT")
    server.serve_forever()


if __name__ == "__main__":
    main()
