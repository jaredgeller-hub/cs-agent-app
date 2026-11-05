# main.py — CS Agent (Full Reset)
# FastAPI + Slack-like UI + CSV Engagement + LLM Summarizer (strict JSON schema)

from fastapi import FastAPI, UploadFile, File, Request, Form, Depends, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, Response, RedirectResponse, FileResponse
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from pathlib import Path
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
import json, uuid, csv, io, os, hashlib, hmac, secrets, time
import asyncio
import logging
import re
import stripe
import requests  # NEW
from product.logging_config import configure_logging, request_id_var, csm_id_var
from product.csm_id_validation import (
    validate_csm_id as _validate_csm_id,
    safe_data_path as _safe_data_path,
    safe_data_file as _safe_data_file,
)

configure_logging()
logger = logging.getLogger("cs-agent")


# =============== LLM Setup =================
from dotenv import load_dotenv
import anthropic
from product.instrumentation.tracker import log_interaction
from product.auth import (
    create_session, get_session, delete_session,
    validate_credentials, get_csm_name, get_csm_email, SESSION_COOKIE,
)
from product.auth_clerk import verify_clerk_token
from product.demo_identity import DEMO_CSM_ID, is_demo_csm_id, is_demo_email
from product.signal_cache import read_signals

ROOT = Path(__file__).resolve().parent
DOTENV_PATH = ROOT / ".env"
load_dotenv(dotenv_path=DOTENV_PATH, override=True)

# Eager import forces fail-closed at app startup if TOKEN_ENCRYPTION_KEY is
# missing or malformed. Without this, the module would only load on the
# first save/load_token call and the app would start cleanly into a broken
# state. Must come after load_dotenv so the env var is available in dev.
import product.token_crypto  # noqa: F401

CALENDAR_PATH = ROOT / "calendar.json"  # NEW — calendar file for meetings

ANTHROPIC_KEY  = os.getenv("ANTHROPIC_API_KEY", "").strip()
# No default: with DEMO_MODE on and DEMO_PASSWORD unset, the /demo gate
# fails closed (see _demo_password_configured).
DEMO_PASSWORD  = (os.getenv("DEMO_PASSWORD") or "").strip()
from product.settings import (
    ANTHROPIC_MODEL as MODEL, COMPANY_DOMAIN, COMPANY_LEGAL_NAME, COMPANY_NAME, PUBLIC_BASE_URL,
    SUPPORT_EMAIL,
    WAITLIST_FORM_URL, billing_enabled, demo_mode_enabled, public_url,
)

# Frozen portfolio data for demo (System #1 retirement, Batch B).
# Risk bands: <=35 at_risk, 36-75 watch, 76-88 healthy, 89+ engaged.
DEMO_PORTFOLIO_FROZEN = [
    {
        "account": "Northgate Logistics",
        "risk": "at_risk",
        "score": 25,
        "trend": "declining",
        "drivers": [
            "No login in 11 days",
            "3 support tickets in last 14 days",
            "Support tickets from new contact (Dana's manager) - champion may have changed",
            "Renewal close in 45 days",
        ],
        "reason": "Low engagement signal.",
    },
    {
        "account": "Meridian Health",
        "risk": "watch",
        "score": 70,
        "trend": "stable",
        "drivers": ["Renewal close in 60 days"],
        "reason": "Renewal close in 60 days",
    },
    {
        "account": "Pinnacle Group",
        "risk": "watch",
        "score": 72,
        "trend": "stable",
        "drivers": ["High priority support ticket open"],
        "reason": "High priority support ticket open",
    },
    {
        "account": "Beacon Financial",
        "risk": "healthy",
        "score": 90,
        "trend": "stable",
        "drivers": [],
        "reason": "Active - no issues flagged.",
    },
    {
        "account": "Sutton Retail",
        "risk": "healthy",
        "score": 92,
        "trend": "stable",
        "drivers": [],
        "reason": "Active - no issues flagged.",
    },
]


def _demo_risk_summary() -> dict:
    """Dict-keyed-by-account view of DEMO_PORTFOLIO_FROZEN.

    Returns the risk and trend per account for legacy chat callers
    that look up per-account values.
    """
    return {a["account"]: {"risk": a["risk"], "trend": a["trend"]} for a in DEMO_PORTFOLIO_FROZEN}


async def _resolve_auth(request: Request) -> dict | None:
    """Try Clerk Bearer/cookie auth first, fall back to session cookie auth.

    Returns a session dict with at least {csm_id, name, email}, or None if
    neither auth method succeeds.
    """
    # 1. Try Clerk token (Bearer header or __session cookie).
    # verify_clerk_token converts every failure (no token, bad signature,
    # expired, CLERK_FRONTEND_API unset) into HTTPException(401). That is the
    # normal "not signed in with Clerk" path: fall through to step 2.
    try:
        clerk_user = await verify_clerk_token(request)
    except HTTPException:
        clerk_user = None

    if clerk_user and clerk_user.get("user_id"):
        from product.org_config import (
            OrgConfigMissingError, get_csm_config, list_active_csms,
        )
        # Map Clerk email -> csm_id via org_config. No config file at all
        # (fresh self-hosted install) means no configured CSMs: the user
        # gets the minimal identity below, keyed by their Clerk user ID.
        # A config file that exists but cannot be read or parsed is NOT
        # treated as empty: Clerk access is refused (fail closed) and the
        # request falls through to step 2, which is demo-only.
        try:
            csm_ids = list_active_csms()
        except OrgConfigMissingError:
            csm_ids = []
        except (OSError, ValueError) as exc:
            logger.error("[auth] org_config unreadable; refusing Clerk session: %s", exc)
            csm_ids = None

        if csm_ids is not None:
            email = (clerk_user.get("email") or "").lower()
            for csm_id in csm_ids:
                try:
                    cfg = get_csm_config(csm_id)
                    if (cfg.get("email") or "").lower() == email:
                        return {
                            "csm_id": csm_id,
                            "name": cfg.get("name") or clerk_user.get("first_name") or csm_id,
                            "email": email,
                        }
                except Exception:
                    continue
            # Clerk token valid but email not in org_config — return minimal dict
            return {
                "csm_id": clerk_user["user_id"],
                "name": clerk_user.get("first_name") or "",
                "email": email,
            }

    # 2. Fall back to the legacy session cookie (demo /login flow only)
    if not demo_mode_enabled():
        return None
    session = get_session(request.cookies.get(SESSION_COOKIE))
    return session


# ── JWT-only identity ────────────────────────────────────────────────────────

async def _require_jwt_identity(request: Request) -> str:
    """Verify the Clerk JWT and return the validated raw Clerk user_id.

    Returns the user_id claim from the verified JWT, after running it
    through _validate_csm_id. JWT-only — no subscription gating, no
    org_config email→csm_id mapping. The returned identity matches the
    namespace under which OAuth tokens are stored in token_store.

    Raises:
        HTTPException(401) if no JWT is present, the JWT is invalid or
            expired, JWKS verification fails, or the JWT lacks a user_id
            claim (missing key, empty string, or None value).
        HTTPException(400) if the user_id fails csm_id validation
            (e.g., contains '..', null bytes, or characters outside
            [a-zA-Z0-9_.-]).

    For routes that also require an active subscription, use
    _require_active_subscription — that helper layers subscription
    gating on top of session resolution (which may include the
    org_config email→csm_id mapping for legacy CSMs).

    Identity-source principle: identity always derives from the verified
    Clerk JWT. Routes that accept identity from any other source
    (request body, URL query string) are a bug.
    """
    clerk_info = await verify_clerk_token(request)
    user_id = clerk_info.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="JWT missing identity claim")
    return _validate_csm_id(user_id)


# ── Subscription gate ────────────────────────────────────────────────────────


class SubscriptionGatedException(Exception):
    """Raised by _require_active_subscription when subscription is inactive.

    Carries the sub_status as a typed field. A global @app.exception_handler
    serializes this to a flat 402 JSON body so every gated route emits the
    same documented schema: {"error": "subscription_required", "status": "..."}.
    Using a custom exception instead of HTTPException(detail=json.dumps(...))
    avoids FastAPI wrapping the body as {"detail": "<JSON-encoded string>"}.
    """

    def __init__(self, status: str):
        self.status = status
        super().__init__(f"subscription_required: {status}")


async def _require_active_subscription(request: Request) -> dict:
    """Resolve auth AND check subscription status. Returns the session dict.

    Raises HTTPException(402) if the user's subscription is gated.
    Bypasses the gate entirely for sarah_c (demo identity).

    Gate predicate lives in product.subscription.is_subscription_active —
    same function is used by /app/subscription-status so server and client
    agree on gate state. Gates when subscription_status is not 'active'
    AND (not 'trialing' or trial_ends_at has passed).
    """
    session = await _resolve_auth(request)
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")

    csm_id = session.get("csm_id", "")

    # Demo bypass — sarah_c has no Customer row and no subscription
    if is_demo_csm_id(csm_id):
        return session

    # BILLING_ENABLED off: no subscription gate at all. When on, everything
    # below runs unchanged and still fails closed on DB errors.
    if not billing_enabled():
        return session

    # Look up subscription state from Postgres
    sub_status = "none"
    trial_ends_at = None
    is_comp_account = False
    try:
        from product.database import SessionLocal, Customer
        if SessionLocal:
            db = SessionLocal()
            try:
                cust = db.query(Customer).filter_by(clerk_user_id=csm_id).first()
                if cust:
                    sub_status = cust.subscription_status or "none"
                    trial_ends_at = cust.trial_ends_at
                    is_comp_account = bool(cust.is_comp_account)
            finally:
                db.close()
    except Exception:
        pass  # DB failure → treat as unknown state → gate (see below)

    from product.subscription import is_subscription_active
    if not is_subscription_active(sub_status, trial_ends_at, is_comp_account):
        raise SubscriptionGatedException(status=sub_status)

    return session


SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "").strip()
def slack_post(text: str):
    """Send a simple text message to Slack via incoming webhook."""
    if not SLACK_WEBHOOK_URL:
        return
    try:
        resp = requests.post(
            SLACK_WEBHOOK_URL,
            json={"text": text},
            timeout=5,
        )
        if resp.status_code != 200:
            logger.warning("Slack post non-200: %s %s", resp.status_code, resp.text)
    except Exception as e:
        logger.warning("Slack post failed: %s", e)

_LLM_READY = bool(ANTHROPIC_KEY)
_anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None

logger.info("🔑 ANTHROPIC_API_KEY prefix: %s", ANTHROPIC_KEY[:8] + '…' if ANTHROPIC_KEY else '(none)')
logger.info("🧠 MODEL: %s", MODEL)

# =============== CSM Identity Registry ======================
# No default key: if CSM_API_KEY_SARAH_C is unset the registry is empty and
# every X-CSM-API-Key request fails closed with 401. The key only maps to
# the demo identity, so it is also refused when DEMO_MODE is off.
def _build_csm_registry() -> dict:
    key = (os.getenv("CSM_API_KEY_SARAH_C") or "").strip()
    return {key: DEMO_CSM_ID} if key else {}

CSM_REGISTRY = _build_csm_registry()

def resolve_user_id(request: Request) -> Optional[str]:
    if not demo_mode_enabled():
        return None
    api_key = request.headers.get("X-CSM-API-Key", "").strip()
    if not api_key:
        return None
    return CSM_REGISTRY.get(api_key)

def require_user_id(request: Request) -> str:
    user_id = resolve_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")
    return user_id

def set_call_mode(active: bool):
    store = load_store()
    store["call_mode"] = active  # boolean
    save_store(store)
    return active
def check_cta_reminders(entries):
    """Return overdue / due soon CTAs across all summaries."""
    today = date.today()
    overdue, due_today, upcoming = [], [], []

    for e in entries:
        for c in e.get("ctas", []):
            raw_due = (c.get("due_date") or "").strip()
            if not raw_due:
                continue
            try:
                due = date.fromisoformat(raw_due[:10])
            except Exception:
                continue

            days = (due - today).days
            title = c.get("title", "(no title)")
            account = e.get("account", "Unknown")
            owner = c.get("owner", "?")

            item = f"{account} — {title} ({owner}, due {raw_due})"

            if days < 0:
                overdue.append(item)
            elif days == 0:
                due_today.append(item)
            elif 0 < days <= 3:
                upcoming.append(item)

    return overdue, due_today, upcoming
# =============== App & Storage ======================
app = FastAPI(
    title="CS Agent — LLM Summaries + UI + CSV",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


# Routes that exist only for the scripted /demo (ui_prototype_2.html) and the
# legacy session-cookie login used by the demo identity. With DEMO_MODE off
# they return 404 as if they did not exist.
DEMO_ONLY_PATHS = frozenset({
    "/demo",
    "/demo/login",
    "/login",
    "/logout",
    "/app/session",
    "/prep/meridian",
    "/store/list",
    "/notes/summarize",
    "/notes/finalize",
    "/call/start",
    "/engagement",
    "/cta/reminders",
    "/assistant/today",
    "/proactive/northgate",
    "/northgate/alert",
    "/northgate/alert/confirm",
    "/priorities/midday",
    "/eod/summary",
    "/recap/meridian",
    "/recap/meridian/confirm",
})


@app.middleware("http")
async def gate_demo_only_routes(request: Request, call_next):
    if not demo_mode_enabled():
        _path = request.url.path.rstrip("/") or "/"
        if _path in DEMO_ONLY_PATHS:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
    return await call_next(request)


@app.middleware("http")
async def add_correlation_id(request: Request, call_next):
    _rid = request.headers.get("x-request-id", str(uuid.uuid4())[:8])
    _token_rid = request_id_var.set(_rid)
    try:
        response = await call_next(request)
        response.headers["x-request-id"] = _rid
        return response
    finally:
        request_id_var.reset(_token_rid)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)

    # HSTS with preload — submitted to hstspreload.org separately
    response.headers["Strict-Transport-Security"] = (
        "max-age=31536000; includeSubDomains; preload"
    )

    # Global hardening — applies to every response
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Permitted-Cross-Domain-Policies"] = "none"

    # Cache-Control: no-store on auth-sensitive paths only.
    # Prevents browsers and intermediaries from caching responses
    # containing personal or session data. Public paths
    # (/, /demo, /privacy, /terms, /.well-known/*, /status) are
    # unaffected so they can still benefit from edge caching.
    _auth_sensitive_prefixes = ("/app", "/chat", "/billing", "/auth", "/admin")
    if any(request.url.path.startswith(p) for p in _auth_sensitive_prefixes):
        response.headers["Cache-Control"] = "no-store"

    return response


@app.exception_handler(SubscriptionGatedException)
async def subscription_gated_handler(request: Request, exc: SubscriptionGatedException):
    return JSONResponse(
        status_code=402,
        content={"error": "subscription_required", "status": exc.status},
    )

from fastapi.middleware.cors import CORSMiddleware

# CORS allowed origins — comma-separated in env var, with sensible defaults.
# Wildcard ('*') is rejected: combined with allow_credentials=True it lets any
# origin make credentialed requests, defeating the allowlist. Fall back to the
# hardcoded default if the env var contains '*' anywhere.
_CORS_DEFAULT_ORIGINS = [
    PUBLIC_BASE_URL,
    "http://localhost:5173",
    "http://localhost:3000",
]
_cors_origins_raw = os.getenv("CORS_ALLOWED_ORIGINS", "").strip()
if _cors_origins_raw:
    _parsed = [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]
    if "*" in _parsed:
        logger.error(
            "[cors] CORS_ALLOWED_ORIGINS contains '*' — rejecting and falling "
            "back to default allowlist (wildcard + credentials is unsafe)"
        )
        CORS_ALLOWED_ORIGINS = _CORS_DEFAULT_ORIGINS
    else:
        CORS_ALLOWED_ORIGINS = _parsed
else:
    CORS_ALLOWED_ORIGINS = _CORS_DEFAULT_ORIGINS

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
logger.info("CORS origins: %s", CORS_ALLOWED_ORIGINS)

# ========== Scheduler Setup (AUTO Morning Digest) ==========
_scheduler = None                   # module-level; set by _start_scheduler()
_last_absence_check_at = None  # type: str | None  # updated after each successful absence detection run
_digest_in_flight: dict[str, float] = {}  # dedup_key -> timestamp (5-min TTL)


def _send_founder_alert(job_name: str, csm_id: str, error: Exception, tb_str: str, csm_email: str = "") -> None:
    """Email the founder when a scheduled job fails. Fails silently if send fails."""
    FOUNDER_EMAIL = os.environ.get("FOUNDER_EMAIL") or SUPPORT_EMAIL
    try:
        from product.digest.delivery import send_system_email as _sse
        _now  = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _body = (
            f"Job:       {job_name}\n"
            f"CSM:       {csm_id}\n"
            f"CSM Email: {csm_email or 'unknown'}\n"
            f"Time:      {_now}\n"
            f"Error:     {error}\n\n"
            f"Traceback:\n{tb_str}"
        )
        _delivered = _sse(
            to=FOUNDER_EMAIL,
            subject=f"[CS Agent] Pipeline failure: {job_name}",
            body_html=f"<pre>{_body}</pre>",
            body_plain=_body,
        )
        if _delivered:
            logger.info("Founder alert sent for %s", job_name)
        else:
            logger.info("Founder alert skipped for %s (recipient filtered)", job_name)
    except Exception as _e:
        logger.warning("Could not send founder alert: %s", _e)


def get_csm_timezone(csm_id: str):
    """Return a ZoneInfo for the CSM's stored timezone.

    Falls back to America/Chicago if null, missing, or invalid.
    Uses zoneinfo from stdlib (Python 3.9+); no pytz.
    """
    from zoneinfo import ZoneInfo
    fallback = ZoneInfo("America/Chicago")
    try:
        from product.database import SessionLocal, Customer
        if not SessionLocal:
            return fallback
        session = SessionLocal()
        try:
            cust = session.query(Customer).filter_by(clerk_user_id=csm_id).first()
            if cust and cust.timezone:
                try:
                    return ZoneInfo(cust.timezone)
                except (KeyError, Exception):
                    logger.debug("Invalid timezone %r for %s, using fallback", cust.timezone, csm_id)
                    return fallback
            return fallback
        finally:
            session.close()
    except Exception:
        return fallback


# ── Stage 3: deterministic digest generator wiring ───────────────────────────
#
# The new generator (product/digest/generator.py) replaces build_morning_digest
# under DIGEST_GENERATOR_ENABLED=true for non-sarah_c users on BOTH the login
# digest path and the /chat path (when a clear digest ask is detected).
#
# sarah_c is pinned to the legacy demo path regardless of the flag - she is
# the regression guard, not the canary.

_DIGEST_TRIGGER_EXACT_PHRASES = {
    "/digest", "digest",
    "my digest", "my digest please", "my morning digest",
    "morning digest", "today's digest", "todays digest",
    "show me my digest", "show me my day", "show my day",
    "what's my digest", "whats my digest",
    "what's my day", "whats my day",
    "what's my day look like", "whats my day look like",
    "give me my digest", "give me today's digest", "give me todays digest",
    "what do i have today",
}


def _is_digest_request(msg: str) -> bool:
    """Conservative exact-phrase match for digest asks.

    Lower-cases, strips whitespace, strips trailing punctuation only. No
    substring match, no keyword scan. A clear ask gets intercepted; every
    other message flows to normal tool-use chat unchanged.
    """
    if not msg:
        return False
    norm = msg.strip().lower().rstrip(".!?")
    return norm in _DIGEST_TRIGGER_EXACT_PHRASES


def _build_new_digest_for_user(
    csm_id: str,
    csm_name: str,
    csm_tz,
    crm_type: str | None,
    account_names: list,
    crm_signals: list,
    calendar_signals: dict,
    classified: dict,
    crm_fetch_status: str | None,
    gmail_status: str,
    calendar_status: str,
    drive_status: str,
    skip_urgency_refresh: bool = False,
) -> dict:
    """Shared helper used by the login digest path (under flag) and the
    chat-intercept path (Stage 3 wiring).

    Runs score_account_urgency (wrapped to degrade urgency to {} on failure),
    applies the defensive owner-scope filter (closes the gate-question
    residual leak documented in Stage 3A), fetches the week-ahead via
    extract_calendar_range, derives overdue_items from existing data,
    builds section_status, and returns a build_morning_digest-shaped dict
    so downstream consumers (save_digest_to_disk, DigestLog, notification
    email, /app/today-digest read) work unchanged.

    Stage 4 single-refresh hoist: the caller controls whether
    score_account_urgency does its own force-refresh via skip_urgency_refresh.
    The login runner refreshes the context cache once up front (so the
    crm_signals gathering and this urgency call read the same fresh snapshot)
    and passes skip_urgency_refresh=True. The chat intercept does NOT refresh
    up front, so it leaves skip_urgency_refresh at its False default and the
    helper inside score_account_urgency runs the refresh as before. Either
    way, exactly one refresh per digest assembly.
    """
    from datetime import date as _date_cls, timedelta as _timedelta_cls
    from product.signals.calendar import extract_calendar_range
    from product.signals.detector import score_account_urgency, tier_priorities
    from product.digest.generator import build_digest_payload

    # 1. score_account_urgency, wrapped fail-closed -> degrade to {}
    try:
        urgency = score_account_urgency(csm_id, refresh=not skip_urgency_refresh)
    except Exception as _su_exc:
        logger.warning(
            "[digest][%s] score_account_urgency failed, degrading to {}: %s",
            csm_id, _su_exc,
        )
        urgency = {}

    # 2. Defensive owner-scope filter (Stage 3A gate-question requirement).
    # A stale snapshot can contain a now-unowned account; the filter ensures
    # the rendered digest can never name an account outside account_names.
    _owned_set = set(account_names or [])
    urgency = {k: v for k, v in urgency.items() if k in _owned_set}

    # 3. Week ahead via extract_calendar_range (today through today+6 in
    # the CSM's local timezone). Failure degrades to an empty week_ahead;
    # the section_status reflects this.
    _week_status_local = "ok"
    week_ahead: dict = {}
    try:
        _today_local = datetime.now(csm_tz).date() if csm_tz is not None else _date_cls.today()
        _week_end = _today_local + _timedelta_cls(days=6)
        week_ahead = extract_calendar_range(
            csm_id, list(account_names or []), _today_local, _week_end,
        )
        if "error" in week_ahead:
            _week_status_local = "failed"
        elif not week_ahead.get("total_customer_meetings", 0):
            _week_status_local = "empty"
    except Exception as _wa_exc:
        logger.warning(
            "[digest][%s] week_ahead fetch failed: %s", csm_id, _wa_exc,
        )
        week_ahead = {}
        _week_status_local = "failed"

    # Calendar status is the worse of (today fetch status, week-ahead status).
    # The two fetches use the same Google Calendar credentials; if either
    # fails, the calendar marker is appropriate.
    _calendar_final = calendar_status
    if _week_status_local == "failed":
        _calendar_final = "failed"

    # 4. todays_meetings from the already-classified output.
    todays_meetings = calendar_signals.get("customer_meetings", []) if calendar_signals else []

    # 5. overdue_items: union of (a) classified.overdue_ctas (store.json,
    # sarah_c-only in practice but harmless to include) and (b) the
    # crm.cta_overdue signals from the owner-scoped detect_crm_signals.
    overdue_items: list = []
    for _oc in (classified.get("overdue_ctas", []) if classified else []) or []:
        if _oc.get("account"):
            overdue_items.append({
                "account": _oc.get("account", ""),
                "title": _oc.get("title", ""),
                "days_overdue": _oc.get("days_overdue", 0),
            })
    from datetime import date as _date_cls2
    _today_iso = _date_cls2.today()
    for _s in (crm_signals or []):
        if _s.get("type") != "crm.cta_overdue":
            continue
        _acct = _s.get("account") or ""
        if not _acct:
            continue
        _due_raw = _s.get("due_date") or ""
        _days = 0
        if _due_raw:
            try:
                _days = max(0, (_today_iso - _date_cls2.fromisoformat(_due_raw[:10])).days)
            except (ValueError, TypeError):
                _days = 0
        overdue_items.append({
            "account": _acct,
            "title": _s.get("title", ""),
            "days_overdue": _days,
        })

    # 6. Tier and render.
    section_status = {
        "crm": crm_fetch_status if crm_fetch_status in ("ok", "empty", "failed") else "ok",
        "gmail": gmail_status,
        "calendar": _calendar_final,
        "drive": drive_status,
    }
    tiers = tier_priorities(
        urgency=urgency,
        todays_meetings=todays_meetings,
        week_ahead=week_ahead,
        overdue_items=overdue_items,
    )
    logger.info(
        "[digest][%s] new generator: owned=%d urgency=%d tier_A=%d tier_B=%d tier_C_days=%d status=%s",
        csm_id, len(_owned_set), len(urgency),
        len(tiers.get("A", [])), len(tiers.get("B", [])), len(tiers.get("C", [])),
        section_status,
    )
    return build_digest_payload(
        csm_name=csm_name,
        csm_tz=csm_tz,
        crm_type=crm_type,
        owned_accounts=list(account_names or []),
        urgency=urgency,
        tiers=tiers,
        section_status=section_status,
        todays_meetings=todays_meetings,
    )


async def _chat_digest_intercept(csm_id: str, csm_name: str) -> dict:
    """Serve the deterministic morning digest to chat verbatim.

    Called only when DIGEST_GENERATOR_ENABLED is on, the csm_id is NOT
    sarah_c, and _is_digest_request matched the user's message. Builds
    the same digest the login surface would, returns its markdown in
    the {"reply": ..., "drafts": []} shape both legacy and tool-use
    chat handlers return.

    Lightweight by design: reuses the same shared helper as the login
    path, with chat-appropriate empty values for Gmail/Drive (the
    score_account_urgency drivers cover email-absence via signal_cache).
    """
    try:
        csm_id = _validate_csm_id(csm_id)
    except Exception:
        return {"reply": "Could not load your digest right now. Please try again.", "drafts": []}

    _first = (csm_name.split()[0].rstrip(".") if csm_name else "there")
    _csm_tz = get_csm_timezone(csm_id)

    # Resolve crm_type. For Clerk users, the cached snapshot carries it.
    # For sarah_c (excluded above) or any legacy registry user, fall back
    # to org_config. Both fail gracefully to "".
    _crm_type = ""
    try:
        from product.context_cache import get_cached_context as _gc
        _cached = _gc(csm_id)
        if _cached:
            _crm_type = (_cached.get("data", {}) or {}).get("crm_type", "") or ""
    except Exception:
        pass
    if not _crm_type:
        try:
            from product.org_config import get_crm_config as _gcc
            _crm_type = _gcc().get("crm_type", "") or ""
        except Exception:
            _crm_type = ""

    # Owned accounts via the same loader the chat tools use.
    try:
        from product.chat_tools.registry import _load_account_names_for_csm
        account_names = await asyncio.to_thread(_load_account_names_for_csm, csm_id)
    except Exception as _al_exc:
        logger.warning("[chat-digest] account load failed for %s: %s", csm_id, _al_exc)
        account_names = []

    # Fetch today's calendar signals + delegate the rest to the shared
    # helper. crm_signals and classified pass empty because the chat
    # intercept does not run detect_crm_signals or detect_and_classify;
    # the shared helper degrades cleanly with empty inputs.
    _calendar_status_today = "ok"
    try:
        from product.signals.calendar import extract_calendar_signals
        calendar_signals = await asyncio.to_thread(
            extract_calendar_signals, csm_id, list(account_names or []),
        )
        _calendar_status_today = (
            "ok" if calendar_signals.get("total_customer_meetings", 0) > 0 else "empty"
        )
    except Exception as _ce:
        logger.warning("[chat-digest] calendar fetch failed for %s: %s", csm_id, _ce)
        calendar_signals = {
            "date": "", "customer_meetings": [], "internal_meetings": [],
            "total_customer_meetings": 0, "total_meetings": 0, "available_gaps": [],
        }
        _calendar_status_today = "failed"

    # CRM status from cache freshness.
    _crm_status = "ok"
    try:
        from product.context_cache import get_cached_context as _gc2
        _c2 = _gc2(csm_id)
        if not _c2:
            _crm_status = "failed"
        elif not ((_c2.get("data", {}) or {}).get("accounts") or []):
            _crm_status = "empty"
    except Exception:
        _crm_status = "failed"

    digest = await asyncio.to_thread(
        _build_new_digest_for_user,
        csm_id,
        _first,
        _csm_tz,
        _crm_type,
        list(account_names or []),
        [],                  # crm_signals: chat intercept does not refetch
        calendar_signals,
        {},                  # classified: chat intercept does not refetch
        _crm_status,
        "empty",             # gmail_status: not fetched on chat path
        _calendar_status_today,
        "empty",             # drive_status: not fetched on chat path
    )
    return {"reply": digest.get("digest_text", ""), "drafts": []}


def _run_digest_for_user(csm_id: str) -> None:
    """Generate morning digest for a single user (synchronous).

    Called from arq worker (via asyncio.to_thread) and from the scheduler.
    Raises on failure so the caller can handle retries.
    """
    csm_id = _validate_csm_id(csm_id)
    import json, traceback
    from pathlib import Path
    from product.signals.gmail import extract_gmail_signals
    from product.signals.calendar import extract_calendar_signals
    from product.signals.detector import detect_and_classify, detect_crm_signals
    from product.digest.builder import build_morning_digest
    from product.digest.delivery import save_digest_to_disk, send_notification_email
    from product.connectors.registry import get_connector
    from product.org_config import get_csm_config, get_crm_config, get_pilot_mode

    _pilot_mode = get_pilot_mode()

    try:
        _crm_type = get_crm_config().get("crm_type", "")
    except Exception:
        _crm_type = ""

    # store.json is a demo artifact — only load for sarah_c
    event_history = []
    store_data = {"summaries": []}
    if is_demo_csm_id(csm_id):
        _store_path = Path("store.json")
        if _store_path.exists():
            try:
                store_data = json.loads(_store_path.read_text())
            except Exception:
                pass

    # PLG / Clerk users load profile from Postgres
    if csm_id.startswith("user_"):
        try:
            from product.database import SessionLocal, Customer
            _db = SessionLocal() if SessionLocal else None
            if _db is None:
                raise RuntimeError("DB not configured")
            try:
                _cust = _db.query(Customer).filter_by(clerk_user_id=csm_id).first()
            finally:
                _db.close()
            if _cust is None:
                raise KeyError(f"No customer row for {csm_id}")
            _display_name = _cust.name or ""
            if not _display_name or _display_name.startswith("user_"):
                _display_name = "there"  # "Good morning, there" is better than a raw ID
            csm_cfg = {
                "name":          _display_name,
                "email":         _cust.email or "",
                "accounts":      _cust.accounts or "auto",
                "timezone":      "America/Chicago",
                "notifications": {"email": True},
            }
        except Exception as _plg_e:
            raise KeyError(f"PLG user {csm_id} not found in DB: {_plg_e}") from _plg_e
    else:
        csm_cfg = get_csm_config(csm_id)

    csm_name = csm_cfg.get("name", csm_id)
    csm_email = csm_cfg.get("email", "")
    _raw_accounts = csm_cfg.get("accounts", "auto")

    # Build account list and contacts via CRM; fall back to config list.
    # _crm_fetch_status tracks whether the live CRM call succeeded so the
    # digest LLM can render a data-issue instead of inventing account names.
    # "ok" | "empty" | "failed" | None — see build_morning_digest docstring.
    account_contacts: dict = {}
    crm_signals: list = []
    _crm_fetch_status: str | None = None

    # Stage 4: single-refresh hoist. On the new-digest path, refresh the context
    # cache ONCE here so the CRM-signal gathering below reads the fresh snapshot
    # and score_account_urgency can skip its own refresh (threaded via the
    # helper). Collapses the cold-start double CRM pull to one. Gated on the
    # same condition as the new-digest dispatch so the legacy/sarah_c path is
    # byte-identical. refresh_user_context never raises on CRM failure; the
    # only raising case is an asyncio loop collision, in which case
    # _upfront_refresh_ok stays False and score_account_urgency does its own
    # refresh (current behavior, no regression).
    _new_digest_path = (
        not is_demo_csm_id(csm_id)
        and os.environ.get("DIGEST_GENERATOR_ENABLED", "false").lower() == "true"
    )
    _upfront_refresh_ok = False
    if _new_digest_path:
        try:
            from product.context_cache import refresh_user_context as _refresh_ctx
            asyncio.run(_refresh_ctx(csm_id))
            _upfront_refresh_ok = True
            logger.info("digest[%s]: Stage 4 up-front context refresh complete", csm_id)
        except Exception as _rf_exc:
            logger.warning(
                "digest[%s]: up-front refresh failed (%s); falling back to "
                "score_account_urgency self-refresh", csm_id, _rf_exc,
            )

    if _crm_type:
        # Try context cache first (no MCP calls)
        _used_cache_for_digest = False
        try:
            from product.context_cache import get_cached_context
            _cached = get_cached_context(csm_id)
            if _cached and _cached.get("refresh_status") == "complete":
                _cache_data = _cached.get("data", {})
                _cached_accounts = _cache_data.get("accounts", [])
                _cached_contacts = _cache_data.get("contacts", [])
                _cached_deals = _cache_data.get("deals", [])

                if _cached_accounts:
                    _all_names = [a.get("name", "") for a in _cached_accounts if a.get("name")]
                    account_names = (
                        [n for n in _all_names if n in _raw_accounts]
                        if isinstance(_raw_accounts, list)
                        else _all_names
                    )

                    # Build account_contacts from cached contacts
                    for _name in account_names:
                        _emails = [
                            c.get("email") for c in _cached_contacts
                            if c.get("account_name") == _name and c.get("email")
                        ]
                        account_contacts[_name] = _emails

                    # Build CRM signals from cached data (no live MCP calls)
                    from datetime import date as _date_cls
                    _today_cache = _date_cls.today()

                    # Renewal proximity signals from deals
                    for d in _cached_deals:
                        days = d.get("days_until_close")
                        if days is not None and days <= 90:
                            crm_signals.append({
                                "type": "crm.renewal_proximity",
                                "account": d.get("account_name"),
                                "days_until_renewal": days,
                                "renewal_date": d.get("close_date"),
                                "contract_value": d.get("amount"),
                                "urgency": "high" if days <= 30 else "medium",
                            })

                    # CTA/ticket signals from cached tickets — only classify as
                    # overdue if there's an actual past due date, otherwise it's
                    # a high-priority item (noteworthy but not risk-elevating).
                    _cached_tickets = _cache_data.get("tickets", [])
                    _today_cache = date.today()
                    for t in _cached_tickets:
                        _pri = (t.get("priority") or "").upper()
                        if _pri in ("HIGH", "URGENT"):
                            _t_due = (t.get("due_date") or t.get("dueDate") or "")[:10]
                            _t_is_overdue = False
                            if _t_due:
                                try:
                                    _t_is_overdue = date.fromisoformat(_t_due) < _today_cache
                                except (ValueError, TypeError):
                                    pass
                            if _t_is_overdue:
                                crm_signals.append({
                                    "type": "crm.cta_overdue",
                                    "account": t.get("account_name"),
                                    "title": t.get("subject", ""),
                                    "due_date": _t_due,
                                    "owner": "",
                                    "urgency": "high",
                                })
                            else:
                                crm_signals.append({
                                    "type": "crm.high_priority_item",
                                    "account": t.get("account_name"),
                                    "title": t.get("subject", ""),
                                    "due_date": _t_due,
                                    "owner": "",
                                    "urgency": "medium",
                                })

                    _pop_count = sum(1 for v in account_contacts.values() if v)
                    logger.info("digest[%s]: loaded from cache — %s accounts, %s with contacts, %s CRM signals", csm_id, len(account_names), _pop_count, len(crm_signals))
                    _used_cache_for_digest = True
        except Exception as _cache_err:
            logger.warning("digest[%s]: cache read failed, falling back to live: %s", csm_id, _cache_err)

        # Fallback: live MCP (only if cache missed)
        if not _used_cache_for_digest:
            # Try org_config connector first (sarah_c path)
            connector = None
            try:
                connector = get_connector(csm_id, {"crm": _crm_type})
            except Exception:
                pass

            # Fallback: create HubSpotCRMConnector directly (Clerk user path)
            if connector is None and _crm_type == "hubspot":
                try:
                    from product.connectors.hubspot_crm import HubSpotCRMConnector as _DigestHSConn
                    connector = _DigestHSConn(csm_id)
                    if connector._access_token:
                        logger.info("digest[%s]: created HubSpot connector from Postgres token", csm_id)
                    else:
                        connector = None
                except Exception as _tk_err:
                    logger.warning("digest[%s]: direct HubSpot connector failed: %s", csm_id, _tk_err)
                    connector = None

            if connector:
                try:
                    _crm_accts = connector.get_accounts()
                    # Translate connector status into the digest marker.
                    # Only HubSpot threads status today; absence of the
                    # accessor (e.g. Gainsight) leaves _crm_fetch_status as
                    # None and behaves like before.
                    if hasattr(connector, "get_accounts_status"):
                        from product.connectors.source_status import SourceStatus as _DigestSS
                        _conn_status = connector.get_accounts_status()
                        if _conn_status == _DigestSS.FAILED:
                            _crm_fetch_status = "failed"
                            logger.warning(
                                "digest[%s]: CRM accounts fetch FAILED — digest will note CRM unavailability",
                                csm_id,
                            )
                        elif _conn_status == _DigestSS.EMPTY:
                            _crm_fetch_status = "empty"
                            logger.info(
                                "digest[%s]: CRM accounts fetch returned EMPTY (user owns nothing)", csm_id,
                            )
                        else:
                            _crm_fetch_status = "ok"
                    _all_names = [a.get("name", "") for a in _crm_accts if a.get("name")]
                    account_names = (
                        [n for n in _all_names if n in _raw_accounts]
                        if isinstance(_raw_accounts, list)
                        else _all_names
                    )
                    for _name in account_names[:20]:
                        _contacts = connector.get_contacts(_name)
                        account_contacts[_name] = [c["email"] for c in _contacts if c.get("email")]
                    # Scope CRM signals to the owned-account set so a portfolio-wide
                    # renewals/CTAs query cannot leak another CSM's accounts into
                    # this user's digest. account_names is the scoped owned set on
                    # every reachable subpath: OK populates it from get_accounts(),
                    # and the EMPTY/FAILED paths below assign it from _raw_accounts
                    # (an empty list for "auto" / Clerk users), which becomes an
                    # empty allowed set here and yields zero CRM signals. Fail-closed
                    # by design; do not loosen.
                    crm_signals = detect_crm_signals(
                        connector,
                        allowed_accounts=set(account_names),
                    )
                    logger.info("digest[%s]: %s CRM signals loaded (live fallback, owned=%s)", csm_id, len(crm_signals), len(account_names))
                except Exception as _e:
                    logger.warning("digest[%s]: CRM connector failed: %s", csm_id, _e)
                    _crm_fetch_status = "failed"
                    account_names = _raw_accounts if isinstance(_raw_accounts, list) else []
                    account_contacts = {n: [] for n in account_names}
            else:
                logger.info("digest[%s]: no CRM connector available", csm_id)
                _crm_fetch_status = "failed"
                account_names = _raw_accounts if isinstance(_raw_accounts, list) else []
                account_contacts = {n: [] for n in account_names}
    else:
        account_names = _raw_accounts if isinstance(_raw_accounts, list) else [
            "Meridian Health", "Beacon Financial", "Sutton Retail",
            "Northgate Logistics", "Pinnacle Group",
        ]
        account_contacts = {n: [] for n in account_names}

    # Fallback for Clerk users with HubSpot: populate contacts from cache or live MCP
    _all_contacts_empty = all(len(v) == 0 for v in account_contacts.values())
    if _all_contacts_empty and account_contacts and csm_id.startswith("user_"):
        try:
            from product.connectors.hubspot_mcp import load_hubspot_token as _load_hs_tok
            if _load_hs_tok(csm_id):
                _populated = False

                # Tier 1: Context cache (fast, no MCP calls)
                try:
                    from product.context_cache import get_cached_context
                    _cached = get_cached_context(csm_id)
                    if _cached:
                        _cached_contacts = _cached.get("data", {}).get("contacts", [])
                        if _cached_contacts:
                            for _cc in _cached_contacts:
                                _acct = _cc.get("account_name", "")
                                _email = _cc.get("email", "")
                                if _acct in account_contacts and _email:
                                    account_contacts[_acct].append(_email)
                            _pop_count = sum(1 for v in account_contacts.values() if v)
                            logger.info("digest[%s]: populated contacts from cache — %s accounts with contacts", csm_id, _pop_count)
                            _populated = True
                except Exception as _ce:
                    logger.warning("digest[%s]: cache contact load failed: %s", csm_id, _ce)

                # Tier 2: Live HubSpot MCP (slow, capped at 10 accounts)
                if not _populated:
                    try:
                        import time as _time
                        from product.connectors.hubspot_crm import HubSpotCRMConnector
                        _hs = HubSpotCRMConnector(csm_id)
                        _hs_accts = _hs.get_accounts()
                        _hs_by_name = {a["name"]: a for a in _hs_accts if a.get("name") and a.get("hubspot_id") is not None}
                        _target = [n for n in account_contacts if n in _hs_by_name][:20]
                        for _i, _tname in enumerate(_target):
                            if _i > 0:
                                _time.sleep(0.5)
                            try:
                                _tc = _hs.get_contacts_by_company_id(int(_hs_by_name[_tname]["hubspot_id"]))
                                account_contacts[_tname] = [c["email"] for c in _tc if c.get("email")]
                            except Exception:
                                pass
                        _pop_count = sum(1 for v in account_contacts.values() if v)
                        logger.info("digest[%s]: populated contacts from live HubSpot — %s accounts with contacts", csm_id, _pop_count)
                    except Exception as _he:
                        logger.warning("digest[%s]: live HubSpot contact load failed: %s", csm_id, _he)
        except Exception as _fb_err:
            logger.warning("digest[%s]: contact fallback failed (non-fatal): %s", csm_id, _fb_err)

    # Gmail signals
    # _gmail_status threads OK/EMPTY/FAILED so the new digest generator can
    # render a "could not load Gmail" marker on failure (Stage 3 addition).
    # The build_morning_digest path ignores _gmail_status; behavior unchanged.
    logger.info("digest[%s]: extracting Gmail signals...", csm_id)
    _gmail_status = "ok"
    try:
        gmail_signals = extract_gmail_signals(csm_id, account_contacts)
        logger.info("digest[%s]: %s Gmail signals", csm_id, len(gmail_signals))
        _gmail_status = "ok" if gmail_signals else "empty"
    except Exception as _e:
        logger.warning("digest[%s]: Gmail extraction failed — continuing: %s", csm_id, _e)
        gmail_signals = []
        _gmail_status = "failed"

    # Persist gmail signals to the signal cache (best-effort; must not
    # break the digest if the cache write fails).
    try:
        from product.signal_cache import write_gmail_signals
        _cache_counts = write_gmail_signals(csm_id, gmail_signals)
        if _cache_counts:
            logger.info("digest[%s]: cached gmail signals — %s", csm_id, _cache_counts)
    except Exception as _e:
        logger.warning("digest[%s]: signal_cache write failed (non-fatal): %s", csm_id, _e)

    # Calendar signals
    # _calendar_status: see comment on _gmail_status above. Stage 3 wiring.
    logger.info("digest[%s]: extracting Calendar signals...", csm_id)
    _calendar_status = "ok"
    try:
        calendar_signals = extract_calendar_signals(csm_id, account_names)
        logger.info("digest[%s]: %s customer meetings", csm_id, calendar_signals.get('total_customer_meetings', 0))
        _calendar_status = (
            "ok" if calendar_signals.get("total_customer_meetings", 0) > 0 else "empty"
        )
    except Exception as _e:
        logger.warning("digest[%s]: Calendar extraction failed — continuing: %s", csm_id, _e)
        calendar_signals = {
            "date": "", "customer_meetings": [], "internal_meetings": [],
            "total_customer_meetings": 0, "total_meetings": 0, "available_gaps": [],
        }
        _calendar_status = "failed"

    # Drive transcripts (gated for sarah_c demo path)
    # _drive_status: see comment on _gmail_status above. sarah_c skips Drive
    # so the status stays at "empty" for her, which is also the harmless
    # default for the new generator (a marker only renders on "failed").
    drive_by_account: dict = {}
    _drive_status = "empty"
    if not is_demo_csm_id(csm_id):
        import time as _drive_time
        _drive_start = _drive_time.perf_counter()
        try:
            from product.signals import drive as _drive_mod
            from product.signals.drive import find_meet_transcripts
            from product.signals._title_match import group_transcripts_by_account
            # _DOCS_FETCH_SEMAPHORE binds to the first event loop it sees.
            # _run_digest_for_user is sync and uses asyncio.run() each call,
            # which creates a fresh loop. Rebind the semaphore so it binds
            # to the current call's loop rather than a stale one. Safe in a
            # single-worker uvicorn / arq context; revisit if we ever go
            # multi-worker per process.
            _drive_mod._DOCS_FETCH_SEMAPHORE = asyncio.Semaphore(5)
            _drive_raw = asyncio.run(find_meet_transcripts(csm_id, days=7))
            drive_by_account = group_transcripts_by_account(_drive_raw or [], account_names)
            _drive_dur_ms = int((_drive_time.perf_counter() - _drive_start) * 1000)
            logger.info(json.dumps({
                "event": "digest_drive_signals",
                "csm_id": csm_id,
                "drive_signal_count": len(_drive_raw or []),
                "matched_to_accounts": len(drive_by_account),
                "duration_ms": _drive_dur_ms,
            }))
            _drive_status = "ok" if drive_by_account else "empty"
        except Exception as _e:
            logger.warning("digest[%s]: Drive extraction failed, continuing: %s", csm_id, _e)
            drive_by_account = {}
            _drive_status = "failed"

    # Zoom unrecapped-call signals (gated on ZOOM_SIGNALS_ENABLED, separate
    # from the digest/slack flags). sarah_c is skipped inside the extractor.
    zoom_signals: list = []
    _zoom_status = "empty"
    if not is_demo_csm_id(csm_id):
        try:
            from product.signals.zoom import extract_zoom_signals
            zoom_signals = extract_zoom_signals(csm_id, account_names)
            logger.info(json.dumps({
                "event": "digest_zoom_signals",
                "csm_id": csm_id,
                "zoom_signal_count": len(zoom_signals),
            }))
            _zoom_status = "ok" if zoom_signals else "empty"
        except Exception as _e:
            logger.warning("digest[%s]: Zoom extraction failed, continuing: %s", csm_id, _e)
            zoom_signals = []
            _zoom_status = "failed"

    # Classify signals
    logger.info("digest[%s]: classifying signals...", csm_id)
    classified = detect_and_classify(
        gmail_signals,
        calendar_signals,
        event_history,
        store_data,
        crm_signals=crm_signals,
        drive_signals=drive_by_account,
        zoom_signals=zoom_signals,
    )

    # Build digest. Two paths:
    #   - DIGEST_GENERATOR_ENABLED=true and csm_id != sarah_c: use the new
    #     deterministic generator via the shared helper.
    #   - Otherwise: byte-identical legacy build_morning_digest. sarah_c is
    #     pinned to this path regardless of the flag (demo regression guard).
    logger.info("digest[%s]: building digest...", csm_id)
    first_name = csm_name.split()[0].rstrip(".")
    _csm_tz = get_csm_timezone(csm_id)

    # Reuse the same boolean the up-front refresh block was gated on, so the
    # two cannot drift. _upfront_refresh_ok is True only if the up-front
    # refresh succeeded; on its False fallback, score_account_urgency runs
    # its own refresh and behavior is identical to pre-Stage-4.
    _use_new_digest = _new_digest_path

    if _use_new_digest:
        digest = _build_new_digest_for_user(
            csm_id=csm_id,
            csm_name=first_name,
            csm_tz=_csm_tz,
            crm_type=_crm_type,
            account_names=account_names,
            crm_signals=crm_signals,
            calendar_signals=calendar_signals,
            classified=classified,
            crm_fetch_status=_crm_fetch_status,
            gmail_status=_gmail_status,
            calendar_status=_calendar_status,
            drive_status=_drive_status,
            skip_urgency_refresh=_upfront_refresh_ok,
        )
    else:
        digest = build_morning_digest(
            classified,
            first_name,
            crm_signals=crm_signals,
            csm_tz=_csm_tz,
            drive_signals=drive_by_account,
            crm_fetch_status=_crm_fetch_status,
        )
    logger.info("digest[%s]: digest built (%s chars)", csm_id, len(digest.get('digest_text', '')))

    # Append trailing notices (alert cap hit / expired connections) before the
    # digest is persisted, so both the disk and Postgres copies carry them and
    # every render — generator or legacy path — shows the same lines.
    try:
        _notices = _build_digest_notices(csm_id)
        if _notices:
            _base = (digest.get("digest_text", "") or "").rstrip()
            digest["digest_text"] = _base + "\n\n" + "\n".join(_notices)
    except Exception as _notice_err:
        logger.warning("digest[%s]: notice append failed (non-fatal): %s", csm_id, _notice_err)

    # Save to disk (backward compat for admin/trigger-digest and local dev)
    save_digest_to_disk(digest, csm_id)
    logger.info("digest[%s]: digest saved to disk", csm_id)

    # Save to Postgres (shared across web + worker containers)
    try:
        from product.database import SessionLocal as _DgSL, DigestLog as _DgLog
        if _DgSL:
            _dg_db = _DgSL()
            try:
                _dg_db.add(_DgLog(
                    id=str(uuid.uuid4()),
                    customer_id=csm_id,
                    date=digest.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d")),
                    status="success",
                    signal_count=len(classified.get("priority_accounts", [])),
                    gmail_signals=len(gmail_signals),
                    calendar_signals=calendar_signals.get("total_customer_meetings", 0),
                    hubspot_signals=len(crm_signals),
                    digest_text=digest.get("digest_text", ""),
                    delivered_at=datetime.now(timezone.utc),
                ))
                _dg_db.commit()
                logger.info("digest[%s]: saved to Postgres", csm_id)
            finally:
                _dg_db.close()
    except Exception as _db_err:
        logger.warning("digest[%s]: Postgres save failed (non-fatal): %s", csm_id, _db_err)

    # Clear Redis pending key so frontend stops showing "generating"
    try:
        import asyncio as _dg_aio
        from product.queue import _get_pool as _dg_get_pool
        async def _dg_clear():
            pool = await _dg_get_pool()
            if pool:
                _dg_date = digest.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
                await pool.delete(f"digest:pending:{csm_id}:{_dg_date}")
        _dg_loop = _dg_aio.new_event_loop()
        _dg_loop.run_until_complete(_dg_clear())
        _dg_loop.close()
    except Exception:
        pass  # Redis key will expire via TTL anyway

    # Send notification email if configured
    if _pilot_mode == "shadow":
        logger.info("Shadow mode — digest saved for %s, notification skipped", csm_id)
    elif csm_cfg.get("notifications", {}).get("email", True) and csm_email:
        send_notification_email(digest, csm_email, csm_id)
        logger.info("digest[%s]: notification email sent to %s", csm_id, csm_email)
    else:
        logger.info("digest[%s]: email notifications off or no address, skipping", csm_id)

    # Clear in-flight dedup flag
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _digest_in_flight.pop(f"{csm_id}:{today_str}", None)


def send_morning_digest_job(is_retry: bool = False, csm_ids: list | None = None):
    """
    Runs the full morning digest pipeline for all active CSMs.
    Each CSM is wrapped in its own try/except so one failure never blocks others.
    On first run, only the failed CSMs are retried at 8:15 AM.
    """
    import traceback
    from product.org_config import list_active_csms

    _label = "morning_digest_retry" if is_retry else "morning_digest"

    if csm_ids is None:
        try:
            csm_ids = list_active_csms()
        except Exception as _e:
            logger.warning("%s: could not load org_config: %s", _label, _e)
            csm_ids = [DEMO_CSM_ID] if demo_mode_enabled() else []

    total = len(csm_ids)
    failed_csms: list[str] = []

    for csm_id in csm_ids:
        try:
            _run_digest_for_user(csm_id)
        except Exception as e:
            _tb = traceback.format_exc()
            logger.warning("%s[%s] failed: %s", _label, csm_id, e)
            failed_csms.append(csm_id)
            _send_founder_alert(_label, csm_id, e, _tb)

    success_count = total - len(failed_csms)
    logger.info("Morning digest: %s/%s succeeded", success_count, total)

    # Retry only failed CSMs at 8:15 AM (first run only)
    if failed_csms and not is_retry and _scheduler:
        try:
            _today      = date.today()
            _retry_dt   = datetime(_today.year, _today.month, _today.day, 8, 15, tzinfo=timezone.utc)
            _retry_ids  = list(failed_csms)
            _scheduler.add_job(
                lambda: send_morning_digest_job(is_retry=True, csm_ids=_retry_ids),
                "date",
                run_date=_retry_dt,
                id="digest_retry",
                replace_existing=True,
            )
            logger.info("Digest retry scheduled for 8:15 AM for: %s", _retry_ids)
        except Exception as _se:
            logger.warning("Could not schedule retry: %s", _se)

def _check_postcall_nudges(
    csm_id: str,
    csm_email: str,
    account_names: list,
    account_profiles: dict,
    pilot_mode: str,
) -> int:
    """
    Scan today's calendar for customer meetings that ended in the last 4 hours
    and send a post-call nudge alert for each one not already processed.

    Dedup state is persisted to product/data/postcall_nudges/{csm_id}.json so
    the same meeting never triggers a second nudge across job runs.

    Returns the number of nudges saved/sent.
    """
    csm_id = _validate_csm_id(csm_id)
    import base64 as _b64
    from datetime import timedelta
    from pathlib import Path

    try:
        from product.signals.calendar import (
            _load_credentials as _cal_creds,
            _parse_event_time,
            _classify_event,
            _match_account,
            _extract_attendee_names,
        )
        from googleapiclient.discovery import build as _gcal_build
    except Exception as _imp_e:
        logger.warning("postcall_nudge[%s]: import failed: %s", csm_id, _imp_e)
        return 0

    # ── Load dedup state ──────────────────────────────────────────────────────
    nudge_state_path = _safe_data_file("product/data/postcall_nudges", csm_id)
    nudge_state_path.parent.mkdir(parents=True, exist_ok=True)
    nudged_ids: set = set()
    if nudge_state_path.exists():
        try:
            nudged_ids = set(
                json.loads(nudge_state_path.read_text(encoding="utf-8"))
                .get("nudged_event_ids", [])
            )
        except Exception:
            pass

    # ── Fetch today's raw calendar events ─────────────────────────────────────
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=4)
    today_d = now.date()
    try:
        _creds = _cal_creds(csm_id)
        _svc   = _gcal_build("calendar", "v3", credentials=_creds)
        _time_min = datetime(today_d.year, today_d.month, today_d.day, tzinfo=timezone.utc).isoformat()
        _time_max = (datetime(today_d.year, today_d.month, today_d.day, tzinfo=timezone.utc) + timedelta(days=1)).isoformat()
        _raw_events = _svc.events().list(
            calendarId="primary",
            timeMin=_time_min,
            timeMax=_time_max,
            singleEvents=True,
            orderBy="startTime",
        ).execute().get("items", [])
    except Exception as _cal_e:
        logger.warning("postcall_nudge[%s]: calendar fetch failed: %s", csm_id, _cal_e)
        return 0

    nudges_sent = 0
    new_nudged_ids = set(nudged_ids)

    for _ev in _raw_events:
        event_id = _ev.get("id", "")
        if not event_id or event_id in nudged_ids:
            continue

        if _classify_event(_ev, account_names) != "customer":
            continue

        end_dt = _parse_event_time(_ev.get("end", {}))
        if not end_dt or not (window_start <= end_dt <= now):
            continue

        account   = _match_account(_ev, account_names)
        attendees = _extract_attendee_names(_ev)
        contact   = attendees[0] if attendees else "your contact"

        # Human-readable time ago
        minutes_ago = int((now - end_dt).total_seconds() / 60)
        if minutes_ago < 60:
            time_ago = f"{minutes_ago} minute{'s' if minutes_ago != 1 else ''} ago"
        else:
            hours_ago = minutes_ago // 60
            time_ago = f"{hours_ago} hour{'s' if hours_ago != 1 else ''} ago"

        # Account context snippet from profiles
        _profile = account_profiles.get(account, {})
        _renewal = _profile.get("renewal_days")
        _silent  = _profile.get("days_silent")
        _ctx_parts: list = []
        if _renewal and isinstance(_renewal, (int, float)) and 0 < _renewal < 365:
            _ctx_parts.append(f"renewal in {int(_renewal)} days")
        if _silent and isinstance(_silent, (int, float)) and _silent > 5:
            _ctx_parts.append(f"{int(_silent)} days since last email contact")
        context_str = ", ".join(_ctx_parts) if _ctx_parts else "I have basic account info on file"

        nudge_text = (
            f"Your call with {contact} at {account} ended {time_ago}. "
            f"Here's what I know: {context_str}. "
            f"What happened on the call? Even a quick summary helps me keep {account} context current."
        )

        sent_at = now.isoformat().replace("+00:00", "Z")
        safe_ts = sent_at.replace(":", "-").replace("+", "-")
        alert_id = str(uuid.uuid4())

        # Save to disk for in-app polling
        try:
            _alert_dir = _safe_data_path("product/data/alerts", csm_id)
            _alert_dir.mkdir(parents=True, exist_ok=True)
            (_alert_dir / f"postcall_{safe_ts}.json").write_text(
                json.dumps({
                    "alert_id":   alert_id,
                    "timestamp":  sent_at,
                    "text":       nudge_text,
                    "draft":      "",
                    "alert_type": "postcall_nudge",
                    "event_id":   event_id,
                    "account":    account,
                }, indent=2),
                encoding="utf-8",
            )
        except Exception as _de:
            logger.warning("postcall_nudge[%s]: disk save failed: %s", csm_id, _de)

        # Send email to CSM (skip in shadow mode)
        if pilot_mode != "shadow" and csm_email:
            try:
                from product.digest.delivery import send_system_email as _sse
                _delivered = _sse(
                    to=csm_email,
                    subject=f"Post-call: {account}",
                    body_html=f"<p>{nudge_text}</p>",
                    body_plain=nudge_text,
                )
                if _delivered:
                    logger.info("postcall_nudge[%s]: nudge sent for %s", csm_id, account)
                else:
                    logger.info("postcall_nudge[%s]: nudge skipped for %s (recipient filtered)", csm_id, account)
            except Exception as _mail_e:
                logger.warning("postcall_nudge[%s]: email send failed: %s", csm_id, _mail_e)
        elif pilot_mode == "shadow":
            logger.info("postcall_nudge[%s]: shadow — nudge saved for %s, email skipped", csm_id, account)

        new_nudged_ids.add(event_id)
        nudges_sent += 1

    # Persist updated dedup state
    if new_nudged_ids != nudged_ids:
        try:
            nudge_state_path.write_text(
                json.dumps({"nudged_event_ids": list(new_nudged_ids)}, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    return nudges_sent


def _check_crm_connected(csm_id: str, crm_type: str) -> bool:
    """Return True if the CSM has a Connection row for their configured CRM
    provider with a usable token.

    Used by F1 to distinguish "never connected CRM" from "connected CRM but
    get_accounts returned empty list" for telemetry. The crm_type argument
    is the CSM's configured CRM provider — do NOT hardcode "hubspot",
    since Gainsight (and future Salesforce / Planhat) users need accurate
    telemetry too.
    """
    from product.connection_service import get_connection_state
    state = get_connection_state(csm_id, crm_type)
    return state.has_token


# WS0.5 — per-CSM daily proactive-alert cap. Sized for a friendly's portfolio
# (~25-30 accounts): a bad day can otherwise produce ~50+ alerts across the two
# alert paths (F1 absence + Sprint 6 urgency). This ceiling is a SHARED budget
# across both paths — both stamp alert_date=today, so a single COUNT covers
# them. Shadow mode writes alerts straight to disk and is knowingly out of scope.
MAX_PROACTIVE_ALERTS_PER_DAY = 8


def _proactive_alert_cap_reached(csm_id: str) -> bool:
    """True if csm_id already has >= MAX_PROACTIVE_ALERTS_PER_DAY alerts today.

    Shared budget across both live alert paths — F1 absence (Path A, via
    send_proactive_alert) and Sprint 6 urgency (Path B, via
    _write_urgency_alerts). Both stamp alert_date = today UTC, so one COUNT
    over (csm_id, alert_date=today) covers both. Postcall alerts leave
    alert_date NULL and are a separate channel — excluded by the filter.

    Fail-OPEN: on any DB error (or no DB) returns False so a transient hiccup
    can never silence every alert for the day.
    """
    from product.database import SessionLocal, ProactiveAlert
    if SessionLocal is None:
        return False
    _s = SessionLocal()
    try:
        today_utc = datetime.now(timezone.utc).date()
        count = (
            _s.query(ProactiveAlert)
            .filter(ProactiveAlert.csm_id == csm_id)
            .filter(ProactiveAlert.alert_date == today_utc)
            .count()
        )
        return count >= MAX_PROACTIVE_ALERTS_PER_DAY
    except Exception as _exc:
        logger.warning("alert cap count failed for %s (fail-open): %s", csm_id, _exc)
        return False
    finally:
        _s.close()


# Digest-relevant providers whose expiry the CSM should see surfaced in the
# briefing. Scoped to the core digest inputs (Google covers Gmail + Calendar;
# HubSpot / Gainsight are the CRM). Slack/Zoom are excluded — they are not core
# digest inputs and the probe already covers reconnect via Settings.
_DIGEST_PROVIDER_LABELS = {
    "google":    "Google",
    "hubspot":   "HubSpot",
    "gainsight": "Gainsight",
}


def _expired_digest_providers(csm_id: str) -> list[str]:
    """Labels of digest-relevant providers whose Connection.status == 'expired'.

    Reads the already-persisted Connection.status (written by the hourly probe
    via connection_writer) — does NOT re-detect at digest time. Stage 1
    confirmed the probe is the only reliable detector (Slack has no refresh;
    Zoom swallows refresh failure on the data path), and its verdict already
    lives in Connection.status. Never raises.
    """
    from product.connection_service import verify_connection_live
    labels = []
    for provider, label in _DIGEST_PROVIDER_LABELS.items():
        try:
            if verify_connection_live(csm_id, provider).get("status") == "expired":
                labels.append(label)
        except Exception as _exc:
            logger.warning("digest connection check failed for %s/%s: %s", csm_id, provider, _exc)
    return labels


def _build_digest_notices(csm_id: str) -> list[str]:
    """Assemble trailing digest notice lines (house convention: emit only in
    the non-default case). Covers (1) the daily proactive-alert cap and (2)
    expired digest-relevant connections. Returns [] when everything is nominal.
    """
    notices: list[str] = []
    if _proactive_alert_cap_reached(csm_id):
        notices.append(
            "A few more accounts need a look than I've flagged here — "
            "open chat for your full at-risk list."
        )
    for label in _expired_digest_providers(csm_id):
        notices.append(
            f"Heads up — your {label} connection needs reconnecting. "
            f"Reconnect {label} in Settings so I don't miss anything."
        )
    return notices


def _write_urgency_alerts(csm_id: str, urgency_by_account: dict[str, dict]) -> int:
    """Sprint 6 H.1 — write per-account urgency alerts to ProactiveAlert.

    For each account whose risk != "on_track", insert one row with
    signal_id = f"urgency:{account}:{risk}" and alert_date = today UTC.
    The partial UNIQUE index on (csm_id, account, signal_id, alert_date)
    makes repeated runs within the same day idempotent. An account
    whose risk band changes within a day (watch → at_risk) produces a
    second row because signal_id differs.

    These rows live alongside existing F1 absence alerts (Decision 5,
    Option B) — they do not replace them. Email side-effect is
    deliberately skipped for Sprint 6 starting point; the alerts UI
    reads from Postgres and will surface them. Email amplification is
    a Sprint 7+ decision after we see how F1 + urgency interact.

    Returns the number of rows successfully inserted (duplicates
    suppressed by the dedup index don't count). Never raises.
    """
    from product.database import SessionLocal, ProactiveAlert
    from sqlalchemy.exc import IntegrityError

    if not urgency_by_account or SessionLocal is None:
        return 0

    today_utc = datetime.now(timezone.utc).date()
    written = 0

    # Order WITHIN this path by its own score (0-100) descending so the daily
    # cap, if hit, drops the least-urgent accounts rather than arbitrary ones.
    # No cross-path ranking — Path A's 0-1 score is on a different scale
    # (Stage 1: the two urgency scores are deliberately unreconciled).
    _ordered = sorted(
        urgency_by_account.items(),
        key=lambda kv: kv[1].get("score", 0),
        reverse=True,
    )

    for account, info in _ordered:
        risk = info.get("risk", "on_track")
        if risk == "on_track":
            continue
        # Shared daily cap across Path A + Path B. Once reached, stop writing
        # (remaining accounts are lower-score after the sort above).
        if _proactive_alert_cap_reached(csm_id):
            logger.info(
                "event=alert_cap_reached csm_id=%s cap=%d skipped_path=urgency",
                csm_id, MAX_PROACTIVE_ALERTS_PER_DAY,
            )
            break
        drivers     = info.get("drivers", []) or []
        risk_display = risk.replace("_", " ")
        top_drivers  = ", ".join(drivers[:3])
        alert_text   = (
            f"{account} is {risk_display}: {top_drivers}"
            if top_drivers else
            f"{account} is {risk_display}"
        )
        signal_id = f"urgency:{account}:{risk}"

        sess = SessionLocal()
        try:
            sess.add(ProactiveAlert(
                id=str(uuid.uuid4()),
                csm_id=csm_id,
                account=account,
                alert_text=alert_text,
                draft_text="",
                urgency=risk,                  # Decision 2 (C): store risk, defer score+drivers
                signal_id=signal_id,
                alert_date=today_utc,
                created_at=datetime.now(timezone.utc),
            ))
            sess.commit()
            written += 1
        except IntegrityError:
            sess.rollback()
            logger.info(json.dumps({
                "event":     "urgency_duplicate_alert_suppressed",
                "csm_id":    csm_id,
                "account":   account,
                "signal_id": signal_id,
            }))
        except Exception as exc:
            sess.rollback()
            logger.warning(
                "absence_detection[%s]: urgency alert write failed for %s: %s",
                csm_id, account, exc,
            )
        finally:
            sess.close()

    return written


def _run_absence_detection(target_csm_id: str | None = None) -> dict:
    """Core absence detection logic for all active CSMs (synchronous).

    Fan-out by default: runs for every CSM returned by
    list_proactive_eligible_csms(). Pass `target_csm_id` to run for a
    single CSM (admin trigger path); the target must still be in the
    eligible pool — non-eligible targets short-circuit with zero work.

    Returns {"csms_processed": int, "urgency_alerts_written": int} so
    the admin trigger can surface counts. The scheduler / arq job
    ignores the return value (no behavior change).
    """
    import json, traceback
    from pathlib import Path
    from product.signals.gmail import extract_gmail_signals
    from product.signals.detector import detect_absence_signals, score_urgency, score_account_urgency
    from product.digest.delivery import send_proactive_alert
    from product.signal_cache import derive_gmail_signal_id
    from product.connectors.registry import get_connector
    from product.connection_service import get_connection_state
    from product.org_config import list_proactive_eligible_csms, get_csm_config, get_crm_config, get_pilot_mode
    from product.scopes import GMAIL_READONLY
    from sqlalchemy.exc import OperationalError

    _pilot_mode = get_pilot_mode()

    logger.info("Running absence detection cycle...")

    try:
        csm_ids = list_proactive_eligible_csms()
    except OperationalError as e:
        logger.error(
            "f1_db_unavailable",
            extra={
                "exception_type": type(e).__name__,
                "exception_message": str(e)[:500],
            },
        )
        raise

    if not csm_ids:
        logger.info("f1_no_eligible_csms")

    # Sprint 6 H.1 — admin trigger path: filter to single CSM. We keep
    # the eligibility gate (target must already be in the eligible pool)
    # rather than bypass it, so the admin endpoint exercises the same
    # invariants as the scheduled run.
    if target_csm_id is not None:
        if target_csm_id not in csm_ids:
            logger.info(json.dumps({
                "event":  "f1_target_not_eligible",
                "csm_id": target_csm_id,
            }))
            return {"csms_processed": 0, "urgency_alerts_written": 0}
        csm_ids = [target_csm_id]

    try:
        _crm_type = get_crm_config().get("crm_type", "")
    except Exception:
        _crm_type = ""

    total                   = len(csm_ids)
    success_cnt             = 0
    urgency_alerts_written  = 0

    for csm_id in csm_ids:
        csm_email = ""  # initialise before try so except block can always reference it

        # Phase 2 scope gate: skip CSMs who have not granted gmail.readonly
        # before any work runs. State check only — does NOT verify token
        # liveness (Sprint 6's verify_connection_live unifies those).
        state = get_connection_state(csm_id, "google")
        if GMAIL_READONLY not in state.granted_scopes:
            logger.info(json.dumps({
                "event": "f1_skipped",
                "reason": "no_gmail_scope",
                "csm_id": csm_id,
            }))
            continue

        try:
            if csm_id.startswith("user_"):
                try:
                    from product.database import SessionLocal, Customer
                    _db = SessionLocal() if SessionLocal else None
                    if _db is None:
                        raise RuntimeError("DB not configured")
                    try:
                        _cust = _db.query(Customer).filter_by(clerk_user_id=csm_id).first()
                    finally:
                        _db.close()
                    if _cust is None:
                        raise KeyError(f"No customer row for {csm_id}")
                    csm_cfg = {
                        "name":     _cust.name or csm_id,
                        "email":    _cust.email or "",
                        "accounts": _cust.accounts or "auto",
                    }
                except Exception as _plg_e:
                    raise KeyError(f"PLG user {csm_id} not found in DB: {_plg_e}") from _plg_e
            else:
                csm_cfg = get_csm_config(csm_id)

            csm_email = csm_cfg.get("email", "")
            _raw_accts = csm_cfg.get("accounts", "auto")

            # Build account contacts from CRM if available
            account_contacts: dict = {}
            connector = None
            if _crm_type:
                try:
                    connector  = get_connector(csm_id, {"crm": _crm_type})
                    _crm_accts = connector.get_accounts()
                    if not _crm_accts:
                        crm_connected = _check_crm_connected(csm_id, _crm_type)
                        logger.info(json.dumps({
                            "event": "f1_skipped",
                            "reason": "no_accounts",
                            "csm_id": csm_id,
                            "crm_connected": crm_connected,
                        }))
                        continue
                    _all_names = [a.get("name", "") for a in _crm_accts if a.get("name")]
                    _names = (
                        [n for n in _all_names if n in _raw_accts]
                        if isinstance(_raw_accts, list)
                        else _all_names
                    )
                    for _name in _names[:10]:
                        _ctcs = connector.get_contacts(_name)
                        account_contacts[_name] = [c["email"] for c in _ctcs if c.get("email")]
                except Exception as _e:
                    logger.warning("absence_detection[%s]: CRM contacts failed — continuing: %s", csm_id, _e)

            if not account_contacts:
                _names = _raw_accts if isinstance(_raw_accts, list) else []
                account_contacts = {n: [] for n in _names}

            renewals_by_account: dict[str, int] = {}
            if connector is not None:
                try:
                    _renewals = connector.get_renewals()
                    for _r in _renewals:
                        _name = _r.get("account_name")
                        _days = _r.get("days_until_renewal")
                        if _name and _days is not None:
                            renewals_by_account[_name] = _days
                except Exception as _re:
                    logger.warning(
                        "absence_detection[%s]: get_renewals failed; falling back to renewal_days=None for all accounts: %s",
                        csm_id, _re,
                    )

            overdue_by_account: dict[str, bool] = {}
            if connector is not None:
                try:
                    _ctas = connector.get_open_ctas()
                    for _c in _ctas:
                        if _c.get("status") == "overdue":
                            _name = _c.get("account_name", "")
                            if _name:
                                overdue_by_account[_name] = True
                except Exception as _ce:
                    logger.warning(
                        "absence_detection[%s]: get_open_ctas failed; falling back to cta_overdue=False for all accounts: %s",
                        csm_id, _ce,
                    )

            account_profiles = {
                acct: {
                    "contacts": emails,
                    "renewal_days": renewals_by_account.get(acct),
                    "avg_response_days": 3.0,
                }
                for acct, emails in account_contacts.items()
            }

            try:
                gmail_signals = extract_gmail_signals(csm_id, account_contacts)
            except Exception as _e:
                logger.warning("absence_detection[%s]: Gmail extraction failed: %s", csm_id, _e)
                continue

            try:
                from product.signal_cache import write_gmail_signals
                write_gmail_signals(csm_id, gmail_signals)
            except Exception as _e:
                logger.warning("absence_detection[%s]: signal_cache write failed: %s", csm_id, _e)

            absence_signals = detect_absence_signals(gmail_signals, [], account_profiles)

            # Score every signal first, then process highest-urgency first so
            # the shared daily cap (below) drops the least-urgent signals in
            # this path rather than arbitrary ones. Ordering is WITHIN this
            # path only — no cross-path ranking against Path B's 0-100 score.
            _scored_signals = []
            for signal in absence_signals:
                account      = signal.get("account", "")
                acct_context = {
                    "renewal_days":              signal.get("renewal_days"),
                    "days_silent":               signal.get("days_silent", 0),
                    "contact_avg_response_days": signal.get("contact_avg_response_days", 3.0),
                    "signal_count":              sum(1 for s in absence_signals if s.get("account") == account),
                    "cta_overdue":               overdue_by_account.get(account, False),
                }
                urgency = score_urgency(signal, acct_context)
                if urgency >= 0.65:
                    _scored_signals.append((urgency, account, signal))
            _scored_signals.sort(key=lambda t: t[0], reverse=True)

            for urgency, account, signal in _scored_signals:
                if _pilot_mode == "shadow":
                    # Shadow mode writes straight to disk — knowingly OUT OF
                    # SCOPE for the daily cap (not the public-launch path).
                    try:
                        _alert_dir = _safe_data_path("product/data/alerts", csm_id)
                        _alert_dir.mkdir(parents=True, exist_ok=True)
                        _sent_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                        _safe_ts = _sent_at.replace(":", "-").replace("+", "-")
                        (_alert_dir / f"{_safe_ts}.json").write_text(
                            json.dumps({
                                "alert_id":  str(uuid.uuid4()),
                                "timestamp": _sent_at,
                                "text":      f"{account} needs your attention.\n\n{signal.get('detail', '')}",
                                "draft":     "",
                            }, indent=2),
                            encoding="utf-8",
                        )
                    except Exception as _se:
                        logger.warning("absence_detection[%s]: shadow disk save failed: %s", csm_id, _se)
                    logger.info("Shadow mode — alert saved for %s, notification skipped", csm_id)
                else:
                    # Shared daily cap across Path A + Path B. Signals are
                    # sorted highest-urgency first, so once the cap is hit the
                    # remaining (lower-urgency) signals are the ones dropped.
                    if _proactive_alert_cap_reached(csm_id):
                        logger.info(
                            "event=alert_cap_reached csm_id=%s cap=%d skipped_path=absence",
                            csm_id, MAX_PROACTIVE_ALERTS_PER_DAY,
                        )
                        break
                    try:
                        send_proactive_alert(
                            signal=signal,
                            urgency_score=urgency,
                            csm_id=csm_id,
                            csm_email=csm_email,
                            suggested_draft="",
                            signal_id=derive_gmail_signal_id(signal),
                            send_email=False,
                        )
                        logger.info("absence_detection[%s]: in-app alert recorded for %s (urgency: %s)", csm_id, account, urgency)
                    except Exception as _e:
                        logger.warning("absence_detection[%s]: alert send failed for %s: %s", csm_id, account, _e)

            # Post-call nudge v1 call site removed — C.1 Commits 5+6's
            # product/postcall/scheduler.py runs every 5 min and is the
            # sole post-call nudge producer going forward. _check_postcall_nudges
            # (defined above) is dormant pending removal in a later sweep.

            # Sprint 6 H.1 — per-account urgency aggregator. Fires
            # alongside (not in place of) the F1 absence path above
            # (Decision 5, Option B). score_account_urgency
            # force-refreshes context_cache, so we pay one CRM round-trip
            # per CSM here. Failure is logged and zero alerts written —
            # never blocks the F1 success accounting for this CSM.
            try:
                _urgency_out = score_account_urgency(csm_id)
            except Exception as _ue:
                logger.warning(
                    "absence_detection[%s]: score_account_urgency failed: %s",
                    csm_id, _ue,
                )
                _urgency_out = {}

            _written = _write_urgency_alerts(csm_id, _urgency_out)
            urgency_alerts_written += _written
            if _written:
                logger.info(json.dumps({
                    "event":  "urgency_alerts_written",
                    "csm_id": csm_id,
                    "count":  _written,
                }))

            success_cnt += 1

        except Exception as e:
            _tb = traceback.format_exc()
            logger.warning("absence_detection[%s] failed: %s", csm_id, e)
            _send_founder_alert("absence_detection", csm_id, e, _tb)

    global _last_absence_check_at
    _last_absence_check_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    logger.info(
        "Absence detection: %s/%s succeeded, %d urgency alerts written",
        success_cnt, total, urgency_alerts_written,
    )
    return {
        "csms_processed":         success_cnt,
        "urgency_alerts_written": urgency_alerts_written,
    }

def _process_trial_emails():
    """Core trial engagement email logic (synchronous). Called from arq and scheduler."""
    import traceback
    if not billing_enabled():
        logger.info("trial_engagement: BILLING_ENABLED off, skipping")
        return
    logger.info("Running trial engagement email job...")

    try:
        from product.database import SessionLocal, Customer
        if not SessionLocal:
            logger.info("trial_engagement: DB not configured, skipping")
            return
        session = SessionLocal()
        try:
            customers = session.query(Customer).filter(
                Customer.email.isnot(None),
                Customer.created_at.isnot(None),
                # I.2: never send trial engagement email to a soft-deleted
                # (orphaned/abandoned) customer row.
                Customer.is_deleted.is_(False),
            ).all()
        finally:
            session.close()
    except Exception as _e:
        logger.warning("trial_engagement: DB query failed: %s", _e)
        return

    from product.engagement_emails import send_engagement_email

    # Generalized catch-up: walk every engagement day in ascending order,
    # filter by day <= _days, and let already_sent (inside send_engagement_email)
    # provide idempotency. This catches three failure modes the strict-equality
    # selector missed:
    #   1. Feature ships mid-trial (no executable path on Days 1/3/7).
    #   2. Cron fire missed due to scheduler restart, free-tier sleep, deploy gap.
    #   3. Drift from clock skew or unusual signup time.
    # Day 13 is excluded once _days >= 14 because its body says "ends tomorrow".
    ENGAGEMENT_DAYS: tuple[int, ...] = (1, 3, 7, 13)

    sent_count = 0
    for _c in customers:
        try:
            _csm_id     = _c.clerk_user_id or _c.id
            _email      = _c.email
            _first_name = (_c.name or "").split()[0] if _c.name else ""
            _created    = _c.created_at
            if _created.tzinfo is None:
                _created = _created.replace(tzinfo=timezone.utc)
            _days = (datetime.now(timezone.utc) - _created).days
            for _day in ENGAGEMENT_DAYS:
                if _day > _days:
                    continue
                if _day == 13 and _days >= 14:
                    continue
                result = send_engagement_email(
                    csm_id=_csm_id,
                    email=_email,
                    first_name=_first_name,
                    day=_day,
                )
                if result.get("status") == "sent":
                    sent_count += 1
        except Exception as _e:
            logger.warning("trial_engagement[%s] failed: %s", _c.email, _e)

    logger.info("Trial engagement job complete: %s email(s) sent", sent_count)


def _expire_trials():
    """Expire trials that have passed trial_ends_at. Runs daily via APScheduler.

    Wrapped in founder-alert guard because silent failure here means trials
    never expire → users stay in trial indefinitely → lost revenue.
    """
    import traceback
    from product.subscription import expire_trials
    if not billing_enabled():
        logger.info("trial_expiry: BILLING_ENABLED off, skipping")
        return
    try:
        expire_trials()
    except Exception as e:
        _send_founder_alert("trial_expiry", "ALL", e, traceback.format_exc())
        raise


def _purge_canceled_users():
    """Hard-purge users canceled past the 29-day retention window. Runs daily.

    Destructive, so wrapped in a founder-alert guard. A cap trip means an
    unexpected number of users became eligible at once and the sweep
    deliberately purged NOTHING; investigate before clearing. Per-user
    failures are also surfaced.
    """
    import traceback
    from product.database import purge_expired_cancellations
    try:
        result = purge_expired_cancellations()
        if result.get("status") == "cap_exceeded":
            _send_founder_alert(
                "purge_sweep_cap_exceeded",
                f"eligible={result.get('eligible_count')} cap={result.get('cap')}",
                Exception("cancel-purge sweep cap exceeded; purged nothing"),
                "",
            )
        elif result.get("failed", 0) > 0:
            _send_founder_alert(
                "purge_sweep_failures",
                f"failed={result.get('failed')} purged={result.get('purged')}",
                Exception("cancel-purge sweep had per-user failures"),
                "",
            )
    except Exception as e:
        _send_founder_alert("purge_canceled_users", "ALL", e, traceback.format_exc())
        raise


def send_absence_detection_job():
    """Scheduler wrapper: enqueue via arq or run directly."""
    from product.queue import is_redis_available
    if is_redis_available():
        try:
            asyncio.run(_enqueue_absence())
        except Exception:
            _run_absence_detection()
    else:
        _run_absence_detection()


async def _enqueue_absence():
    from product.queue import enqueue
    await enqueue("job_detect_absence", _dedup_key="absence")


def send_trial_engagement_job():
    """Scheduler wrapper: enqueue via arq or run directly."""
    from product.queue import is_redis_available
    if is_redis_available():
        try:
            asyncio.run(_enqueue_engagement())
        except Exception:
            _process_trial_emails()
    else:
        _process_trial_emails()


async def _enqueue_engagement():
    from product.queue import enqueue
    await enqueue("job_process_engagement_emails", _dedup_key="engagement")


def send_probe_all_connections_job():
    """Scheduler wrapper for the hourly connection-state probe (G.2 Stage 2).

    Prefers arq for the actual fan-out so the work runs in the worker
    process. Falls back to in-process if Redis is unavailable — the
    write_connection_state call doesn't block the scheduler thread for
    long enough to matter (probe latency is single-digit milliseconds
    per provider per Render production logs).
    """
    from product.queue import is_redis_available
    if is_redis_available():
        try:
            asyncio.run(_enqueue_probe_all_connections())
        except Exception:
            _run_probe_all_connections_inline()
    else:
        _run_probe_all_connections_inline()


async def _enqueue_probe_all_connections():
    from product.queue import enqueue
    await enqueue("job_probe_all_connections", _dedup_key="probe_all_connections")


def _run_probe_all_connections_inline():
    """Direct in-process fallback for environments without arq/Redis."""
    from product.org_config import list_all_csms_with_connections
    from product.connection_writer import write_connection_state
    try:
        csm_ids = list_all_csms_with_connections()
    except Exception as exc:
        logger.warning("probe_all_connections inline eligibility failed: %s", exc)
        return
    for csm_id in csm_ids:
        try:
            asyncio.run(write_connection_state(csm_id))
        except Exception as exc:
            logger.warning("probe_all_connections inline failed for %s: %s", csm_id, exc)


# Start scheduler *after* FastAPI starts
@app.on_event("startup")
def _start_scheduler():
    global _scheduler

    # Load org timezone from org_config (falls back to pilot_config automatically)
    tz = "UTC"
    try:
        from product.org_config import load_org_config as _loc
        tz = _loc().get("timezone", "UTC")
    except Exception:
        logger.warning("Could not read org_config for timezone — defaulting to UTC")

    _scheduler = BackgroundScheduler()
    # Morning digest is NO LONGER a cron job — it's triggered on first login of the day
    _scheduler.add_job(
        send_absence_detection_job,
        CronTrigger(day_of_week="mon-fri", hour="8,12,16", minute=0, timezone=tz),
    )
    _scheduler.add_job(
        send_trial_engagement_job,
        CronTrigger(hour=9, minute=0, timezone=tz),
    )
    _scheduler.add_job(
        _expire_trials,
        CronTrigger(hour=0, minute=0, timezone="UTC"),
    )
    _scheduler.add_job(
        _purge_canceled_users,
        CronTrigger(hour=3, minute=0, timezone="UTC"),
    )
    # C.1 Commits 5+6: post-call scheduler at 5-min cadence, 30-min
    # meeting-ended window. Chains matcher → transcript body fetch →
    # LLM builder → timeline_entry.drafted + ProactiveAlert.
    from product.postcall.scheduler import run_postcall_tick
    _scheduler.add_job(
        run_postcall_tick,
        IntervalTrigger(minutes=5),
    )
    # G.2 Stage 2: eager hourly probe of every user's connections. Keeps
    # Connection.last_verified_at and Connection.status fresh so
    # /app/connection-status serves DB-backed state without making a
    # live probe per request. coalesce=True collapses misfires to a
    # single run on recovery (don't backfill an hour of probes).
    _scheduler.add_job(
        send_probe_all_connections_job,
        IntervalTrigger(hours=1),
        id="probe_all_connections_job",
        misfire_grace_time=300,
        coalesce=True,
    )
    _scheduler.start()
    logger.info("Scheduler started — absence 8am/12pm/4pm %s, trial emails 9am %s, trial expiry midnight UTC, cancel-purge sweep 3am UTC, postcall every 5 min, connection probe every 1 hour. Digest is login-triggered.", tz, tz)

@app.on_event("startup")
def _init_database():
    from product.database import init_db
    init_db()

@app.on_event("startup")
async def _init_redis():
    """Check Redis connectivity at startup."""
    if os.environ.get("REDIS_URL"):
        from product.queue import check_redis
        ok = await check_redis()
        if ok:
            logger.info("Redis connected — arq job queue active")
        else:
            logger.info("Redis not available — using BackgroundTasks fallback")
    else:
        logger.warning("REDIS_URL not set — using BackgroundTasks fallback")

@app.on_event("startup")
def _startup_log():
    logger.info("⚙️  startup — dotenv=%s  exists=%s  LLM_READY=%s", DOTENV_PATH, DOTENV_PATH.exists(), _LLM_READY)


@app.on_event("startup")
async def _probe_all_connections_on_deploy():
    """G.2 Stage 2 probe-on-deploy hook. Fires the connection-state probe
    once at startup so fresh Connection.status / last_verified_at land
    immediately after every Render deploy rather than waiting up to an
    hour for the IntervalTrigger to tick. Best-effort: any failure here
    is logged but does not block app startup."""
    try:
        from product.queue import check_redis, enqueue
        redis_ok = await check_redis()
        if redis_ok:
            await enqueue(
                "job_probe_all_connections",
                _dedup_key="probe_all_connections_startup",
            )
            logger.info("probe_all_connections enqueued at startup")
        else:
            # No worker — run inline in a background task so we don't
            # block the FastAPI startup sequence on a multi-user probe.
            asyncio.create_task(
                asyncio.to_thread(_run_probe_all_connections_inline)
            )
            logger.info("probe_all_connections scheduled inline at startup (Redis unavailable)")
    except Exception as exc:
        logger.warning("probe_all_connections startup hook failed: %s", exc)

ROOT = Path(".")
STORE_PATH = ROOT / "store.json"
ENGAGEMENT_PATH = ROOT / "engagement.json"

def _init_store():
    if not STORE_PATH.exists():
        STORE_PATH.write_text(json.dumps({"summaries": [], "version": 1}, indent=2))

def load_store():
    _init_store()
    try:
        text = STORE_PATH.read_text()
        return json.loads(text or "{}")
    except Exception:
        return {"summaries": [], "version": 1}

def save_store(store: dict):
    STORE_PATH.write_text(json.dumps(store, indent=2))
def get_todays_meetings():
    """
    Stub: read calendar.json and return all entries as meetings.

    Handles both shapes:
      - {"items": [ ... ]}
      - [ ... ]
    For now we do NOT filter by today's date to avoid empty agendas.
    """
    if not CALENDAR_PATH.exists():
        logger.info("calendar.json not found at %s", CALENDAR_PATH)
        return []

    try:
        raw = json.loads(CALENDAR_PATH.read_text())
    except Exception as e:
        logger.warning("calendar.json parse error: %s", e)
        return []

    # Support both dict-with-items and bare list
    if isinstance(raw, dict):
        items = raw.get("items", [])
    elif isinstance(raw, list):
        items = raw
    else:
        logger.warning("calendar.json unknown top-level type: %s", type(raw))
        return []

    meetings = []
    for it in items:
        if not isinstance(it, dict):
            continue
        meetings.append({
            "id": it.get("id") or str(uuid.uuid4()),
            "account": it.get("account") or it.get("summary") or "Unknown Account",
            "title": it.get("title") or it.get("summary") or f"Touchpoint with {it.get('account','Customer')}",
            "start": it.get("start") or "",
            "type": it.get("type") or "customer",
            "time": it.get("time") or "",
            "contact": it.get("contact") or "",
            "contact_title": it.get("contact_title") or "",
            "attendees": it.get("attendees") or [],
        })

    today_str = date.today().isoformat()
    meetings = [m for m in meetings if m.get("start", "").startswith(today_str)]
    meetings = [m for m in meetings
                if m.get("type", "customer") != "personal"]
    logger.info("get_todays_meetings -> %s meetings", len(meetings))
    return meetings
def build_prep_checklist_for_account(account: str) -> dict:
    """
    Build a short prep checklist for a given account using:
    - today's meetings (from calendar.json)
    - latest saved summary (store.json)
    """
    today = date.today()

    # Latest summary for this account (if any)
    store = load_store()
    summaries = store.get("summaries", [])
    acct_summaries = [s for s in summaries if s.get("account") == account]
    latest = None
    if acct_summaries:
        latest = sorted(
            acct_summaries,
            key=lambda s: s.get("created_at", ""),
            reverse=True,
        )[0]

    # Today’s meetings for this account
    meetings = get_todays_meetings()
    todays_mtg = None
    for mtg in meetings:
        if (mtg.get("account") or "").strip() == account:
            todays_mtg = mtg
            break

    checklist: list[str] = []

    # 1) Meeting context
    if todays_mtg:
        start = (todays_mtg.get("start") or "")[11:16] or "today"
        title = todays_mtg.get("title") or f"Touchpoint with {account}"
        checklist.append(f"Confirm agenda for '{title}' at {start} and align outcomes.")

    # 2) Key points from last summary
    if latest:
        kps = latest.get("key_points") or []
        if kps:
            checklist.append("Review last recap & key points before the call.")
        else:
            checklist.append("Skim previous notes for context on goals, value, and blockers.")
    else:
        checklist.append("No previous summary found – skim emails/tickets for context.")

    # 3) CTAs (due / overdue soon)
    overdue_titles = []
    due_soon_titles = []
    if latest:
        ctas = latest.get("ctas") or []
        for c in ctas:
            title = (c.get("title") or "CTA").strip()
            raw_due = (c.get("due_date") or "").strip()
            if not raw_due:
                continue
            try:
                due = date.fromisoformat(raw_due[:10])
            except Exception:
                continue
            delta = (due - today).days
            if delta < 0:
                overdue_titles.append(title)
            elif 0 <= delta <= 3:
                due_soon_titles.append(title)

    if overdue_titles:
        checklist.append("Be ready to address overdue CTAs: " + ", ".join(overdue_titles) + ".")
    if due_soon_titles:
        checklist.append("Confirm progress on upcoming CTAs (next 3 days): " + ", ".join(due_soon_titles) + ".")

    # 4) Value & metrics reminder
    checklist.append("Check product usage/health (dashboards, tickets, sentiment) before the call.")
    checklist.append("Write down 1–2 hypotheses on how you can move their outcomes forward this meeting.")

    return {
        "account": account,
        "date": str(today),
        "meeting": todays_mtg,
        "checklist": checklist[:5],  # keep it tight
    }

def list_accounts_from_store():
    store = load_store()
    names = set()
    for s in store.get("summaries", []):
        if s.get("account"):
            names.add(s["account"])
    return ["__ALL__"] + sorted(names)

# =============== Helpers ============================
def _iso_today():
    return date.today().isoformat()

def _iso_today_plus(days:int):
    return (date.today() + timedelta(days=days)).isoformat()

# ---------- JSON extraction that tolerates fences & truncation ----------
def _coerce_json(raw: str) -> dict:
    """
    Extract the longest complete JSON object from model output.
    - Strips ``` fences if present
    - Starts at the first '{'
    - Tracks quotes/escapes so braces inside strings are ignored
    - Returns the longest balanced {...}; if none complete, tries naive close
    """
    s = (raw or "").strip()

    # strip a leading fence line
    if s.startswith("```"):
        nl = s.find("\n")
        s = s[nl + 1:] if nl != -1 else s
    # strip trailing fence
    if s.endswith("```"):
        s = s[: s.rfind("```")]

    # find first '{'
    start = s.find("{")
    if start == -1:
        raise ValueError("No opening '{' found in model output")
    s = s[start:]

    depth = 0
    in_string = False
    escape = False
    last_complete = -1

    for i, ch in enumerate(s):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                last_complete = i

    if last_complete != -1:
        candidate = s[: last_complete + 1]
        return json.loads(candidate)

    # last resort: close braces
    missing = depth if depth > 0 else 0
    candidate = s + ("}" * missing)
    return json.loads(candidate)

# =============== LLM Persona & Summarizer ==============================
SYSTEM_PROMPT_CSM = """
You are a Customer Success Manager assistant. Your only job is to extract and
summarize what was explicitly said in the call transcript provided.

STRICT GROUNDING RULES — these override everything else:
- Only include information that was explicitly stated in the transcript.
- If something was not said in this call, do not include it.
- Do not infer intent, sentiment, or likelihood from tone or context.
- Do not add strategic commentary, background knowledge, or industry context.
- If the transcript does not contain enough information for a field, return an
  empty array — do not fabricate plausible-sounding content.

Return ONLY a single valid JSON object with this exact shape:

{
  "account": "<string>",
  "key_points": ["<string>", "..."],
  "risks": [
    {"category": "support|adoption|renewal|escalation|general",
     "signal": "<string — must be a direct quote or close paraphrase from the transcript>",
     "likelihood": "low|medium|high",
     "impact": "low|medium|high"}
  ],
  "ctas": [
    {"owner": "CSM", "title": "<string — only include if explicitly committed to in the call>",
     "due_date": "YYYY-MM-DD"}
  ]
}

Rules:
- Return all key_points, risks, and CTAs that are explicitly supported by the
  transcript. Do not cap or truncate. Do not pad with low-value items.
- A key_point must represent a decision, outcome, commitment, or meaningful
  update — not general conversation.
- A risk must be grounded in something explicitly raised in the call.
- A CTA must reflect an explicit commitment or request made during the call.
- Each key_point must be traceable to something said in the transcript.
- Use natural, concise language. No filler phrases.
- Output ONLY a valid JSON object — no markdown, no explanation.
"""


def llm_summarize_notes(transcript: str, account: str):
    """
    Returns (summary_dict, error_string_or_None).
    Uses JSON schema response format to avoid code fences, with robust parser fallback.
    """
    if not _LLM_READY:
        return None, "llm_not_configured"

    # Messages
    messages = [
        {"role": "user", "content": f"Account: {account}\n\nTranscript:\n{transcript}\n\nTODAY: {date.today().isoformat()}"}
    ]

    raw = None
    for _attempt in range(3):
        try:
            resp = _anthropic_client.messages.create(
                model=MODEL,
                max_tokens=900,
                temperature=0.0,
                system=SYSTEM_PROMPT_CSM + "\n\nRespond with valid JSON only, matching the required schema. No code fences or extra text.",
                messages=[{"role": "user", "content": messages[0]["content"]}],
            )
            raw = resp.content[0].text.strip()
            break
        except anthropic.APIStatusError as _api_err:
            if _api_err.status_code == 529 and _attempt < 2:
                import time as _time; _time.sleep(2)
                continue
            return None, str(_api_err)
        except Exception as e:
            return None, str(e)
    if raw is None:
        return None, "LLM unavailable after retries"

    logger.debug("🔎 RAW LLM JSON (first 400 chars): %s", raw[:400])

    try:
        try:
            data = json.loads(raw)
        except Exception:
            data = _coerce_json(raw)
        data["account"] = account
        return data, None
    except Exception as e:
        msg = str(e)
        logger.warning("llm_summarize_notes error: %s", msg)
        return None, msg

# =============== API Routes ===========================================
@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)

@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    return {"ok": True, "saved_summaries": len(load_store().get("summaries", [])), "llm_ready": _LLM_READY, "model": MODEL}
# ---------- Morning Digest helpers (local/mock) ----------

def _collect_ctas_from_store(days_ahead: int = 30):
    """
    Flatten CTAs from store.json and return next actions due within N days.
    This uses the same structure as the entries we already save in /notes/summarize.
    """
    store = load_store()
    out = []
    today = date.today()
    for s in store.get("summaries", []):
        for c in (s.get("ctas") or []):
            due_raw = (c.get("due_date") or "").strip()
            if not due_raw:
                continue

            due = None
            # Try ISO first (e.g., 2025-11-20 or 2025-11-20T10:00:00Z)
            try:
                due = datetime.fromisoformat(due_raw.replace("Z", "")).date()
            except Exception:
                # Then try simple YYYY-MM-DD
                try:
                    due = datetime.strptime(due_raw, "%Y-%m-%d").date()
                except Exception:
                    due = None

            if not due:
                continue

            delta = (due - today).days
            if 0 <= delta <= days_ahead:
                out.append({
                    "account": s.get("account", "Unknown"),
                    "title": c.get("title", ""),
                    "owner": c.get("owner", "CSM"),
                    "due_date": due.isoformat(),
                })

    # Soonest due first
    out.sort(key=lambda x: x["due_date"])
    return out


def _mock_calendar_today():
    """
    Replace this later with a real calendar integration.
    For now, just return some example meetings.
    """
    return [
        {"time": "09:30", "title": "Check-in — Meridian Health", "account": "Meridian Health"},
        {"time": "13:00", "title": "Check-in — Beacon Financial", "account": "Beacon Financial"},
        {"time": "14:30", "title": "Check-in — Sutton Retail", "account": "Sutton Retail"},
    ]


def _last_summary_for(account: str):
    """
    Get the most recent saved summary for an account (if any).
    Used to show 'last meeting summary' in the morning digest.
    """
    store = load_store()
    summaries = [s for s in store.get("summaries", []) if s.get("account") == account]
    if not summaries:
        return None

    # newest first based on created_at
    summaries.sort(key=lambda s: s.get("created_at", ""), reverse=True)
    s = summaries[0]
    return {
        "created_at": s.get("created_at"),
        "key_points": s.get("key_points", []),
        "risks": s.get("risks", []),
        "ctas": s.get("ctas", []),
    }


# ── Scene 2: Meridian pre-call brief (structured, no LLM) ─────────────────

@app.get("/prep/meridian")
def prep_meridian():
    """
    Structured pre-call brief for the Meridian Health demo call at 9:30am.
    Returns the full briefing with exact demo-script wording. Signals are
    sourced from the frozen demo portfolio (System #1 retirement, Batch B).
    """
    meridian_drivers = next(
        (a["drivers"] for a in DEMO_PORTFOLIO_FROZEN if a["account"] == "Meridian Health"),
        [],
    )

    return {
        "account": "Meridian Health",
        "contact": "Marcus Reyes",
        "contact_title": "VP of Support Operations",
        "call_time": "9:30am",
        "signals": meridian_drivers,
        "message": (
            "Sarah, your Meridian Health call is in 15 minutes. "
            "Here\u2019s what I\u2019d want you walking in with.\n\n"
            "Marcus opened the last call frustrated about API response times \u2014 their support team "
            "is missing a first response time SLA target of under 4 hours. Since then there\u2019s been "
            "some improvement but they\u2019re still not hitting the target. He\u2019ll lead with this. "
            "Don\u2019t wait for him to raise it \u2014 acknowledge the progress and give him a clear "
            "timeline from the product team.\n\n"
            "You have an open commitment from last call that hasn\u2019t been fulfilled: you said "
            "you\u2019d follow up with the product team on a timeline. You haven\u2019t sent it yet. "
            "I\u2019d acknowledge that directly. Marcus is data-oriented and will respect transparency "
            "over deflection.\n\n"
            "The QBR agenda is drafted and ready. Want to review it before you jump on?"
        ),
        "qbr_agenda_draft": [
            "Welcome and relationship check-in (5 min)",
            "Platform performance update \u2014 API response times and SLA progress (15 min)",
            "Power user adoption review \u2014 current state and recommendations (15 min)",
            "Roadmap items relevant to Meridian\u2019s use case (10 min)",
            "Strategic goals for Year 3 \u2014 aligning on outcomes (10 min)",
            "Next steps and commitments (5 min)",
        ],
    }


@app.get("/store/list")
def store_list(limit: int = 10, account: str = "__ALL__"):
    store = load_store()
    items = store.get("summaries", [])
    if account and account != "__ALL__":
        items = [s for s in items if s.get("account") == account]
    items = list(reversed(items))[:limit]
    return {"summaries": items, "total": len(items), "account": account}

# ---- LLM notes summarizer endpoint ----
@app.post("/notes/summarize")
async def notes_summarize(request: Request):
    """
    Take raw call notes, summarize with the LLM (if available),
    save into store.json, and (optionally) send a nicely formatted Slack recap.
    """
    # 1) Safely parse JSON body
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}

    # NEW: checkbox from UI controls whether we notify Slack
    notify_slack = bool(payload.get("notify_slack", True))  # default=True

    text = str(payload.get("transcript", "")).strip()
    account = str(payload.get("account", "Unknown Account")).strip() or "Unknown Account"

    if not text:
        return JSONResponse({"error": "Provide 'transcript' text"}, status_code=400)

    # 2) Try LLM summarizer
    summary, err = llm_summarize_notes(text, account)

    # ---------- Happy path: LLM worked ----------
    if summary and not err:
        entry = {
            "id": str(uuid.uuid4()),
            "created_at": datetime.utcnow().isoformat() + "Z",
            "account": summary.get("account", account),
            "key_points": summary.get("key_points", []),
            "risks": summary.get("risks", []),
            "ctas": summary.get("ctas", []),
            "transcript_excerpt": (text[:300] + ("…" if len(text) > 300 else "")),
        }

        # Save to JSON store
        store = load_store()
        store.setdefault("summaries", []).append(entry)
        save_store(store)

        # Send Slack *draft* notification if configured AND allowed by checkbox
        if SLACK_WEBHOOK_URL and notify_slack:
            key_points = summary.get("key_points", [])
            ctas = summary.get("ctas", [])
            risks = summary.get("risks", [])

            key_points_text = (
                "\n• " + "\n• ".join(key_points)
                if key_points else " None"
            )
            ctas_text = (
                "\n• " + "\n• ".join(
                    f"{c.get('title','(no title)')} — {c.get('owner','?')} (Due {c.get('due_date','TBD')})"
                    for c in ctas
                )
                if ctas else " None"
            )
            risks_text = "None" if not risks else ", ".join(
                r.get("category", "risk") for r in risks
            )

            slack_message = (
                f"*New call summary* _(Draft – please review before sending)_\n"
                f"*Account:* {summary.get('account', account)}\n"
                f"*Key Points:*{key_points_text}\n"
                f"*Risks:* {risks_text}\n"
                f"*CTAs:*{ctas_text}\n"
                "\n_You can edit this summary in the CS-Agent UI before sharing with the customer._"
            )
            slack_post(slack_message)

        return {"summary": entry, "saved_total": len(store["summaries"])}

    # ---------- Fallback path: LLM failed / not available ----------
    if err:
        logger.error("⚠ FALLBACK — no LLM used %s", err)

    bullets = [ln.strip("-• ").strip() for ln in text.splitlines() if ln.strip()]
    key_points = bullets[:3]

    entry = {
        "id": str(uuid.uuid4()),
        "created_at": datetime.utcnow().isoformat() + "Z",
        "account": account,
        "summary": "Basic non-AI summary (LLM unavailable)",
        "key_points": key_points,
        "risks": [
            {
                "category": "support",
                "signal": "Manual fallback summarization",
                "likelihood": "low",
                "impact": "low",
            }
        ],
        "ctas": [
            {
                "owner": "CSM",
                "title": "Send recap & next steps",
                "due_date": (date.today() + timedelta(days=1)).isoformat(),
            }
        ],
        "transcript_excerpt": (text[:300] + ("…" if len(text) > 300 else "")),
    }

    store = load_store()
    store.setdefault("summaries", []).append(entry)
    save_store(store)

    if SLACK_WEBHOOK_URL and notify_slack:
        fallback_msg = (
            f"*New call summary (fallback)*\n"
            f"*Account:* {account}\n"
            f"*Key Points:* {'; '.join(key_points) or 'None'}"
        )
        slack_post(fallback_msg)
        set_call_mode(False)

    return {"summary": entry, "saved_total": len(store["summaries"])}


@app.post("/call/start")
def call_start():
    set_call_mode(True)
    return {"call_mode": True}

@app.get("/engagement")
def get_engagement():
    if ENGAGEMENT_PATH.exists():
        try:
            return json.loads(ENGAGEMENT_PATH.read_text())
        except:
            return []
    return []
@app.post("/notes/finalize")
async def notes_finalize(request: Request):
    """
    Take an existing saved summary (by id),
    build a customer-facing recap, and send a Slack preview (to you).
    """
    # 1) Parse body
    try:
        payload = await request.json()
        entry_id = str(payload.get("id", "")).strip()
    except Exception:
        entry_id = ""

    if not entry_id:
        return JSONResponse({"error": "Missing id"}, status_code=400)

    # 2) Find the matching saved summary
    store = load_store()
    entries = store.get("summaries", [])
    target = next((e for e in entries if e.get("id") == entry_id), None)

    if not target:
        return JSONResponse({"error": "Summary not found"}, status_code=404)

    account = target.get("account", "Unknown account")
    subject = f"Call recap — {account}"

    # 3) Build a customer-facing recap body
    body_lines = []
    body_lines.append(f"Hi team at {account},")
    body_lines.append("")
    body_lines.append("Here is a recap of our recent conversation:")
    body_lines.append("")

    for kp in (target.get("key_points") or []):
        body_lines.append(f"- {kp}")

    ctas = target.get("ctas") or []
    if ctas:
        body_lines.append("")
        body_lines.append("Next steps:")
        for c in ctas:
            title = c.get("title", "(no title)")
            owner = c.get("owner", "Owner")
            due = c.get("due_date", "TBD")
            body_lines.append(f"- {title} — {owner} (Due {due})")

    body_lines.append("")
    body_lines.append("Best,")
    body_lines.append("Your CSM")

    customer_text = "\n".join(body_lines)

    # 4) Log + send preview to Slack (to YOU, not the customer)
    logger.debug("Customer recap (preview only):\n %s", customer_text)

    if SLACK_WEBHOOK_URL:
        slack_post(
            f"*Customer recap prepared for {account}*\n"
            f"_Subject:_ {subject}\n"
            "```" + customer_text[:1900] + "```"
        )

    # 5) Mark as 'sent' in the store for tracking
    target["sent_to_customer_at"] = datetime.utcnow().isoformat() + "Z"
    save_store(store)

    return {
        "status": f"Recap prepared for {account} and logged",
        "entry": target,
        "customer_text": customer_text,
        
    }

# =============== Browser UI ===========================================
BASE_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>CSA — Slack Mock</title>
  <style>
    :root{
      --bg:#1a1d21;--panel:#23262b;--text:#e3e5e8;
      --muted:#9aa3ad;--accent:#4f9eee;--border:#2d3136;
    }
    *{box-sizing:border-box}
    body{
      margin:0;font-family:system-ui,Segoe UI,Roboto,Arial;
      background:var(--bg);color:var(--text)
    }
    .app{
      display:grid;grid-template-columns:340px 1fr;height:100vh
    }
    .sidebar{
      border-right:1px solid var(--border);padding:12px
    }
    .channel{
      padding:8px;border-radius:8px
    }
    .channel.active{
      background:rgba(79,158,238,.15)
    }
    .header{
      display:flex;justify-content:space-between;align-items:center;
      padding:12px 16px;border-bottom:1px solid var(--border);background:#23262b
    }
    .scroll{
      padding:16px;overflow:auto;height:calc(100vh - 52px)
    }
    .msg{
      display:grid;grid-template-columns:40px 1fr;gap:12px;padding:8px
    }
    .avatar{
      width:36px;height:36px;border-radius:6px;background:#0e70d1;
      display:flex;align-items:center;justify-content:center;
      color:#fff;font-weight:700
    }
    .bubble{
      background:#23262b;border:1px solid var(--border);
      border-radius:10px;padding:10px 12px
    }
    .btn{
      padding:8px 10px;border-radius:8px;border:1px solid var(--border);
      background:var(--accent);color:#fff;cursor:pointer;font-size:13px
    }
    .btn.secondary{
      background:#3a7bd5;
    }
    .btn.danger{
      background:#b85c5c;
    }
    .muted{color:var(--muted);font-size:12px}
    .card{
      background:#1f2327;border:1px solid var(--border);
      border-radius:10px;padding:10px 12px
    }
    textarea{
      width:100%;min-height:110px;border-radius:8px;
      border:1px solid var(--border);background:#15181c;
      color:#e3e5e8;padding:8px
    }
    label{font-size:12px;color:var(--muted)}
    .small{font-size:12px}
    .list{
      display:flex;flex-direction:column;gap:8px;
      max-height:220px;overflow:auto;margin-top:8px
    }
    .item{
      border:1px solid var(--border);border-radius:8px;
      padding:8px;background:#1b1f23
    }
    input[type="text"], input[type="file"]{
      width:100%;border-radius:8px;border:1px solid var(--border);
      background:#15181c;color:#e3e5e8;padding:8px
    }
    .row{display:flex;gap:8px;align-items:center}
    select{
      width:100%;border-radius:8px;border:1px solid var(--border);
      background:#15181c;color:#e3e5e8;padding:8px
    }
    .err{color:#ffb3b3}
    .badge{
      display:inline-block;padding:2px 6px;border-radius:6px;
      border:1px solid var(--border);font-size:11px;margin-left:6px
    }
    .source{
  margin-top:2px;
  color:var(--muted);
  font-size:11px;
  line-height:1.2;
}
.insight{
  margin-top:4px;
}
    .badge.low{background:#3a1f1f}
    .badge.medium{background:#2f2f1d}
    .badge.high{background:#1f2f1f}
  </style>
</head>
<body>
<div class="app">
  <aside class="sidebar">
    <div class="channel active"># customer-success-agent</div>
    <div class="channel"># meridian-health</div>
    <div class="channel"># beacon-financial</div>
    <div class="channel"># sutton-retail</div>
    <div class="channel"># northgate-logistics</div>
    <div class="channel"># pinnacle-group</div>

    <div style="margin-top:16px" class="card">
      <label>Account</label>
      <div class="row">
        <select id="account">__ACCOUNT_OPTIONS__</select>
      </div>
      <div class="row" style="margin-top:6px">
        <input id="newAccount" type="text" placeholder="Add new account name"/>
        <button id="btnAddAcct" class="btn">Add</button>
      </div>
      <div id="acctErr" class="small err" style="display:none"></div>
    </div>

    <div style="margin-top:16px" class="card">
      <label>Paste call notes</label>
      <textarea id="notes" placeholder="Paste raw notes or transcript here..."></textarea>
      <div class="row" style="margin-top:8px; justify-content:space-between">
        <div class="small muted">Send to Slack</div>
        <input id="notifySlack" type="checkbox" checked/>
      </div>
     <div style="display:flex; gap:8px; margin-top:8px; flex-wrap:wrap">
     <button id="btnSendGmailLeft" class="btn">
  Send via Gmail
</button>
  <button id="btnGen" class="btn">Generate & Save</button>
  <button id="btnCallMode" class="btn" style="background:#4B9A4C">Call Mode</button>
  <button id="btnHistory" class="btn" style="background:#3a7bd5">History</button>
  <button id="btnClear" class="btn" style="background:#b85c5c">Clear Saved</button>
  <button id="btnCTAReminders" class="btn" style="background:#b8744f">
  CTA Reminders
</button>
</div>
      <div class="small muted" style="margin-top:6px">
        History respects the selected account. Choose “All Accounts” to see everything.
      </div>
      <div id="history" class="list" style="display:none"></div>
      <div id="historyMeta" class="small muted" style="display:none"></div>
    </div>

  </aside>

  <main>
    <div class="header">
  <div style="display:flex; gap:8px">
    <button id="btnDigest" class="btn">Morning Digest</button>
    <button id="btnPrep" class="btn">Prep This Account</button>
    <button id="btnSummary" class="btn">Meeting Summary</button>
    <button id="btnScan" class="btn">Low Engagement</button>
    <button id="btnQBR" class="btn">QBR Deck</button>
    <button id="btnSendGmail" class="btn">Send via Gmail</button>
  </div>
</div>

  <!-- THIS MUST BE PRESENT -->
  <div id="feed" class="scroll"></div>
</main>

<script>
const feed = document.getElementById('feed');
function esc(s){
  return String(s || '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function renderSource(src){
  // src can be null/undefined or { type, label }
  if (!src) return '';
  const type = esc(src.type || 'Source');
  const label = esc(src.label || '');
  return `<div class="source"><i>Source:</i> ${type}${label ? ' — ' + label : ''}</div>`;
}

function insight(text, src){
  return `<div class="insight">${esc(text)}${renderSource(src)}</div>`;
}
function bubble(html){
  const m = document.createElement('div'); 
  m.className='msg';
  m.innerHTML = '<div class="avatar">C</div><div><div class="bubble">'+html+'</div></div>';
  feed.appendChild(m); 
  feed.scrollTop = feed.scrollHeight;
}

function errorBubble(msg){
  bubble('<b>Error:</b> <span class="err">'+msg+'</span>');
}

function typing(on=true){
  let el = document.getElementById('typing');
  if(on){
    if(!el){
      el = document.createElement('div'); 
      el.id='typing'; 
      el.className='msg';
      el.innerHTML = '<div class="avatar">C</div><div><div class="bubble"><span class="muted">CSA Bot is typing…</span></div></div>';
      feed.appendChild(el); 
      feed.scrollTop = feed.scrollHeight;
    }
  } else {
    if(el){ el.remove(); }
  }
}

function currentAccount(){
  const el = document.getElementById('account');
  return (el && el.value) ? el.value : 'Unknown Account';
}
async function prepAccount(){
  const acct = currentAccount();
  typing(true);
  try {
    const resp = await fetch('/prep', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        account: (acct === '__ALL__' ? 'Unknown Account' : acct)
      })
    });
    const data = await resp.json();
    typing(false);

    const accountName = data.account || acct;
    const date = data.date || '';
    const meeting = data.meeting || null;
    const items = data.checklist || [];

    let html = `<b>Prep checklist — ${accountName}</b>`;
    if (date){
      html += `<div class="small muted">${date}</div>`;
    }
    if (meeting){
      const t = (meeting.start || '').slice(11,16);
      const title = meeting.title || '';
      html += `<div class="small muted">Today: ${t} • ${title}</div>`;
    }

    if (!items.length){
      html += `<div class="small muted" style="margin-top:4px">
        No specific prep items – review last recap & CTAs.
      </div>`;
    } else {
      html += '<ul style="margin-top:4px;">' +
        items.map(i => `<li>${i}</li>`).join('') +
        '</ul>';
    }

    bubble(html);
  } catch (e){
    typing(false);
    errorBubble('Failed to load prep checklist.');
    console.error(e);
  }
}

function cannedSummary(){
  const acct = currentAccount();
  const html = `<b>Meeting Summary — ${acct === '__ALL__' ? 'All' : acct}</b>
    <div><b>Risks</b></div>
    <ul>
      <li><b>Support</b> — L: medium, I: high <span class="muted">(Backlog grew)</span></li>
      <li><b>Adoption</b> — L: medium, I: medium <span class="muted">(Low macro usage)</span></li>
    </ul>
    <div><b>CTAs</b></div>
    <ul>
      <li><b>Schedule admin training</b> — Owner: CSM (Due: 2 days)</li>
      <li><b>Macro adoption sprint</b> — Owner: CSM</li>
    </ul>`;
  bubble(html);
}

function qbr(){
  const acct = currentAccount();
  const html = `<b>QBR Deck — Ready (${acct === '__ALL__' ? 'All' : acct})</b>
    <div class="muted">Auto-generated from adoption &amp; value metrics</div>
    <div style="margin-top:6px;">
      <button class="btn">Download</button>
      <button class="btn">Open in Slides</button>
    </div>`;
  bubble(html);
}
let callModeOn = false;  // global flag

function startCallMode(){
  const btn = document.getElementById('btnCallMode');
  const acct = currentAccount();

  callModeOn = !callModeOn;   // toggle

  if (callModeOn) {
    // Turned ON
    if (btn) btn.textContent = 'Call Mode: ON';
    bubble(`<b>Call mode started</b><div class="muted">
      Agent will auto-summarize notes for ${acct} and send drafts to Slack when you click “Generate & Save”.
    </div>`);
  } else {
    // Turned OFF
    if (btn) btn.textContent = 'Call Mode';
    bubble(`<b>Call mode ended</b><div class="muted">
      You can still paste notes and generate summaries manually.
    </div>`);
  }
}
function morning(){
  fetch('/digest/morning', { method:'POST' })
    .then(r => r.json())
    .then(d => {
      
      const digest = d.digest || {};
      const agenda = digest.agenda || [];
      let html = `<b>Morning Update — ${digest.date}</b>`;

      agenda.forEach(slot => {
        const acct = slot.account || {};
        const time = slot.time || "";
        const name = acct.name || "";
        const lastItem = slot.last_meeting_summary_item || null;
        
const last = slot.last_meeting_summary || "";
        const openItems = Array.isArray(slot.open_actions_items) ? slot.open_actions_items : null;
const openActionsText = (slot.open_actions || []).join(', ') || 'None logged';
        const usage = (slot.usage_highlights || []).join(', ') || 'Usage not connected yet.';
        const risks = (slot.risks || []).join(', ');
        const riskItems = Array.isArray(slot.risks_items) ? slot.risks_items : null;
        const news = (slot.news || []).join(', ');
        const ctaSummary = slot.cta_summary || "";

        html += `
          <div style="margin-top:10px;">
            <div class="small muted">${time} • ${name}</div>
            <div><b>Last:</b> ${lastItem ? insight(lastItem.text, lastItem.source) : last}</div>
            <ul>
              <li><b>Open Actions:</b>
  ${
    openItems
      ? `<ul style="margin:4px 0 0 14px; padding:0;">` +
        openItems
          .map(x => `<li style="margin:0 0 6px 0;">${insight(x.text, x.source)}</li>`)
          .join('') +
        `</ul>`
      : ` ${esc(openActionsText)}`
  }
</li>
              <li><b>Usage:</b> ${usage}</li>
              ${
  (riskItems && riskItems.length)
    ? `<li><b>Risks:</b>
         <ul style="margin:4px 0 0 14px; padding:0;">
           ${riskItems
             .map(x => `<li style="margin:0 0 6px 0;">${insight(x.text, x.source)}</li>`)
             .join('')}
         </ul>
       </li>`
    : (risks ? `<li><b>Risks:</b> ${risks}</li>` : '')
}
              ${ctaSummary ? `<li><b>CTA Reminders:</b> ${ctaSummary}</li>` : ''}
              <li><b>News:</b> ${news}</li>
            </ul>
          </div>`;
      });

      bubble(html);
    })
    .catch(e => {
      errorBubble('Failed to load Morning Digest');
      console.error(e);
    });
}
async function showCTAReminders(){
  try {
    const r = await fetch('/cta/reminders');
    const d = await r.json();

    let html = `<b>CTA Reminders</b>`;

    function list(name, arr){
      if (arr.length === 0) return `<li>${name}: <span class="muted">none</span></li>`;
      return `<li><b>${name}:</b><ul>${arr.map(x => `<li>${x}</li>`).join('')}</ul></li>`;
    }

    html += `
      <ul>
        ${list("Overdue", d.overdue)}
        ${list("Due Today", d.due_today)}
        ${list("Coming up (3 days)", d.upcoming)}
      </ul>
    `;

    bubble(html);
  } catch (e){
    errorBubble("CTA reminders failed");
    console.error(e);
  }
}

document.getElementById('btnCTAReminders').onclick = showCTAReminders;

async function generateSummary(){
  const notesEl = document.getElementById('notes');
  const notes = (notesEl.value || '').trim();
  const acct = currentAccount();
  const notifyEl = document.getElementById('notifySlack');
  const notifySlack = notifyEl ? !!notifyEl.checked : true;

  if(!notes){
    alert('Paste some notes first.');
    return;
  }
  typing(true);
  try{
    const resp = await fetch('/notes/summarize', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        transcript: notes,
        account: (acct === '__ALL__' ? 'Unknown Account' : acct),
        notify_slack: notifySlack
      })
    });
    const data = await resp.json();
    typing(false);

    if(!resp.ok){
      if (data && data.error === 'llm_insufficient_quota'){
        errorBubble('AI summarizer is out of quota. Check your OpenAI billing/spend limit, then try again.');
        return;
      }
      if (data && data.error === 'llm_rate_limited'){
        errorBubble('AI summarizer hit a rate limit. Please try again in a few seconds.');
        return;
      }
      errorBubble('Summarizer failed. Falling back to basic rules.');
      return;
    }

    if(data.error){
      errorBubble(data.error);
      return;
    }

    const s = data.summary;
    lastSummaryId = s.id
    const risksHtml = (s.risks||[]).map(r =>
      `<li><b>${r.category}</b> — L:${r.likelihood || '?'} I:${r.impact || '?'} <span class="muted">(${r.signal || ''})</span></li>`
    ).join('');
    const ctasHtml = (s.ctas||[]).map(c =>
      `<li><b>${c.title}</b> — ${c.owner} (Due ${c.due_date||'TBD'})</li>`
    ).join('');

    const html = `<b>Saved Summary — ${s.account}</b>
      <div class="small muted">${s.created_at}</div>
      <div>${(s.key_points||[]).join(' • ')}</div>
      <div style="margin-top:6px"><b>Risks</b></div><ul>${risksHtml || '<li>None</li>'}</ul>
      <div><b>CTAs</b></div><ul>${ctasHtml || '<li>None</li>'}</ul>`;
    bubble(html);
    notesEl.value = "";
    showHistory(true);
  }catch(e){
    typing(false);
    errorBubble('Failed to generate/save summary.');
    console.error(e);
  }
}

async function showHistory(forceOpen=false){
  const list = document.getElementById('history');
  const meta = document.getElementById('historyMeta');
  const wasHidden = (list.style.display === 'none');
  if (wasHidden || forceOpen){
    list.style.display = 'block';
    meta.style.display = 'block';
  } else {
    list.style.display = 'none';
    meta.style.display = 'none';
    return;
  }

  const acct = currentAccount();
  try{
    const r = await fetch('/store/list?account=' + encodeURIComponent(acct));
    const d = await r.json();
    list.innerHTML = '';
    d.summaries.forEach(s => {
      const risks = (s.risks||[]).map(r => `<span class="small">${r.category}</span>`).join(' · ') || '<span class="small muted">no risks</span>';
      const item = document.createElement('div');
      item.className = 'item';
      item.innerHTML = `
        <div><b>${s.account}</b> <span class="small muted">${s.created_at}</span></div>
        <div class="small">${s.summary}</div>
        <div class="small muted">Risks: ${risks}</div>
        <div class="small muted">Excerpt: ${(s.transcript_excerpt||'').replaceAll('<','&lt;')}</div>
        <div style="margin-top:6px;">
          <button class="btn secondary" onclick="prepareRecap('${s.id}')">Prepare Recap</button>
        </div>`;
      list.appendChild(item);
    });
    meta.textContent = 'Showing ' + d.summaries.length + ' of ' + d.total + ' saved summaries (' + (acct === '__ALL__' ? 'All Accounts' : acct) + ')';
  }catch(e){
    list.innerHTML = '';
    meta.textContent = 'Could not load history.';
    errorBubble('Failed to load history.');
    console.error(e);
  }
}

function addAccount(){
  const input = document.getElementById('newAccount');
  const name = input.value.trim();
  if(!name){ return; }
  const sel = document.getElementById('account');
  for (const o of sel.options){
    if(o.value === name){
      sel.value = name;
      input.value='';
      return;
    }
  }
  const opt = document.createElement('option');
  opt.value = name; 
  opt.textContent = name;
  sel.appendChild(opt);
  sel.value = name;
  input.value = '';
}

async function lowEngagement(){
  try{
    const r = await fetch('/engagement');
    const rows = await r.json();
    if(!Array.isArray(rows) || rows.length === 0){
      bubble('<b>Low Engagement</b><div class="muted">No CSV processed yet. Upload a CSV in the left panel.</div>');
      return;
    }
    rows.sort((a,b)=> (a.engagement_score - b.engagement_score));
    const top = rows.slice(0, Math.min(5, rows.length));
    let html = `<b>Low Engagement Report</b><div class="muted">Top at-risk accounts (lowest scores)</div><ul>`;
    for(const row of top){
      html += `<li><b>${row.account}</b> — score ${row.engagement_score} <span class="badge ${row.band}">${row.band}</span>
        <div class="small muted">
          adoption ${Math.round(row.adoption_rate*100)}%, 
          usage ratio ${row.usage_ratio}, 
          tickets ${row.tickets_open}, 
          last seen ${row.last_activity_days}d
        </div>
      </li>`;
    }
    html += `</ul>`;
    bubble(html);
  }catch(e){
    errorBubble('Failed to load engagement. Did you upload a CSV?');
    console.error(e);
  }
}

function showTemplate(accountEnc, score, bandLabel){
  const account = decodeURIComponent(accountEnc);
  const tpl = `Subject: Quick check-in & adoption plan for ${account}

Hi team,

I noticed engagement has dipped (score ${score}, ${bandLabel}). Could we align on a short plan to lift adoption? I can bring a 30-min enablement on macros/deflection, a usage dashboard walk-through, and success criteria we can track weekly.

Would next Tue/Wed work?

Thanks,
Your CSM`;
  bubble(`<b>Proposed Outreach — ${account}</b><pre style="white-space:pre-wrap">${tpl}</pre>`);
}
let lastSummaryId = null;  // track the most recent summary we saved

async function prepareRecap(){
  const acct = currentAccount();
  try {
    const r = await fetch('/store/list?account=' + encodeURIComponent(acct));
    const d = await r.json();
    if (!d.summaries || d.summaries.length === 0){
      errorBubble('No summaries found for this account.');
      return;
    }

    // Use the latest summary for this account
    const latest = d.summaries[d.summaries.length - 1];

    const resp = await fetch('/notes/finalize', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ id: latest.id })
    });
    const data = await resp.json();

    if (!resp.ok || data.error){
      errorBubble(data.error || 'Failed to prepare customer recap.');
      return;
    }

    const recapText = data.customer_text || '';

    bubble(
      `<b>Customer Recap — ${data.entry.account}</b>` +
      `<div class="small muted">${data.entry.sent_to_customer_at || ''}</div>` +
      `<pre style="white-space:pre-wrap; margin-top:6px;">${recapText}</pre>`
    );
  } catch (e){
    console.error(e);
    errorBubble('Error while preparing recap.');
  }
}

async function prepareRecap(id){
  try{
    const resp = await fetch('/notes/finalize', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ id })
    });
    const data = await resp.json();
    if(!resp.ok){
      errorBubble(data.error || 'Failed to prepare recap');
      return;
    }
    bubble('<b>Recap prepared</b><div class="small muted">'+ (data.status || '') +'</div>');
  }catch(e){
    errorBubble('Failed to prepare recap.');
    console.error(e);
  }
}
async function sendLatestEmailGmail() {
  const acct = currentAccount();
  if (!acct || acct === '__ALL__') {
    alert('Please select a specific account first.');
    return;
  }

  try {
    // 1) Fetch summaries
    const res = await fetch('/store/list?account=' + encodeURIComponent(acct));
    const data = await res.json();
    const summaries = data.summaries || [];

    if (!summaries.length) {
      alert('No summaries found for ' + acct + ' to email.');
      return;
    }

    // 2) Grab latest summary
    const latest = summaries.sort(
      (a, b) => (a.created_at || '').localeCompare(b.created_at || '')
    ).pop();

    // 3) Send via Gmail
    const resp = await fetch('/email/gmail', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: latest.id })
    });

    const out = await resp.json().catch(() => ({}));
    console.log('EMAIL_GMAIL', out);

    if (out.error) {
      alert('Gmail send failed: ' + out.error);
      return;
    }

    alert(`Sent Email via Gmail for ${acct}. Check your Gmail Sent folder.`);
    
  } catch (err) {
    console.error(err);
    alert('Error sending Gmail: ' + err);
  }
}

// ---- Wire up buttons (defensive: only if element exists) ----
function hookClick(id, handler) {
  const el = document.getElementById(id);
  if (el) el.onclick = handler;
}

// Top bar
hookClick('btnDigest', morning);
hookClick('btnPrep', prepAccount);
hookClick('btnSummary', cannedSummary);
hookClick('btnScan', lowEngagement);
hookClick('btnQBR', qbr);
hookClick('btnSendGmail', sendLatestEmailGmail);
hookClick('btnSendGmailLeft', sendLatestEmailGmail);

// Left panel actions
hookClick('btnGen', generateSummary);
hookClick('btnRecap', prepareRecap);
hookClick('btnCallMode', startCallMode);
hookClick('btnHistory', () => showHistory(false));

const clearBtn = document.getElementById('btnClear');
if (clearBtn) {
  clearBtn.onclick = async () => {
    const acct = currentAccount();
    const label = acct === '__ALL__' ? 'ALL accounts' : ('account "' + acct + '"');
    if (!confirm('Delete saved summaries for ' + label + '?')) return;
    await fetch('/store/clear?account=' + encodeURIComponent(acct), { method:'POST' });
    document.getElementById('history').innerHTML = '';
    document.getElementById('historyMeta').textContent = 'Showing 0 of 0 saved summaries';
  };
}

hookClick('btnAddAcct', addAccount);

// Seed first message
morning();
</script>
</body>
</html>
"""

@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def ui(request: Request):
    return HTMLResponse(_fill_page_tokens(Path("index.html").read_text(encoding="utf-8")))


@app.get("/cta/reminders")
def cta_reminders():
    """Simple CTA reminder endpoint."""
    store = load_store()
    entries = store.get("summaries", [])
    overdue, due_today, upcoming = check_cta_reminders(entries)

    return {
        "overdue": overdue,
        "due_today": due_today,
        "upcoming": upcoming,
        "total_ctas": len(overdue) + len(due_today) + len(upcoming),
    }


# =============== Assistant "Today" Bundle ======================
@app.get("/assistant/today")
def assistant_today():
    """
    Powers the structured morning digest card in the UI.
    Returns accounts (risk-ordered, with reason string), CSM-owned CTAs, and today's meetings.
    No LLM calls - fast and deterministic. Portfolio sourced from
    DEMO_PORTFOLIO_FROZEN (System #1 retirement, Batch B).
    """
    today = date.today()

    # Accounts: iterate the frozen demo portfolio, attach has_meeting from calendar.
    portfolio_account_names = {a["account"] for a in DEMO_PORTFOLIO_FROZEN}
    meetings_raw = get_todays_meetings()
    meeting_accounts = set()
    for m in meetings_raw:
        raw_acct = m.get("account", "")
        for known_acct in portfolio_account_names:
            if known_acct in raw_acct:
                meeting_accounts.add(known_acct)
                break
        else:
            meeting_accounts.add(raw_acct)

    accounts = [
        {
            "account": a["account"],
            "risk": a["risk"],
            "score": a["score"],
            "trend": a["trend"],
            "reason": a["reason"],
            "has_meeting": a["account"] in meeting_accounts,
        }
        for a in DEMO_PORTFOLIO_FROZEN
    ]
    accounts.sort(key=lambda x: x["score"])

    # CTAs from store.json, gated to the demo account set (was: gated to risk_summary keys).
    demo_accounts = {
        "Northgate Logistics",
        "Meridian Health",
        "Pinnacle Group",
        "Beacon Financial",
        "Sutton Retail",
    }
    store = load_store()
    entries = store.get("summaries", [])
    ctas = []
    seen = set()
    for summary in entries:
        acct_name = summary.get("account", "")
        if acct_name not in demo_accounts:
            continue
        items = summary.get("commitments") or summary.get("ctas") or []
        for item in items:
            if (item.get("owner") or "").upper() != "CSM":
                continue
            title = (item.get("title") or "").strip()
            due_raw = (item.get("due_date") or "").strip()
            if not title or not due_raw:
                continue
            import re as _re
            norm_title = _re.sub(r'[^a-z0-9\s]', '', title.lower()).strip()
            key = (acct_name, norm_title)
            if key in seen:
                continue
            seen.add(key)
            try:
                due = date.fromisoformat(due_raw)
            except Exception:
                continue
            delta = (due - today).days
            if delta < 0:
                status = "overdue"
            elif delta == 0:
                status = "due_today"
            elif delta <= 7:
                status = "upcoming"
            else:
                continue
            ctas.append({"account": acct_name, "title": title, "due_date": due_raw, "status": status})

    ctas.sort(key=lambda x: x["due_date"])
    ctas = ctas[:5]

    overdue_out = []
    overdue_seen: set = set()
    for summary in entries:
        acct_name = summary.get("account", "")
        if acct_name not in demo_accounts:
            continue
        for c in (summary.get("ctas") or []):
            raw_due = (c.get("due_date") or "").strip()
            title = (c.get("title") or "").strip()
            if not raw_due or not title:
                continue
            try:
                due = date.fromisoformat(raw_due[:10])
            except Exception:
                continue
            if due >= today:
                continue
            key = (acct_name, title)
            if key in overdue_seen:
                continue
            overdue_seen.add(key)
            overdue_out.append({
                "title": title,
                "account": acct_name,
                "status": "overdue",
                "due_date": due.isoformat(),
            })
    overdue_out.sort(key=lambda x: x["due_date"])
    overdue_out = overdue_out[:5]

    # Meetings stay live from calendar.json.
    meetings = [
        {
            "account": m.get("account", ""),
            "title": m.get("title", ""),
            "time": (m.get("start") or "")[11:16],
            "start": m.get("start", ""),
            "contact": m.get("contact", ""),
            "contact_title": m.get("contact_title", ""),
            "attendees": m.get("attendees") or [],
        }
        for m in meetings_raw
    ]

    return {
        "date": today.isoformat(),
        "accounts": accounts,
        "ctas": ctas,
        "overdue_ctas": overdue_out,
        "due_today_ctas": [],
        "meetings": meetings,
    }


# ── Scene 5b: Northgate proactive alert (structured) ─────────────────────

@app.get("/proactive/northgate")
def proactive_northgate():
    """
    Returns a structured proactive alert for Northgate Logistics. Frozen at
    at_risk in the demo portfolio so the alert always triggers (System #1
    retirement, Batch B).
    """
    northgate = next(
        (a for a in DEMO_PORTFOLIO_FROZEN if a["account"] == "Northgate Logistics"),
        None,
    )
    if not northgate or northgate["risk"] not in ("at_risk", "watch"):
        return {"triggered": False}

    return {
        "triggered": True,
        "account": "Northgate Logistics",
        "risk": northgate["risk"],
        "score": northgate["score"],
        "drivers": northgate["drivers"],
        "suggested_actions": [
            {"id": "send_dana", "label": "Send message to Dana", "description": "Draft a brief, non-renewal message reopening the door"},
            {"id": "log_cta", "label": "Log risk CTA in Gainsight", "description": "Flag this account for the team"},
        ],
        "message": (
            "Sarah, I want to come back to Northgate Logistics before your day gets away from you.\n\n"
            "I\u2019ve been watching this account and a few things are converging that I think warrant your "
            "attention today \u2014 not this week.\n\n"
            "They haven\u2019t logged in for 11 days. Their last three support tickets were submitted by "
            "Dana\u2019s manager, not Dana \u2014 which is unusual. Dana has been your primary contact for "
            "18 months and she\u2019s gone quiet. The tickets themselves are minor but the pattern of who "
            "is submitting them suggests something may have shifted internally.\n\n"
            "Their renewal is in 45 days. In my experience with accounts at this signal level, the "
            "conversation gets significantly harder if it isn\u2019t initiated before the 30-day mark.\n\n"
            "I don\u2019t think this is a lost account. I think this is an account that needs to hear from "
            "you today \u2014 not with an agenda, just with presence.\n\n"
            "I\u2019ve drafted a message to Dana and a risk CTA for Gainsight. Want to see them before I send?"
        ),
        "draft_to_dana": (
            "Hi Dana \u2014 wanted to reach out. I noticed it\u2019s been a couple of weeks since your team "
            "has been active on the platform, and I know Q1 can get hectic. No agenda \u2014 just wanted to "
            "make sure everything is running smoothly on your end. Happy to jump on a quick call this week "
            "if that\u2019s useful."
        ),
        "gainsight_cta": (
            "Risk flag \u2014 Northgate Logistics. 11-day login gap, adoption down 42pp, renewal in 45 days. "
            "Primary contact gone quiet. Recommend outreach before 30-day mark. Owner: Sarah C."
        ),
    }


# ── Scene 5: Northgate proactive alert (narrative) ────────────────────────

@app.get("/northgate/alert")
def northgate_alert():
    """
    Scene 5 narrative alert for Northgate Logistics.
    Returns a prose message for renderNarrative() in the UI.
    """
    return {
        "account": "Northgate Logistics",
        "message": (
            "Sarah, I want to come back to Northgate Logistics before your day gets away from you.\n\n"
            "I\u2019ve been watching this account and a few things are converging that I think warrant your "
            "attention today, not this week.\n\n"
            "They haven\u2019t logged in for 11 days now. Their last three support tickets were all submitted "
            "by the same person \u2014 Dana\u2019s manager, which is unusual. Dana has been your primary "
            "contact for 18 months and she\u2019s gone quiet. The tickets themselves are minor but the pattern "
            "of who is submitting them suggests something may have shifted internally.\n\n"
            "Their renewal is in 45 days. In my experience with accounts at this signal level, the "
            "conversation gets significantly harder if it isn\u2019t initiated before the 30 day mark.\n\n"
            "I don\u2019t think this is a lost account. I think this is an account that needs to hear from "
            "you today \u2014 not with an agenda, just with presence.\n\n"
            "I drafted a message to Dana. It\u2019s brief. It references a relevant product update that\u2019s "
            "genuinely useful for their team. It doesn\u2019t mention the renewal. It just reopens the door.\n\n"
            "I\u2019d also recommend logging a risk CTA in Gainsight so this account is formally on your "
            "team\u2019s radar. I can do that at the same time. Want me to send the message to Dana and log "
            "the CTA now? I\u2019d do both before 3pm."
        ),
    }


@app.post("/northgate/alert/confirm")
async def northgate_alert_confirm(request: Request):
    """
    Scene 5 confirmation: dispatches based on action field.
    action: "send_dana" | "log_cta" | "both" (default: "both")
    """
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    action = (body.get("action") or "both").strip()

    result_parts = []
    response: dict = {"confirmed": True}

    if action in ("send_dana", "both"):
        response["draft_to_dana"] = (
            "Hi Dana \u2014 wanted to reach out. I noticed it\u2019s been a couple of weeks since your team "
            "has been active on the platform, and I know Q1 can get hectic. No agenda \u2014 just wanted to "
            "make sure everything is running smoothly on your end. Happy to jump on a quick call this week "
            "if that\u2019s useful."
        )
        result_parts.append("Message sent to Dana")

    if action in ("log_cta", "both"):
        result_parts.append("Risk CTA logged in Gainsight")

    response["result"] = ". ".join(result_parts) + "." if result_parts else "No action taken."
    return response


@app.get("/priorities/midday")
def priorities_midday():
    """Scene 4 — mid-day priority list. Static, no LLM."""
    return {
        "message": (
            "Three things, in order of urgency.\n\n"
            "First \u2014 Meridian. Jamie on the product team needs a committed SLA update from you before end of day. "
            "You made that commitment on the call. I\u2019ve drafted the note \u2014 ready to send.\n\n"
            "Second \u2014 the QBR deck. Marcus confirmed March 19th. He wants the power user adoption section included. "
            "I can start building it now while you\u2019re in your afternoon calls.\n\n"
            "Third \u2014 Northgate. I flagged them this morning and I don\u2019t want them to get lost in the Meridian focus today. "
            "They haven\u2019t logged in for 11 days and their renewal is in 45 days. "
            "I\u2019ve drafted a brief check-in to Dana \u2014 nothing heavy, just re-establishing presence. Want to see it?\n\n"
            "You have time for all three. Want me to move on all of them?"
        )
    }


@app.get("/eod/summary")
def eod_summary():
    """
    Scene 6: structured end-of-day summary for the demo.
    Returns a pre-built message field plus key signal fields. Demo-stable:
    account list and copy are hardcoded (System #1 retirement, Batch B).
    """
    accounts_touched = [
        "Meridian Health",
        "Northgate Logistics",
        "Beacon Financial",
        "Sutton Retail",
        "Pinnacle Group",
    ]
    northgate_status = "Message sent to Dana at 2:43pm. Risk CTA logged in Gainsight."
    pinnacle_flag    = (
        "Support ticket PG-0887 open \u2014 reporting dashboard slow to load. "
        "Call tomorrow at 10am. Unresolved."
    )

    message = (
        "Sarah, here\u2019s how your day closed out.\n\n"
        "Meridian \u2014 strong call. Product follow-up sent to Jamie. QBR deck structure built, "
        "power user adoption section outlined. Recap sent, timeline entry logged in Gainsight.\n\n"
        "Northgate \u2014 outreach sent to Dana at 2:43pm. Risk CTA logged and flagged for your team.\n\n"
        "Beacon and Sutton \u2014 both calls clean. Recaps sent, everything logged.\n\n"
        "One thing I want to flag for tomorrow. You have a call with Pinnacle Group at 10am. They submitted a support ticket this afternoon \u2014 their reporting dashboard has been slow to load for three days and it hasn\u2019t been resolved yet. I\u2019ve drafted a note to the support team requesting an update. It\u2019s ready to send tomorrow morning.\n\n"
        "You closed 4 CTAs today, sent 3 customer-facing communications, and kept every commitment you made. Great work today."
    )

    return {
        "accounts_touched": accounts_touched,
        "northgate_status": northgate_status,
        "pinnacle_flag":    pinnacle_flag,
        "message":          message,
    }


# ── Scene 3: Post-call recap ───────────────────────────────────────────────

def _recap_narrative(entry: dict) -> str:
    """Build the conversational prose recap from a store.json summary entry."""
    account = entry.get("account", "Meridian Health")

    # Extract date: prefer filename (e.g. meridian-health-2026-02-26.txt) over created_at
    date_str = ""
    filename = entry.get("filename", "")
    if filename:
        stem = filename.replace(".txt", "").replace(".md", "")
        parts = stem.split("-")
        if len(parts) >= 3:
            try:
                from datetime import date as _date
                d = _date(int(parts[-3]), int(parts[-2]), int(parts[-1]))
                date_str = d.strftime("%B %-d, %Y")
            except Exception:
                pass
    if not date_str:
        raw = (entry.get("created_at") or "")[:10]
        if raw:
            try:
                from datetime import datetime as _dt
                date_str = _dt.fromisoformat(raw).strftime("%B %-d, %Y")
            except Exception:
                date_str = raw

    # Overall read: prefer transcript processor field, fall back to summary
    read = entry.get("overall_read") or entry.get("summary", "")

    # Commitments: prefer transcript processor field, fall back to ctas
    commitments = list(entry.get("commitments") or [])
    if not commitments:
        for c in (entry.get("ctas") or []):
            commitments.append({
                "owner": c.get("owner", "CSM"),
                "title": c.get("title", ""),
                "due_date": c.get("due_date", ""),
            })

    lines = [
        "That sounded like a productive one. Here\u2019s what I captured.",
        "",
        f"{account} \u2014 Call Recap \u2014 {date_str}",
        "",
        read,
        "",
    ]

    if commitments:
        n = len(commitments)
        count = {1: "One thing", 2: "Two things", 3: "Three things", 4: "Four things"}.get(n, f"{n} things")
        lines.append(f"{count} were committed to coming out of this call:")
        for c in commitments:
            owner = c.get("owner", "")
            title = c.get("title", "")
            due = (c.get("due_date") or "").strip()
            line = f"\u2022 {owner} \u2014 {title}"
            if due:
                try:
                    from datetime import datetime as _dt
                    due_fmt = _dt.strptime(due, "%Y-%m-%d").strftime("%b %-d")
                except Exception:
                    due_fmt = due
                line += f" (by {due_fmt})"
            lines.append(line)
        lines.append("")

    lines.append(
        "I\u2019ve drafted the follow-up email to Marcus summarizing the call and confirming next steps. "
        "I\u2019ve also created a CTA for the product update commitment due Friday."
    )
    lines.append("")
    lines.append("Want me to send the recap to Marcus and log a timeline entry in Gainsight?")

    return "\n".join(lines)


@app.get("/recap/meridian")
def recap_meridian():
    """
    Scene 3: returns a conversational prose recap of the most recent
    Meridian Health call summary stored in store.json.
    """
    store = load_store()
    summaries = store.get("summaries", [])
    meridian = [s for s in summaries if s.get("account") == "Meridian Health"]
    if not meridian:
        return JSONResponse({"error": "No Meridian Health summary found in store"}, status_code=404)

    # Prefer transcript-processor entries (have overall_read + commitments) over notes/summarize
    transcript_entries = [s for s in meridian if s.get("source") == "transcript"]
    entry = transcript_entries[-1] if transcript_entries else meridian[-1]
    return {
        "account": "Meridian Health",
        "summary_id": entry.get("id"),
        "message": _recap_narrative(entry),
    }


@app.post("/recap/meridian/confirm")
def recap_meridian_confirm():
    """
    Scene 3 confirmation: simulates sending the recap email to Marcus and
    logging a Gainsight timeline entry.
    """
    return {
        "ok": True,
        "message": "Done. Recap sent to Marcus. Timeline entry logged in Gainsight.",
    }


# ── Auth helper ───────────────────────────────────────────────────────────────

def _login_html(error: str = "") -> str:
    error_block = (
        f'<p style="color:#c0392b;font-size:14px;margin-top:0">{error}</p>'
        if error else ""
    )
    return f"""<!DOCTYPE html>
<html>
<head>
  <title>CS Agent — Sign in</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f5f5f5;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
    }}
    .card {{
      background: white;
      border-radius: 12px;
      padding: 48px;
      width: 100%;
      max-width: 400px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.08), 0 4px 16px rgba(0,0,0,0.06);
    }}
    .logo {{
      font-size: 15px;
      font-weight: 600;
      letter-spacing: -0.01em;
      color: #1a1a1a;
      margin-bottom: 32px;
    }}
    h1 {{
      font-size: 22px;
      font-weight: 600;
      color: #1a1a1a;
      margin-bottom: 8px;
    }}
    .subtitle {{
      font-size: 14px;
      color: #888;
      margin-bottom: 32px;
    }}
    label {{
      display: block;
      font-size: 13px;
      font-weight: 500;
      color: #444;
      margin-bottom: 6px;
    }}
    input {{
      width: 100%;
      padding: 10px 14px;
      border: 1px solid #e0e0e0;
      border-radius: 8px;
      font-size: 15px;
      color: #1a1a1a;
      margin-bottom: 20px;
      outline: none;
      transition: border-color 0.15s;
    }}
    input:focus {{ border-color: #1a1a1a; }}
    button {{
      width: 100%;
      padding: 12px;
      background: #1a1a1a;
      color: white;
      border: none;
      border-radius: 8px;
      font-size: 15px;
      font-weight: 500;
      cursor: pointer;
      margin-top: 4px;
    }}
    button:hover {{ background: #333; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">CS Agent</div>
    <h1>Sign in</h1>
    <p class="subtitle">Your AI colleague is ready.</p>
    {error_block}
    <form method="POST" action="/login">
      <label for="csm_id">Username</label>
      <input type="text" id="csm_id" name="csm_id"
             placeholder="your_csm_id" autocomplete="username" required>
      <label for="password">Password</label>
      <input type="password" id="password" name="password"
             autocomplete="current-password" required>
      <button type="submit">Sign in</button>
    </form>
  </div>
</body>
</html>"""


# ── Product routes (Phase 6.1) ────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if get_session(token):
        return RedirectResponse(url="/app", status_code=302)
    return HTMLResponse(content=_login_html(), status_code=200)


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    csm_id = str(form.get("csm_id", "")).strip().lower()
    password = str(form.get("password", ""))

    if not csm_id or not validate_credentials(csm_id, password):
        return HTMLResponse(
            content=_login_html(error="Invalid credentials. Please try again."),
            status_code=401,
        )

    name = get_csm_name(csm_id)
    email = get_csm_email(csm_id)
    token = create_session(csm_id, name, email)

    response = RedirectResponse(url="/app", status_code=302)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        max_age=86400,
        samesite="lax",
        secure=True,
    )
    return response


@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    delete_session(token)
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.api_route("/app", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def app_page():
    # Auth is handled by Clerk on the frontend — no session check here.
    react_index = Path("frontend/dist/index.html")
    if react_index.exists():
        from fastapi.responses import FileResponse as _FR
        return _FR(str(react_index))
    return HTMLResponse(content=Path("product/static/app.html").read_text(encoding="utf-8"), status_code=200)


@app.get("/app/session")
async def app_session(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    session = get_session(token)
    if not session:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    return JSONResponse({"csm_id": session["csm_id"], "name": session["name"], "email": session.get("email", "")})


# ── First-run welcome wrapper + helper chips (serving layer only) ───────────
# Wraps a brand-new user's FIRST daily briefing with a short honest welcome
# note and three fixed tappable chips. Applied at /app/today-digest only,
# first-run only, and gated on DIGEST_GENERATOR_ENABLED so this deploy is a
# runtime no-op until the canary flip. sarah_c, flag-off, and any non-first-
# run user all stay byte-identical ({"digest": text}, no "chips" key).

_FIRST_RUN_CHIPS = [
    "What should I focus on first today?",
    "Show me my at-risk accounts",
    "Which renewals are coming up?",
]
_CRM_DISPLAY_NAME = {"hubspot": "HubSpot", "gainsight": "Gainsight"}


def _is_first_run_digest(csm_id: str) -> bool:
    """True iff this user has no prior successful digest before today.

    sarah_c is never first-run (demo regression guard). Any DB error or
    missing config falls back to False (no wrapper -> legacy behavior).
    """
    if is_demo_csm_id(csm_id):
        return False
    try:
        from product.database import SessionLocal as _SL, DigestLog as _DL
        if not _SL:
            return False
        _today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _s = _SL()
        try:
            prior = (
                _s.query(_DL.id)
                .filter(_DL.customer_id == csm_id,
                        _DL.status == "success",
                        _DL.date < _today)
                .first()
            )
            return prior is None
        finally:
            _s.close()
    except Exception:
        return False


def _resolve_crm_type_for_wrapper(csm_id: str) -> str:
    try:
        from product.context_cache import get_cached_context
        c = get_cached_context(csm_id)
        if c:
            return (c.get("data", {}) or {}).get("crm_type", "") or ""
    except Exception:
        pass
    return ""


def _first_run_wrapper(digest_text: str, crm_type: str) -> str:
    from product.digest.generator import _cta_label
    label = _cta_label(crm_type)
    crm_name = _CRM_DISPLAY_NAME.get((crm_type or "").strip().lower(), "")
    seen = f"your week ahead and your open {label}"
    if crm_name:
        seen += f" from {crm_name}"
    intro = (
        "Welcome to CS Agent. This is your first daily briefing.\n\n"
        f"Right now I can see {seen}. Over the next few days, as I learn "
        "your book, I'll start surfacing the quieter signals too: who's "
        "gone quiet, which relationships are cooling, and where to get "
        "ahead of a renewal before it's urgent.\n\n"
        "Here's your day:\n\n"
    )
    return intro + digest_text


def _maybe_first_run(csm_id: str, digest_text: str):
    """Return (text, chips). chips is None unless this is a first-run serve
    under the enabled flag, in which case text is wrapped and chips is the
    fixed list. Flag-gated so deploy is a no-op until the canary flip.
    """
    if os.environ.get("DIGEST_GENERATOR_ENABLED", "false").lower() != "true":
        return digest_text, None
    if not _is_first_run_digest(csm_id):
        return digest_text, None
    crm_type = _resolve_crm_type_for_wrapper(csm_id)
    return _first_run_wrapper(digest_text, crm_type), list(_FIRST_RUN_CHIPS)


@app.get("/app/today-digest")
async def app_today_digest(request: Request, background_tasks: BackgroundTasks):
    _session = await _require_active_subscription(request)
    csm_id = _validate_csm_id(_session["csm_id"])

    # Trigger context cache refresh if stale or missing (non-blocking)
    try:
        from product.context_cache import get_cached_context
        _ctx = get_cached_context(csm_id)
        _stale = True
        if _ctx and _ctx.get("refreshed_at"):
            _age = (datetime.now(timezone.utc) - _ctx["refreshed_at"].replace(tzinfo=timezone.utc if _ctx["refreshed_at"].tzinfo is None else _ctx["refreshed_at"].tzinfo)).total_seconds()
            _stale = _age > 3600  # 60 min — matches /chat cache threshold
        if _stale:
            from product.queue import is_redis_available, enqueue
            if is_redis_available():
                await enqueue("job_refresh_user_context", csm_id, _dedup_key=f"refresh:{csm_id}")
            logger.info("Refresh enqueued for %s", csm_id)
    except Exception as _cc_err:
        logger.warning("Refresh trigger failed (non-fatal): %s", _cc_err)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # PRIMARY: Check Postgres (shared across web + worker containers)
    try:
        from product.database import SessionLocal as _DigestSL, DigestLog as _DigestLog
        if _DigestSL:
            _ddb = _DigestSL()
            try:
                _digest_row = (
                    _ddb.query(_DigestLog)
                    .filter_by(customer_id=csm_id, date=today, status="success")
                    .filter(_DigestLog.digest_text.isnot(None))
                    .filter(_DigestLog.digest_text != "")
                    .order_by(_DigestLog.created_at.desc())
                    .first()
                )
                if _digest_row and _digest_row.digest_text:
                    _wrapped, _chips = _maybe_first_run(csm_id, _digest_row.digest_text)
                    if _chips is not None:
                        return JSONResponse({"digest": _wrapped, "chips": _chips})
                    return JSONResponse({"digest": _wrapped})
            finally:
                _ddb.close()
    except Exception as _ddb_err:
        logger.warning("today-digest Postgres read failed: %s", _ddb_err)

    # FALLBACK: Check local filesystem (admin/trigger-digest, backward compat)
    digest_path = _safe_data_path("product/data/digests", csm_id, f"{today}.json")
    if digest_path.exists():
        try:
            data = json.loads(digest_path.read_text(encoding="utf-8"))
            _text = data.get("digest_text", "")
            if _text:
                _wrapped, _chips = _maybe_first_run(csm_id, _text)
                if _chips is not None:
                    return JSONResponse({"digest": _wrapped, "chips": _chips})
                return JSONResponse({"digest": _wrapped})
        except Exception:
            pass

    # NO DIGEST FOUND — check if user has completed onboarding
    _has_accounts = False
    try:
        _acct_file = _safe_data_file("product/data/accounts", csm_id)
        if _acct_file.exists():
            _acct_data = json.loads(_acct_file.read_text(encoding="utf-8"))
            _has_accounts = bool(_acct_data.get("accounts"))
    except Exception:
        pass
    if not _has_accounts:
        try:
            from product.database import Customer as _C, SessionLocal as _SL
            if _SL:
                _db = _SL()
                try:
                    _c = _db.query(_C).filter_by(clerk_user_id=csm_id).first()
                    _has_accounts = bool(_c and _c.accounts)
                finally:
                    _db.close()
        except Exception:
            pass

    if not _has_accounts:
        return JSONResponse({"digest": None})

    # ENQUEUE: Use Redis SET NX for atomic dedup (prevents duplicate jobs across containers)
    _should_enqueue = False
    try:
        from product.queue import _get_pool
        pool = await _get_pool()
        if pool:
            _claimed = await pool.set(f"digest:pending:{csm_id}:{today}", "1", nx=True, ex=300)
            _should_enqueue = bool(_claimed)
        else:
            _should_enqueue = True  # No Redis = always enqueue (BackgroundTasks fallback)
    except Exception:
        _should_enqueue = True  # Redis error = fail-open, enqueue anyway

    if not _should_enqueue:
        return JSONResponse({"status": "generating", "message": "Generating your daily briefing now..."})

    # Also set local fast-path flag (prevents redundant Redis checks within same process)
    _digest_in_flight[f"{csm_id}:{today}"] = time.time()

    from product.queue import is_redis_available
    if is_redis_available():
        from product.queue import enqueue
        await enqueue("job_generate_digest", csm_id, _dedup_key=f"digest:{csm_id}:{today}")
    else:
        background_tasks.add_task(_run_digest_for_user, csm_id)

    return JSONResponse({"status": "generating", "message": "Generating your daily briefing now..."})


@app.get("/app/subscription-status")
async def app_subscription_status(request: Request):
    """Return the current user's subscription status for frontend gate logic.

    Auth-required but NOT gated. All fields read directly from the DB row.
    No caching. No fallbacks. No computed status.
    """
    _session = await _resolve_auth(request)
    if not _session:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    csm_id = _session["csm_id"]

    # Demo bypass, and BILLING_ENABLED off: never gated, no Stripe customer
    if is_demo_csm_id(csm_id) or not billing_enabled():
        return JSONResponse({
            "status": "active",
            "is_gated": False,
            "trial_ends_at": None,
            "has_stripe_customer": False,
        })

    from product.subscription import get_customer_by_clerk_id
    cust = get_customer_by_clerk_id(csm_id)

    if cust is None:
        # No customer row — shouldn't happen after first auth, but handle defensively
        return JSONResponse({
            "status": "canceled",
            "is_gated": True,
            "trial_ends_at": None,
            "has_stripe_customer": False,
        })

    status = cust["subscription_status"]
    trial_ends = cust["trial_ends_at"]
    is_comp = bool(cust.get("is_comp_account"))
    trial_ends_iso = trial_ends.isoformat().replace("+00:00", "Z") if trial_ends else None

    from product.subscription import is_subscription_active
    return JSONResponse({
        "status": status,
        "is_gated": not is_subscription_active(status, trial_ends, is_comp),
        "trial_ends_at": trial_ends_iso,
        "has_stripe_customer": bool(cust["stripe_customer_id"]),
    })


@app.get("/app/settings/google-actions")
async def app_settings_google_actions_get(request: Request):
    """Return current Google-actions toggle state for the authenticated user.

    Subscription-gated. enabled reads Customer.google_actions_enabled.
    scope_granted checks the user's Google token for gmail.send (see
    _token_has_write_scopes).
    Fail-closed on DB error: treats as disabled (never accidentally enabled).
    """
    _session = await _require_active_subscription(request)
    csm_id = _session["csm_id"]

    # sarah_c has no Customer row; return a safe demo echo.
    if is_demo_csm_id(csm_id):
        return JSONResponse({
            "enabled": False,
            "scope_granted": _token_has_write_scopes(csm_id),
        })

    enabled = False
    try:
        from product.database import SessionLocal, Customer
        if SessionLocal:
            db = SessionLocal()
            try:
                cust = db.query(Customer).filter_by(clerk_user_id=csm_id).first()
                if cust and cust.google_actions_enabled:
                    enabled = True
            finally:
                db.close()
    except Exception:
        pass  # DB failure → treat as disabled (fail-closed)

    return JSONResponse({
        "enabled": enabled,
        "scope_granted": _token_has_write_scopes(csm_id),
    })


@app.post("/app/settings/google-actions")
async def app_settings_google_actions_post(request: Request):
    """Toggle Google-actions on/off for the authenticated user.

    Body: {"enabled": bool}

    On enable=True: requires gmail.send in the stored token. If missing,
    returns 409 with a populated auth_url to the scope-upgrade re-auth
    endpoint.
    On enable=False: flips the flag; always succeeds.
    """
    _session = await _require_active_subscription(request)
    csm_id = _session["csm_id"]

    body = await request.json()
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(
            status_code=400,
            detail="Missing or invalid 'enabled' (must be boolean)",
        )

    # sarah_c has no Customer row; demo bypass returns echoed state.
    # Known asymmetry: sarah_c can "enable" without the scopes because
    # the bypass skips the scope check. Not a leak — demo UI has no toggle.
    if is_demo_csm_id(csm_id):
        return JSONResponse({
            "enabled": enabled,
            "scope_granted": _token_has_write_scopes(csm_id),
        })

    if enabled and not _token_has_write_scopes(csm_id):
        return JSONResponse(
            {
                "error": "scope_required",
                "auth_url": f"/auth/google/start?scope=write&user_id={csm_id}",
            },
            status_code=409,
        )

    try:
        from product.database import SessionLocal, Customer
        if not SessionLocal:
            raise HTTPException(status_code=503, detail="Database not configured")
        db = SessionLocal()
        try:
            cust = db.query(Customer).filter_by(clerk_user_id=csm_id).first()
            if not cust:
                raise HTTPException(status_code=404, detail="Customer not found")
            cust.google_actions_enabled = enabled
            db.commit()
        finally:
            db.close()
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("google-actions toggle persist failed for %s: %s", csm_id, exc)
        raise HTTPException(status_code=500, detail="Could not update setting")

    return JSONResponse({
        "enabled": enabled,
        "scope_granted": _token_has_write_scopes(csm_id),
    })


@app.post("/app/settings/delete-my-data")
async def app_settings_delete_my_data(request: Request):
    """Self-service hard-delete of the calling user's own data + Clerk identity.

    Fulfills the privacy-policy "Delete my data" promise (Section 8): an
    authenticated user can purge all of their data at any time.

    Ordering is load-bearing. verify_clerk_token re-provisions a fresh
    trialing customers row on EVERY authenticated request (via
    provision_customer_on_first_auth). So a purge that only deletes rows is
    silently undone on the user's next request. We therefore:
      1. purge Postgres + disk data FIRST (the promise), then
      2. delete the Clerk identity LAST, so the JWT the user still holds can
         no longer re-provision.
    If the Clerk delete fails after the purge, the data is already gone (the
    promise is kept); we alert the founder and return a distinct status so
    the frontend still signs the user out.

    Identity comes from _require_jwt_identity — the RAW Clerk user_id, NOT
    _require_active_subscription. _require_active_subscription remaps legacy
    org_config CSMs to a slug that would not match the customers row;
    purge_user_data resolves the root row by the literal clerk_user_id.

    Body: {"confirm": true}  (exact boolean — anything else is a 400)

    Responses:
      400 — confirm missing or not exactly true (nothing touched)
      403 — comp account, or sarah_c demo identity (nothing touched)
      503 — database not configured (nothing touched)
      500 — purge raised and rolled back; nothing deleted, Clerk untouched
      200 {"status": "deleted"} — data purged + Clerk identity removed
      200 {"status": "data_deleted_clerk_failed"} — data purged, Clerk
          delete failed; founder alerted, frontend still signs user out
    """
    clerk_user_id = await _require_jwt_identity(request)

    # 1. Explicit confirmation gate — exact boolean True only.
    try:
        body = await request.json()
    except Exception:
        body = {}
    if body.get("confirm") is not True:
        raise HTTPException(
            status_code=400, detail="Deletion requires explicit confirm=true"
        )

    # 2. Comp-account refusal. purge_user_data only guards sarah_c, so the
    #    comp-account canary is protected here, at the self-service surface.
    from product.database import SessionLocal, Customer, purge_user_data
    if SessionLocal is None:
        raise HTTPException(status_code=503, detail="Database not configured")
    db = SessionLocal()
    try:
        cust = db.query(Customer).filter_by(clerk_user_id=clerk_user_id).first()
        is_comp = bool(cust.is_comp_account) if cust else False
    finally:
        db.close()
    if is_comp:
        logger.warning(
            "delete-my-data refused for comp account %s", clerk_user_id
        )
        raise HTTPException(
            status_code=403, detail="Comp accounts cannot be self-deleted"
        )

    # 3. Purge data FIRST.
    result = purge_user_data(clerk_user_id)
    status = result.get("status")
    if status == "failed":
        logger.error(
            "event=delete_my_data_purge_failed clerk_user_id=%s error=%s",
            clerk_user_id, result.get("error"),
        )
        raise HTTPException(
            status_code=500, detail="Data deletion failed; nothing was deleted"
        )
    if status == "not_purgeable" and result.get("reason") == "sarah_c":
        # Defensive — not reachable via a real Clerk JWT.
        raise HTTPException(
            status_code=403, detail="This account cannot be deleted"
        )
    # status == "purged", or "not_purgeable"/"no_customer" (data already
    # absent) → both proceed to Clerk deletion. Any other not_purgeable
    # reason (e.g. no_db, despite the guard above) is treated as a failure.
    if status not in ("purged",) and not (
        status == "not_purgeable" and result.get("reason") == "no_customer"
    ):
        logger.error(
            "event=delete_my_data_purge_unexpected clerk_user_id=%s result=%s",
            clerk_user_id, result,
        )
        raise HTTPException(
            status_code=500, detail="Data deletion failed; nothing was deleted"
        )

    # 4. Delete the Clerk identity LAST.
    from product.auth_clerk import delete_clerk_user
    clerk_deleted = delete_clerk_user(clerk_user_id)
    if clerk_deleted:
        logger.info(
            "event=delete_my_data_complete clerk_user_id=%s data_status=%s clerk_deleted=True",
            clerk_user_id, status,
        )
        return JSONResponse({"status": "deleted"})

    # Data is already gone — the promise was kept. Alert the founder (same
    # mechanism the cancel-sweep wrapper _purge_canceled_users uses) and
    # return a distinct status so the frontend still signs the user out.
    logger.error(
        "event=delete_my_data_clerk_delete_failed clerk_user_id=%s data_status=%s",
        clerk_user_id, status,
    )
    _send_founder_alert(
        "delete_my_data_clerk_delete_failed",
        clerk_user_id,
        Exception("Clerk user delete returned False after data purge"),
        "",
    )
    return JSONResponse({"status": "data_deleted_clerk_failed"})


_DISCONNECT_CRM_PROVIDERS = ("hubspot", "gainsight")


@app.post("/app/settings/disconnect-crm")
async def app_settings_disconnect_crm(request: Request):
    """Self-service disconnect of one CRM (HubSpot or Gainsight).

    Fulfills the privacy-policy promise that a user can disconnect a CRM at any
    time. Deletes the provider's stored token row and — per the crm-match rule
    in database.disconnect_crm — the CRM-derived context snapshot when that
    snapshot was built from this CRM. Reversible: the user can reconnect later
    via the normal OAuth flow. This is NOT a CASA surface — it removes a stored
    token row, it does not alter the OAuth grant configuration.

    Identity comes from _require_jwt_identity — the RAW Clerk user_id, the same
    namespace under which the CRM token is stored (matches delete-my-data).
    Deliberately no comp-account guard: disconnect is reversible.

    Body: {"provider": "hubspot" | "gainsight", "confirm": true}

    Responses:
      400 — provider not in the allowlist (incl. "google"), or confirm != true
      503 — database not configured (nothing touched)
      500 — disconnect raised unexpectedly; the transaction rolled back so
            nothing partial persists
      200 {"status": "disconnected", "token_deleted": bool,
           "snapshot_deleted": bool}
    """
    clerk_user_id = await _require_jwt_identity(request)

    try:
        body = await request.json()
    except Exception:
        body = {}

    provider = body.get("provider")
    if provider not in _DISCONNECT_CRM_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"provider must be one of {list(_DISCONNECT_CRM_PROVIDERS)}",
        )
    if body.get("confirm") is not True:
        raise HTTPException(
            status_code=400, detail="Disconnect requires explicit confirm=true"
        )

    from product.database import SessionLocal, disconnect_crm
    if SessionLocal is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    try:
        result = disconnect_crm(clerk_user_id, provider)
    except Exception as exc:
        # disconnect_crm rolls back its own transaction on any DB error and
        # never raises out except on a bad provider (allowlisted above). Any
        # exception here means nothing partial persisted.
        logger.error(
            "event=disconnect_crm_unexpected clerk_user_id=%s provider=%s error=%s",
            clerk_user_id, provider, exc,
        )
        raise HTTPException(
            status_code=500, detail="Disconnect failed; nothing was changed"
        )

    return JSONResponse({
        "status": "disconnected",
        "token_deleted": result["token_deleted"],
        "snapshot_deleted": result["snapshot_deleted"],
    })


# ── C.1 Commit 7: post-call nudge cadence preference ────────────────────────
# Gates the transparent-nudge flow C in product/postcall/scheduler.py.
# Does not affect flow A (Drive transcript found) or flow B-skip (CRM
# already logged the call). Deliberately no sarah_c bypass — demo flows
# run through /demo, not /app/settings; the natural "no Customer row →
# default 15" path handles any edge case where sarah_c hits this.

_NUDGE_CADENCE_DEFAULT = 15
_NUDGE_CADENCE_VALID = (5, 15, 30, 60)


@app.get("/app/settings/nudge-cadence")
async def app_settings_nudge_cadence_get(request: Request):
    """Return the current post-call nudge cadence for the authenticated CSM.

    Subscription-gated. Null column, missing Customer row, or unrecognized
    value → returns 15 (the default). DB failures also return 15 so the
    settings UI stays responsive.
    """
    _session = await _require_active_subscription(request)
    csm_id = _session["csm_id"]

    cadence = _NUDGE_CADENCE_DEFAULT
    try:
        from product.database import SessionLocal, Customer
        if SessionLocal:
            db = SessionLocal()
            try:
                cust = db.query(Customer).filter_by(clerk_user_id=csm_id).first()
                if cust and cust.postcall_nudge_cadence_minutes in _NUDGE_CADENCE_VALID:
                    cadence = int(cust.postcall_nudge_cadence_minutes)
            finally:
                db.close()
    except Exception as exc:
        logger.warning("nudge-cadence GET failed for %s: %s — returning default", csm_id, exc)

    return JSONResponse({"cadence_minutes": cadence})


@app.post("/app/settings/nudge-cadence")
async def app_settings_nudge_cadence_post(request: Request):
    """Persist the CSM's post-call nudge cadence.

    Body: {"cadence_minutes": 5 | 15 | 30 | 60}
    Rejects any other value with 400. When the user explicitly picks 15
    we persist 15 (not null) so future default changes don't silently
    move them.
    """
    _session = await _require_active_subscription(request)
    csm_id = _session["csm_id"]

    body = await request.json()
    raw = body.get("cadence_minutes")
    if not isinstance(raw, int) or isinstance(raw, bool) or raw not in _NUDGE_CADENCE_VALID:
        raise HTTPException(
            status_code=400,
            detail=f"cadence_minutes must be one of {list(_NUDGE_CADENCE_VALID)}",
        )

    try:
        from product.database import SessionLocal, Customer
        if not SessionLocal:
            raise HTTPException(status_code=503, detail="Database not configured")
        db = SessionLocal()
        try:
            cust = db.query(Customer).filter_by(clerk_user_id=csm_id).first()
            if not cust:
                raise HTTPException(status_code=404, detail="Customer not found")
            cust.postcall_nudge_cadence_minutes = raw
            db.commit()
        finally:
            db.close()
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("nudge-cadence POST persist failed for %s: %s", csm_id, exc)
        raise HTTPException(status_code=500, detail="Could not update setting")

    return JSONResponse({"cadence_minutes": raw})


_CONNECTION_STATUS_PROVIDERS = ("gmail", "calendar", "hubspot", "gainsight", "slack", "zoom")
_CONNECTION_STATUS_STALE_MINUTES = 120  # 2 hours = two eager-probe cycles

_DB_STATUS_TO_PROBE_STATE = {
    "active":  "ok",
    "expired": "expired",
    "missing": "missing",
}


@app.get("/app/connection-status")
async def app_connection_status(request: Request):
    """Per-provider token state for UI banners and reconnect links.

    Returns one of "ok" | "expired" | "missing" | "error" per provider.
    G.2 Stage 2: served from Connection.status / Connection.last_verified_at
    (populated by the eager hourly probe scheduler — see
    product/worker.job_probe_all_connections). Avoids running an inline
    probe per request.

    Stale-fallback (defense-in-depth): if any provider with a row has
    no last_verified_at, or one older than 2 hours, fall back to a live
    auth_probe.probe_all and fire-and-forget a write_connection_state
    so the next request is DB-backed again. Logs the degraded path
    (event=connection_status_stale_fallback) — that log signal means
    the eager scheduler needs attention.
    """
    _session = await _require_active_subscription(request)
    csm_id = _validate_csm_id(_session["csm_id"])

    from product.connection_service import verify_connection_live

    try:
        wire, db_meta = await asyncio.to_thread(
            _connection_status_from_db, csm_id, verify_connection_live,
        )
    except Exception as exc:
        logger.warning("connection-status DB read failed for %s: %s", csm_id, exc)
        wire, db_meta = None, None

    no_data = wire is None or _connection_status_no_present_rows(db_meta)
    is_stale = (db_meta is not None) and _connection_status_is_stale(db_meta)

    if no_data or is_stale:
        from product.auth_probe import probe_all
        from product.connection_writer import write_connection_state

        if is_stale:
            # Degraded-path signal: the eager scheduler should have kept
            # DB state fresh. If we're here, something is wrong with it.
            logger.info(
                "event=connection_status_stale_fallback csm_id=%s staleness_minutes=%s",
                csm_id, _connection_status_max_age_minutes(db_meta),
            )
        try:
            live = await asyncio.to_thread(probe_all, csm_id)
        except Exception as exc:
            logger.warning("connection-status live probe failed for %s: %s", csm_id, exc)
            return JSONResponse({p: "error" for p in _CONNECTION_STATUS_PROVIDERS})

        # Best-effort persist for the next request; do not block this response.
        if is_stale:
            asyncio.create_task(write_connection_state(csm_id))
        return JSONResponse({p: live.get(p, "missing") for p in _CONNECTION_STATUS_PROVIDERS})

    return JSONResponse(wire)


def _connection_status_from_db(csm_id: str, verify_fn) -> tuple[dict, dict]:
    """Return (wire_response, db_meta).

    wire_response is the flat {provider: probe_state} dict the frontend
    expects. db_meta carries last_verified_age_minutes and presence flags
    per provider so the staleness check can inspect them without a
    second DB roundtrip.
    """
    wire: dict[str, str] = {}
    db_meta: dict[str, dict] = {}
    for provider in _CONNECTION_STATUS_PROVIDERS:
        result = verify_fn(csm_id, provider) or {}
        db_status = result.get("status", "missing")
        wire[provider] = _DB_STATUS_TO_PROBE_STATE.get(db_status, "error")
        db_meta[provider] = {
            "present": db_status != "missing",
            "age_minutes": result.get("last_verified_age_minutes"),
        }
    return wire, db_meta


def _connection_status_no_present_rows(db_meta: dict | None) -> bool:
    """True when the user has zero rows in connections — DB has nothing
    to serve, so fall back to a live probe (cheap for users with no
    tokens; covers the brand-new-user and demo-account cases)."""
    if not db_meta:
        return True
    return not any(
        (db_meta.get(p) or {}).get("present")
        for p in _CONNECTION_STATUS_PROVIDERS
    )


def _connection_status_is_stale(db_meta: dict) -> bool:
    """A user with at least one present row is stale if any present row
    has either no last_verified_at or one older than the 2-hour threshold.
    Triggers a degraded-path log so the eager scheduler health is
    observable from the request path."""
    for provider in _CONNECTION_STATUS_PROVIDERS:
        meta = db_meta.get(provider) or {}
        if not meta.get("present"):
            continue
        age = meta.get("age_minutes")
        if age is None or age > _CONNECTION_STATUS_STALE_MINUTES:
            return True
    return False


def _connection_status_max_age_minutes(db_meta: dict | None) -> int | None:
    if not db_meta:
        return None
    ages = [
        (db_meta.get(p) or {}).get("age_minutes")
        for p in _CONNECTION_STATUS_PROVIDERS
        if (db_meta.get(p) or {}).get("present")
    ]
    ages = [a for a in ages if a is not None]
    return max(ages) if ages else None


@app.get("/app/accounts")
async def app_accounts(request: Request):
    _session = await _require_active_subscription(request)
    csm_id = _validate_csm_id(_session["csm_id"])

    # Path 1: org_config user (sarah_c and other registered CSMs)
    from product.org_config import try_get_csm_config as _try_get_csm_cfg, get_crm_config as _get_crm_cfg
    _csm_cfg = _try_get_csm_cfg(csm_id)
    if _csm_cfg:
        try:
            _crm_cfg  = _get_crm_cfg()
            _crm_type = _crm_cfg.get("crm_type", "")
            _csm_accounts = _csm_cfg.get("accounts", "auto")

            if _crm_type:
                from product.connectors.registry import get_connector as _get_connector
                connector    = _get_connector(csm_id, {"crm": _crm_type})
                accounts_raw = connector.get_accounts()
                health_raw   = connector.get_health_scores()
                renewals_raw = connector.get_renewals()

                health_by_name  = {h["account_name"]: h for h in health_raw}
                renewal_by_name = {r["account_name"]: r for r in renewals_raw}

                accounts = []
                for acct in accounts_raw:
                    name = acct.get("name", "")
                    if isinstance(_csm_accounts, list) and name not in _csm_accounts:
                        continue
                    h     = health_by_name.get(name, {})
                    r     = renewal_by_name.get(name, {})
                    score = h.get("score")
                    accounts.append({
                        "name":         name,
                        "health_score": int(score) if score is not None else None,
                        "health_trend": h.get("trend", "stable"),
                        "renewal_days": r.get("days_until_renewal"),
                    })

                accounts.sort(key=lambda a: (a["health_score"] is None, a["health_score"] or 0))
                return JSONResponse({"accounts": accounts})
        except Exception as e:
            logger.warning("org_config Path 1 failed for %s: %s", csm_id, e)

    # Path 2: Clerk user with CRM token (Gainsight first, HubSpot fallback)
    try:
        _crm_connector = None

        # Check Gainsight
        try:
            from product.connectors.gainsight_mcp import load_gainsight_token
            if load_gainsight_token(csm_id):
                from product.connectors.gainsight import GainsightCRMConnector
                _crm_connector = GainsightCRMConnector(csm_id)
                logger.debug("accounts: using Gainsight connector for %s", csm_id)
        except Exception:
            pass

        # Fall back to HubSpot
        if _crm_connector is None:
            try:
                from product.connectors.hubspot_mcp import load_hubspot_token as _load_hs
                if _load_hs(csm_id):
                    from product.connectors.hubspot_crm import HubSpotCRMConnector
                    _crm_connector = HubSpotCRMConnector(csm_id)
                    logger.debug("accounts: using HubSpot connector for %s", csm_id)
            except Exception:
                pass

        if _crm_connector:
            _raw = _crm_connector.get_accounts()
            # Distinguish an outage from "user owns nothing" so the React
            # sidebar can render a data-issue banner instead of "no accounts".
            # Only the HubSpot connector currently exposes get_accounts_status;
            # for Gainsight and any future connector, the absence of the
            # accessor is treated as OK (legacy behavior preserved).
            from product.connectors.source_status import SourceStatus as _AcctSS
            _acct_status = (
                _crm_connector.get_accounts_status()
                if hasattr(_crm_connector, "get_accounts_status")
                else _AcctSS.OK
            )
            if _acct_status == _AcctSS.FAILED:
                logger.warning(
                    "accounts: connector returned FAILED status for %s — surfacing data-issue", csm_id
                )
                return JSONResponse({
                    "accounts": [],
                    "status": "failed",
                    "notice": "Could not load accounts from your CRM. The connection may be rate-limited or temporarily unavailable.",
                })
            _health = _crm_connector.get_health_scores()
            _renewals = _crm_connector.get_renewals()

            _health_by_name = {h["account_name"]: h for h in _health}
            _renewal_by_name = {r["account_name"]: r for r in _renewals}

            def _safe_score(val):
                """Convert health score to int safely. Handles None, floats, strings."""
                if val is None:
                    return None
                try:
                    return int(float(val))
                except (ValueError, TypeError):
                    return None

            _accounts = []
            for a in _raw:
                name = a.get("name", "")
                if not name:
                    continue
                h = _health_by_name.get(name, {})
                r = _renewal_by_name.get(name, {})
                _accounts.append({
                    "name": name,
                    "health_score": _safe_score(h.get("score")),
                    "health_trend": h.get("trend", "stable"),
                    "renewal_days": r.get("days_until_renewal"),
                })

            _accounts.sort(key=lambda a: (a["health_score"] is None, a["health_score"] or 0))
            _response: dict = {"accounts": _accounts}
            if _acct_status == _AcctSS.EMPTY:
                _response["status"] = "empty"
            return JSONResponse(_response)
    except Exception as e:
        logger.warning("CRM connector path failed for %s: %s", csm_id, e)

    # Path 3: Accounts saved during onboarding (product/data/accounts/{csm_id}.json)
    try:
        _acct_file = _safe_data_file("product/data/accounts", csm_id)
        if _acct_file.exists():
            _acct_data = json.loads(_acct_file.read_text(encoding="utf-8"))
            _names = _acct_data.get("accounts", [])
            return JSONResponse({"accounts": [{"name": n, "health_score": None, "health_trend": "stable", "renewal_days": None} for n in _names]})
    except Exception as e:
        logger.warning("file path failed for %s: %s", csm_id, e)

    # Path 4: DB customer record
    try:
        from product.database import SessionLocal as _SL
        if _SL:
            from product.database import Customer as _Cust, SessionLocal as _SL2
            _db = _SL2()
            try:
                _c = _db.query(_Cust).filter_by(clerk_user_id=csm_id).first()
                if _c and _c.accounts:
                    return JSONResponse({"accounts": [{"name": n, "health_score": None, "health_trend": "stable", "renewal_days": None} for n in _c.accounts]})
            finally:
                _db.close()
    except Exception as e:
        logger.warning("DB path failed for %s: %s", csm_id, e)

    return JSONResponse({"accounts": []})


@app.get("/app/timeline")
async def app_timeline(request: Request):
    _session = await _require_active_subscription(request)
    csm_id = _validate_csm_id(_session["csm_id"])

    now_utc = datetime.now(timezone.utc)
    events = []

    # Daily briefing — use the actual delivered_at time from DigestLog when
    # available so the timeline reflects the user's clock; fall back to the
    # current login time if the digest hasn't landed yet.
    _briefing_time_iso = None
    _briefing_past = False
    try:
        from product.database import SessionLocal as _BSL, DigestLog as _BDL
        if _BSL:
            _bdb = _BSL()
            try:
                _today_str = now_utc.strftime("%Y-%m-%d")
                _row = (
                    _bdb.query(_BDL)
                    .filter_by(customer_id=csm_id, date=_today_str, status="success")
                    .order_by(_BDL.created_at.desc())
                    .first()
                )
                if _row and _row.delivered_at:
                    _dt = _row.delivered_at
                    if _dt.tzinfo is None:
                        _dt = _dt.replace(tzinfo=timezone.utc)
                    _briefing_time_iso = _dt.isoformat().replace("+00:00", "Z")
                    _briefing_past = True
            finally:
                _bdb.close()
    except Exception as _be:
        logger.debug("timeline: digest lookup failed for %s: %s", csm_id, _be)

    if _briefing_time_iso is None:
        _briefing_time_iso = now_utc.isoformat().replace("+00:00", "Z")
    # Derive a display "time" string in the CSM's local timezone.
    _csm_tz = get_csm_timezone(csm_id)
    try:
        _briefing_dt = datetime.fromisoformat(_briefing_time_iso.replace("Z", "+00:00"))
        _briefing_local = _briefing_dt.astimezone(_csm_tz)
        _briefing_display_time = _briefing_local.strftime("%-I:%M %p")
    except Exception:
        _briefing_display_time = "8:00 AM"
    events.append({
        "time":     _briefing_display_time,
        "time_iso": _briefing_time_iso,
        "label":    "Daily briefing",
        "type":     "digest",
        "past":     _briefing_past,
    })

    try:
        from product.signals.calendar import extract_calendar_signals as _extract_cal

        from product.org_config import resolve_csm_config as _resolve_cfg_tl
        _cfg_tl = _resolve_cfg_tl(csm_id)
        account_names = _cfg_tl.accounts if _cfg_tl else []
        if not isinstance(account_names, list):
            account_names = []

        cal          = _extract_cal(csm_id, account_names)
        all_meetings = cal.get("customer_meetings", []) + cal.get("internal_meetings", [])

        for m in all_meetings:
            label        = m.get("account") or m.get("title") or "Meeting"
            start_iso    = m.get("start_iso")
            start_hhmm   = m.get("start", "")

            if start_iso:
                # Canonical UTC ISO from calendar.py — parse for past/display
                meeting_dt   = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
                past         = now_utc >= meeting_dt
                meeting_iso  = start_iso
                # Display time in CSM's local timezone
                _local_dt    = meeting_dt.astimezone(_csm_tz)
                _h, _mi      = _local_dt.hour, _local_dt.minute
                _sfx         = "AM" if _h < 12 else "PM"
                display_time = f"{_h % 12 or 12}:{_mi:02d} {_sfx}"
            elif start_hhmm:
                # Fallback: start_iso absent (stale cache / malformed signal)
                logger.warning("meeting event missing start_iso: %s", m.get("title", "(untitled)"))
                past         = False
                meeting_iso  = None
                # Best-effort display from the HH:MM string
                try:
                    _h, _mi  = map(int, start_hhmm.split(":"))
                    _sfx     = "AM" if _h < 12 else "PM"
                    display_time = f"{_h % 12 or 12}:{_mi:02d} {_sfx}"
                except Exception:
                    display_time = start_hhmm
            else:
                past         = False
                meeting_iso  = None
                display_time = ""

            events.append({"time": display_time, "time_iso": meeting_iso, "label": label, "type": "meeting", "past": past})

    except Exception as e:
        logger.warning("calendar error: %s", e)

    # Proactive alerts from disk
    try:
        _alerts_dir = _safe_data_path("product/data/alerts", csm_id)
        if _alerts_dir.exists():
            _today_str = now_utc.strftime("%Y-%m-%d")
            for _af in sorted(_alerts_dir.glob("*.json")):
                try:
                    _ad  = json.loads(_af.read_text(encoding="utf-8"))
                    _ts  = _ad.get("timestamp", "")
                    if not _ts.startswith(_today_str):
                        continue
                    _adt    = datetime.fromisoformat(_ts.replace("Z", "+00:00"))
                    _h, _mi = _adt.hour, _adt.minute
                    _sfx    = "AM" if _h < 12 else "PM"
                    _text   = _ad.get("text", "")
                    _acct   = _text.split(" needs")[0] if " needs" in _text else "Alert"
                    events.append({
                        "time":  f"{_h % 12 or 12}:{_mi:02d} {_sfx}",
                        "time_iso": _ts,
                        "label": f"Alert: {_acct}",
                        "type":  "alert",
                        "past":  now_utc >= _adt,
                    })
                except Exception:
                    continue
    except Exception as e:
        logger.warning("alert error: %s", e)

    # Chat interactions from interaction_log.jsonl
    try:
        _ilog = Path("interaction_log.jsonl")
        if _ilog.exists():
            _today_str = now_utc.strftime("%Y-%m-%d")
            with open(_ilog, encoding="utf-8") as _f:
                for _line in _f:
                    _line = _line.strip()
                    if not _line:
                        continue
                    try:
                        _entry = json.loads(_line)
                        if _entry.get("csm_id") != csm_id:
                            continue
                        if _entry.get("interaction_type") != "signal.acted":
                            continue
                        _ts = _entry.get("timestamp", "")
                        if not _ts.startswith(_today_str):
                            continue
                        _idt    = datetime.fromisoformat(_ts.replace("Z", "+00:00"))
                        _h, _mi = _idt.hour, _idt.minute
                        _sfx    = "AM" if _h < 12 else "PM"
                        _query  = (_entry.get("payload") or {}).get("query", "")
                        _label  = (f"Asked: {_query[:35]}…" if len(_query) > 35
                                   else (f"Asked: {_query}" if _query else "Chat"))
                        events.append({
                            "time":  f"{_h % 12 or 12}:{_mi:02d} {_sfx}",
                            "time_iso": _ts,
                            "label": _label,
                            "type":  "chat",
                            "past":  True,
                        })
                    except Exception:
                        continue
    except Exception as e:
        logger.warning("interaction error: %s", e)

    def _time_to_minutes(t: str) -> int:
        try:
            parts = t.split()
            h, m = map(int, parts[0].split(":"))
            if len(parts) > 1 and parts[1] == "PM" and h != 12:
                h += 12
            elif len(parts) > 1 and parts[1] == "AM" and h == 12:
                h = 0
            return h * 60 + m
        except Exception:
            return 9999

    events.sort(key=lambda e: _time_to_minutes(e.get("time", "00:00")))
    _active_set = False
    for _ev in events:
        if _ev["past"]:
            _ev["state"] = "past"
        elif not _active_set:
            _ev["state"] = "active"
            _active_set  = True
        else:
            _ev["state"] = "upcoming"
    return JSONResponse({"events": events})


@app.get("/app/alerts")
async def app_alerts(request: Request):
    _session = await _require_active_subscription(request)
    csm_id = _validate_csm_id(_session["csm_id"])

    since_str = request.query_params.get("since", "")
    since_dt = None
    if since_str:
        try:
            since_dt = datetime.fromisoformat(since_str.replace("Z", "+00:00"))
        except Exception:
            pass

    # PRIMARY: Check Postgres (shared across web + worker containers)
    try:
        from product.database import SessionLocal as _AlertSL, ProactiveAlert as _PA
        if _AlertSL:
            _adb = _AlertSL()
            try:
                _since_dt = since_dt if since_dt else datetime.min.replace(tzinfo=timezone.utc)
                _db_alerts = (
                    _adb.query(_PA)
                    .filter(_PA.csm_id == csm_id)
                    .filter(_PA.created_at > _since_dt)
                    .order_by(_PA.created_at.asc())
                    .all()
                )
                if _db_alerts:
                    alerts = []
                    for a in _db_alerts:
                        alert_dict = {
                            "alert_id": a.id,
                            "timestamp": a.created_at.isoformat().replace("+00:00", "Z") if a.created_at else "",
                            "text": a.alert_text,
                            "draft": a.draft_text,
                        }
                        # C.1 Commits 5+6: surface the structured post-call
                        # draft when present. Frontend's useAlerts routes
                        # this to the PostCallDraftCard.
                        raw_postcall = getattr(a, "postcall_draft_json", None)
                        if raw_postcall:
                            try:
                                alert_dict["postcall_draft"] = json.loads(raw_postcall)
                            except Exception:
                                # Malformed JSON in the column — drop silently;
                                # frontend falls through to the plain-alert path.
                                pass
                        alerts.append(alert_dict)
                    return JSONResponse({"alerts": alerts})
            finally:
                _adb.close()
    except Exception as _adb_err:
        logger.warning("alerts Postgres read failed: %s", _adb_err)

    # FALLBACK: filesystem (backward compat for sarah_c / pre-migration data)
    alerts_dir = _safe_data_path("product/data/alerts", csm_id)
    if not alerts_dir.exists():
        return JSONResponse({"alerts": []})

    alerts = []
    for alert_file in sorted(alerts_dir.glob("*.json")):
        try:
            data = json.loads(alert_file.read_text(encoding="utf-8"))
            if since_dt:
                ts = data.get("timestamp", "")
                if ts:
                    try:
                        alert_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if alert_dt <= since_dt:
                            continue
                    except Exception:
                        pass
            alerts.append(data)
        except Exception:
            continue

    return JSONResponse({"alerts": alerts})


@app.post("/app/profile")
async def app_update_profile(request: Request):
    """Save Clerk user's display name to Postgres. Only updates if name is missing or raw user_id."""
    _session = await _resolve_auth(request)
    if not _session:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    body = await request.json()
    name = (body.get("name") or "").strip()
    tz = (body.get("timezone") or "").strip()
    csm_id = _validate_csm_id(_session["csm_id"])

    # Validate timezone if provided
    _valid_tz = ""
    if tz:
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(tz)  # raises KeyError on invalid IANA name
            _valid_tz = tz
        except (KeyError, Exception):
            logger.debug("Invalid timezone %r from %s — ignoring", tz, csm_id)

    if not name and not _valid_tz:
        return {"status": "no_change"}

    try:
        from product.database import SessionLocal, Customer
        if SessionLocal:
            session = SessionLocal()
            try:
                cust = session.query(Customer).filter_by(clerk_user_id=csm_id).first()
                if not cust:
                    return {"status": "unchanged"}
                _changed = False
                if name and (not cust.name or cust.name.startswith("user_")):
                    cust.name = name
                    _changed = True
                if _valid_tz and cust.timezone != _valid_tz:
                    cust.timezone = _valid_tz
                    _changed = True
                if _changed:
                    session.commit()
                    logger.info("Profile updated for %s: name=%s tz=%s", csm_id, cust.name, cust.timezone)
                    return {"status": "updated", "name": cust.name, "timezone": cust.timezone}
            finally:
                session.close()
    except Exception as _e:
        logger.warning("Profile update failed (non-fatal): %s", _e)
    return {"status": "unchanged"}


@app.post("/app/feedback")
async def app_feedback(request: Request):
    _session = await _require_active_subscription(request)
    csm_id   = _session["csm_id"]
    csm_name = _session.get("name", csm_id)

    body = await request.json()
    message_text = (body.get("message_text") or "").strip()
    if not message_text:
        return JSONResponse({"error": "message_text required"}, status_code=400)

    feedback_dir = _safe_data_path("product/data/feedback", csm_id)
    feedback_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    feedback_file = feedback_dir / f"{ts}.json"
    ts_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    feedback_payload = {
        "csm_id":       csm_id,
        "csm_name":     csm_name,
        "timestamp":    ts_iso,
        "message_text": message_text,
        "context":      "feedback",
    }
    feedback_file.write_text(json.dumps(feedback_payload, indent=2), encoding="utf-8")

    # Notify founder (best-effort — never blocks the response)
    try:
        from product.digest.delivery import send_system_email as _sse
        _founder_email = os.environ.get("FOUNDER_EMAIL") or SUPPORT_EMAIL
        _csm_email     = _session.get("email", "unknown")
        _flagged_msg   = (body.get("flagged_message") or "").strip()
        _plain = (
            f"New feedback from CS Agent\n"
            f"{'=' * 40}\n"
            f"CSM ID:    {csm_id}\n"
            f"CSM Email: {_csm_email}\n"
            f"Time:      {ts_iso}\n\n"
            f"Feedback:\n{message_text}\n"
        )
        if _flagged_msg:
            _plain += f"\nFlagged message:\n{_flagged_msg}\n"
        _delivered = _sse(
            to=_founder_email,
            subject=f"[CS Agent] Feedback from {csm_name}",
            body_html=f"<pre>{_plain}</pre>",
            body_plain=_plain,
        )
        if _delivered:
            logger.info("Founder notified of feedback from %s", csm_id)
        else:
            logger.info("Founder feedback notification skipped for %s (recipient filtered)", csm_id)
    except Exception as _fe:
        logger.warning("Could not send founder notification: %s", _fe)

    return JSONResponse({"ok": True})


@app.post("/app/onboarding/complete")
async def app_onboarding_complete(request: Request, background_tasks: BackgroundTasks):
    # Identity from verified Clerk JWT + subscription gate (matches the
    # canonical pattern used by the other /app/* routes). Body.user_id
    # is intentionally ignored — accepted for frontend backward-compat
    # during deploy but never trusted. Closes the unauthenticated
    # body-controlled-identity gap (Stage 1 audit G-1).
    _session = await _require_active_subscription(request)
    user_id = _validate_csm_id(_session["csm_id"])
    user_email = _session.get("email")
    user_first_name = (_session.get("name") or "").strip()

    body = await request.json()
    _raw_accounts = body.get("accounts") or []
    # body.get("user_id") is intentionally not read — identity from JWT only.

    # Auto-resolve accounts from CRM when frontend sends "auto"
    if _raw_accounts == "auto" or (isinstance(_raw_accounts, str) and _raw_accounts.lower() == "auto"):
        resolved_names: list[str] = []
        # Try Gainsight first
        try:
            from product.connectors.gainsight_mcp import load_gainsight_token as _load_gs_onb
            gs_token = _load_gs_onb(user_id)
            if gs_token and gs_token.get("access_token"):
                from product.connectors.gainsight import GainsightCRMConnector
                _gs_conn = GainsightCRMConnector(user_id)
                _gs_raw = _gs_conn.get_accounts()
                resolved_names = [a.get("name", "") for a in _gs_raw if a.get("name")]
                logger.info("onboarding: auto-resolved %d accounts from Gainsight for %s", len(resolved_names), user_id)
        except Exception as _gs_err:
            logger.warning("onboarding: Gainsight auto-resolve failed: %s", _gs_err)
        # Fall back to HubSpot
        if not resolved_names:
            try:
                from product.connectors.hubspot_mcp import load_hubspot_token as _load_hs_onb
                hs_token = _load_hs_onb(user_id)
                if hs_token and hs_token.get("access_token"):
                    from product.connectors.hubspot_crm import HubSpotCRMConnector
                    _hs_conn = HubSpotCRMConnector(user_id)
                    _hs_raw = _hs_conn.get_accounts()
                    resolved_names = [a.get("name", "") for a in _hs_raw if a.get("name")]
                    logger.info("onboarding: auto-resolved %d accounts from HubSpot for %s", len(resolved_names), user_id)
            except Exception as _hs_err:
                logger.warning("onboarding: HubSpot auto-resolve failed: %s", _hs_err)
        accounts = resolved_names
    else:
        accounts = [str(a).strip() for a in _raw_accounts if str(a).strip()]

    # user_email and user_first_name already resolved from the verified
    # session above (line 1 of the handler). The earlier best-effort
    # verify_clerk_token call inside a bare-except was removed: the JWT
    # is now required at the route boundary, so the values are always
    # populated when we reach this point.

    accounts_dir = Path("product/data/accounts")
    accounts_dir.mkdir(parents=True, exist_ok=True)
    (accounts_dir / f"{user_id}.json").write_text(
        json.dumps({
            "user_id":    user_id,
            "accounts":   accounts,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }, indent=2),
        encoding="utf-8",
    )

    # Persist to Postgres (additive — JSON file above is the backup)
    try:
        from product.database import update_customer_onboarding
        update_customer_onboarding(clerk_user_id=user_id, accounts=accounts)
    except Exception as _dbe:
        logger.warning("DB write failed (non-fatal): %s", _dbe)

    # Stripe provisioning removed — new flow is:
    # 1. get_or_create_customer gives trialing + trial_ends_at on signup
    # 2. Trial expiry job sets canceled after 14 days
    # 3. Gate modal offers Checkout session to create Stripe customer
    # 4. Webhook links Stripe customer back to Postgres row

    # Token health check (best-effort — informational only, never blocks onboarding)
    connection_status = {"gmail": "not_connected", "hubspot": "not_connected"}

    try:
        from product.auth_google import load_google_token
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build as _g_build
        from googleapiclient.errors import HttpError as _HttpError
        google_token = load_google_token(user_id)
        if google_token:
            try:
                creds = Credentials.from_authorized_user_info(google_token)
                svc = _g_build("gmail", "v1", credentials=creds)
                svc.users().messages().list(userId="me", maxResults=1).execute()
                connection_status["gmail"] = "connected"
            except _HttpError as _he:
                logger.warning("Gmail token invalid for %s: %s", user_id, _he)
                connection_status["gmail"] = "failed"
            except Exception as _ge:
                logger.warning("Gmail validation error for %s: %s", user_id, _ge)
                connection_status["gmail"] = "failed"
    except Exception as _outer:
        logger.warning("Gmail health check import error: %s", _outer)

    try:
        from product.connectors.hubspot_mcp import load_hubspot_token
        hs_token = load_hubspot_token(user_id)
        if hs_token:
            try:
                access_token = hs_token.get("access_token") or hs_token.get("token")
                resp = requests.get(
                    "https://api.hubapi.com/crm/v3/objects/contacts",
                    headers={"Authorization": f"Bearer {access_token}"},
                    params={"limit": 1},
                    timeout=5,
                )
                if resp.status_code == 401:
                    logger.warning("HubSpot token invalid for %s", user_id)
                    connection_status["hubspot"] = "failed"
                else:
                    connection_status["hubspot"] = "connected"
            except Exception as _he:
                logger.warning("HubSpot validation error for %s: %s", user_id, _he)
                connection_status["hubspot"] = "failed"
    except Exception as _outer:
        logger.warning("HubSpot health check import error: %s", _outer)

    # First digest via arq (or BackgroundTasks fallback) — with dedup
    digest_triggered = False
    _onb_today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _onb_dedup = f"{user_id}:{_onb_today}"
    _onb_digest_path = Path(f"product/data/digests/{user_id}/{_onb_today}.json")
    if _onb_digest_path.exists() or (_onb_dedup in _digest_in_flight and (time.time() - _digest_in_flight[_onb_dedup]) < 300):
        logger.info("Digest already exists for %s today, skipping", user_id)
        digest_triggered = True
    else:
        _digest_in_flight[_onb_dedup] = time.time()
        try:
            from product.queue import is_redis_available
            if is_redis_available():
                from product.queue import enqueue
                await enqueue("job_generate_digest", user_id, _dedup_key=f"digest:{user_id}:{_onb_today}")
            else:
                background_tasks.add_task(_run_digest_for_user, user_id)
            digest_triggered = True
        except Exception as _de:
            logger.warning("Digest background task failed to queue for %s: %s", user_id, _de)

    # Day 1 engagement email — send immediately, don't wait for 9 AM scheduler
    try:
        if user_email:
            from product.engagement_emails import send_engagement_email as _send_eng
            _send_eng(
                csm_id=user_id,
                email=user_email,
                first_name=user_first_name,
                day=1,
            )
    except Exception as _ee:
        logger.warning("Day 1 engagement email failed (non-fatal): %s", _ee)

    return JSONResponse({
        "status": "complete",
        "accounts_saved": len(accounts),
        "accounts_resolved": len(accounts),
        "connection_status": connection_status,
        "digest_triggered": digest_triggered,
    })


def _resolve_to_numeric_company_id(csm_id: str, raw_value: str) -> str:
    """Resolve a raw company_id value to a numeric HubSpot company ID string.

    Handles three input forms from the LLM:
      1. Already numeric ("12345678901") -> returned unchanged.
      2. Exact account name ("Northgate Logistics") -> resolved via HubSpot accounts.
      3. Slugified name ("northgate_logistics") -> slug-normalized match attempted.

    Returns the numeric ID string on success. Falls back to raw_value unchanged
    when resolution fails (the association write will 404 as before, but no regression
    vs the current behaviour - and the note is still created).

    Never raises.
    """
    if not raw_value or raw_value.strip().isdigit():
        return raw_value  # Already numeric or empty, pass through.
    try:
        from product.connectors.hubspot_crm import HubSpotCRMConnector
        connector = HubSpotCRMConnector(csm_id)
        accounts = connector.get_accounts()  # uses internal cache after first call

        # Pass 1: exact name match
        for acct in accounts:
            if acct.get("name") == raw_value:
                hid = acct.get("hubspot_id")
                if hid is not None:
                    return str(hid)

        # Pass 2: case-insensitive name match
        raw_lower = raw_value.strip().lower()
        for acct in accounts:
            if (acct.get("name") or "").lower() == raw_lower:
                hid = acct.get("hubspot_id")
                if hid is not None:
                    return str(hid)

        # Pass 3: slug-normalized match (spaces -> underscores, lowercased)
        for acct in accounts:
            name_slug = (acct.get("name") or "").lower().replace(" ", "_")
            if name_slug == raw_lower:
                hid = acct.get("hubspot_id")
                if hid is not None:
                    return str(hid)

    except Exception as exc:
        import logging as _logging
        _logging.getLogger("cs-agent").warning(
            "company_id resolution failed for csm=%s raw=%r: %s", csm_id, raw_value, exc
        )
    return raw_value  # Fallback: return original value unchanged.


@app.post("/app/actions/execute")
async def app_actions_execute(request: Request):
    """Human-in-the-loop HubSpot write-back.

    Accepts:
        action_type: "log_note" | "create_task" | "update_deal" | "create_contact"
        payload: dict with fields specific to each action type

    The CSM must approve the draft in the UI before this endpoint is called.
    All writes go through HubSpotWriter, which supports HUBSPOT_USE_MOCK=true.
    """
    _session = await _require_active_subscription(request)

    body = await request.json()
    action_type = (body.get("action_type") or "").strip()
    payload = body.get("payload") or {}
    csm_id = _session.get("csm_id", "unknown")

    if action_type not in ("log_note", "create_task", "update_deal", "create_contact", "email_draft", "log_timeline_entry"):
        return JSONResponse({"status": "error", "message": f"Unknown action_type: {action_type!r}"}, status_code=400)

    # Email drafts: record the edit delta first, then decide whether to send.
    if action_type == "email_draft":
        try:
            _original = (body.get("original_text") or "").strip()
            if _original:
                _edited = (body.get("edited_text") or "").strip()
                _was_edited = body.get("was_edited", _original != _edited)
                from product.database import save_edit_delta
                save_edit_delta(
                    csagent_user_id=csm_id,
                    draft_type="email_draft",
                    original_text=_original,
                    edited_text=_edited,
                    was_edited=_was_edited,
                    account_name=payload.get("company_id") or payload.get("account_name"),
                )
                logger.info("Edit delta captured — type=email_draft, was_edited=%s", _was_edited)
        except Exception as _delta_err:
            logger.warning("Email draft delta capture failed (non-fatal): %s", _delta_err)

        # sarah_c demo bypass: filesystem-only credentials, no Customer row.
        # Never reaches the send path, regardless of payload shape.
        if is_demo_csm_id(csm_id):
            return JSONResponse({"status": "success", "result": {"action": "email_draft_approved"}})

        # Is the user opted in to send?
        google_actions_enabled = False
        try:
            from product.database import SessionLocal, Customer
            if SessionLocal:
                db = SessionLocal()
                try:
                    cust = db.query(Customer).filter_by(clerk_user_id=csm_id).first()
                    if cust and cust.google_actions_enabled:
                        google_actions_enabled = True
                finally:
                    db.close()
        except Exception as _db_err:
            logger.warning("google_actions_enabled lookup failed for %s: %s", csm_id, _db_err)
            # Fail-closed: treat as disabled on DB error.

        to_addr = (payload.get("to") or "").strip()
        subject = (payload.get("subject") or "").strip()
        body_text = (body.get("edited_text") or body.get("original_text") or "").strip()
        thread_id = payload.get("thread_id")

        # Backward-compat path: no send if flag off, or any of to/subject/body missing.
        if not google_actions_enabled or not to_addr or not subject or not body_text:
            return JSONResponse({"status": "success", "result": {"action": "email_draft_approved"}})

        # Send path.
        from gmail_helper import send_gmail_message, GmailScopeRevokedError
        try:
            gmail_message_id = send_gmail_message(
                csm_id,
                to_addr,
                subject,
                body_text,
                thread_id=thread_id,
            )
        except GmailScopeRevokedError as _revoked_err:
            logger.warning("Gmail send refused for %s — scope revoked: %s", csm_id, _revoked_err)
            try:
                from product.database import SessionLocal, Customer
                if SessionLocal:
                    db = SessionLocal()
                    try:
                        cust = db.query(Customer).filter_by(clerk_user_id=csm_id).first()
                        if cust:
                            cust.google_actions_enabled = False
                            db.commit()
                    finally:
                        db.close()
            except Exception as db_err:
                logger.warning(
                    "Failed to flip google_actions_enabled to False "
                    "after scope revocation for csm_id=%s: %s",
                    csm_id, db_err,
                )
                # Still return scope_revoked — user sees correct UX; DB
                # inconsistency self-heals on next flag check.
            return JSONResponse({
                "status": "scope_revoked",
                "message": "Your Google permission has expired. Please reconnect in Settings.",
            })

        return JSONResponse({
            "status": "sent",
            "gmail_message_id": gmail_message_id,
            "to": to_addr,
        })

    # ── Post-call timeline entry (C.1 Commit 3) ──────────────────────────────
    # Handler emits timeline_entry.approved, dispatches to the connected CRM,
    # and returns a shape the DraftCard frontend (Commit 4) can branch on.
    if action_type == "log_timeline_entry":
        from product.postcall.events import PostCallDispatchError
        from product.postcall.dispatcher import dispatch_timeline_entry_write

        account_name = payload.get("account_name") or ""
        account_crm_id = payload.get("account_crm_id") or ""
        crm_type = (payload.get("crm_type") or "").lower()
        original_draft_text = (body.get("original_text") or "").strip()
        final_edited_text = (body.get("edited_text") or body.get("original_text") or "").strip()
        was_edited = bool(body.get(
            "was_edited",
            original_draft_text != final_edited_text,
        ))

        # Record edit delta for the inward loop (best-effort, never blocks action)
        try:
            if original_draft_text:
                from product.database import save_edit_delta
                save_edit_delta(
                    csagent_user_id=csm_id,
                    draft_type="log_timeline_entry",
                    original_text=original_draft_text,
                    edited_text=final_edited_text,
                    was_edited=was_edited,
                    account_name=account_name,
                )
                logger.info("Edit delta captured — type=log_timeline_entry, was_edited=%s", was_edited)
        except Exception as _delta_err:
            logger.warning("Edit delta capture failed (non-fatal): %s", _delta_err)

        try:
            result = dispatch_timeline_entry_write(
                csm_id=csm_id,
                account_name=account_name,
                account_crm_id=account_crm_id,
                crm_type=crm_type,
                final_edited_text=final_edited_text,
            )
        except PostCallDispatchError as exc:
            logger.warning(
                "log_timeline_entry dispatch failed for %s: %s (error_type=%s)",
                csm_id, exc, exc.error_type,
            )
            return JSONResponse({
                "status": "error",
                "error_type": exc.error_type,
                "message": str(exc),
            }, status_code=500)

        return JSONResponse({
            "status": "written",
            "external_system_id": result["external_system_id"],
            "crm_type": result["crm_type"],
        })

    # The text the CSM reviewed/edited in the card is the SOURCE OF TRUTH for
    # the free-text field of each action. The frontend sends it as edited_text
    # (and also mirrors it into payload.body); older/stale cards may only carry
    # payload.body. Compute it once so log_note/create_task can't silently write
    # empty content while reporting success.
    _reviewed_text = (
        (body.get("edited_text") or "").strip()
        or (body.get("original_text") or "").strip()
        or (payload.get("body") or "").strip()
    )

    try:
        from product.connectors.hubspot_write import HubSpotWriter
        writer = HubSpotWriter(csm_id)

        if action_type == "log_note":
            note_body = (
                _reviewed_text
                or (payload.get("note_body") or "").strip()
                or (payload.get("body") or "").strip()
            )
            if not note_body:
                return JSONResponse(
                    {"status": "error", "message": "Cannot log an empty note."},
                    status_code=400,
                )
            _raw_company_id = payload.get("company_id", "")
            _resolved_company_id = _resolve_to_numeric_company_id(csm_id, _raw_company_id)
            if _raw_company_id and _resolved_company_id != _raw_company_id:
                logger.info("company_id resolved: %r -> %r for csm=%s", _raw_company_id, _resolved_company_id, csm_id)
            result = writer.log_note(
                company_id=_resolved_company_id,
                note_body=note_body,
            )
        elif action_type == "create_task":
            task_notes = (payload.get("notes") or _reviewed_text or "").strip() or None
            task_title = (payload.get("title") or "").strip()
            if not task_title and not task_notes:
                return JSONResponse(
                    {"status": "error", "message": "Cannot create an empty task."},
                    status_code=400,
                )
            result = writer.create_task(
                title=task_title,
                due_date=payload.get("due_date"),
                company_id=_resolve_to_numeric_company_id(csm_id, payload.get("company_id") or ""),
                notes=task_notes,
            )
        elif action_type == "update_deal":
            result = writer.update_deal(
                deal_id=payload.get("deal_id", ""),
                properties=payload.get("properties", {}),
            )
        elif action_type == "create_contact":
            result = writer.create_contact(
                email=payload.get("email", ""),
                firstname=payload.get("firstname"),
                lastname=payload.get("lastname"),
                company_id=_resolve_to_numeric_company_id(csm_id, payload.get("company_id") or ""),
            )

        # Record edit delta for the inward loop (best-effort, never blocks action)
        try:
            _original = (body.get("original_text") or "").strip()
            if _original:
                _edited = (body.get("edited_text") or "").strip()
                _was_edited = body.get("was_edited", _original != _edited)
                from product.database import save_edit_delta
                save_edit_delta(
                    csagent_user_id=csm_id,
                    draft_type=action_type,
                    original_text=_original,
                    edited_text=_edited,
                    was_edited=_was_edited,
                    account_name=payload.get("company_id") or payload.get("account_name"),
                )
                logger.info("Edit delta captured — type=%s, was_edited=%s", action_type, _was_edited)
        except Exception as _delta_err:
            logger.warning("Edit delta capture failed (non-fatal): %s", _delta_err)

        return JSONResponse({"status": "success", "result": result})

    except Exception as exc:
        logger.warning("%s failed for %s: %s", action_type, csm_id, exc)
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)


# Catch-all for React client-side routes under /app (e.g. /app/onboarding).
# Must be declared AFTER all specific /app/* API routes.
@app.get("/app/{full_path:path}", response_class=HTMLResponse)
async def app_spa_fallback(full_path: str):
    react_index = Path("frontend/dist/index.html")
    if react_index.exists():
        from fastapi.responses import FileResponse as _FR
        return _FR(str(react_index))
    return HTMLResponse(content=Path("product/static/app.html").read_text(encoding="utf-8"), status_code=200)


@app.get("/admin/customers")
async def admin_customers(request: Request):
    """Founder dashboard — list all customers. Requires X-Admin-Key header."""
    admin_secret = os.getenv("ADMIN_SECRET", "")
    if not admin_secret or not secrets.compare_digest(request.headers.get("x-admin-key", ""), admin_secret):
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        from product.database import SessionLocal, Customer
        if not SessionLocal:
            return JSONResponse({"error": "database not configured"}, status_code=503)
        session = SessionLocal()
        try:
            customers = session.query(Customer).order_by(Customer.created_at.desc()).limit(100).all()
            return JSONResponse([
                {
                    "email":               c.email,
                    "name":                c.name,
                    "subscription_status": c.subscription_status,
                    "onboarding_complete": c.onboarding_complete,
                    "accounts":            c.accounts,
                    "created_at":          c.created_at.isoformat() if c.created_at else None,
                }
                for c in customers
            ])
        finally:
            session.close()
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/admin/dashboard")
async def admin_dashboard(request: Request):
    """Founder overview dashboard. Requires X-Admin-Key header."""
    admin_secret = os.getenv("ADMIN_SECRET", "")
    if not admin_secret or not secrets.compare_digest(request.headers.get("x-admin-key", ""), admin_secret):
        raise HTTPException(status_code=403, detail="Forbidden")

    result: dict = {
        "total_customers":       0,
        "active_subscriptions":  0,
        "customers":             [],
        "recent_digests":        [],
        "recent_feedback":       [],
    }

    # ── Customers + connections ───────────────────────────────────────────────
    try:
        from product.database import SessionLocal, Customer, Connection, DigestLog
        if SessionLocal:
            session = SessionLocal()
            try:
                customers = session.query(Customer).order_by(Customer.created_at.desc()).limit(100).all()
                result["total_customers"] = len(customers)
                result["active_subscriptions"] = sum(
                    1 for c in customers
                    if c.subscription_status in ("trialing", "active")
                )

                # Build a set of connected providers per customer
                connections = session.query(Connection).filter(
                    Connection.status == "active"
                ).limit(500).all()
                conn_map: dict[str, dict] = {}
                for conn in connections:
                    conn_map.setdefault(conn.customer_id, {})
                    conn_map[conn.customer_id][conn.provider] = True

                result["customers"] = [
                    {
                        "user_id":             c.clerk_user_id or c.id,
                        "email":               c.email,
                        "subscription_status": c.subscription_status,
                        "trial_ends_at":       c.trial_ends_at.isoformat() if c.trial_ends_at else None,
                        "created_at":          c.created_at.isoformat() if c.created_at else None,
                        "connections": {
                            "gmail":    bool(conn_map.get(c.clerk_user_id or c.id, {}).get("google")),
                            "hubspot":  bool(conn_map.get(c.clerk_user_id or c.id, {}).get("hubspot")),
                        },
                    }
                    for c in customers
                ]
            finally:
                session.close()
    except Exception as _dbe:
        result["db_error"] = str(_dbe)

    # ── Recent digests ────────────────────────────────────────────────────────
    try:
        from product.database import SessionLocal, DigestLog
        if SessionLocal:
            session = SessionLocal()
            try:
                logs = (
                    session.query(DigestLog)
                    .order_by(DigestLog.created_at.desc())
                    .limit(10)
                    .all()
                )
                result["recent_digests"] = [
                    {
                        "csm_id":  l.customer_id,
                        "sent_at": l.created_at.isoformat() if l.created_at else None,
                        "status":  l.status,
                    }
                    for l in logs
                ]
            finally:
                session.close()
    except Exception:
        pass  # digest_logs table may not exist yet

    # ── Recent feedback (filesystem) ──────────────────────────────────────────
    try:
        feedback_root = Path("product/data/feedback")
        feedback_entries = []
        if feedback_root.exists():
            for fb_file in sorted(feedback_root.rglob("*.json"), reverse=True)[:10]:
                try:
                    fb = json.loads(fb_file.read_text(encoding="utf-8"))
                    feedback_entries.append({
                        "csm_id":       fb.get("csm_id"),
                        "csm_name":     fb.get("csm_name"),
                        "timestamp":    fb.get("timestamp"),
                        "message_text": fb.get("message_text"),
                    })
                except Exception:
                    pass
        result["recent_feedback"] = feedback_entries
    except Exception:
        pass

    return JSONResponse(result)


@app.get("/admin/edit-deltas")
async def admin_edit_deltas(request: Request):
    """Inward loop dashboard — recent edit deltas. Requires X-Admin-Key header."""
    admin_secret = os.getenv("ADMIN_SECRET", "")
    if not admin_secret or not secrets.compare_digest(request.headers.get("x-admin-key", ""), admin_secret):
        raise HTTPException(status_code=403, detail="Forbidden")
    limit = int(request.query_params.get("limit", "20"))
    try:
        from product.database import SessionLocal, EditDelta
        if not SessionLocal:
            return JSONResponse({"error": "database not configured"}, status_code=503)
        session = SessionLocal()
        try:
            from sqlalchemy import func as _sqla_func
            deltas = session.query(EditDelta).order_by(EditDelta.created_at.desc()).limit(limit).all()
            total = session.query(_sqla_func.count(EditDelta.id)).scalar() or 0
            total_edited = session.query(_sqla_func.count(EditDelta.id)).filter(EditDelta.was_edited == True).scalar() or 0
            total_as_is = total - total_edited
            return JSONResponse({
                "summary": {
                    "total_deltas": total,
                    "total_edited": total_edited,
                    "total_approved_as_is": total_as_is,
                    "edit_rate_pct": round(total_edited / total * 100, 1) if total > 0 else 0,
                },
                "deltas": [
                    {
                        "csagent_user_id": d.csagent_user_id,
                        "draft_type": d.draft_type,
                        "account_name": d.account_name,
                        "was_edited": d.was_edited,
                        "created_at": d.created_at.isoformat() if d.created_at else None,
                    }
                    for d in deltas
                ],
            })
        finally:
            session.close()
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/admin/worker-health")
async def admin_worker_health(request: Request):
    """Worker/queue health check. Requires X-Admin-Key header."""
    admin_secret = os.getenv("ADMIN_SECRET", "")
    if not admin_secret or not secrets.compare_digest(request.headers.get("x-admin-key", ""), admin_secret):
        raise HTTPException(status_code=403, detail="Forbidden")

    result = {"redis_connected": False, "pending_jobs": 0, "queue_info": {}}
    try:
        from product.queue import _get_pool
        pool = await _get_pool()
        if pool:
            await pool.ping()
            result["redis_connected"] = True
            # Get queue info from arq's internal keys
            try:
                _queue_len = await pool.zcard(b"arq:queue")
                _results_len = await pool.zcard(b"arq:results")
                result["pending_jobs"] = _queue_len
                result["queue_info"] = {
                    "pending": _queue_len,
                    "results": _results_len,
                }
            except Exception:
                pass
    except Exception as _e:
        result["queue_info"]["error"] = str(_e)

    return JSONResponse(result)


@app.get("/admin/context-cache")
async def admin_context_cache(request: Request):
    """Context cache dashboard. Requires X-Admin-Key header."""
    admin_secret = os.getenv("ADMIN_SECRET", "")
    if not admin_secret or not secrets.compare_digest(request.headers.get("x-admin-key", ""), admin_secret):
        raise HTTPException(status_code=403, detail="Forbidden")
    limit = int(request.query_params.get("limit", "20"))
    try:
        from product.database import SessionLocal, UserContextSnapshot
        if not SessionLocal:
            return JSONResponse({"error": "database not configured"}, status_code=503)
        session = SessionLocal()
        try:
            rows = session.query(UserContextSnapshot).order_by(
                UserContextSnapshot.refreshed_at.desc()
            ).limit(limit).all()
            now = datetime.now(timezone.utc)
            total = len(rows)
            total_stale = sum(
                1 for r in rows
                if r.refreshed_at and (now - r.refreshed_at.replace(
                    tzinfo=timezone.utc if r.refreshed_at.tzinfo is None else r.refreshed_at.tzinfo
                )).total_seconds() > 3600
            )
            total_failed = sum(1 for r in rows if r.refresh_status == "failed")
            return JSONResponse({
                "summary": {
                    "total_cached": total,
                    "total_stale": total_stale,
                    "total_failed": total_failed,
                },
                "snapshots": [
                    {
                        "csm_id": r.csm_id,
                        "refreshed_at": r.refreshed_at.isoformat() if r.refreshed_at else None,
                        "refresh_status": r.refresh_status,
                        "context_size_chars": len(r.context_json) if r.context_json else 0,
                    }
                    for r in rows
                ],
            })
        finally:
            session.close()
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/admin/signal-cache/{csm_id}")
async def admin_signal_cache(csm_id: str, request: Request):
    """Inspect cached signals for a single user. Requires X-Admin-Key.
    No freshness filter — stale rows are included so admins can see
    the full cache state for debugging.
    """
    admin_secret = os.getenv("ADMIN_SECRET", "")
    if not admin_secret or not secrets.compare_digest(
        request.headers.get("x-admin-key", ""), admin_secret
    ):
        raise HTTPException(status_code=403, detail="Forbidden")

    from product.csm_id_validation import validate_csm_id
    try:
        csm_id = validate_csm_id(csm_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    from product.database import SessionLocal, SignalCache, as_utc
    if not SessionLocal:
        return JSONResponse({"error": "database not configured"}, status_code=503)

    session = SessionLocal()
    try:
        rows = session.query(SignalCache).filter_by(csm_id=csm_id).all()
        now = datetime.now(timezone.utc)

        groups: dict[str, dict] = {}
        for r in rows:
            refreshed = as_utc(r.refreshed_at)
            age_sec = int((now - refreshed).total_seconds()) if refreshed else None
            g = groups.setdefault(r.signal_type, {
                "count": 0,
                "latest_refreshed_at": None,
                "signals": [],
            })
            g["count"] += 1
            if refreshed and (g["latest_refreshed_at"] is None
                              or refreshed > g["latest_refreshed_at"]):
                g["latest_refreshed_at"] = refreshed
            g["signals"].append({
                "signal_id": r.signal_id,
                "signal_payload": r.signal_payload,
                "refreshed_at": refreshed.isoformat() if refreshed else None,
                "age_seconds": age_sec,
            })

        for g in groups.values():
            if g["latest_refreshed_at"]:
                g["latest_refreshed_at"] = g["latest_refreshed_at"].isoformat()

        return JSONResponse({
            "csm_id": csm_id,
            "total_rows": len(rows),
            "groups": groups,
        })
    finally:
        session.close()


@app.post("/admin/audit-snapshot")
async def admin_audit_snapshot(request: Request):
    """One-shot audit query bundle for Sprint 5 replanning.

    Runs the 8 queries flagged in /tmp/audit_round2/ROUND2_SYNTHESIS.md.
    Each query is wrapped in its own try/except: a failure on one does not
    block the rest. Sample-only — row-returning queries cap at 50 rows.
    Result counts and aggregates are returned; raw PII rows are minimised
    (clerk_user_id is returned, but emails / display names are not).

    Requires X-Admin-Key header. Read-only against Postgres + HubSpot MCP.

    Query 8 (HubSpot contacts-per-account distribution) is opt-in via
    ?include_hubspot=1 because it makes 15+ live MCP calls (~10s wall-time)
    and burns rate budget. Default: skip.
    """
    admin_secret = os.getenv("ADMIN_SECRET", "")
    if not admin_secret or not secrets.compare_digest(
        request.headers.get("x-admin-key", ""), admin_secret
    ):
        raise HTTPException(status_code=403, detail="Forbidden")

    include_hubspot = request.query_params.get("include_hubspot", "0") in ("1", "true", "yes")

    # csm_id is required: the per-CSM queries (contacts, HubSpot distribution)
    # have no default identity.
    target_csm_param = (request.query_params.get("csm_id") or "").strip()
    if not target_csm_param:
        raise HTTPException(status_code=400, detail="csm_id query parameter is required")

    ran_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    queries: dict = {}
    errors: list[dict] = []

    def _record_error(qname: str, exc: Exception) -> None:
        errors.append({"query": qname, "error": str(exc)[:300]})
        queries.setdefault(qname, None)

    # Build org_config CSM ID set once (used by query 1 and 8)
    try:
        from product.org_config import list_active_csms
        org_csm_ids = set(list_active_csms())
    except Exception as _e:
        org_csm_ids = set()
        errors.append({"query": "_org_config_load", "error": str(_e)[:300]})

    # ── Postgres queries 1-7 ─────────────────────────────────────────────────
    try:
        from product.database import (
            SessionLocal, Customer, Connection, DigestLog, ProactiveAlert, as_utc
        )
    except Exception as _e:
        return JSONResponse({
            "ran_at": ran_at,
            "queries": {},
            "errors": [{"query": "_import", "error": str(_e)[:300]}],
        }, status_code=500)

    if not SessionLocal:
        return JSONResponse({
            "ran_at": ran_at,
            "queries": {},
            "errors": [{"query": "_session", "error": "SessionLocal not configured"}],
        }, status_code=503)

    session = SessionLocal()
    try:
        from sqlalchemy import func

        # Query 1 — self-service trialists not in org_config.json
        try:
            q1_rows = session.query(Customer.clerk_user_id).all()
            all_customer_ids = [r[0] for r in q1_rows if r[0]]
            self_service = [cid for cid in all_customer_ids if cid not in org_csm_ids]
            queries["self_service_trialists_count"] = {
                "count": len(self_service),
                "in_org_config_count": sum(1 for cid in all_customer_ids if cid in org_csm_ids),
                "total_customers_with_clerk_id": len(all_customer_ids),
                "org_config_csm_count": len(org_csm_ids),
            }
        except Exception as e:
            _record_error("self_service_trialists_count", e)

        # Query 2 — proactive alerts grouped by csm_id
        try:
            q2_rows = (
                session.query(ProactiveAlert.csm_id, func.count(ProactiveAlert.id))
                .group_by(ProactiveAlert.csm_id)
                .order_by(func.count(ProactiveAlert.id).desc())
                .limit(50)
                .all()
            )
            queries["proactive_alerts_by_csm"] = [
                {"csm_id": cid, "count": cnt} for cid, cnt in q2_rows
            ]
        except Exception as e:
            _record_error("proactive_alerts_by_csm", e)

        # Query 3 — digest_logs with signal_count = 0
        try:
            empty_count = session.query(func.count(DigestLog.id)).filter(
                DigestLog.signal_count == 0
            ).scalar() or 0
            total_logs = session.query(func.count(DigestLog.id)).scalar() or 0
            success_logs = session.query(func.count(DigestLog.id)).filter(
                DigestLog.status == "success"
            ).scalar() or 0
            queries["empty_digests_count"] = {
                "empty_signal_count_rows": int(empty_count),
                "total_digest_log_rows":   int(total_logs),
                "success_status_rows":     int(success_logs),
                "fraction_empty": (
                    round(empty_count / total_logs, 3) if total_logs > 0 else None
                ),
            }
        except Exception as e:
            _record_error("empty_digests_count", e)

        # Query 4 — canceled users in privacy breach
        # Keyed off canceled_at (primary); trial_ends_at is a fallback only
        # for pre-column legacy rows. 30-day cutoff measures the POLICY
        # ceiling — the sweep clears at 29 days, so this should read 0.
        # Comp accounts are excluded so the metric reflects what the sweep
        # would actually purge.
        try:
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(days=30)
            canceled = session.query(Customer).filter(
                Customer.subscription_status == "canceled",
                Customer.is_comp_account.is_(False),
            ).all()

            def _effective_ts(c):
                return as_utc(c.canceled_at) or as_utc(c.trial_ends_at)

            old_canceled = [
                c for c in canceled
                if _effective_ts(c) and _effective_ts(c) < cutoff
            ]
            effective_ts_list = [
                _effective_ts(c) for c in old_canceled if _effective_ts(c)
            ]
            earliest = min(effective_ts_list, default=None)
            latest = max(effective_ts_list, default=None)
            queries["canceled_users_in_privacy_breach"] = {
                "count": len(old_canceled),
                "earliest_canceled_at": earliest.isoformat() if earliest else None,
                "latest_canceled_at":   latest.isoformat() if latest else None,
                "total_canceled":       len(canceled),
                "_proxy_note": (
                    "canceled_at is primary; trial_ends_at is a fallback for "
                    "pre-column legacy rows; comp accounts excluded."
                ),
            }
        except Exception as e:
            _record_error("canceled_users_in_privacy_breach", e)

        # Query 5 — trialing pipeline (clerk_user_id, trial_ends_at)
        try:
            q5_rows = (
                session.query(Customer.clerk_user_id, Customer.trial_ends_at)
                .filter(Customer.subscription_status == "trialing")
                .order_by(Customer.trial_ends_at.asc().nullslast())
                .limit(50)
                .all()
            )
            queries["trial_end_pipeline"] = [
                {
                    "clerk_user_id": cid,
                    "trial_ends_at": ends.isoformat() if ends else None,
                }
                for cid, ends in q5_rows
            ]
        except Exception as e:
            _record_error("trial_end_pipeline", e)

        # Query 6 — @noreply.clerk users
        try:
            noreply_count = session.query(func.count(Customer.id)).filter(
                Customer.email.like("%@noreply.clerk%")
            ).scalar() or 0
            total_customers = session.query(func.count(Customer.id)).scalar() or 0
            queries["noreply_clerk_users"] = {
                "count": int(noreply_count),
                "total_customers": int(total_customers),
            }
        except Exception as e:
            _record_error("noreply_clerk_users", e)

        # Query 7 — expired/revoked connections
        try:
            q7_rows = (
                session.query(Connection.status, func.count(Connection.id))
                .filter(Connection.status.in_(["expired", "revoked"]))
                .group_by(Connection.status)
                .all()
            )
            active_count = session.query(func.count(Connection.id)).filter(
                Connection.status == "active"
            ).scalar() or 0
            queries["expired_revoked_connections"] = {
                "rows": [{"status": s, "count": int(c)} for s, c in q7_rows],
                "active_count": int(active_count),
            }
        except Exception as e:
            _record_error("expired_revoked_connections", e)

        # Query — contact_names_per_account: read cached UserContextSnapshot
        # for a single CSM and surface per-account contact lists plus a grep
        # over the raw context_json for Marcus/Reyes mentions. No live MCP fan-out.
        try:
            from product.database import UserContextSnapshot

            target_csm = target_csm_param

            _cache_caveats = {
                "per_account_contact_cap": 3,
                "total_contact_cap": 60,
                "note": (
                    "contact_count reflects cached state, not live HubSpot. "
                    "Per-account contacts are capped at 3 in the cache writer "
                    "(context_cache.py:178); flat contact list is capped at 60 "
                    "(context_cache.py:320). For ground truth, use "
                    "?include_hubspot=1 with live MCP probe."
                ),
            }
            _grep_patterns = ["marcus", "marc", "reyes"]

            snapshot = (
                session.query(UserContextSnapshot)
                .filter_by(csm_id=target_csm)
                .first()
            )

            if snapshot is None:
                queries["contact_names_per_account"] = {
                    "csm_id": target_csm,
                    "snapshot_age_minutes": None,
                    "snapshot_status": "missing",
                    "accounts": [],
                    "marcus_grep": {
                        "patterns_searched": _grep_patterns,
                        "matches": [],
                    },
                    "cache_caveats": _cache_caveats,
                }
            else:
                refreshed = snapshot.refreshed_at
                if refreshed is not None:
                    if refreshed.tzinfo is None:
                        refreshed = refreshed.replace(tzinfo=timezone.utc)
                    age_minutes = int(round(
                        (datetime.now(timezone.utc) - refreshed).total_seconds() / 60
                    ))
                else:
                    age_minutes = None

                raw_json = snapshot.context_json or "{}"
                try:
                    ctx = json.loads(raw_json)
                except (TypeError, ValueError):
                    ctx = {}

                cached_accounts = ctx.get("accounts") if isinstance(ctx, dict) else None
                cached_contacts = ctx.get("contacts") if isinstance(ctx, dict) else None
                if not isinstance(cached_accounts, list):
                    cached_accounts = []
                if not isinstance(cached_contacts, list):
                    cached_contacts = []

                contacts_by_account: dict = {}
                for _c in cached_contacts:
                    if not isinstance(_c, dict):
                        continue
                    acct_name = _c.get("account_name") or ""
                    contact_label = _c.get("name") or _c.get("email") or ""
                    contacts_by_account.setdefault(acct_name, []).append(contact_label)

                accounts_out = []
                for acct in cached_accounts:
                    if not isinstance(acct, dict):
                        continue
                    acct_name = acct.get("name") or ""
                    names = contacts_by_account.get(acct_name, [])
                    accounts_out.append({
                        "account_name": acct_name,
                        "contact_count": len(names),
                        "contact_names": names,
                    })

                seen_contexts: set = set()
                matches: list = []
                for pat in _grep_patterns:
                    for m in re.finditer(re.escape(pat), raw_json, re.IGNORECASE):
                        start = max(0, m.start() - 50)
                        end = min(len(raw_json), m.end() + 50)
                        context_str = raw_json[start:end]
                        key = (pat, context_str)
                        if key in seen_contexts:
                            continue
                        seen_contexts.add(key)
                        matches.append({"pattern": pat, "context": context_str})

                queries["contact_names_per_account"] = {
                    "csm_id": target_csm,
                    "snapshot_age_minutes": age_minutes,
                    "snapshot_status": snapshot.refresh_status or "unknown",
                    "accounts": accounts_out,
                    "marcus_grep": {
                        "patterns_searched": _grep_patterns,
                        "matches": matches,
                    },
                    "cache_caveats": _cache_caveats,
                }
        except Exception as e:
            _record_error("contact_names_per_account", e)

        # Query: connection_state_per_user. Per-CSM Connection table state
        # plus filesystem token cross-reference and a per-user replication of
        # the /pilot/health logic. Default mode reads DB + filesystem only;
        # ?include_live_gmail=1 opts in to running the live gmail load
        # (with refresh side effects) capped at 20 users.
        try:
            from product.database import Customer as _AuditCustomer
            from product.database import Connection as _AuditConnection
            from product.csm_id_validation import validate_csm_id as _audit_vcsm

            _include_live_gmail = request.query_params.get(
                "include_live_gmail", "0"
            ) in ("1", "true", "yes")

            _total_conn_count = session.query(func.count(_AuditConnection.id)).scalar() or 0
            _non_active_count = session.query(func.count(_AuditConnection.id)).filter(
                _AuditConnection.status != "active"
            ).scalar() or 0
            _non_gmail_count = session.query(func.count(_AuditConnection.id)).filter(
                _AuditConnection.provider != "gmail"
            ).scalar() or 0

            _arch_findings = {
                "connection_status_is_dead_infrastructure": (
                    _total_conn_count > 0 and _non_active_count == 0
                ),
                "connection_table_only_holds_gmail": (
                    _total_conn_count > 0 and _non_gmail_count == 0
                ),
                "scopes_granted_never_read": True,
                # Stage 2a token encryption removed the FS read; the audit
                # no longer inspects filesystem JSON for calendar scope.
                "pilot_health_check_uses_filesystem_for_calendar_scope": False,
                "summary_note": (
                    "These findings characterize the codebase, not the data. "
                    "They are constants until the schema or read paths change."
                ),
            }

            _all_customers = session.query(_AuditCustomer).all()
            _users_total = len(_all_customers)

            if _include_live_gmail and _users_total > 20:
                queries["connection_state_per_user"] = {
                    "error": (
                        f"users_total={_users_total} exceeds the 20-user cap "
                        f"for include_live_gmail=1. The live check would "
                        f"trigger up to {_users_total} Google refresh "
                        f"attempts in one request and burn rate budget. "
                        f"Run a narrower audit (per csm_id) or unset the flag."
                    ),
                    "users_total": _users_total,
                    "architectural_findings": _arch_findings,
                }
            else:
                try:
                    from product.org_config import load_org_config as _load_org
                    _org_cfg = _load_org()
                    _crm_type = _org_cfg.get("crm_type", "") if isinstance(_org_cfg, dict) else ""
                except Exception:
                    _crm_type = ""

                _all_conns = session.query(_AuditConnection).all()
                _conn_index: dict = {}
                for _c in _all_conns:
                    _conn_index.setdefault(_c.customer_id, {})[_c.provider] = _c

                _users_out = []
                _summary = {
                    "users_with_status_lying": 0,
                    "users_with_scope_mismatch": 0,
                    "users_with_calendar_no_token": 0,
                    "users_with_gmail_no_token": 0,
                    "users_total": _users_total,
                }

                for _cust in _all_customers:
                    _csm_id = _cust.clerk_user_id or _cust.id
                    _user_email = _cust.email

                    try:
                        _safe_csm = _audit_vcsm(_csm_id)
                    except Exception:
                        _safe_csm = None

                    # FS read removed in Stage 2a token encryption (Postgres-only model).
                    # Column preserved for response shape compatibility; will always report missing.
                    _fs_token_block = {
                        "path_checked": [],
                        "found_at": None,
                        "scopes_in_file": None,
                        "has_calendar_scope": False,
                        "has_gmail_scope": False,
                    }

                    # Connection.customer_id can hold either Clerk user_id
                    # or Customer.id depending on the write path. Try
                    # clerk_user_id first since the OAuth callback writes
                    # that, fall back to Customer.id for any legacy or
                    # future rows that use UUIDs.
                    _user_conns = (
                        _conn_index.get(_cust.clerk_user_id, {})
                        or _conn_index.get(_cust.id, {})
                    )

                    def _build_block(conn):
                        if conn is None:
                            return {
                                "status": "missing",
                                "scopes_granted": None,
                                "token_data_present": False,
                                "token_data_appears_valid": False,
                                "last_verified_at": None,
                                "last_verified_age_minutes": None,
                            }
                        _scopes_list = (
                            conn.scopes_granted.split() if conn.scopes_granted else None
                        )
                        _td = conn.token_data if isinstance(conn.token_data, dict) else None
                        _td_present = _td is not None
                        _td_valid = bool(_td and _td.get("access_token"))
                        if conn.last_verified_at:
                            _lv = conn.last_verified_at
                            if _lv.tzinfo is None:
                                _lv = _lv.replace(tzinfo=timezone.utc)
                            _age = int(round(
                                (datetime.now(timezone.utc) - _lv).total_seconds() / 60
                            ))
                            _lv_iso = _lv.isoformat()
                        else:
                            _age = None
                            _lv_iso = None
                        return {
                            "status": conn.status or "active",
                            "scopes_granted": _scopes_list,
                            "token_data_present": _td_present,
                            "token_data_appears_valid": _td_valid,
                            "last_verified_at": _lv_iso,
                            "last_verified_age_minutes": _age,
                        }

                    _gmail_conn = _user_conns.get("gmail")
                    _gmail_block = _build_block(_gmail_conn)
                    _gmail_block["filesystem_token"] = _fs_token_block

                    _live_check = {
                        "ran": False,
                        "result": None,
                        "refresh_attempted": False,
                        "refresh_succeeded": False,
                    }
                    if _include_live_gmail and _safe_csm:
                        _live_check["ran"] = True
                        try:
                            from product.digest.delivery import _load_credentials as _lc_live
                            _was_expired_pre = None
                            try:
                                from product.auth_google import load_google_token as _lgt
                                _pre = _lgt(_safe_csm)
                                if _pre and "expiry" in _pre:
                                    _was_expired_pre = True
                            except Exception:
                                pass
                            _creds_live = _lc_live(_safe_csm)
                            _live_check["result"] = "ok"
                            _live_check["refresh_attempted"] = bool(_was_expired_pre)
                            _live_check["refresh_succeeded"] = (
                                bool(_was_expired_pre) and _creds_live is not None
                                and not _creds_live.expired
                            )
                        except Exception as _le:
                            _live_check["result"] = (str(_le)[:200]) or "no_token"
                    _gmail_block["live_gmail_check"] = _live_check
                    _gmail_block["_data_source_caveat"] = (
                        "Connection.customer_id can hold either Clerk user "
                        "IDs or Customer.id UUIDs depending on write path; "
                        "this lookup tries both."
                    )

                    _calendar_conn = _user_conns.get("calendar")
                    _calendar_block = _build_block(_calendar_conn)
                    _calendar_block["filesystem_token"] = _fs_token_block
                    _calendar_block["_data_source_caveat"] = (
                        "Connection rows for calendar are never written. "
                        "status='missing' here is structural, not bug-specific. "
                        "Calendar token state lives in filesystem JSON or in "
                        "the gmail row's combined token_data."
                    )

                    _hubspot_conn = _user_conns.get("hubspot")
                    _hubspot_block = _build_block(_hubspot_conn)
                    _hubspot_block["_data_source_caveat"] = (
                        "Connection rows for hubspot are never written. "
                        "HubSpot token state lives in product.connectors."
                        "hubspot_mcp's separate store."
                    )

                    if _include_live_gmail:
                        _ph_gmail = "ok" if _live_check.get("result") == "ok" else "no_token"
                    else:
                        _ph_gmail = "skipped_live_check_disabled"

                    # FS read removed in Stage 2a. With Postgres-only token storage,
                    # this signal degrades to a constant "no_token" — the audit no
                    # longer inspects filesystem JSON for calendar scope.
                    _ph_calendar = "no_token"

                    if _crm_type:
                        try:
                            from product.connectors.registry import get_connector as _ph_gc
                            _ph_gc(_csm_id, {"crm": _crm_type})
                            _ph_crm = "ok"
                        except Exception as _ce:
                            _ph_crm = "mock" if "mock" in str(_ce).lower() else "error"
                    else:
                        _ph_crm = "not_configured"

                    _pilot_health = {
                        "gmail_api": _ph_gmail,
                        "calendar_api": _ph_calendar,
                        "crm_connector": _ph_crm,
                    }

                    if (_gmail_conn is not None
                            and (_gmail_conn.status or "active") == "active"
                            and (not _gmail_block["token_data_present"]
                                 or not _gmail_block["token_data_appears_valid"])):
                        _summary["users_with_status_lying"] += 1

                    # users_with_scope_mismatch counted FS-scope-vs-DB mismatches.
                    # With FS read removed (Stage 2a), this signal degrades to 0.

                    if _ph_calendar == "no_token":
                        _summary["users_with_calendar_no_token"] += 1

                    if _include_live_gmail and _ph_gmail == "no_token":
                        _summary["users_with_gmail_no_token"] += 1

                    _users_out.append({
                        "csm_id": _csm_id,
                        "user_email": _user_email,
                        "providers": {
                            "gmail": _gmail_block,
                            "calendar": _calendar_block,
                            "hubspot": _hubspot_block,
                        },
                        "pilot_health_check_result": _pilot_health,
                    })

                queries["connection_state_per_user"] = {
                    "connection_state_summary": _summary,
                    "architectural_findings": _arch_findings,
                    "users": _users_out,
                }
        except Exception as e:
            _record_error("connection_state_per_user", e)

        # Query: connection_provider_counts. Provider breakdown of the
        # connections table plus three diagnostic counts that disambiguate
        # the dual-key Connection.customer_id situation (clerk_user_id vs.
        # Customer.id UUID vs. orphan).
        try:
            def _iso(dt):
                if dt is None:
                    return None
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.isoformat()

            _prov_rows = (
                session.query(
                    Connection.provider,
                    func.count(Connection.id),
                    func.min(Connection.created_at),
                    func.max(Connection.created_at),
                )
                .group_by(Connection.provider)
                .order_by(func.count(Connection.id).desc(), Connection.provider.asc())
                .all()
            )
            _by_provider = [
                {
                    "provider": _prov,
                    "count": int(_cnt or 0),
                    "earliest_created_at": _iso(_earliest),
                    "latest_created_at": _iso(_latest),
                }
                for _prov, _cnt, _earliest, _latest in _prov_rows
            ]
            _total_rows = session.query(func.count(Connection.id)).scalar() or 0
            _distinct_providers = sorted({p["provider"] for p in _by_provider})

            # Diagnostic counts: which key space does customer_id live in?
            # clerk_subq excludes NULL clerk_user_id rows so NOT IN behaves
            # consistently across Postgres and SQLite.
            _clerk_subq = session.query(Customer.clerk_user_id).filter(
                Customer.clerk_user_id.isnot(None)
            )
            _uuid_subq = session.query(Customer.id)

            _rows_non_clerk = session.query(func.count(Connection.id)).filter(
                ~Connection.customer_id.in_(_clerk_subq)
            ).scalar() or 0
            _rows_uuid = session.query(func.count(Connection.id)).filter(
                Connection.customer_id.in_(_uuid_subq)
            ).scalar() or 0
            _rows_orphan = session.query(func.count(Connection.id)).filter(
                ~Connection.customer_id.in_(_clerk_subq),
                ~Connection.customer_id.in_(_uuid_subq),
            ).scalar() or 0

            queries["connection_provider_counts"] = {
                "by_provider": _by_provider,
                "total_rows": int(_total_rows),
                "distinct_providers": _distinct_providers,
                "rows_with_non_clerk_customer_id": int(_rows_non_clerk),
                "rows_with_uuid_customer_id": int(_rows_uuid),
                "rows_with_orphan_customer_id": int(_rows_orphan),
            }
        except Exception as e:
            _record_error("connection_provider_counts", e)

        # Query: connection_status_distribution. Status-value breakdown of the
        # connections table to disambiguate whether non-"active" statuses
        # ("expired", "revoked") referenced by admin reads at main.py:5930-5932
        # are produced by any code path or are dead aspirational branches.
        # Samples cap at 3 rows per status and EXCLUDE token_data.
        try:
            def _iso_status(dt):
                if dt is None:
                    return None
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.isoformat()

            _status_rows = (
                session.query(
                    Connection.status,
                    func.count(Connection.id),
                    func.min(Connection.created_at),
                    func.max(Connection.created_at),
                )
                .group_by(Connection.status)
                .order_by(func.count(Connection.id).desc(), Connection.status.asc())
                .all()
            )
            _by_status = []
            for _status, _cnt, _earliest, _latest in _status_rows:
                _samples = (
                    session.query(Connection)
                    .filter(Connection.status == _status)
                    .order_by(Connection.created_at.desc())
                    .limit(3)
                    .all()
                )
                _by_status.append({
                    "status": _status,
                    "count": int(_cnt or 0),
                    "earliest_created_at": _iso_status(_earliest),
                    "latest_created_at": _iso_status(_latest),
                    "samples": [
                        {
                            "id": _s.id,
                            "customer_id": _s.customer_id,
                            "provider": _s.provider,
                            "last_verified_at": _iso_status(_s.last_verified_at),
                            "created_at": _iso_status(_s.created_at),
                        }
                        for _s in _samples
                    ],
                })
            _distinct_statuses = sorted(
                {s["status"] for s in _by_status if s["status"] is not None}
            )
            queries["connection_status_distribution"] = {
                "by_status": _by_status,
                "distinct_statuses": _distinct_statuses,
                "total_rows": int(sum(s["count"] for s in _by_status)),
            }
        except Exception as e:
            _record_error("connection_status_distribution", e)

    finally:
        session.close()

    # ── Query 8 — HubSpot contacts-per-account distribution ──────────────────
    if not include_hubspot:
        queries["contact_distribution_per_account"] = {
            "skipped": True,
            "reason": "include_hubspot=1 not set — query 8 makes 15+ live MCP calls",
        }
    else:
        try:
            target_csm = target_csm_param
            from product.connectors.registry import get_connector
            from product.org_config import get_csm_config

            try:
                cfg = get_csm_config(target_csm)
                target_accounts = cfg.get("accounts", [])
                if not isinstance(target_accounts, list):
                    target_accounts = []
            except Exception as _ce:
                target_accounts = []
                errors.append({"query": "contact_distribution_per_account",
                              "error": f"could not load csm config: {_ce}"})

            connector = get_connector(target_csm, {"crm": "hubspot"})
            distribution = []
            # Bypass cache — query each company's contacts via direct call.
            # We pass limit=100 to count beyond the production [:3] cap.

            async def _count_contacts_for_account(name: str) -> dict:
                try:
                    company_id = connector._resolve_company_id(name)
                    if company_id is None:
                        return {"account_name": name, "total_contacts": None,
                                "error": "company_id not resolved"}
                    raw, _status = await connector._mcp_call_tool(
                        connector._find_tool("contact", "search_crm"),
                        {
                            "objectType": "contacts",
                            "filterGroups": [{
                                "associatedWith": [{
                                    "objectType": "companies",
                                    "operator": "EQUAL",
                                    "objectIdValues": [int(company_id)],
                                }],
                            }],
                            "properties": ["email"],
                            "limit": 100,
                        },
                    )
                    parsed = connector._parse_json_response(raw)
                    items = (
                        parsed if isinstance(parsed, list)
                        else (parsed.get("results", []) if isinstance(parsed, dict) else [])
                    )
                    n = len(items)
                    return {
                        "account_name": name,
                        "total_contacts": n,
                        "hit_query_cap": n >= 100,
                    }
                except Exception as _ce:
                    return {"account_name": name, "total_contacts": None,
                            "error": str(_ce)[:200]}

            distribution = []
            # 0.5s rate-limit between calls (matches context_cache.py pattern).
            # Endpoint is `async def`, so we await directly — using
            # asyncio.run() here would error with "cannot be called from a
            # running event loop".
            for _idx, _name in enumerate(target_accounts[:20]):
                if _idx > 0:
                    await asyncio.sleep(0.5)
                distribution.append(await _count_contacts_for_account(_name))
            queries["contact_distribution_per_account"] = {
                "csm_id": target_csm,
                "accounts_queried": len(distribution),
                "rows": distribution,
                "rows_with_more_than_3_contacts": sum(
                    1 for r in distribution
                    if r.get("total_contacts") is not None and r["total_contacts"] > 3
                ),
            }
        except Exception as e:
            _record_error("contact_distribution_per_account", e)

    return JSONResponse({
        "ran_at": ran_at,
        "queries": queries,
        "errors": errors,
    })


@app.post("/admin/cache-invalidate")
async def admin_cache_invalidate(request: Request):
    """Force-invalidate a user's context cache so the next chat uses live CRM data.
    Body: {"csm_id": "user_xxx"}. Requires X-Admin-Key header."""
    admin_secret = os.getenv("ADMIN_SECRET", "")
    if not admin_secret or not secrets.compare_digest(request.headers.get("x-admin-key", ""), admin_secret):
        raise HTTPException(status_code=403, detail="Forbidden")
    body = await request.json()
    csm_id = (body.get("csm_id") or "").strip()
    if not csm_id:
        return JSONResponse({"error": "csm_id required"}, status_code=400)
    try:
        from product.database import SessionLocal, UserContextSnapshot
        if not SessionLocal:
            return JSONResponse({"error": "database not configured"}, status_code=503)
        session = SessionLocal()
        try:
            row = session.query(UserContextSnapshot).filter_by(csm_id=csm_id).first()
            if row:
                session.delete(row)
                session.commit()
                return JSONResponse({"status": "deleted", "csm_id": csm_id})
            return JSONResponse({"status": "not_found", "csm_id": csm_id})
        finally:
            session.close()
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/admin/digest-reset")
async def admin_digest_reset(request: Request):
    """Clear the digest in-flight flag and optionally delete today's digest file.
    Body: {"csm_id": "user_xxx", "delete_file": false}. Requires X-Admin-Key header."""
    admin_secret = os.getenv("ADMIN_SECRET", "")
    if not admin_secret or not secrets.compare_digest(request.headers.get("x-admin-key", ""), admin_secret):
        raise HTTPException(status_code=403, detail="Forbidden")
    body = await request.json()
    csm_id = _validate_csm_id((body.get("csm_id") or "").strip())

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dedup_key = f"{csm_id}:{today}"
    was_in_flight = dedup_key in _digest_in_flight
    _digest_in_flight.pop(dedup_key, None)

    file_deleted = False
    if body.get("delete_file"):
        digest_path = _safe_data_path("product/data/digests", csm_id, f"{today}.json")
        if digest_path.exists():
            digest_path.unlink()
            file_deleted = True

    return JSONResponse({
        "status": "reset",
        "csm_id": csm_id,
        "was_in_flight": was_in_flight,
        "flag_cleared": True,
        "file_deleted": file_deleted,
    })


@app.post("/admin/trigger-digest")
async def admin_trigger_digest(request: Request):
    """Force-generate a digest synchronously. Bypasses arq worker. Requires X-Admin-Key."""
    admin_secret = os.getenv("ADMIN_SECRET", "")
    if not admin_secret or not secrets.compare_digest(request.headers.get("x-admin-key", ""), admin_secret):
        raise HTTPException(status_code=403, detail="Forbidden")
    body = await request.json()
    csm_id = _validate_csm_id((body.get("csm_id") or "").strip())

    # Clear in-flight flag and delete stale file first
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _digest_in_flight.pop(f"{csm_id}:{today}", None)
    digest_path = _safe_data_path("product/data/digests", csm_id, f"{today}.json")
    if digest_path.exists():
        digest_path.unlink()

    try:
        # _run_digest_for_user is sync and contains asyncio.run() calls
        # (Stage 4 up-front refresh, Drive transcript fetch, and the
        # refresh inside score_account_urgency). Calling it inline from
        # this async handler keeps it on the event-loop thread, so those
        # asyncio.run() calls raise "cannot be called from a running event
        # loop" and the digest degrades to empty. asyncio.to_thread runs it
        # on a worker thread with no loop (same pattern as
        # admin_trigger_absence_detection and arq job_generate_digest).
        await asyncio.to_thread(_run_digest_for_user, csm_id)
        return JSONResponse({"status": "complete", "csm_id": csm_id})
    except Exception as exc:
        return JSONResponse({"status": "failed", "error": str(exc)}, status_code=500)


@app.post("/admin/trigger-postcall-tick")
async def admin_trigger_postcall_tick(request: Request):
    """Force-run the post-call scheduler tick synchronously.

    Bypasses the 5-min IntervalTrigger cadence so demo / CASA-video
    retakes don't wait on the next natural tick. Iterates every
    active CSM in org_config, same as the production scheduler.
    Returns when run_postcall_tick() returns so the caller knows
    the pipeline has finished.

    Requires X-Admin-Key header — same gate as /admin/trigger-digest.
    """
    admin_secret = os.getenv("ADMIN_SECRET", "")
    if not admin_secret or not secrets.compare_digest(
        request.headers.get("x-admin-key", ""), admin_secret
    ):
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        from product.postcall.scheduler import run_postcall_tick
        run_postcall_tick()
        return JSONResponse({"status": "complete"})
    except Exception as exc:
        logger.exception("admin/trigger-postcall-tick failed")
        return JSONResponse({"status": "failed", "error": str(exc)}, status_code=500)


@app.post("/admin/trigger-absence-detection")
async def admin_trigger_absence_detection(request: Request):
    """Force-run absence detection for a single CSM synchronously.

    Bypasses the 8am/12pm/4pm APScheduler cadence so post-deploy
    verification doesn't wait on the next natural fire. Body:
        {"csm_id": "<clerk_user_id>"}

    Runs the full F1 pipeline + the Sprint 6 H.1 per-account urgency
    aggregator for just that one CSM. Eligibility filter still applies
    — non-eligible targets short-circuit with zero work. Returns
    counts so the caller can verify what fired without separately
    querying Postgres.

    Note: ~10-30s per call because score_account_urgency
    force-refreshes context_cache (Decision 4). Admin-only; not
    rate-limited beyond the X-Admin-Key gate.

    Requires X-Admin-Key header — 403 on missing/wrong key, matching
    /admin/trigger-postcall-tick. (The Phase 3 spec said 401; the
    existing codebase pattern is 403, so 403 it is.)
    """
    admin_secret = os.getenv("ADMIN_SECRET", "")
    if not admin_secret or not secrets.compare_digest(
        request.headers.get("x-admin-key", ""), admin_secret
    ):
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    raw_csm_id = body.get("csm_id", "") if isinstance(body, dict) else ""
    if not raw_csm_id:
        raise HTTPException(status_code=400, detail="Missing csm_id")

    # _validate_csm_id raises HTTPException(400) on malformed input —
    # bare "..", null bytes, out-of-charset, length > 64, etc.
    csm_id = _validate_csm_id(raw_csm_id)

    try:
        # _run_absence_detection is sync and contains an asyncio.run() call
        # inside score_account_urgency (which force-refreshes context_cache).
        # Calling it inline from this async handler keeps the function on
        # the same thread as the running event loop, which makes asyncio.run
        # raise "cannot be called from a running event loop" — silently
        # bypassing the force-refresh.
        # asyncio.to_thread runs it on a worker thread with no loop, so the
        # inner asyncio.run() works (same pattern as arq's job_detect_absence
        # in product/worker.py). Bonus: keeps the FastAPI event loop free
        # during the ~10-30s pipeline runtime.
        result = await asyncio.to_thread(_run_absence_detection, target_csm_id=csm_id)
        return JSONResponse({"status": "complete", "csm_id": csm_id, **result})
    except Exception as exc:
        logger.exception("admin/trigger-absence-detection failed")
        return JSONResponse({"status": "failed", "error": str(exc)}, status_code=500)


# ── /status — public health status page ──────────────────────────────────────

_status_cache: dict = {"data": None, "expires_at": 0.0}


def _run_status_checks() -> list:
    """Run all health checks and return a list of result dicts.
    Each dict: {name, status ("ok"|"degraded"|"down"), detail}
    """
    results = []

    # ── Gmail API ─────────────────────────────────────────────────────────────
    # Reads Connection.status (kept fresh by the hourly probe_all_connections
    # job) for active, non-demo customers. No live Google call from here.
    try:
        from product.database import SessionLocal, Customer, Connection
        if not SessionLocal:
            raise RuntimeError("DB not configured")
        _sess = SessionLocal()
        try:
            _custs = (
                _sess.query(Customer.clerk_user_id, Customer.email)
                .filter(Customer.subscription_status.in_(["trialing", "active"]))
                .all()
            )
            _ids = [
                cid for cid, email in _custs
                if cid and not is_demo_csm_id(cid)
                and not is_demo_email(email)
            ]
            _conns = (
                _sess.query(Connection.customer_id, Connection.status)
                .filter(Connection.customer_id.in_(_ids))
                .filter(Connection.provider.in_(["google", "gmail"]))
                .all()
            ) if _ids else []
        finally:
            _sess.close()
        _by_csm: dict = {}
        for cid, st in _conns:
            _by_csm.setdefault(cid, []).append(st)
        if not _by_csm:
            results.append({"name": "Gmail API", "status": "ok", "detail": "No active connections"})
        else:
            _bad = [cid for cid, sts in _by_csm.items() if any(s != "active" for s in sts)]
            if _bad:
                results.append({"name": "Gmail API", "status": "degraded", "detail": f"{len(_bad)} of {len(_by_csm)} connections need attention"})
            else:
                results.append({"name": "Gmail API", "status": "ok", "detail": "Operational"})
    except Exception as _e:
        logger.warning("status check failed: gmail: %s", _e)
        results.append({"name": "Gmail API", "status": "degraded", "detail": "Check failed"})

    # ── HubSpot API ───────────────────────────────────────────────────────────
    # Unauthenticated reachability ping only. Says nothing about any user's
    # HubSpot connection; per-user state lives in Connection.status.
    try:
        _hs_resp = requests.get(
            "https://api.hubapi.com/crm/v3/schemas",
            timeout=4,
        )
        # 401 = reachable but no auth (expected without a key — that's fine)
        if _hs_resp.status_code in (200, 401):
            results.append({"name": "HubSpot API reachable", "status": "ok", "detail": "Ping OK"})
        else:
            results.append({"name": "HubSpot API reachable", "status": "degraded", "detail": f"HTTP {_hs_resp.status_code}"})
    except requests.exceptions.Timeout:
        results.append({"name": "HubSpot API reachable", "status": "degraded", "detail": "Timeout"})
    except Exception as _e:
        logger.warning("status check failed: hubspot ping: %s", _e)
        results.append({"name": "HubSpot API reachable", "status": "down", "detail": "Unreachable"})

    # ── Digest pipeline ───────────────────────────────────────────────────────
    # The digest is login-triggered (no cron), so a stale digest_logs row with
    # no sign-ins in the same window is expected idleness, not degradation.
    # Customer.last_login_at is bumped on every authenticated Clerk request.
    try:
        from sqlalchemy import func as _sql_func
        from product.database import SessionLocal, DigestLog, Customer
        if not SessionLocal:
            raise RuntimeError("DB not configured")
        _sess = SessionLocal()
        try:
            _log = (
                _sess.query(DigestLog)
                .order_by(DigestLog.created_at.desc())
                .first()
            )
            _last_login = _sess.query(_sql_func.max(Customer.last_login_at)).scalar()
        finally:
            _sess.close()
        _now = datetime.now(timezone.utc)
        _recent_login = (
            _last_login is not None
            and (_now - _last_login.replace(tzinfo=timezone.utc)).total_seconds() <= 24 * 3600
        )
        if _log is None:
            if _recent_login:
                results.append({"name": "Digest pipeline", "status": "degraded", "detail": "No digest runs on record"})
            else:
                results.append({"name": "Digest pipeline", "status": "ok", "detail": "Idle: no sign-ins in the last 24h"})
        else:
            _age_h = (_now - _log.created_at.replace(tzinfo=timezone.utc)).total_seconds() / 3600
            if _log.status == "success" and _age_h <= 24:
                results.append({"name": "Digest pipeline", "status": "ok", "detail": f"Last run {int(_age_h)}h ago"})
            elif _log.status != "success":
                results.append({"name": "Digest pipeline", "status": "down", "detail": f"Last run failed ({_log.status})"})
            elif not _recent_login:
                results.append({"name": "Digest pipeline", "status": "ok", "detail": "Idle: no sign-ins in the last 24h"})
            else:
                results.append({"name": "Digest pipeline", "status": "degraded", "detail": f"Last run {int(_age_h)}h ago (>24h)"})
    except Exception as _e:
        logger.warning("status check failed: digest pipeline: %s", _e)
        results.append({"name": "Digest pipeline", "status": "degraded", "detail": "Check failed"})

    # ── Stripe ────────────────────────────────────────────────────────────────
    # BILLING_ENABLED off: no Stripe row and no Stripe API call.
    if not billing_enabled():
        return results
    try:
        from product.billing import STRIPE_SECRET_KEY
        if not STRIPE_SECRET_KEY:
            raise ValueError("STRIPE_SECRET_KEY not set")
        import stripe as _stripe
        _stripe.api_key = STRIPE_SECRET_KEY
        _stripe.Balance.retrieve()
        results.append({"name": "Stripe", "status": "ok", "detail": "Operational"})
    except Exception as _e:
        logger.warning("status check failed: stripe: %s", _e)
        if "STRIPE_SECRET_KEY not set" in str(_e):
            results.append({"name": "Stripe", "status": "down", "detail": "Not configured"})
        else:
            results.append({"name": "Stripe", "status": "degraded", "detail": "Check failed"})

    return results


def _render_status_html(checks: list, checked_at: str) -> str:
    dot = {"ok": ("●", "#16a34a", "Operational"), "degraded": ("●", "#ca8a04", "Degraded"), "down": ("●", "#dc2626", "Down")}

    rows = ""
    overall = "ok"
    for c in checks:
        sym, color, label = dot.get(c["status"], ("●", "#6b7280", "Unknown"))
        if c["status"] == "down":
            overall = "down"
        elif c["status"] == "degraded" and overall != "down":
            overall = "degraded"
        rows += f"""
        <tr>
          <td style="padding:14px 16px;border-bottom:1px solid #f3f4f6;font-weight:500;">{c['name']}</td>
          <td style="padding:14px 16px;border-bottom:1px solid #f3f4f6;">
            <span style="color:{color};font-size:18px;line-height:1;">●</span>
            <span style="margin-left:8px;color:{color};font-weight:500;">{label}</span>
          </td>
          <td style="padding:14px 16px;border-bottom:1px solid #f3f4f6;color:#6b7280;font-size:14px;">{c['detail']}</td>
        </tr>"""

    banner_bg, banner_text_color, banner_msg = {
        "ok":       ("#f0fdf4", "#15803d", "All systems operational"),
        "degraded": ("#fefce8", "#854d0e", "Partial degradation"),
        "down":     ("#fef2f2", "#991b1b",  "Service disruption"),
    }[overall]

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>CS Agent Status</title>
  <style>
    @font-face {{
      font-family: 'DM Sans';
      font-style: normal;
      font-weight: 400;
      font-display: swap;
      src: url('/fonts/dm-sans-400.woff2') format('woff2');
    }}
    @font-face {{
      font-family: 'DM Sans';
      font-style: normal;
      font-weight: 500;
      font-display: swap;
      src: url('/fonts/dm-sans-500.woff2') format('woff2');
    }}
    @font-face {{
      font-family: 'DM Sans';
      font-style: normal;
      font-weight: 600;
      font-display: swap;
      src: url('/fonts/dm-sans-600.woff2') format('woff2');
    }}
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: "DM Sans", -apple-system, BlinkMacSystemFont, sans-serif;
      background: #ffffff;
      color: #111827;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
    }}
    .container {{ max-width: 680px; margin: 0 auto; padding: 60px 24px 40px; flex: 1; }}
    h1 {{ font-size: 24px; font-weight: 600; margin-bottom: 4px; }}
    .subtitle {{ color: #6b7280; font-size: 14px; margin-bottom: 32px; }}
    .banner {{
      padding: 14px 20px;
      border-radius: 8px;
      font-weight: 500;
      font-size: 15px;
      margin-bottom: 24px;
      background: {banner_bg};
      color: {banner_text_color};
    }}
    table {{ width: 100%; border-collapse: collapse; border: 1px solid #f3f4f6; border-radius: 8px; overflow: hidden; }}
    th {{
      text-align: left;
      padding: 11px 16px;
      font-size: 12px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: .05em;
      color: #6b7280;
      background: #f9fafb;
      border-bottom: 1px solid #f3f4f6;
    }}
    .checked-at {{ margin-top: 16px; font-size: 13px; color: #9ca3af; }}
    footer {{
      text-align: center;
      padding: 24px;
      font-size: 13px;
      color: #9ca3af;
      border-top: 1px solid #f3f4f6;
    }}
    footer a {{ color: #6b7280; text-decoration: none; }}
    footer a:hover {{ text-decoration: underline; }}
  </style>
</head>
<body>
  <div class="container">
    <h1>CS Agent Status</h1>
    <p class="subtitle">Real-time service health</p>
    <div class="banner">{banner_msg}</div>
    <table>
      <thead>
        <tr>
          <th>Service</th>
          <th>Status</th>
          <th>Detail</th>
        </tr>
      </thead>
      <tbody>{rows}
      </tbody>
    </table>
    <p class="checked-at">Last checked: {checked_at}</p>
  </div>
  <footer>
    Questions? Email <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a>
  </footer>
</body>
</html>"""


@app.get("/status")
async def status_page():
    """Public status page — no auth required. Cached for 60 seconds."""
    from fastapi.responses import HTMLResponse
    now = time.time()
    if _status_cache["data"] is None or now > _status_cache["expires_at"]:
        checks = _run_status_checks()
        checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        _status_cache["data"]       = (checks, checked_at)
        _status_cache["expires_at"] = now + 60
    checks, checked_at = _status_cache["data"]
    return HTMLResponse(_render_status_html(checks, checked_at))


@app.api_route("/pilot/health", methods=["GET", "HEAD"])
async def pilot_health():
    """Operational health check — returns per-CSM status and org-level summary."""
    from product.digest.delivery import _load_credentials as _lc
    from product.org_config import load_org_config as _loc, list_active_csms as _lac, get_csm_config as _gcc

    try:
        _org_cfg    = _loc()
        _org_name   = _org_cfg.get("org_name", "unknown")
        _crm_type   = _org_cfg.get("crm_type", "")
        _pilot_mode = _org_cfg.get("pilot_mode", "live")
    except Exception:
        _org_name   = "unknown"
        _crm_type   = ""
        _pilot_mode = "live"

    try:
        _csm_ids = _lac()
    except Exception:
        _csm_ids = [DEMO_CSM_ID] if demo_mode_enabled() else []

    csm_statuses: dict = {}
    last_digest_run: str | None = None

    for _cid in _csm_ids:
        _cs: dict = {}

        # name and accounts count
        try:
            _csm_cfg = _gcc(_cid)
            _cs["name"] = _csm_cfg.get("name", _cid)
            _csm_accts  = _csm_cfg.get("accounts", "auto")
            _cs["accounts_count"] = len(_csm_accts) if isinstance(_csm_accts, list) else "auto"
        except Exception:
            _cs["name"] = _cid
            _cs["accounts_count"] = "unknown"

        # gmail_api - can we currently produce a usable Google access token (refresh if needed)?
        # This is a LIVENESS check, not a state check. _load_credentials does an on-demand refresh
        # via the stored refresh_token, so "ok" here means "Gmail API call would succeed right now."
        # We deliberately do NOT migrate this block to ConnectionStateService - the service is
        # read-only by design (scoping doc Section 2) and reports `token_appears_expired=True`
        # for any token whose stored access_token has passed its 1-hour expiry, which would cause
        # /pilot/health to report `no_token` most of the day even when the connection is fine.
        # Sprint 6 will add `verify_connection_live()` to the service so this block can migrate
        # without losing refresh-on-demand semantics.
        _t_gmail = time.perf_counter()
        try:
            _lc(_cid)
            _cs["gmail_api"] = "ok"
            _gmail_result = "ok"
        except Exception as _exc:
            _cs["gmail_api"] = "no_token"
            _gmail_result = f"error:{type(_exc).__name__}"
        _gmail_ms = int((time.perf_counter() - _t_gmail) * 1000)
        logger.info(
            "event=pilot_health_gmail_probe csm_id=%s duration_ms=%d result=%s",
            _cid, _gmail_ms, _gmail_result,
        )

        # calendar_api - does this user have calendar scope granted?
        # This is a STATE check (was permission granted at OAuth time?), not a liveness check.
        # ConnectionStateService is the authoritative owner of granted_scopes; this migration
        # closes the 35-day Calendar token bug where the old filesystem reads returned no_token
        # for users whose tokens live in Postgres rather than filesystem JSON.
        # The substring check `any("calendar" in s for s in ...)` preserves today's matching
        # semantics (matches both calendar.readonly AND calendar.events). Sprint 6 follow-up
        # switches to exact-match composed with OR using product.scopes constants.
        _t_cal = time.perf_counter()
        try:
            from product.connection_service import get_connection_state
            _state = get_connection_state(_cid, "google")
            if not _state.has_token:
                _cs["calendar_api"] = "no_token"
                _cal_result = "no_token"
            elif any("calendar" in s for s in _state.granted_scopes):
                _cs["calendar_api"] = "ok"
                _cal_result = "ok"
            else:
                _cs["calendar_api"] = "no_scope"
                _cal_result = "no_scope"
            _cal_notes = ";".join(_state.diagnostic_notes) if _state.diagnostic_notes else ""
        except Exception as _exc:
            _cs["calendar_api"] = "error"
            _cal_result = f"error:{type(_exc).__name__}"
            _cal_notes = ""
        _cal_ms = int((time.perf_counter() - _t_cal) * 1000)
        logger.info(
            "event=pilot_health_calendar_probe csm_id=%s duration_ms=%d result=%s notes=%s",
            _cid, _cal_ms, _cal_result, _cal_notes,
        )

        # crm_connector
        try:
            if _crm_type:
                from product.connectors.registry import get_connector as _hgc
                _hgc(_cid, {"crm": _crm_type})
                _cs["crm_connector"] = "ok"
            else:
                _cs["crm_connector"] = "not_configured"
        except Exception as _ce:
            _cs["crm_connector"] = "mock" if "mock" in str(_ce).lower() else "error"

        # last_digest
        try:
            _ddir = Path(f"product/data/digests/{_cid}")
            if _ddir.exists():
                _dfiles = sorted(_ddir.glob("*.json"), reverse=True)
                if _dfiles:
                    _gen_at = json.loads(_dfiles[0].read_text(encoding="utf-8")).get("generated_at")
                    _cs["last_digest"] = _gen_at
                    if _gen_at and (last_digest_run is None or _gen_at > last_digest_run):
                        last_digest_run = _gen_at
                else:
                    _cs["last_digest"] = None
            else:
                _cs["last_digest"] = None
        except Exception:
            _cs["last_digest"] = None

        # last_alert
        try:
            _adir = Path(f"product/data/alerts/{_cid}")
            if _adir.exists():
                _aflst = sorted(_adir.glob("*.json"), reverse=True)
                _cs["last_alert"] = (
                    json.loads(_aflst[0].read_text(encoding="utf-8")).get("timestamp")
                    if _aflst else None
                )
            else:
                _cs["last_alert"] = None
        except Exception:
            _cs["last_alert"] = None

        # feedback_count
        try:
            _fdir = Path(f"product/data/feedback/{_cid}")
            _cs["feedback_count"] = len(list(_fdir.glob("*.json"))) if _fdir.exists() else 0
        except Exception:
            _cs["feedback_count"] = 0

        csm_statuses[_cid] = _cs

    _llm_ok   = bool(os.environ.get("ANTHROPIC_API_KEY"))
    _sched_ok = bool(_scheduler and _scheduler.running)
    _any_degraded = any(
        v in ("no_token", "error")
        for _cs in csm_statuses.values()
        for k, v in _cs.items()
        if k in ("gmail_api", "calendar_api")
    )
    overall_ok = _llm_ok and _sched_ok and not _any_degraded

    return JSONResponse(
        {
            "status": "healthy" if overall_ok else "degraded",
            "org": {
                "name":               _org_name,
                "pilot_mode":         _pilot_mode,
                "scheduler_running":  _sched_ok,
                "llm_api":            _llm_ok,
                "total_csms":         len(_csm_ids),
                "active_csms":        len(_csm_ids),
                "last_digest_run":    last_digest_run,
                "last_absence_check": _last_absence_check_at,
            },
            "csms": csm_statuses,
        },
        status_code=200 if overall_ok else 500,
    )


# Cache: csm_id -> (fetched_at_timestamp, crm_context_string)
# Refreshed when stale (> 5 minutes) or when the entry is absent.
_chat_crm_cache: dict[str, tuple[float, str]] = {}

def _build_gmail_signals_section(csm_id: str) -> str:
    """Build the GMAIL SIGNALS block for the /chat system prompt.

    Reads all fresh cached signals and filters to the email.* family
    (email.absence, email.received, email.new_stakeholder). Calendar or
    Drive signals landing in signal_cache later will not surface in this
    block — they get their own block when wired.

    Returns "" when no email signals are fresh — never a dangling header.
    """
    signals = [
        s for s in read_signals(csm_id)
        if s.get("signal_type", "").startswith("email.")
    ]
    if not signals:
        return ""

    now = datetime.now(timezone.utc)
    lines = []
    for s in signals:
        account = s.get("account", "")
        contact = s.get("contact", "")
        subject = s.get("thread_subject", "")
        thread_id = s.get("thread_id", "")
        stype = s.get("signal_type", "")

        if stype == "email.absence":
            days = s.get("days_waiting")
            descriptor = f"no reply in {days} days" if days else "no reply recently"
        elif stype == "email.received":
            try:
                ts = datetime.fromisoformat(s.get("timestamp", "").replace("Z", "+00:00"))
                days = (now - ts).days
                descriptor = f"inbound {days} days ago" if days >= 0 else "inbound recently"
            except (ValueError, TypeError):
                descriptor = "inbound recently"
        elif stype == "email.new_stakeholder":
            descriptor = "new stakeholder in thread"
        else:
            descriptor = stype

        lines.append(f'{account} — {contact} — "{subject}" — thread {thread_id} — {descriptor}')

    return (
        "=== GMAIL SIGNALS ===\n"
        + "\n".join(lines)
        + "\n\n"
        "Each line is a recent email thread identified by thread_id. "
        "See DRAFT_CARD instructions below for how to reference these.\n\n"
    )


# Shared DRAFT_CARD instruction block. Both the legacy single-shot system
# prompt and the new tool-use system prompt embed this verbatim so the
# regex-based draft-card parser stays compatible with both paths.
_DRAFT_CARD_GUIDANCE = (
    "When you suggest an action the CSM should take (logging a note, creating a follow-up task, "
    "updating a deal, or drafting an email to a contact), output a structured draft card using this exact format:\n\n"
    "<<<DRAFT_CARD>>>\n"
    "{\"label\": \"Short title e.g. 'Draft Email — Marcus Reyes'\", \"content\": \"Full draft text here\", "
    "\"action_type\": \"log_note or create_task or update_deal or create_contact or email_draft\", "
    "\"payload\": {<per-action fields — see below>}, \"account_name\": \"Account Name\"}\n"
    "<<<END_DRAFT_CARD>>>\n\n"
    "The card's `content` field is ALWAYS the full editable draft prose the CSM reviews. "
    "For free-text CRM actions (log_note, create_task) the written note/task detail comes from `content` — "
    "do NOT duplicate it into payload. The payload carries only the STRUCTURED fields for each action type:\n"
    "  - log_note: payload = {\"company_id\": \"<hubspot company id>\"}. The note text is the card's `content`.\n"
    "  - create_task: payload = {\"company_id\": \"<hubspot company id>\", \"title\": \"<short task title>\", \"due_date\": \"<YYYY-MM-DD or omit>\"}. "
    "The task detail is the card's `content`.\n"
    "  - update_deal: payload = {\"deal_id\": \"<hubspot deal id>\", \"properties\": {<only the changed deal fields>}}.\n"
    "  - create_contact: payload = {\"email\": \"<addr>\", \"firstname\": \"<name>\", \"lastname\": \"<name>\", \"company_id\": \"<hubspot company id>\"}.\n"
    "For email drafts: set action_type to email_draft. Payload MUST include `to` (recipient email address) and `subject`. "
    "payload.thread_id MUST be present for replies to a known thread and absent for net-new outreach.\n"
    "  - Replies to a known thread: if the CSM's ask clearly references a thread in the GMAIL SIGNALS block above "
    "(e.g. 'reply to the expansion thread'), copy that line's thread_id verbatim into payload.thread_id. "
    "Copy the contact email from that same GMAIL SIGNALS line into payload.to. "
    "Set payload.subject to \"Re: <thread_subject>\" using the subject from the same line.\n"
    "  - Net-new outreach: when the CSM asks for a new email with no specific thread in GMAIL SIGNALS "
    "(e.g. 'draft an intro to a new prospect'), omit thread_id from payload entirely — do not set it to null or an empty string, omit the key. "
    "Resolve payload.to from CRM context (contacts section above) when a specific contact is named. "
    "Compose payload.subject yourself — do not prefix with 'Re:'.\n"
    "  - Missing recipient: if the CSM asks for an email draft but no recipient is identifiable from GMAIL SIGNALS or CRM context, do NOT output a <<<DRAFT_CARD>>> block. "
    "Ask the CSM a direct clarifying question instead (e.g. 'Who should this go to?'). Never fabricate an email address, never use placeholder strings like '[recipient]' or 'TBD'.\n"
    "  - Example reply: {\"label\": \"Reply — Jane Example\", \"content\": \"Hi Jane...\", "
    "\"action_type\": \"email_draft\", \"payload\": {\"to\": \"jane@example.com\", \"subject\": \"Re: Expansion timing\", \"thread_id\": \"thr_example_1a2b3c\"}, "
    "\"account_name\": \"Example Corp\"}\n"
    "  - Example net-new: {\"label\": \"Intro — Example Inc\", \"content\": \"Hi there...\", "
    "\"action_type\": \"email_draft\", \"payload\": {\"to\": \"contact@example.com\", \"subject\": \"Quick intro from Clarify\"}, "
    "\"account_name\": \"Example Inc\"}\n"
    "For Slack messages: when the CSM wants to notify a teammate, post to a channel, or follow up internally, "
    "first call the slack_draft_message tool, then output a <<<DRAFT_CARD>>> with action_type slack_message. "
    "Payload MUST include `recipient` (a #channel name or a teammate name/email) and `message` (the draft text). "
    "The card sends only after the CSM approves it. Do not send Slack messages yourself.\n"
    "  - Example Slack: {\"label\": \"Slack - #customer-success\", \"content\": \"Heads up: Northgate renewal is at risk.\", "
    "\"action_type\": \"slack_message\", \"payload\": {\"recipient\": \"#customer-success\", \"message\": \"Heads up: Northgate renewal is at risk.\"}, "
    "\"account_name\": \"Northgate Logistics\"}\n"
    "You may output multiple draft cards in one response. Never describe what a draft would look like — output the <<<DRAFT_CARD>>> block directly.\n"
    "CS Agent never writes to HubSpot without explicit CSM approval.\n\n"
    "When the CSM responds to a post-call nudge with meeting notes or a call summary, do all of the following:\n"
    "1. Summarize the key takeaways in 3-5 concise bullet points\n"
    "2. Identify any follow-up actions, open questions, or commitments made on the call\n"
    "3. If the CSM asks to draft a follow-up email, output it as a <<<DRAFT_CARD>>> with action_type email_draft\n"
    "4. Suggest CRM updates as <<<DRAFT_CARD>>> blocks:\n"
    "   - log_note: call summary with key points and next steps\n"
    "   - update_deal: if deal stage, amount, or close date changed\n"
    "   - create_task: for each follow-up action with a due date"
)

# Pre-compiled regex used by both the legacy and tool-use chat paths.
_DRAFT_CARD_PATTERN = re.compile(
    r'<<<DRAFT_CARD>>>\s*(.*?)\s*<<<END_DRAFT_CARD>>>',
    re.DOTALL,
)


def _parse_draft_cards(text: str) -> tuple[list[dict], str]:
    """Strip <<<DRAFT_CARD>>>...<<<END_DRAFT_CARD>>> blocks from `text`.

    Returns (drafts, clean_reply). drafts is the list of payloads that
    parsed cleanly as JSON; malformed JSON blocks are silently dropped.
    clean_reply is `text` with every block removed, then trimmed.
    """
    matches = _DRAFT_CARD_PATTERN.findall(text or "")
    drafts: list[dict] = []
    for raw in matches:
        try:
            drafts.append(json.loads(raw))
        except json.JSONDecodeError:
            pass
    clean_reply = _DRAFT_CARD_PATTERN.sub('', text or '').strip()
    return drafts, clean_reply


def _build_tooluse_system_prompt(csm_name: str, today_str: str, csm_id: str) -> str:
    """System prompt for the tool-use chat handler.

    Slimmer than the legacy prompt: data is fetched on demand via tools
    rather than pre-stuffed into the prompt. The DRAFT_CARD guidance is
    appended verbatim so output parsing stays compatible across paths.

    The Zoom capability line is populated at call time from the user's Zoom
    token state (F-3): without it the model defaulted to "Zoom isn't
    connected" even for users who had connected it. Zoom recaps are not a
    callable tool — they arrive via webhook and surface through
    get_account_dossier's recent_activity — so this is guidance, not a tool
    entry, and TOOL_DEFINITIONS is intentionally left unchanged.
    """
    _zoom_connected = False
    try:
        _zoom_connected = load_zoom_token(csm_id) is not None
    except Exception as _zerr:
        logger.warning("Zoom token check failed for %s (treating as not connected): %s", csm_id, _zerr)
        _zoom_connected = False

    if _zoom_connected:
        zoom_line = (
            "  - Zoom is connected for this user. Recorded Zoom call recaps are processed "
            "automatically via webhook after each call and written into the account's CRM "
            "timeline as notes — there is no Zoom tool to call. When the user asks about a "
            "recent Zoom call or what was said on it, call get_account_dossier for the "
            "relevant account and read its recent_activity for the recap.\n"
        )
    else:
        zoom_line = (
            "  - Zoom is not connected for this user, so you have no access to Zoom call "
            "recaps. If the user asks about Zoom calls or recaps, say plainly that Zoom "
            "isn't connected and that they can connect it in Settings.\n"
        )

    return (
        f"You are CS Agent, an AI assistant for {csm_name}, a Customer Success Manager.\n"
        f"Today is {today_str}.\n\n"
        "You have these tools available. Use them when the user's question requires data:\n"
        "  - get_calendar_events: calendar events for today, tomorrow, or a date range. Pass no args for today; pass start_date and end_date as ISO strings (YYYY-MM-DD) for ranges. Maximum range 30 days. All dates interpreted in the CSM's local timezone.\n"
        "  - list_my_accounts: which accounts the CSM manages\n"
        "  - get_account_dossier: full picture of one account from HubSpot (contacts, deals, tickets, recent activity)\n"
        "  - resolve_contact_email: look up a contact's email by name\n"
        "  - find_threads_with_contact: recent Gmail threads with one contact\n"
        "  - find_recent_transcripts: search Google Drive for recent meeting transcripts\n"
        "  - read_transcript_excerpt: read the content of a specific transcript Doc\n\n"
        "Tool guidance:\n"
        "  - Call list_my_accounts when the user mentions an account by name and you need to verify it's in their portfolio.\n"
        "  - When the user asks about a specific account, call get_account_dossier with that account name.\n"
        "  - When the user names a contact (Marcus, Priya), call resolve_contact_email first. If the result has multiple matches, ask the user to disambiguate.\n"
        "  - When the user asks about emails with a specific person, call resolve_contact_email then find_threads_with_contact.\n"
        "  - When the user asks about a past meeting, what was discussed, or what was said, call find_recent_transcripts first (optionally with a query like the contact or account name). If a relevant transcript is found, call read_transcript_excerpt with its file_id to get the actual content.\n"
        "  - When the user asks about calendar events for tomorrow, next week, or a specific date, resolve the date relative to 'Today is X' above into ISO format (YYYY-MM-DD) and call get_calendar_events with start_date and end_date.\n"
        "  - You may chain tools across iterations to gather what you need.\n"
        + zoom_line +
        "  - Tool results may include a `_status` field with per-section status. When the status is 'ok' or 'empty', the records genuinely do not exist. Say so plainly, e.g., 'Marcus is not on the contact list for that account.'\n"
        "  - When the status is 'failed', or a tool result carries an `error` key, the fetch did not succeed and you do not have current information for that section. Say so explicitly, e.g., 'I couldn't reach HubSpot just now to check that, so I don't have a current picture there.' Do not imply the records are empty when the fetch failed; these are different cases.\n\n"
        "Style:\n"
        "  - Speak in plain, direct sentences.\n"
        f"  - Surface what's relevant and let {csm_name} decide what to do.\n"
        "  - Never say 'consider', 'I recommend', 'you should', or 'it's important to'.\n"
        "  - If asked for a status, give the status. If asked what to do, give a specific answer, not a list of options.\n"
        "  - Use Gainsight terminology: 'timeline entry' not 'log', 'CTA' not 'task'.\n\n"
        f"{_DRAFT_CARD_GUIDANCE}"
    )


@app.post("/chat")
async def chat(request: Request):
    """Conversational AI endpoint scoped to the authenticated CSM.

    Routes to one of two handlers:
      - sarah_c (the demo) always uses _chat_singleshot_legacy. Byte-identical
        to the pre-Commit-7 behavior so the demo stays frozen.
      - All other csm_ids use _chat_tooluse when CHAT_TOOL_USE_ENABLED is
        "true"; otherwise fall through to _chat_singleshot_legacy.
    """
    # Demo fallback: unauthenticated requests (e.g., /demo prototype hitting
    # /chat with only the demo_session cookie) get the sarah_c demo handler.
    # Authenticated requests go through the shared subscription gate that
    # every other /app/* route uses.
    # With DEMO_MODE off there is no demo fallback: unauthenticated is 401.
    _chat_session = await _resolve_auth(request)
    if _chat_session is not None:
        _chat_session = await _require_active_subscription(request)
    elif not demo_mode_enabled():
        raise HTTPException(status_code=401, detail="Not authenticated")
    _chat_csm_id   = _chat_session["csm_id"] if _chat_session else DEMO_CSM_ID
    _chat_csm_id   = _validate_csm_id(_chat_csm_id)
    _chat_csm_name = _chat_session["name"] if _chat_session else get_csm_name(DEMO_CSM_ID)

    body = await request.json()
    user_msg  = (body.get("message") or "").strip()
    acct_hint = (body.get("account") or "").strip()
    history   = (body.get("history") or [])[-50:]
    # Entry log so a request that fails fast (e.g. timeout, malformed
    # response, downstream tool exception) still leaves a server-side
    # breadcrumb. request_id is injected automatically by JSONFormatter
    # via the contextvar set in add_correlation_id middleware.
    logger.info(
        "[chat] request received csm_id=%s msg_len=%d history_len=%d",
        _chat_csm_id, len(user_msg), len(history),
    )
    if not user_msg:
        return {"reply": "What can I help you with?"}

    # /feedback command. Save directly, skip LLM. Shared across both paths.
    if user_msg.lower().startswith("/feedback "):
        _fb_text = user_msg[len("/feedback "):].strip()
        if _fb_text:
            try:
                _fb_dir = _safe_data_path("product/data/feedback", _chat_csm_id)
                _fb_dir.mkdir(parents=True, exist_ok=True)
                _fb_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                (_fb_dir / f"{_fb_ts}.json").write_text(
                    json.dumps({
                        "csm_id":       _chat_csm_id,
                        "csm_name":     _chat_csm_name,
                        "timestamp":    datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        "message_text": _fb_text,
                        "context":      "feedback",
                    }, indent=2),
                    encoding="utf-8",
                )
            except Exception as _fbe:
                logger.warning("Save failed (non-fatal): %s", _fbe)
            try:
                log_interaction(
                    csm_id=_chat_csm_id,
                    interaction_type="feedback",
                    payload={"feedback_text": _fb_text},
                )
            except Exception:
                pass
        return {"reply": "Got it — thanks for the feedback. It helps me get better."}

    # Stage 3: deterministic digest intercept. Under the flag, an exact
    # digest ask bypasses the LLM entirely and returns generate_digest output
    # verbatim. sarah_c is never intercepted (demo regression guard).
    if (
        not is_demo_csm_id(_chat_csm_id)
        and os.environ.get("DIGEST_GENERATOR_ENABLED", "false").lower() == "true"
        and _is_digest_request(user_msg)
    ):
        logger.info(
            "[chat] digest intercept matched csm_id=%s msg=%r",
            _chat_csm_id, user_msg[:64],
        )
        return await _chat_digest_intercept(_chat_csm_id, _chat_csm_name)

    use_tooluse = (
        not is_demo_csm_id(_chat_csm_id)
        and os.environ.get("CHAT_TOOL_USE_ENABLED", "false").lower() == "true"
    )
    if use_tooluse:
        return await _chat_tooluse(
            _chat_csm_id, _chat_csm_name, user_msg, history, acct_hint,
        )
    return await _chat_singleshot_legacy(
        _chat_csm_id, _chat_csm_name, user_msg, history, acct_hint,
    )


async def _chat_singleshot_legacy(
    _chat_csm_id: str,
    _chat_csm_name: str,
    user_msg: str,
    history: list,
    acct_hint: str,
) -> dict:
    """Legacy single-shot chat handler.

    Pre-stuffs CRM context, gmail signals, and demo data into the system
    prompt, then makes one Claude call. Sarah_c demo uses this path
    permanently; other csm_ids use it when CHAT_TOOL_USE_ENABLED is off.
    Behavior is byte-identical to the pre-Commit-7 inline body.
    """
    today_str      = date.today().isoformat()
    today_meetings = get_todays_meetings()
    meetings_str = ", ".join(
        f"{m['account']} at {m.get('time', 'TBD')}" for m in today_meetings
    ) or "None today"

    # Demo data sections — only loaded for sarah_c
    account_lines = []
    commitments_section = ""
    contacts_section = ""
    acct_summaries_section = ""

    if is_demo_csm_id(_chat_csm_id):
        # usage.csv
        usage_by_account: dict = {}
        usage_path = ROOT / "usage.csv"
        if usage_path.exists():
            with open(usage_path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    usage_by_account[row["account"]] = row

        risk_summary = _demo_risk_summary()
        for acct, row in usage_by_account.items():
            risk = risk_summary.get(acct, {})
            parts = [
                f"last_login_days={row.get('last_login_days')}",
                f"csat={row.get('csat')}",
                f"backlog={row.get('backlog')}",
                f"dau={row.get('dau')}",
                f"risk={risk.get('risk', 'unknown')}",
                f"trend={risk.get('trend', 'unknown')}",
            ]
            account_lines.append(f"- {acct}: " + ", ".join(parts))

        # Meridian commitments from store.json
        store_path = ROOT / "store.json"
        if store_path.exists():
            import json as _sjson
            try:
                _store = _sjson.loads(store_path.read_text(encoding="utf-8"))
                _meridian_entries = [
                    s for s in (_store.get("summaries") or [])
                    if s.get("account") == "Meridian Health" and s.get("source") == "transcript"
                ]
                if _meridian_entries:
                    _latest = max(_meridian_entries, key=lambda s: s.get("created_at", ""))
                    _commit_lines = []
                    for c in (_latest.get("commitments") or []):
                        owner = c.get("owner", "")
                        title = c.get("title", "")
                        due   = c.get("due_date", "")
                        line  = f"- [{owner}] {title}"
                        if due:
                            line += f" (due {due})"
                        _commit_lines.append(line)
                    if _commit_lines:
                        commitments_section = "Recent commitments (Meridian Health):\n" + "\n".join(_commit_lines) + "\n\n"
            except Exception:
                pass

            # Account summaries from store.json
            try:
                _entries = [
                    s for s in (_store.get("summaries") or [])
                    if s.get("source") == "transcript"
                    and (not acct_hint or s.get("account", "").lower() == acct_hint.lower())
                ]
                if _entries:
                    _latest2 = max(_entries, key=lambda s: s.get("created_at", ""))
                    _kp = "\n".join(f"  - {p}" for p in (_latest2.get("key_points") or []))
                    _acct2 = _latest2.get("account", "")
                    acct_summaries_section = (
                        f"Most recent call summary ({_acct2}):\n{_kp}\n\n"
                        if _kp else ""
                    )
            except Exception:
                pass

        contacts_section = (
            "Key contacts:\n"
            "- Northgate Logistics: Dana Mills (primary contact, 18 months, gone quiet recently)\n"
            "- Meridian Health: Marcus Reyes (VP of Support Operations)\n"
            "- Beacon Financial: Priya (expansion contact)\n"
            "- Pinnacle Group: alex.w@pinnaclegroup.example\n\n"
        )

    # Step 0a: Try cached context snapshot first (< 60 min = fresh)
    _used_cache = False
    crm_context = None
    _csm_biz = "auto"
    logger.info("Checking context cache for %s...", _chat_csm_id)
    try:
        from product.context_cache import get_cached_context
        _cached_ctx = get_cached_context(_chat_csm_id)
        if _cached_ctx and _cached_ctx.get("refreshed_at"):
            _cache_age_s = (datetime.now(timezone.utc) - _cached_ctx["refreshed_at"].replace(
                tzinfo=timezone.utc if _cached_ctx["refreshed_at"].tzinfo is None else _cached_ctx["refreshed_at"].tzinfo
            )).total_seconds()
            logger.info("Cache found for %s — age: %ss, status: %s", _chat_csm_id, int(_cache_age_s), _cached_ctx.get('refresh_status'))
            if _cache_age_s < 3600:  # 60 minutes
                _cached_data = _cached_ctx.get("data", {})
                _summary_text = _cached_data.get("summary_text", "")
                if _summary_text:
                    crm_context = f"=== ACCOUNT CONTEXT (cached snapshot) ===\n{_summary_text}"

                    # Enrich with health scores from cache (Gainsight)
                    _health_scores = _cached_data.get("health_scores", [])
                    if _health_scores:
                        _hl = ["\n\n=== HEALTH SCORES ==="]
                        for _hs in _health_scores:
                            _hn = _hs.get("account_name", "")
                            _hsc = _hs.get("score")
                            _ht = _hs.get("trend", "")
                            _hc = _hs.get("color_code", "")
                            _score_s = str(_hsc) if _hsc is not None else "no score"
                            _detail = f"- {_hn}: {_score_s}"
                            if _ht:
                                _detail += f" ({_ht})"
                            if _hc:
                                _detail += f" [{_hc}]"
                            _hl.append(_detail)
                        crm_context += "\n".join(_hl)

                    # Enrich with success plans from cache (Gainsight)
                    _success_plans = _cached_data.get("success_plans", [])
                    if _success_plans:
                        _spl = ["\n\n=== SUCCESS PLANS ==="]
                        for _sp in _success_plans:
                            _spn = _sp.get("name", "")
                            _spa = _sp.get("account_name", "")
                            _sps = _sp.get("status", "")
                            _spl.append(f"- {_spa}: {_spn} (status: {_sps})")
                        crm_context += "\n".join(_spl)

                    # Enrich with contacts from cache (HubSpot + Gainsight).
                    # Contacts are stored on every refresh but were not being
                    # surfaced to the LLM, so chat couldn't resolve names to
                    # emails when drafting outbound messages.
                    _ctx_contacts = _cached_data.get("contacts", [])
                    _ctx_accounts = _cached_data.get("accounts", [])

                    # Detect incomplete refresh: cache has accounts but no
                    # contacts. The arq worker that populates context_cache is
                    # unreliable (Workstream 2.4, deferred), so partial
                    # snapshots are a real failure mode. When this happens,
                    # fall through to live Path 1 instead of serving incomplete
                    # data. Live Path 1 fetches contacts correctly per 0eb2fc5.
                    if _ctx_accounts and not _ctx_contacts:
                        crm_context = None
                        logger.warning(
                            "Cache for %s has %d accounts but 0 contacts (incomplete refresh) — falling back to live",
                            _chat_csm_id, len(_ctx_accounts),
                        )
                    else:
                        if _ctx_contacts:
                            _by_account = {}
                            for _c in _ctx_contacts:
                                _acct = _c.get("account_name", "")
                                if _acct:
                                    _by_account.setdefault(_acct, []).append(_c)
                            if _by_account:
                                _ccl = ["\n\n=== CONTACTS ==="]
                                for _acct in sorted(_by_account.keys()):
                                    for _c in _by_account[_acct]:
                                        _cn = (_c.get("name") or "").strip() or "(unnamed)"
                                        _cemail = (_c.get("email") or "").strip()
                                        _cr = (_c.get("role") or "").strip()
                                        _line = f"- {_acct}: {_cn}"
                                        if _cr:
                                            _line += f" ({_cr})"
                                        if _cemail:
                                            _line += f", {_cemail}"
                                        _ccl.append(_line)
                                crm_context += "\n".join(_ccl)
                        _used_cache = True
                        logger.info("Using cached context (age: %sm, %s chars)", int(_cache_age_s / 60), len(_summary_text))
                else:
                    logger.info("Cache exists but summary_text is empty — falling back to live")
            else:
                logger.info("Cache stale (%sm old) — falling back to live", int(_cache_age_s / 60))
        else:
            logger.info("No cache entry for %s — falling back to live", _chat_csm_id)
    except Exception as _cc_err:
        logger.warning("Cache read failed (non-fatal): %s", _cc_err)

    # Step 0b: If cache miss, fall back to live CRM enrichment

    # For Clerk users not in org_config, load accounts from the onboarding file
    _onboarding_accounts: list[str] = []
    try:
        _acct_file = _safe_data_file("product/data/accounts", _chat_csm_id)
        if _acct_file.exists():
            _acct_data = json.loads(_acct_file.read_text(encoding="utf-8"))
            _onboarding_accounts = _acct_data.get("accounts", [])
            if _onboarding_accounts:
                _csm_biz = _onboarding_accounts
    except Exception:
        pass

    from product.org_config import get_crm_config as _get_crm_cfg_chat, try_get_csm_config as _try_get_csm_cfg_chat
    _csm_cfg_ch = _try_get_csm_cfg_chat(_chat_csm_id)
    if _csm_cfg_ch:
        try:
            _org_crm    = _get_crm_cfg_chat()
            _crm_type   = _org_crm.get("crm_type", "")
            _csm_biz    = _csm_cfg_ch.get("accounts", "auto")  # list or "auto"

            if _crm_type and not _used_cache:
                from product.connectors.registry import get_connector as _get_crm_connector
                _conn = _get_crm_connector(_chat_csm_id, {"crm": _crm_type})

                _crm_lines = ["=== ACCOUNT CONTEXT (from CRM) ===\n"]

                def _in_biz(acct_name: str) -> bool:
                    """True if account is in this CSM's book of business."""
                    return not isinstance(_csm_biz, list) or acct_name in _csm_biz

                try:
                    _accts = [a for a in _conn.get_accounts() if _in_biz(a.get("name", ""))]
                    if _accts:
                        _crm_lines.append("ACCOUNTS:")
                        for _a in _accts:
                            _crm_lines.append(f"- {_a.get('name')} | tier: {_a.get('tier')} | owner: {_a.get('owner')}")
                        _crm_lines.append("")
                except Exception as _e:
                    logger.warning("get_accounts failed: %s", _e)

                try:
                    _renewals = _conn.get_renewals()
                    _renewals_near = sorted(
                        [r for r in _renewals
                         if (r.get("days_until_renewal") or 999) <= 180 and _in_biz(r.get("account_name", ""))],
                        key=lambda r: r.get("days_until_renewal", 999),
                    )
                    if _renewals_near:
                        _crm_lines.append("RENEWALS:")
                        from product.digest.builder import _format_renewal_days
                        for _r in _renewals_near:
                            _val = int(_r.get("contract_value") or 0)
                            _crm_lines.append(
                                f"- {_r.get('account_name')}: {_format_renewal_days(_r.get('days_until_renewal'))} "
                                f"({_r.get('renewal_date')}) | value: ${_val:,}"
                            )
                        _crm_lines.append("")
                except Exception as _e:
                    logger.warning("get_renewals failed: %s", _e)

                try:
                    _health = sorted(
                        [h for h in _conn.get_health_scores() if _in_biz(h.get("account_name", ""))],
                        key=lambda h: h.get("score") or 0,
                    )
                    if _health:
                        _crm_lines.append("HEALTH SCORES:")
                        for _h in _health:
                            _crm_lines.append(f"- {_h.get('account_name')}: {_h.get('score')}/100 ({_h.get('trend', 'stable')})")
                        _crm_lines.append("")
                except Exception as _e:
                    logger.warning("get_health_scores failed: %s", _e)

                try:
                    _today_d = date.today()
                    _ctas_filtered = []
                    for _c in _conn.get_open_ctas():
                        if not _in_biz(_c.get("account_name", "")):
                            continue
                        _pri = (_c.get("priority") or "").lower()
                        _due = (_c.get("due_date") or "")[:10]
                        _overdue = False
                        if _due:
                            try:
                                _overdue = date.fromisoformat(_due) < _today_d
                            except Exception:
                                pass
                        if _pri == "high" or _overdue:
                            _ctas_filtered.append(_c)
                    if _ctas_filtered:
                        _crm_lines.append("OPEN CTAs:")
                        for _c in _ctas_filtered:
                            _crm_lines.append(
                                f"- {_c.get('account_name')}: {_c.get('title')} | "
                                f"due: {_c.get('due_date')} | priority: {_c.get('priority')} | "
                                f"owner: {_c.get('owner')}"
                            )
                        _crm_lines.append("")
                except Exception as _e:
                    logger.warning("get_open_ctas failed: %s", _e)

                # Contacts per account (cross-CRM via base class get_contacts).
                # Pulls accounts directly from the connector (cached after the
                # earlier ACCOUNTS block) so this works even if that block raised.
                # Capped at 20 accounts to mirror the context_cache refresh path.
                try:
                    _accts_for_contacts = [
                        a for a in (_conn.get_accounts() or [])
                        if _in_biz(a.get("name", ""))
                    ][:20]
                    _contacts_by_acct = {}
                    for _a in _accts_for_contacts:
                        _acct_name = _a.get("name", "")
                        if not _acct_name:
                            continue
                        try:
                            _acct_contacts = _conn.get_contacts(_acct_name) or []
                            if _acct_contacts:
                                _contacts_by_acct[_acct_name] = _acct_contacts[:3]
                        except Exception as _exc:
                            logger.warning(
                                "get_contacts(%s) failed: %s", _acct_name, _exc
                            )
                    if _contacts_by_acct:
                        _crm_lines.append("CONTACTS:")
                        for _acct_name in sorted(_contacts_by_acct.keys()):
                            for _c in _contacts_by_acct[_acct_name]:
                                _cn = (_c.get("name") or "").strip() or "(unnamed)"
                                _cemail = (_c.get("email") or "").strip()
                                _cr = (_c.get("role") or "").strip()
                                _line = f"- {_acct_name}: {_cn}"
                                if _cr:
                                    _line += f" ({_cr})"
                                if _cemail:
                                    _line += f", {_cemail}"
                                _crm_lines.append(_line)
                        _crm_lines.append("")
                except Exception as _e:
                    logger.warning("contacts enrichment failed: %s", _e)

                if len(_crm_lines) > 1:
                    crm_context = "\n".join(_crm_lines)

        except Exception as _e:
            logger.warning("CRM enrichment failed (non-fatal): %s", _e)
            if not _used_cache:
                crm_context = None

    # Path 2: HubSpot connector for self-service Clerk users who connected HubSpot
    # during onboarding but are not registered in org_config.
    if crm_context is None and not _used_cache:
        from product.connectors.hubspot_mcp import load_hubspot_token as _hs_load_check
        if _hs_load_check(_chat_csm_id) is not None:
            _cached = _chat_crm_cache.get(_chat_csm_id)
            if _cached and (time.time() - _cached[0]) < 300:
                crm_context = _cached[1]
            else:
                try:
                    from product.connectors.hubspot_crm import HubSpotCRMConnector as _HSConn
                    _hs = _HSConn(_chat_csm_id)
                    _hs_lines = ["=== ACCOUNT CONTEXT (from HubSpot CRM) ==="]

                    def _hs_in_biz(name: str) -> bool:
                        return not _onboarding_accounts or name in _onboarding_accounts

                    # Fetch all accounts, filter to onboarding scope
                    _hs_accts: list[dict] = []
                    try:
                        _hs_accts = [a for a in _hs.get_accounts() if _hs_in_biz(a.get("name", ""))]
                    except Exception as _he:
                        logger.warning("get_accounts failed: %s", _he)

                    # Per-company block: contacts, deals, tickets (capped at 10, 0.5s delay)
                    for _idx_a, _a in enumerate(_hs_accts[:10]):
                        _hubspot_id = _a.get("hubspot_id")
                        _cname = _a.get("name", "")
                        if _hubspot_id is None or not _cname:
                            continue
                        if _idx_a > 0:
                            await asyncio.sleep(0.5)
                        _hs_lines.append(f"\n{_cname.upper()}:")

                        # Contacts
                        try:
                            _contacts = _hs.get_contacts_by_company_id(_hubspot_id)[:3]
                            if _contacts:
                                for _c in _contacts:
                                    _role = _c.get("role") or "unknown role"
                                    _email = _c.get("email") or ""
                                    _hs_lines.append(f"  Contact: {_c.get('name')} ({_role}, {_email})")
                            else:
                                _hs_lines.append("  Contact: none on file")
                        except Exception as _he:
                            logger.warning("get_contacts_by_company_id(%s) failed: %s", _hubspot_id, _he)

                        # Deals
                        try:
                            _deals = _hs.get_deals_for_company(_hubspot_id)[:5]
                            if _deals:
                                for _d in _deals:
                                    _amt = int(_d.get("amount") or 0)
                                    _hs_lines.append(
                                        f"  Deal: {_d.get('name')} | stage: {_d.get('stage')} "
                                        f"| amount: ${_amt:,} | closes: {_d.get('close_date')}"
                                    )
                            else:
                                _hs_lines.append("  Deal: none")
                        except Exception as _he:
                            logger.warning("get_deals_for_company(%s) failed: %s", _hubspot_id, _he)

                        # Tickets
                        try:
                            _tickets = _hs.get_tickets_for_company(_hubspot_id)[:5]
                            if _tickets:
                                for _t in _tickets:
                                    _hs_lines.append(
                                        f"  Ticket: {_t.get('subject')} | status: {_t.get('status')} | priority: {_t.get('priority')}"
                                    )
                            else:
                                _hs_lines.append("  Ticket: none open")
                        except Exception as _he:
                            logger.warning("get_tickets_for_company(%s) failed: %s", _hubspot_id, _he)

                    if len(_hs_lines) > 1:
                        crm_context = "\n".join(_hs_lines)
                        _chat_crm_cache[_chat_csm_id] = (time.time(), crm_context)
                except Exception as _he:
                    logger.warning("HubSpot enrichment failed (non-fatal): %s", _he)

    # If we used live enrichment (not cache), enqueue a refresh so cache is warm next time
    if crm_context and not _used_cache:
        logger.info("Cache miss — used live CRM enrichment")
        try:
            from product.queue import is_redis_available, enqueue as _enq
            if is_redis_available():
                import asyncio as _aio
                _aio.ensure_future(_enq("job_refresh_user_context", _chat_csm_id, _dedup_key=f"refresh:{_chat_csm_id}"))
        except Exception:
            pass

    if crm_context:
        _crm_section = (
            crm_context + "\n"
            "The above data comes from the CSM's live CRM. Use it as the "
            "authoritative source for account details, renewals, and CTAs.\n\n"
        )
    elif _onboarding_accounts:
        _acct_list = "\n".join(f"- {a}" for a in _onboarding_accounts)
        _crm_section = (
            f"=== ACCOUNTS IN BOOK OF BUSINESS ===\n{_acct_list}\n\n"
            "No CRM is connected yet. Account list comes from onboarding setup.\n\n"
        )
    else:
        _crm_section = ""

    _gmail_signals_section = _build_gmail_signals_section(_chat_csm_id)

    system_prompt = (
        f"You are CS Agent, an AI assistant for {_chat_csm_name}, a Customer Success Manager. "
        f"Today is {today_str}.\n\n"
        f"Today's meetings: {meetings_str}\n\n"
        + _crm_section
        + _gmail_signals_section
        + (f"Portfolio accounts:\n" + "\n".join(account_lines) + "\n\n" if account_lines else "")
        + commitments_section
        + acct_summaries_section
        + contacts_section
        + "You are a knowledgeable colleague, not a coach. Speak in plain, direct sentences. "
        f"Surface what's relevant and let {_chat_csm_name} decide what to do. Never say 'consider', "
        "'I recommend', 'you should', or 'it's important to'. Never offer unsolicited advice. "
        "If the CSM asks for a status, give the status. If they ask what to do, give a specific "
        "answer — not a list of options. "
        "If asked about specific emails or messages not present in your context, say: "
        "'I don't see that in the signals I'm aware of right now. The most reliable place to check is your inbox directly.' "
        "Do not assert that you lack email access entirely. The user has connected Gmail, but you only see signals that have surfaced through the daily digest pipeline.\n"
        + ("If asked about account data you don't have, say: 'I don't have detailed account data yet. "
           "Complete the onboarding to connect your CRM and Gmail for full context.'\n" if not crm_context and not _onboarding_accounts else "")
        + "\n"
        + _DRAFT_CARD_GUIDANCE
    )

    if not _anthropic_client:
        return {"reply": "LLM not configured — set ANTHROPIC_API_KEY in .env."}

    reply = None
    for _attempt in range(3):
        try:
            resp = _anthropic_client.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=system_prompt,
                messages=[*history, {"role": "user", "content": user_msg}],
            )
            reply = resp.content[0].text.strip()
            break
        except anthropic.APIStatusError as _api_err:
            if _api_err.status_code == 529 and _attempt < 2:
                await asyncio.sleep(2)
                continue
            reply = "CS Agent is temporarily busy. Please try again in a moment."
            break
        except Exception:
            reply = "CS Agent is temporarily busy. Please try again in a moment."
            break
    if reply is None:
        reply = "CS Agent is temporarily busy. Please try again in a moment."

    drafts, clean_reply = _parse_draft_cards(reply)

    # Transcript auto-detection — if message looks like call notes, also summarize
    _transcript_keywords = {"call", "spoke", "meeting", "notes", "recap", "transcript"}
    if any(kw in user_msg.lower() for kw in _transcript_keywords) and _LLM_READY:
        _summarize_acct = acct_hint or "Unknown"
        _summary, _err = llm_summarize_notes(user_msg, _summarize_acct)
        if _summary and not _err:
            result = {"reply": clean_reply, "summary": _summary}
            if drafts:
                result["drafts"] = drafts
            return result

    # Log the interaction to the inward loop
    try:
        _biz_list = (
            _csm_biz if isinstance(_csm_biz, list)
            else list(usage_by_account.keys())
        )
        log_interaction(
            csm_id=_chat_csm_id,
            interaction_type="signal.acted",
            payload={
                "query": user_msg,
                "response_length": len(clean_reply),
                "accounts_mentioned": [a for a in _biz_list if a.lower() in clean_reply.lower()],
            }
        )
    except Exception as e:
        logger.warning("Logging failed (non-fatal): %s", e)

    result = {"reply": clean_reply}
    if drafts:
        result["drafts"] = drafts
    return result


_CHAT_TOOLUSE_MAX_ITERATIONS = 4

# Cumulative cap on input + output tokens summed across all iterations of
# one /chat turn. If exceeded, the loop breaks with a degraded reply rather
# than continuing to spend tokens. ~50k accommodates ~3 well-cached turns
# even with large tool results, while bounding worst-case spend per request.
_CHAT_TOOLUSE_TOKEN_BUDGET = 50000

# Max tool_use blocks dispatched in a single assistant turn. If the LLM
# requests more, only the first N are dispatched (and a warning is logged).
# Bounds explosion when the model fans out across all five tools at once.
_CHAT_TOOLUSE_MAX_TOOLS_PER_ITERATION = 5


async def _chat_tooluse(
    _chat_csm_id: str,
    _chat_csm_name: str,
    user_msg: str,
    history: list,
    acct_hint: str,
) -> dict:
    """Tool-using chat handler.

    Lets Claude fetch data via tool calls instead of having it pre-stuffed
    into the system prompt. Loops up to _CHAT_TOOLUSE_MAX_ITERATIONS times,
    dispatching each tool_use block to product.chat_tools.registry.dispatch_tool
    and feeding tool_results back as user-role messages until the model
    emits end_turn.

    Returns the same {"reply": ..., "drafts": [...]} shape the legacy
    handler returns, parsed via _parse_draft_cards.
    """
    from product.chat_tools.registry import (
        get_tools_with_cache_control,
        dispatch_tool,
    )

    # Resolve "today" in the CSM's local timezone so the system prompt
    # agrees with what extract_calendar_signals considers today. Using
    # UTC here would cross the date boundary 5+ hours early for US-based
    # CSMs and bias every tomorrow / next-week date-relative resolution.
    _csm_tz_today = datetime.now(get_csm_timezone(_chat_csm_id))
    today_str = _csm_tz_today.strftime("%A, %B %d, %Y")
    system_prompt = _build_tooluse_system_prompt(_chat_csm_name, today_str, _chat_csm_id)
    tools = get_tools_with_cache_control()

    user_content = user_msg
    if acct_hint:
        user_content = f"{user_content}\n\nAccount hint: {acct_hint}"

    messages: list = [*history, {"role": "user", "content": user_content}]
    final_text = ""
    stopped_cleanly = False
    cumulative_tokens = 0

    # Cache the system prompt alongside the tools list (which is already
    # cache-marked on the last entry by get_tools_with_cache_control). The
    # combined prefix is ~2100 tokens, comfortably above the 1024-token
    # minimum for sonnet-4-6 caching. Subsequent turns within the 5-minute
    # window read this prefix at 0.1x input cost.
    system_blocks = [
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    if not _anthropic_client:
        return {"reply": "LLM not configured. Set ANTHROPIC_API_KEY in .env."}

    for iteration in range(_CHAT_TOOLUSE_MAX_ITERATIONS):
        try:
            resp = await asyncio.to_thread(
                _anthropic_client.messages.create,
                model=MODEL,
                max_tokens=2048,
                system=system_blocks,
                tools=tools,
                messages=messages,
            )
        except anthropic.APIStatusError as exc:
            logger.warning("[chat tooluse] APIStatusError iter=%d: %s", iteration, exc)
            final_text = "CS Agent is temporarily busy. Please try again in a moment."
            stopped_cleanly = True
            break
        except Exception as exc:
            logger.warning("[chat tooluse] Unexpected error iter=%d: %s", iteration, exc)
            final_text = "CS Agent hit an unexpected error. Please try again."
            stopped_cleanly = True
            break

        if hasattr(resp, "usage"):
            def _safe_int(x):
                try:
                    return int(x)
                except (TypeError, ValueError):
                    return 0
            cache_cre = _safe_int(getattr(resp.usage, "cache_creation_input_tokens", 0))
            cache_read = _safe_int(getattr(resp.usage, "cache_read_input_tokens", 0))
            in_tok = _safe_int(getattr(resp.usage, "input_tokens", 0))
            out_tok = _safe_int(getattr(resp.usage, "output_tokens", 0))
            logger.info(
                "[chat tooluse] iter=%d cache_creation=%d cache_read=%d input=%d output=%d",
                iteration, cache_cre, cache_read, in_tok, out_tok,
            )
            cumulative_tokens += in_tok + out_tok

        stop_reason = getattr(resp, "stop_reason", None)
        logger.info("[chat tooluse] iteration %d stop_reason=%s", iteration, stop_reason)

        if stop_reason in ("end_turn", "max_tokens"):
            final_text = "".join(
                getattr(b, "text", "") for b in resp.content
                if getattr(b, "type", "") == "text"
            )
            stopped_cleanly = True
            break

        if stop_reason == "tool_use":
            tool_use_blocks = [
                b for b in resp.content if getattr(b, "type", "") == "tool_use"
            ]
            if len(tool_use_blocks) > _CHAT_TOOLUSE_MAX_TOOLS_PER_ITERATION:
                logger.warning(
                    "[chat tooluse] too many tool calls in one iteration: %d > %d, capping",
                    len(tool_use_blocks),
                    _CHAT_TOOLUSE_MAX_TOOLS_PER_ITERATION,
                )
                tool_use_blocks = tool_use_blocks[:_CHAT_TOOLUSE_MAX_TOOLS_PER_ITERATION]

            messages.append({"role": "assistant", "content": resp.content})
            tool_results: list = []
            for block in tool_use_blocks:
                tool_start = time.perf_counter()
                try:
                    result = await dispatch_tool(block.name, block.input or {}, _chat_csm_id)
                    tool_duration_ms = int((time.perf_counter() - tool_start) * 1000)
                    logger.info(
                        "[chat tooluse] tool=%s duration_ms=%d csm_id=%s",
                        block.name,
                        tool_duration_ms,
                        _chat_csm_id,
                    )
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, default=str),
                    })
                except Exception as exc:
                    tool_duration_ms = int((time.perf_counter() - tool_start) * 1000)
                    logger.warning(
                        "[chat tooluse] tool=%s FAILED duration_ms=%d csm_id=%s error=%s",
                        block.name,
                        tool_duration_ms,
                        _chat_csm_id,
                        str(exc)[:200],
                    )
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps({"error": str(exc)[:200]}),
                        "is_error": True,
                    })
            messages.append({"role": "user", "content": tool_results})

            if cumulative_tokens > _CHAT_TOOLUSE_TOKEN_BUDGET:
                logger.warning(
                    "[chat tooluse] token budget exceeded: %d > %d",
                    cumulative_tokens,
                    _CHAT_TOOLUSE_TOKEN_BUDGET,
                )
                if not final_text:
                    final_text = (
                        "I gathered partial context but ran out of room to fully answer. "
                        "Try a more specific question."
                    )
                stopped_cleanly = True
                break
            continue

        logger.warning("[chat tooluse] unexpected stop_reason: %s", stop_reason)
        final_text = "".join(
            getattr(b, "text", "") for b in (resp.content or [])
            if getattr(b, "type", "") == "text"
        )
        stopped_cleanly = True
        break

    if not stopped_cleanly:
        logger.warning("[chat tooluse] hit MAX_ITERATIONS=%d", _CHAT_TOOLUSE_MAX_ITERATIONS)
        if not final_text:
            final_text = (
                "I needed more steps than I have available to answer fully. "
                "Try a more specific question."
            )

    drafts, clean_reply = _parse_draft_cards(final_text)

    # Compute accounts_mentioned for telemetry. Reuses the same loader the
    # tools use so the list matches what the LLM actually saw in this turn.
    accounts_mentioned: list = []
    try:
        from product.chat_tools.registry import _load_account_names_for_csm
        _accts = await asyncio.to_thread(_load_account_names_for_csm, _chat_csm_id)
        _msg_lower = user_msg.lower()
        accounts_mentioned = [a for a in _accts if a and a.lower() in _msg_lower]
    except Exception as exc:
        logger.debug("[chat tooluse] accounts_mentioned compute failed: %s", exc)

    try:
        log_interaction(
            csm_id=_chat_csm_id,
            interaction_type="signal.acted",
            payload={
                "query": user_msg,
                "response_length": len(clean_reply),
                "accounts_mentioned": accounts_mentioned,
                "handler": "tooluse",
                "cumulative_tokens": cumulative_tokens,
            },
        )
    except Exception as exc:
        logger.warning("Logging failed (non-fatal): %s", exc)

    result = {"reply": clean_reply}
    if drafts:
        result["drafts"] = drafts
    return result


# ── Password gate (demo) ─────────────────────────────────────────────────────

def _password_token(password: str) -> str:
    """sha256 hash of a password, used as the gate cookie value."""
    return hashlib.sha256(password.encode()).hexdigest()


def _demo_password_configured() -> bool:
    """False when DEMO_PASSWORD is unset: the /demo gate then refuses every
    request (fail closed) instead of accepting an empty or default password."""
    if DEMO_PASSWORD:
        return True
    logger.error("demo login refused: DEMO_MODE is on but DEMO_PASSWORD is not set")
    return False


def _demo_token() -> str:
    return _password_token(DEMO_PASSWORD)


def _password_login_page(action_url: str, subtitle: str, error: str = "") -> HTMLResponse:
    error_html = f'<p class="error">{error}</p>' if error else ""
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <meta name="robots" content="noindex, nofollow" />
  <title>CS Agent</title>
  <style>
    @font-face {{
      font-family: 'DM Sans';
      font-style: normal;
      font-weight: 400;
      font-display: swap;
      src: url('/fonts/dm-sans-400.woff2') format('woff2');
    }}
    @font-face {{
      font-family: 'DM Sans';
      font-style: normal;
      font-weight: 500;
      font-display: swap;
      src: url('/fonts/dm-sans-500.woff2') format('woff2');
    }}
    @font-face {{
      font-family: 'DM Sans';
      font-style: normal;
      font-weight: 600;
      font-display: swap;
      src: url('/fonts/dm-sans-600.woff2') format('woff2');
    }}
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: 'DM Sans', sans-serif;
      background: #F7F6F3;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
    }}
    .card {{
      background: #fff;
      border: 1px solid #E5E3DE;
      border-radius: 12px;
      padding: 40px 48px;
      width: 100%;
      max-width: 380px;
      box-shadow: 0 2px 12px rgba(0,0,0,0.06);
    }}
    .wordmark {{
      font-size: 15px;
      font-weight: 600;
      color: #1a1a1a;
      letter-spacing: -0.02em;
      margin-bottom: 6px;
    }}
    .subtitle {{
      font-size: 13px;
      color: #9B9589;
      margin-bottom: 28px;
    }}
    label {{
      display: block;
      font-size: 12px;
      font-weight: 500;
      color: #5c5854;
      margin-bottom: 6px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    input[type="password"] {{
      width: 100%;
      padding: 10px 14px;
      border: 1px solid #D9D6D0;
      border-radius: 8px;
      font-family: inherit;
      font-size: 14px;
      color: #1a1a1a;
      background: #FAFAF8;
      outline: none;
      transition: border-color 0.15s;
      margin-bottom: 18px;
    }}
    input[type="password"]:focus {{ border-color: #1a1a1a; }}
    button {{
      width: 100%;
      padding: 11px;
      background: #1a1a1a;
      color: #fff;
      border: none;
      border-radius: 8px;
      font-family: inherit;
      font-size: 14px;
      font-weight: 500;
      cursor: pointer;
      transition: background 0.15s;
    }}
    button:hover {{ background: #333; }}
    .error {{
      font-size: 13px;
      color: #c0392b;
      margin-bottom: 14px;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="wordmark">CS Agent</div>
    <div class="subtitle">{subtitle}</div>
    {error_html}
    <form method="POST" action="{action_url}">
      <label for="password">Password</label>
      <input type="password" id="password" name="password" autofocus autocomplete="current-password" />
      <button type="submit">Continue</button>
    </form>
  </div>
</body>
</html>"""
    return HTMLResponse(html)


def _demo_login_page(error: str = "") -> HTMLResponse:
    return _password_login_page("/demo/login", "Enter your demo password to continue", error)


_STATIC_DIR = Path(__file__).parent / "product" / "static"


def _page_tokens() -> dict:
    """Placeholder -> value, from settings. Every setting has a non-empty
    default (e.g. COMPANY_NAME -> "Your Company"), so a page never shows a
    raw {{TOKEN}}."""
    return {
        "{{SUPPORT_EMAIL}}": SUPPORT_EMAIL,
        "{{WAITLIST_FORM_URL}}": WAITLIST_FORM_URL,
        "{{PUBLIC_BASE_URL}}": PUBLIC_BASE_URL,
        "{{COMPANY_LEGAL_NAME}}": COMPANY_LEGAL_NAME,
        "{{COMPANY_DOMAIN}}": COMPANY_DOMAIN,
        "{{COMPANY_NAME}}": COMPANY_NAME,
    }


def _fill_page_tokens(text: str) -> str:
    """Fill deployment placeholders in served pages from settings.
    Values are operator config (not user input), inserted verbatim."""
    for token, value in _page_tokens().items():
        text = text.replace(token, value)
    return text


def _templated_static(name: str) -> HTMLResponse:
    return HTMLResponse(_fill_page_tokens((_STATIC_DIR / name).read_text(encoding="utf-8")))


@app.get("/privacy", include_in_schema=False)
async def serve_privacy():
    return FileResponse(_STATIC_DIR / "privacy.html", media_type="text/html")


@app.get("/terms", include_in_schema=False)
async def serve_terms():
    return FileResponse(_STATIC_DIR / "terms.html", media_type="text/html")


@app.get("/accessibility", include_in_schema=False)
async def serve_accessibility():
    return _templated_static("accessibility.html")


@app.get("/support", include_in_schema=False)
async def support_page():
    return _templated_static("support.html")


@app.get("/docs", include_in_schema=False)
async def docs_page():
    return _templated_static("docs.html")


# The /static mount (end of file) would serve these templated pages raw, with
# visible {{...}} placeholders. Routes match before the mount, so serve the
# filled versions at their /static paths too.
_TEMPLATED_STATIC_PAGES = ("accessibility.html", "docs.html", "support.html")


@app.get("/static/accessibility.html", include_in_schema=False)
async def static_accessibility_page():
    return _templated_static("accessibility.html")


@app.get("/static/docs.html", include_in_schema=False)
async def static_docs_page():
    return _templated_static("docs.html")


@app.get("/static/support.html", include_in_schema=False)
async def static_support_page():
    return _templated_static("support.html")


# ===== Security policy pages (/security, /security/*) =====================
# Public, cacheable policy documents rendered server-side from the Markdown
# source of truth in docs/policies/. The .md files are canonical; these routes
# render them into the same visual shell (fonts, CSS, container, footer) as
# /privacy and /terms. The /security/* prefix is not in the security-headers
# middleware's no-store allowlist, so these pages stay edge-cacheable like the
# other public marketing pages.
import markdown as _markdown

_POLICIES_DIR = Path(__file__).resolve().parent / "docs" / "policies"

# URL slug -> (markdown filename, page title, one-sentence index description)
_SECURITY_POLICIES = {
    "information-security": (
        "information-security-policy.md",
        "Information Security Policy",
        f"How {COMPANY_NAME} protects the confidentiality, integrity, and availability "
        "of customer data and company information assets.",
    ),
    "incident-response": (
        "incident-response-policy.md",
        "Incident Response Policy",
        f"How {COMPANY_NAME} identifies, investigates, and responds to security "
        "incidents and data breaches.",
    ),
    "vulnerability-disclosure": (
        "vulnerability-disclosure-policy.md",
        "Vulnerability Disclosure Policy",
        "How to report a security vulnerability in CS Agent and what to expect "
        "after you submit a report.",
    ),
}

# Shared page CSS — copied verbatim from product/static/privacy.html so the
# rendered policy pages match /privacy and /terms exactly. No new CSS file.
_POLICY_PAGE_CSS = """<style>
  @font-face {
    font-family: 'DM Sans';
    font-style: normal;
    font-weight: 400;
    font-display: swap;
    src: url('/fonts/dm-sans-400.woff2') format('woff2');
  }
  @font-face {
    font-family: 'DM Sans';
    font-style: normal;
    font-weight: 500;
    font-display: swap;
    src: url('/fonts/dm-sans-500.woff2') format('woff2');
  }
  @font-face {
    font-family: 'DM Sans';
    font-style: normal;
    font-weight: 600;
    font-display: swap;
    src: url('/fonts/dm-sans-600.woff2') format('woff2');
  }
  @font-face {
    font-family: 'DM Sans';
    font-style: normal;
    font-weight: 700;
    font-display: swap;
    src: url('/fonts/dm-sans-700.woff2') format('woff2');
  }
</style>
<style>
  *, *::before, *::after { box-sizing: border-box; }
  html { -webkit-text-size-adjust: 100%; }
  body {
    font-family: 'DM Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #ffffff;
    color: #1a1a1a;
    line-height: 1.6;
    margin: 0;
    padding: 56px 24px 80px;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
  }
  .container { max-width: 720px; margin: 0 auto; }
  header { margin-bottom: 8px; }
  .eyebrow { font-size: 13px; font-weight: 500; color: #6b7280; letter-spacing: 0.06em; text-transform: uppercase; margin: 0 0 12px; }
  h1 { font-size: 34px; font-weight: 700; margin: 0 0 12px; letter-spacing: -0.025em; color: #0a0a0a; }
  .meta { color: #6b7280; font-size: 14px; margin: 0; }
  .meta + .meta { margin-top: 4px; }
  hr.divider { border: 0; border-top: 1px solid #e5e7eb; margin: 36px 0; }
  h2 { font-size: 22px; font-weight: 600; margin: 44px 0 14px; letter-spacing: -0.015em; color: #0a0a0a; }
  h3 { font-size: 17px; font-weight: 600; margin: 26px 0 10px; color: #0a0a0a; }
  p { font-size: 16px; color: #374151; margin: 0 0 14px; }
  ul { padding-left: 22px; margin: 0 0 16px; }
  li { font-size: 16px; color: #374151; margin-bottom: 10px; }
  a { color: #2563eb; text-decoration: none; }
  a:hover { text-decoration: underline; }
  table { width: 100%; border-collapse: collapse; margin: 18px 0 26px; font-size: 14px; }
  th, td { border: 1px solid #e5e7eb; padding: 12px 14px; text-align: left; vertical-align: top; }
  th { background: #f9fafb; font-weight: 600; color: #0a0a0a; }
  td { color: #374151; }
  strong { font-weight: 600; color: #0a0a0a; }
  code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.9em; background: #f3f4f6; padding: 2px 6px; border-radius: 4px; color: #1f2937; }
  .footer { color: #6b7280; font-size: 14px; text-align: center; margin-top: 64px; padding-top: 28px; border-top: 1px solid #e5e7eb; }
  .footer a { color: #6b7280; }
  .footer a + a::before { content: " · "; margin: 0 6px; color: #d1d5db; }
  @media (max-width: 600px) {
    body { padding: 32px 18px 56px; }
    h1 { font-size: 28px; }
    h2 { font-size: 20px; margin-top: 36px; }
    h3 { font-size: 16px; }
    table { font-size: 13px; }
    th, td { padding: 10px; }
  }
</style>"""

_POLICY_PAGE_FOOTER = (
    f'<a href="{public_url()}">{public_url().split("://", 1)[-1]}</a>'
    f'<a href="{public_url("/security")}">Security</a>'
    f'<a href="{public_url("/privacy")}">Privacy</a>'
    f'<a href="{public_url("/terms")}">Terms</a>'
)


def _policy_page(title: str, body_html: str) -> str:
    """Wrap rendered policy HTML in the shared CS Agent page shell — same
    fonts, CSS, container, eyebrow header, and footer as /privacy and /terms.
    The body's own <h1> (from the Markdown title) leads the content."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} · CS Agent by {COMPANY_NAME}</title>
<meta name="description" content="{title} for CS Agent, an AI-powered assistant for Customer Success Managers operated by {COMPANY_LEGAL_NAME}.">
{_POLICY_PAGE_CSS}
</head>
<body>
<div class="container">

<header>
  <p class="eyebrow">CS Agent by {COMPANY_NAME}</p>
</header>

{body_html}

<div class="footer">
  {_POLICY_PAGE_FOOTER}
</div>

</div>
</body>
</html>"""


def _render_policy_markdown(md_text: str, title: str) -> str:
    """Fill placeholders in policy Markdown and wrap it in the page shell."""
    body_html = _markdown.markdown(_fill_page_tokens(md_text), extensions=["tables", "sane_lists"])
    return _policy_page(title, body_html)


def _render_security_policies() -> dict:
    """Render each policy Markdown file into a full HTML page once at startup.
    A missing or malformed file logs a WARNING and maps to None so its route
    returns 500 — the app still boots and the other routes keep working."""
    rendered: dict = {}
    for slug, (filename, title, _desc) in _SECURITY_POLICIES.items():
        path = _POLICIES_DIR / filename
        try:
            # Policy Markdown carries {{...}} placeholders (company name,
            # legal name, domain, support email); filled from settings.
            rendered[slug] = _render_policy_markdown(path.read_text(encoding="utf-8"), title)
        except Exception as exc:
            logger.warning(
                "policy file %s failed to render at startup, route /security/%s "
                "will return 500 until fixed: %s",
                path, slug, exc,
            )
            rendered[slug] = None
    return rendered


def _build_security_index() -> str:
    """Index page listing the three policies with one-sentence descriptions."""
    items = "\n".join(
        f'<li><a href="/security/{slug}">{title}</a> — {desc}</li>'
        for slug, (_fn, title, desc) in _SECURITY_POLICIES.items()
    )
    body = (
        "<h1>Security &amp; Trust</h1>\n"
        f"<p>{COMPANY_NAME} operates CS Agent under a set of published security "
        "policies. These documents describe how we protect customer data, "
        "respond to incidents, and work with security researchers.</p>\n"
        f"<ul>\n{items}\n</ul>\n"
        "<p>To report a security issue, see our "
        '<a href="/security/vulnerability-disclosure">Vulnerability Disclosure '
        f'Policy</a> or email <a href="mailto:{SUPPORT_EMAIL}">'
        f"{SUPPORT_EMAIL}</a>. Our "
        '<a href="/.well-known/security.txt">security.txt</a> follows RFC 9116.</p>'
    )
    return _policy_page("Security", body)


_RENDERED_POLICIES = _render_security_policies()
_SECURITY_INDEX_HTML = _build_security_index()


def _serve_policy(slug: str) -> HTMLResponse:
    html = _RENDERED_POLICIES.get(slug)
    if html is None:
        raise HTTPException(status_code=500, detail="policy temporarily unavailable")
    return HTMLResponse(html)


@app.get("/security", include_in_schema=False)
async def serve_security_index():
    return HTMLResponse(_SECURITY_INDEX_HTML)


@app.get("/security/information-security", include_in_schema=False)
async def serve_security_information_security():
    return _serve_policy("information-security")


@app.get("/security/incident-response", include_in_schema=False)
async def serve_security_incident_response():
    return _serve_policy("incident-response")


@app.get("/security/vulnerability-disclosure", include_in_schema=False)
async def serve_security_vulnerability_disclosure():
    return _serve_policy("vulnerability-disclosure")


@app.api_route(
    "/.well-known/security.txt",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
async def security_txt():
    """Serve security.txt per RFC 9116."""
    _path = _react_dist / ".well-known" / "security.txt"
    if not _path.exists():
        raise HTTPException(status_code=404)
    return Response(_fill_page_tokens(_path.read_text(encoding="utf-8")), media_type="text/plain")


@app.get("/fonts/{filename}", include_in_schema=False)
async def serve_font(filename: str):
    """Serve self-hosted font files from frontend/dist/fonts/.

    Vite copies frontend/public/fonts/* into the build output at
    dist/fonts/*. FastAPI's only static mount is /assets, so /fonts/
    needs its own route. Filename is validated against a strict
    whitelist (lowercase letters, digits, hyphens, .woff2 suffix)
    to prevent path traversal.
    """
    import re
    if not re.fullmatch(r"[a-z0-9-]+\.woff2", filename):
        raise HTTPException(status_code=404)
    font_path = _react_dist / "fonts" / filename
    if not font_path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(font_path, media_type="font/woff2")


@app.get("/demo", response_class=HTMLResponse)
def demo_get(request: Request):
    if not _demo_password_configured():
        return HTMLResponse("Demo is not configured.", status_code=503)
    token = request.cookies.get("demo_session", "")
    if token and secrets.compare_digest(token, _demo_token()):
        return HTMLResponse(Path("ui_prototype_2.html").read_text(encoding="utf-8"))
    return _demo_login_page()

@app.post("/demo/login")
async def demo_login(password: str = Form(...)):
    if not _demo_password_configured():
        return HTMLResponse("Demo is not configured.", status_code=503)
    if secrets.compare_digest(password.encode(), DEMO_PASSWORD.encode()):
        token = _demo_token()
        response = RedirectResponse(url="/demo", status_code=303)
        response.set_cookie(
            key="demo_session",
            value=token,
            max_age=86400,
            httponly=True,
            samesite="lax",
        )
        return response
    return _demo_login_page(error="Incorrect password. Please try again.")


@app.get("/debug/date")
def debug_date():
    import time
    return {
        "date_today": date.today().isoformat(),
        "datetime_now": datetime.now().isoformat(),
        "timezone": time.tzname,
    }

@app.get("/pilot/status")
def pilot_status():
    """Founder monitoring endpoint — returns pilot health data.

    Source-of-truth migration: digest, alert, and onboarding counts come
    from Postgres (digest_logs, proactive_alerts, customers). Interaction
    counts continue to come from interaction_log.jsonl (separate file,
    out of System #1 retirement scope).
    """
    import json
    from pathlib import Path
    from datetime import datetime, timezone

    from product.database import (
        Customer, DigestLog, ProactiveAlert, SessionLocal, as_utc,
    )
    from sqlalchemy import func as _sql_func

    csm = "sarah_c"

    digests_sent = 0
    alerts_sent = 0
    pilot_day = None
    pg_last_activity = None  # ISO string for max() comparison

    if SessionLocal is not None:
        session = SessionLocal()
        try:
            digests_sent = (
                session.query(_sql_func.count(DigestLog.id))
                .filter(DigestLog.customer_id == csm, DigestLog.status == "success")
                .scalar()
            ) or 0
            alerts_sent = (
                session.query(_sql_func.count(ProactiveAlert.id))
                .filter(ProactiveAlert.csm_id == csm)
                .scalar()
            ) or 0

            customer = session.query(Customer).filter_by(clerk_user_id=csm).first()
            if customer is not None and customer.created_at is not None:
                created_at = as_utc(customer.created_at)
                pilot_day = (datetime.now(timezone.utc) - created_at).days + 1

            latest_digest = (
                session.query(_sql_func.max(DigestLog.delivered_at))
                .filter(DigestLog.customer_id == csm)
                .scalar()
            )
            latest_alert = (
                session.query(_sql_func.max(ProactiveAlert.created_at))
                .filter(ProactiveAlert.csm_id == csm)
                .scalar()
            )
            pg_candidates = [as_utc(t) for t in (latest_digest, latest_alert) if t is not None]
            if pg_candidates:
                pg_last_activity = max(pg_candidates).isoformat().replace("+00:00", "Z")
        finally:
            session.close()

    # Interaction counts from interaction_log.jsonl
    interaction_log = Path("interaction_log.jsonl")
    interactions = []
    if interaction_log.exists():
        with open(interaction_log) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        interactions.append(json.loads(line))
                    except Exception:
                        continue

    alerts_acted = sum(1 for i in interactions if i.get("interaction_type") == "signal.acted")
    alerts_dismissed = sum(1 for i in interactions if i.get("interaction_type") == "signal.dismissed")
    drafts_accepted = sum(1 for i in interactions if i.get("interaction_type") == "draft.accepted")
    drafts_edited = sum(1 for i in interactions if i.get("interaction_type") == "draft.edited")
    drafts_dismissed = sum(1 for i in interactions if i.get("interaction_type") == "draft.dismissed")

    # last_activity: max across Postgres timestamps + interaction timestamps
    interaction_ts_candidates = [
        i.get("timestamp") or i.get("sent_at") or ""
        for i in interactions
        if i.get("timestamp") or i.get("sent_at")
    ]
    last_activity_candidates = list(interaction_ts_candidates)
    if pg_last_activity is not None:
        last_activity_candidates.append(pg_last_activity)
    last_activity = max(last_activity_candidates) if last_activity_candidates else None

    return {
        "csm": csm,
        "pilot_day": pilot_day,
        "digests_sent": digests_sent,
        "alerts_sent": alerts_sent,
        "alerts_acted_on": alerts_acted,
        "alerts_dismissed": alerts_dismissed,
        "drafts_accepted": drafts_accepted,
        "drafts_edited": drafts_edited,
        "drafts_dismissed": drafts_dismissed,
        "interactions_logged": len(interactions),
        "last_activity": last_activity,
    }


# ── HubSpot MCP OAuth 2.1 (PKCE) ─────────────────────────────────────────────

from product.connectors.hubspot_mcp import (
    generate_hubspot_auth_url,
    exchange_hubspot_code,
    get_csm_id_for_state,
    save_hubspot_token,
)
from product.connectors.slack_mcp import (
    generate_slack_auth_url,
    exchange_slack_code,
    save_slack_token,
    load_slack_token,
)
from product.connectors.zoom_mcp import (
    generate_zoom_auth_url,
    exchange_zoom_code,
    save_zoom_token,
    load_zoom_token,
    lookup_csm_id_by_zoom_user_id,
    resolve_csm_id_for_recording,
)
from product.oauth_state_store import get_state_with_status as _get_oauth_state_with_status


def _build_oauth_canceled_html(provider: str, reason: str) -> str:
    """Inline HTML for the popup to notify its opener of cancel and self-close.

    Mirrors the success-path HTML structure: same target-origin
    (window.location.origin), same 1500ms self-close timer. provider and
    reason are JSON-encoded into the message payload, never interpolated
    into HTML markup.
    """
    import json as _json
    payload_provider = _json.dumps(provider)
    payload_reason = _json.dumps(reason)
    return f"""<html><body style="font-family: sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0;">
<div style="text-align: center;">
<h2 style="color: #6b7280;">Connection canceled</h2>
<p>You can close this window.</p>
<script>
if (window.opener) {{ window.opener.postMessage({{type: "oauth_canceled", provider: {payload_provider}, reason: {payload_reason}}}, window.location.origin); }}
setTimeout(() => window.close(), 1500);
</script>
</div>
</body></html>"""


@app.get("/auth/hubspot/start")
async def hubspot_auth_start(
    request: Request,
    csm_id: str | None = None,  # legacy query param — read-only-ignored, see below
):
    """Redirect the CSM to HubSpot's OAuth consent screen.

    Identity comes from the verified Clerk JWT (Authorization Bearer
    header or __session cookie). The `csm_id` query parameter is
    accepted for backward compat with cached frontends but is
    intentionally ignored — pre-fix this parameter was the sole
    source of identity for the OAuth state binding, enabling the
    csm_id-pinning attack documented as G-3 in the Stage 1 audit.
    """
    # Legacy query parameter intentionally not read. JWT-only identity.
    del csm_id
    csm_id = await _require_jwt_identity(request)
    auth_url = generate_hubspot_auth_url(csm_id)
    return RedirectResponse(url=auth_url, status_code=302)


def _populate_hubspot_book_of_business(csm_id: str) -> int:
    """Populate Customer.accounts from the CSM's owner-filtered HubSpot
    companies on connect (F-5). Returns the number of accounts written
    (0 when nothing was written).

    Best-effort and safe against transient outages: an empty result — which
    can mean a transient MCP failure OR a genuinely empty book — does NOT
    overwrite an existing account list, so a reconnect can't wipe a book that
    was set manually or on a prior successful connect. Raises only on
    unexpected errors; the caller wraps this in try/except so population
    never breaks the connect flow.
    """
    from product.connectors.hubspot_crm import HubSpotCRMConnector
    connector = HubSpotCRMConnector(csm_id)
    accounts = connector.get_accounts()  # owner-filtered by hubspot_owner_id
    names = [a["name"] for a in accounts if a.get("name")]
    if not names:
        return 0
    from product.database import SessionLocal as _SessionLocal, Customer as _Customer
    if _SessionLocal is None:
        return 0
    sess = _SessionLocal()
    try:
        cust = sess.query(_Customer).filter_by(clerk_user_id=csm_id).first()
        if cust is not None:
            cust.accounts = names
            sess.commit()
    finally:
        sess.close()
    return len(names)


@app.get("/auth/hubspot/callback")
async def hubspot_auth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    """Receive the authorization code from HubSpot and exchange it for tokens."""
    if error or not code:
        provider_label = "hubspot"
        if state:
            try:
                _entry, _status = _get_oauth_state_with_status(state)
                if _status == "ok" and _entry and _entry.get("provider"):
                    provider_label = _entry["provider"]
            except Exception:
                pass
        reason = error or "missing_code"
        return HTMLResponse(content=_build_oauth_canceled_html(provider_label, reason))
    entry, status = _get_oauth_state_with_status(state)
    if status == "expired":
        raise HTTPException(status_code=400, detail="Connection timed out. Please try again — the authorization took longer than expected.")
    if status == "not_found":
        raise HTTPException(status_code=400, detail="OAuth session not found. Please restart the connection.")
    csm_id = entry["csm_id"]
    token_data = exchange_hubspot_code(code, state)
    save_hubspot_token(csm_id, token_data)

    # F-5: auto-populate the CSM's book of business from owner-filtered
    # HubSpot companies on connect. Best-effort — must never break the connect
    # flow. Runs in a worker thread so the synchronous, network-bound
    # get_accounts() does not stall the single-worker event loop. Explicit
    # demo guard: sarah_c is the scripted demo account with no real HubSpot
    # portal (there is no pre-existing sarah_c guard in this callback to
    # mirror, so the guard is added explicitly here).
    if not is_demo_csm_id(csm_id):
        try:
            _acct_count = await asyncio.to_thread(_populate_hubspot_book_of_business, csm_id)
            logger.info(json.dumps({
                "event": "hubspot_connect_accounts_populated",
                "csm_id": csm_id,
                "count": _acct_count,
            }))
        except Exception as _pop_err:
            logger.warning("hubspot_connect account population failed for %s (non-fatal): %s", csm_id, _pop_err)

    try:
        from product.auth_probe import invalidate_cache as _invalidate_probe
        _invalidate_probe(csm_id)
    except Exception:
        pass
    html = """<html><body style="font-family: sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0;">
<div style="text-align: center;">
<h2 style="color: #22c55e;">&#10003; Connected!</h2>
<p>You can close this window.</p>
<script>
if (window.opener) { window.opener.postMessage({type: "oauth_complete", provider: "hubspot"}, window.location.origin); }
setTimeout(() => window.close(), 1500);
</script>
</div>
</body></html>"""
    return HTMLResponse(content=html)


# ── Slack OAuth 2.1 (PKCE), user token (xoxp-) ───────────────────────────────


@app.get("/auth/slack/start")
async def slack_oauth_start(request: Request):
    """Redirect the CSM to Slack's OAuth consent screen.

    Identity comes from the verified Clerk JWT only (mirrors
    /auth/hubspot/start). No csm_id query parameter is read. The demo
    account (sarah_c) is rejected inside generate_slack_auth_url.
    """
    csm_id = await _require_jwt_identity(request)
    url = generate_slack_auth_url(csm_id)
    return RedirectResponse(url, status_code=302)


@app.get("/auth/slack/callback")
async def slack_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    """Receive the authorization code from Slack and exchange it for a user token."""
    if error or not code:
        provider_label = "slack"
        if state:
            try:
                _entry, _status = _get_oauth_state_with_status(state)
                if _status == "ok" and _entry and _entry.get("provider"):
                    provider_label = _entry["provider"]
            except Exception:
                pass
        reason = error or "missing_code"
        return HTMLResponse(content=_build_oauth_canceled_html(provider_label, reason))
    entry, status = _get_oauth_state_with_status(state)
    if status == "expired":
        raise HTTPException(status_code=400, detail="Connection timed out. Please try again - the authorization took longer than expected.")
    if status == "not_found":
        raise HTTPException(status_code=400, detail="OAuth session not found. Please restart the connection.")
    csm_id = entry["csm_id"]
    token_data = exchange_slack_code(code, state)
    save_slack_token(csm_id, token_data)
    try:
        from product.auth_probe import invalidate_cache as _invalidate_probe
        _invalidate_probe(csm_id)
    except Exception:
        pass
    html = """<html><body style="font-family: sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0;">
<div style="text-align: center;">
<h2 style="color: #22c55e;">&#10003; Connected!</h2>
<p>You can close this window.</p>
<script>
if (window.opener) { window.opener.postMessage({type: "oauth_complete", provider: "slack"}, window.location.origin); }
setTimeout(() => window.close(), 1500);
</script>
</div>
</body></html>"""
    return HTMLResponse(content=html)


# ── Zoom OAuth 2.1 (PKCE), user token ────────────────────────────────────────


@app.get("/auth/zoom/start")
async def zoom_oauth_start(request: Request):
    """Redirect the CSM to Zoom's OAuth consent screen.

    Identity comes from the verified Clerk JWT only (mirrors
    /auth/hubspot/start and /auth/slack/start). No csm_id query parameter
    is read. The demo account (sarah_c) is rejected inside
    generate_zoom_auth_url, which returns None.
    """
    csm_id = await _require_jwt_identity(request)
    url = generate_zoom_auth_url(csm_id)
    if not url:
        # Demo account or misconfiguration: surface the guard rather than
        # redirecting to a half-formed URL.
        raise HTTPException(status_code=403, detail="Zoom connection is not available for this account")
    return RedirectResponse(url, status_code=302)


@app.get("/auth/zoom/callback")
async def zoom_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    """Receive the authorization code from Zoom and exchange it for tokens."""
    if error or not code:
        provider_label = "zoom"
        if state:
            try:
                _entry, _status = _get_oauth_state_with_status(state)
                if _status == "ok" and _entry and _entry.get("provider"):
                    provider_label = _entry["provider"]
            except Exception:
                pass
        reason = error or "missing_code"
        return HTMLResponse(content=_build_oauth_canceled_html(provider_label, reason))
    entry, status = _get_oauth_state_with_status(state)
    if status == "expired":
        raise HTTPException(status_code=400, detail="Connection timed out. Please try again - the authorization took longer than expected.")
    if status == "not_found":
        raise HTTPException(status_code=400, detail="OAuth session not found. Please restart the connection.")
    csm_id = entry["csm_id"]
    token_data = exchange_zoom_code(code, state)
    save_zoom_token(csm_id, token_data)
    # Best-effort: capture the Zoom host_id from the recordings list (we
    # already have cloud_recording:read:list_user_recordings) so a user who
    # has recorded before is on the webhook fast path immediately. The
    # recording.completed handler's scan fallback covers users with no
    # recordings yet. Capture failure must never break the connect flow.
    try:
        from product.connectors.zoom_mcp import capture_zoom_host_id
        captured = capture_zoom_host_id(csm_id)
        logger.info(json.dumps({
            "event": "zoom_connect_host_capture",
            "csm_id": csm_id,
            "captured": bool(captured),
        }))
    except Exception:
        pass
    try:
        from product.auth_probe import invalidate_cache as _invalidate_probe
        _invalidate_probe(csm_id)
    except Exception:
        pass
    html = """<html><body style="font-family: sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0;">
<div style="text-align: center;">
<h2 style="color: #22c55e;">&#10003; Connected!</h2>
<p>You can close this window.</p>
<script>
if (window.opener) { window.opener.postMessage({type: "oauth_complete", provider: "zoom"}, window.location.origin); }
setTimeout(() => window.close(), 1500);
</script>
</div>
</body></html>"""
    return HTMLResponse(content=html)


def _slack_resolve_channel(token: str, recipient: str) -> str | None:
    """Resolve a recipient string to a Slack channel/user ID for chat.postMessage.

    Resolution order:
      - email address  -> users.lookupByEmail, returns the user ID (DM target)
      - "#channel"     -> conversations.list name match, returns channel ID
      - bare name      -> conversations.list name match, returns channel ID

    Returns None when nothing matches. Never raises.
    """
    headers = {"Authorization": f"Bearer {token}"}
    name = recipient.strip()

    if "@" in name and "." in name and not name.startswith("#"):
        try:
            resp = requests.get(
                "https://slack.com/api/users.lookupByEmail",
                headers=headers,
                params={"email": name},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("ok"):
                return (data.get("user") or {}).get("id")
        except Exception as exc:
            logger.warning("slack users.lookupByEmail failed: %s", exc)
        return None

    target_name = name[1:] if name.startswith("#") else name
    try:
        cursor = None
        for _ in range(5):  # up to 5 pages of channels
            resp = requests.get(
                "https://slack.com/api/conversations.list",
                headers=headers,
                params={
                    "types": "public_channel,private_channel",
                    "limit": 200,
                    **({"cursor": cursor} if cursor else {}),
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                return None
            for ch in data.get("channels") or []:
                if (ch.get("name") or "").lower() == target_name.lower():
                    return ch.get("id")
            cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                break
    except Exception as exc:
        logger.warning("slack conversations.list failed: %s", exc)
    return None


@app.post("/app/slack/send")
async def slack_send_message(request: Request):
    """Approve-triggered Slack send. Mirrors the HubSpot write pattern:
    the chat tool only drafts; the actual send happens here, after the CSM
    approves the draft card.

    Identity is JWT-only. The demo account (sarah_c) is rejected. Sends via
    chat.postMessage using the CSM's own xoxp- user token (fail closed when
    no token is on file).
    """
    csm_id = await _require_jwt_identity(request)
    body = await request.json()
    recipient = (body.get("recipient") or "").strip()
    message = (body.get("message") or "").strip()
    if not recipient or not message:
        return JSONResponse({"error": "recipient and message required"}, status_code=400)
    if is_demo_csm_id(csm_id):
        return JSONResponse({"error": "demo account"}, status_code=403)

    token_data = load_slack_token(csm_id)
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "slack_not_connected"}, status_code=400)
    token = token_data["access_token"]

    channel_id = await asyncio.to_thread(_slack_resolve_channel, token, recipient)
    if not channel_id:
        return JSONResponse({"error": f"could not resolve recipient {recipient!r}"}, status_code=400)

    try:
        resp = await asyncio.to_thread(
            lambda: requests.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"channel": channel_id, "text": message},
                timeout=15,
            )
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("slack chat.postMessage failed for %s: %s", csm_id, exc)
        return JSONResponse({"error": "send_failed"}, status_code=502)

    if not data.get("ok"):
        return JSONResponse({"error": data.get("error", "send_failed")}, status_code=502)
    return JSONResponse({"status": "sent", "ts": data.get("ts"), "to": recipient})


# ── Gainsight MCP OAuth 2.1 (PKCE) ───────────────────────────────────────────

from product.connectors.gainsight_mcp import (
    generate_gainsight_auth_url,
    exchange_gainsight_code,
    get_csm_id_for_state as get_gainsight_csm_id_for_state,
    save_gainsight_token,
)


@app.get("/auth/gainsight/start")
async def gainsight_auth_start(
    request: Request,
    csm_id: str | None = None,  # legacy query param — read-only-ignored, see below
):
    """Redirect the CSM to Gainsight's OAuth consent screen.

    Identity comes from the verified Clerk JWT (Authorization Bearer
    header or __session cookie). The `csm_id` query parameter is
    accepted for backward compat with cached frontends but is
    intentionally ignored — pre-fix this parameter was the sole
    source of identity for the OAuth state binding, enabling the
    csm_id-pinning attack documented as G-4 in the Stage 1 audit.
    """
    # Legacy query parameter intentionally not read. JWT-only identity.
    del csm_id
    csm_id = await _require_jwt_identity(request)
    if not os.environ.get("GAINSIGHT_CLIENT_ID") or not os.environ.get("GAINSIGHT_DOMAIN"):
        return JSONResponse({"error": "Gainsight OAuth not configured"}, status_code=503)
    auth_url = generate_gainsight_auth_url(csm_id)
    return RedirectResponse(url=auth_url, status_code=302)


@app.get("/auth/gainsight/callback")
async def gainsight_auth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    """Receive the authorization code from Gainsight and exchange it for tokens."""
    if error or not code:
        provider_label = "gainsight"
        if state:
            try:
                _entry, _status = _get_oauth_state_with_status(state)
                if _status == "ok" and _entry and _entry.get("provider"):
                    provider_label = _entry["provider"]
            except Exception:
                pass
        reason = error or "missing_code"
        return HTMLResponse(content=_build_oauth_canceled_html(provider_label, reason))
    entry, status = _get_oauth_state_with_status(state)
    if status == "expired":
        raise HTTPException(status_code=400, detail="Connection timed out. Please try again — the authorization took longer than expected.")
    if status == "not_found":
        raise HTTPException(status_code=400, detail="OAuth session not found. Please restart the connection.")
    csm_id = entry["csm_id"]
    token_data = exchange_gainsight_code(code, state)
    save_gainsight_token(csm_id, token_data)
    try:
        from product.auth_probe import invalidate_cache as _invalidate_probe
        _invalidate_probe(csm_id)
    except Exception:
        pass
    html = """<html><body style="font-family: sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0;">
<div style="text-align: center;">
<h2 style="color: #22c55e;">&#10003; Connected!</h2>
<p>You can close this window.</p>
<script>
if (window.opener) { window.opener.postMessage({type: "oauth_complete", provider: "gainsight"}, window.location.origin); }
setTimeout(() => window.close(), 1500);
</script>
</div>
</body></html>"""
    return HTMLResponse(content=html)


# ── Google OAuth (Gmail + Calendar) ──────────────────────────────────────────

from product.auth_google import (
    generate_google_auth_url,
    exchange_google_code,
    get_user_id_for_state as get_google_user_id_for_state,
    save_google_token,
)

_GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


def _token_has_write_scopes(csm_id: str) -> bool:
    """Return True if the user's stored Google token grants gmail.send.

    After the April 23 scope hygiene commit, gmail.send is the sole
    write-scope gate. calendar.events was removed from SCOPES_WRITE
    because the calendar write feature is not built; it will be added
    back via supplementary OAuth verification when calendar write ships.

    Works identically for sarah_c (filesystem) and Clerk users
    (Postgres first, filesystem fallback). Returns False when no
    token exists — equivalent to no-grant.
    """
    from product.auth_google import load_google_token
    token = load_google_token(csm_id)
    if not token:
        return False
    scopes = token.get("scopes") or []
    return _GMAIL_SEND_SCOPE in scopes


@app.get("/auth/google/start")
async def google_auth_start(
    request: Request,
    scope: str | None = None,
    user_id: str | None = None,  # legacy query param — read-only-ignored, see below
):
    """Redirect the user to Google's OAuth consent screen.

    Identity comes from the verified Clerk JWT (Authorization Bearer
    header or __session cookie). The `user_id` query parameter is
    accepted for backward compat with cached frontends but is
    intentionally ignored — pre-fix this parameter was the sole
    source of identity for the OAuth state binding, enabling the
    csm_id-pinning attack documented as G-2 in the Stage 1 audit.

    Optional `scope` param:
      - absent (default): use SCOPES_READONLY (existing behavior)
      - "write": use SCOPES_WRITE (readonly + gmail.send) for Google-actions re-auth
      - "seed":  use SCOPES_SEED (write + gmail.modify + calendar.events +
                 drive.file) for scripts/seed_csm_data.py demo data seeding
      - anything else: 400 (prevents silent fall-through on misconfigured clients)
    """
    # The legacy user_id query parameter is intentionally not read.
    # Identity is derived from the verified JWT only (G-2 audit fix).
    del user_id
    csm_id = await _require_jwt_identity(request)

    if not os.environ.get("GOOGLE_CLIENT_ID"):
        return JSONResponse({"error": "Google OAuth not configured"}, status_code=503)

    if scope is not None and scope not in ("write", "seed"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid scope param: {scope!r}. Allowed: 'write', 'seed', or absent.",
        )

    if scope == "write":
        from product.auth_google import SCOPES_WRITE
        auth_url = generate_google_auth_url(csm_id, scopes=SCOPES_WRITE)
    elif scope == "seed":
        from product.auth_google import SCOPES_SEED
        auth_url = generate_google_auth_url(csm_id, scopes=SCOPES_SEED)
    else:
        auth_url = generate_google_auth_url(csm_id)
    return RedirectResponse(url=auth_url, status_code=302)


@app.get("/auth/google/callback")
async def google_auth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    """Receive the authorization code from Google and exchange it for tokens."""
    if error or not code:
        provider_label = "google"
        if state:
            try:
                _entry, _status = _get_oauth_state_with_status(state)
                if _status == "ok" and _entry and _entry.get("provider"):
                    provider_label = _entry["provider"]
            except Exception:
                pass
        reason = error or "missing_code"
        return HTMLResponse(content=_build_oauth_canceled_html(provider_label, reason))
    entry, status = _get_oauth_state_with_status(state)
    if status == "expired":
        raise HTTPException(status_code=400, detail="Connection timed out. Please try again — the authorization took longer than expected.")
    if status == "not_found":
        raise HTTPException(status_code=400, detail="OAuth session not found. Please restart the connection.")
    user_id = entry["csm_id"]
    token_data = exchange_google_code(code, state)
    save_google_token(user_id, token_data)
    try:
        # Gmail shadow row for connection_service merge resilience (silent-
        # failure recovery). Written via token_store.save_token so it is
        # Fernet-encrypted at rest — the same writer as the "google" row
        # above. Previously this used database.save_connection, which wrote
        # token_data in plaintext and left a CASA Q44 gap (re-created a
        # plaintext row on every Google (re)auth). Slated for Sprint 6
        # cleanup once merge logic is simplified to google-only reads.
        from product.token_store import save_token
        save_token(user_id, "gmail", token_data)
    except Exception:
        pass
    try:
        from product.auth_probe import invalidate_cache as _invalidate_probe
        _invalidate_probe(user_id)
    except Exception:
        pass
    html = """<html><body style="font-family: sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0;">
<div style="text-align: center;">
<h2 style="color: #22c55e;">&#10003; Connected!</h2>
<p>You can close this window.</p>
<script>
if (window.opener) { window.opener.postMessage({type: "oauth_complete", provider: "google"}, window.location.origin); }
setTimeout(() => window.close(), 1500);
</script>
</div>
</body></html>"""
    return HTMLResponse(content=html)


# ── Stripe Billing ────────────────────────────────────────────────────────────

# Old /billing/create-checkout and /billing/portal removed.
# New endpoints: /app/billing/checkout-session and /app/billing/portal-session
# (auth-required, Clerk-authenticated, defined above).


@app.post("/billing/webhook")
@app.post("/webhooks/stripe")
async def billing_webhook(request: Request):
    """Receive and process Stripe webhook events.

    Handles:
    - checkout.session.completed → transition_to_active_on_payment
      (matches by metadata.clerk_user_id, NOT email)
    - customer.subscription.deleted → transition_to_canceled_on_subscription_end
      (matches by stripe_customer_id)
    - All other events: log and ignore.

    BILLING_ENABLED off: 404 without reading or verifying the payload.
    """
    if not billing_enabled():
        return JSONResponse({"error": "billing_disabled"}, status_code=404)
    from product.billing import STRIPE_WEBHOOK_SECRET
    payload    = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    # Verify signature. Fail-CLOSED: if the secret is not configured we
    # cannot prove the request came from Stripe, so refuse to process it.
    if not STRIPE_WEBHOOK_SECRET:
        logger.error("STRIPE_WEBHOOK_SECRET not configured; rejecting webhook")
        raise HTTPException(status_code=500, detail="webhook_secret_not_configured")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError as exc:
        logger.warning("Webhook signature verification failed: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    event_type = event["type"]
    obj = event["data"]["object"]
    logger.info("Stripe webhook: %s", event_type)

    event_id = event.get("id") if isinstance(event, dict) else getattr(event, "id", None)
    if not event_id:
        logger.error("Stripe webhook missing event.id; rejecting")
        raise HTTPException(status_code=400, detail="missing_event_id")

    logger.info(
        "[stripe_webhook] received event_id=%s event_type=%s",
        event_id, event_type,
    )

    # INSERT-then-process idempotency. event_id is PRIMARY KEY on
    # stripe_webhook_events, so a duplicate INSERT raises IntegrityError
    # and we treat that as "Stripe redelivered an event we already saw":
    # return 200 without reprocessing. Any other exception (e.g.
    # OperationalError from a transient DB blip) propagates so the
    # handler 500s and Stripe retries.
    from product.database import (
        SessionLocal,
        StripeWebhookEvent,
        update_webhook_event_status,
    )
    from sqlalchemy.exc import IntegrityError

    _idem_session = SessionLocal()
    try:
        _idem_session.add(StripeWebhookEvent(
            event_id=event_id,
            event_type=event_type,
            processing_status="pending",
        ))
        _idem_session.commit()
    except IntegrityError:
        _idem_session.rollback()
        logger.info(
            "stripe_webhook_idempotent_replay_ignored event_id=%s type=%s",
            event_id, event_type,
        )
        return JSONResponse({"received": True, "idempotent_replay": True})
    finally:
        _idem_session.close()

    _idem_outcome = "pending"

    from product.subscription import (
        transition_to_active_on_payment,
        transition_to_canceled_on_subscription_end,
    )

    if event_type == "checkout.session.completed":
        # obj is a Stripe SDK object — use attribute access, not .get()
        metadata = getattr(obj, "metadata", None) or {}
        clerk_user_id = metadata.get("clerk_user_id") if isinstance(metadata, dict) else getattr(metadata, "clerk_user_id", None)
        stripe_customer_id = getattr(obj, "customer", None)
        stripe_subscription_id = getattr(obj, "subscription", None)
        logger.info("checkout.session.completed: clerk_user_id=%s customer=%s subscription=%s",
                     clerk_user_id, stripe_customer_id, stripe_subscription_id)
        if not clerk_user_id:
            logger.warning("checkout.session.completed missing metadata.clerk_user_id — skipping")
            _idem_outcome = "skipped_no_handler"
        elif not stripe_customer_id:
            logger.warning("checkout.session.completed missing customer — skipping")
            _idem_outcome = "skipped_no_handler"
        else:
            try:
                logger.info(
                    "[stripe_webhook] dispatching %s for event_id=%s",
                    event_type, event_id,
                )
                _ok = transition_to_active_on_payment(
                    clerk_user_id=str(clerk_user_id),
                    stripe_customer_id=str(stripe_customer_id),
                    stripe_subscription_id=str(stripe_subscription_id or ""),
                )
                _idem_outcome = "processed" if _ok else "skipped_no_customer_row"
            except Exception as e:
                import traceback
                _send_founder_alert(
                    "stripe_webhook.checkout_completed",
                    str(clerk_user_id),
                    e,
                    traceback.format_exc(),
                )
                # Write the audit-trail row BEFORE re-raising so we capture
                # the attempt even when Stripe retries (the next delivery
                # short-circuits via IntegrityError). Belt-and-suspenders
                # try/except: update_webhook_event_status is best-effort
                # per its COMMIT 2 contract, but we wrap in case its
                # import or dispatch shape changes in the future.
                try:
                    update_webhook_event_status(event_id, "failed_will_retry")
                except Exception as audit_exc:
                    logger.warning(
                        "Failed to update webhook event audit row for event_id=%s: %s",
                        event_id, audit_exc,
                    )
                raise  # re-raise so Stripe retries

    elif event_type == "customer.subscription.deleted":
        stripe_customer_id = getattr(obj, "customer", None)
        stripe_subscription_id = getattr(obj, "id", None)

        if not stripe_customer_id or not stripe_subscription_id:
            logger.warning(
                "customer.subscription.deleted missing customer or "
                "subscription id — skipping (customer=%s subscription=%s)",
                stripe_customer_id, stripe_subscription_id,
            )
            _idem_outcome = "skipped_no_handler"
        else:
            try:
                logger.info(
                    "[stripe_webhook] dispatching %s for event_id=%s",
                    event_type, event_id,
                )
                _ok = transition_to_canceled_on_subscription_end(
                    stripe_customer_id=str(stripe_customer_id),
                    stripe_subscription_id=str(stripe_subscription_id),
                )
                _idem_outcome = "processed" if _ok else "skipped_no_customer_row"
            except Exception as e:
                import traceback
                _send_founder_alert(
                    "stripe_webhook.subscription_deleted",
                    f"customer={stripe_customer_id} subscription={stripe_subscription_id}",
                    e,
                    traceback.format_exc(),
                )
                try:
                    update_webhook_event_status(event_id, "failed_will_retry")
                except Exception as audit_exc:
                    logger.warning(
                        "Failed to update webhook event audit row for event_id=%s: %s",
                        event_id, audit_exc,
                    )
                raise

    elif event_type == "customer.subscription.updated":
        stripe_customer_id = getattr(obj, "customer", None)
        stripe_subscription_id = getattr(obj, "id", None)
        cancel_at_period_end_flag = getattr(obj, "cancel_at_period_end", False)
        current_period_end_ts = getattr(obj, "current_period_end", None)

        # Convert unix timestamp to UTC datetime. Stripe sends
        # current_period_end as a unix timestamp (int seconds since
        # epoch); we store it as a DateTime column.
        cancels_at = None
        if cancel_at_period_end_flag and current_period_end_ts:
            from datetime import datetime as _dt, timezone as _tz
            try:
                cancels_at = _dt.fromtimestamp(
                    int(current_period_end_ts), tz=_tz.utc,
                )
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "customer.subscription.updated: could not parse "
                    "current_period_end=%r: %s",
                    current_period_end_ts, exc,
                )
                # cancels_at stays None; we record the flag without the date

        if not stripe_customer_id or not stripe_subscription_id:
            logger.warning(
                "customer.subscription.updated missing customer or "
                "subscription id — skipping (customer=%s subscription=%s)",
                stripe_customer_id, stripe_subscription_id,
            )
            _idem_outcome = "skipped_no_handler"
        else:
            try:
                logger.info(
                    "[stripe_webhook] dispatching %s for event_id=%s",
                    event_type, event_id,
                )
                from product.subscription import set_cancel_at_period_end
                _ok = set_cancel_at_period_end(
                    stripe_customer_id=str(stripe_customer_id),
                    stripe_subscription_id=str(stripe_subscription_id),
                    cancel_at_period_end=bool(cancel_at_period_end_flag),
                    cancels_at=cancels_at,
                )
                _idem_outcome = "processed" if _ok else "skipped_no_customer_row"
            except Exception as e:
                import traceback
                _send_founder_alert(
                    "stripe_webhook.subscription_updated",
                    f"customer={stripe_customer_id} subscription={stripe_subscription_id}",
                    e,
                    traceback.format_exc(),
                )
                try:
                    update_webhook_event_status(event_id, "failed_will_retry")
                except Exception as audit_exc:
                    logger.warning(
                        "Failed to update webhook event audit row for "
                        "event_id=%s: %s",
                        event_id, audit_exc,
                    )
                raise

    else:
        logger.debug("Ignoring Stripe event: %s", event_type)
        _idem_outcome = "skipped_no_handler"

    update_webhook_event_status(event_id, _idem_outcome)
    logger.info(
        "[stripe_webhook] audit updated event_id=%s outcome=%s",
        event_id, _idem_outcome,
    )
    logger.info(
        "stripe_webhook_processed_ok event_id=%s type=%s outcome=%s",
        event_id, event_type, _idem_outcome,
    )
    return JSONResponse({"received": True})


@app.post("/webhooks/zoom")
async def zoom_webhook(request: Request):
    """Receive Zoom webhooks: endpoint.url_validation + recording.completed.

    Security mirrors the Stripe handler: the RAW request body is captured
    before any JSON parse, and the signature is verified fail-closed. Zoom
    uses HMAC-SHA256 over "v0:{timestamp}:{raw_body}" keyed by
    ZOOM_WEBHOOK_SECRET_TOKEN, compared constant-time against the
    x-zm-signature header.

    endpoint.url_validation is handled BEFORE recording.completed: Zoom
    sends it on registration and rejects the endpoint unless we echo the
    hashed challenge.

    recording.completed returns 200 immediately and enqueues
    job_process_zoom_recording. Slow work (transcript fetch + LLM) never
    runs inline in the request path.
    """
    raw_body = await request.body()  # MUST precede any JSON parse
    signature = request.headers.get("x-zm-signature", "")
    timestamp = request.headers.get("x-zm-request-timestamp", "")
    secret = os.environ.get("ZOOM_WEBHOOK_SECRET_TOKEN", "")

    if not secret:
        logger.error("ZOOM_WEBHOOK_SECRET_TOKEN not set; rejecting webhook")
        return JSONResponse({"error": "misconfigured"}, status_code=500)

    message = f"v0:{timestamp}:{raw_body.decode('utf-8')}"
    expected = "v0=" + hmac.new(
        secret.encode(), message.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        logger.warning("Zoom webhook signature verification failed")
        return JSONResponse({"error": "invalid signature"}, status_code=401)

    try:
        payload = json.loads(raw_body)
    except Exception:
        return JSONResponse({"error": "invalid payload"}, status_code=400)

    event = payload.get("event")

    # endpoint.url_validation MUST be handled before recording.completed.
    if event == "endpoint.url_validation":
        plain_token = (payload.get("payload") or {}).get("plainToken", "")
        encrypted = hmac.new(
            secret.encode(), plain_token.encode(), hashlib.sha256
        ).hexdigest()
        return JSONResponse({"plainToken": plain_token, "encryptedToken": encrypted})

    if event == "recording.completed":
        obj = (payload.get("payload") or {}).get("object") or {}
        recording_id = obj.get("uuid") or obj.get("id")
        host_id = obj.get("host_id")
        if recording_id and host_id:
            # Fast path: host_id -> csm_id via the stored zoom_user_id.
            # Slow path (on miss): scan zoom connections' recordings, match
            # on recording_id/host_id, and bind-once for future webhooks.
            fast_hit = lookup_csm_id_by_zoom_user_id(host_id)
            csm_id = fast_hit or resolve_csm_id_for_recording(host_id, str(recording_id))
            if csm_id:
                if not fast_hit:
                    logger.info(json.dumps({
                        "event": "zoom_recording_host_resolved_via_scan",
                        "csm_id": csm_id,
                        "host_id": host_id,
                    }))
                try:
                    from product.queue import enqueue
                    await enqueue(
                        "job_process_zoom_recording",
                        csm_id=csm_id,
                        recording_id=str(recording_id),
                        _dedup_key=f"zoom:{csm_id}:{recording_id}",
                    )
                except Exception as exc:
                    logger.warning("zoom_webhook: enqueue failed: %s", exc)
                logger.info(json.dumps({
                    "event": "zoom_recording_queued",
                    "csm_id": csm_id,
                    "recording_id": str(recording_id),
                }))
            else:
                logger.info(json.dumps({
                    "event": "zoom_recording_no_csm_match",
                    "host_id": host_id,
                }))

    return JSONResponse({"status": "ok"})


@app.post("/webhooks/zoom/deauthorize")
async def zoom_deauthorize(request: Request):
    """Receive Zoom DEAUTHORIZATION notifications and purge the user's Zoom data.

    This is a SEPARATE, security-sensitive, DESTRUCTIVE surface from the main
    /webhooks/zoom event subscription. Zoom's legacy app config exposes a
    dedicated "Deauthorization Notification - Endpoint URL" field; when a user
    removes CS Agent from their Zoom account, Zoom POSTs here and we delete the
    Zoom connection + dedup ledger for that user. This handler does NOT touch
    the /webhooks/zoom route or the recording.completed path.

    Verification is DEFENSIVE because the legacy deauth signing format is not
    cleanly documented (forum/docs conflict):
      - PRIMARY: modern x-zm-signature HMAC (same scheme as /webhooks/zoom).
      - FALLBACK (only if x-zm-signature is ABSENT): a legacy verification
        token embedded in the payload, compared constant-time to the secret.
      - If neither verifies -> 401, fail-closed, delete nothing.
    Every call logs the header NAMES and payload key SHAPE (secrets redacted)
    so the first real deauth tells us which format Zoom actually used.

    PURGE SCOPE (option 2): the encrypted Zoom connection row + all
    processed_zoom_recordings rows for the csm. ProactiveAlerts / recaps
    (source="zoom") are the CSM's work product and age out on the existing
    60-day retention - they are intentionally NOT deleted here.
    """
    raw_body = await request.body()  # MUST precede any JSON parse
    signature = request.headers.get("x-zm-signature", "")
    timestamp = request.headers.get("x-zm-request-timestamp", "")
    secret = os.environ.get("ZOOM_WEBHOOK_SECRET_TOKEN", "")

    if not secret:
        logger.error("ZOOM_WEBHOOK_SECRET_TOKEN not set; rejecting deauthorize webhook")
        return JSONResponse({"error": "misconfigured"}, status_code=500)

    # --- Observability: header NAMES + payload SHAPE only, never values ---
    # This is the diagnostic that lets the first real deauth reveal Zoom's
    # actual signing format. Never log the secret, signature, or any token.
    try:
        _payload_preview = json.loads(raw_body)
        if isinstance(_payload_preview, dict):
            _top_keys = sorted(_payload_preview.keys())
            _sub = _payload_preview.get("payload")
            _sub_keys = sorted(_sub.keys()) if isinstance(_sub, dict) else None
        else:
            _top_keys, _sub_keys = None, None
    except Exception:
        _top_keys, _sub_keys = None, None
    logger.info(json.dumps({
        "event": "zoom_deauthorize_received",
        "header_names": sorted(request.headers.keys()),
        "x_zm_signature_present": bool(signature),
        "payload_top_keys": _top_keys,
        "payload_payload_keys": _sub_keys,
    }))

    try:
        payload = json.loads(raw_body)
    except Exception:
        return JSONResponse({"error": "invalid payload"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "invalid payload"}, status_code=400)

    sub = payload.get("payload") or {}
    if not isinstance(sub, dict):
        sub = {}

    # endpoint.url_validation MUST be handled first: Zoom posts it when the
    # endpoint URL is saved in the app config, and rejects the URL unless we
    # echo the hashed challenge.
    event = payload.get("event")
    if event == "endpoint.url_validation":
        plain_token = sub.get("plainToken", "")
        encrypted = hmac.new(
            secret.encode(), plain_token.encode(), hashlib.sha256
        ).hexdigest()
        return JSONResponse({"plainToken": plain_token, "encryptedToken": encrypted})

    # --- Verify authenticity, defensively, fail-closed ---
    verified = False
    verify_format = "none"
    if signature:
        # PRIMARY: modern x-zm-signature HMAC over v0:{ts}:{raw_body}.
        message = f"v0:{timestamp}:{raw_body.decode('utf-8')}"
        expected = "v0=" + hmac.new(
            secret.encode(), message.encode(), hashlib.sha256
        ).hexdigest()
        if hmac.compare_digest(expected, signature):
            verified = True
            verify_format = "x-zm-signature"
        else:
            # Header present but wrong -> reject. Do NOT fall through to the
            # legacy token path (that would let an attacker bypass a real,
            # failing signature by also sending a token field).
            logger.warning("zoom deauthorize signature verification failed; format=x-zm-signature")
            return JSONResponse({"error": "invalid signature"}, status_code=401)
    else:
        # FALLBACK (legacy, only when x-zm-signature is ABSENT): the legacy
        # deauth payload embeds a verification token. Be tolerant of which
        # documented field carries it; the observability log above confirms
        # the real shape against the first live payload.
        legacy_token = (
            sub.get("signature")
            or sub.get("token")
            or payload.get("token")
            or payload.get("verification_token")
        )
        if legacy_token and hmac.compare_digest(str(legacy_token), secret):
            verified = True
            verify_format = "legacy-token"

    if not verified:
        logger.warning("zoom deauthorize signature verification failed; format=%s", verify_format)
        return JSONResponse({"error": "invalid signature"}, status_code=401)

    # --- Verified. Confirm this is a deauthorization event. ---
    is_deauth = (
        event == "app_deauthorized"
        or ("user_id" in sub and "account_id" in sub and "deauthorization_time" in sub)
    )
    if not is_deauth:
        # Verified but not a deauthorization payload we recognize. Clean no-op.
        logger.info(json.dumps({
            "event": "zoom_deauthorize_ignored_non_deauth_event",
            "zoom_event": event,
        }))
        return JSONResponse({"status": "ok"})

    zoom_user_id = sub.get("user_id")
    csm_id = lookup_csm_id_by_zoom_user_id(zoom_user_id) if zoom_user_id else None

    if csm_id is None:
        # No match: never connected, already purged, or token gone. A retried
        # deauth for an already-purged user is EXPECTED -> clean 200 no-op.
        logger.info(json.dumps({
            "event": "zoom_deauthorize_noop_no_csm_match",
            "verify_format": verify_format,
        }))
    else:
        # PURGE (retry-driven self-heal). Order matters: delete the dedup
        # ledger FIRST, then delete_token(csm_id, "zoom") LAST. The Zoom token
        # row is the lookup key that lookup_csm_id_by_zoom_user_id scans, so it
        # must be the last thing removed - a crash between the two steps leaves
        # the token intact, and a retried deauth re-resolves the csm and
        # completes the purge.
        #
        # If EITHER delete raises, return HTTP 500 so Zoom retries delivery.
        # Never swallow a partial purge as a 200. (This 500 is only for a
        # verified deauth whose delete actually failed - the no-csm-match
        # no-op and the best-effort compliance callback still return 200.)
        from product.token_store import delete_token
        from product.database import SessionLocal as _SL, ProcessedZoomRecording as _PZR

        recordings_deleted = 0
        if _SL is not None:
            _db_session = _SL()
            try:
                recordings_deleted = _db_session.query(_PZR).filter_by(
                    csm_id=csm_id
                ).delete(synchronize_session=False)
                _db_session.commit()
            except Exception as exc:
                _db_session.rollback()
                logger.warning(
                    "zoom_deauthorize: ledger purge failed for csm=%s: %s", csm_id, exc
                )
                return JSONResponse({"error": "purge_failed"}, status_code=500)
            finally:
                _db_session.close()

        # Token row deleted LAST: it is the self-heal lookup key on retry.
        try:
            delete_token(csm_id, "zoom")
        except Exception as exc:
            logger.warning(
                "zoom_deauthorize: token delete failed for csm=%s: %s", csm_id, exc
            )
            return JSONResponse({"error": "purge_failed"}, status_code=500)

        logger.info(json.dumps({
            "event": "zoom_deauthorized_purged",
            "csm_id": csm_id,
            "recordings_deleted": recordings_deleted,
        }))

    # --- Best-effort Data Compliance callback (never blocks/fails the 200) ---
    # Only when the compliance fields are present in payload.payload.
    client_id = sub.get("client_id")
    user_id = sub.get("user_id")
    account_id = sub.get("account_id")
    deauthorization_time = sub.get("deauthorization_time")
    if client_id and user_id and account_id and deauthorization_time:
        try:
            zoom_client_id = os.environ.get("ZOOM_CLIENT_ID", "")
            zoom_client_secret = os.environ.get("ZOOM_CLIENT_SECRET", "")
            resp = requests.post(
                "https://api.zoom.us/oauth/data/compliance",
                auth=(zoom_client_id, zoom_client_secret),
                json={
                    "client_id": client_id,
                    "user_id": user_id,
                    "account_id": account_id,
                    "deauthorization_event_received": {
                        "client_id": client_id,
                        "user_id": user_id,
                        "account_id": account_id,
                        "deauthorization_time": deauthorization_time,
                    },
                    "compliance_completed": True,
                },
                timeout=10,
            )
            if resp.status_code // 100 != 2:
                # Non-2xx (incl. 404 = API deprecated) is logged and IGNORED.
                # The local purge above is the source of truth.
                logger.info(
                    "zoom_deauthorize: compliance callback non-2xx (status=%s); ignored",
                    resp.status_code,
                )
        except Exception as exc:
            logger.warning("zoom_deauthorize: compliance callback failed (ignored): %s", exc)

    return JSONResponse({"status": "ok"})


@app.post("/app/billing/checkout-session")
async def app_billing_checkout_session(request: Request):
    """Create a Stripe Checkout Session. Auth-required, NOT subscription-gated.

    Used by the gate modal. metadata.clerk_user_id is used by the webhook
    to link the Stripe customer back to the Postgres row.
    No trial_period_days — the user's free trial is over; they're paying.
    """
    if not billing_enabled():
        return JSONResponse({"error": "billing_disabled"}, status_code=404)
    _session = await _resolve_auth(request)
    if not _session:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    csm_id = _session["csm_id"]

    from product.subscription import get_customer_by_clerk_id, is_subscription_active
    cust = get_customer_by_clerk_id(csm_id)
    email = cust["email"] if cust else _session.get("email", "")
    existing_stripe_customer_id = cust["stripe_customer_id"] if cust else None

    # Defensive check: active users should never hit Checkout — the gate modal
    # only appears for non-active users. If someone hits this endpoint while
    # active (URL manipulation, stale tab, race), redirect to Portal so they
    # can manage their existing subscription instead of creating a duplicate.
    if cust and is_subscription_active(cust["subscription_status"], cust["trial_ends_at"]):
        try:
            from product.billing import STRIPE_SECRET_KEY as _SK
            stripe.api_key = _SK
            portal = stripe.billing_portal.Session.create(
                customer=existing_stripe_customer_id,
                return_url=public_url("/app"),
            ) if existing_stripe_customer_id else None
            portal_url = portal.url if portal else None
        except Exception as exc:
            logger.warning("Portal session (active-redirect) failed for %s: %s", csm_id, exc)
            portal_url = None
        return JSONResponse(
            {"error": "already_subscribed", "portal_url": portal_url},
            status_code=409,
        )

    try:
        from product.billing import STRIPE_SECRET_KEY, STRIPE_PRICE_ID, build_checkout_kwargs
        if not STRIPE_SECRET_KEY or not STRIPE_PRICE_ID:
            return JSONResponse({"error": "Stripe not configured"}, status_code=503)

        stripe.api_key = STRIPE_SECRET_KEY
        checkout = stripe.checkout.Session.create(**build_checkout_kwargs(
            clerk_user_id=csm_id,
            email=email,
            existing_stripe_customer_id=existing_stripe_customer_id,
        ))
        logger.info("Checkout session created for %s: %s (existing_customer=%s)",
                    csm_id, checkout.id, existing_stripe_customer_id)
        return JSONResponse({"url": checkout.url})
    except Exception as exc:
        logger.warning("Checkout session creation failed for %s: %s", csm_id, exc)
        return JSONResponse({"error": "Could not create checkout session"}, status_code=500)


@app.post("/app/billing/portal-session")
async def app_billing_portal_session(request: Request):
    """Create a Stripe Billing Portal session. Auth-required, NOT gated.

    Requires customer.stripe_customer_id to exist (400 if null).
    Used from a future "Manage billing" entry point in settings.
    """
    if not billing_enabled():
        return JSONResponse({"error": "billing_disabled"}, status_code=404)
    _session = await _resolve_auth(request)
    if not _session:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    csm_id = _session["csm_id"]

    from product.subscription import get_customer_by_clerk_id
    cust = get_customer_by_clerk_id(csm_id)
    if not cust or not cust.get("stripe_customer_id"):
        return JSONResponse({"error": "No Stripe customer found. Use Checkout to subscribe first."}, status_code=400)

    try:
        from product.billing import STRIPE_SECRET_KEY
        stripe.api_key = STRIPE_SECRET_KEY
        portal = stripe.billing_portal.Session.create(
            customer=cust["stripe_customer_id"],
            return_url=public_url("/app"),
        )
        logger.info("Portal session created for %s", csm_id)
        return JSONResponse({"url": portal.url})
    except Exception as exc:
        logger.warning("Portal session creation failed for %s: %s", csm_id, exc)
        return JSONResponse({"error": "Could not create portal session"}, status_code=500)


# ── React frontend (Phase 19) ─────────────────────────────────────────────────
# Serve Vite build static assets when frontend/dist/ exists.
# The /app route above already serves React index.html when the build is present.
# The demo at /demo is NOT affected.

from fastapi.staticfiles import StaticFiles

_react_dist = ROOT / "frontend" / "dist"
if _react_dist.exists() and (ROOT / "frontend" / "dist" / "assets").exists():
    app.mount(
        "/assets",
        StaticFiles(directory=_react_dist / "assets"),
        name="react-assets",
    )

if Path("product/static").exists():
    app.mount("/static", StaticFiles(directory="product/static"), name="static")
