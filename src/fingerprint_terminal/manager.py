"""GTK4/libadwaita settings window for Fingerprint Terminal."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from .cli import launch_command
from .network import STATE_ROOT
from .profiles import ProfileError, load_store, validate_document
from .sandbox import SandboxError, is_strict


APP_ID = "io.fingerprintterminal.FingerprintTerminal"
DEFAULT_PROFILE_ID = "strict-auto-ip"
PROFILE_DATA_ROOT = Path.home() / ".local" / "share" / "fingerprint-terminal" / "profiles"
LAUNCH_SHORTCUT = ("Mod", "Shift", "T")
# Reloading is local file work that finishes in milliseconds; keep the busy
# state up long enough to be seen, then briefly confirm before resetting.
_REFRESH_MIN_BUSY_MS = 600
_REFRESH_DONE_MS = 1200

# Colors come from the active theme's accent so custom themes carry through.
_CSS = """
.ft-hero {
  padding: 22px 24px 24px;
  border-radius: 24px;
  background-image: linear-gradient(135deg,
      color-mix(in srgb, var(--accent-bg-color) 72%, white) 0%,
      var(--accent-bg-color) 55%,
      color-mix(in srgb, var(--accent-bg-color) 82%, black) 100%);
  color: var(--accent-fg-color);
  box-shadow: 0 8px 24px color-mix(in srgb, var(--accent-bg-color) 30%, transparent),
              0 1px 3px alpha(black, 0.15);
}

.ft-hero-icon {
  min-width: 40px;
  min-height: 40px;
  border-radius: 12px;
  background-color: alpha(white, 0.18);
}

.ft-keycap {
  min-width: 14px;
  padding: 2px 7px 3px;
  border-radius: 6px;
  background-color: alpha(white, 0.16);
  box-shadow: inset 0 -2px alpha(black, 0.15);
  font-size: 0.8em;
  font-weight: 600;
}

.ft-country {
  min-width: 68px;
  min-height: 68px;
  border-radius: 20px;
  background-color: alpha(white, 0.16);
  font-size: 1.6em;
  font-weight: 800;
  letter-spacing: 1px;
}

.ft-place {
  font-size: 1.8em;
  font-weight: 800;
}

.ft-exit-meta {
  font-feature-settings: "tnum";
}

button.ft-launch {
  padding: 8px 22px;
  background-color: alpha(white, 0.92);
  color: var(--accent-bg-color);
  font-weight: 700;
  box-shadow: 0 2px 6px alpha(black, 0.15);
}

button.ft-launch:hover {
  background-color: white;
}

button.ft-launch:active {
  background-color: alpha(white, 0.8);
}

.ft-tile-icon {
  color: var(--accent-color);
}

.ft-folder {
  padding: 16px;
}

flowbox.ft-grid > flowboxchild {
  padding: 0;
}

button.ft-link-tile {
  padding: 14px 16px;
}

.ft-empty {
  padding: 24px;
}
"""
_css_installed = False


def _install_css() -> None:
    global _css_installed
    display = Gdk.Display.get_default()
    if _css_installed or display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_string(_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _css_installed = True


def _tilde(path: str | Path) -> str:
    """Abbreviate the host HOME to ``~`` for display."""

    text = str(Path(os.path.expandvars(str(path))).expanduser())
    home = str(Path.home())
    if text == home:
        return "~"
    if text.startswith(home + "/"):
        return "~" + text[len(home):]
    return text


def _last_exit(profile_id: str) -> tuple[dict, float] | None:
    """Return the identity recorded by the profile's latest launch and its age."""

    path = STATE_ROOT / profile_id / "identity.json"
    try:
        identity = json.loads(path.read_text(encoding="utf-8"))
        age = time.time() - path.stat().st_mtime
    except (OSError, ValueError):
        return None
    if not isinstance(identity, dict) or not identity.get("ip"):
        return None
    return identity, age


