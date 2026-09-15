from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import PerfilUsuario


class MinhaContaTests(TestCase):
    """Tela de edição do número de WhatsApp usado para avisos automáticos (ver core.services.enviar_whatsapp)."""

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

    def test_outro_usuario_nao_ve_nem_altera_perfil_alheio(self):
        outro = User.objects.create_user(username="outro_conta", password="SenhaForte123!")
        PerfilUsuario.objects.create(usuario=outro, numero_whatsapp="5565911112222")

        self.client.post(reverse("accounts:minha_conta"), {"numero_whatsapp": "5565999998888"})

        perfil_outro = PerfilUsuario.objects.get(usuario=outro)
        self.assertEqual(perfil_outro.numero_whatsapp, "5565911112222")  # não foi alterado
        perfil_proprio = PerfilUsuario.objects.get(usuario=self.usuario)
        self.assertEqual(perfil_proprio.numero_whatsapp, "5565999998888")
