"""Deterministic sample data for a fictional automaker, "Aurora Motors".

All names are synthetic and all email addresses use reserved example domains (RFC 2606),
so nothing generated here can reach a real inbox.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta

from ..modules.data_extensions import DESpec, FieldSpec

MASTER_PREFIX = "CK_Master_"
BRAND = "Aurora Motors"

MODELS = {
    # model: (segment, powertrains, base price, first model year)
    "Volt X":       ("EV SUV",          ["EV"],               52000, 2021),
    "Pulse":        ("Compact EV",      ["EV"],               34000, 2022),
    "Strada":       ("Sedan",           ["ICE", "Hybrid"],    31000, 2014),
    "Terra Hybrid": ("Crossover",       ["Hybrid"],           38000, 2017),
    "Ridge":        ("Pickup",          ["ICE"],              46000, 2014),
    "Summit":       ("3-Row SUV",       ["ICE", "Hybrid"],    49000, 2015),
}
TRIMS = ["Base", "Sport", "Premium", "Limited"]
COLORS = ["Glacier White", "Obsidian Black", "Aurora Blue", "Ember Red", "Lunar Silver", "Forest Green"]

FIRST = ["Ava", "Liam", "Mia", "Noah", "Zoe", "Ethan", "Ivy", "Lucas", "Nora", "Mason", "Aria", "Leo", "Maya",
         "Owen", "Ella", "Kai", "Luna", "Elijah", "Chloe", "Mateo", "Priya", "Arjun", "Mei", "Hiro", "Sofia",
         "Diego", "Amara", "Kofi", "Fatima", "Omar", "Hannah", "Jonas", "Ines", "Tomas", "Aisha", "Ravi",
         "Grace", "Samuel", "Lea", "Felix", "Nadia", "Victor", "Rosa", "Andre", "Jade", "Malik", "Emma", "Ryan"]
LAST = ["Nguyen", "Patel", "Garcia", "Kim", "Smith", "Johnson", "Lopez", "Brown", "Chen", "Singh", "Martin",
        "Rossi", "Okafor", "Silva", "Novak", "Haddad", "Larsen", "Murphy", "Tanaka", "Kowalski", "Reyes", "Ali",
        "Fischer", "Dubois", "Moreau", "Clark", "Walker", "Young", "Hall", "Rivera", "Cohen", "Mendes", "Sato"]
METROS = [  # city, state, region, lat, lon, zip prefix
    ("Detroit", "MI", "Midwest", 42.3314, -83.0458, "482"), ("Chicago", "IL", "Midwest", 41.8781, -87.6298, "606"),
    ("Columbus", "OH", "Midwest", 39.9612, -82.9988, "432"), ("Minneapolis", "MN", "Midwest", 44.9778, -93.2650, "554"),
    ("Atlanta", "GA", "South", 33.7490, -84.3880, "303"), ("Dallas", "TX", "South", 32.7767, -96.7970, "752"),
    ("Houston", "TX", "South", 29.7604, -95.3698, "770"), ("Miami", "FL", "South", 25.7617, -80.1918, "331"),
    ("Charlotte", "NC", "South", 35.2271, -80.8431, "282"), ("Nashville", "TN", "South", 36.1627, -86.7816, "372"),
    ("New York", "NY", "Northeast", 40.7128, -74.0060, "100"), ("Boston", "MA", "Northeast", 42.3601, -71.0589, "021"),
    ("Philadelphia", "PA", "Northeast", 39.9526, -75.1652, "191"), ("Newark", "NJ", "Northeast", 40.7357, -74.1724, "071"),
    ("Los Angeles", "CA", "West", 34.0522, -118.2437, "900"), ("San Jose", "CA", "West", 37.3382, -121.8863, "951"),
    ("Seattle", "WA", "West", 47.6062, -122.3321, "981"), ("Denver", "CO", "West", 39.7392, -104.9903, "802"),
    ("Phoenix", "AZ", "West", 33.4484, -112.0740, "850"), ("Portland", "OR", "West", 45.5152, -122.6784, "972"),
]
DEALER_SUFFIX = ["Aurora", "Aurora Motors of", "Aurora Center", "Aurora EV Studio"]
SERVICE_TYPES = [("Oil & Filter", 89), ("Tire Rotation", 49), ("Brake Service", 320), ("Battery Health Check", 0),
                 ("Software Update", 0), ("Annual Inspection", 159), ("Recall Repair", 0), ("Detailing", 199)]
EMAIL_DOMAINS = ["example.com", "example.net", "example.org"]

TODAY = date(2026, 11, 18)


def _d(x: date | datetime) -> str:
    return x.strftime("%Y-%m-%d")


def master_de_specs() -> dict[str, DESpec]:
    P, folder = MASTER_PREFIX, "CampaignKit/Master"
    F = FieldSpec
    specs = [
        DESpec(f"{P}Dealers", f"{P}Dealers", folder=folder, description="Aurora Motors dealer network", fields=[
            F("DealerID", "Text", 10, pk=True), F("DealerName", "Text", 100), F("Region", "Text", 20),
            F("City", "Text", 50), F("State", "Text", 2), F("PostalCode", "Text", 10), F("Phone", "Phone"),
            F("Latitude", "Decimal", precision=9, scale=6), F("Longitude", "Decimal", precision=9, scale=6),
            F("ServiceBays", "Number"), F("IsEVCertified", "Boolean")]),
        DESpec(f"{P}Customers", f"{P}Customers", folder=folder, sendable=True,
               description="Customer master (sendable on SubscriberKey)", fields=[
            F("SubscriberKey", "Text", 50, pk=True), F("EmailAddress", "EmailAddress", required=True),
            F("FirstName", "Text", 50), F("LastName", "Text", 50), F("Phone", "Phone"), F("City", "Text", 50),
            F("State", "Text", 2), F("PostalCode", "Text", 10), F("Region", "Text", 20),
            F("PreferredDealerID", "Text", 10), F("LoyaltyTier", "Text", 20), F("EmailOptIn", "Boolean"),
            F("SMSOptIn", "Boolean"), F("BirthYear", "Number"), F("HouseholdIncomeBand", "Text", 20),
            F("ModelInterest", "Text", 50), F("VehicleCount", "Number"), F("DateJoined", "Date"),
            F("LastServiceDate", "Date")]),
        DESpec(f"{P}Vehicles", f"{P}Vehicles", folder=folder, description="Owned vehicles", fields=[
            F("VIN", "Text", 17, pk=True), F("SubscriberKey", "Text", 50, required=True), F("Model", "Text", 50),
            F("Segment", "Text", 30), F("Trim", "Text", 20), F("Powertrain", "Text", 10), F("ModelYear", "Number"),
            F("Color", "Text", 30), F("PurchaseDate", "Date"), F("PurchaseType", "Text", 10),
            F("Mileage", "Number"), F("WarrantyEndDate", "Date"), F("IsCurrent", "Boolean")]),
        DESpec(f"{P}ServiceAppointments", f"{P}ServiceAppointments", folder=folder, fields=[
            F("AppointmentID", "Text", 20, pk=True), F("VIN", "Text", 17), F("SubscriberKey", "Text", 50),
            F("DealerID", "Text", 10), F("ServiceType", "Text", 50), F("ServiceDate", "Date"),
            F("Status", "Text", 20), F("Cost", "Decimal", precision=10, scale=2)]),
        DESpec(f"{P}Leads_TestDrives", f"{P}Leads_TestDrives", folder=folder, fields=[
            F("LeadID", "Text", 20, pk=True), F("SubscriberKey", "Text", 50), F("Model", "Text", 50),
            F("Source", "Text", 20), F("Status", "Text", 20), F("DealerID", "Text", 10),
            F("CreatedDate", "Date"), F("TestDriveDate", "Date")]),
        DESpec(f"{P}Purchases", f"{P}Purchases", folder=folder, fields=[
            F("PurchaseID", "Text", 20, pk=True), F("SubscriberKey", "Text", 50), F("VIN", "Text", 17),
            F("DealerID", "Text", 10), F("PurchaseDate", "Date"), F("Amount", "Decimal", precision=10, scale=2),
            F("FinanceType", "Text", 10), F("TradeInModel", "Text", 50),
            F("TradeInValue", "Decimal", precision=10, scale=2)]),
    ]
    return {s.key: s for s in specs}


def _vin(rng: random.Random, year: int) -> str:
    chars = "ABCDEFGHJKLMNPRSTUVWXYZ0123456789"
    year_code = "ABCDEFGHJKLMNPRSTVWXY123456789"[(year - 2010) % 30]
    return "1AM" + "".join(rng.choice(chars) for _ in range(6)) + year_code + "D" + "".join(
        rng.choice("0123456789") for _ in range(6))


def generate(scale: str = "small", seed: int = 2026) -> dict[str, list[dict]]:
    n_customers = {"tiny": 60, "small": 500, "full": 5000}[scale]
    rng = random.Random(seed)
    P = MASTER_PREFIX

    dealers = []
    for i in range(40):
        city, st, region, lat, lon, z = METROS[i % len(METROS)]
        suffix = DEALER_SUFFIX[i % len(DEALER_SUFFIX)]
        name = f"{suffix} {city}" if suffix.endswith("of") else f"{city} {suffix}"
        if i >= len(METROS):
            name += " North"
        dealers.append({"DealerID": f"D{i + 1:03d}", "DealerName": name, "Region": region, "City": city, "State": st,
                        "PostalCode": f"{z}{rng.randint(10, 99)}", "Phone": f"555-01{rng.randint(10, 99)}",
                        "Latitude": round(lat + rng.uniform(-0.15, 0.15), 6),
                        "Longitude": round(lon + rng.uniform(-0.15, 0.15), 6),
                        "ServiceBays": rng.randint(6, 30), "IsEVCertified": rng.random() < 0.6})
    dealers_by_city: dict[str, list[dict]] = {}
    for d in dealers:
        dealers_by_city.setdefault(d["City"], []).append(d)

    customers, vehicles, services, leads, purchases = [], [], [], [], []
    used_emails: set[str] = set()
    for i in range(n_customers):
        fn, ln = rng.choice(FIRST), rng.choice(LAST)
        city, st, region, *_rest, z = rng.choice(METROS)
        dealer = rng.choice(dealers_by_city[city])
        base = f"{fn}.{ln}".lower()
        email = f"{base}@{rng.choice(EMAIL_DOMAINS)}"
        k = 1
        while email in used_emails:
            k += 1
            email = f"{base}{k}@{rng.choice(EMAIL_DOMAINS)}"
        used_emails.add(email)
        skey = f"AUR-{100000 + i}"
        joined = TODAY - timedelta(days=rng.randint(30, 12 * 365))
        cust = {"SubscriberKey": skey, "EmailAddress": email, "FirstName": fn, "LastName": ln,
                "Phone": f"555-{rng.randint(100, 999)}-{rng.randint(1000, 9999)}", "City": city, "State": st,
                "PostalCode": f"{z}{rng.randint(10, 99)}", "Region": region, "PreferredDealerID": dealer["DealerID"],
                "LoyaltyTier": rng.choices(["Standard", "Silver", "Gold", "Platinum"], [50, 28, 15, 7])[0],
                "EmailOptIn": rng.random() < 0.88, "SMSOptIn": rng.random() < 0.35,
                "BirthYear": rng.randint(1950, 2004),
                "HouseholdIncomeBand": rng.choices(["<50k", "50-100k", "100-150k", "150k+"], [20, 40, 25, 15])[0],
                "ModelInterest": None, "VehicleCount": 0, "DateJoined": _d(joined), "LastServiceDate": None}

        # ~85% are owners; prospects have only leads
        n_veh = 0 if rng.random() < 0.15 else rng.choices([1, 2, 3], [70, 24, 6])[0]
        last_service: date | None = None
        for v in range(n_veh):
            model = rng.choice(list(MODELS))
            seg, pts, price, first_year = MODELS[model]
            year = rng.randint(max(first_year, joined.year - 1), min(2026, TODAY.year))
            pdate = date(year, 1, 1) + timedelta(days=rng.randint(0, 300))
            pdate = min(max(pdate, joined), TODAY)
            vin = _vin(rng, year)
            ptype = rng.choices(["Finance", "Lease", "Cash"], [55, 30, 15])[0]
            age_years = max((TODAY - pdate).days / 365.25, 0.05)
            vehicles.append({"VIN": vin, "SubscriberKey": skey, "Model": model, "Segment": seg,
                             "Trim": rng.choice(TRIMS), "Powertrain": rng.choice(pts), "ModelYear": year,
                             "Color": rng.choice(COLORS), "PurchaseDate": _d(pdate), "PurchaseType": ptype,
                             "Mileage": int(age_years * rng.randint(7000, 16000)),
                             "WarrantyEndDate": _d(pdate + timedelta(days=int(365.25 * (8 if "EV" in pts else 5)))),
                             "IsCurrent": True})
            trade = rng.random() < 0.35
            amount = price * {"Base": 1, "Sport": 1.08, "Premium": 1.15, "Limited": 1.25}[vehicles[-1]["Trim"]]
            purchases.append({"PurchaseID": f"P{len(purchases) + 1:06d}", "SubscriberKey": skey, "VIN": vin,
                              "DealerID": dealer["DealerID"], "PurchaseDate": _d(pdate),
                              "Amount": round(amount * rng.uniform(0.93, 1.03), 2), "FinanceType": ptype,
                              "TradeInModel": rng.choice(list(MODELS)) if trade else None,
                              "TradeInValue": round(rng.uniform(4000, 22000), 2) if trade else None})
            # service history: roughly every 6-9 months
            d0 = pdate + timedelta(days=rng.randint(120, 270))
            while d0 < TODAY + timedelta(days=45):
                stype, cost = rng.choice(SERVICE_TYPES)
                if d0 > TODAY:
                    status = "Scheduled"
                else:
                    status = rng.choices(["Completed", "NoShow", "Cancelled"], [88, 5, 7])[0]
                services.append({"AppointmentID": f"S{len(services) + 1:07d}", "VIN": vin, "SubscriberKey": skey,
                                 "DealerID": dealer["DealerID"], "ServiceType": stype, "ServiceDate": _d(d0),
                                 "Status": status,
                                 "Cost": round(cost * rng.uniform(0.9, 1.3), 2) if status == "Completed" else 0})
                if status == "Completed" and (last_service is None or d0 > last_service):
                    last_service = d0
                d0 += timedelta(days=rng.randint(150, 300))
                if rng.random() < 0.12:  # some owners lapse
                    break
        cust["VehicleCount"] = n_veh
        cust["LastServiceDate"] = _d(last_service) if last_service else None

        # leads / test drives: prospects always, owners sometimes (often EV-curious)
        if n_veh == 0 or rng.random() < 0.3:
            for _ in range(rng.choices([1, 2], [80, 20])[0]):
                model = rng.choices(list(MODELS), [30, 22, 12, 14, 10, 12])[0]
                created = TODAY - timedelta(days=rng.randint(1, 540))
                status = rng.choices(["New", "Contacted", "TestDriven", "Converted", "Lost"], [20, 25, 25, 10, 20])[0]
                td = created + timedelta(days=rng.randint(2, 21)) if status in ("TestDriven", "Converted") else None
                leads.append({"LeadID": f"L{len(leads) + 1:06d}", "SubscriberKey": skey, "Model": model,
                              "Source": rng.choice(["Web", "Dealer", "Event", "Social", "Referral"]), "Status": status,
                              "DealerID": dealer["DealerID"], "CreatedDate": _d(created),
                              "TestDriveDate": _d(td) if td and td <= TODAY else None})
                cust["ModelInterest"] = model
        customers.append(cust)

    return {f"{P}Dealers": dealers, f"{P}Customers": customers, f"{P}Vehicles": vehicles,
            f"{P}ServiceAppointments": services, f"{P}Leads_TestDrives": leads, f"{P}Purchases": purchases}
