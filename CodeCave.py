"""
Mercenaries PoD — EE RAM Code Cave + Lua Injection
====================================================
Writes MIPS machine code into unused EE RAM that, when triggered,
calls luaL_loadbuffer + lua_pcall to execute arbitrary Lua scripts
inside the game's Lua 5.0.1 VM.

Memory Layout:
  0x01F00000  Script text buffer (4KB, upper EE RAM)
  0x01F00FE0  Chunk name "=mod" (5 bytes)
  0x01F00FF0  Trigger flag (write 1 to execute script)
  0x01F00FF4  Status (0=idle, 1=running, 2=done, 3=error)
  0x01F00FF8  Saved original instruction from hook point
  0x01F00FFC  Re-entrancy guard (prevents infinite recursion)
  0x0044EF20  Code cave (unused BIOS syscall stubs, 448 bytes)

Key Addresses (from disassembly):
  lua_State*         = *(0x005784D8)
  luaL_loadbuffer    = 0x003B6FB8
  lua_pcall          = 0x003BB630
  lua_gettop         = 0x003B9F00

CRITICAL FIX: The code cave includes a re-entrancy guard. This is
essential because we hook lua_gettop, which is called internally by
luaL_loadbuffer and lua_pcall. Without the guard, hooking lua_gettop
would cause infinite recursion and crash the game.

Flow:
  1. Check re-entrancy flag — if set, skip to original instruction + return
  2. Set re-entrancy flag
  3. Check trigger flag — if 0, clear re-entrancy and skip to original + return
  4. Execute Lua injection (luaL_loadbuffer + lua_pcall)
  5. Clear re-entrancy flag
  6. Execute original hooked instruction
  7. Jump back to hook_addr + 4

Usage:
    from CodeCave import LuaCodeCave
    cave = LuaCodeCave(bridge)
    cave.install()
    cave.execute("Traffic_SetZoneDensity(1, 2.0)")
    cave.execute("Player_AdjustMoney(500000)")
"""

import struct
import time
import sys
from pathlib import Path

if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, str(Path(__file__).parent))
from MemoryBridge import GameBridge, KNOWN
import MIPSAssembler as asm

# Memory layout:
# CODE CAVE at 0x0044EF20 — unused PS2 BIOS syscall stubs in .text segment.
#   Verified safe: 10 checks (no JAL, no J, no branches, no function pointers,
#   no LUI+ADDIU/ORI construction). SDK-linked kernel wrappers never called by game.
#   448 bytes available, 16-byte aligned, file-backed (not zeroed at init).
# DATA at 0x01F00000 (upper EE RAM) — read via LW at runtime, not JIT-cached.
# IMPORTANT: All low 16-bit offsets must be < 0x8000 to avoid MIPS sign-extension bug.
SCRIPT_BUF    = 0x01F00000   # 4KB script text buffer (data, not code)
SCRIPT_MAXLEN = 0x0FE0       # Max script length (4064 bytes)
CHUNK_NAME_ADDR = 0x01F00FE0 # "=mod" string stored here
TRIGGER_FLAG  = 0x01F00FF0   # Write 1 here to trigger execution
STATUS_FLAG   = 0x01F00FF4   # Status: 0=idle, 1=running, 2=done, 3=error
ORIG_INSN     = 0x01F00FF8   # Saved original instruction from hook point
REENTRANT_FLAG = 0x01F00FFC  # Re-entrancy guard (0=not in cave, 1=in cave)
CAVE_ADDR     = 0x0044EF20   # Code cave in unused BIOS syscall stubs (448 bytes)

# Game addresses
LUA_STATE_PTR = 0x005784D8   # *(uint32*) = lua_State* L
LUAL_LOADBUF  = 0x003B6FB8   # luaL_loadbuffer(L, buf, len, name)
LUA_PCALL     = 0x003BB630   # lua_pcall(L, nargs, nresults, errfunc)
LUA_GETTOP    = 0x003B9F00   # lua_gettop(L)

CHUNK_NAME    = b"=mod\x00"


