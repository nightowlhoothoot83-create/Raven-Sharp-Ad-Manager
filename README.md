# Raven Sharp Ad Manager

A single-service Railway app. One Express server serves the dashboard, API routes and Stripe Checkout.

## Deploy to Railway

Connect this repository as one Railway service. Leave the root directory at the repository root.

Add these Railway variables:

```text
NODE_ENV=production
STRIPE_SECRET_KEY=sk_test_...
STRIPE_STARTER_PRICE_ID=price_...
STRIPE_PRO_PRICE_ID=price_...
APP_URL=https://your-service.up.railway.app
```

Railway supplies `PORT` automatically. The health endpoint is `/health`.

## Current limitations

- Brand and campaign records use temporary memory and browser storage.
- Server memory resets when Railway restarts.
- Authentication, Supabase persistence and Stripe webhooks are not implemented yet.
- Use Stripe test mode until webhook-based access control is added.
