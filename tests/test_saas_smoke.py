import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SaaSSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.landing = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        cls.backend = (ROOT / "backend" / "server.py").read_text(encoding="utf-8")

    def test_landing_contains_decision_information(self):
        for required in (
            'id="features"',
            'id="demo"',
            'id="plans"',
            "Free",
            "Starter",
            "Pro",
            "Frequently asked questions",
        ):
            self.assertIn(required, self.landing)

    def test_login_and_registration_are_available(self):
        self.assertIn('id="loginForm"', self.landing)
        self.assertIn('id="registerForm"', self.landing)
        self.assertIn("/auth/login", self.landing)
        self.assertIn("/auth/register", self.landing)

    def test_brand_and_legal_footer_is_present(self):
        self.assertIn("Ascension Digital Group", self.landing)
        self.assertIn("/legal/privacy", self.landing)
        self.assertIn("/legal/terms", self.landing)
        self.assertIn("/legal/cookies", self.landing)

    def test_checkout_and_webhook_are_wired(self):
        self.assertIn("/create-checkout-session", self.landing)
        self.assertIn('@api.post("/create-checkout-session")', self.backend)
        self.assertIn('@api.post("/billing/webhook")', self.backend)
        self.assertIn("verify_stripe_signature", self.backend)

    def test_readme_matches_current_fastapi_persistent_architecture(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("FastAPI service", readme)
        self.assertIn("MongoDB", readme)
        self.assertIn("STRIPE_WEBHOOK_SECRET", readme)
        self.assertNotIn("One Express server", readme)
        self.assertNotIn("persistence and Stripe webhooks are not implemented", readme)


if __name__ == "__main__":
    unittest.main()

