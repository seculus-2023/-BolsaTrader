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
    irrf_jcp_pct = models.DecimalField(
        "IRRF - juros sobre capital próprio (%)", max_digits=5, decimal_places=2, default=Decimal("15"),
    )
    irrf_dividendos_pct = models.DecimalField(
        "IRRF - dividendos acima do limite (%)", max_digits=5, decimal_places=2, default=Decimal("10"),
    )
    limite_dividendos_mes = models.DecimalField(
        "Limite mensal de dividendos sem retenção, por empresa (R$)", max_digits=14, decimal_places=2,
        null=True, blank=True,
        help_text="Vazio = dividendos sem retenção (regra até 2025). Lei 15.270/2025: R$ 50 mil por mês da mesma "
        "empresa; acima disso, a retenção incide sobre o total pago no mês.",
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


class Darf(models.Model):
    """
    A guia (DARF 6015) de um mês de apuração. Nasce e é atualizada a partir
    da apuração mensal (tributario.darf.sincronizar_darfs) enquanto estiver
    em aberto - se uma operação antiga for corrigida, o valor da guia em
    aberto acompanha. Depois de paga, fica congelada: se a apuração mudar, a
    tela mostra a diferença (DARF complementar).
    """

    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="darfs")
    competencia = models.DateField("Mês de apuração", help_text="Sempre o dia 1 do mês.")
    codigo_receita = models.CharField("Código da receita", max_length=4, default="6015")
    valor_principal = models.DecimalField("Valor principal (R$)", max_digits=14, decimal_places=2)
    vencimento = models.DateField("Vencimento")
    data_pagamento = models.DateField("Data do pagamento", null=True, blank=True)
    valor_pago = models.DecimalField("Valor pago (R$)", max_digits=14, decimal_places=2, null=True, blank=True)
    multa_paga = models.DecimalField("Multa paga (R$)", max_digits=14, decimal_places=2, default=Decimal("0"))
    juros_pagos = models.DecimalField("Juros pagos (R$)", max_digits=14, decimal_places=2, default=Decimal("0"))
    lancamento = models.OneToOneField(
        "core.LancamentoContaCorrente", on_delete=models.SET_NULL, null=True, blank=True, related_name="darf",
        help_text="Débito na conta corrente gerado ao marcar a guia como paga.",
    )
    ultimo_lembrete = models.DateField("Último lembrete enviado", null=True, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "DARF"
        verbose_name_plural = "DARFs"
        ordering = ["-competencia"]
        constraints = [
            models.UniqueConstraint(fields=["usuario", "competencia"], name="uma_darf_por_usuario_e_mes"),
        ]

    def __str__(self):
        return f"DARF {self.codigo_receita} {self.competencia:%m/%Y} - R$ {self.valor_principal}"

    @property
    def paga(self) -> bool:
        return self.data_pagamento is not None


class Provento(models.Model):
    """
    Dividendo, juros sobre capital próprio (JCP) ou rendimento de FII recebido
    pelo usuário - entra no relatório anual da declaração e, opcionalmente,
    como crédito na conta corrente (valor líquido).
    """

    DIVIDENDO = "DIVIDENDO"
    JCP = "JCP"
    RENDIMENTO_FII = "FII"
    TIPO_CHOICES = [
        (DIVIDENDO, "Dividendo"),
        (JCP, "Juros sobre capital próprio (JCP)"),
        (RENDIMENTO_FII, "Rendimento de FII"),
    ]

    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="proventos")
    ativo = models.ForeignKey("core.Ativo", on_delete=models.PROTECT, related_name="proventos")
    tipo = models.CharField("Tipo", max_length=9, choices=TIPO_CHOICES)
    data_pagamento = models.DateField("Data do pagamento")
    valor_bruto = models.DecimalField("Valor bruto (R$)", max_digits=14, decimal_places=2)
    ir_retido = models.DecimalField(
        "IR retido na fonte (R$)", max_digits=14, decimal_places=2, default=Decimal("0"),
        help_text="Está no informe da corretora. JCP: 15%. Dividendos: 10% quando a mesma empresa paga mais de "
        "R$ 50 mil no mês (Lei 15.270/2025). Rendimento de FII: isento.",
    )
    observacao = models.CharField("Observação", max_length=255, blank=True)
    lancamento = models.OneToOneField(
        "core.LancamentoContaCorrente", on_delete=models.SET_NULL, null=True, blank=True, related_name="provento",
    )
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Provento"
        verbose_name_plural = "Proventos"
        ordering = ["-data_pagamento", "-criado_em"]

    def __str__(self):
        return f"{self.get_tipo_display()} {self.ativo.ticker} {self.data_pagamento:%d/%m/%Y} - R$ {self.valor_bruto}"

    @property
    def valor_liquido(self) -> Decimal:
        return self.valor_bruto - (self.ir_retido or Decimal("0"))


