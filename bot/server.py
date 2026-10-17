"""ThreadingHTTPServer front end for the LINE webhook. Python standard library only.

Binds loopback: the only way in from the internet is the Tailscale Funnel, which mounts
this at a subpath (/cmu) so the existing stock-dashboard keeps "/".

Webhook events are queued and answered by worker threads rather than inline, because
bot/fetch.py serialises the outbound requests to each hospital behind one lock - an event arriving
during another user's fetch would otherwise hold an HTTP request thread long enough for
LINE to time the endpoint out and start retrying.
"""

import json
import os
import queue
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import clock, config, convo, db, estimate, hospitals, line_api, watcher

MAX_BODY = 64 * 1024
_events = queue.Queue(maxsize=64)


def normalize_path(raw, prefix):
    """Route the same whether or not the tunnel stripped its mount point.

    Tailscale is expected to strip `--set-path`, but that is unverified for Funnel on
    1.102, and being wrong about it means a silent 404 on every webhook - a bot that looks
    healthy and answers nothing. Accepting both costs three lines.
    """
    path = raw.split("?", 1)[0]
    if prefix and (path == prefix or path.startswith(prefix + "/")):
        path = path[len(prefix):]
    return path.rstrip("/") or "/"


class Handler(BaseHTTPRequestHandler):
    cfg = None
    server_version = "cmu-bot"

    def respond(self, status, body=b"", content_type="application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        path = normalize_path(self.path, self.cfg["pathPrefix"])
        if path == "/healthz":
            conn = db.get(self.cfg["dbPath"])
            payload = {
                "ok": True,
                "day": clock.today(),
                "subscriptions": len(db.active_subscriptions(conn, clock.today())),
                "pushesThisMonth": db.pushes_this_month(conn),
                # Echoed so the one-time Funnel prefix check can be read straight off the
                # response instead of digging through journald.
                "rawPath": self.path,
            }
            return self.respond(200, json.dumps(payload).encode("utf-8"))
        self.respond(404)

    def do_POST(self):
        path = normalize_path(self.path, self.cfg["pathPrefix"])
        if path != "/callback":
            return self.respond(404)

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self.respond(413)
        body = self.rfile.read(length)

        # Signature is checked against the raw bytes, before any parsing. An unverified
        # body is never json.loads()ed: it is not from LINE, so it is not input.
        if not line_api.verify(self.cfg["channelSecret"], body,
                               self.headers.get("x-line-signature")):
            return self.respond(400, b'{"error":"bad signature"}')

        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            return self.respond(400, b'{"error":"bad json"}')

        for event in payload.get("events", []):
            try:
                _events.put_nowait(event)
            except queue.Full:
                print("event queue full, dropping an event")
        # 200 immediately - LINE's verify button sends an empty events array and expects
        # exactly this.
        self.respond(200, b'{"ok":true}')

    def log_message(self, fmt, *args):
        """Method, path and status only.

        The request body carries whatever the user typed, which on the identifier path is
        a 身分證號. There is deliberately no code path that can log it.
        """
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def worker(cfg, client):
    conn = db.get(cfg["dbPath"])
    while True:
        event = _events.get()
        try:
            _handle(conn, cfg, client, event)
        except Exception as exc:                     # never let one event kill the worker
            print("event handler error: %s: %s" % (type(exc).__name__, exc))
        finally:
            _events.task_done()


def _handle(conn, cfg, client, event):
    source = event.get("source") or {}
    user_id = source.get("userId")
    if not user_id:
        return
    if event.get("type") == "follow":
        db.touch_user(conn, user_id, cfg.get("ownerUserId", ""),
                      tuple(cfg.get("allowedUserIds", ())))
        print("new follower: %s" % user_id)          # the id to pass to `admin allow`
        return
    if event.get("type") != "message":
        return
    message = event.get("message") or {}
    if message.get("type") != "text":
        return
    token = event.get("replyToken")
    messages = convo.handle_text(conn, cfg, client, user_id, message.get("text", ""))
    if token and messages:
        try:
            line_api.reply(cfg, token, messages)
        except line_api.LineError as exc:
            print("reply failed: %s" % exc)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            db.close_all()      # one sqlite connection per thread; threads are per-request


def main():
    cfg = config.load()
    db.init(cfg["dbPath"])
    client = hospitals.build(cfg)

    def record(adapter, room, timecode, snap):
        # Every real fetch, from a query or the watcher, feeds the wait-time estimate.
        estimate.record(db.get(cfg["dbPath"]), adapter.id, room, timecode,
                        snap.get("date") or clock.today(), snap)
    client.set_recorder(record)

    for _ in range(2):
        threading.Thread(target=worker, args=(cfg, client), daemon=True).start()
    threading.Thread(target=watcher.run, args=(cfg, client), daemon=True).start()

    Handler.cfg = cfg
    httpd = Server((cfg["host"], cfg["port"]), Handler)
    print("listening on %s:%d (prefix %s)" % (cfg["host"], cfg["port"], cfg["pathPrefix"]))
    if not cfg["channelSecret"]:
        print("WARNING: channelSecret is empty - every webhook will be rejected")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
