import json
import logging
import mimetypes
import threading
from datetime import datetime
from email.utils import getaddresses
from gettext import gettext as _
from typing import cast

import gi

gi.require_version("JavaScriptCore", "6.0")
gi.require_version("WebKit", "6.0")

from gi.repository import (
    Adw,
    Gdk,
    Gio,
    GLib,
    GObject,
    Gtk,
    JavaScriptCore,
    Pango,
    WebKit,
)

from . import mail_sync
from .core import compose, secrets
from .core.models.account import Account
from .core.models.attachment import Attachment
from .core.net import errors
from .core.store.database import Database
from .core.threader import NO_SUBJECT

logger = logging.getLogger(__name__)

_EDITOR_PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  html, body {{ margin: 0; height: 100%; }}
  body {{
    box-sizing: border-box;
    padding: 12px;
    font-family: {family};
    font-size: {size}pt;
    line-height: 1.5;
    color: #241f31;
    background: transparent;
    outline: none;
  }}
  a {{ color: #1c71d8; }}
  blockquote {{ color: #5e5c64; }}
  .signature {{ color: #5e5c64; }}
  @media (prefers-color-scheme: dark) {{
    body {{ color: #f6f5f4; }}
    a {{ color: #78aeed; }}
    blockquote, .signature {{ color: #c0bfbc; }}
  }}
</style>
</head>
<body contenteditable="true">{body}</body>
</html>
"""

# Injected as a user script rather than written into the page: a <script> in
# the page is part of document.body.innerHTML, and so of every message sent.
_EDITOR_SCRIPT = """
// WebKit writes the editor's own font and colour onto text it moves -- a
// paste, two lines joined -- and in a message they would override the
// recipient's. A signature keeps its own: it is the user's markup.
const EDITOR_STYLES = ['font-size', 'font-family', 'line-height', 'color'];

function messageHtml() {
  const copy = document.body.cloneNode(true);
  for (const element of copy.querySelectorAll('[style]')) {
    if (element.closest('.signature')) continue;
    for (const name of EDITOR_STYLES) element.style.removeProperty(name);
    if (!element.style.length) element.removeAttribute('style');
  }
  return copy.innerHTML;
}

function post(isEdit = false) {
  const states = {};
  for (const name of COMMANDS) {
    states[name] = document.queryCommandState(name);
  }
  window.webkit.messageHandlers.editor.postMessage(JSON.stringify({
    html: messageHtml(),
    states: states,
    isEdit: isEdit,
  }));
}

function insertLink(url) {
  if (getSelection().isCollapsed) {
    const link = document.createElement('a');
    link.href = url;
    link.textContent = url;
    document.execCommand('insertHTML', false, link.outerHTML);
  } else {
    document.execCommand('createLink', false, url);
  }
  post();
}

// Pasted markup is rebuilt from a few plain tags with no attributes but a
// link's href. Anything the source styled -- a <p> margin, a dark-theme text
// colour, a font -- would reach recipients looking unlike it does here.
const KEPT = new RegExp(
  '^(A|B|STRONG|I|EM|U|S|STRIKE|BR|DIV|UL|OL|LI|BLOCKQUOTE|PRE|CODE|' +
  'TABLE|THEAD|TBODY|TR|TH|TD)$'
);
const DROPPED = /^(SCRIPT|STYLE|META|TITLE|LINK|IMG|svg|IFRAME|OBJECT)$/;
const PARAGRAPH = /^(P|H[1-6])$/;

function wrapContents(element, tag) {
  const wrapper = element.ownerDocument.createElement(tag);
  wrapper.append(...element.childNodes);
  element.append(wrapper);
}

function tidy(element) {
  const tag = element.tagName;
  // WebKit's marker for "the copy ended at a line break", not a line itself.
  if (DROPPED.test(tag) || element.className === 'Apple-interchange-newline') {
    element.remove();
    return;
  }
  // Rich-text editors mark formatting with inline styles on a <span>, and
  // Google Docs wraps a whole paste in <b style="font-weight:normal">.
  const style = element.style;
  const isBoldTag = tag === 'B' || tag === 'STRONG';
  if (isBoldTag && /normal|400/.test(style.fontWeight)) {
    element.replaceWith(...element.childNodes);
    return;
  }
  const decoration = style.textDecoration;
  if (!isBoldTag && /bold|[6-9]00/.test(style.fontWeight)) {
    wrapContents(element, 'B');
  }
  if (style.fontStyle === 'italic' && !/^(I|EM)$/.test(tag)) {
    wrapContents(element, 'I');
  }
  if (/underline/.test(decoration) && tag !== 'U') wrapContents(element, 'U');
  if (/line-through/.test(decoration) && tag !== 'S') wrapContents(element, 'S');

  if (PARAGRAPH.test(tag)) {
    // A paragraph's gap is a margin every client draws differently, so it
    // becomes a <div>, and the gap an empty line the writer can see and delete.
    const next = element.nextElementSibling;
    if (/^H/.test(tag)) wrapContents(element, 'B');
    if (element.parentElement && element.parentElement.closest('li')) {
      if (next) element.after(element.ownerDocument.createElement('br'));
      element.replaceWith(...element.childNodes);
      return;
    }
    const line = element.ownerDocument.createElement('div');
    line.append(...element.childNodes);
    element.replaceWith(line);
    if (next && PARAGRAPH.test(next.tagName)) {
      const blank = element.ownerDocument.createElement('div');
      blank.append(element.ownerDocument.createElement('br'));
      line.after(blank);
    }
    return;
  }
  const href = element.getAttribute('href') || '';
  if (!KEPT.test(tag) || (tag === 'A' && !/^(https?|mailto):/i.test(href))) {
    element.replaceWith(...element.childNodes);
    return;
  }
  for (const attribute of Array.from(element.attributes)) {
    if (attribute.name !== 'href') element.removeAttribute(attribute.name);
  }
}

function clean(parent) {
  for (const node of Array.from(parent.childNodes)) {
    if (node.nodeType === Node.ELEMENT_NODE) {
      clean(node);
      tidy(node);
    } else if (node.nodeType !== Node.TEXT_NODE) {
      node.remove();
    }
  }
}

document.addEventListener('paste', (event) => {
  const html = event.clipboardData.getData('text/html');
  if (!html) return;
  event.preventDefault();
  const body = new DOMParser().parseFromString(html, 'text/html').body;
  clean(body);
  document.execCommand('insertHTML', false, body.innerHTML);
});

// A dropped file would go in as a file:// link that no recipient can open.
document.addEventListener('drop', (event) => {
  if (event.dataTransfer.files.length) event.preventDefault();
});

document.addEventListener('input', () => post(true));
document.addEventListener('selectionchange', () => post());

// <div> separators inherit no margin, so a sent message keeps the spacing it
// was typed with even in clients that apply their own stylesheet.
document.execCommand('defaultParagraphSeparator', false, 'div');
document.body.focus();

const first = document.body.firstChild;
if (first) {
  const range = document.createRange();
  range.setStart(first, 0);
  range.collapse(true);
  getSelection().removeAllRanges();
  getSelection().addRange(range);
}
"""

# Every message opens on an empty line to type on, above any signature or quote.
_BLANK_LINE = "<div><br></div>"


class _AddressSuggestions:
    """A drop-down of known addresses under one recipient row.

    Gtk.EntryCompletion only attaches to a Gtk.Entry, and an Adw.EntryRow is a
    list box row, so the popover is driven by hand.
    """

    def __init__(self, row: Adw.EntryRow, addresses: list[str]) -> None:
        self._row = row
        self._addresses = addresses
        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.BROWSE)
        # autohide would steal focus from the entry the moment it pops up.
        self._popover = Gtk.Popover(
            child=self._list,
            autohide=False,
            has_arrow=False,
            position=Gtk.PositionType.BOTTOM,
        )
        self._popover.set_parent(row)

        row.connect("changed", self._on_changed)
        row.connect("destroy", lambda *_: self._popover.unparent())
        self._list.connect("row-activated", self._on_row_activated)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key_pressed)
        row.add_controller(keys)

    def _on_changed(self, _row: Adw.EntryRow) -> None:
        matches = compose.suggest_addresses(self._row.get_text(), self._addresses)
        self._list.remove_all()
        for address in matches:
            self._list.append(
                Gtk.Label(
                    label=address,
                    xalign=0,
                    ellipsize=Pango.EllipsizeMode.END,
                    max_width_chars=40,
                    margin_top=8,
                    margin_bottom=8,
                    margin_start=12,
                    margin_end=12,
                )
            )

        if matches:
            self._list.select_row(self._list.get_row_at_index(0))
            # Line the drop-down up with the row it belongs to.
            self._popover.set_size_request(self._row.get_width(), -1)
            self._popover.popup()
        else:
            self._popover.popdown()

    def _on_row_activated(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        label = row.get_child()
        assert isinstance(label, Gtk.Label)
        # The trailing ", " leaves nothing being typed, so the popover closes
        # itself on the resulting "changed" -- and shows how to add another.
        self._row.set_text(
            compose.replace_last_address(self._row.get_text(), label.get_label())
        )
        self._row.set_position(-1)

    def _on_key_pressed(
        self, _controller: Gtk.EventControllerKey, keyval: int, _code: int, _state: int
    ) -> bool:
        if not self._popover.get_visible():
            return False

        if keyval == Gdk.KEY_Escape:
            self._popover.popdown()
        elif keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_Tab):
            selected = self._list.get_selected_row()
            if selected is None:
                return False
            self._on_row_activated(self._list, selected)
        elif keyval in (Gdk.KEY_Down, Gdk.KEY_Up):
            self._move_selection(1 if keyval == Gdk.KEY_Down else -1)
        else:
            return False
        return True

    def _move_selection(self, step: int) -> None:
        selected = self._list.get_selected_row()
        index = (selected.get_index() if selected else 0) + step
        row = self._list.get_row_at_index(index)
        if row is not None:
            self._list.select_row(row)


# Long enough to read as a slide, short enough not to sit in the way of typing.
_REVEAL_DURATION_MS = 250

_FORMAT_COMMANDS = {
    "bold_button": "bold",
    "italic_button": "italic",
    "underline_button": "underline",
    "strike_button": "strikeThrough",
    "bullets_button": "insertUnorderedList",
    "numbers_button": "insertOrderedList",
}

_HISTORY_COMMANDS = {
    "undo": WebKit.EDITING_COMMAND_UNDO,
    "redo": WebKit.EDITING_COMMAND_REDO,
}

# Caught on the editor's container in the capture phase: WebKit takes every
# key in its own capture phase, so a controller on the WebView itself never
# sees one. Ctrl+Z in an address row stays that row's own undo.
_EDITOR_SHORTCUTS = {
    "<Control>z": "composer.undo",
    "<Control><Shift>z|<Control>y": "composer.redo",
    "<Control>b": "composer.bold",
    "<Control>i": "composer.italic",
    "<Control>u": "composer.underline",
    "<Control>k": "composer.link",
}

_WINDOW_SHORTCUTS = {
    "<Control>Return|<Control>KP_Enter": "composer.send",
}


@Gtk.Template(resource_path="/in/gxanshu/postcard/ui/composer-window.ui")
class PostcardComposerWindow(Adw.Window):
    __gtype_name__ = "PostcardComposerWindow"

    send_stack: Gtk.Stack = Gtk.Template.Child()
    send_spinner: Adw.Spinner = Gtk.Template.Child()
    cancel_button: Gtk.Button = Gtk.Template.Child()
    form: Gtk.Box = Gtk.Template.Child()
    format_bar: Gtk.Box = Gtk.Template.Child()
    from_row: Adw.ComboRow = Gtk.Template.Child()
    to_row: Adw.EntryRow = Gtk.Template.Child()
    extra_recipients_button: Gtk.ToggleButton = Gtk.Template.Child()
    extra_recipients_row: Gtk.ListBoxRow = Gtk.Template.Child()
    extra_recipients_clip: Gtk.ScrolledWindow = Gtk.Template.Child()
    extra_recipients_box: Gtk.Box = Gtk.Template.Child()
    cc_row: Adw.EntryRow = Gtk.Template.Child()
    bcc_row: Adw.EntryRow = Gtk.Template.Child()
    subject_row: Adw.EntryRow = Gtk.Template.Child()
    body_container: Gtk.Box = Gtk.Template.Child()
    bold_button: Gtk.ToggleButton = Gtk.Template.Child()
    italic_button: Gtk.ToggleButton = Gtk.Template.Child()
    underline_button: Gtk.ToggleButton = Gtk.Template.Child()
    strike_button: Gtk.ToggleButton = Gtk.Template.Child()
    bullets_button: Gtk.ToggleButton = Gtk.Template.Child()
    numbers_button: Gtk.ToggleButton = Gtk.Template.Child()
    attach_button: Gtk.Button = Gtk.Template.Child()
    attachments_group: Gtk.Box = Gtk.Template.Child()
    attachments_list: Gtk.ListBox = Gtk.Template.Child()

    __gsignals__ = {
        # The text of a failure to toast, since this window is gone by then;
        # "" when the message was sent or saved.
        "finished": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(
        self,
        app: Gtk.Application | None,
        db: Database,
        account: Account,
        to: str = "",
        subject: str = "",
        body: str = "",
        cc: str = "",
        bcc: str = "",
    ) -> None:
        super().__init__(application=app)
        self._db = db
        self._account = account
        self._attachments: list[Attachment] = []
        self._body_html = _BLANK_LINE + body
        self._syncing = False
        self._is_sending = False
        self._is_editor_loaded = False
        # Whether closing should keep a draft: an untouched reply or a new
        # message holding only its signature is not one.
        self._is_edited = False

        self._format_buttons = {
            command: getattr(self, name) for name, command in _FORMAT_COMMANDS.items()
        }

        self._build_actions()
        self._build_from_row(account)
        self.to_row.set_text(to)
        self.cc_row.set_text(cc)
        self.bcc_row.set_text(bcc)
        self.subject_row.set_text(subject)
        # Cc and Bcc stay folded away until asked for, or until a reply-all
        # arrives with them already filled in.
        self.extra_recipients_button.set_active(self._has_extra_recipients())
        self._build_extra_recipients_animation()
        self._sync_extra_recipients(is_animated=False)
        self._build_editor()

        self.extra_recipients_button.connect(
            "toggled", self._on_extra_recipients_toggled
        )
        self.connect("close-request", self._on_close_request)
        self.attach_button.connect("clicked", self._on_attach_clicked)
        self.add_controller(_shortcut_controller(_WINDOW_SHORTCUTS))

        for command, button in self._format_buttons.items():
            button.connect("toggled", self._on_format_toggled, command)

        for row in (self.to_row, self.cc_row, self.bcc_row, self.subject_row):
            row.connect("changed", self._update_send_sensitivity)
            row.connect("changed", self._mark_edited)
        self._update_send_sensitivity()

        known = db.contact_addresses()
        self._suggestions = [
            _AddressSuggestions(row, known)
            for row in (self.to_row, self.cc_row, self.bcc_row)
        ]

        # A new message starts at its first field; a reply, already addressed,
        # starts in the body.
        (self._webview if to else self.to_row).grab_focus()

    def _build_actions(self) -> None:
        callbacks = {
            "send": self._send,
            "link": self._ask_for_link,
            "bold": lambda: self._toggle(self.bold_button),
            "italic": lambda: self._toggle(self.italic_button),
            "underline": lambda: self._toggle(self.underline_button),
        } | {
            name: lambda command=command: self._webview.execute_editing_command(command)
            for name, command in _HISTORY_COMMANDS.items()
        }
        group = Gio.SimpleActionGroup()
        self._actions: dict[str, Gio.SimpleAction] = {}
        for name, callback in callbacks.items():
            action = Gio.SimpleAction(name=name)
            action.connect("activate", lambda *_args, callback=callback: callback())
            group.add_action(action)
            self._actions[name] = action
        self.insert_action_group("composer", group)
        # Nothing to undo yet; the editor turns these on as it fills its history.
        for name in _HISTORY_COMMANDS:
            self._actions[name].set_enabled(False)

    # Gtk.Revealer is the obvious fit here, but its transition is ease-out in
    # both directions: a collapse jumps away and then crawls the last few
    # pixels. An Adw.TimedAnimation over the row height eases in and out
    # symmetrically, and lands exactly on 0 rather than on the row minimum.
    def _build_extra_recipients_animation(self) -> None:
        self._extra_recipients_animation = Adw.TimedAnimation.new(
            self.extra_recipients_row,
            0,
            0,
            _REVEAL_DURATION_MS,
            Adw.CallbackAnimationTarget.new(self._on_extra_recipients_value),
        )
        self._extra_recipients_animation.set_easing(Adw.Easing.EASE_IN_OUT_CUBIC)
        self._extra_recipients_animation.connect("done", self._on_extra_recipients_done)

    def _on_extra_recipients_toggled(self, button: Gtk.ToggleButton) -> None:
        # Folding away a filled Cc or Bcc would leave those recipients on the
        # message with nothing on screen to say so, so refuse and put the cursor
        # on the field that is in the way.
        # ponytail: the tooltip still reads "Hide"; keeping it honest means
        # following every keystroke in Cc and Bcc.
        if not button.get_active() and self._has_extra_recipients():
            button.set_active(True)
            return
        self._sync_extra_recipients()
        if button.get_active():
            self.cc_row.grab_focus()

    def _on_extra_recipients_value(self, value: float, *_args: object) -> None:
        self.extra_recipients_clip.set_size_request(-1, int(value))

    def _on_extra_recipients_done(self, *_args: object) -> None:
        if self.extra_recipients_button.get_active():
            # Back to following the rows, so a wrapped address or a larger font
            # is not clipped by the height the animation happened to end on.
            self.extra_recipients_clip.set_propagate_natural_height(True)
            self.extra_recipients_clip.set_size_request(-1, -1)
            return
        # Clipped-away rows still take Tab focus, so the collapsed row is only
        # really gone once it is hidden. At height 0 this costs no layout.
        self.extra_recipients_row.set_visible(False)

    def _has_extra_recipients(self) -> bool:
        return bool(self.cc_row.get_text().strip() or self.bcc_row.get_text().strip())

    def _sync_extra_recipients(self, *, is_animated: bool = True) -> None:
        is_shown = self.extra_recipients_button.get_active()
        self.extra_recipients_button.set_tooltip_text(
            _("Hide Cc and Bcc fields") if is_shown else _("Show Cc and Bcc fields")
        )
        if is_shown:
            self.extra_recipients_row.set_visible(True)

        if not is_animated:
            self.extra_recipients_clip.set_propagate_natural_height(is_shown)
            self.extra_recipients_clip.set_size_request(-1, -1 if is_shown else 0)
            self.extra_recipients_row.set_visible(is_shown)
            return

        # While the height is driven by hand the clip must not report the rows'
        # own height as its natural one, or there is nothing left to animate.
        self.extra_recipients_clip.set_propagate_natural_height(False)
        # measure() returns (minimum, natural, ...); unpacking it would bind
        # gettext's _ as a local and break the tooltip above.
        natural = self.extra_recipients_box.measure(Gtk.Orientation.VERTICAL, -1)[1]
        animation = self._extra_recipients_animation
        # Start from where the row actually is, so a click mid-animation turns
        # around from there instead of snapping back to the far end.
        animation.set_value_from(self.extra_recipients_clip.get_height())
        animation.set_value_to(natural if is_shown else 0)
        animation.play()

    # The account every send, draft and Sent copy belongs to. Picking another
    # here is the only way to change it once the composer is open.
    def _build_from_row(self, account: Account) -> None:
        self._accounts = self._db.accounts()
        self.from_row.set_model(
            Gtk.StringList(strings=[each.email for each in self._accounts])
        )
        ids = [each.id for each in self._accounts]
        self.from_row.set_selected(ids.index(account.id) if account.id in ids else 0)
        self.from_row.connect("notify::selected", self._on_from_changed)

    def _on_from_changed(self, *_args: object) -> None:
        self._account = self._accounts[self.from_row.get_selected()]

    def _build_editor(self) -> None:
        manager = WebKit.UserContentManager()
        manager.register_script_message_handler("editor", None)
        manager.connect("script-message-received::editor", self._on_editor_changed)
        manager.add_script(
            WebKit.UserScript.new(
                f"const COMMANDS = {json.dumps(list(self._format_buttons))};"
                + _EDITOR_SCRIPT,
                WebKit.UserContentInjectedFrames.TOP_FRAME,
                WebKit.UserScriptInjectionTime.END,
                None,
                None,
            )
        )

        self._webview = WebKit.WebView(
            user_content_manager=manager, hexpand=True, vexpand=True
        )
        self._webview.get_settings().set_auto_load_images(False)
        self._webview.connect("decide-policy", self._on_decide_policy)
        self.body_container.add_controller(_shortcut_controller(_EDITOR_SHORTCUTS))
        context = self._webview.get_context()
        context.set_spell_checking_enabled(True)
        context.set_spell_checking_languages(_spelling_languages())
        # Transparent, so the Adwaita "card" behind it supplies the background
        # and the editor tracks the theme without hardcoding its colours.
        self._webview.set_background_color(Gdk.RGBA(red=0, green=0, blue=0, alpha=0))
        family, size = _gtk_font()
        self._webview.load_html(
            _EDITOR_PAGE.format(family=family, size=size, body=self._body_html),
            None,
        )
        self.body_container.append(self._webview)

    def _on_decide_policy(
        self,
        _webview: WebKit.WebView,
        decision: WebKit.PolicyDecision,
        decision_type: WebKit.PolicyDecisionType,
    ) -> bool:
        if decision_type == WebKit.PolicyDecisionType.RESPONSE:
            return False
        # The first navigation is load_html putting the editor up. It is never
        # left after that: a followed link would take the message with it.
        if self._is_editor_loaded:
            decision.ignore()
            return True
        self._is_editor_loaded = True
        return False

    def _on_editor_changed(
        self, _manager: WebKit.UserContentManager, value: JavaScriptCore.Value
    ) -> None:
        payload = json.loads(value.to_string())
        self._body_html = payload["html"]
        if payload["isEdit"]:
            self._mark_edited()

        self._syncing = True
        for command, button in self._format_buttons.items():
            button.set_active(payload["states"].get(command, False))
        self._syncing = False

        self._update_send_sensitivity()
        for name, command in _HISTORY_COMMANDS.items():
            self._webview.can_execute_editing_command(
                command, None, self._on_history_checked, name
            )

    def _on_history_checked(
        self, webview: WebKit.WebView, result: Gio.AsyncResult, name: str
    ) -> None:
        self._actions[name].set_enabled(
            webview.can_execute_editing_command_finish(result)
        )

    def _mark_edited(self, *_args: object) -> None:
        self._is_edited = True

    def _run_script(self, script: str) -> None:
        self._webview.evaluate_javascript(script, -1, None, None, None)

    def _toggle(self, button: Gtk.ToggleButton) -> None:
        button.set_active(not button.get_active())

    def _on_format_toggled(self, _button: Gtk.ToggleButton, command: str) -> None:
        if self._syncing:
            return
        # post() as well: a toggle at a bare caret changes no text, so the
        # editor would not report the new state until the next keystroke.
        self._run_script(f"document.execCommand({json.dumps(command)}); post();")
        self._webview.grab_focus()

    def _ask_for_link(self) -> None:
        entry = Gtk.Entry(placeholder_text="https://", activates_default=True)
        dialog = Adw.AlertDialog(heading=_("Insert Link"), extra_child=entry)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("insert", _("Insert"))
        dialog.set_response_appearance("insert", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("insert")
        dialog.connect("response", self._on_link_response, entry)
        dialog.present(self)

    def _on_link_response(
        self, _dialog: Adw.AlertDialog, response: str, entry: Gtk.Entry
    ) -> None:
        url = entry.get_text().strip()
        self._webview.grab_focus()
        if response != "insert" or not url:
            return
        # "example.com" would be a link relative to nothing in the recipient's
        # client.
        if not url.lower().startswith(("http://", "https://", "mailto:")):
            url = f"mailto:{url}" if "@" in url else f"https://{url}"
        self._run_script(f"insertLink({json.dumps(url)})")

    def _update_send_sensitivity(self, *_args: object) -> None:
        has_recipient = bool(self._to_addrs() or self._cc_addrs() or self._bcc_addrs())
        self._actions["send"].set_enabled(
            has_recipient and bool(self.subject_row.get_text().strip())
        )

    def _preview_text(self) -> str:
        return compose.html_to_text(self._body_html)

    @staticmethod
    def _parse_addrs(text: str) -> list[str]:
        return [addr.strip() for addr in text.split(",") if addr.strip()]

    def _to_addrs(self) -> list[str]:
        return self._parse_addrs(self.to_row.get_text())

    def _cc_addrs(self) -> list[str]:
        return self._parse_addrs(self.cc_row.get_text())

    def _bcc_addrs(self) -> list[str]:
        return self._parse_addrs(self.bcc_row.get_text())

    def _recipients_display(self) -> str:
        """A human-readable stand-in for the "sender" column of the Outbox/Drafts
        list, which otherwise has no concept of outgoing recipients."""
        return (
            self.to_row.get_text().strip()
            or self.cc_row.get_text().strip()
            or self.bcc_row.get_text().strip()
            or _("(no recipient)")
        )

    def _has_content(self) -> bool:
        return bool(
            self.to_row.get_text().strip()
            or self.cc_row.get_text().strip()
            or self.bcc_row.get_text().strip()
            or self.subject_row.get_text().strip()
            or self._preview_text()
        )

    def _on_attach_clicked(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileDialog()
        dialog.open_multiple(self, None, self._on_attach_dialog_done)

    def _on_attach_dialog_done(
        self, dialog: Gtk.FileDialog, result: Gio.AsyncResult
    ) -> None:
        try:
            files = dialog.open_multiple_finish(result)
        except GLib.Error:
            return  # user cancelled the dialog

        # A Gio.ListModel of Gio.File, which the stubs only know as GObjects.
        for file in files or []:
            self._attach(cast("Gio.File", file))

    def _attach(self, file: Gio.File) -> None:
        try:
            _ok, content, _etag = file.load_contents(None)
        except GLib.Error as error:
            logger.warning("could not attach %s: %s", file.get_path(), error.message)
            return

        filename = file.get_basename() or "attachment"
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        attachment = Attachment(
            filename=filename, mime_type=mime_type, content=bytes(content)
        )
        self._attachments.append(attachment)
        self._add_attachment_row(attachment)
        self._mark_edited()

    def _add_attachment_row(self, attachment: Attachment) -> None:
        row = Adw.ActionRow(title=attachment.filename)
        remove_button = Gtk.Button(
            icon_name="window-close-symbolic",
            valign=Gtk.Align.CENTER,
            tooltip_text=_("Remove Attachment"),
        )
        remove_button.add_css_class("flat")
        remove_button.connect(
            "clicked", self._on_remove_attachment_clicked, attachment, row
        )
        row.add_suffix(remove_button)
        self.attachments_list.append(row)
        self.attachments_group.set_visible(True)

    def _on_remove_attachment_clicked(
        self, _button: Gtk.Button, attachment: Attachment, row: Adw.ActionRow
    ) -> None:
        self._attachments.remove(attachment)
        self.attachments_list.remove(row)
        self.attachments_group.set_visible(bool(self._attachments))

    # Cancel is window.close, so a draft is kept however the window goes.
    def _on_close_request(self, _window: Adw.Window) -> bool:
        if self._is_sending:
            # The message is already in the Outbox and its send carries on; the
            # window only hides, so the result still has somewhere to land.
            self.set_visible(False)
            return True
        if self._is_edited and self._has_content():
            self._save_draft()
        return False

    def _save_draft(self) -> None:
        folder = mail_sync.drafts_folder(self._db, self._account.id)
        msg = compose.build_mime_message(
            self._account.email,
            self._to_addrs(),
            self._cc_addrs(),
            self.subject_row.get_text().strip(),
            self._body_html,
            self._attachments,
        )
        recipient, recipient_address = mail_sync.first_recipient(self.to_row.get_text())
        row = self._db.save_email(
            folder.id,
            sender=self._recipients_display(),
            recipient=recipient,
            recipient_address=recipient_address,
            subject=self.subject_row.get_text().strip() or NO_SUBJECT,
            preview=self._preview_text()[:100],
            date=_now(),
            is_unread=False,
        )
        self._db.save_raw_message(row.id, msg.as_bytes())
        self.emit("finished", "")

    def _send(self) -> None:
        if self._is_sending:
            return
        to_addrs = self._to_addrs()
        cc_addrs = self._cc_addrs()
        bcc_addrs = self._bcc_addrs()
        subject = self.subject_row.get_text().strip()

        # Bcc is never written to a received message, so this is the only
        # place a Bcc'd address can be learned.
        self._db.save_contacts(getaddresses(to_addrs + cc_addrs + bcc_addrs))

        msg = compose.build_mime_message(
            self._account.email,
            to_addrs,
            cc_addrs,
            subject,
            self._body_html,
            self._attachments,
        )
        raw = msg.as_bytes()

        # The SMTP envelope recipients, unlike the message's own To/Cc
        # headers, also carry Bcc addresses -- they must never appear in the
        # message itself, only in the delivery instructions given to the server.
        recipients = to_addrs + cc_addrs + bcc_addrs

        # Save to Outbox before attempting to send -- a crash mid-send can
        # then never lose the message.
        outbox = self._db.get_or_create_folder(
            self._account.id,
            mail_sync.OUTBOX_FOLDER,
            mail_sync.icon_for_folder(mail_sync.OUTBOX_FOLDER),
        )
        recipient, recipient_address = mail_sync.first_recipient(self.to_row.get_text())
        row = self._db.save_email(
            outbox.id,
            sender=self._recipients_display(),
            recipient=recipient,
            recipient_address=recipient_address,
            subject=subject,
            preview=self._preview_text()[:100],
            date=_now(),
            is_unread=False,
        )
        self._db.save_raw_message(row.id, raw)

        # The button stays lit, spinning, rather than greyed out like the rest.
        self._is_sending = True
        self.send_stack.set_visible_child(self.send_spinner)
        # Edits made now would not be in the message already on its way.
        for widget in (self.cancel_button, self.form, self.format_bar):
            widget.set_sensitive(False)

        thread = threading.Thread(
            target=self._send_worker,
            args=(self._account, row.id, subject, recipients, raw),
            daemon=True,
        )
        thread.start()

    # Runs on the worker thread: network only, no Gtk/database access.
    def _send_worker(
        self,
        account: Account,
        email_id: int,
        subject: str,
        recipients: list[str],
        raw: bytes,
    ) -> None:
        credential = secrets.credential_for(account)
        if credential is None:
            logger.warning("could not sign in to account %s", account.email)
            GLib.idle_add(self._on_send_failed, _("Could not sign in to this account."))
            return

        try:
            mail_sync.send_message(account, credential, account.email, recipients, raw)
        except Exception as error:
            logger.exception(
                "could not send %r to %s via %s (account %s)",
                subject,
                ", ".join(recipients),
                account.smtp_host,
                account.email,
            )
            _is_auth_failure, message = errors.classify(error, account.smtp_host)
            GLib.idle_add(self._on_send_failed, message)
            return
        GLib.idle_add(self._on_send_done, account, email_id, subject, raw)

    # Back on the main thread: safe to touch the database and widgets.
    def _on_send_done(
        self, account: Account, email_id: int, subject: str, raw: bytes
    ) -> bool:
        self._db.delete_email(email_id)
        sent = mail_sync.sent_folder(self._db, account.id)
        recipient, recipient_address = mail_sync.first_recipient(self.to_row.get_text())
        row = self._db.save_email(
            sent.id,
            sender=account.email,
            recipient=recipient,
            recipient_address=recipient_address,
            subject=subject,
            preview=subject,
            date=_now(),
            is_unread=False,
        )
        self._db.save_raw_message(row.id, raw)
        self.emit("finished", "")
        # destroy, not close: there is no draft to keep of a message sent.
        self.destroy()
        return False

    def _on_send_failed(self, message: str) -> bool:
        self.emit(
            "finished", _("Couldn't send: {msg}. Saved to Outbox.").format(msg=message)
        )
        self.destroy()
        return False


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _shortcut_controller(shortcuts: dict[str, str]) -> Gtk.ShortcutController:
    controller = Gtk.ShortcutController(propagation_phase=Gtk.PropagationPhase.CAPTURE)
    for trigger, action in shortcuts.items():
        controller.add_shortcut(
            Gtk.Shortcut(
                trigger=Gtk.ShortcutTrigger.parse_string(trigger),
                action=Gtk.NamedAction.new(action),
            )
        )
    return controller


def _spelling_languages() -> list[str]:
    """The locale's languages as dictionary names: en_US.UTF-8 -> en_US, en."""
    return [
        name
        for name in GLib.get_language_names()
        if name != "C" and "." not in name and "@" not in name
    ]


def _gtk_font() -> tuple[str, str]:
    """Split GTK's "Cantarell 11" style font description into family and size."""
    settings = Gtk.Settings.get_default()
    description = (settings and settings.get_property("gtk-font-name")) or "Sans 11"
    family, _, size = description.rpartition(" ")
    if not size.isdigit():
        return description, "11"
    return family, size


def signature_html(settings: Gio.Settings) -> str:
    """The signature block new messages start with, or "" when it is off."""
    if not settings.get_boolean("signature-enabled"):
        return ""
    return compose.signature_block(
        settings.get_string("signature-text").strip(),
        is_html=settings.get_boolean("signature-is-html"),
    )


# Build a composer from a mailto: URI. Shared by the main window and by a
# mailto: launch, which opens the composer with no main window at all.
def composer_for_mailto(
    app: Gtk.Application | None,
    db: Database,
    account: Account,
    settings: Gio.Settings,
    uri: str,
) -> PostcardComposerWindow:
    draft = compose.parse_mailto(uri)
    return PostcardComposerWindow(
        app,
        db,
        account,
        to=draft.to,
        subject=draft.subject,
        body=draft.body_html or signature_html(settings),
        cc=draft.cc,
        bcc=draft.bcc,
    )
