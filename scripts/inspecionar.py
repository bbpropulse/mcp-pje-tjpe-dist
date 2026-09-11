#!/usr/bin/env python
"""Mapeia a estrutura real de telas autenticadas do PJe, sem alterar nada.

Existe porque o extrator de documentos e o de movimentos foram escritos contra
uma estrutura presumida: contra o PJe real, todo documento sai como
"Documento <id>" e a lista de movimentos volta vazia. Consertar isso no escuro
seria adivinhar; este script mostra o que a página de fato tem.

É diagnóstico, não capacidade: usa o mesmo caminho auditado do MCP (Acervo da
sessão -> Autos), só lê o DOM e não clica em nada que altere estado. O conteúdo
é resumido em forma — classes, atributos e amostras curtas de texto com CPF/CNPJ
mascarados — para não despejar peça processual no terminal.

    uv run python scripts/inspecionar.py painel
    uv run python scripts/inspecionar.py autos "Abreu e Lima - Varas" 0012114-...
    uv run python scripts/inspecionar.py tudo "Abreu e Lima - Varas" 0012114-...

`painel` mapeia as abas do Painel do Advogado e abre "Consulta Processos", que o
MCP nunca tocou: é a pergunta em aberto de saber se ela tem API própria ou só
postback Seam. `tudo` faz as duas coisas num login só, porque autenticar é o
recurso caro aqui.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace

import mcp_pje_tjpe.pje_read as pje_read
from mcp_pje_tjpe.browser import BrowserManager
from mcp_pje_tjpe.config import settings
from mcp_pje_tjpe.credentials import credential_store
from mcp_pje_tjpe.models import Grau
from mcp_pje_tjpe.pje_auth import PjeSessionManager
from mcp_pje_tjpe.pje_public import mask_cpf_cnpj
from mcp_pje_tjpe.pje_read import PjeReadService

# Lê a estrutura de cada nó da timeline e testa os seletores que o extrator usa
# hoje, para mostrar qual deles falha e o que existe no lugar.
_MAPA = r"""
() => {
  const amostra = (texto) => (texto || '').replace(/\s+/g, ' ').trim().slice(0, 120);
  const descrever = (el) => ({
    tag: el.tagName.toLowerCase(),
    classes: (el.className || '').toString().split(/\s+/).filter(Boolean).slice(0, 6),
    atributos: Array.from(el.attributes).map((a) => a.name).slice(0, 12),
    texto: amostra(el.innerText),
  });

  const timeline = document.querySelector('#divTimeLine');
  const raiz = {
    timeline_existe: Boolean(timeline),
    filhos_diretos: timeline
      ? Array.from(timeline.children).slice(0, 6).map(descrever)
      : [],
  };

  // Contagem por seletor: mostra de imediato qual premissa do extrator quebrou.
  const contagens = {};
  for (const sel of [
    '#divTimeLine .media.tipo-D',
    '#divTimeLine .media',
    '#divTimeLine .media.tipo-D [onclick*="abrirLinkDocumento"]',
    '[onclick*="abrirLinkDocumento"]',
    '#divTimeLine > div.media',
    '.timeline-item', '.documento', '[id^="divTimeLine"]',
  ]) {
    contagens[sel] = document.querySelectorAll(sel).length;
  }

  // Para os primeiros documentos, o que cada seletor de título devolveria.
  const alvos = Array.from(
    document.querySelectorAll('[onclick*="abrirLinkDocumento"]')
  ).slice(0, 4);
  const documentos = alvos.map((el) => {
    const container =
      el.closest('.media.tipo-D') || el.closest('.media') || el.parentElement;
    const candidatos = {};
    for (const sel of [
      'span.title', '[title]', 'strong', 'a span', 'a', '.titulo',
      '.media-heading', '.nomeArquivo', 'span',
    ]) {
      const achado = container ? container.querySelector(sel) : null;
      candidatos[sel] = achado ? amostra(achado.textContent) : null;
    }
    return {
      onclick: amostra(el.getAttribute('onclick')),
      texto_do_proprio_elemento: amostra(el.textContent),
      titulo_do_atributo: el.getAttribute('title'),
      container: container ? descrever(container) : null,
      candidatos_de_titulo: candidatos,
      ancestrais: (() => {
        const cadeia = [];
        let no = el.parentElement;
        for (let i = 0; i < 4 && no; i += 1) {
          cadeia.push({
            tag: no.tagName.toLowerCase(),
            classes: (no.className || '').toString().split(/\s+/).filter(Boolean).slice(0, 5),
          });
          no = no.parentElement;
        }
        return cadeia;
      })(),
    };
  });

  return {raiz, contagens, documentos};
}
"""


# Abas do painel são células RichFaces sem papel de acessibilidade; o mapa mostra
# quais existem, e o que "Consulta Processos" traz depois de aberta.
_MAPA_PAINEL = r"""
() => {
  const amostra = (t) => (t || '').replace(/\s+/g, ' ').trim().slice(0, 140);
  const abas = Array.from(document.querySelectorAll('td.rich-tab-header')).map((td) => ({
    rotulo: amostra(td.textContent),
    classes: (td.className || '').toString().split(/\s+/).filter(Boolean).slice(0, 6),
    ativa: (td.className || '').toString().includes('active'),
  }));
  const formularios = Array.from(document.querySelectorAll('form')).map((f) => ({
    id: f.getAttribute('id'),
    action: amostra((f.getAttribute('action') || '').split(';')[0]),
    campos: Array.from(f.elements).slice(0, 60).map((el) => ({
      nome: el.getAttribute('name'),
      tipo: (el.type || el.tagName || '').toLowerCase(),
      rotulo: amostra(el.getAttribute('title') || el.getAttribute('placeholder') || ''),
      preenchido: Boolean(el.value),
    })).filter((c) => c.nome),
  }));
  const botoes = Array.from(
    document.querySelectorAll('input[type=button], input[type=submit], button')
  ).slice(0, 30).map((b) => ({
    texto: amostra(b.value || b.textContent),
    tipo: (b.type || '').toLowerCase(),
    onclick: amostra(b.getAttribute('onclick')),
  }));
  return {abas, formularios, botoes};
}
"""


async def _autenticar(sessoes: PjeSessionManager) -> bool:
    print("abrindo o login assistido; informe certificado e PIN no PJeOffice…", flush=True)
    await sessoes.abrir_login_certificado(Grau.PRIMEIRO)
    for tentativa in range(1, 241):
        status = await sessoes.verificar_login_certificado(Grau.PRIMEIRO)
        if status.autenticado:
            print(f"autenticado (tentativa {tentativa})", flush=True)
            return True
        await asyncio.sleep(5)
    print("login não concluído no prazo", file=sys.stderr)
    return False


def _publicar(rotulo: str, mapa: object) -> None:
    print(f"\n########## {rotulo} ##########")
    print(mask_cpf_cnpj(json.dumps(mapa, ensure_ascii=False, indent=2)))


async def _mapear_painel(leitura: PjeReadService, sessoes: PjeSessionManager) -> None:
    """Abas do painel e o que a aba Consulta Processos apresenta depois de aberta."""
    async with sessoes.read_session(Grau.PRIMEIRO) as lease:
        # Reusa a navegação auditada até o painel; nenhuma jurisdição é escolhida.
        await leitura._load_acervo(lease.page, Grau.PRIMEIRO)
        _publicar("painel — abas visíveis", await lease.page.evaluate(_MAPA_PAINEL))

        aberto = await pje_read._click_visible_exact(
            lease.page, "Consulta Processos", timeout_ms=8_000
        )
        if not aberto:
            print("\naba 'Consulta Processos' não foi encontrada no painel", file=sys.stderr)
            return
        await lease.page.wait_for_timeout(1_500)
        _publicar("aba Consulta Processos — estrutura", await lease.page.evaluate(_MAPA_PAINEL))


async def _mapear_autos(
    leitura: PjeReadService, sessoes: PjeSessionManager, jurisdicao: str, numero: str
) -> None:
    await leitura.listar_acervo(Grau.PRIMEIRO, jurisdicao=jurisdicao, limite=100)
    async with sessoes.read_session(Grau.PRIMEIRO) as lease:
        vinculo = leitura._require_acervo_binding(lease, numero)
        await leitura._load_acervo(lease.page, Grau.PRIMEIRO, vinculo.jurisdicao)
        await leitura._refresh_acervo_binding(lease, numero)
        pagina, contexto, _ = await leitura._open_autos_from_acervo(lease, numero)
        try:
            _publicar("Autos — timeline", await pagina.evaluate(_MAPA))
        finally:
            await contexto.close()


async def main(argv: list[str]) -> int:
    alvo = argv[0] if argv else ""
    if alvo not in {"painel", "autos", "tudo"}:
        print(__doc__, file=sys.stderr)
        return 2
    if alvo in {"autos", "tudo"} and len(argv) != 3:
        print("uso: inspecionar.py <autos|tudo> \"<jurisdição>\" <NPU>", file=sys.stderr)
        return 2

    config = replace(settings, headless=settings.auth_headless)
    navegador = BrowserManager(config, accept_downloads=False)
    sessoes = PjeSessionManager(navegador, config, credential_store)
    leitura = PjeReadService(sessoes, config)
    try:
        if not await _autenticar(sessoes):
            return 1
        if alvo in {"painel", "tudo"}:
            await _mapear_painel(leitura, sessoes)
        if alvo in {"autos", "tudo"}:
            await _mapear_autos(leitura, sessoes, argv[1], argv[2])
        return 0
    finally:
        await sessoes.close()
        await navegador.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
