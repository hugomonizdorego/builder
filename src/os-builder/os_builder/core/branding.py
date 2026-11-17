"""Text generators for the look of the image: GSettings overrides, greeter
configuration, the Plymouth splash theme and GRUB settings.

Everything here is a pure function (text in, text out) so it can be tested
without root; :mod:`os_builder.core.customizer` writes the results into the
image.
"""

from __future__ import annotations

import re
from pathlib import Path

from os_builder import APP_ID

APPEARANCE_OVERRIDE = f"usr/share/glib-2.0/schemas/90_{APP_ID}.gschema.override"
LOGIN_OVERRIDE = f"usr/share/glib-2.0/schemas/91_{APP_ID}-login.gschema.override"
SPLASH_THEME = APP_ID  # /usr/share/plymouth/themes/os-builder
TARGET_GRUB_DROPIN = f"etc/default/grub.d/90-{APP_ID}.cfg"

#: Colours GRUB understands (gfxterm and text mode).
GRUB_COLORS = (
    "black", "blue", "green", "cyan", "red", "magenta", "brown", "light-gray", "dark-gray", "light-blue",
    "light-green", "light-cyan", "light-red", "light-magenta", "yellow", "white",
)  # fmt: skip
_GRUB_BEGIN = f"### BEGIN {APP_ID} appearance ###"
_GRUB_END = f"### END {APP_ID} appearance ###"


def gvariant_string(value: str) -> str:
    """Quote ``value`` as a GVariant string for a ``.gschema.override`` file."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def schema_ids(schema_dir: Path) -> set[str]:
    """GSettings schema ids defined by the ``*.gschema.xml`` files in ``schema_dir``."""
    ids: set[str] = set()
    for path in schema_dir.glob("*.gschema.xml"):
        ids.update(re.findall(r'<schema[^>]*\bid="([^"]+)"', path.read_text(encoding="utf-8", errors="replace")))
    return ids


def _section(schema: str, values: dict[str, str]) -> str:
    return f"[{schema}]\n" + "\n".join(f"{key}={value}" for key, value in values.items())


def appearance_override(
    schemas: set[str], *, wallpaper: str = "", gtk: str = "", icons: str = "", cursor: str = ""
) -> str:
    """Desktop defaults for every desktop family installed in the image.

    GNOME, Budgie and Unity share ``org.gnome.desktop.*``; MATE and Cinnamon
    have their own schemas. Sections for absent schemas are left out, so
    ``glib-compile-schemas`` never sees unknown ids. ``wallpaper`` is an
    absolute path inside the image.
    """
    sections: list[str] = []
    uri = f"file://{wallpaper}" if wallpaper else ""

    def add(schema: str, values: dict[str, str]) -> None:
        values = {k: v for k, v in values.items() if v}
        if values and schema in schemas:
            sections.append(_section(schema, values))
            if schema.startswith("org.gnome.desktop."):
                # The ":ubuntu" variant outranks Ubuntu's own session defaults.
                sections.append(_section(f"{schema}:ubuntu", values))

    quoted = {name: gvariant_string(value) if value else "" for name, value in
              {"uri": uri, "gtk": gtk, "icons": icons, "cursor": cursor, "file": wallpaper}.items()}  # fmt: skip
    add("org.gnome.desktop.background", {"picture-uri": quoted["uri"], "picture-uri-dark": quoted["uri"]})
    add("org.gnome.desktop.screensaver", {"picture-uri": quoted["uri"]})
    add("org.gnome.desktop.interface",
        {"gtk-theme": quoted["gtk"], "icon-theme": quoted["icons"], "cursor-theme": quoted["cursor"]})
    add("org.mate.background", {"picture-filename": quoted["file"]})
    add("org.mate.interface", {"gtk-theme": quoted["gtk"], "icon-theme": quoted["icons"]})
    add("org.mate.peripherals-mouse", {"cursor-theme": quoted["cursor"]})
    add("org.cinnamon.desktop.background", {"picture-uri": quoted["uri"]})
    add("org.cinnamon.desktop.interface",
        {"gtk-theme": quoted["gtk"], "icon-theme": quoted["icons"], "cursor-theme": quoted["cursor"]})
    return "\n\n".join(sections) + "\n" if sections else ""


def login_override(
    schemas: set[str], *, background: str = "", logo: str = "", message: str = "",
    gtk: str = "", icons: str = "", cursor: str = "",
) -> str:  # fmt: skip
    """Login screen settings for GDM (Ubuntu) and Slick Greeter."""
    sections: list[str] = []
    q = {k: gvariant_string(v) if v else "" for k, v in
         {"bg": background, "uri": f"file://{background}" if background else "", "logo": logo, "msg": message,
          "gtk": gtk, "icons": icons, "cursor": cursor}.items()}  # fmt: skip

    def add(schema: str, values: dict[str, str]) -> None:
        values = {k: v for k, v in values.items() if v}
        if values and schema in schemas:
            sections.append(_section(schema, values))

    # GDM: Ubuntu's own schema for the background, GNOME's for logo and banner.
    add("com.ubuntu.login-screen",
        {"background-picture-uri": q["uri"], "background-size": "'cover'" if background else ""})
    add("org.gnome.login-screen", {
        "logo": q["logo"],
        "banner-message-enable": "true" if message else "",
        "banner-message-text": q["msg"],
    })  # fmt: skip
    add("x.dm.slick-greeter", {
        "background": q["bg"], "draw-user-backgrounds": "false" if background else "", "logo": q["logo"],
        "theme-name": q["gtk"], "icon-theme-name": q["icons"], "cursor-theme-name": q["cursor"],
    })  # fmt: skip
    return "\n\n".join(sections) + "\n" if sections else ""


def set_ini_value(text: str, section: str, key: str, value: str) -> str:
    """Set ``key=value`` in ``[section]`` of an INI file, keeping everything else.

    A commented-out ``#key=`` line in the section is replaced in place.
    """
    lines = text.splitlines()
    header = re.compile(rf"^\s*\[{re.escape(section)}\]\s*$", re.IGNORECASE)
    start = next((i for i, line in enumerate(lines) if header.match(line)), None)
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines += [f"[{section}]", f"{key}={value}"]
        return "\n".join(lines) + "\n"
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    pattern = re.compile(rf"^\s*#?\s*{re.escape(key)}\s*=")
    for i in range(start + 1, end):
        if pattern.match(lines[i]):
            lines[i] = f"{key}={value}"
            return "\n".join(lines) + "\n"
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1  # keep the blank line(s) that separate sections
    lines.insert(insert_at, f"{key}={value}")
    return "\n".join(lines) + "\n"


# -- Plymouth -------------------------------------------------------------------------

def plymouth_theme_file(name: str = SPLASH_THEME) -> str:
    return (
        "[Plymouth Theme]\n"
        f"Name={name}\n"
        f"Description=Boot splash generated by {APP_ID}\n"
        "ModuleName=script\n\n"
        "[script]\n"
        f"ImageDir=/usr/share/plymouth/themes/{name}\n"
        f"ScriptFile=/usr/share/plymouth/themes/{name}/{name}.script\n"
    )


def plymouth_script(rgb: tuple[int, int, int]) -> str:
    """Script plugin program: centred logo, progress bar, password prompt, messages."""
    r, g, b = (round(c / 255, 3) for c in rgb)
    return f"""# Boot splash generated by {APP_ID}.
