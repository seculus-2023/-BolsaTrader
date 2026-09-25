"""
Testes automatizados do BolsaTrader.

Executar com:
    python manage.py test
"""
import hashlib
import hmac
import json
import os
import sys
import threading
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from io import StringIO
from unittest.mock import Mock, patch

import requests
from django.apps import apps as django_apps
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import PerfilUsuario

from . import services as core_services
from .forms import OperacaoForm, VendaLoteForm
from .models import (
    Ativo, Cotacao, Operacao, Alerta, MensagemWhatsapp, CotacaoIndice, RegistroAtualizacaoCarteira,
    ContaCorrente, LancamentoContaCorrente, PostIt,
)
from .services import (
    calcular_posicoes, analisar_tendencia, gerar_alertas_para_usuario, construir_comparativo_valores,
    analisar_indicadores_tecnicos, analisar_indicadores_tecnicos_precos,
    buscar_historico_precos, backfill_historico_cotacoes, backtest_sinais_robo,
    buscar_historico_cdi, calcular_comparativo_benchmark, _retorno_indice_periodo, _retorno_cdi_periodo,
    calcular_concentracao_setor, calcular_metricas_risco,
    enviar_whatsapp, whatsapp_envio_configurado,
    registrar_atualizacao_carteira, excluir_registros_atualizacao_antigos,
    excluir_registros_atualizacao_por_periodo, construir_grafico_atualizacoes_dia,
    calcular_variacoes_historico, executar_ciclo_atualizacao_cotacoes, iniciar_agendador_cotacoes_embutido,
    buscar_cotacoes_em_lote, atualizar_cotacoes_ativos, buscar_cotacao_atual_com_historico,
    _ciclo_agendador_embutido, _mensagem_falha_api,
    buscar_cotacao_atual,
    calcular_rsi,
    BrapiError,
    obter_ou_criar_conta_corrente, saldo_conta_corrente, sincronizar_lancamento_compra,
    sincronizar_lancamento_venda, registrar_transferencia_conta_corrente, extrato_conta_corrente,
    gerar_excel_extrato_conta_corrente, gerar_pdf_extrato_conta_corrente,
    calcular_medias_moveis, _classificar_medias_moveis, analisar_volume_precos,
    _classificar_volatilidade, _veredito_scanner, escanear_ativo_precos, escanear_ativo,
    escanear_carteira,
    calcular_atr, calcular_suporte_resistencia, _classificar_suporte_resistencia, sugerir_plano_trade,
    obter_post_it, salvar_post_it,
    ultimo_valor_ibovespa,
    ia_configurada, gerar_analise_b3_ia, _prompt_analise_b3_ia, _parsear_resposta_ia_em_grade, IAError,
)


class OperacaoFormTests(TestCase):
    """
    O campo data_operacao usa <input type='date'>, que exige o valor no formato
    ISO (yyyy-MM-dd) - independente do idioma. Com LANGUAGE_CODE='pt-br', sem
    forçar format='%Y-%m-%d' no widget, o Django renderizava a data em dd/mm/yyyy
    e o navegador rejeitava o valor, deixando o campo "data de hoje" em branco.
    """

    def test_data_operacao_renderiza_em_formato_iso_compativel_com_input_date(self):
        form = OperacaoForm()
        html = str(form["data_operacao"])
        hoje_iso = date.today().isoformat()
        self.assertIn(f'value="{hoje_iso}"', html)

    def test_data_operacao_aceita_valor_iso_submetido_pelo_navegador(self):
        form = OperacaoForm(data={
            "ticker": "PETR4", "tipo": Operacao.COMPRA, "quantidade": "10",
            "preco_unitario": "30.00", "data_operacao": date.today().isoformat(),
            "meta_lucro_pct": "", "meta_perda_pct": "", "observacao": "",
        })
        self.assertTrue(form.is_valid(), form.errors)


class OperacaoTipoReservarTests(TestCase):
    def _dados(self, tipo, quantidade):
        return {
            "ticker": "PETR4", "tipo": tipo, "quantidade": str(quantidade),
            "preco_unitario": "30.00", "data_operacao": date.today().isoformat(),
            "meta_lucro_pct": "", "meta_perda_pct": "", "observacao": "",
        }

    def test_reservar_com_quantidade_1_e_valido(self):
        form = OperacaoForm(data=self._dados(Operacao.RESERVAR, 1))
        self.assertTrue(form.is_valid(), form.errors)

    def test_reservar_com_quantidade_maior_que_1_e_invalido(self):
        form = OperacaoForm(data=self._dados(Operacao.RESERVAR, 5))
        self.assertFalse(form.is_valid())
        self.assertIn("quantidade", form.errors)

    def test_compra_com_quantidade_maior_que_1_continua_valida(self):
        form = OperacaoForm(data=self._dados(Operacao.COMPRA, 100))
        self.assertTrue(form.is_valid(), form.errors)

    def test_reservar_nao_altera_quantidade_nem_custo_da_posicao(self):
        usuario = User.objects.create_user(username="investidor_reserva", password="SenhaForte123!")
        ativo = Ativo.objects.create(ticker="MGLU3")
        Operacao.objects.create(
            usuario=usuario, ativo=ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("5.00"), data_operacao=date.today(),
        )
        Operacao.objects.create(
            usuario=usuario, ativo=ativo, tipo=Operacao.RESERVAR,
            quantidade=1, preco_unitario=Decimal("5.50"), data_operacao=date.today(),
        )
        posicao = calcular_posicoes(usuario)[0]
        self.assertEqual(posicao.quantidade, 10)  # a reserva não entra na posição
        self.assertEqual(posicao.valor_investido, Decimal("50.00"))
        self.assertEqual(posicao.quantidade_reservada, 1)  # mas fica marcada na mesma posição
        self.assertFalse(posicao.apenas_reservado)

    def test_ativo_so_com_reserva_aparece_marcado_como_apenas_reservado(self):
        usuario = User.objects.create_user(username="investidor_so_reserva", password="SenhaForte123!")
        ativo = Ativo.objects.create(ticker="ITSA4")
        Operacao.objects.create(
            usuario=usuario, ativo=ativo, tipo=Operacao.RESERVAR,
            quantidade=1, preco_unitario=Decimal("9.80"), data_operacao=date.today(),
        )
        posicoes = calcular_posicoes(usuario)
        self.assertEqual(len(posicoes), 1)  # antes da reserva "destacada", isso não aparecia
        posicao = posicoes[0]
        self.assertTrue(posicao.apenas_reservado)
        self.assertEqual(posicao.quantidade, 0)
        self.assertEqual(posicao.quantidade_reservada, 1)
        self.assertEqual(posicao.valor_investido, Decimal("0.00"))
        self.assertEqual(posicao.preco_medio, Decimal("9.80"))  # preço pretendido na reserva

    @patch("core.views.mercado_b3_aberto", return_value=True)
    def test_atualizar_cotacoes_agora_inclui_ativo_so_com_reserva(self, mock_mercado_aberto):
        # sem isso, o teste só passa se rodar durante o horário de pregão de
        # verdade - "Atualizar cotações agora" recusa fora do horário (ver
        # core.views.atualizar_cotacoes_agora).
        usuario = User.objects.create_user(username="investidor_cotacao_reserva", password="SenhaForte123!")
        self.client.login(username="investidor_cotacao_reserva", password="SenhaForte123!")
        ativo = Ativo.objects.create(ticker="CSMG3")
        Operacao.objects.create(
            usuario=usuario, ativo=ativo, tipo=Operacao.RESERVAR,
            quantidade=1, preco_unitario=Decimal("16.00"), data_operacao=date.today(),
        )
        with patch("core.services.requests.get") as mock_get:
            mock_get.return_value.raise_for_status = lambda: None
            mock_get.return_value.json = lambda: {
                "results": [{"symbol": "CSMG3", "regularMarketPrice": 16.5, "regularMarketChangePercent": 1.1}]
            }
            self.client.get(reverse("core:atualizar_cotacoes"))
        self.assertTrue(Cotacao.objects.filter(ativo=ativo).exists())


class VendaValidacaoELucroTests(TestCase):
    """Vender um lote não pode passar da quantidade comprada NAQUELE lote, e calcula lucro/perda realizado."""

    def setUp(self):
        self.usuario = User.objects.create_user(username="investidor_venda", password="SenhaForte123!")
        self.client.login(username="investidor_venda", password="SenhaForte123!")
        self.ativo = Ativo.objects.create(ticker="PETR4")

    def _compra(self, quantidade=10, preco="30.00", dias_atras=0):
        return Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=quantidade, preco_unitario=Decimal(preco),
            data_operacao=date.today() - timedelta(days=dias_atras),
        )

    def _dados_venda(self, quantidade, preco="40.00"):
        return {
            "quantidade_vendida": str(quantidade), "preco_venda": preco,
            "data_venda": date.today().isoformat(),
        }

    def test_form_rejeita_venda_maior_que_a_quantidade_do_lote(self):
        compra = self._compra(quantidade=10)
        form = VendaLoteForm(data=self._dados_venda(11), instance=compra)
        self.assertFalse(form.is_valid())
        self.assertIn("quantidade_vendida", form.errors)
        self.assertIn("10 unidade(s)", form.errors["quantidade_vendida"][0])

    def test_form_aceita_venda_de_exatamente_o_que_foi_comprado(self):
        compra = self._compra(quantidade=10)
        form = VendaLoteForm(data=self._dados_venda(10), instance=compra)
        self.assertTrue(form.is_valid(), form.errors)

    def test_form_exige_preco_de_venda_quando_ha_quantidade_vendida(self):
        compra = self._compra(quantidade=10)
        form = VendaLoteForm(
            data={"quantidade_vendida": "5", "preco_venda": "", "data_venda": date.today().isoformat()},
            instance=compra,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("preco_venda", form.errors)

    def test_view_bloqueia_venda_maior_que_o_lote_e_nao_altera_a_operacao(self):
        compra = self._compra(quantidade=5)
        resposta = self.client.post(
            reverse("core:operacao_vender", args=[compra.id]), self._dados_venda(6)
        )
        self.assertEqual(resposta.status_code, 200)  # re-renderiza o form com erro, não redireciona
        compra.refresh_from_db()
        self.assertEqual(compra.quantidade_vendida, 0)

    def test_view_bloqueia_venda_de_lote_de_outro_usuario(self):
        outro = User.objects.create_user(username="outro_investidor", password="SenhaForte123!")
        compra_de_outro = Operacao.objects.create(
            usuario=outro, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("30.00"), data_operacao=date.today(),
        )
        resposta = self.client.post(
            reverse("core:operacao_vender", args=[compra_de_outro.id]), self._dados_venda(5)
        )
        self.assertEqual(resposta.status_code, 404)

    def test_venda_com_lucro_calcula_valor_e_percentual_corretos(self):
        compra = self._compra(quantidade=10, preco="30.00")
        self.client.post(reverse("core:operacao_vender", args=[compra.id]), self._dados_venda(10, preco="40.00"))
        compra.refresh_from_db()
        self.assertEqual(compra.saldo, 0)
        self.assertEqual(compra.lucro_perda_realizado, Decimal("100.00"))  # (40-30) * 10
        self.assertEqual(compra.lucro_perda_pct_realizado, Decimal("33.33"))  # 100 / 300 * 100

    def test_venda_com_perda_calcula_valor_negativo(self):
        compra = self._compra(quantidade=10, preco="30.00")
        self.client.post(reverse("core:operacao_vender", args=[compra.id]), self._dados_venda(10, preco="25.00"))
        compra.refresh_from_db()
        self.assertEqual(compra.lucro_perda_realizado, Decimal("-50.00"))  # (25-30) * 10

    def test_venda_parcial_deixa_saldo_e_lucro_referentes_so_a_parte_vendida(self):
        compra = self._compra(quantidade=10, preco="20.00")
        self.client.post(reverse("core:operacao_vender", args=[compra.id]), self._dados_venda(4, preco="30.00"))
        compra.refresh_from_db()
        self.assertEqual(compra.saldo, 6)
        self.assertEqual(compra.lucro_perda_realizado, Decimal("40.00"))  # (30-20) * 4

    def test_compra_recem_criada_nao_preenche_lucro_perda_realizado(self):
        compra = self._compra(quantidade=5, preco="60.00")
        self.assertIsNone(compra.lucro_perda_realizado)
        self.assertIsNone(compra.lucro_perda_pct_realizado)
        self.assertEqual(compra.saldo, 5)


class OperacaoModelTests(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_user(username="investidor_op", password="SenhaForte123!")
        self.ativo = Ativo.objects.create(ticker="BBAS3")

    def test_dias_desde_operacao_feita_hoje(self):
        op = Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("30.00"), data_operacao=date.today(),
        )
        self.assertEqual(op.dias_desde_operacao, 0)

    def test_dias_desde_operacao_feita_no_passado(self):
        op = Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("30.00"), data_operacao=date.today() - timedelta(days=7),
        )
        self.assertEqual(op.dias_desde_operacao, 7)

    def test_lista_de_operacoes_mostra_dias_na_pagina(self):
        self.client.login(username="investidor_op", password="SenhaForte123!")
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("30.00"), data_operacao=date.today() - timedelta(days=5),
        )
        # data_de/data_ate vazios de propósito: sem eles, a página aplica o
        # filtro padrão (mês atual), e "5 dias atrás" pode cair no mês
        # anterior perto da virada do mês - aqui queremos ver tudo.
        resposta = self.client.get(reverse("core:operacao_lista"), {"data_de": "", "data_ate": ""})
        self.assertContains(resposta, "5 dias")

    def test_valor_total_e_quantidade_vezes_preco_unitario(self):
        op = Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("30.00"), data_operacao=date.today(),
        )
        self.assertEqual(op.valor_total, Decimal("300.00"))

    def test_valor_total_vendido_e_none_sem_venda(self):
        op = Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("30.00"), data_operacao=date.today(),
        )
        self.assertIsNone(op.valor_total_vendido)

    def test_valor_total_vendido_e_preco_venda_vezes_quantidade_vendida(self):
        op = Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("30.00"), data_operacao=date.today(),
            quantidade_vendida=4, preco_venda=Decimal("35.00"), data_venda=date.today(),
        )
        self.assertEqual(op.valor_total_vendido, Decimal("140.00"))

    def test_lista_de_operacoes_mostra_total_compra_e_total_venda(self):
        self.client.login(username="investidor_op", password="SenhaForte123!")
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("30.00"), data_operacao=date.today(),
            quantidade_vendida=4, preco_venda=Decimal("35.00"), data_venda=date.today(),
        )
        resposta = self.client.get(reverse("core:operacao_lista"))
        self.assertContains(resposta, "Total compra")
        self.assertContains(resposta, "Total venda")
        self.assertContains(resposta, "R$ 300,00")  # total compra: 10 * 30
        self.assertContains(resposta, "R$ 140,00")  # total venda: 4 * 35


class ResumoVendasTests(TestCase):
    """Resumo (compra/venda/lucro) na tela de operações, considerando só lotes com venda."""

    def setUp(self):
        self.usuario = User.objects.create_user(username="investidor_resumo", password="SenhaForte123!")
        self.client.login(username="investidor_resumo", password="SenhaForte123!")
        self.ativo = Ativo.objects.create(ticker="ITUB4")

    def test_sem_nenhuma_venda_nao_mostra_resumo(self):
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("30.00"), data_operacao=date.today(),
        )
        resposta = self.client.get(reverse("core:operacao_lista"))
        self.assertNotContains(resposta, "Resumo das vendas")

    def test_reserva_nao_conta_no_resumo(self):
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.RESERVAR,
            quantidade=1, preco_unitario=Decimal("30.00"), data_operacao=date.today(),
        )
        resposta = self.client.get(reverse("core:operacao_lista"))
        self.assertNotContains(resposta, "Resumo das vendas")

    def test_resumo_soma_apenas_lotes_vendidos_ignorando_nao_vendidos(self):
        # lote não vendido - não deve entrar na soma
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=100, preco_unitario=Decimal("50.00"), data_operacao=date.today(),
        )
        # lote 1: comprou 10 a 20, vendeu 10 a 25 -> lucro 50
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("20.00"), data_operacao=date.today(),
            quantidade_vendida=10, preco_venda=Decimal("25.00"), data_venda=date.today(),
        )
        # lote 2: comprou 5 a 30, vendeu 5 a 28 -> perda -10
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=5, preco_unitario=Decimal("30.00"), data_operacao=date.today(),
            quantidade_vendida=5, preco_venda=Decimal("28.00"), data_venda=date.today(),
        )
        resposta = self.client.get(reverse("core:operacao_lista"))
        self.assertContains(resposta, "Resumo das vendas")
        resumo = resposta.context["resumo_vendas"]
        self.assertEqual(resumo["total_comprado"], Decimal("350.00"))  # 10*20 + 5*30
        self.assertEqual(resumo["total_vendido"], Decimal("390.00"))  # 10*25 + 5*28
        self.assertEqual(resumo["lucro_total"], Decimal("40.00"))  # 50 - 10
        self.assertContains(resposta, "R$ 350,00")
        self.assertContains(resposta, "R$ 390,00")
        self.assertContains(resposta, "R$ 40,00")

    def test_venda_parcial_soma_so_a_parte_vendida(self):
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("10.00"), data_operacao=date.today(),
            quantidade_vendida=4, preco_venda=Decimal("15.00"), data_venda=date.today(),
        )
        resposta = self.client.get(reverse("core:operacao_lista"))
        resumo = resposta.context["resumo_vendas"]
        self.assertEqual(resumo["total_comprado"], Decimal("40.00"))  # 4*10, não 10*10
        self.assertEqual(resumo["total_vendido"], Decimal("60.00"))  # 4*15
        self.assertEqual(resumo["lucro_total"], Decimal("20.00"))


class FluxoAutenticacaoTests(TestCase):
    def test_cadastro_cria_usuario_e_loga_automaticamente(self):
        resposta = self.client.post(reverse("accounts:cadastro"), {
            "username": "investidor1",
            "first_name": "Ana",
            "email": "ana@example.com",
            "password1": "SenhaForte123!",
            "password2": "SenhaForte123!",
        })
        self.assertEqual(resposta.status_code, 302)
        self.assertTrue(User.objects.filter(username="investidor1").exists())

    def test_login_com_credenciais_validas(self):
        User.objects.create_user(username="investidor2", password="SenhaForte123!")
        resposta = self.client.post(reverse("accounts:login"), {
            "username": "investidor2",
            "password": "SenhaForte123!",
        })
        self.assertEqual(resposta.status_code, 302)

    def test_dashboard_exige_login(self):
        resposta = self.client.get(reverse("core:dashboard"))
        self.assertEqual(resposta.status_code, 302)  # redireciona para login

    def test_logout_encerra_sessao_e_redireciona_para_login(self):
        # Django 5 exige POST para logout (LogoutView não aceita mais GET) - o
        # botão "Sair" precisa enviar um form POST, não ser um link <a href>.
        User.objects.create_user(username="investidor6", password="SenhaForte123!")
        self.client.login(username="investidor6", password="SenhaForte123!")

        resposta = self.client.post(reverse("accounts:logout"))
        self.assertRedirects(resposta, reverse("accounts:login"))

    def test_usuario_logado_ve_link_cadastrar_usuario_no_menu(self):
        User.objects.create_user(username="investidor7", password="SenhaForte123!")
        self.client.login(username="investidor7", password="SenhaForte123!")
        resposta = self.client.get(reverse("core:dashboard"))
        self.assertContains(resposta, reverse("accounts:cadastro"))
        self.assertContains(resposta, "Cadastrar Usuário")

    def test_usuario_logado_acessa_tela_de_cadastro_sem_ser_redirecionado(self):
        User.objects.create_user(username="investidor8", password="SenhaForte123!")
        self.client.login(username="investidor8", password="SenhaForte123!")
        resposta = self.client.get(reverse("accounts:cadastro"))
        self.assertEqual(resposta.status_code, 200)

    def test_usuario_logado_cadastra_outro_usuario_sem_perder_a_propria_sessao(self):
        admin = User.objects.create_user(username="investidor9", password="SenhaForte123!")
        self.client.login(username="investidor9", password="SenhaForte123!")

        resposta = self.client.post(reverse("accounts:cadastro"), {
            "username": "novo_investidor",
            "first_name": "Bruno",
            "email": "bruno@example.com",
            "password1": "SenhaForte123!",
            "password2": "SenhaForte123!",
        })
        self.assertRedirects(resposta, reverse("accounts:cadastro"))
        self.assertTrue(User.objects.filter(username="novo_investidor").exists())

        # a sessão continua sendo a de quem estava logado, não a do usuário recém-criado
        resposta_dashboard = self.client.get(reverse("core:dashboard"))
        self.assertEqual(resposta_dashboard.wsgi_request.user, admin)


class IsolamentoEntreUsuariosTests(TestCase):
    """Cada usuário só pode ver/gerenciar suas próprias operações, posições e alertas."""

    def setUp(self):
        self.ana = User.objects.create_user(username="ana", password="SenhaForte123!")
        self.bruno = User.objects.create_user(username="bruno", password="SenhaForte123!")
        self.ativo = Ativo.objects.create(ticker="PETR4")
        self.ativo_bruno = Ativo.objects.create(ticker="VALE3")

        self.operacao_ana = Operacao.objects.create(
            usuario=self.ana, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=100, preco_unitario=Decimal("30.00"),
        )
        self.operacao_bruno = Operacao.objects.create(
            usuario=self.bruno, ativo=self.ativo_bruno, tipo=Operacao.COMPRA,
            quantidade=50, preco_unitario=Decimal("31.00"),
        )
        self.alerta_bruno = Alerta.objects.create(
            usuario=self.bruno, tipo=Alerta.LEMBRETE, mensagem="Alerta do Bruno",
        )

    def test_lista_de_operacoes_mostra_apenas_as_do_usuario_logado(self):
        self.client.login(username="ana", password="SenhaForte123!")
        resposta = self.client.get(reverse("core:operacao_lista"))
        operacoes = list(resposta.context["operacoes"])
        self.assertIn(self.operacao_ana, operacoes)
        self.assertNotIn(self.operacao_bruno, operacoes)

    def test_posicoes_nao_misturam_carteiras_de_usuarios_diferentes(self):
        posicoes_ana = calcular_posicoes(self.ana)
        posicoes_bruno = calcular_posicoes(self.bruno)
        self.assertEqual(posicoes_ana[0].quantidade, 100)
        self.assertEqual(posicoes_bruno[0].quantidade, 50)

    def test_usuario_nao_acessa_alerta_de_outro_usuario(self):
        self.client.login(username="ana", password="SenhaForte123!")
        resposta = self.client.get(
            reverse("core:alerta_marcar_lido", args=[self.alerta_bruno.id])
        )
        self.assertEqual(resposta.status_code, 404)
        self.alerta_bruno.refresh_from_db()
        self.assertFalse(self.alerta_bruno.lido)  # continua não lido, não foi alterado

    def test_atividade_recente_mostra_nome_mas_nao_detalhes_da_operacao_alheia(self):
        # o mural "Atividade da comunidade" mudou do Painel de Controle para
        # Histórico de Atualizações (ver core.views.historico_atualizacoes).
        self.client.login(username="ana", password="SenhaForte123!")
        resposta = self.client.get(reverse("core:historico_atualizacoes"))
        conteudo = resposta.content.decode()
        # o nome de quem operou aparece no mural...
        self.assertIn("bruno", conteudo)
        # ...mas o ticker da operação do Bruno não é exposto para a Ana
        self.assertNotIn("VALE3", conteudo)