def _relative_time(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 1:
        return "刚刚"
    if minutes < 60:
        return f"{minutes} 分钟前"
    if minutes < 24 * 60:
        return f"{minutes // 60} 小时前"
    return f"{minutes // (24 * 60)} 天前"


def _share_targets(profile: dict) -> set[str]:
    """Return safe relative mount targets declared by *profile*."""

    targets: set[str] = set()
    for share in profile.get("sandbox", {}).get("shares", []) or []:
        target = str(share.get("target", "") or "").strip()
        relative = PurePosixPath(target)
        if target and not relative.is_absolute() and ".." not in relative.parts:
            targets.add(relative.as_posix())
    return targets


def _cleanup_empty_share_targets(private_home: Path, targets: set[str]) -> int:
    """Remove known empty mountpoint directories below the private HOME."""

    removed = 0
    ordered = sorted(
        targets,
        key=lambda value: len(PurePosixPath(value).parts),
        reverse=True,
    )
    for target in ordered:
        relative = PurePosixPath(target)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            continue
        path = private_home.joinpath(*relative.parts)
        try:
            # rmdir deliberately leaves symlinks, files and non-empty folders
            # untouched, so refreshing cannot remove user data.
            path.rmdir()
        except OSError:
            continue
        removed += 1
    return removed


def _ensure_share_targets(private_home: Path, targets: set[str]) -> int:
    """Create configured mountpoint directories in the private HOME."""

    created = 0
    for target in sorted(targets):
        relative = PurePosixPath(target)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            continue
        path = private_home.joinpath(*relative.parts)
        if path.exists():
            continue
        try:
            path.mkdir(parents=True, exist_ok=False)
        except OSError:
            continue
        created += 1
    return created


def _label(
    text: str = "",
    *css_classes: str,
    xalign: float = 0,
    ellipsize: Pango.EllipsizeMode = Pango.EllipsizeMode.NONE,
) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=xalign, ellipsize=ellipsize)
    for name in css_classes:
        label.add_css_class(name)
    return label


class ManagerWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application) -> None:
        super().__init__(application=application, title="指纹终端设置")
        self.set_default_size(820, 860)
        self.set_size_request(620, 560)
        self.store = load_store()
        self.profile = self._load_default_profile()
        self._known_share_targets = _share_targets(self.profile)
        _ensure_share_targets(self._private_home_path(), self._known_share_targets)
        self._share_chooser: Gtk.FileChooserNative | None = None
        self._reload_pending = False
        self._refresh_reset_source = 0
        _install_css()

        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)

        toolbar = Adw.ToolbarView()
        self.toast_overlay.set_child(toolbar)

        header = Adw.HeaderBar()
        # The hero card names the window; the WM still gets the title.
        header.set_show_title(False)
        toolbar.add_top_bar(header)

        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menu_button.set_tooltip_text("更多")
        menu = Gio.Menu()
        menu.append("打开配置文件", "app.open-config")
        menu.append("重新载入", "app.reload")
        menu_button.set_menu_model(menu)
        header.pack_end(menu_button)

        conversations_button = Gtk.Button(label="AI 对话")
        conversations_button.set_tooltip_text("管理 Claude Code 和 Codex 对话记录")
        conversations_button.connect("clicked", self._open_conversations)
        header.pack_end(conversations_button)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        toolbar.set_content(scroller)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(760)
        clamp.set_tightening_threshold(620)
        scroller.set_child(clamp)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=30)
        page.set_margin_top(12)
        page.set_margin_bottom(36)
        page.set_margin_start(24)
        page.set_margin_end(24)
        clamp.set_child(page)

        page.append(self._build_hero())

        self.shares_group = Adw.PreferencesGroup(
            title="共享目录",
            description="只有这里列出的宿主目录会出现在指纹终端里。",
        )
        share_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        # One stack page per state keeps the button width fixed while it changes.
        self.refresh_stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE,
            transition_duration=150,
        )
        for name, indicator, text in (
            ("idle", Gtk.Image.new_from_icon_name("view-refresh-symbolic"), "刷新"),
            ("busy", Adw.Spinner(), "刷新中…"),
            ("done", Gtk.Image.new_from_icon_name("object-select-symbolic"), "已刷新"),
        ):
            content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            content.set_halign(Gtk.Align.CENTER)
            content.append(indicator)
            content.append(Gtk.Label(label=text))
            self.refresh_stack.add_named(content, name)
        self.refresh_button = Gtk.Button(child=self.refresh_stack)
        self.refresh_button.add_css_class("flat")
        self.refresh_button.set_valign(Gtk.Align.CENTER)
        self.refresh_button.set_tooltip_text("重新载入设置并同步共享目录挂载点")
        self.refresh_button.connect("clicked", lambda _button: self._reload())
        share_actions.append(self.refresh_button)

        add_share = Gtk.Button(
            child=Adw.ButtonContent(icon_name="list-add-symbolic", label="添加目录")
        )
        add_share.add_css_class("flat")
        add_share.set_valign(Gtk.Align.CENTER)
        add_share.connect("clicked", self._add_share)
        share_actions.append(add_share)
        self.shares_group.set_header_suffix(share_actions)

        self.share_grid = Gtk.FlowBox(
            homogeneous=True,
            selection_mode=Gtk.SelectionMode.NONE,
            min_children_per_line=1,
            max_children_per_line=3,
            column_spacing=12,
            row_spacing=12,
        )
        self.share_grid.add_css_class("ft-grid")
        self.shares_group.add(self.share_grid)
        self.shares_empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.shares_empty.add_css_class("card")
        self.shares_empty.add_css_class("ft-empty")
        self.shares_empty.append(_label("还没有共享目录", "heading", xalign=0.5))
        self.shares_empty.append(
            _label("点击右上角的“添加目录”选择一个宿主文件夹。", "caption", "dim-label", xalign=0.5)
        )
        self.shares_group.add(self.shares_empty)
        page.append(self.shares_group)

        self.storage_group = Adw.PreferencesGroup(title="存储")
        links = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, homogeneous=True)
        links.append(
            self._link_tile(
                "Profile 数据",
                _tilde(self._private_home_path()),
                "drive-harddisk-symbolic",
                lambda _button: self._open_private_home(None),
            )
        )
        links.append(
            self._link_tile(
                "高级配置",
                self.store.path.name,
                "document-properties-symbolic",
                lambda _button: self._open_config(None),
            )
        )
        self.storage_group.add(links)
        page.append(self.storage_group)

        self.connect("notify::is-active", self._on_active_changed)
        self._register_actions()
        self._refresh()

    def _build_hero(self) -> Gtk.Widget:
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22)
        hero.add_css_class("ft-hero")

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        icon = Gtk.Image.new_from_icon_name("utilities-terminal-symbolic")
        icon.set_pixel_size(22)
        icon.add_css_class("ft-hero-icon")
        top.append(icon)
        title = _label("指纹终端", "title-3")
        title.set_hexpand(True)
        top.append(title)
        keys = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        keys.set_valign(Gtk.Align.CENTER)
        keys.set_tooltip_text("快捷启动")
        for key in LAUNCH_SHORTCUT:
            keys.append(_label(key, "ft-keycap", xalign=0.5))
        top.append(keys)
        hero.append(top)

        self.exit_row = exit_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        self.country_badge = _label("", "ft-country", xalign=0.5)
        self.country_badge.set_valign(Gtk.Align.CENTER)
        exit_row.append(self.country_badge)
        exit_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        exit_text.set_valign(Gtk.Align.CENTER)
        exit_text.set_hexpand(True)
        self.exit_place = _label("", "ft-place", ellipsize=Pango.EllipsizeMode.END)
        self.exit_meta = _label("", "ft-exit-meta")
        self.exit_meta.set_wrap(True)
        self.exit_meta.set_selectable(True)
        exit_text.append(self.exit_place)
        exit_text.append(self.exit_meta)
        exit_row.append(exit_text)
        hero.append(exit_row)

        launch = Gtk.Button(
            child=Adw.ButtonContent(
                icon_name="media-playback-start-symbolic", label="打开终端"
            )
        )
        launch.add_css_class("pill")
        launch.add_css_class("ft-launch")
        launch.set_halign(Gtk.Align.START)
        launch.connect("clicked", self._launch)
        hero.append(launch)
        return hero

    def _link_tile(self, title: str, subtitle: str, icon_name: str, callback) -> Gtk.Button:
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.add_css_class("ft-tile-icon")
        content.append(icon)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.set_hexpand(True)
        text.append(_label(title, "heading"))
        text.append(_label(subtitle, "caption", "dim-label", ellipsize=Pango.EllipsizeMode.MIDDLE))
        content.append(text)
        content.append(Gtk.Image.new_from_icon_name("adw-external-link-symbolic"))

        button = Gtk.Button(child=content)
        button.add_css_class("card")
        button.add_css_class("ft-link-tile")
        button.set_tooltip_text(subtitle)
        button.connect("clicked", callback)
        return button

    def _load_default_profile(self) -> dict:
        try:
            profile = self.store.get(DEFAULT_PROFILE_ID)
        except ProfileError:
            strict_profiles = [profile for profile in self.store.profiles if is_strict(profile)]
            if not strict_profiles:
                raise ProfileError("no Strict Fingerprint profile is configured")
            profile = strict_profiles[0]
        if not is_strict(profile):
            raise ProfileError(f"profile {profile['id']!r} is not a strict sandbox")
        return profile

    def _register_actions(self) -> None:
        app = self.get_application()
        if app is None:
            return
        if app.lookup_action("open-config") is None:
            action = Gio.SimpleAction.new("open-config", None)
            action.connect("activate", lambda *_args: self._open_config(None))
            app.add_action(action)
        if app.lookup_action("reload") is None:
            action = Gio.SimpleAction.new("reload", None)
            action.connect("activate", lambda *_args: self._reload())
            app.add_action(action)

    def _private_home_path(self) -> Path:
        return PROFILE_DATA_ROOT / str(self.profile["id"]) / "home"

    def _toast(self, message: str) -> None:
        self.toast_overlay.add_toast(Adw.Toast.new(message))

    def _refresh(self) -> None:
        self._refresh_exit()
        self._refresh_shares()

    def _refresh_exit(self) -> None:
        recorded = _last_exit(str(self.profile["id"]))
        if recorded is None:
            self.country_badge.set_label("?")
            self.exit_place.set_label("还没有出口记录")
            self.exit_meta.set_label("打开一次终端后，这里会显示出口 IP 和时区。")
            self.exit_row.set_tooltip_text(None)
            return
        identity, age = recorded
        code = str(identity.get("country_code") or "?").upper()
        place = ", ".join(
            str(part) for part in (identity.get("city"), identity.get("country")) if part
        )
        self.country_badge.set_label(code)
        self.exit_place.set_label(place or code)
        self.exit_meta.set_label(
            " · ".join(
                str(part) for part in (identity.get("ip"), identity.get("timezone")) if part
            )
        )
        self.exit_row.set_tooltip_text(f"记录于 {_relative_time(age)}，每次打开终端时更新")

    def _on_active_changed(self, _window: Gtk.Window, _pspec: object) -> None:
        # Each launch rewrites identity.json after its exit preflight; pick the
        # new exit up whenever the user returns to this window.
        if self.is_active():
            self._refresh_exit()

    def _refresh_shares(self) -> None:
        self.share_grid.remove_all()
        shares = self.profile.get("sandbox", {}).get("shares", []) or []
        username = str(self.profile.get("sandbox", {}).get("username", "dev") or "dev")
        self.shares_empty.set_visible(not shares)
        self.share_grid.set_visible(bool(shares))
        for index, share in enumerate(shares):
            card = self._share_card(index, share, f"/home/{username}")
            self.share_grid.append(card)
            # Focus belongs to the card's own controls, not the grid cell.
            card.get_parent().set_focusable(False)

    def _share_card(self, index: int, share: dict, sandbox_home: str) -> Gtk.Widget:
        source = str(share.get("source", ""))
        target = str(share.get("target", ""))
        mode = str(share.get("mode", "rw"))

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        card.add_css_class("card")
        card.add_css_class("ft-folder")
        card.set_tooltip_text(f"终端内路径 {sandbox_home}/{target}")
        icon = Gtk.Image.new_from_icon_name("folder-symbolic")
        icon.set_pixel_size(32)
        icon.add_css_class("ft-tile-icon")
        icon.set_halign(Gtk.Align.START)
        icon.set_margin_bottom(10)
        card.append(icon)
        name = _label(f"~/{target}", "heading", ellipsize=Pango.EllipsizeMode.END)
        host = _label(
            f"宿主 {_tilde(source)}", "caption", "dim-label", ellipsize=Pango.EllipsizeMode.MIDDLE
        )
        for label in (name, host):
            # Long paths ellipsize instead of widening every grid cell.
            label.set_max_width_chars(1)
            card.append(label)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        actions.set_margin_top(14)
        mode_group = Adw.ToggleGroup()
        mode_group.add(Adw.Toggle(name="rw", label="读写"))
        mode_group.add(Adw.Toggle(name="ro", label="只读"))
        mode_group.set_active_name("ro" if mode == "ro" else "rw")
        mode_group.set_tooltip_text("终端内的访问权限")
        mode_group.connect("notify::active-name", self._share_mode_changed, index)
        actions.append(mode_group)
        remove = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        remove.add_css_class("flat")
        remove.add_css_class("circular")
        remove.set_hexpand(True)
        remove.set_halign(Gtk.Align.END)
        remove.set_tooltip_text("移除共享目录")
        remove.connect("clicked", self._remove_share, index)
        actions.append(remove)
        card.append(actions)
        return card

    def _save_profile(self, profile: dict) -> int:
        previous_targets = self._known_share_targets
        profiles = self.store.document.get("profiles", [])
        for index, existing in enumerate(profiles):
            if existing.get("id") == profile.get("id"):
                profiles[index] = profile
                break
        else:
            raise ProfileError(f"profile disappeared: {profile.get('id')}")
        validate_document(self.store.document)
        self.store.save()
        self.profile = profile
        self._known_share_targets = _share_targets(profile)
        cleaned = _cleanup_empty_share_targets(
            self._private_home_path(), previous_targets - self._known_share_targets
        )
        _ensure_share_targets(self._private_home_path(), self._known_share_targets)
        self._refresh()
        return cleaned

    def _add_share(self, _button: Gtk.Button) -> None:
        if self._share_chooser is not None:
            self._share_chooser.show()
            return

        chooser = Gtk.FileChooserNative.new(
            "选择要共享的本地目录",
            self,
            Gtk.FileChooserAction.SELECT_FOLDER,
            "添加",
            "取消",
        )
        # Keep the chooser rooted in the real host HOME.  The settings window
        # can be opened while the caller's cwd points at the profile's private
        # backing HOME; letting GTK inherit that cwd makes it very easy to pick
        # an internal mountpoint instead of the intended host directory.
        chooser.set_current_folder(Gio.File.new_for_path(str(Path.home().resolve())))
        chooser.connect("response", self._share_chooser_response)
        # GtkNativeDialog is not part of the widget tree.  Retain a strong
        # reference until the response arrives so the async native chooser
        # cannot be finalized before we persist the selected directory.
        self._share_chooser = chooser
        chooser.show()

    def _share_chooser_response(self, chooser: Gtk.FileChooserNative, response: int) -> None:
        if chooser is self._share_chooser:
            self._share_chooser = None
        if response != Gtk.ResponseType.ACCEPT:
            chooser.destroy()
            return
        selected = chooser.get_file()
        chooser.destroy()
        if selected is None or selected.get_path() is None:
            self._toast("没有选到本地目录")
            return

        source = Path(selected.get_path()).resolve()
        if not source.is_dir():
            self._toast("选择的路径不是目录")
            return
        if source == Path.home().resolve() or source == Path("/"):
            self._toast("不能共享整个宿主 Home 或根目录")
            return

        private_home = self._private_home_path().resolve()
        try:
            source.relative_to(private_home)
        except ValueError:
            pass
        else:
            self._toast("请选择宿主目录，不要选择私有 Home 里的内部目录")
            return

        shares = list(self.profile.get("sandbox", {}).get("shares", []) or [])
        source_text = str(source)
        if any(Path(str(item.get("source", ""))).expanduser().resolve() == source for item in shares):
            self._toast("这个目录已经共享了")
            return

        used_targets = {str(item.get("target", "")) for item in shares}
        base_target = source.name or "shared"
        target = base_target
        suffix = 2
        while target in used_targets:
            target = f"{base_target}-{suffix}"
            suffix += 1

        shares.append({"source": source_text, "target": target, "mode": "rw"})
        updated = dict(self.profile)
        sandbox = dict(updated.get("sandbox", {}))
        sandbox["shares"] = shares
        updated["sandbox"] = sandbox
        try:
            self._save_profile(updated)
            self._toast(f"已共享 {source}")
        except ProfileError as exc:
            self._toast(f"保存失败：{exc}")

    def _share_mode_changed(
        self, group: Adw.ToggleGroup, _pspec: object, index: int
    ) -> None:
        # Saving rebuilds the rows, so let the toggle finish handling its click
        # before its widget is replaced.
        GLib.idle_add(self._set_share_mode, index, group.get_active_name())

    def _set_share_mode(self, index: int, mode: str) -> bool:
        shares = list(self.profile.get("sandbox", {}).get("shares", []) or [])
        if not 0 <= index < len(shares) or shares[index].get("mode", "rw") == mode:
            return GLib.SOURCE_REMOVE
        item = dict(shares[index])
        item["mode"] = mode
        shares[index] = item
        updated = dict(self.profile)
        sandbox = dict(updated.get("sandbox", {}))
        sandbox["shares"] = shares
        updated["sandbox"] = sandbox
        try:
            self._save_profile(updated)
            self._toast("共享权限已更新")
        except ProfileError as exc:
            self._toast(f"保存失败：{exc}")
        return GLib.SOURCE_REMOVE

    def _remove_share(self, _button: Gtk.Button, index: int) -> None:
        shares = list(self.profile.get("sandbox", {}).get("shares", []) or [])
        if not 0 <= index < len(shares):
            return
        removed = shares.pop(index)
        updated = dict(self.profile)
        sandbox = dict(updated.get("sandbox", {}))
        sandbox["shares"] = shares
        updated["sandbox"] = sandbox
        try:
            cleaned = self._save_profile(updated)
            suffix = "，空挂载目录已清理" if cleaned else ""
            self._toast(
                f"已移除 {Path(str(removed.get('source', '目录'))).name}{suffix}"
            )
        except ProfileError as exc:
            self._toast(f"保存失败：{exc}")

    def _sync_private_home_hidden_entries(self, path: Path) -> None:
        """Hide bridge-only compatibility mount roots from file-manager views."""

        host_home = Path.home().resolve()
        compatibility_roots: set[str] = set()
        explicit_roots: set[str] = set()

        for share in self.profile.get("sandbox", {}).get("shares", []) or []:
            target = str(share.get("target", "") or "").strip()
            target_parts = Path(target).parts
            if target_parts:
                explicit_roots.add(target_parts[0])

            try:
                source = Path(str(share.get("source", ""))).expanduser().resolve()
                relative = source.relative_to(host_home)
            except (OSError, ValueError):
                continue
            if relative.parts and relative.as_posix() != target:
                compatibility_roots.add(relative.parts[0])

        managed_now = compatibility_roots - explicit_roots
        hidden_file = path / ".hidden"
        marker_file = path / ".fingerprint-terminal-hidden"

        try:
            previous_managed = {
                line.strip()
                for line in marker_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            }
        except OSError:
            previous_managed = set()

        try:
            existing = [
                line.strip()
                for line in hidden_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except OSError:
            existing = []

        user_entries = [entry for entry in existing if entry not in previous_managed]
        combined = list(dict.fromkeys([*user_entries, *sorted(managed_now)]))

        if combined:
            hidden_file.write_text("\n".join(combined) + "\n", encoding="utf-8")
        else:
            hidden_file.unlink(missing_ok=True)

        if managed_now:
            marker_file.write_text(
                "\n".join(sorted(managed_now)) + "\n",
                encoding="utf-8",
            )
        else:
            marker_file.unlink(missing_ok=True)

    def _open_private_home(self, _button: Gtk.Button | None) -> None:
        path = self._private_home_path()
        path.mkdir(parents=True, exist_ok=True)
        try:
            self._sync_private_home_hidden_entries(path)
            # Thunar normally reuses an existing window, which makes an old
            # Downloads tab sit next to the newly opened private HOME and looks
            # as if Fingerprint Terminal opened two tabs.  Force a fresh window
            # when Thunar is the default directory handler; keep the generic
            # Gio path for other desktops/file managers.
            app = Gio.AppInfo.get_default_for_type("inode/directory", True)
            executable = app.get_executable() if app is not None else None
            if executable and Path(executable).name == "thunar":
                subprocess.Popen(
                    [executable, "--window", str(path.resolve())],
                    start_new_session=True,
                )
            else:
                Gio.AppInfo.launch_default_for_uri(path.resolve().as_uri(), None)
            self._toast("已打开私有 Home")
        except (GLib.Error, OSError) as exc:
            message = exc.message if isinstance(exc, GLib.Error) else str(exc)
            self._toast(f"无法打开目录：{message}")

    def _open_conversations(self, _button: Gtk.Button) -> None:
        from .conversation_ui import ConversationWindow

        window = getattr(self, "_conversation_window", None)
        if window is None:
            window = ConversationWindow(self, str(self.profile["id"]))
            self._conversation_window = window
            window.connect(
                "destroy",
                lambda _window: setattr(self, "_conversation_window", None),
            )
        window.present()

    def _launch(self, _button: Gtk.Button) -> None:
        try:
            command, env = launch_command(str(self.profile["id"]))
            subprocess.Popen(command, env=env, start_new_session=True)
            self._toast("指纹终端已打开")
        except (ProfileError, SandboxError, OSError) as exc:
            self._toast(f"启动失败：{exc}")

    def _open_config(self, _button: Gtk.Button | None) -> None:
        try:
            Gio.AppInfo.launch_default_for_uri(self.store.path.resolve().as_uri(), None)
            self._toast("已打开配置文件")
        except GLib.Error as exc:
            self._toast(f"无法打开配置：{exc.message}")

    def _reload(self) -> None:
        if self._reload_pending:
            return
        self._reload_pending = True
        if self._refresh_reset_source:
            GLib.source_remove(self._refresh_reset_source)
            self._refresh_reset_source = 0
        self._set_refresh_state("busy")
        # Idle sources run after the next frame, so the spinner is drawn
        # before the synchronous reload starts.
        GLib.idle_add(self._run_reload, time.monotonic())

    def _run_reload(self, started: float) -> bool:
        succeeded, message = self._reload_settings()
        elapsed_ms = int((time.monotonic() - started) * 1000)
        GLib.timeout_add(
            max(0, _REFRESH_MIN_BUSY_MS - elapsed_ms),
            self._finish_reload,
            succeeded,
            message,
        )
        return GLib.SOURCE_REMOVE

    def _finish_reload(self, succeeded: bool, message: str) -> bool:
        self._reload_pending = False
        self._toast(message)
        if succeeded:
            self._set_refresh_state("done")
            self._refresh_reset_source = GLib.timeout_add(
                _REFRESH_DONE_MS, self._reset_refresh_button
            )
        else:
            self._set_refresh_state("idle")
        return GLib.SOURCE_REMOVE

    def _reset_refresh_button(self) -> bool:
        self._refresh_reset_source = 0
        self._set_refresh_state("idle")
        return GLib.SOURCE_REMOVE

    def _set_refresh_state(self, state: str) -> None:
        self.refresh_stack.set_visible_child_name(state)
        # Ignore clicks while busy without dimming the spinner the way an
        # insensitive button would.
        self.refresh_button.set_can_target(state != "busy")

    def _reload_settings(self) -> tuple[bool, str]:
        """Reload the profile and sync share mountpoints; return (ok, message)."""

        try:
            previous_targets = self._known_share_targets
            self.store = load_store()
            self.profile = self._load_default_profile()
            self._known_share_targets = _share_targets(self.profile)
            cleaned = _cleanup_empty_share_targets(
                self._private_home_path(), previous_targets - self._known_share_targets
            )
            created = _ensure_share_targets(
                self._private_home_path(), self._known_share_targets
            )
            self._refresh()
            if cleaned or created:
                return True, (
                    f"设置已刷新：新建 {created} 个、清理 {cleaned} 个挂载点；"
                    "共享变更将在新终端生效"
                )
            return True, "设置已刷新；共享变更将在新终端生效"
        except ProfileError as exc:
            return False, f"重新载入失败：{exc}"


class FingerprintTerminalApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self) -> None:  # type: ignore[override]
        window = self.props.active_window
        if window is None:
            try:
                window = ManagerWindow(self)
            except ProfileError as exc:
                print(f"fingerprint-terminal: {exc}", file=sys.stderr)
                self.quit()
                return
        window.present()


def main(argv: list[str] | None = None) -> int:
    app = FingerprintTerminalApplication()
    return app.run(argv if argv else [sys.argv[0]])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
