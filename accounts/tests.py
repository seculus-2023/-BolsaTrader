from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import PerfilUsuario


@override_settings(CADASTRO_PUBLICO=True)
class CadastroTelefoneTests(TestCase):
    """Telefone informado no cadastro vai para PerfilUsuario.numero_whatsapp."""

    def _dados(self, **extra):
        dados = {
            "username": "novo_investidor", "first_name": "Novo", "email": "novo@example.com",
            "password1": "SenhaForte123!", "password2": "SenhaForte123!",
        }
        dados.update(extra)
        return dados

    def test_cadastro_com_telefone_grava_no_perfil_so_digitos(self):
        self.client.post(reverse("accounts:cadastro"), self._dados(telefone="+55 (65) 99999-8888"))
        perfil = PerfilUsuario.objects.get(usuario__username="novo_investidor")
        self.assertEqual(perfil.numero_whatsapp, "5565999998888")

    def test_cadastro_sem_telefone_nao_cria_perfil(self):
        self.client.post(reverse("accounts:cadastro"), self._dados())
        self.assertTrue(User.objects.filter(username="novo_investidor").exists())
        self.assertFalse(PerfilUsuario.objects.exists())

    def test_telefone_com_letras_e_invalido(self):
        resposta = self.client.post(reverse("accounts:cadastro"), self._dados(telefone="abc123"))
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "telefone válido")
        self.assertFalse(User.objects.filter(username="novo_investidor").exists())


