#!/usr/bin/env python3
"""Собирает Android-колесо Rust/PyO3-пакета для Chaquopy без maturin и без сборщика Chaquopy.

Схема: исходники с PyPI -> PYO3_CONFIG_FILE (описание целевого Python без интерпретатора) -> cargo build
с линкером из Android NDK и libpython из пакета Chaquopy -> сборка колеса вручную с платформенным тегом
android_21_<abi>, который понимает pip в SlavShell.
"""
import argparse, base64, hashlib, io, json, os, re, shutil, subprocess, sys, tarfile, tempfile, urllib.request, zipfile
import tomllib

ABIS = {
    "armeabi-v7a": dict(rust="armv7-linux-androideabi", clang="armv7a-linux-androideabi21-clang", tag="android_21_armeabi_v7a", ptr=32, readelf="ARM"),
    "arm64-v8a": dict(rust="aarch64-linux-android", clang="aarch64-linux-android21-clang", tag="android_21_arm64_v8a", ptr=64, readelf="AArch64"),
}
UA = {"User-Agent": "slavshell-wheels/1.0"}


def get(url, limit=400 * 1024 * 1024):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as f:
        return f.read(limit)


def run(cmd, **kw):
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def chaquopy_libpython(pyver, abi, work):
    base = "https://chaquo.com/maven-central/com/chaquo/python/target/"
    idx = get(base).decode()
    vers = sorted({m for m in re.findall(r'href="(%s\.\d+-\d+)/"' % re.escape(pyver), idx)},
                  key=lambda v: [int(x) for x in re.split(r"[.-]", v)])
    if not vers:
        sys.exit("На chaquo.com нет пакета target для Python %s" % pyver)
    ver = vers[-1]
    z = zipfile.ZipFile(io.BytesIO(get(f"{base}{ver}/target-{ver}-{abi}.zip")))
    names = [n for n in z.namelist() if n.endswith("libpython%s.so" % pyver)]
    if not names:
        sys.exit("В target-%s-%s.zip нет libpython%s.so" % (ver, abi, pyver))
    z.extract(names[0], work)
    print("libpython из Chaquopy target", ver)
    return os.path.dirname(os.path.join(work, names[0]))


def fetch_sdist(package, version, work):
    meta = json.loads(get(f"https://pypi.org/pypi/{package}/{version}/json"))
    sd = [u for u in meta["urls"] if u["packagetype"] == "sdist"][0]
    data = get(sd["url"])
    if hashlib.sha256(data).hexdigest() != sd["digests"]["sha256"]:
        sys.exit("контрольная сумма sdist не совпала")
    tarfile.open(fileobj=io.BytesIO(data), mode="r:gz").extractall(work)
    roots = [d for d in os.listdir(work) if os.path.isfile(os.path.join(work, d, "Cargo.toml"))]
    if len(roots) != 1:
        sys.exit("не удалось найти корень исходников с Cargo.toml")
    return os.path.join(work, roots[0])


