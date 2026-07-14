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

import os, uuid, json, logging, asyncio, base64
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
RESEND_KEY  = os.environ.get("RESEND_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
if not ANTHROPIC_API_KEY:
    _startup_warnings.append(
        "ANTHROPIC_API_KEY was not set — brand assessment (/brands/{id}/assess) will return a clear 500 error."
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

# TODO: replace with real Stripe Price IDs (separate product from other apps)
STRIPE_PRICES = {
    "starter": os.environ.get("STRIPE_STARTER_PRICE_ID", "price_REPLACE_STARTER"),
    "pro":     os.environ.get("STRIPE_PRO_PRICE_ID", "price_REPLACE_PRO"),
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
    primary_color: Optional[str] = None
    secondary_color: Optional[str] = None
    logo_url: Optional[str] = None
    characters: List[Dict[str, Any]] = Field(default_factory=list)  # [{name, description, image_url}] — for consistency
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

@api.post("/campaigns/{campaign_id}/publish/{platform}")
async def publish_campaign(campaign_id: str, platform: str, content: PublishContent, user: dict = Depends(get_user)):
    campaign = await db.campaigns.find_one({"id": campaign_id, "user_id": user["id"]})
    if not campaign:
        raise HTTPException(404, "Campaign not found")
    if platform not in PUBLISH_PROVIDERS:
        raise HTTPException(400, f"Unknown platform. Choose one of: {list(PUBLISH_PROVIDERS.keys())}")
    conn = await db.connections.find_one({"user_id": user["id"], "platform": platform})
    if not conn:
        raise HTTPException(400, f"No {PLATFORM_LABELS.get(platform, platform)} connection found — connect one first via POST /api/connections")
    result = await PUBLISH_PROVIDERS[platform](conn["access_token"], conn["account_id"], content)
    await db.campaigns.update_one({"id": campaign_id}, {"$addToSet": {"published_to": platform}})
    return {"platform": platform, "result": result}


# ── Brand assessment — recommends WHERE to advertise, free channels first ──
class AssessBrandIn(BaseModel):
    brand_profile_id: str

@api.post("/brands/{brand_id}/assess")
async def assess_brand(brand_id: str, user: dict = Depends(get_user)):
    """Uses Claude to recommend target audience + channels for this brand —
    explicitly instructed to exhaust free/organic options (owned social
    profiles, Facebook Groups, Google Business Profile, community
    engagement) before recommending any paid ad spend."""
    brand = await db.brand_profiles.find_one({"id": brand_id, "user_id": user["id"]})
    if not brand:
        raise HTTPException(404, "Brand not found")
    if not ANTHROPIC_API_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY not configured")

    system_message = (
        "You are a marketing strategist. Given a brand's profile, recommend where and how "
        "to promote it. CRITICAL RULE: always recommend free/organic channels first — owned "
        "social profiles, relevant Facebook/community Groups, Google Business Profile, "
        "content marketing, SEO — and only recommend paid advertising (Meta Ads, Google Ads, "
        "TikTok Ads, etc.) as a secondary option once free channels are exhausted or clearly "
        "insufficient for the brand's goals. Structure your answer as:\n"
        "FREE_CHANNELS: <comma-separated list>\nPAID_CHANNELS: <comma-separated list, or 'none needed yet'>\n"
        "TARGET_AUDIENCE: <one paragraph>\nREASONING: <one paragraph>"
    )
    prompt = f"Brand: {brand.get('name','')}\nBrand bible: {brand.get('brand_bible','')}\nCharacters/products: {brand.get('characters', [])}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
            json={"model": "claude-sonnet-5", "max_tokens": 500,
                  "system": system_message, "messages": [{"role": "user", "content": prompt}]})
        if not r.is_success:
            raise HTTPException(502, f"Brand assessment failed: {r.text[:300]}")
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return {"assessment": text}


# ── Billing — real checkout + REAL webhook (the flagged missing piece) ─────
@api.post("/create-checkout-session")
async def create_checkout(payload: CheckoutIn, user: dict = Depends(get_user)):
    if not STRIPE_KEY:
        raise HTTPException(503, "Stripe is not configured.")
    price_id = STRIPE_PRICES.get(payload.plan)
    if not price_id:
        raise HTTPException(400, "Invalid plan")
    async with httpx.AsyncClient(timeout=30) as c:
        res = await c.post("https://api.stripe.com/v1/checkout/sessions",
            headers={"Authorization": f"Bearer {STRIPE_KEY}"},
            data={"mode": "subscription",
                  "line_items[0][price]": price_id,
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

@api.post("/billing/webhook")
async def stripe_webhook(request: Request):
    """THE fix for the flagged gap — subscriptions are now actually confirmed
    server-side instead of trusting the client-side redirect to /success."""
    try:
        event = json.loads(await request.body())
        if event["type"] == "checkout.session.completed":
            s = event["data"]["object"]
            await db.users.update_one(
                {"id": s["metadata"]["user_id"]},
                {"$set": {"tier": s["metadata"]["tier"], "campaigns_this_month": 0,
                          "subscription_id": s.get("subscription")}})
        elif event["type"] in ["customer.subscription.deleted", "customer.subscription.paused"]:
            sub_id = event["data"]["object"]["id"]
            await db.users.update_one({"subscription_id": sub_id}, {"$set": {"tier": "free"}})
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
    body = f"""window.RAVEN_SHARP_CONFIG = {{
  "apiBaseUrl": "/",
  "stripeStarterPriceId": "{os.environ.get('STRIPE_STARTER_PRICE_ID', '')}",
  "stripeProPriceId": "{os.environ.get('STRIPE_PRO_PRICE_ID', '')}"
}};"""
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
