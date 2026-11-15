"""提示词模板与版本（日志规格 §7）。

模板文件首行声明 `# prompt: <名称>@<标签>`；prompt_ver = `<名称>@<标签>+<内容 sha256 前 6 位>`。
真正区分版本的是内容哈希；标签只给人看。同一 (名称, 标签) 出现过不同哈希即视为"改了内容没升标签"。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from .store import ObsStore

HEADER = re.compile(r"^#\s*prompt:\s*([A-Za-z0-9_\-]+)@([A-Za-z0-9_.\-]+)\s*$")


class PromptVersionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Prompt:
    name: str
    tag: str
    sha: str
    text: str            # 去掉首行声明后的正文
    stale_label: bool    # True = 同标签已见过别的内容哈希

    @property
    def ver(self) -> str:
        return f"{self.name}@{self.tag}+{self.sha}"


def parse_prompt(raw: str) -> tuple[str, str, str]:
    raw = raw.replace("\r\n", "\n")
    first, _, body = raw.partition("\n")
    match = HEADER.match(first.strip())
    if not match:
        raise PromptVersionError("提示词模板首行必须是 '# prompt: 名称@标签'")
    return match.group(1), match.group(2), body


def load_prompt(path: Path, *, store: ObsStore | None = None, now_iso: str = "",
                strict: bool = False) -> Prompt:
    name, tag, body = parse_prompt(Path(path).read_text(encoding="utf-8"))
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()[:6]
    stale = False
    if store is not None and not store.readonly:
        seen = {r["sha"] for r in store.execute(
            "select sha from prompt_versions where name=? and tag=?", (name, tag))}
        stale = bool(seen) and sha not in seen
        if stale and strict:
            raise PromptVersionError(f"提示词 {name}@{tag} 内容已变但标签未升（见过 {sorted(seen)}，现在 {sha}）")
        store.execute(
            "insert or ignore into prompt_versions(name, tag, sha, first_seen) values (?,?,?,?)",
            (name, tag, sha, now_iso))
    return Prompt(name=name, tag=tag, sha=sha, text=body, stale_label=stale)


def render(prompt: Prompt, **values: object) -> str:
    """最简模板替换：{{key}}。缺失的键保留原样并抛错，避免静默发出未填充的提示词。"""
    out = prompt.text
    for key, value in values.items():
        out = out.replace("{{" + key + "}}", str(value))
    leftover = re.findall(r"\{\{([A-Za-z0-9_]+)\}\}", out)
    if leftover:
        raise PromptVersionError(f"{prompt.ver} 有未填充的占位符: {sorted(set(leftover))}")
    return out
