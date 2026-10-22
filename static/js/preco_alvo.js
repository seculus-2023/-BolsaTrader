// Mantém "Preço alvo (R$)" e "Meta de lucro (%)" sincronizados nos
// formulários de compra: mudar o preço ou a meta recalcula o alvo; editar o
// alvo refaz a meta de lucro. Meta vazia usa a meta padrão do sistema.
(function () {
    var campoPreco = document.getElementById("id_preco_unitario");
    var campoMeta = document.getElementById("id_meta_lucro_pct");
    var campoAlvo = document.getElementById("id_preco_alvo");
    if (!campoPreco || !campoMeta || !campoAlvo) return;

    var metaPadrao = parseFloat(campoAlvo.dataset.metaPadrao || "0");

    function numero(campo) {
        var valor = parseFloat(String(campo.value).replace(",", "."));
        return isNaN(valor) ? null : valor;
    }

    function recalcularAlvo() {
        var preco = numero(campoPreco);
        if (!preco) return;
        var meta = numero(campoMeta);
        meta = Math.abs(meta === null ? metaPadrao : meta);
        campoAlvo.value = (preco * (1 + meta / 100)).toFixed(2);
    }

    function recalcularMeta() {
        var preco = numero(campoPreco);
        var alvo = numero(campoAlvo);
        if (!preco || !alvo) return;
        campoMeta.value = ((alvo / preco - 1) * 100).toFixed(2);
    }

    campoPreco.addEventListener("input", recalcularAlvo);
    campoMeta.addEventListener("input", recalcularAlvo);
    campoAlvo.addEventListener("input", recalcularMeta);
    // "Usar no formulário" da calculadora de risco preenche a meta via código
    campoMeta.addEventListener("change", recalcularAlvo);

    if (!campoAlvo.value) recalcularAlvo();
})();
