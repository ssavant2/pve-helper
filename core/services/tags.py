from __future__ import annotations

import colorsys
import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterable

from core.models import DerivedTagStyle, ProxmoxInventory, ScanRun


DERIVED_PREFIX = "pvehelper-vmtype-"
DERIVED_TAGS = (
    f"{DERIVED_PREFIX}vm",
    f"{DERIVED_PREFIX}ct",
    f"{DERIVED_PREFIX}template",
    f"{DERIVED_PREFIX}linked-clone",
)
TAG_RE = re.compile(r"^[a-z0-9_][a-z0-9_+.-]*$")
HEX_RE = re.compile(r"^[0-9a-f]{6}$")


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
    guests: list[ProxmoxInventory] = field(default_factory=list)
    conflicting_guests: list[ProxmoxInventory] = field(default_factory=list)
    registered: bool = False
    derived: bool = False
    background: str = ""
    foreground: str = ""
    namespace_conflict: bool = False

    @property
    def guest_count(self) -> int:
        return len(self.guests)

    @property
    def kind(self) -> str:
        return "derived" if self.derived else "user"

    @property
    def state(self) -> str:
        if self.derived:
            return "Derived"
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


def validate_tag(tag: str, *, allow_derived: bool = False) -> str:
    tag = str(tag or "").strip().lower()
    if not tag or not TAG_RE.fullmatch(tag):
        raise TagValidationError("Use lowercase letters, numbers, _, +, . or -; start with a letter, number or _.")
    if tag.startswith(DERIVED_PREFIX) and not allow_derived:
        raise TagValidationError(f"Tags beginning with {DERIVED_PREFIX} are reserved by pve-helper.")
    return tag


def join_tags(tags: Iterable[str]) -> str:
    return ";".join(parse_tags(list(tags)))


def validate_color(color: str) -> str:
    value = str(color or "").strip().lower().removeprefix("#")
    if value and not HEX_RE.fullmatch(value):
        raise TagValidationError("Color must be six hexadecimal characters.")
    return value


def readable_foreground(background: str) -> str:
    background = validate_color(background)
    if not background:
        return ""
    r, g, b = (int(background[index:index + 2], 16) / 255 for index in (0, 2, 4))
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "000000" if luminance > 0.55 else "ffffff"


def fallback_color(tag: str) -> tuple[str, str]:
    hue = int(hashlib.sha256(tag.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    r, g, b = colorsys.hls_to_rgb(hue, 0.42, 0.58)
    background = "".join(f"{round(channel * 255):02x}" for channel in (r, g, b))
    return background, readable_foreground(background)


def derived_color_map() -> dict[str, tuple[str, str]]:
    return {
        style.tag: (style.background, style.foreground)
        for style in DerivedTagStyle.objects.filter(tag__in=DERIVED_TAGS)
    }


def set_derived_tag_color(tag: str, color: str) -> TagChip:
    tag = validate_tag(tag, allow_derived=True)
    if tag not in DERIVED_TAGS:
        raise TagValidationError("Only known derived system tags can use an app-side color.")
    background = validate_color(color)
    if not background:
        raise TagValidationError("Choose a color.")
    foreground = readable_foreground(background)
    DerivedTagStyle.objects.update_or_create(
        tag=tag,
        defaults={"background": background, "foreground": foreground},
    )
    return TagChip(tag, background, foreground)


def tag_chip(name: str, registered: dict[str, RegisteredTag], derived_colors=None) -> TagChip:
    if name in DERIVED_TAGS:
        colors = (derived_colors if derived_colors is not None else derived_color_map()).get(name)
    else:
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
    return {
        name: RegisteredTag(name, *(colors.get(name) or ("", "")))
        for name in names
    }


def derived_tag_for(*, object_type: str, is_template: bool = False, is_linked_clone: bool = False) -> str:
    if is_template:
        return f"{DERIVED_PREFIX}template"
    if is_linked_clone:
        return f"{DERIVED_PREFIX}linked-clone"
    if object_type == ProxmoxInventory.ObjectType.CT:
        return f"{DERIVED_PREFIX}ct"
    if object_type == ProxmoxInventory.ObjectType.VM:
        return f"{DERIVED_PREFIX}vm"
    return ""


def inventory_rows(scan: ScanRun | None, registered: dict[str, RegisteredTag]) -> list[TagSummary]:
    derived_colors = derived_color_map()
    summaries: dict[str, TagSummary] = {
        name: TagSummary(name=name, derived=True)
        for name in DERIVED_TAGS
    }
    for name, item in registered.items():
        summaries[name] = TagSummary(name=name, registered=True, background=item.background, foreground=item.foreground)
    guests = [] if scan is None else list(
        ProxmoxInventory.objects.filter(
            scan_run=scan,
            object_type__in=[ProxmoxInventory.ObjectType.VM, ProxmoxInventory.ObjectType.CT],
        ).order_by("node", "vmid")
    )
    for guest in guests:
        for name in parse_tags(guest.config):
            summary = summaries.setdefault(name, TagSummary(name=name))
            if name.startswith(DERIVED_PREFIX):
                summary.namespace_conflict = True
                summary.conflicting_guests.append(guest)
            else:
                summary.guests.append(guest)
        if guest.derived_type:
            summary = summaries.setdefault(guest.derived_type, TagSummary(name=guest.derived_type, derived=True))
            summary.derived = True
            summary.guests.append(guest)
    for summary in summaries.values():
        chip = tag_chip(summary.name, registered, derived_colors)
        summary.background, summary.foreground = chip.background, chip.foreground
    return sorted(summaries.values(), key=lambda item: (not item.derived, item.name))
