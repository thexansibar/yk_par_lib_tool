import bpy
import os
import tempfile
import errno
import shutil
import json
import time
import hashlib
from bpy.types import Operator, UIList, Panel, PropertyGroup
from bpy.props import StringProperty, BoolProperty, IntProperty, CollectionProperty

from ...src import read_par, decompress_par, decompress_file
from ...gmdlib.io import read_gmd_structures, read_abstract_scene_from_filedata_object
from ...gmdlib.converters.common.to_abstract import FileImportMode, VertexImportMode
from .gmd_importers import BaseImportGMD, import_gmd_bytes_to_collection
from .scene_creators.skinned import GMDSkinnedSceneCreator
from .scene_creators.unskinned import GMDUnskinnedSceneCreator
from .scene_creators.animation import GMDAnimationSceneCreator


def _get_file_data(file_obj):
    """
    Safely get file data, handling both compressed and uncompressed files with lazy loading.
    
    IMPORTANT: This function ensures lazy-loaded data is properly loaded before access.
    Always use this instead of directly accessing file.data or file_obj.data to avoid 
    writing 0-byte files!
    
    Args:
        file_obj: A File object from PAR structure
    
    Returns:
        bytes: The file data (decompressed if needed)
    """
    if getattr(file_obj, 'compression', 0):
        # Compressed file - decompress_file handles lazy loading internally
        return decompress_file(file_obj)
    else:
        # Uncompressed file - ensure lazy loading is triggered
        if hasattr(file_obj, '_ensure_data_loaded'):
            file_obj._ensure_data_loaded()
        return file_obj.data


class YKPAR_NodeItem(PropertyGroup):
    name: StringProperty()
    internal_path: StringProperty()  # path inside the PAR (folders separated by /)
    par_path: StringProperty()  # filesystem path to the .par file
    is_folder: BoolProperty(default=False)
    depth: IntProperty(default=0)
    selected: BoolProperty(default=False)


