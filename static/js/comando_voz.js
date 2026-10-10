// Comando de voz em português (Web Speech API, pt-BR).
//
// O botão 🎤 do cabeçalho (#btn-comando-voz) escuta UMA frase por clique e a
// traduz em navegação: "abrir posições", "nova operação", "atualizar
// cotações", "cotação de PETR4"... As URLs vêm do template (base.html, bloco
// JSON #rotas-comando-voz, gerado com {% url %}), então este arquivo não
// conhece caminhos fixos. Funciona no Chrome/Edge (desktop e Android); em
// navegadores sem suporte (ex.: Firefox) o botão fica escondido.
(function () {
    var botao = document.getElementById("btn-comando-voz");
    var blocoRotas = document.getElementById("rotas-comando-voz");
    var Reconhecimento = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!botao || !blocoRotas || !Reconhecimento) return;

    var rotas = JSON.parse(blocoRotas.textContent);
    botao.hidden = false;

    // Comandos em ordem de prioridade: os mais específicos primeiro
    // ("nova operação" antes de "operações", "atualizar cripto" antes de
    // "atualizar"). Cada termo é comparado com o texto normalizado (minúsculo
    // e sem acentos).
    var COMANDOS = [
        { termos: ["historico de atualizac", "historico das atualizac"], rota: "historico_atualizacoes" },
        { termos: ["atualizar cripto", "atualizar as cripto", "atualiza cripto"], rota: "cripto_atualizar", aviso: "Atualizando cripto..." },
        { termos: ["atualizar noticia", "atualiza noticia", "atualizar as noticia"], rota: "noticias_atualizar", aviso: "Buscando notícias..." },
        { termos: ["atualizar acoes da b3", "atualizar lista de acoes"], rota: "acoes_b3_atualizar", aviso: "Atualizando ações da B3..." },
        { termos: ["atualizar", "atualiza"], rota: "atualizar_cotacoes", aviso: "Atualizando cotações..." },
        { termos: ["nova operacao cripto", "nova compra cripto", "comprar cripto"], rota: "cripto_operacao_nova" },
        { termos: ["operacoes cripto", "operacoes de cripto", "operacao cripto"], rota: "cripto_operacao_lista" },
        { termos: ["nova operacao", "nova compra", "comprar acao", "registrar compra"], rota: "operacao_nova" },
        { termos: ["registro de cotac", "registros de cotac"], rota: "registros_cotacao" },
        { termos: ["historico", "grafico"], rota: "historico_graficos" },
        { termos: ["detalhes de cotac", "detalhe de cotac", "detalhes da cotac", "detalhes das cotac"], rota: "detalhes_cotacoes" },
        { termos: ["operacoes", "operacao"], rota: "operacao_lista" },
        { termos: ["posicoes", "posicao", "carteira"], rota: "posicoes" },
        { termos: ["analise", "mercado"], rota: "analise_mercado" },
        { termos: ["scanner", "escaner"], rota: "scanner_tecnico" },
        { termos: ["trading view", "tradingview", "treidingview"], rota: "tradingview" },
        { termos: ["acoes da b3", "acoes b3", "lista de acoes"], rota: "acoes_b3" },
        { termos: ["noticia"], rota: "noticias" },
        { termos: ["alerta"], rota: "alertas" },
        { termos: ["conta corrente", "extrato", "saldo"], rota: "conta_corrente" },
        { termos: ["cripto", "bitcoin"], rota: "criptomoedas" },
        { termos: ["whatsapp", "mensagens"], rota: "mensagens_whatsapp" },
        { termos: ["minha conta", "meus dados"], rota: "minha_conta" },
        { termos: ["menu"], rota: "menu" },
        { termos: ["painel", "inicio", "dashboard", "tela inicial"], rota: "dashboard" },
    ];

    // Números falados que aparecem em tickers da B3 (PETR4, BOVA11, TAEE11...).
    var NUMEROS = {
        zero: "0", um: "1", uma: "1", dois: "2", duas: "2", tres: "3", quatro: "4",
        cinco: "5", seis: "6", sete: "7", oito: "8", nove: "9", dez: "10", onze: "11",
        doze: "12", treze: "13",
    };

    var balao = document.createElement("div");
    balao.className = "balao-comando-voz";
    balao.setAttribute("role", "status");
    balao.setAttribute("aria-live", "polite");
    balao.hidden = true;
    document.body.appendChild(balao);
    var timerBalao = null;

    function mostrarBalao(texto, duracaoMs) {
        clearTimeout(timerBalao);
        balao.textContent = texto;
        balao.hidden = false;
        if (duracaoMs) {
            timerBalao = setTimeout(function () { balao.hidden = true; }, duracaoMs);
        }
    }

    function normalizar(texto) {
        return texto
            .toLowerCase()
            .normalize("NFD")
            .replace(/[̀-ͯ]/g, "")
            .replace(/[.,!?;:]/g, " ")
            .replace(/\s+/g, " ")
            .trim();
    }

    // "petr quatro" / "PETR 4" / "p e t r 4" / "bova onze" -> PETR4 / BOVA11
    function extrairTicker(trecho) {
        var compacto = trecho
            .split(" ")
            .map(function (palavra) { return NUMEROS[palavra] || palavra; })
            .join("")
            .toUpperCase()
            .replace(/[^A-Z0-9]/g, "");
        var achado = compacto.match(/[A-Z]{4}\d{1,2}/);
        return achado ? achado[0] : null;
    }

    function irPara(url, aviso) {
        var overlay = document.getElementById("overlay-atualizando");
        var textoOverlay = document.getElementById("overlay-atualizando-texto");
        if (aviso && overlay) {
            textoOverlay.textContent = aviso;
            overlay.hidden = false;
        }
        window.location.href = url;
    }

    function executar(textoFalado) {
        var texto = normalizar(textoFalado);

        if (/\b(ajuda|comandos|o que (eu )?posso falar)\b/.test(texto)) {
            mostrarBalao(
                "Experimente: \"posições\", \"operações\", \"nova operação\", \"alertas\", " +
                "\"notícias\", \"conta corrente\", \"cripto\", \"atualizar cotações\", " +
                "\"cotação de PETR4\", \"voltar\".",
                9000
            );
            return;
        }

        if (/\bvoltar\b/.test(texto)) {
            history.back();
            return;
        }

        // "cotação de PETR4", "preço da vale três", "quanto está BBAS3"
        var consulta = texto.match(/\b(?:cotacao|cotacoes|preco|quanto (?:esta|ta|custa))\b(?: (?:de|da|do|das|dos))? ?(.*)$/);
        if (consulta && consulta[1]) {
            var ticker = extrairTicker(consulta[1]);
            if (ticker) {
                mostrarBalao("Consultando " + ticker + "...");
                irPara(rotas.detalhes_cotacoes + "?ticker=" + encodeURIComponent(ticker));
                return;
            }
        }

        for (var i = 0; i < COMANDOS.length; i++) {
            var comando = COMANDOS[i];
            var casou = comando.termos.some(function (termo) { return texto.indexOf(termo) !== -1; });
            if (casou && rotas[comando.rota]) {
                mostrarBalao("✔ " + textoFalado);
                irPara(rotas[comando.rota], comando.aviso);
                return;
            }
        }

        mostrarBalao("Não entendi: \"" + textoFalado + "\". Diga \"ajuda\" para ver os comandos.", 6000);
    }

    var reconhecimento = new Reconhecimento();
    reconhecimento.lang = "pt-BR";
    reconhecimento.interimResults = true;
    reconhecimento.maxAlternatives = 1;
    reconhecimento.continuous = false;

    var ouvindo = false;
    var textoFinal = "";

    reconhecimento.addEventListener("start", function () {
        ouvindo = true;
        textoFinal = "";
        botao.classList.add("ouvindo");
        botao.setAttribute("aria-pressed", "true");
        mostrarBalao("🎤 Ouvindo... fale um comando");
    });

    reconhecimento.addEventListener("result", function (evento) {
        var parcial = "";
        for (var i = evento.resultIndex; i < evento.results.length; i++) {
            if (evento.results[i].isFinal) {
                textoFinal += evento.results[i][0].transcript;
            } else {
                parcial += evento.results[i][0].transcript;
            }
        }
        mostrarBalao("🎤 " + (textoFinal || parcial));
    });

    reconhecimento.addEventListener("error", function (evento) {
        var mensagens = {
            "not-allowed": "Permissão do microfone negada. Libere o microfone nas configurações do navegador.",
            "service-not-allowed": "Permissão do microfone negada. Libere o microfone nas configurações do navegador.",
            "no-speech": "Não ouvi nada. Clique no 🎤 e tente de novo.",
            "audio-capture": "Nenhum microfone encontrado.",
            "network": "Sem conexão com o serviço de reconhecimento de voz.",
        };
        if (evento.error === "aborted") return;
        mostrarBalao(mensagens[evento.error] || "Erro no reconhecimento de voz (" + evento.error + ").", 6000);
        textoFinal = "";
    });

    reconhecimento.addEventListener("end", function () {
        ouvindo = false;
        botao.classList.remove("ouvindo");
        botao.setAttribute("aria-pressed", "false");
        if (textoFinal.trim()) {
            executar(textoFinal.trim());
        } else if (!balao.hidden && balao.textContent.indexOf("Ouvindo") !== -1) {
            balao.hidden = true;
        }
    });

    botao.addEventListener("click", function () {
        if (ouvindo) {
            reconhecimento.stop();
            return;
        }
        try {
            reconhecimento.start();
        } catch (erro) {
            // start() chamado enquanto ainda finaliza a sessão anterior - ignora
        }
    });

    // Atalho de teclado: Ctrl+Espaço também liga o microfone.
    document.addEventListener("keydown", function (evento) {
        if (evento.ctrlKey && evento.code === "Space") {
            evento.preventDefault();
            botao.click();
        }
    });
})();
