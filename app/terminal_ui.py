"""Small, dependency-free terminal presentation for the chat entry point."""
import os
import shutil
import sys


THEMES = {
    "neon": (51, 45, 39, 99, 135, 213),
    "ocean": (159, 123, 87, 51, 45, 39),
    "sunset": (229, 221, 215, 209, 203, 198),
    "mono": (255,) * 6,
}
LOGO = (
    "  ██████╗ ██████╗ ███████╗███╗   ██╗ █████╗  ██████╗███╗   ███╗",
    " ██╔═══██╗██╔══██╗██╔════╝████╗  ██║██╔══██╗██╔════╝████╗ ████║",
    " ██║   ██║██████╔╝█████╗  ██╔██╗ ██║███████║██║     ██╔████╔██║",
    " ██║   ██║██╔═══╝ ██╔══╝  ██║╚██╗██║██╔══██║██║     ██║╚██╔╝██║",
    " ╚██████╔╝██║     ███████╗██║ ╚████║██║  ██║╚██████╗██║ ╚═╝ ██║",
    "  ╚═════╝ ╚═╝     ╚══════╝╚═╝  ╚═══╝╚═╝  ╚═╝ ╚═════╝╚═╝     ╚═╝",
)


class TerminalUI:
    def __init__(self, theme="neon", color="auto"):
        self.theme = theme
        self.color = color == "always" or (
            color == "auto" and sys.stdout.isatty()
            and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
        )
        self.readline = False
        if sys.stdin.isatty():
            try:
                import readline  # noqa: F401; enables editing and input history
                self.readline = True
            except ImportError:
                pass

    def paint(self, text, index=0, bold=False):
        if not self.color:
            return text
        code = THEMES[self.theme][index % 6]
        weight = "1;" if bold else ""
        return f"\033[{weight}38;5;{code}m{text}\033[0m"

    def banner(self):
        columns = shutil.get_terminal_size((88, 24)).columns
        logo_width = max(map(len, LOGO))
        if columns >= logo_width + 6:
            width = min(columns - 2, 84) - 2
            print(self.paint("╭" + "─" * width + "╮", 0))
            print(self.paint("│" + " CIRCUIT STUDIO  /  LOCAL DEEPSEEK ".center(width) + "│", 0))
            print(self.paint("│" + " " * width + "│", 0))
            for index, line in enumerate(LOGO):
                print(self.paint("│" + line.center(width) + "│", index, bold=True))
            print(self.paint("│" + " " * width + "│", 5))
            print(self.paint("│" + "[ SRAM ]──[ MULTIPLIER ]──[ COMPRESSOR ]──[ DCiM ]".center(width) + "│", 5))
            print(self.paint("╰" + "─" * width + "╯", 5))
        else:
            # Narrow terminals keep the name and controls readable.
            title = "[ OPENACM ]" if columns >= 11 else "OPENACM"
            print(self.paint(title, bold=True))
            print(self.paint("LOCAL DEEPSEEK", 3))
        print(self.paint("  从电路想法，到仿真结果。", 1, bold=True))
        print("  描述需求即可启动任务，完成后直接显示中文结果总结。")
        self.help()

    def help(self):
        print(self.paint("  /plan 需求  查看计划    /new  新对话    /exit  退出", 2))
        print(self.paint("  /theme neon|ocean|sunset|mono  切换配色", 3))
        print(self.paint("  /clear  重绘界面    /help  帮助", 4))
        print(self.paint("  探索8位近似乘法器，预算64个候选，MRED≤0.02", 1))

    def prompt(self):
        if not self.color:
            return "\n你 › "
        start = f"\033[1;38;5;{THEMES[self.theme][0]}m"
        end = "\033[0m"
        if self.readline:
            start, end = f"\001{start}\002", f"\001{end}\002"
        return f"\n{start}你 › {end}"

    def command(self, text):
        """Handle presentation commands; return whether a command was consumed."""
        if text == "/help":
            self.help()
        elif text == "/clear":
            if sys.stdout.isatty():
                print("\033[2J\033[H", end="")
            self.banner()
        elif text == "/theme" or text.startswith("/theme "):
            name = text.partition(" ")[2].strip()
            if name not in THEMES:
                print("可选配色：" + " / ".join(THEMES))
            else:
                self.theme = name
                self.banner()
        else:
            return False
        return True

    def status(self, message):
        print(self.paint("  ◇ " + message, 2), flush=True)

    def error(self, message):
        print(self.paint("  ! " + message, 5), file=sys.stderr)
