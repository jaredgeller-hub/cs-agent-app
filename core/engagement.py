# core/engagement.py

import csv
import io


def clamp(x, lo=0, hi=100):
    return max(lo, min(hi, x))


def band(score: float):
    if score < 40:
        return "low"
    if score < 70:
        return "medium"
    return "high"


def compute_engagement_from_csv(csv_text: str):
    """
    Expect headers (case-insensitive, flexible order):
    account, last_activity_days, monthly_active_users,
    license_count, tickets_open, adoption_rate

    adoption_rate is 0-1 or 0-100 (both accepted)
    """
    
    f = io.StringIO(csv_text)
    reader = csv.DictReader(f)
    rows = []

    for raw in reader:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}

        name = row.get("account") or "Unknown"

        # Map your real CSV fields
        try:
            last_days = float(row.get("last_login_days") or 999)
        except:
            last_days = 999.0

        try:
            mau = float(row.get("dau") or 0)
        except:
            mau = 0.0

        # You don't currently have license count in CSV
        # We'll assume 1 to avoid division errors
        lic = 1.0

        try:
            tickets = float(row.get("backlog") or 0)
        except:
            tickets = 0.0

        try:
            ar = float(row.get("macro_usage_pct") or 0)
            ar = ar / 100.0  # Convert percent to 0-1
        except:
            ar = 0.0

        score = 100.0
        score -= clamp(last_days * 0.6, 0, 50)
        score -= clamp(tickets * 2.0, 0, 30)
        score -= clamp((1 - ar) * 50.0, 0, 50)

        usage_ratio = mau / lic if lic > 0 else 0
        if usage_ratio < 0.5:
            score -= 10
        if usage_ratio < 0.25:
            score -= 10

        score = clamp(score, 0, 100)

                # --- Attention Drivers ---
        drivers = []

        if last_days > 14:
            drivers.append("Login inactivity > 14 days")

        if tickets > 50:
            drivers.append("Support backlog > 50")

        if ar < 0.2:
            drivers.append("Adoption below 20%")

        if score < 40:
            attention_level = "high"
        elif score < 70:
            attention_level = "medium"
        else:
            attention_level = "low"

        if attention_level == "high":
            recommended_action = "Initiate usage recovery + exec alignment"
        elif attention_level == "medium":
            recommended_action = "Proactive check-in on adoption + support load"
        else:
            recommended_action = "Maintain value momentum"

        rows.append({
            "account": name,
            "engagement_score": round(score, 1),
            "band": band(score),
            "attention_level": attention_level,
            "drivers": drivers,
            "recommended_action": recommended_action
        })

    return rows