class MinhaContaTests(TestCase):
    """Tela de edição do número de WhatsApp de contato do usuário."""

    def setUp(self):
        self.usuario = User.objects.create_user(username="investidor_conta", password="SenhaForte123!")
        self.client.login(username="investidor_conta", password="SenhaForte123!")

    def test_exige_login(self):
        self.client.logout()
        resposta = self.client.get(reverse("accounts:minha_conta"))
        self.assertEqual(resposta.status_code, 302)

    def test_primeira_visita_cria_perfil_vazio(self):
        self.assertFalse(PerfilUsuario.objects.filter(usuario=self.usuario).exists())
        resposta = self.client.get(reverse("accounts:minha_conta"))
        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(PerfilUsuario.objects.filter(usuario=self.usuario).exists())

    def test_salva_numero_valido(self):
        self.client.post(reverse("accounts:minha_conta"), {"numero_whatsapp": "5565999998888"})
        perfil = PerfilUsuario.objects.get(usuario=self.usuario)
        self.assertEqual(perfil.numero_whatsapp, "5565999998888")

    def test_numero_com_caracteres_nao_numericos_e_invalido(self):
        resposta = self.client.post(reverse("accounts:minha_conta"), {"numero_whatsapp": "+55 (65) 99999-8888"})
        self.assertEqual(resposta.status_code, 200)  # re-renderiza o form com erro, não redireciona
        self.assertContains(resposta, "só dígitos")
        self.assertFalse(PerfilUsuario.objects.filter(usuario=self.usuario, numero_whatsapp__gt="").exists())

    def test_numero_em_branco_e_permitido_desliga_avisos(self):
        PerfilUsuario.objects.create(usuario=self.usuario, numero_whatsapp="5565999998888")
        resposta = self.client.post(reverse("accounts:minha_conta"), {"numero_whatsapp": ""})
        self.assertRedirects(resposta, reverse("accounts:minha_conta"))
        perfil = PerfilUsuario.objects.get(usuario=self.usuario)
        self.assertEqual(perfil.numero_whatsapp, "")

    def test_salva_bot_do_telegram(self):
        self.client.post(reverse("accounts:minha_conta"), {
            "numero_whatsapp": "", "telegram_bot_token": " 123456789:AAF-abc_DEF ", "telegram_chat_id": "2065125150",
        })
        perfil = PerfilUsuario.objects.get(usuario=self.usuario)
        self.assertEqual(perfil.telegram_bot_token, "123456789:AAF-abc_DEF")
        self.assertEqual(perfil.telegram_chat_id, "2065125150")

    def test_token_do_telegram_fora_do_formato_e_invalido(self):
        resposta = self.client.post(reverse("accounts:minha_conta"), {
            "numero_whatsapp": "", "telegram_bot_token": "nao-e-token", "telegram_chat_id": "2065125150",
        })
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Token inválido")

    def test_telegram_exige_token_e_chat_id_juntos(self):
        resposta = self.client.post(reverse("accounts:minha_conta"), {
            "numero_whatsapp": "", "telegram_bot_token": "123456789:AAF-abc_DEF", "telegram_chat_id": "",
        })
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "preencha o token do bot e o Chat ID")
        self.assertFalse(PerfilUsuario.objects.filter(usuario=self.usuario, telegram_bot_token__gt="").exists())

    def test_salva_assinatura_eletronica(self):
        self.client.post(reverse("accounts:minha_conta"), {
            "numero_whatsapp": "", "assinatura_eletronica": " 12345678 ",
        })
        perfil = PerfilUsuario.objects.get(usuario=self.usuario)
        self.assertEqual(perfil.assinatura_eletronica, "12345678")

    def test_salva_usuario_e_senha_da_corretora(self):
        self.client.post(reverse("accounts:minha_conta"), {
            "numero_whatsapp": "", "corretora_usuario": " 12345678900 ", "corretora_senha": "Senha da Corretora1",
        })
        perfil = PerfilUsuario.objects.get(usuario=self.usuario)
        self.assertEqual(perfil.corretora_usuario, "12345678900")
        self.assertEqual(perfil.corretora_senha, "Senha da Corretora1")

    def test_assinatura_eletronica_aparece_mascarada(self):
        PerfilUsuario.objects.create(usuario=self.usuario, assinatura_eletronica="12345678")
        resposta = self.client.get(reverse("accounts:minha_conta"))
        self.assertContains(resposta, 'type="password"')

    def test_outro_usuario_nao_ve_nem_altera_perfil_alheio(self):
        outro = User.objects.create_user(username="outro_conta", password="SenhaForte123!")
        PerfilUsuario.objects.create(usuario=outro, numero_whatsapp="5565911112222")

        self.client.post(reverse("accounts:minha_conta"), {"numero_whatsapp": "5565999998888"})

        perfil_outro = PerfilUsuario.objects.get(usuario=outro)
        self.assertEqual(perfil_outro.numero_whatsapp, "5565911112222")  # não foi alterado
        perfil_proprio = PerfilUsuario.objects.get(usuario=self.usuario)
        self.assertEqual(perfil_proprio.numero_whatsapp, "5565999998888")


class CamposCriptografadosTests(TestCase):
    """Senha/assinatura da corretora e token do Telegram ficam cifrados no banco (accounts.criptografia)."""

    def test_valor_gravado_cifrado_e_lido_decifrado(self):
        from django.db import connection

        usuario = User.objects.create_user(username="investidor_cifra", password="SenhaForte123!")
        PerfilUsuario.objects.create(
            usuario=usuario, corretora_senha="Senha da Corretora1", assinatura_eletronica="12345678",
            telegram_bot_token="123456789:AAF-abc_DEF", corretora_usuario="12345678900",
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT corretora_senha, assinatura_eletronica, telegram_bot_token, corretora_usuario "
                "FROM accounts_perfilusuario WHERE usuario_id = %s", [usuario.id],
            )
            brutos = cursor.fetchone()
        for bruto, original in zip(brutos, ["Senha da Corretora1", "12345678", "123456789:AAF-abc_DEF", "12345678900"]):
            self.assertTrue(bruto.startswith("enc1:"))
            self.assertNotIn(original, bruto)

        perfil = PerfilUsuario.objects.get(usuario=usuario)
        self.assertEqual(perfil.corretora_senha, "Senha da Corretora1")
        self.assertEqual(perfil.telegram_bot_token, "123456789:AAF-abc_DEF")

    def test_texto_puro_antigo_continua_legivel(self):
        from .criptografia import descriptografar
        self.assertEqual(descriptografar("valor antigo"), "valor antigo")
        self.assertEqual(descriptografar(""), "")

    def test_chave_trocada_devolve_vazio(self):
        from django.test import override_settings as sobrescrever
        from .criptografia import criptografar, descriptografar
        cifrado = criptografar("segredo")
        with sobrescrever(CHAVE_CRIPTOGRAFIA_CAMPOS="", SECRET_KEY="outra-chave-qualquer"):
            self.assertEqual(descriptografar(cifrado), "")


