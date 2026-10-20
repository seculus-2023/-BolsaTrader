"""
Dias úteis bancários - usados no vencimento da DARF (último dia útil do mês
seguinte ao da apuração). Considera sábados, domingos, feriados nacionais e
os dias sem expediente bancário em todo o país (Carnaval, Sexta-feira Santa e
Corpus Christi). Feriados estaduais e municipais não entram.
"""
import calendar
from datetime import date, timedelta
from functools import lru_cache


def pascoa(ano: int) -> date:
    """Domingo de Páscoa (algoritmo de Meeus/Jones/Butcher, calendário gregoriano)."""
    a = ano % 19
    b, c = divmod(ano, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes, dia = divmod(h + l - 7 * m + 114, 31)
    return date(ano, mes, dia + 1)


@lru_cache(maxsize=None)
def feriados_bancarios(ano: int) -> frozenset[date]:
    domingo_pascoa = pascoa(ano)
    fixos = [(1, 1), (4, 21), (5, 1), (9, 7), (10, 12), (11, 2), (11, 15), (12, 25)]
    if ano >= 2024:  # Dia Nacional de Zumbi e da Consciência Negra (Lei 14.759/2023)
        fixos.append((11, 20))
    moveis = [
        domingo_pascoa - timedelta(days=48),  # Carnaval (segunda)
        domingo_pascoa - timedelta(days=47),  # Carnaval (terça)
        domingo_pascoa - timedelta(days=2),  # Sexta-feira Santa
        domingo_pascoa + timedelta(days=60),  # Corpus Christi
    ]
    return frozenset([date(ano, mes, dia) for mes, dia in fixos] + moveis)


def dia_util(dia: date) -> bool:
    return dia.weekday() < 5 and dia not in feriados_bancarios(dia.year)


def ultimo_dia_util(ano: int, mes: int) -> date:
    dia = date(ano, mes, calendar.monthrange(ano, mes)[1])
    while not dia_util(dia):
        dia -= timedelta(days=1)
    return dia
