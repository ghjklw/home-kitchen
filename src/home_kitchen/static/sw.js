'use strict';
const CACHE = 'kitchen-shell-v1';
const ASSETS = ['/static/index.html', '/static/app.js', '/static/style.css', '/static/icon.svg', '/static/manifest.webmanifest'];
self.addEventListener('install', event => { event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(ASSETS))); self.skipWaiting(); });
self.addEventListener('activate', event => { event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key.startsWith('kitchen-shell-') && key !== CACHE).map(key => caches.delete(key)))).then(() => self.clients.claim())); });
self.addEventListener('fetch', event => { const url = new URL(event.request.url); if (event.request.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/') || url.pathname.startsWith('/auth')) return; if (ASSETS.includes(url.pathname)) { event.respondWith(caches.match(event.request).then(cached => cached || fetch(event.request))); return; } if (event.request.mode === 'navigate' && url.pathname === '/') event.respondWith(fetch(event.request).catch(() => caches.match('/static/index.html'))); });
