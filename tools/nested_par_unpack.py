"""
Standalone helper: recursively unpack a .par file and any embedded .par payloads to extract .dds files.

Usage (PowerShell):
    python tools\nested_par_unpack.py "C:\path\to\file.par" "C:\out\dir"

The script imports the add-on's `src.read` and `src.sllz` modules and writes any found .dds files into the output dir.
"""
import os
import sys
import hashlib

# allow importing from package root
ROOT = os.path.dirname(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    from src.read import read_par
    from src.sllz import decompress_file
except Exception as e:
    print(f"Failed to import project modules: {e}")
    raise


def ensure_dir(p):
    os.makedirs(p, exist_ok=True)


def safe_write_bytes(out_dir, name, data):
    p = os.path.join(out_dir, name)
    if os.path.exists(p):
        print(f"Skipping duplicate: {p}")
        return p
    with open(p, 'wb') as f:
        f.write(data)
    return p


def extract_from_par_obj(par_obj, out_dir, visited=None):
    """Recursively scan Par object, writing any .dds files to out_dir and
    extracting embedded .par payloads (written as embedded_<sha1>.par) and
    recursing into them.
    Returns count of extracted DDS files.
    """
    if visited is None:
        visited = set()
    extracted = 0

    def strip_lod(n):
        import re
        return re.sub(r"\[[^\]]+\]$", "", n)

    def process_folder(folder, prefix):
        nonlocal extracted
        for f in getattr(folder, 'files', []) or []:
            name = f.name or ''
            full = (prefix + name).lstrip('/')
            lower = full.lower()
            # embedded PAR candidate
            if not lower.endswith('.dds'):
                try:
                    data = decompress_file(f) if getattr(f, 'compression', 0) else f.data
                    if isinstance(data, (bytearray, memoryview)):
                        data = bytes(data)
                except Exception:
                    data = f.data if hasattr(f, 'data') else None
                if data and len(data) >= 4 and data[:4] == b'PARC' or name.lower().endswith('.par'):
                    sig = hashlib.sha1(data).hexdigest() if data else None
                    if sig and sig in visited:
                        continue
                    visited.add(sig)
                    try:
                        emb_name = f"embedded_{sig or hashlib.sha1(name.encode('utf-8')).hexdigest()}.par"
                        emb_path = os.path.join(out_dir, emb_name)
                        with open(emb_path, 'wb') as tf:
                            tf.write(data)
                        print(f"Wrote embedded par: {emb_path}")
                        nested = None
                        try:
                            nested = read_par(emb_path)
                        except Exception as e:
                            print(f"Failed to read embedded par {emb_path}: {e}")
                            nested = None
                        if nested:
                            rf = nested.folders[0] if getattr(nested, 'folders', None) and len(nested.folders) else None
                            if rf:
                                process_folder(rf, '')
                    except Exception as e:
                        print(f"Error handling embedded par candidate {name}: {e}")
                    continue

            # dds file
            if lower.endswith('.dds'):
                try:
                    data = decompress_file(f) if getattr(f, 'compression', 0) else f.data
                    if isinstance(data, (bytearray, memoryview)):
                        data = bytes(data)
                    safe_write_bytes(out_dir, os.path.basename(name), data)
                    extracted += 1
                    print(f"Extracted DDS: {full}")
                except Exception as e:
                    print(f"Failed to extract dds {name}: {e}")
        for sub in getattr(folder, 'folders', []) or []:
            process_folder(sub, prefix + (sub.name or '') + '/')

    root = par_obj.folders[0] if getattr(par_obj, 'folders', None) and len(par_obj.folders) else None
    if root:
        process_folder(root, '')
    return extracted


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print('Usage: python tools\\nested_par_unpack.py <path.to.par> <out_dir>')
        sys.exit(1)
    par_path = sys.argv[1]
    out_dir = sys.argv[2]
    if not os.path.exists(par_path):
        print(f'PAR not found: {par_path}')
        sys.exit(2)
    ensure_dir(out_dir)
    try:
        par = read_par(par_path)
    except Exception as e:
        print(f'Failed to read PAR: {e}')
        sys.exit(3)
    count = extract_from_par_obj(par, out_dir, visited=set())
    print(f'Done. Extracted {count} DDS files (plus embedded-par dumps) into {out_dir}')
