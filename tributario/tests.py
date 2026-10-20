"""
Testes do módulo tributário (preço médio fiscal, day trade e classe dos ativos).

Executar com:
    python manage.py test tributario
"""
import importlib
from datetime import date, timedelta
from decimal import Decimal

from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core.classificacao import ACAO, BDR, ETF, FII, UNIT, inferir_classe_ativo
from core.models import Ativo, LancamentoContaCorrente, Operacao, VendaLote
from core.services import obter_ou_criar_conta_corrente, sincronizar_lancamento_compra

from .services import COMUM, DAY_TRADE, MovimentoDia, apurar_operacoes, calcular_apuracao

D1 = date(2026, 3, 2)
D2 = date(2026, 3, 3)
D3 = date(2026, 3, 4)


class ClassificacaoAtivoTests(TestCase):
    def test_inferencia_pelo_ticker(self):
        self.assertEqual(inferir_classe_ativo("PETR4"), ACAO)
        self.assertEqual(inferir_classe_ativo("VALE3"), ACAO)
        self.assertEqual(inferir_classe_ativo("PETR4F"), ACAO)
        self.assertEqual(inferir_classe_ativo("AAPL34"), BDR)
        self.assertEqual(inferir_classe_ativo("HGLG11"), FII)
        self.assertEqual(inferir_classe_ativo("BOVA11"), ETF)
        self.assertEqual(inferir_classe_ativo("TAEE11"), UNIT)
        self.assertEqual(inferir_classe_ativo("XPTO11", "iShares Qualquer Índice"), ETF)

    def test_ativo_novo_ganha_classe_e_classe_manual_e_respeitada(self):
        self.assertEqual(Ativo.objects.create(ticker="HGLG11").classe, FII)
        corrigido = Ativo.objects.create(ticker="ABCD11", classe=UNIT)
        corrigido.save()
        corrigido.refresh_from_db()
        self.assertEqual(corrigido.classe, UNIT)


class CalculoPrecoMedioPuroTests(TestCase):
    """Regras de preço médio e day trade direto em calcular_apuracao, sem banco."""

    def test_venda_comum_da_baixa_pelo_preco_medio_ponderado_com_taxas(self):
        movimentos = {
            ("PETR4", D1): MovimentoDia(quantidade_compra=100, custo_compra=Decimal("1005.00")),  # 100 x 10 + 5 de taxas
            ("PETR4", D2): MovimentoDia(quantidade_compra=100, custo_compra=Decimal("2000.00")),
            ("PETR4", D3): MovimentoDia(quantidade_venda=50, valor_venda=Decimal("1500.00"), custos_venda=Decimal("3.00")),
        }
        apuracao = calcular_apuracao(movimentos, {"PETR4": ACAO})

        posicao = apuracao.posicoes["PETR4"]
        self.assertEqual(posicao.quantidade, 150)
        self.assertEqual(posicao.preco_medio, Decimal("15.0250"))  # (1005 + 2000) / 200
        self.assertEqual(posicao.custo_total_centavos, Decimal("2253.75"))

        [resultado] = apuracao.resultados
        self.assertEqual(resultado.modalidade, COMUM)
        self.assertEqual(resultado.resultado, Decimal("745.75"))  # 1500 - 3 - 50 x 15,025
        self.assertEqual(apuracao.inconsistencias, [])

    def test_compra_e_venda_no_mesmo_dia_sao_day_trade_e_nao_mexem_no_preco_medio(self):
        movimentos = {
            ("PETR4", D1): MovimentoDia(quantidade_compra=100, custo_compra=Decimal("1000.00")),
            ("PETR4", D2): MovimentoDia(
                quantidade_compra=50, custo_compra=Decimal("600.00"),
                quantidade_venda=50, valor_venda=Decimal("650.00"),
            ),
        }
        apuracao = calcular_apuracao(movimentos, {})

        [resultado] = apuracao.resultados
        self.assertEqual(resultado.modalidade, DAY_TRADE)
        self.assertEqual(resultado.resultado, Decimal("50.00"))
        self.assertEqual(apuracao.posicoes["PETR4"].quantidade, 100)
        self.assertEqual(apuracao.posicoes["PETR4"].preco_medio, Decimal("10.0000"))

    def test_sobra_do_dia_separa_day_trade_da_operacao_comum(self):
        movimentos = {
            ("PETR4", D1): MovimentoDia(quantidade_compra=100, custo_compra=Decimal("1000.00")),
            ("PETR4", D2): MovimentoDia(
                quantidade_compra=20, custo_compra=Decimal("240.00"),
                quantidade_venda=50, valor_venda=Decimal("650.00"),
            ),
        }
        apuracao = calcular_apuracao(movimentos, {})

        por_modalidade = {r.modalidade: r for r in apuracao.resultados}
        self.assertEqual(por_modalidade[DAY_TRADE].quantidade, 20)
        self.assertEqual(por_modalidade[DAY_TRADE].resultado, Decimal("20.00"))  # 20 x 13 - 240
        self.assertEqual(por_modalidade[COMUM].quantidade, 30)
        self.assertEqual(por_modalidade[COMUM].resultado, Decimal("90.00"))  # 30 x 13 - 30 x 10
        self.assertEqual(apuracao.posicoes["PETR4"].quantidade, 70)

    def test_vender_tudo_zera_o_custo(self):
        movimentos = {
            ("VALE3", D1): MovimentoDia(quantidade_compra=3, custo_compra=Decimal("100.00")),
            ("VALE3", D2): MovimentoDia(quantidade_venda=3, valor_venda=Decimal("120.00")),
        }
        posicao = calcular_apuracao(movimentos, {}).posicoes["VALE3"]
        self.assertEqual(posicao.quantidade, 0)
        self.assertEqual(posicao.custo_total, Decimal("0"))

    def test_venda_maior_que_a_posicao_gera_aviso(self):
        movimentos = {
            ("VALE3", D1): MovimentoDia(quantidade_compra=10, custo_compra=Decimal("100.00")),
            ("VALE3", D2): MovimentoDia(quantidade_venda=15, valor_venda=Decimal("180.00")),
        }
        apuracao = calcular_apuracao(movimentos, {})
        self.assertEqual(len(apuracao.inconsistencias), 1)
        self.assertIn("VALE3", apuracao.inconsistencias[0])
        self.assertEqual(apuracao.posicoes["VALE3"].quantidade, 0)


