"""Library management from the web UI: upload and import an EPUB, export the translation, the API key (ADR-034)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from ..errors import NCTEError

MAX_UPLOAD = 300 * 1024 * 1024
SAFE_NAME = re.compile(r"[^\w.\-（）()【】「」 ]+")


def _uploads(ws: Any) -> Path:
    d = Path(ws.root) / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_upload(ws: Any, filename: str, data: bytes) -> dict[str, Any]:
    """Keep the uploaded file and say what it is (no import yet: a detected series is only suggested)."""
    from ..epub.parser import load_epub

    name = SAFE_NAME.sub("_", Path(filename or "book.epub").name).strip() or "book.epub"
    if not name.lower().endswith(".epub"):
        raise NCTEError("只能上傳 .epub 檔")
    if not data:
        raise NCTEError("檔案是空的")
    path = _uploads(ws) / name
    path.write_bytes(data)
    try:
        pkg = load_epub(path)  # safety checks (DRM, zip bomb, fixed layout…) before anything is kept
    except Exception:
        path.unlink(missing_ok=True)
        raise
    found = ws.find_book(path)
    sug = ws.suggest_series(pkg)
    g = sug["guess"]
    suggestion = None
    if sug["series_id"] or g["confidence"] in ("high", "medium"):
        suggestion = {"series": sug["series_title"] or g["title"], "existing": bool(sug["series_id"]),
                      "volume": sug["volume"], "evidence": g["evidence"]}
    return {"upload": name, "title": pkg.title, "imported_as": found[1] if found else None, "suggestion": suggestion,
            "series": [s["title"] for s in ws.list_series()]}


def import_upload(ws: Any, upload: str, series: str | None, volume: float | None) -> dict[str, Any]:
    path = _uploads(ws) / Path(upload).name
    if not path.is_file():
        raise NCTEError("找不到上傳的檔案，請重新上傳")
    conn, book, created = ws.import_book(path, series=series or None, volume=volume)
    n = conn.execute("SELECT COUNT(*) FROM units WHERE book_id=? AND translatable=1", (book.id,)).fetchone()[0]
    path.unlink(missing_ok=True)  # the library keeps its own copy
    return {"book_id": book.id, "title": book.title, "created": created, "units": n}


def export(conn: Any, cfg: Any, book: Any) -> dict[str, Any]:
    from ..exporter import export_book

    out = export_book(conn, cfg, book, lang="zh-TW", strict=False, run_epubcheck=True)
    v = out.validation
    return {"path": str(out.result.output_path), "name": Path(out.result.output_path).name, "ok": v.ok,
            "epubcheck": v.epubcheck.get("status"), "errors": v.errors[:10], "warnings": v.warnings[:10],
            "stats": out.stats}


def exported_file(cfg: Any, book: Any) -> Path:
    from ..exporter import default_output_path

    path = default_output_path(cfg, book, "zh-TW")
    if not path.is_file():
        raise NCTEError("還沒有匯出檔，請先按「匯出 EPUB」")
    return path


# ---------------------------------------------------------------- API key (stored in .env, never sent back)
def key_status(env_path: Path, provider: str = "openrouter") -> dict[str, Any]:
    from ..config import get_api_key

    if provider == "claude":  # no key: Claude Code's own login on this computer
        import shutil

        found = shutil.which(os.environ.get("NCTE_CLAUDE_BIN") or "claude")
        return {"provider": provider, "set": bool(found), "hint": None, "file": str(env_path.resolve()), "claude": found}
    if provider == "codex":  # no key: the Codex CLI's own ChatGPT login on this computer
        from ..llm.codex_cli import found as codex_found

        found = codex_found()
        return {"provider": provider, "set": bool(found), "hint": None, "file": str(env_path.resolve()), "codex": found}
    if provider == "agy":  # disabled (ADR-105): the page says why and asks for another connection
        return {"provider": provider, "set": False, "hint": None, "file": str(env_path.resolve()),
                "disabled": AGY_DISABLED_ZH}
    if provider == "local":  # no key: is a model server answering, and which model has it loaded?
        return {"provider": provider, **local_status(), "hint": None, "file": str(env_path.resolve())}
    key = get_api_key(provider)
    return {"provider": provider, "set": bool(key), "hint": f"…{key[-4:]}" if key else None, "file": str(env_path.resolve())}


AGY_DISABLED_ZH = ("Antigravity（agy）已停用：Google 的 Antigravity 常見問題（2026-10-16 論壇工作人員確認）說明，"
                   "無頭模式 agy -p 只能用在你自己的腳本與 CI；包在第三方程式或應用程式裡不受消費者方案支援，"
                   "可能導致帳號停權。NCTE 就是這樣的應用程式。請改用「Gemini API（Google AI Studio 金鑰）」或其他連線方式。")


def local_status(base_url: str | None = None) -> dict[str, Any]:
    """Whether the local model server answers, and the chat models it lists (the loaded one first)."""
    from ..config import load_config
    from ..errors import LLMError
    from ..llm.local import LocalClient

    url = base_url or load_config().openrouter.local_base_url
    try:
        models = [m for m in LocalClient(url, timeout=5).list_models() if m["id"] != "local/default"]
    except LLMError as exc:
        return {"set": False, "url": url, "models": [], "error": str(exc)}
    return {"set": True, "url": url, "models": [{"id": m["id"], "state": m["local"].get("state")} for m in models]}


def set_key(env_path: Path, key: str, provider: str = "openrouter") -> dict[str, Any]:
    from ..config import key_name

    key = key.strip()
    if provider not in ("openrouter", "fal", "gemini"):
        raise NCTEError("未知的連線方式")
    if provider == "fal" and not re.fullmatch(r"[A-Za-z0-9_:.-]{10,}", key):
        raise NCTEError("fal 金鑰格式不正確")
    if provider == "openrouter" and not re.fullmatch(r"sk-or-[A-Za-z0-9_\-]{20,}", key):
        raise NCTEError("這不像 OpenRouter 的金鑰（應以 sk-or- 開頭）")
    if provider == "gemini" and not re.fullmatch(r"[A-Za-z0-9_\-]{20,}", key):
        raise NCTEError("這不像 Google AI Studio 的金鑰（只有英數字、底線與連字號，至少 20 個字元）")
    _save_env(env_path, key_name(provider), key)
    return key_status(env_path, provider)


def _save_env(env_path: Path, name: str, value: str) -> None:
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    lines = [x for x in lines if x.split("=", 1)[0].strip() != name] + [f"{name}={value}"]
    env_path.touch(mode=0o600, exist_ok=True)
    env_path.chmod(0o600)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[name] = value


def set_provider(env_path: Path, cfg: Any, provider: str) -> dict[str, Any]:
    from ..config import (
        DEFAULT_MODEL,
        PROVIDERS,
        use_claude_models,
        use_codex_models,
        use_gemini_models,
        use_local_models,
    )

    if provider not in PROVIDERS:
        raise NCTEError("未知的連線方式")
    _save_env(env_path, "NCTE_LLM_PROVIDER", provider)
    was = cfg.openrouter.provider
    cfg.openrouter.provider = provider
    if provider == "claude":
        use_claude_models(cfg)
    elif provider == "local":
        use_local_models(cfg)
    elif provider == "codex":
        use_codex_models(cfg)
    elif provider == "gemini":
        use_gemini_models(cfg)
    elif was in ("claude", "local", "codex", "agy", "gemini"):  # back to OpenRouter: its default, not another's id
        cfg.openrouter.model = DEFAULT_MODEL
        cfg.openrouter.stage_models = {}
        cfg.openrouter.temperature = type(cfg.openrouter)().temperature
    return key_status(env_path, provider)


# another company's model a proofreading pass can use (ADR-082), by the command line tool found on this computer or
# the key set here (the Gemini API, ADR-105: billed by Google per use; agy is not offered)
CROSS_CHOICES = (("gemini", "gemini/gemini-3.8-flash", "Gemini Flash（Gemini API，Google AI Studio 金鑰，按用量計費）",
                  "Google"),
                 ("codex", "codex/gpt-6.1-sol", "GPT（Codex，ChatGPT 方案）", "OpenAI"),
                 ("claude", "claude/opus", "Claude Opus（Claude 方案）", "Anthropic"))


def cross_status(cfg: Any) -> dict[str, Any]:
    """The cross-proofreading choice (off by default) and what can be chosen here: the tools found on this computer,
    other than the run's own source."""
    import shutil

    from ..config import get_api_key
    from ..llm.codex_cli import found as codex_found

    found = {"gemini": get_api_key("gemini"), "codex": codex_found(),
             "claude": shutil.which(os.environ.get("NCTE_CLAUDE_BIN") or "claude")}
    options = [{"model": m, "label": label, "company": who} for src, m, label, who in CROSS_CHOICES
               if found.get(src) and src != cfg.openrouter.provider]
    return {"model": cfg.review.cross_proofread_model, "options": options}


def set_cross(env_path: Path, cfg: Any, model: str | None) -> dict[str, Any]:
    model = (model or "").strip() or None
    if model is not None and model not in {o["model"] for o in cross_status(cfg)["options"]}:
        raise NCTEError("這個模型目前不能用來交叉校稿")
    _save_env(env_path, "NCTE_CROSS_PROOFREAD", model or "off")
    cfg.review.cross_proofread_model = model
    return cross_status(cfg)


def balance(client: Any) -> dict[str, Any]:
    """Credits left on the account and on this key (either can stop a run)."""
    fn = getattr(client, "balance", None)
    return fn() if fn else {}
