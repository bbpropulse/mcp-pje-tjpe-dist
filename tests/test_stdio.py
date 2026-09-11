import sys

import pytest
from mcp import Client, StdioServerParameters


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_real_stdio_entrypoint() -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_pje_tjpe", "serve"],
    )
    async with Client(params, raise_exceptions=True) as client:
        listed = await client.list_tools()
        assert "simular_custas" in {tool.name for tool in listed.tools}
