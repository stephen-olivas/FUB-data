# FUB-data

Pulls leads from Follow Up Boss and reports the RES early funnel for a 7‑day window:

**Leads created → Qualified → Opportunity**, with the unqualified leads broken out by reason.

FUB has no Qualified or Opportunity stage, so both are derived from data FUB does carry (tags, stages, custom fields, the property zip, notes, the lead's own texts, appointments). Because we don't yet know which of those are reliably populated, there are two commands:

| Command | What it answers |
|---|---|
| `discover` | Which fields, tags, stages, custom fields and activity types actually have data on recent leads, and how often each rule fires on a sample |
| `report` | The weekly funnel: one row per lead with its status and the evidence behind it, plus a summary |

## Definitions (from *RES Early Qualified Definition, WIP 10/07/26*)

A lead is **Unqualified** (hard stop) if any of these match:

| Rule id | Meaning | How it's detected today |
|---|---|---|
| `zillow_active` | Already listed / has an agent (active on Zillow) | tags `active listing`, `has an agent` + keywords (incl. `customForSaleOnZillow`, `customNotesRedFlags`) |
| `short_sale_or_foreclosure` | Home is in short sale or foreclosure | keywords in red-flag notes, notes, texts, calls (no tag exists yet) |
| `licensed_agent` | Lead is a licensed agent | keywords in red-flag notes, notes, texts, calls |
| `rural_geo` | Rural area we can't service | tag `out of service area`, or property zip not on the RES sheet or the Preferred Zip Codes sheet |
| `trash` | Moved to Trash in FUB | stage `Trash` (only the primary reason when nothing more specific matches) |
| `specialist_property` | Highly unique property needing a specialist | keywords |

A non‑unqualified lead becomes an **Opportunity** when it agrees to speak with someone about the property ("I would like to intro with an agent"). Detected by stage (`Referred Out/Appointment Set` or anything later), a `Referral Agent` being set, or keywords in notes, calls, or the lead's **inbound** texts (a rep's outbound "want an intro?" doesn't count).

Everything else is **Qualified**. HAP tags (`HAP Soft Qualified` / `HAP Soft Unqualified`) belong to a separate program's screening and are ignored. Qualified in the summary includes Opportunities.

All matching lives in [`config/criteria.yaml`](config/criteria.yaml) — edit that, not the code.

## Setup

1. In FUB: **Admin → API** → create an API key.
2. In this repo: **Settings → Secrets and variables → Actions**
   - Secret `FUB_API_KEY` (required)
   - Secrets `FUB_SYSTEM` / `FUB_SYSTEM_KEY` (optional; a registered system key doubles FUB's rate limit)
   - Variable `FUB_APP_URL`, e.g. `https://yourteam.followupboss.com`, to get clickable lead links in the CSV

## Running it

**In GitHub:** Actions → *FUB funnel report* → *Run workflow*. Pick `discover` or `report`, optionally a start/end date. Results are attached to the run as an artifact (`output/` folder); the report summary also shows on the run page. It runs automatically every Monday for the previous Mon–Sun week.

**Locally:**

```bash
pip install -r requirements.txt
export FUB_API_KEY=...
python fub.py discover --days 30            # start here
python fub.py report                        # last complete Mon–Sun week (Pacific)
python fub.py report --start 2026-09-28     # 7 days from that date
python fub.py report --start 2026-09-28 --end 2026-10-04
python fub.py report --no-activity          # person fields only, much faster
```

Outputs land in `output/` (git‑ignored, because they contain lead names and note snippets):

- `funnel_<window>.csv` — per lead: status, primary + all unqualified reasons, the evidence text that triggered each, opportunity evidence, zip/market, review flags
- `funnel_<window>_summary.md|json` — counts, rates, breakdown by reason, market and source
- `discovery_<window>.md|json` — field coverage, tags, stages, custom fields, activity schemas, config check, rule hit rates

## Recommended first pass

1. Run `discover` for the last 30 days.
2. In the report, look at **Tags used**, **Stages in account**, **Custom fields** and the **Config check**. Put the real names into `config/criteria.yaml` (e.g. the actual short‑sale tag, the actual appointment stages, any custom field that records property type or agent status).
3. Check **Rule preview on sample** for keywords that fire too often or never.
4. Run `report` for last week and spot‑check the `unqualified_evidence` and `opportunity_evidence` columns.

## How it works

- `/people` has no created‑date filter, so the script walks people newest‑first (`sort=-created`, `fields=allFields`, following FUB's `next` token) and keeps the ones created in the window, stopping after 200 consecutive older records.
- Per lead it only calls the activity endpoints the enabled rules need (events, notes, textMessages, calls, appointments, deals). It honours FUB's `Retry-After` on 429s. Notes are limited to 10 requests / 10 s, so a few hundred leads take a few minutes.
- Window boundaries use Pacific time (`timezone` in the config).
- Every lead's property zip gets a **geography tier** (CSV column `geo_tier`; first match wins):

  | Tier | Zips |
  |---|---|
  | `res_core` | *ReSvcs Zips All Core Markets.xlsx* — 286 RES zips in Phoenix, Atlanta, Las Vegas, Denver, Colorado Springs (`data/core_market_zips.csv`) |
  | `core_metro` | the rest of those 5 metros plus Nashville, from *Preferred Zip codes - Bonus* (`data/preferred_zips.csv`) |
  | `other_metro` | every other metro on the preferred sheet |
  | *(none)* | rural / out of area → unqualified |

  The summary's **Funnel by geography** table shows each tier, **Core markets overall** (`res_core` + `core_metro`), and all leads. Tiers are set under `geo_tiers` in the config.

## Open questions

- **"Active on Zillow before they hit FUB"** — `customForSaleOnZillow` is filled on every lead; the next discovery run shows its values so it can become a direct check.
- **Licensed agent / unique property** — is there a tag or custom field reps set? Keywords catch notes like "lead is an agent" but will also hit "not in foreclosure"‑style negations; the evidence column makes these easy to spot.
- **Missing zip** — leads with no property or address zip are not counted as rural; they're flagged `no_zip_found` for review.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

Tests run against an in‑memory fake of the FUB API (`tests/fake_fub.py`).
