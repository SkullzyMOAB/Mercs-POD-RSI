"""
Mercenaries PoD — Complete Game Mod Runtime
=============================================
The all-in-one runtime mod that attaches to PCSX2 and applies
every enhancement: Lua injection, traffic boost, faction control,
money tools, combat tweaks, atmosphere, and the full Zero Engine API.

Run alongside the game for the complete modded experience.

Usage:
    python GameMod.py              # Interactive mod console
    python GameMod.py --auto       # Auto-apply all enhancements
    python GameMod.py --inject     # Install code cave + run preset scripts
    python GameMod.py --test       # Test injection pipeline only
"""

import sys
import time
import json
import argparse
from pathlib import Path

if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, str(Path(__file__).parent))
from MemoryBridge import GameBridge
from CodeCave import LuaCodeCave, SCRIPTS, TRIGGER_FLAG, STATUS_FLAG, REENTRANT_FLAG


BANNER = """
+=======================================================+
|     MERCENARIES: PLAYGROUND OF DESTRUCTION             |
|              REVAMPED EDITION                          |
|                                                        |
|  309 Lua functions | Code cave injection               |
|  Re-entrancy safe  | Zero Engine fully mapped          |
+=======================================================+
"""

# Combined enhancement profiles
PROFILES = {
    "full": {
        "name": "Full Enhancement Suite",
        "scripts": [
            # World density
            "Traffic_SetZoneDensity(1, 2.0)",
            "Traffic_SetZoneCivDensity(1, 2.5)",
            "Traffic_SetZoneSpawnerCap(1, 20)",
            # Faction relations (make allies friendlier)
            'Faction_ModifyRelation("SK", "ExOps", 0.25)',
            'Faction_ModifyRelation("AN", "ExOps", 0.25)',
            'Faction_ModifyRelation("China", "ExOps", 0.15)',
            'Faction_ModifyRelation("Mafia", "ExOps", 0.15)',
            # Shop access
            'Shop_UnlockItem("template_support_deliverH2")',
            'Shop_UnlockItem("template_support_deliverH3")',
        ],
    },
    "immersive": {
        "name": "Immersive World",
        "scripts": [
            "Traffic_SetZoneDensity(1, 3.0)",
            "Traffic_SetZoneCivDensity(1, 4.0)",
            "Traffic_SetZoneSpawnerCap(1, 30)",
            "Renderer_SetAmbientLight(0.85, 0.78, 0.65)",
        ],
    },
    "combat": {
        "name": "Enhanced Combat",
        "scripts": [
            "Traffic_SetZoneDensity(1, 2.5)",
            "Traffic_SetZoneSpawnerCap(1, 25)",
            'Faction_ModifyRelation("NK", "ExOps", -0.3)',
        ],
    },
}


def wait_for_pcsx2(timeout=180):
    """Wait for PCSX2 to start and a game to be loaded."""
    bridge = GameBridge()
    print("[*] Waiting for PCSX2 with game loaded...")

    start = time.time()
    attempt = 0
    while time.time() - start < timeout:
        try:
            bridge.attach()
            return bridge
        except RuntimeError:
            attempt += 1
            if attempt % 5 == 0:
                elapsed = int(time.time() - start)
                print(f"    Still waiting... ({elapsed}s / {timeout}s)")
            time.sleep(2)

    print("[!] Timeout waiting for PCSX2")
    return None


def wait_for_gameplay(bridge, timeout=120):
    """Wait for the game to be past menus and into gameplay.

    The Lua VM isn't active during boot/loading screens.
    We detect gameplay by checking if the lua_State pointer is valid.
    """
    from CodeCave import LUA_STATE_PTR
    print("[*] Waiting for gameplay (Lua VM active)...")

    start = time.time()
    while time.time() - start < timeout:
        try:
            lua_state = bridge.read_u32(LUA_STATE_PTR)
            if lua_state != 0 and lua_state != 0xFFFFFFFF:
                # Verify it's a plausible pointer (in EE RAM range)
                if 0x00100000 < lua_state < 0x02000000:
                    print(f"[+] Lua VM active! (L = 0x{lua_state:08X})")
                    return True
        except Exception:
            pass
        time.sleep(2)

    print("[!] Timeout waiting for gameplay")
    return False


def apply_profile(cave, profile_name):
    """Apply a named enhancement profile."""
    if profile_name not in PROFILES:
        print(f"[!] Unknown profile: {profile_name}")
        return False

    profile = PROFILES[profile_name]
    print(f"\n[*] Applying profile: {profile['name']}")
    results = cave.execute_batch(profile["scripts"])
    success = sum(1 for r in results if r)
    total = len(results)
    print(f"    {success}/{total} scripts executed successfully")
    return success == total


