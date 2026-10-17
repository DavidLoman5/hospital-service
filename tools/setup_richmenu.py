"""Creates the rich menu on LINE and makes it the default for every user.

Standard library only - the image is uploaded as a raw PNG body, not multipart, which is
what LINE's content endpoint expects and what keeps this script dependency-free.

    python3 tools/setup_richmenu.py            # create + upload + set default
    python3 tools/setup_richmenu.py --list     # what exists now
    python3 tools/setup_richmenu.py --clear    # unset default and delete all

Re-running replaces the menu: LINE has a per-channel limit on rich menus, and leaving
orphans behind eventually hits it.
"""

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot import config                                          # noqa: E402

API = "https://api.line.me"
DATA_API = "https://api-data.line.me"
IMAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "richmenu.png")

W, H = 2500, 1686
COLS, ROWS = 3, 2
# Each cell sends the same text a user could type, so the menu is a shortcut to the
# conversation rather than a second, separate interface to keep working. Order matches
# CELLS in make_richmenu.py.
TEXTS = ["查進度", "我的提醒", "回診", "領藥", "換醫院", "說明"]


def _areas():
    areas = []
    for i, text in enumerate(TEXTS):
        row, col = divmod(i, COLS)
        x, y = col * (W // COLS), row * (H // ROWS)
        w = W - x if col == COLS - 1 else W // COLS
        h = H - y if row == ROWS - 1 else H // ROWS
        areas.append({"bounds": {"x": x, "y": y, "width": w, "height": h},
                      "action": {"type": "message", "text": text}})
    return areas


MENU = {
    "size": {"width": W, "height": H},
    "selected": True,
    "name": "主選單",
    "chatBarText": "選單",
    "areas": _areas(),
}


def call(base, path, method="GET", body=None, content_type="application/json"):
    cfg = config.load()
    req = urllib.request.Request(
        base + path, data=body,
        headers={"Authorization": "Bearer " + cfg["channelAccessToken"],
                 **({"Content-Type": content_type} if body else {})},
        method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        sys.exit("%s %s -> HTTP %s: %s" % (method, path, exc.code,
                                           exc.read().decode("utf-8", "replace")[:400]))


def clear():
    try:
        call(API, "/v2/bot/user/all/richmenu", "DELETE")
        print("unset default rich menu")
    except SystemExit:
        pass
    for m in call(API, "/v2/bot/richmenu/list").get("richmenus", []):
        call(API, "/v2/bot/richmenu/" + m["richMenuId"], "DELETE")
        print("deleted", m["richMenuId"])


def main(argv):
    if "--list" in argv:
        print(json.dumps(call(API, "/v2/bot/richmenu/list"), ensure_ascii=False, indent=2))
        return
    if "--clear" in argv:
        return clear()

    if not os.path.exists(IMAGE):
        sys.exit("missing %s - run tools/make_richmenu.py first" % IMAGE)

    clear()                                   # replace, do not accumulate
    menu_id = call(API, "/v2/bot/richmenu", "POST",
                   json.dumps(MENU).encode())["richMenuId"]
    print("created", menu_id)

    with open(IMAGE, "rb") as fh:
        call(DATA_API, "/v2/bot/richmenu/%s/content" % menu_id, "POST",
             fh.read(), "image/png")
    print("uploaded image")

    call(API, "/v2/bot/user/all/richmenu/%s" % menu_id, "POST", b"")
    print("set as default for all users")


if __name__ == "__main__":
    main(sys.argv[1:])
