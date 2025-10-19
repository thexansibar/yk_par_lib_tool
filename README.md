# yk_par_lib_tool — PAR / GMD Importer Add-on for Blender

This is a tool that unpacks PARs and allows for .gmd import directly from within blender, no partool required!

Uses gmd-io baked in, thanks TurboTurnip for the io functionality and NotYoshi for Neo Yakuza Shader

## What this does:
1. Select a .par of your choosing, and it will unpack and display a file hierarchy that you can sift through manually or search directly for the gmd you want.
2. when you select a .gmd you want, it will import and create a folder and only import the textures that are linked to the related gmd. (they will autolink!) They also come with their _l counterparts.
3. done!

## What I need from you:
1. Test it out! I know this is not a perfect tool but with your input i can fix the problems as they develop.
2. let me know what you think and provide feedback on the ui, speed, and ease of use.


### Special Thanks to:
@Ret-HZ for the idea to utilize the Python PAR reader by @mosamadeeb
@theturboturnip for the IO that reads and imports the GMDs 
@NotYoshi for Neo Yakuza for easy texture linking
@Fronklin (Jhrino) and @sutandotsukai181 for .gmt import / export functionality.
@Mugen for testing