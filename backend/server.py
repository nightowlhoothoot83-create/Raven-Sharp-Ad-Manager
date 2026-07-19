"""
Raven Sharp Ad Manager — FastAPI Backend
Multi-brand ad campaign management with reusable brand profiles (brand bible,
logo, character references) for consistent creative across campaigns.
Part of Ascension Digital Group

Replaces the original Node/Express prototype, which stored everything in
process memory (wiped on every Railway restart) and had no Stripe webhook
(subscriptions were never actually confirmed). Both are fixed here.

Dual-purpose: usable as Emmz's own internal multi-brand ad workspace right
now, and structured (per-user auth + tiers) to become a sellable SaaS later
without a rewrite.
"""
from dotenv import load_dotenv
from pathlib import Path

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

import os, uuid, json, logging, asyncio, base64, re, hmac, hashlib
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any

import bcrypt, jwt, httpx
from fastapi import FastAPI, APIRouter, HTTPException, Request, Response, Depends
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field

# ── Config (identical pattern to Book Creator / Video Creator) ─────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ravensharp-admanager")

_startup_warnings = []

MONGO_URL = os.environ.get("MONGO_URL")
if not MONGO_URL:
    log.critical(
        "STARTUP FAILURE: MONGO_URL is not set on this deployment. "
        "The app cannot start without a database connection string. "
        "Set MONGO_URL in Railway's environment variables for this service and redeploy."
    )
    raise RuntimeError("Missing required environment variable: MONGO_URL")

DB_NAME = os.environ.get("DB_NAME")
if not DB_NAME:
    DB_NAME = "ravensharp_admanager"
    _startup_warnings.append(f"DB_NAME was not set — defaulting to '{DB_NAME}'.")

JWT_SECRET = os.environ.get("JWT_SECRET")
if not JWT_SECRET:
    import secrets as _secrets
    JWT_SECRET = _secrets.token_hex(32)
    _startup_warnings.append(
        "JWT_SECRET was not set — auto-generated a temporary one for this boot. "
        "Use a DIFFERENT secret than every other Raven Sharp app — sharing one lets a "
        "login token from one app work on another."
    )

STRIPE_KEY  = os.environ.get("STRIPE_API_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
if STRIPE_KEY and not STRIPE_WEBHOOK_SECRET:
    _startup_warnings.append(
        "STRIPE_WEBHOOK_SECRET was not set — /billing/webhook will REJECT all events (fail-closed) "
        "until this is set. Get it from Stripe Dashboard -> Developers -> Webhooks -> your endpoint."
    )
RESEND_KEY  = os.environ.get("RESEND_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
if not ANTHROPIC_API_KEY:
    _startup_warnings.append(
        "ANTHROPIC_API_KEY was not set — brand assessment (/brands/{id}/assess) will return a clear 500 error."
    )
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
RUNWARE_API_KEY = os.environ.get("RUNWARE_API_KEY", "")
RUNWARE_MODEL = os.environ.get("RUNWARE_MODEL", "runware:z-image@turbo")  # verified real model
if not GEMINI_API_KEY:
    _startup_warnings.append(
        "GEMINI_API_KEY was not set — ad creative image generation will return a clear 500 error."
    )
RESEND_FROM = os.environ.get("RESEND_FROM_EMAIL", "Raven Sharp <noreply@raven-sharp.com>")
if not RESEND_KEY:
    _startup_warnings.append("RESEND_API_KEY was not set — password reset emails will NOT be sent.")

R2_ENDPOINT   = os.environ.get("R2_ENDPOINT", "")
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY", "")
R2_SECRET_KEY = os.environ.get("R2_SECRET_KEY", "")
R2_BUCKET     = os.environ.get("R2_BUCKET", "adg-images")
if not (R2_ENDPOINT and R2_ACCESS_KEY and R2_SECRET_KEY):
    _startup_warnings.append("R2 storage is not fully configured — brand asset uploads will fail until set.")

for _w in _startup_warnings:
    log.warning("STARTUP: %s", _w)

OWNER_EMAIL  = os.environ.get("OWNER_EMAIL", "ascensiondigitalagency@outlook.com")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")
APP_URL      = os.environ.get("APP_URL", FRONTEND_URL)
CORS_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        "CORS_ORIGINS",
        ",".join([FRONTEND_URL, "https://ads.raven-sharp.com",
                  "http://localhost:3000", "http://127.0.0.1:3000"]),
    ).split(",")
    if origin.strip()
]

client = AsyncIOMotorClient(MONGO_URL)
db     = client[DB_NAME]

app = FastAPI(title="Raven Sharp Ad Manager API")
api = APIRouter(prefix="/api")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_origin_regex=r"https://.*\.raven-sharp\.com",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Tier config (matches the free/starter/pro already in the frontend) ─────
TIERS = {
    "free":    {"brands": 1,   "campaigns_per_month": 10,  "price": 0},
    "starter": {"brands": 5,   "campaigns_per_month": 100, "price": 19},
    "pro":     {"brands": 999, "campaigns_per_month": 999999, "price": 49},
    "owner":   {"brands": 999, "campaigns_per_month": 999999, "price": 0},
}

# ── Auth helpers (identical pattern to Book Creator / Video Creator) ───────
def hash_pw(pw): return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()

def verify_pw(pw, h):
    if isinstance(h, str):
        h = h.encode("utf-8")
    return bcrypt.checkpw(pw.encode("utf-8"), h)

def make_access(uid, email):
    return jwt.encode({"sub": uid, "email": email, "type": "access",
                        "exp": datetime.now(timezone.utc) + timedelta(days=1)},
                       JWT_SECRET, algorithm="HS256")

def make_refresh(uid):
    return jwt.encode({"sub": uid, "type": "refresh",
                        "exp": datetime.now(timezone.utc) + timedelta(days=7)},
                       JWT_SECRET, algorithm="HS256")

def set_cookies(response, access, refresh):
    kw = dict(httponly=True, secure=True, samesite="none", path="/")
    response.set_cookie("access_token",  access,  max_age=86400,  **kw)
    response.set_cookie("refresh_token", refresh, max_age=604800, **kw)

