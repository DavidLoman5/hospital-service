"""LINE Messaging API over stdlib urllib. Two endpoints and an HMAC - a SDK would pull in
pydantic, aiohttp and six more packages to save about forty lines.

The quota model is the whole reason this module has a database handle. On the free "light"
plan the account gets 200 messages a month and cannot buy more; replies (answering a user
with their replyToken) are NOT counted, pushes are. So every interactive answer goes
through reply() and is free, and push() - the only function that spends budget - is the
single place the caps are enforced.
"""

import hashlib
import hmac
import json
import base64
import urllib.error
import urllib.request

from bot import db

REPLY_PATH = "/v2/bot/message/reply"
PUSH_PATH = "/v2/bot/message/push"


class LineError(Exception):
    pass


def verify(channel_secret, body_bytes, signature_header):
    """base64(HMAC-SHA256(channel secret, raw request body)).

    Must run on the exact bytes as received - parsing or re-encoding the JSON first
    changes them and every signature fails. compare_digest, not ==, so a mismatch does not
    leak where it diverged through timing.
    """
    if not channel_secret or not signature_header:
        return False
    expected = base64.b64encode(
        hmac.new(channel_secret.encode("utf-8"), body_bytes, hashlib.sha256).digest())
    return hmac.compare_digest(expected, signature_header.encode("utf-8"))


def text_msg(text, quick=None):
    """A text message, optionally with quick-reply buttons. Quick replies are free and
    ride along on a reply, which is why the conversation leans on them instead of a rich
    menu: every next step is one tap away at no cost to the budget."""
    msg = {"type": "text", "text": text}
    if quick:
        msg["quickReply"] = {"items": [
            {"type": "action", "action": {"type": "message", "label": label[:20],
                                          "text": send}}
            for label, send in quick[:13]        # LINE caps quick replies at 13
        ]}
    return msg


def _post(cfg, path, payload):
    if cfg.get("dryRunLine"):
        print("[dry-run] POST %s %s" % (path, json.dumps(payload, ensure_ascii=False)))
        return {"dryRun": True}
    token = cfg.get("channelAccessToken")
    if not token:
        raise LineError("channelAccessToken is not configured")
    req = urllib.request.Request(
        cfg["lineApiBase"].rstrip("/") + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + token},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise LineError("HTTP %s: %s" % (exc.code, detail)) from None
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise LineError(str(exc)) from None


def reply(cfg, reply_token, messages):
    """Free and unmetered. Everything the user asks for is answered this way.

    A reply token is single-use and short-lived, so a failure here is not retried: the
    user asked a question and will simply ask again.
    """
    if isinstance(messages, dict):
        messages = [messages]
    return _post(cfg, REPLY_PATH, {"replyToken": reply_token, "messages": messages[:5]})


def push(conn, cfg, line_user_id, text, kind, ref=(None, None), occurrence="", reserve=0):
    """The ONLY function that spends monthly quota. Returns True if a push went out.

    `ref` is what the push is about - ("sub", id) or ("reminder", id) - and with `kind` and
    `occurrence` it is the dedupe key. `reserve` holds back that many units of the month's
    budget: 回診/領藥 reminders pass one so they can never eat the pushes that "it's your
    turn" needs.

    The budget claim happens before the send, not after: db.claim_push writes a row that a
    unique index rejects on the second attempt, so a retry or a restart mid-send cannot
    wake the same person twice or double-charge the budget. A send that then fails costs
    one unit of a deliberately generous margin - much better than the alternative.
    """
    if not cfg.get("pushEnabled", True):
        return False
    used = db.pushes_this_month(conn)
    if used >= cfg["monthlyPushBudget"] - reserve:
        # Deliberately silent to the user here: telling them costs a push. The next reply
        # they trigger carries the warning instead, for free.
        print("push refused: monthly budget %d reached (reserve %d)"
              % (cfg["monthlyPushBudget"], reserve))
        return False
    ref_type, ref_id = ref
    if not db.claim_push(conn, line_user_id, ref_type, ref_id, kind, occurrence):
        return False        # already sent
    try:
        _post(cfg, PUSH_PATH, {"to": line_user_id, "messages": [text_msg(text)]})
    except LineError as exc:
        print("push failed (budget unit already claimed): %s" % exc)
        return False
    return True


def budget_left(conn, cfg):
    return max(0, cfg["monthlyPushBudget"] - db.pushes_this_month(conn))
