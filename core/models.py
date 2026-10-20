from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from .classificacao import CLASSE_CHOICES, inferir_classe_ativo


class Ativo(models.Model):
    """
    Uma ação negociada na bolsa (B3), identificada pelo ticker.

    Além do cadastro básico, guarda o retrato mais recente da cotação (preço
    do dia, volume, valor de mercado, faixa de 52 semanas etc.), obtido da
    API brapi.dev sempre que o usuário atualiza as cotações. Não é histórico
    - é só o "instantâneo" mais atual; o histórico diário fica em Cotacao.
    """

    ticker = models.CharField("Código (ticker)", max_length=12, unique=True)
    nome = models.CharField("Nome da empresa", max_length=150, blank=True)
    nome_longo = models.CharField("Nome completo", max_length=255, blank=True)
    moeda = models.CharField("Moeda", max_length=8, blank=True)
    logo_url = models.URLField("URL do logo", max_length=500, blank=True)
    setor = models.CharField(
        "Setor", max_length=150, blank=True,
        help_text="Preenchido automaticamente pela API de cotações ou pelo catálogo Ações da B3, "
        "quando disponível - usado para a análise de concentração por setor da carteira.",
    )
    classe = models.CharField(
        "Classe do ativo", max_length=4, choices=CLASSE_CHOICES, blank=True,
        help_text="Define a regra de imposto de renda (alíquota e isenção). Inferida pelo ticker ao "
        "cadastrar - corrija aqui quando a inferência errar (ex: um sufixo 11 que é unit, não FII).",
    )

    maxima_dia = models.DecimalField("Máxima do dia (R$)", max_digits=12, decimal_places=2, null=True, blank=True)
    minima_dia = models.DecimalField("Mínima do dia (R$)", max_digits=12, decimal_places=2, null=True, blank=True)
    abertura = models.DecimalField("Abertura (R$)", max_digits=12, decimal_places=2, null=True, blank=True)
    fechamento_anterior = models.DecimalField(
        "Fechamento anterior (R$)", max_digits=12, decimal_places=2, null=True, blank=True
    )
    variacao_dia_valor = models.DecimalField(
        "Variação no dia (R$)", max_digits=12, decimal_places=2, null=True, blank=True
    )
    volume = models.BigIntegerField("Volume negociado", null=True, blank=True)
    valor_mercado = models.DecimalField(
        "Valor de mercado (R$)", max_digits=20, decimal_places=2, null=True, blank=True
    )
    minima_52_semanas = models.DecimalField(
        "Mínima em 52 semanas (R$)", max_digits=12, decimal_places=2, null=True, blank=True
    )
    maxima_52_semanas = models.DecimalField(
        "Máxima em 52 semanas (R$)", max_digits=12, decimal_places=2, null=True, blank=True
    )

    hora_cotacao = models.DateTimeField("Horário da cotação (mercado)", null=True, blank=True)
    atualizado_em = models.DateTimeField("Última atualização", auto_now=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Ativo"
        verbose_name_plural = "Ativos"
        ordering = ["ticker"]

    def __str__(self):
        return self.ticker.upper()

    def save(self, *args, **kwargs):
        if not self.classe:
            self.classe = inferir_classe_ativo(self.ticker, self.nome)
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = {*kwargs["update_fields"], "classe"}
        super().save(*args, **kwargs)

    def ultima_cotacao(self):
        return self.cotacoes.order_by("-data").first()


class Operacao(models.Model):
    """
    Uma compra ou uma reserva de um ativo, feita por um usuário.

    Uma compra funciona como um "lote": cada venda (total ou parcial) desse
    lote é um VendaLote ligado a ele, e o lucro/perda do lote é uma simples
    comparação entre o preço de compra e os preços de venda dele mesmo, sem
    custo médio entre vários lotes - é a visão gerencial (metas, preço alvo).
    O imposto de renda usa o preço médio ponderado por ativo, calculado à
    parte em tributario.services a partir destes mesmos registros.
    """

    COMPRA = "COMPRA"
    VENDA = "VENDA"  # mantido só para compatibilidade de leitura de dados antigos
    RESERVAR = "RESERVAR"
    TIPO_CHOICES = [
        (COMPRA, "Compra"),
        (RESERVAR, "Reservar"),
    ]

    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="operacoes"
    )
    ativo = models.ForeignKey(Ativo, on_delete=models.PROTECT, related_name="operacoes")
    tipo = models.CharField(max_length=8, choices=TIPO_CHOICES)
    quantidade = models.PositiveIntegerField("Quantidade")
    preco_unitario = models.DecimalField("Preço unitário (R$)", max_digits=12, decimal_places=2)
    data_operacao = models.DateField("Data da operação", default=timezone.now)
    taxas_compra = models.DecimalField(
        "Taxas da compra (R$)", max_digits=12, decimal_places=2, default=Decimal("0"),
        help_text="Corretagem, emolumentos e taxa de liquidação da B3 e ISS pagos nesta compra "
        "(estão na nota de corretagem). Entram no custo de aquisição para o imposto de renda.",
    )

    # metas individuais (percentual) para gerar avisos de lucro/perda
    meta_lucro_pct = models.DecimalField(
        "Meta de lucro (%)", max_digits=6, decimal_places=2, null=True, blank=True
    )
    meta_perda_pct = models.DecimalField(
        "Meta de perda (%) - use valor negativo", max_digits=6, decimal_places=2, null=True, blank=True
    )

    # resumo das vendas deste lote - cada venda de verdade fica em VendaLote
    # (um lote pode ser vendido aos poucos, em datas diferentes) e estes
    # campos são só um espelho mantido por core.services.recalcular_resumo_vendas:
    # quantidade vendida somada, preço médio de venda, data da última venda e
    # percentual da corretora da última venda. Ficam vazios/zerados enquanto o
    # lote não foi vendido, e não se aplicam ao tipo Reservar.
    quantidade_vendida = models.PositiveIntegerField("Quantidade vendida", default=0)
    preco_venda = models.DecimalField("Preço de venda (R$)", max_digits=12, decimal_places=2, null=True, blank=True)
    data_venda = models.DateField("Data da venda", null=True, blank=True)
    percentual_corretora = models.DecimalField(
        "Percentual da corretora (%)", max_digits=5, decimal_places=2, null=True, blank=True,
        help_text="Taxa cobrada pela corretora sobre o total vendido - usada para calcular o valor líquido a receber.",
    )

    observacao = models.TextField("Observações", blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Operação"
        verbose_name_plural = "Operações"
        ordering = ["-data_operacao", "-criado_em"]

    def __str__(self):
        return f"{self.get_tipo_display()} {self.quantidade}x {self.ativo.ticker} @ R$ {self.preco_unitario}"

    def clean(self):
        super().clean()
        if self.tipo == self.RESERVAR and self.quantidade is not None and self.quantidade > 1:
            raise ValidationError({
                "quantidade": 'Para o tipo "Reservar", a quantidade não pode ser maior que 1.',
            })
        if (
            self.tipo == self.COMPRA
            and self.quantidade is not None
            and self.quantidade_vendida is not None
            and self.quantidade_vendida > self.quantidade
        ):
            raise ValidationError({
                "quantidade_vendida": "A quantidade vendida não pode ser maior que a quantidade comprada neste lote.",
            })

    @property
    def valor_total(self):
        return self.quantidade * self.preco_unitario

    def vendas_registradas(self):
        """
        Vendas deste lote, da mais antiga para a mais recente. Usa o
        prefetch_related("vendas") quando houver. Lotes sem nenhum VendaLote
        mas com os campos de resumo preenchidos (dados gravados direto nesses
        campos, ex: em testes) viram uma venda única, não salva, montada a
        partir do resumo - assim todo cálculo passa por aqui, sem dois caminhos.
        """
        if self.pk:
            vendas = sorted(self.vendas.all(), key=lambda v: (v.data, v.pk or 0))
            if vendas:
                return vendas
        if self.quantidade_vendida and self.preco_venda is not None:
            return [VendaLote(
                operacao=self, quantidade=self.quantidade_vendida, preco=self.preco_venda,
                data=self.data_venda or self.data_operacao, percentual_corretora=self.percentual_corretora,
            )]
        return []

    @property
    def valor_total_vendido(self):
        """Total bruto recebido nas vendas deste lote (soma de preço de venda x quantidade de cada venda)."""
        vendas = self.vendas_registradas()
        if not vendas:
            return None
        return sum((venda.valor_bruto for venda in vendas), Decimal("0"))

    @property
    def valor_liquido_vendido(self):
        """Valor líquido a receber pelas vendas deste lote, já descontados corretora e taxas de cada venda."""
        vendas = self.vendas_registradas()
        if not vendas:
            return None
        return sum((venda.valor_liquido for venda in vendas), Decimal("0"))

    @property
    def dias_desde_operacao(self):
        """Dias corridos desde a data da operação até hoje."""
        return (timezone.localdate() - self.data_operacao).days

    @property
    def dias_em_carteira_ate_venda(self):
        """
        Dias que o lote ficou em carteira até ser vendido (compra -> venda) -
        travado no dia da venda, ao contrário de dias_desde_operacao, que
        continua contando até hoje mesmo depois de vendido.
        """
        if self.data_venda is None:
            return None
        return (self.data_venda - self.data_operacao).days

    @property
    def saldo(self):
        """Quantidade deste lote que ainda não foi vendida."""
        return self.quantidade - self.quantidade_vendida

    @property
    def lucro_perda_realizado(self):
        """Lucro/perda em R$ da parte já vendida deste lote (compra x venda do próprio lote)."""
        total_vendido = self.valor_total_vendido
        if total_vendido is None:
            return None
        quantidade = sum(venda.quantidade for venda in self.vendas_registradas())
        return (total_vendido - self.preco_unitario * quantidade).quantize(Decimal("0.01"))

    @property
    def lucro_perda_pct_realizado(self):
        """Lucro/perda realizado, em percentual sobre o custo de compra da parte vendida."""
        lucro = self.lucro_perda_realizado
        if lucro is None:
            return None
        custo = self.preco_unitario * self.quantidade_vendida
        if not custo:
            return None
        return (lucro / custo * 100).quantize(Decimal("0.01"))

    @property
    def lucro_perda_por_dia(self):
        """
        Lucro/perda realizado (R$) dividido pelos dias que o lote ficou em
        carteira até a venda - mede a "velocidade" do resultado (R$/dia), não
        só o total. None quando vendido no mesmo dia da compra (0 dias, não
        dá pra ratear) ou quando ainda não há lucro/perda apurado.
        """
        lucro = self.lucro_perda_realizado
        dias = self.dias_em_carteira_ate_venda
        if lucro is None or not dias:
            return None
        return (lucro / dias).quantize(Decimal("0.01"))

    @property
    def lucro_perda_pct_por_dia(self):
        """Lucro/perda realizado (%) dividido pelos dias em carteira até a venda - mesma ideia de lucro_perda_por_dia, em percentual."""
        pct = self.lucro_perda_pct_realizado
        dias = self.dias_em_carteira_ate_venda
        if pct is None or not dias:
            return None
        return (pct / dias).quantize(Decimal("0.01"))

    @property
    def preco_atual(self):
        """Última cotação conhecida do ativo (preço de fechamento mais recente)."""
        cotacao = self.ativo.ultima_cotacao()
        return cotacao.preco_fechamento if cotacao else None

    @property
    def lucro_perda_pct_atual(self):
        """
        Lucro/perda percentual NÃO realizado do saldo deste lote ainda em
        carteira, comparando o preço de compra com a cotação mais recente do
        ativo (não considera a parte já vendida - essa usa lucro_perda_pct_realizado).
        """
        if self.tipo != self.COMPRA or self.saldo <= 0:
            return None
        preco_atual = self.preco_atual
        if preco_atual is None:
            return None
        return ((preco_atual - self.preco_unitario) / self.preco_unitario * 100).quantize(Decimal("0.01"))

    @property
    def meta_lucro_atingida(self):
        """
        True quando o saldo em carteira deste lote já atingiu a meta de lucro
        (a própria, ou a padrão do sistema quando o lote não tem uma definida)
        - usado para destacar na grid que é hora de considerar a venda.
        """
        pct_atual = self.lucro_perda_pct_atual
        if pct_atual is None:
            return False
        # a meta de lucro é sempre um ganho (valor positivo) - normaliza caso
        # tenha sido salva como negativa (dado antigo, de antes da validação)
        meta = abs(
            self.meta_lucro_pct
            if self.meta_lucro_pct is not None
            else Decimal(str(settings.META_LUCRO_PADRAO))
        )
        return pct_atual >= meta

    @property
    def preco_alvo(self):
        """
        Preço (R$) em que este lote atinge a meta de lucro: preço de compra
        acrescido da meta do lote (ou da padrão do sistema) - mesma regra de
        meta_lucro_atingida. Sugerido como preço na tela de venda. None para
        reservas.
        """
        if self.tipo != self.COMPRA or not self.preco_unitario:
            return None
        meta = abs(
            self.meta_lucro_pct
            if self.meta_lucro_pct is not None
            else Decimal(str(settings.META_LUCRO_PADRAO))
        )
        return (self.preco_unitario * (1 + meta / 100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @property
    def variacao_pct_reserva(self):
        """
        Para o tipo Reservar: variação percentual do preço atual em relação ao
        preço pretendido na reserva - usada para saber se a baixa desejada
        para comprar já foi atingida.
        """
        if self.tipo != self.RESERVAR:
            return None
        preco_atual = self.preco_atual
        if preco_atual is None:
            return None
        return ((preco_atual - self.preco_unitario) / self.preco_unitario * 100).quantize(Decimal("0.01"))

    @property
    def meta_compra_atingida(self):
        """
        True quando uma reserva já caiu até (ou além) a meta de baixa definida
        (a própria, ou a padrão do sistema quando não há uma definida) -
        usado para destacar na grid que é hora de considerar a compra.
        """
        variacao = self.variacao_pct_reserva
        if variacao is None:
            return False
        meta_bruta = (
            self.meta_perda_pct
            if self.meta_perda_pct is not None
            else Decimal(str(settings.META_PERDA_PADRAO))
        )
        # a meta de perda é sempre uma queda (valor negativo) - normaliza caso
        # tenha sido salva como positiva (dado antigo, de antes da validação)
        meta = -abs(meta_bruta)
        return variacao <= meta


class VendaLote(models.Model):
    """
    Uma venda (total ou parcial) de um lote de compra (Operacao). Um lote pode
    ter várias vendas, em datas e preços diferentes - o imposto de renda é
    apurado por mês, então cada venda precisa da própria data e do próprio
    valor. Os campos de resumo do lote (Operacao.quantidade_vendida etc.)
    são recalculados a partir daqui por core.services.recalcular_resumo_vendas.
    """

    operacao = models.ForeignKey(Operacao, on_delete=models.CASCADE, related_name="vendas")
    quantidade = models.PositiveIntegerField("Quantidade vendida")
    preco = models.DecimalField("Preço de venda (R$)", max_digits=12, decimal_places=2)
    data = models.DateField("Data da venda", default=timezone.localdate)
    percentual_corretora = models.DecimalField(
        "Percentual da corretora (%)", max_digits=5, decimal_places=2, null=True, blank=True,
        help_text="Taxa cobrada pela corretora sobre o total vendido - usada para calcular o valor líquido a receber.",
    )
    taxas = models.DecimalField(
        "Outras taxas da venda (R$)", max_digits=12, decimal_places=2, default=Decimal("0"),
        help_text="Emolumentos e taxa de liquidação da B3, corretagem fixa e ISS desta venda (estão na nota "
        "de corretagem). Descontados do valor líquido e do ganho para o imposto de renda.",
    )
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Venda de lote"
        verbose_name_plural = "Vendas de lotes"
        ordering = ["data", "criado_em"]

    def __str__(self):
        return f"Venda {self.quantidade}x {self.operacao.ativo.ticker} @ R$ {self.preco} em {self.data:%d/%m/%Y}"

    @property
    def valor_bruto(self):
        return self.preco * self.quantidade

    @property
    def custos(self):
        """Corretagem percentual mais as outras taxas - tudo que sai do valor bruto desta venda."""
        percentual = self.percentual_corretora or Decimal("0")
        return (self.valor_bruto * percentual / 100 + (self.taxas or Decimal("0"))).quantize(Decimal("0.01"))

    @property
    def valor_liquido(self):
        return (self.valor_bruto - self.custos).quantize(Decimal("0.01"))


class Cotacao(models.Model):
    """Histórico de cotações diárias de um ativo (usado para acompanhar lucro/perda e tendência)."""

    ativo = models.ForeignKey(Ativo, on_delete=models.CASCADE, related_name="cotacoes")
    data = models.DateField("Data da cotação")
    preco_fechamento = models.DecimalField("Preço de fechamento (R$)", max_digits=12, decimal_places=2)
    variacao_dia_pct = models.DecimalField(
        "Variação no dia (%)", max_digits=6, decimal_places=2, null=True, blank=True
    )
    volume = models.BigIntegerField(
        "Volume negociado", null=True, blank=True,
        help_text="Vem de graça no mesmo retorno da API que já busca o preço - nulo em registros salvos antes desse campo existir.",
    )
    maxima = models.DecimalField(
        "Máxima do dia (R$)", max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Vem de graça no mesmo retorno da API que já busca o preço - usada no ATR e no suporte/resistência do Scanner Técnico.",
    )
    minima = models.DecimalField(
        "Mínima do dia (R$)", max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Vem de graça no mesmo retorno da API que já busca o preço - usada no ATR e no suporte/resistência do Scanner Técnico.",
    )
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Cotação"
        verbose_name_plural = "Cotações"
        ordering = ["-data"]
        unique_together = ("ativo", "data")

    def __str__(self):
        return f"{self.ativo.ticker} {self.data} R$ {self.preco_fechamento}"


class Alerta(models.Model):
    """Aviso/lembrete gerado para o usuário (meta de lucro/perda atingida, tendência, etc.)."""

    LUCRO = "LUCRO"
    PERDA = "PERDA"
    TENDENCIA = "TENDENCIA"
    SINAL_COMPRA = "SINAL_COMPRA"
    SINAL_VENDA = "SINAL_VENDA"
    # Scanner Técnico com predomínio de alta e cotação na entrada (ou abaixo)
    # do plano de trade - ver core.services.notificar_compras_scanner_telegram
    SCANNER_COMPRA = "SCAN_COMPRA"
    # cotação caiu até o preço de uma reserva (Operacao RESERVAR) - ver
    # core.services.notificar_reservas_atingidas_telegram
    RESERVA_ATINGIDA = "RESERVA_OK"
    # marca que a Análise/Sugestões diárias da IA já foram enviadas hoje pelo
    # Telegram - ver core.services.enviar_ia_diaria_telegram
    IA_DIARIA = "IA_DIARIA"
    LEMBRETE = "LEMBRETE"
    TIPO_CHOICES = [
        (LUCRO, "Meta de lucro atingida"),
        (PERDA, "Meta de perda atingida"),
        (TENDENCIA, "Sinal de tendência de mercado"),
        (SINAL_COMPRA, "Robô: sinal de compra"),
        (SINAL_VENDA, "Robô: sinal de venda"),
        (SCANNER_COMPRA, "Scanner: cotação na entrada (compra)"),
        (RESERVA_ATINGIDA, "Reserva: cotação no preço reservado"),
        (IA_DIARIA, "IA do dia enviada ao Telegram"),
        (LEMBRETE, "Lembrete geral"),
    ]

    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="alertas"
    )
    ativo = models.ForeignKey(Ativo, on_delete=models.CASCADE, related_name="alertas", null=True, blank=True)
    tipo = models.CharField(max_length=12, choices=TIPO_CHOICES)
    mensagem = models.CharField(max_length=255)
    percentual = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    lido = models.BooleanField(default=False)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Alerta"
        verbose_name_plural = "Alertas"
        ordering = ["-criado_em"]

    def __str__(self):
        return f"[{self.get_tipo_display()}] {self.mensagem}"


class MensagemWhatsapp(models.Model):
    """
    Mensagem recebida via WhatsApp (webhook da Meta Cloud API) ou lançada
    manualmente pelo administrador. Conteúdo compartilhado (avisos, análises
    etc.), visível para todos os usuários logados - não é uma operação
    pessoal de compra/venda.
    """

    remetente = models.CharField("Remetente (nome ou número)", max_length=150)
    texto = models.TextField("Mensagem")
    recebido_em = models.DateTimeField("Recebido em", auto_now_add=True)

    class Meta:
        verbose_name = "Mensagem do WhatsApp"
        verbose_name_plural = "Mensagens do WhatsApp"
        ordering = ["-recebido_em"]

    def __str__(self):
        return f"{self.remetente}: {self.texto[:40]}"


class FonteNoticia(models.Model):
    """
    Um site de notícias/análises de mercado configurado para ser lido
    periodicamente (ex: TradingView, InfoMoney) - a lista é editável pelo
    usuário, não fixa no código. Compartilhada entre todos os usuários
    logados, como o mural de atividade e as mensagens do WhatsApp.
    """

    nome = models.CharField("Nome da fonte", max_length=100)
    url = models.URLField("URL da página a ser lida", max_length=500)
    ativa = models.BooleanField("Ativa", default=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Fonte de notícias"
        verbose_name_plural = "Fontes de notícias"
        ordering = ["nome"]

    def __str__(self):
        return self.nome


class Noticia(models.Model):
    """
    Uma manchete capturada de uma FonteNoticia - só título e link (a matéria
    completa fica no site de origem). Extraída por heurística genérica
    (títulos com link), não por um parser dedicado a cada site.
    """

    fonte = models.ForeignKey(FonteNoticia, on_delete=models.CASCADE, related_name="noticias")
    titulo = models.CharField("Título", max_length=500)
    url = models.URLField("Link da matéria", max_length=700, unique=True)
    capturada_em = models.DateTimeField("Capturada em", auto_now_add=True)

    class Meta:
        verbose_name = "Notícia"
        verbose_name_plural = "Notícias"
        ordering = ["-capturada_em"]

    def __str__(self):
        return self.titulo[:60]


class AcaoB3(models.Model):
    """
    Catálogo de referência com todas as ações negociadas na B3, sincronizado
    com a API brapi.dev pelo botão "Atualizar lista" na tela Ações da B3
    (ver core.services.sincronizar_acoes_b3) - a atualização apaga a lista
    inteira e cadastra de novo a partir do que a API retorna no momento.

    Independente do Ativo/Operacao: é só uma lista de consulta, não tem
    relação com o que algum usuário comprou, vendeu ou reservou.
    """

    ticker = models.CharField("Ticker", max_length=12, unique=True)
    nome = models.CharField("Nome", max_length=150, blank=True)
    setor = models.CharField("Setor", max_length=150, blank=True)
    logo_url = models.URLField("URL do logo", max_length=500, blank=True)
    preco_atual = models.DecimalField("Preço atual (R$)", max_digits=12, decimal_places=2, null=True, blank=True)
    variacao_dia_pct = models.DecimalField(
        "Variação do dia (%)", max_digits=6, decimal_places=2, null=True, blank=True
    )
    atualizado_em = models.DateTimeField("Atualizado em", auto_now=True)

    class Meta:
        verbose_name = "Ação da B3"
        verbose_name_plural = "Ações da B3"
        ordering = ["ticker"]

    def __str__(self):
        return self.ticker


class CotacaoIndice(models.Model):
    """
    Histórico diário de um índice/indicador de mercado usado como referência
    (benchmark) para comparar com o desempenho da carteira - ver
    core.services.calcular_comparativo_benchmark.

    IBOVESPA: valor é o número de pontos do índice no fechamento do dia (ex:
    134500.00), igual ao de uma cotação normal - o retorno do período é
    calculado comparando o valor do primeiro e do último dia.

    CDI: valor é a taxa diária (%) do dia, publicada pelo Banco Central (SGS
    série 12) - o retorno acumulado do período é obtido compondo (juros
    compostos) as taxas diárias, não subtraindo o primeiro do último valor.
    """

    IBOVESPA = "IBOVESPA"
    CDI = "CDI"
    INDICE_CHOICES = [(IBOVESPA, "Ibovespa"), (CDI, "CDI")]

    indice = models.CharField("Índice", max_length=10, choices=INDICE_CHOICES)
    data = models.DateField("Data")
    valor = models.DecimalField(
        "Valor", max_digits=14, decimal_places=6,
        help_text="Pontos do índice (Ibovespa) ou taxa diária em % (CDI).",
    )
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Cotação de índice"
        verbose_name_plural = "Cotações de índices"
        ordering = ["indice", "-data"]
        unique_together = ("indice", "data")

    def __str__(self):
        return f"{self.get_indice_display()} {self.data} = {self.valor}"


class RegistroAtualizacaoCarteira(models.Model):
    """
    "Retrato" (snapshot) dos totais da carteira comprada do usuário, gravado
    toda vez que as cotações são atualizadas - pelo botão manual "Atualizar
    cotações agora" ou pelo comando de management "atualizar_cotacoes"
    (inclusive em --loop). Forma o histórico usado na tela Histórico de
    Atualizações (ver core.services.registrar_atualizacao_carteira).
    """

    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="registros_atualizacao_carteira",
    )
    criado_em = models.DateTimeField("Data/hora", auto_now_add=True)
    total_ativos = models.PositiveIntegerField("Total de ativos")
    valor_investido = models.DecimalField("Valor investido", max_digits=14, decimal_places=2)
    valor_atual = models.DecimalField("Valor atual", max_digits=14, decimal_places=2)
    lucro_perda = models.DecimalField("Lucro/Perda (R$)", max_digits=14, decimal_places=2)
    lucro_perda_pct = models.DecimalField(
        "Lucro/Perda (%)", max_digits=8, decimal_places=2, null=True, blank=True,
    )

    class Meta:
        verbose_name = "Registro de atualização da carteira"
        verbose_name_plural = "Registros de atualização da carteira"
        ordering = ["-criado_em"]

    def __str__(self):
        return f"{self.usuario} - {self.criado_em:%d/%m/%Y %H:%M}"


class RegistroCotacao(models.Model):
    """
    Registro (log) da cotação de um ativo comprado pelo usuário, gravado a
    cada atualização de cotações - pelo ciclo automático (agendador embutido
    / comando "atualizar_cotacoes"), pelo botão "Atualizar cotações agora"
    ou pela consulta avulsa em Detalhes de Cotações. Só entram ativos com
    saldo em carteira (ver core.services.registrar_cotacoes_compradas).
    Alimenta a grid de registros em Detalhes de Cotações e o gráfico em
    popup (compra x alvo) das grids de ativos comprados.
    """

    AUTOMATICA = "AUTOMATICA"
    MANUAL = "MANUAL"
    ORIGEM_CHOICES = [
        (AUTOMATICA, "Automática"),
        (MANUAL, "Manual"),
    ]

    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="registros_cotacao",
    )
    ativo = models.ForeignKey(Ativo, on_delete=models.CASCADE, related_name="registros_cotacao")
    data = models.DateField("Data", default=timezone.localdate)
    hora = models.TimeField("Hora")
    preco = models.DecimalField("Valor da cotação (R$)", max_digits=12, decimal_places=2)
    origem = models.CharField("Origem", max_length=10, choices=ORIGEM_CHOICES, default=AUTOMATICA)

    class Meta:
        verbose_name = "Registro de cotação"
        verbose_name_plural = "Registros de cotações"
        ordering = ["-data", "-hora", "-id"]
        indexes = [models.Index(fields=["usuario", "ativo", "data", "hora"])]

    def __str__(self):
        return f"{self.ativo.ticker} {self.data:%d/%m/%Y} {self.hora:%H:%M} R$ {self.preco}"


class ConsumoApiBrapi(models.Model):
    """
    Contador diário de requisições feitas à API brapi.dev - base do controle
    de orçamento mensal (ver core.services.consumo_api_ultimos_30_dias): o
    plano tem um limite de requisições por ciclo, e estourar deixa o sistema
    sem cotações até a renovação. Compartilhado entre todos os processos que
    usam o mesmo banco (servidor de desenvolvimento, produção, comandos).
    """

    data = models.DateField("Data", unique=True)
    requisicoes = models.PositiveIntegerField("Requisições", default=0)

    class Meta:
        verbose_name = "Consumo diário da API brapi.dev"
        verbose_name_plural = "Consumo diário da API brapi.dev"
        ordering = ["-data"]

    def __str__(self):
        return f"{self.data}: {self.requisicoes} requisição(ões)"


class ContaCorrente(models.Model):
    """
    Conta corrente do usuário para acompanhar o caixa das operações de bolsa:
    saldo inicial informado manualmente, débito automático a cada compra,
    crédito automático a cada venda (ver core.services.
    sincronizar_lancamento_compra/venda), e lançamentos manuais de
    transferência de/para outras contas (ex: Nubank). Uma por usuário.
    """

    usuario = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="conta_corrente"
    )
    saldo_inicial = models.DecimalField("Saldo inicial (R$)", max_digits=14, decimal_places=2, default=Decimal("0"))
    atualizado_em = models.DateTimeField(auto_now=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Conta corrente"
        verbose_name_plural = "Contas correntes"

    def __str__(self):
        return f"Conta corrente de {self.usuario}"


class LancamentoContaCorrente(models.Model):
    """
    Um lançamento (crédito ou débito) na conta corrente do usuário. Os
    lançamentos de COMPRA/VENDA nascem e são mantidos automaticamente em
    sincronia com a Operacao correspondente (1 lançamento por operação de
    compra, 1 por venda de lote) - ver core.services.
    sincronizar_lancamento_compra/venda. Excluir a Operacao exclui o
    lançamento junto (on_delete=CASCADE). Lançamentos de TRANSFERENCIA são
    cadastrados manualmente pelo usuário, sem Operacao associada.
    """

    CREDITO = "CREDITO"
    DEBITO = "DEBITO"
    TIPO_CHOICES = [(CREDITO, "Crédito"), (DEBITO, "Débito")]

    ORIGEM_COMPRA = "COMPRA"
    ORIGEM_VENDA = "VENDA"
    ORIGEM_COMPRA_CRIPTO = "COMPRA_CRIPTO"
    ORIGEM_VENDA_CRIPTO = "VENDA_CRIPTO"
    ORIGEM_TRANSFERENCIA = "TRANSFERENCIA"
    ORIGEM_IMPOSTO = "IMPOSTO"
    ORIGEM_CHOICES = [
        (ORIGEM_COMPRA, "Compra de ações"),
        (ORIGEM_VENDA, "Venda de ações"),
        (ORIGEM_COMPRA_CRIPTO, "Compra de criptomoeda"),
        (ORIGEM_VENDA_CRIPTO, "Venda de criptomoeda"),
        (ORIGEM_TRANSFERENCIA, "Transferência"),
        (ORIGEM_IMPOSTO, "Imposto (DARF)"),
    ]

    conta = models.ForeignKey(ContaCorrente, on_delete=models.CASCADE, related_name="lancamentos")
    tipo = models.CharField("Tipo", max_length=8, choices=TIPO_CHOICES)
    origem = models.CharField("Origem", max_length=15, choices=ORIGEM_CHOICES)
    valor = models.DecimalField("Valor (R$)", max_digits=14, decimal_places=2)
    descricao = models.CharField("Descrição", max_length=255)
    data = models.DateField("Data", default=timezone.localdate)
    operacao = models.ForeignKey(
        Operacao, on_delete=models.CASCADE, null=True, blank=True, related_name="lancamentos_conta_corrente",
        help_text="Preenchido só nos lançamentos automáticos de compra/venda de ações - nulo nos de cripto e nas transferências manuais.",
    )
    venda_lote = models.ForeignKey(
        VendaLote, on_delete=models.CASCADE, null=True, blank=True, related_name="lancamentos_conta_corrente",
        help_text="Preenchido só nos lançamentos de venda de ações - um por venda, já que um lote pode ser vendido aos poucos.",
    )
    operacao_cripto = models.ForeignKey(
        "OperacaoCripto", on_delete=models.CASCADE, null=True, blank=True,
        related_name="lancamentos_conta_corrente",
        help_text="Preenchido só nos lançamentos automáticos de compra/venda de criptomoeda - nulo nos de ações e nas transferências manuais.",
    )
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Lançamento da conta corrente"
        verbose_name_plural = "Lançamentos da conta corrente"
        ordering = ["-data", "-criado_em"]
        constraints = [
            models.UniqueConstraint(
                fields=["operacao", "origem"], condition=models.Q(operacao__isnull=False, venda_lote__isnull=True),
                name="um_lancamento_por_operacao_e_origem",
            ),
            models.UniqueConstraint(
                fields=["venda_lote"], condition=models.Q(venda_lote__isnull=False),
                name="um_lancamento_por_venda_lote",
            ),
            models.UniqueConstraint(
                fields=["operacao_cripto", "origem"], condition=models.Q(operacao_cripto__isnull=False),
                name="um_lancamento_por_operacao_cripto_e_origem",
            ),
        ]

    def __str__(self):
        sinal = "+" if self.tipo == self.CREDITO else "-"
        return f"{sinal}R$ {self.valor} - {self.descricao}"


class CriptoAtivo(models.Model):
    """
    Uma criptomoeda acompanhada, identificada pelo código (coin) usado pela
    API brapi.dev (ex: BTC, ETH, SOL). Espelha Ativo (mesma ideia de guardar
    o "retrato" mais atual da cotação, com o histórico diário em
    CotacaoCripto), mas é um modelo à parte de propósito: cripto não deve se
    misturar com ações da B3 em nenhuma tela, cálculo ou relatório (Posições
    em Carteira, Scanner Técnico, robô consultor, comparação com Ibovespa/CDI
    etc. continuam só sobre ações - cripto tem sua própria tela e suas
    próprias Operações/Posições, ver OperacaoCripto).
    """

    coin = models.CharField("Código (ex: BTC, ETH)", max_length=12, unique=True)
    nome = models.CharField("Nome", max_length=150, blank=True)
    moeda = models.CharField("Moeda", max_length=8, default="BRL")
    logo_url = models.URLField("URL do logo", max_length=500, blank=True)

    preco_atual = models.DecimalField("Preço atual", max_digits=18, decimal_places=8, null=True, blank=True)
    variacao_dia_pct = models.DecimalField(
        "Variação no dia (%)", max_digits=8, decimal_places=2, null=True, blank=True
    )
    variacao_dia_valor = models.DecimalField(
        "Variação no dia (valor)", max_digits=18, decimal_places=8, null=True, blank=True
    )
    maxima_dia = models.DecimalField("Máxima do dia", max_digits=18, decimal_places=8, null=True, blank=True)
    minima_dia = models.DecimalField("Mínima do dia", max_digits=18, decimal_places=8, null=True, blank=True)
    volume = models.DecimalField("Volume negociado (24h)", max_digits=24, decimal_places=2, null=True, blank=True)
    valor_mercado = models.DecimalField(
        "Valor de mercado", max_digits=24, decimal_places=2, null=True, blank=True
    )

    hora_cotacao = models.DateTimeField("Horário da cotação", null=True, blank=True)
    atualizado_em = models.DateTimeField("Última atualização", auto_now=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Criptomoeda"
        verbose_name_plural = "Criptomoedas"
        ordering = ["coin"]

    def __str__(self):
        return self.coin.upper()

    def ultima_cotacao(self):
        return self.cotacoes.order_by("-data").first()


class OperacaoCripto(models.Model):
    """
    Uma compra ou uma reserva de criptomoeda, feita por um usuário - espelha
    Operacao (mesma lógica de lote/venda parcial no próprio registro, ver a
    docstring de Operacao), mas em modelo separado e ligado a CriptoAtivo em
    vez de Ativo, de propósito (ver CriptoAtivo). A diferença estrutural mais
    importante é que quantidade e preço são fracionários (dá pra comprar
    0,0035 BTC), enquanto em Operacao (ações) a quantidade é sempre inteira.
    """

    COMPRA = "COMPRA"
    RESERVAR = "RESERVAR"
    TIPO_CHOICES = [
        (COMPRA, "Compra"),
        (RESERVAR, "Reservar"),
    ]

    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="operacoes_cripto"
    )
    cripto_ativo = models.ForeignKey(CriptoAtivo, on_delete=models.PROTECT, related_name="operacoes")
    tipo = models.CharField(max_length=8, choices=TIPO_CHOICES)
    quantidade = models.DecimalField("Quantidade", max_digits=20, decimal_places=8)
    preco_unitario = models.DecimalField("Preço unitário", max_digits=18, decimal_places=8)
    data_operacao = models.DateField("Data da operação", default=timezone.now)

    meta_lucro_pct = models.DecimalField(
        "Meta de lucro (%)", max_digits=6, decimal_places=2, null=True, blank=True
    )
    meta_perda_pct = models.DecimalField(
        "Meta de perda (%) - use valor negativo", max_digits=6, decimal_places=2, null=True, blank=True
    )

    quantidade_vendida = models.DecimalField("Quantidade vendida", max_digits=20, decimal_places=8, default=Decimal("0"))
    preco_venda = models.DecimalField("Preço de venda", max_digits=18, decimal_places=8, null=True, blank=True)
    data_venda = models.DateField("Data da venda", null=True, blank=True)

    observacao = models.TextField("Observações", blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Operação de criptomoeda"
        verbose_name_plural = "Operações de criptomoeda"
        ordering = ["-data_operacao", "-criado_em"]

    def __str__(self):
        return f"{self.get_tipo_display()} {self.quantidade}x {self.cripto_ativo.coin} @ R$ {self.preco_unitario}"

    def clean(self):
        super().clean()
        if self.tipo == self.RESERVAR and self.quantidade is not None and self.quantidade_vendida:
            raise ValidationError({
                "quantidade_vendida": 'Uma reserva não pode ter quantidade vendida.',
            })
        if (
            self.tipo == self.COMPRA
            and self.quantidade is not None
            and self.quantidade_vendida is not None
            and self.quantidade_vendida > self.quantidade
        ):
            raise ValidationError({
                "quantidade_vendida": "A quantidade vendida não pode ser maior que a quantidade comprada neste lote.",
            })

    @property
    def valor_total(self):
        return self.quantidade * self.preco_unitario

    @property
    def valor_total_vendido(self):
        if not self.quantidade_vendida or self.preco_venda is None:
            return None
        return self.preco_venda * self.quantidade_vendida

    @property
    def dias_desde_operacao(self):
        return (timezone.localdate() - self.data_operacao).days

    @property
    def dias_em_carteira_ate_venda(self):
        if self.data_venda is None:
            return None
        return (self.data_venda - self.data_operacao).days

    @property
    def saldo(self):
        return self.quantidade - self.quantidade_vendida

    @property
    def lucro_perda_realizado(self):
        if not self.quantidade_vendida or self.preco_venda is None:
            return None
        return ((self.preco_venda - self.preco_unitario) * self.quantidade_vendida).quantize(Decimal("0.00000001"))

    @property
    def lucro_perda_pct_realizado(self):
        lucro = self.lucro_perda_realizado
        if lucro is None:
            return None
        custo = self.preco_unitario * self.quantidade_vendida
        if not custo:
            return None
        return (lucro / custo * 100).quantize(Decimal("0.01"))

    @property
    def preco_atual(self):
        """Última cotação conhecida da criptomoeda (preço de fechamento mais recente)."""
        cotacao = self.cripto_ativo.ultima_cotacao()
        return cotacao.preco_fechamento if cotacao else None

    @property
    def lucro_perda_pct_atual(self):
        if self.tipo != self.COMPRA or self.saldo <= 0:
            return None
        preco_atual = self.preco_atual
        if preco_atual is None:
            return None
        return ((preco_atual - self.preco_unitario) / self.preco_unitario * 100).quantize(Decimal("0.01"))

    @property
    def meta_lucro_atingida(self):
        pct_atual = self.lucro_perda_pct_atual
        if pct_atual is None:
            return False
        meta = abs(
            self.meta_lucro_pct
            if self.meta_lucro_pct is not None
            else Decimal(str(settings.META_LUCRO_PADRAO))
        )
        return pct_atual >= meta

    @property
    def variacao_pct_reserva(self):
        if self.tipo != self.RESERVAR:
            return None
        preco_atual = self.preco_atual
        if preco_atual is None:
            return None
        return ((preco_atual - self.preco_unitario) / self.preco_unitario * 100).quantize(Decimal("0.01"))

    @property
    def meta_compra_atingida(self):
        variacao = self.variacao_pct_reserva
        if variacao is None:
            return False
        meta_bruta = (
            self.meta_perda_pct
            if self.meta_perda_pct is not None
            else Decimal(str(settings.META_PERDA_PADRAO))
        )
        meta = -abs(meta_bruta)
        return variacao <= meta


class CotacaoCripto(models.Model):
    """Histórico de cotações diárias de uma criptomoeda - espelha Cotacao, mas ligado a CriptoAtivo."""

    cripto_ativo = models.ForeignKey(CriptoAtivo, on_delete=models.CASCADE, related_name="cotacoes")
    data = models.DateField("Data da cotação")
    preco_fechamento = models.DecimalField("Preço de fechamento", max_digits=18, decimal_places=8)
    variacao_dia_pct = models.DecimalField(
        "Variação no dia (%)", max_digits=8, decimal_places=2, null=True, blank=True
    )
    maxima = models.DecimalField("Máxima do dia", max_digits=18, decimal_places=8, null=True, blank=True)
    minima = models.DecimalField("Mínima do dia", max_digits=18, decimal_places=8, null=True, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Cotação de criptomoeda"
        verbose_name_plural = "Cotações de criptomoeda"
        ordering = ["-data"]
        unique_together = ("cripto_ativo", "data")

    def __str__(self):
        return f"{self.cripto_ativo.coin} {self.data} R$ {self.preco_fechamento}"


class PostIt(models.Model):
    """
    Bloco de notas pessoal e fixo do usuário (tipo "post-it"): um texto livre
    que fica salvo e aparece flutuando nas principais telas do sistema (Menu,
    Minhas Operações, Posições em Carteira), pronto pra anotar lembretes
    rápidos sem precisar de uma tela dedicada. Um por usuário - salva sozinho
    enquanto o usuário digita (ver core.views.post_it_salvar).
    """

    usuario = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="post_it"
    )
    texto = models.TextField("Texto", blank=True)
    minimizado = models.BooleanField("Minimizado", default=False)
    atualizado_em = models.DateTimeField(auto_now=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Post-it"
        verbose_name_plural = "Post-its"

    def __str__(self):
        return f"Post-it de {self.usuario}"


class RegistroAtualizacaoCarteiraCripto(models.Model):
    """
    Espelha RegistroAtualizacaoCarteira (ações), mas para a carteira de
    criptomoedas - grava toda vez que as cotações de cripto são atualizadas
    (ver core.views.cripto_atualizar_cotacoes e core.services.
    registrar_atualizacao_carteira_cripto). Usa os mesmos nomes de campo
    (criado_em/valor_atual/lucro_perda/lucro_perda_pct) que
    RegistroAtualizacaoCarteira de propósito, pra poder reaproveitar
    construir_grafico_atualizacoes_dia/calcular_variacoes_historico sem
    duplicar essas funções.
    """

    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="registros_atualizacao_carteira_cripto",
    )
    criado_em = models.DateTimeField("Data/hora", auto_now_add=True)
    total_moedas = models.PositiveIntegerField("Total de moedas")
    valor_investido = models.DecimalField("Valor investido", max_digits=14, decimal_places=2)
    valor_atual = models.DecimalField("Valor atual", max_digits=14, decimal_places=2)
    lucro_perda = models.DecimalField("Lucro/Perda (R$)", max_digits=14, decimal_places=2)
    lucro_perda_pct = models.DecimalField(
        "Lucro/Perda (%)", max_digits=8, decimal_places=2, null=True, blank=True,
    )

    class Meta:
        verbose_name = "Registro de atualização da carteira de cripto"
        verbose_name_plural = "Registros de atualização da carteira de cripto"
        ordering = ["-criado_em"]

    def __str__(self):
        return f"{self.usuario} - {self.criado_em:%d/%m/%Y %H:%M}"


class PrevisaoAnalista(models.Model):
    """
    Previsão de consenso dos analistas para um ativo num horizonte de 12
    meses (preço-alvo médio/mínimo/máximo e recomendação, como o TradingView
    mostra na aba "Previsão"), registrada junto com a cotação do dia. Passado
    o prazo, o sistema compara com o preço real e mede se os analistas
    acertaram a direção (alta/baixa) e o alvo - ver
    core.services.avaliar_previsoes_analistas.
    """

    PRAZO_DIAS = 365

    COMPRA_FORTE = "COMPRA_FORTE"
    COMPRA = "COMPRA"
    NEUTRO = "NEUTRO"
    VENDA = "VENDA"
    VENDA_FORTE = "VENDA_FORTE"
    RECOMENDACAO_CHOICES = [
        (COMPRA_FORTE, "Compra forte"),
        (COMPRA, "Compra"),
        (NEUTRO, "Neutro"),
        (VENDA, "Venda"),
        (VENDA_FORTE, "Venda forte"),
    ]

    MANUAL = "MANUAL"
    API = "API"
    ORIGEM_CHOICES = [
        (MANUAL, "Manual (TradingView)"),
        (API, "API de cotações"),
    ]

    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="previsoes_analistas",
    )
    ativo = models.ForeignKey(Ativo, on_delete=models.CASCADE, related_name="previsoes_analistas")
    data_registro = models.DateField("Data do registro", default=timezone.localdate)
    preco_registro = models.DecimalField("Cotação na data (R$)", max_digits=12, decimal_places=2)
    alvo_medio = models.DecimalField("Preço-alvo médio (R$)", max_digits=12, decimal_places=2)
    alvo_minimo = models.DecimalField("Preço-alvo mínimo (R$)", max_digits=12, decimal_places=2, null=True, blank=True)
    alvo_maximo = models.DecimalField("Preço-alvo máximo (R$)", max_digits=12, decimal_places=2, null=True, blank=True)
    recomendacao = models.CharField("Recomendação", max_length=12, choices=RECOMENDACAO_CHOICES, blank=True)
    numero_analistas = models.PositiveIntegerField("Nº de analistas", null=True, blank=True)
    origem = models.CharField("Origem", max_length=8, choices=ORIGEM_CHOICES, default=MANUAL)
    observacao = models.CharField("Observação", max_length=255, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Previsão de analistas"
        verbose_name_plural = "Previsões de analistas"
        ordering = ["-data_registro", "-id"]
        indexes = [models.Index(fields=["usuario", "data_registro"])]

    def __str__(self):
        return f"{self.ativo.ticker} {self.data_registro:%d/%m/%Y} alvo R$ {self.alvo_medio}"

    @property
    def data_vencimento(self):
        return self.data_registro + timedelta(days=self.PRAZO_DIAS)

    @property
    def direcao_prevista(self) -> str:
        """"ALTA", "BAIXA" ou "NEUTRA" - pelo alvo médio em relação à cotação do dia do registro."""
        if self.alvo_medio > self.preco_registro:
            return "ALTA"
        if self.alvo_medio < self.preco_registro:
            return "BAIXA"
        return "NEUTRA"

    @property
    def variacao_prevista_pct(self) -> Decimal:
        return ((self.alvo_medio / self.preco_registro - 1) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class FundamentosAtivo(models.Model):
    """
    Indicadores fundamentalistas de uma ação e a nota de qualidade (0 a 10)
    calculada a partir deles (ver core.services.calcular_qualidade_fundamentos).
    Dados públicos da empresa, compartilhados entre usuários e guardados por
    ticker (sem exigir um Ativo cadastrado) - balanços mudam por trimestre,
    então só são buscados de novo na API depois de FUNDAMENTOS_VALIDADE_DIAS.
    """

    ticker = models.CharField("Ticker", max_length=12, unique=True)
    nome = models.CharField("Nome", max_length=255, blank=True)
    setor = models.CharField("Setor", max_length=150, blank=True)
    financeira = models.BooleanField(
        "Instituição financeira", default=False,
        help_text="Bancos e seguradoras: endividamento não é avaliado (passivo alto é o próprio negócio).",
    )
    preco = models.DecimalField("Preço (R$)", max_digits=12, decimal_places=2, null=True, blank=True)
    p_l = models.DecimalField("P/L", max_digits=12, decimal_places=2, null=True, blank=True)
    p_vp = models.DecimalField("P/VP", max_digits=12, decimal_places=2, null=True, blank=True)
    roe_pct = models.DecimalField("ROE (%)", max_digits=10, decimal_places=2, null=True, blank=True)
    margem_liquida_pct = models.DecimalField("Margem líquida (%)", max_digits=10, decimal_places=2, null=True, blank=True)
    cagr_lucro_pct = models.DecimalField(
        "Crescimento do lucro (% ao ano, 3 anos)", max_digits=10, decimal_places=2, null=True, blank=True,
    )
    anos_lucro_positivo = models.PositiveSmallIntegerField("Anos com lucro", null=True, blank=True)
    anos_avaliados = models.PositiveSmallIntegerField("Anos avaliados", null=True, blank=True)
    endividamento_pct = models.DecimalField(
        "Passivo / ativo total (%)", max_digits=10, decimal_places=2, null=True, blank=True,
    )
    dividend_yield_pct = models.DecimalField(
        "Dividend yield 12 meses (%)", max_digits=10, decimal_places=2, null=True, blank=True,
    )
    nota = models.DecimalField("Nota de qualidade (0 a 10)", max_digits=4, decimal_places=1, null=True, blank=True)
    criterios = models.JSONField("Critérios avaliados", default=list, blank=True)
    atualizado_em = models.DateTimeField("Atualizado em", auto_now=True)

    class Meta:
        verbose_name = "Fundamentos de ativo"
        verbose_name_plural = "Fundamentos de ativos"
        ordering = ["-nota", "ticker"]

    def __str__(self):
        return f"{self.ticker} nota {self.nota}"

    @property
    def classificacao(self) -> str:
        """"ALTA", "MEDIA" ou "BAIXA" qualidade pela nota (ou "SEM_DADOS")."""
        if self.nota is None:
            return "SEM_DADOS"
        if self.nota >= 7:
            return "ALTA"
        if self.nota >= 4:
            return "MEDIA"
        return "BAIXA"
