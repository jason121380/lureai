"use strict";

const CACHE_NAME = "lureai-shell-v1";
const SHELL_ASSETS = [
  "/",
  "/app.css",
  "/chat.js",
  "/logo.svg",
  "/favicon.png",
  "/app-icon-192.png",
  "/app-icon.png",
  "/manifest.webmanifest",
  "/vendor/lucide.min.js",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  // Never intercept API traffic or cross-origin requests: answers, auth and
  // usage must always reflect the live server.
  if (request.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/api/")) {
    return;
  }
  // Network-first for the shell so deploys show up immediately; the cache is
  // only the offline/slow-network fallback.
  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response.ok && (SHELL_ASSETS.includes(url.pathname) || url.pathname.startsWith("/vendor/"))) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
        }
        return response;
      })
      .catch(() =>
        caches.match(request).then((cached) => cached || (request.mode === "navigate" ? caches.match("/") : undefined))
      )
  );
});
