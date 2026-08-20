# Travel Agent

Grok skill and Chat personas for live flight pricing.

The agent quotes the cheapest **all-in** fare that matches the dates, cabin, and stop rules. It always prices the matching **round-trip or multi-city ticket** and **two one-ways**, then recommends whichever is cheaper. It does not treat summed long-haul one-ways as the trip price until the one-ticket fare is also on the table.

## Files

| File | Use |
|---|---|
| `SKILL.md` | Grok Build skill (`/travel-agent`). Copy or symlink to `~/.grok/skills/travel-agent/SKILL.md`. |
| `chat-persona.md` | Grok Chat persona for the **Rome/Paris** open-jaw watch. |
| `hawaii-chat-persona.md` | Grok Chat persona for the **YVR + Casper → Hawaii** watch. |

## Grok Build

```bash
mkdir -p ~/.grok/skills/travel-agent
cp SKILL.md ~/.grok/skills/travel-agent/
# optional: copy personas too
cp chat-persona.md hawaii-chat-persona.md ~/.grok/skills/travel-agent/
```

Then say `travel agent` or `/travel-agent`.

## Grok Chat

Paste the contents of `chat-persona.md` or `hawaii-chat-persona.md` into Custom Instructions or a Skill. Daily prompts:

- `Run the daily Rome/Paris check`
- `Run the daily Hawaii check`

## Active watches

**Rome / Paris (open-jaw, 1 adult, economy)**  
YVR → FCO on 12–14 Sep 2026; CDG → YVR on 30 Sep 2026. Max 1 stop. Benchmark: Lufthansa multi-city about CA$1,173.

**Hawaii (two travelers, separate tickets)**  
YVR ⇄ HNL (nonstop preferred) and CPR ⇄ HNL (1 stop preferred), 8–12 Oct 2026. Benchmark combined: about USD $1,758.
