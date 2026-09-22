from decimal import Decimal

from django import forms
from django.conf import settings
from django.utils import timezone

from .models import Operacao, Ativo, FonteNoticia, ContaCorrente, LancamentoContaCorrente
from .services import ativos_distintos_comprados


class MetasValidacaoMixin:
    """
    Garante o sinal correto das metas de lucro/perda (lucro é sempre um ganho
    positivo, perda é sempre uma queda negativa). Validação de formulário, não
    do model: nem todo ModelForm de Operacao expõe esses dois campos (ex:
    VendaLoteForm), e uma checagem em Model.clean() dispararia um ValueError
    ao tentar anexar o erro a um campo que aquele form não tem.
    """

    def clean_meta_perda_pct(self):
        valor = self.cleaned_data.get("meta_perda_pct")
        if valor is not None and valor > 0:
            raise forms.ValidationError("A meta de perda deve ser um valor negativo (ex: -5.00), não positivo.")
        return valor

    def clean_meta_lucro_pct(self):
        valor = self.cleaned_data.get("meta_lucro_pct")
        if valor is not None and valor < 0:
            raise forms.ValidationError("A meta de lucro deve ser um valor positivo (ex: 5.00), não negativo.")
        return valor


class OperacaoForm(MetasValidacaoMixin, forms.ModelForm):
    ticker = forms.CharField(
        label="Ticker do ativo (ex: PETR4, VALE3, ITUB4)",
        max_length=12,
        widget=forms.TextInput(attrs={"placeholder": "PETR4"}),
    )
    nome_ativo = forms.CharField(
        label="Nome da empresa (ex: Petrobras) - opcional",
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "Petrobras"}),
    )

    class Meta:
        model = Operacao
        fields = [
            "tipo",
            "quantidade",
            "preco_unitario",
            "data_operacao",
            "meta_lucro_pct",
            "meta_perda_pct",
            "observacao",
        ]
        widgets = {
            "data_operacao": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "observacao": forms.Textarea(attrs={"rows": 2}),
        }
        labels = {
            "meta_lucro_pct": "Meta de lucro (%) - opcional",
            "meta_perda_pct": "Meta de perda (%) - opcional, use valor negativo",
        }

    def __init__(self, *args, usuario=None, **kwargs):
        self.usuario = usuario
        super().__init__(*args, **kwargs)

        if "data_operacao" in self.fields and not self.initial.get("data_operacao"):
            self.initial["data_operacao"] = timezone.localdate()

        for name, field in self.fields.items():
            css = field.widget.attrs.get("class", "")
            field.widget.attrs["class"] = (css + " form-control-futurista").strip()

    def clean_ticker(self):
        return self.cleaned_data["ticker"].strip().upper()

    def clean_nome_ativo(self):
        return self.cleaned_data["nome_ativo"].strip()

    def clean(self):
        cleaned_data = super().clean()
        ticker = cleaned_data.get("ticker")
        tipo = cleaned_data.get("tipo")

        # o limite é só pra compra de verdade (capital comprometido) - uma
        # reserva é só intenção, não conta pra cota de ativos em carteira.
        if tipo == Operacao.COMPRA and ticker and self.usuario is not None:
            ativos_comprados = ativos_distintos_comprados(self.usuario)
            ativo_existente = Ativo.objects.filter(ticker=ticker).first()
            ja_possui_este_ativo = ativo_existente is not None and ativo_existente.id in ativos_comprados
            if not ja_possui_este_ativo and len(ativos_comprados) >= settings.MAX_ATIVOS_EM_CARTEIRA:
                self.add_error(
                    "ticker",
                    f"Você já atingiu o limite de {settings.MAX_ATIVOS_EM_CARTEIRA} ativo(s) diferentes "
                    "em carteira. Venda algum ativo atual antes de comprar um novo (o limite é configurável "
                    "em MAX_ATIVOS_EM_CARTEIRA no .env).",
                )
        return cleaned_data

    def save(self, commit=True):
        operacao = super().save(commit=False)
        ticker = self.cleaned_data["ticker"]
        ativo, _ = Ativo.objects.get_or_create(ticker=ticker)

        nome_ativo = self.cleaned_data.get("nome_ativo")
        if nome_ativo:
            ativo.nome = nome_ativo
            ativo.save(update_fields=["nome"])

        operacao.ativo = ativo
        if commit:
            operacao.save()
        return operacao


