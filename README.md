# Raven Sharp Ad Manager

A FastAPI service on Railway serves the dashboard, API routes, authentication,
persistent product data and Stripe Checkout. MongoDB stores user, brand,
campaign and connection records; Cloudflare R2 can store uploaded brand assets.

## Deploy to Railway

Connect this repository as one Railway service. Leave the root directory at the repository root.

Add these required Railway variables:

```text
MONGO_URL=mongodb+srv://...
DB_NAME=ravensharp_admanager
JWT_SECRET=replace-with-a-long-random-secret
FRONTEND_URL=https://ads.raven-sharp.com
APP_URL=https://ads.raven-sharp.com
```

Configure these variables for the corresponding optional workflows:

```text
STRIPE_API_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_STARTER_PRICE_ID=price_...
STRIPE_PRO_PRICE_ID=price_...
RESEND_API_KEY=re_...
R2_ENDPOINT=https://...
R2_ACCESS_KEY=...
R2_SECRET_KEY=...
R2_BUCKET=adg-images
R2_PUBLIC_URL=https://...
ANTHROPIC_API_KEY=...
```

Railway supplies `PORT` automatically. The health endpoint is `/health`.
See `backend/.env.example` for the full configuration list and social-publishing
provider notes.

## Local development

```text
cd backend
pip install -r requirements.txt
uvicorn server:app --reload
```

## Current limitations

- MongoDB is required at startup; the service fails fast when `MONGO_URL` is absent.
- If `JWT_SECRET` is absent, a temporary secret is generated and sessions will not
  survive a restart. Set a stable production secret.
- Billing checkout requires Stripe configuration. Webhooks reject requests when
  `STRIPE_WEBHOOK_SECRET` is absent or the signature is invalid.
- Brand assessment and AI creative generation require `ANTHROPIC_API_KEY`.
- R2 asset uploads require the R2 variables above.
- Social publishing also depends on each provider's developer-app approval and a
  per-user access token supplied through the connections API.
