{% load static %}
const CACHE_NAME = 'infinity-ohm-v1';
const OFFLINE_URL = '{% url "landing_page:offline" %}';

// Add the assets you want to cache on install
const PRECACHE_ASSETS = [
    OFFLINE_URL,
    '{% static "landing_page/css/style.css" %}',
    '{% static "landing_page/img/favicon.ico" %}',
    '{% static "landing_page/img/InfinityOhm_Logo.webp" %}'
];

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => {
            return cache.addAll(PRECACHE_ASSETS);
        })
    );
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((cacheNames) => {
            return Promise.all(
                cacheNames.map((cacheName) => {
                    if (cacheName !== CACHE_NAME) {
                        return caches.delete(cacheName);
                    }
                })
            );
        })
    );
    self.clients.claim();
});

self.addEventListener('fetch', (event) => {
    // Only intercept navigation requests for HTML
    if (event.request.mode === 'navigate') {
        event.respondWith(
            fetch(event.request).catch(() => {
                return caches.match(OFFLINE_URL);
            })
        );
        return;
    }

    // For other requests (CSS, JS, images), use a cache-first approach
    // falling back to network
    event.respondWith(
        caches.match(event.request).then((response) => {
            return response || fetch(event.request);
        })
    );
});
