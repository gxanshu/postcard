"""Recolor a message body so it sits on the reader's own libadwaita surface.

Mail HTML hard-codes its page color, nearly always white. The reader shows it
over a transparent WebView on `@view_bg_color`, so a neutral page color (white,
or black for mail designed dark) is removed to let that surface through. In
dark mode, what is left is mirrored: light fills go dark and dark text goes
light, keeping each hue, so a light design can't put black text on the dark
surface. Mid tones -- brand colors, buttons -- sit fine on either and are kept.

Only `color`, `background` and `background-color` in `<style>` blocks and
`style` attributes are touched, plus the legacy `bgcolor`, `color` and `text`
attributes. Text content is never rewritten. The same approach as Hylki's
reader (github.com/hyprlab/hylki).
"""

import colorsys
import re

# A page color: nearly no chroma, at either end of lightness.
_NEUTRAL_CHROMA = 0.06
_WHITE_FLOOR = 0.88
_BLACK_CEILING = 0.12
# Dark mode flips light fills and dark text; the floors keep mirrored fills
# above pure black and mirrored text clearly readable.
_FILL_FLIP_ABOVE = 0.6
_TEXT_FLIP_BELOW = 0.5
_FILL_FLOOR = 0.08
_TEXT_FLOOR = 0.72
# Below this alpha a color is invisible whichever way it is flipped.
_INVISIBLE_ALPHA = 0.01

# The named colors mail actually uses; anything else is left alone.
_NAMED = {
    "white": "ffffff",
    "snow": "fffafa",
    "whitesmoke": "f5f5f5",
    "ghostwhite": "f8f8ff",
    "gainsboro": "dcdcdc",
    "lightgray": "d3d3d3",
    "lightgrey": "d3d3d3",
    "silver": "c0c0c0",
    "gray": "808080",
    "grey": "808080",
    "dimgray": "696969",
    "dimgrey": "696969",
    "black": "000000",
    "navy": "000080",
    "darkblue": "00008b",
    "maroon": "800000",
    "darkred": "8b0000",
    "darkgreen": "006400",
}

_DECLARATION = re.compile(
    r"(?<![\w-])(color|background-color|background)(\s*:\s*)([^;{}\"']*)", re.I
)
_VALUE_TOKEN = re.compile(
    r"url\([^)]*\)|(#[0-9a-f]{3,8}\b|rgba?\([^)]*\)|\b[a-z]+\b)", re.I
)
_STYLE_ATTR = re.compile(r"(\sstyle\s*=\s*)(\"[^\"]*\"|'[^']*')", re.I)
_COLOR_ATTR = re.compile(r"(\s(bgcolor|color|text)\s*=\s*)([\"']?)([#\w]+)\3", re.I)
_MARKUP = re.compile(r"(<style[^>]*>)(.*?)(</style\s*>)|<[a-z][^>]*>", re.I | re.S)


def recolor(html: str, *, is_dark: bool) -> str:
    return _MARKUP.sub(lambda match: _recolor_markup(match, is_dark), html)


def _recolor_markup(match: re.Match[str], is_dark: bool) -> str:
    if match[1]:
        return match[1] + _recolor_css(match[2], is_dark) + match[3]

    def recolor_style(attr: re.Match[str]) -> str:
        return attr[1] + _recolor_css(attr[2], is_dark)

    def recolor_legacy(attr: re.Match[str]) -> str:
        is_background = attr[2].lower() == "bgcolor"
        replacement = _adapt(attr[4], is_background=is_background, is_dark=is_dark)
        return attr[0] if replacement is None else f'{attr[1]}"{replacement}"'

    return _COLOR_ATTR.sub(recolor_legacy, _STYLE_ATTR.sub(recolor_style, match[0]))


def _recolor_css(css: str, is_dark: bool) -> str:
    def recolor_declaration(match: re.Match[str]) -> str:
        is_background = match[1].lower() != "color"
        value = _recolor_value(match[3], is_background=is_background, is_dark=is_dark)
        return match[1] + match[2] + value

    return _DECLARATION.sub(recolor_declaration, css)


def _recolor_value(value: str, *, is_background: bool, is_dark: bool) -> str:
    def recolor_token(token: re.Match[str]) -> str:
        if token[1] is None:
            return token[0]
        replacement = _adapt(token[1], is_background=is_background, is_dark=is_dark)
        return token[0] if replacement is None else replacement

    return _VALUE_TOKEN.sub(recolor_token, value)


def _adapt(token: str, *, is_background: bool, is_dark: bool) -> str | None:
    """The color to use instead of `token`, or None to keep it."""
    rgba = _parse(token)
    if rgba is None or rgba[3] <= _INVISIBLE_ALPHA:
        return None
    red, green, blue, alpha = rgba
    hue, lightness, saturation = colorsys.rgb_to_hls(red, green, blue)

    if is_background:
        is_neutral = max(red, green, blue) - min(red, green, blue) < _NEUTRAL_CHROMA
        if is_neutral and (
            lightness >= _WHITE_FLOOR or (is_dark and lightness <= _BLACK_CEILING)
        ):
            return "transparent"
        if not is_dark or lightness <= _FILL_FLIP_ABOVE:
            return None
        lightness = max(1 - lightness, _FILL_FLOOR)
    else:
        if not is_dark or lightness >= _TEXT_FLIP_BELOW:
            return None
        lightness = max(1 - lightness, _TEXT_FLOOR)

    channels = [round(c * 255) for c in colorsys.hls_to_rgb(hue, lightness, saturation)]
    if alpha < 1:
        return f"rgba({channels[0]},{channels[1]},{channels[2]},{alpha:.2f})"
    return "#{:02x}{:02x}{:02x}".format(*channels)


def _parse(token: str) -> tuple[float, float, float, float] | None:
    text = token.strip().lower()
    text = _NAMED.get(text, text).removeprefix("#")
    if re.fullmatch(r"[0-9a-f]{3,4}", text):
        text = "".join(c * 2 for c in text)
    if re.fullmatch(r"[0-9a-f]{6}([0-9a-f]{2})?", text):
        values = [int(text[i : i + 2], 16) / 255 for i in range(0, len(text), 2)]
        return (values[0], values[1], values[2], values[3] if len(values) > 3 else 1)

    function = re.fullmatch(r"rgba?\(([^)]*)\)", text)
    if function is None:
        return None
    parts = re.split(r"[\s,/]+", function[1].strip())
    if len(parts) < 3:
        return None
    try:
        rgb = [_channel(part, 255) for part in parts[:3]]
        alpha = _channel(parts[3], 1) if len(parts) > 3 else 1
    except ValueError:
        return None
    return (rgb[0], rgb[1], rgb[2], alpha)


def _channel(part: str, scale: float) -> float:
    if part.endswith("%"):
        return min(max(float(part[:-1]) / 100, 0), 1)
    return min(max(float(part) / scale, 0), 1)
