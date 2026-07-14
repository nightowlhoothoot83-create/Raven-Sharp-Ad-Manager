# Raven Sharp Ad Manager

A two-service starter project for Railway.

- `backend/` contains the Express API and Stripe Checkout endpoint.
- `frontend/` contains the Raven Sharp Ad Manager dashboard and web server.

## Deploy on Railway

Create two Railway services from this one GitHub repository.

### 1. Backend service

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

Railway supplies `PORT` automatically. The health endpoint is `/health`.

### 2. Frontend service

Set the Railway root directory to:

```text
/frontend
```

After the backend receives a public Railway domain, add:

```text
API_BASE_URL=https://your-backend-service.up.railway.app
STRIPE_STARTER_PRICE_ID=price_...
STRIPE_PRO_PRICE_ID=price_...
```

The frontend health endpoint is `/health`.

## Stripe setup

Use Stripe test mode first. Create recurring Starter and Pro prices. Put the safe `price_...` identifiers in the frontend variables, and place the secret key only in the backend variables.

## Current prototype limitations

- Brand and campaign data is stored in the browser and also sent to temporary backend memory when connected.
- Backend memory resets when the service restarts.
- Authentication is not implemented yet.
- Supabase persistence and Stripe webhooks still need to be added before production use.
- A successful checkout does not yet grant access automatically.