class EventoCorporativo(models.Model):
    """
    Desdobramento, grupamento ou bonificação de um ativo que o usuário tem em
    carteira. Ao cadastrar (tributario.eventos.aplicar_evento), o saldo de
    cada lote anterior à data passa para um lote NOVO, já na quantidade e no
    preço ajustados (para metas, preço alvo e vendas futuras), e o lote
    original fica só com o que já tinha sido vendido. O cálculo fiscal ignora
    esses lotes novos e aplica o evento direto no preço médio, na data certa.
    """

    DESDOBRAMENTO = "DESDOBRAMENTO"
    GRUPAMENTO = "GRUPAMENTO"
    BONIFICACAO = "BONIFICACAO"
    TIPO_CHOICES = [
        (DESDOBRAMENTO, "Desdobramento (split)"),
        (GRUPAMENTO, "Grupamento (inplit)"),
        (BONIFICACAO, "Bonificação em ações"),
    ]

    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="eventos_corporativos")
    ativo = models.ForeignKey("core.Ativo", on_delete=models.PROTECT, related_name="eventos_corporativos")
    tipo = models.CharField("Tipo", max_length=13, choices=TIPO_CHOICES)
    data = models.DateField(
        "Data ex", help_text="Primeiro dia em que o papel é negociado já ajustado - vale para as ações compradas antes dela.",
    )
    proporcao = models.DecimalField(
        "Proporção", max_digits=12, decimal_places=6,
        help_text="Desdobramento: ações novas para cada 1 (ex: 1 para 2 = 2). Grupamento: quantas viram 1 (ex: 10 "
        "para 1 = 10). Bonificação: percentual (ex: 10% = 10).",
    )
    custo_unitario_bonificacao = models.DecimalField(
        "Custo unitário da bonificação (R$)", max_digits=12, decimal_places=6, null=True, blank=True,
        help_text="Só bonificação: valor por ação atribuído pela empresa (está no fato relevante/aviso aos acionistas).",
    )
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Evento corporativo"
        verbose_name_plural = "Eventos corporativos"
        ordering = ["-data", "-criado_em"]

    def __str__(self):
        return f"{self.get_tipo_display()} {self.ativo.ticker} em {self.data:%d/%m/%Y}"

    def nova_quantidade(self, quantidade: int) -> int:
        """Quantidade depois do evento (frações são descartadas - a empresa as vende em leilão)."""
        if self.tipo == self.DESDOBRAMENTO:
            return int(quantidade * self.proporcao)
        if self.tipo == self.GRUPAMENTO:
            return int(quantidade / self.proporcao)
        return quantidade + int(quantidade * self.proporcao / 100)


