/*
 * Registro do Service Worker e controle do botão "Instalar app" (PWA).
 */
(function () {
  // Registra o Service Worker (escopo raiz: /sw.js)
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/sw.js").catch(function (erro) {
        console.warn("Falha ao registrar o Service Worker do BolsaTrader:", erro);
      });
    });

    // O service-worker-source.js chama skipWaiting()/clients.claim() ao
    // ativar uma versão nova, então o navegador troca o "controller" da
    // página sem avisar - sem isso, a aba continua rodando com o HTML/JS
    // antigo enquanto o SW novo já assumiu, misturando versões. Recarrega
    // uma única vez quando isso acontece, pra página e SW ficarem sempre
    // na mesma versão (a flag evita loop caso o evento dispare mais de uma vez).
    let jaRecarregou = false;
    navigator.serviceWorker.addEventListener("controllerchange", function () {
      if (jaRecarregou) return;
      jaRecarregou = true;
      window.location.reload();
    });
  }

  // Captura o evento de instalação e exibe o botão "Instalar app" quando
  // disponível - só um dos dois botões existe em cada página (o do
  // cabeçalho pra quem está logado, o flutuante pra quem não está), mas o
  // evento de instalação do navegador pode disparar em qualquer uma delas.
  let eventoInstalacaoAdiado = null;
  const botoesInstalar = [
    document.getElementById("btn-instalar-pwa"),
    document.getElementById("btn-instalar-pwa-anonimo"),
  ].filter(Boolean);

  window.addEventListener("beforeinstallprompt", function (evento) {
    evento.preventDefault();
    eventoInstalacaoAdiado = evento;
    botoesInstalar.forEach(function (botao) {
      botao.style.display = "inline-block";
    });
  });

  botoesInstalar.forEach(function (botao) {
    botao.addEventListener("click", function () {
      if (!eventoInstalacaoAdiado) return;
      botoesInstalar.forEach(function (b) { b.style.display = "none"; });
      eventoInstalacaoAdiado.prompt();
      eventoInstalacaoAdiado.userChoice.finally(function () {
        eventoInstalacaoAdiado = null;
      });
    });
  });

  window.addEventListener("appinstalled", function () {
    botoesInstalar.forEach(function (botao) {
      botao.style.display = "none";
    });
  });

  // Menu de navegação colapsável (hambúrguer) em telas estreitas
  const botaoMenu = document.getElementById("btn-menu-mobile");
  const navPrincipal = document.getElementById("nav-principal");
  if (botaoMenu && navPrincipal) {
    botaoMenu.addEventListener("click", function () {
      const aberto = navPrincipal.classList.toggle("nav-aberta");
      botaoMenu.setAttribute("aria-expanded", aberto ? "true" : "false");
    });
    // fecha o menu ao navegar para outra página pelo link tocado
    navPrincipal.addEventListener("click", function (evento) {
      if (evento.target.tagName === "A") {
        navPrincipal.classList.remove("nav-aberta");
        botaoMenu.setAttribute("aria-expanded", "false");
      }
    });
  }
})();