class LuaCodeCave:
    """Manages a MIPS code cave in EE RAM for Lua script injection."""

    def __init__(self, bridge: GameBridge):
        self.bridge = bridge
        self.installed = False
        self.hook_addr = None
        self.orig_insn_bytes = None
        self._pine = None  # Lazy PINE connection (set by _get_pine or install)

    def _build_code_cave(self, orig_insn_bytes: bytes) -> bytes:
        """Generate MIPS machine code for the Lua execution cave.

        The re-entrancy guard is CRITICAL: lua_gettop is called by
        luaL_loadbuffer and lua_pcall internally. Without this guard,
        the hook would cause infinite recursion.

        Args:
            orig_insn_bytes: The 4 bytes of the original instruction at
                           the hook point, to be embedded and executed.
        """
        code = b""

        # ━━ PHASE 1: Re-entrancy check ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # If we're already inside the cave (recursive call from
        # luaL_loadbuffer/lua_pcall), skip straight to executing the
        # original instruction and returning.
        code += asm.lui("t0", REENTRANT_FLAG >> 16)              # t0 = high16(flag region)
        code += asm.lw("t1", REENTRANT_FLAG & 0xFFFF, "t0")     # t1 = reentrant flag
        code += asm.bne("t1", "zero", 0)                         # if re-entrant: skip to original
        reentrant_skip_pos = len(code) - 4                        # (patch target later)
        code += asm.nop()                                         # delay slot

        # ━━ PHASE 2: Set re-entrancy guard ━━━━━━━━━━━━━━━━━━━━━━━━━━━
        code += asm.addiu("t1", "zero", 1)
        code += asm.sw("t1", REENTRANT_FLAG & 0xFFFF, "t0")     # reentrant = 1

        # ━━ PHASE 3: Check trigger flag ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        code += asm.lui("t0", TRIGGER_FLAG >> 16)                # t0 = high16(trigger region)
        code += asm.lw("t1", TRIGGER_FLAG & 0xFFFF, "t0")       # t1 = trigger flag
        code += asm.beq("t1", "zero", 0)                         # if no trigger: skip to cleanup
        no_trigger_skip_pos = len(code) - 4                       # (patch target later)
        code += asm.nop()                                         # delay slot

        # ━━ PHASE 4: Set status = 1 (running), clear trigger ━━━━━━━━━
        code += asm.addiu("t1", "zero", 1)
        code += asm.sw("t1", STATUS_FLAG & 0xFFFF, "t0")        # status = 1
        code += asm.sw("zero", TRIGGER_FLAG & 0xFFFF, "t0")     # trigger = 0

        # ━━ PHASE 5: Save registers ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        code += asm.addiu("sp", "sp", -64)
        code += asm.sw("ra", 0, "sp")
        code += asm.sw("s0", 4, "sp")
        code += asm.sw("s1", 8, "sp")
        code += asm.sw("s2", 12, "sp")
        code += asm.sw("a0", 16, "sp")
        code += asm.sw("a1", 20, "sp")
        code += asm.sw("a2", 24, "sp")
        code += asm.sw("a3", 28, "sp")
        code += asm.sw("t0", 32, "sp")
        code += asm.sw("t1", 36, "sp")
        code += asm.sw("t2", 40, "sp")
        code += asm.sw("t3", 44, "sp")
        code += asm.sw("v0", 48, "sp")
        code += asm.sw("v1", 52, "sp")

        # ━━ PHASE 6: Get lua_State* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # CRITICAL: When the low 16 bits of an address are >= 0x8000,
        # LW sign-extends them, effectively subtracting 0x10000.
        # Compensate by adding 1 to the LUI upper half.
        lui_hi = LUA_STATE_PTR >> 16
        lw_lo = LUA_STATE_PTR & 0xFFFF
        if lw_lo >= 0x8000:
            lui_hi += 1                  # compensate for sign extension
            lw_lo = lw_lo - 0x10000      # make signed negative offset
        code += asm.lui("s0", lui_hi)
        code += asm.lw("s0", lw_lo, "s0")                       # s0 = lua_State* L

        # ━━ PHASE 7: Calculate script length (strlen) ━━━━━━━━━━━━━━━━
        code += asm.li("s1", SCRIPT_BUF)                         # s1 = script buffer addr
        code += asm.move("t2", "s1")                              # t2 = iterator

        # strlen loop: count bytes until null terminator
        # IMPORTANT: beq offset is +3 (not +2) to skip the addiu delay slot
        # when the null is found. Otherwise strlen returns len+1.
        strlen_start = len(code)
        code += asm.lbu("t3", 0, "t2")                           # t3 = *t2
        code += asm.beq("t3", "zero", 3)                         # if '\0', skip to subu
        code += asm.nop()                                         # delay slot
        code += asm.beq("zero", "zero", -4)                      # unconditional loop back
        code += asm.addiu("t2", "t2", 1)                         # delay: t2++

        # t2 = pointer to null terminator, s1 = start
        code += asm.subu("a2", "t2", "s1")                      # a2 = length

        # ━━ PHASE 8: Call luaL_loadbuffer(L, buf, len, name) ━━━━━━━━━
        code += asm.move("a0", "s0")                              # a0 = L
        code += asm.move("a1", "s1")                              # a1 = script buf
        # a2 already = length
        code += asm.li("a3", CHUNK_NAME_ADDR)                    # a3 = "=mod"
        code += asm.jal(LUAL_LOADBUF)
        code += asm.nop()                                         # delay slot

        # ━━ PHASE 9: Check luaL_loadbuffer result ━━━━━━━━━━━━━━━━━━━━
        code += asm.bne("v0", "zero", 0)                         # if error, skip pcall
        error_skip_pos = len(code) - 4
        code += asm.nop()                                         # delay slot

        # ━━ PHASE 10: Call lua_pcall(L, 0, 0, 0) ━━━━━━━━━━━━━━━━━━━━━
        code += asm.move("a0", "s0")                              # a0 = L
        code += asm.move("a1", "zero")                            # a1 = 0 args
        code += asm.move("a2", "zero")                            # a2 = 0 results
        code += asm.move("a3", "zero")                            # a3 = 0 errfunc
        code += asm.jal(LUA_PCALL)
        code += asm.nop()                                         # delay slot

        # ━━ PHASE 11: Set status = 2 (done) ━━━━━━━━━━━━━━━━━━━━━━━━━━
        code += asm.lui("t0", STATUS_FLAG >> 16)
        code += asm.addiu("t1", "zero", 2)
        code += asm.sw("t1", STATUS_FLAG & 0xFFFF, "t0")
        code += asm.beq("zero", "zero", 0)                       # jump to restore
        restore_skip_pos = len(code) - 4
        code += asm.nop()                                         # delay slot

        # ━━ Error path: set status = 3 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        error_target = len(code)
        code += asm.lui("t0", STATUS_FLAG >> 16)
        code += asm.addiu("t1", "zero", 3)
        code += asm.sw("t1", STATUS_FLAG & 0xFFFF, "t0")

        # ━━ PHASE 12: Restore registers ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        restore_target = len(code)
        code += asm.lw("ra", 0, "sp")
        code += asm.lw("s0", 4, "sp")
        code += asm.lw("s1", 8, "sp")
        code += asm.lw("s2", 12, "sp")
        code += asm.lw("a0", 16, "sp")
        code += asm.lw("a1", 20, "sp")
        code += asm.lw("a2", 24, "sp")
        code += asm.lw("a3", 28, "sp")
        code += asm.lw("t0", 32, "sp")
        code += asm.lw("t1", 36, "sp")
        code += asm.lw("t2", 40, "sp")
        code += asm.lw("t3", 44, "sp")
        code += asm.lw("v0", 48, "sp")
        code += asm.lw("v1", 52, "sp")
        code += asm.addiu("sp", "sp", 64)

        # ━━ PHASE 13: Clear re-entrancy guard ━━━━━━━━━━━━━━━━━━━━━━━━
        clear_reentrant_target = len(code)
        code += asm.lui("t0", REENTRANT_FLAG >> 16)
        code += asm.sw("zero", REENTRANT_FLAG & 0xFFFF, "t0")   # reentrant = 0

        # ━━ PHASE 14: Execute original instruction + return ━━━━━━━━━━━
        exec_original_target = len(code)
        # Embed the original instruction directly (patched in at install time)
        code += orig_insn_bytes                                   # original hooked instruction
        # Jump back to hook_addr + 4 (placeholder, patched at install)
        code += asm.nop()                                         # placeholder: J hook+4
        code += asm.nop()                                         # delay slot

        # ━━ Patch all branch offsets ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        code_arr = bytearray(code)

        # Patch re-entrancy skip -> exec_original_target
        _patch_branch(code_arr, reentrant_skip_pos, exec_original_target, 0x05, 9, 0)

        # Patch no-trigger skip -> clear_reentrant_target
        _patch_branch(code_arr, no_trigger_skip_pos, clear_reentrant_target, 0x04, 9, 0)

        # Patch error skip -> error_target
        _patch_branch(code_arr, error_skip_pos, error_target, 0x05, 2, 0)

        # Patch restore skip -> restore_target
        _patch_branch(code_arr, restore_skip_pos, restore_target, 0x04, 0, 0)

        return bytes(code_arr)

    def install(self, hook_addr: int = None):
        """Prepare the code cave for Lua injection.

        IMPORTANT: Due to PCSX2's JIT recompiler, code patches written via
        WriteProcessMemory are IGNORED. The hook and code cave must be
        installed via pnach patches (applied before JIT compilation).

        Run GeneratePnach.py first to create the pnach, then restart the game.
        This method verifies the pnach is active and prepares data regions.

        If pnach is not detected, falls back to direct memory writes
        (which may work in interpreter mode or if PCSX2 invalidates cache).
        """
        if hook_addr is None:
            hook_addr = LUA_GETTOP

        self.hook_addr = hook_addr

        print(f"[*] Preparing Lua code cave...")
        print(f"    Script buffer:   0x{SCRIPT_BUF:08X}")
        print(f"    Trigger flag:    0x{TRIGGER_FLAG:08X}")
        print(f"    Code cave:       0x{CAVE_ADDR:08X}")
        print(f"    Hook point:      0x{hook_addr:08X}")

        # 1. Check if pnach already installed the hook
        hook_bytes = self.bridge.read_bytes(hook_addr, 4)
        hook_val = struct.unpack("<I", hook_bytes)[0] if hook_bytes else 0
        hook_op = (hook_val >> 26) & 0x3F
        hook_target = (hook_val & 0x03FFFFFF) << 2

        pnach_active = (hook_op == 0x02 and hook_target == CAVE_ADDR)

        if pnach_active:
            print(f"    [OK] Pnach hook detected! (J 0x{hook_target:08X})")

            # Verify cave code is present — game init may have wiped the data region
            cave_first = self.bridge.read_bytes(CAVE_ADDR, 4)
            cave_val = struct.unpack("<I", cave_first)[0] if cave_first else 0
            cave_op = (cave_val >> 26) & 0x3F
            if cave_op == 0x0F:  # LUI — expected first instruction
                print(f"    [OK] Code cave verified at 0x{CAVE_ADDR:08X}")
            else:
                # Cave code was wiped by game init — rewrite it via WriteProcessMemory.
                # The hook is in .text (JIT-cached, persists), but the cave is in a data
                # region that gets cleared during ELF loading. WriteProcessMemory works
                # fine for data regions since the JIT doesn't cache them.
                print(f"    [!!] Cave code wiped by game init — rewriting via bridge...")
                orig_insn = self.bridge.read_bytes(hook_addr + 4, 4)  # insn AFTER hook (not the J)
                # The original instruction at hook_addr was LW v1, 0x000C(a0) = 0x8C83000C
                # The pnach replaced it with J, but the cave needs to execute it before returning.
                orig_insn_at_hook = b'\x0C\x00\x83\x8C'  # 8C83000C little-endian
                cave_code = self._build_code_cave(orig_insn_at_hook)
                return_jump = asm.j(hook_addr + 4) + asm.nop()
                cave_code = cave_code[:-8] + return_jump
                self.bridge.write_bytes(CAVE_ADDR, cave_code)
                # Verify it took
                verify = self.bridge.read_bytes(CAVE_ADDR, 4)
                verify_val = struct.unpack("<I", verify)[0] if verify else 0
                if (verify_val >> 26) & 0x3F == 0x0F:
                    print(f"    [OK] Cave code rewritten successfully at 0x{CAVE_ADDR:08X}")
                else:
                    print(f"    [!!] Cave rewrite FAILED (got 0x{verify_val:08X})")
        else:
            print(f"    [!!] Pnach hook NOT detected at 0x{hook_addr:08X}")
            print(f"         Found: 0x{hook_val:08X} (expected J 0x{CAVE_ADDR:08X})")
            print()
            print(f"    Attempting PINE installation (JIT-cache-safe)...")
            try:
                from PINEClient import install_hook_via_pine
                if install_hook_via_pine():
                    print(f"    [OK] Hook installed via PINE!")
                    # Keep PINE connection for execute()
                    from PINEClient import PINEClient
                    self._pine = PINEClient()
                    self._pine.connect(timeout=2)
                else:
                    print(f"    [!!] PINE install failed. Enable PINE in PCSX2 settings.")
                    return
            except Exception as e:
                print(f"    [!!] PINE not available: {e}")
                print(f"         Enable EnablePINE=true in PCSX2.ini and restart.")
                return

        # Try to get PINE connection for reliable execute()
        if not self._pine:
            self._get_pine()

        # 2. Write chunk name + clear flags (use PINE if available)
        if self._pine:
            self._pine.write_bytes(CHUNK_NAME_ADDR, CHUNK_NAME + b'\x00' * 3)
            self._pine.write32(TRIGGER_FLAG, 0)
            self._pine.write32(STATUS_FLAG, 0)
            self._pine.write32(REENTRANT_FLAG, 0)
        else:
            self.bridge.write_bytes(CHUNK_NAME_ADDR, CHUNK_NAME)
            self.bridge.write_u32(TRIGGER_FLAG, 0)
            self.bridge.write_u32(STATUS_FLAG, 0)
            self.bridge.write_u32(REENTRANT_FLAG, 0)

        self.installed = True
        print(f"[+] Code cave ready. Injection pipeline active.")

    def uninstall(self):
        """Deactivate injection (clear flags). Does NOT remove pnach hook.

        The pnach-installed hook remains active but is harmless — it checks
        the trigger flag (which we clear) and immediately returns.
        """
        if not self.installed:
            return

        # Clear all flags — the cave will see trigger=0 and do nothing
        self.bridge.write_u32(TRIGGER_FLAG, 0)
        self.bridge.write_u32(STATUS_FLAG, 0)
        self.bridge.write_u32(REENTRANT_FLAG, 0)

        # Only restore code if we did direct writes (non-pnach fallback)
        if self.orig_insn_bytes:
            self.bridge.write_bytes(self.hook_addr, self.orig_insn_bytes)
            print("[+] Hook removed, original code restored.")
        else:
            print("[+] Injection deactivated (pnach hook remains — harmless).")

        self.installed = False

    def execute(self, lua_script: str, timeout: float = 3.0) -> bool:
        """Execute a Lua script in the game's VM.

        Uses PINE IPC if available (more reliable — goes through PCSX2's
        memory system). Falls back to MemoryBridge (WriteProcessMemory).

        Returns True on success, False on error/timeout.
        """
        if not self.installed:
            print("[!] Code cave not installed. Call install() first.")
            return False

        script_bytes = lua_script.encode("ascii", errors="replace") + b"\x00"
        if len(script_bytes) > SCRIPT_MAXLEN:
            print(f"[!] Script too long ({len(script_bytes)} > {SCRIPT_MAXLEN})")
            return False

        # Pre-flight: if a previous execution left flags in a bad state, reset them.
        # This prevents a stuck trigger/re-entrancy flag from causing a freeze.
        self._ensure_flags_clean()

        # Use PINE if available (reliable), fall back to bridge
        if self._pine:
            return self._execute_pine(script_bytes, timeout)
        else:
            return self._execute_bridge(script_bytes, lua_script, timeout)

    def _ensure_flags_clean(self):
        """Reset flags if a previous execution left them stuck.

        A stuck re-entrancy flag (=1) blocks all cave execution.
        A stuck trigger flag (=1) with status idle means the hook isn't firing.
        Either state can cause apparent freezes or broken injection.
        """
        try:
            if self._pine:
                reentrant = self._pine.read32(REENTRANT_FLAG)
                status = self._pine.read32(STATUS_FLAG)
                trigger = self._pine.read32(TRIGGER_FLAG)
            else:
                reentrant = self.bridge.read_u32(REENTRANT_FLAG)
                status = self.bridge.read_u32(STATUS_FLAG)
                trigger = self.bridge.read_u32(TRIGGER_FLAG)

            if reentrant != 0:
                if self._pine:
                    self._pine.write32(REENTRANT_FLAG, 0)
                else:
                    self.bridge.write_u32(REENTRANT_FLAG, 0)

            if trigger != 0 and status == 0:
                # Trigger set but status never changed — hook didn't fire
                if self._pine:
                    self._pine.write32(TRIGGER_FLAG, 0)
                else:
                    self.bridge.write_u32(TRIGGER_FLAG, 0)
        except Exception:
            pass  # Best-effort cleanup

    def _get_pine(self):
        """Lazily connect to PINE."""
        if self._pine is None:
            try:
                from PINEClient import PINEClient
                pine = PINEClient()
                if pine.connect(timeout=2):
                    self._pine = pine
                else:
                    self._pine = False  # Mark as unavailable
            except Exception:
                self._pine = False
        return self._pine if self._pine else None

    def _execute_pine(self, script_bytes, timeout):
        """Execute via PINE IPC (reliable)."""
        pine = self._pine
        # Clear full buffer to prevent stale data from causing strlen overrun
        # if this write partially fails. Also place a null sentinel at the end.
        clear_len = min(len(script_bytes) + 64, SCRIPT_MAXLEN)
        pine.write_bytes(SCRIPT_BUF, b"\x00" * clear_len)
        pine.write_bytes(SCRIPT_BUF, script_bytes)
        # Safety sentinel: ensure a null exists even if script write was partial
        pine.write_bytes(SCRIPT_BUF + len(script_bytes), b"\x00\x00\x00\x00")
        pine.write32(REENTRANT_FLAG, 0)
        pine.write32(STATUS_FLAG, 0)
        pine.write32(TRIGGER_FLAG, 1)

        start = time.time()
        poll_interval = 0.050
        while time.time() - start < timeout:
            status = pine.read32(STATUS_FLAG)
            if status == 2:
                return True
            elif status == 3:
                return False
            time.sleep(poll_interval)
            poll_interval = min(poll_interval * 2.0, 0.2)
        return False

    def _execute_bridge(self, script_bytes, lua_script, timeout):
        """Execute via MemoryBridge (fallback)."""
        self.bridge.write_u32(TRIGGER_FLAG, 0)
        # Clear enough buffer to cover script + margin, preventing strlen overrun
        clear_len = min(len(script_bytes) + 64, SCRIPT_MAXLEN)
        self.bridge.write_bytes(SCRIPT_BUF, b"\x00" * clear_len)
        self.bridge.write_bytes(SCRIPT_BUF, script_bytes)
        # Safety sentinel after script
        self.bridge.write_bytes(SCRIPT_BUF + len(script_bytes), b"\x00\x00\x00\x00")
        self.bridge.write_u32(STATUS_FLAG, 0)
        self.bridge.write_u32(TRIGGER_FLAG, 1)

        start = time.time()
        poll_interval = 0.050
        while time.time() - start < timeout:
            status = self.bridge.read_u32(STATUS_FLAG)
            if status == 2:
                return True
            elif status == 3:
                print(f"  [!] Lua error in script: {lua_script[:60]}")
                return False
            time.sleep(poll_interval)
            poll_interval = min(poll_interval * 2.0, 0.2)

        trigger = self.bridge.read_u32(TRIGGER_FLAG)
        status = self.bridge.read_u32(STATUS_FLAG)
        reentrant = self.bridge.read_u32(REENTRANT_FLAG)
        print(f"  [!] Timeout ({timeout}s): trigger={trigger} status={status} reentrant={reentrant}")
        return False

    def execute_batch(self, scripts: list, delay: float = 0.05) -> list:
        """Execute multiple Lua scripts in sequence."""
        results = []
        for i, script in enumerate(scripts):
            ok = self.execute(script)
            results.append(ok)
            if ok:
                print(f"  [+] {script[:70]}")
            if ok and delay > 0 and i < len(scripts) - 1:
                time.sleep(delay)
        return results

    def test_injection(self) -> bool:
        """Test that the injection pipeline works with a harmless script."""
        print("[*] Testing injection pipeline...")
        # This Lua expression evaluates to nothing but confirms the VM runs it
        return self.execute("do end", timeout=5.0)


