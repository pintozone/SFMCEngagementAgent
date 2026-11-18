# SFMC Campaign Kit

From a short YAML **campaign brief**, this kit builds every Salesforce Marketing Cloud component for a campaign
and reads the results back:

| Brief section | What gets built in SFMC |
|---|---|
| `audience.de` / `data_extensions` | Data Extensions (SOAP), placed in `CampaignKit/<CODE>` folders |
| `audience.sql` | SQL Query Activity, plus an Automation that runs it |
| `emails` | Content Builder HTML emails (AMPscript-personalized templates) |
| `journey` | Entry Event Definition and a Journey (EMAILV2 / WAIT / engagement splits), with the emails attached to its activities |
| (automatic) | Analytics report DEs, queries and an automation that read **Data Views** (`_Sent`, `_Open`, `_Click`, `_Bounce`, `_Unsubscribe`, `_Job`, `_Journey`, `_JourneyActivity`) |

It has two modes, and both use the same CLI and code path:

* **`mock`** (the default) runs an offline SFMC tenant on SQLite. DEs are real tables, query activities run
  (SFMC SQL is translated to SQLite), and *publishing* a journey simulates sends, opens, clicks, bounces and
  unsubscribes into Data View tables, so analytics work end to end.
* **`live`** works against your SFMC tenant through an Installed Package (server-to-server).

Sample client: **Aurora Motors**, a fictional automaker. All generated email addresses use the reserved
`example.com/.net/.org` domains, so no generated address can reach a real inbox.

## Quick start (mock)

```bash
python -m sfmc_kit seed --scale full                    # 6 master DEs, ~37k rows
python -m sfmc_kit build briefs/ev_launch.yaml --publish  # all components + simulated sends
python -m sfmc_kit analytics EVLAUNCH                     # KPIs from Data Views
python -m sfmc_kit preview briefs/ev_launch.yaml          # personalized HTML previews in data/previews/
python -m sfmc_kit status EVLAUNCH                        # objects + lineage
python -m sfmc_kit teardown EVLAUNCH
```

## Connecting to your SFMC tenant (live)

1. **Installed Package.** In SFMC, open Setup → Apps → Installed Packages → *New*. Add a **Server-to-Server** API Integration component with these scopes:
   * Channels → **Email**: Read, Write, Send
   * Assets → **Documents and Images**, **Saved Content**: Read, Write
   * Automation → **Automations**: Read, Write, Execute · **Journeys**: Read, Write, Execute, Activate/Stop/Pause/Send/Schedule, Delete
   * Contacts → **List and Subscribers**: Read, Write
   * Data → **Data Extensions**: Read, Write · **Tracking Events**: Read
   * Event Notifications → **Event Definitions**: Read, Write (in some tenants this sits under *Journeys*)
   * Setup → **Sender Profiles**, **Send Classifications**, **Delivery Profiles**: Read
2. **Credentials.** Set these as environment variables, or put them in a git-ignored `.env` file (see `.env.example`):
   `SFMC_SUBDOMAIN`, `SFMC_CLIENT_ID`, `SFMC_CLIENT_SECRET`, `SFMC_ACCOUNT_ID` (MID of the target BU).
   The token is held only in memory, and the secret is never logged.
3. Run the steps in order, checking each one:

```bash
python -m sfmc_kit --mode live auth-test
python -m sfmc_kit --mode live de create examples/de_spec.yaml
python -m sfmc_kit --mode live de load CK_Smoke_Test examples/smoke_rows.csv && python -m sfmc_kit --mode live de rows CK_Smoke_Test
python -m sfmc_kit --mode live seed --scale small --push
python -m sfmc_kit --mode live build briefs/ev_launch.yaml --dry-run
python -m sfmc_kit --mode live build briefs/ev_launch.yaml
python -m sfmc_kit --mode live analytics EVLAUNCH
```

Live-mode safety: every live write asks you to type a confirmation (`BUILD`, `PUSH` or the campaign code).
Journeys are created as **drafts**. `--publish` activates a journey and asks for a separate confirmation.
`teardown` deletes only the objects recorded for that campaign.

