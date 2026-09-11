from mcp.server.mcpserver.exceptions import ToolError


class PjeTjpeError(ToolError):
    """Erro esperado e apresentável ao usuário do MCP."""


class ServicoIndisponivelError(PjeTjpeError):
    """O serviço do tribunal não respondeu como esperado."""


class ValidacaoError(PjeTjpeError):
    """A entrada não atende ao formato exigido pelo tribunal."""


class CredenciaisAusentesError(PjeTjpeError):
    """Credenciais ainda não foram configuradas localmente."""


class InterfacePjeAlteradaError(PjeTjpeError):
    """A interface autenticada divergiu do fluxo de leitura auditado."""
