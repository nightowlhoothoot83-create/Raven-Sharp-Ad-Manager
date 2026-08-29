const OWNER_EMAIL = "ascensiondigitalagency@outlook.com";
const OWNER_NAME = "Emma James";
const DEFAULT_BACKEND_URL = "https://raven-sharp-ad-manager-production.up.railway.app";

const HOMEPAGE_MUSHROOM_EXAMPLE = /\s*<figure class="creative-example"><img src="\/showcase\/mushroom-artwork-v2\.jpg"[^>]*><figcaption>Visual-led creative · artwork campaign<\/figcaption><\/figure>/;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname.startsWith('/api/')) {
      const backend = String(env.BACKEND_URL || DEFAULT_BACKEND_URL).replace(/\/$/, '');
      const target = new URL(url.pathname + url.search, backend + '/');
      const headers = new Headers(request.headers);
      headers.set('host', target.host);
      headers.set('x-forwarded-host', url.host);
      headers.set('x-forwarded-proto', 'https');

      return fetch(new Request(target, {
        method: request.method,
        headers,
        body: ['GET', 'HEAD'].includes(request.method) ? undefined : request.body,
        redirect: 'manual'
      }));
    }

    const response = await env.ASSETS.fetch(request);
    if (request.method !== 'GET' || (url.pathname !== '/' && url.pathname !== '/index.html')) {
      return response;
    }

    const contentType = response.headers.get('content-type') || '';
    if (!contentType.includes('text/html')) return response;

    const html = (await response.text()).replace(HOMEPAGE_MUSHROOM_EXAMPLE, '');
    const headers = new Headers(response.headers);
    headers.delete('content-length');

    return new Response(html, {
      status: response.status,
      statusText: response.statusText,
      headers
    });
  }
};