class ApurarOperacoesTests(TestCase):
    """apurar_operacoes a partir dos lotes e vendas gravados."""

    def setUp(self):
        self.usuario = User.objects.create_user(username="contribuinte", password="SenhaForte123!")
        self.ativo = Ativo.objects.create(ticker="PETR4")

    def _lote(self, quantidade, preco, data, taxas="0", usuario=None):
        return Operacao.objects.create(
            usuario=usuario or self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=quantidade, preco_unitario=Decimal(preco), data_operacao=data, taxas_compra=Decimal(taxas),
        )

    def test_vender_o_lote_mais_caro_nao_muda_o_custo_fiscal(self):
        self._lote(100, "10.00", D1)
        lote_caro = self._lote(100, "20.00", D2)
        VendaLote.objects.create(operacao=lote_caro, quantidade=50, preco=Decimal("30.00"), data=D3)

        apuracao = apurar_operacoes(self.usuario)

        # visão do lote: (30 - 20) x 50; visão fiscal: 30 x 50 - 50 x 15 (preço médio)
        self.assertEqual(lote_caro.lucro_perda_realizado, Decimal("500.00"))
        self.assertEqual(apuracao.resultados[0].resultado, Decimal("750.00"))
        self.assertEqual(apuracao.posicoes["PETR4"].preco_medio, Decimal("15.0000"))

    def test_vender_lote_antigo_no_dia_de_outra_compra_e_day_trade(self):
        lote_antigo = self._lote(100, "10.00", D1)
        self._lote(50, "12.00", D3)
        VendaLote.objects.create(operacao=lote_antigo, quantidade=50, preco=Decimal("13.00"), data=D3)

        [resultado] = apurar_operacoes(self.usuario).resultados
        self.assertEqual(resultado.modalidade, DAY_TRADE)
        self.assertEqual(resultado.resultado, Decimal("50.00"))  # 50 x 13 - 50 x 12

    def test_taxas_entram_no_custo_e_corretora_reduz_o_ganho(self):
        lote = self._lote(10, "10.00", D1, taxas="1.00")
        VendaLote.objects.create(
            operacao=lote, quantidade=10, preco=Decimal("12.00"), data=D2,
            percentual_corretora=Decimal("0.50"), taxas=Decimal("0.40"),
        )
        [resultado] = apurar_operacoes(self.usuario).resultados
        self.assertEqual(resultado.resultado, Decimal("18.00"))  # 120 - 0,60 - 0,40 - 101

    def test_lote_com_venda_so_no_resumo_tambem_entra(self):
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA, quantidade=10,
            preco_unitario=Decimal("10.00"), data_operacao=D1,
            quantidade_vendida=10, preco_venda=Decimal("11.00"), data_venda=D2,
        )
        [resultado] = apurar_operacoes(self.usuario).resultados
        self.assertEqual(resultado.resultado, Decimal("10.00"))

    def test_data_limite_ignora_o_que_vem_depois(self):
        lote = self._lote(10, "10.00", D1)
        VendaLote.objects.create(operacao=lote, quantidade=10, preco=Decimal("12.00"), data=D3)
        apuracao = apurar_operacoes(self.usuario, ate=D2)
        self.assertEqual(apuracao.resultados, [])
        self.assertEqual(apuracao.posicoes["PETR4"].quantidade, 10)

    def test_operacoes_de_outro_usuario_nao_entram(self):
        outro = User.objects.create_user(username="outro_contribuinte", password="SenhaForte123!")
        self._lote(10, "10.00", D1, usuario=outro)
        self.assertEqual(apurar_operacoes(self.usuario).posicoes, {})

    def test_reserva_nao_entra(self):
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.RESERVAR,
            quantidade=1, preco_unitario=Decimal("10.00"), data_operacao=D1,
        )
        self.assertEqual(apurar_operacoes(self.usuario).posicoes, {})