def _patch_branch(code_arr: bytearray, patch_pos: int, target_offset: int,
                  opcode: int, rs: int, rt: int):
    """Patch a MIPS branch instruction with the correct relative offset."""
    offset_words = (target_offset - (patch_pos + 4)) // 4
    insn = (opcode << 26) | (rs << 21) | (rt << 16) | (offset_words & 0xFFFF)
    struct.pack_into("<I", code_arr, patch_pos, insn)


# ── Preset Script Library ────────────────────────────────────────────────────

SCRIPTS = {
    "traffic_boost": [
        "Traffic_SetZoneDensity(1, 2.5)",
        "Traffic_SetZoneCivDensity(1, 3.0)",
        "Traffic_SetZoneSpawnerCap(1, 25)",
    ],
    "traffic_warzone": [
        "Traffic_SetZoneDensity(1, 4.0)",
        "Traffic_SetZoneCivDensity(1, 1.0)",
        "Traffic_SetZoneSpawnerCap(1, 40)",
    ],
    "shop_unlock": [
        'Shop_UnlockItem("template_support_deliverH2")',
        'Shop_UnlockItem("template_support_deliverH3")',
        'Shop_SetShopLocked(false)',
    ],
    "faction_friendly": [
        'Faction_ModifyRelation("SK", "ExOps", 0.3)',
        'Faction_ModifyRelation("AN", "ExOps", 0.3)',
        'Faction_ModifyRelation("China", "ExOps", 0.2)',
        'Faction_ModifyRelation("Mafia", "ExOps", 0.2)',
    ],
    "faction_chaos": [
        'Faction_ModifyRelation("NK", "SK", -0.5)',
        'Faction_ModifyRelation("NK", "China", -0.5)',
        'Faction_ModifyRelation("SK", "Mafia", -0.3)',
        'Faction_ModifyRelation("China", "Mafia", -0.3)',
        'Faction_ModifyRelation("AN", "NK", -0.5)',
    ],
    "money_boost": [
        "Player_AdjustMoney(500000)",
    ],
    "world_alive": [
        "Traffic_SetZoneDensity(1, 2.0)",
        "Traffic_SetZoneCivDensity(1, 2.5)",
        "Traffic_SetZoneSpawnerCap(1, 20)",
        'Faction_ModifyRelation("SK", "ExOps", 0.2)',
        'Faction_ModifyRelation("AN", "ExOps", 0.2)',
    ],
    "combat_enhance": [
        'Utility_SetDamageTableValue("ARMOR_TYPE_DEFAULT", "DAMAGE_TYPE_BULLET", 1.2)',
        'Utility_SetDamageTableValue("ARMOR_TYPE_DEFAULT", "DAMAGE_TYPE_EXPLOSION", 1.5)',
    ],
    "atmosphere": [
        "Renderer_SetAmbientLight(0.85, 0.78, 0.65)",
        "Renderer_SetMotionBlur(0.15)",
    ],
    "atmosphere_sunset": [
        "Renderer_SetAmbientLight(1.0, 0.65, 0.40)",
        "Renderer_SetMotionBlur(0.10)",
    ],
    "atmosphere_night": [
        "Renderer_SetAmbientLight(0.25, 0.28, 0.40)",
        "Renderer_SetMotionBlur(0.05)",
    ],
    "camera_wide": [
        "Camera_SetZoom(0.7)",
    ],
    "camera_close": [
        "Camera_SetZoom(1.3)",
    ],
    "slowmo": [
        "Utility_SetTimeScale(0.5)",
    ],
    "normal_speed": [
        "Utility_SetTimeScale(1.0)",
    ],
    "passenger_mode": [
        # Find nearest vehicle and ride as passenger
        # Uses Utility_GetActorsInRange to find vehicles, then ReceiveRider
        'local actors = Utility_GetActorsInRange(0, 0, 0, 50)\n'
        'if actors then\n'
        '  for i, actor in ipairs(actors) do\n'
        '    local veh = Utility_GetActorsVehicle(actor)\n'
        '    if veh then\n'
        '      ActorVehicle_ReceiveRider(veh, "player")\n'
        '      Ui_PrintHudMessage("Boarding vehicle as passenger!")\n'
        '      break\n'
        '    end\n'
        '  end\n'
        'end',
    ],
    "passenger_nearest": [
        # Simpler: just try to board the nearest vehicle directly
        'ActorVehicle_ReceiveRider("nearest_vehicle", "player")',
    ],
    "kick_passengers": [
        'ActorVehicle_KickOutAllPassengers("player_vehicle")',
    ],
    "unlock_all_shop": [
        'Shop_SetShopLocked(false)',
        'Shop_UnlockItem("template_support_deliverH2")',
        'Shop_UnlockItem("template_support_deliverH3")',
        'Shop_UnlockItem("template_support_carpetbomb")',
        'Shop_UnlockItem("template_support_surgical")',
        'Shop_UnlockItem("template_support_moab")',
    ],
    "god_mode": [
        'Utility_SetDamageTableValue("ARMOR_TYPE_HERO", "DAMAGE_TYPE_BULLET", 0.0)',
        'Utility_SetDamageTableValue("ARMOR_TYPE_HERO", "DAMAGE_TYPE_EXPLOSION", 0.0)',
        'Utility_SetDamageTableValue("ARMOR_TYPE_HERO", "DAMAGE_TYPE_ROCKET", 0.0)',
        'Utility_SetDamageTableValue("ARMOR_TYPE_HERO", "DAMAGE_TYPE_MISSILE", 0.0)',
    ],
    "realistic_damage": [
        'Utility_SetDamageTableValue("ARMOR_TYPE_HERO", "DAMAGE_TYPE_BULLET", 0.5)',
        'Utility_SetDamageTableValue("ARMOR_TYPE_HERO", "DAMAGE_TYPE_EXPLOSION", 1.0)',
        'Utility_SetDamageTableValue("ARMOR_TYPE_HERO", "DAMAGE_TYPE_ROCKET", 1.5)',
        'Utility_SetDamageTableValue("ARMOR_TYPE_HERO", "DAMAGE_TYPE_MISSILE", 2.0)',
    ],
    "refill_ammo": [
        "Player_RefillWeaponAmmo()",
    ],
    "spawn_warzone": [
        "Traffic_SetZoneDensity(1, 5.0)",
        "Traffic_SetZoneCivDensity(1, 0.5)",
        "Traffic_SetZoneSpawnerCap(1, 50)",
        "Traffic_SetZoneUrgency(1, 1.0)",
        'Faction_ModifyRelation("NK", "SK", -1.0)',
        'Faction_ModifyRelation("NK", "China", -0.8)',
        'Faction_ModifyRelation("NK", "AN", -1.0)',
    ],
    "hud_message_test": [
        'Ui_PrintHudMessage("Mercenaries Revamped Edition - Lua injection active!")',
    ],
    "bullet_time": [
        "Utility_SetTimeScale(0.3)",
        "Renderer_SetMotionBlur(0.25)",
        'Ui_PrintHudMessage("Bullet Time!")',
    ],
    "bullet_time_off": [
        "Utility_SetTimeScale(1.0)",
        "Renderer_SetMotionBlur(0.0)",
    ],
    "hardcore_mode": [
        'Mission_EnableGlobalGpsJammer(true)',
        'Utility_SetDamageTableValue("ARMOR_TYPE_HERO", "DAMAGE_TYPE_BULLET", 0.5)',
        'Utility_SetDamageTableValue("ARMOR_TYPE_HERO", "DAMAGE_TYPE_EXPLOSION", 1.2)',
        'Faction_ModifyRelation("SK", "ExOps", -0.5)',
        'Faction_ModifyRelation("AN", "ExOps", -0.3)',
        'Faction_ModifyRelation("China", "ExOps", -0.4)',
        'Ui_PrintHudMessage("HARDCORE MODE - No GPS, hostile world")',
    ],
    "debug_menu": [
        "Debug_EnableDebugMenu(true)",
        'Ui_PrintHudMessage("Debug menu enabled (F-keys)")',
    ],
    "nuke_strike": [
        "Building_SiloPrepare(0)",
        "Building_SiloLaunch(0)",
        "Camera_Shake(2.0)",
        'Ui_PrintHudMessage("NUKE INBOUND!")',
    ],
    "cinema_mode": [
        "Video_LetterboxEffect(true)",
        "Renderer_SetMotionBlur(0.10)",
        "Renderer_SetAmbientLight(0.80, 0.70, 0.55)",
        'Ui_EnableHud(false)',
    ],
    "cinema_off": [
        "Video_LetterboxEffect(false)",
        "Renderer_SetMotionBlur(0.0)",
        'Ui_EnableHud(true)',
    ],
    "horn_chaos": [
        "Ai_EnableHornResponse(true)",
    ],
    "emp_effect": [
        "Video_TVEffect(true)",
        "Mission_EnableGlobalGpsJammer(true)",
        'Ui_PrintHudMessage("EMP ACTIVATED")',
    ],
    "emp_off": [
        "Video_TVEffect(false)",
        "Mission_EnableGlobalGpsJammer(false)",
    ],
    "hud_on": [
        "Ui_EnableHud(true)",
    ],
    "hud_off": [
        "Ui_EnableHud(false)",
    ],
    "hud_clean": [
        # Minimal HUD: disable non-essential elements for cleaner screen
        "Ui_SetFactionMoodVisible(false)",
    ],
    "hud_full": [
        # Restore full HUD visibility
        "Ui_EnableHud(true)",
        "Ui_SetFactionMoodVisible(true)",
    ],
}


