import os
import tempfile
from bpy.types import Operator
from bpy.props import StringProperty, BoolProperty
from bpy_extras.io_utils import ImportHelper

from ...src import read_par, decompress_par
import bpy
import pathlib
from ..error_reporter import BlenderErrorReporter
from types import SimpleNamespace
from .gmd_importers import BaseImportGMD
from .scene_creators.skinned import GMDSkinnedSceneCreator
from .scene_creators.unskinned import GMDUnskinnedSceneCreator
from ...gmdlib.io import read_gmd_structures, read_abstract_scene_from_filedata_object
from ...gmdlib.converters.common.to_abstract import FileImportMode, VertexImportMode
from ...gmdlib.errors.error_reporter import StrictErrorReporter
from ..error_reporter import BlenderErrorReporter


class ImportPARAsAssets(BaseImportGMD, Operator, ImportHelper):
    """Import a .par archive and load contained .gmd files as assets into Blender"""
    bl_idname = "import_scene.par_gmds"
    bl_label = "Import Yakuza PAR (extract .gmd to asset library)"

    filename_ext = ".par"
    filter_glob: StringProperty(default="*.par", options={"HIDDEN"})

    strict: BoolProperty(name="Strict File Import", default=True)
    output_dir: StringProperty(
        name="Asset Output Directory",
        description="Folder where per-GMD .blend asset files will be written. If empty, assets remain in current file.",
        subtype='DIR_PATH',
        default=""
    )
    debug_logging: BoolProperty(name="Debug Logging", default=False)

    # `BaseImportGMD` already provides `create_logger` and `create_gmd_config`

    def execute(self, context):
        # Allow immediate import when a single .par file is selected via File > Import
        prefs = context.preferences.addons.get('yk_par_lib_tool')
        prefs_obj = prefs.preferences if prefs else None

        if getattr(self, 'filepath', None) and os.path.isfile(self.filepath) and self.filepath.lower().endswith('.par'):
            par_entries = [SimpleNamespace(path=self.filepath)]
        else:
            if not prefs_obj:
                self.report({'ERROR'}, "yk_par_lib_tool addon preferences not found. Please enable the addon or provide a .par file via File > Import.")
                return {'CANCELLED'}
            par_entries = getattr(prefs_obj, 'par_files', [])
            if not par_entries:
                self.report({'ERROR'}, "No .par files configured in Add-on preferences. Open Preferences > Add-ons > yk_par_lib_tool and add .par files, or import a .par directly via File > Import.")
                return {'CANCELLED'}

        error = self.create_logger()
        imported = 0
        added_dirs = set()

        # Process each configured .par file
        for entry in par_entries:
            path = entry.path
            if not path or not os.path.isfile(path) or not path.lower().endswith('.par'):
                self.report({'WARNING'}, f"Skipping invalid .par entry: {path}")
                continue

            # Read the PAR using existing helpers and decompress in-place
            par = read_par(path)
            decompress_par(par)

            # Helper to iterate files in the folder tree (par.folders[0] is the root folder in read_par)
            def iter_folder_files(folder):
                for file in getattr(folder, 'files', []) or []:
                    yield file
                for sub in getattr(folder, 'folders', []) or []:
                    yield from iter_folder_files(sub)

            root_folder = par.folders[0] if getattr(par, 'folders', None) and len(par.folders) else None
            if root_folder is None:
                self.report({'WARNING'}, f"PAR '{path}' contains no folders/files")
                continue

            # Default output root is ~/YakuzaPAR_Assets; write all assets into this single root
            out_root = pathlib.Path(self.output_dir) if self.output_dir else pathlib.Path.home() / 'YakuzaPAR_Assets'
            out_root.mkdir(parents=True, exist_ok=True)
            out_dir = out_root

            # Iterate files in the PAR and import .gmd files
            for f in iter_folder_files(root_folder):
                    if not getattr(f, 'name', '').lower().endswith('.gmd'):
                        continue

                    # Work with bytes directly using read_gmd_structures which accepts path or bytes
                    try:
                        # Ensure we pass bytes to the GMD reader (it accepts path/str/bytes)
                        file_bytes = bytes(f.data) if isinstance(f.data, (bytearray, memoryview)) else f.data
                        gmd_version, gmd_header, gmd_contents = read_gmd_structures(file_bytes, error)
                    except Exception as e:
                        self.report({'ERROR'}, f"Failed to parse GMD {f.name}: {e}")
                        continue

                    try:
                        # Try skinned import first
                        gmd_scene = read_abstract_scene_from_filedata_object(gmd_version, FileImportMode.SKINNED,
                                                                             VertexImportMode.IMPORT_VERTICES,
                                                                             gmd_contents, error)
                        gmd_config = self.create_gmd_config(gmd_version, error)

                        scene_creator = GMDSkinnedSceneCreator(f.name, gmd_scene, gmd_config, error)
                        scene_creator.validate_scene()

                        gmd_collection = scene_creator.make_collection(context)
                        gmd_armature = scene_creator.make_bone_hierarchy(context, gmd_collection)
                        scene_creator.make_objects(context, gmd_collection, gmd_armature)

                    except Exception:
                        # Fallback to unskinned import
                        try:
                            gmd_scene = read_abstract_scene_from_filedata_object(gmd_version, FileImportMode.UNSKINNED,
                                                                                 VertexImportMode.IMPORT_VERTICES,
                                                                                 gmd_contents, error)
                            gmd_config = self.create_gmd_config(gmd_version, error)

                            scene_creator = GMDUnskinnedSceneCreator(f.name, gmd_scene, gmd_config, error)
                            scene_creator.validate_scene()

                            gmd_collection = scene_creator.make_collection(context)
                            scene_creator.make_objects(context, gmd_collection)
                        except Exception as e:
                            self.report({'ERROR'}, f"Failed to import GMD {f.name}: {e}")
                            continue

                    # Mark collection and its objects as assets so Asset Browser picks them up
                    try:
                        # Prefer marking the collection as an asset
                        gmd_collection.asset_mark()
                    except Exception:
                        # Fallback: mark objects and their data-blocks
                        for obj in list(gmd_collection.objects):
                            try:
                                obj.asset_mark()
                            except Exception:
                                # try marking mesh datablock
                                if obj.data:
                                    try:
                                        obj.data.asset_mark()
                                    except Exception:
                                        pass

                    imported += 1

                    # If an output directory is provided, write this collection and related datablocks to a .blend
                    if out_dir:
                        try:
                            out_dir.mkdir(parents=True, exist_ok=True)

                            # Collect datablocks to write: collection, objects, meshes, materials, images
                            objs = [obj for obj in gmd_collection.objects]
                            meshes = [obj.data for obj in objs if getattr(obj, 'data', None) is not None]
                            materials = []
                            images = []
                            for m in meshes:
                                # materials referenced on the mesh (via users)
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
                                    continue

                            # Build a set of datablocks (IDs) for libraries.write (Blender expects a set of IDs)
                            write_ids = set()
                            try:
                                write_ids.add(gmd_collection)
                            except Exception:
                                pass
                            for o in objs:
                                try:
                                    write_ids.add(o)
                                except Exception:
                                    pass
                            for m in meshes:
                                try:
                                    write_ids.add(m)
                                except Exception:
                                    pass
                            for mat in materials:
                                try:
                                    write_ids.add(mat)
                                except Exception:
                                    pass
                            for img in images:
                                try:
                                    write_ids.add(img)
                                except Exception:
                                    pass

                            # Ensure assets are marked in-memory before writing so flags persist where possible
                            try:
                                gmd_collection.asset_mark()
                            except Exception:
                                pass
                            for obj in objs:
                                try:
                                    obj.asset_mark()
                                except Exception:
                                    pass
                                try:
                                    if getattr(obj, 'data', None):
                                        obj.data.asset_mark()
                                except Exception:
                                    pass

                            out_path = str(out_dir / (pathlib.Path(f.name).stem + '.blend'))
                            wrote = False
                            try:
                                if self.debug_logging:
                                    self.report({'INFO'}, f"Attempting libraries.write (IDs) -> {out_path}")
                                bpy.data.libraries.write(out_path, write_ids)
                                self.report({'INFO'}, f"Wrote asset library: {out_path}")
                                wrote = True
                            except Exception as e:
                                if self.debug_logging:
                                    self.report({'WARNING'}, f"libraries.write(ids) failed: {e}")
                                # Try smaller write sets as fallbacks
                                try:
                                    if write_ids:
                                        subset = {g for g in write_ids if g is not None}
                                        bpy.data.libraries.write(out_path, subset)
                                        self.report({'INFO'}, f"Wrote asset library (subset): {out_path}")
                                        wrote = True
                                except Exception as e2:
                                    if self.debug_logging:
                                        self.report({'WARNING'}, f"libraries.write(subset) failed: {e2}")
                                    try:
                                        # Try writing just the collection as a last resort
                                        bpy.data.libraries.write(out_path, {gmd_collection})
                                        self.report({'INFO'}, f"Wrote asset collection: {out_path}")
                                        wrote = True
                                    except Exception as e3:
                                        if self.debug_logging:
                                            self.report({'WARNING'}, f"libraries.write(collection) failed: {e3}")
                                        self.report({'WARNING'}, f"Could not write .blend asset for {gmd_collection.name}: {e}; {e2}; {e3}")

                            if wrote:
                                added_dirs.add(str(out_dir))
                                # register single root as asset library (one entry for all exports)
                                try:
                                    prefs_ctx = bpy.context.preferences
                                    libs = prefs_ctx.filepaths.asset_libraries
                                    root_path = str(out_root)
                                    exists = any(root_path == str(pathlib.Path(l.path)) for l in libs)
                                    if not exists:
                                        new = libs.add()
                                        new.name = "YakuzaPAR_Assets"
                                        new.path = root_path
                                        try:
                                            bpy.ops.preferences.asset_library_refresh()
                                        except Exception:
                                            try:
                                                bpy.ops.asset.library_refresh()
                                            except Exception:
                                                pass
                                except Exception:
                                    pass
                        except Exception as e:
                            self.report({'WARNING'}, f"Failed to write asset .blend: {e}")

        # --- end per-entry processing ---
        # Final result
        if added_dirs:
            self.report({'INFO'}, f"Wrote assets to: {', '.join(sorted(set(added_dirs)))}")
        if imported == 0:
            self.report({'WARNING'}, "No .gmd files were imported from the configured PAR files")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Imported {imported} .gmd files from configured PAR entries")
        return {'FINISHED'}
        # Finalize execute: report results and return proper operator result set
        if imported == 0:
            self.report({'WARNING'}, "No .gmd files were imported from the configured PAR files")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Imported {imported} .gmd files from configured PAR entries")
        return {'FINISHED'}

    # Diagnostic operator helper (callable by script) - not registered as a Blender operator class here,
    # but we provide a small Operator below to call this functionality.
    def _diagnose_par_entries(self, context):
        prefs = context.preferences.addons.get('yk_par_lib_tool')
        if not prefs:
            print("yk_par_lib_tool preferences not found")
            return
        prefs = prefs.preferences
        for entry in getattr(prefs, 'par_files', []):
            print(f"PAR entry: {entry.path}")
            if not entry.path or not os.path.isfile(entry.path):
                print("  -> missing or invalid path")
                continue
            try:
                par = read_par(entry.path)
                decompress_par(par)
                def _iter(folder, prefix=""):
                    for file in getattr(folder, 'files', []) or []:
                        print(f"  {prefix}{file.name}")
                    for sub in getattr(folder, 'folders', []) or []:
                        print(f"  {prefix}{sub.name}/")
                        _iter(sub, prefix + "  ")
                root = par.folders[0] if getattr(par, 'folders', None) and len(par.folders) else None
                if root:
                    _iter(root)
                else:
                    print("  -> no folders/files found in PAR")
            except Exception as e:
                print(f"  -> failed to read PAR: {e}")