def auto_mode(bridge):
    """Apply all safe enhancements automatically with monitoring."""
    print(BANNER)
    print("[MODE] Auto-Enhancement")
    print()

    # Wait for gameplay
    if not wait_for_gameplay(bridge):
        print("[!] Could not detect active gameplay. Trying anyway...")

    cave = LuaCodeCave(bridge)
    cave.install()

    # Test injection first
    print()
    if cave.test_injection():
        print("[+] INJECTION VERIFIED — Lua VM responding!")
    else:
        print("[!] Injection test failed. Game may be in menus/loading.")
        print("    Will retry when gameplay starts...")
        # Wait a bit and retry
        time.sleep(10)
        if not cave.test_injection():
            print("[!] Still failing. Continuing anyway — scripts will queue.")

    # Apply full enhancement profile
    print()
    apply_profile(cave, "full")

    # Apply individual presets
    print()
    print("[*] Applying additional presets...")
    for name in ["traffic_boost", "atmosphere"]:
        if name in SCRIPTS:
            print(f"    [{name}]")
            cave.execute_batch(SCRIPTS[name])

    print()
    print("=" * 55)
    print("  ALL ENHANCEMENTS APPLIED")
    print("  Game world is now alive and enhanced.")
    print("=" * 55)
    print()
    print("  Monitoring active — traffic will refresh on zone changes.")
    print("  Press Ctrl+C to stop.")

    # Maintenance loop: re-apply traffic periodically
    # (zones reset on map transitions / fast travel)
    # Use long intervals to avoid stutter from memory writes
    cycle = 0
    try:
        while True:
            time.sleep(60)  # 60s between checks (was 20s — caused stutter)
            cycle += 1
            try:
                # Re-apply traffic density (resets on zone transitions)
                # Batch into a single Lua call to minimize memory writes
                cave.execute(
                    "Traffic_SetZoneDensity(1, 2.0)\n"
                    "Traffic_SetZoneCivDensity(1, 2.5)\n"
                    "Traffic_SetZoneSpawnerCap(1, 20)"
                )

                # Every 5 cycles (~5min), set faction floors (capped, not accumulating)
                if cycle % 5 == 0:
                    cave.execute(
                        'Faction_SetMinimumRelation("SK", "ExOps", -0.1)\n'
                        'Faction_SetMinimumRelation("AN", "ExOps", -0.1)'
                    )

            except Exception as e:
                # Connection lost — try to reconnect
                print(f"\n[!] Error during maintenance: {e}")
                print("    Attempting reconnect...")
                try:
                    bridge.detach()
                    time.sleep(5)
                    bridge.attach()
                    cave = LuaCodeCave(bridge)
                    cave.install()
                    print("[+] Reconnected!")
                except Exception:
                    print("[!] Reconnect failed. Game may have closed.")
                    break

    except KeyboardInterrupt:
        pass

    try:
        cave.uninstall()
    except Exception:
        pass  # Bridge may be disconnected


