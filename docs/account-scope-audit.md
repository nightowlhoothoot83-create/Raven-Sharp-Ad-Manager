# Ad Manager Brand Account-Scope Audit

Branch-only audit. No production, merge, deploy, billing, or ad-publishing changes are authorised by this file.

## Backend finding

The backend brand API is already user-scoped:

- `POST /api/brands` counts and creates profiles for the authenticated user.
- `GET /api/brands` queries `brand_profiles` by `user_id`.
- `GET /api/brands/{id}`, `PUT`, `DELETE`, and brand asset upload all filter by both brand id and authenticated `user_id`.
- Registration assigns the configured owner email to the `owner` tier.
- Owner-brand seeding is guarded by `tier == owner` and creates profiles with the owner user's id.

So the backend ownership model is substantially correct.

## Frontend problem

The current `public/index.html` still uses a single browser-local key (`ravenSharpAdManager`) and seeds Raven Sharp, Spew Crew Kids and MyCalcTools directly into browser state. It also contains broken mojibake symbols in the card icon field.

That means the frontend source of truth is not aligned with the backend account isolation.

## Repair target

1. Authenticated backend `/api/brands` becomes the source of truth for brand cards.
2. New customer accounts start with no Emma/ADG brands.
3. Owner presets are seeded only through the existing owner-only backend endpoint.
4. Remove broken symbol/icon text from cards.
5. If local storage remains at all, use it only as an account-keyed cache and never as authoritative brand ownership state.
6. On logout, clear account-specific UI state from memory.
7. Add a two-account E2E test proving Account B cannot see Account A brands/assets.

## Acceptance criteria

- Customer A creates Brand A.
- Customer B logs in in the same browser and cannot see Brand A.
- Customer B never receives Raven Sharp / Spew Crew / MyCalcTools owner presets unless B is the configured owner account.
- Owner account can seed its presets idempotently.
- No mojibake or broken symbols render in brand cards.
- Logout/login switches UI state to the authenticated account's backend records.
