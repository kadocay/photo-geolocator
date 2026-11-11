# photo-geolocator-01 entry (Python 3.13, standard library only).
#
# Strategy
# --------
# 1. Primary path: one vision-model call per photo through the kit gateway
#    (Anthropic Messages, claude-sonnet-5-5, max_tokens 1024) -- the same model
#    and budget shape as the house baseline (which costs ~$0.15/trial), but with
#    a structured evidence-first prompt instead of the baseline's bare
#    "where was this taken?" prompt. The model must first list the visible
#    evidence (script/language on signs, driving side, road markings, plates,
#    vegetation/climate, architecture, terrain) and only then commit to a
#    country/region and a lat/lon, with a self-reported confidence. It must
#    always give its best guess, never refuse, never answer "unknown".
# 2. Repair pass: photos with no usable coordinates, or confidence < 0.35,
#    get exactly one retry with a minimal prompt asking for JSON only.
#    The repair pass is skipped entirely if pass 1 was refused for auth
#    (no API key pushed, HTTP 401) or budget (budget_exhausted), because a
#    retry cannot help then and would only burn the trial's $1 budget.
# 3. Code-only fallback: any photo still unanswered gets a fixed prior point
#    (see FALLBACK_LAT/LON). This runs when no model key was pushed at all,
#    so the entry still answers all 25 photos instead of leaving them at the
#    maximum 20,015 km error each. No photo is ever looked up: the fallback
#    is one constant, not a table of photos/places, and no EXIF/metadata is
#    read (the photos carry none anyway).
#
# Determinism: every model request is a pure function of the photo bytes and
# fixed prompts. Calls run 4 at a time but results are collected by photo
# index and answers are posted in p01..p25 order, so the replay sends the
# same requests and leaves the same answers. No wall clock, no unseeded
# randomness, no network except the env and the gateway. No API key is
# stored in this file: the gateway key comes from the environment.
#
# Env vars (set by the trial harness):
#   MAKEFUN_ENV_URL, MAKEFUN_PROMPT_FILE (read, informational only),
#   ANTHROPIC_BASE_URL, ANTHROPIC_API_KEY, MAKEFUN_TRIAL_SEED (unused:
#   this entry needs no randomness).

import base64
import concurrent.futures
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

MODEL = "claude-sonnet-5-5"
MAX_TOKENS = 1024
CONCURRENCY = 4
RETRIES = 3
# Transient (provider-side) statuses worth retrying with fixed backoff.
# A gateway refusal (4xx, incl. 401 no key / budget_exhausted) is final.
RETRY_STATUSES = (408, 429, 500, 502, 503, 504, 529)
FINAL_STATUSES = (400, 401, 403, 404)

# Structured evidence-first prompt. Deliberately short: the image dominates
# the input cost, and the output cap is 1024 tokens.
PROMPT_1 = (
    "You are an expert photo geolocator. Look carefully at this street-level photo "
    "and work out where on Earth it was taken. First note the visible evidence: "
    "any text, script or language on signs; which side of the road traffic drives on; "
    "road markings, signs and license plates; vegetation, climate and terrain; "
    "architecture and building style. Then commit to your best estimate, even if you "
    "are unsure -- never refuse, never answer unknown, always give coordinates. "
    'Reply with exactly one JSON object and nothing else: '
    '{"country": "<2-letter code or ?>", "region": "<place/city/area>", '
    '"evidence": "<one short sentence>", "confidence": <0.0 to 1.0>, '
    '"lat": <degrees north>, "lon": <degrees east>}.'
)
# Minimal retry prompt for photos pass 1 could not place.
PROMPT_2 = (
    "Where was this photo taken? Give your best estimate even if unsure. "
    'Reply with exactly one JSON object and nothing else: '
    '{"lat": <degrees north>, "lon": <degrees east>}.'
)

# Code-only fallback point (single constant; see header). Chosen as a rough
# landmass prior for Mapillary/OpenStreetView-5M street photos, which are
# concentrated in the Americas, Europe and Asia around the mid-latitudes:
# 35N 10E sits in the Mediterranean, minimising the worst-case distance to
# the bulk of inhabited land. It is expected to err by ~7,000 km on average
# on practice photos -- far worse than a vision model, far better than
# leaving a photo unanswered (20,015 km).
FALLBACK_LAT = 35.0
FALLBACK_LON = 10.0

LOW_CONFIDENCE = 0.35


