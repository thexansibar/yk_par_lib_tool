# par_to_assetlib.py
# Usage: blender.exe -b --python par_to_assetlib.py -- <par-or-dir> [out_root]
import sys
import os
import pathlib

# -------------- CONFIG: update this to your addon path if needed --------------
ADDON_PATH = r"C:\Users\bbail\AppData\Roaming\Blender Foundation\Blender\5.0\scripts\addons\yk_par_lib_tool"
# ---------------------------------------------------------------------------

# insert addon root so 'src', 'gmdlib', and 'blender' packages can be imported
sys.path.insert(0, ADDON_PATH)

import bpy
# import local helpers from the addon
from src import read_par, decompress_par
from gmdlib.io import read_gmd_structures, read_abstract_scene_from_filedata_object
from gmdlib.converters.common.to_abstract import FileImportMode, VertexImportMode
# scene creators live under the addon's blender package
from blender.importer.scene_creators.skinned import GMDSkinnedSceneCreator
from blender.importer.scene_creators.unskinned import GMDUnskinnedSceneCreator

def write_asset_blend(out_path: str, gmd_collection, debug=False):
    """
    Write a .blend file containing the provided datablocks.
    Use a set of ID datablocks (what Blender expects).
    """
    objs = [obj for obj in gmd_collection.objects]
    meshes = [obj.data for obj in objs if getattr(obj, 'data', None) is not None]
    materials = []
    images = []
    for m in meshes:
        for slot_mat in getattr(m, 'materials', []):
            if slot_mat and slot_mat not in materials:
                materials.append(slot_mat)
    for mat in materials:
        try:
            nodes = getattr(mat, 'node_tree', None)
            if nodes:
                for node in nodes.nodes:
                    if node.type == 'TEX_IMAGE' and getattr(node, 'image', None):
                        img = node.image
                        if img not in images:
                            images.append(img)
        except Exception:
            pass

    id_set = set()
    try:
        id_set.add(gmd_collection)
    except Exception:
        pass
    for o in objs:
        try:
            id_set.add(o)
        except Exception:
            pass
    for m in meshes:
        try:
            id_set.add(m)
        except Exception:
            pass
    for mat in materials:
        try:
            id_set.add(mat)
        except Exception:
            pass
    for img in images:
        try:
            id_set.add(img)
        except Exception:
            pass

    # ensure assets are marked (helps persist flags)
    try:
        gmd_collection.asset_mark()
    except Exception:
        pass
    for o in objs:
        try:
            o.asset_mark()
        except Exception:
            pass
        try:
            if getattr(o, 'data', None):
                o.data.asset_mark()
        except Exception:
            pass

    # try writing
    wrote = False
    try:
        bpy.data.libraries.write(out_path, id_set)
        wrote = True
    except Exception as e:
        if debug:
            print("libraries.write(ids) failed:", e)
        # fallback: try writing only the collection
        try:
            bpy.data.libraries.write(out_path, {gmd_collection})
            wrote = True
        except Exception as e2:
            if debug:
                print("libraries.write(collection) failed:", e2)
            wrote = False
    return wrote

def process_par_file(par_path: str, out_root: pathlib.Path, debug=False):
    par = read_par(par_path)
    decompress_par(par)

    root = par.folders[0] if getattr(par, 'folders', None) and len(par.folders) else None
    if not root:
        if debug:
            print(f"No folders in {par_path}")
        return []

    exported = []
    # simple recursive iterator for .gmd files
    def iter_all(folder):
        for f in getattr(folder, 'files', []) or []:
            if getattr(f, 'name', '').lower().endswith('.gmd'):
                yield f
        for sub in getattr(folder, 'folders', []) or []:
            yield from iter_all(sub)

    for f in iter_all(root):
        try:
            file_bytes = bytes(f.data) if isinstance(f.data, (bytearray, memoryview)) else f.data
            gmd_version, gmd_header, gmd_contents = read_gmd_structures(file_bytes, None)
        except Exception as e:
            if debug:
                print(f"Failed to parse GMD {f.name} in {par_path}: {e}")
            continue

        # try skinned then unskinned
        gmd_scene = None
        scene_creator = None
        try:
            gmd_scene = read_abstract_scene_from_filedata_object(gmd_version, FileImportMode.SKINNED,
                                                                 VertexImportMode.IMPORT_VERTICES,
                                                                 gmd_contents, None)
            gmd_config = None  # not using the add-on logger/config here
            scene_creator = GMDSkinnedSceneCreator(f.name, gmd_scene, gmd_config, None)
            scene_creator.validate_scene()
            gmd_collection = scene_creator.make_collection(bpy.context)
            _ = scene_creator.make_bone_hierarchy(bpy.context, gmd_collection)
            scene_creator.make_objects(bpy.context, gmd_collection, _)
        except Exception:
            try:
                gmd_scene = read_abstract_scene_from_filedata_object(gmd_version, FileImportMode.UNSKINNED,
                                                                     VertexImportMode.IMPORT_VERTICES,
                                                                     gmd_contents, None)
                gmd_config = None
                scene_creator = GMDUnskinnedSceneCreator(f.name, gmd_scene, gmd_config, None)
                scene_creator.validate_scene()
                gmd_collection = scene_creator.make_collection(bpy.context)
                scene_creator.make_objects(bpy.context, gmd_collection)
            except Exception as e:
                if debug:
                    print(f"Failed to create scene for {f.name}: {e}")
                continue

        # ensure output path
        out_root.mkdir(parents=True, exist_ok=True)
        out_path = str(out_root / (pathlib.Path(f.name).stem + ".blend"))

        ok = write_asset_blend(out_path, gmd_collection, debug=debug)
        if ok:
            exported.append(out_path)
        # cleanup the created collections and objects from this background process
        try:
            bpy.data.collections.remove(gmd_collection)
        except Exception:
            pass

    return exported

def main():
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = []

    if len(argv) < 1:
        print("Usage: blender -b --python par_to_assetlib.py -- <par-or-dir> [out_root]")
        return

    source = argv[0]
    out_root = pathlib.Path(argv[1]) if len(argv) > 1 else pathlib.Path.home() / "YakuzaPAR_Assets"
    out_root = out_root.resolve()

    debug = True

    exported_all = []
    if os.path.isdir(source):
        for p in pathlib.Path(source).glob("*.par"):
            exported = process_par_file(str(p), out_root, debug=debug)
            exported_all.extend(exported)
    elif os.path.isfile(source) and source.lower().endswith(".par"):
        exported = process_par_file(source, out_root, debug=debug)
        exported_all.extend(exported)
    else:
        print("Source must be a .par file or a directory containing .par files")
        return

    print("Exported asset files:")
    for e in exported_all:
        print(" -", e)

if __name__ == "__main__":
    main()