class ConfirmarCompraForm(MetasValidacaoMixin, forms.ModelForm):
    """
    Efetiva uma reserva (intenção de compra) como uma compra real: converte o
    mesmo registro de tipo Reservar para Compra, com a quantidade e o preço
    realmente pagos (podem diferir do que foi planejado na reserva).
    """

    class Meta:
        model = Operacao
        fields = ["quantidade", "preco_unitario", "data_operacao", "meta_lucro_pct", "meta_perda_pct"]
        widgets = {
            "data_operacao": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }
        labels = {
            "quantidade": "Quantidade comprada",
            "preco_unitario": "Preço pago (R$)",
            "data_operacao": "Data da compra",
            "meta_lucro_pct": "Meta de lucro (%) - opcional",
            "meta_perda_pct": "Meta de perda (%) - opcional, use valor negativo",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # já muda o tipo aqui (antes da validação) - a regra de "Reservar não
        # pode ter quantidade > 1" (Operacao.clean) não deve valer mais aqui,
        # já que este registro está virando uma compra de verdade
        self.instance.tipo = Operacao.COMPRA

        if not self.initial.get("data_operacao"):
            self.initial["data_operacao"] = timezone.localdate()

        for name, field in self.fields.items():
            css = field.widget.attrs.get("class", "")
            field.widget.attrs["class"] = (css + " form-control-futurista").strip()

    def clean(self):
        cleaned_data = super().clean()
        usuario = self.instance.usuario
        ativo = self.instance.ativo

        # mesmo limite de ativos diferentes em carteira aplicado na nova
        # compra (ver OperacaoForm.clean) - efetivar uma reserva também
        # comete capital de verdade, então conta pra mesma cota.
        if usuario is not None:
            ativos_comprados = ativos_distintos_comprados(usuario)
            if ativo.id not in ativos_comprados and len(ativos_comprados) >= settings.MAX_ATIVOS_EM_CARTEIRA:
                self.add_error(
                    "quantidade",
                    f"Você já atingiu o limite de {settings.MAX_ATIVOS_EM_CARTEIRA} ativo(s) diferentes "
                    "em carteira. Venda algum ativo atual antes de efetivar esta reserva como compra "
                    "(o limite é configurável em MAX_ATIVOS_EM_CARTEIRA no .env).",
                )
        return cleaned_data


class EditarCompraForm(MetasValidacaoMixin, forms.ModelForm):
    """Corrige os dados de uma compra já registrada (ticker, nome, quantidade, preço, data e metas)."""

    ticker = forms.CharField(
        label="Ticker do ativo (ex: PETR4, VALE3, ITUB4)",
        max_length=12,
        widget=forms.TextInput(attrs={"placeholder": "PETR4"}),
    )
    nome_ativo = forms.CharField(
        label="Nome da empresa (ex: Petrobras) - opcional",
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "Petrobras"}),
    )

    class Meta:
        model = Operacao
        fields = ["quantidade", "preco_unitario", "data_operacao", "meta_lucro_pct", "meta_perda_pct", "observacao"]
        widgets = {
            "data_operacao": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "observacao": forms.Textarea(attrs={"rows": 2}),
        }
        labels = {
            "quantidade": "Quantidade comprada",
            "preco_unitario": "Preço de compra (R$)",
            "data_operacao": "Data da compra",
            "meta_lucro_pct": "Meta de lucro (%) - opcional",
            "meta_perda_pct": "Meta de perda (%) - opcional, use valor negativo",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if not self.initial.get("ticker"):
            self.initial["ticker"] = self.instance.ativo.ticker
        if not self.initial.get("nome_ativo"):
            self.initial["nome_ativo"] = self.instance.ativo.nome

        for name, field in self.fields.items():
            css = field.widget.attrs.get("class", "")
            field.widget.attrs["class"] = (css + " form-control-futurista").strip()

    def clean_quantidade(self):
        quantidade = self.cleaned_data["quantidade"]
        if quantidade < self.instance.quantidade_vendida:
            raise forms.ValidationError(
                f"Este lote já tem {self.instance.quantidade_vendida} unidade(s) vendida(s) - "
                f"a quantidade não pode ficar menor que isso."
            )
        return quantidade

    def clean_ticker(self):
        return self.cleaned_data["ticker"].strip().upper()

    def clean_nome_ativo(self):
        return self.cleaned_data["nome_ativo"].strip()

    def save(self, commit=True):
        operacao = super().save(commit=False)

        ticker = self.cleaned_data["ticker"]
        if ticker != operacao.ativo.ticker:
            operacao.ativo, _ = Ativo.objects.get_or_create(ticker=ticker)

        nome_ativo = self.cleaned_data.get("nome_ativo")
        if nome_ativo:
            operacao.ativo.nome = nome_ativo
            operacao.ativo.save(update_fields=["nome"])

        if commit:
            operacao.save()
        return operacao


class EditarReservaForm(MetasValidacaoMixin, forms.ModelForm):
    """Corrige os dados de uma reserva já registrada (ticker, nome, preço pretendido, data e metas)."""

    ticker = forms.CharField(
        label="Ticker do ativo (ex: PETR4, VALE3, ITUB4)",
        max_length=12,
        widget=forms.TextInput(attrs={"placeholder": "PETR4"}),
    )
    nome_ativo = forms.CharField(
        label="Nome da empresa (ex: Petrobras) - opcional",
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "Petrobras"}),
    )

    class Meta:
        model = Operacao
        fields = ["quantidade", "preco_unitario", "data_operacao", "meta_lucro_pct", "meta_perda_pct"]
        widgets = {
            "data_operacao": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }
        labels = {
            "quantidade": "Quantidade reservada",
            "preco_unitario": "Preço pretendido (R$)",
            "data_operacao": "Data da reserva",
            "meta_lucro_pct": "Meta de lucro (%) - opcional",
            "meta_perda_pct": "Meta de perda (%) - opcional, use valor negativo",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if not self.initial.get("ticker"):
            self.initial["ticker"] = self.instance.ativo.ticker
        if not self.initial.get("nome_ativo"):
            self.initial["nome_ativo"] = self.instance.ativo.nome

        for name, field in self.fields.items():
            css = field.widget.attrs.get("class", "")
            field.widget.attrs["class"] = (css + " form-control-futurista").strip()

    def clean_ticker(self):
        return self.cleaned_data["ticker"].strip().upper()

    def clean_nome_ativo(self):
        return self.cleaned_data["nome_ativo"].strip()

    def save(self, commit=True):
        operacao = super().save(commit=False)

        ticker = self.cleaned_data["ticker"]
        if ticker != operacao.ativo.ticker:
            operacao.ativo, _ = Ativo.objects.get_or_create(ticker=ticker)

        nome_ativo = self.cleaned_data.get("nome_ativo")
        if nome_ativo:
            operacao.ativo.nome = nome_ativo
            operacao.ativo.save(update_fields=["nome"])

        if commit:
            operacao.save()
        return operacao


class VendaLoteForm(forms.ModelForm):
    """Registra a venda (total ou parcial) de um lote de compra já existente."""

    class Meta:
        model = Operacao
        fields = ["quantidade_vendida", "preco_venda", "data_venda", "percentual_corretora"]
        widgets = {
            "data_venda": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }
        labels = {
            "quantidade_vendida": "Quantidade vendida",
            "preco_venda": "Preço de venda (R$)",
            "data_venda": "Data da venda",
            "percentual_corretora": "Percentual da corretora (%)",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if not self.initial.get("data_venda"):
            self.initial["data_venda"] = timezone.localdate()

        for name, field in self.fields.items():
            css = field.widget.attrs.get("class", "")
            field.widget.attrs["class"] = (css + " form-control-futurista").strip()

    def clean_quantidade_vendida(self):
        quantidade_vendida = self.cleaned_data["quantidade_vendida"]
        if quantidade_vendida > self.instance.quantidade:
            raise forms.ValidationError(
                f"Este lote tem {self.instance.quantidade} unidade(s) compradas - "
                f"não é possível vender {quantidade_vendida}."
            )
        return quantidade_vendida

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("quantidade_vendida") and not cleaned_data.get("preco_venda"):
            self.add_error("preco_venda", "Informe o preço de venda para calcular o lucro/perda.")
        return cleaned_data


class FonteNoticiaForm(forms.ModelForm):
    """Cadastra uma nova fonte de notícias (site a ser lido periodicamente)."""

    class Meta:
        model = FonteNoticia
        fields = ["nome", "url"]
        widgets = {
            "url": forms.URLInput(attrs={"placeholder": "https://..."}),
        }
        labels = {
            "nome": "Nome da fonte",
            "url": "URL da página",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            css = field.widget.attrs.get("class", "")
            field.widget.attrs["class"] = (css + " form-control-futurista").strip()


class SaldoInicialContaCorrenteForm(forms.ModelForm):
    """Define/corrige o saldo inicial da conta corrente do usuário."""

    class Meta:
        model = ContaCorrente
        fields = ["saldo_inicial"]
        labels = {"saldo_inicial": "Saldo inicial da conta (R$)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            css = field.widget.attrs.get("class", "")
            field.widget.attrs["class"] = (css + " form-control-futurista").strip()


class TransferenciaContaCorrenteForm(forms.Form):
    """
    Lançamento manual de transferência de/para outra conta (ex: Nubank,
    corretora) - crédito quando o dinheiro entra na conta corrente do
    sistema, débito quando sai dela para outro lugar.
    """

    tipo = forms.ChoiceField(label="Tipo", choices=LancamentoContaCorrente.TIPO_CHOICES)
    valor = forms.DecimalField(label="Valor (R$)", max_digits=14, decimal_places=2, min_value=Decimal("0.01"))
    descricao = forms.CharField(
        label="Descrição", max_length=255,
        widget=forms.TextInput(attrs={"placeholder": "Ex: Transferência recebida do Nubank"}),
    )
    data = forms.DateField(
        label="Data", widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.initial.get("data"):
            self.initial["data"] = timezone.localdate()
        for name, field in self.fields.items():
            css = field.widget.attrs.get("class", "")
            field.widget.attrs["class"] = (css + " form-control-futurista").strip()