class TaxasCompraContaCorrenteTests(TestCase):
    def test_debito_da_compra_inclui_as_taxas(self):
        usuario = User.objects.create_user(username="pagador_taxas", password="SenhaForte123!")
        lote = Operacao.objects.create(
            usuario=usuario, ativo=Ativo.objects.create(ticker="ITUB4"), tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("30.00"), data_operacao=D1, taxas_compra=Decimal("1.25"),
        )
        sincronizar_lancamento_compra(lote)
        lancamento = obter_ou_criar_conta_corrente(usuario).lancamentos.get(origem=LancamentoContaCorrente.ORIGEM_COMPRA)
        self.assertEqual(lancamento.valor, Decimal("301.25"))


class MigracaoVendasLoteTests(TestCase):
    """A migração cria um VendaLote para cada lote já vendido e liga o lançamento de venda existente a ele."""

    def test_cria_venda_e_liga_lancamento(self):
        usuario = User.objects.create_user(username="dados_antigos", password="SenhaForte123!")
        lote = Operacao.objects.create(
            usuario=usuario, ativo=Ativo.objects.create(ticker="BBAS3"), tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("20.00"), data_operacao=D1,
            quantidade_vendida=6, preco_venda=Decimal("25.00"), data_venda=D2, percentual_corretora=Decimal("0.50"),
        )
        lancamento = LancamentoContaCorrente.objects.create(
            conta=obter_ou_criar_conta_corrente(usuario), tipo=LancamentoContaCorrente.CREDITO,
            origem=LancamentoContaCorrente.ORIGEM_VENDA, valor=Decimal("149.25"),
            descricao="Venda de 6x BBAS3", data=D2, operacao=lote,
        )

        migracao = importlib.import_module("core.migrations.0028_preenche_classe_e_vendas_lote")
        migracao.preencher(django_apps, None)

        venda = lote.vendas.get()
        self.assertEqual((venda.quantidade, venda.preco, venda.data), (6, Decimal("25.00"), D2))
        self.assertEqual(venda.percentual_corretora, Decimal("0.50"))
        lancamento.refresh_from_db()
        self.assertEqual(lancamento.venda_lote, venda)

        migracao.preencher(django_apps, None)  # rodar de novo não duplica
        self.assertEqual(lote.vendas.count(), 1)


class PosicaoFiscalViewTests(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_user(username="tela_fiscal", password="SenhaForte123!")
        self.client.login(username="tela_fiscal", password="SenhaForte123!")

    def test_exige_login(self):
        self.client.logout()
        resposta = self.client.get(reverse("tributario:posicao_fiscal"))
        self.assertEqual(resposta.status_code, 302)

    def test_mostra_posicao_e_resultado_do_ano(self):
        ativo = Ativo.objects.create(ticker="WEGE3")
        lote = Operacao.objects.create(
            usuario=self.usuario, ativo=ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("40.00"), data_operacao=D1,
        )
        VendaLote.objects.create(operacao=lote, quantidade=4, preco=Decimal("50.00"), data=D1)

        resposta = self.client.get(reverse("tributario:posicao_fiscal"), {"ano": "2026"})
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "WEGE3")
        self.assertContains(resposta, "Day trade")
        self.assertContains(resposta, "R$ 240,00")  # custo dos 6 que sobraram

    def test_sem_operacoes_nao_quebra(self):
        resposta = self.client.get(reverse("tributario:posicao_fiscal"), {"ano": "abc"})
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Nenhum ativo em carteira")

    def test_menu_tem_link(self):
        self.assertContains(self.client.get(reverse("core:menu")), reverse("tributario:posicao_fiscal"))