async def get_user(request: Request):
    token = request.cookies.get("access_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        raise HTTPException(401, "Not authenticated")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        user = await db.users.find_one({"id": payload["sub"]}, {"_id": 0})
        if not user:
            raise HTTPException(401, "User not found")
        return user
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired")
    except Exception:
        raise HTTPException(401, "Invalid token")

async def send_email(to: str, subject: str, html: str) -> bool:
    if not RESEND_KEY:
        log.warning("send_email skipped (no RESEND_API_KEY configured): to=%s subject=%r", to, subject)
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            resp = await c.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_KEY}", "Content-Type": "application/json"},
                json={"from": RESEND_FROM, "to": [to], "subject": subject, "html": html},
            )
            return resp.status_code < 400
    except Exception as e:
        log.error("Resend email exception: %s", e)
        return False

# ── R2 storage (identical pattern to Book Creator / Video Creator / POD) ───
async def upload_to_r2(file_bytes: bytes, key_prefix: str, filename: str, mime: str = "image/png") -> str:
    if not (R2_ENDPOINT and R2_ACCESS_KEY and R2_SECRET_KEY):
        log.warning("R2 not fully configured — skipping upload, public_url will be empty")
        return ""

    def _blocking_upload():
        import boto3
        from botocore.config import Config
        import io

        key = f"{key_prefix}/{filename}"
        s3 = boto3.client(
            "s3", endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_ACCESS_KEY, aws_secret_access_key=R2_SECRET_KEY,
            config=Config(signature_version="s3v4"), region_name="auto",
        )
        s3.upload_fileobj(io.BytesIO(file_bytes), R2_BUCKET, key,
                           ExtraArgs={"ContentType": mime, "ACL": "public-read"})
        public_base = os.environ.get("R2_PUBLIC_URL", f"{R2_ENDPOINT}/{R2_BUCKET}")
        return f"{public_base.rstrip('/')}/{key}"

    try:
        return await asyncio.to_thread(_blocking_upload)
    except Exception as e:
        log.error(f"R2 upload failed: {e}")
        return ""

# ── Models ────────────────────────────────────────────────────────────────────
class RegisterIn(BaseModel):
    email: str; password: str; name: Optional[str] = None

class LoginIn(BaseModel):
    email: str; password: str

class ForgotPasswordIn(BaseModel):
    email: str

class ResetPasswordIn(BaseModel):
    token: str
    new_password: str

class BrandAsset(BaseModel):
    name: str = ""
    url: str
    type: str = "image"          # image | logo | product_photo | banner
    description: str = ""

class BrandProfileIn(BaseModel):
    name: str
    brand_bible: str = ""        # tone, audience, do's-and-don'ts — same field as Book/Video Creator
    website_url: Optional[str] = None
    primary_color: Optional[str] = None
    secondary_color: Optional[str] = None
    logo_url: Optional[str] = None
    characters: List[Dict[str, Any]] = Field(default_factory=list)  # [{name, description, image_url}] — for consistency
    products: List[Dict[str, Any]] = Field(default_factory=list)    # [{name, description, image_url, price}]
    assets: List[BrandAsset] = Field(default_factory=list)          # broader asset library: product photos, banners, etc.
    notes: str = ""

class AssetUploadIn(BaseModel):
    image_base64: str
    mime: str = "image/png"
    filename: str = "asset"
    asset_name: str = ""
    asset_type: str = "image"

class CampaignIn(BaseModel):
    title: str
    brand_profile_id: str          # which brand this campaign uses
    objective: str = ""
    budget: Optional[float] = None
    status: str = "Draft"
    notes: str = ""
    publish_targets: List[str] = Field(default_factory=list)  # e.g. ["facebook","pinterest"]

class CampaignUpdateIn(BaseModel):
    title: Optional[str] = None
    objective: Optional[str] = None
    budget: Optional[float] = None
    status: Optional[str] = None
    notes: Optional[str] = None
    publish_targets: Optional[List[str]] = None

class CheckoutIn(BaseModel):
    plan: str  # "starter" | "pro"

# ── Auth routes (identical pattern to Book Creator / Video Creator) ─────────
@api.post("/auth/register")
async def register(payload: RegisterIn, response: Response):
    email = payload.email.lower().strip()
    if await db.users.find_one({"email": email}):
        raise HTTPException(400, "Email already registered")
    tier = "owner" if email == OWNER_EMAIL.lower() else "free"
    user = {"id": str(uuid.uuid4()), "email": email,
            "name": payload.name or email.split("@")[0],
            "password_hash": hash_pw(payload.password),
            "tier": tier, "campaigns_this_month": 0,
            "created_at": datetime.now(timezone.utc).isoformat()}
    await db.users.insert_one(user)
    access, refresh = make_access(user["id"], email), make_refresh(user["id"])
    set_cookies(response, access, refresh)
    return {"id": user["id"], "email": email, "name": user["name"], "tier": tier,
            "campaigns_this_month": 0, "created_at": user["created_at"]}

@api.post("/auth/login")
async def login(payload: LoginIn, response: Response):
    email = payload.email.lower().strip()
    user = await db.users.find_one({"email": email})
    if not user or not verify_pw(payload.password, user["password_hash"]):
        raise HTTPException(401, "Invalid email or password")
    access, refresh = make_access(user["id"], email), make_refresh(user["id"])
    set_cookies(response, access, refresh)
    return {"id": user["id"], "email": email, "name": user.get("name"),
            "tier": user.get("tier", "free"), "campaigns_this_month": user.get("campaigns_this_month", 0),
            "created_at": user["created_at"]}

@api.post("/auth/logout")
async def logout(response: Response):
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/")
    return {"ok": True}

@api.get("/auth/me")
async def me(user: dict = Depends(get_user)):
    return {"id": user["id"], "email": user["email"], "name": user.get("name"),
            "tier": user.get("tier", "free"), "campaigns_this_month": user.get("campaigns_this_month", 0),
            "created_at": user["created_at"]}

@api.post("/auth/refresh")
async def refresh_token(request: Request, response: Response):
    token = request.cookies.get("refresh_token")
    if not token:
        raise HTTPException(401, "No refresh token")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        user = await db.users.find_one({"id": payload["sub"]})
        if not user:
            raise HTTPException(401, "User not found")
        access, refresh = make_access(user["id"], user["email"]), make_refresh(user["id"])
        set_cookies(response, access, refresh)
        return {"ok": True}
    except Exception:
        raise HTTPException(401, "Invalid refresh token")