class CalculoPosicoesTests(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_user(username="investidor3", password="SenhaForte123!")
        self.ativo = Ativo.objects.create(ticker="VALE3", nome="Vale ON")

    def test_posicao_em_lucro(self):
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=50, preco_unitario=Decimal("60.00"), data_operacao=date.today(),
        )
        Cotacao.objects.create(ativo=self.ativo, data=date.today(), preco_fechamento=Decimal("66.00"))

        posicoes = calcular_posicoes(self.usuario)
        self.assertEqual(len(posicoes), 1)
        posicao = posicoes[0]
        self.assertEqual(posicao.quantidade, 50)
        self.assertEqual(posicao.situacao, "LUCRO")
        self.assertGreater(posicao.lucro_perda_pct, 0)

    def test_posicao_em_perda(self):
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=50, preco_unitario=Decimal("60.00"), data_operacao=date.today(),
        )
        Cotacao.objects.create(ativo=self.ativo, data=date.today(), preco_fechamento=Decimal("54.00"))

        posicoes = calcular_posicoes(self.usuario)
        self.assertEqual(posicoes[0].situacao, "PERDA")

    def test_venda_reduz_posicao(self):
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=100, preco_unitario=Decimal("60.00"), data_operacao=date.today(),
            quantidade_vendida=40, preco_venda=Decimal("65.00"), data_venda=date.today(),
        )
        posicoes = calcular_posicoes(self.usuario)
        self.assertEqual(posicoes[0].quantidade, 60)

    def test_posicao_zerada_nao_aparece(self):
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("60.00"), data_operacao=date.today(),
            quantidade_vendida=10, preco_venda=Decimal("65.00"), data_venda=date.today(),
        )
        posicoes = calcular_posicoes(self.usuario)
        self.assertEqual(len(posicoes), 0)

    def test_dias_desde_compra_conta_a_partir_da_primeira_compra(self):
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=50, preco_unitario=Decimal("60.00"), data_operacao=date.today() - timedelta(days=10),
        )
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=20, preco_unitario=Decimal("62.00"), data_operacao=date.today() - timedelta(days=3),
        )
        posicao = calcular_posicoes(self.usuario)[0]
        self.assertEqual(posicao.dias_desde_compra, 10)  # conta desde a compra mais antiga, não a mais recente

    def test_dias_desde_compra_reinicia_apos_posicao_ser_zerada(self):
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("60.00"), data_operacao=date.today() - timedelta(days=30),
            quantidade_vendida=10, preco_venda=Decimal("65.00"),
            data_venda=date.today() - timedelta(days=20),
        )
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=5, preco_unitario=Decimal("58.00"), data_operacao=date.today() - timedelta(days=4),
        )
        posicao = calcular_posicoes(self.usuario)[0]
        self.assertEqual(posicao.dias_desde_compra, 4)  # não deve contar a partir da compra zerada anterior

    def test_dias_desde_compra_hoje_retorna_zero(self):
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("60.00"), data_operacao=date.today(),
        )
        posicao = calcular_posicoes(self.usuario)[0]
        self.assertEqual(posicao.dias_desde_compra, 0)

    def test_lucro_perda_por_dia_divide_pelos_dias_em_carteira(self):
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("60.00"), data_operacao=date.today() - timedelta(days=10),
        )
        Cotacao.objects.create(ativo=self.ativo, data=date.today(), preco_fechamento=Decimal("70.00"))

        posicao = calcular_posicoes(self.usuario)[0]

        self.assertEqual(posicao.lucro_perda_valor, Decimal("100.00"))
        self.assertEqual(posicao.lucro_perda_por_dia_valor, Decimal("10.00"))
        self.assertEqual(posicao.lucro_perda_pct, Decimal("16.67"))
        self.assertEqual(posicao.lucro_perda_por_dia_pct, Decimal("1.67"))

    def test_lucro_perda_por_dia_none_quando_comprado_hoje(self):
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("60.00"), data_operacao=date.today(),
        )
        Cotacao.objects.create(ativo=self.ativo, data=date.today(), preco_fechamento=Decimal("70.00"))

        posicao = calcular_posicoes(self.usuario)[0]

        self.assertIsNone(posicao.lucro_perda_por_dia_valor)
        self.assertIsNone(posicao.lucro_perda_por_dia_pct)

    def test_lucro_perda_por_dia_none_sem_cotacao(self):
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("60.00"), data_operacao=date.today() - timedelta(days=5),
        )
        posicao = calcular_posicoes(self.usuario)[0]
        self.assertIsNone(posicao.lucro_perda_por_dia_valor)

    def test_posicoes_ordenadas_por_dias_em_carteira_crescente(self):
        # VALE3 (self.ativo): comprado há 5 dias
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("60.00"), data_operacao=date.today() - timedelta(days=5),
        )

        # PETR4: comprado hoje - menos dias em carteira, deve vir primeiro
        petr4 = Ativo.objects.create(ticker="PETR4")
        Operacao.objects.create(
            usuario=self.usuario, ativo=petr4, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("30.00"), data_operacao=date.today(),
        )

        # ITUB4: comprado há 10 dias - mais dias em carteira entre os comprados
        itub4 = Ativo.objects.create(ticker="ITUB4")
        Operacao.objects.create(
            usuario=self.usuario, ativo=itub4, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("30.00"), data_operacao=date.today() - timedelta(days=10),
        )

        # BBAS3: só reserva, sem "dias em carteira" pra comparar - deve ficar no final de tudo
        bbas3 = Ativo.objects.create(ticker="BBAS3")
        Operacao.objects.create(
            usuario=self.usuario, ativo=bbas3, tipo=Operacao.RESERVAR,
            quantidade=1, preco_unitario=Decimal("25.00"), data_operacao=date.today(),
        )

        tickers = [p.ativo.ticker for p in calcular_posicoes(self.usuario)]
        self.assertEqual(tickers, ["PETR4", "VALE3", "ITUB4", "BBAS3"])


class ComparativoValoresTests(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_user(username="investidor_comparativo", password="SenhaForte123!")
        self.ativo = Ativo.objects.create(ticker="TAEE11")

    def _duas_cotacoes(self, ativo, preco_ontem, preco_hoje):
        Cotacao.objects.create(
            ativo=ativo, data=date.today() - timedelta(days=1), preco_fechamento=Decimal(preco_ontem),
        )
        Cotacao.objects.create(ativo=ativo, data=date.today(), preco_fechamento=Decimal(preco_hoje))

    def test_sem_posicoes_retorna_lista_vazia(self):
        self.assertEqual(construir_comparativo_valores([]), [])

    def test_apenas_reservado_nao_entra_no_grafico(self):
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.RESERVAR,
            quantidade=1, preco_unitario=Decimal("35.00"), data_operacao=date.today(),
        )
        posicoes = calcular_posicoes(self.usuario)
        self.assertEqual(construir_comparativo_valores(posicoes), [])  # só reserva, sem compra real

    def test_posicao_com_menos_de_2_cotacoes_nao_entra_no_grafico(self):
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("35.00"), data_operacao=date.today(),
        )
        Cotacao.objects.create(ativo=self.ativo, data=date.today(), preco_fechamento=Decimal("40.00"))
        posicoes = calcular_posicoes(self.usuario)
        self.assertEqual(construir_comparativo_valores(posicoes), [])  # precisa de pelo menos 2 pregões

    def test_posicao_comprada_com_historico_gera_grafico_de_duas_linhas(self):
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("35.00"), data_operacao=date.today() - timedelta(days=1),
        )
        self._duas_cotacoes(self.ativo, "38.00", "40.00")
        posicoes = calcular_posicoes(self.usuario)
        graficos = construir_comparativo_valores(posicoes)
        self.assertEqual(len(graficos), 1)
        grafico = graficos[0]
        self.assertEqual(grafico["ativo"].ticker, "TAEE11")
        self.assertTrue(grafico["ganho_atual"])  # 400 (10x40) > 350 (investido), valorizou
        self.assertEqual(len(grafico["pontos"]), 2)  # um ponto por pregão no histórico
        self.assertIn("R$ 350,00", grafico["pontos"][-1]["valor_compra_label"])
        self.assertIn("R$ 400,00", grafico["pontos"][-1]["valor_atual_label"])
        self.assertIn("M", grafico["path_compra"])  # linha de compra: horizontal, constante
        self.assertIn("M", grafico["path_atual"])

    def test_posicao_com_perda_marca_grafico_como_perda(self):
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("35.00"), data_operacao=date.today() - timedelta(days=1),
        )
        self._duas_cotacoes(self.ativo, "32.00", "30.00")
        posicoes = calcular_posicoes(self.usuario)
        graficos = construir_comparativo_valores(posicoes)
        self.assertFalse(graficos[0]["ganho_atual"])
        self.assertFalse(graficos[0]["pontos"][-1]["ganho"])

    def test_pagina_posicoes_renderiza_grafico_comparativo(self):
        self.client.login(username="investidor_comparativo", password="SenhaForte123!")
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("35.00"), data_operacao=date.today() - timedelta(days=1),
        )
        self._duas_cotacoes(self.ativo, "38.00", "40.00")
        resposta = self.client.get(reverse("core:posicoes"))
        self.assertContains(resposta, "grafico-comparativo")
        self.assertContains(resposta, "TAEE11")

    def test_linha_svg_nao_tem_virgula_decimal_do_locale_pt_br(self):
        # regressão: mesmo bug do gráfico de histórico - {{ valor }} de um float vira
        # "97,98" em vez de "97.98" com LANGUAGE_CODE=pt-br, quebrando o <circle>/<path> do SVG.
        self.client.login(username="investidor_comparativo", password="SenhaForte123!")
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=7, preco_unitario=Decimal("35.37"), data_operacao=date.today() - timedelta(days=1),
        )
        self._duas_cotacoes(self.ativo, "39.11", "41.83")
        resposta = self.client.get(reverse("core:posicoes"))
        conteudo = resposta.content.decode()
        self.assertRegex(conteudo, r'<circle cx="[\d.]+" cy="[\d.]+"')
        self.assertNotRegex(conteudo, r'<circle cx="\d+,\d')
        self.assertNotRegex(conteudo, r'cy="\d+,\d')


class ExportacaoPosicoesTests(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_user(username="investidor_export", password="SenhaForte123!")
        self.client.login(username="investidor_export", password="SenhaForte123!")
        self.ativo = Ativo.objects.create(ticker="WEGE3", nome="WEG ON")
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=20, preco_unitario=Decimal("40.00"), data_operacao=date.today(),
        )
        Cotacao.objects.create(ativo=self.ativo, data=date.today(), preco_fechamento=Decimal("44.00"))

    def test_exportar_excel_retorna_arquivo_xlsx_valido(self):
        resposta = self.client.get(reverse("core:posicoes_exportar_excel"))
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(
            resposta["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertIn("attachment", resposta["Content-Disposition"])
        self.assertIn(".xlsx", resposta["Content-Disposition"])
        self.assertTrue(resposta.content.startswith(b"PK"))  # assinatura de arquivo .xlsx (zip)

    def test_exportar_excel_contem_dados_da_posicao(self):
        from openpyxl import load_workbook
        import io

        resposta = self.client.get(reverse("core:posicoes_exportar_excel"))
        wb = load_workbook(io.BytesIO(resposta.content))
        ws = wb.active
        linhas = list(ws.values)
        self.assertEqual(linhas[0][0], "Ativo")
        self.assertEqual(linhas[1][0], "WEGE3")
        self.assertEqual(linhas[1][1], 20)

    def test_exportar_pdf_retorna_arquivo_pdf_valido(self):
        resposta = self.client.get(reverse("core:posicoes_exportar_pdf"))
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta["Content-Type"], "application/pdf")
        self.assertIn("attachment", resposta["Content-Disposition"])
        self.assertIn(".pdf", resposta["Content-Disposition"])
        self.assertTrue(resposta.content.startswith(b"%PDF"))

    def test_exportar_sem_posicoes_nao_quebra(self):
        Operacao.objects.filter(usuario=self.usuario).delete()
        resposta_excel = self.client.get(reverse("core:posicoes_exportar_excel"))
        resposta_pdf = self.client.get(reverse("core:posicoes_exportar_pdf"))
        self.assertEqual(resposta_excel.status_code, 200)
        self.assertEqual(resposta_pdf.status_code, 200)

    def test_exportacao_exige_login(self):
        self.client.logout()
        resposta = self.client.get(reverse("core:posicoes_exportar_excel"))
        self.assertEqual(resposta.status_code, 302)

    def test_exportar_excel_inclui_abas_de_analise(self):
        from openpyxl import load_workbook
        import io

        resposta = self.client.get(reverse("core:posicoes_exportar_excel"))
        wb = load_workbook(io.BytesIO(resposta.content))
        self.assertEqual(
            wb.sheetnames,
            ["Posições", "Carteira x Mercado", "Concentração por setor", "Indicadores de risco"],
        )

        ws_setor = wb["Concentração por setor"]
        linhas_setor = list(ws_setor.values)
        self.assertEqual(linhas_setor[0], ("Setor", "Valor (R$)", "% da carteira"))
        self.assertEqual(linhas_setor[1][0], "Sem setor")  # ativo de teste não tem setor cadastrado

        ws_risco = wb["Indicadores de risco"]
        linhas_risco = list(ws_risco.values)
        self.assertIn("WEGE3", [row[0] for row in linhas_risco])

    def test_exportar_pdf_inclui_secoes_de_analise(self):
        from pypdf import PdfReader
        import io

        resposta = self.client.get(reverse("core:posicoes_exportar_pdf"))
        texto = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(resposta.content)).pages)
        self.assertIn("Sua carteira x mercado", texto)
        self.assertIn("Concentração por setor", texto)
        self.assertIn("Indicadores de risco", texto)


class AnaliseTendenciaTests(TestCase):
    def setUp(self):
        self.ativo = Ativo.objects.create(ticker="ITUB4")

    def test_tendencia_de_alta(self):
        precos = [Decimal("30.0"), Decimal("30.5"), Decimal("31.2"), Decimal("32.5"), Decimal("34.0")]
        for i, preco in enumerate(precos):
            Cotacao.objects.create(
                ativo=self.ativo, data=date.today() - timedelta(days=len(precos) - i), preco_fechamento=preco
            )
        self.assertEqual(analisar_tendencia(self.ativo), "ALTA")

    def test_dados_insuficientes(self):
        Cotacao.objects.create(ativo=self.ativo, data=date.today(), preco_fechamento=Decimal("30.0"))
        self.assertEqual(analisar_tendencia(self.ativo), "DADOS_INSUFICIENTES")


class AlertasTests(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_user(username="investidor4", password="SenhaForte123!")
        self.ativo = Ativo.objects.create(ticker="BBDC4")

    def test_alerta_de_lucro_gerado_ao_atingir_meta(self):
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("20.00"), data_operacao=date.today(),
            meta_lucro_pct=Decimal("5.0"), meta_perda_pct=Decimal("-5.0"),
        )
        Cotacao.objects.create(ativo=self.ativo, data=date.today(), preco_fechamento=Decimal("25.00"))  # +25%

        alertas = gerar_alertas_para_usuario(self.usuario)
        tipos = [a.tipo for a in alertas]
        self.assertIn(Alerta.LUCRO, tipos)

    def test_alerta_nao_duplica_no_mesmo_dia(self):
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("20.00"), data_operacao=date.today(),
            meta_lucro_pct=Decimal("5.0"), meta_perda_pct=Decimal("-5.0"),
        )
        Cotacao.objects.create(ativo=self.ativo, data=date.today(), preco_fechamento=Decimal("25.00"))

        gerar_alertas_para_usuario(self.usuario)
        total_antes = Alerta.objects.count()
        gerar_alertas_para_usuario(self.usuario)
        total_depois = Alerta.objects.count()
        self.assertEqual(total_antes, total_depois)


class BrapiIntegracaoTests(TestCase):
    """Testa a integração com a API de cotações usando um mock (sem chamar a API real)."""

    @patch("core.services.requests.get")
    def test_atualizar_cotacao_diaria_grava_registro(self, mock_get):
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: {
            "results": [{
                "symbol": "PETR4",
                "shortName": "Petrobras PN",
                "regularMarketPrice": 38.5,
                "regularMarketChangePercent": 1.2,
            }]
        }

        from .services import atualizar_cotacao_diaria
        ativo = Ativo.objects.create(ticker="PETR4")
        cotacao = atualizar_cotacao_diaria(ativo)

        self.assertEqual(cotacao.preco_fechamento, Decimal("38.50"))
        ativo.refresh_from_db()
        self.assertEqual(ativo.nome, "Petrobras PN")

    @patch("core.services.requests.get")
    def test_atualizar_no_mesmo_dia_altera_em_vez_de_duplicar(self, mock_get):
        from .services import atualizar_cotacao_diaria

        ativo = Ativo.objects.create(ticker="VALE3")

        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: {
            "results": [{"regularMarketPrice": 60.0, "regularMarketChangePercent": 0.5}]
        }
        atualizar_cotacao_diaria(ativo)

        mock_get.return_value.json = lambda: {
            "results": [{"regularMarketPrice": 62.0, "regularMarketChangePercent": 3.3}]
        }
        atualizar_cotacao_diaria(ativo)

        self.assertEqual(Cotacao.objects.filter(ativo=ativo).count(), 1)
        cotacao = Cotacao.objects.get(ativo=ativo)
        self.assertEqual(cotacao.preco_fechamento, Decimal("62.00"))

    def _payload_completo(self, aninhado=False):
        campos = {
            "symbol": "B3SA3",
            "shortName": "B3SA3",
            "longName": "B3 SA - Brasil, Bolsa, Balcao",
            "currency": "BRL",
            "regularMarketPrice": 14.54,
            "regularMarketDayHigh": 14.7,
            "regularMarketDayLow": 14.31,
            "regularMarketChange": 0.13,
            "regularMarketChangePercent": 0.9,
            "regularMarketTime": "2026-08-18T14:48:30.000Z",
            "marketCap": 70010885753,
            "regularMarketVolume": 5689600,
            "regularMarketPreviousClose": 14.54,
            "regularMarketOpen": 14.31,
            "fiftyTwoWeekLow": 12.12,
            "fiftyTwoWeekHigh": 20.33,
            "logourl": "https://icons.brapi.dev/icons/B3SA3.svg",
        }
        if aninhado:
            return {"requestedSymbol": "B3SA3", "symbol": "B3SA3", "changed": False, "data": campos}
        return campos

    def test_atualizar_cotacao_preenche_todos_os_campos_extras_do_ativo(self):
        from .services import atualizar_cotacao_diaria

        ativo = Ativo.objects.create(ticker="B3SA3")
        cotacao = atualizar_cotacao_diaria(ativo, dados_api=self._payload_completo())
        ativo.refresh_from_db()

        self.assertEqual(ativo.nome_longo, "B3 SA - Brasil, Bolsa, Balcao")
        self.assertEqual(ativo.moeda, "BRL")
        self.assertEqual(ativo.maxima_dia, Decimal("14.70"))
        self.assertEqual(ativo.minima_dia, Decimal("14.31"))
        self.assertEqual(ativo.abertura, Decimal("14.31"))
        self.assertEqual(ativo.fechamento_anterior, Decimal("14.54"))
        self.assertEqual(ativo.variacao_dia_valor, Decimal("0.13"))
        self.assertEqual(ativo.volume, 5689600)
        # o mesmo volume também fica no histórico diário (Cotacao.volume) -
        # ver Scanner Técnico, core.services.analisar_volume_precos.
        self.assertEqual(cotacao.volume, 5689600)
        # máxima/mínima do dia também ficam no histórico diário (Cotacao.maxima/minima) -
        # usadas no ATR e no suporte/resistência do Scanner Técnico.
        self.assertEqual(cotacao.maxima, Decimal("14.70"))
        self.assertEqual(cotacao.minima, Decimal("14.31"))
        self.assertEqual(ativo.valor_mercado, Decimal("70010885753.00"))
        self.assertEqual(ativo.minima_52_semanas, Decimal("12.12"))
        self.assertEqual(ativo.maxima_52_semanas, Decimal("20.33"))
        self.assertEqual(ativo.logo_url, "https://icons.brapi.dev/icons/B3SA3.svg")
        self.assertIsNotNone(ativo.hora_cotacao)
        self.assertIsNotNone(ativo.atualizado_em)

    def test_atualizar_cotacao_aceita_formato_aninhado_em_data(self):
        # a brapi.dev às vezes retorna os campos aninhados em "data" (visto em
        # consultas específicas) em vez de direto no objeto do resultado.
        from .services import atualizar_cotacao_diaria

        ativo = Ativo.objects.create(ticker="B3SA3")
        atualizar_cotacao_diaria(ativo, dados_api=self._payload_completo(aninhado=True))
        ativo.refresh_from_db()

        self.assertEqual(ativo.nome_longo, "B3 SA - Brasil, Bolsa, Balcao")
        self.assertEqual(ativo.maxima_dia, Decimal("14.70"))

    @patch("core.services.requests.get")
    def test_atualizado_em_muda_a_cada_atualizacao(self, mock_get):
        from .services import atualizar_cotacao_diaria

        ativo = Ativo.objects.create(ticker="ELET3")
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: {"results": [{"regularMarketPrice": 40.0}]}

        atualizar_cotacao_diaria(ativo)
        primeira_atualizacao = ativo.atualizado_em

        mock_get.return_value.json = lambda: {"results": [{"regularMarketPrice": 41.0}]}
        atualizar_cotacao_diaria(ativo)
        ativo.refresh_from_db()

        self.assertGreaterEqual(ativo.atualizado_em, primeira_atualizacao)


class ComandoAtualizarCotacoesTests(TestCase):
    """Testa o management command 'atualizar_cotacoes', inclusive o modo --loop."""

    @patch("core.services.requests.get")
    def test_sem_loop_roda_uma_vez_e_termina(self, mock_get):
        usuario = User.objects.create_user(username="investidor_cmd", password="SenhaForte123!")
        ativo = Ativo.objects.create(ticker="PETR4")
        Operacao.objects.create(
            usuario=usuario, ativo=ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("30.00"), data_operacao=date.today(),
        )
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: {"results": [{"symbol": "PETR4", "regularMarketPrice": 32.0}]}

        saida = StringIO()
        call_command("atualizar_cotacoes", stdout=saida)

        self.assertTrue(Cotacao.objects.filter(ativo=ativo).exists())
        self.assertIn("Cotações atualizadas: 1", saida.getvalue())
        mock_get.assert_called_once()  # rodou só uma vez, sem --loop

    @patch("core.management.commands.atualizar_cotacoes.time.sleep")
    @patch("core.services.requests.get")
    def test_loop_usa_intervalo_padrao_das_settings_e_repete(self, mock_get, mock_sleep):
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: {"results": [{"regularMarketPrice": 10.0}]}

        # sleep() é chamado a cada volta do loop - na 2ª chamada, simula Ctrl+C pra parar o teste
        mock_sleep.side_effect = [None, KeyboardInterrupt()]

        saida = StringIO()
        with override_settings(COTACOES_INTERVALO_MINUTOS=42):
            call_command("atualizar_cotacoes", "--loop", stdout=saida)

        self.assertIn("cada 42 minuto(s)", saida.getvalue())
        self.assertIn("Interrompido pelo usuário", saida.getvalue())
        mock_sleep.assert_any_call(42 * 60)

    @patch("core.management.commands.atualizar_cotacoes.time.sleep")
    @patch("core.services.requests.get")
    def test_loop_aceita_intervalo_informado_na_linha_de_comando(self, mock_get, mock_sleep):
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: {"results": [{"regularMarketPrice": 10.0}]}
        mock_sleep.side_effect = KeyboardInterrupt()

        saida = StringIO()
        call_command("atualizar_cotacoes", "--loop", "--intervalo", "5", stdout=saida)

        self.assertIn("cada 5 minuto(s)", saida.getvalue())
        mock_sleep.assert_any_call(5 * 60)

    def test_intervalo_zero_ou_negativo_gera_erro(self):
        with self.assertRaises(CommandError):
            call_command("atualizar_cotacoes", "--loop", "--intervalo", "0", stdout=StringIO())

    @patch("core.management.commands.atualizar_cotacoes.time.sleep")
    @patch("core.management.commands.atualizar_cotacoes.mercado_b3_aberto", return_value=False)
    @patch("core.services.requests.get")
    def test_loop_pula_o_ciclo_com_b3_fechada(self, mock_get, mock_mercado, mock_sleep):
        ativo = Ativo.objects.create(ticker="PETR4")
        Operacao.objects.create(
            usuario=User.objects.create_user(username="investidor_b3_fechada", password="SenhaForte123!"),
            ativo=ativo, tipo=Operacao.COMPRA, quantidade=10, preco_unitario=Decimal("30.00"),
            data_operacao=date.today(),
        )
        mock_sleep.side_effect = KeyboardInterrupt()

        saida = StringIO()
        call_command("atualizar_cotacoes", "--loop", stdout=saida)

        self.assertIn("B3 fechada agora - pulando este ciclo.", saida.getvalue())
        mock_get.assert_not_called()
        self.assertFalse(Cotacao.objects.filter(ativo=ativo).exists())

    @patch("core.management.commands.atualizar_cotacoes.time.sleep")
    @patch("core.management.commands.atualizar_cotacoes.mercado_b3_aberto", return_value=True)
    @patch("core.services.requests.get")
    def test_loop_roda_o_ciclo_com_b3_aberta(self, mock_get, mock_mercado, mock_sleep):
        ativo = Ativo.objects.create(ticker="PETR4")
        Operacao.objects.create(
            usuario=User.objects.create_user(username="investidor_b3_aberta", password="SenhaForte123!"),
            ativo=ativo, tipo=Operacao.COMPRA, quantidade=10, preco_unitario=Decimal("30.00"),
            data_operacao=date.today(),
        )
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: {"results": [{"symbol": "PETR4", "regularMarketPrice": 32.0}]}
        mock_sleep.side_effect = KeyboardInterrupt()

        saida = StringIO()
        call_command("atualizar_cotacoes", "--loop", stdout=saida)

        self.assertNotIn("pulando este ciclo", saida.getvalue())
        self.assertTrue(Cotacao.objects.filter(ativo=ativo).exists())


class MensagemFalhaApiTests(TestCase):
    """
    core.services._mensagem_falha_api - mensagem amigável para falhas de API,
    sem vazar a URL da requisição (que inclui BRAPI_TOKEN) nem para o
    usuário nem para o 429 especificamente.
    """

    def _http_error(self, status_code):
        resposta = Mock()
        resposta.status_code = status_code
        erro = requests.exceptions.HTTPError(response=resposta)
        return erro

    def test_429_gera_mensagem_de_limite_de_consultas(self):
        mensagem = _mensagem_falha_api(self._http_error(429), "o ticker PETR4")
        self.assertIn("Limite de consultas", mensagem)
        self.assertIn("o ticker PETR4", mensagem)

    def test_429_de_limite_mensal_esgotado_gera_mensagem_com_data_de_renovacao(self):
        # a brapi.dev também responde 429 quando a cota MENSAL do plano
        # gratuito acaba - nesse caso "tente novamente em instantes" seria
        # enganoso (só volta a funcionar na próxima virada do ciclo, ver
        # code=MONTHLY_LIMIT_EXCEEDED no corpo da resposta).
        resposta = Mock()
        resposta.status_code = 429
        resposta.json = lambda: {
            "error": True,
            "code": "MONTHLY_LIMIT_EXCEEDED",
            "details": {"usage": {"current": 15000, "limit": 15000, "resetsAt": "2026-10-17T14:00:11.000Z"}},
        }
        erro = requests.exceptions.HTTPError(response=resposta)

        mensagem = _mensagem_falha_api(erro, "o ticker PETR4")

        self.assertIn("Limite mensal", mensagem)
        self.assertIn("17/10/2026", mensagem)
        self.assertNotIn("Tente novamente em alguns minutos", mensagem)

    def test_429_generico_sem_corpo_reconhecivel_usa_mensagem_padrao(self):
        # response.json() de um Mock() genérico não é um dict de verdade -
        # _detalhe_limite_brapi precisa degradar pra mensagem padrão, não
        # quebrar com AttributeError/TypeError.
        mensagem = _mensagem_falha_api(self._http_error(429), "o ticker PETR4")
        self.assertIn("Tente novamente em alguns minutos", mensagem)

    def test_outros_erros_geram_mensagem_generica(self):
        mensagem = _mensagem_falha_api(self._http_error(500), "o ticker PETR4")
        self.assertIn("Falha ao consultar a API", mensagem)

    def test_mensagem_nunca_expoe_o_token_da_api(self):
        # a exceção "de verdade" da requests inclui a URL completa (com
        # ?token=...) no __str__ - a mensagem amigável não pode repassar isso.
        url_com_token = f"{settings.BRAPI_BASE_URL}/quote/PETR4?token=segredo-super-secreto"
        exc = requests.exceptions.ConnectionError(f"Falha de conexão: {url_com_token}")
        mensagem = _mensagem_falha_api(exc, "o ticker PETR4")
        self.assertNotIn("segredo-super-secreto", mensagem)
        self.assertNotIn("token=", mensagem)

    @patch("core.services.requests.get")
    def test_buscar_cotacao_atual_nao_vaza_token_quando_api_falha(self, mock_get):
        resposta_429 = Mock()
        resposta_429.status_code = 429
        mock_get.return_value.raise_for_status = Mock(
            side_effect=requests.exceptions.HTTPError(response=resposta_429)
        )

        with override_settings(BRAPI_TOKEN="token-secreto-de-teste"):
            with self.assertRaises(BrapiError) as contexto:
                buscar_cotacao_atual("PETR4")

        mensagem = str(contexto.exception)
        self.assertNotIn("token-secreto-de-teste", mensagem)
        self.assertNotIn("token=", mensagem)
        self.assertIn("Limite de consultas", mensagem)

    def test_consultar_cotacao_avulsa_nao_vaza_token_na_resposta_json(self):
        usuario = User.objects.create_user(username="investidor_sem_vazamento", password="SenhaForte123!")
        self.client.login(username="investidor_sem_vazamento", password="SenhaForte123!")

        resposta_429 = Mock()
        resposta_429.status_code = 429
        with override_settings(BRAPI_TOKEN="token-secreto-de-teste"):
            with patch("core.services.requests.get") as mock_get:
                mock_get.return_value.raise_for_status = Mock(
                    side_effect=requests.exceptions.HTTPError(response=resposta_429)
                )
                resposta = self.client.get(reverse("core:consultar_cotacao_avulsa"), {"ticker": "AXIA3"})

        self.assertNotIn(b"token-secreto-de-teste", resposta.content)
        self.assertNotIn(b"token=", resposta.content)


