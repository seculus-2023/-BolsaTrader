/*
 * Interatividade dos gráficos de linha (histórico de cotações e comparativo
 * investido x atual): crosshair vertical + tooltip que segue o ponto mais
 * próximo do cursor. O SVG e os pontos (x/y em unidades do viewBox) já vêm
 * prontos do backend; este script só cuida do hover.
 *
 * O tooltip usa position:fixed e é appendado no <body> porque esses gráficos
 * ficam dentro de um container com scroll horizontal (overflow-x:auto), que
 * recorta qualquer elemento "absolute" que ultrapasse sua borda.
 */
document.addEventListener("DOMContentLoaded", () => {
    const NS = "http://www.w3.org/2000/svg";

    function ativarCrosshair(svg, montarTooltip) {
        const dataEl = document.getElementById(svg.dataset.pontosId);
        if (!dataEl) return;

        let pontos;
        try {
            pontos = JSON.parse(dataEl.textContent);
        } catch (erro) {
            return;
        }
        if (!Array.isArray(pontos) || pontos.length === 0) return;

        const vb = svg.viewBox.baseVal;

        const crosshair = document.createElementNS(NS, "line");
        crosshair.setAttribute("class", "grafico-crosshair");
        crosshair.setAttribute("y1", vb.y);
        crosshair.setAttribute("y2", vb.y + vb.height);
        svg.appendChild(crosshair);

        const overlay = document.createElementNS(NS, "rect");
        overlay.setAttribute("x", vb.x);
        overlay.setAttribute("y", vb.y);
        overlay.setAttribute("width", vb.width);
        overlay.setAttribute("height", vb.height);
        overlay.setAttribute("fill", "transparent");
        overlay.style.cursor = "crosshair";
        svg.appendChild(overlay);

        const tooltip = document.createElement("div");
        tooltip.className = "grafico-tooltip";
        tooltip.style.position = "fixed";
        document.body.appendChild(tooltip);

        function pontoMaisProximo(xSvg) {
            let melhor = pontos[0];
            let menorDistancia = Math.abs(pontos[0].x - xSvg);
            for (const ponto of pontos) {
                const distancia = Math.abs(ponto.x - xSvg);
                if (distancia < menorDistancia) {
                    menorDistancia = distancia;
                    melhor = ponto;
                }
            }
            return melhor;
        }

        function mover(evento) {
            const pt = svg.createSVGPoint();
            pt.x = evento.clientX;
            pt.y = evento.clientY;
            const pontoSvg = pt.matrixTransform(svg.getScreenCTM().inverse());
            const ponto = pontoMaisProximo(pontoSvg.x);

            crosshair.setAttribute("x1", ponto.x);
            crosshair.setAttribute("x2", ponto.x);
            crosshair.style.opacity = "1";

            tooltip.replaceChildren();
            montarTooltip(tooltip, ponto);

            const retSvg = svg.getBoundingClientRect();
            const escalaX = retSvg.width / vb.width;
            tooltip.style.left = `${retSvg.left + (ponto.x - vb.x) * escalaX}px`;
            tooltip.style.top = `${Math.max(retSvg.top - 6, 4)}px`;
            tooltip.style.opacity = "1";
        }

        function esconder() {
            crosshair.style.opacity = "0";
            tooltip.style.opacity = "0";
        }

        overlay.addEventListener("pointermove", mover);
        overlay.addEventListener("pointerleave", esconder);
    }

    // Gráfico de histórico de cotações: data, preço de fechamento, variação do dia.
    document.querySelectorAll("svg.grafico-cotacoes").forEach((svg) => {
        ativarCrosshair(svg, (tooltip, ponto) => {
            const linhaData = document.createElement("div");
            linhaData.style.color = "var(--text-dim)";
            linhaData.textContent = ponto.data_label;
            tooltip.appendChild(linhaData);

            const linhaPreco = document.createElement("div");
            linhaPreco.style.fontWeight = "700";
            linhaPreco.textContent = ponto.preco_label;
            tooltip.appendChild(linhaPreco);

            if (ponto.variacao_label) {
                const linhaVariacao = document.createElement("div");
                linhaVariacao.style.color = ponto.variacao_positiva ? "var(--neon-green)" : "var(--neon-red)";
                linhaVariacao.textContent = ponto.variacao_label;
                tooltip.appendChild(linhaVariacao);
            }
        });
    });

    // Gráfico comparativo (posições): valor de compra x valor atual no histórico, por ativo.
    document.querySelectorAll("svg.grafico-comparativo").forEach((svg) => {
        ativarCrosshair(svg, (tooltip, ponto) => {
            const linhaData = document.createElement("div");
            linhaData.style.color = "var(--text-dim)";
            linhaData.textContent = ponto.data_label;
            tooltip.appendChild(linhaData);

            const linhaCompra = document.createElement("div");
            linhaCompra.style.color = "var(--text-dim)";
            linhaCompra.textContent = `Compra: ${ponto.valor_compra_label}`;
            tooltip.appendChild(linhaCompra);

            const linhaAtual = document.createElement("div");
            linhaAtual.style.fontWeight = "700";
            linhaAtual.textContent = `Atual: ${ponto.valor_atual_label}`;
            tooltip.appendChild(linhaAtual);

            const linhaDiferenca = document.createElement("div");
            linhaDiferenca.style.color = ponto.ganho ? "var(--neon-green)" : "var(--neon-red)";
            linhaDiferenca.style.fontWeight = "700";
            linhaDiferenca.textContent = ponto.diferenca_label;
            tooltip.appendChild(linhaDiferenca);
        });
    });
});
