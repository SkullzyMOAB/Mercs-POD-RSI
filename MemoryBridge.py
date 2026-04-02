"""
Mercenaries PoD — PCSX2 Memory Bridge
======================================
Attaches to a running PCSX2 process and provides read/write access to
the PS2's Emotion Engine (EE) RAM. This is the foundation for the
scripting framework — all game state reads and writes go through here.

Usage:
    from MemoryBridge import GameBridge
    bridge = GameBridge()
    bridge.attach()
    money = bridge.read_u32(0x004cec10)
"""

import ctypes
import ctypes.wintypes as wt
import struct
import time
import sys

if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ── Windows Process Memory API ──────────────────────────────────────────────

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)

PROCESS_VM_READ      = 0x0010
PROCESS_VM_WRITE     = 0x0020
PROCESS_VM_OPERATION = 0x0008
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_ALL_ACCESS   = 0x001FFFFF

TH32CS_SNAPPROCESS = 0x02

class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize",              wt.DWORD),
        ("cntUsage",            wt.DWORD),
        ("th32ProcessID",       wt.DWORD),
        ("th32DefaultHeapID",   ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID",        wt.DWORD),
        ("cntThreads",          wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase",      ctypes.c_long),
        ("dwFlags",             wt.DWORD),
        ("szExeFile",           ctypes.c_char * 260),
    ]

# ── EE RAM Layout ───────────────────────────────────────────────────────────

EE_RAM_SIZE = 32 * 1024 * 1024  # 32 MB

# Signature to find EE RAM base in PCSX2's process memory.
# PCSX2 allocates 32MB for EE RAM — we scan for the ELF header that
# gets loaded at offset 0x00100000 within that allocation.
ELF_MAGIC = b"\x7fELF"
MIPS_HEADER = bytes([0x7f, 0x45, 0x4c, 0x46, 0x01, 0x01, 0x01])  # ELF 32-bit LE MIPS


class GameBridge:
    """Attach to PCSX2 and read/write PS2 EE RAM."""

    def __init__(self):
        self.pid = None
        self.handle = None
        self.ee_base = None  # Base address of EE RAM in PCSX2's virtual memory
        self._cache_time = 0
        self._cache = {}

    def find_pcsx2_pid(self) -> int:
        """Find PCSX2 process ID."""
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == -1:
            raise RuntimeError("CreateToolhelp32Snapshot failed")

        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)

        try:
            if not kernel32.Process32First(snapshot, ctypes.byref(entry)):
                raise RuntimeError("Process32First failed")

            while True:
                name = entry.szExeFile.decode("utf-8", errors="ignore").lower()
                if "pcsx2" in name:
                    return entry.th32ProcessID
                if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                    break
        finally:
            kernel32.CloseHandle(snapshot)

        raise RuntimeError("PCSX2 process not found. Is it running?")

    def attach(self, pid: int = None):
        """Attach to PCSX2 process."""
        if pid is None:
            pid = self.find_pcsx2_pid()
        self.pid = pid

        access = PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION | PROCESS_QUERY_INFORMATION
        self.handle = kernel32.OpenProcess(access, False, pid)
        if not self.handle:
            err = ctypes.get_last_error()
            raise RuntimeError(f"OpenProcess failed (error {err}). Try running as admin.")

        print(f"[+] Attached to PCSX2 (PID {pid})")
        self._find_ee_ram()

    def _find_ee_ram(self):
        """Scan PCSX2's memory to find the EE RAM allocation.

        PCSX2 2.x may have multiple copies of game strings (JIT cache, etc).
        Strategy: find ALL candidates matching a known .data string, then
        validate each by checking multiple strings and write accessibility.
        The real EE RAM is the one we can write to and that contains all
        expected game data.
        """
        print("[*] Scanning for EE RAM base...")

        class MEMORY_BASIC_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BaseAddress",       ctypes.c_uint64),
                ("AllocationBase",    ctypes.c_uint64),
                ("AllocationProtect", wt.DWORD),
                ("_pad0",             wt.DWORD),
                ("RegionSize",        ctypes.c_uint64),
                ("State",             wt.DWORD),
                ("Protect",           wt.DWORD),
                ("Type",              wt.DWORD),
                ("_pad1",             wt.DWORD),
            ]

        MEM_COMMIT = 0x1000
        READABLE = {0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80}

        # Known string in .data: "[green]%s: +$%i" at EE addr 0x004cec10
        NEEDLE = b"[green]%s: +$%i"
        NEEDLE_EE_ADDR = 0x004cec10

        # Additional verification strings at known EE offsets
        VERIFY_STRINGS = [
            (0x004d81c0, b"Faction_Set"),
            (0x004d39b8, b"FactionRelation"),
        ]

        mbi = MEMORY_BASIC_INFORMATION()
        address = 0
        candidates = []

        while address < 0x7FFFFFFFFFFF:
            result = kernel32.VirtualQueryEx(
                self.handle, ctypes.c_void_p(address),
                ctypes.byref(mbi), ctypes.sizeof(mbi)
            )
            if result == 0:
                break

            base = int(mbi.BaseAddress)
            size = int(mbi.RegionSize)

            if (mbi.State == MEM_COMMIT and
                mbi.Protect in READABLE and
                size >= 0x1000):

                chunk = min(size, 4 * 1024 * 1024)
                offset = 0
                while offset < size:
                    read_size = min(chunk, size - offset)
                    buf = self._raw_read(base + offset, read_size)
                    if buf:
                        search_start = 0
                        while True:
                            idx = buf.find(NEEDLE, search_start)
                            if idx < 0:
                                break
                            candidate_base = base + offset + idx - NEEDLE_EE_ADDR
                            candidates.append(candidate_base)
                            search_start = idx + 1
                    offset += chunk

            address = base + size
            if address <= base:
                break

        if not candidates:
            raise RuntimeError(
                "Could not locate EE RAM. Make sure a game is loaded in PCSX2."
            )

        # Validate candidates: check multiple strings + write access
        for candidate in candidates:
            score = 0
            for verify_addr, verify_str in VERIFY_STRINGS:
                vbuf = self._raw_read(candidate + verify_addr, 24)
                if vbuf and verify_str in vbuf:
                    score += 1

            # Check write accessibility (real EE RAM is writable)
            test_addr = candidate + 0x004A0000  # .text/.data gap — safe to test
            test_data = self._raw_read(test_addr, 4)
            if test_data is not None:
                # Try writing and reading back
                ok = self._raw_write(test_addr, test_data)  # Write same data back
                if ok:
                    score += 2  # Writable = strong signal this is real EE RAM

            if score >= 3:  # Both verify strings + writable
                self.ee_base = candidate
                print(f"[+] EE RAM found at 0x{self.ee_base:X} (score {score}, {len(candidates)} candidates)")
                return

        # Fallback: use best candidate (most verification strings matched)
        best = candidates[0]
        self.ee_base = best
        print(f"[+] EE RAM (best guess) at 0x{self.ee_base:X} ({len(candidates)} candidates)")
        print(f"    WARNING: could not fully verify — injection may not work")

    def _raw_read(self, address: int, size: int) -> bytes:
        """Read raw bytes from PCSX2 process memory."""
        buf = ctypes.create_string_buffer(size)
        bytes_read = ctypes.c_size_t(0)
        ok = kernel32.ReadProcessMemory(
            self.handle, ctypes.c_void_p(address),
            buf, size, ctypes.byref(bytes_read)
        )
        if not ok or bytes_read.value != size:
            return None
        return buf.raw

    def _raw_write(self, address: int, data: bytes) -> bool:
        """Write raw bytes to PCSX2 process memory."""
        buf = ctypes.create_string_buffer(data)
        bytes_written = ctypes.c_size_t(0)
        ok = kernel32.WriteProcessMemory(
            self.handle, ctypes.c_void_p(address),
            buf, len(data), ctypes.byref(bytes_written)
        )
        return ok and bytes_written.value == len(data)

    # ── EE RAM Access (PS2 Address Space) ────────────────────────────────────

    def read_bytes(self, ee_addr: int, size: int) -> bytes:
        """Read bytes from PS2 EE address space. Raises on failure."""
        if self.ee_base is None:
            raise RuntimeError("Not attached or EE RAM not found")
        if ee_addr < 0 or ee_addr + size > EE_RAM_SIZE:
            raise ValueError(f"EE address 0x{ee_addr:08X} out of range")
        data = self._raw_read(self.ee_base + ee_addr, size)
        if data is None:
            raise RuntimeError(f"Failed to read {size} bytes at EE 0x{ee_addr:08X}")
        return data

    def write_bytes(self, ee_addr: int, data: bytes) -> bool:
        """Write bytes to PS2 EE address space."""
        if self.ee_base is None:
            raise RuntimeError("Not attached or EE RAM not found")
        if ee_addr < 0 or ee_addr + len(data) > EE_RAM_SIZE:
            raise ValueError(f"EE address 0x{ee_addr:08X} out of range")
        return self._raw_write(self.ee_base + ee_addr, data)

    def read_u8(self, ee_addr: int) -> int:
        return struct.unpack("<B", self.read_bytes(ee_addr, 1))[0]

    def read_u16(self, ee_addr: int) -> int:
        return struct.unpack("<H", self.read_bytes(ee_addr, 2))[0]

    def read_u32(self, ee_addr: int) -> int:
        return struct.unpack("<I", self.read_bytes(ee_addr, 4))[0]

    def read_i32(self, ee_addr: int) -> int:
        return struct.unpack("<i", self.read_bytes(ee_addr, 4))[0]

    def read_float(self, ee_addr: int) -> float:
        return struct.unpack("<f", self.read_bytes(ee_addr, 4))[0]

    def read_string(self, ee_addr: int, max_len: int = 256) -> str:
        data = self.read_bytes(ee_addr, max_len)
        end = data.find(b"\x00")
        if end >= 0:
            data = data[:end]
        return data.decode("utf-8", errors="replace")

    def write_u32(self, ee_addr: int, value: int):
        self.write_bytes(ee_addr, struct.pack("<I", value & 0xFFFFFFFF))

    def write_i32(self, ee_addr: int, value: int):
        self.write_bytes(ee_addr, struct.pack("<i", value))

    def write_float(self, ee_addr: int, value: float):
        self.write_bytes(ee_addr, struct.pack("<f", value))

    # ── Memory Scanning ──────────────────────────────────────────────────────

    def scan_u32(self, value: int, start: int = 0x004a3100, end: int = 0x0094a8d3) -> list:
        """Scan EE RAM for a 32-bit value. Returns list of EE addresses."""
        target = struct.pack("<I", value & 0xFFFFFFFF)
        results = []
        chunk_size = 0x10000  # 64KB chunks
        addr = start

        while addr < end:
            size = min(chunk_size, end - addr)
            try:
                data = self.read_bytes(addr, size)
            except (RuntimeError, ValueError):
                addr += size
                continue
            offset = 0
            while True:
                idx = data.find(target, offset)
                if idx < 0:
                    break
                results.append(addr + idx)
                offset = idx + 4
            addr += size

        return results

    def scan_float(self, value: float, start: int = 0x004a3100, end: int = 0x0094a8d3) -> list:
        """Scan EE RAM for a float value."""
        target = struct.pack("<f", value)
        return self.scan_u32(struct.unpack("<I", target)[0], start, end)

    # ── Convenience ──────────────────────────────────────────────────────────

    def dump_region(self, ee_addr: int, size: int = 256):
        """Hex dump a region of EE RAM."""
        try:
            data = self.read_bytes(ee_addr, size)
        except (RuntimeError, ValueError):
            data = None
        if not data:
            print(f"[!] Failed to read 0x{ee_addr:08X}")
            return

        for i in range(0, len(data), 16):
            hex_part = " ".join(f"{b:02X}" for b in data[i:i+16])
            ascii_part = "".join(
                chr(b) if 32 <= b < 127 else "." for b in data[i:i+16]
            )
            print(f"  {ee_addr + i:08X}  {hex_part:<48s}  {ascii_part}")

    def detach(self):
        """Close handle to PCSX2."""
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = None
            self.ee_base = None
            print("[+] Detached from PCSX2")


