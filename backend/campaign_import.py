"""Offline Content Creator manifest bridge. Never calls a publishing provider."""
import hashlib
import ipaddress
import json
from datetime import datetime, timezone
from urllib.parse import urlsplit

IMAGE_PLATFORMS = {'facebook_page', 'instagram', 'pinterest'}


def public_https(value):
    if not isinstance(value, str) or any(c.isspace() for c in value):
        return False
    try:
        p = urlsplit(value)
        host = (p.hostname or '').lower().rstrip('.')
        if p.scheme != 'https' or not host or p.username or p.password or p.port not in (None, 443):
            return False
        if '.' not in host or host.endswith(('.localhost', '.local', '.internal', '.invalid', '.test', '.example')) or host in {'example.com', 'example.org', 'example.net'}:
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return True
    except ValueError:
        return False


def campaign_identity(user_id, brand_id, manifest, media_urls, selections, board_ids):
    payload = [user_id, brand_id, manifest, media_urls, selections, board_ids]
    return 'import-' + hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def plan_import(*, user_id, brand_id, manifest, media_urls=None, selections=None, board_ids=None):
    media_urls, selections, board_ids = media_urls or {}, selections or {}, board_ids or {}
    if not isinstance(manifest, dict) or not isinstance(manifest.get('campaign'), str) or not manifest['campaign'].strip():
        raise ValueError('Manifest requires campaign name')
    posts, assets = manifest.get('posts'), manifest.get('assets')
    if not isinstance(posts, list) or not posts or not isinstance(assets, list):
        raise ValueError('Manifest requires posts and assets arrays')
    files = {}
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get('file'), str):
            raise ValueError('Each asset requires a file name')
        name = asset['file']
        if '/' in name or '\\' in name or name in files:
            raise ValueError('Asset names must be unique plain file names')
        files[name] = asset
    images = [name for name in files if name.lower().endswith(('.png', '.jpg', '.jpeg'))]
    planned, seen = [], set()
    for post in posts:
        if not isinstance(post, dict) or not isinstance(post.get('platform'), str) or not isinstance(post.get('caption'), str):
            raise ValueError('Each post requires platform and caption')
        platform = {'facebook': 'facebook_page'}.get(post['platform'], post['platform'])
        if platform in seen:
            raise ValueError('Duplicate platform in manifest')
        seen.add(platform)
        selected = selections.get(platform, images[0] if images else None)
        url = media_urls.get(selected) if selected else None
        reasons = []
        if platform not in IMAGE_PLATFORMS:
            reasons.append('unsupported_platform_in_manifest_bridge')
        if selected not in files:
            reasons.append('missing_manifest_asset')
        elif selected not in images:
            reasons.append('unsupported_media_type_video_or_other')
        if not public_https(url):
            reasons.append('public_https_media_url_required')
        if not public_https(post.get('destination')):
            reasons.append('public_https_destination_required')
        if platform == 'pinterest' and not board_ids.get(platform):
            reasons.append('pinterest_board_id_required')
        if manifest.get('affiliate') and not post['caption'].lower().startswith('ad / affiliate link:'):
            reasons.append('affiliate_disclosure_required')
        planned.append({'platform': platform, 'status': 'blocked' if reasons else 'prepared_for_review',
            'blockers': reasons, 'asset_file': selected,
            'content': {'text': post['caption'], 'link': post.get('destination'),
                'image_url': url if selected in images else None, 'board_id': board_ids.get(platform)}})
    return {'campaign_id': campaign_identity(user_id, brand_id, manifest, media_urls, selections, board_ids),
        'status': 'dry_run', 'posts': planned, 'published': False,
        'checks_remaining': ['media ownership and reachability (including DNS/redirects)',
            'audience fit and affiliate network approval', 'platform connection and current API contract',
            'profile link setup where needed', 'explicit campaign approval before publishing']}


async def import_draft(collection, *, user_id, brand_id, manifest, media_urls=None, selections=None, board_ids=None):
    plan = plan_import(user_id=user_id, brand_id=brand_id, manifest=manifest,
        media_urls=media_urls, selections=selections, board_ids=board_ids)
    key = plan['campaign_id']
    record = {'_id': key, 'id': key, 'user_id': user_id, 'brand_profile_id': brand_id,
        'title': manifest['campaign'], 'objective': manifest.get('audience', ''), 'status': 'Draft',
        'publish_targets': [p['platform'] for p in plan['posts'] if not p['blockers']],
        'import_plan': plan, 'source_manifest': manifest, 'created_at': datetime.now(timezone.utc).isoformat()}
    try:
        await collection.insert_one(record)
    except Exception as exc:
        if getattr(exc, 'code', None) != 11000:
            raise
        return {'campaign_id': key, 'status': 'existing_draft', 'duplicate_prevented': True, 'plan': plan}
    return {'campaign_id': key, 'status': 'draft_created', 'duplicate_prevented': False, 'plan': plan}
