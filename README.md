# Travel Agent

Grok skill, Chat personas, and API-backed search for live flight pricing.

The agent quotes the cheapest **all-in** fare that matches dates, cabin, time, and stop rules. It always prices the matching **one-ticket** fare (round-trip or multi-city) **and** two one-ways, then recommends whichever is cheaper. Flights within the configured `near_match_minutes` tolerance (30 by default) are retained and clearly labelled. Scraping Google Flights in a headless browser is **not** used — SerpAPI, Duffel, and Amadeus provide structured quotes.

## Files

| File | Use |
|---|---|
| `SKILL.md` | Grok Build skill (`/travel-agent`). Copy to `~/.grok/skills/travel-agent/SKILL.md`. |
| `scripts/search.py` | CLI search — SerpAPI → Duffel → Amadeus waterfall |
| `watches/*.yml` | Trip definitions (dates, flex, time windows, stop rules, benchmarks) |
| `chat-persona.md` | Grok Chat persona — **Rome/Paris** daily watch |
| `hawaii-chat-persona.md` | Grok Chat persona — **Hawaii** daily watch |
| `quotes/` | Saved daily run output (gitignored) |

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Add SERPAPI_API_KEY, DUFFEL_API_KEY, and/or AMADEUS_API_KEY + AMADEUS_API_SECRET
```

The repo loads `.env` automatically for Python scripts, tests, and Cursor/VS Code terminals (see `.vscode/settings.json`). Keep secrets in `.env` only — it is gitignored.

Get keys:
- [SerpAPI Google Flights](https://serpapi.com/google-flights-api)
- [Amadeus for Developers](https://developers.amadeus.com/) (free test tier)

## Run searches

```bash
python scripts/search.py --watch hawaii
python scripts/search.py --watch rome-paris
python scripts/search.py --config my-trip.yml
python scripts/search.py --watch hawaii --save   # also writes quotes/YYYY-MM-DD-hawaii.md
```

## Grok Build

```bash
mkdir -p ~/.grok/skills/travel-agent
cp SKILL.md ~/.grok/skills/travel-agent/
cp chat-persona.md hawaii-chat-persona.md ~/.grok/skills/travel-agent/
```

Then say `travel agent` or `/travel-agent`.

## Grok Chat

Paste `chat-persona.md`, `hawaii-chat-persona.md`, or `montreal-chat-persona.md` into Custom Instructions. Daily prompts:

- `Run the daily Rome/Paris check`
- `Run the daily Hawaii check`
- `Run the daily Montreal check`

Prefer running `scripts/search.py --watch …` locally and pasting the output — APIs avoid bot blocks.

## Active watches

**Rome / Paris** — open-jaw shape, one-ticket ticketing; YVR → FCO (12–14 Sep 2026 flex); CDG → YVR (30 Sep). Max 1 stop. Benchmark ~CA$1,173.

**Hawaii** — round-trip shape, separate ticket per traveler; YVR ⇄ HNL/OGG/KOA (nonstop preferred) + CPR ⇄ same (max 1 stop); 8–12 Oct 2026. Benchmark ~USD $1,758 combined to HNL.

**Montreal** — round-trip shape, separate ticket per traveler; YVR ⇄ YUL (red-eye out after 18:00, latest evening return) + CPR ⇄ YUL (late return); 8–12 Oct 2026. Benchmark ~USD $1,770 / CA$2,460 combined. See `montreal-chat-persona.md`.

```bash
python scripts/search.py --watch montreal
python scripts/search.py --watch montreal --save
```

### Time-window configuration

Use local airport time for each leg. “Between” is strict; nearby flights remain visible as near matches:

```yaml
time_windows:
  outbound:
    field: departure
    earliest: "18:00"
    latest: "24:00"
  return:
    field: departure
    earliest: "07:00"
    latest: "09:00"
near_match_minutes: 30
near_match_savings_threshold: 25
manual_omissions: ["Flair Airlines"]  # presentation-time omission only
```

Carrier omissions are deliberately manual. They are not sent as hard provider filters, so the output can show what was omitted and the fare that was sacrificed.

## Trip shape vs ticketing

| Watch | Trip shape | Ticketing |
|---|---|---|
| Rome/Paris | Open-jaw | One multi-city ticket |
| Hawaii | Round-trip (each person) | Separate ticket per traveler |
| Montreal | Round-trip (each person) | Separate ticket per traveler |

Cheapest usable fare **wins**. Same-alliance / one-carrier options appear as a **labeled alternative**, not a silent override.
