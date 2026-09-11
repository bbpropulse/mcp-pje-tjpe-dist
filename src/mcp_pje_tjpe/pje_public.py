from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, expect
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from mcp_pje_tjpe.browser import BrowserManager
from mcp_pje_tjpe.config import Settings
from mcp_pje_tjpe.errors import ServicoIndisponivelError, ValidacaoError
from mcp_pje_tjpe.models import Grau, MovimentoPublico, ProcessoPublico

# A máscara do campo de processo é montada por script; uma segunda tentativa
# cobre a corrida entre o campo ficar visível e o script assumir o controle.
_TENTATIVAS_MASCARA = 2
# Observado no TJPE: 202 com corpo vazio é a resposta do limitador de tráfego.
_HTTP_LIMITADO = 202
_ALLOWED_NPU_INPUT = re.compile(r"[0-9.\-\s]+")
_RESULT_COUNT = re.compile(r"(?:(\d+)\s+)?resultados?\s+encontrados?", re.IGNORECASE)
_CPF = re.compile(
    r"(?P<label>\bCPF\s*:?\s*)"
    r"(?P<value>[0-9]{3}\.?[0-9]{3}\.?[0-9]{3}-?[0-9]{2})(?![0-9])",
    re.IGNORECASE,
)
_CNPJ = re.compile(
    r"(?P<label>\bCNPJ\s*:?\s*)"
    r"(?P<value>[0-9]{2}\.?[0-9]{3}\.?[0-9]{3}/?[0-9]{4}-?[0-9]{2})(?![0-9])",
    re.IGNORECASE,
)
_DETAIL_PATH = re.compile(
    r"['\"](?P<path>/(?:1g|2g)/ConsultaPublica/"
    r"DetalheProcessoConsultaPublica/listView\.seam\?ca=[^'\"]+)['\"]"
)
_MOVEMENT = re.compile(
    r"^(?P<data>[0-9]{2}/[0-9]{2}/[0-9]{4}\s+[0-9]{2}:[0-9]{2}:[0-9]{2})\s+-\s+"
    r"(?P<descricao>.+)$"
)
_BLOCKED_PAGE = re.compile(
    r"(?:\b403\b.*\bforbidden\b|access denied|acesso negado|request blocked)",
    re.IGNORECASE,
)
_CAPTCHA_TEXT = re.compile(
    r"(?:não sou um robô|nao sou um robo|verifique que você é humano|"
    r"verifique que voce e humano|resolva o captcha)",
    re.IGNORECASE,
)

_CPF_MASK = "***.***.***-**"
_CNPJ_MASK = "**.***.***/****-**"
_SAFE_OBSERVATION = (
    "Consulta pública do TJPE. CPF e CNPJ foram mascarados. Processos sob sigilo não "
    "aparecem, e as movimentações correspondem à página exibida pelo tribunal."
)


def _compact(value: str) -> str:
    return " ".join(value.split())


def _npu_digits(numero: str) -> str:
    return re.sub(r"[^0-9]", "", numero)


def _format_npu(digits: str) -> str:
    return (
        f"{digits[0:7]}-{digits[7:9]}.{digits[9:13]}.{digits[13]}.{digits[14:16]}.{digits[16:20]}"
    )


def validate_npu_tjpe(numero: str) -> bool:
    """Valida formato, ramo, tribunal e dígito verificador CNJ de um NPU do TJPE."""
    candidate = numero.strip()
    if not candidate or _ALLOWED_NPU_INPUT.fullmatch(candidate) is None:
        return False

    digits = _npu_digits(candidate)
    if len(digits) != 20 or digits[13] != "8" or digits[14:16] != "17":
        return False

    reordered = (
        digits[0:7] + digits[9:13] + digits[13] + digits[14:16] + digits[16:20] + digits[7:9]
    )
    return int(reordered) % 97 == 1


def normalize_npu_tjpe(numero: str) -> str:
    """Valida e devolve o NPU do TJPE no formato oficial."""
    if not validate_npu_tjpe(numero):
        raise ValidacaoError("informe um NPU válido do TJPE no formato NNNNNNN-DD.AAAA.8.17.OOOO")
    return _format_npu(_npu_digits(numero))


