const express = require('express');
const path = require('path');

const app = express();
const PORT = process.env.PORT || 3000;
const publicDirectory = path.join(__dirname, 'public');

app.get('/health', (req, res) => {
  res.json({
    status: 'ok',
    service: 'raven-sharp-ad-manager-frontend'
  });
});

app.get('/config.js', (req, res) => {
  const config = {
    apiBaseUrl: process.env.API_BASE_URL || '',
    stripeStarterPriceId: process.env.STRIPE_STARTER_PRICE_ID || '',
    stripeProPriceId: process.env.STRIPE_PRO_PRICE_ID || ''
  };

  res.type('application/javascript');
  res.send(`window.RAVEN_SHARP_CONFIG = ${JSON.stringify(config)};`);
});

app.use(express.static(publicDirectory));

app.get('/success', (req, res) => {
  res.sendFile(path.join(publicDirectory, 'success.html'));
});

app.get('/billing', (req, res) => {
  res.sendFile(path.join(publicDirectory, 'billing.html'));
});

app.get('*', (req, res) => {
  res.sendFile(path.join(publicDirectory, 'index.html'));
});

app.listen(PORT, '0.0.0.0', () => {
  console.log(`Raven Sharp Ad Manager frontend running on port ${PORT}`);
});
