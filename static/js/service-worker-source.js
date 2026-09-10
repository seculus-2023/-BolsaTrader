/*
 * Service Worker do BolsaTrader (PWA).
 *
 * Estratégia adotada, pensada para um sistema financeiro pessoal:
 *   - O "app shell" (CSS, ícones, manifest, tela de login e tela offline)
 *     é pré-cacheado e funciona mesmo sem internet.
 *   - Páginas com dados da carteira (painel, posições, operações, etc.)
 *     NUNCA são exibidas a partir do cache: elas exigem rede, pois mostram
 *     cotações e saldos que precisam estar sempre atualizados e são
 *     específicos de cada usuário logado. Se a rede falhar, o usuário vê a
 *     tela "Você está offline" em vez de dados desatualizados ou de outra
 *     conta.
 *   - Arquivos estáticos (CSS/JS/ícones) usam "cache-first com atualização
 *     em segundo plano" (stale-while-revalidate), para carregar instantâneo
 *     e sempre se manter atualizado.
 */

const VERSAO_CACHE = "bolsatrader-v2";
const CACHE_SHELL = `${VERSAO_CACHE}-shell`;

const ARQUIVOS_APP_SHELL = [
  "/static/css/futurista.css",
  "/static/js/pwa.js",
  "/static/manifest.webmanifest",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/icon-512-maskable.png",
  "/static/icons/apple-touch-icon.png",
  "/static/icons/favicon-32.png",
  "/static/icons/favicon-16.png",
  "/offline/",
];

self.addEventListener("install", (evento) => {
  evento.waitUntil(
    caches.open(CACHE_SHELL).then((cache) => cache.addAll(ARQUIVOS_APP_SHELL))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (evento) => {
  evento.waitUntil(
    caches.keys().then((nomes) =>
      Promise.all(
        nomes
          .filter((nome) => nome.startsWith("bolsatrader-") && nome !== CACHE_SHELL)
          .map((nome) => caches.delete(nome))
      )
    )
  );
  self.clients.claim();
});

function ehArquivoEstatico(url) {
  return url.origin === self.location.origin && url.pathname.startsWith("/static/");
}

self.addEventListener("fetch", (evento) => {
  const { request } = evento;
  if (request.method !== "GET") return; // nunca interceptar POST (formulários) etc.

  const url = new URL(request.url);

  // Navegação de página (o usuário abrindo/trocando de tela)
  if (request.mode === "navigate") {
    evento.respondWith(
      fetch(request).catch(() =>
        caches.match("/offline/").then((resp) => resp || Response.error())
      )
    );
    return;
  }

  // Arquivos estáticos: cache-first, com atualização em segundo plano
  if (ehArquivoEstatico(url)) {
    evento.respondWith(
      caches.open(CACHE_SHELL).then((cache) =>
        cache.match(request).then((respostaCache) => {
          const buscaRede = fetch(request)
            .then((respostaRede) => {
              cache.put(request, respostaRede.clone());
              return respostaRede;
            })
            .catch(() => respostaCache);
          return respostaCache || buscaRede;
        })
      )
    );
    return;
  }

  // Demais requisições (dados, API, admin, etc.): sempre buscar da rede.
});
