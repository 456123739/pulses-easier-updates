import argparse, hashlib, resource, sys, time
from pathlib import Path
from app.core import container as ct

def rss(): return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024

def files(base): return [p for p in sorted(Path(base).rglob('*')) if p.is_file()]

ap = argparse.ArgumentParser()
ap.add_argument('--base', default='/tmp/mcc-bench/src2')
ap.add_argument('--block', type=int, default=64)
ap.add_argument('--level', type=int, default=22)
ap.add_argument('--window', type=int, default=0)
ap.add_argument('--jar', type=int, default=1)
ap.add_argument('--label', default='')
a = ap.parse_args()
base = Path(a.base); out = Path('/vol2/@appshare/Harness/easier-compression-lab/engine/out') / f'{a.label}.eapack'
out.parent.mkdir(parents=True, exist_ok=True)
t0 = time.time()
w = ct.ContainerWriter(out, level=a.level, block_size=a.block*1024*1024,
                       long_window=True, jar_transform=bool(a.jar))
for p in files(base):
    name = p.relative_to(base).as_posix()
    w.add_file(name, p, transform='mca' if p.suffix.lower() in ('.mca', '.mcr') else None)
st = w.close(); dt = time.time() - t0
size = out.stat().st_size
print(f'{a.label:22s} B={a.block:3d} L{a.level:<2d} jar{int(a.jar)} {size/1048576:8.2f} MB  '
      f'{dt:6.1f}s files={st.files} transformed={st.transformed} rss={rss():5.0f}MB')

t0 = time.time(); bad = 0
with ct.open_container(out) as r:
    for p in files(base):
        name = p.relative_to(base).as_posix()
        if hashlib.sha256(r.read(name)).digest() != hashlib.sha256(p.read_bytes()).digest():
            bad += 1
print(f'{a.label:22s} verify {time.time()-t0:5.1f}s bad={bad}')