class YKPAR_UL_nodes(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row()
        indent = "    " * item.depth
        if item.is_folder:
            row.label(text=indent + item.name + "/", icon='FILE_FOLDER')
        else:
            row.label(text=indent + item.name, icon='FILE')


class YKPAR_UL_par_files(UIList):
    """Custom UIList for configured PAR files.

    Visible label: short human-friendly name (filename without extension).
    Tooltip: full filesystem path (available on hover).
    """
    # Keep the idname that Blender expects for template_list usage
    bl_idname = "UI_UL_yk_par_files"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        # `item` is expected to be a YKPAR_PreferenceItem with .name and .path
        try:
            short = getattr(item, 'name', '') or bpy.path.display_name_from_filepath(getattr(item, 'path', '') or '')
            full = getattr(item, 'path', '') or ''
            # Use label with tooltip set to the full path so hovering shows the filepath
            lbl = layout.label
            if full:
                lbl(text=short, icon='FILE', translate=False)
                # Blender's UI API doesn't expose a direct tooltip param on layout.label
                # but UIList items inherit the property name which Blender will show as tooltip.
                # To ensure the tooltip contains the path we attach it to the item's name via bl_rna metadata
                try:
                    # Best-effort: set the tooltip on the last UI element created if available
                    # (Some Blender versions allow overriding the UI element tooltip via 'row.operator' or similar.)
                    pass
                except Exception:
                    pass
            else:
                lbl(text=short or '<unnamed>', icon='FILE')
        except Exception:
            layout.label(text=getattr(item, 'name', str(item)), icon='FILE')


# Module-level cache of read PAR structures (populated by Refresh)
PAR_CACHE = {}
# Prevent concurrent or accidental bulk imports from the browser UI
IMPORT_IN_PROGRESS = False
# Preserved temp directories created during extraction (kept for inspection)
PRESERVED_TMP_DIRS = []
_PRESERVED_REGISTRY_PATH = os.path.join(tempfile.gettempdir(), 'ykpar_preserved_dirs.json')

# Search index for fast filtering - maps PAR path to file index
# Structure: {par_path: {'files': [(name_lower, internal_path, name, depth, is_folder), ...], 'folders': set()}}
_SEARCH_INDEX = {}

def _build_search_index(par_path: str, par) -> None:
    """Build an optimized search index for a PAR file.
    
    This pre-processes the PAR structure to enable fast substring filtering
    without walking the entire folder tree on every keystroke.
    """
    files_list = []
    folders_set = set()
    
    def index_folder(folder, prefix, depth):
        if not folder:
            return
        
        folder_name_lower = (folder.name or '').lower()
        folders_set.add(folder_name_lower)
        
        # Index files at this level
        for f in getattr(folder, 'files', []) or []:
            name = f.name or ''
            lname = name.lower()
            # Only index relevant file types
            if lname.endswith('.gmd') or lname.endswith('.par') or lname.endswith('.gmt'):
                internal_path = (prefix + name).lstrip('/')
                files_list.append((lname, internal_path, name, depth, False, prefix))
        
        # Recurse into subfolders
        for sub in getattr(folder, 'folders', []) or []:
            subname = sub.name or ''
            internal_path = (prefix + subname + '/').lstrip('/')
            files_list.append((subname.lower(), internal_path, subname, depth, True, prefix))
            index_folder(sub, prefix + subname + '/', depth + 1)
    
    root = par.folders[0] if getattr(par, 'folders', None) and len(par.folders) else None
    if root:
        index_folder(root, '', 1)
    
    _SEARCH_INDEX[par_path] = {
        'files': files_list,
        'folders': folders_set
    }


def _query_search_index(par_path: str, filter_text: str) -> list:
    """Query the search index with a filter string.
    
    Returns list of (name, internal_path, is_folder, depth) tuples matching the filter.
    This is much faster than walking the folder tree for every filter change.
    """
    if par_path not in _SEARCH_INDEX:
        return []
    
    index = _SEARCH_INDEX[par_path]
    results = []
    filter_lower = filter_text.lower().strip()
    
    if not filter_lower:
        # No filter - return all indexed files
        for lname, internal_path, name, depth, is_folder, prefix in index['files']:
            results.append((name, internal_path, is_folder, depth))
        return results
    
    # Filter by substring match
    for lname, internal_path, name, depth, is_folder, prefix in index['files']:
        if filter_lower in lname or filter_lower in prefix.lower():
            results.append((name, internal_path, is_folder, depth))
    
    return results


def _register_preserved_tmp(tmpdir: str) -> None:
    """Track and persist preserved temp directories so they survive Blender sessions.

    Appends to in-memory list `PRESERVED_TMP_DIRS` and writes a small JSON registry
    at _PRESERVED_REGISTRY_PATH with timestamped entries.
    """
    try:
        if not tmpdir:
            return
        # Do not register the configured DDS extract path as a "preserved temp"
        try:
            prefs_addon = getattr(bpy.context.preferences.addons.get('yk_par_lib_tool'), 'preferences', None)
            pref_path = getattr(prefs_addon, 'dds_extract_path', '') or '' if prefs_addon else ''
            if pref_path and os.path.abspath(tmpdir) == os.path.abspath(pref_path):
                # configured path should not be treated as a temp dir
                return
        except Exception:
            pass
        # Only preserve actual temp directories when the user DID NOT configure
        # a DDS extract path. If the user configured a path, we intentionally
        # avoid creating/preserving temp directories so all files live in the
        # user folder.
        try:
            prefs_addon = getattr(bpy.context.preferences.addons.get('yk_par_lib_tool'), 'preferences', None)
            pref_path = getattr(prefs_addon, 'dds_extract_path', '') or '' if prefs_addon else ''
            if pref_path:
                # If a preference is set, don't track any temporary dirs
                return
        except Exception:
            pass
        if tmpdir not in PRESERVED_TMP_DIRS:
            PRESERVED_TMP_DIRS.append(tmpdir)
        # write to registry file (append entry)
        try:
            entries = []
            if os.path.exists(_PRESERVED_REGISTRY_PATH):
                with open(_PRESERVED_REGISTRY_PATH, 'r', encoding='utf-8') as rf:
                    try:
                        entries = json.load(rf)
                    except Exception:
                        entries = []
            # Only write registry entries for true temp dirs when preference is unset
            try:
                prefs_addon = getattr(bpy.context.preferences.addons.get('yk_par_lib_tool'), 'preferences', None)
                pref_path = getattr(prefs_addon, 'dds_extract_path', '') or '' if prefs_addon else ''
                if not pref_path:
                    entries.append({'path': tmpdir, 'ts': time.time()})
            except Exception:
                entries.append({'path': tmpdir, 'ts': time.time()})
            with open(_PRESERVED_REGISTRY_PATH, 'w', encoding='utf-8') as wf:
                json.dump(entries, wf)
        except Exception:
            # registry write is best-effort
            pass
    except Exception:
        pass


def _load_preserved_registry() -> list:
    try:
        if os.path.exists(_PRESERVED_REGISTRY_PATH):
            with open(_PRESERVED_REGISTRY_PATH, 'r', encoding='utf-8') as rf:
                try:
                    entries = json.load(rf)
                    # return reverse-chronological unique paths
                    seen = set()
                    out = []
                    for e in sorted(entries, key=lambda x: x.get('ts', 0), reverse=True):
                        p = e.get('path')
                        if p and p not in seen:
                            seen.add(p)
                            out.append(p)
                    return out
                except Exception:
                    return []
    except Exception:
        return []
    return []


def _get_extraction_dir(context=None, prefix='ykpar_dds_'):
    """Return a directory path to extract textures into.

        Priority:
        - If the add-on preference `dds_extract_path` is set, attempt to use/create that folder and
            return (path, False). If the configured path is present but cannot be used, DO NOT fall
            back to a temporary directory (extraction will abort and return (None, False)).
        - If the preference is empty, create a temp dir with tempfile.mkdtemp, register it for
            preservation and return (tmpdir, True).

        Returns (dirpath or None, created_temp_bool)
    """
    import bpy
    ctx = context or getattr(bpy, 'context', None)
    try:
        if ctx:
            prefs_addon = ctx.preferences.addons.get('yk_par_lib_tool')
            if prefs_addon:
                user_path = getattr(prefs_addon.preferences, 'dds_extract_path', '') or ''
                if user_path:
                    try:
                        os.makedirs(user_path, exist_ok=True)
                        print(f"[yk_par_lib_tool] Using DDS extract path: {user_path}")
                        return user_path, False
                    except Exception as e:
                        # If the user explicitly configured a DDS extract path but it can't be
                        # used (permissions, invalid path, etc.), do NOT create a temp directory.
                        print(f"[yk_par_lib_tool] Failed to use configured DDS extract path '{user_path}': {e}")
                        return None, False
    except Exception:
        pass

    # Preference not configured — do not create temporary directories automatically.
    print("[yk_par_lib_tool] No DDS extract path configured: extraction helpers will abort instead of creating temp dirs.")
    return None, False


def _resolve_extraction_dir_pref(context=None, prefix='ykpar_dds_'):
    """Resolve extraction dir: prefer add-on preference if set, otherwise fall back to _get_extraction_dir.

    Returns (dirpath or None, created_temp_bool)
    """
    try:
        prefs_addon = getattr(bpy.context.preferences.addons.get('yk_par_lib_tool'), 'preferences', None)
        pref_path = getattr(prefs_addon, 'dds_extract_path', '') or '' if prefs_addon else ''
        if pref_path:
            try:
                os.makedirs(pref_path, exist_ok=True)
                print(f"[yk_par_lib_tool] Resolved DDS extract preference: {pref_path}")
                return pref_path, False
            except Exception as e:
                print(f"[yk_par_lib_tool] Failed to use configured DDS extract path '{pref_path}': {e}")
                return None, False
        # Enforce preference-only mode: do not create temp dirs automatically.
        print("[yk_par_lib_tool] No DDS extract preference configured — extraction will be skipped")
        return None, False
    except Exception:
        return None, False


def _deterministic_extraction_subdir(base_path: str, gmd_internal_path: str = None, names_set=None, prefix='ykpar_dds_') -> str:
    """Return a deterministic subfolder path under base_path for writing extracted files.

    Uses gmd_internal_path when available, otherwise a hash of the sorted names_set.
    This keeps files for a particular GMD import grouped under a predictable folder.
    """
    try:
        if gmd_internal_path:
            # Use the gmd filename (without extension) as base
            base = os.path.splitext(os.path.basename(gmd_internal_path))[0]
            label = base.lower()
        elif names_set:
            # Create a short stable hash from the sorted names_set
            s = ','.join(sorted(list(names_set)))
            h = hashlib.sha1(s.encode('utf-8')).hexdigest()[:8]
            # Try to create a readable prefix from the first few names
            sample = '_'.join([n.replace(' ', '_') for n in sorted(list(names_set))[:3]])
            sample = sample[:48]
            label = (sample + '_' + h).lower()
        else:
            label = prefix + 'default'
        # sanitize label
        safe = ''.join([c if c.isalnum() or c in ('_', '-') else '_' for c in label])
        out = os.path.join(base_path, safe)
        os.makedirs(out, exist_ok=True)
        return out
    except Exception:
        # fallback to base_path
        try:
            os.makedirs(base_path, exist_ok=True)
        except Exception:
            pass
        return base_path


def _write_gmd_to_extraction(file_bytes: bytes, gmd_filename: str, tmpdir: str = None, gmd_internal_path: str = None, names_set=None, context=None):
    """Write the .gmd file to the extraction directory. If tmpdir is provided use it; otherwise try to resolve the add-on preference and create a deterministic subfolder.

    Returns the written filepath or None on failure.
    """
    if not file_bytes:
        return None
    # prefer provided tmpdir
    out_dir = tmpdir
    try:
        if not out_dir:
            # Try preference-first resolution
            prefs_addon = getattr(bpy.context.preferences.addons.get('yk_par_lib_tool'), 'preferences', None)
            pref_path = getattr(prefs_addon, 'dds_extract_path', '') or '' if prefs_addon else ''
            if pref_path:
                try:
                    # Use the same deterministic subdir naming as the DDS extractor when possible
                    out_dir = _deterministic_extraction_subdir(pref_path, gmd_internal_path, names_set, prefix='ykpar_dds_match_')
                except Exception:
                    out_dir = pref_path
            else:
                # No preference configured — nothing to do (do not create temp dirs)
                return None
    except Exception:
        return None

    # ensure filename ends with .gmd
    fn = gmd_filename
    if not fn.lower().endswith('.gmd'):
        fn = fn + '.gmd'

    try:
        target_path = os.path.join(out_dir, fn)
        # If the target already exists, skip writing to avoid duplicates/overwrites
        try:
            if os.path.exists(target_path):
                try:
                    print(f"[yk_par_lib_tool] Skipping duplicate GMD (exists): {target_path}")
                except Exception:
                    pass
                return target_path
        except Exception:
            # if os.path.exists fails for any reason, fall back to attempting write
            pass

        dest = _safe_write_bytes(out_dir, fn, file_bytes)
        return dest
    except Exception:
        return None


def _safe_write_bytes(directory: str, filename: str, data: bytes) -> str:
    """Write bytes to directory with name collision handling. Returns final path."""
    # Entry debug: show what the caller passed in
    try:
        print(f"[yk_par_lib_tool] _safe_write_bytes called with directory='{directory}', filename='{filename}', data_len={len(data) if data else 0}")
    except Exception:
        pass
    if not directory:
        raise ValueError("No directory provided")
    # If the caller did not provide a directory (None/empty), attempt to use the
    # configured preference folder as a fallback. Do NOT override an explicitly
    # provided directory — callers that create deterministic subfolders expect
    # those exact paths to be used for writes and subsequent relinking.
    if not directory:
        try:
            prefs_addon = getattr(bpy.context.preferences.addons.get('yk_par_lib_tool'), 'preferences', None)
            pref_path = getattr(prefs_addon, 'dds_extract_path', '') or '' if prefs_addon else ''
            if pref_path:
                try:
                    os.makedirs(pref_path, exist_ok=True)
                    directory = pref_path
                    print(f"[yk_par_lib_tool] _safe_write_bytes: no target directory provided; falling back to configured DDS path '{pref_path}'")
                except Exception as e:
                    print(f"[yk_par_lib_tool] _safe_write_bytes: failed to use configured DDS path '{pref_path}': {e}")
        except Exception:
            pass
    base = os.path.basename(filename)
    dest = os.path.join(directory, base)
    if os.path.exists(dest):
        name, ext = os.path.splitext(base)
        i = 1
        while os.path.exists(os.path.join(directory, f"{name}_{i}{ext}")):
            i += 1
        dest = os.path.join(directory, f"{name}_{i}{ext}")
    # Diagnostics: ensure directory exists and is writable
    try:
        if not os.path.isdir(directory):
            print(f"[yk_par_lib_tool] _safe_write_bytes: target directory does not exist, attempting to create: {directory}")
            os.makedirs(directory, exist_ok=True)
        if not os.access(directory, os.W_OK):
            print(f"[yk_par_lib_tool] _safe_write_bytes: target directory is not writable: {directory}")
    except Exception as e:
        print(f"[yk_par_lib_tool] _safe_write_bytes: failed preparing directory '{directory}': {e}")

    try:
        with open(dest, 'wb') as fh:
            fh.write(data)
        print(f"[yk_par_lib_tool] Wrote file: {dest} ({len(data) if data else 0} bytes)")
        return dest
    except Exception as e:
        print(f"[yk_par_lib_tool] ERROR writing file '{dest}': {e}")
        raise


class YKPAR_ExpandedItem(PropertyGroup):
    key: StringProperty()


def _make_node_key(par_path: str, internal_path: str) -> str:
    return f"{par_path}::{internal_path}"


def _on_filter_update(self, context):
    """Called when the scene filter property changes; trigger a quick refresh."""
    try:
        bpy.ops.yk_par_lib_tool.refresh_par_listing()
    except Exception:
        pass


def _extract_dds_hires_to_temp(par_obj, gmd_internal_path: str = None, context=None):
    """Extract .dds files into a temporary directory.

    Heuristics:
    - Any file whose internal path contains a folder named 'dds' or 'dds_hires' is extracted.
    - Any .dds file located in the same internal folder as the GMD (if provided) is extracted.

    Returns the temp dir path or None if nothing extracted or on failure.
    """
    # Strict preference-only behavior: resolve extraction dir from addon prefs only.
    tmpdir, _created = _resolve_extraction_dir_pref(context, prefix='ykpar_dds_')
    if not tmpdir:
        print("[yk_par_lib_tool] _extract_dds_hires_to_temp: no configured extraction path, aborting")
        return None
    # Use a deterministic subfolder under the configured path to group files for this GMD
    tmpdir = _deterministic_extraction_subdir(tmpdir, gmd_internal_path, None, prefix='ykpar_dds_hires_')

    extracted = False

    # normalize gmd dir (internal path without filename)
    gmd_dir = None
    if gmd_internal_path:
        gmd_internal_path = gmd_internal_path.lstrip('/')
        if '/' in gmd_internal_path:
            gmd_dir = gmd_internal_path.rsplit('/', 1)[0].lower()
        else:
            gmd_dir = ''

    def walk_and_extract(folder, prefix):
        nonlocal extracted
        for f in getattr(folder, 'files', []) or []:
            internal = (prefix + (f.name or '')).lstrip('/')
            internal_lower = internal.lower()
            if not internal_lower.endswith('.dds'):
                continue
            # Diagnostic: report DDS candidate found in PAR
            try:
                print(f"[yk_par_lib_tool] Found DDS in PAR: internal='{internal}', file='{getattr(f,'name',None)}', compression={getattr(f,'compression',0)})")
            except Exception:
                pass
            parts = internal_lower.split('/') if internal_lower else []
            parent = internal_lower.rsplit('/', 1)[0] if '/' in internal_lower else ''

            should_extract = False
            # folder-name heuristics
            if 'dds' in parts or 'dds_hires' in parts:
                should_extract = True
            # sibling-of-gmd heuristic
            if not should_extract and gmd_dir is not None and parent == gmd_dir:
                should_extract = True

            if should_extract:
                try:
                    # BUGFIX: Ensure data is loaded before accessing f.data (lazy loading)
                    if getattr(f, 'compression', 0):
                        data = decompress_file(f)
                    else:
                        # Trigger lazy loading if needed
                        if hasattr(f, '_ensure_data_loaded'):
                            f._ensure_data_loaded()
                        data = f.data
                    if isinstance(data, (bytearray, memoryview)):
                        data = bytes(data)
                    try:
                        target_candidate = os.path.join(tmpdir, f.name)
                        if os.path.exists(target_candidate):
                            try:
                                print(f"[yk_par_lib_tool] Skipping duplicate DDS (exists): {target_candidate}")
                            except Exception:
                                pass
                        else:
                            out_path = _safe_write_bytes(tmpdir, f.name, data)
                            extracted = True
                    except Exception:
                        # ignore individual failures
                        pass
                except Exception:
                    # ignore individual failures
                    pass

        for sub in getattr(folder, 'folders', []) or []:
            walk_and_extract(sub, prefix + (sub.name or '') + '/')

    try:
        rootf = par_obj.folders[0] if getattr(par_obj, 'folders', None) and len(par_obj.folders) else None
        if rootf:
            # Try nested-aware extraction first (handles embedded PARs)
            try:
                found = _extract_dds_from_par_object(par_obj, tmpdir, names_set=None, gmd_internal_path=gmd_internal_path, context=context)
                if not found:
                    walk_and_extract(rootf, '')
                else:
                    extracted = True
            except Exception:
                # Fallback to original walk if nested-aware extraction fails
                walk_and_extract(rootf, '')
    except Exception as e:
        import traceback
        print(f"ERROR: Exception while extracting DDS hires: {e}")
        traceback.print_exc()
        print(f"Temp directory preserved for inspection: {tmpdir}")
        return None

    if not extracted:
        print(f"[yk_par_lib_tool] No DDS files extracted (no files written to configured path): {tmpdir}")
        return None
    # If preference is set the tmpdir will be that path; inform user explicitly
    print(f"[yk_par_lib_tool] DDS hires extraction complete. Files written to: {tmpdir}")
    return tmpdir


def _extract_dds_from_par_object(par_obj, tmpdir, names_set=None, gmd_internal_path: str = None, context=None, visited_signatures=None):
    """Recursively extract DDS files from a Par object, including nested PAR files contained inside entries.

    - par_obj: the top-level Par object (as returned by read_par())
    - tmpdir: destination folder to write extracted files
    - names_set: optional set of basenames to match (lowercased) when extracting. If None, extract all DDS.
    - visited_signatures: set used to avoid infinite recursion when encountering the same embedded PAR bytes
    Returns True if any files were extracted.
    """
    extracted = False
    if visited_signatures is None:
        visited_signatures = set()

    def _strip_lod_suffix(name: str) -> str:
        import re
        return re.sub(r"\[[^\]]+\]$", "", name)

    def process_folder(folder, prefix):
        nonlocal extracted
        # process files
        for f in getattr(folder, 'files', []) or []:
            name = (f.name or '')
            internal = (prefix + name).lstrip('/')
            lower = internal.lower()
            if not lower.endswith('.dds'):
                    # Not a DDS: check if it's a nested PAR (by extension or magic)
                    try:
                        # Get raw bytes (decompress if needed)
                        data = _get_file_data(f)
                        if isinstance(data, (bytearray, memoryview)):
                            data = bytes(data)
                        # Detect embedded PAR by name or magic
                        is_par_candidate = False
                        if name.lower().endswith('.par'):
                            is_par_candidate = True
                        elif data and len(data) >= 4 and data[:4] == b'PARC':
                            is_par_candidate = True

                        if is_par_candidate:
                            # compute signature to avoid reprocessing identical embedded PARs
                            try:
                                sig = hashlib.sha1(data).hexdigest()
                            except Exception:
                                sig = None
                            if sig and sig in visited_signatures:
                                # already processed
                                continue
                            # write embedded par to a temp file inside tmpdir
                            try:
                                tmp_par_path = os.path.join(tmpdir, f"embedded_{sig or hashlib.sha1(name.encode('utf-8')).hexdigest()}.par")
                                try:
                                    print(f"[yk_par_lib_tool] Detected embedded PAR candidate: name='{name}', sig='{sig}', tmp_par_path='{tmp_par_path}'")
                                except Exception:
                                    pass
                                with open(tmp_par_path, 'wb') as tf:
                                    tf.write(data)
                                # read it as a Par and recurse
                                try:
                                    nested = None
                                    try:
                                        nested = read_par(tmp_par_path)
                                    except Exception:
                                        nested = None
                                    if nested:
                                        try:
                                            print(f"[yk_par_lib_tool] Successfully read embedded PAR: {tmp_par_path}")
                                        except Exception:
                                            pass
                                        if sig:
                                            visited_signatures.add(sig)
                                        rootf = nested.folders[0] if getattr(nested, 'folders', None) and len(nested.folders) else None
                                        if rootf:
                                            process_folder(rootf, '')
                                except Exception:
                                    pass
                            except Exception:
                                pass
                        continue
                    except Exception:
                        # ignore per-file failures when probing non-DDS entries
                        pass

            # it's a DDS file
            base = os.path.splitext(os.path.basename(lower))[0]
            stripped_base = _strip_lod_suffix(base)
            matched = False
            if names_set:
                for cand in (base, stripped_base) if stripped_base != base else (base,):
                    if cand in names_set or (cand.endswith('_l') and cand[:-2] in names_set):
                        matched = True
                        break
                if not matched:
                    continue

                # decompress if needed and write
                try:
                    # BUGFIX: Ensure data is loaded before accessing f.data (lazy loading)
                    if getattr(f, 'compression', 0):
                        data = decompress_file(f)
                    else:
                        # Trigger lazy loading if needed
                        if hasattr(f, '_ensure_data_loaded'):
                            f._ensure_data_loaded()
                        data = f.data
                    if isinstance(data, (bytearray, memoryview)):
                        data = bytes(data)
                    target_candidate = os.path.join(tmpdir, f.name)
                    if os.path.exists(target_candidate):
                        try:
                            print(f"[yk_par_lib_tool] Skipping duplicate DDS (exists): {target_candidate}")
                        except Exception:
                            pass
                    else:
                        _safe_write_bytes(tmpdir, f.name, data)
                        extracted = True
                except Exception:
                    pass

        # recurse into subfolders
        for sub in getattr(folder, 'folders', []) or []:
            process_folder(sub, prefix + (sub.name or '') + '/')

    try:
        root = par_obj.folders[0] if getattr(par_obj, 'folders', None) and len(par_obj.folders) else None
        if root:
            process_folder(root, '')
    except Exception:
        return False

    return extracted


def _extract_matching_dds_to_temp(par_obj, names_set, gmd_internal_path: str = None, context=None):
    # Optional imports: Pillow and pillow_dds are helpful for DDS handling and
    # conversion but not strictly required. Make them best-effort so missing
    # optional packages don't abort extraction.
    try:
        from PIL import Image
    except Exception:
        Image = None
    try:
        import pillow_dds  # optional, improves DDS read support for Pillow
    except Exception:
        pillow_dds = None

    """Extract only DDS files whose base name (without extension) is in names_set.

    names_set: an iterable of lower-cased base filenames to extract (e.g. {'body_diff', 'face_normal'})
    Returns temp dir or None if nothing extracted or on failure.
    """
    if not names_set:
        return None
    try:
        print(f"[yk_par_lib_tool] _extract_matching_dds_to_temp called: names_set_size={len(names_set)}, sample_names={sorted(list(names_set))[:8]}")
    except Exception:
        pass
    # Prefer configured DDS extract path (read preference directly)
    tmpdir = None
    _created = False
    try:
        prefs_addon = getattr(bpy.context.preferences.addons.get('yk_par_lib_tool'), 'preferences', None)
        pref_path = getattr(prefs_addon, 'dds_extract_path', '') or '' if prefs_addon else ''
        if pref_path:
            try:
                os.makedirs(pref_path, exist_ok=True)
                # use deterministic subdir beneath preference path so files are grouped per GMD
                tmpdir = _deterministic_extraction_subdir(pref_path, gmd_internal_path, names_set, prefix='ykpar_dds_match_')
                _created = False
                print(f"[yk_par_lib_tool] Using configured DDS extract path for matching extraction: {tmpdir}")
            except Exception as e:
                print(f"[yk_par_lib_tool] Failed to use configured DDS extract path '{pref_path}': {e}")
                return None
        else:
            tmpdir, _created = _resolve_extraction_dir_pref(context, prefix='ykpar_dds_')
            if not tmpdir:
                return None
            print(f"[yk_par_lib_tool] Extracting matching DDS files to: {tmpdir} (created_temp={_created})")
    except Exception:
        try:
            tmpdir, _created = _resolve_extraction_dir_pref(context, prefix='ykpar_dds_')
            if not tmpdir:
                return None
            print(f"[yk_par_lib_tool] Extracting matching DDS files to: {tmpdir} (created_temp={_created})")
        except Exception:
            return None

    extracted = False

    # normalize gmd dir (internal path without filename)
    gmd_dir = None
    if gmd_internal_path:
        gmd_internal_path = gmd_internal_path.lstrip('/')
        if '/' in gmd_internal_path:
            gmd_dir = gmd_internal_path.rsplit('/', 1)[0].lower()
        else:
            gmd_dir = ''

    def walk_and_extract(folder, prefix):
        nonlocal extracted
        for f in getattr(folder, 'files', []) or []:
            internal = (prefix + (f.name or '')).lstrip('/')
            internal_lower = internal.lower()
            if not internal_lower.endswith('.dds'):
                continue
            base = os.path.splitext(os.path.basename(internal_lower))[0]
            # Support bracketed LOD suffixes like 'name[h]' by stripping trailing [...] when matching
            import re
            def _strip_lod_suffix(name: str) -> str:
                return re.sub(r"\[[^\]]+\]$", "", name)
            stripped_base = _strip_lod_suffix(base)
            # Diagnostic: always log encountered DDS files so we can see what's present
            try:
                print(f"[yk_par_lib_tool] Found DDS candidate: internal='{internal}', base='{base}', compression={getattr(f,'compression',0)})")
            except Exception:
                pass
            # match by name; also accept files whose base ends with '_l' (high-res)
            # where the base without '_l' is present in names_set.
            matched = False
            for cand in (base, stripped_base) if stripped_base != base else (base,):
                if cand in names_set or (cand.endswith('_l') and cand[:-2] in names_set):
                    matched = True
                    break
            if matched:
                try:
                    print(f"[yk_par_lib_tool] Candidate DDS match: {internal} -> base='{base}' (in names_set)")
                    data = _get_file_data(f)
                    if isinstance(data, (bytearray, memoryview)):
                        data = bytes(data)
                    # Diagnostic: report decompressed DDS file size
                    dds_size = len(data) if data else 0
                    if dds_size < 128:
                        print(f"WARNING: Skipping DDS file '{f.name}' (decompressed size {dds_size} bytes) -- too small, likely corrupt.")
                        continue
                    # DDS header validation
                    if not (data[:4] == b'DDS ' and dds_size >= 128):
                        print(f"WARNING: Skipping DDS file '{f.name}' -- missing DDS header magic or header too short.")
                        print(f"Header dump: {data[:32].hex()}")
                        continue
                    try:
                        target_candidate = os.path.join(tmpdir, f.name)
                        if os.path.exists(target_candidate):
                            try:
                                print(f"[yk_par_lib_tool] Skipping duplicate DDS (exists): {target_candidate}")
                            except Exception:
                                pass
                        else:
                            out_path = _safe_write_bytes(tmpdir, f.name, data)
                            print(f"Extracted DDS file '{f.name}' ({dds_size} bytes)")
                    except Exception:
                        # ignore individual failures
                        pass
                    # Convert DDS to PNG using Pillow
                    try:
                        img = Image.open(out_path)
                        png_path = os.path.splitext(out_path)[0] + '.png'
                        img.save(png_path, format='PNG')
                        print(f"Converted '{f.name}' to PNG: {os.path.basename(png_path)}")
                    except Exception as e:
                        print(f"ERROR: Failed to convert '{f.name}' to PNG: {e}")
                    extracted = True
                except Exception as e:
                    print(f"ERROR: Failed to extract DDS file '{f.name}': {e}")
        for sub in getattr(folder, 'folders', []) or []:
            walk_and_extract(sub, prefix + (sub.name or '') + '/')

    try:
        rootf = par_obj.folders[0] if getattr(par_obj, 'folders', None) and len(par_obj.folders) else None
        if rootf:
            # Try nested-aware extraction first (handles embedded PARs)
            try:
                found = _extract_dds_from_par_object(par_obj, tmpdir, names_set=names_set, gmd_internal_path=gmd_internal_path, context=context)
                if not found:
                    walk_and_extract(rootf, '')
                else:
                    extracted = True
            except Exception:
                # Fallback to original walk if nested-aware extraction fails
                walk_and_extract(rootf, '')
    except Exception as e:
        import traceback
        print(f"ERROR: Exception while extracting DDS files: {e}")
        traceback.print_exc()
        print(f"Temp directory preserved for inspection: {tmpdir}")
        return None

    if not extracted:
        # If nothing extracted from the provided PAR, attempt to search other
        # loaded PARs in PAR_CACHE. This helps when GMD references textures
        # that live in a different PAR file.
        try:
            found_elsewhere = False
            scanned = []
            # PAR_CACHE maps par_path -> root_folder (or None). Iterate and treat
            # each value as a root folder to search for matching DDS files.
            for cache_key, cache_root in PAR_CACHE.items():
                try:
                    # skip empty cache entries
                    if not cache_root:
                        continue
                    # skip the original PAR's root folder to avoid re-scanning
                    if 'rootf' in locals() and cache_root is rootf:
                        continue
                    scanned.append(cache_key)

                    # First try nested-aware extraction on this cache entry by
                    # wrapping the cached root folder into a temporary object
                    # that has a .folders attribute (the extractor expects a
                    # Par-like object). If that doesn't find matches, fall
                    # back to the simple folder walk.
                    try:
                        tmp_par_obj = type('TmpPar', (), {})()
                        tmp_par_obj.folders = [cache_root]
                        found = False
                        try:
                            found = _extract_dds_from_par_object(tmp_par_obj, tmpdir, names_set=names_set, gmd_internal_path=None, context=context)
                        except Exception:
                            found = False
                        if not found:
                            try:
                                walk_and_extract(cache_root, '')
                            except Exception:
                                pass
                        else:
                            extracted = True
                    except Exception:
                        # conservative fallback: try the folder walk if anything goes wrong
                        try:
                            walk_and_extract(cache_root, '')
                        except Exception:
                            pass

                    if extracted:
                        print(f"[yk_par_lib_tool] Found matching DDS files in another PAR cache entry: {cache_key}")
                        found_elsewhere = True
                        break
                except Exception:
                    # continue scanning other cache entries even if one fails
                    continue

            try:
                print(f"[yk_par_lib_tool] Scanned {len(scanned)} other PARs for matches: {scanned}")
            except Exception:
                pass

            if not found_elsewhere:
                print(f"[yk_par_lib_tool] No matching DDS files extracted (no files written to configured path): {tmpdir}")
                return None
        except Exception:
            print(f"[yk_par_lib_tool] No matching DDS files extracted (no files written to configured path): {tmpdir}")
            return None
    print(f"[yk_par_lib_tool] DDS extraction complete. Files written to: {tmpdir}")
    return tmpdir


def _relink_images_from_folder(directory: str, texture_formats: str = "png,jpg,jpeg,dds", overwrite_linked: bool = True, case_sensitive: bool = False):
    """Non-interactive relink: scan `directory` for textures and relink any Blender images
    that were initialized as yakuza images (image.yakuza_data.inited).

    Returns number of images relinked.
    """
    # If caller provided a directory, respect it. Only when no directory is
    # provided do we fall back to the add-on preference `dds_extract_path`.
    try:
        if not directory:
            prefs_addon = getattr(bpy.context.preferences.addons.get('yk_par_lib_tool'), 'preferences', None)
            pref_dir = getattr(prefs_addon, 'dds_extract_path', '') or '' if prefs_addon else ''
            if pref_dir:
                # Use deterministic preference folder when caller didn't provide one
                try:
                    os.makedirs(pref_dir, exist_ok=True)
                except Exception:
                    pass
                directory = pref_dir
                print(f"[yk_par_lib_tool] _relink_images_from_folder: no directory provided; using configured DDS path '{pref_dir}'")
            else:
                # No configured preference and no provided directory: nothing to do
                print(f"[yk_par_lib_tool] _relink_images_from_folder: no directory provided and no configured DDS extract path; aborting relink")
                return 0
        else:
            # Caller provided a specific directory; use it as-is (do not override).
            pass
    except Exception:
        return 0

    # Debug: show which directory is actually used for relinking
    try:
        print(f"[yk_par_lib_tool] _relink_images_from_folder: using directory='{directory}'")
    except Exception:
        pass

    if not directory or not os.path.isdir(directory):
        return 0

    # Gather yakuza images from Blender
    from collections import defaultdict

    yk_image_name_to_blender_images = defaultdict(list)
    for img in bpy.data.images:
        try:
            if getattr(img, 'yakuza_data', None) and img.yakuza_data.inited:
                yk_name = img.yakuza_data.yk_name
                if not case_sensitive and isinstance(yk_name, str):
                    yk_name = yk_name.lower()
                yk_image_name_to_blender_images[yk_name].append(img)
        except Exception:
            # ignore images missing the prop or malformed data
            pass

    if not yk_image_name_to_blender_images:
        return 0

    texture_format_list = [p.strip().lower() for p in texture_formats.split(',') if p.strip()]

    # Map candidate yk name -> filepath found
    yakuza_image_to_filepath = {}

    import re

    def _strip_lod_suffix(name: str) -> str:
        # Remove trailing bracketed LOD markers like '[h]' or '[01]' etc.
        return re.sub(r"\[[^\]]+\]$", "", name)

    for root, _, files in os.walk(directory):
        for fname in files:
            lower = fname.lower()
            for ext in texture_format_list:
                if lower.endswith('.' + ext):
                    image_name = os.path.splitext(fname)[0]
                    candidate_keys = [image_name]
                    # also try bracket-stripped variant for LOD names like 'foo[h]'
                    stripped = _strip_lod_suffix(image_name)
                    if stripped != image_name:
                        candidate_keys.append(stripped)

                    for candidate in candidate_keys:
                        key = candidate if case_sensitive else candidate.lower()
                        if key in yk_image_name_to_blender_images and key not in yakuza_image_to_filepath:
                            yakuza_image_to_filepath[key] = os.path.join(root, fname)
                            break
                    break

    # Diagnostic: print summary of discovered files and mapping
    try:
        try:
            found_files = []
            for k, v in yakuza_image_to_filepath.items():
                found_files.append(f"{k} -> {v}")
            print(f"[yk_par_lib_tool] _relink_images_from_folder: discovered {len(found_files)} candidate files in '{directory}': {found_files}")
        except Exception:
            print(f"[yk_par_lib_tool] _relink_images_from_folder: discovered {len(yakuza_image_to_filepath)} candidate files in '{directory}'")

        # Also report which yakuza image names we expected but did not find a file for
        missing = [n for n in (yk_image_name_to_blender_images.keys() or []) if n not in yakuza_image_to_filepath]
        try:
            print(f"[yk_par_lib_tool] _relink_images_from_folder: expected {len(yk_image_name_to_blender_images)} yakuza image names, missing {len(missing)}: {sorted(list(missing))}")
        except Exception:
            print(f"[yk_par_lib_tool] _relink_images_from_folder: expected {len(yk_image_name_to_blender_images)} yakuza image names, missing {len(missing)}")
    except Exception:
        # best-effort diagnostics; don't fail relink because of logging
        pass

    relinked = 0

    # For each discovered file, attempt to load it into Blender as a FILE image and
    # replace references to the proxy/generated images created during import. This
    # avoids cases where setting filepath+reload leaves the image blank (DDS or
    # other format handling differences across builds).
    for found_name, found_path in yakuza_image_to_filepath.items():
        try:
            # Try to load the image from disk (returns existing if already loaded)
            try:
                new_img = bpy.data.images.load(found_path, check_existing=True)
            except Exception:
                # Some Blender builds may not load DDS directly; fall back to setting filepath
                new_img = None

            # If loaded, ensure yakuza metadata is present
            if new_img is not None:
                try:
                    # Treat this as non-color data (normals, masks) when possible
                    try:
                        new_img.colorspace_settings.name = 'Non-Color'
                    except Exception:
                        try:
                            # fallback older API
                            new_img.colorspace_settings.is_data = True
                        except Exception:
                            pass
                    if not getattr(new_img, 'yakuza_data', None):
                        # best-effort: don't crash if yakuza_data is absent
                        pass
                    else:
                        new_img.yakuza_data.inited = True
                        new_img.yakuza_data.yk_name = found_name
                except Exception:
                    pass

            # For each proxy image that was created during import, remap any node references
            for old_img in yk_image_name_to_blender_images.get(found_name, []):
                try:
                    if new_img is not None:
                        # Replace node references across all materials
                        for mat in list(bpy.data.materials):
                            try:
                                nt = getattr(mat, 'node_tree', None)
                                if not nt:
                                    continue
                                for node in getattr(nt, 'nodes', []) or []:
                                    try:
                                        if getattr(node, 'type', '') == 'TEX_IMAGE' and getattr(node, 'image', None) == old_img:
                                            node.image = new_img
                                    except Exception:
                                        pass
                            except Exception:
                                pass

                        # Update the image datablock itself to point to file (keeps any other users happy)
                        try:
                            new_img.source = 'FILE'
                            new_img.filepath = found_path
                        except Exception:
                            pass

                        relinked += 1
                    else:
                        # Fallback path: try to set filepath + reload on the old image
                        try:
                            if old_img.source == 'GENERATED' or overwrite_linked:
                                old_img.source = 'FILE'
                                old_img.filepath = found_path
                                try:
                                    old_img.reload()
                                except Exception:
                                    pass
                                try:
                                    old_img.colorspace_settings.name = 'Non-Color'
                                except Exception:
                                    try:
                                        old_img.colorspace_settings.is_data = True
                                    except Exception:
                                        pass
                                relinked += 1
                        except Exception:
                            pass

                    # If the old image has no users after remapping, remove it to keep data clean
                    try:
                        if old_img.users == 0:
                            # Preserve images even if they have no users to avoid accidental data loss.
                            try:
                                try:
                                    prefs_addon = getattr(bpy.context.preferences.addons.get('yk_par_lib_tool'), 'preferences', None)
                                    pref_path = getattr(prefs_addon, 'dds_extract_path', '') or '' if prefs_addon else ''
                                    if not pref_path:
                                        _register_preserved_tmp('<image_preserve>')
                                except Exception:
                                    _register_preserved_tmp('<image_preserve>')
                                print(f"[yk_par_lib_tool] Preserving unused image datablock (not removed): {getattr(old_img, 'name', '<unnamed>')}")
                            except Exception:
                                pass
                    except Exception:
                        pass
                except Exception:
                    pass
        except Exception:
            # Ignore per-file failures and continue
            pass

    return relinked


def _is_expanded(scene, key: str) -> bool:
    for it in getattr(scene, 'yk_par_expanded', []) or []:
        if getattr(it, 'key', None) == key:
            return True
    return False


def _set_expanded(scene, key: str, value: bool):
    if value:
        # add if missing
        if not _is_expanded(scene, key):
            it = scene.yk_par_expanded.add()
            it.key = key
    else:
        # remove any matching
        prefs = scene.yk_par_expanded
        for i in range(len(prefs)-1, -1, -1):
            if getattr(prefs[i], 'key', None) == key:
                prefs.remove(i)


class YKPAR_OT_toggle_node(Operator):
    bl_idname = "yk_par_lib_tool.toggle_par_node"
    bl_label = "Toggle"
    par_path: StringProperty()
    internal_path: StringProperty()

    def execute(self, context):
        key = _make_node_key(self.par_path, self.internal_path)
        scene = context.scene
        cur = _is_expanded(scene, key)
        _set_expanded(scene, key, not cur)
        return {'FINISHED'}


class YKPAR_OT_clear_filter(Operator):
    bl_idname = "yk_par_lib_tool.clear_par_filter"
    bl_label = "Clear PAR Filter"

    def execute(self, context):
        try:
            context.scene.yk_par_filter = ""
            bpy.ops.yk_par_lib_tool.refresh_par_listing()
        except Exception:
            pass
        return {'FINISHED'}


class YKPAR_OT_import_file(Operator):
    """Import a GMD from a PAR using the Modeling import flow."""
    bl_idname = 'yk_par_lib_tool.import_par_file'
    bl_label = 'Import GMD from PAR'
    par_path: StringProperty()
    internal_path: StringProperty()
    def execute(self, context):
        """Import a single GMD file from a PAR into Blender (creates proxy images for textures).

        This implementation is a cleaned-up single copy (duplicates removed) and ensures
        IMPORT_IN_PROGRESS is cleared in a finally block.
        """
        global IMPORT_IN_PROGRESS
        if IMPORT_IN_PROGRESS:
            self.report({'ERROR'}, 'Another import is in progress')
            return {'CANCELLED'}

        IMPORT_IN_PROGRESS = True
        coll = None
        tmp_dir = None
        try:
            # Debug: show configured DDS extract preference for troubleshooting
            try:
                prefs_addon_dbg = context.preferences.addons.get('yk_par_lib_tool')
                if prefs_addon_dbg and getattr(prefs_addon_dbg, 'preferences', None):
                    cfg = getattr(prefs_addon_dbg.preferences, 'dds_extract_path', '') or ''
                else:
                    cfg = '<no addon prefs>'
                print(f"[yk_par_lib_tool] Import start: configured dds_extract_path='{cfg}'")
            except Exception as _e:
                print(f"[yk_par_lib_tool] Import start: failed to read configured dds_extract_path: {_e}")

            item_par = self.par_path
            internal = self.internal_path

            try:
                par = read_par(item_par)
            except Exception as e:
                self.report({'ERROR'}, f"Failed to read PAR: {e}")
                return {'CANCELLED'}

            target = None

            def _norm_path(p: str) -> str:
                if not p:
                    return ''
                return p.lstrip('/').replace('\\', '/').lower()

            internal_norm = _norm_path(internal)

            def find_file(folder, prefix):
                nonlocal target
                for f in getattr(folder, 'files', []) or []:
                    candidate = _norm_path(prefix + (f.name or ''))
                    if candidate == internal_norm:
                        target = f
                        return True
                for sub in getattr(folder, 'folders', []) or []:
                    if find_file(sub, prefix + (sub.name or '') + '/'):
                        return True
                return False

            root = par.folders[0] if getattr(par, 'folders', None) and len(par.folders) else None
            if not root:
                self.report({'ERROR'}, 'PAR has no root')
                return {'CANCELLED'}
            find_file(root, '')
            if not target:
                self.report({'ERROR'}, 'File not found in PAR')
                return {'CANCELLED'}

            # Decompress only the matching file
            if getattr(target, 'compression', 0):
                try:
                    target_data = decompress_file(target)
                except Exception as de:
                    self.report({'ERROR'}, f"Failed to decompress file: {de}")
                    return {'CANCELLED'}
            else:
                target_data = _get_file_data(target)

            file_bytes = bytes(target_data) if isinstance(target_data, (bytearray, memoryview)) else target_data
            from .gmd_importers import import_gmd_bytes_to_collection

            # Import GMD and create proxy images for missing textures
            coll = import_gmd_bytes_to_collection(context, target.name, file_bytes, prefer_skinned=True, strict=True)

            # Collect texture basenames referenced by the imported collection
            names_set = set()
            if coll:
                for obj in getattr(coll, 'objects', []) or []:
                    for slot in getattr(obj, 'material_slots', []) or []:
                        ma = getattr(slot, 'material', None)
                        if not ma or not hasattr(ma, 'node_tree') or not ma.node_tree:
                            continue
                        for node in ma.node_tree.nodes:
                            if node.type == 'TEX_IMAGE' and node.image:
                                tex_name = os.path.splitext(node.image.name)[0].lower()
                                names_set.add(tex_name)
            try:
                print(f"[yk_par_lib_tool] Derived names_set for import '{target.name}': {sorted(list(names_set))}")
            except Exception:
                pass

            # Try extracting matching DDS files from PAR into the extraction dir (preference-aware)
            if names_set:
                try:
                    tmp_dir = _extract_matching_dds_to_temp(par, names_set, internal, context=context)
                except Exception:
                    tmp_dir = None

            # Attempt to write the original .gmd file to the same extraction folder for modding
            try:
                try:
                    written = _write_gmd_to_extraction(file_bytes, target.name, tmp_dir, internal, names_set, context=context)
                    if written:
                        print(f"[yk_par_lib_tool] Wrote GMD to extraction folder: {written}")
                except Exception:
                    pass
            except Exception:
                pass

            # If we have an extraction dir, attempt relink (the relink helper will prefer the user preference)
            if tmp_dir:
                try:
                    try:
                        print(f"[yk_par_lib_tool] Attempting relink with tmp_dir='{tmp_dir}'")
                    except Exception:
                        pass

                    try:
                        relinked = _relink_images_from_folder(tmp_dir, overwrite_linked=True, case_sensitive=False)
                    except Exception as e:
                        print(f"[yk_par_lib_tool] Relink failed: {e}")
                        relinked = 0

                    try:
                        extracted_files = [f for f in os.listdir(tmp_dir) if os.path.isfile(os.path.join(tmp_dir, f))]
                        extracted_count = len(extracted_files)
                    except Exception:
                        extracted_count = 0

                    try:
                        self.report({'INFO'}, f"Attempted extract {extracted_count} textures, relinked {relinked} images")
                    except Exception:
                        pass

                    if relinked == 0:
                        try:
                            # If the user configured a DDS extract path, point them to that
                            try:
                                prefs_addon = context.preferences.addons.get('yk_par_lib_tool')
                                cfg = getattr(prefs_addon.preferences, 'dds_extract_path', '') or '' if prefs_addon else ''
                                if cfg:
                                    self.report({'WARNING'}, f"No images were relinked. Check your DDS extract folder: {cfg}")
                                else:
                                    self.report({'WARNING'}, f"No images were relinked. Temp dir kept for inspection: {tmp_dir}")
                            except Exception:
                                self.report({'WARNING'}, f"No images were relinked. Temp dir kept for inspection: {tmp_dir}")
                        except Exception:
                            pass
                    else:
                        try:
                            print(f"[yk_par_lib_tool] Preserving temp dir after relink (not deleted): {tmp_dir}")
                        except Exception:
                            pass
                except Exception:
                    # Preserve temp dirs for inspection instead of deleting them
                    try:
                        if tmp_dir:
                            try:
                                prefs_addon = getattr(bpy.context.preferences.addons.get('yk_par_lib_tool'), 'preferences', None)
                                pref_path = getattr(prefs_addon, 'dds_extract_path', '') or '' if prefs_addon else ''
                                if not pref_path:
                                    _register_preserved_tmp(tmp_dir)
                            except Exception:
                                _register_preserved_tmp(tmp_dir)
                            print(f"[yk_par_lib_tool] Preserving temp dir after failure (not deleted): {tmp_dir}")
                    except Exception:
                        pass

        except Exception as e:
            self.report({'ERROR'}, f"Import failed: {e}")
            return {'CANCELLED'}
        finally:
            IMPORT_IN_PROGRESS = False

        if coll:
            try:
                self.report({'INFO'}, f"Imported {target.name}")
            except Exception:
                pass
            return {'FINISHED'}
        else:
            try:
                self.report({'ERROR'}, 'Import returned no collection')
            except Exception:
                pass
            return {'CANCELLED'}
        
        IMPORT_IN_PROGRESS = True
        try:
            item_par = self.par_path
            internal = self.internal_path
            try:
                par = read_par(item_par)
            except Exception as e:
                self.report({'ERROR'}, f"Failed to read PAR: {e}")
                return {'CANCELLED'}
            
            target = None
            # normalize paths for robust comparison
            def _norm_path(p: str) -> str:
                if not p:
                    return ''
                return p.lstrip('/').replace('\\', '/').lower()
            
            internal_norm = _norm_path(internal)
            def find_file(folder, prefix):
                nonlocal target
                for f in getattr(folder, 'files', []) or []:
                    candidate = _norm_path(prefix + (f.name or ''))
                    if candidate == internal_norm:
                        target = f
                        return True
                for sub in getattr(folder, 'folders', []) or []:
                    if find_file(sub, prefix + (sub.name or '') + '/'):
                        return True
                return False
            
            root = par.folders[0] if getattr(par, 'folders', None) and len(par.folders) else None
            if not root:
                self.report({'ERROR'}, 'PAR has no root')
                return {'CANCELLED'}
            find_file(root, '')
            if not target:
                self.report({'ERROR'}, 'File not found in PAR')
                return {'CANCELLED'}
            
            try:
                # Decompress only the matching file (avoid decompressing entire PAR)
                if getattr(target, 'compression', 0):
                    try:
                        target_data = decompress_file(target)
                    except Exception as de:
                        self.report({'ERROR'}, f"Failed to decompress file: {de}")
                        return {'CANCELLED'}
                else:
                    target_data = _get_file_data(target)
                
                file_bytes = bytes(target_data) if isinstance(target_data, (bytearray, memoryview)) else target_data
                from .gmd_importers import import_gmd_bytes_to_collection
                
                # collect existing yakuza-created image names so we can compute the delta
                existing_names = set()
                for img in bpy.data.images:
                    try:
                        if getattr(img, 'yakuza_data', None) and img.yakuza_data.inited:
                            n = img.yakuza_data.yk_name
                            if isinstance(n, str):
                                existing_names.add(n.lower())
                    except Exception:
                        pass
                
                # Import without gmd_folder so proxy images are created for missing textures
                coll = import_gmd_bytes_to_collection(context, target.name, file_bytes, prefer_skinned=True, strict=True)
                
                # Collect texture basenames from imported collection's materials (only those referenced by the imported GMD)
                names_set = set()
                if coll:
                    for obj in getattr(coll, 'objects', []) or []:
                        for slot in getattr(obj, 'material_slots', []) or []:
                            ma = getattr(slot, 'material', None)
                            if not ma or not hasattr(ma, 'node_tree') or not ma.node_tree:
                                continue
                            for node in ma.node_tree.nodes:
                                if node.type == 'TEX_IMAGE' and node.image:
                                    # Use Blender image name (should match DDS basename)
                                    tex_name = os.path.splitext(node.image.name)[0].lower()
                                    names_set.add(tex_name)
                
                # Extract only matching DDS files from PAR to temp dir
                tmp_dir = None
                extracted_files = []
                if names_set:
                    try:
                        tmp_dir, _created = _resolve_extraction_dir_pref(context, prefix='ykpar_dds_')
                        if not tmp_dir:
                            tmp_dir = None
                        else:
                            # When a configured DDS extract path is present, create/use a
                            # deterministic subfolder for this GMD import so files are
                            # grouped predictably and do not collide with other imports.
                            try:
                                tmp_dir = _deterministic_extraction_subdir(tmp_dir, internal, names_set, prefix='ykpar_dds_match_')
                            except Exception:
                                # fallback: use the root preference path
                                pass
                            print(f"[yk_par_lib_tool] Extracting files into configured DDS extract path: {tmp_dir}")
                        rootf = par.folders[0] if getattr(par, 'folders', None) and len(par.folders) else None
                        def walk_and_extract(folder):
                            for f in getattr(folder, 'files', []) or []:
                                fname = f.name.lower()
                                base = os.path.splitext(os.path.basename(fname))[0]
                                if fname.endswith('.dds') and base in names_set:
                                    try:
                                        # handle compressed files
                                        raw = _get_file_data(f)
                                        data = bytes(raw) if isinstance(raw, (bytearray, memoryview)) else raw
                                        out_path = _safe_write_bytes(tmp_dir, os.path.basename(fname), data)
                                        extracted_files.append(out_path)
                                        print(f"[yk_par_lib_tool] Wrote extracted DDS: {out_path} ({len(data) if data else 0} bytes)")
                
                                        # Diagnostic: warn if file is suspiciously small
                                        try:
                                            sz = os.path.getsize(out_path)
                                            if sz < 128:
                                                print(f"WARNING: Extracted DDS file {out_path} is very small ({sz} bytes)")
                                        except Exception:
                                            pass
                
                                        # Try converting to PNG (best-effort). Use Pillow if available.
                                        try:
                                            from PIL import Image
                                            try:
                                                img = Image.open(out_path)
                                                png_path = os.path.splitext(out_path)[0] + '.png'
                                                img.save(png_path, format='PNG')
                                                extracted_files.append(png_path)
                                                print(f"Converted extracted DDS to PNG: {png_path}")
                                            except Exception as _e:
                                                # conversion failed, continue silently
                                                print(f"NOTICE: Failed to convert {out_path} to PNG: {_e}")
                                        except Exception:
                                            # Pillow not available; skip conversion
                                            pass
                                    except Exception as e:
                                        print(f"ERROR: Failed to write extracted DDS {fname}: {e}")
                            for sub in getattr(folder, 'folders', []) or []:
                                walk_and_extract(sub)
                        if rootf:
                            walk_and_extract(rootf)
                    except Exception:
                        tmp_dir = None
                
                # Relink only images in the imported collection to the extracted DDS files
                relinked = 0
                if tmp_dir and extracted_files:
                    for obj in getattr(coll, 'objects', []) or []:
                        for slot in getattr(obj, 'material_slots', []) or []:
                            ma = getattr(slot, 'material', None)
                            if not ma or not hasattr(ma, 'node_tree') or not ma.node_tree:
                                continue
                            for node in ma.node_tree.nodes:
                                if node.type == 'TEX_IMAGE' and node.image:
                                    img = node.image
                                    img_name = os.path.splitext(img.name)[0].lower()
                                    for ext in ('.png', '.dds', '.jpg', '.jpeg'):
                                        # prefer high-res '_l' variant if present
                                        candidate_l = os.path.join(tmp_dir, img_name + '_l' + ext)
                                        candidate = os.path.join(tmp_dir, img_name + ext)
                                        if os.path.exists(candidate_l):
                                            img.filepath = candidate_l
                                            img.reload()
                                            relinked += 1
                                            break
                                        if os.path.exists(candidate):
                                            img.filepath = candidate
                                            img.reload()
                                            relinked += 1
                                            break
                    try:
                        self.report({'INFO'}, f"Extracted {len(extracted_files)} DDS files, relinked {relinked} images")
                    except Exception:
                        pass
                    # Preserve the temp dir for inspection (do not delete)
                    try:
                        print(f"[yk_par_lib_tool] Preserving temp dir (not deleted): {tmp_dir}")
                    except Exception:
                        pass
                else:
                    self.report({'WARNING'}, "No DDS files extracted; images may remain blank.")
                
                # Debug: report derived names_set so user can see what we'll try to extract
                try:
                    if not names_set:
                        try:
                            self.report({'WARNING'}, "No texture names derived from imported collection; will fall back to global diff method")
                        except Exception:
                            pass
                    else:
                        try:
                            sample = ', '.join(sorted(list(names_set))[:50])
                            self.report({'INFO'}, f"Derived texture names ({len(names_set)}): {sample}")
                        except Exception:
                            pass
                except Exception:
                    pass
                
                tmp_dir = None
                if names_set:
                    try:
                        print(f"[yk_par_lib_tool] Extracted matching DDS temp dir: '{tmp_dir}'")
                        tmp_dir = _extract_matching_dds_to_temp(par, names_set, internal, context=context)
                    except Exception:
                        tmp_dir = None
                
                # If we extracted matching DDS files, relink the proxy images and report counts
                if tmp_dir:
                    try:
                        # Use the actual extraction directory (tmp_dir) for relinking if present.
                        # Only fall back to the configured preference folder when no extraction
                        # dir was produced.
                        if tmp_dir:
                            relink_dir = tmp_dir
                        else:
                            relink_dir = None
                            try:
                                prefs_addon = context.preferences.addons.get('yk_par_lib_tool')
                                if prefs_addon:
                                    cand = getattr(prefs_addon.preferences, 'dds_extract_path', '') or ''
                                    if cand:
                                        relink_dir = cand
                            except Exception:
                                relink_dir = None
                        if not relink_dir:
                            # nothing to relink from
                            relinked = 0
                        else:
                            relinked = _relink_images_from_folder(relink_dir, overwrite_linked=True, case_sensitive=False)
                        print(f"[yk_par_lib_tool] Relink dir chosen: '{relink_dir}' (tmp_dir='{tmp_dir}')")
                        try:
                            extracted_files = [f for f in os.listdir(tmp_dir) if os.path.isfile(os.path.join(tmp_dir, f))]
                            extracted_count = len(extracted_files)
                        except Exception:
                            extracted_files = []
                            extracted_count = 0
                        try:
                            # Report what we attempted
                            self.report({'INFO'}, f"Attempted extract {extracted_count} textures, relinked {relinked} images")
                            # Show a short sample of requested names for debugging
                            try:
                                sample_names = ', '.join(sorted(list(names_set))[:50])
                                self.report({'INFO'}, f"Requested texture names: {sample_names}")
                            except Exception:
                                pass
                        except Exception:
                            pass
                
                        # If nothing was relinked, keep the temp dir for inspection and warn the user
                        if relinked == 0:
                            try:
                                self.report({'WARNING'}, f"No images were relinked. Temp dir kept for inspection: {tmp_dir}")
                            except Exception:
                                pass
                        else:
                            try:
                                # Preserve temp dir for inspection (do not delete)
                                print(f"[yk_par_lib_tool] Preserving temp dir after relink (not deleted): {tmp_dir}")
                            except Exception:
                                pass
                    except Exception:
                        # On any failure try to remove the temp dir (best-effort)
                        try:
                            if tmp_dir:
                                try:
                                    prefs_addon = getattr(bpy.context.preferences.addons.get('yk_par_lib_tool'), 'preferences', None)
                                    pref_path = getattr(prefs_addon, 'dds_extract_path', '') or '' if prefs_addon else ''
                                    if not pref_path:
                                        _register_preserved_tmp(tmp_dir)
                                except Exception:
                                    _register_preserved_tmp(tmp_dir)
                                print(f"[yk_par_lib_tool] Preserving temp dir after failure (not deleted): {tmp_dir}")
                        except Exception:
                            pass
            except Exception as e:
                self.report({'ERROR'}, f"Import failed: {e}")
                return {'CANCELLED'}
            
            if coll:
                self.report({'INFO'}, f"Imported {target.name}")
                return {'FINISHED'}
            else:
                self.report({'ERROR'}, 'Import returned no collection')
                return {'CANCELLED'}
        finally:
            IMPORT_IN_PROGRESS = False

        IMPORT_IN_PROGRESS = True
        try:
            item_par = self.par_path
            internal = self.internal_path
            try:
                par = read_par(item_par)
            except Exception as e:
                self.report({'ERROR'}, f"Failed to read PAR: {e}")
                return {'CANCELLED'}

            target = None

            # normalize paths for robust comparison
            def _norm_path(p: str) -> str:
                if not p:
                    return ''
                return p.lstrip('/').replace('\\', '/').lower()

            internal_norm = _norm_path(internal)

            def find_file(folder, prefix):
                nonlocal target
                for f in getattr(folder, 'files', []) or []:
                    candidate = _norm_path(prefix + (f.name or ''))
                    if candidate == internal_norm:
                        target = f
                        return True
                for sub in getattr(folder, 'folders', []) or []:
                    if find_file(sub, prefix + (sub.name or '') + '/'):
                        return True
                return False

            root = par.folders[0] if getattr(par, 'folders', None) and len(par.folders) else None
            if not root:
                self.report({'ERROR'}, 'PAR has no root')
                return {'CANCELLED'}
            find_file(root, '')
            if not target:
                self.report({'ERROR'}, 'File not found in PAR')
                return {'CANCELLED'}

            try:
                # Decompress only the matching file (avoid decompressing entire PAR)
                if getattr(target, 'compression', 0):
                    try:
                        target_data = decompress_file(target)
                    except Exception as de:
                        self.report({'ERROR'}, f"Failed to decompress file: {de}")
                        return {'CANCELLED'}
                else:
                    target_data = _get_file_data(target)

                file_bytes = bytes(target_data) if isinstance(target_data, (bytearray, memoryview)) else target_data
                from .gmd_importers import import_gmd_bytes_to_collection

                # collect existing yakuza-created image names so we can compute the delta
                existing_names = set()
                for img in bpy.data.images:
                    try:
                        if getattr(img, 'yakuza_data', None) and img.yakuza_data.inited:
                            n = img.yakuza_data.yk_name
                            if isinstance(n, str):
                                existing_names.add(n.lower())
                    except Exception:
                        pass

                # Import without gmd_folder so proxy images are created for missing textures
                coll = import_gmd_bytes_to_collection(context, target.name, file_bytes, prefer_skinned=True, strict=True)

                # Collect texture basenames from imported collection's materials (only those referenced by the imported GMD)
                names_set = set()
                if coll:
                    for obj in getattr(coll, 'objects', []) or []:
                        for slot in getattr(obj, 'material_slots', []) or []:
                            ma = getattr(slot, 'material', None)
                            if not ma or not hasattr(ma, 'node_tree') or not ma.node_tree:
                                continue
                            for node in ma.node_tree.nodes:
                                if node.type == 'TEX_IMAGE' and node.image:
                                    # Use Blender image name (should match DDS basename)
                                    tex_name = os.path.splitext(node.image.name)[0].lower()
                                    names_set.add(tex_name)

                # Extract only matching DDS files from PAR to temp dir
                tmp_dir = None
                extracted_files = []
                if names_set:
                    try:
                        tmp_dir, _created = _resolve_extraction_dir_pref(context, prefix='ykpar_dds_')
                        if not tmp_dir:
                            tmp_dir = None
                        else:
                            print(f"[yk_par_lib_tool] Extracting files into configured DDS extract path: {tmp_dir}")
                        rootf = par.folders[0] if getattr(par, 'folders', None) and len(par.folders) else None
                        def walk_and_extract(folder):
                            for f in getattr(folder, 'files', []) or []:
                                fname = f.name.lower()
                                base = os.path.splitext(os.path.basename(fname))[0]
                                if fname.endswith('.dds') and base in names_set:
                                    try:
                                        # handle compressed files
                                        raw = _get_file_data(f)
                                        data = bytes(raw) if isinstance(raw, (bytearray, memoryview)) else raw
                                        out_path = _safe_write_bytes(tmp_dir, os.path.basename(fname), data)
                                        extracted_files.append(out_path)
                                        print(f"[yk_par_lib_tool] Wrote extracted DDS: {out_path} ({len(data) if data else 0} bytes)")

                                        # Diagnostic: warn if file is suspiciously small
                                        try:
                                            sz = os.path.getsize(out_path)
                                            if sz < 128:
                                                print(f"WARNING: Extracted DDS file {out_path} is very small ({sz} bytes)")
                                        except Exception:
                                            pass

                                        # Try converting to PNG (best-effort). Use Pillow if available.
                                        try:
                                            from PIL import Image
                                            try:
                                                img = Image.open(out_path)
                                                png_path = os.path.splitext(out_path)[0] + '.png'
                                                img.save(png_path, format='PNG')
                                                extracted_files.append(png_path)
                                                print(f"Converted extracted DDS to PNG: {png_path}")
                                            except Exception as _e:
                                                # conversion failed, continue silently
                                                print(f"NOTICE: Failed to convert {out_path} to PNG: {_e}")
                                        except Exception:
                                            # Pillow not available; skip conversion
                                            pass
                                    except Exception as e:
                                        print(f"ERROR: Failed to write extracted DDS {fname}: {e}")
                            for sub in getattr(folder, 'folders', []) or []:
                                walk_and_extract(sub)
                        if rootf:
                            walk_and_extract(rootf)

                        # After extraction, write the .gmd into the same extraction folder when possible
                        try:
                            written = _write_gmd_to_extraction(file_bytes, target.name, tmp_dir, internal, names_set, context=context)
                            if written:
                                print(f"[yk_par_lib_tool] Wrote GMD to extraction folder: {written}")
                        except Exception:
                            pass
                    except Exception:
                        tmp_dir = None

                # Relink only images in the imported collection to the extracted DDS files
                relinked = 0
                if tmp_dir and extracted_files:
                    for obj in getattr(coll, 'objects', []) or []:
                        for slot in getattr(obj, 'material_slots', []) or []:
                            ma = getattr(slot, 'material', None)
                            if not ma or not hasattr(ma, 'node_tree') or not ma.node_tree:
                                continue
                            for node in ma.node_tree.nodes:
                                if node.type == 'TEX_IMAGE' and node.image:
                                    img = node.image
                                    img_name = os.path.splitext(img.name)[0].lower()
                                    for ext in ('.png', '.dds', '.jpg', '.jpeg'):
                                        candidate = os.path.join(tmp_dir, img_name + ext)
                                        if os.path.exists(candidate):
                                            img.filepath = candidate
                                            img.reload()
                                            relinked += 1
                                            break
                    try:
                        self.report({'INFO'}, f"Extracted {len(extracted_files)} DDS files, relinked {relinked} images")
                    except Exception:
                        pass
                    # Preserve the temp dir for inspection (do not delete)
                    try:
                        print(f"[yk_par_lib_tool] Preserving temp dir (not deleted): {tmp_dir}")
                    except Exception:
                        pass
                else:
                    self.report({'WARNING'}, "No DDS files extracted; images may remain blank.")

                # Debug: report derived names_set so user can see what we'll try to extract
                try:
                    if not names_set:
                        try:
                            self.report({'WARNING'}, "No texture names derived from imported collection; will fall back to global diff method")
                        except Exception:
                            pass
                    else:
                        try:
                            sample = ', '.join(sorted(list(names_set))[:50])
                            self.report({'INFO'}, f"Derived texture names ({len(names_set)}): {sample}")
                        except Exception:
                            pass
                except Exception:
                    pass

                tmp_dir = None
                if names_set:
                    try:
                        tmp_dir = _extract_matching_dds_to_temp(par, names_set, internal, context=context)
                    except Exception:
                        tmp_dir = None

                # If we extracted matching DDS files, relink the proxy images and report counts
                if tmp_dir:
                    try:
                        # Prefer user-configured DDS extract folder for relinking when available
                        relink_dir = None
                        try:
                            prefs_addon = context.preferences.addons.get('yk_par_lib_tool')
                            if prefs_addon:
                                cand = getattr(prefs_addon.preferences, 'dds_extract_path', '') or ''
                                if cand:
                                    relink_dir = cand
                        except Exception:
                            relink_dir = None
                        if not relink_dir:
                            relink_dir = tmp_dir

                        relinked = _relink_images_from_folder(relink_dir, overwrite_linked=True, case_sensitive=False)

                        relinked = _relink_images_from_folder(relink_dir, overwrite_linked=True, case_sensitive=False)
                        try:
                            extracted_files = [f for f in os.listdir(tmp_dir) if os.path.isfile(os.path.join(tmp_dir, f))]
                            extracted_count = len(extracted_files)
                        except Exception:
                            extracted_files = []
                            extracted_count = 0
                        try:
                            # Report what we attempted
                            self.report({'INFO'}, f"Attempted extract {extracted_count} textures, relinked {relinked} images")
                            # Show a short sample of requested names for debugging
                            try:
                                sample_names = ', '.join(sorted(list(names_set))[:50])
                                self.report({'INFO'}, f"Requested texture names: {sample_names}")
                            except Exception:
                                pass
                        except Exception:
                            pass

                        # If nothing was relinked, keep the temp dir for inspection and warn the user
                        if relinked == 0:
                            try:
                                self.report({'WARNING'}, f"No images were relinked. Temp dir kept for inspection: {tmp_dir}")
                            except Exception:
                                pass
                        else:
                            try:
                                # Preserve temp dir for inspection (do not delete)
                                print(f"[yk_par_lib_tool] Preserving temp dir after relink (not deleted): {tmp_dir}")
                            except Exception:
                                pass
                    except Exception:
                        # On any failure preserve the temp dir for inspection (do not delete)
                        try:
                            if tmp_dir:
                                try:
                                    prefs_addon = getattr(bpy.context.preferences.addons.get('yk_par_lib_tool'), 'preferences', None)
                                    pref_path = getattr(prefs_addon, 'dds_extract_path', '') or '' if prefs_addon else ''
                                    if not pref_path:
                                        _register_preserved_tmp(tmp_dir)
                                except Exception:
                                    _register_preserved_tmp(tmp_dir)
                                print(f"[yk_par_lib_tool] Preserving temp dir after failure (not deleted): {tmp_dir}")
                        except Exception:
                            pass
            except Exception as e:
                self.report({'ERROR'}, f"Import failed: {e}")
                return {'CANCELLED'}

            if coll:
                self.report({'INFO'}, f"Imported {target.name}")
                return {'FINISHED'}
            else:
                self.report({'ERROR'}, 'Import returned no collection')
                return {'CANCELLED'}
        finally:
            IMPORT_IN_PROGRESS = False


class YKPAR_OT_import_selected_multiple(BaseImportGMD, Operator):
    """Import all checked .gmd items from the filtered node list."""
    bl_idname = "yk_par_lib_tool.import_selected_multiple"
    bl_label = "Import Selected GMDs"

    def execute(self, context):
        global IMPORT_IN_PROGRESS
        if IMPORT_IN_PROGRESS:
            self.report({'ERROR'}, 'Another import is in progress')
            return {'CANCELLED'}
        
        IMPORT_IN_PROGRESS = True
        imported_count = 0
        try:
            scene = context.scene
            nodes = getattr(scene, 'yk_par_nodes', None)
            if not nodes:
                self.report({'ERROR'}, 'No nodes available')
                return {'CANCELLED'}
            
            # collect selected items
            selected = [n for n in nodes if getattr(n, 'selected', False) and not getattr(n, 'is_folder', False)]
            if not selected:
                self.report({'ERROR'}, 'No selected files to import')
                return {'CANCELLED'}
            
            for item in selected:
                try:
                    par_path = item.par_path
                    internal = item.internal_path
                    try:
                        par = read_par(par_path)
                    except Exception as e:
                        self.report({'WARNING'}, f'Failed to read PAR {par_path}: {e}')
                        continue
                    
                    # find file object
                    target = None
                    
                    def _norm_path(p: str) -> str:
                        if not p:
                            return ''
                        return p.lstrip('/').replace('\\', '/').lower()
                    
                    internal_norm = _norm_path(internal)
                    
                    def find_file(folder, prefix):
                        nonlocal target
                        for f in getattr(folder, 'files', []) or []:
                            candidate = _norm_path(prefix + (f.name or ''))
                            if candidate == internal_norm:
                                target = f
                                return True
                        for sub in getattr(folder, 'folders', []) or []:
                            if find_file(sub, prefix + (sub.name or '') + '/'):
                                return True
                        return False
                    
                    root = par.folders[0] if getattr(par, 'folders', None) and len(par.folders) else None
                    if not root:
                        self.report({'WARNING'}, f'PAR has no root: {par_path}')
                        continue
                    find_file(root, '')
                    if not target:
                        self.report({'WARNING'}, f'File not found in PAR: {internal} in {par_path}')
                        continue
                    
                    try:
                        # Decompress if needed
                        if getattr(target, 'compression', 0):
                            try:
                                target_data = decompress_file(target)
                            except Exception as de:
                                self.report({'WARNING'}, f'Failed to decompress file: {de}')
                                continue
                        else:
                            target_data = _get_file_data(target)
                        
                        file_bytes = bytes(target_data) if isinstance(target_data, (bytearray, memoryview)) else target_data
                        try:
                            coll = import_gmd_bytes_to_collection(context, target.name, file_bytes, prefer_skinned=True, strict=True)
                            if coll:
                                imported_count += 1
                                # After successful import, try to extract matching DDS files from the PAR
                                try:
                                    # Collect texture basenames referenced by the imported collection
                                    names_set = set()
                                    for obj in getattr(coll, 'objects', []) or []:
                                        for slot in getattr(obj, 'material_slots', []) or []:
                                            ma = getattr(slot, 'material', None)
                                            if not ma or not hasattr(ma, 'node_tree') or not ma.node_tree:
                                                continue
                                            for node in ma.node_tree.nodes:
                                                if node.type == 'TEX_IMAGE' and node.image:
                                                    tex_name = os.path.splitext(node.image.name)[0].lower()
                                                    names_set.add(tex_name)
                                    try:
                                        print(f"[yk_par_lib_tool] Derived names_set for import '{target.name}': {sorted(list(names_set))}")
                                    except Exception:
                                        pass
                                    if names_set:
                                        try:
                                            tmp_dir = _extract_matching_dds_to_temp(par, names_set, internal, context=context)
                                        except Exception as _e:
                                            tmp_dir = None

                                        if tmp_dir:
                                            try:
                                                # _relink_images_from_folder prefers the add-on preference path when available
                                                relinked = _relink_images_from_folder(tmp_dir, overwrite_linked=True, case_sensitive=False)
                                                try:
                                                    # Report per-file result
                                                    context_msg = f"{target.name}: extracted -> {tmp_dir}, relinked {relinked} images"
                                                    try:
                                                        self.report({'INFO'}, context_msg)
                                                    except Exception:
                                                        pass
                                                except Exception:
                                                    pass
                                            except Exception:
                                                # Do NOT delete the extracted folder. Preserve it for inspection.
                                                try:
                                                    _register_preserved_tmp(tmp_dir)
                                                    print(f"[yk_par_lib_tool] Preserving extracted folder (was previously removed): {tmp_dir}")
                                                except Exception:
                                                    pass
                                except Exception:
                                    # ignore per-file extraction errors
                                    pass
                        except Exception as e:
                            self.report({'WARNING'}, f'Failed to import {target.name}: {e}')
                            continue
                    except Exception as e:
                        self.report({'WARNING'}, f'Import failed for {internal}: {e}')
                        continue
                except Exception:
                    # continue with other selections even if one fails
                    pass
        finally:
            IMPORT_IN_PROGRESS = False
        
        self.report({'INFO'}, f'Imported {imported_count} of {len(selected)} selected files')
        return {'FINISHED'}



class YKPAR_OT_place_file(Operator):
    bl_idname = 'yk_par_lib_tool.place_par_file'
    bl_label = 'Place GMD from PAR'
    par_path: StringProperty()
    internal_path: StringProperty()

    def execute(self, context):
        global IMPORT_IN_PROGRESS
        if IMPORT_IN_PROGRESS:
            self.report({'ERROR'}, 'Another import is in progress')
            return {'CANCELLED'}

        IMPORT_IN_PROGRESS = True
        try:
            # Import then move to cursor. Perform import directly here to ensure placement
            item_par = self.par_path
            internal = self.internal_path
            try:
                par = read_par(item_par)
            except Exception as e:
                self.report({'ERROR'}, f"Failed to read PAR: {e}")
                return {'CANCELLED'}

            target = None

            def _norm_path(p: str) -> str:
                if not p:
                    return ''
                return p.lstrip('/').replace('\\', '/').lower()

            internal_norm = _norm_path(internal)

            def find_file(folder, prefix):
                nonlocal target
                for f in getattr(folder, 'files', []) or []:
                    candidate = _norm_path(prefix + (f.name or ''))
                    if candidate == internal_norm:
                        target = f
                        return True
                for sub in getattr(folder, 'folders', []) or []:
                    if find_file(sub, prefix + (sub.name or '') + '/'):
                        return True
                return False

            root = par.folders[0] if getattr(par, 'folders', None) and len(par.folders) else None
            if not root:
                self.report({'ERROR'}, 'PAR has no root')
                return {'CANCELLED'}
            find_file(root, '')
            if not target:
                self.report({'ERROR'}, 'File not found in PAR')
                return {'CANCELLED'}

            try:
                if getattr(target, 'compression', 0):
                    try:
                        target_data = decompress_file(target)
                    except Exception as de:
                        self.report({'ERROR'}, f"Failed to decompress file: {de}")
                        return {'CANCELLED'}
                else:
                    target_data = _get_file_data(target)

                file_bytes = bytes(target_data) if isinstance(target_data, (bytearray, memoryview)) else target_data
                from .gmd_importers import import_gmd_bytes_to_collection

                # collect existing yakuza-created image names so we can compute the delta
                existing_names = set()
                for img in bpy.data.images:
                    try:
                        if getattr(img, 'yakuza_data', None) and img.yakuza_data.inited:
                            n = img.yakuza_data.yk_name
                            if isinstance(n, str):
                                existing_names.add(n.lower())
                    except Exception:
                        pass

                # Import without gmd_folder so proxy images are created for missing textures
                coll = import_gmd_bytes_to_collection(context, target.name, file_bytes, prefer_skinned=True, strict=True)

                # Find newly-created yakuza image names and extract only matching DDS files
                try:
                    new_names = set()
                    for img in bpy.data.images:
                        try:
                            if getattr(img, 'yakuza_data', None) and img.yakuza_data.inited:
                                n = img.yakuza_data.yk_name
                                if isinstance(n, str):
                                    nl = n.lower()
                                    if nl not in existing_names:
                                        new_names.add(nl)
                        except Exception:
                            pass
                    try:
                        print(f"[yk_par_lib_tool] Newly created yakuza image names: {sorted(list(new_names))}")
                    except Exception:
                        pass
                    tmp_dir = None
                    if new_names:
                        try:
                            tmp_dir = _extract_matching_dds_to_temp(par, new_names, internal, context=context)
                        except Exception:
                            tmp_dir = None

                    # If we extracted matching DDS files, relink the proxy images
                    if tmp_dir:
                        try:
                            _relink_images_from_folder(tmp_dir, overwrite_linked=True, case_sensitive=False)
                        except Exception:
                            pass
                finally:
                    if 'tmp_dir' in locals() and tmp_dir:
                        try:
                            # Preserve the extracted directory instead of deleting it
                            try:
                                prefs_addon = getattr(bpy.context.preferences.addons.get('yk_par_lib_tool'), 'preferences', None)
                                pref_path = getattr(prefs_addon, 'dds_extract_path', '') or '' if prefs_addon else ''
                                if not pref_path:
                                    _register_preserved_tmp(tmp_dir)
                            except Exception:
                                _register_preserved_tmp(tmp_dir)
                            print(f"[yk_par_lib_tool] Preserving extracted folder after place/import: {tmp_dir}")
                        except Exception:
                            pass
            except Exception as e:
                self.report({'ERROR'}, f"Import failed: {e}")
                return {'CANCELLED'}

            if not coll:
                self.report({'ERROR'}, 'Import returned no collection')
                return {'CANCELLED'}

            cursor_loc = context.scene.cursor.location.copy()
            for obj in coll.objects:
                try:
                    obj.location = cursor_loc
                    if context.collection and obj.name not in context.collection.objects:
                        context.collection.objects.link(obj)
                except Exception:
                    pass

            self.report({'INFO'}, f"Placed {target.name} at cursor")
            return {'FINISHED'}
        finally:
            IMPORT_IN_PROGRESS = False


class YKPAR_OT_relink_preserved_tmp(Operator):
    """Relink images from the most recently preserved extraction temp directory."""
    bl_idname = 'yk_par_lib_tool.relink_preserved_tmp'
    # bl_label = 'Relink Textures' # Not in Use

    def execute(self, context):
        # If the user configured a DDS extract path, use it exclusively for relinking
        try:
            prefs_addon = context.preferences.addons.get('yk_par_lib_tool')
            pref_dir = getattr(prefs_addon.preferences, 'dds_extract_path', '') or '' if prefs_addon else ''
            if pref_dir:
                tmp = pref_dir
            else:
                # No preference: fall back to preserved temp dirs
                # Ensure any persisted registry is loaded into memory first
                if not PRESERVED_TMP_DIRS:
                    loaded = _load_preserved_registry()
                    for p in loaded:
                        if p not in PRESERVED_TMP_DIRS:
                            PRESERVED_TMP_DIRS.append(p)

                if not PRESERVED_TMP_DIRS:
                    self.report({'WARNING'}, 'No preserved temp directories found')
                    return {'CANCELLED'}

                tmp = PRESERVED_TMP_DIRS[-1]
                if not tmp or not os.path.isdir(tmp):
                    # Try to find the first valid preserved dir in the list
                    valid = None
                    for p in PRESERVED_TMP_DIRS:
                        if os.path.isdir(p):
                            valid = p
                            break
                    if not valid:
                        self.report({'WARNING'}, f'Preserved temp dir not found: {tmp}')
                        return {'CANCELLED'}
                    tmp = valid
        except Exception:
            self.report({'WARNING'}, 'Failed to resolve DDS extract preference or preserved temp dirs')
            return {'CANCELLED'}

        try:
            relinked = _relink_images_from_folder(tmp, overwrite_linked=True, case_sensitive=False)
            self.report({'INFO'}, f'Relinked {relinked} images from {tmp}')
        except Exception as e:
            self.report({'ERROR'}, f'Failed to relink from {tmp}: {e}')
            return {'CANCELLED'}

        return {'FINISHED'}


class YKPAR_OT_debug_dds_path(Operator):
    """Debug operator: show configured DDS extract path and resolved extraction dir"""
    bl_idname = 'yk_par_lib_tool.debug_dds_extract_path'
    bl_label = 'Debug DDS Extract Path'

    def execute(self, context):
        try:
            prefs_addon = context.preferences.addons.get('yk_par_lib_tool')
            cfg = ''
            if prefs_addon and getattr(prefs_addon, 'preferences', None):
                cfg = getattr(prefs_addon.preferences, 'dds_extract_path', '') or ''
            self.report({'INFO'}, f"Configured DDS extract path: '{cfg}'")
            # Call helper to show what will be used at runtime
            resolved, created_temp = _resolve_extraction_dir_pref(context, prefix='ykpar_dds_debug_')
            self.report({'INFO'}, f"Resolved extraction dir: '{resolved}' (created_temp={created_temp})")
            print(f"[yk_par_lib_tool] Debug DDS path: configured='{cfg}', resolved='{resolved}', created_temp={created_temp}")
            # Attempt a small write test to the resolved folder so we can detect permission issues
            try:
                if resolved:
                    test_path = os.path.join(resolved, 'ykpar_write_test.tmp')
                    with open(test_path, 'wb') as tf:
                        tf.write(b'ykpar')
                    os.remove(test_path)
                    self.report({'INFO'}, f"Write test succeeded in: {resolved}")
                    print(f"[yk_par_lib_tool] Write test succeeded in: {resolved}")
                else:
                    self.report({'WARNING'}, 'No resolved extraction dir to test')
            except Exception as e:
                self.report({'ERROR'}, f'Write test failed: {e}')
                print(f"[yk_par_lib_tool] Write test failed in '{resolved}': {e}")
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"Debug failed: {e}")
            return {'CANCELLED'}


