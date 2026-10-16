def logo_conta(request):
    """
    Disponibiliza em todos os templates a logo da conta logada (data URI,
    ver PerfilUsuario.logo_data_uri) - base.html mostra ela no lugar da marca
    "⚡ BOLSATRADER"; "" quando a conta não tem logo (fica a marca padrão).
    Busca só a coluna da logo, sem criar perfil.
    """
    if not request.user.is_authenticated:
        return {"logo_conta": ""}

    from .models import PerfilUsuario

    logo = PerfilUsuario.objects.filter(usuario=request.user).values_list("logo", flat=True).first()
    return {"logo_conta": PerfilUsuario(logo=logo).logo_data_uri}
