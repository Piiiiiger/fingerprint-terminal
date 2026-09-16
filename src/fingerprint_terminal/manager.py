"""GTK4/libadwaita settings window for Fingerprint Terminal."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .cli import launch_command
from .profiles import ProfileError, load_store, validate_document
from .sandbox import SandboxError, is_strict


APP_ID = "io.fingerprintterminal.FingerprintTerminal"
DEFAULT_PROFILE_ID = "strict-auto-ip"
PROFILE_DATA_ROOT = Path.home() / ".local" / "share" / "fingerprint-terminal" / "profiles"


class ManagerWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application) -> None:
        super().__init__(application=application, title="指纹终端设置")
        self.set_default_size(780, 760)
        self.set_size_request(620, 560)
        self.store = load_store()
        self.profile = self._load_default_profile()

        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)

        toolbar = Adw.ToolbarView()
        self.toast_overlay.set_child(toolbar)

        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label="指纹终端设置"))
        toolbar.add_top_bar(header)

        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menu_button.set_tooltip_text("更多")
        menu = Gio.Menu()
        menu.append("打开配置文件", "app.open-config")
        menu.append("重新载入", "app.reload")
        menu_button.set_menu_model(menu)
        header.pack_end(menu_button)

        launch_button = Gtk.Button(label="打开终端")
        launch_button.add_css_class("suggested-action")
        launch_button.connect("clicked", self._launch)
        header.pack_end(launch_button)

        conversations_button = Gtk.Button(label="AI 对话")
        conversations_button.set_tooltip_text("管理 Claude Code 和 Codex 对话记录")
        conversations_button.connect("clicked", self._open_conversations)
        header.pack_end(conversations_button)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        toolbar.set_content(scroller)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(720)
        clamp.set_tightening_threshold(620)
        scroller.set_child(clamp)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        page.set_margin_top(30)
        page.set_margin_bottom(36)
        page.set_margin_start(24)
        page.set_margin_end(24)
        clamp.set_child(page)

        hero = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        hero.set_margin_bottom(4)
        page.append(hero)

        hero_icon = Gtk.Image.new_from_icon_name("utilities-terminal-symbolic")
        hero_icon.set_pixel_size(52)
        hero_icon.add_css_class("accent")
        hero.append(hero_icon)

        hero_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        hero_text.set_valign(Gtk.Align.CENTER)
        hero_text.set_hexpand(True)
        hero.append(hero_text)

        title = Gtk.Label(label="指纹终端", xalign=0)
        title.add_css_class("title-1")
        hero_text.append(title)
        subtitle = Gtk.Label(
            label="透明网络 · Strict 隔离 · 私有 Home",
            xalign=0,
        )
        subtitle.add_css_class("dim-label")
        hero_text.append(subtitle)

        shortcut = Gtk.Label(label="Mod + Shift + T")
        shortcut.add_css_class("caption")
        shortcut.add_css_class("dim-label")
        shortcut.set_valign(Gtk.Align.CENTER)
        hero.append(shortcut)

        self.identity_group = Adw.PreferencesGroup(
            title="环境",
            description="终端内部看到的身份与网络环境。",
        )
        page.append(self.identity_group)

        self.network_row = Adw.ActionRow(title="网络")
        self.network_row.set_icon_name("network-vpn-symbolic")
        self.identity_group.add(self.network_row)

        self.home_row = Adw.ActionRow(title="私有 Home")
        self.home_row.set_icon_name("user-home-symbolic")
        self.identity_group.add(self.home_row)

        self.hostname_row = Adw.ActionRow(title="主机名")
        self.hostname_row.set_icon_name("computer-symbolic")
        self.identity_group.add(self.hostname_row)

        self.shares_group = Adw.PreferencesGroup(
            title="共享目录",
            description="只有这里列出的宿主目录会出现在指纹终端里。",
        )
        self.share_rows: list[Adw.ActionRow] = []
        page.append(self.shares_group)

        share_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        share_actions.set_halign(Gtk.Align.START)
        page.append(share_actions)

        add_share = Gtk.Button(label="添加目录")
        add_share.set_icon_name("folder-new-symbolic")
        add_share.add_css_class("suggested-action")
        add_share.add_css_class("pill")
        add_share.connect("clicked", self._add_share)
        share_actions.append(add_share)

        open_home = Gtk.Button(label="打开私有 Home")
        open_home.set_icon_name("folder-open-symbolic")
        open_home.add_css_class("pill")
        open_home.connect("clicked", self._open_private_home)
        share_actions.append(open_home)

        self.storage_group = Adw.PreferencesGroup(title="存储")
        page.append(self.storage_group)

        private_home_row = Adw.ActionRow(
            title="Profile 数据",
            subtitle=str(self._private_home_path()),
        )
        private_home_row.set_icon_name("drive-harddisk-symbolic")
        open_button = Gtk.Button(label="打开")
        open_button.set_valign(Gtk.Align.CENTER)
        open_button.add_css_class("flat")
        open_button.connect("clicked", self._open_private_home)
        private_home_row.add_suffix(open_button)
        self.storage_group.add(private_home_row)

        config_row = Adw.ActionRow(
            title="高级配置",
            subtitle="profiles.json",
        )
        config_row.set_icon_name("document-properties-symbolic")
        config_button = Gtk.Button(label="打开")
        config_button.set_valign(Gtk.Align.CENTER)
        config_button.add_css_class("flat")
        config_button.connect("clicked", self._open_config)
        config_row.add_suffix(config_button)
        self.storage_group.add(config_row)

        self._register_actions()
        self._refresh()

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
        network = self.profile.get("network", {})
        identity = self.profile.get("identity", {})
        username = str(self.profile.get("sandbox", {}).get("username", "dev") or "dev")
        if network.get("mode") == "transparent":
            network_subtitle = "透明代理 · 自动跟随出口 IP"
            bypasses = network.get("direct_bypass", []) or []
            if bypasses:
                names = [str(rule.get("name") or rule.get("destination")) for rule in bypasses]
                network_subtitle += " · 直连：" + "、".join(names)
        else:
            network_subtitle = "未启用透明网络"
        if self.profile.get("sandbox", {}).get("clipboard") == "wayland":
            network_subtitle += " · 宿主剪贴板"
        self.network_row.set_subtitle(network_subtitle)
        self.home_row.set_subtitle(f"/home/{username} · 与宿主 Home 隔离")
        self.hostname_row.set_subtitle(str(identity.get("hostname", "auto")))
        self._refresh_shares()

    def _clear_share_rows(self) -> None:
        for row in self.share_rows:
            self.shares_group.remove(row)
        self.share_rows.clear()

    def _refresh_shares(self) -> None:
        self._clear_share_rows()
        shares = self.profile.get("sandbox", {}).get("shares", []) or []
        username = str(self.profile.get("sandbox", {}).get("username", "dev") or "dev")
        sandbox_home = f"/home/{username}"

        if not shares:
            empty = Adw.ActionRow(
                title="还没有共享目录",
                subtitle="点击下面的“添加目录”选择一个宿主文件夹。",
            )
            empty.set_icon_name("folder-symbolic")
            self.shares_group.add(empty)
            self.share_rows.append(empty)
            return

        for index, share in enumerate(shares):
            source = str(share.get("source", ""))
            target = str(share.get("target", ""))
            mode = str(share.get("mode", "rw"))
            row = Adw.ActionRow(
                title=f"{sandbox_home}/{target}",
                subtitle=f"宿主：{source}",
            )
            row.set_icon_name("folder-symbolic")

            mode_button = Gtk.Button(label="读写" if mode == "rw" else "只读")
            mode_button.set_valign(Gtk.Align.CENTER)
            mode_button.add_css_class("flat")
            mode_button.set_tooltip_text("切换读写权限")
            mode_button.connect("clicked", self._toggle_share_mode, index)
            row.add_suffix(mode_button)

            remove = Gtk.Button.new_from_icon_name("user-trash-symbolic")
            remove.set_valign(Gtk.Align.CENTER)
            remove.add_css_class("flat")
            remove.set_tooltip_text("移除共享目录")
            remove.connect("clicked", self._remove_share, index)
            row.add_suffix(remove)
            self.shares_group.add(row)
            self.share_rows.append(row)

    def _save_profile(self, profile: dict) -> None:
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
        self._refresh()

    def _add_share(self, _button: Gtk.Button) -> None:
        chooser = Gtk.FileChooserNative.new(
            "选择要共享的本地目录",
            self,
            Gtk.FileChooserAction.SELECT_FOLDER,
            "添加",
            "取消",
        )
        chooser.connect("response", self._share_chooser_response)
        chooser.show()

    def _share_chooser_response(self, chooser: Gtk.FileChooserNative, response: int) -> None:
        if response != Gtk.ResponseType.ACCEPT:
            chooser.destroy()
            return
        selected = chooser.get_file()
        chooser.destroy()
        if selected is None or selected.get_path() is None:
            return

        source = Path(selected.get_path()).resolve()
        if source == Path.home().resolve() or source == Path("/"):
            self._toast("不能共享整个宿主 Home 或根目录")
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
            self._toast(f"已共享 {source.name}")
        except ProfileError as exc:
            self._toast(f"保存失败：{exc}")

    def _toggle_share_mode(self, _button: Gtk.Button, index: int) -> None:
        shares = list(self.profile.get("sandbox", {}).get("shares", []) or [])
        if not 0 <= index < len(shares):
            return
        item = dict(shares[index])
        item["mode"] = "ro" if item.get("mode", "rw") == "rw" else "rw"
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
            self._save_profile(updated)
            self._toast(f"已移除 {Path(str(removed.get('source', '目录'))).name}")
        except ProfileError as exc:
            self._toast(f"保存失败：{exc}")

    def _open_private_home(self, _button: Gtk.Button | None) -> None:
        path = self._private_home_path()
        path.mkdir(parents=True, exist_ok=True)
        try:
            Gio.AppInfo.launch_default_for_uri(path.resolve().as_uri(), None)
            self._toast("已打开私有 Home")
        except GLib.Error as exc:
            self._toast(f"无法打开目录：{exc.message}")

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
        try:
            self.store = load_store()
            self.profile = self._load_default_profile()
            self._refresh()
            self._toast("设置已重新载入")
        except ProfileError as exc:
            self._toast(f"重新载入失败：{exc}")


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
