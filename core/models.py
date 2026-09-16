from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


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

    def ultima_cotacao(self):
        return self.cotacoes.order_by("-data").first()


class Operacao(models.Model):
    """
    Uma compra ou uma reserva de um ativo, feita por um usuário.

    Uma compra funciona como um "lote": a venda (total ou parcial) desse lote
    é registrada nos próprios campos quantidade_vendida/preco_venda/data_venda
    do MESMO registro (ver core.views.operacao_vender), em vez de criar uma
    operação de venda separada. Isso deixa o lucro/perda de cada lote como
    uma simples comparação entre o preço de compra e o preço de venda dele
    mesmo, sem precisar de custo médio entre vários registros.
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

    # metas individuais (percentual) para gerar avisos de lucro/perda
    meta_lucro_pct = models.DecimalField(
        "Meta de lucro (%)", max_digits=6, decimal_places=2, null=True, blank=True
    )
    meta_perda_pct = models.DecimalField(
        "Meta de perda (%) - use valor negativo", max_digits=6, decimal_places=2, null=True, blank=True
    )

    # venda (total ou parcial) deste lote de compra - ficam vazios/zerados
    # enquanto o lote não foi vendido, e não se aplicam ao tipo Reservar.
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

    @property
    def valor_total_vendido(self):
        """Total recebido na venda deste lote (preço de venda x quantidade vendida)."""
        if not self.quantidade_vendida or self.preco_venda is None:
            return None
        return self.preco_venda * self.quantidade_vendida

    @property
    def valor_liquido_vendido(self):
        """Valor líquido a receber pela venda deste lote, já descontado o percentual da corretora."""
        total = self.valor_total_vendido
        if total is None:
            return None
        percentual = self.percentual_corretora or Decimal("0")
        return (total * (1 - percentual / 100)).quantize(Decimal("0.01"))

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
        if not self.quantidade_vendida or self.preco_venda is None:
            return None
        return ((self.preco_venda - self.preco_unitario) * self.quantidade_vendida).quantize(Decimal("0.01"))

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


class Cotacao(models.Model):
    """Histórico de cotações diárias de um ativo (usado para acompanhar lucro/perda e tendência)."""

    ativo = models.ForeignKey(Ativo, on_delete=models.CASCADE, related_name="cotacoes")
    data = models.DateField("Data da cotação")
    preco_fechamento = models.DecimalField("Preço de fechamento (R$)", max_digits=12, decimal_places=2)
    variacao_dia_pct = models.DecimalField(
        "Variação no dia (%)", max_digits=6, decimal_places=2, null=True, blank=True
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
    LEMBRETE = "LEMBRETE"
    TIPO_CHOICES = [
        (LUCRO, "Meta de lucro atingida"),
        (PERDA, "Meta de perda atingida"),
        (TENDENCIA, "Sinal de tendência de mercado"),
        (SINAL_COMPRA, "Robô: sinal de compra"),
        (SINAL_VENDA, "Robô: sinal de venda"),
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