def parse_result_count(text: str) -> int:
    """Converte o contador JSF; o TJPE omite o algarismo quando o total é zero."""
    normalized = _compact(text)
    match = _RESULT_COUNT.fullmatch(normalized)
    if match is None:
        raise ServicoIndisponivelError(
            f"contador inesperado na consulta pública do TJPE: {normalized!r}"
        )
    value = match.group(1)
    return int(value) if value is not None else 0


def mask_cpf_cnpj(text: str) -> str:
    """Remove identificadores pessoais rotulados antes de devolver texto ao MCP."""
    masked = _CNPJ.sub(lambda match: f"{match.group('label')}{_CNPJ_MASK}", text)
    return _CPF.sub(lambda match: f"{match.group('label')}{_CPF_MASK}", masked)


def _lines(value: str) -> list[str]:
    return [_compact(line) for line in value.splitlines() if _compact(line)]


def _value_after_label(lines: list[str], label: str) -> str | None:
    expected = label.casefold()
    for index, line in enumerate(lines[:-1]):
        if line.casefold() == expected:
            return lines[index + 1]
    return None


def _parse_result_summary(
    summary_text: str, link_text: str, numero: str
) -> tuple[str | None, str | None, list[str]]:
    summary_lines = _lines(summary_text)
    normalized_link = _compact(link_text)
    classe = summary_lines[0] if summary_lines else None

    subject_marker = f"{numero} - "
    marker_index = normalized_link.find(subject_marker)
    assunto = (
        normalized_link[marker_index + len(subject_marker) :].strip() if marker_index >= 0 else None
    )

    parties_text = ""
    for index, line in enumerate(summary_lines):
        if line == normalized_link:
            parties_text = " ".join(summary_lines[index + 1 :])
            break
    if not parties_text and len(summary_lines) >= 3:
        parties_text = " ".join(summary_lines[2:])

    parties = [
        mask_cpf_cnpj(_compact(party))
        for party in re.split(r"\s+X\s+", parties_text)
        if _compact(party)
    ]
    return classe, assunto, parties


async def _captcha_required(page: Page) -> bool:
    widgets = page.locator(
        "iframe[src*='recaptcha'], iframe[src*='hcaptcha'], iframe[src*='turnstile'], "
        ".g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey]"
    )
    for index in range(await widgets.count()):
        if await widgets.nth(index).is_visible():
            return True

    body_text = await page.locator("body").inner_text()
    if _CAPTCHA_TEXT.search(body_text):
        return True

    try:
        source = await page.evaluate(
            """() => typeof executarReCaptcha === "function"
                ? executarReCaptcha.toString()
                : ""
            """
        )
    except PlaywrightError:
        return False
    return isinstance(source, str) and re.search(r"if\s*\(\s*true\s*\)", source) is not None


async def _raise_for_blocker(page: Page) -> None:
    if await _captcha_required(page):
        raise ServicoIndisponivelError(
            "o TJPE solicitou CAPTCHA; é necessária interação humana e o MCP não o contorna"
        )
    visible_text = await page.locator("body").inner_text()
    if _BLOCKED_PAGE.search(visible_text):
        raise ServicoIndisponivelError(
            "a consulta pública do TJPE bloqueou esta sessão ou endereço de rede"
        )


