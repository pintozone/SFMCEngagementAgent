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

### Things to verify on the first live build
These are the parts of the SFMC API that are least documented, or that differ between tenants. The kit sets them in one place each:
* **Engagement split `statsTypeId`** (`sfmc_kit/modules/journey.py`, `STATS_TYPE`): opened = 2, clicked = 3.
  If Journey Builder rejects the split, export a split from a journey you built in the UI and copy its values.
* **Data Extension entry schedule.** The journey is created with a DE entry source. Set the entry schedule
  (or a "Journey Audience" automation step) in Journey Builder before publishing, or use `entry.type: api_event`.
* **Send classification.** `send.send_classification` is the *external key* (default `Default Commercial`).
  The kit looks up its sender and delivery profiles over SOAP.
* **Data Views** keep about 6 months of history, and engagement only shows up after real sends.

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