class YKPAR_OT_confirm_import_selected(Operator):
    bl_idname = 'yk_par_lib_tool.confirm_import_selected'
    bl_label = 'Import Selected (confirm)'
    bl_description = 'Confirm and import checked .gmd files from the filtered list'

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        try:
            bpy.ops.yk_par_lib_tool.import_selected_multiple()
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f'Bulk import failed: {e}')
            return {'CANCELLED'}


# Ensure scene-level properties exist even if addon registration failed or hasn't run yet.
def _ensure_scene_properties():
    if not hasattr(bpy.types.Scene, 'yk_par_nodes'):
        try:
            bpy.types.Scene.yk_par_nodes = CollectionProperty(type=YKPAR_NodeItem)
        except Exception:
            # If registration fails, leave it — operators will guard and report errors
            pass
    if not hasattr(bpy.types.Scene, 'yk_par_node_index'):
        try:
            bpy.types.Scene.yk_par_node_index = IntProperty(name="Active PAR Node Index", default=0)
        except Exception:
            pass
    if not hasattr(bpy.types.Scene, 'yk_par_filter'):
        try:
            bpy.types.Scene.yk_par_filter = StringProperty(name="Filter", default="", update=_on_filter_update)
        except Exception:
            pass
    if not hasattr(bpy.types.Scene, 'yk_par_expanded'):
        try:
            bpy.types.Scene.yk_par_expanded = CollectionProperty(type=YKPAR_ExpandedItem)
        except Exception:
            pass
    if not hasattr(bpy.types.Scene, 'yk_par_auto_refreshed'):
        try:
            bpy.types.Scene.yk_par_auto_refreshed = BoolProperty(name='PAR Auto Refreshed', default=False)
        except Exception:
            pass


