"""
Proventos (dividendos, JCP e rendimentos de FII): IR retido sugerido pela
regra vigente e crédito do valor líquido na conta corrente.

Separado de tributario.services porque usa core.services (conta corrente).
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Sum

from core.models import LancamentoContaCorrente
from core.services import obter_ou_criar_conta_corrente

from .models import Provento
from .services import parametro_vigente


def raiz_empresa(ticker: str) -> str:
    """As 4 letras do ticker identificam a empresa (PETR3 e PETR4 são a mesma pagadora)."""
    return (ticker or "")[:4].upper()


def ir_retido_sugerido(usuario, ativo, tipo: str, data_pagamento, valor_bruto: Decimal, excluir_id=None) -> Decimal:
    """
    IR que a fonte pagadora deveria ter retido: JCP pela alíquota de JCP;
    dividendo pela alíquota de dividendos quando a mesma empresa pagou ao
    usuário mais que o limite no mês (contando este pagamento); FII, isento.
    """
    parametro = parametro_vigente(data_pagamento)
    if tipo == Provento.JCP:
        return (valor_bruto * parametro.irrf_jcp_pct / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if tipo != Provento.DIVIDENDO or parametro.limite_dividendos_mes is None:
        return Decimal("0")

    do_mes = Provento.objects.filter(
        usuario=usuario, tipo=Provento.DIVIDENDO, ativo__ticker__startswith=raiz_empresa(ativo.ticker),
        data_pagamento__year=data_pagamento.year, data_pagamento__month=data_pagamento.month,
    )
    if excluir_id:
        do_mes = do_mes.exclude(id=excluir_id)
    total_mes = (do_mes.aggregate(total=Sum("valor_bruto"))["total"] or Decimal("0")) + valor_bruto
    if total_mes <= parametro.limite_dividendos_mes:
        return Decimal("0")
    return (valor_bruto * parametro.irrf_dividendos_pct / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@transaction.atomic
def sincronizar_lancamento_provento(provento: Provento, lancar: bool) -> None:
    """Cria/atualiza (ou remove, se lancar=False) o crédito do valor líquido na conta corrente."""
    if not lancar:
        if provento.lancamento_id:
            lancamento = provento.lancamento
            provento.lancamento = None
            provento.save(update_fields=["lancamento"])
            lancamento.delete()
        return

    dados = {
        "tipo": LancamentoContaCorrente.CREDITO,
        "origem": LancamentoContaCorrente.ORIGEM_PROVENTO,
        "valor": provento.valor_liquido,
        "descricao": f"{provento.get_tipo_display()} {provento.ativo.ticker}",
        "data": provento.data_pagamento,
    }
    if provento.lancamento_id:
        LancamentoContaCorrente.objects.filter(id=provento.lancamento_id).update(**dados)
    else:
        provento.lancamento = LancamentoContaCorrente.objects.create(
            conta=obter_ou_criar_conta_corrente(provento.usuario), **dados
        )
        provento.save(update_fields=["lancamento"])


@transaction.atomic
def excluir_provento(provento: Provento) -> None:
    lancamento_id = provento.lancamento_id
    provento.delete()
    if lancamento_id:
        LancamentoContaCorrente.objects.filter(id=lancamento_id).delete()
