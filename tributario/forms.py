from decimal import Decimal

from django import forms
from django.utils import timezone

from .models import SaldoInicialFiscal


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