if __name__ == "__main__":
    print("=" * 55)
    print("  Mercenaries PoD — Lua Code Cave Injector")
    print("  Re-entrancy guard: ACTIVE")
    print("=" * 55)

    bridge = GameBridge()
    try:
        bridge.attach()
    except RuntimeError as e:
        print(f"[!] {e}")
        print("\nAvailable script presets:")
        for name, scripts in SCRIPTS.items():
            print(f"  {name}:")
            for s in scripts:
                print(f"    {s}")
        sys.exit(1)

    cave = LuaCodeCave(bridge)
    cave.install()

    # Auto-test
    print()
    if cave.test_injection():
        print("[+] Injection pipeline VERIFIED — Lua VM responding!")
    else:
        print("[!] Injection test failed — check hook point and game state")

    print("\nAvailable presets:")
    for i, (name, scripts) in enumerate(SCRIPTS.items()):
        print(f"  {i+1}. {name} ({len(scripts)} commands)")

    print(f"  {len(SCRIPTS)+1}. Custom Lua command")
    print(f"  {len(SCRIPTS)+2}. Test injection")
    print(f"  {len(SCRIPTS)+3}. Uninstall and quit")

    while True:
        try:
            choice = input("\nChoice: ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if choice.isdigit():
            idx = int(choice) - 1
            preset_names = list(SCRIPTS.keys())
            if 0 <= idx < len(preset_names):
                name = preset_names[idx]
                print(f"\n  Executing '{name}'...")
                cave.execute_batch(SCRIPTS[name])
            elif idx == len(preset_names):
                cmd = input("  Lua> ").strip()
                if cmd:
                    cave.execute(cmd)
            elif idx == len(preset_names) + 1:
                cave.test_injection()
            elif idx == len(preset_names) + 2:
                break
        elif choice.startswith("lua "):
            cave.execute(choice[4:])

    cave.uninstall()
    bridge.detach()