class BuscarCotacoesEmLoteTests(TestCase):
    """core.services.buscar_cotacoes_em_lote / atualizar_cotacoes_ativos - consulta em lote (evita 429 da brapi.dev)."""

    @patch("core.services.requests.get")
    def test_um_unico_lote_para_poucos_tickers(self, mock_get):
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: {
            "results": [
                {"symbol": "PETR4", "regularMarketPrice": 32.0},
                {"symbol": "VALE3", "regularMarketPrice": 68.0},
            ]
        }

        resultado = buscar_cotacoes_em_lote(["PETR4", "VALE3"])

        mock_get.assert_called_once()
        url_chamada = mock_get.call_args[0][0]
        self.assertIn("PETR4,VALE3", url_chamada)
        self.assertEqual(resultado["PETR4"]["regularMarketPrice"], 32.0)
        self.assertEqual(resultado["VALE3"]["regularMarketPrice"], 68.0)

    @patch("core.services.requests.get")
    def test_tamanho_padrao_do_lote_e_10(self, mock_get):
        # regressão: o plano Startup (e o gratuito) da brapi.dev aceita no
        # máximo 10 tickers por requisição - pedir mais não dá 429, dá 400
        # "QUOTES_PER_REQUEST_EXCEEDED" e o lote inteiro falha em silêncio.
        # O padrão daqui NUNCA pode voltar a ser maior que 10 sem checar o
        # plano contratado primeiro.
        tickers = [f"T{i}" for i in range(11)]
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: {"results": []}

        buscar_cotacoes_em_lote(tickers)

        self.assertEqual(mock_get.call_count, 2)  # 10 + 1, não 1 chamada só

    @patch("core.services.requests.get")
    def test_tamanho_do_lote_respeita_configuracao(self, mock_get):
        tickers = [f"T{i}" for i in range(7)]
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: {"results": []}

        with override_settings(BRAPI_TICKERS_POR_LOTE=3):
            buscar_cotacoes_em_lote(tickers)

        self.assertEqual(mock_get.call_count, 3)  # 3 + 3 + 1

    @patch("core.services.requests.get")
    def test_divide_em_varios_lotes_quando_excede_o_tamanho_maximo(self, mock_get):
        tickers = [f"T{i}" for i in range(32)]  # mais que 2x o tamanho do lote
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: {"results": [{"symbol": "T0", "regularMarketPrice": 10.0}]}

        with override_settings(BRAPI_TICKERS_POR_LOTE=15):
            buscar_cotacoes_em_lote(tickers)

        self.assertEqual(mock_get.call_count, 3)  # 15 + 15 + 2

    @patch("core.services.requests.get")
    def test_falha_em_um_lote_nao_derruba_os_demais(self, mock_get):
        chamada = {"n": 0}

        def _resposta(url, params=None, timeout=None):
            chamada["n"] += 1
            if chamada["n"] == 1:
                raise requests.RequestException("falha de rede simulada")
            resposta = Mock()
            resposta.raise_for_status = lambda: None
            resposta.json = lambda: {"results": [{"symbol": "T20", "regularMarketPrice": 20.0}]}
            return resposta

        mock_get.side_effect = _resposta
        tickers = [f"T{i}" for i in range(20)] + ["T20"]  # 21 tickers -> 2 lotes (15 + 6)

        resultado = buscar_cotacoes_em_lote(tickers)

        self.assertNotIn("T0", resultado)  # 1º lote falhou
        self.assertEqual(resultado["T20"]["regularMarketPrice"], 20.0)  # 2º lote deu certo

    @patch("core.services.requests.get")
    def test_atualizar_cotacoes_ativos_grava_cotacao_e_conta_falha_de_ticker_ausente(self, mock_get):
        ativo_ok = Ativo.objects.create(ticker="PETR4")
        ativo_ausente = Ativo.objects.create(ticker="FANTASMA9")
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: {"results": [{"symbol": "PETR4", "regularMarketPrice": 32.0}]}

        atualizados, falhas = atualizar_cotacoes_ativos([ativo_ok, ativo_ausente])

        self.assertEqual(atualizados, 1)
        self.assertEqual(falhas, 1)
        self.assertTrue(Cotacao.objects.filter(ativo=ativo_ok).exists())
        self.assertFalse(Cotacao.objects.filter(ativo=ativo_ausente).exists())

    def test_atualizar_cotacoes_ativos_lista_vazia_nao_chama_api(self):
        with patch("core.services.requests.get") as mock_get:
            atualizados, falhas = atualizar_cotacoes_ativos([])
        mock_get.assert_not_called()
        self.assertEqual((atualizados, falhas), (0, 0))


class ExecutarCicloAtualizacaoTests(TestCase):
    """core.services.executar_ciclo_atualizacao_cotacoes - ciclo compartilhado pelo comando de management e pelo agendador embutido."""

    @patch("core.services.requests.get")
    def test_ciclo_atualiza_cotacoes_gera_alertas_e_registra_carteira(self, mock_get):
        usuario = User.objects.create_user(username="investidor_ciclo", password="SenhaForte123!")
        ativo = Ativo.objects.create(ticker="PETR4")
        Operacao.objects.create(
            usuario=usuario, ativo=ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("30.00"), data_operacao=date.today(),
            meta_lucro_pct=Decimal("5.0"),
        )
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: {"results": [{"symbol": "PETR4", "regularMarketPrice": 32.0}]}

        resultado = executar_ciclo_atualizacao_cotacoes()

        self.assertEqual(resultado["ativos_total"], 1)
        self.assertEqual(resultado["ativos_atualizados"], 1)
        self.assertEqual(resultado["ativos_falha"], 0)
        self.assertGreaterEqual(resultado["alertas_gerados"], 1)  # meta de 5% batida (30 -> 32 = +6,67%)
        self.assertTrue(Cotacao.objects.filter(ativo=ativo).exists())
        self.assertTrue(RegistroAtualizacaoCarteira.objects.filter(usuario=usuario).exists())

    @patch("core.services.requests.get")
    def test_ciclo_conta_falha_sem_derrubar_o_resto(self, mock_get):
        User.objects.create_user(username="investidor_ciclo_falha", password="SenhaForte123!")
        ativo = Ativo.objects.create(ticker="VALE3")
        Operacao.objects.create(
            usuario=User.objects.get(username="investidor_ciclo_falha"), ativo=ativo, tipo=Operacao.COMPRA,
            quantidade=5, preco_unitario=Decimal("60.00"), data_operacao=date.today(),
        )
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: {"results": [{}]}  # sem regularMarketPrice -> BrapiError

        resultado = executar_ciclo_atualizacao_cotacoes()

        self.assertEqual(resultado["ativos_falha"], 1)
        self.assertEqual(resultado["ativos_atualizados"], 0)


class CoreConfigReadyTests(TestCase):
    """
    core.apps.CoreConfig.ready() - liga o agendador embutido de cotações
    também sob "manage.py runserver" (antes só nascia via wsgi.py, então
    quem roda o site só com runserver nunca tinha atualização automática de
    verdade, mesmo com COTACOES_INTERVALO_MINUTOS configurado).
    """

    def setUp(self):
        self.argv_original = sys.argv[:]
        self.run_main_original = os.environ.get("RUN_MAIN")
        self.config = django_apps.get_app_config("core")

    def tearDown(self):
        sys.argv[:] = self.argv_original
        if self.run_main_original is None:
            os.environ.pop("RUN_MAIN", None)
        else:
            os.environ["RUN_MAIN"] = self.run_main_original

    def _chamar_ready(self, argv, run_main=None):
        sys.argv[:] = argv
        if run_main is None:
            os.environ.pop("RUN_MAIN", None)
        else:
            os.environ["RUN_MAIN"] = run_main
        self.config.ready()

    @patch("core.services.iniciar_agendador_cotacoes_embutido")
    def test_nao_inicia_para_comandos_que_nao_sao_runserver(self, mock_iniciar):
        self._chamar_ready(["manage.py", "test"])
        mock_iniciar.assert_not_called()

    @patch("core.services.iniciar_agendador_cotacoes_embutido")
    def test_nao_inicia_no_processo_pai_do_autoreload(self, mock_iniciar):
        # runserver com autoreload (padrão): o processo "pai" (só observa
        # arquivos) não tem RUN_MAIN definida.
        self._chamar_ready(["manage.py", "runserver"])
        mock_iniciar.assert_not_called()

    @patch("core.services.iniciar_agendador_cotacoes_embutido")
    def test_inicia_no_processo_filho_do_autoreload(self, mock_iniciar):
        self._chamar_ready(["manage.py", "runserver"], run_main="true")
        mock_iniciar.assert_called_once()

    @patch("core.services.iniciar_agendador_cotacoes_embutido")
    def test_inicia_com_noreload_mesmo_sem_run_main(self, mock_iniciar):
        # com --noreload existe um processo só, e RUN_MAIN nunca é definida
        self._chamar_ready(["manage.py", "runserver", "--noreload"])
        mock_iniciar.assert_called_once()


class AgendadorCotacoesEmbutidoTests(TestCase):
    """core.services.iniciar_agendador_cotacoes_embutido - thread de atualização automática ligada em bolsatrader/wsgi.py."""

    def setUp(self):
        core_services._agendador_cotacoes_iniciado = False

    def tearDown(self):
        core_services._agendador_cotacoes_iniciado = False

    def test_nao_inicia_com_intervalo_zero_ou_negativo(self):
        with override_settings(COTACOES_INTERVALO_MINUTOS=0, AGENDADOR_COTACOES_EMBUTIDO=True):
            self.assertFalse(iniciar_agendador_cotacoes_embutido())

    def test_nao_inicia_quando_desligado_por_configuracao(self):
        with override_settings(AGENDADOR_COTACOES_EMBUTIDO=False, COTACOES_INTERVALO_MINUTOS=10):
            self.assertFalse(iniciar_agendador_cotacoes_embutido())

    def test_inicia_e_e_idempotente(self):
        with override_settings(AGENDADOR_COTACOES_EMBUTIDO=True, COTACOES_INTERVALO_MINUTOS=10):
            self.assertTrue(iniciar_agendador_cotacoes_embutido())
            self.assertTrue(iniciar_agendador_cotacoes_embutido())  # 2ª chamada não inicia outra thread

        nomes_threads = [t.name for t in threading.enumerate()]
        self.assertEqual(nomes_threads.count("bolsatrader-cotacoes-agendador"), 1)

    @patch("core.services.mercado_b3_aberto", return_value=False)
    @patch("core.services.executar_ciclo_atualizacao_cotacoes")
    def test_ciclo_do_agendador_nao_roda_com_b3_fechada(self, mock_executar, mock_mercado):
        _ciclo_agendador_embutido()
        mock_executar.assert_not_called()

    @patch("core.services.mercado_b3_aberto", return_value=True)
    @patch("core.services.executar_ciclo_atualizacao_cotacoes")
    def test_ciclo_do_agendador_roda_com_b3_aberta(self, mock_executar, mock_mercado):
        mock_executar.return_value = {
            "ativos_total": 1, "ativos_atualizados": 1, "ativos_falha": 0,
            "alertas_gerados": 0, "sinais_robo_gerados": 0,
        }
        _ciclo_agendador_embutido()
        mock_executar.assert_called_once()

    @patch("core.services.mercado_b3_aberto", return_value=True)
    @patch("core.services.executar_ciclo_atualizacao_cotacoes", side_effect=RuntimeError("falha simulada"))
    def test_ciclo_do_agendador_nao_propaga_excecao(self, mock_executar, mock_mercado):
        _ciclo_agendador_embutido()  # não deve levantar - a thread não pode morrer por uma falha pontual


class VerificarAtualizacaoCotacoesTests(TestCase):
    """core.views.verificar_atualizacao_cotacoes - endpoint de polling usado pela tela Posições em Carteira."""

    def setUp(self):
        self.usuario = User.objects.create_user(username="investidor_polling", password="SenhaForte123!")
        self.client.login(username="investidor_polling", password="SenhaForte123!")
        self.ativo = Ativo.objects.create(ticker="ITSA4")
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("9.00"), data_operacao=date.today(),
        )

    def test_exige_login(self):
        self.client.logout()
        resposta = self.client.get(reverse("core:verificar_atualizacao_cotacoes"))
        self.assertEqual(resposta.status_code, 302)

    def test_retorna_nulo_para_usuario_sem_ativos(self):
        User.objects.create_user(username="sem_ativos_polling", password="SenhaForte123!")
        self.client.login(username="sem_ativos_polling", password="SenhaForte123!")

        resposta = self.client.get(reverse("core:verificar_atualizacao_cotacoes"))

        self.assertEqual(resposta.status_code, 200)
        self.assertIsNone(resposta.json()["ultima_atualizacao"])

    def test_retorna_timestamp_iso_do_ativo_do_usuario(self):
        self.ativo.refresh_from_db()

        resposta = self.client.get(reverse("core:verificar_atualizacao_cotacoes"))

        self.assertEqual(resposta.json()["ultima_atualizacao"], self.ativo.atualizado_em.isoformat())

    def test_nao_enxerga_atualizacao_de_ativo_de_outro_usuario(self):
        outro_usuario = User.objects.create_user(username="outro_polling", password="SenhaForte123!")
        outro_ativo = Ativo.objects.create(ticker="BBAS3")
        Operacao.objects.create(
            usuario=outro_usuario, ativo=outro_ativo, tipo=Operacao.COMPRA,
            quantidade=5, preco_unitario=Decimal("20.00"), data_operacao=date.today(),
        )
        # .update() não passa pelo auto_now do save() - dá pra forçar um
        # timestamp bem mais recente que o do ativo do usuário logado, pra
        # garantir que um eventual vazamento entre usuários apareceria aqui.
        Ativo.objects.filter(id=outro_ativo.id).update(atualizado_em=timezone.now())
        self.ativo.refresh_from_db()

        resposta = self.client.get(reverse("core:verificar_atualizacao_cotacoes"))

        self.assertEqual(resposta.json()["ultima_atualizacao"], self.ativo.atualizado_em.isoformat())

    def test_tela_posicoes_tem_marcador_e_script_de_polling(self):
        resposta = self.client.get(reverse("core:posicoes"))
        self.assertContains(resposta, 'id="marcador-ultima-atualizacao"')
        self.assertContains(resposta, reverse("core:verificar_atualizacao_cotacoes"))


class DetalhesCotacoesTests(TestCase):
    """
    Tela Detalhes de Cotações - só a consulta avulsa (a grid com a cotação
    de cada ativo em carteira foi removida a pedido; quem quiser acompanhar
    os ativos comprados usa Posições em Carteira).
    """

    def setUp(self):
        self.usuario = User.objects.create_user(username="investidor_detalhes", password="SenhaForte123!")
        self.client.login(username="investidor_detalhes", password="SenhaForte123!")

    def test_exige_login(self):
        self.client.logout()
        resposta = self.client.get(reverse("core:detalhes_cotacoes"))
        self.assertEqual(resposta.status_code, 302)

    def test_pagina_mostra_a_consulta_avulsa(self):
        resposta = self.client.get(reverse("core:detalhes_cotacoes"))
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Consultar cotação avulsa")
        self.assertContains(resposta, 'id="campo-ticker-avulso"')
        self.assertContains(resposta, "Preço alvo (R$)")  # calculadora de lucro/perda do card da consulta

    def test_pagina_mostra_explicacao_do_ifr(self):
        resposta = self.client.get(reverse("core:detalhes_cotacoes"))
        self.assertContains(resposta, "O que é o IFR (RSI)?")
        self.assertContains(resposta, "IFR = 100")
        self.assertContains(resposta, "Sobrecomprado")
        self.assertContains(resposta, "Sobrevendido")
        self.assertContains(resposta, "Divergência altista")
        self.assertContains(resposta, "Divergência baixista")

    def test_pagina_mostra_checklist_de_sinais_de_alta_e_baixa(self):
        resposta = self.client.get(reverse("core:detalhes_cotacoes"))
        self.assertContains(resposta, "Sinais de alta e de baixa")
        self.assertContains(resposta, "Sinal de alta")
        self.assertContains(resposta, "IFR (RSI, 14 períodos) &lt; 35")
        self.assertContains(resposta, "Preço acima da média móvel de 21 dias")
        self.assertContains(resposta, "Volume acima da média")
        self.assertContains(resposta, "MACD positivo")
        self.assertContains(resposta, "Tendência de curto prazo positiva")
        self.assertContains(resposta, "Sinal de baixa")
        self.assertContains(resposta, "IFR (RSI, 14 períodos) &gt; 65")
        self.assertContains(resposta, "Preço abaixo da média móvel de 21 dias")
        self.assertContains(resposta, "Volume aumentando na queda")
        self.assertContains(resposta, "MACD negativo")
        self.assertContains(resposta, "Tendência de curto prazo negativa")

    def test_pagina_nao_mostra_mais_a_grid_de_ativos_acompanhados(self):
        # a grid antiga (um card por ativo em carteira, com botão "Atualizar
        # cotações agora") foi removida - só sobra a consulta avulsa.
        ativo = Ativo.objects.create(ticker="B3SA3")
        Operacao.objects.create(
            usuario=self.usuario, ativo=ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("14.00"), data_operacao=date.today(),
        )
        Cotacao.objects.create(ativo=ativo, data=date.today(), preco_fechamento=Decimal("10.35"))

        resposta = self.client.get(reverse("core:detalhes_cotacoes"))

        self.assertNotContains(resposta, "grid-cotacoes")
        self.assertNotContains(resposta, 'class="btn-futurista btn-atualizar-cotacoes"')
    def test_seletor_lista_apenas_os_ativos_comprados_do_usuario(self):
        comprado = Ativo.objects.create(ticker="B3SA3")
        Operacao.objects.create(
            usuario=self.usuario, ativo=comprado, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("14.00"), data_operacao=date.today(),
        )
        so_reservado = Ativo.objects.create(ticker="ITSA4")
        Operacao.objects.create(
            usuario=self.usuario, ativo=so_reservado, tipo=Operacao.RESERVAR,
            quantidade=1, preco_unitario=Decimal("9.00"), data_operacao=date.today(),
        )
        de_outro = Ativo.objects.create(ticker="RENT3")
        Operacao.objects.create(
            usuario=User.objects.create_user(username="outro_seletor", password="SenhaForte123!"),
            ativo=de_outro, tipo=Operacao.COMPRA, quantidade=5, preco_unitario=Decimal("50.00"),
            data_operacao=date.today(),
        )

        resposta = self.client.get(reverse("core:detalhes_cotacoes"))

        self.assertContains(resposta, '<option value="B3SA3">')
        self.assertNotContains(resposta, "ITSA4")  # reserva não é ativo comprado
        self.assertNotContains(resposta, "RENT3")  # ativo de outro usuário

    def test_sem_ativos_comprados_nao_mostra_o_seletor_mas_mantem_a_busca_avulsa(self):
        resposta = self.client.get(reverse("core:detalhes_cotacoes"))
        self.assertNotContains(resposta, 'id="campo-ativo-comprado"')
        self.assertContains(resposta, 'id="campo-ticker-avulso"')


class BuscarCotacaoAtualComHistoricoTests(TestCase):
    """core.services.buscar_cotacao_atual_com_historico - cotação + histórico numa única chamada (usado para o IFR/RSI da consulta avulsa)."""

    def _payload(self, pontos_historico):
        return {
            "results": [{
                "symbol": "PETR4",
                "regularMarketPrice": 32.0,
                "shortName": "PETR4",
                "historicalDataPrice": pontos_historico,
            }]
        }

    @patch("core.services.requests.get")
    def test_retorna_dados_atuais_e_precos_em_ordem_cronologica(self, mock_get):
        base = int(datetime(2026, 1, 1, tzinfo=dt_timezone.utc).timestamp())
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: self._payload([
            {"date": base + 2 * 86400, "close": 30.0},
            {"date": base, "close": 28.0},  # fora de ordem de propósito
            {"date": base + 86400, "close": 29.0},
        ])

        dados, precos = buscar_cotacao_atual_com_historico("PETR4")

        self.assertEqual(dados["regularMarketPrice"], 32.0)
        self.assertEqual(precos, [28.0, 29.0, 30.0])  # do mais antigo pro mais recente

    @patch("core.services.requests.get")
    def test_ignora_pontos_sem_fechamento_ou_data_duplicada(self, mock_get):
        base = int(datetime(2026, 1, 1, tzinfo=dt_timezone.utc).timestamp())
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: self._payload([
            {"date": base, "close": 28.0},
            {"date": base, "close": 99.0},  # mesma data - ignorado
            {"date": base + 86400, "close": None},  # sem fechamento - ignorado
        ])

        _, precos = buscar_cotacao_atual_com_historico("PETR4")

        self.assertEqual(precos, [28.0])

    @patch("core.services.requests.get")
    def test_ticker_nao_encontrado_gera_brapierror(self, mock_get):
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: {"results": []}

        with self.assertRaises(BrapiError):
            buscar_cotacao_atual_com_historico("FANTASMA9")


class ConsultarCotacaoAvulsaTests(TestCase):
    """core.views.consultar_cotacao_avulsa - inclui o IFR/RSI calculado a partir do histórico retornado pela API."""

    def setUp(self):
        self.usuario = User.objects.create_user(username="investidor_avulso_rsi", password="SenhaForte123!")
        self.client.login(username="investidor_avulso_rsi", password="SenhaForte123!")

    def _payload_com_historico(self, precos_fechamento):
        base = int(datetime(2026, 1, 1, tzinfo=dt_timezone.utc).timestamp())
        pontos = [{"date": base + i * 86400, "close": preco} for i, preco in enumerate(precos_fechamento)]
        return {
            "results": [{
                "symbol": "PETR4",
                "shortName": "PETR4",
                "regularMarketPrice": 32.0,
                "regularMarketChangePercent": 1.5,
                "historicalDataPrice": pontos,
            }]
        }

    @patch("core.services.requests.get")
    def test_retorna_rsi_com_historico_suficiente(self, mock_get):
        # 15 fechamentos subindo direto -> RSI deve vir alto (sobrecomprado)
        precos = [20.0 + i for i in range(15)]
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: self._payload_com_historico(precos)

        resposta = self.client.get(reverse("core:consultar_cotacao_avulsa"), {"ticker": "PETR4"})
        dados = resposta.json()

        self.assertTrue(dados["ok"])
        self.assertIsNotNone(dados["rsi"])
        self.assertEqual(dados["rsi_classe"], "baixa")  # RSI_CLASSES["SOBRECOMPRADO"] == "baixa"
        self.assertIn("Sobrecomprado", dados["rsi_label"])

    @patch("core.services.requests.get")
    def test_rsi_nulo_com_historico_insuficiente(self, mock_get):
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: self._payload_com_historico([20.0, 21.0])  # só 2 pontos

        resposta = self.client.get(reverse("core:consultar_cotacao_avulsa"), {"ticker": "PETR4"})
        dados = resposta.json()

        self.assertTrue(dados["ok"])
        self.assertIsNone(dados["rsi"])
        self.assertIn("Aguardando histórico", dados["rsi_label"])


class GraficoCotacoesTests(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_user(username="investidor6", password="SenhaForte123!")
        self.client.login(username="investidor6", password="SenhaForte123!")
        self.ativo = Ativo.objects.create(ticker="ITUB4")
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("25.00"),
        )
        hoje = date.today()
        for i in range(5):
            Cotacao.objects.create(
                ativo=self.ativo,
                data=hoje - timedelta(days=4 - i),
                preco_fechamento=Decimal("25.00") + i,
                variacao_dia_pct=Decimal("1.0"),
            )

    def test_historico_graficos_renderiza_grafico_com_pontos(self):
        resposta = self.client.get(reverse("core:historico_graficos"))
        self.assertEqual(resposta.status_code, 200)
        conteudo = resposta.content.decode()
        self.assertIn('class="grafico-cotacoes"', conteudo)
        self.assertIn('"preco_label": "R$ 29,00"', conteudo)  # último fechamento (25 + 4)

    def test_marcadores_svg_nao_tem_virgula_decimal_do_locale_pt_br(self):
        # regressão: com LANGUAGE_CODE=pt-br, {{ valor }} de um float vira "128,98"
        # em vez de "128.98", o que quebra os atributos numéricos de um <svg>.
        resposta = self.client.get(reverse("core:historico_graficos"))
        conteudo = resposta.content.decode()
        self.assertRegex(conteudo, r'<circle cx="[\d.]+" cy="[\d.]+"')
        self.assertNotRegex(conteudo, r'<circle cx="\d+,\d')

    def test_analise_mercado_nao_traz_mais_grafico_nem_historico(self):
        resposta = self.client.get(reverse("core:analise_mercado"))
        self.assertEqual(resposta.status_code, 200)
        conteudo = resposta.content.decode()
        self.assertNotIn('class="grafico-cotacoes"', conteudo)
        self.assertIn("ITUB4", conteudo)  # continua mostrando o sinal do ativo