def log(msg):
    sys.stderr.write("geolocator: %s\n" % msg)
    sys.stderr.flush()


def http_json(method, url, headers, body=None, timeout=120):
    """One HTTP exchange; returns (status, parsed-JSON-or-None, raw-text)."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as res:
            raw = res.read()
    except urllib.error.HTTPError as err:
        try:
            raw = err.read()
        except Exception:
            raw = b""
        return err.code, None, raw.decode("utf-8", errors="replace")
    except Exception as err:  # connection failed before any answer
        return 0, None, str(err)
    try:
        return res.status, json.loads(raw.decode("utf-8")), raw.decode("utf-8", errors="replace")
    except Exception:
        return res.status, None, raw.decode("utf-8", errors="replace")


def http_bytes(url, timeout=120):
    req = urllib.request.Request(url, method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as err:
        return err.code, b""
    except Exception:
        return 0, b""


def is_lat(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and -90 <= v <= 90


def is_lon(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and -180 <= v <= 180


def parse_coords(text):
    """(lat, lon, confidence) from a model reply, or (None, None, None).

    Prefers the first JSON object carrying lat/lon; falls back to the first
    two in-range numbers in the text (the baseline's fallback).
    """
    if not text:
        return None, None, None
    for m in re.finditer(r"\{[^{}]*\}", text):
        try:
            obj = json.loads(m.group(0))
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        lat = obj.get("lat", obj.get("latitude"))
        lon = obj.get("lon", obj.get("lng", obj.get("longitude")))
        if is_lat(lat) and is_lon(lon):
            conf = obj.get("confidence")
            if not (isinstance(conf, (int, float)) and 0 <= conf <= 1):
                conf = None
            return float(lat), float(lon), conf
    nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", text)]
    for i in range(len(nums) - 1):
        if is_lat(nums[i]) and is_lon(nums[i + 1]):
            return nums[i], nums[i + 1], None
    return None, None, None


class Gateway:
    """Minimal Anthropic Messages client for the trial gateway."""

    def __init__(self, base_url, api_key):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.auth_failed = False   # 401: no key pushed -- retries cannot help
        self.budget_failed = False  # budget_exhausted -- retries cannot help

    def ask(self, jpeg, prompt):
        """Model reply text, or None if the call was refused/failed."""
        body = {
            "model": MODEL,
            "max_tokens": MAX_TOKENS,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/jpeg",
                        "data": base64.b64encode(jpeg).decode("ascii")}},
                    {"type": "text", "text": prompt},
                ],
            }],
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }
        payload = json.dumps(body)
        for attempt in range(RETRIES + 1):
            req = urllib.request.Request(
                self.base_url + "/v1/messages", data=payload.encode("utf-8"),
                method="POST", headers=headers)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with opener.open(req, timeout=300) as res:
                    msg = json.loads(res.read().decode("utf-8"))
            except urllib.error.HTTPError as err:
                try:
                    raw = err.read().decode("utf-8", errors="replace")
                except Exception:
                    raw = ""
                if err.code == 401:
                    self.auth_failed = True
                    return None
                if "budget_exhausted" in raw:
                    self.budget_failed = True
                    return None
                if err.code in RETRY_STATUSES and attempt < RETRIES:
                    time.sleep(2 * (attempt + 1))
                    continue
                log("model call refused (HTTP %d): %s" % (err.code, raw[:200]))
                return None
            except Exception as err:
                if attempt < RETRIES:
                    time.sleep(2 * (attempt + 1))
                    continue
                log("model call failed: %s" % err)
                return None
            text = "".join(
                b.get("text", "") for b in msg.get("content", [])
                if isinstance(b, dict) and b.get("type") == "text")
            return text
        return None


def fetch_photos(env_url):
    status, data, _ = http_json("GET", env_url + "/photos",
                                {"Accept": "application/json"})
    if status != 200 or not isinstance(data, dict):
        raise RuntimeError("GET /photos failed: HTTP %d" % status)
    photos = data.get("photos") or []
    out = []
    for p in photos:
        if p.get("answered"):
            continue
        out.append({"id": p["id"], "url": p["url"]})
    return out


def post_answers(env_url, answers):
    """Post answers; returns (ok, answered_count_or_None)."""
    status, data, raw = http_json(
        "POST", env_url + "/answers", {"Content-Type": "application/json"},
        {"answers": answers})
    if status == 200 and isinstance(data, dict):
        return True, data.get("answered")
    log("POST /answers HTTP %d: %s" % (status, raw[:200]))
    return False, None


def main():
    env_url = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
    if not env_url:
        log("MAKEFUN_ENV_URL is not set")
        return 1
    prompt_file = os.environ.get("MAKEFUN_PROMPT_FILE")
    if prompt_file:
        try:
            with open(prompt_file, "r", encoding="utf-8") as f:
                f.read(1)  # the brief; this entry needs nothing from it
        except OSError as err:
            log("cannot read prompt file: %s" % err)

    try:
        photos = fetch_photos(env_url)
    except Exception as err:
        log(str(err))
        return 1
    if not photos:
        log("no unanswered photos")
        return 0
    log("%d photos to place" % len(photos))

    # Fetch all JPEGs (4 at a time; results indexed by photo order).
    jpegs = [b""] * len(photos)

    def _fetch(i):
        status, data = http_bytes(env_url + photos[i]["url"])
        if status == 200 and data:
            jpegs[i] = data
        else:
            log("GET %s failed: HTTP %d" % (photos[i]["url"], status))

    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        list(ex.map(_fetch, range(len(photos))))

    results = [None] * len(photos)   # (lat, lon) or None
    confidences = [None] * len(photos)

    gateway = None
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if base_url and api_key:
        gateway = Gateway(base_url, api_key)
    else:
        log("no Anthropic gateway/key in env; model-free fallback only")

    if gateway is not None:
        # Pass 1: one structured call per photo, 4 at a time.
        def _ask1(i):
            if not jpegs[i]:
                return
            text = gateway.ask(jpegs[i], PROMPT_1)
            if text is None:
                return
            lat, lon, conf = parse_coords(text)
            if lat is None:
                log("no coordinates in pass-1 answer for %s: %.200s"
                    % (photos[i]["id"], text))
                return
            results[i] = (lat, lon)
            confidences[i] = conf

        with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
            list(ex.map(_ask1, range(len(photos))))

        # Pass 2 (repair): only missing / low-confidence photos, only if the
        # gateway did not refuse pass 1 for auth or budget reasons.
        if not gateway.auth_failed and not gateway.budget_failed:
            need = [i for i in range(len(photos))
                    if results[i] is None
                    or (confidences[i] is not None
                        and confidences[i] < LOW_CONFIDENCE)]
            if need:
                log("repair pass for %d photos" % len(need))

                def _ask2(i):
                    if not jpegs[i]:
                        return
                    text = gateway.ask(jpegs[i], PROMPT_2)
                    if text is None:
                        return
                    lat, lon, _ = parse_coords(text)
                    if lat is None:
                        return
                    # Keep the pass-1 answer if there was one and the repair
                    # offers nothing better to compare against; a repair only
                    # replaces a missing answer or a low-confidence one, and
                    # it is the only answer we have for the missing ones.
                    # For low-confidence pass-1 answers, accept the repair:
                    # a second, simpler read is the tie-breaker by design.
                    results[i] = (lat, lon)
                    confidences[i] = None

                with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
                    list(ex.map(_ask2, need))
        elif gateway.auth_failed:
            log("model calls refused (401): no key pushed; using fallback")
        elif gateway.budget_failed:
            log("model budget exhausted; keeping pass-1 answers")

    # Fallback: every photo still unanswered gets the fixed prior point.
    n_model = sum(1 for r in results if r is not None)
    n_fallback = 0
    answers = []
    for i, p in enumerate(photos):
        if results[i] is None:
            results[i] = (FALLBACK_LAT, FALLBACK_LON)
            n_fallback += 1
        lat, lon = results[i]
        lat = max(-90.0, min(90.0, lat))
        lon = max(-180.0, min(180.0, lon))
        answers.append({"id": p["id"],
                        "lat": round(lat, 6), "lon": round(lon, 6)})
    log("model placed %d, fallback placed %d, of %d"
        % (n_model, n_fallback, len(photos)))

    ok, _ = post_answers(env_url, answers)
    if not ok:
        # The POST is whole-or-nothing; check whether anything landed.
        try:
            remaining = fetch_photos(env_url)
        except Exception:
            remaining = photos
        if not remaining:
            log("all photos answered despite POST error")
            return 0
        ok, _ = post_answers(env_url, [a for a in answers
                                       if a["id"] in {p["id"] for p in remaining}])
        if not ok:
            return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as err:  # never die without trying to post what we have
        log("fatal: %s" % err)
        sys.exit(1)