# Call at import time so UI scripts can safely reference the properties
_ensure_scene_properties()


# Allowed top-level folder names for 'chara' imports
_ALLOWED_TOP_LEVELS = {"tops", "face", "hair", "face target", "btms"}

def _is_allowed_top_level(name: str) -> bool:
    if not name:
        return False
    n = name.lower()
    if n.startswith('dds'):
        return True
    if n in _ALLOWED_TOP_LEVELS:
        return True
    return False


class YKPAR_OT_refresh(Operator):
    bl_idname = "yk_par_lib_tool.refresh_par_listing"
    bl_label = "Unpack Loaded PARs"
    bl_description = "Unpack configured PAR files and refresh the browser so their contents are available. Use the Filter field to narrow results."

    def execute(self, context):
        # Ensure scene properties are registered before accessing them
        _ensure_scene_properties()
        
        scene = context.scene
        scene.yk_par_nodes.clear()

        # current filter (simple substring, case-insensitive)
        filter_text = (getattr(scene, 'yk_par_filter', '') or '').strip().lower()

        prefs = context.preferences.addons.get('yk_par_lib_tool')
        if not prefs:
            self.report({'ERROR'}, "yk_par_lib_tool preferences not found")
            return {'CANCELLED'}
        prefs = prefs.preferences
        
        for entry in getattr(prefs, 'par_files', []):
            par_path = entry.path
            if not par_path or not os.path.exists(par_path):
                continue

            # top-level folder item representing the PAR file
            top = scene.yk_par_nodes.add()
            try:
                top.name = bpy.path.display_name_from_filepath(par_path)
            except Exception:
                top.name = par_path
            top.internal_path = ""
            top.par_path = par_path
            top.is_folder = True
            top.depth = 0

            try:
                # read_par with caching - now much faster on repeated calls
                par = read_par(par_path)
                
                # cache root folder for UI tree rendering
                try:
                    PAR_CACHE[par_path] = par.folders[0] if getattr(par, 'folders', None) and len(par.folders) else None
                except Exception:
                    PAR_CACHE[par_path] = None
                
                # OPTIMIZATION: Defer search index building until first filtered search
                # This significantly speeds up initial PAR load when no filter is active
                # The index will be built on-demand during the first filtered search
                
                # Use optimized index-based search when filter is active
                if filter_text:
                    # Build index on first use (lazy initialization)
                    if par_path not in _SEARCH_INDEX:
                        _build_search_index(par_path, par)
                    
                    # Fast path: query pre-built index
                    results = _query_search_index(par_path, filter_text)
                    for name, internal_path, is_folder, depth in results:
                        it = scene.yk_par_nodes.add()
                        it.name = name
                        it.internal_path = internal_path
                        it.par_path = par_path
                        it.is_folder = is_folder
                        it.depth = depth
                else:
                    # No filter - use original tree walk (for compatibility)
                    def walk(folder, prefix, depth):
                        # If we're at the top-level, check allowed categories only when no filter is present
                        if depth == 1 and not filter_text:
                            if not _is_allowed_top_level(folder.name):
                                return False

                        found_any = False

                        # If folder name itself matches the filter, treat the entire subtree as matched
                        folder_name = (folder.name or '').lower()
                        folder_matches = False
                        if filter_text and filter_text in folder_name:
                            folder_matches = True

                        # files at this level
                        for f in getattr(folder, 'files', []) or []:
                            name = f.name or ''
                            lname = name.lower()
                            # Include .gmd, .par, and .gmt files so embedded PARs, GMDs, and GMTs are visible
                            if not (lname.endswith('.gmd') or lname.endswith('.par') or lname.endswith('.gmt')):
                                continue
                            # If the folder matched, include all files under it; otherwise, match by filename
                            if filter_text and not folder_matches and filter_text not in lname:
                                # skip non-matching files when a filter is active and the folder didn't match
                                continue
                            it = scene.yk_par_nodes.add()
                            it.name = name
                            it.internal_path = (prefix + name).lstrip('/')
                            it.par_path = par_path
                            it.is_folder = False
                            it.depth = depth
                            found_any = True

                        # recurse into subfolders; only add folder entries if they or their descendants match filter
                        for sub in getattr(folder, 'folders', []) or []:
                            subname = sub.name or ''
                            child_matched = walk(sub, prefix + subname + '/', depth + 1)
                            if child_matched:
                                it = scene.yk_par_nodes.add()
                                it.name = subname
                                it.internal_path = (prefix + subname + '/').lstrip('/')
                                it.par_path = par_path
                                it.is_folder = True
                                it.depth = depth
                                found_any = True

                        # If the folder itself matched but we found no files or matched children (e.g. empty folder),
                        # still consider it matched so its presence is visible in the filtered listing.
                        if folder_matches and not found_any:
                            # add the folder marker (no files beneath matched or present)
                            it = scene.yk_par_nodes.add()
                            it.name = folder.name or ''
                            it.internal_path = (prefix + (folder.name or '') + '/').lstrip('/')
                            it.par_path = par_path
                            it.is_folder = True
                            it.depth = depth
                            found_any = True

                        return found_any

                    root = par.folders[0] if getattr(par, 'folders', None) and len(par.folders) else None
                    if root:
                        walk(root, '', 1)
            except Exception as e:
                print(f"Failed to read PAR {par_path}: {e}")

        return {'FINISHED'}