def interactive_mode(bridge):
    """Full interactive mod console."""
    cave = LuaCodeCave(bridge)

    print(BANNER)
    print("[MODE] Interactive Console")
    print()
    print("  Commands:")
    print("    install      - Install Lua code cave")
    print("    test         - Test injection pipeline")
    print("    lua <cmd>    - Execute Lua command")
    print("    traffic      - Boost traffic density")
    print("    warzone      - Max traffic, min civilians")
    print("    bigwar       - Total warzone (all factions hostile)")
    print("    factions     - Boost faction relations")
    print("    chaos        - Turn factions against each other")
    print("    money [N]    - Give yourself $N (default 500k)")
    print("    shop         - Unlock all shop items")
    print("    ammo         - Refill all weapon ammo")
    print("    alive        - Apply world-alive preset")
    print("    combat       - Apply combat enhancement preset")
    print("    godmode      - Invulnerable to all damage")
    print("    hardcore     - Realistic damage (more lethal)")
    print("    atmosphere   - Apply warm atmosphere lighting")
    print("    sunset       - Golden hour atmosphere")
    print("    night        - Dark nighttime atmosphere")
    print("    widecam      - Wider camera FOV")
    print("    closecam     - Tighter camera FOV")
    print("    slowmo       - Half-speed time scale")
    print("    normalspeed  - Reset time scale")
    print("    hud          - Test HUD message display")
    print("    passenger    - Board nearest vehicle as passenger")
    print("    kickpass     - Kick all passengers from your vehicle")
    print("    bullettime   - Slow-mo bullet time effect")
    print("    btoff        - Turn off bullet time")
    print("    hardcore     - No GPS + hostile world + lethal")
    print("    debug        - Enable developer debug menu")
    print("    nuke         - Launch a nuke strike")
    print("    cinema       - Cinematic mode (letterbox+no HUD)")
    print("    cinemaoff    - Exit cinema mode")
    print("    emp          - EMP effect (TV static + no GPS)")
    print("    empoff       - Disable EMP")
    print("    hudon        - Show HUD")
    print("    hudoff       - Hide HUD")
    print("    hudclean     - Minimal HUD (hide mood bar)")
    print("    hudfull      - Restore full HUD")
    print("    full         - Apply full enhancement profile")
    print("    immersive    - Apply immersive world profile")
    print("    all          - Apply ALL presets")
    print("    status       - Show mod status + diagnostics")
    print("    uninstall    - Remove code cave")
    print("    quit         - Exit")
    print()

    installed = False

    def ensure_installed():
        nonlocal installed
        if not installed:
            cave.install()
            installed = True
            if cave.test_injection():
                print("[+] Injection verified!")
            else:
                print("[!] Injection test failed — game may be loading")

    while True:
        try:
            cmd = input("mod> ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not cmd:
            continue

        parts = cmd.split(None, 1)
        action = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        try:
            if action == "install":
                ensure_installed()

            elif action == "test":
                ensure_installed()
                if cave.test_injection():
                    print("[+] Injection pipeline WORKING!")
                else:
                    print("[!] Test failed")

            elif action == "lua" and arg:
                ensure_installed()
                cave.execute(arg)

            elif action == "traffic":
                ensure_installed()
                cave.execute_batch(SCRIPTS["traffic_boost"])

            elif action == "warzone":
                ensure_installed()
                cave.execute_batch(SCRIPTS["traffic_warzone"])

            elif action == "factions":
                ensure_installed()
                cave.execute_batch(SCRIPTS["faction_friendly"])

            elif action == "chaos":
                ensure_installed()
                cave.execute_batch(SCRIPTS["faction_chaos"])

            elif action == "money":
                ensure_installed()
                amount = int(arg) if arg else 500000
                cave.execute(f"Player_AdjustMoney({amount})")

            elif action == "shop":
                ensure_installed()
                cave.execute_batch(SCRIPTS["unlock_all_shop"])

            elif action == "ammo":
                ensure_installed()
                cave.execute_batch(SCRIPTS["refill_ammo"])

            elif action == "godmode":
                ensure_installed()
                cave.execute_batch(SCRIPTS["god_mode"])
                print("  [+] God mode ON - invulnerable to all damage")

            elif action == "hardcore":
                ensure_installed()
                cave.execute_batch(SCRIPTS["realistic_damage"])
                print("  [+] Hardcore mode - realistic damage active")

            elif action == "bigwar":
                ensure_installed()
                cave.execute_batch(SCRIPTS["spawn_warzone"])
                print("  [+] Total warzone spawned!")

            elif action == "hud":
                ensure_installed()
                cave.execute_batch(SCRIPTS["hud_message_test"])

            elif action == "alive":
                ensure_installed()
                cave.execute_batch(SCRIPTS["world_alive"])

            elif action == "combat":
                ensure_installed()
                apply_profile(cave, "combat")

            elif action == "passenger":
                ensure_installed()
                cave.execute_batch(SCRIPTS["passenger_mode"])

            elif action == "kickpass":
                ensure_installed()
                cave.execute_batch(SCRIPTS["kick_passengers"])

            elif action == "atmosphere":
                ensure_installed()
                cave.execute_batch(SCRIPTS.get("atmosphere", []))

            elif action == "sunset":
                ensure_installed()
                cave.execute_batch(SCRIPTS["atmosphere_sunset"])

            elif action == "night":
                ensure_installed()
                cave.execute_batch(SCRIPTS["atmosphere_night"])

            elif action == "widecam":
                ensure_installed()
                cave.execute_batch(SCRIPTS["camera_wide"])

            elif action == "closecam":
                ensure_installed()
                cave.execute_batch(SCRIPTS["camera_close"])

            elif action == "bullettime":
                ensure_installed()
                cave.execute_batch(SCRIPTS["bullet_time"])

            elif action == "btoff":
                ensure_installed()
                cave.execute_batch(SCRIPTS["bullet_time_off"])

            elif action in ("hardcore",):
                ensure_installed()
                cave.execute_batch(SCRIPTS["hardcore_mode"])

            elif action == "debug":
                ensure_installed()
                cave.execute_batch(SCRIPTS["debug_menu"])

            elif action == "nuke":
                ensure_installed()
                cave.execute_batch(SCRIPTS["nuke_strike"])

            elif action == "cinema":
                ensure_installed()
                cave.execute_batch(SCRIPTS["cinema_mode"])

            elif action == "cinemaoff":
                ensure_installed()
                cave.execute_batch(SCRIPTS["cinema_off"])

            elif action == "emp":
                ensure_installed()
                cave.execute_batch(SCRIPTS["emp_effect"])

            elif action == "empoff":
                ensure_installed()
                cave.execute_batch(SCRIPTS["emp_off"])

            elif action == "hudon":
                ensure_installed()
                cave.execute_batch(SCRIPTS["hud_on"])
                print("  [+] HUD enabled")

            elif action == "hudoff":
                ensure_installed()
                cave.execute_batch(SCRIPTS["hud_off"])
                print("  [+] HUD hidden")

            elif action == "hudclean":
                ensure_installed()
                cave.execute_batch(SCRIPTS["hud_clean"])
                print("  [+] Minimal HUD active")

            elif action == "hudfull":
                ensure_installed()
                cave.execute_batch(SCRIPTS["hud_full"])
                print("  [+] Full HUD restored")

            elif action == "slowmo":
                ensure_installed()
                cave.execute_batch(SCRIPTS["slowmo"])

            elif action == "normalspeed":
                ensure_installed()
                cave.execute_batch(SCRIPTS["normal_speed"])

            elif action == "full":
                ensure_installed()
                apply_profile(cave, "full")

            elif action == "immersive":
                ensure_installed()
                apply_profile(cave, "immersive")

            elif action == "all":
                ensure_installed()
                print("\n[*] Applying ALL presets...")
                for name, scripts in SCRIPTS.items():
                    print(f"\n  [{name}]")
                    cave.execute_batch(scripts)
                print("\n[*] Applying ALL profiles...")
                for pname in PROFILES:
                    apply_profile(cave, pname)

            elif action == "status":
                print(f"\n  === Mod Status ===")
                print(f"  Code cave installed: {installed}")
                print(f"  Hook address:        0x{cave.hook_addr:08X}" if cave.hook_addr else "  Hook address:        (none)")
                if installed:
                    trigger = bridge.read_u32(TRIGGER_FLAG)
                    status = bridge.read_u32(STATUS_FLAG)
                    reentrant = bridge.read_u32(REENTRANT_FLAG)
                    status_names = {0: "idle", 1: "running", 2: "done", 3: "error"}
                    print(f"  Trigger flag:        {trigger}")
                    print(f"  Status flag:         {status} ({status_names.get(status, 'unknown')})")
                    print(f"  Re-entrancy guard:   {reentrant}")

                    # Check lua_State
                    from CodeCave import LUA_STATE_PTR
                    lua_state = bridge.read_u32(LUA_STATE_PTR)
                    print(f"  lua_State*:          0x{lua_state:08X}")

                    if trigger == 1 and status == 0:
                        print(f"\n  [!] WARNING: Trigger is set but status idle.")
                        print(f"      Hook may not be firing. Is game in gameplay?")

            elif action == "uninstall":
                if installed:
                    cave.uninstall()
                    installed = False

            elif action in ("quit", "exit", "q"):
                break

            else:
                print(f"  Unknown: {action}")

        except Exception as e:
            print(f"  [!] Error: {e}")

    if installed:
        cave.uninstall()


def test_mode(bridge):
    """Just test that injection works and exit."""
    print(BANNER)
    print("[MODE] Injection Test")
    print()

    if not wait_for_gameplay(bridge, timeout=60):
        print("[!] No gameplay detected")

    cave = LuaCodeCave(bridge)
    cave.install()

    print()
    for i in range(3):
        print(f"  Test {i+1}/3...")
        if cave.test_injection():
            print(f"  [+] PASS")
        else:
            print(f"  [!] FAIL")
        time.sleep(0.5)

    print()
    # Try a real script
    print("  Testing real Lua script...")
    if cave.execute("Player_AdjustMoney(1)"):
        print("  [+] Real script execution: PASS")
    else:
        print("  [!] Real script execution: FAIL")

    cave.uninstall()


def main():
    parser = argparse.ArgumentParser(description="Mercenaries PoD Game Mod Runtime")
    parser.add_argument("--auto", action="store_true", help="Auto-apply all enhancements")
    parser.add_argument("--inject", action="store_true", help="Install cave and run presets")
    parser.add_argument("--test", action="store_true", help="Test injection pipeline and exit")
    parser.add_argument("--profile", type=str, help="Apply specific profile (full/immersive/combat)")
    args = parser.parse_args()

    bridge = wait_for_pcsx2()
    if not bridge:
        sys.exit(1)

    try:
        if args.test:
            test_mode(bridge)
        elif args.auto or args.inject:
            auto_mode(bridge)
        elif args.profile:
            if not wait_for_gameplay(bridge):
                print("[!] No gameplay detected, trying anyway...")
            cave = LuaCodeCave(bridge)
            cave.install()
            apply_profile(cave, args.profile)
            cave.uninstall()
        else:
            interactive_mode(bridge)
    finally:
        bridge.detach()
        print("\n[+] Mod runtime stopped.")


if __name__ == "__main__":
    main()
