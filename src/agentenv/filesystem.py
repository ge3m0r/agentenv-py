from __future__ import annotations

import base64
import binascii
import shutil
import stat
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .orchestrator import Orchestrator


class FilesystemError(RuntimeError):
    """Base error for sandbox filesystem operations."""


class FilesystemPathError(FilesystemError):
    """Raised when a path is invalid or escapes the sandbox root."""


class FilesystemNotFoundError(FilesystemError):
    """Raised when a requested sandbox path does not exist."""


class FilesystemConflictError(FilesystemError):
    """Raised when an operation conflicts with an existing filesystem entry."""


class FilesystemUnavailableError(FilesystemError):
    """Raised when the selected backend has no filesystem data plane."""


@dataclass(frozen=True)
class FilesystemEntry:
    name: str
    path: str
    type: str
    size: int
    mode: int
    permissions: str
    modified_time: str
    symlink_target: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class WorkspaceFilesystem:
    """Filesystem data plane rooted inside a Local or Docker workspace."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def read(self, path: str, encoding: str = "utf-8") -> dict[str, Any]:
        target = self._existing(path)
        if not target.is_file():
            raise FilesystemConflictError(f"path is not a file: {path}")
        data = target.read_bytes()
        if encoding == "base64":
            content = base64.b64encode(data).decode("ascii")
        elif encoding == "utf-8":
            try:
                content = data.decode("utf-8")
            except UnicodeDecodeError as error:
                raise FilesystemConflictError(
                    f"file is not valid UTF-8; read it with encoding=base64: {path}"
                ) from error
        else:
            raise FilesystemPathError("encoding must be utf-8 or base64")
        return {
            "path": self._virtual_path(target),
            "encoding": encoding,
            "data": content,
            "size": len(data),
        }

    def write(
        self, path: str, data: str, encoding: str = "utf-8"
    ) -> FilesystemEntry:
        target = self._resolve(path)
        if target.exists() and target.is_dir():
            raise FilesystemConflictError(f"path is a directory: {path}")
        if not isinstance(data, str):
            raise FilesystemPathError("data must be a string")
        if encoding == "utf-8":
            content = data.encode("utf-8")
        elif encoding == "base64":
            try:
                content = base64.b64decode(data, validate=True)
            except (ValueError, binascii.Error) as error:
                raise FilesystemPathError("data is not valid base64") from error
        else:
            raise FilesystemPathError("encoding must be utf-8 or base64")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        except (FileExistsError, IsADirectoryError, NotADirectoryError) as error:
            raise FilesystemConflictError(
                f"path conflicts with an existing entry: {path}"
            ) from error
        return self.stat(path)

    def stat(self, path: str) -> FilesystemEntry:
        target = self._existing(path)
        info = target.lstat()
        if stat.S_ISDIR(info.st_mode):
            entry_type = "directory"
        elif stat.S_ISREG(info.st_mode):
            entry_type = "file"
        elif stat.S_ISLNK(info.st_mode):
            entry_type = "symlink"
        else:
            entry_type = "other"
        symlink_target = str(target.readlink()) if target.is_symlink() else None
        return FilesystemEntry(
            name=target.name or "/",
            path=self._virtual_path(target),
            type=entry_type,
            size=info.st_size,
            mode=stat.S_IMODE(info.st_mode),
            permissions=stat.filemode(info.st_mode),
            modified_time=datetime.fromtimestamp(
                info.st_mtime, timezone.utc
            ).isoformat(),
            symlink_target=symlink_target,
        )

    def list(self, path: str = "/") -> list[FilesystemEntry]:
        target = self._existing(path)
        if not target.is_dir():
            raise FilesystemConflictError(f"path is not a directory: {path}")
        return [
            self.stat(self._virtual_path(child))
            for child in sorted(target.iterdir(), key=lambda item: item.name)
        ]

    def make_dir(
        self, path: str, *, parents: bool = True, exist_ok: bool = True
    ) -> FilesystemEntry:
        target = self._resolve(path)
        if target.exists() and not target.is_dir():
            raise FilesystemConflictError(f"path already exists as a file: {path}")
        try:
            target.mkdir(parents=parents, exist_ok=exist_ok)
        except FileExistsError as error:
            raise FilesystemConflictError(f"path already exists: {path}") from error
        except FileNotFoundError as error:
            raise FilesystemNotFoundError(
                f"parent directory does not exist: {path}"
            ) from error
        except (NotADirectoryError, OSError) as error:
            raise FilesystemConflictError(
                f"path conflicts with an existing entry: {path}"
            ) from error
        return self.stat(path)

    def move(self, source: str, destination: str) -> FilesystemEntry:
        source_path = self._existing(source)
        destination_path = self._resolve(destination)
        if destination_path.exists() or destination_path.is_symlink():
            raise FilesystemConflictError(
                f"destination already exists: {destination}"
            )
        if source_path == self.root:
            raise FilesystemPathError("cannot move the sandbox root")
        try:
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source_path), str(destination_path))
        except (FileExistsError, NotADirectoryError, OSError) as error:
            raise FilesystemConflictError(
                f"cannot move {source} to {destination}: {error}"
            ) from error
        return self.stat(destination)

    def remove(self, path: str, *, recursive: bool = False) -> None:
        target = self._existing(path)
        if target == self.root:
            raise FilesystemPathError("cannot remove the sandbox root")
        if target.is_symlink():
            target.unlink()
        elif target.is_dir():
            try:
                if recursive:
                    shutil.rmtree(target)
                else:
                    target.rmdir()
            except OSError as error:
                raise FilesystemConflictError(
                    f"directory is not empty; use recursive=true: {path}"
                ) from error
        else:
            target.unlink()

    def _existing(self, path: str) -> Path:
        target = self._resolve(path)
        if not target.exists() and not target.is_symlink():
            raise FilesystemNotFoundError(f"path not found: {path}")
        return target

    def _resolve(self, path: str) -> Path:
        if not isinstance(path, str) or not path or "\x00" in path:
            raise FilesystemPathError("path must be a non-empty string")
        virtual = PurePosixPath(path)
        parts = virtual.parts[1:] if virtual.is_absolute() else virtual.parts
        if any(part == ".." for part in parts):
            raise FilesystemPathError(f"path cannot leave the sandbox: {path}")
        candidate = self.root.joinpath(*parts)
        resolved = candidate.resolve(strict=False)
        if resolved != self.root and self.root not in resolved.parents:
            raise FilesystemPathError(f"path cannot leave the sandbox: {path}")
        return candidate

    def _virtual_path(self, target: Path) -> str:
        try:
            relative = target.relative_to(self.root)
        except ValueError as error:
            raise FilesystemPathError("path cannot leave the sandbox") from error
        return "/" if not relative.parts else "/" + relative.as_posix()


class FilesystemService:
    """Lifecycle-aware service used by APIs and future controller transports."""

    def __init__(self, orchestrator: "Orchestrator"):
        self.orchestrator = orchestrator

    def for_sandbox(self, sandbox_id: str) -> WorkspaceFilesystem:
        sandbox = self.orchestrator.get_sandbox(sandbox_id)
        self.orchestrator.ensure_backend(sandbox)
        sandbox = self.orchestrator.prepare_activity(sandbox, "filesystem")
        return self.orchestrator.backend.filesystem(sandbox)

    def read(
        self, sandbox_id: str, path: str, encoding: str = "utf-8"
    ) -> dict[str, Any]:
        return self.for_sandbox(sandbox_id).read(path, encoding)

    def write(
        self,
        sandbox_id: str,
        path: str,
        data: str,
        encoding: str = "utf-8",
    ) -> FilesystemEntry:
        return self.for_sandbox(sandbox_id).write(path, data, encoding)

    def stat(self, sandbox_id: str, path: str) -> FilesystemEntry:
        return self.for_sandbox(sandbox_id).stat(path)

    def list(
        self, sandbox_id: str, path: str = "/"
    ) -> list[FilesystemEntry]:
        return self.for_sandbox(sandbox_id).list(path)

    def make_dir(
        self,
        sandbox_id: str,
        path: str,
        *,
        parents: bool = True,
        exist_ok: bool = True,
    ) -> FilesystemEntry:
        return self.for_sandbox(sandbox_id).make_dir(
            path, parents=parents, exist_ok=exist_ok
        )

    def move(
        self, sandbox_id: str, source: str, destination: str
    ) -> FilesystemEntry:
        return self.for_sandbox(sandbox_id).move(source, destination)

    def remove(
        self, sandbox_id: str, path: str, *, recursive: bool = False
    ) -> None:
        self.for_sandbox(sandbox_id).remove(path, recursive=recursive)
