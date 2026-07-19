from __future__ import annotations

import colorsys
import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from core.models import CurrentGuestInventory

TAG_RE = re.compile(r"^[a-z0-9_][a-z0-9_+.-]*$")
HEX_RE = re.compile(r"^[0-9a-f]{6}$")
TAG_NAME_ERROR = "Use lowercase letters, numbers, _, +, . or -; start with a letter, number or _."
TAG_COLOR_ERROR = "Color must be six hexadecimal characters."


class TagValidationError(ValueError):
    pass


@dataclass(frozen=True)
class RegisteredTag:
    name: str
    background: str = ""
    foreground: str = ""


@dataclass(frozen=True)
class TagChip:
    name: str
    background: str
    foreground: str


@dataclass
class TagSummary:
    name: str
    guests: list[CurrentGuestInventory] = field(default_factory=list)
    registered: bool = False
    background: str = ""
    foreground: str = ""

    @property
    def guest_count(self) -> int:
        return len(self.guests)

    @property
    def state(self) -> str:
        if self.registered and self.guests:
            return "Registered"
        if self.registered:
            return "Registered, unused"
        return "Ad-hoc"


def parse_tags(value_or_config) -> list[str]:
    if isinstance(value_or_config, dict):
        value_or_config = value_or_config.get("tags")
    if isinstance(value_or_config, (list, tuple, set)):
        parts = [str(item) for item in value_or_config]
    else:
        parts = re.split(r"[;,\s]+", str(value_or_config or "").strip())
    result, seen = [], set()
    for raw in parts:
        tag = raw.strip().lower()
        if tag and tag not in seen:
            seen.add(tag)
            result.append(tag)
    return result


def validate_tag(tag: str) -> str:
    tag = str(tag or "").strip().lower()
    if not tag or not TAG_RE.fullmatch(tag):
        raise TagValidationError(TAG_NAME_ERROR)
    return tag


def join_tags(tags: Iterable[str]) -> str:
    return ";".join(parse_tags(list(tags)))


def validate_color(color: str) -> str:
    value = str(color or "").strip().lower().removeprefix("#")
    if value and not HEX_RE.fullmatch(value):
        raise TagValidationError(TAG_COLOR_ERROR)
    return value


def readable_foreground(background: str) -> str:
    background = validate_color(background)
    if not background:
        return ""
    r, g, b = (int(background[index : index + 2], 16) / 255 for index in (0, 2, 4))
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "000000" if luminance > 0.55 else "ffffff"


def fallback_color(tag: str) -> tuple[str, str]:
    hue = int(hashlib.sha256(tag.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    r, g, b = colorsys.hls_to_rgb(hue, 0.42, 0.58)
    background = "".join(f"{round(channel * 255):02x}" for channel in (r, g, b))
    return background, readable_foreground(background)


def tag_chip(name: str, registered: dict[str, RegisteredTag]) -> TagChip:
    item = registered.get(name)
    colors = (item.background, item.foreground) if item and item.background else None
    background, foreground = colors or fallback_color(name)
    if not foreground:
        foreground = readable_foreground(background)
    return TagChip(name, background, foreground)


def _option_text(value) -> str:
    if isinstance(value, list):
        return ";".join(str(item) for item in value)
    return str(value or "")


def parse_tag_style(value) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(key): str(raw) for key, raw in value.items() if raw is not None}
    result: dict[str, str] = {}
    for part in _option_text(value).split(","):
        key, sep, raw = part.partition("=")
        if sep and key.strip():
            result[key.strip()] = raw.strip()
    return result


def serialize_tag_style(options: dict[str, str]) -> str:
    return ",".join(f"{key}={value}" for key, value in options.items() if value != "")


def parse_color_map(value: str) -> dict[str, tuple[str, str]]:
    result = {}
    for entry in str(value or "").split(";"):
        tag, sep, colors = entry.partition(":")
        if not sep:
            continue
        background, _, foreground = colors.partition(":")
        tag = tag.strip().lower()
        if tag and HEX_RE.fullmatch(background.lower()):
            result[tag] = (background.lower(), foreground.lower() if HEX_RE.fullmatch(foreground.lower()) else "")
    return result


def serialize_color_map(colors: dict[str, tuple[str, str]]) -> str:
    entries = []
    for tag in sorted(colors):
        background, foreground = colors[tag]
        entries.append(f"{tag}:{background}" + (f":{foreground}" if foreground else ""))
    return ";".join(entries)


def parse_registered_tags(cluster_options: dict) -> dict[str, RegisteredTag]:
    names = parse_tags(_option_text(cluster_options.get("registered-tags")))
    style = parse_tag_style(cluster_options.get("tag-style"))
    colors = parse_color_map(style.get("color-map", ""))
    return {name: RegisteredTag(name, *(colors.get(name) or ("", ""))) for name in names}


def inventory_rows(guests: Iterable[CurrentGuestInventory], registered: dict[str, RegisteredTag]) -> list[TagSummary]:
    summaries: dict[str, TagSummary] = {}
    for name, item in registered.items():
        summaries[name] = TagSummary(name=name, registered=True, background=item.background, foreground=item.foreground)
    for guest in guests:
        for name in parse_tags(guest.config):
            summary = summaries.setdefault(name, TagSummary(name=name))
            summary.guests.append(guest)
    for summary in summaries.values():
        chip = tag_chip(summary.name, registered)
        summary.background, summary.foreground = chip.background, chip.foreground
    return sorted(summaries.values(), key=lambda item: item.name)
