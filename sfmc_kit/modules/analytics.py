"""Campaign analytics from SFMC Data Views.

Data Views can only be read with SQL Query Activities, so the kit creates two report DEs, two
queries and one automation per campaign, runs it, and reads the report DEs back over REST.
The same SQL runs unchanged against the mock sandbox (whose Data View tables use SFMC column names).
"""
from __future__ import annotations

from .automation import AutomationSpec, QuerySpec
from .data_extensions import DESpec, FieldSpec


def _esc(s: str) -> str:
    return s.replace("'", "''")


def report_des(prefix: str) -> tuple[DESpec, DESpec]:
    by_email = DESpec(
        key=f"{prefix}Analytics_ByEmail", name=f"{prefix}Analytics_ByEmail", folder="CampaignKit/Analytics",
        description="Per journey version and email activity KPIs (from Data Views)",
        fields=[
            FieldSpec("JourneyName", "Text", 200, pk=True), FieldSpec("VersionNumber", "Number", pk=True),
            FieldSpec("ActivityExternalKey", "Text", 200, pk=True), FieldSpec("ActivityName", "Text", 200),
            FieldSpec("EmailName", "Text", 200), FieldSpec("Sends", "Number"), FieldSpec("UniqueOpens", "Number"),
            FieldSpec("UniqueClicks", "Number"), FieldSpec("Bounces", "Number"), FieldSpec("Unsubs", "Number"),
            FieldSpec("FirstSend", "Date"), FieldSpec("LastSend", "Date"),
        ])
    daily = DESpec(
        key=f"{prefix}Analytics_Daily", name=f"{prefix}Analytics_Daily", folder="CampaignKit/Analytics",
        description="Daily send/open/click trend for the campaign journey",
        fields=[
            FieldSpec("JourneyName", "Text", 200, pk=True), FieldSpec("EventDay", "Date", pk=True),
            FieldSpec("Sends", "Number"), FieldSpec("Opens", "Number"), FieldSpec("Clicks", "Number"),
        ])
    return by_email, daily


def by_email_sql(journey_name: str) -> str:
    j = _esc(journey_name)
    return f"""
SELECT j.JourneyName, j.VersionNumber, ja.ActivityName, ja.ActivityExternalKey,
       MAX(jb.EmailName) AS EmailName,
       COUNT(DISTINCT s.SubscriberKey) AS Sends,
       COUNT(DISTINCT o.SubscriberKey) AS UniqueOpens,
       COUNT(DISTINCT c.SubscriberKey) AS UniqueClicks,
       COUNT(DISTINCT b.SubscriberKey) AS Bounces,
       COUNT(DISTINCT u.SubscriberKey) AS Unsubs,
       MIN(s.EventDate) AS FirstSend, MAX(s.EventDate) AS LastSend
FROM _Sent s
INNER JOIN _JourneyActivity ja ON s.TriggererSendDefinitionObjectID = ja.JourneyActivityObjectID
INNER JOIN _Journey j ON ja.VersionID = j.VersionID
LEFT JOIN _Job jb ON jb.JobID = s.JobID
LEFT JOIN _Open o ON o.JobID = s.JobID AND o.ListID = s.ListID AND o.BatchID = s.BatchID
                 AND o.SubscriberID = s.SubscriberID AND o.IsUnique = 1
LEFT JOIN _Click c ON c.JobID = s.JobID AND c.ListID = s.ListID AND c.BatchID = s.BatchID
                  AND c.SubscriberID = s.SubscriberID AND c.IsUnique = 1
LEFT JOIN _Bounce b ON b.JobID = s.JobID AND b.ListID = s.ListID AND b.BatchID = s.BatchID
                   AND b.SubscriberID = s.SubscriberID
LEFT JOIN _Unsubscribe u ON u.JobID = s.JobID AND u.SubscriberID = s.SubscriberID
WHERE j.JourneyName = '{j}'
GROUP BY j.JourneyName, j.VersionNumber, ja.ActivityName, ja.ActivityExternalKey
""".strip()


def daily_sql(journey_name: str) -> str:
    j = _esc(journey_name)
    return f"""
SELECT x.JourneyName, x.EventDay, SUM(x.Sends) AS Sends, SUM(x.Opens) AS Opens, SUM(x.Clicks) AS Clicks
FROM (
  SELECT j.JourneyName, CONVERT(DATE, s.EventDate) AS EventDay, 1 AS Sends, 0 AS Opens, 0 AS Clicks
  FROM _Sent s
  INNER JOIN _JourneyActivity ja ON s.TriggererSendDefinitionObjectID = ja.JourneyActivityObjectID
  INNER JOIN _Journey j ON ja.VersionID = j.VersionID
  WHERE j.JourneyName = '{j}'
  UNION ALL
  SELECT j.JourneyName, CONVERT(DATE, o.EventDate), 0, 1, 0
  FROM _Open o
  INNER JOIN _JourneyActivity ja ON o.TriggererSendDefinitionObjectID = ja.JourneyActivityObjectID
  INNER JOIN _Journey j ON ja.VersionID = j.VersionID
  WHERE j.JourneyName = '{j}' AND o.IsUnique = 1
  UNION ALL
  SELECT j.JourneyName, CONVERT(DATE, c.EventDate), 0, 0, 1
  FROM _Click c
  INNER JOIN _JourneyActivity ja ON c.TriggererSendDefinitionObjectID = ja.JourneyActivityObjectID
  INNER JOIN _Journey j ON ja.VersionID = j.VersionID
  WHERE j.JourneyName = '{j}' AND c.IsUnique = 1
) x
GROUP BY x.JourneyName, x.EventDay
""".strip()


def analytics_objects(prefix: str, journey_name: str) -> tuple[list[DESpec], list[QuerySpec], AutomationSpec]:
    by_email, daily = report_des(prefix)
    q1 = QuerySpec(key=f"{prefix}Q_Analytics_ByEmail", name=f"{prefix}Q_Analytics_ByEmail",
                   sql=by_email_sql(journey_name), target_key=by_email.key, target_name=by_email.name)
    q2 = QuerySpec(key=f"{prefix}Q_Analytics_Daily", name=f"{prefix}Q_Analytics_Daily",
                   sql=daily_sql(journey_name), target_key=daily.key, target_name=daily.name)
    auto = AutomationSpec(key=f"{prefix}AUTO_AnalyticsRefresh", name=f"{prefix}AUTO_AnalyticsRefresh",
                          steps=[[q1.key, q2.key]], description="Refresh campaign KPIs from Data Views")
    return [by_email, daily], [q1, q2], auto


def _num(v) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def summarize(rows: list[dict]) -> dict:
    tot = {k: sum(_num(r.get(k)) for r in rows) for k in ("Sends", "UniqueOpens", "UniqueClicks", "Bounces", "Unsubs")}
    delivered = tot["Sends"] - tot["Bounces"]

    def pct(a: int, b: int) -> str:
        return f"{(100.0 * a / b):.1f}%" if b else "n/a"

    return {**tot, "Delivered": delivered,
            "OpenRate": pct(tot["UniqueOpens"], delivered), "ClickRate": pct(tot["UniqueClicks"], delivered),
            "ClickToOpen": pct(tot["UniqueClicks"], tot["UniqueOpens"]),
            "BounceRate": pct(tot["Bounces"], tot["Sends"]), "UnsubRate": pct(tot["Unsubs"], delivered)}
