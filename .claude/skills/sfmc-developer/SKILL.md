---
name: sfmc-developer
description: Day-to-day Salesforce Marketing Cloud developer work on the connected business unit — tenant inventory and audits, "what uses this DE / field?" impact analysis, finding where code references something, health checks (failing automations, broken references, risky SQL, stale objects). Use when the user asks about existing SFMC objects, dependencies, cleanup, troubleshooting, or anything a SFMC developer would investigate in their tenant (not for building a new campaign from a brief — that is the campaign-brief skill).
---

# SFMC developer workflows

The toolkit is `python -m sfmc_kit`. Live mode (`--mode live`) talks to the business unit in `SFMC_ACCOUNT_ID`; mock mode is the offline simulator.

## Safety (applies to every section)
- Investigations are **read-only**. Never create, change, run, pause, delete, unsubscribe or send anything in the live tenant unless the user asks for that specific action in their own words. Then use the CLI's confirmation (`-y` only after they said so).
- Inventory contents (names, SQL, email HTML) are the client's data. Show what answers the question. Don't paste large bodies unless asked.

## 1. Tenant audit & dependencies
Sync first. It is fast (seconds for hundreds of objects) and read-only:
```bash
python -m sfmc_kit --mode live audit sync
```
It stores DEs, fields, queries (with SQL), automations (steps, status, last run), journeys (activities, entry events), entry events, and emails/blocks/CloudPages (with code) in `data/sandbox.db`. Re-sync before answering if the inventory is older than the user's recent changes. The commands print the sync time.

| Question | Command |
|---|---|
| What breaks if I change/delete X? | `audit impact <DE name/key, query, email, block…>` |
| Where is field F of DE X used? | `audit impact <DE>.<Field>` (text match on code — treat as a starting point and say so) |
| Where do we reference / use … ? | `audit search "<text>"` (or `--regex`) |
| What's wrong / risky in this BU? | `audit health` (`--severity error,warn`, `--stale-days N`) |
| Give me an overview / report | `audit report` → `data/reports/audit_<MID>_live.html` |

How to read impact:
- **"Depends on this"** walks users of the object, then follows the data. For example, a query that reads X and writes Y brings in Y's users: entry events, then journeys.
- **"This depends on"** lists what the object needs.

Limits to state when relevant:
- Imports, API writes, UI sends and other BUs or shared (ENT) DEs aren't visible, so an "unreferenced DE" may still be used, and a "missing de" may be a shared DE.
- Field usage comes from text matching.

When reporting findings, group them by severity, explain the likely cause in a sentence, and suggest a fix. Don't apply the fix unless asked. Offer the HTML report when the user wants something to share.

## 2. SQL, AMPscript and SSJS development
Write code, then **lint before you hand it over**. Run `audit sync` first so DE and field checks work.

| Task | Command |
|---|---|
| Check a query (SFMC rules + SFMC's own validator) | `--mode live sql lint <file\|"SQL"> [--target <DE>]` |
| Explain a query (sources, joins, filters, outputs) | `sql explain <file\|"SQL">`, then describe it in plain English |
| See what a SELECT returns | `--mode live sql run <file\|"SQL"> --top 50` |
| Check AMPscript / SSJS (file, or an email/block/page by name) | `--mode live amp lint <file\|name> [--de <sendable DE>]` |
| See an email rendered for a real subscriber | `--mode live amp render <email key> --de <DE> --key <SubscriberKey>` |

**SQL rules to write by:**
- One `SELECT` per query, with no variables, temp tables or `SELECT INTO`.
- Name every expression column.
- Use `ORDER BY` only together with `TOP`.
- List columns explicitly. `SELECT *` fails when there's a JOIN.
- Dedupe to the target's primary key with `ROW_NUMBER() OVER (PARTITION BY …)`.
- Filter event data views (`_Sent`, `_Open`, `_Click`, …) by date, keeping within about 6 months.
- `GETDATE()` is US Central Standard Time (UTC-6, no daylight saving).

**`sql run` in live mode:**
- It creates a temporary DE, query and automation called `CK_Scratch_<hash>`, runs them (about 2 minutes, because SFMC queues the automation), shows the rows, and deletes all three.
- It writes to the tenant temporarily, so say so and get the user's go-ahead before running it.
- It refuses `SELECT *`.

**AMPscript and SSJS rules:**
- Compare with `==`, and use straight quotes.
- Every DE and field name in `Lookup`/`Upsert…` calls must exist.
- SSJS needs `Platform.Load("Core","1.1.1")` before using Core objects. It is ES3/5 only, so no `let`, `const`, arrow functions or template literals. Wrap writes in try/catch.

**Reading results:**
- `variable not set here` is informational. Variables often come from the template or from included blocks.
- `amp render` uses SFMC's server-side preview, so personalization, lookups and the account's sender address are real. "verified" means the rendered email shows that subscriber's address.

## 3. Proofing DEs for emails with visibility conditions
Use this when an email shows or hides blocks by subscriber data, and the developer needs proof rows that cover the combinations.

**Writing the email (kit-built):**
- Give the email `sections:` with `when:` conditions. `briefs/ownernews.yaml` is the model.
- Condition syntax: `Field == v`, `!=`, `> >= < <=`, `in [a, b]`, `not in [..]`, `is empty`, `is not empty`, combined with `and`, `or` and `( )`.
- Sections that share a `group` are first-match (IF/ELSEIF/ELSE), and `when: otherwise` is the fallback.
- The kit compiles sections to AMPscript and marks each block so it can be verified.

**Emails built elsewhere:** pass the Content Builder customer key plus `--de <audience DE>`. Their IF conditions are parsed on a best-effort basis. There are no block markers, so the rows cover the value combinations, but `verify` can only report render errors.

**Workflow** (live writes need confirmation; never send):
1. `proof analyze <brief.yaml|email key>` lists the conditions and candidate values: values from the conditions, an "other" value, empty, and common values from the data.
2. `--mode live proof setup <target>` creates or refreshes `CK_Proof_Values`, `CK_Proof_Settings` and the form block `CK_PROOF_FormBlock`. The first time, the user publishes a CloudPage containing only `%%=ContentBlockByKey("CK_PROOF_FormBlock")=%%`.
3. The user opens `<page>?email=<EmailKey>`, ticks or adds values, enters proof inboxes, chooses the coverage and saves. `proof answer --inboxes … --select "Field=a|b"` does the same from the CLI, if the user asks.
4. `--mode live proof generate <target>` builds the sendable `CK_<CODE>_Proofing`:
   - **Smart** (default): every pair of values appears together, and every block is seen both shown and hidden.
   - **Full:** every permutation, up to MaxRows.
   - **Per block:** each block shown once and hidden once.
   - Rows are named `PROOF-<CODE>-NNN`, with `FirstName` "Proof NNN" and inboxes assigned round-robin, plus `ProofCase` and `ExpectedBlocks` columns.
5. `--mode live proof verify <target>`: SFMC renders every row server-side, and the kit compares the blocks that rendered against `ExpectedBlocks`. The report goes to `data/reports/proof_<CODE>_live.html`.
6. The user sends proofs from Content Builder: **Preview and Test → Test Send → Recipients: Data Extension → `CK_<CODE>_Proofing`**.

**SFMC quirk:** preview row numbers don't follow the REST row order, so `verify` and `amp render --key` match each render to its row by a column that has unique values (for example "Proof 007").
