from __future__ import annotations

import re
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlsplit

from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    Locator,
    Page,
    Response,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from mcp_pje_tjpe.browser import BrowserManager
from mcp_pje_tjpe.config import Settings
from mcp_pje_tjpe.errors import ServicoIndisponivelError, ValidacaoError
from mcp_pje_tjpe.models import ClasseCustas, ItemCustas, SimulacaoCustas
from mcp_pje_tjpe.money import format_brl_input, parse_decimal

DISCLAIMER = (
    "A simulação é uma prévia fornecida pelo SICAJUD. Situações externas ao sistema "
    "podem alterar a guia; o resultado não substitui a conferência oficial do TJPE."
)
NATUREZA_SIMULACAO = "estimativa_sicajud"

FORM_SELECTOR = "form#simulacaoForm"
CLASS_INPUT_SELECTOR = f"{FORM_SELECTOR} input[id$=':descricaoClasse']"
SUGGESTION_ROWS_SELECTOR = "table[id$=':sugestaoClasse:suggest'] tbody tr.rich-sb-int:visible"
VALUE_INPUT_SELECTOR = f"{FORM_SELECTOR} input[id$=':valorCausa']"
ERROR_SELECTOR = f"{FORM_SELECTOR} [id$=':errorMessage']"
RESULT_SELECTOR = f"{FORM_SELECTOR} span[id$=':item-preparo-container']"
SELECTED_CLASS_TABLE_SELECTOR = f"{FORM_SELECTOR} table.item-preparo-container"
ITEM_TABLE_SELECTOR = f"{FORM_SELECTOR} table[id$=':itensDataTable']"
ITEM_ROWS_SELECTOR = f"{ITEM_TABLE_SELECTOR} tbody[id$=':tb'] > tr"
TOTAL_SELECTOR = f"{ITEM_TABLE_SELECTOR} tfoot .custas-diversas-footer label:last-child"

_TOTAL_RE = re.compile(r"Valor Total:\s*(R\$\s*[\d.,]+)", re.IGNORECASE)
_VERSION_RE = re.compile(r"Versão\s+([\d.]+)", re.IGNORECASE)
_ITEM_RE = re.compile(r"(.+?)\s+(R\$\s*[\d.,]+)$")
_LEGAL_BASIS_RE = re.compile(r"Fundamenta(?:ção|cao) Legal\s*:?\s*(.+)$", re.IGNORECASE)
_CLASS_SELECTION_POST_RE = re.compile(r"sugestaoClasse(?:%3A|:)[^=&]+=")

AjaxAction = Callable[[], Awaitable[None]]
ResponseFilter = Callable[[Response], bool]
ItemRow = tuple[str, str, str | None]


def _compact(value: str | None) -> str:
    return " ".join((value or "").split())