class LimiteTentativasLoginTests(TestCase):
    """Login bloqueado após LOGIN_MAX_TENTATIVAS senhas erradas (accounts.views.LoginFuturistaView)."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        User.objects.create_user(username="alvo", password="SenhaForte123!")

    @override_settings(LOGIN_MAX_TENTATIVAS=3, LOGIN_BLOQUEIO_MINUTOS=15)
    def test_bloqueia_apos_tentativas_erradas_mesmo_com_senha_certa(self):
        for _ in range(3):
            self.client.post(reverse("accounts:login"), {"username": "alvo", "password": "errada"})
        resposta = self.client.post(
            reverse("accounts:login"), {"username": "alvo", "password": "SenhaForte123!"}, follow=True
        )
        self.assertContains(resposta, "Muitas tentativas")
        self.assertFalse(resposta.wsgi_request.user.is_authenticated)

    @override_settings(LOGIN_MAX_TENTATIVAS=3, LOGIN_BLOQUEIO_MINUTOS=15)
    def test_login_certo_zera_o_contador(self):
        for _ in range(2):
            self.client.post(reverse("accounts:login"), {"username": "alvo", "password": "errada"})
        self.client.post(reverse("accounts:login"), {"username": "alvo", "password": "SenhaForte123!"})
        self.client.logout()
        for _ in range(2):
            self.client.post(reverse("accounts:login"), {"username": "alvo", "password": "errada"})
        resposta = self.client.post(reverse("accounts:login"), {"username": "alvo", "password": "SenhaForte123!"})
        self.assertEqual(resposta.status_code, 302)
        self.assertNotEqual(resposta.url, reverse("accounts:login"))


class RecuperacaoSenhaTests(TestCase):
    """'Esqueci minha senha': e-mail com link, link troca a senha (accounts/urls.py)."""

    def setUp(self):
        self.usuario = User.objects.create_user(
            username="esquecido", email="esquecido@example.com", password="SenhaAntiga123!"
        )

    def test_login_tem_link_esqueci_minha_senha(self):
        self.assertContains(self.client.get(reverse("accounts:login")), reverse("accounts:senha_esqueci"))

    def test_fluxo_completo_redefine_a_senha(self):
        import re
        from django.core import mail

        resposta = self.client.post(reverse("accounts:senha_esqueci"), {"email": "esquecido@example.com"})
        self.assertRedirects(resposta, reverse("accounts:senha_enviada"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("redefinição de senha", mail.outbox[0].subject)

        link = re.search(r"https?://[^/]+(/contas/senha/redefinir/\S+)", mail.outbox[0].body).group(1)
        resposta = self.client.get(link, follow=True)  # o Django troca o token da URL pela sessão
        self.assertTrue(resposta.context["validlink"])
        resposta = self.client.post(
            resposta.wsgi_request.path, {"new_password1": "SenhaNova456!", "new_password2": "SenhaNova456!"}
        )
        self.assertRedirects(resposta, reverse("accounts:senha_redefinida"))
        self.usuario.refresh_from_db()
        self.assertTrue(self.usuario.check_password("SenhaNova456!"))

    def test_email_desconhecido_nao_envia_nada_mas_nao_revela(self):
        from django.core import mail

        resposta = self.client.post(reverse("accounts:senha_esqueci"), {"email": "ninguem@example.com"})
        self.assertRedirects(resposta, reverse("accounts:senha_enviada"))
        self.assertEqual(len(mail.outbox), 0)

    def test_usuario_logado_altera_a_propria_senha(self):
        self.client.login(username="esquecido", password="SenhaAntiga123!")
        self.assertContains(self.client.get(reverse("accounts:minha_conta")), reverse("accounts:senha_alterar"))
        resposta = self.client.post(reverse("accounts:senha_alterar"), {
            "old_password": "SenhaAntiga123!", "new_password1": "SenhaNova456!", "new_password2": "SenhaNova456!",
        })
        self.assertRedirects(resposta, reverse("accounts:senha_alterada"))
        self.usuario.refresh_from_db()
        self.assertTrue(self.usuario.check_password("SenhaNova456!"))


class PainelTvTests(TestCase):
    """Link secreto do painel para TV/monitor: gerado em Minha Conta, abre sem login."""

    def setUp(self):
        self.usuario = User.objects.create_user(
            username="investidor_tv", password="SenhaForte123!", first_name="Ana", last_name="TV",
        )
        self.client.login(username="investidor_tv", password="SenhaForte123!")

    def _gerar(self):
        self.client.post(reverse("accounts:painel_tv_link"), {"acao": "gerar"})
        return PerfilUsuario.objects.get(usuario=self.usuario).painel_tv_token

    def test_minha_conta_sem_link_mostra_botao_gerar(self):
        resposta = self.client.get(reverse("accounts:minha_conta"))
        self.assertContains(resposta, "Gerar link do painel")

    def test_gerar_link_mostra_url_em_minha_conta(self):
        token = self._gerar()
        self.assertTrue(token and len(token) >= 40)
        resposta = self.client.get(reverse("accounts:minha_conta"))
        self.assertContains(resposta, reverse("core:painel_tv", args=[token]))

    def test_painel_abre_sem_login_e_so_leitura(self):
        token = self._gerar()
        self.client.logout()
        resposta = self.client.get(reverse("core:painel_tv", args=[token]))
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Ana TV")
        self.assertContains(resposta, "Posições em carteira")
        self.assertNotContains(resposta, "Atualizar cotações agora")
        self.assertEqual(resposta["X-Robots-Tag"], "noindex, nofollow")

    def test_verificar_devolve_versao_sem_login(self):
        token = self._gerar()
        self.client.logout()
        resposta = self.client.get(reverse("core:painel_tv_verificar", args=[token]))
        self.assertEqual(resposta.status_code, 200)
        self.assertIn("versao", resposta.json())

    def test_versao_muda_quando_entra_alerta_novo(self):
        from core.models import Alerta

        token = self._gerar()
        url = reverse("core:painel_tv_verificar", args=[token])
        antes = self.client.get(url).json()["versao"]
        Alerta.objects.create(usuario=self.usuario, tipo=Alerta.LEMBRETE, mensagem="teste")
        self.assertNotEqual(self.client.get(url).json()["versao"], antes)

    def test_token_invalido_da_404(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("core:painel_tv", args=["nao-existe"])).status_code, 404)

    def test_gerar_novo_link_invalida_o_anterior(self):
        antigo = self._gerar()
        novo = self._gerar()
        self.assertNotEqual(antigo, novo)
        self.assertEqual(self.client.get(reverse("core:painel_tv", args=[antigo])).status_code, 404)
        self.assertEqual(self.client.get(reverse("core:painel_tv", args=[novo])).status_code, 200)

    def test_desativar_link(self):
        token = self._gerar()
        self.client.post(reverse("accounts:painel_tv_link"), {"acao": "desativar"})
        self.assertIsNone(PerfilUsuario.objects.get(usuario=self.usuario).painel_tv_token)
        self.assertEqual(self.client.get(reverse("core:painel_tv", args=[token])).status_code, 404)

    def test_gerar_link_exige_login_e_post(self):
        self.assertEqual(self.client.get(reverse("accounts:painel_tv_link")).status_code, 405)
        self.client.logout()
        self.client.post(reverse("accounts:painel_tv_link"), {"acao": "gerar"})
        self.assertFalse(PerfilUsuario.objects.filter(painel_tv_token__isnull=False).exists())


class LogoContaTests(TestCase):
    """Logo da conta: enviada em Minha Conta, aparece no menu e no painel para TV."""

    def setUp(self):
        self.usuario = User.objects.create_user(username="investidor_logo", password="SenhaForte123!")
        self.client.login(username="investidor_logo", password="SenhaForte123!")

    def _imagem(self, formato="PNG", tamanho=(1200, 400), nome="logo.png"):
        from io import BytesIO

        from django.core.files.uploadedfile import SimpleUploadedFile
        from PIL import Image

        saida = BytesIO()
        Image.new("RGBA" if formato == "PNG" else "RGB", tamanho, (0, 200, 220, 255)).save(saida, format=formato)
        return SimpleUploadedFile(nome, saida.getvalue())

    def _enviar(self, arquivo):
        return self.client.post(reverse("accounts:logo_conta"), {"acao": "enviar", "logo": arquivo})

    def test_sem_logo_menu_mostra_marca_padrao(self):
        resposta = self.client.get(reverse("accounts:minha_conta"))
        self.assertContains(resposta, 'class="brand"')
        self.assertNotContains(resposta, 'class="brand-logo"')

    def test_envio_reduz_grava_png_e_mostra_no_menu(self):
        from io import BytesIO

        from PIL import Image

        self._enviar(self._imagem(formato="JPEG", nome="logo.jpg"))
        perfil = PerfilUsuario.objects.get(usuario=self.usuario)
        imagem = Image.open(BytesIO(bytes(perfil.logo)))
        self.assertEqual(imagem.format, "PNG")
        self.assertLessEqual(imagem.width, 480)
        self.assertLessEqual(imagem.height, 160)

        resposta = self.client.get(reverse("accounts:minha_conta"))
        self.assertContains(resposta, 'class="brand-logo"')
        self.assertContains(resposta, "data:image/png;base64,")

    def test_arquivo_que_nao_e_imagem_e_recusado(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self._enviar(SimpleUploadedFile("logo.svg", b"<svg onload='alert(1)'></svg>"))
        self.assertFalse(PerfilUsuario.objects.filter(usuario=self.usuario, logo__isnull=False).exists())

    def test_remover_volta_marca_padrao(self):
        self._enviar(self._imagem())
        self.client.post(reverse("accounts:logo_conta"), {"acao": "remover"})
        self.assertIsNone(PerfilUsuario.objects.get(usuario=self.usuario).logo)
        self.assertNotContains(self.client.get(reverse("accounts:minha_conta")), 'class="brand-logo"')

    def test_logo_aparece_no_painel_tv(self):
        self._enviar(self._imagem())
        self.client.post(reverse("accounts:painel_tv_link"), {"acao": "gerar"})
        token = PerfilUsuario.objects.get(usuario=self.usuario).painel_tv_token
        self.client.logout()
        resposta = self.client.get(reverse("core:painel_tv", args=[token]))
        self.assertContains(resposta, 'class="tv-logo"')


@override_settings(CADASTRO_PUBLICO=False, CONVITE_VALIDADE_DIAS=5)
class ConviteCadastroTests(TestCase):
    """Link de convite: cria conta com o cadastro fechado, uma vez só e dentro da validade."""

    def setUp(self):
        self.admin = User.objects.create_superuser(username="admin_conv", password="SenhaForte123!")
        self.comum = User.objects.create_user(username="comum_conv", password="SenhaForte123!")
        self.staff = User.objects.create_user(username="staff_conv", password="SenhaForte123!", is_staff=True)

    def _gerar(self):
        from .models import ConviteCadastro

        self.client.login(username="admin_conv", password="SenhaForte123!")
        self.client.post(reverse("accounts:convites"), {"observacao": "Fulano"})
        self.client.logout()
        return ConviteCadastro.objects.latest("criado_em")

    def _dados(self, username="convidado"):
        return {
            "username": username, "first_name": "Convidado", "email": "c@example.com",
            "password1": "SenhaForte123!", "password2": "SenhaForte123!",
        }

    def test_staff_gera_convite_com_validade_do_env(self):
        from datetime import timedelta
        from django.utils import timezone

        convite = self._gerar()
        self.assertEqual(convite.observacao, "Fulano")
        self.assertTrue(convite.valido)
        self.assertAlmostEqual(
            convite.expira_em, timezone.now() + timedelta(days=5), delta=timedelta(minutes=1)
        )

    def test_so_superusuario_acessa_convites(self):
        from .models import ConviteCadastro

        for quem in ("comum_conv", "staff_conv"):
            self.client.login(username=quem, password="SenhaForte123!")
            self.assertRedirects(self.client.get(reverse("accounts:convites")), reverse("core:dashboard"),
                                 fetch_redirect_response=False)
            self.client.post(reverse("accounts:convites"), {})
            self.assertNotContains(self.client.get(reverse("core:menu")), reverse("accounts:convites"))
            self.client.logout()
        self.assertFalse(ConviteCadastro.objects.exists())

        self.client.login(username="admin_conv", password="SenhaForte123!")
        self.assertContains(self.client.get(reverse("core:menu")), reverse("accounts:convites"))

    def test_cadastro_pelo_convite_cria_conta_loga_e_vai_para_instalacao(self):
        convite = self._gerar()
        url = reverse("accounts:convite_cadastro", args=[convite.token])
        self.assertEqual(self.client.get(url).status_code, 200)
        resposta = self.client.post(url, self._dados())
        self.assertRedirects(resposta, reverse("accounts:instalar_app"))
        convite.refresh_from_db()
        self.assertEqual(convite.usado_por.username, "convidado")
        self.assertEqual(convite.situacao, "usado")

    def test_convite_nao_pode_ser_usado_duas_vezes(self):
        convite = self._gerar()
        url = reverse("accounts:convite_cadastro", args=[convite.token])
        self.client.post(url, self._dados())
        self.client.logout()
        resposta = self.client.post(url, self._dados(username="segundo"))
        self.assertEqual(resposta.status_code, 410)
        self.assertFalse(User.objects.filter(username="segundo").exists())

    def test_convite_vencido_ou_cancelado_recusa(self):
        from django.utils import timezone

        convite = self._gerar()
        url = reverse("accounts:convite_cadastro", args=[convite.token])
        convite.expira_em = timezone.now()
        convite.save()
        self.assertContains(self.client.get(url), "venceu", status_code=410)

        convite = self._gerar()
        self.client.login(username="admin_conv", password="SenhaForte123!")
        self.client.post(reverse("accounts:convite_cancelar", args=[convite.pk]))
        self.client.logout()
        resposta = self.client.post(reverse("accounts:convite_cadastro", args=[convite.token]), self._dados())
        self.assertEqual(resposta.status_code, 410)
        self.assertFalse(User.objects.filter(username="convidado").exists())

    def test_token_inexistente_da_404(self):
        self.assertEqual(self.client.get(reverse("accounts:convite_cadastro", args=["nao-existe"])).status_code, 404)

    def test_tela_de_instalacao_exige_login(self):
        self.assertEqual(self.client.get(reverse("accounts:instalar_app")).status_code, 302)
        self.client.login(username="comum_conv", password="SenhaForte123!")
        self.assertContains(self.client.get(reverse("accounts:instalar_app")), "btn-instalar-pwa-pagina")


@override_settings(CADASTRO_PUBLICO=False)
class ValidadeAcessoTests(TestCase):
    """Data limite de acesso da conta criada por convite (só superusuário define)."""

    def setUp(self):
        self.super = User.objects.create_superuser(username="super_acesso", password="SenhaForte123!")
        self.staff = User.objects.create_user(username="staff_acesso", password="SenhaForte123!", is_staff=True)

    def _gerar(self, quem, acesso_ate=""):
        from .models import ConviteCadastro

        self.client.login(username=quem, password="SenhaForte123!")
        self.client.post(reverse("accounts:convites"), {"acesso_ate": acesso_ate})
        self.client.logout()
        return ConviteCadastro.objects.latest("criado_em")

    def _cadastrar(self, convite):
        self.client.post(reverse("accounts:convite_cadastro", args=[convite.token]), {
            "username": "conta_prazo", "first_name": "Prazo", "email": "p@example.com",
            "password1": "SenhaForte123!", "password2": "SenhaForte123!",
        })
        return User.objects.get(username="conta_prazo")

    def test_superusuario_define_validade_que_vai_para_a_conta(self):
        from datetime import timedelta
        from django.utils import timezone

        data = timezone.localdate() + timedelta(days=30)
        convite = self._gerar("super_acesso", data.isoformat())
        self.assertEqual(convite.acesso_ate, data)
        usuario = self._cadastrar(convite)
        self.assertEqual(usuario.perfil.acesso_ate, data)

    def test_data_no_passado_e_recusada(self):
        from .models import ConviteCadastro

        self.client.login(username="super_acesso", password="SenhaForte123!")
        self.client.post(reverse("accounts:convites"), {"acesso_ate": "2000-01-01"})
        self.assertFalse(ConviteCadastro.objects.exists())

    def test_acesso_vencido_barra_login_e_derruba_sessao(self):
        from datetime import timedelta
        from django.utils import timezone

        convite = self._gerar("super_acesso", (timezone.localdate() + timedelta(days=1)).isoformat())
        usuario = self._cadastrar(convite)  # já fica logado
        self.assertEqual(self.client.get(reverse("accounts:instalar_app")).status_code, 200)

        usuario.perfil.acesso_ate = timezone.localdate() - timedelta(days=1)
        usuario.perfil.save()
        resposta = self.client.get(reverse("accounts:instalar_app"))
        self.assertRedirects(resposta, reverse("accounts:login"))
        self.assertNotIn("_auth_user_id", self.client.session)

        resposta = self.client.post(reverse("accounts:login"), {"username": "conta_prazo", "password": "SenhaForte123!"})
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_ultimo_dia_ainda_tem_acesso(self):
        from django.utils import timezone

        convite = self._gerar("super_acesso", timezone.localdate().isoformat())
        self._cadastrar(convite)
        self.assertEqual(self.client.get(reverse("accounts:instalar_app")).status_code, 200)

    def test_superusuario_altera_e_tira_limite(self):
        from datetime import timedelta
        from django.utils import timezone

        convite = self._gerar("super_acesso")
        usuario = self._cadastrar(convite)
        self.client.logout()
        self.client.login(username="super_acesso", password="SenhaForte123!")
        nova = timezone.localdate() + timedelta(days=60)
        self.client.post(reverse("accounts:convite_acesso", args=[convite.pk]), {"acesso_ate": nova.isoformat()})
        usuario.perfil.refresh_from_db()
        self.assertEqual(usuario.perfil.acesso_ate, nova)
        self.client.post(reverse("accounts:convite_acesso", args=[convite.pk]), {"acesso_ate": ""})
        usuario.perfil.refresh_from_db()
        self.assertIsNone(usuario.perfil.acesso_ate)

    def test_staff_comum_nao_altera_validade(self):
        from datetime import timedelta
        from django.utils import timezone

        convite = self._gerar("super_acesso")
        usuario = self._cadastrar(convite)
        self.client.logout()
        self.client.login(username="staff_acesso", password="SenhaForte123!")
        self.client.post(reverse("accounts:convite_acesso", args=[convite.pk]),
                         {"acesso_ate": (timezone.localdate() + timedelta(days=5)).isoformat()})
        self.assertFalse(PerfilUsuario.objects.filter(usuario=usuario, acesso_ate__isnull=False).exists())
