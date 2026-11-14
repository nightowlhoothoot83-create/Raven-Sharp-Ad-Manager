# Publication receipts and recovery

The existing campaign approval route still gates all publication. Identical user/campaign/platform/account/content submits have one durable Mongo `_id`; requests after an ambiguous timeout are blocked to avoid duplicate posts. Records contain no access tokens. Read records with authenticated `GET /api/campaigns/{id}/publications`.

`confirmed` means a provider API returned an object identifier; it does not prove public visibility, traffic or sales. `submitted` means TikTok accepted an asynchronous submission. The existing TikTok adapter has no status polling and must not be described as confirmed publication. `submitting` that remains after a crash and `needs_reconciliation` enter the failure queue for operator inspection. There is one submission attempt and no automatic retry of irreversible actions.

Before retrying, check the provider account and API status for the exact timestamp/content, record a found post identifier, or establish that no submission occurred. Escalate expired tokens, suspensions and unfamiliar errors. Never delete an ambiguous record merely to make retry work. Rollback the code to the previous commit without clearing publication records. No schedule is enabled by this change.

Facebook Group publishing was deprecated by Meta in 2024 and is excluded from the supported provider map. Pinterest requires its actual board ID. See https://help.metricool.com/api-limitations-per-social-network-n7zlr . Other adapters still need live account and current API-version verification before launch.

## Content Creator manifest bridge

Authenticated `POST /api/campaign-imports/plan` accepts the Content Creator `campaign.json` directly in `manifest`, plus an owned `brand_profile_id`. This performs deterministic validation without fetching URLs, invoking AI, or contacting social providers. `POST /api/campaign-imports` accepts the same body and stores a Draft campaign with its source manifest and plan. Exact repeated imports reuse the campaign ID through Mongo `_id` uniqueness; changing user, brand, manifest or media configuration creates a different draft. Existing campaign approval and durable submission remain separate actions.

Example request body (replace the asset URL and board ID with actual authorised values):

```json
{
  "brand_profile_id": "YOUR_OWNED_BRAND_ID",
  "manifest": {"campaign": "example-name", "posts": [], "assets": []},
  "media_urls": {"slide-01.png": "https://YOUR_PUBLIC_ASSET_HOST/slide-01.png"},
  "selections": {"facebook_page": "slide-01.png", "pinterest": "slide-01.png"},
  "board_ids": {"pinterest": "YOUR_BOARD_ID"}
}
```

The abbreviated `manifest` above must be replaced with the complete generated JSON. `media_urls` maps its exact asset filenames to public HTTPS locations; `selections` uses mapped platform names and defaults to the first PNG/JPEG. `facebook` maps to `facebook_page`. The bridge currently prepares a single image for Facebook Pages, Instagram and Pinterest. It does not assemble an Instagram carousel. Video files, YouTube and TikTok remain explicitly blocked; TikTok's existing unverified asynchronous adapter is not enabled by this bridge.

`prepared_for_review` only means the offline request shape passed. Missing media or Pinterest board IDs, invalid destinations and missing expected affiliate disclosure block individual posts. Captions and destination URLs are retained byte-for-byte, including tracking and signed referral parameters. Network approval still requires operator verification; a disclosure prefix does not establish approval. There are no invented analytics or submission receipts.

URL checks require HTTPS and reject credentials, nonstandard ports, local/reserved hostname suffixes, and literal nonpublic IPs. No DNS lookup, redirect validation, file download, MIME or remote checksum verification occurs: ownership, reachability, public DNS resolution, image type, platform account permissions and current API contracts remain launch checks. Importing never schedules or publishes a post. An operator can use each prepared plan entry's `content` with the existing approved-campaign publish route after those checks. Publish requests now enforce the same URL shape checks before provider calls.
