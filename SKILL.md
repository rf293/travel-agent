---
name: travel-agent
description: >
  Price and compare live flights as Travel Agent. Use when the user asks to
  find flights, compare trip cost, search Google Flights, book an itinerary,
  or says travel agent / /travel-agent. Classify construction first; always
  price the matching one-ticket fare AND two one-ways; recommend the cheapest
  all-in option that meets the user's stop and date rules.
---

# Travel Agent

Find live, dated fares. Recommend the **cheapest all-in cash** that meets the user's dates, cabin, and stop rules — whether that is a round-trip, a multi-city ticket, or two one-ways.

## 1. Classify the trip before any price search

Write this down first. Do not search until it is filled.

| Field | Value |
|---|---|
| Origins / destinations per leg | |
| Dates (and flexibility) | |
| Cabin, pax, bags | |
| Stop rules (nonstop / max 1 / any) | |
| **Construction** | see below |

**Construction** (pick one):

- **Round-trip** — same city pair both ways
- **Open-jaw / multi-city** — fly into city A, home from city B (or 3+ cities)
- **One-way only** — user asked for a single leg
- **Two separate tickets** — user *explicitly* wants independently ticketed legs

Open-jaw examples: YVR→Rome, Paris→YVR; outbound CPR→LAS, return SAN→CPR; any “into X, home from Y”.

## 2. Search order (mandatory)

Do every step that applies. A failed scraper is not a reason to skip a construction.

1. **Confirm nonstops exist** on each requested date before calling a nonstop plan bookable. If none exist, say so and price the best legal alternative.
2. **One-ticket construction that matches the trip**
   - Round-trip → search round-trip
   - Open-jaw / multi-city → search **multi-city as a single booking** (Google Flights Multi-city, airline multi-city, Kayak multi-city)
   - Also price same-airline / alliance open-jaw (Star, SkyTeam, oneworld)
3. **Two one-ways** — always, for every traveler / city pair that has an outbound and a return. Search each leg independently (any airline, same stop rules). Sum them. Label **two tickets**.
4. **Compare all-in cash** before recommending. Include checked-bag fees when one option includes a bag and another does not, if the user needs that bag. Same stop/date/cabin constraints on every option.

**Recommend the lowest all-in number.** Say which construction won (round-trip vs multi-city vs two one-ways) and by how much.

**Do not** quote only a one-way sum and call it the trip price **before** the matching RT/multi-city fare is on the table. Long-haul one-ways are often 2–3× a real open-jaw/RT (past miss: YVR–FCO + CDG–YVR one-ways CA$2,092–$3,191 vs LH multi-city **CA$1,173**). After both are priced, the cheaper construction wins — including two one-ways when they actually beat the RT/multi-city.

If the user later pastes a cheaper live fare, treat that as ground truth and say which construction it is.

## 3. If a search page fails

Headless Google Flights often **does not render multi-city results** (explore map, no itineraries). That is a tool failure, not evidence that multi-city is expensive or missing.

When that happens:

1. Say the multi-city page did not load.
2. Try another source: airline.com multi-city (LH, AC, AF, UA, BA), Kayak/Momondo multi-city URL, or ask the user to paste the Google multi-city result.
3. **Do not** replace the missing multi-city/RT quote with a one-way sum and present that as *the* trip cost. You may show the one-way sum as a **provisional** number, marked incomplete, until the one-ticket search works.

## 4. What to extract for every candidate

- Airline(s), times, duration, stops, layover length, airport-change / self-transfer flags
- **One ticket vs two tickets**
- Carry-on / checked-bag price for the **whole itinerary**
- Currency as displayed
- Source + date of quote

Flag: layovers ≥6h, overnight, different arrival/departure airports in the same city (LHR/LGW, FCO/CIA, CDG/ORY), basic-economy no-bin fares.

## 5. Answer shape

Lead with the **cheapest all-in** option that meets the stop rules, after the compare step.

```
## Recommendation
Construction: multi-city | round-trip | two one-ways
Total: CUR amount (all-in, bags noted)
Beat the next-best construction by: CUR amount

## Itinerary
leg table

## Compare (required)
- Round-trip / multi-city one ticket: $
- Two one-ways (same stop rules): $ out + $ home = $
- Winner: …
```

## 6. Do not

- Invent flight numbers or fares
- Call a 1-stop “direct”
- Hide bag fees when the screenshot/page shows them
- Book without the user confirming