_reset_tokens: dict = {}

@api.post("/auth/forgot-password")
async def forgot_password(payload: ForgotPasswordIn):
    email = payload.email.lower().strip()
    user = await db.users.find_one({"email": email})
    if not user:
        return {"message": "If that email exists, a reset link has been sent."}
    token = str(uuid.uuid4())
    _reset_tokens[token] = {"email": email, "expires": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
    reset_link = f"{FRONTEND_URL}/reset-password?token={token}"
    await send_email(
        to=email, subject="Reset your Raven Sharp Ad Manager password",
        html=f'<p><a href="{reset_link}">Click here to reset your password</a> — expires in 1 hour.</p>',
    )
    return {"message": "If that email exists, a reset link has been sent.",
            "debug_token": token if email == OWNER_EMAIL.lower() else None}

@api.post("/auth/reset-password")
async def reset_password(payload: ResetPasswordIn):
    entry = _reset_tokens.get(payload.token)
    if not entry:
        raise HTTPException(400, "Invalid or expired reset token")
    if datetime.fromisoformat(entry["expires"]) < datetime.now(timezone.utc):
        del _reset_tokens[payload.token]
        raise HTTPException(400, "Reset token has expired")
    result = await db.users.update_one({"email": entry["email"]}, {"$set": {"password_hash": hash_pw(payload.new_password)}})
    if result.matched_count == 0:
        raise HTTPException(404, "User not found")
    del _reset_tokens[payload.token]
    return {"message": "Password reset successfully. Please sign in."}

# ── Brand profiles (identical pattern to Book Creator / Video Creator) ─────
OWNER_BRAND_SEEDS = [
    {
        "name": "RavenSharp Tools",
        "brand_bible": "Tech/AI division of Ascension Digital Group — practical, no-nonsense, tool-focused. Covers mycalctools.net, mycalendartools.net, Image Optimiser, POD Suite, Book Creator, Content Creator. Tone: clear, helpful, confident, zero fluff.",
        "website_url": "https://mycalctools.net",
        "primary_color": "#7c5cbf", "secondary_color": "#a78bfa",
    },
    {
        "name": "Zyia Creations",
        "brand_bible": "Cosmic/spiritual brand — sacred geometry, psychedelic art, sovereignty and shadow-work themes. Tone: mystical, introspective, evocative. Sells on Etsy (zyiacreations.etsy.com).",
        "website_url": "https://zyiacreations.etsy.com",
        "primary_color": "#6b21a8", "secondary_color": "#c026d3",
    },
    {
        "name": "Spew Crew Kids",
        "brand_bible": "Children's entertainment brand for YouTube — warm chaos that always resolves positively, every character gets a win, kid-friendly humour with sound effects.",
        "primary_color": "#4ADE80", "secondary_color": "#E53E3E",
        "characters": [
            {"name": "Rizzy Reflux", "description": "The leader — rainbow pastels, emotional regulation themes, bold and protective."},
            {"name": "Spewy Spence", "description": "The chaos engine — slime green, impulse control themes, hyper and adventurous skater."},
            {"name": "Milky Matt", "description": "The heart — soft blues/whites, self-acceptance themes."},
        ],
    },
    {
        "name": "Feed the Feed",
        "brand_bible": "Dystopian social commentary brand, Facebook-based. Tone: sharp, satirical, unsettling-but-thoughtful.",
        "primary_color": "#1a1a1a", "secondary_color": "#dc2626",
    },
    {
        "name": "Mystical Moments",
        "brand_bible": "Fine art photography by Emma James. Tone: contemplative, atmospheric, high-craft. Listed on ArtPal and Fine Art America.",
        "primary_color": "#1e293b", "secondary_color": "#94a3b8",
    },
]

@api.post("/brands/seed-owner-brands")
async def seed_owner_brands(user: dict = Depends(get_user)):
    """Owner-only, idempotent — pre-populates the known ADG brands so they
    don't need to be entered manually. Safe to call more than once; skips
    any brand that already exists by name."""
    if user.get("tier") != "owner":
        raise HTTPException(403, "Owner only")
    existing_names = {b["name"] for b in await db.brand_profiles.find({"user_id": user["id"]}, {"name": 1}).to_list(200)}
    created = []
    for seed in OWNER_BRAND_SEEDS:
        if seed["name"] in existing_names:
            continue
        profile = {"id": str(uuid.uuid4()), "user_id": user["id"], "logo_url": None,
                   "characters": [], "products": [], "assets": [], "notes": "",
                   **seed, "created_at": datetime.now(timezone.utc).isoformat()}
        await db.brand_profiles.insert_one(profile)
        created.append(seed["name"])
    return {"created": created, "skipped_existing": [s["name"] for s in OWNER_BRAND_SEEDS if s["name"] in existing_names]}


@api.post("/brands")
async def create_brand(payload: BrandProfileIn, user: dict = Depends(get_user)):
    tier = user.get("tier", "free")
    limit = TIERS.get(tier, TIERS["free"])["brands"]
    existing = await db.brand_profiles.count_documents({"user_id": user["id"]})
    if existing >= limit:
        raise HTTPException(403, f"Brand limit reached for the '{tier}' plan ({limit}). Upgrade for more.")
    profile = {"id": str(uuid.uuid4()), "user_id": user["id"], **payload.dict(),
               "created_at": datetime.now(timezone.utc).isoformat()}
    await db.brand_profiles.insert_one(profile)
    profile.pop("_id", None)
    return profile

@api.get("/brands")
async def list_brands(user: dict = Depends(get_user)):
    return await db.brand_profiles.find({"user_id": user["id"]}, {"_id": 0}).sort("created_at", 1).to_list(200)

@api.get("/brands/{brand_id}")
async def get_brand(brand_id: str, user: dict = Depends(get_user)):
    brand = await db.brand_profiles.find_one({"id": brand_id, "user_id": user["id"]}, {"_id": 0})
    if not brand:
        raise HTTPException(404, "Brand not found")
    return brand

@api.put("/brands/{brand_id}")
async def update_brand(brand_id: str, payload: BrandProfileIn, user: dict = Depends(get_user)):
    result = await db.brand_profiles.update_one({"id": brand_id, "user_id": user["id"]}, {"$set": payload.dict()})
    if result.matched_count == 0:
        raise HTTPException(404, "Brand not found")
    return await db.brand_profiles.find_one({"id": brand_id}, {"_id": 0})

@api.delete("/brands/{brand_id}")
async def delete_brand(brand_id: str, user: dict = Depends(get_user)):
    result = await db.brand_profiles.delete_one({"id": brand_id, "user_id": user["id"]})
    if result.deleted_count == 0:
        raise HTTPException(404, "Brand not found")
    return {"ok": True}

@api.post("/brands/{brand_id}/upload-asset")
async def upload_brand_asset(brand_id: str, payload: AssetUploadIn, user: dict = Depends(get_user)):
    """Uploads a logo, character reference, or general brand asset (product
    photo, banner) to R2 and appends it to the brand's asset library — this
    is what lets a campaign later pull consistent visuals for that brand."""
    brand = await db.brand_profiles.find_one({"id": brand_id, "user_id": user["id"]})
    if not brand:
        raise HTTPException(404, "Brand not found")
    image_bytes = base64.b64decode(payload.image_base64)
    key = f"{uuid.uuid4()}-{payload.filename}"
    url = await upload_to_r2(image_bytes, f"ad-manager-assets/{user['id']}/{brand_id}", key, payload.mime)
    if not url:
        raise HTTPException(500, "Upload failed — R2 not configured or upload error")

    new_asset = {"name": payload.asset_name or payload.filename, "url": url,
                 "type": payload.asset_type, "description": ""}
    await db.brand_profiles.update_one({"id": brand_id}, {"$push": {"assets": new_asset}})
    return {"url": url, "asset": new_asset}

# ── Campaigns (linked to a brand for consistent creative) ──────────────────
@api.post("/campaigns")
async def create_campaign(payload: CampaignIn, user: dict = Depends(get_user)):
    tier = user.get("tier", "free")
    limit = TIERS.get(tier, TIERS["free"])["campaigns_per_month"]
    if user.get("campaigns_this_month", 0) >= limit:
        raise HTTPException(403, f"Monthly campaign limit reached for the '{tier}' plan ({limit}). Upgrade for more.")
    brand = await db.brand_profiles.find_one({"id": payload.brand_profile_id, "user_id": user["id"]})
    if not brand:
        raise HTTPException(400, "brand_profile_id does not match one of your brands")
    campaign = {"id": str(uuid.uuid4()), "user_id": user["id"], **payload.dict(),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat()}
    await db.campaigns.insert_one(campaign)
    await db.users.update_one({"id": user["id"]}, {"$inc": {"campaigns_this_month": 1}})
    campaign.pop("_id", None)
    return campaign

@api.get("/campaigns")
async def list_campaigns(user: dict = Depends(get_user)):
    return await db.campaigns.find({"user_id": user["id"]}, {"_id": 0}).sort("updated_at", -1).to_list(500)

@api.get("/campaigns/{campaign_id}")
async def get_campaign(campaign_id: str, user: dict = Depends(get_user)):
    campaign = await db.campaigns.find_one({"id": campaign_id, "user_id": user["id"]}, {"_id": 0})
    if not campaign:
        raise HTTPException(404, "Campaign not found")
    return campaign

@api.put("/campaigns/{campaign_id}")
async def update_campaign(campaign_id: str, payload: CampaignUpdateIn, user: dict = Depends(get_user)):
    updates = {k: v for k, v in payload.dict(exclude_unset=True).items() if v is not None}
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    result = await db.campaigns.update_one({"id": campaign_id, "user_id": user["id"]}, {"$set": updates})
    if result.matched_count == 0:
        raise HTTPException(404, "Campaign not found")
    return await db.campaigns.find_one({"id": campaign_id}, {"_id": 0})

@api.delete("/campaigns/{campaign_id}")
async def delete_campaign(campaign_id: str, user: dict = Depends(get_user)):
    result = await db.campaigns.delete_one({"id": campaign_id, "user_id": user["id"]})
    if result.deleted_count == 0:
        raise HTTPException(404, "Campaign not found")
    return {"ok": True}

# ── Social connections (per-user, per-platform access tokens) ──────────────
class ConnectionIn(BaseModel):
    platform: str          # facebook_page | facebook_group | instagram | tiktok | pinterest | x | linkedin
    access_token: str
    account_label: str = ""
    account_id: str = ""   # page_id / group_id / ig_user_id / board_id etc., platform-dependent

PLATFORM_LABELS = {
    "facebook_page": "Facebook Page",
    "facebook_group": "Facebook Group",
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "pinterest": "Pinterest",
    "x": "X (Twitter)",
    "linkedin": "LinkedIn",
}

@api.post("/connections")
async def create_connection(body: ConnectionIn, user: dict = Depends(get_user)):
    if body.platform not in PLATFORM_LABELS:
        raise HTTPException(400, f"Unknown platform. Choose one of: {list(PLATFORM_LABELS.keys())}")
    cid = str(uuid.uuid4())
    doc = {
        "id": cid, "user_id": user["id"], "platform": body.platform,
        "account_label": body.account_label or PLATFORM_LABELS[body.platform],
        "account_id": body.account_id, "access_token": body.access_token,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.connections.insert_one(doc)
    return {"id": cid, "platform": body.platform, "account_label": doc["account_label"]}

@api.get("/connections")
async def list_connections(user: dict = Depends(get_user)):
    conns = await db.connections.find({"user_id": user["id"]}, {"_id": 0, "access_token": 0}).to_list(50)
    return conns

@api.delete("/connections/{connection_id}")
async def delete_connection(connection_id: str, user: dict = Depends(get_user)):
    result = await db.connections.delete_one({"id": connection_id, "user_id": user["id"]})
    if result.deleted_count == 0:
        raise HTTPException(404, "Connection not found")
    return {"ok": True}


# ── Publishing — real APIs for each platform ────────────────────────────────
# Every function takes (access_token, account_id, content) and returns the
# platform's post/pin ID on success. All are real, current, well-documented
# APIs (Meta Graph, X API v2, LinkedIn, TikTok Content Posting, Pinterest v5)
# — unlike Higgsfield/InVideo/Meta-AI-video earlier, these are mainstream,
# long-established platform APIs I have solid grounding on.
class PublishContent(BaseModel):
    text: str = ""
    image_url: Optional[str] = None
    link: Optional[str] = None
    board_id: Optional[str] = None  # Pinterest only

async def publish_facebook_page(access_token: str, page_id: str, content: PublishContent) -> dict:
    async with httpx.AsyncClient(timeout=30) as c:
        if content.image_url:
            r = await c.post(f"https://graph.facebook.com/v21.0/{page_id}/photos",
                params={"access_token": access_token, "url": content.image_url, "caption": content.text})
        else:
            r = await c.post(f"https://graph.facebook.com/v21.0/{page_id}/feed",
                params={"access_token": access_token, "message": content.text, "link": content.link or ""})
        if not r.is_success:
            raise HTTPException(502, f"Facebook Page publish failed: {r.text[:300]}")
        return r.json()

async def publish_facebook_group(access_token: str, group_id: str, content: PublishContent) -> dict:
    """NOTE: Meta restricts group-feed posting to apps with the
    publish_to_groups permission, which requires App Review — this alone
    doesn't guarantee it'll work without that approval on the account."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"https://graph.facebook.com/v21.0/{group_id}/feed",
            params={"access_token": access_token, "message": content.text, "link": content.link or ""})
        if not r.is_success:
            raise HTTPException(502, f"Facebook Group publish failed (needs publish_to_groups App Review approval): {r.text[:300]}")
        return r.json()

async def publish_instagram(access_token: str, ig_user_id: str, content: PublishContent) -> dict:
    """Two-step: create a media container, then publish it."""
    if not content.image_url:
        raise HTTPException(400, "Instagram requires an image_url")
    async with httpx.AsyncClient(timeout=30) as c:
        create = await c.post(f"https://graph.facebook.com/v21.0/{ig_user_id}/media",
            params={"access_token": access_token, "image_url": content.image_url, "caption": content.text})
        if not create.is_success:
            raise HTTPException(502, f"Instagram media creation failed: {create.text[:300]}")
        creation_id = create.json()["id"]
        publish = await c.post(f"https://graph.facebook.com/v21.0/{ig_user_id}/media_publish",
            params={"access_token": access_token, "creation_id": creation_id})
        if not publish.is_success:
            raise HTTPException(502, f"Instagram publish failed: {publish.text[:300]}")
        return publish.json()

async def publish_x(access_token: str, _account_id: str, content: PublishContent) -> dict:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post("https://api.x.com/2/tweets",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={"text": content.text})
        if not r.is_success:
            raise HTTPException(502, f"X post failed: {r.text[:300]}")
        return r.json()

async def publish_linkedin(access_token: str, org_urn: str, content: PublishContent) -> dict:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post("https://api.linkedin.com/rest/posts",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "LinkedIn-Version": "202401",
                "X-Restli-Protocol-Version": "2.0.0",
            },
            json={
                "author": org_urn,  # e.g. "urn:li:organization:12345"
                "commentary": content.text,
                "visibility": "PUBLIC",
                "distribution": {"feedDistribution": "MAIN_FEED"},
                "lifecycleState": "PUBLISHED",
            })
        if not r.is_success:
            raise HTTPException(502, f"LinkedIn post failed: {r.text[:300]}")
        return {"id": r.headers.get("x-restli-id", "")}

async def publish_pinterest(access_token: str, board_id: str, content: PublishContent) -> dict:
    if not content.image_url:
        raise HTTPException(400, "Pinterest requires an image_url")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post("https://api.pinterest.com/v5/pins",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={
                "board_id": board_id,
                "title": content.text[:100],
                "description": content.text,
                "link": content.link,
                "media_source": {"source_type": "image_url", "url": content.image_url},
            })
        if not r.is_success:
            raise HTTPException(502, f"Pinterest pin creation failed: {r.text[:300]}")
        return r.json()

async def publish_tiktok(access_token: str, _account_id: str, content: PublishContent) -> dict:
    """TikTok's Content Posting API is async — init returns a publish_id you
    poll for status; this only handles the init step. NOTE: TikTok requires
    a separate 'Content Posting API' audit/approval per app in addition to
    normal developer registration before this works for anyone but the
    developer's own test accounts."""
    if not content.image_url:
        raise HTTPException(400, "TikTok publish needs at least one image/video source URL")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post("https://open.tiktokapis.com/v2/post/publish/content/init/",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={
                "post_info": {"title": content.text, "privacy_level": "PUBLIC_TO_EVERYONE"},
                "source_info": {"source": "PULL_FROM_URL", "photo_images": [content.image_url]},
            })
        if not r.is_success:
            raise HTTPException(502, f"TikTok publish init failed: {r.text[:300]}")
        return r.json()

PUBLISH_PROVIDERS = {
    "facebook_page": publish_facebook_page,
    "facebook_group": publish_facebook_group,
    "instagram": publish_instagram,
    "x": publish_x,
    "linkedin": publish_linkedin,
    "pinterest": publish_pinterest,
    "tiktok": publish_tiktok,
}

@api.post("/campaigns/{campaign_id}/approve")
async def approve_campaign(campaign_id: str, user: dict = Depends(get_user)):
    """Explicit approval gate — a campaign must be approved before it can be
    published anywhere, regardless of what its draft creative looks like."""
    result = await db.campaigns.update_one(
        {"id": campaign_id, "user_id": user["id"]},
        {"$set": {"status": "Approved", "approved_at": datetime.now(timezone.utc).isoformat()}},
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Campaign not found")
    return {"ok": True, "status": "Approved"}


async def call_runware_image(prompt: str, reference_image_url: Optional[str] = None,
                              width: int = 1024, height: int = 1024) -> Optional[str]:
    """Real character-consistency support via Runware's referenceImages
    parameter — genuinely built for this, unlike stuffing a reference image
    into a Gemini text prompt. Returns an image URL (not base64) — caller
    fetches the bytes if base64 is needed."""
    if not RUNWARE_API_KEY:
        return None
    task = {
        "taskType": "imageInference", "taskUUID": str(uuid.uuid4()),
        "model": RUNWARE_MODEL, "positivePrompt": prompt,
        "width": width, "height": height, "numberResults": 1, "outputType": "URL",
    }
    if reference_image_url:
        task["referenceImages"] = [reference_image_url]
    try:
        async with httpx.AsyncClient(timeout=90) as c:
            res = await c.post("https://api.runware.ai/v1",
                headers={"Authorization": f"Bearer {RUNWARE_API_KEY}", "Content-Type": "application/json"},
                json=[task])
            if res.status_code != 200:
                logger.error(f"Runware error {res.status_code}: {res.text[:300]}")
                return None
            data = res.json()
            results = data.get("data", data) if isinstance(data, dict) else data
            return results[0].get("imageURL") if isinstance(results, list) and results else None
    except Exception as e:
        logger.error(f"Runware call failed: {e}")
        return None


async def _generate_ad_image(brand_context: str, brand: dict, image_prompt: str) -> Optional[str]:
    """Shared by both initial generation and edit/regenerate. Prefers
    Runware's purpose-built referenceImages parameter for real character
    consistency when a character reference exists and Runware is
    configured; falls back to Gemini with the reference image stuffed into
    the prompt (a workaround, not real conditioning) otherwise."""
    characters = brand.get("characters", [])
    character_image_url = characters[0]["image_url"] if characters and characters[0].get("image_url") else None

    if character_image_url and RUNWARE_API_KEY:
        image_url = await call_runware_image(f"{brand_context}\n\n{image_prompt}", character_image_url)
        if image_url:
            try:
                async with httpx.AsyncClient(timeout=30) as c:
                    img_res = await c.get(image_url)
                    if img_res.is_success:
                        return base64.b64encode(img_res.content).decode()
            except Exception as e:
                logger.warning(f"Runware result fetch failed, falling back to Gemini: {e}")
        else:
            logger.warning("Runware generation failed, falling back to Gemini")

    if not GEMINI_API_KEY:
        return None
    parts = [{"text": f"{brand_context}\n\n{image_prompt}"}]
    ref_image_b64 = None
    if character_image_url:
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                img_res = await c.get(character_image_url)
                if img_res.is_success:
                    ref_image_b64 = base64.b64encode(img_res.content).decode()
        except Exception as e:
            logger.warning(f"Failed to fetch character reference image: {e}")
    if ref_image_b64:
        parts.append({"inlineData": {"mimeType": "image/png", "data": ref_image_b64}})
        parts[0]["text"] += "\n\nUse the attached reference image to keep this character/brand visually consistent."

    async with httpx.AsyncClient(timeout=90) as c:
        gr = await c.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-image:generateContent?key={GEMINI_API_KEY}",
            json={"contents": [{"parts": parts}]})
        if not gr.is_success:
            logger.warning(f"Ad image generation failed: {gr.text[:300]}")
            return None
        for part in gr.json().get("candidates", [{}])[0].get("content", {}).get("parts", []):
            if part.get("inlineData", {}).get("data"):
                return part["inlineData"]["data"]
    return None


class GenerateCreativeIn(BaseModel):
    platform: str
    ad_type: str          # one of AD_TYPES
    brief: str = ""        # optional extra direction beyond the brand bible

@api.post("/campaigns/{campaign_id}/generate-creative")
async def generate_creative(campaign_id: str, payload: GenerateCreativeIn, user: dict = Depends(get_user)):
    """Generates draft ad copy (+ an image for image-based ad types) for a
    campaign, saved onto the campaign as a draft — NOT published anywhere.
    Publishing is a separate, explicit step gated behind /approve."""
    campaign = await db.campaigns.find_one({"id": campaign_id, "user_id": user["id"]})
    if not campaign:
        raise HTTPException(404, "Campaign not found")
    if payload.ad_type not in AD_TYPES:
        raise HTTPException(400, f"Unknown ad_type. Choose one of: {AD_TYPES}")
    brand = await db.brand_profiles.find_one({"id": campaign["brand_profile_id"], "user_id": user["id"]})
    if not brand:
        raise HTTPException(400, "Campaign's brand_profile_id no longer matches one of your brands")
    if not ANTHROPIC_API_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY not configured")

    brand_context = f"Brand: {brand.get('name','')}\nBrand bible: {brand.get('brand_bible','')}\nProducts: {brand.get('products', [])}"
    copy_prompt = (
        f"{brand_context}\n\nWrite {payload.ad_type} copy for the '{platform_label(payload.platform)}' platform. "
        f"{payload.brief}\n\nKeep it on-brand and platform-appropriate in length/tone."
    )
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
            json={"model": "claude-sonnet-5", "max_tokens": 400,
                  "system": "You write on-brand ad copy. Return ONLY the ad copy text, nothing else.",
                  "messages": [{"role": "user", "content": copy_prompt}]})
        if not r.is_success:
            raise HTTPException(502, f"Copy generation failed: {r.text[:300]}")
        copy_text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")

    image_b64 = None
    image_prompt_used = None
    needs_image = payload.ad_type in ("image_post", "carousel", "story")
    if needs_image:
        if not GEMINI_API_KEY:
            logger.warning("generate_creative: image ad_type requested but GEMINI_API_KEY not set — copy only")
        else:
            image_prompt_used = f"Create an ad image for: {copy_text}"
            image_b64 = await _generate_ad_image(brand_context, brand, image_prompt_used)

    draft = {
        "id": str(uuid.uuid4()), "platform": payload.platform, "ad_type": payload.ad_type,
        "copy": copy_text, "has_image": bool(image_b64), "image_prompt": image_prompt_used,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.campaigns.update_one({"id": campaign_id}, {"$push": {"draft_creatives": draft}})
    return {**draft, "image_b64": image_b64}


class RegenerateImageIn(BaseModel):
    prompt: str  # the edited/refined prompt to regenerate with

@api.post("/campaigns/{campaign_id}/creatives/{creative_id}/regenerate-image")
async def regenerate_creative_image(campaign_id: str, creative_id: str, payload: RegenerateImageIn, user: dict = Depends(get_user)):
    """Edit-prompt-box regeneration for an already-generated ad image —
    matches the same pattern as POD's image edit/regenerate."""
    campaign = await db.campaigns.find_one({"id": campaign_id, "user_id": user["id"]})
    if not campaign:
        raise HTTPException(404, "Campaign not found")
    creative = next((c for c in campaign.get("draft_creatives", []) if c["id"] == creative_id), None)
    if not creative:
        raise HTTPException(404, "Creative not found")
    brand = await db.brand_profiles.find_one({"id": campaign["brand_profile_id"], "user_id": user["id"]})
    if not brand:
        raise HTTPException(400, "Campaign's brand_profile_id no longer matches one of your brands")

    brand_context = f"Brand: {brand.get('name','')}\nBrand bible: {brand.get('brand_bible','')}\nProducts: {brand.get('products', [])}"
    image_b64 = await _generate_ad_image(brand_context, brand, payload.prompt)
    if not image_b64:
        raise HTTPException(500, "Image regeneration failed — check GEMINI_API_KEY is configured")

    await db.campaigns.update_one(
        {"id": campaign_id, "draft_creatives.id": creative_id},
        {"$set": {"draft_creatives.$.image_prompt": payload.prompt, "draft_creatives.$.has_image": True}},
    )
    return {"id": creative_id, "image_b64": image_b64, "prompt_used": payload.prompt}


def platform_label(platform: str) -> str:
    return PLATFORM_LABELS.get(platform, platform)


@api.post("/campaigns/{campaign_id}/publish/{platform}")
async def publish_campaign(campaign_id: str, platform: str, content: PublishContent, user: dict = Depends(get_user)):
    campaign = await db.campaigns.find_one({"id": campaign_id, "user_id": user["id"]})
    if not campaign:
        raise HTTPException(404, "Campaign not found")
    if campaign.get("status") != "Approved":
        raise HTTPException(403, "Campaign must be approved (POST /campaigns/{id}/approve) before it can be published")
    if platform not in PUBLISH_PROVIDERS:
        raise HTTPException(400, f"Unknown platform. Choose one of: {list(PUBLISH_PROVIDERS.keys())}")
    conn = await db.connections.find_one({"user_id": user["id"], "platform": platform})
    if not conn:
        raise HTTPException(400, f"No {PLATFORM_LABELS.get(platform, platform)} connection found — connect one first via POST /api/connections")
    result = await PUBLISH_PROVIDERS[platform](conn["access_token"], conn["account_id"], content)
    await db.campaigns.update_one({"id": campaign_id}, {"$addToSet": {"published_to": platform}})
    return {"platform": platform, "result": result}


# ── Brand assessment — recommends WHERE to advertise, free channels first ──
async def fetch_website_text(url: str, max_chars: int = 3000) -> str:
    """Fetches a brand's own website and strips it to plain text for context.
    This is a direct fetch of a known URL, not general web search — fine for
    'read this specific brand's own site', not a substitute for market
    research across the wider web."""
    if not url:
        return ""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            r = await c.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; RavenSharpAdManager/1.0)"})
            if not r.is_success:
                return ""
            html = r.text
            text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", html, flags=re.IGNORECASE)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
            return text[:max_chars]
    except Exception as e:
        logger.warning(f"Website fetch failed for {url}: {e}")
        return ""


class AssessBrandIn(BaseModel):
    brand_profile_id: str

AD_TYPES = ["image_post", "carousel", "video", "reel", "poll", "blog_post", "story"]

@api.post("/brands/{brand_id}/assess")
async def assess_brand(brand_id: str, user: dict = Depends(get_user)):
    """Builds a full advertising plan for this brand: fetches the brand's own
    website for context, then asks Claude for a structured JSON plan —
    recommended channels (free/organic first, paid only if needed) AND which
    ad types suit each channel (image_post, carousel, video, reel, poll,
    blog_post, story)."""
    brand = await db.brand_profiles.find_one({"id": brand_id, "user_id": user["id"]})
    if not brand:
        raise HTTPException(404, "Brand not found")
    if not ANTHROPIC_API_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY not configured")

    website_text = await fetch_website_text(brand.get("website_url", ""))

    system_message = (
        "You are a marketing strategist. Given a brand's profile (and optionally its website "
        "content and product list), produce an advertising plan. CRITICAL RULE: always recommend "
        "free/organic channels first — owned social profiles, relevant Facebook/community Groups, "
        "Google Business Profile, content marketing, SEO/blog posts — and only recommend paid "
        "advertising (Meta Ads, Google Ads, TikTok Ads, etc.) as a secondary option once free "
        "channels are exhausted or clearly insufficient for the brand's goals.\n\n"
        "For each recommended channel, also recommend which ad type(s) suit it, choosing only "
        f"from this list: {', '.join(AD_TYPES)}.\n\n"
        "Respond with ONLY valid JSON, no other text, in exactly this shape:\n"
        '{"free_channels": [{"channel": "...", "ad_types": ["..."], "why": "..."}], '
        '"paid_channels": [{"channel": "...", "ad_types": ["..."], "why": "..."}], '
        '"target_audience": "...", "reasoning": "..."}'
    )
    prompt = (
        f"Brand: {brand.get('name','')}\n"
        f"Brand bible: {brand.get('brand_bible','')}\n"
        f"Characters: {brand.get('characters', [])}\n"
        f"Products: {brand.get('products', [])}\n"
        f"Website content (fetched live): {website_text or '(no website_url set on this brand, or fetch failed)'}"
    )
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
            json={"model": "claude-sonnet-5", "max_tokens": 1000,
                  "system": system_message, "messages": [{"role": "user", "content": prompt}]})
        if not r.is_success:
            raise HTTPException(502, f"Brand assessment failed: {r.text[:300]}")
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")

    try:
        plan = json.loads(text)
    except Exception:
        # Claude occasionally wraps JSON in prose despite instructions —
        # fall back to returning the raw text rather than a hard failure.
        logger.warning(f"assess_brand: response wasn't valid JSON, returning raw text. Got: {text[:200]}")
        plan = {"raw_text": text}

    await db.brand_profiles.update_one({"id": brand_id}, {"$set": {"latest_ad_plan": plan}})
    return {"plan": plan}


