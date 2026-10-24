"""
YouTube Gameplay addon for Stremio / WuPlay (Python + Flask, no API key needed).

EDIT THE CHANNELS LIST BELOW, then deploy (see README steps in chat).
Each channel becomes its own row/catalog in the app, plus a "Latest (All)" row.
"""
import os
import re
import time
import threading
import xml.etree.ElementTree as ET

import requests
from flask import Flask, jsonify

# ---------------------------------------------------------------------------
# 1) YOUR CHANNELS  ->  ("Name shown in app", "@handle or UC... channel id")
#    These are just examples. Replace them with gameplay channels you like.
# ---------------------------------------------------------------------------
CHANNELS = [
    ("MKIceAndFire", "@MKIceAndFire"),
    ("GTX 1050 Ti", "@GTX1050TiFireflyGaming"),
    ("Gameranx", "@gameranx"),
]
# ---------------------------------------------------------------------------

CACHE_MINUTES = 20
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

app = Flask(__name__)
lock = threading.Lock()
channel_id_cache = {}   # "@handle" -> "UCxxxx"
video_cache = {}        # channel index -> (timestamp, [videos])
video_index = {}        # video id -> video dict (for meta lookups)


@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    return resp


def resolve_channel_id(ref):
    ref = ref.strip()
    if re.fullmatch(r"UC[\w-]{22}", ref):
        return ref
    if ref in channel_id_cache:
        return channel_id_cache[ref]
    handle = ref if ref.startswith("@") else "@" + ref
    r = requests.get(
        f"https://www.youtube.com/{handle}",
        headers=UA,
        cookies={"CONSENT": "YES+1"},
        timeout=10,
    )
    for pattern in (
        r'"externalId":"(UC[\w-]{22})"',
        r'channel_id=(UC[\w-]{22})',
        r'"channelId":"(UC[\w-]{22})"',
    ):
        m = re.search(pattern, r.text)
        if m:
            channel_id_cache[ref] = m.group(1)
            return m.group(1)
    raise ValueError(f"Could not find channel id for {ref}")


def fetch_channel(index):
    """Return the latest videos for CHANNELS[index], cached."""
    now = time.time()
    with lock:
        cached = video_cache.get(index)
        if cached and now - cached[0] < CACHE_MINUTES * 60:
            return cached[1]
    name, ref = CHANNELS[index]
    videos = []
    try:
        cid = resolve_channel_id(ref)
        r = requests.get(
            f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}",
            headers=UA,
            timeout=10,
        )
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for e in root.findall("a:entry", NS):
            vid = e.findtext("yt:videoId", namespaces=NS)
            if not vid:
                continue
            desc = e.findtext("media:group/media:description", default="", namespaces=NS)
            videos.append({
                "id": vid,
                "title": e.findtext("a:title", default=vid, namespaces=NS),
                "published": e.findtext("a:published", default="", namespaces=NS),
                "description": (desc or "")[:600],
                "channel": name,
            })
    except Exception as err:  # keep the addon alive if one channel fails
        print(f"[warn] {name}: {err}")
        with lock:
            old = video_cache.get(index)
        return old[1] if old else []
    with lock:
        video_cache[index] = (now, videos)
        for v in videos:
            video_index[v["id"]] = v
    return videos


def videos_for_catalog(cat_id):
    if cat_id == "yt-all":
        allv = []
        for i in range(len(CHANNELS)):
            allv.extend(fetch_channel(i))
        return sorted(allv, key=lambda v: v["published"], reverse=True)
    m = re.fullmatch(r"yt-(\d+)", cat_id)
    if m and int(m.group(1)) < len(CHANNELS):
        return fetch_channel(int(m.group(1)))
    return []


def thumb(vid):
    return f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"


def to_meta(v):
    year = v["published"][:4] if v["published"] else ""
    return {
        "id": "yt:" + v["id"],
        "type": "movie",
        "name": v["title"],
        "poster": thumb(v["id"]),
        "posterShape": "landscape",
        "background": f"https://i.ytimg.com/vi/{v['id']}/maxresdefault.jpg",
        "description": f"{v['channel']}\n\n{v['description']}",
        "releaseInfo": year,
        "released": v["published"],
    }


MANIFEST = {
    "id": "community.youtube.gameplay",
    "version": "1.0.0",
    "name": "YouTube Gameplay",
    "description": "Latest gameplay videos from your favourite YouTube channels",
    "resources": ["catalog", "meta", "stream"],
    "types": ["movie"],
    "idPrefixes": ["yt:"],
    "catalogs": (
        [{"type": "movie", "id": "yt-all", "name": "Gameplay - Latest (All)"}]
        + [
            {"type": "movie", "id": f"yt-{i}", "name": f"Gameplay - {name}"}
            for i, (name, _) in enumerate(CHANNELS)
        ]
    ),
}


@app.route("/")
def home():
    return "YouTube Gameplay addon is running. Add /manifest.json to the URL to install it."


@app.route("/manifest.json")
def manifest():
    return jsonify(MANIFEST)


@app.route("/catalog/<type_>/<cat_id>.json")
@app.route("/catalog/<type_>/<cat_id>/<extra>.json")
def catalog(type_, cat_id, extra=None):
    # RSS feeds only hold the latest ~15 videos, so ignore further pages
    if extra and "skip=" in extra and not extra.endswith("skip=0"):
        return jsonify({"metas": []})
    return jsonify({"metas": [to_meta(v) for v in videos_for_catalog(cat_id)]})


@app.route("/meta/<type_>/<path:vid>.json")
def meta(type_, vid):
    yid = vid.replace("yt:", "", 1)
    if yid not in video_index:  # cold start: load everything once
        for i in range(len(CHANNELS)):
            fetch_channel(i)
    v = video_index.get(yid)
    return jsonify({"meta": to_meta(v) if v else {}})


@app.route("/stream/<type_>/<path:vid>.json")
def stream(type_, vid):
    yid = vid.replace("yt:", "", 1)
    return jsonify({
        "streams": [
            {"name": "YouTube", "title": "Play in app", "ytId": yid},
            {"name": "YouTube", "title": "Open in YouTube app", "externalUrl": f"https://www.youtube.com/watch?v={yid}"},
        ]
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 7000)))