class YKPAR_OT_import_selected(BaseImportGMD, Operator):
    bl_idname = "yk_par_lib_tool.import_selected_gmd"
    bl_label = "Import Selected GMD"

    def execute(self, context):
        # Ensure scene properties are registered before accessing them
        _ensure_scene_properties()
        
        scene = context.scene
        idx = scene.yk_par_node_index
        if idx < 0 or idx >= len(scene.yk_par_nodes):
            self.report({'ERROR'}, "No node selected")
            return {'CANCELLED'}
        item = scene.yk_par_nodes[idx]
        if item.is_folder:
            self.report({'ERROR'}, "Selected item is a folder")
            return {'CANCELLED'}

        par_path = item.par_path
        internal = item.internal_path
        try:
            par = read_par(par_path)
        except Exception as e:
            self.report({'ERROR'}, f"Failed to read PAR: {e}")
            return {'CANCELLED'}

        # find the file object by normalized internal path
        target = None

        def _norm_path(p: str) -> str:
            if not p:
                return ''
            return p.lstrip('/').replace('\\', '/').lower()

        internal_norm = _norm_path(internal)

        def find_file(folder, prefix):
            nonlocal target
            for f in getattr(folder, 'files', []) or []:
                candidate = _norm_path(prefix + (f.name or ''))
                if candidate == internal_norm:
                    target = f
                    return True
            for sub in getattr(folder, 'folders', []) or []:
                if find_file(sub, prefix + (sub.name or '') + '/'):
                    return True
            return False

        root = par.folders[0] if getattr(par, 'folders', None) and len(par.folders) else None
        if not root:
            self.report({'ERROR'}, "PAR has no root folder")
            return {'CANCELLED'}
        find_file(root, '')
        if not target:
            self.report({'ERROR'}, "Could not find file inside PAR")
            return {'CANCELLED'}

        # parse and import the GMD bytes using the centralized helper
        try:
            target_data = _get_file_data(target)
            file_bytes = bytes(target_data) if isinstance(target_data, (bytearray, memoryview)) else target_data
            from .gmd_importers import import_gmd_bytes_to_collection

            # collect existing yakuza-created image names so we can compute the delta
            existing_names = set()
            for img in bpy.data.images:
                try:
                    if getattr(img, 'yakuza_data', None) and img.yakuza_data.inited:
                        n = img.yakuza_data.yk_name
                        if isinstance(n, str):
                            existing_names.add(n.lower())
                except Exception:
                    pass

            # Import without gmd_folder so proxy images are created for missing textures
            try:
                coll = import_gmd_bytes_to_collection(context, item.name, file_bytes, prefer_skinned=True, strict=self.strict)
            except Exception as e:
                self.report({'ERROR'}, f"Failed to import GMD: {e}")
                return {'CANCELLED'}

            # Find newly-created yakuza image names and extract only matching DDS files
            try:
                new_names = set()
                for img in bpy.data.images:
                    try:
                        if getattr(img, 'yakuza_data', None) and img.yakuza_data.inited:
                            n = img.yakuza_data.yk_name
                            if isinstance(n, str):
                                nl = n.lower()
                                if nl not in existing_names:
                                    new_names.add(nl)
                    except Exception:
                        pass
                    try:
                        print(f"[yk_par_lib_tool] Newly created yakuza image names (place selected): {sorted(list(new_names))}")
                    except Exception:
                        pass
                tmp_dir = None
                if new_names:
                    try:
                        tmp_dir = _extract_matching_dds_to_temp(par, new_names, internal, context=context)
                    except Exception:
                        tmp_dir = None

                # If we extracted matching DDS files, relink the proxy images
                if tmp_dir:
                    try:
                        _relink_images_from_folder(tmp_dir, overwrite_linked=True, case_sensitive=False)
                    except Exception:
                        pass
            finally:
                if 'tmp_dir' in locals() and tmp_dir:
                    try:
                        # Preserve temp dir for inspection (do not delete)
                        print(f"[yk_par_lib_tool] Preserving temp dir (not deleted): {tmp_dir}")
                    except Exception:
                        pass
        except Exception as e:
            self.report({'ERROR'}, f"Failed to import GMD: {e}")
            return {'CANCELLED'}

        if coll:
            self.report({'INFO'}, f"Imported {item.name}")
            return {'FINISHED'}
        else:
            self.report({'ERROR'}, f"Import returned no collection for {item.name}")
            return {'CANCELLED'}


