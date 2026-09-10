"""
Testes automatizados do BolsaTrader.

Executar com:
    python manage.py test
"""
import hashlib
import hmac
import json
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse

from .forms import OperacaoForm, VendaLoteForm
from .models import Ativo, Cotacao, Operacao, Alerta, MensagemWhatsapp
from .services import (
    calcular_posicoes, analisar_tendencia, gerar_alertas_para_usuario, construir_comparativo_valores,
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

    def test_atualizar_cotacoes_agora_inclui_ativo_so_com_reserva(self):
        usuario = User.objects.create_user(username="investidor_cotacao_reserva", password="SenhaForte123!")
        self.client.login(username="investidor_cotacao_reserva", password="SenhaForte123!")
        ativo = Ativo.objects.create(ticker="CSMG3")
        Operacao.objects.create(
            usuario=usuario, ativo=ativo, tipo=Operacao.RESERVAR,
            quantidade=1, preco_unitario=Decimal("16.00"), data_operacao=date.today(),
        )
        with patch("core.services.buscar_cotacao_atual") as mock_busca:
            mock_busca.return_value = {"regularMarketPrice": 16.5, "regularMarketChangePercent": 1.1}
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
        self.client.login(username="ana", password="SenhaForte123!")
        resposta = self.client.get(reverse("core:dashboard"))
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
        atualizar_cotacao_diaria(ativo, dados_api=self._payload_completo())
        ativo.refresh_from_db()

        self.assertEqual(ativo.nome_longo, "B3 SA - Brasil, Bolsa, Balcao")
        self.assertEqual(ativo.moeda, "BRL")
        self.assertEqual(ativo.maxima_dia, Decimal("14.70"))
        self.assertEqual(ativo.minima_dia, Decimal("14.31"))
        self.assertEqual(ativo.abertura, Decimal("14.31"))
        self.assertEqual(ativo.fechamento_anterior, Decimal("14.54"))
        self.assertEqual(ativo.variacao_dia_valor, Decimal("0.13"))
        self.assertEqual(ativo.volume, 5689600)
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
        mock_get.return_value.json = lambda: {"results": [{"regularMarketPrice": 32.0}]}

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


class DetalhesCotacoesTests(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_user(username="investidor_detalhes", password="SenhaForte123!")
        self.outro_usuario = User.objects.create_user(username="outro_detalhes", password="SenhaForte123!")
        self.client.login(username="investidor_detalhes", password="SenhaForte123!")

    def test_mostra_apenas_ativos_do_usuario_logado(self):
        meu_ativo = Ativo.objects.create(ticker="B3SA3", nome_longo="B3 SA")
        Operacao.objects.create(
            usuario=self.usuario, ativo=meu_ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("14.00"), data_operacao=date.today(),
        )
        ativo_do_outro = Ativo.objects.create(ticker="RENT3")
        Operacao.objects.create(
            usuario=self.outro_usuario, ativo=ativo_do_outro, tipo=Operacao.COMPRA,
            quantidade=5, preco_unitario=Decimal("50.00"), data_operacao=date.today(),
        )

        resposta = self.client.get(reverse("core:detalhes_cotacoes"))
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "B3SA3")
        self.assertNotContains(resposta, "RENT3")

    def test_pagina_mostra_campos_detalhados(self):
        ativo = Ativo.objects.create(
            ticker="B3SA3", nome_longo="B3 SA - Brasil, Bolsa, Balcao",
            maxima_dia=Decimal("14.70"), minima_dia=Decimal("14.31"),
            volume=5689600, valor_mercado=Decimal("70010885753.00"),
        )
        Operacao.objects.create(
            usuario=self.usuario, ativo=ativo, tipo=Operacao.COMPRA,
            quantidade=10, preco_unitario=Decimal("14.00"), data_operacao=date.today(),
        )
        resposta = self.client.get(reverse("core:detalhes_cotacoes"))
        self.assertContains(resposta, "B3 SA - Brasil, Bolsa, Balcao")
        self.assertContains(resposta, "14,70")
        self.assertContains(resposta, "5.689.600")


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
        self.assertIn("bolsatrader-v1", resposta.content.decode("utf-8"))

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