class PaginasPrincipaisTests(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_user(username="investidor5", password="SenhaForte123!")
        self.client.login(username="investidor5", password="SenhaForte123!")

    def test_paginas_respondem_200(self):
        for nome_url in ["core:menu", "core:operacao_lista", "core:operacao_nova",
                          "core:posicoes", "core:alertas", "core:analise_mercado",
                          "core:historico_graficos", "core:mensagens_whatsapp"]:
            resposta = self.client.get(reverse(nome_url))
            self.assertEqual(resposta.status_code, 200, f"{nome_url} falhou")


class PwaTests(TestCase):
    """Garante que a infraestrutura do PWA (manifest, service worker, offline) está disponível."""

    def test_manifest_acessivel_via_static(self):
        import json
        from django.conf import settings

        caminho = settings.BASE_DIR / "static" / "manifest.webmanifest"
        conteudo = json.loads(caminho.read_text(encoding="utf-8"))
        self.assertEqual(conteudo["display"], "standalone")
        self.assertEqual(conteudo["start_url"], "/")
        self.assertEqual(conteudo["scope"], "/")
        self.assertTrue(len(conteudo["icons"]) >= 4)
        self.assertTrue(any(icone["sizes"] == "512x512" for icone in conteudo["icons"]))
        self.assertTrue(any(icone.get("purpose") == "maskable" for icone in conteudo["icons"]))

    def test_service_worker_servido_na_raiz(self):
        resposta = self.client.get("/sw.js")
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta["Content-Type"], "application/javascript")
        # regressão: o teste ficou preso em "v1" depois que o cache do Service
        # Worker foi renomeado pra "v2" (ver static/js/service-worker-source.js) -
        # checa só o prefixo, pra não travar de novo na próxima renomeação.
        self.assertIn("bolsatrader-v", resposta.content.decode("utf-8"))

    def test_pagina_offline_nao_exige_login(self):
        resposta = self.client.get("/offline/")
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "offline")

    def test_icones_pwa_existem(self):
        from django.conf import settings

        pasta_icones = settings.BASE_DIR / "static" / "icons"
        for nome in ["icon-192.png", "icon-512.png", "icon-512-maskable.png",
                     "apple-touch-icon.png", "favicon-32.png", "favicon-16.png"]:
            self.assertTrue((pasta_icones / nome).exists(), f"ícone ausente: {nome}")


class WhatsappTests(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_user(username="investidor7", password="SenhaForte123!")
        self.client.login(username="investidor7", password="SenhaForte123!")

    def test_icone_flutuante_aparece_no_painel_com_link_correto(self):
        resposta = self.client.get(reverse("core:dashboard"))
        self.assertContains(resposta, "whatsapp-flutuante")
        self.assertContains(resposta, "https://wa.me/5565981132995")

    def test_pagina_de_mensagens_lista_mensagens_recebidas(self):
        MensagemWhatsapp.objects.create(remetente="Cliente Teste", texto="Mensagem de teste")
        resposta = self.client.get(reverse("core:mensagens_whatsapp"))
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Cliente Teste")
        self.assertContains(resposta, "Mensagem de teste")

    def test_webhook_get_verifica_token_correto(self):
        with override_settings(WHATSAPP_VERIFY_TOKEN="token-secreto"):
            resposta = self.client.get(reverse("whatsapp_webhook"), {
                "hub.mode": "subscribe",
                "hub.verify_token": "token-secreto",
                "hub.challenge": "12345",
            })
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.content.decode(), "12345")

    def test_webhook_get_rejeita_token_incorreto(self):
        with override_settings(WHATSAPP_VERIFY_TOKEN="token-secreto"):
            resposta = self.client.get(reverse("whatsapp_webhook"), {
                "hub.mode": "subscribe",
                "hub.verify_token": "token-errado",
                "hub.challenge": "12345",
            })
        self.assertEqual(resposta.status_code, 403)

    def test_webhook_post_grava_mensagem_recebida(self):
        payload = {
            "entry": [{
                "changes": [{
                    "value": {
                        "contacts": [{"wa_id": "5565999998888", "profile": {"name": "João"}}],
                        "messages": [{"from": "5565999998888", "text": {"body": "Olá, tudo bem?"}}],
                    }
                }]
            }]
        }
        resposta = self.client.post(
            reverse("whatsapp_webhook"), data=json.dumps(payload), content_type="application/json"
        )
        self.assertEqual(resposta.status_code, 200)
        mensagem = MensagemWhatsapp.objects.get()
        self.assertEqual(mensagem.remetente, "João")
        self.assertEqual(mensagem.texto, "Olá, tudo bem?")

    def test_webhook_post_rejeita_assinatura_invalida_quando_app_secret_configurado(self):
        payload = json.dumps({"entry": []}).encode()
        with override_settings(WHATSAPP_APP_SECRET="segredo-do-app"):
            resposta = self.client.post(
                reverse("whatsapp_webhook"), data=payload, content_type="application/json",
                HTTP_X_HUB_SIGNATURE_256="sha256=assinatura-forjada",
            )
        self.assertEqual(resposta.status_code, 403)

    def test_webhook_post_aceita_assinatura_valida_quando_app_secret_configurado(self):
        payload = json.dumps({"entry": []}).encode()
        segredo = "segredo-do-app"
        assinatura = "sha256=" + hmac.new(segredo.encode(), payload, hashlib.sha256).hexdigest()
        with override_settings(WHATSAPP_APP_SECRET=segredo):
            resposta = self.client.post(
                reverse("whatsapp_webhook"), data=payload, content_type="application/json",
                HTTP_X_HUB_SIGNATURE_256=assinatura,
            )
        self.assertEqual(resposta.status_code, 200)


# ==========================================================================
# Testes das melhorias de apoio à decisão (backfill de histórico,
# backtesting do robô, benchmark Ibovespa/CDI, concentração por setor,
# métricas de risco e avisos proativos via WhatsApp)
# ==========================================================================
class BuscarHistoricoPrecosTests(TestCase):
    """core.services.buscar_historico_precos / backfill_historico_cotacoes"""

    def setUp(self):
        self.ativo = Ativo.objects.create(ticker="PETR4")

    @patch("core.services.requests.get")
    def test_converte_timestamp_unix_e_remove_data_duplicada(self, mock_get):
        base = int(datetime(2026, 9, 1, tzinfo=dt_timezone.utc).timestamp())
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: {
            "results": [{
                "symbol": "PETR4",
                "historicalDataPrice": [
                    {"date": base + 86400, "close": 31.0},
                    {"date": base, "close": 30.0},
                    {"date": base, "close": 30.5},  # mesma data - só a primeira deve ficar
                ],
            }]
        }
        historico = buscar_historico_precos("PETR4", dias=30)
        self.assertEqual(len(historico), 2)
        self.assertEqual(historico[0]["data"], date(2026, 9, 1))
        self.assertEqual(historico[0]["fechamento"], Decimal("30.00"))
        self.assertEqual(historico[1]["data"], date(2026, 9, 2))

    @patch("core.services.requests.get")
    def test_ticker_nao_encontrado_gera_brapierror(self, mock_get):
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: {"results": []}
        with self.assertRaises(BrapiError):
            buscar_historico_precos("XXXX9")

    @patch("core.services.requests.get")
    def test_falha_de_rede_gera_brapierror(self, mock_get):
        mock_get.side_effect = requests.RequestException("timeout")
        with self.assertRaises(BrapiError):
            buscar_historico_precos("PETR4")

    @patch("core.services.requests.get")
    def test_backfill_grava_so_as_datas_que_faltam_sem_sobrescrever_existentes(self, mock_get):
        Cotacao.objects.create(ativo=self.ativo, data=date(2026, 9, 1), preco_fechamento=Decimal("99.00"))
        base = int(datetime(2026, 9, 1, tzinfo=dt_timezone.utc).timestamp())
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: {"results": [{"historicalDataPrice": [
            {"date": base, "close": 30.0},
            {"date": base + 86400, "close": 31.0},
        ]}]}

        gravadas = backfill_historico_cotacoes(self.ativo)

        self.assertEqual(gravadas, 1)  # só 02/09 - 01/09 já existia
        self.assertEqual(Cotacao.objects.filter(ativo=self.ativo).count(), 2)
        cotacao_existente = Cotacao.objects.get(ativo=self.ativo, data=date(2026, 9, 1))
        self.assertEqual(cotacao_existente.preco_fechamento, Decimal("99.00"))  # não foi sobrescrita

    @patch("core.services.requests.get")
    def test_backfill_sem_pontos_no_historico_nao_quebra(self, mock_get):
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: {"results": [{"historicalDataPrice": []}]}
        self.assertEqual(backfill_historico_cotacoes(self.ativo), 0)

    @patch("core.services.requests.get")
    def test_historico_traz_o_volume_de_cada_ponto_sem_custo_extra(self, mock_get):
        base = int(datetime(2026, 9, 1, tzinfo=dt_timezone.utc).timestamp())
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: {"results": [{"historicalDataPrice": [
            {"date": base, "close": 30.0, "volume": 123456},
        ]}]}
        historico = buscar_historico_precos("PETR4", dias=30)
        self.assertEqual(historico[0]["volume"], 123456)

    @patch("core.services.requests.get")
    def test_backfill_grava_volume_nas_cotacoes_novas(self, mock_get):
        base = int(datetime(2026, 9, 1, tzinfo=dt_timezone.utc).timestamp())
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: {"results": [{"historicalDataPrice": [
            {"date": base, "close": 30.0, "volume": 111},
        ]}]}
        backfill_historico_cotacoes(self.ativo)
        cotacao = Cotacao.objects.get(ativo=self.ativo, data=date(2026, 9, 1))
        self.assertEqual(cotacao.volume, 111)

    @patch("core.services.requests.get")
    def test_backfill_completa_volume_faltante_em_cotacao_ja_existente_sem_mexer_no_preco(self, mock_get):
        # simula uma cotação gravada antes do campo volume existir (ou antes
        # de ele ter sido preenchido) - rodar o backfill de novo (ex: comando
        # "backfill_cotacoes --todos") deve completar só o volume, mantendo o
        # preço já salvo intacto.
        Cotacao.objects.create(ativo=self.ativo, data=date(2026, 9, 1), preco_fechamento=Decimal("99.00"), volume=None)
        base = int(datetime(2026, 9, 1, tzinfo=dt_timezone.utc).timestamp())
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: {"results": [{"historicalDataPrice": [
            {"date": base, "close": 30.0, "volume": 555},
        ]}]}

        gravadas = backfill_historico_cotacoes(self.ativo)

        self.assertEqual(gravadas, 0)  # a data já existia - não conta como "nova"
        cotacao = Cotacao.objects.get(ativo=self.ativo, data=date(2026, 9, 1))
        self.assertEqual(cotacao.volume, 555)  # completado
        self.assertEqual(cotacao.preco_fechamento, Decimal("99.00"))  # preço não foi tocado

    @patch("core.services.requests.get")
    def test_backfill_nao_sobrescreve_volume_ja_preenchido(self, mock_get):
        Cotacao.objects.create(ativo=self.ativo, data=date(2026, 9, 1), preco_fechamento=Decimal("99.00"), volume=777)
        base = int(datetime(2026, 9, 1, tzinfo=dt_timezone.utc).timestamp())
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: {"results": [{"historicalDataPrice": [
            {"date": base, "close": 30.0, "volume": 555},
        ]}]}

        backfill_historico_cotacoes(self.ativo)

        cotacao = Cotacao.objects.get(ativo=self.ativo, data=date(2026, 9, 1))
        self.assertEqual(cotacao.volume, 777)

    @patch("core.services.requests.get")
    def test_historico_traz_maxima_e_minima_de_cada_ponto_sem_custo_extra(self, mock_get):
        base = int(datetime(2026, 9, 1, tzinfo=dt_timezone.utc).timestamp())
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: {"results": [{"historicalDataPrice": [
            {"date": base, "close": 30.0, "high": 31.5, "low": 29.2},
        ]}]}
        historico = buscar_historico_precos("PETR4", dias=30)
        self.assertEqual(historico[0]["maxima"], Decimal("31.5"))
        self.assertEqual(historico[0]["minima"], Decimal("29.2"))

    @patch("core.services.requests.get")
    def test_backfill_grava_maxima_e_minima_nas_cotacoes_novas(self, mock_get):
        base = int(datetime(2026, 9, 1, tzinfo=dt_timezone.utc).timestamp())
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: {"results": [{"historicalDataPrice": [
            {"date": base, "close": 30.0, "high": 31.0, "low": 29.0},
        ]}]}
        backfill_historico_cotacoes(self.ativo)
        cotacao = Cotacao.objects.get(ativo=self.ativo, data=date(2026, 9, 1))
        self.assertEqual(cotacao.maxima, Decimal("31.0"))
        self.assertEqual(cotacao.minima, Decimal("29.0"))

    @patch("core.services.requests.get")
    def test_backfill_completa_maxima_e_minima_faltantes_em_cotacao_ja_existente_sem_mexer_no_preco(self, mock_get):
        Cotacao.objects.create(
            ativo=self.ativo, data=date(2026, 9, 1), preco_fechamento=Decimal("99.00"), maxima=None, minima=None,
        )
        base = int(datetime(2026, 9, 1, tzinfo=dt_timezone.utc).timestamp())
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: {"results": [{"historicalDataPrice": [
            {"date": base, "close": 30.0, "high": 40.0, "low": 20.0},
        ]}]}

        gravadas = backfill_historico_cotacoes(self.ativo)

        self.assertEqual(gravadas, 0)
        cotacao = Cotacao.objects.get(ativo=self.ativo, data=date(2026, 9, 1))
        self.assertEqual(cotacao.maxima, Decimal("40.0"))
        self.assertEqual(cotacao.minima, Decimal("20.0"))
        self.assertEqual(cotacao.preco_fechamento, Decimal("99.00"))

    @patch("core.services.requests.get")
    def test_backfill_nao_sobrescreve_maxima_e_minima_ja_preenchidas(self, mock_get):
        Cotacao.objects.create(
            ativo=self.ativo, data=date(2026, 9, 1), preco_fechamento=Decimal("99.00"),
            maxima=Decimal("35.0"), minima=Decimal("28.0"),
        )
        base = int(datetime(2026, 9, 1, tzinfo=dt_timezone.utc).timestamp())
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: {"results": [{"historicalDataPrice": [
            {"date": base, "close": 30.0, "high": 40.0, "low": 20.0},
        ]}]}

        backfill_historico_cotacoes(self.ativo)

        cotacao = Cotacao.objects.get(ativo=self.ativo, data=date(2026, 9, 1))
        self.assertEqual(cotacao.maxima, Decimal("35.0"))
        self.assertEqual(cotacao.minima, Decimal("28.0"))


class BackfillAoRegistrarOperacaoTests(TestCase):
    """A primeira compra de um ativo novo aciona o backfill automático (ver core.views.operacao_nova)."""

    def setUp(self):
        self.usuario = User.objects.create_user(username="investidor_backfill", password="SenhaForte123!")
        self.client.login(username="investidor_backfill", password="SenhaForte123!")

    def _resposta_por_endpoint(self, base_timestamp):
        def _resposta(url, params=None, timeout=None):
            resposta = Mock()
            resposta.raise_for_status = lambda: None
            if params and "range" in params:
                resposta.json = lambda: {"results": [{"historicalDataPrice": [
                    {"date": base_timestamp, "close": 29.0},
                    {"date": base_timestamp + 86400, "close": 29.5},
                ]}]}
            else:
                resposta.json = lambda: {
                    "results": [{"regularMarketPrice": 30.0, "regularMarketChangePercent": 1.0}]
                }
            return resposta
        return _resposta

    @patch("core.services.requests.get")
    def test_primeira_compra_de_ativo_novo_preenche_historico(self, mock_get):
        base = int(datetime(2026, 1, 1, tzinfo=dt_timezone.utc).timestamp())
        mock_get.side_effect = self._resposta_por_endpoint(base)

        self.client.post(reverse("core:operacao_nova"), {
            "ticker": "PETR4", "tipo": Operacao.COMPRA, "quantidade": "10",
            "preco_unitario": "30.00", "data_operacao": date.today().isoformat(),
            "meta_lucro_pct": "", "meta_perda_pct": "", "observacao": "",
        })

        ativo = Ativo.objects.get(ticker="PETR4")
        # 1 cotação de hoje (atualizar_cotacao_diaria) + 2 do backfill (datas de 2026, diferentes de hoje)
        self.assertEqual(ativo.cotacoes.count(), 3)

    @patch("core.services.requests.get")
    def test_segunda_compra_do_mesmo_ativo_nao_aciona_backfill_de_novo(self, mock_get):
        base = int(datetime(2026, 1, 1, tzinfo=dt_timezone.utc).timestamp())
        mock_get.side_effect = self._resposta_por_endpoint(base)

        dados_post = {
            "ticker": "VALE3", "tipo": Operacao.COMPRA, "quantidade": "5",
            "preco_unitario": "60.00", "data_operacao": date.today().isoformat(),
            "meta_lucro_pct": "", "meta_perda_pct": "", "observacao": "",
        }
        self.client.post(reverse("core:operacao_nova"), dados_post)
        total_apos_primeira = mock_get.call_count

        self.client.post(reverse("core:operacao_nova"), dados_post)
        # a 2ª compra só atualiza a cotação de hoje (1 chamada a mais) - não repete o backfill,
        # porque o ativo já deixou de ter "menos de 2 cotações"
        self.assertEqual(mock_get.call_count, total_apos_primeira + 1)


class IndicadoresRefatoradosTests(TestCase):
    """Garante que extrair o núcleo puro (por preços) não mudou o resultado de analisar_indicadores_tecnicos."""

    def test_versao_por_ativo_bate_com_versao_por_precos(self):
        ativo = Ativo.objects.create(ticker="ITSA4")
        hoje = date.today()
        for i in range(40):
            Cotacao.objects.create(
                ativo=ativo, data=hoje - timedelta(days=40 - i),
                preco_fechamento=Decimal("10.00") + Decimal(i) * Decimal("0.10"),
            )
        via_ativo = analisar_indicadores_tecnicos(ativo)
        precos = [float(v) for v in ativo.cotacoes.order_by("data").values_list("preco_fechamento", flat=True)]
        via_precos = analisar_indicadores_tecnicos_precos(precos)
        self.assertEqual(via_ativo, via_precos)


class BacktestRoboTests(TestCase):
    """core.services.backtest_sinais_robo"""

    def setUp(self):
        self.ativo = Ativo.objects.create(ticker="VALE3")

    def test_sem_historico_suficiente_marca_dados_insuficientes(self):
        Cotacao.objects.create(ativo=self.ativo, data=date.today(), preco_fechamento=Decimal("60.00"))
        resultado = backtest_sinais_robo(self.ativo, dias_retorno=5)
        self.assertEqual(resultado["total_sinais"], 0)
        self.assertTrue(resultado["dados_insuficientes"])

    def test_serie_consistentemente_em_alta_acerta_todo_sinal_de_compra(self):
        # uptrend com pequenos recuos periódicos (não uma reta perfeita, que satura o RSI em
        # 100 e nunca gera sinal de compra) - qualquer sinal de compra que o robô emita nessa
        # série de tendência forte deve "acertar" (preço sempre mais alto adiante)
        hoje = date.today()
        passo = [Decimal("0.35"), Decimal("0.30"), Decimal("-0.10"), Decimal("0.25"), Decimal("0.30"), Decimal("-0.05")]
        preco = Decimal("10.00")
        for i in range(60):
            Cotacao.objects.create(ativo=self.ativo, data=hoje - timedelta(days=60 - i), preco_fechamento=preco)
            preco += passo[i % len(passo)]

        resultado = backtest_sinais_robo(self.ativo, dias_retorno=3)

        self.assertFalse(resultado["dados_insuficientes"])
        self.assertGreater(resultado["sinais_compra"], 0)
        self.assertEqual(resultado["taxa_acerto_compra_pct"], 100.0)
        for sinal in resultado["sinais"]:
            if sinal["sinal"] == "COMPRA":
                self.assertTrue(sinal["acerto"])
                self.assertGreater(sinal["retorno_pct"], 0)

    def test_view_backtest_robo_renderiza_para_ativos_do_usuario(self):
        usuario = User.objects.create_user(username="investidor_bt", password="SenhaForte123!")
        client_logado = self.client
        client_logado.login(username="investidor_bt", password="SenhaForte123!")
        Operacao.objects.create(
            usuario=usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("10.00"), data_operacao=date.today(),
        )
        resposta = client_logado.get(reverse("core:backtest_robo"))
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "VALE3")

    def test_view_backtest_robo_exige_login(self):
        resposta = self.client.get(reverse("core:backtest_robo"))
        self.assertEqual(resposta.status_code, 302)


class BenchmarkTests(TestCase):
    """core.services: buscar_historico_cdi, atualizar_benchmarks, calcular_comparativo_benchmark"""

    def setUp(self):
        self.usuario = User.objects.create_user(username="investidor_bench", password="SenhaForte123!")
        self.ativo = Ativo.objects.create(ticker="WEGE3")

    @patch("core.services.requests.get")
    def test_buscar_historico_cdi_converte_data_br_para_iso(self, mock_get):
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: [
            {"data": "01/09/2026", "valor": "0.0539"},
            {"data": "02/09/2026", "valor": "0.0540"},
        ]
        historico = buscar_historico_cdi(dias=30)
        self.assertEqual(historico[0]["data"], date(2026, 9, 1))
        self.assertEqual(historico[0]["taxa_pct"], Decimal("0.0539"))
        self.assertEqual(historico[1]["data"], date(2026, 9, 2))

    @patch("core.services.requests.get")
    def test_buscar_historico_cdi_falha_de_rede_gera_brapierror(self, mock_get):
        mock_get.side_effect = requests.RequestException("Banco Central fora do ar")
        with self.assertRaises(BrapiError):
            buscar_historico_cdi()

    def test_retorno_indice_periodo_compara_primeiro_e_ultimo_valor(self):
        CotacaoIndice.objects.create(indice=CotacaoIndice.IBOVESPA, data=date(2026, 9, 1), valor=Decimal("130000"))
        CotacaoIndice.objects.create(indice=CotacaoIndice.IBOVESPA, data=date(2026, 9, 10), valor=Decimal("136500"))
        retorno = _retorno_indice_periodo(CotacaoIndice.IBOVESPA, date(2026, 9, 1), date(2026, 9, 10))
        self.assertEqual(retorno, Decimal("5.00"))

    def test_retorno_indice_sem_dados_retorna_none(self):
        self.assertIsNone(_retorno_indice_periodo(CotacaoIndice.IBOVESPA, date(2026, 9, 1), date(2026, 9, 10)))

    def test_retorno_cdi_periodo_compoe_juros_em_vez_de_somar(self):
        # taxas diárias "grandes" o bastante pra o efeito de juros sobre juros sobreviver ao
        # arredondamento de 2 casas decimais do resultado (com 0.05% a diferença some no quantize)
        CotacaoIndice.objects.create(indice=CotacaoIndice.CDI, data=date(2026, 9, 1), valor=Decimal("1.00"))
        CotacaoIndice.objects.create(indice=CotacaoIndice.CDI, data=date(2026, 9, 2), valor=Decimal("1.00"))
        retorno = _retorno_cdi_periodo(date(2026, 9, 1), date(2026, 9, 2))
        esperado = ((Decimal("1.01") * Decimal("1.01")) - 1) * 100
        self.assertEqual(retorno, esperado.quantize(Decimal("0.01")))
        # a composição rende um pouquinho mais que a soma simples (1.00 + 1.00 = 2.00)
        self.assertGreater(retorno, Decimal("2.00"))

    def test_comparativo_benchmark_sem_posicao_comprada_retorna_none(self):
        self.assertIsNone(calcular_comparativo_benchmark(self.usuario, posicoes=[]))

    def test_comparativo_benchmark_calcula_retorno_da_carteira(self):
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("50.00"), data_operacao=date.today() - timedelta(days=10),
        )
        Cotacao.objects.create(
            ativo=self.ativo, data=date.today() - timedelta(days=10), preco_fechamento=Decimal("50.00")
        )
        Cotacao.objects.create(ativo=self.ativo, data=date.today(), preco_fechamento=Decimal("55.00"))

        comparativo = calcular_comparativo_benchmark(self.usuario)

        self.assertIsNotNone(comparativo)
        self.assertEqual(comparativo["retorno_carteira_pct"], Decimal("10.00"))
        self.assertIsNone(comparativo["retorno_ibovespa_pct"])  # sem CotacaoIndice cadastrada ainda

    def test_dashboard_exibe_comparativo_quando_disponivel(self):
        self.client.login(username="investidor_bench", password="SenhaForte123!")
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("50.00"), data_operacao=date.today() - timedelta(days=5),
        )
        Cotacao.objects.create(
            ativo=self.ativo, data=date.today() - timedelta(days=5), preco_fechamento=Decimal("50.00")
        )
        Cotacao.objects.create(ativo=self.ativo, data=date.today(), preco_fechamento=Decimal("52.00"))
        resposta = self.client.get(reverse("core:dashboard"))
        self.assertContains(resposta, "Sua carteira x mercado")

    def test_posicoes_exibe_comparativo_quando_disponivel(self):
        self.client.login(username="investidor_bench", password="SenhaForte123!")
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("50.00"), data_operacao=date.today() - timedelta(days=5),
        )
        Cotacao.objects.create(
            ativo=self.ativo, data=date.today() - timedelta(days=5), preco_fechamento=Decimal("50.00")
        )
        Cotacao.objects.create(ativo=self.ativo, data=date.today(), preco_fechamento=Decimal("52.00"))

        resposta = self.client.get(reverse("core:posicoes"))
        self.assertContains(resposta, "Sua carteira x mercado")

    @patch("core.services.requests.get")
    def test_botao_atualizar_benchmarks_grava_historico_e_redireciona(self, mock_get):
        self.client.login(username="investidor_bench", password="SenhaForte123!")
        base = int(datetime(2026, 1, 1, tzinfo=dt_timezone.utc).timestamp())

        def _resposta(url, params=None, timeout=None):
            resposta = Mock()
            resposta.raise_for_status = lambda: None
            if "bcb.gov.br" in url:
                resposta.json = lambda: [{"data": "01/01/2026", "valor": "0.05"}]
            else:
                resposta.json = lambda: {"results": [{"historicalDataPrice": [{"date": base, "close": 130000.0}]}]}
            return resposta

        mock_get.side_effect = _resposta

        resposta = self.client.get(reverse("core:atualizar_benchmarks") + "?next=" + reverse("core:posicoes"))

        self.assertRedirects(resposta, reverse("core:posicoes"))
        self.assertEqual(CotacaoIndice.objects.filter(indice=CotacaoIndice.IBOVESPA).count(), 1)
        self.assertEqual(CotacaoIndice.objects.filter(indice=CotacaoIndice.CDI).count(), 1)


