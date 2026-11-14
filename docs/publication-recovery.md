# Publication receipts and recovery

The existing campaign approval route still gates all publication. Identical user/campaign/platform/account/content submits have one durable Mongo `_id`; requests after an ambiguous timeout are blocked to avoid duplicate posts. Records contain no access tokens. Read records with authenticated `GET /api/campaigns/{id}/publications`.

`confirmed` means a provider API returned an object identifier; it does not prove public visibility, traffic or sales. `submitted` means TikTok accepted an asynchronous submission. The existing TikTok adapter has no status polling and must not be described as confirmed publication. `submitting` that remains after a crash and `needs_reconciliation` enter the failure queue for operator inspection. There is one submission attempt and no automatic retry of irreversible actions.

Before retrying, check the provider account and API status for the exact timestamp/content, record a found post identifier, or establish that no submission occurred. Escalate expired tokens, suspensions and unfamiliar errors. Never delete an ambiguous record merely to make retry work. Rollback the code to the previous commit without clearing publication records. No schedule is enabled by this change.

Facebook Group publishing was deprecated by Meta in 2024 and is excluded from the supported provider map. Pinterest requires its actual board ID. See https://help.metricool.com/api-limitations-per-social-network-n7zlr . Other adapters still need live account and current API-version verification before launch.