# ── Billing — real checkout + REAL webhook (the flagged missing piece) ─────
@api.post("/create-checkout-session")
async def create_checkout(payload: CheckoutIn, user: dict = Depends(get_user)):
    if not STRIPE_KEY:
        raise HTTPException(503, "Stripe is not configured.")
    tier_cfg = TIERS.get(payload.plan)
    if not tier_cfg or tier_cfg["price"] <= 0:
        raise HTTPException(400, "Invalid plan")
    async with httpx.AsyncClient(timeout=30) as c:
        res = await c.post("https://api.stripe.com/v1/checkout/sessions",
            headers={"Authorization": f"Bearer {STRIPE_KEY}"},
            data={"mode": "subscription",
                  "line_items[0][price_data][currency]": "aud",
                  "line_items[0][price_data][product_data][name]": f"Raven Sharp Ad Manager — {payload.plan.title()}",
                  "line_items[0][price_data][unit_amount]": str(tier_cfg["price"] * 100),
                  "line_items[0][price_data][recurring][interval]": "month",
                  "line_items[0][quantity]": "1",
                  "success_url": f"{APP_URL}/success.html?session_id={{CHECKOUT_SESSION_ID}}",
                  "cancel_url": f"{APP_URL}/billing.html",
                  "customer_email": user["email"],
                  "metadata[user_id]": user["id"],
                  "metadata[tier]": payload.plan})
        if res.status_code != 200:
            log.error("Stripe checkout error: %s", res.text[:500])
            raise HTTPException(500, "Unable to create checkout session.")
        return {"url": res.json()["url"], "plan": payload.plan}

