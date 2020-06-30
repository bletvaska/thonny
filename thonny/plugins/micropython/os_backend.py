import os
import sys
import logging
from thonny.plugins.micropython.backend import MicroPythonBackend, EOT, ends_overlap, ENCODING
import textwrap
from textwrap import dedent
from thonny.common import UserError, BackendEvent, serialize_message, ToplevelResponse
from thonny.plugins.micropython.subprocess_connection import SubprocessConnection
from thonny.plugins.micropython.connection import ConnectionFailedException
from thonny.plugins.micropython.bare_metal_backend import NORMAL_PROMPT, LF
import ast
import re

FALLBACK_BUILTIN_MODULES = [
    "cmath",
    "gc",
    "math",
    "sys",
    "array",
    # "binascii", # don't include it, as it may give false signal for reader/writer
    "collections",
    "errno",
    "hashlib",
    "heapq",
    "io",
    "json",
    "os",
    "re",
    "select",
    "socket",
    "ssl",
    "struct",
    "time",
    "zlib",
    "_thread",
    "btree",
    "micropython",
    "cryptolib",
    "ctypes",
]

PASTE_MODE_CMD = b"\x05"
PASTE_MODE_LINE_PREFIX = b"=== "


class MicroPythonOsBackend(MicroPythonBackend):
    def __init__(self, executable, clean, api_stubs_path):
        self._executable = executable
        try:
            self._connection = SubprocessConnection(executable)
        except ConnectionFailedException as e:
            text = "\n" + str(e) + "\n"
            msg = BackendEvent(event_type="ProgramOutput", stream_name="stderr", data=text)
            sys.stdout.write(serialize_message(msg) + "\n")
            sys.stdout.flush()
            return

        super().__init__(clean, api_stubs_path)

    def _get_custom_helpers(self):
        return textwrap.dedent(
            """
            if not hasattr(os, "getcwd") or not hasattr(os, "getcwd") or not hasattr(os, "rmdir"):
                # https://github.com/pfalcon/pycopy-lib/blob/master/os/os/__init__.py
                
                import ffi
                
                libc = ffi.open(
                    "libc.so.6" if sys.platform == "linux" else "libc.dylib"
                )
                
                @classmethod
                def check_error(cls, ret):
                    if ret == -1:
                        raise OSError(cls.os.errno())
                
                _getcwd = libc.func("s", "getcwd", "si")
                @classmethod
                def getcwd(cls):
                    buf = bytearray(512)
                    return cls._getcwd(buf, 512)

                _chdir = libc.func("i", "chdir", "s")
                @classmethod
                def chdir(cls, dir):
                    r = cls._chdir(dir)
                    cls.check_error(r)
                
                _rmdir = libc.func("i", "rmdir", "s")
                @classmethod
                def rmdir(cls, name):
                    e = cls._rmdir(name)
                    cls.check_error(e)                                    
                """
        )

    def _process_until_initial_prompt(self, clean):
        output = []

        def collect_output(data, stream_name):
            output.append(data)

        self._forward_output_until_active_prompt(collect_output, "stdout")
        self._original_welcome_text = b"".join(output).decode(ENCODING).replace("\r\n", "\n")
        self._welcome_text = self._original_welcome_text.replace(
            "Use Ctrl-D to exit, Ctrl-E for paste mode\n", ""
        )

    def _fetch_builtin_modules(self):
        return FALLBACK_BUILTIN_MODULES

    def _soft_reboot(self, side_command):
        raise NotImplementedError()

    def _execute_with_consumer(self, script, output_consumer):
        """Ensures prompt and submits the script.
        Returns (out, value_repr, err) if there are no problems, ie. all parts of the 
        output are present and it reaches active prompt.
        Otherwise raises ProtocolError.
        
        The execution may block. In this case the user should do something (eg. provide
        required input or issue an interrupt). The UI should remind the interrupt in case
        of Thonny commands.
        """
        self._connection.write(PASTE_MODE_CMD)
        self._connection.read_until(PASTE_MODE_LINE_PREFIX)
        self._connection.write(script + "#uuu")
        self._connection.read_until(b"#uuu")
        self._connection.write(EOT)
        self._connection.read_until(b"\n")

        out = self._connection.read_until(NORMAL_PROMPT)[: -len(NORMAL_PROMPT)]
        output_consumer(out, "stdout")

    def _forward_output_until_active_prompt(self, output_consumer, stream_name="stdout"):
        INCREMENTAL_OUTPUT_BLOCK_CLOSERS = re.compile(
            b"|".join(map(re.escape, [LF, NORMAL_PROMPT]))
        )

        pending = b""
        while True:
            # There may be an input submission waiting
            # and we can't progress without resolving it first
            self._check_for_side_commands()

            # Prefer whole lines, but allow also incremental output to single line
            new_data = self._connection.soft_read_until(
                INCREMENTAL_OUTPUT_BLOCK_CLOSERS, timeout=0.05
            )
            if not new_data:
                continue

            pending += new_data

            if pending.endswith(LF):
                output_consumer(pending, stream_name)
                pending = b""

            elif pending.endswith(NORMAL_PROMPT):
                out = pending[: -len(NORMAL_PROMPT)]
                output_consumer(out, stream_name)
                return NORMAL_PROMPT

            elif ends_overlap(pending, NORMAL_PROMPT):
                # Maybe we have a prefix of the prompt and the rest is still coming?
                follow_up = self._connection.soft_read(1, timeout=0.1)
                if not follow_up:
                    # most likely not a Python prompt, let's forget about it
                    output_consumer(pending, stream_name)
                    pending = b""
                else:
                    # Let's withhold this for now
                    pending += follow_up

            else:
                # No prompt in sight.
                # Output and keep working.
                output_consumer(pending, stream_name)
                pending = b""

    def _forward_unexpected_output(self, stream_name="stdout"):
        "Invoked between commands"
        data = self._connection.read_all()
        if data.endswith(NORMAL_PROMPT):
            self._send_output(data[: -len(NORMAL_PROMPT)], "stdout")
        elif data:
            self._send_output(data, "stdout")

    def _write(self, data):
        self._connection.write(data)

    def _cmd_Run(self, cmd):
        self._connection.close()
        self._report_time("befconn")
        self._connection = SubprocessConnection(self._executable, ["-i"] + cmd.args)
        self._report_time("afconn")
        self._forward_output_until_active_prompt(self._send_output, "stdout")
        self._report_time("afforv")
        self.send_message(
            BackendEvent(event_type="HideTrailingOutput", text=self._original_welcome_text)
        )
        self._report_time("beffhelp")
        self._prepare_helpers()
        self._report_time("affhelp")

    def _cmd_execute_system_command(self, cmd):
        assert cmd.cmd_line.startswith("!")
        cmd_line = cmd.cmd_line[1:]
        # "or None" in order to avoid MP repl to print its value
        self._execute("__thonny_helper.os.system(%r) or None" % cmd_line)

    def _cmd_get_fs_info(self, cmd):
        raise NotImplementedError()

    def _cmd_write_file(self, cmd):
        raise NotImplementedError()

    def _cmd_delete(self, cmd):
        raise NotImplementedError()

    def _cmd_read_file(self, cmd):
        raise NotImplementedError()

    def _cmd_mkdir(self, cmd):
        raise NotImplementedError()

    def _upload_file(self, source, target, notifier):
        raise NotImplementedError()

    def _download_file(self, source, target, notifier=None):
        raise NotImplementedError()

    def _is_connected(self):
        return not self._connection._error


if __name__ == "__main__":
    THONNY_USER_DIR = os.environ["THONNY_USER_DIR"]
    logger = logging.getLogger("thonny.micropython.backend")
    logger.propagate = False
    logFormatter = logging.Formatter("%(levelname)s: %(message)s")
    file_handler = logging.FileHandler(
        os.path.join(THONNY_USER_DIR, "micropython-backend.log"), encoding="UTF-8", mode="w"
    )
    file_handler.setFormatter(logFormatter)
    file_handler.setLevel(logging.INFO)
    logger.addHandler(file_handler)

    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", type=str)
    parser.add_argument("--api_stubs_path", type=str)
    args = parser.parse_args()

    vm = MicroPythonOsBackend(args.executable, clean=None, api_stubs_path=args.api_stubs_path)
