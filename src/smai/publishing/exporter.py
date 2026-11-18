"""Local export bundle generator for offline or manual Instagram publishing."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any


def export_draft_bundle(
    draft: dict[str, Any],
    assets: list[dict[str, Any]],
    export_root: Path,
) -> Path:
    """Write a self-contained folder with media files, caption.txt, and metadata.json."""
    bundle_dir = export_root / f"draft_{draft['id']}_{draft['format']}"
    bundle_dir.mkdir(parents=True, exist_ok=True)

    copied_files: list[str] = []
    for asset in assets:
        src = Path(asset["local_path"])
        if src.exists():
            dst = bundle_dir / src.name
            if src.resolve() != dst.resolve():
                shutil.copy2(src, dst)
            copied_files.append(dst.name)

    ab_hooks = json.loads(draft.get("ab_hooks_json") or "[]")
    selected_idx = int(draft.get("selected_hook_idx") or 0)
    caption_text = draft["caption"]
    if ab_hooks and 0 <= selected_idx < len(ab_hooks):
        chosen_hook = ab_hooks[selected_idx].get("hook_text", "")
        if chosen_hook and chosen_hook not in caption_text:
            caption_text = f"{chosen_hook}\n\n{caption_text}"

    (bundle_dir / "caption.txt").write_text(caption_text, encoding="utf-8")
    (bundle_dir / "metadata.json").write_text(
        json.dumps(
            {
                "draft_id": draft["id"],
                "format": draft["format"],
                "title": draft["title"],
                "qa_status": draft["qa_status"],
                "qa_score": draft["qa_score"],
                "files": copied_files,
                "ab_hooks": ab_hooks,
                "selected_hook_idx": selected_idx,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return bundle_dir