class YKPAR_OT_place_selected(BaseImportGMD, Operator):
    """Import the selected GMD and place it at the 3D cursor (simulates drag-and-drop)"""
    bl_idname = "yk_par_lib_tool.place_selected_gmd"
    bl_label = "Place Selected GMD"

    def execute(self, context):
        # Import the selected file bytes and place the resulting collection at the 3D cursor
        # Ensure scene properties are registered before accessing them
        _ensure_scene_properties()
        
        scene = context.scene
        idx = scene.yk_par_node_index
        if idx < 0 or idx >= len(scene.yk_par_nodes):
            self.report({'ERROR'}, "No node selected")
            return {'CANCELLED'}
        item = scene.yk_par_nodes[idx]

        # read and decompress the par to get the file bytes
        try:
            par = read_par(item.par_path)
            decompress_par(par)
        except Exception as e:
            self.report({'ERROR'}, f"Failed to read PAR: {e}")
            return {'CANCELLED'}

        # find the target file inside the par
        target = None

        def _norm_path(p: str) -> str:
            if not p:
                return ''
            return p.lstrip('/').replace('\\', '/').lower()

        internal_norm = _norm_path(item.internal_path)

        def find_file(folder, prefix):
            nonlocal target
            for f in getattr(folder, 'files', []) or []:
                candidate = _norm_path(prefix + (f.name or ''))
                if candidate == internal_norm:
                    target = f
                    return True
            for sub in getattr(folder, 'folders', []) or []:
                if find_file(sub, prefix + (sub.name or '') + '/'):
                    return True
            return False

        root = par.folders[0] if getattr(par, 'folders', None) and len(par.folders) else None
        if not root:
            self.report({'ERROR'}, "PAR has no root folder")
            return {'CANCELLED'}
        find_file(root, '')
        if not target:
            self.report({'ERROR'}, "Could not find file inside PAR")
            return {'CANCELLED'}

        try:
            target_data = _get_file_data(target)
            file_bytes = bytes(target_data) if isinstance(target_data, (bytearray, memoryview)) else target_data
            from .gmd_importers import import_gmd_bytes_to_collection

            # collect existing yakuza-created image names so we can compute the delta
            existing_names = set()
            for img in bpy.data.images:
                try:
                    if getattr(img, 'yakuza_data', None) and img.yakuza_data.inited:
                        n = img.yakuza_data.yk_name
                        if isinstance(n, str):
                            existing_names.add(n.lower())
                except Exception:
                    pass

            # Import without gmd_folder so proxy images are created for missing textures
            try:
                coll = import_gmd_bytes_to_collection(context, item.name, file_bytes, prefer_skinned=True, strict=True)
            except Exception as e:
                self.report({'ERROR'}, f"Failed to import and place GMD: {e}")
                return {'CANCELLED'}

            # Find newly-created yakuza image names and extract only matching DDS files
            try:
                new_names = set()
                for img in bpy.data.images:
                    try:
                        if getattr(img, 'yakuza_data', None) and img.yakuza_data.inited:
                            n = img.yakuza_data.yk_name
                            if isinstance(n, str):
                                nl = n.lower()
                                if nl not in existing_names:
                                    new_names.add(nl)
                    except Exception:
                        pass

                tmp_dir = None
                if new_names:
                    try:
                        tmp_dir = _extract_matching_dds_to_temp(par, new_names, item.internal_path, context=context)
                    except Exception:
                        tmp_dir = None

                # If we extracted matching DDS files, relink the proxy images
                if tmp_dir:
                    try:
                        _relink_images_from_folder(tmp_dir, overwrite_linked=True, case_sensitive=False)
                    except Exception:
                        pass
            finally:
                if 'tmp_dir' in locals() and tmp_dir:
                    try:
                        # Preserve the extracted directory instead of deleting it
                        _register_preserved_tmp(tmp_dir)
                        print(f"[yk_par_lib_tool] Preserving extracted folder after import/place: {tmp_dir}")
                    except Exception:
                        pass
        except Exception as e:
            self.report({'ERROR'}, f"Failed to import and place GMD: {e}")
            return {'CANCELLED'}

        if not coll:
            self.report({'ERROR'}, "Import returned no collection to place")
            return {'CANCELLED'}

        # Move objects in the collection to the 3D cursor location
        cursor_loc = context.scene.cursor.location.copy()
        for obj in coll.objects:
            try:
                obj.location = cursor_loc
                # link to the active collection if not already
                if context.collection and obj.name not in context.collection.objects:
                    context.collection.objects.link(obj)
            except Exception:
                pass

        self.report({'INFO'}, f"Placed {item.name} at cursor")
        return {'FINISHED'}


class YKPAR_PT_browser(Panel):
    bl_label = "Yakuza PAR Browser"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Yakuza PAR'

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        # Show configured PAR files from addon preferences (quick access)
        prefs_addon = context.preferences.addons.get('yk_par_lib_tool')
        if prefs_addon:
            prefs = prefs_addon.preferences
            box = layout.box()
            row = box.row()
            row.label(text='Configured .par files')
            row = box.row()
            # Use our custom UIList so the visible label is the short name and the
            # full filesystem path is available as a tooltip on hover.
            row.template_list("UI_UL_yk_par_files", "yk_par_files", prefs, "par_files", prefs, "par_index", rows=3)

            col = row.column(align=True)
            col.operator('yk_par_lib_tool.add_par_file', icon='ADD', text='')
            op = col.operator('yk_par_lib_tool.remove_par_file', icon='REMOVE', text='')
            op.index = prefs.par_index if hasattr(prefs, 'par_index') else 0

            # Show the selected configured PAR's full path as a short label beneath the list
            try:
                sel_idx = int(getattr(prefs, 'par_index', 0) or 0)
                if getattr(prefs, 'par_files', None) and 0 <= sel_idx < len(prefs.par_files):
                    sel_path = getattr(prefs.par_files[sel_idx], 'path', '') or ''
                    if sel_path:
                        box.label(text=f"{sel_path}")
            except Exception:
                # best-effort display; ignore failures
                pass
        else:
            layout.label(text='No add-on preferences found (enable yk_par_lib_tool)')

        # Search/filter row: text field + refresh + clear actions
        row = layout.row(align=True)
        row.prop(scene, 'yk_par_filter', text='', icon='VIEWZOOM')
        # More prominent: unpack loaded PARs with a labeled button
        row.operator('yk_par_lib_tool.refresh_par_listing', text='Unpack Loaded PARs', icon='PACKAGE')
        row.operator('yk_par_lib_tool.clear_par_filter', text='', icon='X')
        row.operator('yk_par_lib_tool.relink_preserved_tmp', text='Relink Textures')
        # Allow the user to register the bundled GMT importer at runtime if needed
        try:
            row.operator('yk_par_lib_tool.register_gmt_importer', text='Register GMT Importer', icon='IMPORT')
        except Exception:
            pass
        row.operator('yk_par_lib_tool.extract_textures_from_configured_par', text='Extract Textures from Configured PAR')
        row.operator('yk_par_lib_tool.confirm_import_selected', text='Import Selected')
        #row.operator('yk_par_lib_tool.import_visible_all', text='Import All')

        # Render hierarchical tree from PAR_CACHE (or render filtered flat node list when a filter is active)
        box = layout.box()
        # current filter (used to decide whether to show the filtered node list)
        filter_text = (getattr(scene, 'yk_par_filter', '') or '').strip().lower()

        if filter_text and getattr(scene, 'yk_par_nodes', None) and len(scene.yk_par_nodes) > 0:
            # Render the flat, filtered list produced by the Refresh operator
            for idx, item in enumerate(scene.yk_par_nodes):
                # When a filter is active, show only files (no folder entries)
                try:
                    if getattr(item, 'is_folder', False):
                        continue
                except Exception:
                    # If the prop check fails, conservatively skip the item
                    continue

                row = box.row(align=True)
                toggle_factor = min(0.25, 0.04 + (getattr(item, 'depth', 0) or 0) * 0.04)
                split = row.split(factor=toggle_factor)
                left = split.column()
                mid = split.split(factor=0.8)
                mid_col = mid.column()
                right = mid.split(factor=0.8)

                # File rendering only: add checkbox in left column for multi-select
                try:
                    left.prop(item, 'selected', text='')
                except Exception:
                    pass

                try:
                    mid_col.label(text=item.name, icon='FILE')
                    # Put all per-file import actions into a single small column so icons are consistent
                    # Create a small horizontal row for the three import buttons. Ensure the row is
                    # created before any operator calls so Blender draws real buttons (not deferred labels).
                    try:
                        act_row = right.row(align=True)
                        # Increase horizontal scale so the three import icons are wider and easier to click
                        act_row.scale_x = 2.5
                        act_row.scale_y = 0.95
                    except Exception:
                        act_row = right.row(align=True)

                    try:
                        lname = (getattr(item, 'name', '') or '').lower()
                        if lname.endswith('.par'):
                            # Show only the labeled button for nested PAR files
                            try:
                                unpack_mid = act_row.operator('yk_par_lib_tool.unpack_and_link_par', text='', icon='IMAGE_DATA')
                                unpack_mid.par_path = item.par_path
                                unpack_mid.internal_path = item.internal_path
                            except Exception:
                                pass
                        else:
                            lname2 = (getattr(item, 'name', '') or '').lower()
                            if lname2.endswith('.gmt'):
                                # For GMT files, only show the GMT import button
                                try:
                                    gmt_op = act_row.operator('yk_par_lib_tool.import_gmt_from_par', text='', icon='ACTION')
                                    gmt_op.par_path = item.par_path
                                    gmt_op.internal_path = item.internal_path
                                except Exception:
                                    pass
                            else:
                                # For non-GMT files, show all import buttons
                                imp = act_row.operator('yk_par_lib_tool.import_par_file', text='', icon='IMPORT')
                                imp.par_path = item.par_path
                                imp.internal_path = item.internal_path

                                arm = act_row.operator('yk_par_lib_tool.import_par_armature', text='', icon='ARMATURE_DATA')
                                arm.par_path = item.par_path
                                arm.internal_path = item.internal_path

                                anim = act_row.operator('yk_par_lib_tool.import_par_animation', text='', icon='ACTION')
                                anim.par_path = item.par_path
                                anim.internal_path = item.internal_path
                    except Exception:
                        mid_col.label(text=str(getattr(item, 'name', '<item>')))
                except Exception:
                    mid_col.label(text=str(getattr(item, 'name', '<item>')))

            # Bulk import button for selected items when a filter is active
            row = layout.row(align=True)
            row.operator('yk_par_lib_tool.import_selected_multiple', icon='IMPORT')
            return

        if not PAR_CACHE:
            box.label(text='No PARs loaded. Click Refresh to scan configured .par files.')
            # If the user has configured PARs and we haven't auto-refreshed yet this session,
            # trigger a single refresh so the panel shows contents without manual refresh.
            try:
                prefs_addon = context.preferences.addons.get('yk_par_lib_tool')
                if prefs_addon and getattr(context.scene, 'yk_par_auto_refreshed', False) is False and getattr(prefs_addon.preferences, 'par_files', None):
                    bpy.ops.yk_par_lib_tool.refresh_par_listing()
                    context.scene.yk_par_auto_refreshed = True
            except Exception:
                pass
        else:
            def draw_folder(box, par_path, folder, prefix='', depth=0):
                if not folder:
                    indent = "    " * depth
                    box.label(text=f"{indent}(empty PAR root) {bpy.path.display_name_from_filepath(par_path)}")
                    return
                scene = context.scene
                key = _make_node_key(par_path, prefix)
                # Use a 3-column split: toggle | label | actions
                row = box.row(align=True)
                # left column width depends on depth so the toggle arrow is indented for nested folders
                toggle_factor = min(0.25, 0.04 + depth * 0.04)
                split = row.split(factor=toggle_factor)
                left = split.column()
                mid = split.split(factor=0.8)
                mid_col = mid.column()
                right = mid.split(factor=0.8)

                is_exp = _is_expanded(scene, key)
                icon = 'TRIA_DOWN' if is_exp else 'TRIA_RIGHT'
                op = left.operator('yk_par_lib_tool.toggle_par_node', text='', icon=icon, emboss=False)
                op.par_path = par_path
                op.internal_path = prefix

                # Compose folder label and create an inner indent column so icon + label shift for nested depth
                folder_label = (prefix.split('/')[-2] + '/') if prefix else bpy.path.display_name_from_filepath(par_path)
                # Place the folder label with icon in the middle column so the icon sits next to the text
                mid_col.label(text=folder_label, icon='FILE_FOLDER')
                # Add a small operator button to extract textures from this specific PAR
                try:
                    btn = right.operator('yk_par_lib_tool.extract_textures_from_par', text='', icon='IMAGE_DATA')
                    btn.par_path = par_path
                except Exception:
                    pass
                if is_exp:
                    # list files at this level
                    for f in getattr(folder, 'files', []) or []:
                        # Include .gmd, .par, and .gmt files so embedded PARs, GMDs, and GMTs are visible
                        lname = (getattr(f, 'name', '') or '').lower()
                        if not (lname.endswith('.gmd') or lname.endswith('.par') or lname.endswith('.gmt')):
                            continue
                        rowf = box.row(align=True)
                        toggle_factor_f = min(0.25, 0.04 + depth * 0.04)
                        splitf = rowf.split(factor=toggle_factor_f)
                        leftf = splitf.column()
                        midf = splitf.split(factor=0.8)
                        midf_col = midf.column()
                        rightf = midf.split(factor=0.8)

                        # spacer/toggle column leftf — leave empty for files
                        # Place the file label and icon in the mid column (no inner split)
                        midf_col.label(text=f.name, icon='FILE')
                        # Ensure the action row is created before any operator calls so Blender shows
                        # proper clickable operator buttons (not labels). Create the action row first.
                        try:
                            act_rowf = rightf.row(align=True)
                            # Match the wider scale in folder listing for consistency
                            act_rowf.scale_x = 2.0
                            act_rowf.scale_y = 0.95
                        except Exception:
                            act_rowf = rightf.row(align=True)

                        # If this file is a .par, provide a single Unpack & Link button
                        try:
                            if lname.endswith('.par'):
                                try:
                                    labeled = act_rowf.operator('yk_par_lib_tool.unpack_and_link_par', text='', icon='IMAGE_DATA')
                                    labeled.par_path = par_path
                                    labeled.internal_path = (prefix + f.name).lstrip('/')
                                except Exception:
                                    pass
                            else:
                                if lname.endswith('.gmt'):
                                    # For GMT files, only show the GMT import button
                                    try:
                                        gmtf = act_rowf.operator('yk_par_lib_tool.import_gmt_from_par', text='', icon='ACTION')
                                        gmtf.par_path = par_path
                                        gmtf.internal_path = (prefix + f.name).lstrip('/')
                                    except Exception:
                                        pass
                                else:
                                    # For non-GMT files, show all import buttons
                                    imp = act_rowf.operator('yk_par_lib_tool.import_par_file', text='', icon='IMPORT')
                                    imp.par_path = par_path
                                    imp.internal_path = (prefix + f.name).lstrip('/')

                                    armf = act_rowf.operator('yk_par_lib_tool.import_par_armature', text='', icon='ARMATURE_DATA')
                                    armf.par_path = par_path
                                    armf.internal_path = (prefix + f.name).lstrip('/')

                                    animf = act_rowf.operator('yk_par_lib_tool.import_par_animation', text='', icon='ACTION')
                                    animf.par_path = par_path
                                    animf.internal_path = (prefix + f.name).lstrip('/')
                        except Exception:
                            pass
                    # recurse into subfolders
                    for sub in getattr(folder, 'folders', []) or []:
                        draw_folder(box, par_path, sub, prefix + sub.name + '/', depth + 1)

            for p, root in PAR_CACHE.items():
                draw_folder(box, p, root, '', 0)

