"""Offline Obsidian vault index, search and integrity checks."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from urllib.parse import unquote, urlsplit

VAULT_ROOT = Path(__file__).resolve().parents[1] / "vault"
INDEX = "00-导航/当前索引.md"
MANIFEST = "00-导航/打包完整性.json"
HOME = "00-首页.md"
REGISTER = "06-更新与证据/来源登记.json"
OLD_CHECK = "06-更新与证据/校验结果.json"
OPERATIONS = "07-账号运营记录"
WIKI = re.compile(r"!?\[\[([^\]\n]+)\]\]")
MARKDOWN = re.compile(r"!?\[[^\]\n]*\]\((<[^>\n]*>|(?:\\.|[^()\n]|\([^()\n]*\))+)\)")
FENCES = re.compile(r"(?ms)^[ \t]*([~\x60]{3,})[^\n]*\n.*?^[ \t]*\1[ \t]*(?:\n|$)")
INLINE = re.compile(r"(\x60+)[^\n]*?\1")
DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
RECORD = re.compile(r"运营记录|续运营|运营续做|主动开发记录|档案转化与行业互动|起步执行记录")


def relative_files(vault: Path) -> dict[str, Path]:
    if not vault.is_dir():
        raise ValueError(f"找不到知识库目录 {vault}")
    return {
        p.relative_to(vault).as_posix(): p
        for p in sorted(vault.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


def get_json(files: dict[str, Path], name: str, default):
    if name not in files:
        return default
    return json.loads(files[name].read_text(encoding="utf-8-sig"))


def markdown_notes(files: dict[str, Path]) -> dict[str, str]:
    return {
        rel: path.read_text(encoding="utf-8-sig")
        for rel, path in files.items()
        if rel.lower().endswith(".md") and rel != INDEX
        and not any(part.startswith(".") for part in PurePosixPath(rel).parts)
    }


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def valid_date(text: str | None) -> str | None:
    match = DATE.match(text or "")
    if match:
        try:
            return date.fromisoformat(match[1]).isoformat()
        except ValueError:
            pass
    return None


def operation_records(notes: dict[str, str]) -> list[str]:
    # A research draft or a future editorial date is not an operating record.
    return sorted(
        (
            path for path in notes
            if path.startswith(OPERATIONS + "/")
            and valid_date(PurePosixPath(path).name)
            and RECORD.search(PurePosixPath(path).stem)
            and not any(word in PurePosixPath(path).stem for word in ("草稿", "计划"))
        ),
        reverse=True,
    )


def scope_for(relative_path: str) -> str:
    head = relative_path.split("/", 1)[0]
    if head == OPERATIONS:
        return "operations"
    if head.startswith(("04-", "05-", "06-")):
        return "sources"
    return "methods"


def summarize(files: dict[str, Path], notes: dict[str, str]) -> dict:
    source = get_json(files, REGISTER, {})
    validation = get_json(files, OLD_CHECK, {})
    logs = operation_records(notes)
    articles = [path for path in notes if path.startswith("04-文章学习卡/")]
    videos = [path for path in notes if path.startswith("05-视频学习卡/")]
    source_operation = source.get("accountExecution") or {}
    stored_counts = source.get("counts") or {}
    linked_dates = [
        valid_date(PurePosixPath(path).name)
        for path in logs
        if any(
            ref.split("|", 1)[0].split("#", 1)[0] == PurePosixPath(path).stem
            for ref in WIKI.findall(notes.get(HOME, ""))
        )
    ]
    newest = valid_date(PurePosixPath(logs[0]).name) if logs else None
    snapshots = {
        "original_home_latest_linked": max((d for d in linked_dates if d), default=None),
        "source_register_account_date": valid_date(source_operation.get("lastUpdated")),
        "historical_validation_date": valid_date(validation.get("verified")),
    }
    stale = {
        label: value
        for label, value in snapshots.items()
        if newest and value and value < newest
    }
    legacy_note_count = validation.get("markdownFiles")
    if isinstance(legacy_note_count, int) and legacy_note_count != len(notes):
        stale["historical_validation_markdown_count"] = legacy_note_count
    return {
        "markdown_notes_excluding_generated_index": len(notes),
        "article_cards": len(articles),
        "video_cards": len(videos),
        "supplemental_checks": len(source.get("supplementalChecks", [])),
        "recorded_transcripts": stored_counts.get("fullTranscripts"),
        "recorded_complete_video_views": stored_counts.get("fullVideosWatched"),
        "registry_article_entries": len(source.get("articles", [])),
        "registry_video_entries": len(source.get("videos", [])),
        "latest_operation_record_date": newest,
        "operation_records": logs,
        "historical_snapshots": snapshots,
        "historical_staleness": stale,
    }


def note_link(path: str) -> str:
    title = PurePosixPath(path).stem
    # Use fully qualified vault paths. Notes with duplicate names remain distinct.
    return f"[[{path.removesuffix('.md')}|{title}]]"


def index_markdown(notes: dict[str, str], summary: dict) -> str:
    lines = [
        "# 当前索引",
        "",
        "本页由本地文件重建，反映库内记录的覆盖范围。以明细文件及真实回执为准。",
        "",
        f"- 最近运营日志文件日期：{summary['latest_operation_record_date'] or '未找到'}",
        f"- Markdown 笔记（不含本生成页）：{summary['markdown_notes_excluding_generated_index']}",
        f"- 文章学习卡：{summary['article_cards']}；视频学习卡：{summary['video_cards']}",
        f"- 补充核验登记：{summary['supplemental_checks']}",
    ]
    if summary["recorded_transcripts"] is not None:
        lines.append(f"- 原来源登记中的完整字幕数：{summary['recorded_transcripts']}")
    if summary["recorded_complete_video_views"] is not None:
        lines.append(f"- 原来源登记中的完整画面观看数：{summary['recorded_complete_video_views']}")
    names = {
        "original_home_latest_linked": "原首页已链接的运营日志",
        "source_register_account_date": "来源登记账号汇总",
        "historical_validation_date": "原库校验快照",
        "historical_validation_markdown_count": "原库校验快照记录的 Markdown 数",
    }
    if summary["historical_staleness"]:
        lines += ["", "## 历史汇总口径", ""]
        for key, value in summary["historical_staleness"].items():
            lines.append(f"- {names[key]}：{value}")
        lines.append("- 以上是保留的历史快照。查询较新事项时，打开下面对应日志。")
    lines += ["", "## 最近运营记录", ""]
    lines.extend(f"- {note_link(p)}" for p in summary["operation_records"][:16])
    if not summary["operation_records"]:
        lines.append("- 尚无可识别日期的运营日志。")
    groups: dict[str, list[str]] = defaultdict(list)
    for path in sorted(notes):
        directory = path.split("/", 1)[0]
        match = re.match(r"^(\d{2})-", directory)
        if "/" in path and match and (
            int(match[1]) in (1, 2, 3) or int(match[1]) >= 8
        ):
            groups[directory].append(path)
    for directory, paths in sorted(groups.items()):
        lines.extend(["", f"## {directory}", ""])
        lines.extend(f"- {note_link(p)}" for p in paths)
    navigation = [
        HOME, "00-导航/文章索引.md", "00-导航/视频索引.md",
        "00-导航/历史证据附件.md", "06-更新与证据/更新日志.md",
    ]
    lines += ["", "## 导航", ""]
    lines.extend(f"- {note_link(p)}" for p in navigation if p in notes)
    return "\n".join(lines) + "\n"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A synced Obsidian client only sees the complete file.
    with NamedTemporaryFile(dir=path.parent, encoding="utf-8", mode="w",
                            newline="\n", delete=False) as temporary:
        temporary.write(text)
        temp_path = Path(temporary.name)
    try:
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def search(notes: dict[str, str], query: str, scope: str, limit: int) -> list[dict]:
    terms = normalize(query).split()
    if not 1 <= len(terms) <= 20:
        raise ValueError("搜索需要 1–20 个空格分隔的术语")
    if not 1 <= limit <= 100:
        raise ValueError("--limit 应在 1–100 之间")
    results = []
    for rel, body in notes.items():
        category = scope_for(rel)
        if scope != "all" and scope != category:
            continue
        heading = re.search(r"^#\s+(.+)$", body, flags=re.MULTILINE)
        title = heading[1] if heading else PurePosixPath(rel).stem
        lead = normalize(f"{title} {rel}")
        searchable = normalize(body)
        if not all(term in lead or term in searchable for term in terms):
            continue
        snippets = [
            {"line": line_no, "text": line.strip()[:260]}
            for line_no, line in enumerate(body.splitlines(), 1)
            if any(term in normalize(line) for term in terms)
        ][:3]
        score = sum(
            15 * (term in lead) + min(6, searchable.count(term))
            for term in terms
        )
        results.append({
            "path": rel, "title": title, "scope": category,
            "score": score, "evidence": snippets,
        })
    return sorted(results, key=lambda item: (-item["score"], item["path"]))[:limit]


def local_name(target: str) -> str:
    target = unquote(target.split("#", 1)[0]).strip()
    return target.replace("\\", "/")


def internal_candidate(root: Path, source: str, target: str) -> str | None:
    candidate = (root / PurePosixPath(source).parent / target).resolve()
    try:
        return candidate.relative_to(root).as_posix()
    except ValueError:
        return None


def candidates(by_folded_key: dict[str, list[str]],
               root: Path, source: str, target: str, is_wiki: bool) -> list[str]:
    if not target:
        return [source]
    # Prefer a concrete relative or root-based match. A bare Obsidian note
    # name may also resolve to a unique suffix elsewhere in the vault.
    relative = internal_candidate(root, source, target)
    keys = [relative]
    if is_wiki:
        keys.append(target.lstrip("/"))
    for key in keys:
        if key is None:
            continue
        hits = by_folded_key.get(normalize(key)) or by_folded_key.get(normalize(key + ".md"))
        if hits:
            return hits
    if is_wiki:
        canonical = normalize(target.lstrip("/"))
        return sorted({
            value for key, entries in by_folded_key.items()
            if key.endswith(("/" + canonical, "/" + canonical + ".md"))
            for value in entries
        })
    return []


def check_links(root: Path, files: dict[str, Path], notes: dict[str, str]) -> dict:
    by_folded_key: dict[str, list[str]] = defaultdict(list)
    for name in files:
        by_folded_key[normalize(name)].append(name)
    broken = []
    external_local = []
    ambiguous = []
    resolved = 0
    for rel, original in sorted(notes.items()):
        body = INLINE.sub("", FENCES.sub("", original))
        found = [(True, ref.split("|", 1)[0]) for ref in WIKI.findall(body)]
        found.extend((False, ref) for ref in MARKDOWN.findall(body))
        for is_wiki, value in found:
            ref = value.strip()
            if not is_wiki:
                if ref.startswith("<") and ref.endswith(">"):
                    ref = ref[1:-1]
                else:
                    ref = re.sub(r'\s+["\x27].*["\x27]$', "", ref)
                if ref.startswith("#"):
                    continue
                if re.match(r"^(file:|[a-zA-Z]:[\\/]|\\\\)", ref):
                    external_local.append({"from": rel, "target": value})
                    continue
                if urlsplit(ref).scheme or ref.startswith("//"):
                    # Network sources remain references and are not fetched.
                    continue
            target = local_name(ref)
            if not target:
                continue
            if (target.startswith("/") and not is_wiki
                    or target.startswith("..") and internal_candidate(root, rel, target) is None):
                external_local.append({"from": rel, "target": value})
                continue
            hits = candidates(by_folded_key, root, rel, target, is_wiki)
            if not hits:
                broken.append({"from": rel, "target": value})
            elif len(hits) > 1:
                ambiguous.append({"from": rel, "target": value, "candidates": hits})
            else:
                resolved += 1
    return {
        "resolved": resolved, "broken": broken, "ambiguous": ambiguous,
        "external_local_references": external_local,
        "scope": "笔记和附件文件。未联网核验外链或检查段落锚点。",
    }


def check_manifest(files: dict[str, Path]) -> dict:
    manifest = get_json(files, MANIFEST, None)
    if manifest is None:
        return {"available": False, "missing": [], "changed": []}
    expected = {
        **manifest.get("original_packed_sha256", {}),
        **manifest.get("added_historical_artifact_sha256", {}),
    }
    missing = [rel for rel in sorted(expected) if rel not in files]
    changed = [
        rel for rel in sorted(expected)
        if rel in files and hashlib.sha256(files[rel].read_bytes()).hexdigest() != expected[rel]
    ]
    return {
        "available": True,
        "files_recorded": len(expected),
        "exact_matches": len(expected) - len(missing) - len(changed),
        "missing": missing,
        "changed": changed,
    }


def verify(vault: Path, files: dict[str, Path], notes: dict[str, str], summary: dict) -> dict:
    index = files.get(INDEX)
    if index is not None:
        indexed = index.read_text(encoding="utf-8-sig")
        all_notes = {**notes, INDEX: indexed}
    else:
        indexed = None
        all_notes = notes
    checks = check_links(vault, files, all_notes)
    manifest = check_manifest(files)
    source = get_json(files, REGISTER, {})
    mismatches = []
    for directory, actual, entries, url_field in (
        ("04-文章学习卡/", summary["article_cards"], source.get("articles"), "url"),
        ("05-视频学习卡/", summary["video_cards"], source.get("videos"), "youtube"),
    ):
        if entries is None:
            continue
        registered_ids = [item.get("id") for item in entries]
        urls = [item.get(url_field) for item in entries]
        local_ids = [
            match[1] if (match := re.search(r"^id:\s*(\S+)", body, flags=re.M)) else None
            for rel, body in notes.items() if rel.startswith(directory)
        ]
        if (actual != len(entries) or not all(registered_ids) or not all(urls)
            or len(set(registered_ids)) != len(registered_ids)
            or len(set(urls)) != len(urls)
            or Counter(local_ids) != Counter(registered_ids)):
            mismatches.append(directory)
    fresh = indexed == index_markdown(notes, summary)
    return {
        "index_fresh": fresh,
        "source_card_registry_mismatches": mismatches,
        "links": checks,
        "bundle_manifest": manifest,
        "passed": bool(
            fresh and not mismatches and not checks["broken"] and not checks["ambiguous"]
            and not checks["external_local_references"]
            and not manifest["missing"] and not manifest["changed"]
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="在本地重建、检索和校验 Obsidian 知识库。")
    parser.add_argument("--vault", type=Path, default=VAULT_ROOT, help="知识库所在目录")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("index", help="重建当前索引")
    commands.add_parser("verify", help="检查引用、索引和打包完整性")
    lookup = commands.add_parser("search", help="检索本地 Markdown 笔记")
    lookup.add_argument("query", help="空格分隔的关键词，所有词均须命中")
    lookup.add_argument("--scope", choices=("all", "methods", "operations", "sources"),
                        default="all")
    lookup.add_argument("--limit", type=int, default=12)
    args = parser.parse_args()
    try:
        vault = args.vault.resolve()
        files = relative_files(vault)
        notes = markdown_notes(files)
        if args.command == "search":
            result = {"query": args.query, "results": search(notes, args.query, args.scope, args.limit)}
        else:
            summary = summarize(files, notes)
            if args.command == "index":
                atomic_write(vault / INDEX, index_markdown(notes, summary))
                result = {"index_path": INDEX, **summary}
            else:
                result = {**summary, **verify(vault, files, notes, summary)}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("passed") is False else 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    # Windows pipes may be on an ANSI code page, even with a Unicode terminal.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
