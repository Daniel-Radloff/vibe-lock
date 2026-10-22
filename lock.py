#!/usr/bin/env python3
"""
notelock - a tiny screen locker (Wayland/sway or X11) where colleagues can
leave you notes.

On Wayland it uses the ext-session-lock protocol via the gtk-session-lock
library (a real lock: if notelock crashes, sway stays locked).
On X11 it falls back to grabbing keyboard + pointer, like i3lock.

  * You unlock with your normal Linux password (checked through PAM).
  * Anyone else can type a name + short note and press Enter / "Leave note".
  * Notes are saved to ~/.local/share/notelock/notes.json and shown on the
    lock screen as sticky notes. When you unlock you get a desktop
    notification with how many notes arrived while you were away.

Dependencies (Debian/Ubuntu):
    sudo apt install python3-gi gir1.2-gtk-3.0 python3-pam libnotify-bin
(Arch: python-gobject gtk3 python-pam libnotify)
(If python3-pam isn't packaged: pip install --user python-pam)
Wayland/sway ALSO needs gtk-session-lock (with its GtkSessionLock typelib):
    https://github.com/Cu3PO42/gtk-session-lock   (AUR: gtk-session-lock)

Usage:
    notelock.py --test     # safe mode: no input grab, Esc quits. TRY THIS FIRST
    notelock.py            # real lock
    notelock.py --read     # print saved notes
    notelock.py --clear    # delete saved notes

sway config:
    bindsym $mod+l exec /path/to/notelock.py
    exec swayidle -w timeout 300 '/path/to/notelock.py' \
                     before-sleep '/path/to/notelock.py'
(i3: bindsym $mod+l exec --no-startup-id /path/to/notelock.py)

Recovery on Wayland: if notelock dies while locked, sway deliberately stays
locked (screen goes red/blank). Switch to a TTY (Ctrl+Alt+F2), log in, and run:
    WAYLAND_DISPLAY=wayland-1 XDG_RUNTIME_DIR=/run/user/$(id -u) /path/to/notelock.py
then unlock with your password. (`ls /run/user/$(id -u)` shows the socket name.)
Test mode (--test) never locks anything, and Esc quits it.
"""
import getpass
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

PAM_SERVICE = os.environ.get("NOTELOCK_PAM_SERVICE", "login")
DATA_DIR = os.path.join(
    os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "notelock"
)
NOTES_FILE = os.path.join(DATA_DIR, "notes.json")
MAX_NOTE_LEN = 140
MAX_NAME_LEN = 24
MAX_NOTES = 30          # oldest notes are dropped beyond this
SHOW_NOTES = 8          # how many sticky notes to show on screen
HEADLINE = "Away from my desk"
SUBLINE = "Leave me a note below"


