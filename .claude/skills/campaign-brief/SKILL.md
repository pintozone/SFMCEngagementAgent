---
name: campaign-brief
description: Turn a plain-English campaign request for Aurora Motors (the sample automotive client) into a validated SFMC campaign brief, rehearse it in the mock tenant, and build it in live SFMC only after the user approves. Use whenever the user describes a campaign, journey, email series, audience or offer they want in Marketing Cloud ("win back lapsed owners…", "launch email for…", "service reminder for…"), or asks to change an existing brief in briefs/.
---

# Campaign brief from plain English

You turn the user's description into `briefs/<code>.yaml`, check it, rehearse it offline, and build it in SFMC only when they say so. The toolkit is `python -m sfmc_kit` (see README.md). Mock mode is the default; live mode needs `--mode live`.

## 1. Understand the request
Pull these from the user's words. Ask **one** short question only when something essential is missing or ambiguous:
- **Objective**: what the campaign should drive (test drives, service bookings, purchases, engagement).
- **Audience**: who gets it (models, regions, loyalty tiers, last service, lead status, vehicle age…).
- **Emails and cadence**: how many, the gaps between them, and any follow-up rules (resend to non-openers, offer to clickers).
- **Offer / CTA**: what the reader should do, and any offer value, code or expiry.

Default everything else and state your defaults when you show the brief. For example: entry mode `SingleEntryAcrossAllVersions`, send classification `Default Commercial`, Aurora brand colors, and the URLs on `https://www.example.com/aurora/...`.

## 2. Look up what exists
- `python -m sfmc_kit schema` lists the master DE fields with example values. **The audience SQL may only use these fields.** If the mock tenant is empty, run `python -m sfmc_kit seed --scale full` first.
- `python -m sfmc_kit templates` lists each template, the blocks it needs, and the `@variables` usable in subjects and blocks.
- Use `briefs/ev_launch.yaml` (nested open and click splits) and `briefs/service_reminder.yaml` as models.

## 3. Write the brief: `briefs/<code_lowercase>.yaml`
- `campaign.code`: 3–20 characters, A–Z and 0–9 (e.g. `RIDGEWINBACK`). Every SFMC object is named `CK_<CODE>_*`.
- **Audience DE**: list exactly the columns the SQL returns. Include `SubscriberKey` (pk, Text 50) and `EmailAddress` (EmailAddress, required), plus every field a chosen template reads (`templates` prints "reads DE fields"). Name SQL columns to match those fields, e.g. `v.Model AS CurrentModel` for `hero_cta.html`.
- **Audience SQL** rules:
  - Always filter `c.EmailOptIn = 1`.
  - Use one row per SubscriberKey: dedupe with `ROW_NUMBER() OVER (PARTITION BY c.SubscriberKey ORDER BY …)` in a subquery and keep `rn = 1`.
  - Reference DEs as `[CK_Master_…] alias`.
  - Stay inside the supported subset: `GETDATE()`, `DATEADD(unit, n, x)`, `DATEDIFF`, `CONVERT(DATE, x)`, `ISNULL`, `TOP n`, `NOT EXISTS`, `ROW_NUMBER()`.
  - Never use `+` for string concatenation.
- **Journey steps**: `email`, `wait: {days: N}`, and `decision: {on: opened|clicked, email: <ref>, yes: [...], no: [...]}`. A decision must be the **last** step of its path. It can only reference an email already sent earlier on that path.
- **Copy** (Aurora Motors voice: confident, warm, plain-spoken; no hype, no ALL CAPS, no exclamation stacks):
  - Subject ≤ 60 characters and preheader ≤ 100 (personalization counts as about 10).
  - One primary CTA per email, and give `cta_alias` a readable name.
  - Personalize only through the template's `@variables`, as `%%=v(@FirstName)=%%`.
  - Offers need `terms`. Mark them as sample/demo content, because this is a sample brand.

### Booking page (when the user wants customers to book test drives)
- Add a `cloudpage:` section (`type: test_drive_booking`, `models`, `default_model`, `headline`, `intro`, `fallback_url`; optional `time_slots`, `expect`, `button_text`, `max_days_ahead`). Then set `cta_url: "{{cloudpage_url}}"` on the emails that should open it. `briefs/ev_launch.yaml` is the model.
- Include `DealerID`, `FirstName`, `LastName` and `EmailAddress` in the audience DE where possible, so the form pre-fills.
- Bookings land in `CK_<CODE>_TestDriveBookings`. The page body is the HTML block `CK_<CODE>_CP_TestDriveBooking`.
- Publishing a CloudPage is a one-time UI step that only the user can do. Give them the output of `python -m sfmc_kit cloudpage stub <brief>` and the README steps. When they send the page ID, set `cloudpage.page_id` and rebuild. Until then the emails use `fallback_url`.
- Rehearse with `cloudpage preview <brief>`, and show the user the preview file. In mock mode, `analytics` adds Bookings and the click-to-booking rate.

## 4. Check until clean
`python -m sfmc_kit brief check briefs/<file>.yaml`. It validates the schema, template blocks, copy lengths, personalization and links, and runs the audience SQL against the mock tenant (count, PK duplicates, missing emails, unknown columns). Fix every `ERROR`. Fix `WARN` lines unless there's a reason not to, and if you leave one, say why. Repeat until it prints `OK`.

## 5. Rehearse offline, then show the user
```bash
python -m sfmc_kit build briefs/<file>.yaml --publish   # mock tenant: simulated sends
python -m sfmc_kit analytics <CODE>
python -m sfmc_kit preview briefs/<file>.yaml           # personalized HTML in data/previews/
```
Show the user, briefly:
- the campaign name and code
- the audience rule in words, with the mock count (and a note that the live count will differ)
- the journey as an indented outline (email → wait → split → …)
- each email's subject and CTA
- the simulated KPIs, labelled **simulated**
- the preview file paths
- your defaults

Then ask whether to build it in SFMC.

## 6. Live build only on explicit approval
Only when the user clearly says to build in SFMC:
```bash
python -m sfmc_kit --mode live -y build briefs/<file>.yaml   # creates a DRAFT journey; never add --publish
```
The master DEs must already exist live (`--mode live de list --like CK_Master_`). If they don't, ask before running `--mode live seed --push`.

After the build, read the journey back to verify it:
```python
from sfmc_kit.config import load_live_config
from sfmc_kit.client.live import LiveBackend
LiveBackend(load_live_config()).rest.get("/interaction/v1/interactions/key:CK_<CODE>_JRN")
```
Check that its status is Draft, the activity count matches, and each EMAILV2 has an `emailId`. Report what was created, and where to find it in Email Studio, Automation Studio, Content Builder and Journey Builder.

## Never, without an explicit request in the user's own words
- publish or activate a journey (`--publish` in live mode)
- run `teardown` or `de delete` in live mode
- change the master DEs or push seed data live
- put real customer email addresses in the briefs or the sample data

After changing code (not just briefs), run `python -m pytest -q` before committing. Briefs may be committed and pushed when the user wants them kept.
