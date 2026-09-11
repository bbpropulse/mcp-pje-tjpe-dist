from __future__ import annotations

import asyncio

import typer

from mcp_pje_tjpe import __version__
from mcp_pje_tjpe.browser import BrowserManager
from mcp_pje_tjpe.config import settings
from mcp_pje_tjpe.credentials import credential_store
from mcp_pje_tjpe.diagnostics import run_diagnostics

app = typer.Typer(
    name="pje-tjpe",
    help="Servidor MCP local para o PJe e o SICAJUD do TJPE.",
    no_args_is_help=True,
)


@app.command()
def serve() -> None:
    """Inicia o servidor MCP usando o transporte stdio."""
    from mcp_pje_tjpe.server import serve_stdio

    serve_stdio()


@app.command()
def setup() -> None:
    """Salva credenciais pessoais no Keychain da máquina."""
    cpf = typer.prompt("CPF")
    password = typer.prompt("Senha do PJe", hide_input=True, confirmation_prompt=True)
    typer.echo(
        "O TJPE exige MFA. Guardar a semente TOTP junto da senha reduz a "
        "separação entre os fatores; prefira o login manual com navegador visível."
    )
    totp_seed = ""
    if typer.confirm("Deseja guardar, por sua conta e risco, a semente TOTP?", default=False):
        totp_seed = typer.prompt("Semente TOTP", hide_input=True)
    credential_store.save(cpf, password, totp_seed or None)
    typer.echo("Credenciais salvas no Keychain. Nenhum segredo foi gravado no projeto.")


@app.command("clear-credentials")
def clear_credentials() -> None:
    """Remove do Keychain as credenciais guardadas por este MCP."""
    if not typer.confirm("Remover CPF, senha e TOTP do Keychain?"):
        raise typer.Abort()
    credential_store.clear()
    typer.echo("Credenciais removidas.")


@app.command()
def doctor() -> None:
    """Executa diagnóstico local e dos endpoints públicos do TJPE."""

    async def _run() -> None:
        browser = BrowserManager(settings)
        try:
            result = await run_diagnostics(browser, settings, credential_store)
        finally:
            await browser.close()
        for item in result.itens:
            marker = "OK" if item.sucesso else "ERRO"
            typer.echo(f"[{marker}] {item.nome}: {item.detalhe}")
        if not result.sucesso:
            raise typer.Exit(code=1)

    asyncio.run(_run())


@app.command()
def version() -> None:
    """Mostra a versão instalada."""
    typer.echo(__version__)
