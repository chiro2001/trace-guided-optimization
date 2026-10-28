"""Linux per-thread PMU group, measured only during native validation ROIs."""
import ctypes
import fcntl
import os
import platform
import struct


class Group:
    def __init__(self):
        self.fds, self.names, self.errors = [], [], []
        nr = {"aarch64": 241, "x86_64": 298}.get(platform.machine())
        if nr is None:
            self.errors.append("unsupported perf_event_open ABI"); return
        libc = ctypes.CDLL(None, use_errno=True)
        for name, typ, config in [("cycles", 0, 0), ("instructions", 0, 1),
                                  ("frontend_stall_cycles", 0, 7), ("l1i_read_misses", 3, 0x10001)]:
            attr = ctypes.create_string_buffer(128)
            # perf_event_attr: type/size/config, period, sample_type, read_format, flags.
            struct.pack_into("=IIQQQQQ", attr, 0, typ, 128, config, 0, 0, 11,
                             1 | (1 << 5) | (1 << 6))
            leader = self.fds[0] if self.fds else -1
            fd = libc.syscall(ctypes.c_long(nr), ctypes.byref(attr), ctypes.c_int(0),
                              ctypes.c_int(-1), ctypes.c_int(leader), ctypes.c_ulong(8))
            if fd < 0:
                self.errors.append({"event": name, "errno": ctypes.get_errno()})
            else:
                self.fds.append(fd); self.names.append(name)

    def start(self):
        if self.fds:
            fcntl.ioctl(self.fds[0], 0x2403, 1)
            fcntl.ioctl(self.fds[0], 0x2400, 1)

    def stop(self):
        result = {"scope": "native-calling-thread-user", "open_errors": self.errors}
        if self.fds:
            fcntl.ioctl(self.fds[0], 0x2401, 1)
            raw = os.read(self.fds[0], 8 * (3 + len(self.fds)))
            if len(raw) != 8 * (3 + len(self.fds)):
                raise RuntimeError("short PMU group read")
            count, enabled, running, *values = struct.unpack("=" + "Q" * (3 + len(self.fds)), raw)
            if count != len(self.fds):
                raise RuntimeError("unexpected PMU group size")
            result.update(time_enabled=enabled, time_running=running,
                          no_multiplex=enabled == running and running > 0,
                          events=dict(zip(self.names, values)))
        return result

    def close(self):
        for fd in self.fds:
            os.close(fd)
        self.fds = []
