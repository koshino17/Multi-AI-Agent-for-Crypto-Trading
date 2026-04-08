from __future__ import annotations

import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk


class TradingAgentsGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Trading Agents Control")
        self.process: subprocess.Popen[str] | None = None

        self.mode_var = tk.StringVar(value="bybit-demo")
        self.symbol_var = tk.StringVar(value="BTC/USDT")
        self.interval_var = tk.StringVar(value="900")
        self.status_var = tk.StringVar(value="Stopped")

        frame = ttk.Frame(root, padding=12)
        frame.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)

        ttk.Label(frame, text="Mode").grid(row=0, column=0, sticky="w")
        ttk.Combobox(frame, textvariable=self.mode_var, values=["bybit-demo", "mock"], state="readonly").grid(row=0, column=1, sticky="ew")

        ttk.Label(frame, text="Symbol").grid(row=1, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.symbol_var).grid(row=1, column=1, sticky="ew")

        ttk.Label(frame, text="Interval (sec)").grid(row=2, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.interval_var).grid(row=2, column=1, sticky="ew")

        ttk.Label(frame, text="Status").grid(row=3, column=0, sticky="w")
        ttk.Label(frame, textvariable=self.status_var).grid(row=3, column=1, sticky="w")

        ttk.Button(frame, text="Start", command=self.start).grid(row=4, column=0, sticky="ew", pady=(8, 8))
        ttk.Button(frame, text="Stop", command=self.stop).grid(row=4, column=1, sticky="ew", pady=(8, 8))

        self.log = tk.Text(frame, width=88, height=16)
        self.log.grid(row=5, column=0, columnspan=2, sticky="nsew")
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(5, weight=1)

        self.root.after(1000, self.poll_process)

    def start(self) -> None:
        if self.process and self.process.poll() is None:
            self.status_var.set("Running")
            return

        cmd = [
            sys.executable,
            "-m",
            "trading_agents.runner",
            "--mode",
            self.mode_var.get(),
            "--symbol",
            self.symbol_var.get(),
            "--interval",
            self.interval_var.get(),
        ]
        self.process = subprocess.Popen(
            cmd,
            cwd=Path(__file__).resolve().parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.status_var.set("Running")
        self.log.insert("end", "Started agent loop.\n")
        self.log.see("end")

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.status_var.set("Stopping")
            self.log.insert("end", "Stopping agent loop.\n")
            self.log.see("end")

    def poll_process(self) -> None:
        if self.process and self.process.stdout:
            line = self.process.stdout.readline()
            if line:
                self.log.insert("end", line)
                self.log.see("end")
        if self.process and self.process.poll() is not None:
            self.status_var.set("Stopped")
        self.root.after(1000, self.poll_process)


def main() -> int:
    root = tk.Tk()
    root.geometry("860x440")
    TradingAgentsGUI(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
