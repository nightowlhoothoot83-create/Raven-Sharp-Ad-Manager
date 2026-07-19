"""Stripe billing extension for Raven Sharp Ad Manager.

Keeps the existing FastAPI application and frontend, while replacing the
checkout/config/webhook routes with monthly + yearly subscription handling.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Literal

import httpx
from fastapi import Depends, HTTPException, Request, Response
from pydantic import BaseModel

import server

app = server.app
log = server.log
db = server.db

APP_SLUG = "ad-manager"
LIVE_PRICES = {
    "starter": {
        "monthly": "price_1Tt7AgRt1GNtAll7vONiC7uP",
        "yearly": "price_1TtSLjRt1GNtAll70LiNpdv5",
    },
    "pro": {
        "monthly": "price_1Tt7AoRt1GNtAll76fG9WJ0P",
        "yearly": "price_1TtSLxRt1GNtAll7LH5zrbW7",
    },
}


def _price_from_env(plan: str, billing: str) -> str:
    names = {
        ("starter", "monthly"): (
            "STRIPE_STARTER_MONTHLY_PRICE_ID",
            "STRIPE_STARTER_PRICE_ID",
        ),
        ("starter", "yearly"): ("STRIPE_STARTER_YEARLY_PRICE_ID",),
        ("pro", "monthly"): (
            "STRIPE_PRO_MONTHLY_PRICE_ID",
            "STRIPE_PRO_PRICE_ID",
        ),
        ("pro", "yearly"): ("STRIPE_PRO_YEARLY_PRICE_ID",),
    }
    for name in names[(plan, billing)]:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return LIVE_PRICES[plan][billing]


def _all_prices() -> dict[str, dict[str, str]]:
    return {
        plan: {
            billing: _price_from_env(plan, billing)
            for billing in ("monthly", "yearly")
        }
        for plan in ("starter", "pro")
    }


def _allowed_price_ids() -> set[str]:
    return {
        price_id
        for options in _all_prices().values()
        for price_id in options.values()
        if price_id
    }


class CheckoutIn(BaseModel):
    plan: Literal["starter", "pro"]
    billing: Literal["monthly", "yearly"] = "monthly"


_static_mounts = [
    route
    for route in app.router.routes
    if route.__class__.__name__ == "Mount" and getattr(route, "name", None) == "static"
]
_remove_paths = {
    "/api/create-checkout-session",
    "/create-checkout-session",
    "/api/billing/webhook",
    "/config.js",
}
app.router.routes = [
    route
    for route in app.router.routes
    if route not in _static_mounts and getattr(route, "path", None) not in _remove_paths
]


async def _create_checkout(payload: CheckoutIn, user: dict) -> dict:
    if not server.STRIPE_KEY:
        raise HTTPException(503, "Stripe is not configured.")

    price_id = _price_from_env(payload.plan, payload.billing)
    if not price_id or not price_id.startswith("price_"):
        raise HTTPException(503, "Stripe pricing is not configured.")

    metadata = {
        "app_slug": APP_SLUG,
        "user_id": user["id"],
        "tier": payload.plan,
        "billing": payload.billing,
        "price_id": price_id,
    }
    data = {
        "mode": "subscription",
        "line_items[0][price]": price_id,
        "line_items[0][quantity]": "1",
        "success_url": f"{server.APP_URL}/success.html?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{server.APP_URL}/billing.html",
        "customer_email": user["email"],
    }
    for key, value in metadata.items():
        data[f"metadata[{key}]"] = value
        data[f"subscription_data[metadata][{key}]"] = value

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://api.stripe.com/v1/checkout/sessions",
            headers={"Authorization": f"Bearer {server.STRIPE_KEY}"},
            data=data,
        )

    if not response.is_success:
        log.error("Stripe checkout error: %s", response.text[:500])
        raise HTTPException(502, "Unable to create checkout session.")

    return {
        "url": response.json()["url"],
        "plan": payload.plan,
        "billing": payload.billing,
    }


@app.post("/api/create-checkout-session")
async def create_checkout_api(
    payload: CheckoutIn,
    user: dict = Depends(server.get_user),
):
    return await _create_checkout(payload, user)


@app.post("/create-checkout-session")
async def create_checkout_root(
    payload: CheckoutIn,
    user: dict = Depends(server.get_user),
):
    return await _create_checkout(payload, user)


def _subscription_id(value) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("id")
    return None


def _valid_tier(value: str | None) -> bool:
    return value in {"starter", "pro"}


async def _apply_subscription(
    *,
    user_id: str | None,
    subscription_id: str | None,
    tier: str | None,
    billing: str | None,
    status: str | None,
) -> None:
    if not user_id or not _valid_tier(tier):
        raise ValueError("Stripe event is missing valid user/tier metadata.")

    update = {
        "tier": tier,
        "subscription_id": subscription_id,
        "subscription_status": status or "active",
        "billing_interval": billing or "monthly",
        "campaigns_this_month": 0,
        "payment_failed_at": None,
        "payment_failure_count": 0,
    }
    result = await db.users.update_one({"id": user_id}, {"$set": update})
    if result.matched_count == 0:
        raise ValueError(f"No Ad Manager user matches Stripe metadata user_id={user_id!r}.")


@app.post("/api/billing/webhook")
async def stripe_webhook(request: Request):
    raw_body = await request.body()

    if not server.STRIPE_WEBHOOK_SECRET:
        log.error("Webhook rejected: STRIPE_WEBHOOK_SECRET is not configured")
        raise HTTPException(503, "Webhook not configured.")

    signature = request.headers.get("stripe-signature", "")
    if not server.verify_stripe_signature(
        raw_body,
        signature,
        server.STRIPE_WEBHOOK_SECRET,
    ):
        raise HTTPException(400, "Invalid Stripe signature.")

    try:
        event = json.loads(raw_body)
        event_type = event.get("type", "")
        obj = event.get("data", {}).get("object", {})
        metadata = obj.get("metadata") or {}

        event_app = metadata.get("app_slug")
        if event_app and event_app != APP_SLUG:
            return {"ok": True, "ignored": True}

        if event_type == "checkout.session.completed":
            price_id = metadata.get("price_id")
            if price_id not in _allowed_price_ids():
                raise ValueError("Checkout completed with an unexpected Price ID.")
            await _apply_subscription(
                user_id=metadata.get("user_id"),
                subscription_id=_subscription_id(obj.get("subscription")),
                tier=metadata.get("tier"),
                billing=metadata.get("billing"),
                status="active",
            )

        elif event_type in {
            "customer.subscription.created",
            "customer.subscription.updated",
        }:
            status = obj.get("status")
            subscription_id = obj.get("id")
            if status in {"active", "trialing", "past_due"}:
                await _apply_subscription(
                    user_id=metadata.get("user_id"),
                    subscription_id=subscription_id,
                    tier=metadata.get("tier"),
                    billing=metadata.get("billing"),
                    status=status,
                )
            else:
                await db.users.update_one(
                    {"subscription_id": subscription_id},
                    {"$set": {"subscription_status": status}},
                )

        elif event_type in {
            "customer.subscription.deleted",
            "customer.subscription.paused",
        }:
            subscription_id = obj.get("id")
            await db.users.update_one(
                {"subscription_id": subscription_id},
                {
                    "$set": {
                        "tier": "free",
                        "subscription_status": obj.get("status") or "canceled",
                    }
                },
            )

        elif event_type == "invoice.payment_failed":
            subscription_id = _subscription_id(obj.get("subscription"))
            if subscription_id:
                await db.users.update_one(
                    {"subscription_id": subscription_id},
                    {
                        "$set": {
                            "payment_failed_at": datetime.now(timezone.utc).isoformat(),
                            "subscription_status": "past_due",
                        },
                        "$inc": {"payment_failure_count": 1},
                    },
                )

        elif event_type == "invoice.paid":
            subscription_id = _subscription_id(obj.get("subscription"))
            if subscription_id:
                await db.users.update_one(
                    {"subscription_id": subscription_id},
                    {
                        "$set": {
                            "payment_failed_at": None,
                            "payment_failure_count": 0,
                            "subscription_status": "active",
                        }
                    },
                )

    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Stripe webhook processing failed: %s", exc)
        raise HTTPException(500, "Webhook processing failed.") from exc

    return {"ok": True}


@app.get("/config.js")
async def config_js():
    prices = _all_prices()
    config = {
        "apiBaseUrl": "/",
        "stripeStarterPriceId": prices["starter"]["monthly"],
        "stripeProPriceId": prices["pro"]["monthly"],
        "stripeStarterMonthlyPriceId": prices["starter"]["monthly"],
        "stripeStarterYearlyPriceId": prices["starter"]["yearly"],
        "stripeProMonthlyPriceId": prices["pro"]["monthly"],
        "stripeProYearlyPriceId": prices["pro"]["yearly"],
    }

    enhancement = r"""
