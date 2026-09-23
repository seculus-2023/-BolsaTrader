def alertas_pendentes(request):
    """
    Disponibiliza em todos os templates a contagem de alertas não lidos
    (badge do menu) e, separadamente, quantos desses realmente pedem uma
    ação do usuário (meta de lucro/perda atingida, ou sinal de compra/venda
    do robô consultor) - usado para tocar um som de aviso (ver base.html);
    tendência simples e lembrete geral não emitem som.
    """
    if not request.user.is_authenticated:
        return {"total_alertas_pendentes": 0, "total_alertas_acao_pendentes": 0}

    from .models import Alerta

    nao_lidos = request.user.alertas.filter(lido=False)
    total = nao_lidos.count()
    total_acao = nao_lidos.filter(
        tipo__in=[Alerta.LUCRO, Alerta.PERDA, Alerta.SINAL_COMPRA, Alerta.SINAL_VENDA]
    ).count()
    return {"total_alertas_pendentes": total, "total_alertas_acao_pendentes": total_acao}


def whatsapp_link(request):
    """Disponibiliza o link 'wa.me' do ícone de contato do WhatsApp em todos os templates."""
    from django.conf import settings

    numero = settings.WHATSAPP_NUMERO
    return {"whatsapp_link": f"https://wa.me/{numero}" if numero else ""}


def ultima_atualizacao_cotacoes(request):
    """
    Disponibiliza em todos os templates o horário da cotação mais recente
    entre os ativos do usuário logado (mesmo conjunto que "Atualizar cotações
    agora" e o comando "atualizar_cotacoes --loop" atualizam) - mostrado perto
    do botão de atualizar, pra deixar claro se o preço na tela está fresco.
    """
    if not request.user.is_authenticated:
        return {"ultima_atualizacao_cotacoes": None}

    from django.db.models import Max

    from .models import Ativo

    ultima = Ativo.objects.filter(operacoes__usuario=request.user).aggregate(Max("atualizado_em"))
    return {"ultima_atualizacao_cotacoes": ultima["atualizado_em__max"]}


def horario_b3(request):
    """
    Disponibiliza em todos os templates o horário de negociação da B3
    (configurável em B3_HORARIO_ABERTURA/B3_HORARIO_FECHAMENTO no .env) e se
    o mercado está aberto agora (dia útil - segunda a sexta - dentro do
    horário) - mostrado em destaque no Painel, Posições e Minhas Operações, e
    usado também para bloquear "Atualizar cotações agora" fora do horário
    (ver core.services.mercado_b3_aberto).
    """
    from django.conf import settings

    from .services import mercado_b3_aberto

    return {
        "b3_horario_abertura": settings.B3_HORARIO_ABERTURA,
        "b3_horario_fechamento": settings.B3_HORARIO_FECHAMENTO,
        "b3_mercado_aberto": mercado_b3_aberto(),
    }


def post_it(request):
    """
    Disponibiliza o post-it (bloco de notas pessoal e fixo) do usuário nos
    templates que o exibem (Menu - ver templates/core/_post_it.html). Só
    busca (não cria) - criar um post-it
    vazio aqui gravaria no banco em toda requisição de todo usuário
    autenticado, mesmo em telas onde ele nunca aparece.
    """
    if not request.user.is_authenticated:
        return {"post_it_texto": "", "post_it_minimizado": False}

    from .services import obter_post_it

    post_it = obter_post_it(request.user)
    return {
        "post_it_texto": post_it.texto if post_it else "",
        "post_it_minimizado": post_it.minimizado if post_it else False,
    }
