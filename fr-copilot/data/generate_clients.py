"""Generate 25 fully SYNTHETIC FR Copilot clients (deterministic, seeded).

No real people or real company data: names are random combinations, carriers are
fictional, account numbers are masked placeholders. Run:  python data/generate_clients.py
"""
import json
import random
from pathlib import Path

CURRENT_YEAR = 2026
CARRIER = "Lakeshore Mutual Life (fictional)"
REPS = ["rep.jordan", "rep.casey"]

FIRST = ["Avery", "Blake", "Camila", "Dorian", "Elena", "Felix", "Gianna", "Hector", "Isla", "Jasper",
         "Keira", "Lucan", "Mara", "Nolan", "Odette", "Pierce", "Quinn", "Rosalind", "Silas", "Talia",
         "Ulric", "Vera", "Wesley", "Xiomara", "Yusuf", "Zelda", "Brennan", "Calla", "Desmond", "Fiona"]
LAST = ["Ashworth", "Brightwater", "Calloway", "Dunmore", "Ellery", "Fairbanks", "Galloway", "Hartwell",
        "Ivers", "Juniper", "Kestrel", "Lindqvist", "Merriweather", "Northcott", "Oakhurst", "Pemberton",
        "Quillfeather", "Rourke", "Sterling", "Thistlewood", "Underhill", "Vance", "Whitlock", "Yardley"]
STATES = ["WI", "IL", "MN", "OH", "MI", "IA", "IN", "MO", "CO", "TX", "NC", "AZ"]
OCCUPATIONS = [("Software engineer", 165000), ("Registered nurse", 92000), ("High school teacher", 68000),
               ("Small business owner", 135000), ("Civil engineer", 112000), ("Pharmacist", 138000),
               ("Marketing manager", 118000), ("Electrician", 84000), ("Physician", 285000),
               ("Accountant", 96000), ("Sales director", 172000), ("Physical therapist", 99000),
               ("Architect", 121000), ("Police officer", 79000), ("Dentist", 210000)]
RISK = ["conservative", "moderate", "moderate", "growth", "aggressive"]


def masked(rng):
    return f"****{rng.randint(1000, 9999)}"


def make_client(i, rng):
    cid = f"C-{i:03d}"
    age = rng.randint(29, 70)
    occ, base = rng.choice(OCCUPATIONS)
    income = int(round(base * rng.uniform(0.8, 1.25), -3))
    married = rng.random() < 0.65
    spouse_age = age + rng.randint(-5, 5) if married else None
    spouse_income = int(round(rng.choice([0, 0, 42000, 61000, 88000]) * rng.uniform(0.8, 1.2), -3)) if married else 0
    n_kids = rng.choice([0, 0, 1, 2, 2, 3]) if age < 58 else 0
    kids = [{"relationship": "child", "age": max(0, min(age - 22, rng.randint(1, 20)))} for _ in range(n_kids)]
    working_years = max(age - 25, 1)
    retire_age = 67 if age >= 60 else rng.choice([62, 65, 65, 67])
    rate = rng.uniform(0.06, 0.15)
    k401 = int(round(income * rate * working_years * rng.uniform(1.0, 2.2), -2))
    mortgage = int(round(rng.choice([0, 180000, 260000, 340000, 420000]) * (1.0 if age < 60 else 0.3), -3))
    client = {
        "client_id": cid,
        "first_name": rng.choice(FIRST),
        "last_name": rng.choice(LAST),
        "age": age,
        "birth_year": CURRENT_YEAR - age,
        "state": rng.choice(STATES),
        "occupation": occ if age < 67 else f"Retired {occ.lower()}",
        "annual_income": income if age < 67 else 0,
        "marital_status": "married" if married else rng.choice(["single", "divorced", "widowed"]),
        "spouse_age": spouse_age,
        "spouse_income": spouse_income,
        "dependents": kids,
        "target_retirement_age": retire_age,
        "desired_retirement_income_pct": rng.choice([0.7, 0.75, 0.8, 0.85]),
        "social_security_estimate_annual": int(round(min(income, 168600) * rng.uniform(0.22, 0.32), -2)),
        "debts": {"mortgage_balance": mortgage, "other_debt": int(round(rng.choice([0, 0, 8000, 15000, 32000]), -2))},
        "emergency_fund": int(round(income * rng.uniform(0.05, 0.5), -2)),
        "risk_tolerance": rng.choice(RISK),
        "assigned_rep": REPS[i % 2],
        "notes": rng.choice(["Prefers email summaries.", "Interested in college funding.",
                             "Recently changed employers.", "Considering early retirement.",
                             "Caring for an aging parent.", "Wants to review beneficiaries."]),
    }
    policies = []
    if rng.random() < 0.7:
        issue = rng.randint(2008, 2023)
        policies.append({"policy_id": f"TL-{rng.randint(100000, 999999)}", "type": "Term Life",
                         "term_years": rng.choice([10, 20, 30]), "issue_year": issue, "insured": "client",
                         "death_benefit": rng.choice([250000, 500000, 750000, 1000000]),
                         "annual_premium": rng.randint(300, 1600), "status": "in force", "carrier": CARRIER})
    if rng.random() < 0.35:
        policies.append({"policy_id": f"WL-{rng.randint(100000, 999999)}", "type": "Whole Life",
                         "issue_year": rng.randint(2000, 2022), "insured": "client",
                         "death_benefit": rng.choice([100000, 250000, 500000]),
                         "cash_value": rng.randint(8000, 90000), "annual_premium": rng.randint(1800, 7200),
                         "status": "in force", "carrier": CARRIER})
    if age < 65 and rng.random() < 0.6:
        policies.append({"policy_id": f"GL-{rng.randint(100000, 999999)}", "type": "Group Life (employer)",
                         "insured": "client", "death_benefit": income * rng.choice([1, 2]),
                         "portable": False, "status": "in force", "carrier": "Employer plan (fictional)"})
    if age < 62 and rng.random() < 0.4:
        policies.append({"policy_id": f"DI-{rng.randint(100000, 999999)}", "type": "Disability Income",
                         "monthly_benefit": int(round(income * 0.6 / 12, -2)), "benefit_period": "to age 65",
                         "annual_premium": rng.randint(900, 3200), "status": "in force", "carrier": CARRIER})
    accounts = [{"account_id": masked(rng), "type": "401(k)", "owner": "client", "balance": k401,
                 "annual_contribution": int(round(income * rate, -2)) if age < 67 else 0,
                 "employer_match_annual": int(round(income * rng.choice([0.03, 0.04, 0.05]), -2)) if age < 67 else 0,
                 "allocation": {"stocks": 0.7, "bonds": 0.25, "cash": 0.05}}]
    if rng.random() < 0.7:
        accounts.append({"account_id": masked(rng), "type": rng.choice(["Roth IRA", "Traditional IRA"]), "owner": "client",
                         "balance": int(round(rng.uniform(5000, 250000), -2)),
                         "annual_contribution": rng.choice([0, 3000, 6500, 7000]) if age < 67 else 0,
                         "employer_match_annual": 0, "allocation": {"stocks": 0.8, "bonds": 0.15, "cash": 0.05}})
    if rng.random() < 0.4:
        accounts.append({"account_id": masked(rng), "type": "Brokerage", "owner": "joint" if married else "client",
                         "balance": int(round(rng.uniform(10000, 400000), -2)), "annual_contribution": rng.choice([0, 2400, 6000]),
                         "employer_match_annual": 0, "allocation": {"stocks": 0.6, "bonds": 0.3, "cash": 0.1}})
    if kids and rng.random() < 0.5:
        accounts.append({"account_id": masked(rng), "type": "529 College Savings", "owner": "client",
                         "balance": int(round(rng.uniform(4000, 60000), -2)), "annual_contribution": 2400,
                         "employer_match_annual": 0, "allocation": {"stocks": 0.5, "bonds": 0.45, "cash": 0.05}})
    client["policies"] = policies
    client["accounts"] = accounts
    return client