class YKPAR_OT_diagnose(Operator):
    """Diagnose configured .par entries and list contained files"""
    bl_idname = "yk_par_lib_tool.diagnose_pars"
    bl_label = "Diagnose configured PARs"

    def execute(self, context):
        ImportPARAsAssets._diagnose_par_entries(self, context)
        self.report({'INFO'}, "Diagnosis printed to system console")
        return {'FINISHED'}


class YKPAR_OT_export_par_as_assetlib(BaseImportGMD, Operator, ImportHelper):
    """Export configured PARs as one .blend asset library per PAR."""
    bl_idname = "yk_par_lib_tool.export_par_asset_lib"
    bl_label = "Export PARs as Asset Libraries"

    output_dir: StringProperty(
        name="Output Directory",
        subtype='DIR_PATH',
        default=""
    )
    debug_logging: BoolProperty(name="Debug Logging", default=False)
    # ImportHelper file selection for choosing a single .par to export
    filename_ext = ".par"
    filter_glob: StringProperty(default="*.par", options={"HIDDEN"})

    # Comma-separated list of top-level folder names to export (e.g. "tops,face,hair").
    # If empty, all folders are exported.
    folder_filter: StringProperty(
        name="Folder filter",
        description="Comma-separated top-level folder names to export (leave empty for all)",
        default=""
    )

    # internal flag set after user confirms dialog
    _confirmed: bool = False

    def execute(self, context):
        # If this is the first run and the user has not confirmed folder selection,
        # show a small dialog (invoke_props_dialog) so they can set folder_filter.
        if not getattr(self, '_confirmed', False):
            # If a filepath wasn't provided before, open file selector
            if not getattr(self, 'filepath', None):
                return context.window_manager.fileselect_add(self)
            # Show properties dialog to allow setting folder_filter
            self._confirmed = True
            return context.window_manager.invoke_props_dialog(self)

        prefs = context.preferences.addons.get('yk_par_lib_tool')
        if not prefs:
            self.report({'ERROR'}, "yk_par_lib_tool addon preferences not found")
            return {'CANCELLED'}
        prefs = prefs.preferences
        par_entries = getattr(prefs, 'par_files', [])
        if not par_entries and not getattr(self, 'filepath', None):
            self.report({'ERROR'}, "No .par files configured in Add-on preferences")
            return {'CANCELLED'}

        out_root = pathlib.Path(self.output_dir) if self.output_dir else pathlib.Path.home() / 'YakuzaPAR_Assets/.gmd'
        out_root.mkdir(parents=True, exist_ok=True)

        error = self.create_logger()
        written = 0

        # diagnostics per-PAR
        par_stats = []

        scene = context.scene
        master_collection = scene.collection

        def iter_folder_files(folder, depth=1):
            """Yield files from allowed folders.

            This supports archives where allowed folders may be nested under a container
            (for example a top-level 'chara' folder containing 'tops', 'face', etc.).
            Rules:
            - At depth==1, allow folders whose name startswith 'dds' or is in allowed set.
              If the folder at depth==1 is not allowed but has children, recurse into
              children to find allowed folders deeper in the tree.
            - At depth>=2, accept folders whose name indicates allowed categories; otherwise
              keep recursing into children to locate allowed categories.
            """
            allowed = {'tops', 'face', 'hair', 'face target', 'btms'}

            name = getattr(folder, 'name', '') or ''
            n = name.lower()

            # Depth 1: if this folder isn't one of the allowed top-level categories,
            # don't bail out immediately — some archives nest categories under a container.
            if depth == 1:
                if not (n.startswith('dds') or n in allowed):
                    # Recurse into children to locate allowed categories deeper
                    for sub in getattr(folder, 'folders', []) or []:
                        yield from iter_folder_files(sub, depth + 1)
                    return

            # Depth >= 2: if the folder name matches allowed categories, yield files under it
            if depth >= 2 and not (n.startswith('dds') or n in allowed):
                # Not an allowed category at this depth; keep searching deeper
                for sub in getattr(folder, 'folders', []) or []:
                    yield from iter_folder_files(sub, depth + 1)
                return

            # If we reach here, either this folder is allowed or we're at an allowed top-level folder.
            for file in getattr(folder, 'files', []) or []:
                yield file
            for sub in getattr(folder, 'folders', []) or []:
                yield from iter_folder_files(sub, depth + 1)

        for entry in par_entries:
            par_path = entry.path
            if not par_path or not os.path.isfile(par_path):
                continue

            try:
                par = read_par(par_path)
                decompress_par(par)
            except Exception as e:
                self.report({'WARNING'}, f"Failed to read PAR {par_path}: {e}")
                continue

            par_name = pathlib.Path(par_path).stem
            # create a parent collection for this PAR (do NOT link into current scene)
            parent_coll_name = f"PAR_{par_name}"
            parent_coll = bpy.data.collections.new(parent_coll_name)

            # process selected top-level folders if folder_filter provided, else process all
            root_folder = par.folders[0] if getattr(par, 'folders', None) and len(par.folders) else None
            if root_folder is None:
                self.report({'WARNING'}, f"PAR '{par_path}' contains no folders/files")
                continue

            # Build set of filters (lowercase, trimmed)
            filters = [s.strip().lower() for s in self.folder_filter.split(',') if s.strip()] if getattr(self, 'folder_filter', None) else []

            def iter_selected(folder):
                for file in getattr(folder, 'files', []) or []:
                    if getattr(file, 'name', '').lower().endswith('.gmd'):
                        yield file
                for sub in getattr(folder, 'folders', []) or []:
                    yield from iter_selected(sub)

            gmd_entries = []
            # include files directly in root
            for f in getattr(root_folder, 'files', []) or []:
                if getattr(f, 'name', '').lower().endswith('.gmd'):
                    gmd_entries.append(f)
            # iterate top-level folders
            for top in getattr(root_folder, 'folders', []) or []:
                name = (getattr(top, 'name', '') or '').lower()
                if not filters or any(name.startswith(fl) or fl in name for fl in filters):
                    gmd_entries.extend(list(iter_selected(top)))

            # manifest entries for this PAR
            par_manifest = []

            # diagnostic counters
            par_gmd_count = len(gmd_entries)
            par_written = 0

            if self.debug_logging:
                self.report({'INFO'}, f"PAR '{par_name}': found {par_gmd_count} .gmd files to process")

            for f in gmd_entries:
                if not getattr(f, 'name', '').lower().endswith('.gmd'):
                    continue

                try:
                    file_bytes = bytes(f.data) if isinstance(f.data, (bytearray, memoryview)) else f.data
                    gmd_version, gmd_header, gmd_contents = read_gmd_structures(file_bytes, error)
                except Exception as e:
                    self.report({'WARNING'}, f"Failed to parse GMD {f.name}: {e}")
                    continue

                try:
                    # Try skinned first
                    gmd_scene = read_abstract_scene_from_filedata_object(gmd_version, FileImportMode.SKINNED,
                                                                         VertexImportMode.IMPORT_VERTICES,
                                                                         gmd_contents, error)
                    gmd_config = self.create_gmd_config(gmd_version, error)
                    scene_creator = GMDSkinnedSceneCreator(f.name, gmd_scene, gmd_config, error)
                    scene_creator.validate_scene()
                    gmd_collection = scene_creator.make_collection(context)
                    gmd_armature = scene_creator.make_bone_hierarchy(context, gmd_collection)
                    scene_creator.make_objects(context, gmd_collection, gmd_armature)
                except Exception:
                    try:
                        gmd_scene = read_abstract_scene_from_filedata_object(gmd_version, FileImportMode.UNSKINNED,
                                                                             VertexImportMode.IMPORT_VERTICES,
                                                                             gmd_contents, error)
                        gmd_config = self.create_gmd_config(gmd_version, error)
                        scene_creator = GMDUnskinnedSceneCreator(f.name, gmd_scene, gmd_config, error)
                        scene_creator.validate_scene()
                        gmd_collection = scene_creator.make_collection(context)
                        scene_creator.make_objects(context, gmd_collection)
                    except Exception as e:
                        self.report({'WARNING'}, f"Failed to import GMD {f.name}: {e}")
                        continue

                # link gmd collection under the parent collection (parent is unlinked, so asset stays out of scene)
                try:
                    parent_coll.children.link(gmd_collection)
                except Exception:
                    # ignore linking failures; we'll still try to write collections directly
                    pass

                # mark as asset
                try:
                    gmd_collection.asset_mark()
                except Exception:
                    pass

                # Build per-gmd out_path and datablocks to write a single-asset .blend (RE-Asset-Library style)
                gmd_base_name = pathlib.Path(f.name).stem
                asset_out_path = out_root / par_name / (gmd_base_name + '.blend')
                asset_out_path.parent.mkdir(parents=True, exist_ok=True)

                # Collect necessary datablocks for this single asset file
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
                        continue

                datablocks = {
                    'collections': [gmd_collection],
                    'objects': objs,
                    'meshes': meshes,
                    'materials': materials,
                    'images': images,
                }

                wrote_asset = False
                try:
                    bpy.data.libraries.write(str(asset_out_path), datablocks)
                    wrote_asset = True
                except Exception as e:
                    # try filtered
                    try:
                        filtered = {k: v for k, v in datablocks.items() if v}
                        bpy.data.libraries.write(str(asset_out_path), filtered)
                        wrote_asset = True
                    except Exception:
                        try:
                            bpy.data.libraries.write(str(asset_out_path), {'collections': [gmd_collection]})
                            wrote_asset = True
                        except Exception as e3:
                            self.report({'WARNING'}, f"Failed to write asset {gmd_base_name}: {e3}")

                if wrote_asset:
                    written += 1
                    par_written += 1
                    # add to par manifest
                    par_manifest.append({
                        'asset_name': gmd_collection.name,
                        'file': str(asset_out_path.relative_to(out_root)),
                        'source_par': str(par_path),
                    })

            # record stats for this PAR
            par_stats.append({'par': par_name, 'found': par_gmd_count, 'written': par_written})

            # write manifest for this PAR if we produced any assets
            if par_manifest:
                try:
                    par_dir = out_root / par_name
                    par_dir.mkdir(parents=True, exist_ok=True)
                    manifest_path = par_dir / 'manifest.json'
                    import json
                    with open(manifest_path, 'w', encoding='utf-8') as mf:
                        json.dump({'par': par_name, 'assets': par_manifest}, mf, indent=2)
                except Exception:
                    pass

            # add the per-PAR directory to blender asset libraries (one entry per PAR dir)
            try:
                prefs_ctx = bpy.context.preferences
                libs = prefs_ctx.filepaths.asset_libraries
                par_out_dir = str(out_root / par_name)
                exists = any(par_out_dir == str(pathlib.Path(l.path)) for l in libs)
                if not exists:
                    new = libs.add()
                    new.name = f"YakuzaPAR_{par_name}"
                    new.path = par_out_dir
                    try:
                        bpy.ops.preferences.asset_library_refresh()
                    except Exception:
                        try:
                            bpy.ops.asset.library_refresh()
                        except Exception:
                            pass
            except Exception:
                pass

            # cleanup: remove parent collection and all its children from current file to avoid polluting user file
            try:
                bpy.data.collections.remove(parent_coll)
            except Exception:
                pass

        if written == 0:
            # Provide more diagnostic detail when nothing was written
            try:
                details = "; ".join([f"{p['par']}: found={p['found']}, written={p['written']}" for p in par_stats])
            except Exception:
                details = "(no per-PAR stats available)"
            self.report({'WARNING'}, "No asset libraries were written")
            if self.debug_logging:
                self.report({'INFO'}, f"Per-PAR stats: {details}")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Wrote {written} asset library files to {out_root}")
        return {'FINISHED'}

        # If we wrote to any persistent output directories, add them to Asset Library preferences (best-effort)
        if added_dirs and imported > 0:
            try:
                prefs = bpy.context.preferences
                libs = prefs.filepaths.asset_libraries
                for ad in added_dirs:
                    exists = any(ad == str(pathlib.Path(l.path)) for l in libs)
                    if not exists:
                        new = libs.add()
                        new.name = f"YakuzaPAR_{pathlib.Path(ad).name}"
                        new.path = ad
                        self.report({'INFO'}, f"Added asset library path: {ad}")

                # Try to refresh asset libraries so Asset Browser picks up new .blend files
                try:
                    bpy.ops.preferences.asset_library_refresh()
                except Exception:
                    try:
                        bpy.ops.asset.library_refresh()
                    except Exception:
                        # If no refresh operator available, that's fine; user can refresh manually
                        pass
            except Exception as e:
                self.report({'WARNING'}, f"Could not register asset library path(s): {e}")

        if imported == 0:
            self.report({'WARNING'}, "No .gmd files were imported from the configured PAR files")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Imported {imported} .gmd files from configured PAR entries")
        return {'FINISHED'}


def menu_func_import_par(self, context):
    self.layout.operator(ImportPARAsAssets.bl_idname, text="Yakuza PAR (.par) - Import contained GMDs")
