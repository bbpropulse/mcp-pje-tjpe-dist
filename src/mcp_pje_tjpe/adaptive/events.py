from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import stat
import sys
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from pydantic import ValidationError

from mcp_pje_tjpe.adaptive.models import ObservacaoNavegacao

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

_MAX_LINE_BYTES = 64 * 1024
_MAX_FILE_BYTES = 16 * 1024 * 1024
_MAX_EVENTS = 10_000


def observation_signature_payload(observation: ObservacaoNavegacao) -> bytes:
    return json.dumps(
        observation.model_dump(mode="json", exclude={"assinatura_pagina"}),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class ObservationStore:
    """JSONL local com lock entre processos, rotação e arquivos sem symlink."""

    def __init__(self, data_dir: Path, *, max_events: int = 200) -> None:
        if not 1 <= max_events <= _MAX_EVENTS:
            raise ValueError(f"max_events deve estar entre 1 e {_MAX_EVENTS}")
        self.root = data_dir / "adaptive"
        self.path = self.root / "events-v1.jsonl"
        self.lock_path = self.root / "events-v1.lock"
        self.key_path = self.root / "signature-key-v1"
        self.retention_path = self.root / "retention-v1"
        self.max_events = max_events
        self.last_error: str | None = None
        self._async_lock = asyncio.Lock()
        self._key: bytes | None = None
        self._key_lock = threading.Lock()

    def sign(self, value: bytes) -> str:
        with self._key_lock:
            if self._key is None:
                self._ensure_root()
                with self._locked():
                    self._key = self._load_or_create_key()
            key = self._key
        return hmac.new(key, value, hashlib.sha256).hexdigest()

    def seal(self, observation: ObservacaoNavegacao) -> ObservacaoNavegacao:
        validated = ObservacaoNavegacao.model_validate(observation.model_dump(mode="python"))
        payload = validated.model_dump(mode="python")
        payload["assinatura_pagina"] = self.sign(observation_signature_payload(validated))
        return ObservacaoNavegacao.model_validate(payload)

    async def append(self, observation: ObservacaoNavegacao) -> bool:
        async with self._async_lock:
            try:
                await asyncio.to_thread(self._append_sync, observation)
            except Exception:
                self.last_error = "não foi possível atualizar o JSONL sanitizado"
                return False
            self.last_error = None
            return True

    async def list(self, *, limit: int = 50) -> list[ObservacaoNavegacao]:
        if not 1 <= limit <= self.max_events:
            raise ValueError("limit fora do intervalo permitido")
        async with self._async_lock:
            try:
                result = await asyncio.to_thread(self._read_sync)
            except Exception:
                self.last_error = "não foi possível ler o JSONL sanitizado"
                return []
            self.last_error = None
            return list(reversed(result[-limit:]))

    async def count(self) -> int:
        async with self._async_lock:
            try:
                result = await asyncio.to_thread(self._read_sync)
            except Exception:
                self.last_error = "não foi possível contar o JSONL sanitizado"
                return 0
            self.last_error = None
            return len(result)

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.root.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or self._is_reparse_point(info)
            or not stat.S_ISDIR(info.st_mode)
        ):
            raise ValueError("o diretório adaptativo não pode ser link ou arquivo")
        self.root.chmod(0o700)

    @staticmethod
    def _is_reparse_point(info: os.stat_result) -> bool:
        attributes = getattr(info, "st_file_attributes", 0)
        flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return bool(attributes & flag)

    @staticmethod
    def _restrict_file_mode(fd: int) -> None:
        if sys.platform != "win32":
            os.fchmod(fd, 0o600)

    def _secure_open(self, path: Path, flags: int, mode: int = 0o600) -> int:
        try:
            existing = path.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            stat.S_ISLNK(existing.st_mode) or self._is_reparse_point(existing)
        ):
            raise ValueError("o armazenamento adaptativo não aceita link simbólico")
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        binary = getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags | nofollow | binary, mode)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            os.close(fd)
            raise ValueError("o armazenamento adaptativo deve usar arquivo regular exclusivo")
        self._restrict_file_mode(fd)
        return fd

    @staticmethod
    def _write_all(fd: int, payload: bytes) -> None:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("gravação local incompleta")
            view = view[written:]

    @staticmethod
    def _read_up_to(fd: int, limit: int) -> bytes:
        payload = bytearray()
        while len(payload) < limit:
            chunk = os.read(fd, limit - len(payload))
            if not chunk:
                break
            payload.extend(chunk)
        return bytes(payload)

    def _fsync_root(self) -> None:
        if sys.platform == "win32":
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.root, flags)
        try:
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                raise ValueError("a raiz adaptativa deixou de ser um diretório")
            os.fsync(fd)
        finally:
            os.close(fd)

    def _validate_retention_unlocked(self, *, create: bool) -> None:
        try:
            fd = self._secure_open(self.retention_path, os.O_RDONLY)
        except FileNotFoundError:
            if not create:
                return
            payload = f"{self.max_events}\n".encode("ascii")
            temporary = self.root / f".retention-v1-{secrets.token_hex(8)}.tmp"
            try:
                fd = self._secure_open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                try:
                    self._restrict_file_mode(fd)
                    self._write_all(fd, payload)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(temporary, self.retention_path)
                self._fsync_root()
            finally:
                temporary.unlink(missing_ok=True)
            return
        try:
            payload = self._read_up_to(fd, 32)
        finally:
            os.close(fd)
        if payload != f"{self.max_events}\n".encode("ascii"):
            raise ValueError("a retenção configurada diverge da política local compartilhada")

    @staticmethod
    def _acquire_lock(fd: int) -> None:
        if sys.platform == "win32":
            if os.fstat(fd).st_size == 0:
                ObservationStore._write_all(fd, b"\0")
                os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX)

    @staticmethod
    def _release_lock(fd: int) -> None:
        if sys.platform == "win32":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)

    @contextmanager
    def _locked(self) -> Generator[None, None, None]:
        fd = self._secure_open(self.lock_path, os.O_RDWR | os.O_CREAT)
        acquired = False
        try:
            self._restrict_file_mode(fd)
            self._acquire_lock(fd)
            acquired = True
            yield
        finally:
            try:
                if acquired:
                    self._release_lock(fd)
            finally:
                os.close(fd)

    def _load_or_create_key(self) -> bytes:
        try:
            fd = self._secure_open(self.key_path, os.O_RDONLY)
        except FileNotFoundError:
            key = secrets.token_bytes(32)
            temporary = self.root / f".signature-key-v1-{secrets.token_hex(8)}.tmp"
            try:
                fd = self._secure_open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                try:
                    self._restrict_file_mode(fd)
                    self._write_all(fd, key)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(temporary, self.key_path)
                self._fsync_root()
            finally:
                temporary.unlink(missing_ok=True)
            return key
        try:
            self._restrict_file_mode(fd)
            key = self._read_up_to(fd, 33)
        finally:
            os.close(fd)
        if len(key) != 32:
            raise ValueError("a chave local de assinatura possui tamanho inválido")
        return key

    def _read_sync(self) -> list[ObservacaoNavegacao]:
        try:
            self.root.lstat()
        except FileNotFoundError:
            return []
        self._ensure_root()
        with self._locked():
            self._validate_retention_unlocked(create=False)
            return self._read_unlocked()

    def _read_unlocked(self) -> list[ObservacaoNavegacao]:
        try:
            fd = self._secure_open(self.path, os.O_RDONLY)
        except FileNotFoundError:
            return []
        try:
            size = os.fstat(fd).st_size
            if size > _MAX_FILE_BYTES:
                raise ValueError("JSONL excede o teto local")
            payload = bytearray()
            while len(payload) <= _MAX_FILE_BYTES:
                chunk = os.read(fd, min(64 * 1024, _MAX_FILE_BYTES + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
        finally:
            os.close(fd)
        if len(payload) > _MAX_FILE_BYTES:
            raise ValueError("JSONL excede o teto local")
        result: list[ObservacaoNavegacao] = []
        for line in bytes(payload).splitlines():
            if not line:
                continue
            if len(line) > _MAX_LINE_BYTES:
                raise ValueError("evento JSONL excede o teto local")
            try:
                raw = json.loads(line)
                result.append(ObservacaoNavegacao.model_validate(raw))
            except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
                raise ValueError("JSONL contém evento inválido") from exc
        return result

    def _append_sync(self, observation: ObservacaoNavegacao) -> None:
        validated = ObservacaoNavegacao.model_validate(observation.model_dump(mode="python"))
        expected = self.sign(observation_signature_payload(validated))
        if not hmac.compare_digest(expected, validated.assinatura_pagina):
            raise ValueError("evento adaptativo não possui assinatura local válida")
        serialized = validated.model_dump_json().encode("utf-8")
        if len(serialized) > _MAX_LINE_BYTES or b"\n" in serialized or b"\r" in serialized:
            raise ValueError("evento adaptativo excede o formato permitido")
        self._ensure_root()
        with self._locked():
            self._validate_retention_unlocked(create=True)
            current = self._read_unlocked()
            current.append(validated)
            selected = current[-self.max_events :]
            payload = b"\n".join(item.model_dump_json().encode("utf-8") for item in selected)
            if payload:
                payload += b"\n"
            if len(payload) > _MAX_FILE_BYTES:
                raise ValueError("JSONL excede o teto local")
            temporary = self.root / f".events-v1-{secrets.token_hex(8)}.tmp"
            try:
                fd = self._secure_open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                try:
                    self._restrict_file_mode(fd)
                    self._write_all(fd, payload)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(temporary, self.path)
                self._fsync_root()
            finally:
                temporary.unlink(missing_ok=True)
