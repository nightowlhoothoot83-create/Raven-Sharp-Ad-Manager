const express = require('express');
const cors = require('cors');
const dotenv = require('dotenv');
const path = require('path');
const Stripe = require('stripe');

dotenv.config();

const app = express();
const PORT = process.env.PORT || 3000;
const publicDir = path.join(__dirname, 'public');

app.use(cors());
app.use(express.json({ limit: '1mb' }));
app.use(express.static(publicDir));

const brands = [];
const campaigns = [];
const ads = [];

app.get('/health', (req, res) => {
  res.json({ status: 'ok', service: 'raven-sharp-ad-manager', timestamp: new Date().toISOString() });
});

app.get('/config.js', (req, res) => {
  res.type('application/javascript');
  res.send(`window.RAVEN_SHARP_CONFIG = ${JSON.stringify({
    apiBaseUrl: '/',
    stripeStarterPriceId: process.env.STRIPE_STARTER_PRICE_ID || '',
    stripeProPriceId: process.env.STRIPE_PRO_PRICE_ID || ''
  })};`);
});

app.get('/brands', (req, res) => res.json(brands));
app.post('/brands', (req, res) => {
  const brand = { id: Date.now().toString(), ...req.body, createdAt: new Date().toISOString() };
  brands.push(brand);
  res.status(201).json(brand);
});

app.get('/campaigns', (req, res) => res.json(campaigns));
app.post('/campaigns', (req, res) => {
  const campaign = { id: Date.now().toString(), ...req.body, status: 'Draft', createdAt: new Date().toISOString() };
  campaigns.push(campaign);
  res.status(201).json(campaign);
});

app.get('/ads', (req, res) => res.json(ads));

app.post('/create-checkout-session', async (req, res) => {
  const { priceId, plan } = req.body;

  if (!process.env.STRIPE_SECRET_KEY) {
    return res.status(503).json({ error: 'Stripe is not configured.' });
  }

  if (!priceId) {
    return res.status(400).json({ error: 'priceId is required.' });
  }

  try {
    const stripe = new Stripe(process.env.STRIPE_SECRET_KEY);
    const baseUrl = process.env.APP_URL || `${req.protocol}://${req.get('host')}`;
    const session = await stripe.checkout.sessions.create({
      mode: 'subscription',
      payment_method_types: ['card'],
      line_items: [{ price: priceId, quantity: 1 }],
      success_url: `${baseUrl}/success?session_id={CHECKOUT_SESSION_ID}`,
      cancel_url: `${baseUrl}/billing`
    });

    res.json({ url: session.url, plan: plan || null });
  } catch (error) {
    console.error('Stripe checkout error:', error);
    res.status(500).json({ error: 'Unable to create checkout session.' });
  }
});

app.get('/success', (req, res) => res.sendFile(path.join(publicDir, 'success.html')));
app.get('/billing', (req, res) => res.sendFile(path.join(publicDir, 'billing.html')));
app.get('*', (req, res) => res.sendFile(path.join(publicDir, 'index.html')));

app.listen(PORT, '0.0.0.0', () => {
  console.log(`Raven Sharp Ad Manager running on port ${PORT}`);
});
