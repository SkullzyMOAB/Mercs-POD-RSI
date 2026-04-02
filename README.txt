MERCENARIES LUA INJECTOR
========================

Injects Lua scripts into Mercenaries: PoD running on PCSX2.
309 game functions exposed. Works at runtime during gameplay.

REQUIRES: Windows 10+, Python 3.10+, PCSX2 2.x, Mercenaries NTSC [SLUS-20932]


SETUP
-----
1. Copy cheats/23510F99.pnach into your PCSX2 cheats folder.
2. Enable Cheats in PCSX2 (Settings > Game Properties).
3. Optional: Enable PINE in PCSX2 (Settings > Advanced) for better reliability.
4. Restart PCSX2.


USAGE
-----
1. Start PCSX2, load Mercenaries, get into gameplay.
2. Open terminal here, run: python GameMod.py
3. Type: install
4. Type: test
5. If test passes, you're good. Type commands:

   lua <command>   - Run Lua directly
   traffic         - More NPCs/vehicles
   money 500000    - Add cash
   godmode         - No damage
   atmosphere      - Cinematic lighting
   bullettime      - Slow motion
   cinema          - Letterbox, no HUD
   status          - Show diag
   quit            - Exit

   Full list shown on startup.

Auto mode (applies everything, runs in background):
   python GameMod.py --auto


COMMANDS REFERENCE
------------------
   install, test, lua <cmd>, traffic, warzone, bigwar,
   factions, chaos, money [N], shop, ammo, godmode, hardcore,
   atmosphere, sunset, night, widecam, closecam, slowmo,
   normalspeed, hud, passenger, kickpass, bullettime, btoff,
   debug, nuke, cinema, cinemaoff, emp, empoff,
   hudon, hudoff, hudclean, hudfull,
   full, immersive, all, status, uninstall, quit

Custom Lua examples:
   lua Player_AdjustMoney(100000)
   lua Traffic_SetZoneDensity(1, 3.0)
   lua Faction_ModifyRelation("SK", "ExOps", 0.5)
   lua Ui_PrintHudMessage("Test")
   lua Camera_Shake(1.5)

See lua_functions.json for all 309 functions.


IF ITS FUCKING UP SOMEHOW
---------------
"Could not locate EE RAM" - Game must be loaded past BIOS.
"Injection test failed" - Must be in gameplay, not menus. Cheats must be ON.
Game freezes - Run GameMod.py, type "status". If re-entrancy shows 1, type "install" to reset.


FILES
-----
GameMod.py         - Mod console
CodeCave.py        - MIPS code cave + Lua engine
MemoryBridge.py  
MIPSAssembler.py 
PINEClient.py 
lua_functions.json 
cheats/23510F99.pnach 