def c007():
    """Hand-crafted showcase client used in the class demo script."""
    return {
        "client_id": "C-007", "first_name": "Marisol", "last_name": "Thistlewood", "age": 46, "birth_year": 1980,
        "state": "WI", "occupation": "Civil engineer", "annual_income": 142000, "marital_status": "married",
        "spouse_age": 44, "spouse_income": 38000,
        "dependents": [{"relationship": "child", "age": 12}, {"relationship": "child", "age": 9}],
        "target_retirement_age": 65, "desired_retirement_income_pct": 0.8,
        "social_security_estimate_annual": 32400,
        "debts": {"mortgage_balance": 318000, "other_debt": 14500}, "emergency_fund": 21000,
        "risk_tolerance": "moderate", "assigned_rep": "rep.jordan",
        "notes": "Primary earner; spouse works part-time. Asked about college funding for both kids.",
        "policies": [
            {"policy_id": "TL-470215", "type": "Term Life", "term_years": 20, "issue_year": 2016, "insured": "client",
             "death_benefit": 250000, "annual_premium": 410, "status": "in force", "carrier": CARRIER},
            {"policy_id": "GL-883104", "type": "Group Life (employer)", "insured": "client", "death_benefit": 284000,
             "portable": False, "status": "in force", "carrier": "Employer plan (fictional)"},
        ],
        "accounts": [
            {"account_id": "****4821", "type": "401(k)", "owner": "client", "balance": 212000,
             "annual_contribution": 14200, "employer_match_annual": 5680,
             "allocation": {"stocks": 0.7, "bonds": 0.25, "cash": 0.05}},
            {"account_id": "****1937", "type": "Roth IRA", "owner": "client", "balance": 38500,
             "annual_contribution": 3000, "employer_match_annual": 0,
             "allocation": {"stocks": 0.85, "bonds": 0.1, "cash": 0.05}},
            {"account_id": "****6650", "type": "Brokerage", "owner": "joint", "balance": 22000,
             "annual_contribution": 0, "employer_match_annual": 0,
             "allocation": {"stocks": 0.6, "bonds": 0.3, "cash": 0.1}},
            {"account_id": "****3092", "type": "529 College Savings", "owner": "client", "balance": 18400,
             "annual_contribution": 2400, "employer_match_annual": 0,
             "allocation": {"stocks": 0.5, "bonds": 0.45, "cash": 0.05}},
        ],
    }


def main():
    rng = random.Random(20261102)
    clients, used = [], set()
    for i in range(1, 26):
        c = c007() if i == 7 else make_client(i, rng)
        while (c["first_name"], c["last_name"]) in used:
            c["first_name"] = rng.choice(FIRST)
        used.add((c["first_name"], c["last_name"]))
        c["synthetic"] = True
        clients.append(c)
    out = Path(__file__).with_name("clients.json")
    out.write_text(json.dumps(clients, indent=1))
    print(f"wrote {len(clients)} synthetic clients -> {out}")


if __name__ == "__main__":
    main()
