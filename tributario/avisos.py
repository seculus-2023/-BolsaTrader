"""
Aviso de limite de isenção (R$ 20 mil em vendas de ações no mês): depois de
registrar ou corrigir uma venda, a tela mostra se o mês chegou perto do
limite ou passou dele, e o Telegram avisa uma vez por mês de cada situação.

Separado de tributario.services pelo mesmo motivo de tributario.darf: usa
core.services (Telegram).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from core.services import credenciais_telegram, enviar_notificacao_telegram

from .models import AvisoLimiteIsencao
from .services import ApuracaoMes, apurar_meses

PERCENTUAL_AVISO_ISENCAO = Decimal("80")


def situacao_limite(mes: ApuracaoMes | None) -> str | None:
    """PERTO (80% ou mais do limite), EXCEDIDO, ou None quando ainda há folga ou não houve venda de ações."""
    if mes is None or not mes.vendas_acoes:
        return None
    if not mes.isento:
        return AvisoLimiteIsencao.EXCEDIDO
    if mes.vendas_acoes >= mes.limite_isencao * PERCENTUAL_AVISO_ISENCAO / 100:
        return AvisoLimiteIsencao.PERTO
    return None


def mensagem_limite(mes: ApuracaoMes, situacao: str) -> str:
    if situacao == AvisoLimiteIsencao.EXCEDIDO:
        return (
            f"As vendas de ações de {mes.competencia} somam R$ {mes.vendas_acoes}, acima do limite de "
            f"R$ {mes.limite_isencao}: o ganho com ações em operação comum do mês inteiro passa a ser tributado "
            f"(IR estimado do mês: R$ {mes.imposto_bruto}, DARF até {mes.vencimento:%d/%m/%Y})."
        )
    return (
        f"As vendas de ações de {mes.competencia} já somam R$ {mes.vendas_acoes} - restam R$ "
        f"{mes.disponivel_isencao} para o limite de isenção de R$ {mes.limite_isencao}. "
        "Passando dele, o ganho do mês inteiro é tributado."
    )


def verificar_limite_isencao(usuario, data: date) -> tuple[str | None, str]:
    """
    Situação do limite no mês de `data` e o texto para mostrar na tela. Se o
    usuário tem Telegram, avisa por lá também - uma vez por mês e situação.
    """
    mes = next((m for m in apurar_meses(usuario) if (m.ano, m.mes) == (data.year, data.month)), None)
    situacao = situacao_limite(mes)
    if situacao is None:
        return None, ""
    texto = mensagem_limite(mes, situacao)

    if credenciais_telegram(usuario) is not None:
        competencia = date(mes.ano, mes.mes, 1)
        _, criado = AvisoLimiteIsencao.objects.get_or_create(usuario=usuario, competencia=competencia, tipo=situacao)
        if criado and not enviar_notificacao_telegram(usuario, f"⚠️ Isenção de IR - {usuario.get_username()}\n{texto}"):
            # não conseguiu enviar: apaga o registro para tentar de novo na próxima venda
            AvisoLimiteIsencao.objects.filter(usuario=usuario, competencia=competencia, tipo=situacao).delete()
    return situacao, texto