Window.SetBackgroundTopColor({r}, {g}, {b});
Window.SetBackgroundBottomColor({r}, {g}, {b});

screen_x = Window.GetX();
screen_y = Window.GetY();
screen_w = Window.GetWidth();
screen_h = Window.GetHeight();

logo_image = Image("logo.png");
scale = Math.Min(screen_w * 0.4 / logo_image.GetWidth(), screen_h * 0.4 / logo_image.GetHeight());
if (scale < 1)
  logo_image = logo_image.Scale(logo_image.GetWidth() * scale, logo_image.GetHeight() * scale);
logo_sprite = Sprite(logo_image);
logo_y = screen_y + screen_h / 2 - logo_image.GetHeight() / 2;
logo_sprite.SetPosition(screen_x + screen_w / 2 - logo_image.GetWidth() / 2, logo_y, 1);

bar_w = Math.Int(screen_w / 4);
bar_x = screen_x + screen_w / 2 - bar_w / 2;
bar_y = logo_y + logo_image.GetHeight() + 40;
bar_bg = Sprite(Image("progress_bg.png").Scale(bar_w, 4));
bar_bg.SetPosition(bar_x, bar_y, 2);
bar_fg_image = Image("progress_fg.png");
bar_fg = Sprite();
bar_fg.SetPosition(bar_x, bar_y, 3);

