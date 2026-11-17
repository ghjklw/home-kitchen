'use strict';
const CACHE = 'kitchen-shell-v13';
const ASSETS = ['/static/index.html', '/static/app.js', '/static/shopping-share.js', '/static/shopping-view.js', '/static/recipe-editor.js', '/static/recipe-photo.js', '/static/recipe-collections.js', '/static/recipe-images.js', '/static/recipe-discovery.js', '/static/planned-cooking.js', '/static/basket-match-view.js', '/static/settings-view.js', '/static/unmapped-ingredients-view.js', '/static/recipe-feedback-quick.js', '/static/style.css', '/static/icon.svg', '/static/manifest.webmanifest'];
self.addEventListener('install', event => { event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(ASSETS))); self.skipWaiting(); });
self.addEventListener('activate', event => { event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key.startsWith('kitchen-shell-') && key !== CACHE).map(key => caches.delete(key)))).then(() => self.clients.claim())); });
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin || !ASSETS.includes(url.pathname)) {
    if (event.request.mode === 'navigate' && url.origin === self.location.origin && url.pathname === '/') {
      event.respondWith(fetch(event.request).catch(() => caches.match('/static/index.html')));
    }
    return;
  }
  // Refresh static code on every online visit; never cache sign-in redirects.
  event.respondWith(fetch(event.request).then(async response => {
    if (response.ok && !response.redirected && new URL(response.url).origin === self.location.origin) {
      const cache = await caches.open(CACHE);
      await cache.put(event.request, response.clone());
    }
    return response;
  }).catch(() => caches.match(event.request)));
});
