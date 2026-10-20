from decimal import Decimal

from django.conf import settings
from django.db import models


class ParametroTributario(models.Model):
    """
    Alíquotas e limites do imposto de renda sobre renda variável, com período
    de vigência - quando a lei mudar, cadastra-se um novo registro a partir da
    data nova (e fecha-se o anterior), sem mexer no código e sem alterar a
    apuração dos meses passados. O mês é apurado com o registro vigente no
    primeiro dia dele.
    """

    vigencia_inicio = models.DateField("Vigente a partir de")
    vigencia_fim = models.DateField("Vigente até", null=True, blank=True, help_text="Vazio = em vigor.")
    aliquota_comum_pct = models.DecimalField(
        "Alíquota - operação comum (%)", max_digits=5, decimal_places=2, default=Decimal("15"),
        help_text="Ações, ETFs, BDRs e units fora do day trade.",
    )
    aliquota_day_trade_pct = models.DecimalField(
        "Alíquota - day trade (%)", max_digits=5, decimal_places=2, default=Decimal("20"),
    )
    aliquota_fii_pct = models.DecimalField(
        "Alíquota - fundos imobiliários (%)", max_digits=5, decimal_places=2, default=Decimal("20"),
        help_text="Ganho na venda de cotas de FII, comum ou day trade.",
    )
    limite_isencao_acoes = models.DecimalField(
        "Limite de isenção - vendas de ações no mês (R$)", max_digits=12, decimal_places=2, default=Decimal("20000"),
        help_text="Soma das vendas de ações no mês (inclusive day trade). Até esse valor, o ganho em operação "
        "comum com ações é isento.",
    )
    units_isentas = models.BooleanField(
        "Units entram na isenção", default=False,
        help_text="Desmarcado = vendas de units não somam no limite e o ganho com elas é tributado (entendimento "
        "mais conservador).",
    )
    irrf_comum_pct = models.DecimalField(
        "IRRF - operação comum (% sobre o valor vendido)", max_digits=7, decimal_places=4, default=Decimal("0.005"),
    )
    irrf_day_trade_pct = models.DecimalField(
        "IRRF - day trade (% sobre o ganho)", max_digits=7, decimal_places=4, default=Decimal("1"),
    )
    darf_minimo = models.DecimalField(
        "Valor mínimo de DARF (R$)", max_digits=8, decimal_places=2, default=Decimal("10"),
        help_text="Imposto abaixo disso não é pago no mês - acumula para os meses seguintes.",
    )
    observacao = models.CharField("Base legal / observação", max_length=255, blank=True)

    class Meta:
        verbose_name = "Parâmetro tributário"
        verbose_name_plural = "Parâmetros tributários"
        ordering = ["-vigencia_inicio"]

    def __str__(self):
        fim = f" até {self.vigencia_fim:%d/%m/%Y}" if self.vigencia_fim else ""
        return f"Vigente a partir de {self.vigencia_inicio:%d/%m/%Y}{fim}"


class SaldoInicialFiscal(models.Model):
    """
    Prejuízos de renda variável acumulados ANTES das operações cadastradas no
    sistema (ex: de anos anteriores, declarados na ficha Renda Variável) -
    entram na compensação desde o primeiro mês apurado. Um por usuário.
    """

    usuario = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saldo_inicial_fiscal"
    )
    prejuizo_comum = models.DecimalField(
        "Prejuízo a compensar - operação comum (R$)", max_digits=14, decimal_places=2, default=Decimal("0"),
    )
    prejuizo_day_trade = models.DecimalField(
        "Prejuízo a compensar - day trade (R$)", max_digits=14, decimal_places=2, default=Decimal("0"),
    )
    prejuizo_fii = models.DecimalField(
        "Prejuízo a compensar - fundos imobiliários (R$)", max_digits=14, decimal_places=2, default=Decimal("0"),
    )
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Saldo inicial fiscal"
        verbose_name_plural = "Saldos iniciais fiscais"

    def __str__(self):
        return f"Prejuízos anteriores de {self.usuario}"