# ── Known Addresses ──────────────────────────────────────────────────────────
# These are string/data addresses in the ELF's .data segment.
# Runtime game state (player health, money, position) lives in .bss
# and needs to be found via memory scanning during gameplay.

KNOWN = {
    "money_format_str":      0x004cec10,  # "[green]%s: +$%i"
    "money_format_str_2":    0x004db308,  # "[green]%s: +$%i"
    "inverse_health":        0x004d09c8,  # "inverse_health"
    "faction_save_key":      0x004d39b8,  # "FactionRelation%i_%i"
    "lua_faction_set":       0x004d81c0,  # "Faction_SetRelation"
    "lua_faction_modify":    0x004d81d8,  # "Faction_ModifyRelation"

    # .text segment
    "text_start":            0x00100000,
    "text_end":              0x0049e717,

    # .data segment
    "data_start":            0x004a3100,
    "data_end":              0x004ce06f,

    # .bss segment (runtime state lives here)
    "bss_start":             0x004f3600,
    "bss_end":               0x0094a8d3,
}


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    bridge = GameBridge()

    try:
        bridge.attach()
    except RuntimeError as e:
        print(f"[!] {e}")
        sys.exit(1)

    print()
    print("=== Mercenaries PoD Memory Bridge ===")
    print()

    # Verify by reading known strings
    for name, addr in KNOWN.items():
        if addr >= 0x004a3100 and addr < 0x004ce06f:  # .data segment
            s = bridge.read_string(addr, 40)
            print(f"  {name:30s} @ 0x{addr:08X} = \"{s}\"")

    print()
    print("  Bridge ready. Use interactively or import as module.")
    print("  Example: bridge.scan_u32(50000)  # find money address if you have $50,000")
    print()

    # Interactive mode
    import code
    code.interact(
        banner="Mercenaries PoD Memory Bridge — interactive shell",
        local={"bridge": bridge, "KNOWN": KNOWN}
    )
