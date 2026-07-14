const express = require('express');
const cors = require('cors');
const dotenv = require('dotenv');
const Stripe = require('stripe');

dotenv.config();

const app = express();
const PORT = process.env.PORT || 3000;
const frontendUrl = process.env.FRONTEND_URL || '';
const allowedOrigins = (process.env.ALLOWED_ORIGINS || frontendUrl || '*')
  .split(',')
  .map((value) => value.trim())
  .filter(Boolean);

app.use(cors({
  origin(origin, callback) {
    if (!origin || allowedOrigins.includes('*') || allowedOrigins.includes(origin)) {
      return callback(null, true);
    }

    return callback(new Error('Origin is not allowed by CORS.'));
  }
}));

app.use(express.json({ limit: '1mb' }));

// Temporary prototype storage. Replace with Supabase/Postgres before production.
const brands = [];
const campaigns = [];
const ads = [];

app.get('/', (req, res) => {
  res.json({
    name: 'Raven Sharp Ad Manager API',
    status: 'online'
  });
});

app.get('/health', (req, res) => {
  res.json({
    status: 'ok',
    timestamp: new Date().toISOString()
  });
});

app.get('/brands', (req, res) => res.json(brands));

app.post('/brands', (req, res) => {
  const brand = {
    id: Date.now().toString(),
    ...req.body,
    createdAt: new Date().toISOString()
  };

  brands.push(brand);
  res.status(201).json(brand);
});

app.get('/campaigns', (req, res) => res.json(campaigns));

app.post('/campaigns', (req, res) => {
  const campaign = {
    id: Date.now().toString(),
    ...req.body,
    status: 'Draft',
    createdAt: new Date().toISOString()
  };

  campaigns.push(campaign);
  res.status(201).json(campaign);
});

app.get('/ads', (req, res) => res.json(ads));

app.post('/create-checkout-session', async (req, res) => {
  const { priceId, plan } = req.body;

  if (!process.env.STRIPE_SECRET_KEY) {
    return res.status(503).json({ error: 'Stripe is not configured on the server.' });
  }

  if (!frontendUrl) {
    return res.status(503).json({ error: 'FRONTEND_URL is not configured on the server.' });
  }

  if (!priceId) {
    return res.status(400).json({ error: 'priceId is required.' });
  }

  try {
    const stripe = new Stripe(process.env.STRIPE_SECRET_KEY);
    const session = await stripe.checkout.sessions.create({
      mode: 'subscription',
      payment_method_types: ['card'],
      line_items: [{ price: priceId, quantity: 1 }],
      success_url: `${frontendUrl}/success?session_id={CHECKOUT_SESSION_ID}`,
      cancel_url: `${frontendUrl}/billing`
    });

    return res.json({ url: session.url, plan: plan || null });
  } catch (error) {
    console.error('Stripe checkout error:', error);
    return res.status(500).json({
      error: 'Unable to create checkout session.',
      details: process.env.NODE_ENV === 'development' ? error.message : undefined
    });
  }
});

app.use((req, res) => {
  res.status(404).json({ error: 'Route not found.' });
});

app.use((error, req, res, next) => {
  console.error('Unhandled server error:', error);
  res.status(500).json({ error: 'Internal server error.' });
});

app.listen(PORT, '0.0.0.0', () => {
  console.log(`Raven Sharp Ad Manager API running on port ${PORT}`);
});