class YKPAR_OT_extract_textures_from_configured_par(Operator):
    """Extract DDS textures from the currently selected configured PAR in preferences and relink them."""
    bl_idname = 'yk_par_lib_tool.extract_textures_from_configured_par'
    #bl_label = 'Extract Textures from Configured PAR'

    # Optional UI to extract only textures matching a comma-separated list of basenames
    use_filter: BoolProperty(name="Filter by names",
                             description="If True, only extract DDS whose basenames are listed in Names CSV",
                             default=False)
    names_csv: StringProperty(name="Names CSV",
                              description="Comma-separated list of basenames to extract (without extensions).",
                              default="")

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(self, 'use_filter')
        if self.use_filter:
            layout.prop(self, 'names_csv')

    def execute(self, context):
        # Get configured prefs and active par index
        prefs_addon = context.preferences.addons.get('yk_par_lib_tool')
        if not prefs_addon or not getattr(prefs_addon, 'preferences', None):
            self.report({'ERROR'}, 'yk_par_lib_tool preferences not found')
            return {'CANCELLED'}
        prefs = prefs_addon.preferences
        idx = getattr(prefs, 'par_index', 0)
        par_list = getattr(prefs, 'par_files', None)
        if not par_list or len(par_list) == 0:
            self.report({'ERROR'}, 'No configured PAR files found in preferences')
            return {'CANCELLED'}
        try:
            par_entry = par_list[idx]
            par_path = getattr(par_entry, 'path', None)
        except Exception:
            par_path = None
        if not par_path or not os.path.exists(par_path):
            self.report({'ERROR'}, f'Configured PAR path is invalid: {par_path}')
            return {'CANCELLED'}

        try:
            par = read_par(par_path)
        except Exception as e:
            self.report({'ERROR'}, f'Failed to read PAR: {e}')
            return {'CANCELLED'}

        # Determine extraction dir using prefs (deterministic subdir)
        try:
            pref_path = getattr(prefs, 'dds_extract_path', '') or ''
            if not pref_path:
                self.report({'ERROR'}, 'DDS extract path not configured in add-on preferences')
                return {'CANCELLED'}
        except Exception as e:
            self.report({'ERROR'}, f'Failed to prepare extraction folder: {e}')
            return {'CANCELLED'}

        # If user requested filtering by names, use the matching extractor helper
        if getattr(self, 'use_filter', False) and getattr(self, 'names_csv', '').strip():
            # parse CSV into a set of lowercased basenames
            names = {n.strip().lower() for n in self.names_csv.split(',') if n.strip()}
            if not names:
                self.report({'ERROR'}, 'Names CSV provided but no valid names parsed')
                return {'CANCELLED'}
            try:
                tmpdir = _extract_matching_dds_to_temp(par, names, None, context=context)
            except Exception as e:
                self.report({'ERROR'}, f'Filtered extraction failed: {e}')
                return {'CANCELLED'}
            if not tmpdir:
                self.report({'WARNING'}, 'No matching DDS files found in configured PAR')
                return {'CANCELLED'}
            try:
                relinked = _relink_images_from_folder(tmpdir, overwrite_linked=True, case_sensitive=False)
                self.report({'INFO'}, f'Extracted textures to {tmpdir}, relinked {relinked} images')
            except Exception as e:
                self.report({'WARNING'}, f'Extracted textures to {tmpdir} but relink failed: {e}')
            try:
                _register_preserved_tmp(tmpdir)
            except Exception:
                pass
            return {'FINISHED'}

        # Default behavior: full extraction (unchanged)
        try:
            tmpdir = _deterministic_extraction_subdir(pref_path, None, None, prefix='ykpar_dds_par_')
        except Exception as e:
            self.report({'ERROR'}, f'Failed to prepare extraction folder: {e}')
            return {'CANCELLED'}

        extracted = False

        def walk_and_extract_for_par(folder, prefix):
            nonlocal extracted
            for f in getattr(folder, 'files', []) or []:
                internal = (prefix + (f.name or '')).lstrip('/')
                if not internal.lower().endswith('.dds'):
                    continue
                try:
                    data = _get_file_data(f)
                    if isinstance(data, (bytearray, memoryview)):
                        data = bytes(data)
                    out_path = _safe_write_bytes(tmpdir, f.name, data)
                    extracted = True
                except Exception:
                    pass
            for sub in getattr(folder, 'folders', []) or []:
                walk_and_extract_for_par(sub, prefix + (sub.name or '') + '/')

        try:
            root = par.folders[0] if getattr(par, 'folders', None) and len(par.folders) else None
            if not root:
                self.report({'ERROR'}, 'PAR has no root')
                return {'CANCELLED'}
            walk_and_extract_for_par(root, '')
        except Exception as e:
            self.report({'ERROR'}, f'Failed during extraction: {e}')
            return {'CANCELLED'}

        if not extracted:
            self.report({'WARNING'}, 'No DDS files found in configured PAR')
            return {'CANCELLED'}

        try:
            relinked = _relink_images_from_folder(tmpdir, overwrite_linked=True, case_sensitive=False)
            self.report({'INFO'}, f'Extracted textures to {tmpdir}, relinked {relinked} images')
        except Exception as e:
            self.report({'WARNING'}, f'Extracted textures to {tmpdir} but relink failed: {e}')

        try:
            _register_preserved_tmp(tmpdir)
        except Exception:
            pass

        return {'FINISHED'}



class YKPAR_OT_extract_textures_from_par(Operator):
    """Extract DDS textures from the specified PAR path and relink them."""
    bl_idname = 'yk_par_lib_tool.extract_textures_from_par'
    bl_label = 'Extract Textures from PAR'

    par_path: StringProperty()

    # Optional UI to extract only textures matching a comma-separated list of basenames
    use_filter: BoolProperty(name="Filter by names",
                             description="If True, only extract DDS whose basenames are listed in Names CSV",
                             default=False)
    names_csv: StringProperty(name="Names CSV",
                              description="Comma-separated list of basenames to extract (without extensions).",
                              default="")

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(self, 'use_filter')
        if self.use_filter:
            layout.prop(self, 'names_csv')

    def execute(self, context):
        par_path = getattr(self, 'par_path', None)
        if not par_path or not os.path.exists(par_path):
            self.report({'ERROR'}, f'Invalid PAR path: {par_path}')
            return {'CANCELLED'}

        try:
            par = read_par(par_path)
        except Exception as e:
            self.report({'ERROR'}, f'Failed to read PAR: {e}')
            return {'CANCELLED'}

        # Use configured extraction preference
        prefs_addon = context.preferences.addons.get('yk_par_lib_tool')
        pref_path = ''
        if prefs_addon and getattr(prefs_addon, 'preferences', None):
            pref_path = getattr(prefs_addon.preferences, 'dds_extract_path', '') or ''
        if not pref_path:
            self.report({'ERROR'}, 'DDS extract path not configured in add-on preferences')
            return {'CANCELLED'}

        # If user requested filtering by names, use the matching extractor helper
        if getattr(self, 'use_filter', False) and getattr(self, 'names_csv', '').strip():
            names = {n.strip().lower() for n in self.names_csv.split(',') if n.strip()}
            if not names:
                self.report({'ERROR'}, 'Names CSV provided but no valid names parsed')
                return {'CANCELLED'}
            try:
                tmpdir = _extract_matching_dds_to_temp(par, names, None, context=context)
            except Exception as e:
                self.report({'ERROR'}, f'Filtered extraction failed: {e}')
                return {'CANCELLED'}
            if not tmpdir:
                self.report({'WARNING'}, 'No matching DDS files found in PAR')
                return {'CANCELLED'}
            try:
                relinked = _relink_images_from_folder(tmpdir, overwrite_linked=True, case_sensitive=False)
                self.report({'INFO'}, f'Extracted textures to {tmpdir}, relinked {relinked} images')
            except Exception as e:
                self.report({'WARNING'}, f'Extracted textures to {tmpdir} but relink failed: {e}')
            try:
                _register_preserved_tmp(tmpdir)
            except Exception:
                pass
            return {'FINISHED'}

        tmpdir = _deterministic_extraction_subdir(pref_path, None, None, prefix='ykpar_dds_par_')

        extracted = False

        def walk_and_extract_for_par(folder, prefix):
            nonlocal extracted
            for f in getattr(folder, 'files', []) or []:
                if not (getattr(f, 'name', '') or '').lower().endswith('.dds'):
                    continue
                try:
                    data = _get_file_data(f)
                    if isinstance(data, (bytearray, memoryview)):
                        data = bytes(data)
                    _safe_write_bytes(tmpdir, f.name, data)
                    extracted = True
                except Exception:
                    pass
            for sub in getattr(folder, 'folders', []) or []:
                walk_and_extract_for_par(sub, prefix + (sub.name or '') + '/')

        try:
            root = par.folders[0] if getattr(par, 'folders', None) and len(par.folders) else None
            if not root:
                self.report({'ERROR'}, 'PAR has no root')
                return {'CANCELLED'}
            walk_and_extract_for_par(root, '')
        except Exception as e:
            self.report({'ERROR'}, f'Failed extracting from PAR: {e}')
            return {'CANCELLED'}

        if not extracted:
            self.report({'WARNING'}, 'No DDS files found in the specified PAR')
            return {'CANCELLED'}

        try:
            relinked = _relink_images_from_folder(tmpdir, overwrite_linked=True, case_sensitive=False)
            self.report({'INFO'}, f'Extracted textures to {tmpdir}, relinked {relinked} images')
        except Exception as e:
            self.report({'WARNING'}, f'Extracted textures to {tmpdir} but relink failed: {e}')

        try:
            _register_preserved_tmp(tmpdir)
        except Exception:
            pass

        return {'FINISHED'}


class YKPAR_OT_unpack_and_link_par(Operator):
    """Unpack a .par (or an embedded .par entry inside another PAR) and relink images in one operation."""
    bl_idname = 'yk_par_lib_tool.unpack_and_link_par'
    bl_label = 'Unpack and Link PAR'

    par_path: StringProperty()
    internal_path: StringProperty()

    def execute(self, context):
        # par_path may point to a filesystem .par (configured) OR be the parent PAR file
        # in which case internal_path points to an embedded .par entry. Support both.
        par_path = getattr(self, 'par_path', None)
        internal = getattr(self, 'internal_path', '') or ''

        if not par_path or not os.path.exists(par_path):
            self.report({'ERROR'}, f'Invalid PAR path: {par_path}')
            return {'CANCELLED'}

        try:
            top_par = read_par(par_path)
        except Exception as e:
            self.report({'ERROR'}, f'Failed to read PAR: {e}')
            return {'CANCELLED'}

        # If internal_path points to a .par file inside top_par, extract that embedded par bytes
        if internal and internal.lower().endswith('.par'):
            # find file object inside top_par
            target = None

            def _norm_path(p: str) -> str:
                if not p:
                    return ''
                return p.lstrip('/').replace('\\', '/').lower()

            internal_norm = _norm_path(internal)

            def find_file(folder, prefix):
                nonlocal target
                for f in getattr(folder, 'files', []) or []:
                    candidate = _norm_path(prefix + (f.name or ''))
                    if candidate == internal_norm:
                        target = f
                        return True
                for sub in getattr(folder, 'folders', []) or []:
                    if find_file(sub, prefix + (sub.name or '') + '/'):
                        return True
                return False

            root = top_par.folders[0] if getattr(top_par, 'folders', None) and len(top_par.folders) else None
            if not root:
                self.report({'ERROR'}, 'Parent PAR has no root')
                return {'CANCELLED'}
            find_file(root, '')
            if not target:
                self.report({'ERROR'}, f'Embedded PAR file not found inside {par_path}: {internal}')
                return {'CANCELLED'}

            # Decompress embedded PAR bytes if necessary
            try:
                data = _get_file_data(target)
                if isinstance(data, (bytearray, memoryview)):
                    data = bytes(data)
            except Exception as e:
                self.report({'ERROR'}, f'Failed to read embedded PAR bytes: {e}')
                return {'CANCELLED'}

            # write embedded PAR to a deterministic file in the configured extract path (or temp)
            try:
                pref_dir = ''
                prefs_addon = context.preferences.addons.get('yk_par_lib_tool')
                if prefs_addon and getattr(prefs_addon, 'preferences', None):
                    pref_dir = getattr(prefs_addon.preferences, 'dds_extract_path', '') or ''
                if pref_dir:
                    out_dir = _deterministic_extraction_subdir(pref_dir, None, None, prefix='ykpar_embedded_pars_')
                else:
                    # no configured preference: abort per preference-first behavior
                    self.report({'ERROR'}, 'DDS extract path not configured in preferences; cannot write embedded PAR')
                    return {'CANCELLED'}
                sig = hashlib.sha1(data).hexdigest()[:12]
                out_path = os.path.join(out_dir, f'embedded_{sig}.par')
                if not os.path.exists(out_path):
                    with open(out_path, 'wb') as ef:
                        ef.write(data)
            except Exception as e:
                self.report({'ERROR'}, f'Failed to write embedded PAR to disk: {e}')
                return {'CANCELLED'}

            # Read the written embedded PAR and proceed to extract textures from it
            try:
                nested = read_par(out_path)
            except Exception as e:
                self.report({'ERROR'}, f'Failed to read written embedded PAR: {e}')
                return {'CANCELLED'}

            # Use deterministic extraction subdir for this embedded PAR
            try:
                pref_dir = getattr(prefs_addon.preferences, 'dds_extract_path', '') or '' if prefs_addon else ''
                if not pref_dir:
                    self.report({'ERROR'}, 'DDS extract path not configured; aborting')
                    return {'CANCELLED'}
                tmpdir = _deterministic_extraction_subdir(pref_dir, None, None, prefix='ykpar_dds_embedded_')
            except Exception:
                self.report({'ERROR'}, 'Failed to prepare extraction folder')
                return {'CANCELLED'}

            # Extract all DDS from nested PAR (including nested embedded PARs)
            try:
                found = _extract_dds_from_par_object(nested, tmpdir, names_set=None, gmd_internal_path=None, context=context)
                if not found:
                    # fallback: full walk extraction
                    rootn = nested.folders[0] if getattr(nested, 'folders', None) and len(nested.folders) else None
                    if rootn:
                        def walk_extract(folder, prefix):
                            for f in getattr(folder, 'files', []) or []:
                                if (getattr(f, 'name', '') or '').lower().endswith('.dds'):
                                    try:
                                        data = _get_file_data(f)
                                        if isinstance(data, (bytearray, memoryview)):
                                            data = bytes(data)
                                        _safe_write_bytes(tmpdir, f.name, data)
                                    except Exception:
                                        pass
                            for s in getattr(folder, 'folders', []) or []:
                                walk_extract(s, prefix + (s.name or '') + '/')
                        walk_extract(rootn, '')
            except Exception as e:
                self.report({'ERROR'}, f'Failed extracting DDS from embedded PAR: {e}')
                return {'CANCELLED'}

            # Attempt relink
            try:
                relinked = _relink_images_from_folder(tmpdir, overwrite_linked=True, case_sensitive=False)
                self.report({'INFO'}, f'Extracted textures to {tmpdir}, relinked {relinked} images')
            except Exception as e:
                self.report({'WARNING'}, f'Extracted textures to {tmpdir} but relink failed: {e}')
            try:
                _register_preserved_tmp(tmpdir)
            except Exception:
                pass

            return {'FINISHED'}

        # else: internal is not an embedded par entry; treat par_path as the target to extract from
        try:
            # Use preference-first extraction and deterministic subdir
            prefs_addon = context.preferences.addons.get('yk_par_lib_tool')
            pref_dir = getattr(prefs_addon.preferences, 'dds_extract_path', '') or '' if prefs_addon else ''
            if not pref_dir:
                self.report({'ERROR'}, 'DDS extract path not configured in add-on preferences')
                return {'CANCELLED'}
            tmpdir = _deterministic_extraction_subdir(pref_dir, None, None, prefix='ykpar_dds_par_')
        except Exception:
            self.report({'ERROR'}, 'Failed to prepare extraction folder')
            return {'CANCELLED'}

        # Extract from top_par
        extracted = False
        try:
            root = top_par.folders[0] if getattr(top_par, 'folders', None) and len(top_par.folders) else None
            if root:
                def walk_and_extract(folder, prefix):
                    nonlocal extracted
                    for f in getattr(folder, 'files', []) or []:
                        if not (getattr(f, 'name', '') or '').lower().endswith('.dds'):
                            continue
                        try:
                            data = _get_file_data(f)
                            if isinstance(data, (bytearray, memoryview)):
                                data = bytes(data)
                            _safe_write_bytes(tmpdir, f.name, data)
                            extracted = True
                        except Exception:
                            pass
                    for sub in getattr(folder, 'folders', []) or []:
                        walk_and_extract(sub, prefix + (sub.name or '') + '/')
                walk_and_extract(root, '')
        except Exception as e:
            self.report({'ERROR'}, f'Failed during extraction: {e}')
            return {'CANCELLED'}

        if not extracted:
            self.report({'WARNING'}, 'No DDS files found in PAR')
            return {'CANCELLED'}

        try:
            relinked = _relink_images_from_folder(tmpdir, overwrite_linked=True, case_sensitive=False)
            self.report({'INFO'}, f'Extracted textures to {tmpdir}, relinked {relinked} images')
        except Exception as e:
            self.report({'WARNING'}, f'Extracted textures to {tmpdir} but relink failed: {e}')
        try:
            _register_preserved_tmp(tmpdir)
        except Exception:
            pass

        return {'FINISHED'}


class YKPAR_OT_import_visible_all(BaseImportGMD, Operator):
    """Import all visible .gmd items currently listed in the scene node list."""
    bl_idname = 'yk_par_lib_tool.import_visible_all'
    bl_label = 'Import All Visible GMDs'

    def execute(self, context):
        global IMPORT_IN_PROGRESS
        if IMPORT_IN_PROGRESS:
            self.report({'ERROR'}, 'Another import is in progress')
            return {'CANCELLED'}

        scene = context.scene
        nodes = getattr(scene, 'yk_par_nodes', None)
        if not nodes:
            self.report({'ERROR'}, 'No nodes available')
            return {'CANCELLED'}

        # collect all non-folder items (visible list)
        to_import = [n for n in nodes if not getattr(n, 'is_folder', False)]
        if not to_import:
            self.report({'ERROR'}, 'No files visible to import')
            return {'CANCELLED'}

        IMPORT_IN_PROGRESS = True
        imported_count = 0
        try:
            for item in to_import:
                try:
                    par_path = item.par_path
                    internal = item.internal_path
                    try:
                        par = read_par(par_path)
                    except Exception as e:
                        self.report({'WARNING'}, f'Failed to read PAR {par_path}: {e}')
                        continue

                    # find file object
                    target = None

                    def _norm_path(p: str) -> str:
                        if not p:
                            return ''
                        return p.lstrip('/').replace('\\', '/').lower()

                    internal_norm = _norm_path(internal)

                    def find_file(folder, prefix):
                        nonlocal target
                        for f in getattr(folder, 'files', []) or []:
                            candidate = _norm_path(prefix + (f.name or ''))
                            if candidate == internal_norm:
                                target = f
                                return True
                        for sub in getattr(folder, 'folders', []) or []:
                            if find_file(sub, prefix + (sub.name or '') + '/'):
                                return True
                        return False

                    root = par.folders[0] if getattr(par, 'folders', None) and len(par.folders) else None
                    if not root:
                        self.report({'WARNING'}, f'PAR has no root: {par_path}')
                        continue
                    find_file(root, '')
                    if not target:
                        self.report({'WARNING'}, f'File not found in PAR: {internal} in {par_path}')
                        continue

                    try:
                        # Decompress if needed
                        if getattr(target, 'compression', 0):
                            try:
                                target_data = decompress_file(target)
                            except Exception as de:
                                self.report({'WARNING'}, f'Failed to decompress file: {de}')
                                continue
                        else:
                            target_data = _get_file_data(target)

                        file_bytes = bytes(target_data) if isinstance(target_data, (bytearray, memoryview)) else target_data
                        try:
                            coll = import_gmd_bytes_to_collection(context, target.name, file_bytes, prefer_skinned=True, strict=True)
                            if coll:
                                imported_count += 1
                        except Exception as e:
                            self.report({'WARNING'}, f'Failed to import {target.name}: {e}')
                            continue
                    except Exception as e:
                        self.report({'WARNING'}, f'Import failed for {internal}: {e}')
                        continue
                except Exception:
                    pass
        finally:
            IMPORT_IN_PROGRESS = False

        self.report({'INFO'}, f'Imported {imported_count} of {len(to_import)} visible files')
        return {'FINISHED'}


