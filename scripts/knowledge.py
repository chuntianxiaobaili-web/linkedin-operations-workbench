"""Initialize a Markdown vault and persist attributed model knowledge cards."""
import argparse
import hashlib
import json
from pathlib import Path
import re
from datetime import date
import sys

MODEL = "model-knowledge"
LOCAL = "local-evidence"
EXPERIMENT = "experiment"
KINDS = {MODEL: "模型已有知识", LOCAL: "本地资料归纳", EXPERIMENT: "待验证的实验设想"}
FIELDS = ("title", "topic", "audience", "problem", "steps", "deliverable", "checks", "limits")
DIRECTORY = "08-模型归纳"


def write_new(path, text):
    with path.open("x", encoding="utf-8", newline="\n") as file:
        file.write(text)


def create_vault(path, name):
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ValueError("新库目录必须不存在或为空。导入已有库请用 load。")
    path.mkdir(parents=True, exist_ok=True)
    (path / ".obsidian").mkdir()
    (path / DIRECTORY).mkdir()
    (path / "附件").mkdir()
    write_new(path / ".obsidian" / "app.json",
              json.dumps({"alwaysUpdateLinks": True, "attachmentFolderPath": "附件"}, ensure_ascii=False) + "\n")
    write_new(path / "00-首页.md",
              f"# {name}\n\n[[08-模型归纳/方法卡索引|方法卡索引]]\n\n"
              "按市场、买家、产品与当前问题检索方法卡。每张卡保留来源类别、适用边界与检验方法。\n")
    update_index(path / DIRECTORY)


def update_index(directory):
    notes = sorted(directory.glob("KB-*.md"), key=lambda p: p.name)
    links = []
    for note in notes:
        header = next((s.removeprefix("# ") for s in note.read_text("utf-8").splitlines()
                       if s.startswith("# ")), note.stem)
        links.append(f"- [[{DIRECTORY}/{note.stem}|{header.replace('|', '｜').replace(']', '）')}]]")
    with (directory / "方法卡索引.md").open("w", encoding="utf-8", newline="\n") as file:
        file.write("# 方法卡索引\n\n" + "\n".join(links) + "\n")


def render_card(card, generated_on):
    if card.get("origin") not in KINDS or set(FIELDS) - card.keys():
        raise ValueError("方法卡字段缺失或来源类别无效。")
    if any(not isinstance(card[k], str) or not card[k].strip() for k in FIELDS if k not in {"steps", "checks"}):
        raise ValueError("方法卡文字字段必须是非空字符串。")
    if any(len(card[k]) > 200 or "\n" in card[k] for k in ("title", "topic", "audience")):
        raise ValueError("标题、主题及适用对象须为单行，且不超过200字符。")
    for key in ("steps", "checks", "references"):
        items = card.get(key, [])
        if (not isinstance(items, list)
                or (not items and key != "references")
                or any(not isinstance(item, str) or not item.strip() or "\n" in item for item in items)):
            raise ValueError(f"{key} 须为单行字符串列表。")
    refs = card.get("references", [])
    if card["origin"] == LOCAL and not refs:
        raise ValueError("本地资料归纳需要至少一条读过的资料位置。")
    if card["origin"] != LOCAL and refs:
        raise ValueError("模型已有知识和实验设想的 references 留空。")
    key = json.dumps([card["title"].strip().casefold(), card["topic"].strip().casefold()],
                     ensure_ascii=False)
    filename = "KB-" + hashlib.sha256(key.encode()).hexdigest()[:16] + ".md"
    body = ["# " + card["title"], "", f"- 主题：{card['topic']}", f"- 适用：{card['audience']}",
            f"- 来源类别：{KINDS[card['origin']]}", f"- 归纳日期：{generated_on}",
            "", "## 要解决的问题", "", card["problem"], "", "## 操作步骤", ""]
    body.extend(f"{i}. {step}" for i, step in enumerate(card["steps"], 1))
    body.extend(["", "## 交付物", "", card["deliverable"], "", "## 如何检验", ""])
    body.extend(f"- {check}" for check in card["checks"])
    body.extend(["", "## 适用边界", "", card["limits"], ""])
    if refs:
        body.extend(["## 本地资料", "", *(f"- {ref}" for ref in refs), ""])
    return filename, "\n".join(body)


def load_cards(vault, input_path):
    if not (vault / "00-首页.md").is_file():
        raise ValueError("请指定已有本工具首页的 vault，或先运行 new。")
    cards = json.loads(input_path.read_text(encoding="utf-8-sig"))
    if not isinstance(cards, list) or not cards or len(cards) > 100:
        raise ValueError("每批提交1至100张方法卡的 JSON 数组。")
    directory = vault / DIRECTORY
    prepared = {}
    skipped = 0
    for card in cards:
        if not isinstance(card, dict):
            raise ValueError("每张卡必须是 JSON 对象。")
        name, text = render_card(card, date.today().isoformat())
        target = directory / name
        if name in prepared:
            raise ValueError(f"批次内存在重复标题和主题：{card['title']}")
        if target.exists():
            old = target.read_text("utf-8")
            normalize = lambda body: re.sub(r"(?m)^- 归纳日期：[^\n]+", "", body)
            if normalize(old) != normalize(text):
                raise ValueError(f"同名方法卡已有不同内容，请人工确认版本：{card['title']}")
            skipped += 1
            prepared[name] = None
        else:
            prepared[name] = text
    directory.mkdir(exist_ok=True)
    for name, text in prepared.items():
        if text is not None:
            write_new(directory / name, text)
    update_index(directory)
    return {"vault": str(vault), "added": len(prepared) - skipped, "already_present": skipped,
            "index": str(directory / "方法卡索引.md")}


def main():
    parser = argparse.ArgumentParser(description="创建知识库并归档方法卡")
    commands = parser.add_subparsers(dest="command", required=True)
    new = commands.add_parser("new")
    new.add_argument("--dest", required=True, type=Path)
    new.add_argument("--name", required=True)
    load = commands.add_parser("load")
    load.add_argument("--vault", required=True, type=Path)
    load.add_argument("--cards", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "new":
            vault = args.dest.expanduser().resolve()
            create_vault(vault, args.name)
            result = {"vault": str(vault), "homepage": str(vault / "00-首页.md")}
        else:
            result = load_cards(args.vault.expanduser().resolve(), args.cards)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
