/* Сервис-воркер: принимает push и открывает нужный отчёт по клику.
   Пути везде относительные — приложение живёт в подпапке GitHub Pages
   (https://<пользователь>.github.io/<репозиторий>/), и абсолютный "/" увёл бы
   в корень домена. */

const CACHE = 'regwatch-v1';
const SHELL = ['./', './index.html', './config.js', './icon-192.png', './manifest.json'];

self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

/* Офлайн: отдаём из кэша, если сеть недоступна. Отчёты всегда тянем свежие. */
self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  if (req.url.includes('latest')) return;   // отчёт не кэшируем
  event.respondWith(
    fetch(req).catch(() => caches.match(req).then((r) => r || caches.match('./index.html')))
  );
});

self.addEventListener('push', (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = { body: event.data ? event.data.text() : '' };
  }

  const critical = data.level === 'critical';
  const options = {
    body: data.body || '',
    icon: './icon-192.png',
    badge: './icon-192.png',
    tag: data.tag || 'regwatch',
    renotify: true,
    // Срочное не должно исчезнуть само, пока его не увидели.
    requireInteraction: critical,
    data: { url: data.url || './' },
  };
  event.waitUntil(
    self.registration.showNotification(data.title || 'Регмонитор', options)
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || './';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
      for (const client of list) {
        if ('focus' in client) {
          if ('navigate' in client) client.navigate(target).catch(() => {});
          return client.focus();
        }
      }
      return self.clients.openWindow(target);
    })
  );
});
