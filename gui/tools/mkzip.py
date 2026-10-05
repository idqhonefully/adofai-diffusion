"""mkzip.py —— 把交付目录打成 zip（中文文件名不乱码、写完自动回读校验）。

用法：
    python mkzip.py <交付目录> <输出.zip> [--verify]

约定（与既有交付保持一致）：
  · 顶层带目录名（`workbench-v2/...`、`chartgen-0.5.0-drop-in/...`），解压即得一个文件夹
  · 文件名一律 UTF-8 + 置 zip 的 0x800 位 ⇒ 资源管理器 / 7-Zip / WinRAR 都正常显示中文
  · 条目按路径排序写，结果可复现
  · `--verify`：写完立刻读回来，逐条核对 CRC + 大小，不一致就非零退出
    （⚠️ 不要用 git-bash 的 `unzip` 校验 —— 中文名会乱码，那是解压工具的问题，不是包的问题）
"""

import os
import sys
import zipfile


def build(src_dir: str, out_zip: str) -> int:
    src_dir = os.path.abspath(src_dir)
    top = os.path.basename(src_dir.rstrip("\\/"))
    items = []
    for root, _dirs, files in os.walk(src_dir):
        for fn in files:
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, src_dir).replace("\\", "/")
            items.append((top + "/" + rel, full))
    items.sort(key=lambda t: t[0])

    if os.path.exists(out_zip):
        os.remove(out_zip)
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for arc, full in items:
            zi = zipfile.ZipInfo.from_file(full, arc)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.flag_bits |= 0x800            # UTF-8 文件名
            with open(full, "rb") as fh, zf.open(zi, "w") as dst:
                while True:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        break
                    dst.write(chunk)
    print("已写出 %s：%d 个条目，%.2f MB" % (out_zip, len(items), os.path.getsize(out_zip) / 1048576.0))
    return len(items)


def verify(src_dir: str, out_zip: str) -> bool:
    src_dir = os.path.abspath(src_dir)
    top = os.path.basename(src_dir.rstrip("\\/"))
    bad = 0
    with zipfile.ZipFile(out_zip) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        for arc in names:
            rel = arc.split("/", 1)[1] if "/" in arc else arc
            full = os.path.join(src_dir, rel.replace("/", os.sep))
            if not os.path.exists(full):
                print("★ 包里多出文件：" + arc)
                bad += 1
                continue
            with open(full, "rb") as fh:
                if zf.read(arc) != fh.read():
                    print("★ 内容不一致：" + arc)
                    bad += 1
        on_disk = []
        for root, _dirs, files in os.walk(src_dir):
            for fn in files:
                rel = os.path.relpath(os.path.join(root, fn), src_dir).replace("\\", "/")
                on_disk.append(top + "/" + rel)
        missing = sorted(set(on_disk) - set(names))
        for m in missing:
            print("★ 包里缺文件：" + m)
            bad += 1
    if bad:
        print("校验失败：%d 处不一致" % bad)
        return False
    print("校验通过：%d 个条目与源目录逐字节一致" % len(names))
    return True


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    do_verify = "--verify" in sys.argv
    if len(args) != 2:
        print(__doc__)
        return 2
    src, out = args
    n = build(src, out)
    if do_verify and not verify(src, out):
        return 1
    return 0 if n else 1


if __name__ == "__main__":
    sys.exit(main())
