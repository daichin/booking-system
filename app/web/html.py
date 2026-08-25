"""HTML construction helpers.

Markup is built from Python rather than a template language: with no
third-party dependency available, a builder that escapes by default is safer
than a hand-rolled template parser that escapes by convention.

The rule is simple -- everything passed as a child or attribute value is
escaped unless it is already :class:`Markup`.
"""

from __future__ import annotations

from html import escape
from typing import Any, Iterable

#: Tags that never have a closing tag.
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
     "source", "track", "wbr"}
)


class Markup(str):
    """A string that is already safe HTML and must not be escaped again."""

    __slots__ = ()

    def __add__(self, other: object) -> "Markup":
        return Markup(str(self) + esc(other))


def esc(value: Any) -> str:
    """Escape a value for HTML output. :class:`Markup` passes through."""
    if isinstance(value, Markup):
        return str(value)
    if value is None or value is False:
        return ""
    return escape(str(value), quote=True)


def raw(value: str) -> Markup:
    """Mark a string as already-safe HTML. Use sparingly and never on input."""
    return Markup(value)


def join(children: Iterable[Any]) -> Markup:
    return Markup("".join(esc(child) for child in children))


def _attrs(attributes: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in attributes.items():
        if value is None or value is False:
            continue
        # class_ -> class, data_id -> data-id, http_equiv -> http-equiv
        name = key.rstrip("_").replace("_", "-")
        if value is True:
            parts.append(f" {name}")
        else:
            parts.append(f' {name}="{escape(str(value), quote=True)}"')
    return "".join(parts)


def el(tag: str, *children: Any, **attributes: Any) -> Markup:
    """Build an element. ``el("p", "hi", class_="lead")``."""
    opening = f"<{tag}{_attrs(attributes)}>"
    if tag in _VOID:
        return Markup(opening)
    return Markup(f"{opening}{join(children)}</{tag}>")


def _maker(tag: str):
    def build(*children: Any, **attributes: Any) -> Markup:
        return el(tag, *children, **attributes)

    build.__name__ = tag
    return build


# Elements the pages actually use.
div = _maker("div")
span = _maker("span")
p = _maker("p")
a = _maker("a")
h1, h2, h3 = _maker("h1"), _maker("h2"), _maker("h3")
ul, ol, li = _maker("ul"), _maker("ol"), _maker("li")
table = _maker("table")
thead, tbody, tr, th, td = (
    _maker("thead"), _maker("tbody"), _maker("tr"), _maker("th"), _maker("td")
)
form = _maker("form")
label = _maker("label")
button = _maker("button")
select, option = _maker("select"), _maker("option")
textarea = _maker("textarea")
section, nav, header, main, footer = (
    _maker("section"), _maker("nav"), _maker("header"), _maker("main"),
    _maker("footer"),
)
small, strong, em = _maker("small"), _maker("strong"), _maker("em")
fieldset, legend = _maker("fieldset"), _maker("legend")
details, summary = _maker("details"), _maker("summary")
dialog = _maker("dialog")


def input_(**attributes: Any) -> Markup:
    return el("input", **attributes)


def hidden(name: str, value: Any) -> Markup:
    return input_(type="hidden", name=name, value=value)


def field(
    name: str,
    caption: str,
    *,
    type: str = "text",
    value: Any = "",
    required: bool = True,
    readonly: bool = False,
    help_text: str = "",
    **attributes: Any,
) -> Markup:
    """A labelled form control with optional help text."""
    control = input_(
        type=type,
        name=name,
        id=f"f-{name}",
        value=value if value is not None else "",
        required=required,
        readonly=readonly,
        **attributes,
    )
    parts = [label(caption, for_=f"f-{name}"), control]
    if help_text:
        parts.append(small(help_text, class_="help"))
    return div(*parts, class_="field")


def notice(message: Any, *, kind: str = "info") -> Markup:
    """A banner. ``kind`` is one of info, success, warning, error."""
    return div(message, class_=f"notice notice-{kind}", role="status")