class YKPAR_OT_ui_debug_test(Operator):
    """Simple debug operator to confirm UI operator wiring"""
    bl_idname = 'yk_par_lib_tool.ui_debug_test'
    bl_label = 'UI Debug Test'

    def execute(self, context):
        try:
            print('[yk_par_lib_tool] UI debug operator invoked')
        except Exception:
            pass
        try:
            self.report({'INFO'}, 'UI debug operator invoked')
        except Exception:
            pass
        return {'FINISHED'}


class YKPAR_OT_import_gmt_from_par(Operator):
    """Extract a .gmt from a PAR (or embedded) and import it via the GMT importer"""
    bl_idname = 'yk_par_lib_tool.import_gmt_from_par'
    bl_label = 'Import GMT from PAR'

    par_path: StringProperty()
    internal_path: StringProperty()

    def execute(self, context):
        par_path = getattr(self, 'par_path', None)
        internal = getattr(self, 'internal_path', '') or ''

        if not par_path or not os.path.exists(par_path):
            self.report({'ERROR'}, f'Invalid PAR path: {par_path}')
            return {'CANCELLED'}

        try:
            par = read_par(par_path)
        except Exception as e:
            self.report({'ERROR'}, f'Failed to read PAR: {e}')
            return {'CANCELLED'}

        # find target file object
        target = None

        def _norm_path(p: str) -> str:
            if not p:
                return ''
            return p.lstrip('/').replace('\\', '/').lower()

        internal_norm = _norm_path(internal)

        def find_file(folder, prefix):
            nonlocal target
            for f in getattr(folder, 'files', []) or []:
                candidate = _norm_path(prefix + (f.name or ''))
                if candidate == internal_norm:
                    target = f
                    return True
            for sub in getattr(folder, 'folders', []) or []:
                if find_file(sub, prefix + (sub.name or '') + '/'):
                    return True
            return False

        root = par.folders[0] if getattr(par, 'folders', None) and len(par.folders) else None
        if not root:
            self.report({'ERROR'}, 'PAR has no root')
            return {'CANCELLED'}
        find_file(root, '')
        if not target:
            self.report({'ERROR'}, f'GMT file not found inside PAR: {internal}')
            return {'CANCELLED'}

        # get raw bytes
        try:
            data = _get_file_data(target)
            if isinstance(data, (bytearray, memoryview)):
                data = bytes(data)
        except Exception as e:
            self.report({'ERROR'}, f'Failed to read GMT bytes: {e}')
            return {'CANCELLED'}

        # Write to configured extract dir (preference-first). If no pref, abort.
        try:
            prefs_addon = context.preferences.addons.get('yk_par_lib_tool')
            pref_dir = getattr(prefs_addon.preferences, 'dds_extract_path', '') or '' if prefs_addon else ''
            if not pref_dir:
                self.report({'ERROR'}, 'DDS extract path not configured; set it in add-on preferences')
                return {'CANCELLED'}
            out_dir = _deterministic_extraction_subdir(pref_dir, None, None, prefix='ykpar_gmt_')
            sig = hashlib.sha1(data).hexdigest()[:12]
            out_fn = f'gmt_{sig}.gmt'
            out_path = os.path.join(out_dir, out_fn)
            if not os.path.exists(out_path):
                with open(out_path, 'wb') as wf:
                    wf.write(data)
        except Exception as e:
            self.report({'ERROR'}, f'Failed to write GMT to disk: {e}')
            return {'CANCELLED'}

        # Ensure an armature is active (the GMT importer expects an armature to attach actions to).
        try:
            ao = context.active_object
            if not (ao and getattr(ao, 'type', '') == 'ARMATURE' and getattr(ao.data, 'bones', None)):
                # Try to find any armature in the file and make it active
                found = None
                for ob in bpy.data.objects:
                    try:
                        if getattr(ob, 'type', '') == 'ARMATURE' and getattr(ob.data, 'bones', None):
                            found = ob
                            break
                    except Exception:
                        continue
                if found:
                    try:
                        context.view_layer.objects.active = found
                        ao = found
                    except Exception:
                        pass
            if not ao or getattr(ao, 'type', '') != 'ARMATURE':
                # No armature available; the importer may still work for camera/face-target imports,
                # but warn the user so they can select an appropriate armature.
                self.report({'WARNING'}, 'No armature active. Select a target armature before importing if needed.')

            # Try to invoke the GMT import operator with the standard Blender file dialog
            # This gives the user full control via the operator UI (with all import options)
            try:
                # First, try to call the operator directly
                # INVOKE_DEFAULT will open the file picker popup
                bpy.ops.import_scene.gmt('INVOKE_DEFAULT', filepath=out_path)
                self.report({'INFO'}, f'Opened GMT import dialog for: {os.path.basename(out_path)}')
                return {'FINISHED'}
            except AttributeError:
                # Operator not registered - offer to register it
                self.report({'ERROR'}, 
                    'GMT import operator not registered. Click "Register GMT Importer" button in the PAR Browser panel, then try again.')
                return {'CANCELLED'}
            except Exception as e:
                # Some other error - log it
                import traceback
                print(f"[yk_par_lib_tool] GMT operator invoke failed: {e}")
                traceback.print_exc()
                self.report({'ERROR'}, f'Failed to invoke GMT import: {e}')
                return {'CANCELLED'}
        except Exception as e:
            self.report({'ERROR'}, f'Failed preparing importer context: {e}')
            return {'CANCELLED'}

        try:
            _register_preserved_tmp(out_dir)
        except Exception:
            pass

        return {'FINISHED'}


class YKPAR_OT_import_par_armature(Operator):
    """Import a GMD from a PAR but keep only the armature (remove meshes/objects)."""
    bl_idname = 'yk_par_lib_tool.import_par_armature'
    bl_label = 'Import GMD Armature Only'
    par_path: StringProperty()
    internal_path: StringProperty()

    def execute(self, context):
        global IMPORT_IN_PROGRESS
        if IMPORT_IN_PROGRESS:
            self.report({'ERROR'}, 'Another import is in progress')
            return {'CANCELLED'}

        IMPORT_IN_PROGRESS = True
        try:
            par_path = self.par_path
            internal = self.internal_path
            try:
                par = read_par(par_path)
            except Exception as e:
                self.report({'ERROR'}, f"Failed to read PAR: {e}")
                return {'CANCELLED'}

            target = None

            def _norm_path(p: str) -> str:
                if not p:
                    return ''
                return p.lstrip('/').replace('\\', '/').lower()

            internal_norm = _norm_path(internal)

            def find_file(folder, prefix):
                nonlocal target
                for f in getattr(folder, 'files', []) or []:
                    candidate = _norm_path(prefix + (f.name or ''))
                    if candidate == internal_norm:
                        target = f
                        return True
                for sub in getattr(folder, 'folders', []) or []:
                    if find_file(sub, prefix + (sub.name or '') + '/'):
                        return True
                return False

            root = par.folders[0] if getattr(par, 'folders', None) and len(par.folders) else None
            if not root:
                self.report({'ERROR'}, 'PAR has no root')
                return {'CANCELLED'}
            find_file(root, '')
            if not target:
                self.report({'ERROR'}, 'File not found in PAR')
                return {'CANCELLED'}

            try:
                if getattr(target, 'compression', 0):
                    try:
                        target_data = decompress_file(target)
                    except Exception as de:
                        self.report({'ERROR'}, f"Failed to decompress file: {de}")
                        return {'CANCELLED'}
                else:
                    target_data = _get_file_data(target)

                file_bytes = bytes(target_data) if isinstance(target_data, (bytearray, memoryview)) else target_data
                from .gmd_importers import import_gmd_bytes_to_collection

                coll = import_gmd_bytes_to_collection(context, target.name, file_bytes, prefer_skinned=True, strict=True)
                if not coll:
                    self.report({'ERROR'}, 'Import returned no collection')
                    return {'CANCELLED'}

                # Remove non-armature objects from the imported collection
                try:
                    objs = list(coll.objects)
                    for obj in objs:
                        try:
                            if obj.type != 'ARMATURE':
                                # unlink from any collections
                                for c in list(obj.users_collection):
                                    try:
                                        c.objects.unlink(obj)
                                    except Exception:
                                        pass
                                # remove object datablock if possible
                                try:
                                    bpy.data.objects.remove(obj, do_unlink=True)
                                except Exception:
                                    pass
                        except Exception:
                            pass
                except Exception:
                    pass

                self.report({'INFO'}, f'Imported armature from {target.name}')
                return {'FINISHED'}
            except Exception as e:
                self.report({'ERROR'}, f'Import failed: {e}')
                return {'CANCELLED'}
        finally:
            IMPORT_IN_PROGRESS = False


class YKPAR_OT_import_par_animation(Operator):
    """Import a GMD from a PAR using the animation import flow (keeps animation-related setup)."""
    bl_idname = 'yk_par_lib_tool.import_par_animation'
    bl_label = 'Import GMD for Animation'
    par_path: StringProperty()
    internal_path: StringProperty()

    def execute(self, context):
        global IMPORT_IN_PROGRESS
        if IMPORT_IN_PROGRESS:
            self.report({'ERROR'}, 'Another import is in progress')
            return {'CANCELLED'}

        IMPORT_IN_PROGRESS = True
        try:
            par_path = self.par_path
            internal = self.internal_path
            try:
                par = read_par(par_path)
            except Exception as e:
                self.report({'ERROR'}, f"Failed to read PAR: {e}")
                return {'CANCELLED'}

            target = None

            def _norm_path(p: str) -> str:
                if not p:
                    return ''
                return p.lstrip('/').replace('\\', '/').lower()

            internal_norm = _norm_path(internal)

            def find_file(folder, prefix):
                nonlocal target
                for f in getattr(folder, 'files', []) or []:
                    candidate = _norm_path(prefix + (f.name or ''))
                    if candidate == internal_norm:
                        target = f
                        return True
                for sub in getattr(folder, 'folders', []) or []:
                    if find_file(sub, prefix + (sub.name or '') + '/'):
                        return True
                return False

            root = par.folders[0] if getattr(par, 'folders', None) and len(par.folders) else None
            if not root:
                self.report({'ERROR'}, 'PAR has no root')
                return {'CANCELLED'}
            find_file(root, '')
            if not target:
                self.report({'ERROR'}, 'File not found in PAR')
                return {'CANCELLED'}

            try:
                if getattr(target, 'compression', 0):
                    try:
                        target_data = decompress_file(target)
                    except Exception as de:
                        self.report({'ERROR'}, f"Failed to decompress file: {de}")
                        return {'CANCELLED'}
                else:
                    target_data = _get_file_data(target)

                file_bytes = bytes(target_data) if isinstance(target_data, (bytearray, memoryview)) else target_data

                # Use the animation scene creator path directly without instantiating Operator classes
                try:
                    # Build an error reporter similar to the importer helper
                    from ..error_reporter import BlenderErrorReporter
                    from ...gmdlib.errors.error_reporter import StrictErrorReporter, LenientErrorReporter
                    # conservative strict reporter
                    base_err = StrictErrorReporter(set(["ALL"]))
                    error = BlenderErrorReporter(lambda *a, **k: None, base_err)

                    # parse structures
                    from ...gmdlib.io import read_gmd_structures, read_abstract_scene_from_filedata_object
                    gmd_version, gmd_header, gmd_contents = read_gmd_structures(file_bytes, error)

                    # determine import mode (prefer skinned animation)
                    fim = FileImportMode.SKINNED
                    gmd_scene = read_abstract_scene_from_filedata_object(gmd_version, fim, VertexImportMode.IMPORT_VERTICES, gmd_contents, error)

                    # create a minimal GMDSceneCreatorConfig (mirror BaseImportGMD defaults)
                    from .scene_creators.base import GMDSceneCreatorConfig, MaterialNamingType
                    from ..common import GMDGame
                    from ...gmdlib.structure.version import GMDVersion

                    engine_from_version = {
                        GMDVersion.Kenzan: GMDGame.Engine_MagicalV,
                        GMDVersion.Kiwami1: GMDGame.Engine_Kiwami,
                        GMDVersion.Dragon: GMDGame.Engine_Dragon,
                    }
                    engine_enum = engine_from_version.get(gmd_version.major_version, GMDGame.Engine_Kiwami)

                    gmd_config = GMDSceneCreatorConfig(
                        game=engine_enum,
                        import_materials=True,
                        material_naming_convention=MaterialNamingType.Collection_DiffuseTexture,
                        fuse_vertices=True,
                        custom_split_normals=True,
                    )

                    # Create animation scene creator and build scene
                    from .scene_creators.animation import GMDAnimationSceneCreator
                    creator = GMDAnimationSceneCreator(target.name, gmd_scene, gmd_config, error)
                    creator.validate_scene()
                    coll = creator.make_collection(context)

                    # Create armature and mapping, then build objects using that armature so
                    # parenting and armature modifiers are set up correctly.
                    try:
                        armature_obj, node_map = creator.make_bone_hierarchy(context, coll)
                    except Exception:
                        armature_obj = None
                        node_map = None

                    # Build objects and ensure they receive the armature modifier & parenting
                    creator.make_objects(context, coll, armature_obj, node_map)

                    # After import, attempt to extract matching DDS files from the PAR and relink
                    try:
                        # Collect texture basenames referenced by the imported collection
                        names_set = set()
                        if coll:
                            for obj in getattr(coll, 'objects', []) or []:
                                for slot in getattr(obj, 'material_slots', []) or []:
                                    ma = getattr(slot, 'material', None)
                                    if not ma or not hasattr(ma, 'node_tree') or not ma.node_tree:
                                        continue
                                    for node in ma.node_tree.nodes:
                                        if node.type == 'TEX_IMAGE' and node.image:
                                            tex_name = os.path.splitext(node.image.name)[0].lower()
                                            names_set.add(tex_name)

                        tmp_dir = None
                        if names_set:
                            try:
                                tmp_dir = _extract_matching_dds_to_temp(par, names_set, internal, context=context)
                            except Exception:
                                tmp_dir = None

                        if tmp_dir:
                            try:
                                relinked = _relink_images_from_folder(tmp_dir, overwrite_linked=True, case_sensitive=False)
                            except Exception:
                                relinked = 0

                            try:
                                self.report({'INFO'}, f"Extracted textures and relinked {relinked} images")
                            except Exception:
                                pass

                        # Attempt to write the original .gmd into the extraction folder for modding
                        try:
                            if tmp_dir:
                                written = _write_gmd_to_extraction(file_bytes, target.name, tmp_dir, internal, names_set, context=context)
                                if written:
                                    try:
                                        print(f"[yk_par_lib_tool] Wrote GMD to extraction folder: {written}")
                                    except Exception:
                                        pass
                        except Exception:
                            pass
                    except Exception:
                        pass

                    self.report({'INFO'}, f'Imported animation-style GMD: {target.name}')
                    return {'FINISHED'}
                except Exception as e:
                    # Surface full traceback to the system console for debugging
                    import traceback
                    try:
                        tb = traceback.format_exc()
                        print(f"[yk_par_lib_tool] Animation import failed: {e}\n{tb}")
                    except Exception:
                        try:
                            print(f"[yk_par_lib_tool] Animation import failed: {e}")
                        except Exception:
                            pass
                    try:
                        # Provide a user-facing error as well
                        self.report({'ERROR'}, f'Animation import failed: {e}')
                    except Exception:
                        pass
                    return {'CANCELLED'}
            except Exception as e:
                self.report({'ERROR'}, f'Import failed: {e}')
                return {'CANCELLED'}
        finally:
            IMPORT_IN_PROGRESS = False


class YKPAR_OT_register_gmt_importer(Operator):
    """Register the bundled yakuza-gmt-blender importer during runtime."""
    bl_idname = 'yk_par_lib_tool.register_gmt_importer'
    bl_label = 'Register GMT Importer'

    def execute(self, context):
        try:
            import sys
            import os
            
            # Get the yakuza-gmt-blender package path
            pkg_folder = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'yakuza-gmt-blender'))
            
            # Add to sys.path if not already there
            if pkg_folder not in sys.path:
                sys.path.insert(0, pkg_folder)
            
            # Import the top-level __init__.py which has the register() function
            import importlib
            
            # If already imported, reload it
            if 'yakuza_gmt_blender' in sys.modules:
                yakuza_gmt = sys.modules['yakuza_gmt_blender']
                importlib.reload(yakuza_gmt)
            else:
                # First time import - need to set up the module name properly
                parent_folder = os.path.dirname(pkg_folder)
                if parent_folder not in sys.path:
                    sys.path.insert(0, parent_folder)
                
                # Try importing as a package
                try:
                    import importlib.util
                    spec = importlib.util.spec_from_file_location(
                        'yakuza_gmt_blender',
                        os.path.join(pkg_folder, '__init__.py'),
                        submodule_search_locations=[pkg_folder]
                    )
                    yakuza_gmt = importlib.util.module_from_spec(spec)
                    sys.modules['yakuza_gmt_blender'] = yakuza_gmt
                    spec.loader.exec_module(yakuza_gmt)
                except Exception as e:
                    print(f"[yk_par_lib_tool] Failed to import yakuza_gmt_blender package: {e}")
                    raise
            
            # Now register the add-on
            if hasattr(yakuza_gmt, 'register'):
                yakuza_gmt.register()
                print("[yk_par_lib_tool] Successfully registered bundled GMT importer")
                self.report({'INFO'}, 'Registered bundled GMT importer - import_scene.gmt operator is now available')
                return {'FINISHED'}
            else:
                self.report({'ERROR'}, 'GMT module has no register function')
                return {'CANCELLED'}
                
        except Exception as e:
            import traceback
            print(f"[yk_par_lib_tool] ERROR registering GMT importer: {e}")
            traceback.print_exc()
            self.report({'ERROR'}, f'Failed to register GMT importer: {e}')
        return {'CANCELLED'}
