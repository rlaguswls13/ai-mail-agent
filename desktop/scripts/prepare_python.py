"""번들용 Python 준비 — python-embed zip 다운로드 + Flask 계열만 vendor.

`desktop/pybundle/` 을 만든다 (electron-builder 가 extraResources 로 앱에 넣음):
  pybundle/
    python.exe  python311.dll  python311.zip  ... (embed 배포판 그대로)
    python311._pth   ← Lib 를 sys.path 에 추가하도록 수정
    Lib/
      flask/ werkzeug/ jinja2/ click/ blinker/ markupsafe/ itsdangerous/

파이프라인 본체(fetch_mail.py 등)는 표준 라이브러리만 쓰므로 embed 배포판이면 충분하고,
admin_app.py(Flask)만 위 7개 패키지가 필요하다.

  python desktop/scripts/prepare_python.py [--force]
"""
import shutil
import site
import sys
import urllib.request
import zipfile
from pathlib import Path

PY_VERSION = "3.11.9"
EMBED_URL = f"https://www.python.org/ftp/python/{PY_VERSION}/python-{PY_VERSION}-embed-amd64.zip"

# admin_app.py 가 import 하는 것 + 그 전이 의존성 (전부 순수 Python).
VENDOR_PACKAGES = [
    "flask",
    "werkzeug",
    "jinja2",
    "click",
    "blinker",
    "markupsafe",
    "itsdangerous",
]

HERE = Path(__file__).resolve().parent
DESKTOP = HERE.parent
CACHE = DESKTOP / ".cache"
BUNDLE = DESKTOP / "pybundle"


def download_embed() -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    dest = CACHE / f"python-{PY_VERSION}-embed-amd64.zip"
    if dest.exists() and dest.stat().st_size > 1_000_000:
        print(f"[cache] {dest.name}")
        return dest
    print(f"[download] {EMBED_URL}")
    with urllib.request.urlopen(EMBED_URL, timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    print(f"[download] {dest.stat().st_size // 1024} KiB")
    return dest


def extract_embed(zip_path: Path) -> None:
    print(f"[extract] -> {BUNDLE}")
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(BUNDLE)
    # embed 배포판의 ._pth 는 stdlib zip + "." 만 있다. vendored 패키지를 위해 Lib 추가.
    pth = next(BUNDLE.glob("python*._pth"))
    lines = pth.read_text(encoding="utf-8").splitlines()
    if "Lib" not in lines:
        # "." 다음 줄에 Lib 삽입 (없으면 끝에).
        try:
            i = lines.index(".") + 1
        except ValueError:
            i = len(lines)
        lines.insert(i, "Lib")
        pth.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"[._pth] Lib 추가 -> {pth.name}")


def find_source_site_packages() -> Path:
    for p in site.getsitepackages() + [site.getusersitepackages()]:
        sp = Path(p)
        if (sp / "flask").is_dir():
            return sp
    raise SystemExit(
        "flask 를 찾을 수 없습니다. 이 스크립트를 flask 가 설치된 Python 으로 실행하세요\n"
        f"  (현재: {sys.executable})"
    )


def _copy_dist_info(src_site: Path, lib: Path, name: str) -> None:
    """<name>-<ver>.dist-info 도 복사한다 — Flask/Werkzeug 가 런타임에
    importlib.metadata.version() 을 호출하므로 메타데이터가 있어야 한다."""
    for di in src_site.glob(f"{name}-*.dist-info"):
        dest = lib / di.name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(di, dest, ignore=shutil.ignore_patterns("RECORD", "__pycache__"))
        print(f"[vendor] {di.name}")
        return


def vendor_packages(src_site: Path) -> None:
    lib = BUNDLE / "Lib"
    lib.mkdir(parents=True, exist_ok=True)
    for name in VENDOR_PACKAGES:
        src = src_site / name
        if not src.is_dir():
            # 단일 모듈(.py)일 수도 있음
            src_py = src_site / f"{name}.py"
            if src_py.is_file():
                shutil.copy2(src_py, lib / src_py.name)
                print(f"[vendor] {name}.py")
                _copy_dist_info(src_site, lib, name)
                continue
            raise SystemExit(f"패키지를 찾을 수 없음: {name} ({src_site})")
        dest = lib / name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests", "test"))
        print(f"[vendor] {name}/")
        _copy_dist_info(src_site, lib, name)


def main() -> None:
    force = "--force" in sys.argv
    if BUNDLE.exists():
        if not force:
            print(f"[skip] {BUNDLE} 이미 존재 (--force 로 재생성)")
            return
        shutil.rmtree(BUNDLE)

    zip_path = download_embed()
    extract_embed(zip_path)
    vendor_packages(find_source_site_packages())

    # 검증: 번들 Python 으로 flask import
    exe = BUNDLE / "python.exe"
    import subprocess

    r = subprocess.run(
        [str(exe), "-c", "import flask, sqlite3, ssl; print('bundle OK', flask.__name__)"],
        capture_output=True,
        text=True,
    )
    print(r.stdout.strip() or r.stderr.strip())
    if r.returncode != 0:
        raise SystemExit(f"번들 Python 검증 실패:\n{r.stderr}")
    total = sum(f.stat().st_size for f in BUNDLE.rglob("*") if f.is_file())
    print(f"[done] {BUNDLE}  ({total // 1024 // 1024} MiB)")


if __name__ == "__main__":
    main()
