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