# ---------------------------------------------------------------- notes store
def load_notes():
    try:
        with open(NOTES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [n for n in data if isinstance(n, dict) and "text" in n]
    except (OSError, ValueError):
        pass
    return []


def save_notes(notes):
    os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix="notes-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(notes[-MAX_NOTES:], f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, NOTES_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def add_note(name, text):
    name = " ".join(name.split())[:MAX_NAME_LEN] or "Someone"
    text = " ".join(text.split())[:MAX_NOTE_LEN]
    if not text:
        return None
    note = {"name": name, "text": text, "ts": int(time.time())}
    notes = load_notes()
    notes.append(note)
    save_notes(notes)
    return note


def cli_read():
    notes = load_notes()
    if not notes:
        print("No notes.")
        return
    for n in notes:
        when = time.strftime("%a %H:%M", time.localtime(n.get("ts", 0)))
        print(f"[{when}] {n.get('name', '?')}: {n['text']}")


# ------------------------------------------------------------------------ PAM
def check_password(user, password):
    try:
        import pam  # python-pam
    except ImportError:
        print("notelock: python-pam is not installed", file=sys.stderr)
        return False
    try:
        return bool(pam.authenticate(user, password, service=PAM_SERVICE))
    except Exception as exc:  # never crash into an unlocked state
        print(f"notelock: PAM error: {exc}", file=sys.stderr)
        return False


# ------------------------------------------------------------------------- UI
def run_lock(test_mode):
    # On Wayland the session-lock library has to be loaded before libwayland,
    # so re-exec ourselves once with LD_PRELOAD if it is available.
    wayland_env = bool(os.environ.get("WAYLAND_DISPLAY")) and (
        os.environ.get("GDK_BACKEND") != "x11"
    )
    if wayland_env and not test_mode and not os.environ.get("NOTELOCK_REEXEC"):
        import ctypes.util

        libname = ctypes.util.find_library("gtk-session-lock")
        if libname:
            env = dict(os.environ)
            env["NOTELOCK_REEXEC"] = "1"
            env["LD_PRELOAD"] = (libname + " " + env.get("LD_PRELOAD", "")).strip()
            os.execvpe(sys.executable, [sys.executable] + sys.argv, env)

    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GLib, Gtk

    display = Gdk.Display.get_default()
    if display is None:
        sys.exit("notelock: could not open a display")
    wayland = display.__gtype__.name == "GdkWaylandDisplay"

    session_lock = None
    if wayland and not test_mode:
        try:
            gi.require_version("GtkSessionLock", "0.1")
            from gi.repository import GtkSessionLock
        except (ValueError, ImportError):
            sys.exit(
                "notelock: the gtk-session-lock library is missing.\n"
                "Install it (https://github.com/Cu3PO42/gtk-session-lock) and retry."
            )
        if not GtkSessionLock.is_supported():
            sys.exit("notelock: this compositor doesn't support ext-session-lock")

        def lock_gone(*_):
            # compositor ended the lock (or refused it)
            Gtk.main_quit()

        session_lock = GtkSessionLock.Instance.new()
        session_lock.connect("failed", lock_gone)
        session_lock.connect("finished", lock_gone)
        session_lock.lock()

    user = getpass.getuser()
    session_start = int(time.time())

    css = b"""
    window { background-color: #10131a; }
    .blank { background-color: #000000; }
    .clock { color: #f2f2f2; font-size: 110px; font-weight: 200; }
    .date  { color: #9aa4b2; font-size: 22px; }
    .headline { color: #f2f2f2; font-size: 26px; font-weight: 600; }
    .subline { color: #9aa4b2; font-size: 15px; }
    .status { color: #ff6b6b; font-size: 14px; min-height: 18px; }
    .ok { color: #7bd88f; }
    .sticky { background-color: #fff3a3; border-radius: 4px; padding: 10px 12px; }
    .sticky label { color: #2b2b2b; }
    .sticky .who { font-weight: bold; font-size: 13px; }
    .sticky .txt { font-size: 15px; }
    .sticky .when { font-size: 11px; color: #6b6b6b; }
    entry { min-height: 34px; font-size: 15px; }
    .pwbox entry { background: #1b2030; color: #dfe6f0; border: 1px solid #2c3550; }
    button { min-height: 34px; }
    """
    provider = Gtk.CssProvider()
    provider.load_from_data(css)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )

    screen = Gdk.Screen.get_default()
    n_monitors = display.get_n_monitors()
    primary = display.get_primary_monitor() or display.get_monitor(0)
    primary_idx = 0
    for i in range(n_monitors):
        if display.get_monitor(i) == primary:
            primary_idx = i

    def new_window(idx, blank):
        w = Gtk.Window()
        w.set_decorated(False)
        w.set_keep_above(True)
        w.set_skip_taskbar_hint(True)
        w.set_skip_pager_hint(True)
        if blank:
            w.get_style_context().add_class("blank")
        if session_lock is not None:
            session_lock.assign_window_to_monitor(w, display.get_monitor(idx))
        elif test_mode:
            m = display.get_monitor(idx).get_geometry()
            w.set_default_size(min(m.width, 1100), min(m.height, 800))
            w.move(m.x + 40, m.y + 40)
        else:
            w.fullscreen_on_monitor(screen, idx)
        w.connect("delete-event", lambda *_: True)  # ignore close requests
        return w

    main = new_window(primary_idx, blank=False)
    blanks = [
        new_window(i, blank=True) for i in range(n_monitors) if i != primary_idx
    ]

    # ---- layout
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    outer.set_halign(Gtk.Align.CENTER)
    outer.set_valign(Gtk.Align.CENTER)
    main.add(outer)

    clock = Gtk.Label()
    clock.get_style_context().add_class("clock")
    date_lbl = Gtk.Label()
    date_lbl.get_style_context().add_class("date")
    outer.pack_start(clock, False, False, 0)
    outer.pack_start(date_lbl, False, False, 0)

    head = Gtk.Label(label=HEADLINE)
    head.get_style_context().add_class("headline")
    head.set_margin_top(24)
    sub = Gtk.Label(label=SUBLINE)
    sub.get_style_context().add_class("subline")
    outer.pack_start(head, False, False, 0)
    outer.pack_start(sub, False, False, 0)

    notes_box = Gtk.FlowBox()
    notes_box.set_selection_mode(Gtk.SelectionMode.NONE)
    notes_box.set_max_children_per_line(4)
    notes_box.set_min_children_per_line(1)
    notes_box.set_homogeneous(False)
    notes_box.set_row_spacing(10)
    notes_box.set_column_spacing(10)
    notes_box.set_margin_top(10)
    notes_box.set_size_request(760, -1)
    outer.pack_start(notes_box, False, False, 0)

    # note entry row
    note_row = Gtk.Box(spacing=8)
    name_entry = Gtk.Entry()
    name_entry.set_placeholder_text("Your name")
    name_entry.set_max_length(MAX_NAME_LEN)
    name_entry.set_width_chars(16)
    note_entry = Gtk.Entry()
    note_entry.set_placeholder_text("Your note for me...")
    note_entry.set_max_length(MAX_NOTE_LEN)
    note_entry.set_width_chars(46)
    send_btn = Gtk.Button(label="Leave note")
    note_row.pack_start(name_entry, False, False, 0)
    note_row.pack_start(note_entry, True, True, 0)
    note_row.pack_start(send_btn, False, False, 0)
    note_row.set_margin_top(14)
    outer.pack_start(note_row, False, False, 0)

    note_status = Gtk.Label()
    note_status.get_style_context().add_class("status")
    outer.pack_start(note_status, False, False, 0)

    # password row (kept small and at the bottom: it's for the owner)
    pw_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    pw_box.get_style_context().add_class("pwbox")
    pw_box.set_margin_top(30)
    pw_entry = Gtk.Entry()
    pw_entry.set_visibility(False)
    pw_entry.set_placeholder_text(f"{user}: password to unlock")
    pw_entry.set_width_chars(30)
    pw_entry.set_halign(Gtk.Align.CENTER)
    pw_box.pack_start(pw_entry, False, False, 0)
    pw_status = Gtk.Label()
    pw_status.get_style_context().add_class("status")
    pw_box.pack_start(pw_status, False, False, 4)
    outer.pack_start(pw_box, False, False, 0)

    # ---- behaviour
    def rebuild_notes():
        for child in notes_box.get_children():
            notes_box.remove(child)
        for n in load_notes()[-SHOW_NOTES:]:
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            card.get_style_context().add_class("sticky")
            who = Gtk.Label(label=str(n.get("name", "Someone")), xalign=0)
            who.get_style_context().add_class("who")
            txt = Gtk.Label(label=str(n["text"]), xalign=0)
            txt.get_style_context().add_class("txt")
            txt.set_line_wrap(True)
            txt.set_max_width_chars(26)
            when = Gtk.Label(
                label=time.strftime("%a %H:%M", time.localtime(n.get("ts", 0))),
                xalign=0,
            )
            when.get_style_context().add_class("when")
            for w in (who, txt, when):
                card.pack_start(w, False, False, 0)
            notes_box.add(card)
        notes_box.show_all()

    def tick():
        clock.set_text(time.strftime("%H:%M"))
        date_lbl.set_text(time.strftime("%A, %d %B"))
        return True

    def submit_note(*_):
        text = note_entry.get_text()
        if not text.strip():
            return
        try:
            add_note(name_entry.get_text(), text)
        except OSError:
            note_status.set_text("Couldn't save note :(")
            return
        note_entry.set_text("")
        note_status.get_style_context().add_class("ok")
        note_status.set_text("Thanks, note saved!")
        GLib.timeout_add_seconds(3, lambda: note_status.set_text("") or False)
        rebuild_notes()

    def unlock():
        new = [n for n in load_notes() if n.get("ts", 0) >= session_start]
        if new:
            body = "\n".join(f"{n.get('name','?')}: {n['text']}" for n in new[-5:])
            try:
                subprocess.Popen(
                    ["notify-send", f"{len(new)} note(s) while you were away", body]
                )
            except OSError:
                pass
        if session_lock is not None:
            session_lock.unlock()
            display.sync()  # make sure the unlock request reaches the compositor
            display.flush()
        elif not test_mode:
            try:
                display.get_default_seat().ungrab()
            except Exception:
                pass
        Gtk.main_quit()

    checking = {"busy": False}

    def on_password(*_):
        if checking["busy"]:
            return
        password = pw_entry.get_text()
        pw_entry.set_text("")
        if not password:
            return
        checking["busy"] = True
        pw_entry.set_sensitive(False)
        pw_status.set_text("Checking...")

        def worker():
            ok = check_password(user, password)
            GLib.idle_add(done, ok)

        def done(ok):
            checking["busy"] = False
            pw_entry.set_sensitive(True)
            if ok:
                unlock()
            else:
                pw_status.set_text("Wrong password")
                pw_entry.grab_focus()
                GLib.timeout_add_seconds(3, lambda: pw_status.set_text("") or False)
            return False

        threading.Thread(target=worker, daemon=True).start()

    def on_key(_w, ev):
        if test_mode and ev.keyval == Gdk.KEY_Escape:
            Gtk.main_quit()
            return True
        return False

    send_btn.connect("clicked", submit_note)
    note_entry.connect("activate", submit_note)
    name_entry.connect("activate", lambda *_: note_entry.grab_focus())
    pw_entry.connect("activate", on_password)
    main.connect("key-press-event", on_key)

    # Reload notes every few seconds in case something else wrote to the file.
    GLib.timeout_add_seconds(5, lambda: rebuild_notes() or True)

    tick()
    GLib.timeout_add_seconds(1, tick)
    rebuild_notes()

    for b in blanks:
        b.show_all()
    main.show_all()

    # ---- grab keyboard + pointer so nothing else can receive input
    if not test_mode and session_lock is None:
        attempts = {"n": 0}
        seat = display.get_default_seat()

        def try_grab():
            gdk_win = main.get_window()
            if gdk_win is not None and gdk_win.is_visible():
                status = seat.grab(
                    gdk_win, Gdk.SeatCapabilities.ALL, True, None, None, None
                )
                if status == Gdk.GrabStatus.SUCCESS:
                    pw_entry.grab_focus()
                    return False
            attempts["n"] += 1
            if attempts["n"] > 40:
                print("notelock: could not grab input, refusing to lock", file=sys.stderr)
                Gtk.main_quit()
                return False
            return True

        GLib.timeout_add(100, try_grab)
    else:
        pw_entry.grab_focus()

    Gtk.main()


def main():
    args = sys.argv[1:]
    if "--read" in args:
        return cli_read()
    if "--clear" in args:
        save_notes([])
        print("Notes cleared.")
        return
    if "-h" in args or "--help" in args:
        print(__doc__)
        return
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        sys.exit("notelock: no display found (DISPLAY / WAYLAND_DISPLAY unset)")
    run_lock(test_mode="--test" in args)


if __name__ == "__main__":
    main()
