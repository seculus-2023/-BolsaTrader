from decimal import Decimal

from django import forms
from django.utils import timezone

from core.models import Ativo, Operacao

from .models import EventoCorporativo, Provento, SaldoInicialFiscal


def _estilizar(form):
    for field in form.fields.values():
        if not isinstance(field.widget, forms.CheckboxInput):
            field.widget.attrs["class"] = (field.widget.attrs.get("class", "") + " form-control-futurista").strip()


class ProventoForm(forms.ModelForm):
    """Dividendo, JCP ou rendimento de FII recebido. IR retido em branco = o sistema sugere pela regra vigente."""

    ticker = forms.CharField(label="Ticker (ex: PETR4, HGLG11)", max_length=12)
    lancar_conta_corrente = forms.BooleanField(
        label="Lançar o valor líquido como crédito na conta corrente", required=False, initial=True,
    )

    class Meta:
        model = Provento
        fields = ["tipo", "data_pagamento", "valor_bruto", "ir_retido", "observacao"]
        widgets = {"data_pagamento": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}
        labels = {"ir_retido": "IR retido na fonte (R$) - em branco, o sistema calcula pela regra vigente"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["ir_retido"].required = False
        self.order_fields(["ticker", "tipo", "data_pagamento", "valor_bruto", "ir_retido", "observacao", "lancar_conta_corrente"])
        if self.instance.pk:
            self.initial.setdefault("ticker", self.instance.ativo.ticker)
            self.initial.setdefault("lancar_conta_corrente", self.instance.lancamento_id is not None)
        elif not self.initial.get("data_pagamento"):
            self.initial["data_pagamento"] = timezone.localdate()
        _estilizar(self)

    def clean_ticker(self):
        return self.cleaned_data["ticker"].strip().upper()

    def clean_valor_bruto(self):
        valor = self.cleaned_data["valor_bruto"]
        if valor <= 0:
            raise forms.ValidationError("Informe um valor maior que zero.")
        return valor

    def clean(self):
        dados = super().clean()
        ir, bruto = dados.get("ir_retido"), dados.get("valor_bruto")
        if ir is not None and bruto is not None and not (0 <= ir <= bruto):
            self.add_error("ir_retido", "O IR retido deve ficar entre zero e o valor bruto.")
        return dados


class EventoCorporativoForm(forms.ModelForm):
    ticker = forms.ChoiceField(label="Ativo")

    class Meta:
        model = EventoCorporativo
        fields = ["tipo", "data", "proporcao", "custo_unitario_bonificacao"]
        widgets = {"data": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}

    def __init__(self, *args, usuario, **kwargs):
        super().__init__(*args, **kwargs)
        tickers = (
            Operacao.objects.filter(usuario=usuario, tipo=Operacao.COMPRA)
            .order_by("ativo__ticker").values_list("ativo__ticker", flat=True).distinct()
        )
        self.fields["ticker"].choices = [(t, t) for t in tickers]
        self.order_fields(["ticker", "tipo", "data", "proporcao", "custo_unitario_bonificacao"])
        _estilizar(self)

    def clean(self):
        dados = super().clean()
        tipo, proporcao = dados.get("tipo"), dados.get("proporcao")
        if proporcao is not None:
            if proporcao <= 0:
                self.add_error("proporcao", "Informe uma proporção maior que zero.")
            elif tipo in (EventoCorporativo.DESDOBRAMENTO, EventoCorporativo.GRUPAMENTO) and proporcao <= 1:
                self.add_error("proporcao", "Desdobramento e grupamento usam proporção maior que 1 (ex: 1 para 2 = 2).")
        if tipo == EventoCorporativo.BONIFICACAO and not dados.get("custo_unitario_bonificacao"):
            self.add_error("custo_unitario_bonificacao", "Informe o custo por ação atribuído pela empresa na bonificação.")
        if "ticker" in dados:
            self.instance.ativo = Ativo.objects.get(ticker=dados["ticker"])
        return dados


class PagamentoDarfForm(forms.Form):
    """Data e valor efetivamente pagos (com multa e juros, se pagou em atraso)."""

    data_pagamento = forms.DateField(
        label="Data do pagamento", widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )
    valor_pago = forms.DecimalField(label="Valor pago (R$)", max_digits=14, decimal_places=2, min_value=Decimal("0.01"))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control-futurista"

    def clean_data_pagamento(self):
        data = self.cleaned_data["data_pagamento"]
        if data > timezone.localdate():
            raise forms.ValidationError("A data do pagamento não pode ser no futuro.")
        return data


class SaldoInicialFiscalForm(forms.ModelForm):
    """Prejuízos acumulados antes das operações cadastradas no sistema (valores positivos)."""

    class Meta:
        model = SaldoInicialFiscal
        fields = ["prejuizo_comum", "prejuizo_day_trade", "prejuizo_fii"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.required = False
            field.widget.attrs["class"] = (field.widget.attrs.get("class", "") + " form-control-futurista").strip()
            field.widget.attrs["min"] = "0"

    def _limpar(self, nome):
        valor = self.cleaned_data.get(nome) or Decimal("0")
        if valor < 0:
            raise forms.ValidationError("Informe o prejuízo como valor positivo.")
        return valor

    def clean_prejuizo_comum(self):
        return self._limpar("prejuizo_comum")

    def clean_prejuizo_day_trade(self):
        return self._limpar("prejuizo_day_trade")

    def clean_prejuizo_fii(self):
        return self._limpar("prejuizo_fii")
