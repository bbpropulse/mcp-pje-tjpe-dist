"""MCP local para serviços do PJe e do SICAJUD do TJPE."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("mcp-pje-tjpe")
except PackageNotFoundError:  # pragma: no cover - execução direto da árvore de fontes
    __version__ = "0.7.0"

__all__ = ["__version__"]