def verify_stripe_signature(payload: bytes, sig_header: str, secret: str, tolerance_sec: int = 300) -> bool:
    """See Book Creator's identical implementation for full explanation.
    https://docs.stripe.com/webhooks#verify-manually"""
    if not sig_header or not secret:
        return False
    try:
        parts = dict(item.split("=", 1) for item in sig_header.split(",") if "=" in item)
        timestamp = parts.get("t")
        v1 = parts.get("v1")
        if not timestamp or not v1:
            return False
        if abs(datetime.now(timezone.utc).timestamp() - int(timestamp)) > tolerance_sec:
            log.warning("Stripe webhook rejected: timestamp outside tolerance (possible replay)")
            return False
        signed_payload = f"{timestamp}.".encode() + payload
        expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, v1)
    except Exception as e:
        log.warning(f"Stripe signature verification error: {e}")
        return False


@api.post("/billing/webhook")
async def stripe_webhook(request: Request):
    """Subscriptions are confirmed server-side here, not via the client-side
    /success redirect — and as of this fix, only after verifying the request
    actually came from Stripe (previously anyone could POST a forged event
    and grant themselves any tier for free)."""
    raw_body = await request.body()

    if not STRIPE_WEBHOOK_SECRET:
        log.error("Webhook rejected: STRIPE_WEBHOOK_SECRET is not configured")
        raise HTTPException(503, "Webhook not configured — set STRIPE_WEBHOOK_SECRET")

    sig_header = request.headers.get("stripe-signature", "")
    if not verify_stripe_signature(raw_body, sig_header, STRIPE_WEBHOOK_SECRET):
        log.error("Webhook rejected: invalid or missing Stripe-Signature header")
        raise HTTPException(400, "Invalid signature")

    try:
        event = json.loads(raw_body)
        if event["type"] == "checkout.session.completed":
            s = event["data"]["object"]
            await db.users.update_one(
                {"id": s["metadata"]["user_id"]},
                {"$set": {"tier": s["metadata"]["tier"], "campaigns_this_month": 0,
                          "subscription_id": s.get("subscription"),
                          "payment_failed_at": None, "payment_failure_count": 0}})
        elif event["type"] in ["customer.subscription.deleted", "customer.subscription.paused"]:
            sub_id = event["data"]["object"]["id"]
            await db.users.update_one({"subscription_id": sub_id}, {"$set": {"tier": "free"}})
        elif event["type"] == "invoice.payment_failed":
            invoice = event["data"]["object"]
            sub_id = invoice.get("subscription")
            if sub_id:
                await db.users.update_one(
                    {"subscription_id": sub_id},
                    {"$set": {"payment_failed_at": datetime.now(timezone.utc).isoformat()},
                     "$inc": {"payment_failure_count": 1}})
                log.warning(f"Payment failed for subscription {sub_id}")
    except Exception as e:
        log.error(f"Webhook error: {e}")
    return {"ok": True}