### Verified against a live tenant (stack S7, 2026-11-18)
DE create/describe/delete (SOAP) · rowset upsert/read (REST) · DE, query and asset folders · audience Query Activity
and Automation run · 5 Content Builder HTML emails · DE entry Event Definition · 12-activity draft journey with
emails attached to its EMAILV2 steps and open/click splits (`statsTypeId` 2/3 accepted) · analytics automation over
Data Views. API quirks the kit already handles: automations need `startSource: {typeId: 1}` for *run once*;
Journey Builder returns **400** (not 404) for an unknown key; journey updates must send back `modifiedDate`;
YAML `on:`/`yes:`/`no:` keys are read as strings (not YAML 1.1 booleans).

### Still to do by hand before publishing
These are the parts of the SFMC API that are least documented, or that differ between tenants. The kit sets them in one place each:
* **Data Extension entry schedule.** The journey is created with a DE entry source. Set the entry schedule
  (or a "Journey Audience" automation step) in Journey Builder before publishing, or use `entry.type: api_event`.
* **Send classification.** `send.send_classification` is the *external key* (default `Default Commercial`).
  The kit looks up its sender and delivery profiles over SOAP.
* **Data Views** keep about 6 months of history, and engagement only shows up after real sends.

## Describe a campaign in plain English
In a Claude Code session on this repo, just describe the campaign. For example:

> Win back South-region Ridge pickup owners whose last service was over 9 months ago: 2 emails 5 days apart,
> 20% off service, resend to non-openers.

The repo skill `.claude/skills/campaign-brief` drives the workflow:
1. **Look up**: `schema` (master DE fields with example values) and `templates` (layouts, required blocks, `@variables`).
2. **Write** `briefs/<code>.yaml`.
3. **Check**: `brief check` validates the schema, template blocks, subject/preheader length, personalization fields and link aliases, then runs the audience SQL read-only in the mock tenant (count, duplicate keys, missing emails, unknown columns). Claude loops until it prints `OK`.
4. **Rehearse** in the mock tenant (`build --publish`, `analytics`, `preview`) and show you the audience, journey outline, subjects, simulated KPIs and previews.
5. **Build live as a draft** only after you say so, then read the journey back to verify it. Publishing, live teardown and master-data changes always need your explicit request.

`briefs/ridgewinback.yaml` is the brief generated from the example above.

Templates: `hero_cta.html` (launch/invite), `offer_card.html` (offer value, code, expiry, terms),
`newsletter.html` (intro + 3 cards), `service_reminder.html` (vehicle/last-service box).
Run `python -m sfmc_kit templates` for the blocks each one needs.

## Tenant audit and dependencies
Inventory the whole business unit (read-only, seconds) and ask questions offline:
```bash
python -m sfmc_kit --mode live audit sync                         # DEs, fields, queries, automations, journeys, entry events, emails/blocks/pages
python -m sfmc_kit --mode live audit impact CK_Master_Customers   # what depends on it (follows data flow into journeys)
python -m sfmc_kit --mode live audit impact CK_Master_Customers.LoyaltyTier   # where a field is used
python -m sfmc_kit --mode live audit search "UpsertData"          # find code references
python -m sfmc_kit --mode live audit health                       # failing automations, missing DEs/blocks, SELECT *, retention, stale objects
python -m sfmc_kit --mode live audit report                       # self-contained HTML report in data/reports/
```
Dependencies come from:
- the SQL `FROM`/`JOIN` sources and targets of queries
- automation steps
- journey entry events and email activities
- AMPscript/SSJS in emails, blocks and CloudPages (`Lookup*`, `Insert/Upsert/Update/DeleteData`, `DataExtension.Init`, `ContentBlockByKey/ById`, `CloudPagesURL`)

They're stored in SQLite (`inv_*` tables) and walked with recursive queries. The skill `.claude/skills/sfmc-developer` guides these investigations.

