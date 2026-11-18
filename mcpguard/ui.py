"""User interfaces for approval prompts.

``TkPrompter`` is the real one: an always-on-top popup per pending request.
Tkinter must own the main thread, so the prompter's :meth:`run` is the
daemon's main loop and the network side reaches it through a thread-safe
queue that the Tk event loop polls.

``ScriptedPrompter`` answers automatically and exists for tests and headless
smoke runs only.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from typing import Callable, Dict, Optional

from .broker import ApprovalRequest, Prompter

MAX_DISPLAY_CHARS = 20_000
#: The Allow button stays disabled this long so that a keystroke or click
#: aimed at another window cannot approve a popup that just appeared.
ALLOW_ARM_DELAY_MS = 700


def format_arguments(arguments) -> str:
    try:
        text = json.dumps(arguments, indent=2, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = repr(arguments)
    if len(text) > MAX_DISPLAY_CHARS:
        hidden = len(text) - MAX_DISPLAY_CHARS
        text = text[:MAX_DISPLAY_CHARS] + f"\n... [{hidden} more characters not shown]"
    return text


class TkPrompter(Prompter):
    def __init__(self) -> None:
        self._commands: "queue.Queue[tuple]" = queue.Queue()
        self._stop = threading.Event()
        self._windows: Dict[str, object] = {}
        self._opened = 0

    # -- called from the asyncio thread ------------------------------------

    def show(self, request: ApprovalRequest, timeout: float) -> None:
        self._commands.put(("show", request, time.monotonic() + timeout))

    def dismiss(self, request_id: str) -> None:
        self._commands.put(("dismiss", request_id, None))

    def stop(self) -> None:
        self._stop.set()

    # -- main thread --------------------------------------------------------

    def run(self) -> None:
        import tkinter as tk

        self._tk = tk
        self._root = tk.Tk()
        self._root.withdraw()  # no main window; only popups
        self._root.after(100, self._poll)
        self._root.mainloop()

    def _poll(self) -> None:
        if self._stop.is_set():
            self._root.destroy()
            return
        try:
            while True:
                kind, payload, extra = self._commands.get_nowait()
                if kind == "show":
                    self._open(payload, extra)
                else:
                    self._close(payload)
        except queue.Empty:
            pass
        self._root.after(100, self._poll)

    def _answer(self, request_id: str, allow: bool) -> None:
        self._close(request_id)
        self._resolver(request_id, allow, "")

    def _close(self, request_id: str) -> None:
        window = self._windows.pop(request_id, None)
        if window is not None:
            window.destroy()

    def _open(self, request: ApprovalRequest, deadline: float) -> None:
        tk = self._tk
        win = tk.Toplevel(self._root)
        self._windows[request.id] = win
        win.title("MCP Guard: approval required")
        win.protocol("WM_DELETE_WINDOW", lambda: self._answer(request.id, False))
        win.bind("<Escape>", lambda _e: self._answer(request.id, False))

        # Cascade so that simultaneous requests are all visible.
        offset = 32 * (self._opened % 8)
        self._opened += 1
        width, height = 560, 460
        x = (win.winfo_screenwidth() - width) // 2 + offset
        y = (win.winfo_screenheight() - height) // 3 + offset
        win.geometry(f"{width}x{height}+{x}+{y}")
        win.minsize(420, 320)

        pad = {"padx": 14, "pady": 3}
        tk.Label(win, text="Claude wants to make an MCP request",
                 font=("TkDefaultFont", 13, "bold"), anchor="w").pack(fill="x", padx=14, pady=(12, 6))

        grid = tk.Frame(win)
        grid.pack(fill="x", **pad)
        rows = [("Source", request.source), ("MCP server", request.server),
                ("Method", request.method), ("Tool", request.tool)]
        if request.description:
            rows.append(("Description", request.description))
        for row, (label, value) in enumerate(rows):
            tk.Label(grid, text=label + ":", anchor="nw", fg="#555").grid(row=row, column=0, sticky="nw", pady=1)
            tk.Label(grid, text=value, anchor="w", justify="left", wraplength=420).grid(
                row=row, column=1, sticky="w", padx=(8, 0), pady=1)

        tk.Label(win, text="Parameters:", anchor="w", fg="#555").pack(fill="x", padx=14, pady=(6, 0))

        # Pack the footer before the expanding text box so it never gets clipped.
        footer = tk.Frame(win)
        footer.pack(side="bottom", fill="x", padx=14, pady=12)
        countdown = tk.Label(footer, anchor="w", fg="#555")
        countdown.pack(side="left")
        allow = tk.Button(footer, text="Allow", width=10, state="disabled",
                          command=lambda: self._answer(request.id, True))
        allow.pack(side="right")
        block = tk.Button(footer, text="Block", width=10, default="active",
                          command=lambda: self._answer(request.id, False))
        block.pack(side="right", padx=(0, 8))

        body = tk.Frame(win)
        body.pack(fill="both", expand=True, padx=14, pady=(2, 0))
        scroll = tk.Scrollbar(body)
        scroll.pack(side="right", fill="y")
        text = tk.Text(body, wrap="word", height=8, yscrollcommand=scroll.set,
                       font=("TkFixedFont", 10))
        text.insert("1.0", format_arguments(request.arguments))
        text.configure(state="disabled")  # read-only, plain text only
        text.pack(side="left", fill="both", expand=True)
        scroll.configure(command=text.yview)

        def tick() -> None:
            if request.id not in self._windows:
                return
            remaining = max(0, int(deadline - time.monotonic()))
            countdown.configure(text=f"Blocks automatically in {remaining}s")
            win.after(500, tick)

        def arm() -> None:
            if request.id in self._windows:
                allow.configure(state="normal")

        # Stay above every other window and grab attention.
        win.attributes("-topmost", True)
        win.lift()
        win.after(50, win.focus_force)
        win.after(ALLOW_ARM_DELAY_MS, arm)
        block.focus_set()  # Enter/Space on a fresh popup blocks, never allows
        win.bell()
        tick()


class ScriptedPrompter(Prompter):
    """Answers with ``policy(request)``; ``None`` leaves the request pending."""

    def __init__(self, policy: Callable[[ApprovalRequest], Optional[bool]]):
        self.policy = policy
        self.shown = []
        self._stop = threading.Event()

    def show(self, request: ApprovalRequest, timeout: float) -> None:
        self.shown.append(request)
        answer = self.policy(request)
        if answer is not None:
            self._resolver(request.id, bool(answer), "scripted answer")

    def run(self) -> None:
        self._stop.wait()

    def stop(self) -> None:
        self._stop.set()


def make_prompter(kind: str) -> Prompter:
    if kind == "tk":
        try:
            import tkinter  # noqa: F401
        except ImportError as exc:
            raise SystemExit(
                "mcpguard: tkinter is not available in this Python. Install it "
                "(Linux: 'sudo apt install python3-tk'; macOS Homebrew: "
                "'brew install python-tk') and start the daemon again."
            ) from exc
        return TkPrompter()
    if kind == "test-allow":
        return ScriptedPrompter(lambda _r: True)
    if kind == "test-deny":
        return ScriptedPrompter(lambda _r: False)
    raise SystemExit(f"mcpguard: unknown ui '{kind}'")