document.addEventListener("DOMContentLoaded", () => {
  let billing = "monthly";
  const plans = document.getElementById("plans");
  if (!plans) return;

  const sectionHead = plans.querySelector(".section-head");
  if (sectionHead && !document.getElementById("billingToggle")) {
    const toggle = document.createElement("div");
    toggle.id = "billingToggle";
    toggle.className = "actions";
    toggle.style.marginTop = "0";
    toggle.innerHTML = `
      <button class="btn secondary" data-billing="monthly">Monthly</button>
      <button class="btn secondary" data-billing="yearly">Yearly · save 2 months</button>
    `;
    sectionHead.appendChild(toggle);
    toggle.querySelectorAll("button").forEach((button) => {
      button.addEventListener("click", () => {
        billing = button.dataset.billing;
        refreshPrices();
      });
    });
  }

  const cards = Array.from(plans.querySelectorAll(".plan"));
  const findCard = (name) => cards.find(
    (card) => card.querySelector("h3")?.textContent?.trim() === name
  );

  function refreshPrices() {
    const values = billing === "monthly"
      ? { Starter: ["$19", "/ month"], Pro: ["$49", "/ month"] }
      : { Starter: ["$190", "/ year"], Pro: ["$490", "/ year"] };

    for (const [name, [amount, period]] of Object.entries(values)) {
      const price = findCard(name)?.querySelector(".price");
      if (price) price.innerHTML = `${amount} <small>${period}</small>`;
    }

    document.querySelectorAll("#billingToggle button").forEach((button) => {
      button.style.background = button.dataset.billing === billing ? "#fff" : "transparent";
      button.style.color = button.dataset.billing === billing ? "#08101f" : "#f4f5ff";
    });
  }

  window.checkout = async (plan, event) => {
    const button = event?.currentTarget;
    const original = button?.textContent || "Choose plan";
    if (button) {
      button.disabled = true;
      button.textContent = "Opening Stripe…";
    }

    try {
      const response = await fetch("/create-checkout-session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ plan, billing }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || "Could not open Stripe Checkout.");
      if (!data.url) throw new Error("Stripe returned no checkout URL.");
      window.location.assign(data.url);
    } catch (error) {
      if (typeof window.showToast === "function") {
        window.showToast(error.message || "Could not open Stripe Checkout.");
      } else {
        alert(error.message || "Could not open Stripe Checkout.");
      }
      if (button) {
        button.disabled = false;
        button.textContent = original;
      }
    }
  };

  const notice = plans.querySelector(".notice");
  if (notice) {
    notice.textContent = "Secure Stripe Checkout with server-verified subscription access.";
  }
  refreshPrices();
});
"""
    body = f"window.RAVEN_SHARP_CONFIG = {json.dumps(config)};\n{enhancement}"
    return Response(content=body, media_type="application/javascript")


app.router.routes.extend(_static_mounts)
