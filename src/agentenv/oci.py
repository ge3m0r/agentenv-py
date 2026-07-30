from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from typing import Callable


_NAME = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$")
_TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_DIGEST = re.compile(r"^[A-Za-z][A-Za-z0-9]*:[0-9a-fA-F]{32,}$")


class OCIReferenceError(ValueError):
    pass


@dataclass(frozen=True)
class OCIReference:
    registry: str
    repository: str
    tag: str | None = None
    digest: str | None = None

    @classmethod
    def parse(cls, value: str) -> "OCIReference":
        value = value.strip()
        if not value or any(character.isspace() for character in value):
            raise OCIReferenceError("OCI image reference cannot be empty or contain spaces")

        name_and_tag, separator, digest = value.partition("@")
        if separator and (not digest or not _DIGEST.fullmatch(digest)):
            raise OCIReferenceError(f"invalid OCI digest: {digest}")
        if "@" in digest:
            raise OCIReferenceError("OCI image reference contains multiple digests")

        last_slash = name_and_tag.rfind("/")
        last_colon = name_and_tag.rfind(":")
        tag = None
        name = name_and_tag
        if last_colon > last_slash:
            name, tag = name_and_tag[:last_colon], name_and_tag[last_colon + 1 :]
            if not _TAG.fullmatch(tag):
                raise OCIReferenceError(f"invalid OCI tag: {tag}")

        components = name.split("/")
        first = components[0]
        has_registry = "." in first or ":" in first or first == "localhost"
        if has_registry:
            registry = first
            repository = "/".join(components[1:])
        else:
            registry = "docker.io"
            repository = name
        if registry == "docker.io" and "/" not in repository:
            repository = f"library/{repository}"
        if not repository or not _NAME.fullmatch(repository):
            raise OCIReferenceError(f"invalid OCI repository: {repository}")

        return cls(
            registry=registry,
            repository=repository,
            tag=tag if tag else (None if digest else "latest"),
            digest=digest or None,
        )

    @property
    def canonical(self) -> str:
        value = f"{self.registry}/{self.repository}"
        if self.tag:
            value += f":{self.tag}"
        if self.digest:
            value += f"@{self.digest}"
        return value


@dataclass
class ResolvedImage:
    reference: OCIReference
    image_id: str
    digest: str | None
    env: dict[str, str] = field(default_factory=dict)
    workdir: str = "/workspace"


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class DockerImageResolver:
    def __init__(
        self,
        docker_binary: str = "docker",
        pull_missing: bool = True,
        runner: CommandRunner = subprocess.run,
    ):
        self.docker_binary = docker_binary
        self.pull_missing = pull_missing
        self.runner = runner

    def resolve(self, value: str) -> ResolvedImage:
        reference = OCIReference.parse(value)
        inspected = self._inspect(reference.canonical)
        if inspected is None and self.pull_missing:
            self._run(["pull", reference.canonical])
            inspected = self._inspect(reference.canonical)
        if inspected is None:
            raise OCIReferenceError(
                f"OCI image is not available locally: {reference.canonical}"
            )
        config = inspected.get("Config") or {}
        environment = {}
        for item in config.get("Env") or []:
            key, separator, value = item.partition("=")
            if separator:
                environment[key] = value
        repo_digests = inspected.get("RepoDigests") or []
        digest = repo_digests[0].partition("@")[2] if repo_digests else None
        return ResolvedImage(
            reference=reference,
            image_id=inspected.get("Id", ""),
            digest=digest or reference.digest,
            env=environment,
            workdir=config.get("WorkingDir") or "/workspace",
        )

    def _inspect(self, image: str) -> dict | None:
        result = self.runner(
            [self.docker_binary, "image", "inspect", image],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        values = json.loads(result.stdout)
        return values[0] if values else None

    def _run(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        result = self.runner(
            [self.docker_binary, *arguments],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise OCIReferenceError(result.stderr.strip() or "docker command failed")
        return result
