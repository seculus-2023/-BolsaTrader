from decimal import Decimal

from django import forms

from .models import SaldoInicialFiscal


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
