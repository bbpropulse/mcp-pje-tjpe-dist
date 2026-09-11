from __future__ import annotations

import re
from dataclasses import dataclass

import keyring
from keyring.errors import PasswordDeleteError
from pydantic import SecretStr

from mcp_pje_tjpe.errors import CredenciaisAusentesError, ValidacaoError

SERVICE = "mcp-pje-tjpe"
CPF_KEY = "cpf"
PASSWORD_KEY = "password"
TOTP_KEY = "totp_seed"


@dataclass(frozen=True, slots=True)
class Credentials:
    cpf: str
    password: SecretStr
    totp_seed: SecretStr | None = None


def normalize_cpf(value: str) -> str:
    cpf = re.sub(r"\D", "", value)
    if len(cpf) != 11:
        raise ValidacaoError("CPF deve conter 11 dígitos")
    return cpf


class CredentialStore:
    def has_credentials(self) -> bool:
        return bool(keyring.get_password(SERVICE, CPF_KEY)) and bool(
            keyring.get_password(SERVICE, PASSWORD_KEY)
        )

    def save(self, cpf: str, password: str, totp_seed: str | None = None) -> None:
        clean_cpf = normalize_cpf(cpf)
        if not password:
            raise ValidacaoError("senha não pode ser vazia")
        keyring.set_password(SERVICE, CPF_KEY, clean_cpf)
        keyring.set_password(SERVICE, PASSWORD_KEY, password)
        if totp_seed:
            keyring.set_password(SERVICE, TOTP_KEY, totp_seed.replace(" ", ""))
        else:
            try:
                keyring.delete_password(SERVICE, TOTP_KEY)
            except PasswordDeleteError:
                pass

    def load(self) -> Credentials:
        cpf = keyring.get_password(SERVICE, CPF_KEY)
        password = keyring.get_password(SERVICE, PASSWORD_KEY)
        if not cpf or not password:
            raise CredenciaisAusentesError(
                "credenciais ausentes; execute 'pje-tjpe setup' no terminal"
            )
        totp_seed = keyring.get_password(SERVICE, TOTP_KEY)
        return Credentials(
            cpf=normalize_cpf(cpf),
            password=SecretStr(password),
            totp_seed=SecretStr(totp_seed) if totp_seed else None,
        )

    def clear(self) -> None:
        for key in (CPF_KEY, PASSWORD_KEY, TOTP_KEY):
            try:
                keyring.delete_password(SERVICE, key)
            except PasswordDeleteError:
                pass


credential_store = CredentialStore()
