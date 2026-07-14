# Raven Sharp Ad Manager

A two-service starter project for Railway:

- `backend/` is the Express API and Stripe Checkout service.
- `frontend/` is the Raven Sharp dashboard.

## Railway deployment

Create **two Railway services from this one GitHub repository**.

### Backend service

Set the Railway root directory to:

```text
/backend
```

Add these variables:

```text
NODE_ENV=production
STRIPE_SECRET_KEY=sk_test_...
FRONTEND_URL=https://your-frontend-service.up.railway.app
```

Railway supplies `PORT` automatically. The health check is `/health`.

### Frontend service

Set the Railway root directory to:

```text
/frontend
```

Add these variables after the backend has a public Railway domain:

```text
API_BASE_URL=https://your-backend-service.up.railway.app
STRIPE_STARTER_PRICE_ID=price_...
STRIPE_PRO_PRICE_ID=price_...
```

The frontend health check is `/health`.

## Stripe

Use Stripe test mode first. Create recurring Starter and Pro prices, then place their `price_...` IDs in the frontend service variables. Put the Stripe secret key only in the backend service variables.

## Current limitations

This is an early working foundation. Brand, campaign and ad records are stored in backend memory and reset when the backend restarts. Authentication, Supabase persistence and Stripe webhooks still need to be added before production use.
