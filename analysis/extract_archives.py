import concurrent.futures
import pathlib
import time
import py7zr
from audit_paths import DATA_ROOT

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEST = ROOT / 'analysis' / 'extracted'
DEST.mkdir(exist_ok=True)

def extract(path):
    start = time.time()
    with py7zr.SevenZipFile(path) as archive:
        for item in archive.list():
            target = (DEST / item.filename).resolve()
            if not target.is_relative_to(DEST.resolve()):
                raise ValueError(item.filename)
        archive.extractall(path=DEST)
    print(f'{path.name}: extracted and CRC checked in {time.time()-start:.1f}s', flush=True)

with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
    archives = sorted(DATA_ROOT.glob('*.7z'), reverse=True)
    if not archives:
        raise FileNotFoundError(f'No .7z archives in {DATA_ROOT}')
    list(pool.map(extract, archives))