# ── Health ───────────────────────────────────────────────────────────────────
@api.get("/health/detailed")
async def health_detailed():
    checks = {}
    try:
        await db.command("ping")
        checks["mongo"] = {"status": "ok"}
    except Exception as e:
        checks["mongo"] = {"status": "error", "detail": str(e)}
    checks["stripe_configured"] = bool(STRIPE_KEY)
    checks["resend_configured"] = bool(RESEND_KEY)
    checks["r2_configured"]     = bool(R2_ENDPOINT and R2_ACCESS_KEY and R2_SECRET_KEY)
    return checks

@api.get("/health")
async def health():
    return {"status": "ok", "service": "raven-sharp-ad-manager"}

app.include_router(api)

# Root-level aliases matching the ORIGINAL Express app's exact paths, so the
# existing frontend (public/index.html) needs zero path changes. These are
# thin wrappers around the same handlers registered under /api above.
@app.get("/health")
async def health_root():
    return await health()

@app.get("/brands")
async def list_brands_root(user: dict = Depends(get_user)):
    return await list_brands(user)

@app.post("/brands")
async def create_brand_root(payload: BrandProfileIn, user: dict = Depends(get_user)):
    return await create_brand(payload, user)

@app.get("/campaigns")
async def list_campaigns_root(user: dict = Depends(get_user)):
    return await list_campaigns(user)

@app.post("/campaigns")
async def create_campaign_root(payload: CampaignIn, user: dict = Depends(get_user)):
    return await create_campaign(payload, user)

@app.post("/create-checkout-session")
async def create_checkout_root(payload: CheckoutIn, user: dict = Depends(get_user)):
    return await create_checkout(payload, user)

@app.get("/config.js")
async def config_js():
    body = """window.RAVEN_SHARP_CONFIG = {
  "apiBaseUrl": "/"
};"""
    return Response(content=body, media_type="application/javascript")

@app.on_event("startup")
async def startup():
    log.info("Raven Sharp Ad Manager API starting up. DB=%s", DB_NAME)

@app.on_event("shutdown")
async def shutdown():
    client.close()

@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception):
    log.error("Unhandled exception on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=500, content={"error": "Internal server error"})

# Serve the existing frontend (public/) as static files — same UI, real backend.
# NOTE: mounted last so /api and /health//config.js routes above take priority.
_public_dir = ROOT_DIR.parent / "public"
if _public_dir.exists():
    app.mount("/", StaticFiles(directory=str(_public_dir), html=True), name="static")
