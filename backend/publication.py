"""Durable at-most-once submission; ambiguous failures require reconciliation."""
import hashlib
import json
from datetime import datetime, timezone


class ReconciliationRequired(Exception):
    pass


def publication_id(user_id, campaign_id, platform, account_id, content):
    payload = [user_id, campaign_id, platform, account_id, content]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def receipt(platform, result):
    """An asynchronous acceptance is never publication confirmation."""
    data = result.get('data') or {}
    if platform == 'tiktok':
        if not data.get('publish_id'):
            raise ReconciliationRequired('Provider returned no submission identifier')
        return 'submitted', str(data['publish_id'])
    post_id = result.get('post_id') or result.get('id') or data.get('id')
    if not post_id:
        raise ReconciliationRequired('Provider returned no publication identifier')
    return 'confirmed', str(post_id)


async def submit_once(collection, *, user_id, campaign_id, platform, account_id, content, send):
    key = publication_id(user_id, campaign_id, platform, account_id, content)
    now = datetime.now(timezone.utc).isoformat()
    # Mongo _id uniqueness makes this atomic across processes and restarts.
    try:
        await collection.insert_one({'_id': key, 'user_id': user_id, 'campaign_id': campaign_id,
            'platform': platform, 'account_id': account_id, 'status': 'submitting',
            'created_at': now, 'updated_at': now, 'attempts': 1})
    except Exception as exc:
        if getattr(exc, 'code', None) != 11000:
            raise
        existing = await collection.find_one({'_id': key, 'user_id': user_id}, {'_id': 0})
        if existing and existing['status'] in ('confirmed', 'submitted'):
            return {**existing, 'duplicate_prevented': True}
        raise ReconciliationRequired('Prior attempt may have reached provider; reconcile before retry') from None
    try:
        result = await send()
        status, provider_id = receipt(platform, result)
    except Exception as exc:
        # Do not store provider errors: they may contain tokens or personal data.
        await collection.update_one({'_id': key}, {'$set': {'status': 'needs_reconciliation',
            'error_type': type(exc).__name__, 'updated_at': datetime.now(timezone.utc).isoformat()}})
        raise ReconciliationRequired('Submission outcome uncertain; automatic retry suppressed') from None
    record = {'status': status, 'provider_id': provider_id, 'updated_at': datetime.now(timezone.utc).isoformat()}
    await collection.update_one({'_id': key}, {'$set': record})
    return {'publication_id': key, **record, 'duplicate_prevented': False}
