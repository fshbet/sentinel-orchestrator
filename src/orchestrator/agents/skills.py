"""Skills.

A skill is reusable knowledge or procedure that an agent can be given: how to
approach a kind of problem, what to check, what the house conventions are (spec
section 13). It is content, not code — which is the one place Markdown belongs
in an executable system (spec section 105).

Skills are:

* **modular** — one file, one skill;
* **discoverable** — loaded from directories, listed through the CLI and API;
* **versionable** — every version is retained and executions pin what they used;
* **composable** — a skill may require others, resolved transitively;
* **domain-independent at the core** — the platform ships none.

They carry no executable content. A skill cannot grant a permission or call a
tool; it can only tell an agent how to think about the work it was already
authorised to do.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..core.domain.serde import to_jsonable
from ..errors import ConfigurationError, NotFound

# Front matter delimited the usual way, so a skill file reads as a document.
_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)


@dataclass(frozen=True)
class Skill:
    """Reusable knowledge, addressable by id and version."""

    id: str
    version: str = "1.0.0"
    description: str = ""
    content: str = ""
    tags: tuple[str, ...] = ()
    # Other skills this one builds on, resolved transitively.
    requires: tuple[str, ...] = ()
    # Capability ids this skill is relevant to, for automatic attachment.
    applies_to: tuple[str, ...] = ()
    source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)

    def render(self) -> str:
        """The form an agent sees."""
        heading = f"## {self.id}" + (f" — {self.description}" if self.description else "")
        return f"{heading}\n\n{self.content.strip()}"


def _parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    match = _FRONT_MATTER.match(text)
    if match is None:
        return {}, text
    raw, body = match.group(1), match.group(2)
    try:
        import yaml

        metadata = yaml.safe_load(raw) or {}
    except ImportError:
        # Without PyYAML, accept the simple ``key: value`` subset that covers
        # every field a skill actually needs.
        metadata = {}
        for line in raw.splitlines():
            if ":" not in line or line.strip().startswith("#"):
                continue
            key, _, value = line.partition(":")
            value = value.strip()
            if value.startswith("[") and value.endswith("]"):
                metadata[key.strip()] = [
                    item.strip().strip("\"'")
                    for item in value[1:-1].split(",")
                    if item.strip()
                ]
            else:
                metadata[key.strip()] = value.strip("\"'")
    if not isinstance(metadata, dict):
        raise ConfigurationError("skill front matter must be a mapping")
    return metadata, body


def skill_from_text(text: str, *, source: str | None = None, fallback_id: str = "") -> Skill:
    """Parse a skill document: front matter plus body."""
    metadata, body = _parse_front_matter(text)
    skill_id = str(metadata.get("id") or fallback_id).strip()
    if not skill_id:
        raise ConfigurationError(
            f"skill {source or '<inline>'} has no id, in front matter or filename"
        )
    return Skill(
        id=skill_id,
        version=str(metadata.get("version", "1.0.0")),
        description=str(metadata.get("description", "")),
        content=body.strip() or str(metadata.get("content", "")),
        tags=tuple(str(t) for t in metadata.get("tags", ()) or ()),
        requires=tuple(str(r) for r in metadata.get("requires", ()) or ()),
        applies_to=tuple(str(c) for c in metadata.get("applies_to", ()) or ()),
        source=source,
    )


def skill_from_dict(data: dict[str, Any]) -> Skill:
    if not data.get("id"):
        raise ConfigurationError("skill definition requires an id")
    return Skill(
        id=str(data["id"]),
        version=str(data.get("version", "1.0.0")),
        description=str(data.get("description", "")),
        content=str(data.get("content", "")),
        tags=tuple(str(t) for t in data.get("tags", ()) or ()),
        requires=tuple(str(r) for r in data.get("requires", ()) or ()),
        applies_to=tuple(str(c) for c in data.get("applies_to", ()) or ()),
        source=data.get("source"),
    )


class SkillRegistry:
    """Version-keyed store of skills, with transitive composition."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], Skill] = {}
        self._latest: dict[str, str] = {}

    # -- registration ------------------------------------------------------

    def register(self, skill: Skill) -> Skill:
        key = (skill.id, skill.version)
        existing = self._by_key.get(key)
        if existing is not None and existing != skill:
            raise ConfigurationError(
                f"skill {skill.id} version {skill.version} is already registered with"
                " different content; publish a new version instead"
            )
        self._by_key[key] = skill
        current = self._latest.get(skill.id)
        if current is None or _version_key(skill.version) >= _version_key(current):
            self._latest[skill.id] = skill.version
        return skill

    def register_many(self, skills: Iterable[Skill]) -> list[Skill]:
        return [self.register(s) for s in skills]

    def load_directory(self, directory: str | Path) -> list[Skill]:
        """Load ``*.md``, ``*.yaml``, and ``*.json`` skills from a directory."""
        path = Path(directory)
        if not path.is_dir():
            return []
        loaded: list[Skill] = []
        for file in sorted(path.rglob("*")):
            if file.suffix not in (".md", ".markdown", ".yaml", ".yml", ".json"):
                continue
            text = file.read_text(encoding="utf-8")
            if file.suffix in (".md", ".markdown"):
                loaded.append(
                    self.register(
                        skill_from_text(text, source=str(file), fallback_id=file.stem)
                    )
                )
                continue
            if file.suffix == ".json":
                document = json.loads(text)
            else:
                try:
                    import yaml
                except ImportError as exc:  # pragma: no cover - environment dependent
                    raise ConfigurationError(
                        f"reading {file} requires PyYAML; install"
                        " universal-orchestrator[yaml] or use JSON or Markdown"
                    ) from exc
                document = yaml.safe_load(text) or {}
            entries = document if isinstance(document, list) else [document]
            for entry in entries:
                entry.setdefault("source", str(file))
                entry.setdefault("id", file.stem)
                loaded.append(self.register(skill_from_dict(entry)))
        return loaded

    def load_all(self, directories: Iterable[str | Path]) -> list[Skill]:
        return [s for directory in directories for s in self.load_directory(directory)]

    # -- lookup ------------------------------------------------------------

    def get(self, skill_id: str, version: str | None = None) -> Skill:
        resolved = version or self._latest.get(skill_id)
        if resolved is None:
            raise NotFound(f"skill {skill_id} is not registered", id=skill_id)
        try:
            return self._by_key[(skill_id, resolved)]
        except KeyError as exc:
            raise NotFound(
                f"skill {skill_id} version {resolved} is not registered",
                id=skill_id,
                version=resolved,
            ) from exc

    def has(self, skill_id: str) -> bool:
        return skill_id in self._latest

    def list(self) -> list[Skill]:
        return sorted(
            (self.get(skill_id) for skill_id in self._latest), key=lambda s: s.id
        )

    def versions(self, skill_id: str) -> list[str]:
        return sorted(
            (v for (sid, v) in self._by_key if sid == skill_id), key=_version_key
        )

    def missing(self, required: Iterable[str]) -> list[str]:
        return sorted(s for s in required if not self.has(s))

    def snapshot_versions(self) -> dict[str, str]:
        """Skill id -> version, pinned onto an execution for reproducibility."""
        return dict(self._latest)

    def for_capabilities(self, capabilities: Iterable[str]) -> list[Skill]:
        """Skills that declare themselves relevant to these capabilities."""
        wanted = set(capabilities)
        return [
            skill
            for skill in self.list()
            if wanted & set(skill.applies_to)
        ]

    # -- composition -------------------------------------------------------

    def resolve(
        self, skill_ids: Sequence[str], *, pinned: dict[str, str] | None = None
    ) -> list[Skill]:
        """Resolve skills and everything they require, in dependency order.

        A missing skill is skipped rather than raising: an agent should still
        run when an optional piece of guidance is unavailable, and verification
        reports the gap separately.
        """
        pinned = pinned or {}
        resolved: list[Skill] = []
        emitted: set[str] = set()
        # Depth-first post-order, so a skill's requirements are emitted before
        # it. Colouring makes a cycle in ``requires`` terminate: the back edge
        # is dropped and both skills are still delivered, because unusable
        # guidance is worse than imperfectly ordered guidance.
        visiting: set[str] = set()

        def walk(skill_id: str, depth: int = 0) -> None:
            if skill_id in emitted or skill_id in visiting or depth > 32:
                return
            if not self.has(skill_id):
                return
            visiting.add(skill_id)
            skill = self.get(skill_id, pinned.get(skill_id))
            for requirement in skill.requires:
                walk(requirement, depth + 1)
            visiting.discard(skill_id)
            emitted.add(skill_id)
            resolved.append(skill)

        for skill_id in skill_ids:
            walk(skill_id)
        return resolved

    def render(
        self, skill_ids: Sequence[str], *, pinned: dict[str, str] | None = None
    ) -> str:
        """Compose skills into the text an agent is given."""
        skills = self.resolve(skill_ids, pinned=pinned)
        if not skills:
            return ""
        body = "\n\n".join(skill.render() for skill in skills)
        return f"# Applicable skills\n\n{body}"


def _version_key(version: str) -> tuple[int, ...]:
    parts = []
    for chunk in version.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts)
