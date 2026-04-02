"""
Mercenaries PoD — Minimal MIPS Assembler
==========================================
Assembles MIPS I instructions into machine code for EE RAM injection.
Only implements instructions needed for the Lua injection code cave.
"""

import struct


def _reg(name):
    """Convert register name to number."""
    _REGS = {
        "zero": 0, "at": 1, "v0": 2, "v1": 3,
        "a0": 4, "a1": 5, "a2": 6, "a3": 7,
        "t0": 8, "t1": 9, "t2": 10, "t3": 11,
        "t4": 12, "t5": 13, "t6": 14, "t7": 15,
        "s0": 16, "s1": 17, "s2": 18, "s3": 19,
        "s4": 20, "s5": 21, "s6": 22, "s7": 23,
        "t8": 24, "t9": 25, "k0": 26, "k1": 27,
        "gp": 28, "sp": 29, "fp": 30, "ra": 31,
    }
    if isinstance(name, int):
        return name
    key = name.lower().strip("$")
    if key in _REGS:
        return _REGS[key]
    return int(name)


def _imm16(val):
    """Convert to signed 16-bit immediate."""
    if val < 0:
        val = val & 0xFFFF
    return val & 0xFFFF


def lui(rt, imm):
    """LUI rt, imm16 — Load Upper Immediate"""
    return struct.pack("<I", (0x0F << 26) | (_reg(rt) << 16) | (imm & 0xFFFF))


def addiu(rt, rs, imm):
    """ADDIU rt, rs, imm16"""
    return struct.pack("<I", (0x09 << 26) | (_reg(rs) << 21) | (_reg(rt) << 16) | _imm16(imm))


def lw(rt, offset, rs):
    """LW rt, offset(rs)"""
    return struct.pack("<I", (0x23 << 26) | (_reg(rs) << 21) | (_reg(rt) << 16) | _imm16(offset))


def sw(rt, offset, rs):
    """SW rt, offset(rs)"""
    return struct.pack("<I", (0x2B << 26) | (_reg(rs) << 21) | (_reg(rt) << 16) | _imm16(offset))


def lbu(rt, offset, rs):
    """LBU rt, offset(rs) — Load Byte Unsigned"""
    return struct.pack("<I", (0x24 << 26) | (_reg(rs) << 21) | (_reg(rt) << 16) | _imm16(offset))


def beq(rs, rt, offset_words):
    """BEQ rs, rt, offset (in words, relative to PC+4)"""
    return struct.pack("<I", (0x04 << 26) | (_reg(rs) << 21) | (_reg(rt) << 16) | _imm16(offset_words))


def bne(rs, rt, offset_words):
    """BNE rs, rt, offset"""
    return struct.pack("<I", (0x05 << 26) | (_reg(rs) << 21) | (_reg(rt) << 16) | _imm16(offset_words))


def j(target_addr):
    """J target — Jump to 26-bit word address"""
    word_addr = (target_addr >> 2) & 0x03FFFFFF
    return struct.pack("<I", (0x02 << 26) | word_addr)


def jal(target_addr):
    """JAL target — Jump and Link"""
    word_addr = (target_addr >> 2) & 0x03FFFFFF
    return struct.pack("<I", (0x03 << 26) | word_addr)


def jr(rs):
    """JR rs — Jump Register"""
    return struct.pack("<I", (_reg(rs) << 21) | 0x08)


def nop():
    """NOP"""
    return struct.pack("<I", 0)


def ori(rt, rs, imm):
    """ORI rt, rs, imm16"""
    return struct.pack("<I", (0x0D << 26) | (_reg(rs) << 21) | (_reg(rt) << 16) | (imm & 0xFFFF))


def addu(rd, rs, rt):
    """ADDU rd, rs, rt"""
    return struct.pack("<I", (_reg(rs) << 21) | (_reg(rt) << 16) | (_reg(rd) << 11) | 0x21)


def subu(rd, rs, rt):
    """SUBU rd, rs, rt"""
    return struct.pack("<I", (_reg(rs) << 21) | (_reg(rt) << 16) | (_reg(rd) << 11) | 0x23)


def move(rd, rs):
    """MOVE rd, rs (pseudo — ADDU rd, rs, zero)"""
    return addu(rd, rs, "zero")


def li(rt, imm32):
    """LI rt, imm32 (pseudo — LUI + ORI)"""
    hi = (imm32 >> 16) & 0xFFFF
    lo = imm32 & 0xFFFF
    code = lui(rt, hi)
    if lo:
        code += ori(rt, rt, lo)
    return code


def strlen_loop(str_reg, len_reg, temp_reg):
    """Generate MIPS code for strlen: len = strlen(str_ptr)
    str_reg: register holding string pointer (MODIFIED: points past end)
    len_reg: register to store length
    temp_reg: scratch register

    NOTE: After this loop, str_reg points past the null terminator.
    Use a copy if you need the original pointer.
    """
    # move len_reg, zero
    # loop: lbu temp_reg, 0(str_reg)
    #        beq temp_reg, zero, done   (offset +3 to skip delay slot addiu)
    #        nop                        (delay slot)
    #        addiu str_reg, str_reg, 1
    #        b loop
    #        addiu len_reg, len_reg, 1  (delay slot)
    # done:
    # Layout (offsets from start of code):
    #   0: move len, zero
    #   4: lbu temp, 0(str)        <-- loop start
    #   8: beq temp, zero, +4      --> done (offset 28)
    #  12: nop                     (delay slot)
    #  16: addiu str, str, 1
    #  20: beq zero, zero, -5      --> loop start (offset 4)
    #  24: addiu len, len, 1       (delay slot)
    #  28: (done)
    code = move(len_reg, "zero")
    code += lbu(temp_reg, 0, str_reg)
    code += beq(temp_reg, "zero", 4)  # +4 words from PC+4 = offset 28
    code += nop()                      # delay slot
    code += addiu(str_reg, str_reg, 1)
    code += beq("zero", "zero", -5)   # -5 words from PC+4 = offset 4
    code += addiu(len_reg, len_reg, 1) # delay slot
    # done: (falls through here)
    return code