class ComandoAtualizarBenchmarksTests(TestCase):
    @patch("core.services.requests.get")
    def test_atualiza_ibovespa_e_cdi_de_fontes_independentes(self, mock_get):
        base = int(datetime(2026, 1, 1, tzinfo=dt_timezone.utc).timestamp())

        def _resposta(url, params=None, timeout=None):
            resposta = Mock()
            resposta.raise_for_status = lambda: None
            if "bcb.gov.br" in url:
                resposta.json = lambda: [{"data": "01/01/2026", "valor": "0.05"}]
            else:
                resposta.json = lambda: {"results": [{"historicalDataPrice": [
                    {"date": base, "close": 130000.0}
                ]}]}
            return resposta

        mock_get.side_effect = _resposta
        saida = StringIO()
        call_command("atualizar_benchmarks", stdout=saida)

        self.assertEqual(CotacaoIndice.objects.filter(indice=CotacaoIndice.IBOVESPA).count(), 1)
        self.assertEqual(CotacaoIndice.objects.filter(indice=CotacaoIndice.CDI).count(), 1)
        self.assertIn("Ibovespa: 1", saida.getvalue())

    @patch("core.services.requests.get")
    def test_falha_em_uma_fonte_nao_impede_a_outra(self, mock_get):
        def _resposta(url, params=None, timeout=None):
            if "bcb.gov.br" in url:
                raise requests.RequestException("Banco Central fora do ar")
            resposta = Mock()
            resposta.raise_for_status = lambda: None
            base = int(datetime(2026, 1, 1, tzinfo=dt_timezone.utc).timestamp())
            resposta.json = lambda: {"results": [{"historicalDataPrice": [{"date": base, "close": 130000.0}]}]}
            return resposta

        mock_get.side_effect = _resposta
        saida = StringIO()
        call_command("atualizar_benchmarks", stdout=saida)

        self.assertEqual(CotacaoIndice.objects.filter(indice=CotacaoIndice.IBOVESPA).count(), 1)
        self.assertEqual(CotacaoIndice.objects.filter(indice=CotacaoIndice.CDI).count(), 0)
        self.assertIn("FALHA", saida.getvalue())


class ComandoBackfillCotacoesTests(TestCase):
    @patch("core.services.requests.get")
    def test_processa_apenas_ativos_com_poucas_cotacoes(self, mock_get):
        ativo_pouco = Ativo.objects.create(ticker="PETR4")
        ativo_completo = Ativo.objects.create(ticker="VALE3")
        hoje = date.today()
        for i in range(40):
            Cotacao.objects.create(
                ativo=ativo_completo, data=hoje - timedelta(days=40 - i), preco_fechamento=Decimal("10.00")
            )

        base = int(datetime(2026, 1, 1, tzinfo=dt_timezone.utc).timestamp())
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: {"results": [{"historicalDataPrice": [
            {"date": base, "close": 30.0}, {"date": base + 86400, "close": 31.0},
        ]}]}

        saida = StringIO()
        call_command("backfill_cotacoes", "--minimo", "35", stdout=saida)

        self.assertEqual(Cotacao.objects.filter(ativo=ativo_pouco).count(), 2)
        mock_get.assert_called_once()  # só ativo_pouco foi processado - ativo_completo já tem 40 >= 35
        self.assertIn("1 ativo(s) com menos de 35", saida.getvalue())

    @patch("core.services.requests.get")
    def test_todos_processa_mesmo_ativo_com_historico_completo(self, mock_get):
        # --todos serve pra completar o campo volume em cotações antigas de
        # ativos que já têm histórico suficiente (ver core.services.
        # backfill_historico_cotacoes) - sem ele, esses ativos nunca seriam
        # reprocessados pelo comando (ficam sempre abaixo do --minimo).
        ativo_completo = Ativo.objects.create(ticker="VALE3")
        hoje = date.today()
        for i in range(40):
            Cotacao.objects.create(
                ativo=ativo_completo, data=hoje - timedelta(days=40 - i), preco_fechamento=Decimal("10.00")
            )

        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: {"results": [{"historicalDataPrice": []}]}

        saida = StringIO()
        call_command("backfill_cotacoes", "--todos", stdout=saida)

        mock_get.assert_called_once()
        self.assertIn("no total (--todos", saida.getvalue())


class ConcentracaoSetorTests(TestCase):
    """core.services.calcular_concentracao_setor"""

    def setUp(self):
        self.usuario = User.objects.create_user(username="investidor_setor", password="SenhaForte123!")
        self.banco1 = Ativo.objects.create(ticker="ITUB4", setor="Bancos")
        self.banco2 = Ativo.objects.create(ticker="BBDC4", setor="Bancos")
        self.varejo = Ativo.objects.create(ticker="MGLU3", setor="Varejo")

    def _comprar(self, ativo, preco):
        Operacao.objects.create(
            usuario=self.usuario, ativo=ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal(preco), data_operacao=date.today(),
        )
        Cotacao.objects.create(ativo=ativo, data=date.today(), preco_fechamento=Decimal(preco))

    def test_agrupa_por_setor_e_calcula_percentual(self):
        self._comprar(self.banco1, "10.00")
        self._comprar(self.banco2, "10.00")
        self._comprar(self.varejo, "10.00")

        concentracao = calcular_concentracao_setor(calcular_posicoes(self.usuario))
        por_setor = {item["setor"]: item["percentual"] for item in concentracao}

        self.assertEqual(por_setor["Bancos"], Decimal("66.67"))
        self.assertEqual(por_setor["Varejo"], Decimal("33.33"))
        # ordenado do maior para o menor
        self.assertEqual(concentracao[0]["setor"], "Bancos")

    def test_ativo_sem_setor_cai_em_sem_setor(self):
        sem_setor = Ativo.objects.create(ticker="XYZ3")
        self._comprar(sem_setor, "1.00")

        concentracao = calcular_concentracao_setor(calcular_posicoes(self.usuario))

        self.assertEqual(concentracao[0]["setor"], "Sem setor")

    def test_sem_posicoes_compradas_retorna_lista_vazia(self):
        self.assertEqual(calcular_concentracao_setor([]), [])

    def test_reserva_nao_entra_na_concentracao(self):
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.banco1, tipo=Operacao.RESERVAR,
            quantidade=1, preco_unitario=Decimal("10.00"), data_operacao=date.today(),
        )
        concentracao = calcular_concentracao_setor(calcular_posicoes(self.usuario))
        self.assertEqual(concentracao, [])


class MetricasRiscoTests(TestCase):
    """core.services.calcular_metricas_risco"""

    def setUp(self):
        self.usuario = User.objects.create_user(username="investidor_risco", password="SenhaForte123!")
        self.ativo = Ativo.objects.create(ticker="PETR4")
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("30.00"), data_operacao=date.today(),
        )

    def test_sem_historico_suficiente_retorna_none_nos_indicadores(self):
        Cotacao.objects.create(ativo=self.ativo, data=date.today(), preco_fechamento=Decimal("30.00"))
        metricas = calcular_metricas_risco(calcular_posicoes(self.usuario))
        item = metricas["por_ativo"][0]
        self.assertIsNone(item["volatilidade_pct"])
        self.assertIsNone(item["drawdown_pct"])

    def test_drawdown_maximo_detectado_corretamente(self):
        # pico em 36, fundo em 27 depois do pico -> queda de 25% do topo ao fundo
        precos = [Decimal("30.00"), Decimal("36.00"), Decimal("27.00"), Decimal("28.00")]
        hoje = date.today()
        for i, preco in enumerate(precos):
            Cotacao.objects.create(ativo=self.ativo, data=hoje - timedelta(days=len(precos) - i), preco_fechamento=preco)

        item = calcular_metricas_risco(calcular_posicoes(self.usuario))["por_ativo"][0]

        self.assertEqual(item["drawdown_pct"], -25.0)
        self.assertIsNotNone(item["volatilidade_pct"])
        self.assertGreater(item["volatilidade_pct"], 0)

    def test_concentracao_alta_detectada_quando_um_ativo_domina_a_carteira(self):
        outro = Ativo.objects.create(ticker="VALE3")
        Operacao.objects.create(
            usuario=self.usuario, ativo=outro, tipo=Operacao.COMPRA,
            quantidade=1, preco_unitario=Decimal("1.00"), data_operacao=date.today(),
        )
        Cotacao.objects.create(ativo=self.ativo, data=date.today(), preco_fechamento=Decimal("30.00"))
        Cotacao.objects.create(ativo=outro, data=date.today(), preco_fechamento=Decimal("1.00"))

        metricas = calcular_metricas_risco(calcular_posicoes(self.usuario))

        self.assertTrue(metricas["concentracao_alta"])
        self.assertEqual(metricas["maior_posicao_ativo"], self.ativo)

    def test_carteira_bem_distribuida_nao_gera_alerta_de_concentracao(self):
        # com só 2 ativos, mesmo 50/50 (o máximo de diversificação possível) já passa dos 40%
        # do limiar de alerta - uma carteira "bem distribuída" de verdade precisa de mais ativos,
        # cada um abaixo do limiar
        outro = Ativo.objects.create(ticker="VALE3")
        terceiro = Ativo.objects.create(ticker="ITSA4")
        Operacao.objects.create(
            usuario=self.usuario, ativo=outro, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("30.00"), data_operacao=date.today(),
        )
        Operacao.objects.create(
            usuario=self.usuario, ativo=terceiro, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("30.00"), data_operacao=date.today(),
        )
        Cotacao.objects.create(ativo=self.ativo, data=date.today(), preco_fechamento=Decimal("30.00"))
        Cotacao.objects.create(ativo=outro, data=date.today(), preco_fechamento=Decimal("30.00"))
        Cotacao.objects.create(ativo=terceiro, data=date.today(), preco_fechamento=Decimal("30.00"))

        metricas = calcular_metricas_risco(calcular_posicoes(self.usuario))

        self.assertFalse(metricas["concentracao_alta"])
        self.assertEqual(metricas["maior_posicao_pct"], Decimal("33.33"))

    def test_posicoes_page_exibe_secao_de_risco_e_setor(self):
        self.client.login(username="investidor_risco", password="SenhaForte123!")
        Cotacao.objects.create(ativo=self.ativo, data=date.today(), preco_fechamento=Decimal("33.00"))
        resposta = self.client.get(reverse("core:posicoes"))
        self.assertContains(resposta, "Indicadores de risco")


class EnviarWhatsappTests(TestCase):
    """core.services.enviar_whatsapp / whatsapp_envio_configurado e o disparo a partir dos alertas."""

    def setUp(self):
        self.usuario = User.objects.create_user(username="investidor_wa", password="SenhaForte123!")
        self.ativo = Ativo.objects.create(ticker="PETR4")

    def test_envio_desligado_sem_credenciais_configuradas(self):
        with override_settings(WHATSAPP_ACCESS_TOKEN="", WHATSAPP_PHONE_NUMBER_ID=""):
            self.assertFalse(whatsapp_envio_configurado())
            self.assertFalse(enviar_whatsapp("5565999998888", "teste"))

    @patch("core.services.requests.post")
    def test_envio_bem_sucedido_chama_a_api_da_meta_corretamente(self, mock_post):
        mock_post.return_value.raise_for_status = lambda: None
        with override_settings(WHATSAPP_ACCESS_TOKEN="token123", WHATSAPP_PHONE_NUMBER_ID="1234567890"):
            resultado = enviar_whatsapp("5565999998888", "PETR4: meta de lucro atingida")

        self.assertTrue(resultado)
        url_chamada = mock_post.call_args[0][0]
        self.assertIn("1234567890", url_chamada)
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["to"], "5565999998888")
        self.assertEqual(payload["text"]["body"], "PETR4: meta de lucro atingida")

    @patch("core.services.requests.post")
    def test_falha_na_api_retorna_false_sem_lancar_excecao(self, mock_post):
        mock_post.side_effect = requests.RequestException("fora do ar")
        with override_settings(WHATSAPP_ACCESS_TOKEN="token123", WHATSAPP_PHONE_NUMBER_ID="1234567890"):
            self.assertFalse(enviar_whatsapp("5565999998888", "teste"))

    @patch("core.services.enviar_whatsapp")
    def test_alerta_de_meta_dispara_envio_quando_usuario_tem_numero_cadastrado(self, mock_enviar):
        PerfilUsuario.objects.create(usuario=self.usuario, numero_whatsapp="5565999998888")
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("20.00"), data_operacao=date.today(),
            meta_lucro_pct=Decimal("5.0"), meta_perda_pct=Decimal("-5.0"),
        )
        Cotacao.objects.create(ativo=self.ativo, data=date.today(), preco_fechamento=Decimal("25.00"))

        with override_settings(WHATSAPP_ACCESS_TOKEN="token123", WHATSAPP_PHONE_NUMBER_ID="1234567890"):
            gerar_alertas_para_usuario(self.usuario)

        mock_enviar.assert_called_once()
        self.assertEqual(mock_enviar.call_args[0][0], "5565999998888")

    @patch("core.services.enviar_whatsapp")
    def test_alerta_nao_dispara_envio_sem_numero_cadastrado(self, mock_enviar):
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("20.00"), data_operacao=date.today(),
            meta_lucro_pct=Decimal("5.0"), meta_perda_pct=Decimal("-5.0"),
        )
        Cotacao.objects.create(ativo=self.ativo, data=date.today(), preco_fechamento=Decimal("25.00"))

        with override_settings(WHATSAPP_ACCESS_TOKEN="token123", WHATSAPP_PHONE_NUMBER_ID="1234567890"):
            gerar_alertas_para_usuario(self.usuario)

        mock_enviar.assert_not_called()

    @patch("core.services.enviar_whatsapp")
    def test_alerta_nao_dispara_envio_quando_credenciais_nao_configuradas(self, mock_enviar):
        PerfilUsuario.objects.create(usuario=self.usuario, numero_whatsapp="5565999998888")
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("20.00"), data_operacao=date.today(),
            meta_lucro_pct=Decimal("5.0"), meta_perda_pct=Decimal("-5.0"),
        )
        Cotacao.objects.create(ativo=self.ativo, data=date.today(), preco_fechamento=Decimal("25.00"))

        with override_settings(WHATSAPP_ACCESS_TOKEN="", WHATSAPP_PHONE_NUMBER_ID=""):
            gerar_alertas_para_usuario(self.usuario)

        mock_enviar.assert_not_called()


class AnaliseB3IaTests(TestCase):
    """core.services: ia_configurada, _parsear_resposta_ia_em_grade, gerar_analise_b3_ia."""

    def _sinal_exemplo(self):
        ativo = Ativo.objects.create(ticker="PETR4")
        return {
            "ativo": ativo, "tendencia_label": "Alta", "rsi": 45.0, "rsi_label": "Neutro",
            "macd_label": "Cruzamento de alta", "sinal_geral_label": "Sinal de compra",
            "votos_compra": 2, "votos_venda": 0,
        }

    def test_nao_configurada_sem_chave(self):
        with override_settings(OPENAI_API_KEY=""):
            self.assertFalse(ia_configurada())

    def test_configurada_com_chave(self):
        with override_settings(OPENAI_API_KEY="sk-teste"):
            self.assertTrue(ia_configurada())

    def test_gerar_analise_levanta_erro_sem_chave_configurada(self):
        with override_settings(OPENAI_API_KEY=""):
            with self.assertRaises(IAError):
                gerar_analise_b3_ia([self._sinal_exemplo()], [], [])

    def test_parsear_resposta_em_linhas_validas(self):
        texto = "MERCADO|ALTA|Pregão positivo, puxado pelo setor financeiro.\nPETR4|NEUTRO|Sem direção clara no curto prazo."
        linhas = _parsear_resposta_ia_em_grade(texto)
        self.assertEqual(len(linhas), 2)
        self.assertTrue(linhas[0]["eh_mercado_geral"])
        self.assertEqual(linhas[0]["situacao_classe"], "alta")
        self.assertFalse(linhas[1]["eh_mercado_geral"])
        self.assertEqual(linhas[1]["ticker"], "PETR4")

    def test_parsear_ignora_linhas_fora_do_formato(self):
        texto = "isso não segue o formato pedido\nPETR4|ALTA|Comentário válido\nVALE3|SITUACAO_INVALIDA|Comentário"
        linhas = _parsear_resposta_ia_em_grade(texto)
        self.assertEqual(len(linhas), 1)
        self.assertEqual(linhas[0]["ticker"], "PETR4")

    def test_parsear_texto_totalmente_fora_do_formato_fica_vazio(self):
        self.assertEqual(_parsear_resposta_ia_em_grade("Só um parágrafo qualquer, sem o formato pedido."), [])

    def test_prompt_inclui_dados_do_sinal_e_das_variacoes(self):
        maiores_altas = [{"stock": "VALE3", "name": "Vale", "change": 3.5}]
        maiores_baixas = [{"stock": "MGLU3", "name": "Magazine Luiza", "change": -4.2}]
        prompt = _prompt_analise_b3_ia([self._sinal_exemplo()], maiores_altas, maiores_baixas)
        self.assertIn("PETR4", prompt)
        self.assertIn("VALE3", prompt)
        self.assertIn("MGLU3", prompt)
        self.assertIn("TICKER|SITUACAO|COMENTARIO", prompt)

    @patch("core.services.requests.post")
    def test_gerar_analise_com_sucesso_retorna_texto_e_linhas(self, mock_post):
        mock_post.return_value.raise_for_status = lambda: None
        mock_post.return_value.json = lambda: {
            "choices": [{"message": {"content": "MERCADO|ALTA|Pregão positivo hoje.\nPETR4|ALTA|Rompeu resistência."}}]
        }
        with override_settings(OPENAI_API_KEY="sk-teste"):
            resultado = gerar_analise_b3_ia([self._sinal_exemplo()], [], [])

        self.assertIn("MERCADO|ALTA", resultado["texto"])
        self.assertEqual(len(resultado["linhas"]), 2)
        headers_chamada = mock_post.call_args.kwargs["headers"]
        self.assertEqual(headers_chamada["Authorization"], "Bearer sk-teste")

    @patch("core.services.requests.post")
    def test_gerar_analise_falha_de_rede_gera_iaerror(self, mock_post):
        mock_post.side_effect = requests.RequestException("timeout")
        with override_settings(OPENAI_API_KEY="sk-teste"):
            with self.assertRaises(IAError):
                gerar_analise_b3_ia([self._sinal_exemplo()], [], [])

    @patch("core.services.requests.post")
    def test_gerar_analise_resposta_com_formato_inesperado_gera_iaerror(self, mock_post):
        mock_post.return_value.raise_for_status = lambda: None
        mock_post.return_value.json = lambda: {"algo_diferente": True}
        with override_settings(OPENAI_API_KEY="sk-teste"):
            with self.assertRaises(IAError):
                gerar_analise_b3_ia([self._sinal_exemplo()], [], [])


class AnaliseMercadoIaViewTests(TestCase):
    """Tela Análise de Mercado - botão "Análise da B3 hoje (IA)" (core.views.analise_mercado_ia)."""

    def setUp(self):
        self.usuario = User.objects.create_user(username="investidor_ia_view", password="SenhaForte123!")
        self.client.login(username="investidor_ia_view", password="SenhaForte123!")

    def test_botao_nao_aparece_sem_ia_configurada(self):
        with override_settings(OPENAI_API_KEY=""):
            resposta = self.client.get(reverse("core:analise_mercado"))
        self.assertNotContains(resposta, reverse("core:analise_mercado_ia"))
        self.assertContains(resposta, "ainda não está configurada")

    def test_botao_aparece_com_ia_configurada(self):
        with override_settings(OPENAI_API_KEY="sk-teste"):
            resposta = self.client.get(reverse("core:analise_mercado"))
        self.assertContains(resposta, reverse("core:analise_mercado_ia"))

    def test_exige_login(self):
        self.client.logout()
        resposta = self.client.get(reverse("core:analise_mercado_ia"))
        self.assertEqual(resposta.status_code, 302)

    def test_sem_ia_configurada_mostra_aviso_sem_quebrar(self):
        with override_settings(OPENAI_API_KEY=""):
            resposta = self.client.get(reverse("core:analise_mercado_ia"))
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "ainda não está configurada")

    @patch("core.services.requests.post")
    def test_com_ia_configurada_mostra_grade_com_resultado(self, mock_post):
        ativo = Ativo.objects.create(ticker="ITUB4")
        Operacao.objects.create(
            usuario=self.usuario, ativo=ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("30.00"), data_operacao=date.today(),
        )
        mock_post.return_value.raise_for_status = lambda: None
        mock_post.return_value.json = lambda: {
            "choices": [{"message": {"content": "MERCADO|ALTA|Pregão positivo hoje.\nITUB4|NEUTRO|Sem direção clara."}}]
        }
        with override_settings(OPENAI_API_KEY="sk-teste"):
            resposta = self.client.get(reverse("core:analise_mercado_ia"))

        self.assertContains(resposta, "B3 (mercado geral)")
        self.assertContains(resposta, "ITUB4")
        self.assertContains(resposta, "Sem direção clara.")

    @patch("core.services.requests.post")
    def test_falha_na_consulta_mostra_aviso_sem_quebrar_a_tela(self, mock_post):
        mock_post.side_effect = requests.RequestException("timeout")
        with override_settings(OPENAI_API_KEY="sk-teste"):
            resposta = self.client.get(reverse("core:analise_mercado_ia"))
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Não foi possível consultar a IA agora")


