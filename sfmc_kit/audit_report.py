"""Self-contained HTML audit report (no external assets)."""
from __future__ import annotations

import html
from collections import Counter
from datetime import datetime

from .audit import AuditStore

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#111827;--muted:#6b7280;--line:#e5e7eb;--err:#b91c1c;--warn:#b45309;--info:#1d4ed8;--accent:#0b3d91}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--ink:#e5e7eb;--muted:#9ca3af;--line:#2a2f3a;--err:#f87171;--warn:#fbbf24;--info:#93c5fd;--accent:#93c5fd}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 48px}h1{font-size:24px;margin:0 0 4px}h2{font-size:18px;margin:28px 0 10px}
.muted{color:var(--muted)}.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin:16px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}.tile b{display:block;font-size:22px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow-x:auto}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600;position:sticky;top:0;background:var(--card)}
.sev{font-weight:700;font-size:12px;text-transform:uppercase}.error{color:var(--err)}.warn{color:var(--warn)}.info{color:var(--info)}
code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
"""


def _t(rows: list[dict], cols: list[tuple[str, str]], sev_col: str | None = None) -> str:
    if not rows:
        return '<p class="muted">None.</p>'
    head = "".join(f"<th>{html.escape(label)}</th>" for _, label in cols)
    body = []
    for r in rows:
        tds = []
        for c, _ in cols:
            v = html.escape(str(r.get(c, "") if r.get(c) is not None else ""))
            if c == sev_col:
                v = f'<span class="sev {v}">{v}</span>'
            tds.append(f"<td>{v}</td>")
        body.append("<tr>" + "".join(tds) + "</tr>")
    return f'<div class="card"><table><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def render(store: AuditStore, findings: list[dict]) -> str:
    sync = store.synced() or {"synced_at": "?", "counts": {}}
    c = sync["counts"]
    sev = Counter(f["severity"] for f in findings)
    tiles = [("Data extensions", c.get("de", 0)), ("Queries", c.get("query", 0)),
             ("Automations", c.get("automation", 0)), ("Journeys", c.get("journey", 0)),
             ("Emails", c.get("email", 0)), ("Blocks & pages", c.get("block", 0) + c.get("webpage", 0) + c.get("page", 0)),
             ("Dependencies", c.get("edges", 0)), ("Errors", sev.get("error", 0)), ("Warnings", sev.get("warn", 0))]
    autos = [{"name": a["name"], "status": a["meta"].get("status"), "last_run": a["meta"].get("last_run"),
              "last_status": a["meta"].get("last_run_status"),
              "steps": " → ".join(", ".join(x["name"] or x["type"] for x in s["activities"]) for s in a["meta"].get("steps", []))}
             for a in store.objects("automation")]
    journeys = [{"name": j["name"], "status": j["meta"].get("status"), "version": j["meta"].get("version"),
                 "activities": len(j["meta"].get("activities", [])), "modified": j["modified"]}
                for j in store.objects("journey")]
    edges = [dict(r) for r in store.db.execute(
        "SELECT src_type, src_key, rel, dst_type, dst_key FROM inv_edges WHERE mid=? AND mode=? "
        "ORDER BY src_type, src_key", (store.mid, store.mode))]
    for e in edges:
        e["src"] = f"{e['src_type']}: {store.name_of(e['src_type'], e['src_key'])}"
        e["dst"] = f"{e['dst_type']}: {store.name_of(e['dst_type'], e['dst_key'])}"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SFMC Tenant Audit</title><style>{CSS}</style></head><body><main>
<h1>SFMC tenant audit</h1><p class="muted">MID {html.escape(store.mid)} · {store.mode} · synced {html.escape(sync['synced_at'])} · report {datetime.now():%Y-%m-%d %H:%M}</p>
<div class="tiles">{''.join(f'<div class="tile"><b>{v}</b>{html.escape(k)}</div>' for k, v in tiles)}</div>
<h2>Findings</h2>{_t(findings, [("severity", "Severity"), ("check", "Check"), ("object", "Object"), ("detail", "Detail")], "severity")}
<h2>Automations</h2>{_t(autos, [("name", "Automation"), ("status", "Status"), ("last_run", "Last run"), ("last_status", "Last result"), ("steps", "Steps")])}
<h2>Journeys</h2>{_t(journeys, [("name", "Journey"), ("status", "Status"), ("version", "Version"), ("activities", "Activities"), ("modified", "Modified")])}
<h2>Dependencies</h2>{_t(edges, [("src", "Object"), ("rel", "Relation"), ("dst", "Target")])}
</main></body></html>"""
