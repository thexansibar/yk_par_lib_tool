# yk_par_lib_tool — PAR / GMD Importer Add-on for Blender

This is a tool that unpacks PARs and allows for .gmd import directly from within blender, no partool required!

Uses gmd-io baked in, thanks TurboTurnip for the io functionality and NotYoshi for Neo Yakuza Shader

## What this does:
1. Select a .par of your choosing, and it will unpack and display a file hierarchy that you can sift through manually or search directly for the gmd you want.
2. when you select a .gmd you want, it will import and create a folder and only import the textures that are linked to the related gmd. (they will autolink!) They also come with their _l counterparts.
3. done!

## Whats new:
1. You can import stages! (xan's note: i would advise against this if you dont have a beefy PC, but i did work exceptionally hard on nested par searching so if you wanna go crazy I'm not gonna stop you)
2. its kinda faster? (xan's note: its difficult to optimize a task like this as its unpacking only the files you need in real-time, if you open the console you can see it working exceptionally fast! I think the pars themselves can be unpacked faster, but I need to do more digging if i get more free time again)

Special Thanks to:
@Ret-HZ for the idea to utilize the Python PAR reader by @mosamadeeb
@theturboturnip for the IO that reads and imports the GMDs 
@NotYoshi for Neo Yakuza for easy texture linking
@Mugen for testing modding capabilities.