fun progress_callback(duration, progress) {{
  width = Math.Int(bar_w * progress);
  if (width >= 1)
    bar_fg.SetImage(bar_fg_image.Scale(width, 4));
}}
Plymouth.SetBootProgressFunction(progress_callback);

# Disk encryption passphrase.
prompt_sprite = Sprite();
bullets_sprite = Sprite();
text_y = bar_y + 30;

fun display_password_callback(prompt, bullets) {{
  prompt_image = Image.Text(prompt, 1, 1, 1);
  prompt_sprite.SetImage(prompt_image);
  prompt_sprite.SetPosition(screen_x + screen_w / 2 - prompt_image.GetWidth() / 2, text_y, 10);
  stars = "";
  for (i = 0; i < bullets; i++)
    stars = stars + "*";
  bullets_image = Image.Text(stars + " ", 1, 1, 1);
  bullets_sprite.SetImage(bullets_image);
  bullets_sprite.SetPosition(screen_x + screen_w / 2 - bullets_image.GetWidth() / 2, text_y + 30, 10);
  prompt_sprite.SetOpacity(1);
  bullets_sprite.SetOpacity(1);
  bar_bg.SetOpacity(0);
  bar_fg.SetOpacity(0);
}}

fun display_normal_callback() {{
  prompt_sprite.SetOpacity(0);
  bullets_sprite.SetOpacity(0);
  bar_bg.SetOpacity(1);
  bar_fg.SetOpacity(1);
}}
Plymouth.SetDisplayPasswordFunction(display_password_callback);
Plymouth.SetDisplayNormalFunction(display_normal_callback);

message_sprite = Sprite();
fun message_callback(text) {{
  message_image = Image.Text(text, 1, 1, 1);
  message_sprite.SetImage(message_image);
  message_sprite.SetPosition(screen_x + screen_w / 2 - message_image.GetWidth() / 2, screen_y + screen_h - 60, 10);
}}
Plymouth.SetMessageFunction(message_callback);
"""


# -- GRUB -----------------------------------------------------------------------------

def grub_appearance_block(background: str = "", text: str = "", highlight: str = "") -> str:
    """Settings for the ISO's boot menu. ``background`` is a path on the ISO."""
    lines = [_GRUB_BEGIN]
    if background:
        module = "jpeg" if background.lower().endswith((".jpg", ".jpeg")) else "png"
        lines += [
            "insmod all_video", "insmod gfxterm", f"insmod {module}", "loadfont unicode", "set gfxmode=auto",
            "terminal_output gfxterm", f"background_image {background}",
        ]  # fmt: skip
    if text:
        # With a background image, "black" as background colour is transparent.
        lines += [f"set color_normal={text}/black", f"set menu_color_normal={text}/black"]
    if highlight:
        lines.append(f"set menu_color_highlight={highlight}")
    lines.append(_GRUB_END)
    return "\n".join(lines) + "\n"


def apply_grub_appearance(config_text: str, block: str) -> str:
    """Insert ``block`` before the first menu entry (replacing an earlier one)."""
    cleaned = re.sub(rf"{re.escape(_GRUB_BEGIN)}.*?{re.escape(_GRUB_END)}\n?\n?", "", config_text, flags=re.DOTALL)
    match = re.search(r"^\s*(menuentry|submenu)\b", cleaned, re.MULTILINE)
    position = match.start() if match else len(cleaned)
    return cleaned[:position] + block + "\n" + cleaned[position:]


def is_grub_color(value: str) -> bool:
    return value in GRUB_COLORS


def is_grub_color_pair(value: str) -> bool:
    fg, sep, bg = value.partition("/")
    return bool(sep) and fg in GRUB_COLORS and bg in GRUB_COLORS


def target_grub_dropin(*, timeout: int | None, cmdline: str, background: str, theme: str) -> str:
    """``/etc/default/grub.d`` snippet for the installed system's boot menu."""
    lines = [f"# Written by {APP_ID}."]
    if timeout is not None:
        lines += ["GRUB_TIMEOUT_STYLE=menu", f"GRUB_TIMEOUT={timeout}"]
    if cmdline:
        lines.append(f'GRUB_CMDLINE_LINUX_DEFAULT="{cmdline}"')
    if theme:
        lines += ["GRUB_GFXMODE=auto", f'GRUB_THEME="{theme}"']
    elif background:
        lines += ["GRUB_GFXMODE=auto", f'GRUB_BACKGROUND="{background}"']
    return "\n".join(lines) + "\n"