## SQL, AMPscript and SSJS development
```bash
python -m sfmc_kit --mode live sql lint query.sql --target CK_EVLAUNCH_Audience   # SFMC rules + SFMC's validator
python -m sfmc_kit sql explain query.sql                                         # sources, joins, filters, outputs
python -m sfmc_kit --mode live sql run query.sql --top 50                        # temporary DE/query/automation, rows, cleanup
python -m sfmc_kit --mode live amp lint Survey_Cloud_Page                        # file, or email/block/page by name
python -m sfmc_kit --mode live amp render CK_EVLAUNCH_EM_teaser --de CK_EVLAUNCH_Audience --key AUR-100007
```
**SQL lint** catches:
- non-SELECT statements, variables, temp tables and `SELECT INTO`
- `ORDER BY` without `TOP`, and `SELECT *` (an error when there's a JOIN)
- unnamed or duplicate columns
- event data views without a date filter, or looking back more than 6 months
- unknown DEs and fields, from the inventory
- target mismatches: unknown columns, missing required or primary-key fields, possible duplicate keys, and values that may be truncated

In live mode it also runs SFMC's own query validator.

**AMPscript and SSJS lint** catches:
- unbalanced blocks, IF/ENDIF and FOR/NEXT
- `=` used for comparison, unknown functions, smart quotes and unclosed strings
- `Lookup`/`Upsert…` calls naming DEs or fields that don't exist
- SSJS without `Platform.Load`, writes without try/catch, and ES6 syntax

**`amp render`** uses SFMC's server-side preview, so all AMPscript runs for real against the chosen DE row.

## Proofing DEs for emails with visibility conditions
Emails can have `sections:` that show or hide by subscriber data (see `briefs/ownernews.yaml`):
```yaml
sections:
  - {id: ev_tips, when: "Model in [Volt X, Pulse]", title: "…", body: "…"}
  - {id: platinum_concierge, group: offer, when: "LoyaltyTier == Platinum", title: "…", body: "…"}
  - {id: standard_offer,     group: offer, when: otherwise,                title: "…", body: "…"}
  - {id: ridge_accessories, when: "Model == Ridge and Region in [South, West]", title: "…", body: "…"}
```
The proofing workflow then covers the combinations:
```bash
python -m sfmc_kit proof analyze briefs/ownernews.yaml                 # conditions + candidate values
python -m sfmc_kit --mode live proof setup briefs/ownernews.yaml       # config DEs + form page (CloudPage)
#   → open the form CloudPage ?email=CK_OWNERNEWS_EM_update, pick values + proof inboxes, Save
python -m sfmc_kit --mode live proof generate briefs/ownernews.yaml    # sendable CK_OWNERNEWS_Proofing
python -m sfmc_kit --mode live proof verify briefs/ownernews.yaml      # SFMC renders each row; blocks checked
```
**Coverage.** Smart coverage covers every pair of values and shows and hides every block. The example needs 12 rows instead of 72 permutations. Full permutations and per-block coverage are also available.

**Rows.** Each row records its `ProofCase` and `ExpectedBlocks`, and rows go round-robin to the inboxes entered in the form.

**Verify.** `verify` renders every row on SFMC and compares the blocks that showed against what was expected. It writes an HTML report.

**One-time setup.** The form is one reusable CloudPage, published once with `%%=ContentBlockByKey("CK_PROOF_FormBlock")=%%`. Its selections are stored in `CK_Proof_Values` and `CK_Proof_Settings`.

**Other emails.** Emails not built by the kit work too: pass the email's customer key and `--de`, and the kit parses its IF conditions.

## Test-drive booking CloudPage
Add a `cloudpage:` section to a brief (see `briefs/ev_launch.yaml`) and set an email's `cta_url: "{{cloudpage_url}}"`.
The build then creates:
* **`CK_<CODE>_TestDriveBookings`** (sendable, PK SubscriberKey + Model): one row per booking, with name, email,
  phone, dealer, preferred date and time, comments, consent, status, source, campaign and created date.
* **`CK_<CODE>_CP_TestDriveBooking`**, a Content Builder HTML block holding the styled, mobile-friendly page.
  Its AMPscript pre-fills the form from the email link and checks every field on the server (model and
  time-slot whitelists, a real dealer from `CK_Master_Dealers`, valid email, a date from tomorrow up to
  `max_days_ahead`, consent). It HTML-escapes anything echoed back, then `UpsertData`s the booking and shows
  a confirmation.
* **Email buttons** that link with `CloudPagesURL(page_id, …)`: encrypted parameters (SubscriberKey, name, email,
  dealer, model, campaign) pre-fill the form.

**One-time step in the UI** (publishing CloudPages isn't available on the public API):
1. Run `python -m sfmc_kit cloudpage stub briefs/ev_launch.yaml` to print one line:
   `%%=ContentBlockByKey("CK_EVLAUNCH_CP_TestDriveBooking")=%%`
2. In **Web Studio → CloudPages**, open or create a collection, add a **Landing Page**, and choose a blank
   layout. Add an **HTML** content block, paste the line, then **Publish**.
3. Copy the page's **ID** (CloudPages → page → *Page ID*, or from the page URL details), set `cloudpage.page_id`
   in the brief, and rebuild. The emails now link to the page.

After that, design and logic changes only need a rebuild: the kit updates the block and the published page
picks it up without re-publishing.

```bash
python -m sfmc_kit cloudpage preview briefs/ev_launch.yaml     # static preview + exact AMPscript body
python -m sfmc_kit --mode live cloudpage bookings EVLAUNCH     # submitted bookings
python -m sfmc_kit --mode live analytics EVLAUNCH              # KPIs incl. Bookings and click-to-booking rate
```
In mock mode, publishing the journey also simulates bookings from a share of email clickers
(`cloudpage.booking_rate`), so analytics show the full send → open → click → booking funnel.

## Writing a brief
See `briefs/ev_launch.yaml` (a 12-activity journey with nested open and click splits) and `briefs/service_reminder.yaml`.
Journey steps:

```yaml
steps:
  - email: teaser
  - wait: {days: 3}                     # minutes | hours | days | weeks
  - decision:                           # must be the last step in its path
      on: opened                        # or clicked
      email: teaser
      yes: [ {email: invite} ]
      no:  [ {email: resend}, {wait: {days: 3}}, {email: invite} ]
```

Keep audience SQL inside the subset the mock translator supports: `[brackets]`, `GETDATE()`, `DATEADD`, `DATEDIFF`,
`CONVERT(DATE, x)`, `ISNULL`, `TOP n`, and `ROW_NUMBER() OVER (...)`. Don't use `+` for string concatenation.

Every object is named `CK_<CODE>_*`. Rebuilding is idempotent: existing objects are found by key and updated.
DE schemas are **not** altered if the DE already exists. Tear down the campaign (or delete the DE) to change its fields.

## Sandbox database: why SQLite (and not a graph DB)
SFMC's own model is relational. DEs and Data Views are tables, and campaign logic is SQL. A relational sandbox
lets the *same* query text run in mock mode as in Automation Studio. The relationships between campaign assets
(brief → DEs → queries → automations → event → journey → emails) form a small tree. A `lineage` table covers it
(`status <CODE>`), so a graph DB would add infrastructure without adding capability. Files:

* `data/sandbox.db`: build runs, object registry, lineage, and a mirror of the sample data (`sample_*` tables)
* `data/mock_sfmc.db`: the simulated tenant (DE tables, Data Views, object store)
* `data/manifests/<CODE>.<mode>.json`: every object ID created, plus the exact journey JSON that was sent

If several people or a BI tool need this data, Postgres is the upgrade path. The schema is plain SQL.

## Layout
```
sfmc_kit/
  auth.py            OAuth2 client_credentials (in-memory token, auto refresh)
  client/rest.py     REST with retry/backoff     client/soap.py   SOAP partner API (Retrieve/Create/Delete)
  client/live.py     LiveBackend                 client/mock.py   MockBackend + send simulator
  client/sqlxlate.py SFMC SQL -> SQLite
  modules/           data_extensions, assets, automation, journey, analytics (spec + payload builders)
  brief.py           brief schema/validation     builder.py       orchestrator, analytics, teardown
  sampledata/        Aurora Motors generator (deterministic)
templates/emails/    hero_cta.html, service_reminder.html
briefs/              ev_launch.yaml, service_reminder.yaml
tests/               pytest: units, mock end-to-end, LiveBackend request shapes (fake HTTP)
```

`python -m pytest -q` runs the full suite offline.
