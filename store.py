"""wx_ROBOT 的本地计划队列。

每个朋友圈计划是一个文件夹：
    待发布/
      20261030_0830_晚霞/
        文案.txt
        图片/
          01_xxx.jpg
        post.json

文件夹可以直接备份、拷贝；程序只读取 post.json 与 文案.txt。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
QUEUE_DIR = ROOT / "待发布"
SENT_DIR = ROOT / "已发布"
FAILED_DIR = ROOT / "失败"
DELETED_DIR = ROOT / "已删除"
LOG_DIR = ROOT / "logs"
PREVIEW_DIR = LOG_DIR / "previews"

STATUS_DRAFT = "draft"
STATUS_SCHEDULED = "scheduled"
STATUS_PUBLISHING = "publishing"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

STATUS_LABELS = {
    STATUS_DRAFT: "草稿",
    STATUS_SCHEDULED: "待发布",
    STATUS_PUBLISHING: "发布中",
    STATUS_SENT: "已发布",
    STATUS_FAILED: "失败",
    STATUS_SKIPPED: "已跳过",
}

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp"}
MAX_IMAGE_BYTES = 20 * 1024 * 1024


def now_local() -> datetime:
    return datetime.now().astimezone()


def ensure_dirs() -> None:
    for path in (QUEUE_DIR, SENT_DIR, FAILED_DIR, DELETED_DIR, LOG_DIR, PREVIEW_DIR):
        path.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    defaults = {
        "account_wxid": "",
        "max_schedule_hours": 24,
        "catch_up_minutes": 90,
        "check_interval_seconds": 3,
        "max_retries": 2,
        "retry_delay_minutes": 8,
        "max_images": 9,
        "max_text_chars": 1500,
        "dry_run": False,
        "keep_awake": True,
        "wechat_exe": "",
        "start_wechat_if_needed": True,
        "verify_text": True,
        "debug_screenshots": True,
        "window_wait_seconds": 8,
        "publish_wait_seconds": 35,
        "image_settle_seconds": 4,
        "failure_check_seconds": 15,
        "privacy": "public",
    }
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                defaults.update(data)
        except Exception:
            pass
    return defaults


def save_config(cfg: dict) -> None:
    _atomic_write_text(
        CONFIG_PATH,
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
    )


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _atomic_write_json(path: Path, data: dict) -> None:
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def normalize_text(text: str) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _slug(text: str, limit: int = 14) -> str:
    first = ""
    for line in normalize_text(text).splitlines():
        line = line.strip()
        if line:
            first = line
            break
    first = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", first)
    first = re.sub(r"\s+", "", first)
    return first[:limit] or "朋友圈"


def _unique_dir(parent: Path, name: str) -> Path:
    candidate = parent / name
    if not candidate.exists():
        return candidate
    for i in range(2, 1000):
        candidate = parent / f"{name}_{i}"
        if not candidate.exists():
            return candidate
    return parent / f"{name}_{uuid.uuid4().hex[:6]}"


@dataclass
class Post:
    id: str
    folder: Path
    publish_at: datetime
    text: str
    images: list[Path] = field(default_factory=list)
    status: str = STATUS_SCHEDULED
    attempts: int = 0
    last_error: str = ""
    created_at: str = ""
    updated_at: str = ""
    sent_at: str = ""
    next_retry_at: str = ""

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def title(self) -> str:
        t = normalize_text(self.text).replace("\n", " ")
        return (t[:26] + "…") if len(t) > 26 else (t or "(无文案)")

    def due(self, now: datetime | None = None) -> bool:
        now = now or now_local()
        if self.status != STATUS_SCHEDULED:
            return False
        if self.next_retry_at:
            try:
                if parse_dt(self.next_retry_at) > now:
                    return False
            except Exception:
                pass
        return self.publish_at <= now

    def late_minutes(self, now: datetime | None = None) -> float:
        now = now or now_local()
        return (now - self.publish_at).total_seconds() / 60.0

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "publish_at": self.publish_at.isoformat(timespec="seconds"),
            "status": self.status,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "sent_at": self.sent_at,
            "next_retry_at": self.next_retry_at,
            "images": [p.relative_to(self.folder).as_posix() for p in self.images],
        }


def _read_post(folder: Path) -> Post | None:
    meta_path = folder / "post.json"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
        text_path = folder / "文案.txt"
        if text_path.exists():
            text = text_path.read_text(encoding="utf-8-sig")
        else:
            text = meta.get("text", "")
        images: list[Path] = []
        img_dir = folder / "图片"
        if img_dir.exists():
            for p in sorted(img_dir.iterdir()):
                if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                    images.append(p)
        return Post(
            id=str(meta.get("id") or folder.name),
            folder=folder,
            publish_at=parse_dt(meta["publish_at"]),
            text=text,
            images=images,
            status=str(meta.get("status") or STATUS_SCHEDULED),
            attempts=int(meta.get("attempts") or 0),
            last_error=str(meta.get("last_error") or ""),
            created_at=str(meta.get("created_at") or ""),
            updated_at=str(meta.get("updated_at") or ""),
            sent_at=str(meta.get("sent_at") or ""),
            next_retry_at=str(meta.get("next_retry_at") or ""),
        )
    except Exception:
        return None


def list_posts() -> list[Post]:
    ensure_dirs()
    posts: list[Post] = []
    for folder in QUEUE_DIR.iterdir():
        if folder.is_dir():
            post = _read_post(folder)
            if post is not None:
                posts.append(post)
    posts.sort(key=lambda p: p.publish_at)
    return posts


def get_post(post_id: str) -> Post | None:
    for p in list_posts():
        if p.id == post_id:
            return p
    return None


def validate(publish_at: datetime, text: str, images: Iterable[Path], cfg: dict) -> list[str]:
    errors: list[str] = []
    text = normalize_text(text)
    images = list(images)
    max_hours = int(cfg.get("max_schedule_hours", 24) or 24)
    max_chars = int(cfg.get("max_text_chars", 1500) or 1500)
    max_images = int(cfg.get("max_images", 9) or 9)
    now = now_local()

    if not text:
        errors.append("文案不能为空。")
    if len(text) > max_chars:
        errors.append(f"文案最多 {max_chars} 字，目前 {len(text)} 字。")
    if not images:
        errors.append("请至少添加 1 张图片（1–9 张）。")
    if len(images) > max_images:
        errors.append(f"最多 {max_images} 张图片，目前 {len(images)} 张。")
    if publish_at < now - timedelta(minutes=1):
        errors.append("发布时间已经过去，请选择未来的时间。")
    if publish_at > now + timedelta(hours=max_hours):
        errors.append(f"发布时间最多只能安排在 {max_hours} 小时内。")
    for p in images:
        if not Path(p).exists():
            errors.append(f"图片不存在：{p}")
            continue
        if Path(p).suffix.lower() not in IMAGE_EXTS:
            errors.append(f"不支持的图片格式：{Path(p).name}（支持 jpg/png/bmp）")
        try:
            if Path(p).stat().st_size > MAX_IMAGE_BYTES:
                errors.append(f"图片超过 20MB：{Path(p).name}")
        except OSError:
            pass
    return errors


def _copy_images(post_folder: Path, images: Iterable[Path]) -> list[Path]:
    img_dir = post_folder / "图片"
    img_dir.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for i, src in enumerate(images, 1):
        src = Path(src)
        base = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", src.stem).strip() or "image"
        name = f"{i:02d}_{base}{src.suffix.lower()}"
        dest = img_dir / name
        shutil.copy2(src, dest)
        copied.append(dest)
    return copied


def create_post(publish_at: datetime, text: str, images: Iterable[Path], cfg: dict) -> Post:
    ensure_dirs()
    text = normalize_text(text)
    images = [Path(p) for p in images]
    errors = validate(publish_at, text, images, cfg)
    if errors:
        raise ValueError("\n".join(errors))
    stamp = publish_at.strftime("%Y%m%d_%H%M")
    folder = _unique_dir(QUEUE_DIR, f"{stamp}_{_slug(text)}")
    folder.mkdir(parents=True)
    copied = _copy_images(folder, images)
    (folder / "文案.txt").write_text(text + "\n", encoding="utf-8")
    ts = now_local().isoformat(timespec="seconds")
    post = Post(
        id=uuid.uuid4().hex[:12],
        folder=folder,
        publish_at=publish_at,
        text=text,
        images=copied,
        status=STATUS_SCHEDULED,
        attempts=0,
        created_at=ts,
        updated_at=ts,
    )
    _atomic_write_json(folder / "post.json", post.to_json())
    return post


def update_post(post: Post, publish_at: datetime, text: str, images: Iterable[Path], cfg: dict) -> Post:
    text = normalize_text(text)
    images = [Path(p) for p in images]
    errors = validate(publish_at, text, images, cfg)
    if errors:
        raise ValueError("\n".join(errors))

    # 先复制到临时目录，再替换旧图片；这样即使新图片就是旧文件也不会被删掉。
    folder = post.folder
    img_dir = folder / "图片"
    tmp_dir = folder / f".图片_临时_{uuid.uuid4().hex[:6]}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for i, src in enumerate(images, 1):
        src = Path(src)
        base = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", src.stem).strip() or "image"
        tmp = tmp_dir / f"{i:02d}_{base}{src.suffix.lower()}"
        shutil.copy2(src, tmp)
        copied.append(tmp)
    if img_dir.exists():
        shutil.rmtree(img_dir)
    tmp_dir.rename(img_dir)
    copied = [img_dir / p.name for p in copied]
    (folder / "文案.txt").write_text(text + "\n", encoding="utf-8")
    post.publish_at = publish_at
    post.text = text
    post.images = copied
    post.updated_at = now_local().isoformat(timespec="seconds")
    post.last_error = ""
    if post.status in (STATUS_FAILED, STATUS_SKIPPED):
        post.status = STATUS_SCHEDULED
        post.next_retry_at = ""
    _atomic_write_json(folder / "post.json", post.to_json())
    return post


def set_status(post: Post, status: str, *, error: str = "", sent_at: str = "",
               next_retry_at: str = "", attempts: int | None = None) -> None:
    post.status = status
    post.last_error = error
    if sent_at:
        post.sent_at = sent_at
    if next_retry_at:
        post.next_retry_at = next_retry_at
    if attempts is not None:
        post.attempts = attempts
    post.updated_at = now_local().isoformat(timespec="seconds")
    _atomic_write_json(post.folder / "post.json", post.to_json())


def recover_interrupted() -> int:
    count = 0
    for post in list_posts():
        if post.status == STATUS_PUBLISHING:
            set_status(
                post,
                STATUS_SCHEDULED,
                error="上次发布过程中程序退出，已自动恢复为待发布。",
            )
            count += 1
    return count


def move_post(post: Post, target_root: Path, reason: str = "") -> Path:
    target_root.mkdir(parents=True, exist_ok=True)
    if target_root == SENT_DIR:
        month = (post.sent_at or now_local().isoformat())[:7].replace("-", "-")
        target_root = target_root / month
    target_root.mkdir(parents=True, exist_ok=True)
    dest = _unique_dir(target_root, post.folder.name)
    shutil.move(str(post.folder), str(dest))
    return dest


def delete_post(post: Post) -> Path:
    DELETED_DIR.mkdir(parents=True, exist_ok=True)
    dest = _unique_dir(DELETED_DIR, f"{post.folder.name}_{now_local().strftime('%Y%m%d%H%M%S')}")
    shutil.move(str(post.folder), str(dest))
    return dest


def latest_login_account() -> tuple[str, float]:
    """根据 xwechat 登录目录推测最近登录的 wxid（仅用于界面提示）。"""
    login_root = Path(os.environ.get("APPDATA", "")) / "Tencent" / "xwechat" / "login"
    best = ("", 0.0)
    if not login_root.exists():
        return best
    for d in login_root.iterdir():
        if not d.is_dir():
            continue
        key = d / "key_info.dat"
        try:
            mtime = key.stat().st_mtime if key.exists() else d.stat().st_mtime
        except OSError:
            continue
        if mtime > best[1]:
            best = (d.name, mtime)
    return best
