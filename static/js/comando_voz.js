// Comando de voz em português (Web Speech API, pt-BR).
//
// O botão 🎤 do cabeçalho (#btn-comando-voz) escuta UMA frase por clique e a
// traduz em navegação: "abrir posições", "nova operação", "atualizar
// cotações", "cotação de PETR4"... As URLs vêm do template (base.html, bloco
// JSON #rotas-comando-voz, gerado com {% url %}), então este arquivo não
// conhece caminhos fixos. Funciona no Chrome/Edge (desktop e Android); em
// navegadores sem suporte (ex.: Firefox) o botão fica escondido.
//
// Modo "sempre ouvindo" (botão 👂, #btn-voz-continua): o microfone fica aberto
// e só reage à palavra de ativação "Trader", como a Alexa - "Trader, abrir
// posições" executa direto; "Trader" sozinho abre uma janela de alguns
// segundos para a próxima frase. A escolha fica no localStorage e o modo
// religa sozinho a cada página. Diferente da Alexa, o áudio vai continuamente
// para o serviço de reconhecimento do navegador enquanto o modo está ligado.
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
        { termos: ["atualizar cripto", "atualizar as cripto", "atualiza cripto"], rota: "cripto_atualizar", post: true, aviso: "Atualizando cripto..." },
        { termos: ["atualizar noticia", "atualiza noticia", "atualizar as noticia"], rota: "noticias_atualizar", post: true, aviso: "Buscando notícias..." },
        { termos: ["atualizar acoes da b3", "atualizar lista de acoes"], rota: "acoes_b3_atualizar", post: true, aviso: "Atualizando ações da B3..." },
        { termos: ["atualizar", "atualiza"], rota: "atualizar_cotacoes", post: true, aviso: "Atualizando cotações..." },
        { termos: ["nova operacao cripto", "nova compra cripto", "comprar cripto"], rota: "cripto_operacao_nova" },
        { termos: ["operacoes cripto", "operacoes de cripto", "operacao cripto"], rota: "cripto_operacao_lista" },
        { termos: ["nova operacao", "nova compra", "comprar acao", "registrar compra"], rota: "operacao_nova" },
        { termos: ["registro de cotac", "registros de cotac"], rota: "registros_cotacao" },
        { termos: ["historico", "grafico"], rota: "historico_graficos" },
        { termos: ["detalhes de cotac", "detalhe de cotac", "detalhes da cotac", "detalhes das cotac"], rota: "detalhes_cotacoes" },
        { termos: ["operacoes", "operacao"], rota: "operacao_lista" },
        { termos: ["posicoes", "posicao", "carteira"], rota: "posicoes" },
        { termos: ["qualidade", "fundamento", "fundamentalista"], rota: "qualidade_empresas" },
        { termos: ["analise", "mercado"], rota: "analise_mercado" },
        { termos: ["scanner", "escaner"], rota: "scanner_tecnico" },
        { termos: ["previsao dos analistas", "previsoes dos analistas", "previsao de analista", "previsoes de analista", "preco alvo"], rota: "previsoes_analistas" },
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

    // Comandos que alteram dados (post: true) são enviados por POST, com o
    // token CSRF da página - as views correspondentes recusam GET.
    function enviarPost(url) {
        var form = document.createElement("form");
        form.method = "post";
        form.action = url;
        var token = document.createElement("input");
        token.type = "hidden";
        token.name = "csrfmiddlewaretoken";
        token.value = blocoRotas.dataset.csrf || "";
        form.appendChild(token);
        document.body.appendChild(form);
        form.submit();
    }

    function irPara(url, aviso, post) {
        var overlay = document.getElementById("overlay-atualizando");
        var textoOverlay = document.getElementById("overlay-atualizando-texto");
        if (aviso && overlay) {
            textoOverlay.textContent = aviso;
            overlay.hidden = false;
        }
        if (post) {
            enviarPost(url);
        } else {
            window.location.href = url;
        }
    }

    function executar(textoFalado) {
        var texto = normalizar(textoFalado);

        if (/\b(ajuda|comandos|o que (eu )?posso falar)\b/.test(texto)) {
            mostrarBalao(
                "Experimente: \"posições\", \"operações\", \"nova operação\", \"alertas\", " +
                "\"notícias\", \"conta corrente\", \"cripto\", \"atualizar cotações\", " +
                "\"cotação de PETR4\", \"voltar\". Com o 👂 ligado, comece com \"Trader\": " +
                "\"Trader, posições\".",
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
                irPara(rotas[comando.rota], comando.aviso, comando.post);
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
        // Com o modo contínuo ligado o microfone já está aberto: o clique
        // equivale a dizer "Trader" (não dá para rodar dois reconhecimentos).
        if (modoContinuo) {
            aguardarComando();
            return;
        }
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

    // ----- Modo "sempre ouvindo" com palavra de ativação -----

    var botaoContinuo = document.getElementById("btn-voz-continua");
    var CHAVE_PREFERENCIA = "bolsatrader.vozContinua";
    var JANELA_COMANDO_MS = 8000;
    // "Trader" em pt-BR costuma vir transcrito de vários jeitos.
    var PALAVRA_ATIVACAO = /(^|\s)(trader|traider|treider|treder|trayder|treyder|tréider|trade|treide)(?=[\s,.!?]|$)[\s,.!?]*/i;

    var modoContinuo = false;
    var aguardandoAte = 0;
    var timerJanela = null;
    var falhasSeguidas = 0;
    var continuo = new Reconhecimento();
    continuo.lang = "pt-BR";
    continuo.interimResults = true;
    continuo.maxAlternatives = 1;
    continuo.continuous = true;

    function lerPreferencia() {
        try { return localStorage.getItem(CHAVE_PREFERENCIA) === "1"; } catch (e) { return false; }
    }

    function gravarPreferencia(ligado) {
        try { localStorage.setItem(CHAVE_PREFERENCIA, ligado ? "1" : "0"); } catch (e) { /* sem storage */ }
    }

    function aguardarComando() {
        aguardandoAte = Date.now() + JANELA_COMANDO_MS;
        mostrarBalao("🎤 Pode falar...");
        clearTimeout(timerJanela);
        timerJanela = setTimeout(function () {
            aguardandoAte = 0;
            if (balao.textContent.indexOf("Pode falar") !== -1) balao.hidden = true;
        }, JANELA_COMANDO_MS);
    }

    function encerrarJanela() {
        aguardandoAte = 0;
        clearTimeout(timerJanela);
    }

    function atualizarBotaoContinuo() {
        botaoContinuo.classList.toggle("continuo", modoContinuo);
        botaoContinuo.setAttribute("aria-pressed", modoContinuo ? "true" : "false");
    }

    function iniciarContinuo() {
        try {
            continuo.start();
        } catch (erro) {
            // já em execução - ignora
        }
    }

    function ligarContinuo(avisar) {
        if (ouvindo) reconhecimento.abort();
        modoContinuo = true;
        falhasSeguidas = 0;
        gravarPreferencia(true);
        atualizarBotaoContinuo();
        iniciarContinuo();
        if (avisar) mostrarBalao("👂 Sempre ouvindo. Diga \"Trader\" e o comando, ex.: \"Trader, posições\".", 6000);
    }

    function desligarContinuo(mensagem) {
        modoContinuo = false;
        encerrarJanela();
        gravarPreferencia(false);
        atualizarBotaoContinuo();
        continuo.abort();
        mostrarBalao(mensagem || "👂 Modo sempre ouvindo desligado.", mensagem ? 7000 : 3000);
    }

    continuo.addEventListener("start", function () {
        falhasSeguidas = 0;
    });

    continuo.addEventListener("result", function (evento) {
        for (var i = evento.resultIndex; i < evento.results.length; i++) {
            var frase = evento.results[i][0].transcript.trim();
            var ativacao = frase.match(PALAVRA_ATIVACAO);
            var emJanela = Date.now() < aguardandoAte;

            if (!evento.results[i].isFinal) {
                // Mostra o que está sendo dito só quando é com o sistema.
                if (ativacao || emJanela) mostrarBalao("🎤 " + frase);
                continue;
            }

            if (ativacao) {
                var comando = frase.slice(ativacao.index + ativacao[0].length).trim();
                if (comando) {
                    encerrarJanela();
                    executar(comando);
                } else {
                    aguardarComando();
                }
            } else if (emJanela && frase) {
                encerrarJanela();
                executar(frase);
            } else if (balao.textContent.indexOf("🎤 ") === 0 && !emJanela) {
                balao.hidden = true;
            }
            // Qualquer outra fala (conversa, TV...) é ignorada.
        }
    });

    continuo.addEventListener("error", function (evento) {
        if (evento.error === "not-allowed" || evento.error === "service-not-allowed") {
            if (esperandoInteracao) return;
            desligarContinuo("Permissão do microfone negada. Libere o microfone nas configurações do navegador.");
        } else if (evento.error === "audio-capture") {
            desligarContinuo("Nenhum microfone encontrado. Modo sempre ouvindo desligado.");
        } else if (evento.error !== "no-speech" && evento.error !== "aborted") {
            falhasSeguidas++;
        }
    });

    // O navegador encerra a escuta depois de um tempo (silêncio, limite de
    // sessão, erro de rede): religa sozinho enquanto o modo estiver ligado,
    // esperando mais a cada falha seguida para não ficar em loop.
    continuo.addEventListener("end", function () {
        if (!modoContinuo || esperandoInteracao) return;
        if (falhasSeguidas >= 5) {
            desligarContinuo("Reconhecimento de voz indisponível (sem conexão?). Modo sempre ouvindo desligado.");
            return;
        }
        setTimeout(function () {
            if (modoContinuo) iniciarContinuo();
        }, 300 + falhasSeguidas * 2000);
    });

    botaoContinuo.hidden = false;
    botaoContinuo.addEventListener("click", function () {
        if (modoContinuo) {
            desligarContinuo();
        } else {
            esperandoInteracao = false;
            ligarContinuo(true);
        }
    });

    // Religa ao abrir cada página. Se o navegador exigir um gesto do usuário
    // para abrir o microfone, tenta de novo no primeiro clique/tecla.
    var esperandoInteracao = false;
    if (lerPreferencia()) {
        esperandoInteracao = true;
        modoContinuo = true;
        atualizarBotaoContinuo();
        continuo.addEventListener("start", function () { esperandoInteracao = false; }, { once: true });
        iniciarContinuo();
        var tentarDeNovo = function () {
            document.removeEventListener("pointerdown", tentarDeNovo, true);
            document.removeEventListener("keydown", tentarDeNovo, true);
            if (esperandoInteracao && modoContinuo) {
                esperandoInteracao = false;
                iniciarContinuo();
            }
        };
        document.addEventListener("pointerdown", tentarDeNovo, true);
        document.addEventListener("keydown", tentarDeNovo, true);
    }
})();