class HistoricoAtualizacoesTests(TestCase):
    """core.services: registrar_atualizacao_carteira, excluir_registros_atualizacao_antigos,
    construir_grafico_atualizacoes_dia + tela Histórico de Atualizações."""

    def setUp(self):
        self.usuario = User.objects.create_user(username="investidor_historico", password="SenhaForte123!")
        self.ativo = Ativo.objects.create(ticker="VALE3")
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("60.00"), data_operacao=date.today(),
        )
        Cotacao.objects.create(ativo=self.ativo, data=date.today(), preco_fechamento=Decimal("66.00"))

    def test_registrar_atualizacao_carteira_grava_totais_corretos(self):
        registro = registrar_atualizacao_carteira(self.usuario)

        self.assertEqual(registro.total_ativos, 1)
        self.assertEqual(registro.valor_investido, Decimal("600.00"))
        self.assertEqual(registro.valor_atual, Decimal("660.00"))
        self.assertEqual(registro.lucro_perda, Decimal("60.00"))
        self.assertEqual(registro.lucro_perda_pct, Decimal("10.00"))

    def test_registrar_atualizacao_carteira_sem_posicoes_grava_zeros(self):
        usuario_vazio = User.objects.create_user(username="investidor_vazio", password="SenhaForte123!")
        registro = registrar_atualizacao_carteira(usuario_vazio)

        self.assertEqual(registro.total_ativos, 0)
        self.assertEqual(registro.valor_investido, Decimal("0"))
        self.assertIsNone(registro.lucro_perda_pct)

    def test_excluir_registros_antigos_respeita_limite_de_dias(self):
        recente = registrar_atualizacao_carteira(self.usuario)
        antigo = registrar_atualizacao_carteira(self.usuario)
        RegistroAtualizacaoCarteira.objects.filter(id=antigo.id).update(
            criado_em=timezone.now() - timedelta(days=10)
        )

        excluidos = excluir_registros_atualizacao_antigos(self.usuario, dias=5)

        self.assertEqual(excluidos, 1)
        self.assertTrue(RegistroAtualizacaoCarteira.objects.filter(id=recente.id).exists())
        self.assertFalse(RegistroAtualizacaoCarteira.objects.filter(id=antigo.id).exists())

    def test_excluir_registros_antigos_nao_afeta_outro_usuario(self):
        outro_usuario = User.objects.create_user(username="investidor_outro", password="SenhaForte123!")
        registro_outro = registrar_atualizacao_carteira(outro_usuario)
        RegistroAtualizacaoCarteira.objects.filter(id=registro_outro.id).update(
            criado_em=timezone.now() - timedelta(days=10)
        )

        excluidos = excluir_registros_atualizacao_antigos(self.usuario, dias=5)

        self.assertEqual(excluidos, 0)
        self.assertTrue(RegistroAtualizacaoCarteira.objects.filter(id=registro_outro.id).exists())

    def test_excluir_por_periodo_inclui_as_bordas_do_intervalo(self):
        hoje = timezone.localdate()
        registro_no_inicio = registrar_atualizacao_carteira(self.usuario)
        RegistroAtualizacaoCarteira.objects.filter(id=registro_no_inicio.id).update(
            criado_em=timezone.make_aware(datetime.combine(hoje - timedelta(days=5), datetime.min.time()))
        )
        registro_no_fim = registrar_atualizacao_carteira(self.usuario)
        RegistroAtualizacaoCarteira.objects.filter(id=registro_no_fim.id).update(
            criado_em=timezone.make_aware(datetime.combine(hoje - timedelta(days=3), datetime.max.time()))
        )
        registro_antes = registrar_atualizacao_carteira(self.usuario)
        RegistroAtualizacaoCarteira.objects.filter(id=registro_antes.id).update(
            criado_em=timezone.make_aware(datetime.combine(hoje - timedelta(days=6), datetime.min.time()))
        )

        excluidos = excluir_registros_atualizacao_por_periodo(
            self.usuario, hoje - timedelta(days=5), hoje - timedelta(days=3),
        )

        self.assertEqual(excluidos, 2)
        self.assertFalse(RegistroAtualizacaoCarteira.objects.filter(id=registro_no_inicio.id).exists())
        self.assertFalse(RegistroAtualizacaoCarteira.objects.filter(id=registro_no_fim.id).exists())
        self.assertTrue(RegistroAtualizacaoCarteira.objects.filter(id=registro_antes.id).exists())

    def test_grafico_atualizacoes_dia_none_com_menos_de_dois_registros(self):
        registro = registrar_atualizacao_carteira(self.usuario)
        self.assertIsNone(construir_grafico_atualizacoes_dia([registro]))

    def test_grafico_atualizacoes_dia_com_dois_registros(self):
        r1 = registrar_atualizacao_carteira(self.usuario)
        Cotacao.objects.filter(ativo=self.ativo, data=date.today()).update(preco_fechamento=Decimal("72.00"))
        r2 = registrar_atualizacao_carteira(self.usuario)

        grafico = construir_grafico_atualizacoes_dia([r1, r2])

        self.assertIsNotNone(grafico)
        self.assertEqual(len(grafico["pontos"]), 2)
        self.assertTrue(grafico["tendencia_alta"])

    def test_calcular_variacoes_historico_compara_com_registro_anterior(self):
        r1 = registrar_atualizacao_carteira(self.usuario)  # valor_atual = 660.00
        Cotacao.objects.filter(ativo=self.ativo, data=date.today()).update(preco_fechamento=Decimal("72.00"))
        r2 = registrar_atualizacao_carteira(self.usuario)  # valor_atual = 720.00

        # ordem "-criado_em" (mais recente primeiro), igual à view
        resultado = calcular_variacoes_historico([r2, r1])

        self.assertEqual(resultado[0]["registro"], r2)
        self.assertEqual(resultado[0]["variacao_valor"], Decimal("60.00"))
        self.assertAlmostEqual(float(resultado[0]["variacao_pct"]), 9.09, places=2)
        self.assertIsNone(resultado[1]["variacao_valor"])  # registro mais antigo não tem anterior
        self.assertIsNone(resultado[1]["variacao_pct"])

    def test_calcular_variacoes_historico_negativa_quando_valor_cai(self):
        r1 = registrar_atualizacao_carteira(self.usuario)  # valor_atual = 660.00
        Cotacao.objects.filter(ativo=self.ativo, data=date.today()).update(preco_fechamento=Decimal("60.00"))
        r2 = registrar_atualizacao_carteira(self.usuario)  # valor_atual = 600.00

        resultado = calcular_variacoes_historico([r2, r1])

        self.assertEqual(resultado[0]["variacao_valor"], Decimal("-60.00"))
        self.assertTrue(resultado[0]["variacao_pct"] < 0)

    def test_calcular_variacoes_historico_com_um_unico_registro(self):
        r1 = registrar_atualizacao_carteira(self.usuario)
        resultado = calcular_variacoes_historico([r1])
        self.assertIsNone(resultado[0]["variacao_valor"])

    def test_calcular_variacoes_historico_lista_vazia(self):
        self.assertEqual(calcular_variacoes_historico([]), [])

    def test_tela_historico_atualizacoes_mostra_coluna_variacao(self):
        registrar_atualizacao_carteira(self.usuario)
        Cotacao.objects.filter(ativo=self.ativo, data=date.today()).update(preco_fechamento=Decimal("72.00"))
        registrar_atualizacao_carteira(self.usuario)
        self.client.login(username="investidor_historico", password="SenhaForte123!")

        resposta = self.client.get(reverse("core:historico_atualizacoes"))

        self.assertContains(resposta, "Variação desde a atualização anterior")
        self.assertContains(resposta, "R$ 60,00")

    def test_tela_historico_atualizacoes_exige_login(self):
        resposta = self.client.get(reverse("core:historico_atualizacoes"))
        self.assertEqual(resposta.status_code, 302)

    def test_tela_historico_atualizacoes_lista_registros(self):
        registrar_atualizacao_carteira(self.usuario)
        self.client.login(username="investidor_historico", password="SenhaForte123!")

        resposta = self.client.get(reverse("core:historico_atualizacoes"))

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Histórico de Atualizações")
        self.assertNotContains(resposta, "VALE3")  # a tabela mostra só os totais, não o ticker

    def test_campos_de_data_do_formulario_de_periodo_vem_com_hoje_preenchido(self):
        self.client.login(username="investidor_historico", password="SenhaForte123!")

        resposta = self.client.get(reverse("core:historico_atualizacoes"))

        hoje = timezone.localdate().isoformat()
        self.assertContains(
            resposta, f'id="campo-data-inicio-excluir" value="{hoje}"',
        )
        self.assertContains(
            resposta, f'id="campo-data-fim-excluir" value="{hoje}"',
        )

    def test_excluir_por_dias_via_view(self):
        registro = registrar_atualizacao_carteira(self.usuario)
        RegistroAtualizacaoCarteira.objects.filter(id=registro.id).update(
            criado_em=timezone.now() - timedelta(days=10)
        )
        self.client.login(username="investidor_historico", password="SenhaForte123!")

        resposta = self.client.post(reverse("core:historico_atualizacoes_excluir"), {"dias": "5"})

        self.assertRedirects(resposta, reverse("core:historico_atualizacoes"))
        self.assertFalse(RegistroAtualizacaoCarteira.objects.filter(id=registro.id).exists())

    def test_excluir_por_dias_invalido_nao_exclui_nada(self):
        registro = registrar_atualizacao_carteira(self.usuario)
        self.client.login(username="investidor_historico", password="SenhaForte123!")

        resposta = self.client.post(reverse("core:historico_atualizacoes_excluir"), {"dias": "0"})

        self.assertRedirects(resposta, reverse("core:historico_atualizacoes"))
        self.assertTrue(RegistroAtualizacaoCarteira.objects.filter(id=registro.id).exists())

    def test_excluir_por_periodo_via_view(self):
        registro_dentro = registrar_atualizacao_carteira(self.usuario)
        RegistroAtualizacaoCarteira.objects.filter(id=registro_dentro.id).update(
            criado_em=timezone.now() - timedelta(days=5)
        )
        registro_fora = registrar_atualizacao_carteira(self.usuario)
        RegistroAtualizacaoCarteira.objects.filter(id=registro_fora.id).update(
            criado_em=timezone.now() - timedelta(days=30)
        )
        self.client.login(username="investidor_historico", password="SenhaForte123!")

        resposta = self.client.post(reverse("core:historico_atualizacoes_excluir_por_periodo"), {
            "data_inicio": (date.today() - timedelta(days=7)).isoformat(),
            "data_fim": (date.today() - timedelta(days=3)).isoformat(),
        })

        self.assertRedirects(resposta, reverse("core:historico_atualizacoes"))
        self.assertFalse(RegistroAtualizacaoCarteira.objects.filter(id=registro_dentro.id).exists())
        self.assertTrue(RegistroAtualizacaoCarteira.objects.filter(id=registro_fora.id).exists())

    def test_excluir_por_periodo_sem_datas_nao_exclui_nada(self):
        registro = registrar_atualizacao_carteira(self.usuario)
        self.client.login(username="investidor_historico", password="SenhaForte123!")

        resposta = self.client.post(reverse("core:historico_atualizacoes_excluir_por_periodo"), {
            "data_inicio": "", "data_fim": "",
        })

        self.assertRedirects(resposta, reverse("core:historico_atualizacoes"))
        self.assertTrue(RegistroAtualizacaoCarteira.objects.filter(id=registro.id).exists())

    def test_excluir_por_periodo_data_inicio_depois_da_data_fim_nao_exclui_nada(self):
        registro = registrar_atualizacao_carteira(self.usuario)
        self.client.login(username="investidor_historico", password="SenhaForte123!")

        resposta = self.client.post(reverse("core:historico_atualizacoes_excluir_por_periodo"), {
            "data_inicio": date.today().isoformat(),
            "data_fim": (date.today() - timedelta(days=1)).isoformat(),
        })

        self.assertRedirects(resposta, reverse("core:historico_atualizacoes"))
        self.assertTrue(RegistroAtualizacaoCarteira.objects.filter(id=registro.id).exists())

    def test_excluir_por_periodo_nao_afeta_outro_usuario(self):
        outro_usuario = User.objects.create_user(username="investidor_historico_outro", password="SenhaForte123!")
        registro_outro = registrar_atualizacao_carteira(outro_usuario)
        RegistroAtualizacaoCarteira.objects.filter(id=registro_outro.id).update(
            criado_em=timezone.now() - timedelta(days=5)
        )
        self.client.login(username="investidor_historico", password="SenhaForte123!")

        resposta = self.client.post(reverse("core:historico_atualizacoes_excluir_por_periodo"), {
            "data_inicio": (date.today() - timedelta(days=7)).isoformat(),
            "data_fim": date.today().isoformat(),
        })

        self.assertRedirects(resposta, reverse("core:historico_atualizacoes"))
        self.assertTrue(RegistroAtualizacaoCarteira.objects.filter(id=registro_outro.id).exists())

    def test_atualizar_cotacoes_agora_grava_registro(self):
        self.client.login(username="investidor_historico", password="SenhaForte123!")
        with patch("core.views.mercado_b3_aberto", return_value=True), \
             patch("core.views.atualizar_cotacao_diaria") as mock_atualizar:
            mock_atualizar.return_value = Cotacao.objects.filter(ativo=self.ativo).first()
            self.client.get(reverse("core:atualizar_cotacoes"))

        self.assertEqual(RegistroAtualizacaoCarteira.objects.filter(usuario=self.usuario).count(), 1)

    def test_exportar_excel_historico_retorna_xlsx_com_dados(self):
        from openpyxl import load_workbook
        import io

        registrar_atualizacao_carteira(self.usuario)
        self.client.login(username="investidor_historico", password="SenhaForte123!")

        resposta = self.client.get(reverse("core:historico_atualizacoes_exportar_excel"))

        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.content.startswith(b"PK"))
        wb = load_workbook(io.BytesIO(resposta.content))
        ws = wb.active
        linhas = list(ws.values)
        self.assertEqual(
            linhas[3],
            ("Data/hora", "Total de ativos", "Valor investido (R$)", "Valor atual (R$)",
             "Lucro/Perda (R$)", "Lucro/Perda (%)"),
        )
        self.assertEqual(linhas[4][1], 1)  # total_ativos

    def test_exportar_pdf_historico_retorna_pdf_valido(self):
        registrar_atualizacao_carteira(self.usuario)
        self.client.login(username="investidor_historico", password="SenhaForte123!")

        resposta = self.client.get(reverse("core:historico_atualizacoes_exportar_pdf"))

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta["Content-Type"], "application/pdf")
        self.assertTrue(resposta.content.startswith(b"%PDF"))

    def test_exportar_sem_registros_nao_quebra(self):
        self.client.login(username="investidor_historico", password="SenhaForte123!")
        resposta_excel = self.client.get(reverse("core:historico_atualizacoes_exportar_excel"))
        resposta_pdf = self.client.get(reverse("core:historico_atualizacoes_exportar_pdf"))
        self.assertEqual(resposta_excel.status_code, 200)
        self.assertEqual(resposta_pdf.status_code, 200)

    def test_botoes_de_acesso_ao_historico_nas_telas_relacionadas(self):
        # "Ações rápidas" (que tinha esse link) saiu do Painel de Controle e
        # foi para Posições em Carteira - ver core.views.dashboard/posicoes.
        self.client.login(username="investidor_historico", password="SenhaForte123!")
        url_historico = reverse("core:historico_atualizacoes")

        resposta_operacoes = self.client.get(reverse("core:operacao_lista"))
        resposta_posicoes = self.client.get(reverse("core:posicoes"))

        self.assertContains(resposta_operacoes, url_historico)
        self.assertContains(resposta_posicoes, url_historico)

    def test_posicoes_compradas_tem_acoes_vender_alterar_excluir_por_lote(self):
        # mesmas ações de Minhas Operações > Em carteira, uma linha por lote
        lote1 = Operacao.objects.get(usuario=self.usuario, ativo=self.ativo)
        lote2 = Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=5, preco_unitario=Decimal("62.00"), data_operacao=date.today(),
        )
        self.client.login(username="investidor_historico", password="SenhaForte123!")

        resposta = self.client.get(reverse("core:posicoes"))

        for lote in (lote1, lote2):
            self.assertContains(resposta, reverse("core:operacao_vender", args=[lote.id]))
            self.assertContains(resposta, reverse("core:operacao_editar", args=[lote.id]))
            self.assertContains(resposta, reverse("core:operacao_excluir", args=[lote.id]))

    def test_posicoes_nao_oferece_acoes_para_lote_ja_vendido(self):
        lote_vendido = Operacao.objects.get(usuario=self.usuario, ativo=self.ativo)
        Operacao.objects.filter(id=lote_vendido.id).update(
            quantidade_vendida=lote_vendido.quantidade, preco_venda=Decimal("70.00"), data_venda=date.today(),
        )
        self.client.login(username="investidor_historico", password="SenhaForte123!")

        resposta = self.client.get(reverse("core:posicoes"))

        self.assertNotContains(resposta, reverse("core:operacao_vender", args=[lote_vendido.id]))

    def test_excluir_compra_a_partir_de_posicoes_volta_para_posicoes(self):
        lote = Operacao.objects.get(usuario=self.usuario, ativo=self.ativo)
        self.client.login(username="investidor_historico", password="SenhaForte123!")

        resposta = self.client.post(
            reverse("core:operacao_excluir", args=[lote.id]), {"next": reverse("core:posicoes")},
        )

        self.assertRedirects(resposta, reverse("core:posicoes"))
        self.assertFalse(Operacao.objects.filter(id=lote.id).exists())

    def test_posicoes_tem_aba_compradas_primeiro_e_ativa_por_padrao(self):
        self.client.login(username="investidor_historico", password="SenhaForte123!")

        resposta = self.client.get(reverse("core:posicoes"))
        html = resposta.content.decode("utf-8")

        self.assertContains(resposta, 'data-alvo="painel-historico-atualizacoes"')
        self.assertContains(resposta, 'id="painel-historico-atualizacoes"')
        # a aba "Compradas" vem antes da aba do histórico na ordem do HTML
        self.assertLess(
            html.index('data-alvo="painel-compradas"'), html.index("painel-historico-atualizacoes"),
        )
        # e é a aba ativa por padrão (a do histórico passa a vir escondida)
        self.assertContains(resposta, 'id="painel-historico-atualizacoes" role="tabpanel" hidden')

    def test_posicoes_exibe_grafico_e_registros_do_historico(self):
        r1 = registrar_atualizacao_carteira(self.usuario)
        Cotacao.objects.filter(ativo=self.ativo, data=date.today()).update(preco_fechamento=Decimal("72.00"))
        r2 = registrar_atualizacao_carteira(self.usuario)
        self.client.login(username="investidor_historico", password="SenhaForte123!")

        resposta = self.client.get(reverse("core:posicoes"))

        self.assertContains(resposta, "Variações de hoje")
        self.assertContains(resposta, "grafico-carteira-dia")
        self.assertContains(resposta, "Registros recentes")
        self.assertContains(resposta, "R$ 660,00")  # valor_atual de r1 (formatação pt-br, vírgula decimal)
        self.assertContains(resposta, "R$ 720,00")  # valor_atual de r2

    def test_posicoes_nao_mostra_registro_de_outro_usuario_na_aba_historico(self):
        outro_usuario = User.objects.create_user(username="investidor_historico_isolado", password="SenhaForte123!")
        registrar_atualizacao_carteira(outro_usuario)
        self.client.login(username="investidor_historico", password="SenhaForte123!")

        resposta = self.client.get(reverse("core:posicoes"))

        self.assertContains(resposta, "Nenhum registro ainda")


class NomeDoUsuarioLogadoTests(TestCase):
    """base.html - o nome do usuário logado aparece no cabeçalho de todas as telas autenticadas."""

    def setUp(self):
        self.usuario = User.objects.create_user(
            username="maria_investe", password="SenhaForte123!", first_name="Maria", last_name="Silva",
        )
        self.client.login(username="maria_investe", password="SenhaForte123!")

    def test_nome_completo_aparece_em_varias_telas(self):
        for nome_url in ("core:dashboard", "core:menu", "core:operacao_lista", "core:posicoes",
                         "core:detalhes_cotacoes", "core:alertas", "core:noticias"):
            resposta = self.client.get(reverse(nome_url))
            self.assertContains(resposta, "nav-usuario-logado", msg_prefix=nome_url)
            self.assertContains(resposta, "Maria Silva", msg_prefix=nome_url)

    def test_sem_nome_cadastrado_mostra_o_username(self):
        User.objects.create_user(username="sem_nome", password="SenhaForte123!")
        self.client.login(username="sem_nome", password="SenhaForte123!")
        resposta = self.client.get(reverse("core:dashboard"))
        self.assertContains(resposta, "👤 sem_nome")


class OrcamentoApiBrapiTests(TestCase):
    """Controle de orçamento de requisições à brapi.dev (contador, teto automático/absoluto, projeção)."""

    @patch("core.services.requests.get")
    def test_cada_requisicao_a_brapi_e_contada(self, mock_get):
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.json = lambda: {"results": [{"symbol": "PETR4", "regularMarketPrice": 32.0}]}

        buscar_cotacao_atual("PETR4")
        buscar_cotacoes_em_lote(["PETR4", "VALE3"])

        self.assertEqual(core_services.consumo_api_ultimos_30_dias(), 2)

    def test_consumo_considera_so_os_ultimos_30_dias(self):
        from .models import ConsumoApiBrapi
        ConsumoApiBrapi.objects.create(data=timezone.localdate(), requisicoes=100)
        ConsumoApiBrapi.objects.create(data=timezone.localdate() - timedelta(days=29), requisicoes=50)
        ConsumoApiBrapi.objects.create(data=timezone.localdate() - timedelta(days=30), requisicoes=9999)
        self.assertEqual(core_services.consumo_api_ultimos_30_dias(), 150)

    @override_settings(BRAPI_LIMITE_MENSAL=1000, BRAPI_MARGEM_SEGURANCA_PCT=30)
    def test_limites_automatico_e_absoluto(self):
        self.assertEqual(core_services.limite_automatico_api(), 700)
        self.assertEqual(core_services.limite_absoluto_api(), 950)

    @override_settings(BRAPI_LIMITE_MENSAL=1000, BRAPI_MARGEM_SEGURANCA_PCT=30)
    def test_ciclo_automatico_pausa_ao_atingir_o_limite_automatico(self):
        from .models import ConsumoApiBrapi
        ConsumoApiBrapi.objects.create(data=timezone.localdate(), requisicoes=700)
        with patch("core.services.mercado_b3_aberto", return_value=True), \
             patch("core.services.executar_ciclo_atualizacao_cotacoes") as mock_ciclo:
            _ciclo_agendador_embutido()
        mock_ciclo.assert_not_called()

    @override_settings(BRAPI_LIMITE_MENSAL=1000, BRAPI_MARGEM_SEGURANCA_PCT=30)
    def test_ciclo_automatico_roda_abaixo_do_limite_automatico(self):
        from .models import ConsumoApiBrapi
        ConsumoApiBrapi.objects.create(data=timezone.localdate(), requisicoes=699)
        with patch("core.services.mercado_b3_aberto", return_value=True), \
             patch("core.services.executar_ciclo_atualizacao_cotacoes") as mock_ciclo:
            mock_ciclo.return_value = {
                "ativos_total": 0, "ativos_atualizados": 0, "ativos_falha": 0,
                "alertas_gerados": 0, "sinais_robo_gerados": 0,
            }
            _ciclo_agendador_embutido()
        mock_ciclo.assert_called_once()

    @override_settings(BRAPI_LIMITE_MENSAL=1000)
    @patch("core.services.requests.get")
    def test_acima_do_teto_absoluto_nenhuma_requisicao_e_feita(self, mock_get):
        from .models import ConsumoApiBrapi
        ConsumoApiBrapi.objects.create(data=timezone.localdate(), requisicoes=950)

        with self.assertRaises(BrapiError):
            buscar_cotacao_atual("PETR4")

        mock_get.assert_not_called()
        self.assertEqual(core_services.consumo_api_ultimos_30_dias(), 950)  # bloqueada não é contada

    @override_settings(BRAPI_LIMITE_MENSAL=1000)
    @patch("core.services.requests.get")
    def test_atualizacao_em_lote_conta_tudo_como_falha_quando_bloqueada(self, mock_get):
        from .models import ConsumoApiBrapi
        ConsumoApiBrapi.objects.create(data=timezone.localdate(), requisicoes=950)
        ativo = Ativo.objects.create(ticker="PETR4")

        atualizados, falhas = atualizar_cotacoes_ativos([ativo])

        self.assertEqual((atualizados, falhas), (0, 1))
        mock_get.assert_not_called()

    @override_settings(
        BRAPI_LIMITE_MENSAL=150000, BRAPI_MARGEM_SEGURANCA_PCT=30, BRAPI_TICKERS_POR_LOTE=10,
        B3_HORARIO_ABERTURA="10:00", B3_HORARIO_FECHAMENTO="17:00",
    )
    def test_projecao_com_27_ativos_e_intervalo_de_10_minutos_cabe_folgado(self):
        p = core_services.projetar_consumo_mensal(27, 10)
        self.assertEqual(p["requisicoes_por_ciclo"], 3)  # 27 ativos / 10 por lote
        self.assertEqual(p["ciclos_dia"], 43)  # 420 min / 10 + 1
        self.assertEqual(p["requisicoes_mes"], 43 * 3 * 22)
        self.assertTrue(p["cabe_no_orcamento"])
        self.assertLess(p["percentual_do_limite"], 5)

    @override_settings(BRAPI_LIMITE_MENSAL=150000, BRAPI_MARGEM_SEGURANCA_PCT=30, BRAPI_TICKERS_POR_LOTE=1)
    def test_projecao_avisa_quando_o_intervalo_nao_cabe(self):
        p = core_services.projetar_consumo_mensal(200, 1)  # 1 ativo por lote, ciclo a cada minuto
        self.assertFalse(p["cabe_no_orcamento"])

    def test_tela_mostra_consumo_e_projecao(self):
        User.objects.create_user(username="orcamento_tela", password="SenhaForte123!")
        self.client.login(username="orcamento_tela", password="SenhaForte123!")
        resposta = self.client.get(reverse("core:detalhes_cotacoes"))
        self.assertContains(resposta, "Consumo da API de cotações")
        self.assertContains(resposta, "Projeção do ciclo automático")


