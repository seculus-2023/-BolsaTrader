from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import PerfilUsuario


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
