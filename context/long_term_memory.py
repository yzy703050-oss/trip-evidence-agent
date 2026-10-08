"""
长期记忆 (Long-term Memory)
持久化存储用户信息，支持跨会话访问
"""
from typing import Dict, Any, List, Optional
import json
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path
import logging

from .session_store import SessionStore, atomic_json_write, storage_component

logger = logging.getLogger(__name__)


class LongTermMemory:
    """
    长期记忆：持久化用户信息
    - 用户偏好（家庭地址、酒店品牌、航空公司等）
    - 历史行程记录
    - 统计信息
    """

    def __init__(self, user_id: str, storage_path: str = "data/memory"):
        """
        初始化长期记忆

        Args:
            user_id: 用户ID
            storage_path: 存储路径
        """
        self.user_id = storage_component(user_id)
        self.storage_path = storage_path
        self.db_path = os.path.join(storage_path, f"{self.user_id}.json")  # retained legacy file
        self.user_dir = Path(storage_path) / self.user_id
        self.profile_path = self.user_dir / "profile.json"
        self.trips_path = self.user_dir / "trips.json"

        # 确保存储目录存在
        self.user_dir.mkdir(parents=True, exist_ok=True)

        # 加载或初始化数据
        self.data = self._load()
        logger.info(f"Long-term memory initialized for user: {user_id}")

    def _load(self) -> Dict[str, Any]:
        """从文件加载数据"""
        legacy = {}
        if os.path.exists(self.db_path):
            with open(self.db_path, "r", encoding="utf-8") as handle:
                legacy = json.load(handle)

        if self.profile_path.exists() or self.trips_path.exists():
            profile = json.loads(self.profile_path.read_text(encoding="utf-8")) if self.profile_path.exists() else {}
            trips = json.loads(self.trips_path.read_text(encoding="utf-8")) if self.trips_path.exists() else {}
            data = self._init_data()
            data.update(profile)
            data["trip_history"] = trips.get("trip_history", [])
            data["chat_history"] = []
            self.data = data
        elif legacy:
            self.data = self._migrate_data(legacy)
        else:
            self.data = self._init_data()
            self._save()

        if not self.data.get("legacy_chat_cleared"):
            self._migrate_legacy_chat(legacy.get("chat_history", []))
        return self.data

    def _migrate_legacy_chat(self, messages: List[Dict[str, Any]]) -> None:
        """Replay old chat messages with stable IDs so restarts never duplicate them."""
        turns: Dict[str, str] = {}
        for index, message in enumerate(messages):
            session_id = message.get("session_id") or "legacy"
            if message.get("role") == "user" or session_id not in turns:
                turns[session_id] = f"legacy-turn-{index}"
            SessionStore(self.storage_path, self.user_id, session_id).append({
                "event_id": f"legacy-message-{index}",
                "turn_id": turns[session_id],
                "scope": "session",
                "type": "message",
                "role": message.get("role", "assistant"),
                "content": message.get("content", ""),
                "timestamp": message.get("timestamp", ""),
                "final": message.get("role") == "assistant",
            })

    def _migrate_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        迁移旧数据格式到新格式

        Args:
            data: 原始数据

        Returns:
            迁移后的数据
        """
        # 1. 确保必需字段存在
        if "chat_history" not in data:
            data["chat_history"] = []
        if "trip_history" not in data:
            data["trip_history"] = []
        if "statistics" not in data:
            data["statistics"] = {}
        if "total_messages" not in data.get("statistics", {}):
            data["statistics"]["total_messages"] = 0
        if "preferences" not in data:
            data["preferences"] = []

        # 2. 迁移旧格式：字典 → 列表
        if isinstance(data.get("preferences"), dict):
            old_prefs = data["preferences"]
            new_prefs = []
            for pref_type, pref_value in old_prefs.items():
                if pref_value is not None:
                    new_prefs.append({"type": pref_type, "value": pref_value})
            data["preferences"] = new_prefs
            logger.info(f"Migrated: Converted preferences from dict to list ({len(new_prefs)} items)")

        # 3. 修复嵌套 bug（旧代码产生的错误数据）
        if isinstance(data.get("preferences"), list):
            fixed_prefs = []
            for pref in data["preferences"]:
                if isinstance(pref, dict):
                    # 错误的嵌套：{"type": "preferences", "value": [...]}
                    if pref.get("type") == "preferences" and isinstance(pref.get("value"), list):
                        for nested_pref in pref["value"]:
                            if isinstance(nested_pref, dict) and "type" in nested_pref:
                                fixed_prefs.append({"type": nested_pref["type"], "value": nested_pref["value"]})
                        logger.info("Migrated: Fixed nested preferences bug")
                    else:
                        fixed_prefs.append(pref)

            if fixed_prefs != data["preferences"]:
                data["preferences"] = fixed_prefs

        # 保存迁移后的数据
        self.data = data
        self._save()

        return data

    def _init_data(self) -> Dict[str, Any]:
        """初始化数据结构"""
        return {
            "user_id": self.user_id,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "preferences": [],  # 偏好列表: [{"type": "home_location", "value": "天津"}, ...]
            "chat_history": [],  # 所有聊天记录（跨会话）
            "trip_history": [],  # 所有行程记录
            "statistics": {
                "total_trips": 0,
                "total_messages": 0,
                "frequent_destinations": {}
            }
        }

    def _save(self):
        """Persist profile and trips independently; never rewrite legacy JSON."""
        self.data["updated_at"] = datetime.now().isoformat()
        profile = {key: value for key, value in self.data.items() if key not in {"chat_history", "trip_history"}}
        atomic_json_write(self.profile_path, profile)
        atomic_json_write(self.trips_path, {"trip_history": self.data["trip_history"]})

    def save_preference(self, pref_type: str, value: Any, *, source: str = "agent"):
        """
        保存用户偏好（列表格式）

        Args:
            pref_type: 偏好类型
            value: 偏好值
        """
        # 查找是否已存在该类型的偏好
        preferences = self.data["preferences"]
        found = False

        for pref in preferences:
            if pref.get("type") == pref_type:
                if pref.get("source") == "user" and source != "user":
                    return False
                pref["value"] = value
                pref["source"] = source
                pref.pop("deleted", None)
                found = True
                break

        # 如果不存在，添加新的偏好
        if not found:
            preferences.append({"type": pref_type, "value": value, "source": source})

        self._save()
        logger.info(f"Saved preference: {pref_type} = {value}")
        return True

    def set_user_preference(self, pref_type: str, value: Any) -> None:
        """A user's explicit choice takes precedence over agent extraction."""
        self.save_preference(pref_type, value, source="user")

    def delete_user_preference(self, pref_type: str) -> None:
        """Keep a user-owned tombstone so agents cannot silently restore it."""
        self.save_preference(pref_type, None, source="user")
        for pref in self.data["preferences"]:
            if pref.get("type") == pref_type:
                pref["deleted"] = True
                break
        self._save()

    def get_preference(self, pref_type: str = None) -> Any:
        """
        获取用户偏好

        Args:
            pref_type: 偏好类型，None返回字典格式的全部偏好

        Returns:
            偏好值或偏好字典
        """
        preferences = self.data["preferences"]

        if pref_type is None:
            # 返回字典格式，方便调用方使用
            result = {}
            for pref in preferences:
                if not pref.get("deleted"):
                    result[pref.get("type")] = pref.get("value")
            return result
        else:
            # 查找特定类型的偏好
            for pref in preferences:
                if pref.get("type") == pref_type and not pref.get("deleted"):
                    return pref.get("value")
            return None

    def add_hotel_brand(self, brand: str):
        """添加酒店品牌偏好（追加到列表）"""
        current = self.get_preference("hotel_brands")
        brands = list(current) if isinstance(current, list) else ([current] if current else [])
        if brand not in brands:
            brands.append(brand)
        return self.save_preference("hotel_brands", brands)

    def add_airline(self, airline: str):
        """添加航空公司偏好（追加到列表）"""
        current = self.get_preference("airlines")
        airlines = list(current) if isinstance(current, list) else ([current] if current else [])
        if airline not in airlines:
            airlines.append(airline)
        return self.save_preference("airlines", airlines)

    def add_chat_message(self, role: str, content: str, session_id: str = None):
        """
        添加聊天消息到长期记忆

        Args:
            role: 角色 (user/assistant)
            content: 消息内容
            session_id: 会话ID（可选）
        """
        store = SessionStore(self.storage_path, self.user_id, session_id or "legacy")
        previous = store.read_events()
        latest_user = next((event for event in reversed(previous) if event.get("scope") == "session" and event.get("role") == "user"), None)
        turn_id = latest_user["turn_id"] if role == "assistant" and latest_user else uuid.uuid4().hex
        store.append({"turn_id": turn_id, "scope": "session", "type": "message", "role": role, "content": content, "final": role == "assistant"})
        self.data["statistics"]["total_messages"] = len(self.get_chat_history())
        self._save()
        logger.debug(f"Added chat message to long-term memory: {role}")

    def get_chat_history(self, limit: int = None, session_id: str = None) -> List[Dict[str, Any]]:
        """
        获取聊天历史

        Args:
            limit: 返回数量限制
            session_id: 会话ID（只返回特定会话的消息）

        Returns:
            消息列表
        """
        sessions_dir = self.user_dir / "sessions"
        if not sessions_dir.exists():
            return []
        session_dirs = [sessions_dir / storage_component(session_id)] if session_id else list(sessions_dir.iterdir())
        messages = []
        for directory in session_dirs:
            if not directory.is_dir():
                continue
            for event in SessionStore(self.storage_path, self.user_id, directory.name).read_events():
                if event.get("type") == "message" and event.get("scope") == "session" and event.get("role") in {"user", "assistant"}:
                    messages.append({"role": event["role"], "content": event.get("content", ""), "timestamp": event.get("timestamp", ""), "session_id": directory.name})
        messages.sort(key=lambda item: item["timestamp"])
        return messages[-limit:] if limit else messages

    def save_trip_history(self, trip_info: Dict[str, Any]):
        """
        保存行程历史

        Args:
            trip_info: 行程信息
        """
        trip_record = {
            "trip_id": f"trip_{len(self.data['trip_history']) + 1}",
            "timestamp": datetime.now().isoformat(),
            **trip_info
        }

        self.data["trip_history"].append(trip_record)

        # 更新统计信息
        self.data["statistics"]["total_trips"] += 1

        # 更新常去目的地统计
        destination = trip_info.get("destination")
        if destination:
            freq = self.data["statistics"]["frequent_destinations"]
            freq[destination] = freq.get(destination, 0) + 1

        self._save()
        logger.info(f"Saved trip history: {trip_record['trip_id']}")

    def get_trip_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        获取历史行程

        Args:
            limit: 返回数量限制

        Returns:
            行程列表
        """
        return self.data["trip_history"][-limit:] if limit else self.data["trip_history"]

    def get_frequent_destinations(self, top_n: int = 5) -> List[tuple]:
        """
        获取常去目的地

        Args:
            top_n: 返回前N个

        Returns:
            [(destination, count), ...]
        """
        freq = self.data["statistics"]["frequent_destinations"]
        sorted_dest = sorted(freq.items(), key=lambda x: x[1], reverse=True)
        return sorted_dest[:top_n]

    def increment_query_count(self):
        """增加查询计数"""
        self.data["statistics"]["total_queries"] = self.data["statistics"].get("total_queries", 0) + 1
        self._save()

    def get_statistics(self) -> Dict[str, Any]:
        """获取统计信息"""
        statistics = self.data["statistics"].copy()
        statistics["total_messages"] = len(self.get_chat_history())
        return statistics

    def clear_history(self):
        """清空历史记录（保留偏好）"""
        sessions_dir = self.user_dir / "sessions"
        if sessions_dir.exists():
            shutil.rmtree(sessions_dir)
        self.data["legacy_chat_cleared"] = True
        self.data["chat_history"] = []
        self.data["trip_history"] = []
        self.data["statistics"]["total_trips"] = 0
        self.data["statistics"]["total_messages"] = 0
        self.data["statistics"]["frequent_destinations"] = {}
        self._save()
        logger.info("Cleared all history (chat + trips)")

    def delete_all(self):
        """删除所有数据（包括文件）"""
        if self.user_dir.exists():
            shutil.rmtree(self.user_dir)
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
            logger.warning(f"Deleted long-term memory file: {self.db_path}")
