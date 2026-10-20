"""
Eventos corporativos (desdobramento, grupamento e bonificação) nos lotes do
usuário. O efeito no preço médio fiscal é aplicado à parte, em
tributario.services.calcular_apuracao - aqui só se ajustam os lotes da tela
de operações (visão gerencial), para que quantidade, preço, metas e vendas
futuras já fiquem na unidade nova.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction

from core.models import Operacao

from .models import EventoCorporativo


class EventoInvalido(Exception):
    """O evento não pode ser aplicado/desfeito (a mensagem explica o motivo para o usuário)."""


def _lotes_afetados(evento: EventoCorporativo):
    return Operacao.objects.select_for_update().filter(
        usuario=evento.usuario, ativo=evento.ativo, tipo=Operacao.COMPRA, data_operacao__lt=evento.data,
    )


@transaction.atomic
def aplicar_evento(evento: EventoCorporativo) -> list[Operacao]:
    """
    Desdobramento/grupamento: o saldo de cada lote comprado antes da data vai
    para um lote novo, com a quantidade ajustada e o mesmo custo total; o
    lote antigo fica só com o que já tinha sido vendido. Bonificação: as ações
    recebidas viram um lote novo, ao custo informado pela empresa. Devolve os
    lotes criados.
    """
    lotes = list(_lotes_afetados(evento))
    for lote in lotes:
        if any(venda.data >= evento.data for venda in lote.vendas.all()):
            raise EventoInvalido(
                f"O lote de {lote.data_operacao:%d/%m/%Y} tem venda registrada a partir de {evento.data:%d/%m/%Y} - "
                "cadastre o evento antes de registrar vendas posteriores à data ex."
            )

    criados = []
    descricao = f"{evento.get_tipo_display()} de {evento.data:%d/%m/%Y}"
    if evento.tipo == EventoCorporativo.BONIFICACAO:
        saldo_total = sum(lote.saldo for lote in lotes)
        bonus = evento.nova_quantidade(saldo_total) - saldo_total
        if bonus <= 0:
            raise EventoInvalido("Com o saldo em carteira antes da data, a bonificação não gera nenhuma ação inteira.")
        criados.append(Operacao.objects.create(
            usuario=evento.usuario, ativo=evento.ativo, tipo=Operacao.COMPRA, quantidade=bonus,
            preco_unitario=(evento.custo_unitario_bonificacao or Decimal("0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            data_operacao=evento.data, evento=evento,
            observacao=f"{descricao}: {bonus} ação(ões) recebida(s) sobre {saldo_total} em carteira.",
        ))
        return criados

    for lote in lotes:
        saldo = lote.saldo
        if saldo <= 0:
            continue
        nova_quantidade = evento.nova_quantidade(saldo)
        if nova_quantidade <= 0:
            raise EventoInvalido(
                f"O lote de {lote.data_operacao:%d/%m/%Y} tem só {saldo} ação(ões) - no grupamento viraria menos de 1."
            )
        custo_saldo = lote.preco_unitario * saldo
        criados.append(Operacao.objects.create(
            usuario=evento.usuario, ativo=evento.ativo, tipo=Operacao.COMPRA, quantidade=nova_quantidade,
            preco_unitario=(custo_saldo / nova_quantidade).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            data_operacao=lote.data_operacao, meta_lucro_pct=lote.meta_lucro_pct, meta_perda_pct=lote.meta_perda_pct,
            evento=evento, lote_anterior=lote,
            observacao=f"{descricao}: {saldo} → {nova_quantidade} (lote de {lote.data_operacao:%d/%m/%Y}).",
        ))
        lote.quantidade_original = lote.quantidade_comprada
        lote.quantidade = lote.quantidade_vendida
        lote.save(update_fields=["quantidade_original", "quantidade"])
    if not criados:
        raise EventoInvalido(f"Não há saldo de {evento.ativo.ticker} comprado antes de {evento.data:%d/%m/%Y}.")
    return criados


@transaction.atomic
def desfazer_evento(evento: EventoCorporativo) -> None:
    """Apaga os lotes gerados, devolve o saldo aos lotes de origem e exclui o evento."""
    gerados = list(evento.lotes_gerados.select_for_update())
    for lote in gerados:
        if lote.vendas.exists() or lote.quantidade_vendida:
            raise EventoInvalido(
                f"O lote gerado em {lote.data_operacao:%d/%m/%Y} já tem venda - exclua a venda antes de desfazer o evento."
            )
        if lote.quantidade_original is not None or lote.lotes_posteriores.exists():
            raise EventoInvalido("Um evento posterior já ajustou estes lotes - desfaça o mais recente primeiro.")

    for lote in gerados:
        origem = lote.lote_anterior
        lote.delete()
        if origem is not None and origem.quantidade_original is not None:
            origem.quantidade = origem.quantidade_original
            origem.quantidade_original = None
            origem.save(update_fields=["quantidade", "quantidade_original"])
    evento.delete()
