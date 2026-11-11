#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""听打练习 · 素材预处理（支持多篇文章）

把文章切成适合听打的短段，并为每段生成语音文件。
可一次处理多篇：界面顶部会多出一个文章下拉框，随时切换。

用法:
    python prep.py                       处理根目录 *.txt 和 docs/*.txt 里的全部文章
    python prep.py docs/散文.txt          只处理指定文章（可给多个）
    python prep.py --list                查看当前文章库
    python prep.py --force               全部重新切分并重新合成语音
    python prep.py --no-audio            只切段，界面用系统语音朗读
    python prep.py 文章.txt --voice zh-CN-YunxiNeural --rate -20%

添加新文章:
    把 txt 放进本目录或 docs/ ，再运行一次 python prep.py 即可（已生成的不会重复合成）。

生成物:
    library.js     界面读取的文章数据（含每篇的分段文本）
    library.json   prep.py 自用的状态文件，用于增量判断，不用手改
    audio/<id>/     每篇文章的语音目录

依赖(可选):
    pip install edge-tts
未安装时脚本仍可运行，只是不生成音频，浏览器会退回系统语音。
"""

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
DOCS_DIR = HERE / "docs"
AUDIO_DIR = HERE / "audio"
LIBRARY_JS = HERE / "library.js"
STATE_JSON = HERE / "library.json"
LEGACY_SEGS = HERE / "segs.js"

SENT_END = "。！？；!?;…"


# ---------------------------------------------------------------- 文本切分

def split_sentences(paragraph: str) -> list[str]:
    """按句末标点切句，标点跟随前一句。"""
    out, buf = [], ""
    for ch in paragraph:
        buf += ch
        if ch in SENT_END:
            out.append(buf)
            buf = ""
    if buf.strip():
        out.append(buf)
    return [s.strip() for s in out if s.strip()]


CLAUSE_BREAK = "，、；：,;:"


def split_clauses(sentence: str, hi: int) -> list[str]:
    """单句超过上限时，在逗号/顿号处再切。"""
    if len(sentence) <= hi:
        return [sentence]
    parts, buf = [], ""
    for ch in sentence:
        buf += ch
        if ch in CLAUSE_BREAK and len(buf) >= max(6, hi // 2):
            parts.append(buf)
            buf = ""
    if buf:
        parts.append(buf)
    return parts


def hard_wrap(s: str, hi: int) -> list[str]:
    """兜底：没有标点的长句均分成若干份，每份不超过 hi，优先断在标点后。"""
    if len(s) <= hi:
        return [s]
    n = -(-len(s) // hi)          # 需要几份
    per = -(-len(s) // n)         # 每份目标长度，均分避免长尾碎片
    out, buf = [], ""
    for ch in s:
        buf += ch
        if len(buf) >= per:
            k = max(buf.rfind(p) for p in CLAUSE_BREAK)
            if k >= int(per * 0.6):
                out.append(buf[: k + 1])
                buf = buf[k + 1:]
            else:
                out.append(buf)
                buf = ""
    if buf:
        if out and len(buf) < per * 0.5 and len(out[-1] + buf) <= hi:
            out[-1] += buf
        else:
            out.append(buf)
    return out


def glue(buf: str, s: str) -> str:
    """拼接句子；英文单词之间补回空格。"""
    if buf and s and buf[-1].isascii() and buf[-1].isalnum() \
            and s[0].isascii() and s[0].isalnum():
        return buf + " " + s
    return buf + s


def pack(units: list[str], lo: int, hi: int) -> list[str]:
    """贪心装段，严格不超过 hi。"""
    out: list[str] = []
    buf = ""
    for s in units:
        if not s:
            continue
        if not buf:
            buf = s
        elif len(glue(buf, s)) <= hi:
            buf = glue(buf, s)
        else:
            out.append(buf)
            buf = s
    if buf.strip():
        if out and len(buf) < lo and len(glue(out[-1], buf)) <= hi:
            out[-1] = glue(out[-1], buf)  # 尾巴太短，并回上一段
        else:
            out.append(buf)
    return out


def split_article(text: str, lo: int, hi: int) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\u3000", " ")
    # 先把超长段落按句号切开，再按段打包；段落之间不跨段合并
    paragraphs = [p.strip() for p in re.split(r"\n+", text)]
    segments: list[str] = []
    for p in paragraphs:
        if not p:
            continue
        if len(p) <= hi:
            segments.append(p)
            continue
        units = []
        for s in split_sentences(p):
            for c in split_clauses(s, hi):
                units.extend(hard_wrap(c, hi))
        segments.extend(pack(units, lo, hi))

    # 纯标点的碎片（如单独一个"。"）没有朗读内容，并回上一段或丢弃
    cleaned: list[str] = []
    for s in segments:
        if not s.strip():
            continue
        if re.search(r"[0-9A-Za-z\u4e00-\u9fff]", s):
            cleaned.append(s)
        elif cleaned and len(cleaned[-1]) + len(s) <= hi:
            cleaned[-1] += s
    return cleaned


def extract_title(raw: str) -> tuple[str, str]:
    """首行是 markdown 标题（# xxx）时取作标题，并从正文中剔除。"""
    lines = raw.split("\n")
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        m = re.match(r"^#{1,6}\s+(.+)$", line.strip())
        if m:
            return m.group(1).strip(), "\n".join(lines[i + 1:])
        break
    return "", raw


def slugify(name: str) -> str:
    s = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "-", name).strip("-")
    return s or "doc"


# ---------------------------------------------------------------- 语音合成

def normalize_rate(rate: str) -> str:
    rate = rate.strip()
    if not rate.endswith("%"):
        rate += "%"
    if not rate.startswith(("+", "-")):
        rate = "+" + rate
    return rate


def audio_complete(audio_dir: Path, count: int) -> bool:
    if not audio_dir.is_dir():
        return False
    for i in range(count):
        f = audio_dir / f"{i:03d}.mp3"
        if not f.exists() or f.stat().st_size <= 800:
            return False
    return True


def clear_audio(audio_dir: Path) -> None:
    if audio_dir.is_dir():
        for f in audio_dir.glob("*.mp3"):
            f.unlink()


def migrate_flat_audio(doc_id: str, count: int) -> bool:
    """老版本把音频平铺在 audio/ 下，首次转多文档时挪进 audio/<id>/，省一次重合成。"""
    if not AUDIO_DIR.is_dir():
        return False
    target = AUDIO_DIR / doc_id
    flat = sorted(p for p in AUDIO_DIR.glob("*.mp3") if p.stem.isdigit())
    if target.exists() or len(flat) < count:
        return False
    target.mkdir(parents=True, exist_ok=True)
    for f in flat:
        f.rename(target / f.name)
    return True


async def synthesize(segs: list[str], audio_dir: Path, voice: str, rate: str, force: bool) -> int:
    import edge_tts

    audio_dir.mkdir(parents=True, exist_ok=True)
    total = len(segs)
    done = 0
    failed = 0
    for i, text in enumerate(segs):
        target = audio_dir / f"{i:03d}.mp3"
        if not force and target.exists() and target.stat().st_size > 800:
            done += 1
            continue
        ok = False
        for attempt in range(1, 4):
            try:
                await edge_tts.Communicate(text, voice, rate=rate).save(str(target))
                ok = True
                break
            except Exception as exc:                       # noqa: BLE001
                if attempt == 3:
                    print(f"\n  [{i:03d}] 失败: {exc}")
                else:
                    await asyncio.sleep(1.5 * attempt)
        if ok:
            done += 1
        else:
            failed += 1
        print(f"\r  合成 {done}/{total}" + (f"（失败 {failed}）" if failed else ""), end="", flush=True)
    print()
    return failed


# ---------------------------------------------------------------- 状态与输出

def load_state() -> dict:
    if STATE_JSON.exists():
        try:
            data = json.loads(STATE_JSON.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("docs"), list):
                return data
        except Exception:                                   # noqa: BLE001
            print("旧的 library.json 读不出来，按全新开始处理")
    return {"version": 1, "docs": []}


def save_state(docs: list[dict]) -> None:
    STATE_JSON.write_text(
        json.dumps({"version": 1, "docs": docs}, ensure_ascii=False, indent=1),
        encoding="utf-8")


def write_library_js(docs: list[dict]) -> None:
    body = ",\n".join(json.dumps(d, ensure_ascii=False) for d in docs)
    content = (
        "// 由 prep.py 自动生成，请勿手改；重新运行 prep.py 会覆盖本文件\n"
        "const DOCS = [\n" + body + "\n];\n"
    )
    content = content.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    LIBRARY_JS.write_text(content, encoding="utf-8")


def find_sources(cli_files: list[str]) -> list[Path]:
    if cli_files:
        out = []
        for f in cli_files:
            p = Path(f)
            if not p.is_absolute():
                p = HERE / p
            out.append(p)
        return out
    files = sorted(HERE.glob("*.txt"))
    if DOCS_DIR.is_dir():
        files += sorted(DOCS_DIR.rglob("*.txt"))
    return files


def print_library(docs: list[dict]) -> None:
    if not docs:
        print("文章库是空的，先运行 python prep.py 添加文章。")
        return
    print(f"共 {len(docs)} 篇：")
    for i, d in enumerate(docs, 1):
        mark = "语音" if d.get("audio") else "TTS "
        print(f"  {i}. {d['title']}　[{mark}] {d['count']} 段　id={d['id']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="听打练习素材预处理（支持多篇文章）")
    ap.add_argument("files", nargs="*", help="要处理的文章路径，省略则处理全部 txt")
    ap.add_argument("--voice", default="zh-CN-XiaoxiaoNeural", help="edge-tts 音色")
    ap.add_argument("--rate", default="-15%", help="语速，如 -15%% 或 +10%%")
    ap.add_argument("--min", type=int, default=8, help="每段最少字数，默认 8")
    ap.add_argument("--max", type=int, default=15, help="每段最多字数（硬上限），默认 15")
    ap.add_argument("--no-audio", action="store_true", help="只切段，不生成音频")
    ap.add_argument("--force", action="store_true", help="强制重新切分并重新合成")
    ap.add_argument("--list", action="store_true", help="只列出当前文章库")
    args = ap.parse_args()

    if args.list:
        print_library(load_state()["docs"])
        return 0

    sources = find_sources(args.files)
    missing = [p for p in sources if not p.exists()]
    for p in missing:
        print(f"找不到文件: {p}")
    sources = [p for p in sources if p.exists()]
    if not sources:
        print("没有找到任何 txt 文章。把文章放到本目录或 docs/ 下再运行。")
        return 1

    rate = normalize_rate(args.rate)
    use_audio = not args.no_audio
    if use_audio:
        try:
            import edge_tts  # noqa: F401
        except ImportError:
            print("未检测到 edge-tts，本次只切分文本，界面将使用系统语音朗读。")
            print("如需真人音色:  pip install edge-tts")
            use_audio = False

    # 先给源文件分配稳定 id（由文件名决定，重命名标题不影响 id 与音频目录）
    assigned: list[tuple[str, Path]] = []
    used: set[str] = set()
    for src in sources:
        base = slugify(src.stem)
        doc_id, n = base, 2
        while doc_id in used:
            doc_id = f"{base}-{n}"
            n += 1
        used.add(doc_id)
        assigned.append((doc_id, src))

    old_docs = {d["id"]: d for d in load_state()["docs"] if "id" in d}
    new_docs: list[dict] = []

    for doc_id, src in assigned:
        raw = src.read_text(encoding="utf-8-sig")
        title, body = extract_title(raw)
        title = title or src.stem
        segs = split_article(body, args.min, args.max)
        if not segs:
            print(f"《{src.stem}》没有可用内容，跳过")
            continue

        old = old_docs.get(doc_id)
        same_text = bool(old) and old.get("segs") == segs
        same_param = bool(old) and old.get("voice") == args.voice and old.get("rate") == rate
        audio_dir = AUDIO_DIR / doc_id

        doc = {
            "id": doc_id,
            "title": title,
            "count": len(segs),
            "voice": args.voice,
            "rate": rate,
            "audio": False,
            "segs": segs,
        }

        lengths = [len(s) for s in segs]
        head = f"《{title}》 {len(segs)} 段（{min(lengths)}–{max(lengths)} 字）"

        if not use_audio:
            print(head + "：只切段")
            new_docs.append(doc)
            continue

        if same_text and same_param and not args.force and audio_complete(audio_dir, len(segs)):
            print(head + "：未变化，跳过合成")
            doc["audio"] = True
            new_docs.append(doc)
            continue

        # 段落文本变了 → 旧音频会错位，必须清空重来
        if not same_text or args.force:
            clear_audio(audio_dir)
        # 首次从单文档升级：把平铺的旧音频直接认领过来，省一次重合成
        if migrate_flat_audio(doc_id, len(segs)):
            print(f"　已把旧音频迁移到 audio/{doc_id}/")

        synth_force = args.force or (same_text and not same_param)
        print(head + f"：合成语音 [{args.voice} {rate}]")
        failed = asyncio.run(synthesize(segs, audio_dir, args.voice, rate, synth_force))
        doc["audio"] = failed == 0
        if failed:
            print(f"　{failed} 段合成失败，这些段在界面里会自动改用系统语音")
        # 段数减少时清掉多余音频
        for f in audio_dir.glob("*.mp3"):
            if f.stem.isdigit() and int(f.stem) >= len(segs):
                f.unlink()
        new_docs.append(doc)

    dropped = [d for d in old_docs.values() if d["id"] not in used]
    for d in dropped:
        print(f"已从文章库移除《{d.get('title', d['id'])}》（原文件不在了，音频目录保留）")

    save_state(new_docs)
    write_library_js(new_docs)
    if LEGACY_SEGS.exists():
        LEGACY_SEGS.unlink()
        print("旧的 segs.js 已删除，改由 library.js 提供数据")

    print()
    print_library(new_docs)
    print("\n完成。用浏览器打开 index.html，顶部下拉框可切换文章。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