class ContaCorrenteServicosTests(TestCase):
    """core.services: obter_ou_criar_conta_corrente, saldo_conta_corrente,
    sincronizar_lancamento_compra/venda, registrar_transferencia_conta_corrente, extrato_conta_corrente."""

    def setUp(self):
        self.usuario = User.objects.create_user(username="investidor_cc", password="SenhaForte123!")
        self.ativo = Ativo.objects.create(ticker="VALE3")

    def test_obter_ou_criar_conta_corrente_cria_uma_so_vez(self):
        conta1 = obter_ou_criar_conta_corrente(self.usuario)
        conta2 = obter_ou_criar_conta_corrente(self.usuario)
        self.assertEqual(conta1.id, conta2.id)
        self.assertEqual(conta1.saldo_inicial, Decimal("0"))

    def test_saldo_conta_corrente_soma_saldo_inicial_creditos_e_debitos(self):
        conta = obter_ou_criar_conta_corrente(self.usuario)
        conta.saldo_inicial = Decimal("1000.00")
        conta.save()
        LancamentoContaCorrente.objects.create(
            conta=conta, tipo=LancamentoContaCorrente.CREDITO,
            origem=LancamentoContaCorrente.ORIGEM_TRANSFERENCIA, valor=Decimal("200.00"), descricao="Depósito",
        )
        LancamentoContaCorrente.objects.create(
            conta=conta, tipo=LancamentoContaCorrente.DEBITO,
            origem=LancamentoContaCorrente.ORIGEM_TRANSFERENCIA, valor=Decimal("50.00"), descricao="Saque",
        )
        self.assertEqual(saldo_conta_corrente(conta), Decimal("1150.00"))

    def test_sincronizar_lancamento_compra_cria_debito_com_nome_do_ativo(self):
        operacao = Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("60.00"), data_operacao=date(2026, 1, 5),
        )
        sincronizar_lancamento_compra(operacao)

        lancamento = LancamentoContaCorrente.objects.get(operacao=operacao, origem=LancamentoContaCorrente.ORIGEM_COMPRA)
        self.assertEqual(lancamento.tipo, LancamentoContaCorrente.DEBITO)
        self.assertEqual(lancamento.valor, Decimal("600.00"))
        self.assertIn("VALE3", lancamento.descricao)
        self.assertEqual(lancamento.data, date(2026, 1, 5))

    def test_sincronizar_lancamento_compra_ignora_reserva(self):
        reserva = Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.RESERVAR,
            quantidade=1, preco_unitario=Decimal("60.00"), data_operacao=date.today(),
        )
        sincronizar_lancamento_compra(reserva)
        self.assertFalse(LancamentoContaCorrente.objects.filter(operacao=reserva).exists())

    def test_sincronizar_lancamento_compra_atualiza_valor_ao_reeditar(self):
        operacao = Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("60.00"), data_operacao=date.today(),
        )
        sincronizar_lancamento_compra(operacao)

        operacao.quantidade = 20
        operacao.save()
        sincronizar_lancamento_compra(operacao)

        lancamentos = LancamentoContaCorrente.objects.filter(operacao=operacao, origem=LancamentoContaCorrente.ORIGEM_COMPRA)
        self.assertEqual(lancamentos.count(), 1)
        self.assertEqual(lancamentos.first().valor, Decimal("1200.00"))

    def test_sincronizar_lancamento_venda_cria_credito_com_valor_liquido(self):
        operacao = Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("60.00"), data_operacao=date.today(),
            quantidade_vendida=10, preco_venda=Decimal("70.00"), data_venda=date(2026, 2, 10),
            percentual_corretora=Decimal("1.00"),
        )
        sincronizar_lancamento_venda(operacao)

        lancamento = LancamentoContaCorrente.objects.get(operacao=operacao, origem=LancamentoContaCorrente.ORIGEM_VENDA)
        self.assertEqual(lancamento.tipo, LancamentoContaCorrente.CREDITO)
        self.assertEqual(lancamento.valor, operacao.valor_liquido_vendido)
        self.assertIn("VALE3", lancamento.descricao)
        self.assertEqual(lancamento.data, date(2026, 2, 10))

    def test_sincronizar_lancamento_venda_remove_lancamento_quando_venda_desfeita(self):
        operacao = Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("60.00"), data_operacao=date.today(),
            quantidade_vendida=5, preco_venda=Decimal("70.00"), data_venda=date.today(),
        )
        sincronizar_lancamento_venda(operacao)
        self.assertTrue(LancamentoContaCorrente.objects.filter(operacao=operacao, origem=LancamentoContaCorrente.ORIGEM_VENDA).exists())

        operacao.quantidade_vendida = 0
        operacao.preco_venda = None
        operacao.data_venda = None
        operacao.save()
        sincronizar_lancamento_venda(operacao)

        self.assertFalse(LancamentoContaCorrente.objects.filter(operacao=operacao, origem=LancamentoContaCorrente.ORIGEM_VENDA).exists())

    def test_excluir_operacao_remove_lancamentos_junto(self):
        operacao = Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("60.00"), data_operacao=date.today(),
            quantidade_vendida=10, preco_venda=Decimal("70.00"), data_venda=date.today(),
        )
        sincronizar_lancamento_compra(operacao)
        sincronizar_lancamento_venda(operacao)
        self.assertEqual(LancamentoContaCorrente.objects.filter(operacao=operacao).count(), 2)

        operacao.delete()

        self.assertEqual(LancamentoContaCorrente.objects.filter(operacao_id=operacao.id).count(), 0)

    def test_registrar_transferencia_cria_lancamento_manual(self):
        registrar_transferencia_conta_corrente(
            self.usuario, LancamentoContaCorrente.CREDITO, Decimal("500.00"), "Recebido do Nubank", date(2026, 3, 1),
        )
        conta = obter_ou_criar_conta_corrente(self.usuario)
        lancamento = conta.lancamentos.get()
        self.assertEqual(lancamento.origem, LancamentoContaCorrente.ORIGEM_TRANSFERENCIA)
        self.assertEqual(lancamento.tipo, LancamentoContaCorrente.CREDITO)
        self.assertEqual(lancamento.valor, Decimal("500.00"))
        self.assertEqual(lancamento.descricao, "Recebido do Nubank")
        self.assertEqual(lancamento.data, date(2026, 3, 1))
        self.assertIsNone(lancamento.operacao)

    def test_extrato_filtra_por_periodo_e_mantem_saldo_acumulado_correto(self):
        conta = obter_ou_criar_conta_corrente(self.usuario)
        conta.saldo_inicial = Decimal("100.00")
        conta.save()
        registrar_transferencia_conta_corrente(
            self.usuario, LancamentoContaCorrente.CREDITO, Decimal("50.00"), "Antes do período", date(2026, 1, 1),
        )
        registrar_transferencia_conta_corrente(
            self.usuario, LancamentoContaCorrente.DEBITO, Decimal("30.00"), "Dentro do período", date(2026, 1, 10),
        )
        registrar_transferencia_conta_corrente(
            self.usuario, LancamentoContaCorrente.CREDITO, Decimal("999.00"), "Depois do período", date(2026, 2, 1),
        )

        linhas = extrato_conta_corrente(conta, date(2026, 1, 5), date(2026, 1, 20))

        self.assertEqual(len(linhas), 1)
        self.assertEqual(linhas[0]["lancamento"].descricao, "Dentro do período")
        # saldo_apos = 100 (inicial) + 50 (antes, incorporado ao saldo de entrada) - 30 (dentro do período)
        self.assertEqual(linhas[0]["saldo_apos"], Decimal("120.00"))

    def test_extrato_sem_filtro_traz_tudo_do_mais_recente_para_o_mais_antigo(self):
        conta = obter_ou_criar_conta_corrente(self.usuario)
        registrar_transferencia_conta_corrente(
            self.usuario, LancamentoContaCorrente.CREDITO, Decimal("10.00"), "Primeira", date(2026, 1, 1),
        )
        registrar_transferencia_conta_corrente(
            self.usuario, LancamentoContaCorrente.CREDITO, Decimal("20.00"), "Segunda", date(2026, 1, 2),
        )

        linhas = extrato_conta_corrente(conta)

        self.assertEqual([item["lancamento"].descricao for item in linhas], ["Segunda", "Primeira"])

    def test_gerar_excel_extrato_produz_arquivo_xlsx_valido(self):
        conta = obter_ou_criar_conta_corrente(self.usuario)
        registrar_transferencia_conta_corrente(
            self.usuario, LancamentoContaCorrente.CREDITO, Decimal("100.00"), "Depósito", date.today(),
        )
        linhas = extrato_conta_corrente(conta)
        conteudo = gerar_excel_extrato_conta_corrente(linhas, conta.saldo_inicial, saldo_conta_corrente(conta))
        self.assertTrue(conteudo.startswith(b"PK"))  # assinatura do formato .xlsx (zip)

    def test_gerar_pdf_extrato_produz_arquivo_pdf_valido(self):
        conta = obter_ou_criar_conta_corrente(self.usuario)
        linhas = extrato_conta_corrente(conta)
        conteudo = gerar_pdf_extrato_conta_corrente(linhas, conta.saldo_inicial, saldo_conta_corrente(conta), "Período: todos", "Investidor")
        self.assertTrue(conteudo.startswith(b"%PDF"))


class ContaCorrenteViewsTests(TestCase):
    """Views da tela Conta Corrente: fluxo completo via formulários (saldo inicial, transferência, extrato, exclusão)."""

    def setUp(self):
        self.usuario = User.objects.create_user(username="investidor_cc_view", password="SenhaForte123!")
        self.client.login(username="investidor_cc_view", password="SenhaForte123!")
        self.ativo = Ativo.objects.create(ticker="PETR4")

    def test_compra_gera_debito_automatico_na_conta_corrente(self):
        resposta = self.client.post(reverse("core:operacao_nova"), {
            "ticker": "PETR4", "nome_ativo": "", "tipo": Operacao.COMPRA, "quantidade": "10",
            "preco_unitario": "30.00", "data_operacao": date.today().isoformat(),
            "meta_lucro_pct": "", "meta_perda_pct": "", "observacao": "",
        })
        self.assertEqual(resposta.status_code, 302)

        conta = obter_ou_criar_conta_corrente(self.usuario)
        lancamento = conta.lancamentos.get(origem=LancamentoContaCorrente.ORIGEM_COMPRA)
        self.assertEqual(lancamento.tipo, LancamentoContaCorrente.DEBITO)
        self.assertEqual(lancamento.valor, Decimal("300.00"))
        self.assertEqual(saldo_conta_corrente(conta), Decimal("-300.00"))

    def test_venda_gera_credito_automatico_na_conta_corrente(self):
        operacao = Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("30.00"), data_operacao=date.today(),
        )
        resposta = self.client.post(reverse("core:operacao_vender", args=[operacao.id]), {
            "quantidade_vendida": "10", "preco_venda": "35.00",
            "data_venda": date.today().isoformat(), "percentual_corretora": "0",
        })
        self.assertEqual(resposta.status_code, 302)

        conta = obter_ou_criar_conta_corrente(self.usuario)
        lancamento = conta.lancamentos.get(origem=LancamentoContaCorrente.ORIGEM_VENDA)
        self.assertEqual(lancamento.tipo, LancamentoContaCorrente.CREDITO)
        self.assertEqual(lancamento.valor, Decimal("350.00"))

    def test_excluir_operacao_remove_lancamento_da_tela(self):
        operacao = Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("30.00"), data_operacao=date.today(),
        )
        sincronizar_lancamento_compra(operacao)

        self.client.post(reverse("core:operacao_excluir", args=[operacao.id]))

        conta = obter_ou_criar_conta_corrente(self.usuario)
        self.assertEqual(conta.lancamentos.count(), 0)

    def test_tela_conta_corrente_abre_e_mostra_saldo(self):
        resposta = self.client.get(reverse("core:conta_corrente"))
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Conta Corrente")

    def test_definir_saldo_inicial(self):
        resposta = self.client.post(reverse("core:conta_corrente_definir_saldo"), {"saldo_inicial": "1000.00"})
        self.assertEqual(resposta.status_code, 302)
        conta = obter_ou_criar_conta_corrente(self.usuario)
        self.assertEqual(conta.saldo_inicial, Decimal("1000.00"))

    def test_nova_transferencia_credito(self):
        resposta = self.client.post(reverse("core:conta_corrente_transferencia_nova"), {
            "tipo": LancamentoContaCorrente.CREDITO, "valor": "250.00",
            "descricao": "Transferência recebida do Nubank", "data": date.today().isoformat(),
        })
        self.assertEqual(resposta.status_code, 302)
        conta = obter_ou_criar_conta_corrente(self.usuario)
        lancamento = conta.lancamentos.get(origem=LancamentoContaCorrente.ORIGEM_TRANSFERENCIA)
        self.assertEqual(lancamento.valor, Decimal("250.00"))
        self.assertIn("Nubank", lancamento.descricao)

    def test_excluir_transferencia_manual(self):
        lancamento = registrar_transferencia_conta_corrente(
            self.usuario, LancamentoContaCorrente.CREDITO, Decimal("100.00"), "Depósito", date.today(),
        )
        resposta = self.client.post(reverse("core:conta_corrente_lancamento_excluir", args=[lancamento.id]))
        self.assertEqual(resposta.status_code, 302)
        self.assertFalse(LancamentoContaCorrente.objects.filter(id=lancamento.id).exists())

    def test_nao_pode_excluir_lancamento_automatico_de_compra(self):
        operacao = Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("30.00"), data_operacao=date.today(),
        )
        sincronizar_lancamento_compra(operacao)
        lancamento = LancamentoContaCorrente.objects.get(operacao=operacao)

        resposta = self.client.post(reverse("core:conta_corrente_lancamento_excluir", args=[lancamento.id]))

        self.assertEqual(resposta.status_code, 404)
        self.assertTrue(LancamentoContaCorrente.objects.filter(id=lancamento.id).exists())

    def test_usuario_nao_ve_nem_exclui_lancamento_de_outro_usuario(self):
        outro_usuario = User.objects.create_user(username="investidor_cc_outro", password="SenhaForte123!")
        lancamento_outro = registrar_transferencia_conta_corrente(
            outro_usuario, LancamentoContaCorrente.CREDITO, Decimal("999.00"), "De outra pessoa", date.today(),
        )

        resposta = self.client.post(reverse("core:conta_corrente_lancamento_excluir", args=[lancamento_outro.id]))

        self.assertEqual(resposta.status_code, 404)
        self.assertTrue(LancamentoContaCorrente.objects.filter(id=lancamento_outro.id).exists())

    def test_exportar_excel_conta_corrente(self):
        resposta = self.client.get(reverse("core:conta_corrente_exportar_excel"))
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(
            resposta["Content-Type"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def test_exportar_pdf_conta_corrente(self):
        resposta = self.client.get(reverse("core:conta_corrente_exportar_pdf"))
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta["Content-Type"], "application/pdf")

    def test_menu_tem_link_para_conta_corrente(self):
        resposta = self.client.get(reverse("core:menu"))
        self.assertContains(resposta, reverse("core:conta_corrente"))


class ScannerTecnicoServicosTests(TestCase):
    """core.services: calcular_medias_moveis, analisar_volume_precos, _classificar_volatilidade, _veredito_scanner."""

    def test_medias_moveis_none_com_historico_insuficiente(self):
        self.assertIsNone(calcular_medias_moveis(list(range(1, 21))))  # 20 preços, precisa de 21

    def test_medias_moveis_alinhamento_de_alta(self):
        precos = list(range(1, 22))  # 1..21, sequência ascendente
        info = calcular_medias_moveis(precos)
        self.assertEqual(info["media_longa"], 11.0)
        self.assertEqual(info["media_curta"], 17.0)  # média dos últimos 9 (13..21)
        self.assertTrue(info["preco_acima_da_longa"])
        self.assertTrue(info["curta_acima_da_longa"])
        self.assertEqual(_classificar_medias_moveis(info), "ALTA")

    def test_medias_moveis_alinhamento_de_baixa(self):
        precos = list(range(21, 0, -1))  # 21..1, sequência descendente
        info = calcular_medias_moveis(precos)
        self.assertTrue(info["preco_abaixo_da_longa"])
        self.assertTrue(info["curta_abaixo_da_longa"])
        self.assertEqual(_classificar_medias_moveis(info), "BAIXA")

    def test_medias_moveis_sinal_misto_fica_neutro(self):
        # sobe forte e cai bruscamente no último dia: preço atual fica abaixo
        # da longa, mas a curta (ainda "puxada" pela alta recente) fica acima
        # dela - as duas partes do indicador discordam entre si.
        precos = [10] * 12 + [30] * 8 + [5]
        info = calcular_medias_moveis(precos)
        self.assertTrue(info["preco_abaixo_da_longa"])
        self.assertTrue(info["curta_acima_da_longa"])
        self.assertEqual(_classificar_medias_moveis(info), "NEUTRO")

    def test_medias_moveis_none_classificado_como_dados_insuficientes(self):
        self.assertEqual(_classificar_medias_moveis(None), "DADOS_INSUFICIENTES")

    def test_volume_dados_insuficientes_com_poucos_pontos(self):
        volumes = [None] * 5 + [100]
        precos = list(range(6))
        info = analisar_volume_precos(volumes, precos)
        self.assertEqual(info["classificacao"], "DADOS_INSUFICIENTES")

    def test_volume_acima_da_media_confirmando_alta(self):
        volumes = [100, 100, 100, 100, 100, 200]
        precos = [10, 10, 10, 10, 10, 11]  # subiu no último dia
        info = analisar_volume_precos(volumes, precos)
        self.assertEqual(info["classificacao"], "ALTA")
        self.assertEqual(info["volume_atual"], 200)
        self.assertEqual(info["media_volume"], 100)

    def test_volume_aumentando_na_queda(self):
        volumes = [100, 100, 100, 100, 100, 200]
        precos = [10, 10, 10, 10, 10, 9]  # caiu no último dia
        info = analisar_volume_precos(volumes, precos)
        self.assertEqual(info["classificacao"], "BAIXA")

    def test_volume_normal_fica_neutro(self):
        volumes = [100] * 6
        precos = [10, 10, 10, 10, 10, 11]
        info = analisar_volume_precos(volumes, precos)
        self.assertEqual(info["classificacao"], "NEUTRO")

    def test_classificar_volatilidade(self):
        self.assertEqual(_classificar_volatilidade(None), "DADOS_INSUFICIENTES")
        self.assertEqual(_classificar_volatilidade(50.0), "ALTA")
        self.assertEqual(_classificar_volatilidade(10.0), "BAIXA")
        self.assertEqual(_classificar_volatilidade(25.0), "MODERADA")

    def test_veredito_conflitante_quando_ha_alta_e_baixa_ao_mesmo_tempo(self):
        self.assertEqual(
            _veredito_scanner(["ALTA", "BAIXA", "NEUTRO", "NEUTRO", "NEUTRO"]), "CONFLITANTE"
        )

    def test_veredito_predominio_alta_ignora_neutros_e_sem_dados(self):
        self.assertEqual(
            _veredito_scanner(["ALTA", "NEUTRO", "NEUTRO", "DADOS_INSUFICIENTES", "NEUTRO"]),
            "PREDOMINIO_ALTA",
        )

    def test_veredito_predominio_baixa(self):
        self.assertEqual(
            _veredito_scanner(["BAIXA", "BAIXA", "NEUTRO", "DADOS_INSUFICIENTES", "NEUTRO"]),
            "PREDOMINIO_BAIXA",
        )

    def test_veredito_neutro_quando_nenhum_indicador_aponta_direcao(self):
        self.assertEqual(_veredito_scanner(["NEUTRO"] * 5), "NEUTRO")

    def test_veredito_dados_insuficientes_só_quando_todos_sem_dados(self):
        self.assertEqual(_veredito_scanner(["DADOS_INSUFICIENTES"] * 5), "DADOS_INSUFICIENTES")
        # um único indicador com dado real já basta pra não ser "sem dados"
        self.assertEqual(
            _veredito_scanner(["ALTA"] + ["DADOS_INSUFICIENTES"] * 4), "PREDOMINIO_ALTA"
        )


class AtrSuporteResistenciaPlanoTradeTests(TestCase):
    """core.services: calcular_atr, calcular_suporte_resistencia, sugerir_plano_trade."""

    def test_atr_none_com_historico_insuficiente(self):
        maximas = [11.0] * 10
        minimas = [9.0] * 10
        fechamentos = [10.0] * 10
        self.assertIsNone(calcular_atr(maximas, minimas, fechamentos, periodo=14))

    def test_atr_com_oscilacao_constante(self):
        # sem gap entre fechamento e máxima/mínima do dia seguinte: TR = máxima - mínima = 2.0 todo dia.
        maximas = [11.0] * 15
        minimas = [9.0] * 15
        fechamentos = [10.0] * 15
        self.assertEqual(calcular_atr(maximas, minimas, fechamentos, periodo=14), 2.0)

    def test_atr_tolera_gaps_de_maxima_minima_ausentes(self):
        maximas = [None] * 5 + [11.0] * 14
        minimas = [None] * 5 + [9.0] * 14
        fechamentos = [10.0] * 19
        self.assertEqual(calcular_atr(maximas, minimas, fechamentos, periodo=14), 2.0)

    def test_suporte_resistencia_none_com_historico_insuficiente(self):
        minimas = [9.0] * 19
        maximas = [11.0] * 19
        self.assertIsNone(calcular_suporte_resistencia(minimas, maximas, preco_atual=10.0, janela=20))

    def test_suporte_resistencia_exclui_o_pregao_de_hoje_da_conta(self):
        # 20 pregões anteriores oscilando entre 9 e 11, e hoje um rompimento forte pra 50 -
        # o rompimento de hoje não pode "esticar" a própria resistência que ele está rompendo.
        minimas = [9.0] * 20 + [40.0]
        maximas = [11.0] * 20 + [50.0]
        info = calcular_suporte_resistencia(minimas, maximas, preco_atual=50.0, janela=20)
        self.assertEqual(info["suporte"], 9.0)
        self.assertEqual(info["resistencia"], 11.0)
        self.assertTrue(info["rompeu_resistencia"])
        self.assertFalse(info["perdeu_suporte"])

    def test_suporte_resistencia_perdeu_suporte(self):
        minimas = [9.0] * 20 + [3.0]
        maximas = [11.0] * 20 + [8.0]
        info = calcular_suporte_resistencia(minimas, maximas, preco_atual=3.0, janela=20)
        self.assertTrue(info["perdeu_suporte"])
        self.assertFalse(info["rompeu_resistencia"])

    def test_suporte_resistencia_dentro_do_canal_fica_neutro(self):
        minimas = [9.0] * 21
        maximas = [11.0] * 21
        info = calcular_suporte_resistencia(minimas, maximas, preco_atual=10.0, janela=20)
        self.assertEqual(_classificar_suporte_resistencia(info), "NEUTRO")

    def test_classificar_suporte_resistencia_none_fica_dados_insuficientes(self):
        self.assertEqual(_classificar_suporte_resistencia(None), "DADOS_INSUFICIENTES")

    def test_plano_trade_none_quando_veredito_nao_e_direcional(self):
        for veredito in ("CONFLITANTE", "NEUTRO", "DADOS_INSUFICIENTES"):
            self.assertIsNone(sugerir_plano_trade(10.0, veredito, None, atr=1.0))

    def test_plano_trade_de_compra_usa_suporte_e_resistencia_como_stop_e_alvo(self):
        suporte_resistencia = {"suporte": 8.0, "resistencia": 12.0, "rompeu_resistencia": False, "perdeu_suporte": False}
        plano = sugerir_plano_trade(10.0, "PREDOMINIO_ALTA", suporte_resistencia, atr=0.5)
        self.assertEqual(plano["entrada"], 10.0)
        self.assertEqual(plano["stop"], 8.0)
        self.assertEqual(plano["alvo"], 12.0)
        self.assertEqual(plano["relacao_risco_retorno"], 1.0)
        self.assertIsNone(plano["observacao"])

    def test_plano_trade_de_venda_usa_resistencia_como_stop_e_suporte_como_alvo(self):
        suporte_resistencia = {"suporte": 8.0, "resistencia": 12.0, "rompeu_resistencia": False, "perdeu_suporte": False}
        plano = sugerir_plano_trade(10.0, "PREDOMINIO_BAIXA", suporte_resistencia, atr=0.5)
        self.assertEqual(plano["entrada"], 10.0)
        self.assertEqual(plano["stop"], 12.0)
        self.assertEqual(plano["alvo"], 8.0)
        self.assertIsNotNone(plano["observacao"])
        self.assertIn("não opera venda a descoberto", plano["observacao"])

    def test_plano_trade_sem_suporte_resistencia_usa_buffer_do_atr(self):
        plano = sugerir_plano_trade(10.0, "PREDOMINIO_ALTA", None, atr=1.0)
        self.assertEqual(plano["stop"], 8.5)  # entrada - atr * 1.5
        self.assertEqual(plano["alvo"], 12.0)  # entrada + atr * 2

    def test_plano_trade_sem_atr_nem_suporte_resistencia_usa_2_por_cento_do_preco(self):
        plano = sugerir_plano_trade(100.0, "PREDOMINIO_ALTA", None, atr=None)
        self.assertEqual(plano["stop"], 97.0)  # entrada - (preco * 2%) * 1.5
        self.assertEqual(plano["alvo"], 104.0)  # entrada + (preco * 2%) * 2


class EscanearAtivoTests(TestCase):
    """core.services.escanear_ativo_precos / escanear_ativo / escanear_carteira."""

    def test_indicadores_discordantes_geram_veredito_conflitante(self):
        # série longa e puramente decrescente: o IFR fica sobrevendido (vota
        # ALTA, é contrário) enquanto médias móveis e tendência de curto prazo
        # seguem o movimento e votam BAIXA - um caso real de discordância.
        precos = [float(v) for v in range(100, 60, -1)]  # 100, 99, ..., 61 (40 pregões)

        resultado = escanear_ativo_precos(precos)

        chaves = {item["nome"]: item["chave"] for item in resultado["indicadores"]}
        self.assertEqual(chaves["IFR (RSI)"], "ALTA")
        self.assertEqual(chaves["Médias móveis"], "BAIXA")
        self.assertEqual(chaves["Tendência de curto prazo"], "BAIXA")
        self.assertEqual(resultado["veredito"], "CONFLITANTE")
        self.assertEqual(resultado["veredito_label"], "Indicadores conflitantes - sem sinal claro")
        self.assertGreater(resultado["votos_alta"], 0)
        self.assertGreater(resultado["votos_baixa"], 0)

    def test_indicadores_concordantes_geram_predominio_de_alta(self):
        # leve tendência de alta com ruído dia a dia (mantém o IFR numa faixa
        # neutra, sem votar) enquanto médias móveis, MACD e tendência sobem juntas.
        precos = [50 + i * 0.2 + (0.3 if i % 2 == 0 else -0.3) for i in range(41)]

        rsi = calcular_rsi(precos)
        self.assertIsNotNone(rsi)
        self.assertTrue(30 < rsi < 70, f"pré-condição do teste falhou: RSI={rsi} não ficou na faixa neutra")

        resultado = escanear_ativo_precos(precos)

        self.assertEqual(resultado["votos_baixa"], 0)
        self.assertGreater(resultado["votos_alta"], 0)
        self.assertEqual(resultado["veredito"], "PREDOMINIO_ALTA")

    def test_sem_historico_fica_dados_insuficientes(self):
        # um único preço: nem a tendência (precisa de >= 2) consegue opinar -
        # todos os indicadores ficam sem dados, e só nesse caso o veredito é
        # DADOS_INSUFICIENTES (com histórico curto mas >= 2 preços, a
        # tendência já vota NEUTRO, e o veredito vira NEUTRO, não isso).
        resultado = escanear_ativo_precos([100.0])
        self.assertEqual(resultado["veredito"], "DADOS_INSUFICIENTES")

    def test_historico_curto_mas_com_2_precos_fica_neutro_nao_sem_dados(self):
        resultado = escanear_ativo_precos([100.0, 101.0])
        self.assertEqual(resultado["veredito"], "NEUTRO")

    def test_sem_maxima_minima_suporte_resistencia_e_atr_ficam_sem_dados(self):
        precos = [float(v) for v in range(100, 60, -1)]
        resultado = escanear_ativo_precos(precos)
        chaves = {item["nome"]: item["chave"] for item in resultado["indicadores"]}
        self.assertEqual(chaves["Suporte e resistência"], "DADOS_INSUFICIENTES")
        self.assertIsNone(resultado["atr"])
        self.assertIsNone(resultado["suporte_resistencia"])

    def test_rompimento_de_resistencia_vota_alta_e_gera_plano_de_trade(self):
        # 25 pregões estáveis entre 9 e 11, e no último dia um rompimento forte pra 20 -
        # o rompimento sozinho já basta pra votar ALTA no indicador de suporte/resistência.
        precos = [10.0] * 25 + [20.0]
        maximas = [11.0] * 25 + [21.0]
        minimas = [9.0] * 25 + [19.0]

        resultado = escanear_ativo_precos(precos, maximas_cronologico=maximas, minimas_cronologico=minimas)

        chaves = {item["nome"]: item["chave"] for item in resultado["indicadores"]}
        self.assertEqual(chaves["Suporte e resistência"], "ALTA")
        self.assertIsNotNone(resultado["suporte_resistencia"])
        self.assertTrue(resultado["suporte_resistencia"]["rompeu_resistencia"])
        if resultado["veredito"] == "PREDOMINIO_ALTA":
            self.assertIsNotNone(resultado["plano_trade"])
            self.assertEqual(resultado["plano_trade"]["entrada"], 20.0)

    def test_escanear_ativo_busca_precos_volumes_maxima_e_minima_do_banco(self):
        ativo = Ativo.objects.create(ticker="SCAN3")
        hoje = date.today()
        for i, preco in enumerate(range(100, 60, -1)):
            Cotacao.objects.create(
                ativo=ativo, data=hoje - timedelta(days=(39 - i)), preco_fechamento=Decimal(str(preco)),
                maxima=Decimal(str(preco + 1)), minima=Decimal(str(preco - 1)),
            )

        resultado = escanear_ativo(ativo)

        self.assertEqual(resultado["ativo"], ativo)
        self.assertEqual(resultado["veredito"], "CONFLITANTE")
        self.assertIsNotNone(resultado["atr"])
        self.assertIsNotNone(resultado["suporte_resistencia"])

    def test_escanear_carteira_inclui_comprados_e_reservados_mas_nao_de_outro_usuario(self):
        usuario = User.objects.create_user(username="investidor_scanner", password="SenhaForte123!")
        outro_usuario = User.objects.create_user(username="investidor_scanner_outro", password="SenhaForte123!")

        comprado = Ativo.objects.create(ticker="COMP3")
        Operacao.objects.create(
            usuario=usuario, ativo=comprado, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("30.00"), data_operacao=date.today(),
        )
        reservado = Ativo.objects.create(ticker="RES3")
        Operacao.objects.create(
            usuario=usuario, ativo=reservado, tipo=Operacao.RESERVAR,
            quantidade=1, preco_unitario=Decimal("20.00"), data_operacao=date.today(),
        )
        ativo_de_outro = Ativo.objects.create(ticker="OUTRO3")
        Operacao.objects.create(
            usuario=outro_usuario, ativo=ativo_de_outro, tipo=Operacao.COMPRA,
            quantidade=5, preco_unitario=Decimal("15.00"), data_operacao=date.today(),
        )

        resultados = escanear_carteira(usuario)

        tickers = [r["ativo"].ticker for r in resultados]
        self.assertEqual(tickers, ["COMP3", "RES3"])
        por_ticker = {r["ativo"].ticker: r for r in resultados}
        self.assertFalse(por_ticker["COMP3"]["apenas_reservado"])
        self.assertTrue(por_ticker["RES3"]["apenas_reservado"])


class ScannerTecnicoViewTests(TestCase):
    """Tela Scanner Técnico (core.views.scanner_tecnico)."""

    def setUp(self):
        self.usuario = User.objects.create_user(username="investidor_scanner_view", password="SenhaForte123!")
        self.client.login(username="investidor_scanner_view", password="SenhaForte123!")

    def test_exige_login(self):
        self.client.logout()
        resposta = self.client.get(reverse("core:scanner_tecnico"))
        self.assertEqual(resposta.status_code, 302)

    def test_sem_ativos_em_carteira_mostra_mensagem(self):
        resposta = self.client.get(reverse("core:scanner_tecnico"))
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "ainda não tem ativos em carteira")

    def test_mostra_ativo_comprado_com_veredito_conflitante(self):
        ativo = Ativo.objects.create(ticker="SCVW3")
        Operacao.objects.create(
            usuario=self.usuario, ativo=ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("70.00"), data_operacao=date.today() - timedelta(days=39),
        )
        hoje = date.today()
        for i, preco in enumerate(range(100, 60, -1)):
            Cotacao.objects.create(
                ativo=ativo, data=hoje - timedelta(days=(39 - i)), preco_fechamento=Decimal(str(preco)),
            )

        resposta = self.client.get(reverse("core:scanner_tecnico"))

        self.assertContains(resposta, "SCVW3")
        self.assertContains(resposta, "Indicadores conflitantes")

    def test_mostra_reserva_com_selo_mas_nao_ativo_de_outro_usuario(self):
        reservado = Ativo.objects.create(ticker="RESV3")
        Operacao.objects.create(
            usuario=self.usuario, ativo=reservado, tipo=Operacao.RESERVAR,
            quantidade=1, preco_unitario=Decimal("10.00"), data_operacao=date.today(),
        )
        outro_usuario = User.objects.create_user(username="investidor_scanner_view_outro", password="SenhaForte123!")
        ativo_de_outro = Ativo.objects.create(ticker="ALHEIO3")
        Operacao.objects.create(
            usuario=outro_usuario, ativo=ativo_de_outro, tipo=Operacao.COMPRA,
            quantidade=5, preco_unitario=Decimal("15.00"), data_operacao=date.today(),
        )

        resposta = self.client.get(reverse("core:scanner_tecnico"))

        self.assertContains(resposta, "RESV3")
        self.assertContains(resposta, "Reservado")
        self.assertNotContains(resposta, "ALHEIO3")

    def test_menu_posicoes_e_analise_mercado_tem_link_para_o_scanner(self):
        resposta_menu = self.client.get(reverse("core:menu"))
        self.assertContains(resposta_menu, reverse("core:scanner_tecnico"))

        resposta_posicoes = self.client.get(reverse("core:posicoes"))
        self.assertContains(resposta_posicoes, reverse("core:scanner_tecnico"))

        resposta_analise = self.client.get(reverse("core:analise_mercado"))
        self.assertContains(resposta_analise, reverse("core:scanner_tecnico"))