class AvisoLimiteIsencao(models.Model):
    """Registro de aviso já enviado pelo Telegram sobre o limite de isenção de um mês - um de cada tipo por mês."""

    PERTO = "PERTO"
    EXCEDIDO = "EXCEDIDO"
    TIPO_CHOICES = [(PERTO, "Perto do limite"), (EXCEDIDO, "Limite ultrapassado")]

    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="avisos_limite_isencao"
    )
    competencia = models.DateField("Mês", help_text="Sempre o dia 1 do mês.")
    tipo = models.CharField("Tipo", max_length=8, choices=TIPO_CHOICES)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Aviso de limite de isenção"
        verbose_name_plural = "Avisos de limite de isenção"
        constraints = [
            models.UniqueConstraint(fields=["usuario", "competencia", "tipo"], name="um_aviso_por_usuario_mes_tipo"),
        ]

    def __str__(self):
        return f"{self.get_tipo_display()} - {self.competencia:%m/%Y} - {self.usuario}"


class TaxaSelicMensal(models.Model):
    """Selic acumulada de cada mês (série 4390 do Banco Central) - base dos juros de DARF paga em atraso."""

    mes = models.DateField("Mês", unique=True, help_text="Sempre o dia 1 do mês.")
    taxa_pct = models.DecimalField("Taxa no mês (%)", max_digits=7, decimal_places=4)

    class Meta:
        verbose_name = "Taxa Selic mensal"
        verbose_name_plural = "Taxas Selic mensais"
        ordering = ["-mes"]

    def __str__(self):
        return f"Selic {self.mes:%m/%Y}: {self.taxa_pct}%"


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


class NotaCorretagem(models.Model):
    """
    Nota de negociação (corretagem) importada do PDF da corretora - guarda os
    totais da nota e as compras (Operacao) e vendas (VendaLote) que a
    importação criou, para a apuração poder separar o que veio das notas do
    que foi lançado à mão e para a importação poder ser desfeita.
    """

    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notas_corretagem"
    )
    corretora = models.CharField("Corretora", max_length=100)
    numero = models.CharField("Número da nota", max_length=30)
    data_pregao = models.DateField("Data do pregão")
    arquivo = models.CharField("Arquivo", max_length=255, blank=True)
    valor_compras = models.DecimalField("Compras (R$)", max_digits=14, decimal_places=2, default=Decimal("0"))
    valor_vendas = models.DecimalField("Vendas (R$)", max_digits=14, decimal_places=2, default=Decimal("0"))
    custos = models.DecimalField(
        "Custos (R$)", max_digits=12, decimal_places=2, default=Decimal("0"),
        help_text="Taxas de liquidação e registro, emolumentos e custos operacionais da nota.",
    )
    irrf = models.DecimalField("IRRF retido (R$)", max_digits=12, decimal_places=2, default=Decimal("0"))
    liquido = models.DecimalField(
        "Líquido da nota (R$)", max_digits=14, decimal_places=2, default=Decimal("0"),
        help_text="Positivo quando a nota é a crédito, negativo quando é a débito.",
    )
    operacoes = models.ManyToManyField("core.Operacao", blank=True, related_name="notas_corretagem")
    vendas = models.ManyToManyField("core.VendaLote", blank=True, related_name="notas_corretagem")
    importada_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Nota de corretagem"
        verbose_name_plural = "Notas de corretagem"
        ordering = ["-data_pregao", "-numero"]
        constraints = [
            models.UniqueConstraint(fields=["usuario", "corretora", "numero"], name="nota_corretagem_unica"),
        ]

    def __str__(self):
        return f"Nota {self.numero} ({self.corretora}) de {self.data_pregao:%d/%m/%Y}"


class MapeamentoTitulo(models.Model):
    """
    Ticker de cada "especificação do título" das notas (ex: "MAGAZ LUIZA ON"
    -> MGLU3). A nota traz o nome de pregão, não o código - o que o usuário
    confirma na importação fica salvo aqui e vira a sugestão das próximas.
    """

    especificacao = models.CharField("Especificação na nota", max_length=120, unique=True)
    ticker = models.CharField("Ticker", max_length=12)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Mapeamento de título da nota"
        verbose_name_plural = "Mapeamentos de títulos das notas"
        ordering = ["especificacao"]

    def __str__(self):
        return f"{self.especificacao} -> {self.ticker}"