def _plain(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return " ".join(
        "".join(char for char in normalized if not unicodedata.combining(char)).upper().split()
    )


def _parse_suggestion_cells(cells: Sequence[str]) -> ClasseCustas | None:
    """Extrai uma sugestão sem depender da coluna oculta do RichFaces."""

    clean = [_compact(cell) for cell in cells]
    for index, value in enumerate(clean):
        if not value.isdigit():
            continue
        description = next(
            (item for item in clean[index + 1 :] if item and not item.isdigit()),
            "",
        )
        if not description:
            description = next(
                (item for item in reversed(clean[:index]) if item and not item.isdigit()),
                "",
            )
        if description:
            return ClasseCustas(codigo=value, descricao=description)
    return None


def _parse_brl_text(value: str, *, field: str) -> Decimal:
    match = re.search(r"R\$\s*([\d.,]+)", value)
    if not match:
        raise ServicoIndisponivelError(f"o SICAJUD não apresentou {field} em formato monetário")
    return parse_decimal(match.group(1))


def _extract_legal_basis(details: str | None) -> str | None:
    match = _LEGAL_BASIS_RE.search(_compact(details))
    return match.group(1).strip() if match and match.group(1).strip() else None


def _extract_version(page_text: str) -> str | None:
    match = _VERSION_RE.search(page_text)
    return match.group(1) if match else None


def parse_simulation_snapshot(
    *,
    classe_codigo: str,
    classe_descricao: str,
    valor_causa: Decimal,
    item_rows: Sequence[ItemRow],
    total_text: str,
    source_url: str,
    page_text: str = "",
    captured_at: datetime | None = None,
) -> SimulacaoCustas:
    """Converte o estado estruturado da tela em uma simulação validada."""

    items = [
        ItemCustas(
            descricao=_compact(description),
            valor=_parse_brl_text(value, field=f"o item {_compact(description)!r}"),
            fundamento_legal=_extract_legal_basis(details),
        )
        for description, value, details in item_rows
        if _compact(description)
    ]
    total = _parse_brl_text(total_text, field="o valor total")
    calculated_total = sum((item.valor for item in items), start=Decimal("0.00"))
    if calculated_total != total:
        raise ServicoIndisponivelError(
            "a soma dos itens do SICAJUD não corresponde ao valor total "
            f"({calculated_total} != {total})"
        )

    return SimulacaoCustas(
        classe=ClasseCustas(
            codigo=_compact(classe_codigo),
            descricao=_compact(classe_descricao),
        ),
        valor_causa=valor_causa,
        valor_total=total,
        itens=items,
        url_fonte=source_url,
        versao_sicajud=_extract_version(page_text),
        aviso=DISCLAIMER,
        natureza=NATUREZA_SIMULACAO,
        capturado_em=captured_at or datetime.now(UTC),
    )


def parse_result_rows(
    rows: list[str], *, classe_fallback: str, valor_causa: Decimal, source_url: str
) -> SimulacaoCustas:
    """Compatibilidade com o parser textual usado nas primeiras versões do MCP."""

    clean_rows = [_compact(row) for row in rows if _compact(row)]
    joined = "\n".join(clean_rows)

    total_match = _TOTAL_RE.search(joined)
    if not total_match:
        raise ServicoIndisponivelError("o SICAJUD não apresentou o valor total esperado")

    code_match = re.search(r"Código da Classe\s+(\d+)", joined, re.IGNORECASE)
    description_match = re.search(
        r"Descrição da Classe\s+(.+?)(?:\n|Item de Preparo)",
        joined,
        re.IGNORECASE,
    )

    item_rows: list[ItemRow] = []
    for row in clean_rows:
        if re.search(r"Valor Total", row, re.IGNORECASE):
            continue
        match = _ITEM_RE.fullmatch(row)
        if not match:
            continue
        description = match.group(1).strip()
        if _plain(description) in {"VALOR", "VALOR DA CAUSA"}:
            continue
        item_rows.append((description, match.group(2), None))

    return parse_simulation_snapshot(
        classe_codigo=code_match.group(1) if code_match else "",
        classe_descricao=(
            description_match.group(1).strip() if description_match else classe_fallback
        ),
        valor_causa=valor_causa,
        item_rows=item_rows,
        total_text=total_match.group(0),
        source_url=source_url,
        page_text=joined,
    )


class SicajudClient:
    def __init__(self, browser: BrowserManager, config: Settings) -> None:
        self.browser = browser
        self.config = config

    async def _open_simulation(self, page: Page) -> None:
        try:
            response = await page.goto(
                self.config.urls.sicajud_simulacao,
                wait_until="domcontentloaded",
            )
            if response is not None and not response.ok:
                raise ServicoIndisponivelError(
                    f"o SICAJUD respondeu HTTP {response.status} ao abrir a simulação"
                )
            await page.locator(FORM_SELECTOR).wait_for(state="visible")
            await page.get_by_role(
                "heading", name="Simulação de Taxa e Custas", exact=True
            ).wait_for(state="visible")
        except ServicoIndisponivelError:
            raise
        except PlaywrightError as exc:
            raise ServicoIndisponivelError(
                "não foi possível abrir a simulação pública do SICAJUD"
            ) from exc

    async def _await_ajax(
        self,
        page: Page,
        action: AjaxAction,
        *,
        phase: str,
        response_filter: ResponseFilter | None = None,
    ) -> Response:
        def is_simulation_post(response: Response) -> bool:
            path = urlsplit(response.url).path.split(";", maxsplit=1)[0]
            return (
                response.request.method == "POST"
                and path.endswith("/simularCustas.xhtml")
                and (response_filter is None or response_filter(response))
            )

        try:
            async with page.expect_response(
                is_simulation_post, timeout=self.config.timeout_ms
            ) as response_info:
                await action()
            response = await response_info.value
            await response.body()
        except PlaywrightTimeoutError as exc:
            raise ServicoIndisponivelError(
                f"o SICAJUD não concluiu {phase} dentro do prazo"
            ) from exc
        except PlaywrightError as exc:
            raise ServicoIndisponivelError(
                f"falha de comunicação com o SICAJUD durante {phase}"
            ) from exc

        if not response.ok:
            raise ServicoIndisponivelError(
                f"o SICAJUD respondeu HTTP {response.status} durante {phase}"
            )
        return response

    async def _type_class(self, page: Page, term: str) -> list[ClasseCustas]:
        normalized_term = _compact(term)
        if len(normalized_term) < 3:
            raise ValidacaoError("informe pelo menos 3 caracteres da classe processual")

        field = page.locator(CLASS_INPUT_SELECTOR)
        await field.fill("")
        # O RichFaces 3 perde eventos quando a digitação é rápida demais.
        await self._await_ajax(
            page,
            lambda: field.press_sequentially(normalized_term, delay=100),
            phase="a pesquisa de classes",
        )

        suggestions = page.locator(SUGGESTION_ROWS_SELECTOR)
        try:
            await suggestions.first.wait_for(state="visible", timeout=self.config.timeout_ms)
        except PlaywrightTimeoutError as exc:
            raise ServicoIndisponivelError(
                "o SICAJUD não concluiu a pesquisa de classes dentro do prazo"
            ) from exc

        result: list[ClasseCustas] = []
        seen: set[tuple[str, str]] = set()
        for row in await suggestions.all():
            item = _parse_suggestion_cells(await row.locator("td").all_text_contents())
            if item is None:
                continue
            key = (item.codigo, item.descricao)
            if key not in seen:
                seen.add(key)
                result.append(item)

        if not result:
            raise ValidacaoError(f"nenhuma classe encontrada para {normalized_term!r}")
        return result

    async def pesquisar_classes(self, termo: str) -> list[ClasseCustas]:
        async with self.browser.page() as page:
            await self._open_simulation(page)
            return await self._type_class(page, termo)

    async def _click_class(self, page: Page, selected: ClasseCustas) -> None:
        target: Locator | None = None
        for row in await page.locator(SUGGESTION_ROWS_SELECTOR).all():
            candidate = _parse_suggestion_cells(await row.locator("td").all_text_contents())
            if candidate is None:
                continue
            if candidate.codigo == selected.codigo and _plain(candidate.descricao) == _plain(
                selected.descricao
            ):
                target = row
                break

        if target is None:
            raise ServicoIndisponivelError("a sugestão escolhida desapareceu antes da seleção")

        await self._await_ajax(
            page,
            target.click,
            phase="a seleção da classe",
            response_filter=lambda response: bool(
                _CLASS_SELECTION_POST_RE.search(response.request.post_data or "")
            ),
        )
        await self._wait_selected_class(page, selected)

    async def _wait_selected_class(self, page: Page, expected: ClasseCustas) -> None:
        try:
            await page.wait_for_function(
                """
                code => {
                    const cell = document.querySelector(
                        "table.item-preparo-container tbody tr:first-child td:nth-child(2)"
                    );
                    return cell && cell.textContent.trim() === code;
                }
                """,
                arg=expected.codigo,
                timeout=self.config.timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
            raise ServicoIndisponivelError("o SICAJUD não confirmou a classe selecionada") from exc
        self._assert_selected_class(await self._read_selected_class(page), expected)

    async def _read_selected_class(self, page: Page) -> ClasseCustas:
        rows = page.locator(f"{SELECTED_CLASS_TABLE_SELECTOR} tbody tr")
        if await rows.count() < 2:
            raise ServicoIndisponivelError(
                "o SICAJUD não apresentou os dados da classe selecionada"
            )
        code = _compact(await rows.nth(0).locator("td").nth(1).text_content())
        description = _compact(await rows.nth(1).locator("td").nth(1).text_content())
        if not code or not description:
            raise ServicoIndisponivelError(
                "os dados da classe selecionada vieram incompletos do SICAJUD"
            )
        return ClasseCustas(codigo=code, descricao=description)

    @staticmethod
    def _assert_selected_class(actual: ClasseCustas, expected: ClasseCustas) -> None:
        if actual.codigo != expected.codigo or _plain(actual.descricao) != _plain(
            expected.descricao
        ):
            raise ServicoIndisponivelError(
                "o SICAJUD manteve uma classe diferente da solicitada: "
                f"esperada {expected.codigo} - {expected.descricao}; "
                f"recebida {actual.codigo} - {actual.descricao}"
            )

    async def _fill_value(self, page: Page, amount: Decimal) -> None:
        formatted = format_brl_input(amount)
        field = page.locator(VALUE_INPUT_SELECTOR)
        await field.fill(formatted)
        if await field.input_value() != formatted:
            raise ServicoIndisponivelError(
                "o SICAJUD alterou inesperadamente o valor da causa informado"
            )
        await self._await_ajax(
            page,
            lambda: field.press("Tab"),
            phase="o preenchimento do valor da causa",
        )
        if await page.locator(VALUE_INPUT_SELECTOR).input_value() != formatted:
            raise ServicoIndisponivelError("o SICAJUD não confirmou o valor da causa informado")

    async def _wait_for_result_or_error(self, page: Page) -> None:
        try:
            await page.wait_for_function(
                """
                () => {
                    const error = document.querySelector(
                        "form#simulacaoForm [id$=':errorMessage']"
                    );
                    const result = document.querySelector(
                        "form#simulacaoForm span[id$=':item-preparo-container']"
                    );
                    return Boolean(error && error.textContent.trim()) || Boolean(
                        result && getComputedStyle(result).display !== "none"
                    );
                }
                """,
                timeout=self.config.timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
            raise ServicoIndisponivelError(
                "o SICAJUD não apresentou resultado nem mensagem de erro"
            ) from exc

    async def _parse_page_result(
        self,
        page: Page,
        *,
        selected: ClasseCustas,
        amount: Decimal,
    ) -> SimulacaoCustas:
        actual = await self._read_selected_class(page)
        self._assert_selected_class(actual, selected)

        item_rows: list[ItemRow] = []
        for row in await page.locator(ITEM_ROWS_SELECTOR).all():
            cells = row.locator("td")
            if await cells.count() < 2:
                continue
            description = _compact(await cells.nth(0).text_content())
            value = _compact(await cells.nth(1).text_content())
            details: str | None = None
            if await cells.count() >= 3:
                detail_span = cells.nth(2).locator("span")
                if await detail_span.count():
                    details = await detail_span.first.text_content()
            item_rows.append((description, value, details))

        total = await page.locator(TOTAL_SELECTOR).text_content()
        if total is None:
            raise ServicoIndisponivelError("o SICAJUD não apresentou o valor total esperado")
        page_text = await page.locator("body").text_content() or ""
        return parse_simulation_snapshot(
            classe_codigo=actual.codigo,
            classe_descricao=actual.descricao,
            valor_causa=amount,
            item_rows=item_rows,
            total_text=total,
            source_url=self.config.urls.sicajud_simulacao,
            page_text=page_text,
        )

    async def simular(
        self,
        classe: str,
        valor_causa: str | int | float | Decimal,
        *,
        codigo_classe: str | None = None,
    ) -> SimulacaoCustas:
        amount = parse_decimal(valor_causa)
        if amount <= 0:
            raise ValidacaoError("o valor da causa deve ser maior que zero")

        async with self.browser.page() as page:
            await self._open_simulation(page)
            candidates = await self._type_class(page, classe)
            selected = self._select_class(candidates, classe, codigo_classe)

            await self._click_class(page, selected)
            self._assert_selected_class(await self._read_selected_class(page), selected)
            await self._fill_value(page, amount)

            await self._await_ajax(
                page,
                page.get_by_role("button", name="Calcular", exact=True).click,
                phase="o cálculo das custas",
            )
            await self._wait_for_result_or_error(page)

            error_message = _compact(await page.locator(ERROR_SELECTOR).text_content())
            if error_message:
                raise ValidacaoError(f"o SICAJUD recusou a simulação: {error_message}")
            if not await page.locator(RESULT_SELECTOR).is_visible():
                raise ServicoIndisponivelError(
                    "o SICAJUD concluiu o cálculo sem apresentar o resultado"
                )

            return await self._parse_page_result(page, selected=selected, amount=amount)

    @staticmethod
    def _select_class(
        candidates: list[ClasseCustas], requested: str, code: str | None
    ) -> ClasseCustas:
        if code:
            for candidate in candidates:
                if candidate.codigo == code:
                    return candidate
            raise ValidacaoError(f"código {code!r} não apareceu entre as sugestões do SICAJUD")

        exact = [item for item in candidates if _plain(item.descricao) == _plain(requested)]
        if len(exact) == 1:
            return exact[0]
        if len(candidates) == 1:
            return candidates[0]
        options = ", ".join(f"{item.codigo} - {item.descricao}" for item in candidates[:10])
        raise ValidacaoError(
            "a descrição corresponde a mais de uma classe; informe codigo_classe. "
            f"Opções: {options}"
        )
