# photo-geolocator-01 — Entry Report

Competition: **photo-geolocator-01** ("Find where a photo was taken"), make.fun.
Deadline: 2026-11-12T05:30:00Z. Repo name for later (NOT created here):
`kadocay/photo-geolocator`. This task covered building only — no repo was
created, nothing was submitted, no wallets/keys were created or handled.

## What was built

`main.py` — a Python 3.13, standard-library-only entry (16,072 bytes),
plus this report and an MIT `LICENSE`. Total repo size is far under the
256 KiB cap (single-file cap 128 KiB).

### How it works

1. **Primary path — vision model through the kit gateway.** One
   `claude-sonnet-5-5` call per photo via Anthropic Messages
   (`POST {ANTHROPIC_BASE_URL}/v1/messages`, `max_tokens: 1024`, image sent
   inline as base64 JPEG), 4 calls at a time, answers posted in p01–p25
   order. Same model and budget shape as the house baseline (baseline:
   ~$0.15/trial, well inside the $1.00/trial budget), but with a
   **structured evidence-first prompt** instead of the baseline's bare
   "where was this taken?": the model must first list visible evidence
   (script/language on signs, driving side, road markings/plates,
   vegetation/climate/terrain, architecture) and then commit to
   country/region + lat/lon + a 0–1 confidence — always giving its best
   guess, never refusing or answering "unknown". Coordinates are parsed
   from the first JSON object with `lat`/`lon`, falling back to the first
   two in-range numbers (the baseline's fallback).
2. **Repair pass.** Photos with no usable coordinates, or confidence
   < 0.35, get exactly one retry with a minimal JSON-only prompt. The
   repair pass is skipped entirely if pass 1 was refused for auth
   (HTTP 401 — no API key pushed) or budget (`budget_exhausted`), since a
   retry cannot help then. A repair answer replaces a missing answer, or
   a low-confidence pass-1 answer.
3. **Code-only fallback.** Any photo still unanswered gets one fixed
   prior point, **35.0°N, 10.0°E** (a Mediterranean landmass prior for
   Mapillary/OSV-5M street photos). This is what runs when no model key
   was pushed at all, so the entry still answers all 25 photos instead of
   leaving them at the maximum 20,015 km error each. It is a single
   constant — not a table of photos/places — and no EXIF/metadata is read
   (the photos carry none; the kit strips it).

### Determinism / rules compliance

- No wall clock, no unseeded randomness (no randomness at all — the
  trial seed is not needed), no network except the env and the gateway,
  no API key in the repo, no model of its own, no trained weights /
  embeddings / photo-place table. Requests are a pure function of the
  photo bytes and the fixed prompts; parallel calls are collected by
  photo index and posted in order, so live and replay runs match.
- Transient model errors (408/429/5xx/529) are retried up to 3 times with
  fixed backoff; gateway refusals (4xx) are final, as the rules require.
- If a POST /answers fails, the entry re-checks GET /photos and retries
  once for the still-unanswered photos.

## Feasibility note (vision)

The subagent that built this has **no API key and no vision model of its
own**, and the entry was therefore **never tested against a real model**.
That is by design of the competition, not a gap in the entry: the kit
provides a model gateway, and the entry is expected to call an allowlisted
vision model through it at trial time, billed to the entrant's own
Anthropic key (pushed via `makefun secrets push` before submitting). All
eight allowlisted models are vision models per the rules. **If no key is
pushed, every model call is answered 401 and the entry runs entirely on
its code-only fallback** (see scores below) — legal, but it will not beat
the house baseline. A key must be pushed for this entry to be competitive.

A stdlib-only local vision substitute was investigated and rejected on
evidence: a minimal JPEG DC-coefficient thumbnail decoder was built and
run over all 75 practice photos; the correlation between mean DC
luminance and |latitude| was **0.002 (no signal)**, and a full pure-Python
JPEG decode + colour heuristics would be large, slow, and unreliable.
No such pseudo-vision is claimed.

## Verification (actually run, 2026-11-11)

Kit: `~/workspace/makefun-werewolf/kits/photo-geolocator-01/photo-geolocator-01-kit/`.
Local Python 3.12.3 / Node v24.22.1 (trial uses Python 3.13 / Node 22 —
code uses only stdlib features common to both). **Docker is not
installed here, so `makefun grade-local --driver docker` could NOT be
run; the kit's env server and scorer were run directly instead.**
No live model call was made (no key).

### 1. Model-free fallback, practice trial-0, kit env + kit scorer

```sh
MAKEFUN_ENV_DATA_DIR=<kit>/practice/trial-0/input/env MAKEFUN_STATE_DIR=/tmp/geo-test/state \
  MAKEFUN_ENV_HOST=127.0.0.1 MAKEFUN_ENV_PORT=18081 node <kit>/program/env/server.mjs &
MAKEFUN_ENV_URL=http://127.0.0.1:18081 MAKEFUN_PROMPT_FILE=<kit>/practice/trial-0/input/prompt.txt \
  MAKEFUN_TRIAL_SEED=testseed python3 -P -s main.py
# stderr: "25 photos to place / no Anthropic gateway/key in env; model-free fallback only / model placed 0, fallback placed 25, of 25" ; exit 0
MAKEFUN_OUTPUT_DIR=/tmp/geo-test/state/export MAKEFUN_REFERENCE_DIR=<kit>/practice/trial-0/reference \
  MAKEFUN_SCORES_DIR=/tmp/geo-test/scores node <kit>/program/scorer/score.mjs
```

Result (kit scorer, trial-0): **score 266,448; working 1; total_km 233,929;
median_km 9,380; within_1km 0%; within_25km 0%; within_200km 0%;
within_750km 0%; within_2,500km 0%.** All 25 answered at (35, 10).

Fallback point (35, 10) scored offline with the scorer's own haversine
math on the other practice trials (not via the kit scorer binary):
trial-1 score 331,305 (total 169,072 km, median 5,604 km), trial-2 score
361,106 (total 139,271 km, median 4,669 km). Mean fallback score over the
3 practice trials ≈ 319,620. For comparison, leaving all 25 unanswered
scores 0.

### 2. Model path, mock gateway (4 scenarios, practice trial-1 env)

A local mock Anthropic gateway (in /tmp, not shipped) returned canned
replies; the real gateway/model was never contacted:

| Scenario | Result |
|---|---|
| Valid JSON coords, conf 0.9 | 25/25 model answers posted, exit 0 |
| Low confidence (0.1) pass 1 | Repair pass ran for 25 photos; repair coords posted, exit 0 |
| Pass 1 garbage text (no coords) | Repair pass ran; valid repair coords posted, exit 0 |
| HTTP 401 (no key) | No repair pass; fallback placed 25/25, exit 0 |

The mock "Paris for every photo" run on trial-1 was also scored with the
kit scorer: score 339,041, total_km 161,336, median_km 6,089 — confirming
the full post → export → score pipeline, not the model's accuracy
(the mock's coordinates were deliberately fixed, not real guesses).

### 3. Determinism

The mock-gateway run was executed twice on trial-1; the exported
`state.json` files were **byte-identical** (`cmp` clean). `python3
-m py_compile main.py` passes. `main.py` = 16,072 bytes (cap 128 KiB).

## Known weaknesses (honest)

1. **Untested against a real vision model.** The evidence-first prompt
   is the intended edge over the baseline, but whether it actually beats
   the baseline's plain prompt is unverified — it depends on the model
   (claude-sonnet-5-5) and on the entrant pushing an Anthropic key. A
   dev-trial grade on the real gateway (with a key) is the first thing to
   do after submitting; if the structured prompt underperforms, the
   prompt is the only thing to change.
2. **Repair pass replaces low-confidence answers blindly.** A second
   reading is used as the tie-breaker by design, but there is no check
   that the repair is actually better; on photos where pass 1 was
   already right, it can make the answer worse.
3. **No second model / no cross-check.** A cheaper haiku first pass or
   an opus second opinion was not added: the $1.00 worst-case budget
   accounting (input tokens billed at body-bytes/3) makes a second full
   round of 25 calls a budget risk, and the entry stays inside the
   baseline's proven cost shape instead.
4. **Fallback is very weak (~7,000 km mean error).** It only exists so
   a key-less run still answers everything. The fixed point was chosen
   as a generic landmass prior; the practice-optimal point (55°N, 25°E)
   was deliberately NOT used — it wins on trials 1–2 but loses badly on
   trial-0 (best point there is off Brazil), i.e. it is practice-pool
   overfitting, not a real prior.
5. **Local test used Python 3.12, not the trial's 3.13; `grade-local`
   with Docker was not runnable here.** Both are low-risk (stdlib-only
   code, no version-specific features), but a real `grade-local` run
   before submitting is recommended.
6. **No use of photo dimensions / file size as a signal.** Checked and
   discarded: they carry no location information.

## Files

- `main.py` — the entry (Python 3.13, stdlib only)
- `LICENSE` — MIT
- `REPORT.md` — this file
