from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from platformdirs import user_data_path, user_downloads_path


class ModoAdaptativo(StrEnum):
    OBSERVE = "observe"
    SHADOW = "shadow"
    ACTIVE = "active"


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "nao", "não", "off"}


def _adaptive_mode() -> ModoAdaptativo:
    raw = os.getenv("PJE_TJPE_ADAPTIVE_MODE", ModoAdaptativo.OBSERVE.value).strip().lower()
    try:
        return ModoAdaptativo(raw)
    except ValueError:
        choices = ", ".join(mode.value for mode in ModoAdaptativo)
        raise ValueError(f"PJE_TJPE_ADAPTIVE_MODE deve ser um de: {choices}") from None


def _positive_env_int(name: str, default: int, *, maximum: int | None = None) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} deve ser positivo")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} deve ser no máximo {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class TribunalUrls:
    pje_1g: str = "https://pje.cloud.tjpe.jus.br/1g"
    pje_2g: str = "https://pje.cloud.tjpe.jus.br/2g"
    sicajud: str = "https://www.tjpe.jus.br/custasjudiciais"
    sicajud_simulacao: str = (
        "https://www.tjpe.jus.br/custasjudiciais/xhtml/simulacao/simularCustas.xhtml"
    )
    # Central de Atendimento Processual do 1º Grau: o portal só aponta para o chat
    # Mibew, e é a instância do Mibew que decide se há operador ou não.
    cap1g_portal: str = (
        "https://portal.tjpe.jus.br/web/central-de-atendimento-processual-do-1%C2%BA-grau"
    )
    cap1g_chat: str = (
        "https://www.tjpe.jus.br/mibew/index.php/chat?locale=pt-br&style=default&group=1"
    )

    def pje_base(self, grau: str) -> str:
        if grau == "1g":
            return self.pje_1g
        if grau == "2g":
            return self.pje_2g
        raise ValueError("grau deve ser '1g' ou '2g'")


@dataclass(frozen=True, slots=True)
class Settings:
    headless: bool = field(default_factory=lambda: _env_bool("PJE_TJPE_HEADLESS", True))
    auth_headless: bool = field(default_factory=lambda: _env_bool("PJE_TJPE_AUTH_HEADLESS", False))
    # O chat da CAP1G é uma conversa com um servidor do tribunal: a janela fica visível
    # para o advogado acompanhar e intervir; o padrão só muda se ele pedir.
    chat_headless: bool = field(default_factory=lambda: _env_bool("PJE_TJPE_CHAT_HEADLESS", False))
    timeout_ms: int = field(default_factory=lambda: int(os.getenv("PJE_TJPE_TIMEOUT_MS", "30000")))
    max_document_bytes: int = field(
        default_factory=lambda: int(os.getenv("PJE_TJPE_MAX_DOCUMENT_BYTES", str(3 * 1024 * 1024)))
    )
    max_pjedocs_bytes: int = field(
        default_factory=lambda: int(os.getenv("PJE_TJPE_MAX_PJEDOCS_BYTES", str(512 * 1024 * 1024)))
    )
    adaptive_mode: ModoAdaptativo = field(default_factory=_adaptive_mode)
    adaptive_max_events: int = field(
        default_factory=lambda: _positive_env_int(
            "PJE_TJPE_ADAPTIVE_MAX_EVENTS", 200, maximum=10_000
        )
    )
    data_dir: Path = field(
        default_factory=lambda: Path(
            os.getenv(
                "PJE_TJPE_DATA_DIR",
                str(user_data_path("mcp-pje-tjpe", appauthor=False)),
            )
        )
    )
    downloads_dir: Path = field(
        default_factory=lambda: Path(
            os.getenv(
                "PJE_TJPE_DOWNLOAD_DIR",
                str(user_downloads_path() / "PJe-TJPE"),
            )
        )
    )
    # Chave pública do DataJud. O CNJ publica uma chave fixa e avisa que pode trocá-la
    # a qualquer momento; por isso ela é sobrescrevível por ambiente em vez de ser
    # constante de código. Fonte: datajud-wiki.cnj.jus.br/api-publica/acesso
    datajud_api_key: str = field(
        default_factory=lambda: os.getenv(
            "PJE_TJPE_DATAJUD_API_KEY",
            "cDZHYzlZa0JadVREZDJCendQbXY6SkJlTzNjLV9TRENyQk1RdnFKZGRQdw==",
        )
    )
    urls: TribunalUrls = field(default_factory=TribunalUrls)

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