class PostItServicosTests(TestCase):
    """core.services: obter_post_it, salvar_post_it."""

    def setUp(self):
        self.usuario = User.objects.create_user(username="investidor_postit", password="SenhaForte123!")

    def test_obter_post_it_retorna_none_quando_nunca_salvou(self):
        self.assertIsNone(obter_post_it(self.usuario))

    def test_salvar_post_it_cria_na_primeira_vez(self):
        post_it = salvar_post_it(self.usuario, texto="Vender PETR4 se bater 40")
        self.assertEqual(PostIt.objects.count(), 1)
        self.assertEqual(post_it.texto, "Vender PETR4 se bater 40")
        self.assertFalse(post_it.minimizado)
        self.assertEqual(obter_post_it(self.usuario).texto, "Vender PETR4 se bater 40")

    def test_salvar_so_o_texto_nao_mexe_no_minimizado(self):
        salvar_post_it(self.usuario, texto="rascunho", minimizado=True)
        salvar_post_it(self.usuario, texto="rascunho final")
        post_it = obter_post_it(self.usuario)
        self.assertEqual(post_it.texto, "rascunho final")
        self.assertTrue(post_it.minimizado)  # não foi tocado na segunda chamada

    def test_salvar_so_o_minimizado_nao_mexe_no_texto(self):
        salvar_post_it(self.usuario, texto="lembrete importante")
        salvar_post_it(self.usuario, minimizado=True)
        post_it = obter_post_it(self.usuario)
        self.assertEqual(post_it.texto, "lembrete importante")
        self.assertTrue(post_it.minimizado)

    def test_texto_vazio_e_diferente_de_nao_informado(self):
        salvar_post_it(self.usuario, texto="alguma coisa")
        salvar_post_it(self.usuario, texto="")  # apagar o texto deliberadamente
        self.assertEqual(obter_post_it(self.usuario).texto, "")

    def test_post_it_e_isolado_por_usuario(self):
        outro = User.objects.create_user(username="investidor_postit_outro", password="SenhaForte123!")
        salvar_post_it(self.usuario, texto="nota do usuario 1")
        self.assertIsNone(obter_post_it(outro))


class PostItViewTests(TestCase):
    """Endpoint core.views.post_it_salvar + inclusão do widget nas telas Menu/Operações/Posições."""

    def setUp(self):
        self.usuario = User.objects.create_user(username="investidor_postit_view", password="SenhaForte123!")
        self.client.login(username="investidor_postit_view", password="SenhaForte123!")

    def test_exige_login(self):
        self.client.logout()
        resposta = self.client.post(reverse("core:post_it_salvar"), {"texto": "x"})
        self.assertEqual(resposta.status_code, 302)

    def test_exige_post(self):
        resposta = self.client.get(reverse("core:post_it_salvar"))
        self.assertEqual(resposta.status_code, 405)

    def test_salva_texto_via_post(self):
        resposta = self.client.post(reverse("core:post_it_salvar"), {"texto": "comprar mais ITSA4"})
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.json()["ok"], True)
        self.assertIn("atualizado_em", resposta.json())
        self.assertEqual(obter_post_it(self.usuario).texto, "comprar mais ITSA4")

    def test_salva_minimizado_via_post(self):
        self.client.post(reverse("core:post_it_salvar"), {"minimizado": "1"})
        self.assertTrue(obter_post_it(self.usuario).minimizado)
        self.client.post(reverse("core:post_it_salvar"), {"minimizado": "0"})
        self.assertFalse(obter_post_it(self.usuario).minimizado)

    def test_widget_aparece_no_menu(self):
        resposta = self.client.get(reverse("core:menu"))
        self.assertContains(resposta, 'id="post-it-widget"')

    def test_widget_nao_aparece_em_posicoes_nem_em_operacoes(self):
        for nome_url in ("core:posicoes", "core:operacao_lista"):
            resposta = self.client.get(reverse(nome_url))
            self.assertNotContains(resposta, 'id="post-it-widget"')

    def test_texto_salvo_aparece_pre_preenchido_na_tela(self):
        salvar_post_it(self.usuario, texto="lembrete visivel na tela")
        resposta = self.client.get(reverse("core:menu"))
        self.assertContains(resposta, "lembrete visivel na tela")

    def test_widget_nao_vaza_texto_de_outro_usuario(self):
        outro = User.objects.create_user(username="investidor_postit_view_outro", password="SenhaForte123!")
        salvar_post_it(outro, texto="segredo do outro usuario")
        resposta = self.client.get(reverse("core:menu"))
        self.assertNotContains(resposta, "segredo do outro usuario")


class RedistribuicaoPainelTests(TestCase):
    """
    "Atividade da comunidade", "Ações rápidas" e "Alertas recentes" saíram do
    Painel de Controle original: "Ações rápidas" foi para Posições em
    Carteira, "Atividade da comunidade" para Histórico de Atualizações (que
    passou a se atualizar sozinho, igual a Posições em Carteira), e "Alertas
    recentes" voltou pro Painel, embaixo de "Variações de hoje".
    """

    def setUp(self):
        self.usuario = User.objects.create_user(username="investidor_redistrib", password="SenhaForte123!")
        self.client.login(username="investidor_redistrib", password="SenhaForte123!")
        Alerta.objects.create(usuario=self.usuario, tipo=Alerta.LEMBRETE, mensagem="Alerta de teste")

    def test_painel_nao_mostra_mais_ativiade_da_comunidade_nem_acoes_rapidas(self):
        resposta = self.client.get(reverse("core:dashboard"))
        self.assertNotContains(resposta, "Atividade da comunidade")
        self.assertNotContains(resposta, "Ações rápidas")

    def test_painel_mostra_alertas_recentes(self):
        resposta = self.client.get(reverse("core:dashboard"))
        self.assertContains(resposta, "Alertas recentes")
        self.assertContains(resposta, "Alerta de teste")

    def test_alertas_recentes_tem_rolagem_para_nivelar_com_posicoes(self):
        resposta = self.client.get(reverse("core:dashboard"))
        self.assertContains(resposta, 'class="lista-scroll"')

    def test_variacoes_de_hoje_mostra_badges_de_atualizacao_e_horario_b3(self):
        from .services import atualizar_cotacao_diaria

        ativo = Ativo.objects.create(ticker="BADGE3")
        Operacao.objects.create(
            usuario=self.usuario, ativo=ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("10.00"), data_operacao=date.today(),
        )
        atualizar_cotacao_diaria(ativo, dados_api={"regularMarketPrice": 11.0, "regularMarketChangePercent": 1.0})

        resposta = self.client.get(reverse("core:dashboard"))

        self.assertContains(resposta, "Última atualização das cotações:")
        self.assertContains(resposta, "badge-horario-b3")
        self.assertContains(resposta, "B3:")
        self.assertContains(resposta, "Mercado")

    def test_painel_mostra_alertas_recentes_embaixo_de_variacoes_de_hoje(self):
        resposta = self.client.get(reverse("core:dashboard"))
        conteudo = resposta.content.decode()
        # "Variações de hoje" precisa aparecer antes de "Alertas recentes" no
        # HTML pra ficar "embaixo" dela na coluna direita do grid.
        self.assertLess(conteudo.index("Variações de hoje"), conteudo.index("Alertas recentes"))

    def test_posicoes_mostra_acoes_rapidas_mas_nao_alertas_recentes(self):
        resposta = self.client.get(reverse("core:posicoes"))
        self.assertContains(resposta, "Ações rápidas")
        self.assertNotContains(resposta, "Alertas recentes")
        self.assertNotContains(resposta, "Alerta de teste")

    def test_historico_atualizacoes_mostra_atividade_da_comunidade(self):
        resposta = self.client.get(reverse("core:historico_atualizacoes"))
        self.assertContains(resposta, "Atividade da comunidade")

    def test_historico_atualizacoes_tem_o_mesmo_mecanismo_de_refresh_automatico(self):
        resposta = self.client.get(reverse("core:historico_atualizacoes"))
        self.assertContains(resposta, 'id="marcador-ultima-atualizacao"')
        self.assertContains(resposta, reverse("core:verificar_atualizacao_cotacoes"))

    def test_painel_tambem_tem_o_mesmo_mecanismo_de_refresh_automatico(self):
        # a grid "Posições em carteira" e o gráfico "Variações de hoje" do
        # Painel também precisam recarregar sozinhos após uma atualização de
        # cotações em segundo plano, igual às outras duas telas.
        resposta = self.client.get(reverse("core:dashboard"))
        self.assertContains(resposta, 'id="marcador-ultima-atualizacao"')
        self.assertContains(resposta, reverse("core:verificar_atualizacao_cotacoes"))

    def test_painel_mostra_registros_recentes_abaixo_do_grafico_limitado_a_5(self):
        for i in range(7):
            registrar_atualizacao_carteira(self.usuario)

        resposta = self.client.get(reverse("core:dashboard"))
        conteudo = resposta.content.decode()

        self.assertContains(resposta, "Registros recentes")
        self.assertEqual(conteudo.count("R$ 0,00</td>"), 5)  # só os 5 mais recentes (carteira vazia = R$0)
        # "Registros recentes" precisa vir depois de "Variações de hoje" (embaixo do gráfico)
        self.assertLess(conteudo.index("Variações de hoje"), conteudo.index("Registros recentes"))
        self.assertLess(conteudo.index("Registros recentes"), conteudo.index("Alertas recentes"))

    def test_painel_mostra_credito_do_desenvolvedor_em_destaque(self):
        resposta = self.client.get(reverse("core:dashboard"))
        self.assertContains(resposta, "Antonio Roberto Pereira Junior")
        self.assertContains(resposta, "arpjunior40@gmail.com")
        self.assertContains(resposta, "+55 65 98113-2995")
        self.assertContains(resposta, "https://wa.me/5565981132995")
        self.assertContains(resposta, 'class="card-glass card-desenvolvedor"')

    def test_painel_mostra_posicoes_e_variacoes_de_hoje_lado_a_lado(self):
        ativo = Ativo.objects.create(ticker="LADO3")
        Operacao.objects.create(
            usuario=self.usuario, ativo=ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("10.00"), data_operacao=date.today(),
        )
        Cotacao.objects.create(ativo=ativo, data=date.today(), preco_fechamento=Decimal("11.00"))
        r1 = registrar_atualizacao_carteira(self.usuario)
        Cotacao.objects.filter(ativo=ativo, data=date.today()).update(preco_fechamento=Decimal("12.00"))
        registrar_atualizacao_carteira(self.usuario)

        resposta = self.client.get(reverse("core:dashboard"))

        self.assertContains(resposta, 'class="grid-duas-colunas"')
        self.assertContains(resposta, "Posições em carteira")
        self.assertContains(resposta, "Variações de hoje")
        self.assertContains(resposta, "grafico-carteira-dia")

    def test_painel_mostra_so_as_posicoes_realmente_compradas(self):
        ativo_comprado = Ativo.objects.create(ticker="COMP4")
        Operacao.objects.create(
            usuario=self.usuario, ativo=ativo_comprado, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("20.00"), data_operacao=date.today(),
        )
        ativo_reservado = Ativo.objects.create(ticker="RESV4")
        Operacao.objects.create(
            usuario=self.usuario, ativo=ativo_reservado, tipo=Operacao.RESERVAR,
            quantidade=1, preco_unitario=Decimal("15.00"), data_operacao=date.today(),
        )

        resposta = self.client.get(reverse("core:dashboard"))

        self.assertContains(resposta, "COMP4")
        self.assertNotContains(resposta, "RESV4")
        self.assertEqual(resposta.context["total_ativos"], 1)

    def test_painel_usa_a_mesma_tabela_de_compradas_de_posicoes_em_carteira(self):
        # mesma tabela compartilhada via templates/core/
        # _tabela_posicoes_compradas.html - mas no Painel (resumo) a coluna
        # Ação (vender/alterar/excluir por lote) fica escondida.
        ativo = Ativo.objects.create(ticker="IGUAL3")
        operacao = Operacao.objects.create(
            usuario=self.usuario, ativo=ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("20.00"), data_operacao=date.today(),
        )

        resposta_painel = self.client.get(reverse("core:dashboard"))
        resposta_posicoes = self.client.get(reverse("core:posicoes"))

        for resposta in (resposta_painel, resposta_posicoes):
            self.assertContains(resposta, "IGUAL3")
            self.assertContains(resposta, "Lucro/Perda por dia em carteira")

        self.assertContains(resposta_posicoes, reverse("core:operacao_vender", args=[operacao.id]))
        self.assertContains(resposta_posicoes, reverse("core:operacao_editar", args=[operacao.id]))
        self.assertContains(resposta_posicoes, reverse("core:operacao_excluir", args=[operacao.id]))

        self.assertNotContains(resposta_painel, ">Ação<")
        self.assertNotContains(resposta_painel, reverse("core:operacao_vender", args=[operacao.id]))
        self.assertNotContains(resposta_painel, reverse("core:operacao_editar", args=[operacao.id]))
        self.assertNotContains(resposta_painel, reverse("core:operacao_excluir", args=[operacao.id]))


class SinaisRoboSomenteCompradosTests(TestCase):
    """
    "Indicadores técnicos dos seus ativos acompanhados" (Análise de Mercado)
    e os relatórios do robô (Excel/PDF) mostram só ativos realmente
    comprados (saldo > 0) - reservas (intenção de compra) não entram mais
    (ver core.views._sinais_robo).
    """

    def setUp(self):
        self.usuario = User.objects.create_user(username="investidor_sinais_robo", password="SenhaForte123!")
        self.client.login(username="investidor_sinais_robo", password="SenhaForte123!")

        self.ativo_comprado = Ativo.objects.create(ticker="COMPR3")
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo_comprado, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("20.00"), data_operacao=date.today(),
        )
        self.ativo_reservado = Ativo.objects.create(ticker="RESER3")
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo_reservado, tipo=Operacao.RESERVAR,
            quantidade=1, preco_unitario=Decimal("15.00"), data_operacao=date.today(),
        )
        self.ativo_vendido = Ativo.objects.create(ticker="VEND3")
        Operacao.objects.create(
            usuario=self.usuario, ativo=self.ativo_vendido, tipo=Operacao.COMPRA,
            quantidade=5, preco_unitario=Decimal("10.00"), data_operacao=date.today(),
            quantidade_vendida=5, preco_venda=Decimal("12.00"), data_venda=date.today(),
        )

    def test_analise_mercado_mostra_so_o_ativo_comprado(self):
        resposta = self.client.get(reverse("core:analise_mercado"))
        tickers = [item["ativo"].ticker for item in resposta.context["sinais"]]
        self.assertEqual(tickers, ["COMPR3"])

    def test_analise_mercado_nao_mostra_reservado_nem_totalmente_vendido(self):
        resposta = self.client.get(reverse("core:analise_mercado"))
        self.assertContains(resposta, "COMPR3")
        self.assertNotContains(resposta, "RESER3")
        self.assertNotContains(resposta, "VEND3")

    def test_exportar_excel_e_pdf_do_robo_respondem_ok(self):
        resposta_excel = self.client.get(reverse("core:robo_exportar_excel"))
        resposta_pdf = self.client.get(reverse("core:robo_exportar_pdf"))
        self.assertEqual(resposta_excel.status_code, 200)
        self.assertEqual(resposta_pdf.status_code, 200)


class ComentariosDeTemplateNaoVazamTests(TestCase):
    """
    {# ... #} do Django não suporta múltiplas linhas (vira texto literal na
    página em vez de sumir) - só {% comment %}...{% endcomment %} suporta.
    Já aconteceu em _tabela_posicoes_compradas.html e _post_it.html; este
    teste evita que a mesma armadilha volte a vazar texto de comentário para
    o usuário em qualquer tela.
    """

    def setUp(self):
        self.usuario = User.objects.create_user(username="investidor_sem_comentario_vazado", password="SenhaForte123!")
        self.client.login(username="investidor_sem_comentario_vazado", password="SenhaForte123!")
        ativo = Ativo.objects.create(ticker="LIMPO3")
        Operacao.objects.create(
            usuario=self.usuario, ativo=ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("10.00"), data_operacao=date.today(),
        )

    def test_paginas_principais_nao_vazam_texto_de_comentario_de_template(self):
        trechos_de_comentario = [
            "compartilhada entre Posições em Carteira",
            "core.context_processors.post_it",
            "ocultar_acoes",
        ]
        for nome_url in ("core:dashboard", "core:menu", "core:posicoes", "core:operacao_lista"):
            resposta = self.client.get(reverse(nome_url))
            conteudo = resposta.content.decode()
            for trecho in trechos_de_comentario:
                self.assertNotIn(trecho, conteudo, f"{nome_url} vazou texto de comentário: {trecho!r}")


class UltimoValorIbovespaTests(TestCase):
    """core.services.ultimo_valor_ibovespa"""

    def test_none_sem_nenhum_valor_salvo(self):
        self.assertIsNone(ultimo_valor_ibovespa())

    def test_um_so_valor_salvo_fica_sem_variacao(self):
        CotacaoIndice.objects.create(indice=CotacaoIndice.IBOVESPA, data=date.today(), valor=Decimal("130000"))
        resultado = ultimo_valor_ibovespa()
        self.assertEqual(resultado["valor"], Decimal("130000"))
        self.assertIsNone(resultado["variacao_pct"])

    def test_calcula_variacao_percentual_em_relacao_ao_pregao_anterior(self):
        CotacaoIndice.objects.create(
            indice=CotacaoIndice.IBOVESPA, data=date.today() - timedelta(days=1), valor=Decimal("130000")
        )
        CotacaoIndice.objects.create(indice=CotacaoIndice.IBOVESPA, data=date.today(), valor=Decimal("131300"))

        resultado = ultimo_valor_ibovespa()

        self.assertEqual(resultado["valor"], Decimal("131300"))
        self.assertEqual(resultado["variacao_pct"], Decimal("1.00"))

    def test_ignora_o_cdi_e_pega_so_o_ibovespa(self):
        CotacaoIndice.objects.create(indice=CotacaoIndice.CDI, data=date.today(), valor=Decimal("0.05"))
        self.assertIsNone(ultimo_valor_ibovespa())

    def test_none_quando_o_ultimo_valor_salvo_nao_e_de_hoje(self):
        # valor de ontem (ex: ninguém clicou em "Atualizar benchmarks" hoje
        # ainda) não deve aparecer como se fosse o de agora.
        CotacaoIndice.objects.create(
            indice=CotacaoIndice.IBOVESPA, data=date.today() - timedelta(days=1), valor=Decimal("130000")
        )
        self.assertIsNone(ultimo_valor_ibovespa())


class IbovespaNoPainelTests(TestCase):
    """Painel de Controle mostra o último valor conhecido do Ibovespa (ver ultimo_valor_ibovespa)."""

    def setUp(self):
        self.usuario = User.objects.create_user(username="investidor_ibov_painel", password="SenhaForte123!")
        self.client.login(username="investidor_ibov_painel", password="SenhaForte123!")

    def test_painel_mostra_valor_e_variacao_do_ibovespa(self):
        CotacaoIndice.objects.create(
            indice=CotacaoIndice.IBOVESPA, data=date.today() - timedelta(days=1), valor=Decimal("130000")
        )
        CotacaoIndice.objects.create(indice=CotacaoIndice.IBOVESPA, data=date.today(), valor=Decimal("131300"))

        resposta = self.client.get(reverse("core:dashboard"))

        self.assertContains(resposta, "Ibovespa")
        self.assertContains(resposta, "131300")
        self.assertContains(resposta, "1,00")

    def test_painel_sem_dado_de_ibovespa_mostra_link_para_atualizar(self):
        resposta = self.client.get(reverse("core:dashboard"))
        self.assertContains(resposta, "sem dados")
        self.assertContains(resposta, reverse("core:atualizar_benchmarks"))

    def test_painel_com_ibovespa_desatualizado_mostra_sem_dados_em_vez_do_valor_velho(self):
        CotacaoIndice.objects.create(
            indice=CotacaoIndice.IBOVESPA, data=date.today() - timedelta(days=3), valor=Decimal("130000")
        )
        resposta = self.client.get(reverse("core:dashboard"))
        self.assertContains(resposta, "sem dados")
        self.assertNotContains(resposta, "130000")
