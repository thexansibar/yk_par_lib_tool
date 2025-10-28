# Include the bl_info at the top level always
bl_info = {
    "name": "Yakuza PAR Viewer/Importer",
    "author": "xansibar",
    "version": (0, 2, 5),
    "blender": (4, 0, 0),
    "location": "File > Import-Export",
    "description": ".Par Library Viewer/Importer",
    "warning": "",
    "doc_url": "",
    "category": "System",
}


def register():
    # Defer Blender-specific registration to blender.addon if available
    try:
        from .blender import addon as _addon
        _addon.register()
    except Exception:
        # Not running inside Blender or addon import failed; ignore.
        pass


def unregister():
    try:
        from .blender import addon as _addon
        _addon.unregister()
    except Exception:
        pass


if __name__ == "__main__":
    register()