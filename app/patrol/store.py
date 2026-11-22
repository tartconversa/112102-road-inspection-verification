"""运行时存储：只追加的事件日志与不可变报告快照。

不回写 sources/ 下的试点台账；原试点名单永远不变，所有运行期变化
都以事件形式追加，支持完整重放与审计。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


class StoreError(RuntimeError):
    pass


class Store:
    def __init__(self, runtime_dir: str | Path):
        self.root = Path(runtime_dir)
        self.events_path = self.root / "events.log.jsonl"
        self.reports_dir = self.root / "reports"
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        if not self.events_path.exists():
            self.events_path.touch()

    # ---------- 事件日志 ----------
    def append_event(self, event_type: str, day: int, payload: dict, event_id: str) -> dict:
        existing = self._load_event_ids()
        if event_id in existing:
            raise StoreError(f"事件编号重复：{event_id}（事件日志只追加，不能覆盖）")
        seq = len(existing) + 1
        record = {
            "seq": seq, "id": event_id, "type": event_type,
            "day": day, "payload": payload,
        }
        with self.events_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return record

    def load_events(self) -> list[dict]:
        with self.events_path.open(encoding="utf-8") as stream:
            return [json.loads(line) for line in stream if line.strip()]

    def _load_event_ids(self) -> set[str]:
        return {event["id"] for event in self.load_events()}

    def log_hash(self) -> str:
        return hashlib.sha256(self.events_path.read_bytes()).hexdigest()

    # ---------- 报告快照 ----------
    def save_report(self, report_id: str, payload: dict) -> Path:
        path = self.reports_dir / f"{report_id}.json"
        if path.exists():
            raise StoreError(f"报告 {report_id} 已存在且不可变；请用新编号另存")
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return path

    def list_reports(self) -> list[dict]:
        items = []
        for path in sorted(self.reports_dir.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            items.append({
                "report_id": payload["report_id"],
                "created_after_event_seq": payload.get("created_after_event_seq"),
                "source_hashes": payload.get("source_hashes"),
                "event_log_hash": payload.get("event_log_hash"),
                "title": payload.get("title"),
            })
        return items

    def load_report(self, report_id: str) -> dict:
        path = self.reports_dir / f"{report_id}.json"
        if not path.exists():
            raise StoreError(f"报告不存在：{report_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def diff_reports(self, left_id: str, right_id: str) -> dict:
        left, right = self.load_report(left_id), self.load_report(right_id)

        def flatten(obj, prefix=""):
            flat = {}
            if isinstance(obj, dict):
                for key, value in obj.items():
                    flat.update(flatten(value, f"{prefix}.{key}" if prefix else key))
            elif isinstance(obj, list):
                flat[prefix] = json.dumps(obj, ensure_ascii=False, sort_keys=True)
            else:
                flat[prefix] = obj
            return flat

        fl, fr = flatten(left), flatten(right)
        changed = [
            {"field": key, "from": fl.get(key), "to": fr.get(key)}
            for key in sorted(set(fl) | set(fr))
            if fl.get(key) != fr.get(key)
        ]
        return {
            "left": left_id,
            "right": right_id,
            "left_event_seq": left.get("created_after_event_seq"),
            "right_event_seq": right.get("created_after_event_seq"),
            "changed_fields": changed,
        }