def make_wheel(src, pysrc, top, module, soname, so, package, version, python, abi_tag, outdir):
    """Собирает колесо вручную: python-пакет из исходников + нативная библиотека + dist-info с RECORD."""
    name = re.sub(r"[-_.]+", "_", package)
    dist = "%s-%s.dist-info" % (name, version)
    tag = "cp%s-cp%s-%s" % (python.replace(".", ""), python.replace(".", ""), abi_tag)
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, "%s-%s-%s.whl" % (name, version, tag))
    rec = []

    def add(zf, arc, payload):
        zf.writestr(arc, payload)
        h = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode()
        rec.append("%s,sha256=%s,%d" % (arc, h, len(payload)))

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        pkgroot = os.path.join(src, pysrc, top)
        for dp, dn, fns in os.walk(pkgroot):
            dn[:] = [d for d in dn if d != "__pycache__"]
            for f in fns:
                if f.endswith((".pyc", ".so")):
                    continue
                full = os.path.join(dp, f)
                add(zf, os.path.relpath(full, os.path.join(src, pysrc)).replace(os.sep, "/"), open(full, "rb").read())
        add(zf, "%s/%s.so" % (module.rsplit(".", 1)[0].replace(".", "/"), soname), open(so, "rb").read())
        add(zf, dist + "/METADATA", open(os.path.join(src, "PKG-INFO"), "rb").read())
        add(zf, dist + "/WHEEL", ("Wheel-Version: 1.0\nGenerator: slavshell-wheels\nRoot-Is-Purelib: false\nTag: %s\n" % tag).encode())
        lic = os.path.join(src, "LICENSE")
        if os.path.isfile(lic):
            add(zf, dist + "/licenses/LICENSE", open(lic, "rb").read())
        rec.append(dist + "/RECORD,,")
        zf.writestr(dist + "/RECORD", "\n".join(rec) + "\n")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--package", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--python", default="3.11")
    ap.add_argument("--abi", required=True, choices=ABIS)
    ap.add_argument("--out", default="dist")
    a = ap.parse_args()
    cfg = ABIS[a.abi]
    ndk = next((os.environ[k] for k in ("ANDROID_NDK_LATEST_HOME", "ANDROID_NDK_HOME", "ANDROID_NDK_ROOT") if os.environ.get(k)), None)
    if not ndk:
        sys.exit("не найден Android NDK (ANDROID_NDK_LATEST_HOME)")
    bindir = os.path.join(ndk, "toolchains", "llvm", "prebuilt", "linux-x86_64", "bin")
    work = tempfile.mkdtemp(prefix="wheel-")
    libdir = chaquopy_libpython(a.python, a.abi, work)
    src = fetch_sdist(a.package, a.version, work)

    cargo = tomllib.load(open(os.path.join(src, "Cargo.toml"), "rb"))
    pyproj = tomllib.load(open(os.path.join(src, "pyproject.toml"), "rb"))
    libname = cargo["lib"]["name"]
    mt = pyproj.get("tool", {}).get("maturin", {})
    module = mt.get("module-name", libname)               # например pydantic_core._pydantic_core
    pysrc = mt.get("python-source", ".")
    top = module.split(".")[0]
    soname = module.split(".")[-1]

    pyo3cfg = os.path.join(work, "pyo3.cfg")
    open(pyo3cfg, "w").write("\n".join([
        "implementation=CPython", "version=" + a.python, "shared=true", "abi3=false",
        "lib_name=python" + a.python, "lib_dir=" + libdir, "pointer_width=%d" % cfg["ptr"],
        "build_flags=", "suppress_build_script_link_lines=false", ""]))

    triple = cfg["rust"]
    up = triple.upper().replace("-", "_")
    env = dict(os.environ)
    env["PATH"] = bindir + os.pathsep + env["PATH"]
    env["PYO3_CONFIG_FILE"] = pyo3cfg
    env["CARGO_TARGET_%s_LINKER" % up] = os.path.join(bindir, cfg["clang"])
    env["CC_" + triple.replace("-", "_")] = os.path.join(bindir, cfg["clang"])
    env["AR_" + triple.replace("-", "_")] = os.path.join(bindir, "llvm-ar")
    # Без feature extension-module: расширение должно явно линковаться с libpython (на Android символы из
    # libpython другим библиотекам иначе не видны)
    run(["cargo", "build", "--release", "--lib", "--locked", "--target", triple], cwd=src, env=env)

    so = os.path.join(os.environ.get("CARGO_TARGET_DIR", os.path.join(src, "target")), triple, "release", "lib%s.so" % libname)
    if not os.path.isfile(so):
        sys.exit("cargo не создал " + so)
    run([os.path.join(bindir, "llvm-strip"), "--strip-unneeded", so])

    # проверки собранной библиотеки
    hdr = subprocess.check_output([os.path.join(bindir, "llvm-readelf"), "-h", so], text=True)
    dyn = subprocess.check_output([os.path.join(bindir, "llvm-readelf"), "-d", so], text=True)
    syms = subprocess.check_output([os.path.join(bindir, "llvm-nm"), "-D", "--defined-only", so], text=True)
    assert cfg["readelf"] in hdr, "не та архитектура:\n" + hdr
    assert "libpython%s.so" % a.python in dyn, "нет зависимости от libpython:\n" + dyn
    assert "PyInit_" + soname in syms, "нет символа PyInit_%s" % soname
    print("проверки ELF пройдены: архитектура, libpython, PyInit_%s" % soname)

    out = make_wheel(src, pysrc, top, module, soname, so, a.package, a.version, a.python, cfg["tag"], a.out)
    print("Готово:", out, os.path.getsize(out) // 1024, "КБ")


if __name__ == "__main__":
    main()
