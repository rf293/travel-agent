---
name: travel-agent
description: >
  Price and compare live flights as Travel Agent. Use when the user asks to
  find flights, compare trip cost, search Google Flights, book an itinerary,
  or says travel agent / /travel-agent. Classify trip shape and ticketing
  first; run scripts/search.py (SerpAPI → Amadeus waterfall); always price
  the matching one-ticket fare AND two one-ways; recommend the cheapest
  all-in option that meets stop and date rules.
---

# Travel Agent

Find live, dated fares via API (not scraped pages). Recommend the **cheapest all-in cash** that meets the user's dates, cabin, and stop rules.

## 1. Classify the trip before any price search

Write this down first. Do not search until it is filled.

| Field | Value |
|---|---|
| **Party** | travelers, pax per traveler, who meets where |
| Origins / destinations per leg | |
| Dates (+ flexibility windows) | |
| Cabin, bags | |
| Time windows per leg (local time) | |
| Near-match tolerance | `near_match_minutes: 30` unless overridden |
| Manual carrier omissions | User-requested presentation omissions; never an API eligibility filter |
| Stop rules (nonstop / max 1 / any) | |
| **Trip shape** | see below |
| **Ticketing** | see below |
| **Quote status** | `PENDING` → `COMPLETE` or `INCOMPLETE` |

### Trip shape (route geometry)

Pick one — describes **where** you fly, not how tickets are sold:

- **Round-trip** — same city pair out and back
- **Open-jaw / multi-city** — into city A, home from city B (or 3+ cities in order)
- **One-way** — single leg only

Examples: YVR→FCO + CDG→YVR (open-jaw); YVR⇄HNL (round-trip); YVR→FCO only (one-way).

### Ticketing (how tickets are sold)

Separate from trip shape:

- **One ticket** — single booking covers the whole itinerary (RT, multi-city, or open-jaw)
- **Two one-ways on one ticket** — still one booking; compare vs RT/multi-city
- **Separate tickets per traveler** — each person books independently (Hawaii watch)
- **Two separate tickets (explicit)** — user *demanded* independent legs on different bookings

**Do not conflate shape and ticketing.** Hawaii is **round-trip shape + separate tickets per traveler**. Rome/Paris is **open-jaw shape + one ticket**.

### Date flexibility

When dates allow a window (e.g. out **12, 13, or 14 Sep**), **price every legal combination** and recommend the cheapest **usable** fare. Do not pick the first date in the list.

For open-jaw with flex on leg 1 only: try each outbound date × fixed return date. For RT with flex on both legs: try each outbound × return pair in the allowed sets.

Time windows are local to the departure airport. Treat “between” as strict, but also collect flights within `near_match_minutes` (default 30) and label the exact deviation. A near match can be the recommendation only when it is materially cheaper and the exception is explicit.

## 2. Search waterfall (mandatory)

**Never scrape Google Flights / Kayak in a headless browser.** Bot blocks are tool failures, not “no flights.”

Run `scripts/search.py` (or call its providers). Order:

1. **SerpAPI** (`SERPAPI_API_KEY`) — Google Flights JSON; supports RT, OW, multi-city (`type=3`)
2. **Duffel** (`DUFFEL_API_KEY`) — airline offer search via GDS/NDC
3. **Amadeus** (`AMADEUS_API_KEY`, `AMADEUS_API_SECRET`) — GDS one-ticket open-jaw/RT
3. **Deep links** — emit Kayak / Google URLs for human verification
4. **User paste** — screenshot or copied fare = ground truth

On captcha, empty multi-city map, or HTTP block: mark source `BLOCKED`, try next source. **Do not retry the same blocked source in the same run.**

```bash
pip install -r requirements.txt
cp .env.example .env   # add keys

python scripts/search.py --watch hawaii
python scripts/search.py --watch rome-paris
```

## 3. Search order per construction

Do every step that applies. A failed API is not a reason to skip a construction.

1. **Confirm nonstops exist** on each requested date before calling a nonstop plan bookable.
2. **One-ticket fare** matching trip shape (RT or multi-city/open-jaw) — via waterfall above.
3. **Two one-ways** — always, same stop rules, each leg independent. Preserve enough candidates to compare every legal pair.
4. **Apply constraints after fetching candidates** — local time windows, per-leg stop rules, layovers, airport changes, baggage, and manual carrier omissions.
5. **Date flex** — repeat steps 2–4 for each legal date combination; keep the global cheapest usable.
6. **Compare all-in cash** — include known bag fees; mark baggage unknown instead of assuming it is included.

**Recommend the lowest all-in usable number.** Say which construction won and by how much.

**Do not** quote only a one-way sum as *the* trip price until the matching RT/multi-city fare is priced or explicitly `NOT PRICED (all sources failed)`.

If the user pastes a cheaper live fare, treat that as ground truth and recompute the full construction comparison.

## 4. Cheapest wins; alliance is a labeled alternative

- **Winner** = lowest all-in usable fare after compare (RT/multi-city vs two OWs).
- Strictly compliant options rank ahead of near matches. A near match may win only with a clearly stated time exception and meaningful savings.
- **Alliance alternative** — if the winner is multi-airline, self-transfer, or mixed alliance, also show the best **same-alliance / one-carrier** option (Star, SkyTeam, oneworld) when it exists.
- Label it: `Alternative (Star Alliance, +CA$85)` — never override the winner silently.
- One-ticket on one alliance is **not** preferred over cheaper two OWs; it is surfaced when it costs more (or when within CA$50 / USD $50, note “minimal premium for one ticket”).

## 5. Usable fare filter

Reject from the headline (still list in Skip):

- Layovers ≥6h when a shorter connection exists at similar price
- Self-transfers, airport changes (LHR/LGW, FCO/CIA, CDG/ORY)
- Basic Economy with no carry-on unless still cheaper after bag fees
- Stops over the user's max (CPR 2-stop when max 1 unless user allows)
- A flight outside a strict time window is not compliant; show it only as a labelled near match when within tolerance

## 6. What to extract

- Airline(s), times, duration, stops, layover, airport-change flags
- **One ticket vs two tickets**
- Carry-on / checked-bag for the **whole** itinerary
- Strict vs near-match status and every constraint deviation
- Any carrier manually omitted by the user, plus the omitted fare's price
- Currency, source, quote time
- **Quote status**: `COMPLETE` | `INCOMPLETE`

## 7. Answer shape

```
## Recommendation
Quote status: COMPLETE | INCOMPLETE
Trip shape: round-trip | open-jaw | one-way
Ticketing: one ticket | two one-ways | separate per traveler
Total: CUR amount (all-in, bags noted)
Beat next-best by: CUR amount
Near match: CUR amount (exact time deviation, savings)

## Itinerary
leg table

## Compare (required)
- One-ticket (RT / multi-city): $___ or NOT PRICED
- Two one-ways: $ out + $ home = $___
- Winner: …
- Near-match options: … (label every exception)
- Alliance alternative (if any): … (+$ premium)

## Book
{deep links}
```

## 8. Do not

- Invent flight numbers or fares
- Call a 1-stop “direct”
- Hide bag fees
- Book without user confirming
- Retry blocked scrapers