class PjePublicClient:
    def __init__(self, browser: BrowserManager, config: Settings) -> None:
        self.browser = browser
        self.config = config

    async def consultar(self, numero: str, grau: Grau) -> ProcessoPublico:
        formatted = normalize_npu_tjpe(numero)
        digits = _npu_digits(formatted)
        base_url = self.config.urls.pje_base(grau.value)
        source_url = f"{base_url}/ConsultaPublica/listView.seam"

        async with self.browser.page() as page:
            try:
                response = await page.goto(source_url, wait_until="domcontentloaded")
                if response is not None and response.status != 200:
                    # A camada de proteção do TJPE responde 202 com corpo vazio quando
                    # limita o tráfego. Não é indisponibilidade do tribunal, e dizer o
                    # número cru leva o usuário a concluir que o PJe está fora do ar.
                    if response.status == _HTTP_LIMITADO:
                        raise ServicoIndisponivelError(
                            "a consulta pública do TJPE limitou o acesso desta máquina no "
                            "momento (HTTP 202 sem conteúdo); aguarde alguns minutos antes "
                            "de repetir a consulta"
                        )
                    raise ServicoIndisponivelError(
                        f"a consulta pública do TJPE respondeu HTTP {response.status}"
                    )
                await page.get_by_label("Processo", exact=True).wait_for(state="visible")
                await _raise_for_blocker(page)
                await self._submit_search(page, digits)
                return await self._read_search_result(
                    page,
                    formatted=formatted,
                    grau=grau,
                    base_url=base_url,
                    source_url=source_url,
                )
            except PlaywrightTimeoutError as exc:
                if await _captcha_required(page):
                    raise ServicoIndisponivelError(
                        "o TJPE solicitou CAPTCHA; é necessária interação humana e o MCP não o "
                        "contorna"
                    ) from exc
                raise ServicoIndisponivelError(
                    "a consulta pública do TJPE não respondeu no tempo esperado"
                ) from exc

    async def _type_exact_npu(self, page: Page, digits: str) -> None:
        """Preenche o campo mascarado e confere dígito a dígito o que ficou nele.

        O campo fica visível antes de a máscara JS ser montada, então digitar
        imediatamente após o domcontentloaded pode perder caracteres. A conferência
        detecta isso; uma segunda tentativa, já com a página carregada, resolve sem
        afrouxar a garantia de que o campo contém exatamente o NPU pedido.
        """
        process_field = page.get_by_label("Processo", exact=True)
        for tentativa in range(_TENTATIVAS_MASCARA):
            if tentativa:
                await page.wait_for_load_state("load")
            await process_field.press("ControlOrMeta+A")
            await process_field.press("Backspace")
            await process_field.press_sequentially(digits, delay=20)
            if _npu_digits(await process_field.input_value()) == digits:
                return
        raise ServicoIndisponivelError(
            "a máscara de processo do TJPE não aceitou o NPU informado"
        )

    async def _submit_search(self, page: Page, digits: str) -> None:
        await self._type_exact_npu(page, digits)

        button = page.get_by_role("button", name="Pesquisar", exact=True)
        try:
            async with page.expect_response(
                lambda item: (
                    item.request.method == "POST" and "/ConsultaPublica/listView.seam" in item.url
                ),
                timeout=self.config.timeout_ms,
            ) as pending:
                await button.click()
            response = await pending.value
            await response.body()
        except PlaywrightTimeoutError:
            if await _captcha_required(page):
                raise ServicoIndisponivelError(
                    "o TJPE solicitou CAPTCHA; é necessária interação humana e o MCP não o contorna"
                ) from None
            raise

        if response.status != 200:
            raise ServicoIndisponivelError(
                f"a pesquisa pública do TJPE respondeu HTTP {response.status}"
            )
        await expect(page.locator('[id="_viewRoot:status.start"]')).to_be_hidden(
            timeout=self.config.timeout_ms
        )
        await expect(page.locator("#modalStatusContainer")).to_be_hidden(
            timeout=self.config.timeout_ms
        )
        await expect(page.locator('[id="fPP:processosTable"]')).to_be_visible(
            timeout=self.config.timeout_ms
        )
        await expect(page.locator('[id="fPP:processosTable"] tfoot .text-muted')).to_be_attached(
            timeout=self.config.timeout_ms
        )
        await _raise_for_blocker(page)

    async def _read_search_result(
        self,
        page: Page,
        *,
        formatted: str,
        grau: Grau,
        base_url: str,
        source_url: str,
    ) -> ProcessoPublico:
        table = page.locator('[id="fPP:processosTable"]')
        footer_text = await table.locator("tfoot .text-muted").inner_text()
        declared_count = parse_result_count(footer_text)
        rows = table.locator("tbody tr")
        rendered_count = await rows.count()

        if declared_count == 0:
            if rendered_count:
                raise ServicoIndisponivelError(
                    "a consulta pública do TJPE apresentou um resultado inconsistente"
                )
            return ProcessoPublico(
                numero=formatted,
                grau=grau,
                url_fonte=source_url,
                observacao=(
                    "Nenhum resultado público. Isso pode indicar número inexistente, informação "
                    "incorreta ou processo sob sigilo."
                ),
            )

        matching_rows = rows.filter(has_text=formatted)
        if await matching_rows.count() == 0:
            raise ServicoIndisponivelError(
                "o TJPE informou resultados, mas não devolveu o NPU consultado"
            )
        row = matching_rows.first
        cells = row.locator("td")
        if await cells.count() < 3:
            raise ServicoIndisponivelError("a estrutura dos resultados públicos do TJPE mudou")

        summary_cell = cells.nth(1)
        process_link = summary_cell.locator("a").first
        classe, assunto, fallback_parties = _parse_result_summary(
            await summary_cell.inner_text(), await process_link.inner_text(), formatted
        )

        detail_link = cells.nth(0).get_by_role(
            "link", name=re.compile(r"ver detalhes do processo", re.IGNORECASE)
        )
        onclick = await detail_link.get_attribute("onclick")
        detail_url = self._detail_url(onclick, grau, base_url)

        response = await page.goto(detail_url, wait_until="domcontentloaded")
        if response is not None and response.status != 200:
            raise ServicoIndisponivelError(
                f"o detalhe público do TJPE respondeu HTTP {response.status}"
            )
        await page.get_by_text("Dados do Processo", exact=True).first.wait_for(state="visible")
        await _raise_for_blocker(page)

        return await self._read_detail(
            page,
            formatted=formatted,
            grau=grau,
            classe_fallback=classe,
            assunto=assunto,
            parties_fallback=fallback_parties,
            source_url=source_url,
        )

    @staticmethod
    def _detail_url(onclick: str | None, grau: Grau, base_url: str) -> str:
        match = _DETAIL_PATH.search(onclick or "")
        if match is None:
            raise ServicoIndisponivelError(
                "o TJPE não forneceu o endereço público dos detalhes do processo"
            )

        detail_url = urljoin(f"{base_url}/", match.group("path"))
        parsed = urlparse(detail_url)
        expected_prefix = f"/{grau.value}/ConsultaPublica/DetalheProcessoConsultaPublica/"
        if (
            parsed.scheme != "https"
            or parsed.hostname != "pje.cloud.tjpe.jus.br"
            or not parsed.path.startswith(expected_prefix)
        ):
            raise ServicoIndisponivelError(
                "o TJPE devolveu um endereço de detalhes fora do domínio esperado"
            )
        return detail_url

    async def _read_detail(
        self,
        page: Page,
        *,
        formatted: str,
        grau: Grau,
        classe_fallback: str | None,
        assunto: str | None,
        parties_fallback: list[str],
        source_url: str,
    ) -> ProcessoPublico:
        body_text = mask_cpf_cnpj(await page.locator("body").inner_text())
        body_lines = _lines(body_text)
        classe = _value_after_label(body_lines, "Classe Judicial") or classe_fallback
        orgao = _value_after_label(body_lines, "Órgão Julgador")
        valor_causa = _value_after_label(body_lines, "Valor da Causa")

        parties: list[str] = []
        for selector in (
            '[id$=":processoPartesPoloAtivoResumidoList"]',
            '[id$=":processoPartesPoloPassivoResumidoList"]',
        ):
            table = page.locator(selector)
            if await table.count() == 0:
                continue
            for row in await table.locator("tbody tr").all():
                cells = await row.locator("td").all_inner_texts()
                if not cells:
                    continue
                participant = mask_cpf_cnpj(_compact(cells[0]))
                if participant and participant not in parties:
                    parties.append(participant)
        if not parties:
            parties = parties_fallback

        movements: list[MovimentoPublico] = []
        movement_table = page.locator('[id$=":processoEvento"]')
        if await movement_table.count():
            for row in await movement_table.locator("tbody tr").all():
                cells = await row.locator("td").all_inner_texts()
                if not cells:
                    continue
                movement_text = mask_cpf_cnpj(_compact(cells[0]))
                if not movement_text:
                    continue
                match = _MOVEMENT.match(movement_text)
                movements.append(
                    MovimentoPublico(
                        data=match.group("data") if match else None,
                        descricao=(match.group("descricao").strip() if match else movement_text),
                    )
                )

        return ProcessoPublico(
            numero=formatted,
            grau=grau,
            classe=mask_cpf_cnpj(classe) if classe else None,
            assunto=mask_cpf_cnpj(assunto) if assunto else None,
            orgao_julgador=mask_cpf_cnpj(orgao) if orgao else None,
            valor_causa=mask_cpf_cnpj(valor_causa) if valor_causa else None,
            partes=parties,
            movimentos=movements,
            url_fonte=source_url,
            observacao=_SAFE_OBSERVATION,
        )
