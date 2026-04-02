"""
Mercenaries PoD - PCSX2 PINE IPC Client
========================================
Writes to EE memory through PCSX2's PINE interface, which goes through
the vtlb and automatically invalidates the JIT recompiler cache.
This is the ONLY reliable way to patch code at runtime.

PINE protocol: TCP socket on port 28011 (configurable via PINESlot in PCSX2.ini)
Message format: [size:u32_le] [command:u8] [payload...]
"""

import socket
import struct
import sys
import time

if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

PINE_PORT = 28011
PINE_HOST = "127.0.0.1"

# PINE IPC commands
MsgRead8    = 0
MsgRead16   = 1
MsgRead32   = 2
MsgRead64   = 3
MsgWrite8   = 4
MsgWrite16  = 5
MsgWrite32  = 6
MsgWrite64  = 7
MsgVersion  = 8
MsgTitle    = 0x0E
MsgID       = 0x0F
MsgStatus   = 0x20


class PINEClient:
    """Minimal PCSX2 PINE IPC client for EE memory access."""

    def __init__(self, host=PINE_HOST, port=PINE_PORT):
        self.host = host
        self.port = port
        self.sock = None

    def connect(self, timeout=15.0):
        """Connect to PCSX2 PINE server."""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        try:
            self.sock.connect((self.host, self.port))
            print(f"[+] PINE connected to {self.host}:{self.port}")
            return True
        except (ConnectionRefusedError, socket.timeout) as e:
            print(f"[!] PINE connection failed: {e}")
            print("    Make sure EnablePINE=true in PCSX2.ini and restart PCSX2")
            self.sock = None
            return False

    def _send_recv(self, payload):
        """Send a PINE message and receive response."""
        # Message format: [size:u32_le][payload]
        size = len(payload) + 4  # size includes itself
        msg = struct.pack("<I", size) + payload
        self.sock.sendall(msg)

        # Read response size
        size_data = self._recv_exact(4)
        resp_size = struct.unpack("<I", size_data)[0]

        # Read response payload
        resp_data = self._recv_exact(resp_size - 4)
        return resp_data

    def _recv_exact(self, n):
        """Receive exactly n bytes."""
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError("PINE connection closed")
            data += chunk
        return data

    def read32(self, addr):
        """Read a 32-bit value from EE memory."""
        payload = struct.pack("<BI", MsgRead32, addr)
        resp = self._send_recv(payload)
        # Response: [result:u8][value:u32]
        result = resp[0]
        if result != 0:
            raise RuntimeError(f"PINE read32 failed at 0x{addr:08X} (result={result})")
        return struct.unpack_from("<I", resp, 1)[0]

    def write32(self, addr, value):
        """Write a 32-bit value to EE memory. Invalidates JIT cache."""
        payload = struct.pack("<BII", MsgWrite32, addr, value)
        resp = self._send_recv(payload)
        result = resp[0]
        if result != 0:
            raise RuntimeError(f"PINE write32 failed at 0x{addr:08X} (result={result})")

    def write_bytes(self, addr, data):
        """Write arbitrary bytes to EE memory (4-byte aligned, paced)."""
        import time
        for i in range(0, len(data), 4):
            chunk = data[i:i+4]
            if len(chunk) < 4:
                chunk = chunk + b'\x00' * (4 - len(chunk))
            val = struct.unpack("<I", chunk)[0]
            self.write32(addr + i, val)
            if i % 32 == 28:  # Pace every 8 writes to avoid overwhelming PINE
                time.sleep(0.02)

    def read_bytes(self, addr, size):
        """Read bytes from EE memory (4-byte aligned)."""
        result = b""
        for i in range(0, size, 4):
            val = self.read32(addr + i)
            result += struct.pack("<I", val)
        return result[:size]

    def get_version(self):
        """Get PINE protocol version."""
        payload = struct.pack("<B", MsgVersion)
        resp = self._send_recv(payload)
        if resp[0] != 0:
            return None
        return resp[1:]

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None


def install_hook_via_pine():
    """Install the Lua injection hook using PINE (JIT-cache-safe)."""
    from CodeCave import (CAVE_ADDR, SCRIPT_BUF, CHUNK_NAME_ADDR,
                          TRIGGER_FLAG, STATUS_FLAG, REENTRANT_FLAG,
                          CHUNK_NAME, LUA_GETTOP, LuaCodeCave)
    import MIPSAssembler as asm

    pine = PINEClient()
    if not pine.connect():
        return False

    print(f"[*] Installing Lua injection via PINE...")

    # Build cave code
    orig_insn = struct.pack("<I", 0x8C83000C)  # original lua_gettop insn
    cave_obj = LuaCodeCave.__new__(LuaCodeCave)
    cave_obj.bridge = None
    cave_obj.installed = False
    cave_obj.hook_addr = None
    cave_obj.orig_insn_bytes = None
    cave_code = cave_obj._build_code_cave(orig_insn)
    return_jump = asm.j(LUA_GETTOP + 4) + asm.nop()
    cave_code = cave_code[:-8] + return_jump

    print(f"    Cave: {len(cave_code)} bytes at 0x{CAVE_ADDR:08X}")

    # Write cave code first (before hook, so it's ready when hook fires)
    pine.write_bytes(CAVE_ADDR, cave_code)
    print(f"    [OK] Cave code written")

    # Write data region
    pine.write_bytes(CHUNK_NAME_ADDR, CHUNK_NAME + b'\x00' * 3)
    pine.write32(TRIGGER_FLAG, 0)
    pine.write32(STATUS_FLAG, 0)
    pine.write32(REENTRANT_FLAG, 0)
    print(f"    [OK] Data region initialized")

    # Write hook LAST (this triggers JIT invalidation for lua_gettop)
    j_insn = struct.unpack("<I", asm.j(CAVE_ADDR))[0]
    pine.write32(LUA_GETTOP, j_insn)
    print(f"    [OK] Hook installed at 0x{LUA_GETTOP:08X} -> J 0x{CAVE_ADDR:08X}")

    # Verify
    hook_check = pine.read32(LUA_GETTOP)
    if hook_check == j_insn:
        print(f"    [OK] Hook verified!")
    else:
        print(f"    [!!] Hook verification failed (got 0x{hook_check:08X})")

    cave_check = pine.read32(CAVE_ADDR)
    cave_op = (cave_check >> 26) & 0x3F
    if cave_op == 0x0F:  # LUI
        print(f"    [OK] Cave code verified!")
    else:
        print(f"    [!!] Cave verification failed (got 0x{cave_check:08X})")

    pine.close()
    print(f"[+] PINE installation complete!")
    return True


if __name__ == "__main__":
    install_hook_via_pine()
