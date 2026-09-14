"""
QQ群文件管理插件 - 完整版
支持：文件列表、文件夹列表、查看文件夹内容、关键词搜索文件、重命名文件/文件夹、创建文件夹、删除文件夹、删除文件、移动文件、下载文件
权限管理：按功能维度配置禁用群组，支持 all 全局禁用
调试模式：布尔开关，开启时显示详细日志
兼容性：自动适配 napcat 和 llonebot 的移动/重命名等文件 API（自适应探测）
"""

import os
import json
import aiohttp
import aiofiles
import asyncio
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime

from core.plugin import BasePlugin, logger, register_tool as tool
from core.chat.message_utils import KiraMessageBatchEvent


class QQFileManager(BasePlugin):
    """QQ群文件管理插件"""

    # 搜索文件时单次向协议端拉取的文件数量（对应 get_group_*files 的 file_count 参数）
    SEARCH_FILE_COUNT = 200

    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        self.download_history = {}
        self.history_file = None
        self.qq_adapter = None
        self.download_timeout = 60
        self.pending_downloads = {}
        self._folder_cache = {}
        self.debug_mode = False
        # 合并配置：schema 折叠分组（section_xxx）内的字段优先，旧版扁平配置兜底
        self._cfg = self._build_merged_cfg()
        # 权限模式: "deny_list"(黑名单，默认) / "allow_list"(白名单)
        self.permission_mode = self._cfg.get("permission_mode", "deny_list")
        self.enabled_groups = []
        # API 提供者: "auto" (自动探测), "napcat", "llonebot"
        self.api_provider = self._cfg.get("api_provider", "auto")
        self._api_provider_detected = False  # 是否已探测成功

    def _build_merged_cfg(self) -> dict:
        """合并折叠分组（section）嵌套配置与旧版扁平配置，section 优先，保证旧配置平滑兼容"""
        merged = dict(self.plugin_cfg or {})
        for key, value in (self.plugin_cfg or {}).items():
            if not key.startswith("section_") or not isinstance(value, dict):
                continue
            fields = value.get("fields") if isinstance(value.get("fields"), dict) else value
            for fk, fv in fields.items():
                if fk != "fields" and fv is not None:
                    merged[fk] = fv
        return merged

    async def initialize(self):
        """插件初始化"""
        self.qq_adapter = self._get_qq_adapter()
        if not self.qq_adapter:
            logger.warning("[QQFileManager] QQ适配器未找到，插件功能将不可用")
            return

        # 读取调试模式配置（布尔开关）
        self.debug_mode = self._cfg.get("debug_mode", False)
        if self.debug_mode:
            logger.info("[QQFileManager] 🔧 调试模式已开启，将显示详细日志")

        # 权限模式与全局白名单
        mode = str(self._cfg.get("permission_mode", "deny_list") or "deny_list").strip().lower()
        self.permission_mode = mode if mode in ("allow_list", "deny_list") else "deny_list"
        raw_enabled = self._cfg.get("enabled_groups", []) or []
        self.enabled_groups = [str(g).strip() for g in raw_enabled if str(g).strip()]
        if self.permission_mode == "allow_list":
            if self.enabled_groups:
                self._log_info(f"白名单模式已启用，允许使用群文件功能的群: {', '.join(self.enabled_groups)}")
            else:
                logger.warning("[QQFileManager] 白名单模式已启用但未配置允许的群号，所有群的群文件功能均被禁用")

        # 设置下载路径（仅允许相对路径，防止越权访问）
        download_path = str(self._cfg.get("download_path", "files") or "files").strip().strip("/\\")
        if not download_path or ".." in download_path or ":" in download_path:
            logger.warning("[QQFileManager] download_path 配置非法（仅支持相对路径），已回退为 'files'")
            download_path = "files"
        project_root = Path(__file__).parent.parent.parent.parent
        data_dir = project_root / "data"
        self.download_dir = data_dir / download_path
        self.download_dir.mkdir(parents=True, exist_ok=True)

        self._log_info(f"下载目录: {self.download_dir}")

        self.history_file = self.ctx.get_plugin_data_dir() / "download_history.json"
        if self.history_file.exists():
            try:
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    self.download_history = json.load(f)
            except Exception as e:
                logger.error(f"[QQFileManager] 加载下载历史失败: {e}")

        # 允许下载的扩展名白名单（统一去空格/去点/小写，空列表 = 允许所有类型）
        raw_exts = self._cfg.get("allowed_extensions", []) or []
        self.allowed_extensions = sorted({str(e).strip().lstrip('.').lower() for e in raw_exts if str(e).strip()})

        # 数值型配置的类型容错
        try:
            self.max_file_size = max(0, int(self._cfg.get("max_file_size_mb", 100))) * 1024 * 1024
        except (TypeError, ValueError):
            self.max_file_size = 100 * 1024 * 1024
        try:
            self.max_files_list = max(1, int(self._cfg.get("max_files_list", 20)))
        except (TypeError, ValueError):
            self.max_files_list = 20
        try:
            self.download_timeout = max(1, int(self._cfg.get("download_timeout", 60)))
        except (TypeError, ValueError):
            self.download_timeout = 60

        if self.allowed_extensions:
            self._log_info(f"下载扩展名白名单: {', '.join(self.allowed_extensions)}")
        if self.max_file_size:
            self._log_info(f"下载大小上限: {self.max_file_size // (1024 * 1024)}MB")

        if self.api_provider != "auto":
            self._log_info(f"使用手动配置的 API 提供者: {self.api_provider}")
        else:
            self._log_info("API 提供者设为自动探测，首次移动文件时将自动识别 napcat 或 llonebot")

        self._log_info("✅ 初始化完成")

    async def terminate(self):
        """插件终止"""
        if self.history_file:
            try:
                with open(self.history_file, 'w', encoding='utf-8') as f:
                    json.dump(self.download_history, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"[QQFileManager] 保存下载历史失败: {e}")
        self._log_info("👋 已终止")

    def _log_debug(self, msg: str):
        """调试日志（仅在调试模式下输出）"""
        if self.debug_mode:
            logger.info(f"[QQFileManager] 🔍 {msg}")

    def _log_info(self, msg: str):
        """信息日志（始终输出）"""
        logger.info(f"[QQFileManager] {msg}")

    def _log_error(self, msg: str):
        """错误日志（始终输出）"""
        logger.error(f"[QQFileManager] ❌ {msg}")

    def _get_qq_adapter(self):
        """获取QQ Adapter实例"""
        try:
            adapters = {}
            if hasattr(self.ctx.adapter_mgr, 'adapters'):
                adapters = self.ctx.adapter_mgr.adapters
            elif hasattr(self.ctx.adapter_mgr, '_adapters'):
                adapters = self.ctx.adapter_mgr._adapters

            for name, adapter in adapters.items():
                if adapter.__class__.__name__ == "QQAdapter":
                    self._log_debug(f"找到QQ适配器: {name}")
                    return adapter

            for attr_name in dir(self.ctx.adapter_mgr):
                attr_val = getattr(self.ctx.adapter_mgr, attr_name)
                if hasattr(attr_val, '__class__') and attr_val.__class__.__name__ == "QQAdapter":
                    self._log_debug(f"通过属性找到QQ适配器: {attr_name}")
                    return attr_val
        except Exception as e:
            self._log_error(f"获取QQ适配器时出错: {e}")
        return None

    def _get_group_id_from_event(self, event: KiraMessageBatchEvent) -> Optional[str]:
        if not event.messages:
            return None
        for msg in event.messages:
            if msg.group and msg.group.group_id:
                return str(msg.group.group_id)
        return None

    def _get_session_id_from_event(self, event: KiraMessageBatchEvent) -> Optional[str]:
        """从事件中获取会话ID"""
        if not event.messages:
            return None
        for msg in event.messages:
            if msg.group and msg.group.group_id:
                return f"qq:gm:{msg.group.group_id}"
            elif msg.sender and msg.sender.user_id:
                return f"qq:dm:{msg.sender.user_id}"
        return None

    def _is_feature_disabled(self, group_id: str, feature: str) -> bool:
        """
        检查指定群组的某个功能是否被禁用
        feature: create_folder, delete_folder, delete_file, move_file, download_file, list_files,
                 rename_file, rename_folder
        """
        config_key = f"disabled_{feature}"
        raw_list = self._cfg.get(config_key, []) or []
        # 统一转为字符串并去空格，兼容配置中被存为数字类型的群号
        disabled_list = [str(g).strip() for g in raw_list if str(g).strip()]

        if not disabled_list:
            return False

        if "all" in disabled_list:
            return True

        return str(group_id).strip() in disabled_list

    def _check_feature_permission(self, group_id: str, feature: str) -> tuple[bool, str]:
        """检查群组是否有特定功能的权限（先全局白名单，再单功能黑名单）"""
        gid = str(group_id).strip()

        # 全局白名单模式：仅 enabled_groups 中的群可用（留空 = 全部禁用）
        if self.permission_mode == "allow_list":
            if not self.enabled_groups:
                return False, "群文件插件当前为白名单模式且未配置允许的群号，所有群均已禁用，请联系管理员在插件配置中填写"
            if gid not in self.enabled_groups:
                return False, f"本群（{gid}）不在群文件功能的允许名单中，无法使用该功能"

        if self._is_feature_disabled(gid, feature):
            feature_names = {
                "create_folder": "创建文件夹",
                "delete_folder": "删除文件夹",
                "delete_file": "删除文件",
                "move_file": "移动文件",
                "download_file": "下载文件",
                "list_files": "查看文件列表",
                "rename_file": "重命名文件",
                "rename_folder": "重命名文件夹"
            }
            return False, f"{feature_names.get(feature, feature)}功能已在当前群禁用"
        return True, ""

    async def _get_group_files_and_folders(self, group_id: str, file_count: int = None) -> tuple[List[Dict], List[Dict]]:
        """获取根目录文件和文件夹"""
        if not self.qq_adapter:
            return [], []
        try:
            bot = self.qq_adapter.get_client()
            params = {"group_id": group_id}
            if file_count:
                params["file_count"] = file_count
            result = await bot.send_action(
                "get_group_root_files",
                params
            )
            if not result:
                return [], []

            files, folders = [], []
            if isinstance(result, dict):
                if result.get("status") == "failed":
                    return [], []
                data = result.get("data", {})
                if isinstance(data, dict):
                    files = data.get("files", [])
                    folders = data.get("folders", [])
                elif isinstance(data, list):
                    files = data
            self._log_debug(f"获取到 {len(files)} 个文件, {len(folders)} 个文件夹")
            return files, folders
        except Exception as e:
            self._log_error(f"获取列表异常: {e}")
            return [], []

    async def _get_folder_files(self, group_id: str, folder_id: str, retry: int = 2, file_count: int = None) -> List[Dict]:
        """获取文件夹内的文件，支持重试和缓存"""
        if not self.qq_adapter:
            return []
        
        clean_folder_id = folder_id.lstrip('/') if folder_id else folder_id
        cache_key = f"folder_files_{group_id}_{clean_folder_id}_{file_count or 'default'}"
        current_time = datetime.now().timestamp()
        
        if cache_key in self._folder_cache:
            cache_time, cache_files = self._folder_cache[cache_key]
            if current_time - cache_time < 5:
                self._log_debug(f"使用缓存: 文件夹 {folder_id} 内有 {len(cache_files)} 个文件")
                return cache_files
        
        for attempt in range(retry + 1):
            try:
                bot = self.qq_adapter.get_client()
                params = {
                    "group_id": group_id,
                    "folder_id": clean_folder_id
                }
                if file_count:
                    params["file_count"] = file_count
                result = await bot.send_action(
                    "get_group_files_by_folder",
                    params
                )
                
                if not result:
                    if attempt < retry:
                        self._log_debug(f"获取文件夹内容失败(尝试 {attempt + 1}/{retry + 1})，重试中...")
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                    return []

                files = []
                if isinstance(result, dict):
                    if result.get("status") == "failed":
                        if attempt < retry:
                            self._log_debug(f"API返回失败(尝试 {attempt + 1}/{retry + 1})，重试中...")
                            await asyncio.sleep(0.5 * (attempt + 1))
                            continue
                        return []
                    data = result.get("data", {})
                    if isinstance(data, dict):
                        files = data.get("files", [])
                    elif isinstance(data, list):
                        files = data
                
                self._folder_cache[cache_key] = (current_time, files)
                self._log_debug(f"文件夹 {folder_id} 内有 {len(files)} 个文件")
                return files
                
            except Exception as e:
                self._log_error(f"获取文件夹内容异常: {e}")
                if attempt < retry:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                return []
        
        return []

    async def _get_folder_id_by_name(self, group_id: str, folder_name: str) -> Optional[str]:
        _, folders = await self._get_group_files_and_folders(group_id)
        for folder in folders:
            name = folder.get("folder_name", folder.get("name", ""))
            if name == folder_name:
                folder_id = folder.get("folder_id", folder.get("id", ""))
                self._log_debug(f"找到文件夹: {folder_name}, ID: {folder_id}")
                return folder_id
        return None

    async def _get_file_info_from_folder(self, group_id: str, file_name: str, folder_id: str = None) -> Optional[Dict]:
        """从指定文件夹获取文件信息"""
        try:
            if folder_id:
                files = await self._get_folder_files(group_id, folder_id)
            else:
                files, _ = await self._get_group_files_and_folders(group_id)

            self._log_debug(f"共获取到 {len(files)} 个文件")
            
            for f in files:
                name = f.get("file_name", f.get("name", ""))
                if name == file_name:
                    self._log_debug(f"找到匹配文件: {name}")
                    return {
                        "file_id": f.get("file_id", f.get("id", "")),
                        "file_uuid": f.get("file_uuid", f.get("file_id", "")),
                        "busid": f.get("busid", 0),
                        "current_folder_id": folder_id or "/",
                        "file_name": name,
                        "size": f.get("file_size", f.get("size", 0)),
                    }
            return None
        except Exception as e:
            self._log_error(f"获取文件信息异常: {e}")
            return None

    async def _find_file_in_all_folders(self, group_id: str, file_name: str) -> Optional[Dict]:
        """递归搜索所有文件夹，查找文件"""
        self._log_debug(f"开始搜索文件: {file_name}")
        
        file_info = await self._get_file_info_from_folder(group_id, file_name, None)
        if file_info:
            self._log_debug(f"在根目录找到文件: {file_name}")
            return file_info

        _, folders = await self._get_group_files_and_folders(group_id)
        self._log_debug(f"找到 {len(folders)} 个文件夹，开始搜索...")

        for folder in folders:
            folder_name_display = folder.get("folder_name", folder.get("name", "未知"))
            folder_id = folder.get("folder_id", folder.get("id", ""))
            if not folder_id:
                continue

            self._log_debug(f"搜索文件夹: {folder_name_display} (ID: {folder_id})")
            
            file_info = await self._get_file_info_from_folder(group_id, file_name, folder_id)
            if file_info:
                self._log_debug(f"在文件夹 {folder_name_display} 中找到文件: {file_name}")
                file_info["current_folder_id"] = folder_id
                return file_info

        self._log_debug(f"未找到文件: {file_name}")
        return None

    async def _find_file_matches_all(self, group_id: str, file_name: str) -> List[Dict]:
        """
        查找群内所有与文件名完全一致的文件（根目录 + 所有一级文件夹），附带位置信息。
        用于删除/重命名前的精确定位与同名歧义校验，避免连坐误删。
        """
        matches: List[Dict] = []
        root_files, folders = await self._get_group_files_and_folders(group_id)

        def _scan(files: List[Dict], location: str, folder_id: str):
            for f in files:
                name = f.get("file_name", f.get("name", ""))
                if name == file_name:
                    matches.append({
                        "file_id": f.get("file_id", f.get("id", "")),
                        "file_uuid": f.get("file_uuid", f.get("file_id", "")),
                        "busid": f.get("busid", 0),
                        "location": location,
                        "folder_id": folder_id or "/",
                    })

        _scan(root_files, "根目录", "/")
        for folder in folders:
            fid = folder.get("folder_id") or folder.get("id") or folder.get("folder") or ""
            if not fid:
                continue
            fname = folder.get("folder_name") or folder.get("name") or fid
            fs = await self._get_folder_files(group_id, fid)
            _scan(fs, fname, fid)

        self._log_debug(f"精确查找「{file_name}」: 命中 {len(matches)} 处")
        return matches

    async def _get_folder_name_by_id(self, group_id: str, folder_id: str) -> Optional[str]:
        """根据文件夹ID获取文件夹名称（兼容ID带不带 '/' 前缀两种形式）"""
        target = (folder_id or "").lstrip('/')
        _, folders = await self._get_group_files_and_folders(group_id)
        for folder in folders:
            fid = folder.get("folder_id", folder.get("id", ""))
            if fid == folder_id or fid.lstrip('/') == target:
                return folder.get("folder_name", folder.get("name", ""))
        return None

    async def _collect_group_files(self, group_id: str, folder_id: str = None) -> tuple[List[Dict], str]:
        """
        收集群文件并附带位置信息（每个文件 dict 增加 _location 和 _location_folder_id 字段）。
        folder_id 为 None：收集根目录及所有一级文件夹内的文件（顺序逐个拉取）。
        folder_id 为 "/"：仅收集根目录。
        其他 folder_id：仅收集该文件夹内的文件。
        返回 (文件列表, 搜索范围名称)。
        """
        if not self.qq_adapter:
            return [], ""

        count = self.SEARCH_FILE_COUNT

        # 限定根目录
        if folder_id == "/":
            files, _ = await self._get_group_files_and_folders(group_id, file_count=count)
            tagged = [{**f, "_location": "根目录", "_location_folder_id": "/"} for f in files]
            self._log_debug(f"收集根目录文件 {len(tagged)} 个（用于搜索）")
            return tagged, "根目录"

        # 限定某个文件夹（folder_id 带不带 '/' 前缀均可，内部调用会自行清洗）
        if folder_id:
            folder_name = await self._get_folder_name_by_id(group_id, folder_id)
            scope = folder_name or folder_id
            files = await self._get_folder_files(group_id, folder_id, file_count=count)
            tagged = [{**f, "_location": scope, "_location_folder_id": folder_id} for f in files]
            self._log_debug(f"收集文件夹 {scope} 内文件 {len(tagged)} 个（用于搜索）")
            return tagged, f"文件夹「{scope}」"

        # 全量收集：根目录 + 顺序遍历所有一级文件夹。
        # 注意：必须逐个顺序请求，不能并发——NapCat/LLOneBot 对群文件接口的瞬时
        # 并发请求会失败/限流，导致各文件夹全部静默返回空（与 _find_file_in_all_folders 保持一致）。
        root_files, folders = await self._get_group_files_and_folders(group_id, file_count=count)
        collected = [{**f, "_location": "根目录", "_location_folder_id": "/"} for f in root_files]

        for index, folder in enumerate(folders, 1):
            # 兼容不同实现的字段：folder_id / id / folder
            fid = folder.get("folder_id") or folder.get("id") or folder.get("folder") or ""
            if not fid:
                self._log_debug(f"跳过第 {index} 个文件夹：未能解析文件夹ID，原始数据={folder}")
                continue
            fname = folder.get("folder_name") or folder.get("name") or fid

            fs = await self._get_folder_files(group_id, fid, file_count=count)
            if fs:
                collected.extend({**f, "_location": fname, "_location_folder_id": fid} for f in fs)
            elif int(folder.get("total_file_count", 0) or 0) > 0:
                # 文件夹声称有文件却拉取为空，通常是请求失败/被限流而非真空文件夹
                self._log_error(f"文件夹 {fname}（ID: {fid}）应有 {folder.get('total_file_count')} 个文件，但拉取结果为空，可能被限流")
            self._log_debug(f"遍历文件夹 {index}/{len(folders)}：{fname}（ID: {fid}）-> {len(fs)} 个文件")

        self._log_debug(f"全量收集完成：{len(folders)} 个文件夹，根目录 {len(root_files)} 个，共 {len(collected)} 个文件（用于搜索）")
        return collected, "根目录及全部文件夹"

    @staticmethod
    def _match_files_by_keyword(files: List[Dict], keyword: str) -> List[Dict]:
        """按文件名关键词做不区分大小写的模糊匹配，并按相关度排序"""
        kw = keyword.strip().lower()
        matched = []
        for f in files:
            name = f.get("file_name", f.get("name", ""))
            if not name:
                continue
            name_lower = name.lower()
            pos = name_lower.find(kw)
            if pos != -1:
                # 完全相同优先，其次按关键词出现位置，再次按文件名字典序
                matched.append((0 if name_lower == kw else 1, pos, name_lower, f))
        matched.sort(key=lambda item: (item[0], item[1], item[2]))
        return [item[3] for item in matched]

    # ========== API 方法 ==========

    async def _create_folder_api(self, group_id: str, folder_name: str) -> Optional[str]:
        """创建群文件夹（兼容 NapCat 和 LLOneBot）"""
        if not self.qq_adapter:
            return None
        try:
            bot = self.qq_adapter.get_client()
            # 同时传递 folder_name 和 name，确保两种实现都能识别
            result = await bot.send_action(
                "create_group_file_folder",
                {
                    "group_id": group_id,
                    "folder_name": folder_name,  # 供 NapCat / 标准 OneBot 使用
                    "name": folder_name          # 供 LLOneBot 使用（实际要求 name）
                }
            )

            if not result:
                return None

            if isinstance(result, dict):
                if result.get("status") == "ok":
                    data = result.get("data", {})
                    folder_id = data.get("folder_id") or data.get("id")
                    if folder_id:
                        self._log_debug(f"创建文件夹成功: {folder_name}, ID: {folder_id}")
                        return folder_id
                    self._log_debug(f"创建文件夹成功（未返回ID）: {folder_name}")
                    return "success"
                elif result.get("status") == "failed":
                    msg = result.get("message", "")
                    if "已存在" in msg or "exists" in msg.lower():
                        self._log_debug(f"文件夹已存在: {folder_name}")
                        return "exists"
                    self._log_error(f"创建文件夹失败: {result}")
                    return None
            return "success"
        except Exception as e:
            self._log_error(f"创建文件夹异常: {e}")
            return None

    async def _delete_folder_api(self, group_id: str, folder_id: str) -> bool:
        """删除群文件夹"""
        if not self.qq_adapter:
            return False
        try:
            bot = self.qq_adapter.get_client()
            result = await bot.send_action(
                "delete_group_folder",
                {"group_id": group_id, "folder_id": folder_id}
            )
            if not result:
                return False
            if isinstance(result, dict):
                success = result.get("status") == "ok"
                if success:
                    self._log_debug(f"删除文件夹成功: {folder_id}")
                else:
                    self._log_error(f"删除文件夹失败: {result}")
                return success
            return False
        except Exception as e:
            self._log_error(f"删除文件夹异常: {e}")
            return False

    async def _delete_file_api(self, group_id: str, file_id: str) -> bool:
        """删除群文件"""
        if not self.qq_adapter:
            return False
        try:
            bot = self.qq_adapter.get_client()
            result = await bot.send_action(
                "delete_group_file",
                {"group_id": group_id, "file_id": file_id}
            )
            if not result:
                return False
            if isinstance(result, dict):
                success = result.get("status") == "ok"
                if success:
                    self._log_debug(f"删除文件成功: {file_id}")
                else:
                    self._log_error(f"删除文件失败: {result}")
                return success
            return False
        except Exception as e:
            self._log_error(f"删除文件异常: {e}")
            return False

    async def _rename_file_api(self, group_id: str, file_id: str,
                               current_parent_directory: str, new_name: str) -> tuple[bool, str]:
        """重命名群文件（NapCat: rename_group_file，需父目录；其他协议端自动探测参数变体）"""
        if not self.qq_adapter:
            return False, "QQ适配器未就绪"

        clean_dir = "/" if not current_parent_directory or current_parent_directory == "/" else current_parent_directory.lstrip('/')

        attempts = [
            ("rename_group_file", {
                "group_id": int(group_id),
                "file_id": file_id,
                "current_parent_directory": clean_dir,
                "new_name": new_name
            }),
            ("rename_group_file", {
                "group_id": int(group_id),
                "file_id": file_id,
                "current_parent_directory": clean_dir,
                "new_file_name": new_name  # 参数名变体兼容
            }),
        ]
        return await self._probe_rename_actions(attempts, f"文件 {file_id}")

    async def _rename_folder_api(self, group_id: str, folder_id: str, new_folder_name: str) -> tuple[bool, str]:
        """重命名群文件夹（LLOneBot: rename_group_folder；其他协议端自动探测）"""
        if not self.qq_adapter:
            return False, "QQ适配器未就绪"

        attempts = [
            ("rename_group_folder", {
                "group_id": int(group_id),
                "folder_id": folder_id,
                "new_folder_name": new_folder_name
            }),
            ("rename_group_folder", {
                "group_id": int(group_id),
                "folder_id": folder_id,
                "name": new_folder_name  # 参数名变体兼容
            }),
        ]
        return await self._probe_rename_actions(attempts, f"文件夹 {folder_id}")

    async def _probe_rename_actions(self, attempts: List[tuple], target_desc: str) -> tuple[bool, str]:
        """依次尝试重命名接口的多种参数格式，全部失败时返回最后一次的错误信息"""
        bot = self.qq_adapter.get_client()
        last_msg = ""
        for action, params in attempts:
            try:
                self._log_debug(f"尝试重命名接口: {action} {params}")
                result = await bot.send_action(action, params)
                if not result:
                    last_msg = "API返回为空"
                    continue
                if isinstance(result, dict):
                    if result.get("status") == "ok":
                        self._log_debug(f"重命名成功: {target_desc}")
                        return True, ""
                    last_msg = result.get("message", "") or "未知失败原因"
                else:
                    last_msg = "未知响应格式"
            except Exception as e:
                last_msg = str(e)
        if last_msg:
            return False, f"{last_msg}（当前协议端可能不支持该重命名接口，NapCat 支持文件重命名，LLOneBot 支持文件夹重命名）"
        return False, "当前协议端可能不支持该重命名接口"


    async def _move_file_to_folder(self, group_id: str, file_uuid: str, current_folder_id: str, target_folder_id: str, file_name: str = "") -> tuple[bool, str]:
        """移动文件到指定文件夹（支持内存记录 + 自动回退）"""
        if not self.qq_adapter:
            return False, "QQ适配器未就绪"

        clean_current = "/" if not current_folder_id or current_folder_id == "/" else current_folder_id.lstrip('/')
        clean_target = "/" if not target_folder_id or target_folder_id == "/" else target_folder_id.lstrip('/')

        # 定义两种格式的参数构造器
        def build_params(provider: str):
            if provider == "llonebot":
                return {
                    "group_id": int(group_id),
                    "file_id": file_uuid,
                    "parent_directory": clean_current,
                    "target_directory": clean_target
                }
            else:  # napcat
                return {
                    "group_id": int(group_id),
                    "file_id": file_uuid,
                    "current_parent_directory": clean_current,
                    "target_parent_directory": clean_target
                }

        async def try_move(provider: str) -> tuple[bool, str]:
            self._log_debug(f"尝试 {provider} 格式移动文件: {file_name}")
            try:
                bot = self.qq_adapter.get_client()
                result = await bot.send_action("move_group_file", build_params(provider))
                if not result:
                    return False, "API返回为空"
                if isinstance(result, dict):
                    if result.get("status") == "ok":
                        return True, ""
                    elif result.get("status") == "failed":
                        msg = result.get("message", "")
                        return False, msg
                return False, "未知响应格式"
            except Exception as e:
                return False, str(e)

        # 确定尝试顺序
        if self.api_provider == "auto":
            # 未探测过，按顺序尝试 napcat -> llonebot
            providers = ["napcat", "llonebot"]
        else:
            # 已记录 provider，先尝试记录的，如果失败且错误特征符合则回退另一种
            providers = [self.api_provider]
            # 回退时的另一种
            fallback = "llonebot" if self.api_provider == "napcat" else "napcat"

        # 第一次尝试
        success, msg = await try_move(providers[0])
        if success:
            # 成功，如果之前是 auto 则记录
            if self.api_provider == "auto":
                self.api_provider = providers[0]
                self._log_info(f"自适应检测到 API 提供者: {self.api_provider}")
            return True, ""

        # 第一次失败，检查是否需要回退
        need_fallback = False
        if self.api_provider != "auto":
            # 已记录的 provider 失败，检查错误特征是否是格式不匹配
            if "parent_directory missing required value" in msg or "target_directory" in msg.lower():
                need_fallback = True
                self._log_debug(f"当前 provider ({self.api_provider}) 格式失败，尝试回退到另一种格式")
        else:
            # 自动模式第一次失败，尝试第二种
            need_fallback = True

        if need_fallback:
            fallback_provider = providers[1] if len(providers) > 1 else fallback
            success2, msg2 = await try_move(fallback_provider)
            if success2:
                # 回退成功，更新内存中的 provider
                self.api_provider = fallback_provider
                self._log_info(f"自适应切换 API 提供者: {fallback_provider}")
                return True, ""
            else:
                return False, msg2

        return False, msg

    async def _send_notification(self, session_id: str, file_name: str):
        """发送下载完成通知"""
        try:
            from core.chat import MessageChain
            from core.chat.message_elements import Text
            
            notification_text = f"✅ 文件「{file_name}」已下载完成，随时可以查看哦~"
            message_chain = MessageChain([Text(notification_text)])
            await self.ctx.publish_notice(session_id, message_chain, is_mentioned=True)
            self._log_debug(f"已发送下载完成通知: {file_name} -> {session_id}")
        except Exception as e:
            self._log_error(f"发送通知失败: {e}")

    async def _download_file_async(self, group_id: str, file_name: str, task_id: str, session_id: str = None):
        """异步下载文件"""
        try:
            file_info = await self._find_file_in_all_folders(group_id, file_name)
            if not file_info or not file_info.get("file_id"):
                self.pending_downloads[task_id] = {"status": "failed", "error": f"未找到文件: {file_name}"}
                return

            current_file_id = file_info["file_id"]
            bot = self.qq_adapter.get_client()
            result = await bot.send_action(
                "get_group_file_url",
                {"group_id": group_id, "file_id": current_file_id}
            )

            if not result:
                self.pending_downloads[task_id] = {"status": "failed", "error": "无法获取下载链接"}
                return

            if isinstance(result, dict):
                if result.get("status") == "failed":
                    msg = result.get("message", "")
                    self.pending_downloads[task_id] = {"status": "failed", "error": msg[:200]}
                    return

                data = result.get("data", {})
                download_url = data.get("url") if isinstance(data, dict) else None
                if not download_url:
                    self.pending_downloads[task_id] = {"status": "failed", "error": "未获取到下载链接"}
                    return

            safe_name = self._sanitize_filename(file_name)
            save_path = self.download_dir / safe_name
            if save_path.exists():
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                name, ext = os.path.splitext(safe_name)
                save_path = self.download_dir / f"{name}_{timestamp}{ext}"

            self._log_debug(f"开始下载文件: {file_name} -> {save_path}")

            timeout = aiohttp.ClientTimeout(total=self.download_timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(download_url) as response:
                    if response.status != 200:
                        self.pending_downloads[task_id] = {"status": "failed", "error": f"HTTP {response.status}"}
                        return
                    # 按实际响应大小二次校验（兜底文件列表中大小缺失的情况）
                    if self.max_file_size:
                        try:
                            content_length = int(response.headers.get("Content-Length", 0) or 0)
                        except (TypeError, ValueError):
                            content_length = 0
                        if content_length > self.max_file_size:
                            self._log_error(f"文件超出大小限制，取消下载: {file_name}")
                            self.pending_downloads[task_id] = {
                                "status": "failed",
                                "error": f"文件大小 {self._format_file_size(content_length)} 超过限制（{self.max_file_size // (1024 * 1024)}MB）"
                            }
                            return
                    async with aiofiles.open(save_path, 'wb') as f:
                        async for chunk in response.content.iter_chunked(8192):
                            await f.write(chunk)

            self.download_history[file_name] = {
                "group_id": group_id, "file_name": file_name,
                "local_path": str(save_path), "download_time": datetime.now().isoformat(),
                "file_size": save_path.stat().st_size
            }
            self.pending_downloads[task_id] = {
                "status": "success", "file_name": file_name,
                "save_path": str(save_path), "file_size": save_path.stat().st_size
            }
            
            self._log_debug(f"文件下载完成: {file_name}")
            
            if session_id:
                await self._send_notification(session_id, file_name)
                
        except asyncio.TimeoutError:
            self._log_error(f"下载超时: {file_name}")
            self.pending_downloads[task_id] = {"status": "failed", "error": f"下载超时 ({self.download_timeout}秒)"}
        except Exception as e:
            self._log_error(f"下载异常: {e}")
            self.pending_downloads[task_id] = {"status": "failed", "error": str(e)}

    async def _clear_cache(self, group_id: str):
        """清除指定群的缓存"""
        keys_to_delete = [k for k in self._folder_cache.keys() if k.startswith(f"folder_files_{group_id}")]
        for key in keys_to_delete:
            del self._folder_cache[key]
        self._log_debug(f"已清除群 {group_id} 的缓存")

    def _check_download_allowed(self, file_name: str, size: Any = None) -> Optional[str]:
        """
        下载前校验扩展名白名单与文件大小限制。
        返回错误信息（拒绝下载）或 None（允许）。
        size 为 0/未知时跳过大小预检，由下载中的 Content-Length 二次校验兜底。
        """
        # 扩展名白名单（列表非空时生效）
        if self.allowed_extensions:
            ext = os.path.splitext(file_name)[1].lstrip('.').lower()
            if ext not in self.allowed_extensions:
                return (f"❌ 文件类型「{ext or '无扩展名'}」不在允许下载的列表中"
                        f"（允许：{'、'.join(self.allowed_extensions)}）")
        # 大小限制（0 表示不限制）
        if self.max_file_size:
            try:
                size_int = int(size or 0)
            except (TypeError, ValueError):
                size_int = 0
            if size_int > self.max_file_size:
                return (f"❌ 文件大小 {self._format_file_size(size_int)} 超过允许下载的最大值"
                        f"（{self.max_file_size // (1024 * 1024)}MB）")
        return None

    @staticmethod
    def _sanitize_filename(filename: str) -> str:
        import re
        filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
        if len(filename) > 200:
            name, ext = os.path.splitext(filename)
            filename = name[:195] + ext
        return filename

    @staticmethod
    def _format_file_size(size_bytes: int) -> str:
        if size_bytes < 1024:
            return f"{size_bytes}B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f}KB"
        elif size_bytes < 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.1f}MB"
        else:
            return f"{size_bytes / (1024 * 1024 * 1024):.1f}GB"

    @staticmethod
    def _format_file_list(files: List[Dict], folders: List[Dict], max_items: int = 20) -> str:
        result = []
        if folders:
            result.append(f"📁 文件夹（{len(folders)}个）：\n")
            for i, f in enumerate(folders[:max_items], 1):
                name = f.get("folder_name", f.get("name", "未知文件夹"))
                fid = f.get("folder_id", f.get("id", ""))
                result.append(f"{i}. {name}\n   🆔 ID: {fid}")
            result.append("")
        if files:
            result.append(f"📄 文件（{len(files)}个）：\n")
            for i, f in enumerate(files[:max_items], 1):
                name = f.get("file_name", f.get("name", "未知文件"))
                size = f.get("file_size", f.get("size", 0))
                uploader = f.get("uploader_name", f.get("uploader", "未知用户"))
                result.append(f"{i}. {name}\n   💾 {QQFileManager._format_file_size(int(size))} | 👤 {uploader}")
        return "\n".join(result) if result else "当前群文件列表为空"

    @staticmethod
    def _format_timestamp(ts: Any) -> str:
        """将秒/毫秒级 Unix 时间戳格式化为 YYYY-MM-DD，无法解析时返回空字符串"""
        try:
            if not ts:
                return ""
            ts = int(ts)
            if ts > 10 ** 12:  # 毫秒级时间戳
                ts //= 1000
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OSError):
            return ""

    def _format_search_results(self, keyword: str, scope: str,
                               matched_files: List[Dict], max_items: int = 20) -> str:
        """格式化文件搜索结果，结果中包含 file_id 以便后续删除/移动"""
        total = len(matched_files)
        if total == 0:
            return f"🔍 在{scope}中未找到文件名包含「{keyword}」的文件"

        lines = [f"🔍 搜索「{keyword}」共找到 {total} 个文件（{scope}）：", ""]
        for i, f in enumerate(matched_files[:max_items], 1):
            name = f.get("file_name", f.get("name", "未知文件"))
            size = f.get("file_size", f.get("size", 0))
            uploader = f.get("uploader_name") or f.get("uploader") or "未知用户"
            file_id = f.get("file_id", f.get("id", ""))
            location = f.get("_location", "未知位置")
            date = self._format_timestamp(f.get("upload_time", f.get("modify_time")))

            meta = f"   📁 {location} | 💾 {self._format_file_size(int(size) if size else 0)} | 👤 {uploader}"
            if date:
                meta += f" | 📅 {date}"
            lines.append(f"{i}. {name}")
            lines.append(meta)
            lines.append(f"   🆔 {file_id}")

        if total > max_items:
            lines.append("")
            lines.append(f"⚠️ 结果较多，仅显示前 {max_items} 条，请使用更精确的关键词缩小范围")
        return "\n".join(lines)

    # ========== 工具函数 ==========

    @tool(
        "qq_list_files",
        "获取QQ群根目录的文件和文件夹列表",
        {"type": "object", "properties": {"group_id": {"type": "string", "description": "QQ群号"}}, "required": ["group_id"]}
    )
    async def list_files(self, event: KiraMessageBatchEvent, group_id: str) -> str:
        if not group_id:
            group_id = self._get_group_id_from_event(event)
            if not group_id:
                return "❌ 无法确定群号"
        
        allowed, msg = self._check_feature_permission(group_id, "list_files")
        if not allowed:
            return f"❌ {msg}"

        files, folders = await self._get_group_files_and_folders(group_id)
        return self._format_file_list(files, folders, self.max_files_list)

    @tool(
        "qq_list_folder_files",
        "查看指定文件夹内的文件列表",
        {
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "QQ群号"},
                "folder_id": {"type": "string", "description": "文件夹ID"},
                "folder_name": {"type": "string", "description": "文件夹名称（与folder_id二选一）"}
            },
            "required": ["group_id"]
        }
    )
    async def list_folder_files(self, event: KiraMessageBatchEvent, group_id: str,
                                 folder_id: str = None, folder_name: str = None) -> str:
        if not group_id:
            group_id = self._get_group_id_from_event(event)
            if not group_id:
                return "❌ 无法确定群号"
        
        allowed, msg = self._check_feature_permission(group_id, "list_files")
        if not allowed:
            return f"❌ {msg}"

        if not folder_id and folder_name:
            folder_id = await self._get_folder_id_by_name(group_id, folder_name)
            if not folder_id:
                return f"❌ 未找到文件夹: {folder_name}"

        if not folder_id:
            return "❌ 请提供文件夹ID或文件夹名称"

        files = await self._get_folder_files(group_id, folder_id)
        if not files:
            return f"📂 文件夹内没有文件"

        result = [f"📂 文件夹内的文件（{len(files)}个）：\n"]
        for i, f in enumerate(files[:self.max_files_list], 1):
            name = f.get("file_name", f.get("name", "未知文件"))
            size = f.get("file_size", f.get("size", 0))
            uploader = f.get("uploader_name", f.get("uploader", "未知用户"))
            result.append(f"{i}. {name}\n   💾 {self._format_file_size(int(size))} | 👤 {uploader}")
        return "\n".join(result)

    @tool(
        "qq_search_files",
        "按文件名关键词搜索QQ群文件（不区分大小写的模糊匹配）。默认搜索根目录及所有文件夹，传入 folder_id 或 folder_name 可限定范围。返回文件名、所在文件夹、大小、上传者、日期和 file_id，可用 file_id 直接调用删除/移动工具",
        {
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "QQ群号"},
                "keyword": {"type": "string", "description": "文件名关键词，支持部分匹配，不区分大小写"},
                "folder_id": {"type": "string", "description": "可选，限定搜索的文件夹ID；传 '/' 表示仅搜索根目录；不传则搜索根目录及全部文件夹"},
                "folder_name": {"type": "string", "description": "可选，限定搜索的文件夹名称（与folder_id二选一），填'根目录'表示仅搜索根目录"}
            },
            "required": ["group_id", "keyword"]
        }
    )
    async def search_files(self, event: KiraMessageBatchEvent, group_id: str,
                           keyword: str, folder_id: str = None, folder_name: str = None) -> str:
        if not group_id:
            group_id = self._get_group_id_from_event(event)
            if not group_id:
                return "❌ 无法确定群号"

        # 搜索属于读取类操作，复用查看列表的权限开关，避免绕过禁用限制
        allowed, msg = self._check_feature_permission(group_id, "list_files")
        if not allowed:
            return f"❌ {msg}"

        keyword = (keyword or "").strip()
        if not keyword:
            return "❌ 搜索关键词不能为空"
        if len(keyword) > 100:
            return "❌ 搜索关键词过长（最多100个字符）"

        # 解析搜索范围
        target_folder_id = None
        if folder_id:
            target_folder_id = folder_id
        elif folder_name:
            if folder_name in ("根目录", "/"):
                target_folder_id = "/"
            else:
                target_folder_id = await self._get_folder_id_by_name(group_id, folder_name)
                if not target_folder_id:
                    return f"❌ 未找到文件夹: {folder_name}"

        self._log_debug(f"搜索文件: 群={group_id}, 关键词='{keyword}', 范围={target_folder_id or '全部'}")

        all_files, scope = await self._collect_group_files(group_id, target_folder_id)

        # 范围为空（如空文件夹）属于正常情况，走统一的无匹配输出，不报错
        matched_files = self._match_files_by_keyword(all_files, keyword)
        self._log_debug(f"搜索完成: 扫描 {len(all_files)} 个文件，匹配 {len(matched_files)} 个")
        return self._format_search_results(keyword, scope, matched_files, self.max_files_list)

    @tool(
        "qq_create_folder",
        "在QQ群中创建文件夹",
        {
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "QQ群号"},
                "folder_name": {"type": "string", "description": "要创建的文件夹名称"}
            },
            "required": ["group_id", "folder_name"]
        }
    )
    async def create_folder(self, event: KiraMessageBatchEvent, group_id: str, folder_name: str) -> str:
        if not group_id:
            group_id = self._get_group_id_from_event(event)
            if not group_id:
                return "❌ 无法确定群号"
        
        allowed, msg = self._check_feature_permission(group_id, "create_folder")
        if not allowed:
            return f"❌ {msg}"

        if not folder_name or len(folder_name.strip()) == 0:
            return "❌ 文件夹名称不能为空"

        if len(folder_name) > 50:
            return "❌ 文件夹名称过长"

        existing_id = await self._get_folder_id_by_name(group_id, folder_name)
        if existing_id:
            return f"📁 文件夹 '{folder_name}' 已存在"

        result = await self._create_folder_api(group_id, folder_name)
        if result:
            await self._clear_cache(group_id)
            return f"✅ 成功创建文件夹: {folder_name}"
        else:
            return f"❌ 创建文件夹失败"

    @tool(
        "qq_delete_folder",
        "删除QQ群中的文件夹（文件夹必须为空）",
        {
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "QQ群号"},
                "folder_name": {"type": "string", "description": "要删除的文件夹名称"},
                "folder_id": {"type": "string", "description": "文件夹ID（与folder_name二选一）"}
            },
            "required": ["group_id"]
        }
    )
    async def delete_folder(self, event: KiraMessageBatchEvent, group_id: str,
                            folder_name: str = None, folder_id: str = None) -> str:
        if not group_id:
            group_id = self._get_group_id_from_event(event)
            if not group_id:
                return "❌ 无法确定群号"
        
        allowed, msg = self._check_feature_permission(group_id, "delete_folder")
        if not allowed:
            return f"❌ {msg}"

        if not folder_id and not folder_name:
            return "❌ 请提供文件夹名称或文件夹ID"

        if not folder_id and folder_name:
            folder_id = await self._get_folder_id_by_name(group_id, folder_name)
            if not folder_id:
                return f"❌ 未找到文件夹: {folder_name}"

        files = await self._get_folder_files(group_id, folder_id)
        if files and len(files) > 0:
            return f"❌ 文件夹不为空，请先删除文件夹内的 {len(files)} 个文件"

        success = await self._delete_folder_api(group_id, folder_id)
        if success:
            await self._clear_cache(group_id)
            return f"✅ 成功删除文件夹: {folder_name or folder_id}"
        else:
            return f"❌ 删除文件夹失败"

    @tool(
        "qq_delete_file",
        "删除QQ群中的文件（支持批量删除）。file_ids 为精确删除；file_names 仅在文件名于群内唯一时可用，存在多个同名文件时会拒绝执行并列出各位置，防止连坐误删",
        {
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "QQ群号"},
                "file_names": {"type": "array", "items": {"type": "string"}, "description": "要删除的文件名列表（文件名必须在群内唯一）"},
                "file_ids": {"type": "array", "items": {"type": "string"}, "description": "要删除的文件ID列表（精确，优先使用）"}
            },
            "required": ["group_id"]
        }
    )
    async def delete_file(self, event: KiraMessageBatchEvent, group_id: str,
                          file_names: List[str] = None, file_ids: List[str] = None) -> str:
        if not group_id:
            group_id = self._get_group_id_from_event(event)
            if not group_id:
                return "❌ 无法确定群号"

        allowed, msg = self._check_feature_permission(group_id, "delete_file")
        if not allowed:
            return f"❌ {msg}"

        if not file_ids and not file_names:
            return "❌ 请提供要删除的文件名或文件ID"

        delete_ids = []
        delete_names = []

        # file_ids 精确删除（去重，避免同一文件被删两次）
        for fid in (file_ids or []):
            if fid and fid not in delete_ids:
                delete_ids.append(fid)

        # file_names 先精确解析再删除：同一文件名在多处存在时拒绝执行，防止连坐误删
        missing_names = []
        if file_names:
            for name in file_names:
                matches = await self._find_file_matches_all(group_id, name)
                if not matches:
                    missing_names.append(name)
                    continue
                if len(matches) > 1:
                    lines = [f"❌ 群内存在 {len(matches)} 个同名文件「{name}」，为避免误删已取消本次删除，请改用 file_ids 精确指定："]
                    for m in matches:
                        lines.append(f"  - 🆔 {m['file_id']}（位于 {m['location']}）")
                    return "\n".join(lines)
                info = matches[0]
                if info["file_id"] in delete_ids:
                    continue  # file_ids 已包含该文件，跳过避免重复删除
                delete_ids.append(info["file_id"])
                delete_names.append(f"{name}（{info['location']}）")

        if missing_names:
            return f"❌ 未找到文件: {'、'.join(missing_names)}"

        if not delete_ids:
            return "❌ 未找到要删除的文件"

        success_count = 0
        fail_count = 0

        for file_id in delete_ids:
            success = await self._delete_file_api(group_id, file_id)
            if success:
                success_count += 1
            else:
                fail_count += 1

        await self._clear_cache(group_id)

        if success_count == len(delete_ids):
            if len(delete_ids) == 1:
                name = delete_names[0] if delete_names else file_ids[0]
                return f"✅ 成功删除文件: {name}"
            else:
                return f"✅ 成功删除 {success_count} 个文件"
        elif success_count > 0:
            return f"✅ 成功删除 {success_count} 个文件\n❌ {fail_count} 个文件删除失败"
        else:
            return f"❌ 删除失败"

    @tool(
        "qq_move_file",
        "移动QQ群文件到指定文件夹",
        {
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "QQ群号"},
                "file_name": {"type": "string", "description": "要移动的文件名"},
                "folder_name": {"type": "string", "description": "目标文件夹名称，移动到根目录请传入'根目录'或'/'"}
            },
            "required": ["group_id", "file_name", "folder_name"]
        }
    )
    async def move_file(self, event: KiraMessageBatchEvent, group_id: str,
                        file_name: str, folder_name: str) -> str:
        if not group_id:
            group_id = self._get_group_id_from_event(event)
            if not group_id:
                return "❌ 无法确定群号"
        
        allowed, msg = self._check_feature_permission(group_id, "move_file")
        if not allowed:
            return f"❌ {msg}"

        target_folder_id = None
        if folder_name == "根目录" or folder_name == "/" or folder_name == "":
            target_folder_id = "/"
        else:
            target_folder_id = await self._get_folder_id_by_name(group_id, folder_name)

        if not target_folder_id:
            return f"❌ 未找到文件夹: {folder_name}"

        file_info = await self._find_file_in_all_folders(group_id, file_name)
        if not file_info or not file_info.get("file_uuid"):
            return f"❌ 未找到文件: {file_name}"

        file_uuid = file_info["file_uuid"]
        current_folder_id = file_info.get("current_folder_id", "/")

        success, msg = await self._move_file_to_folder(group_id, file_uuid, current_folder_id, target_folder_id, file_name)
        if success:
            await self._clear_cache(group_id)
            if target_folder_id == "/":
                return f"✅ 文件 '{file_name}' 已移动到根目录"
            else:
                return f"✅ 文件 '{file_name}' 已移动到文件夹 '{folder_name}'"
        return f"❌ {msg}"

    @tool(
        "qq_rename_file",
        "重命名QQ群文件。优先用 file_id 精确重命名；也可传 file_name 自动定位（要求该文件名在群内唯一，否则会列出所有同名位置并拒绝执行）。注意：NapCat 支持文件重命名，其他协议端可能不支持",
        {
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "QQ群号"},
                "new_name": {"type": "string", "description": "新文件名（需保留扩展名，如 '新名字.pdf'）"},
                "file_id": {"type": "string", "description": "要重命名的文件ID（来自列表/搜索结果，优先使用）"},
                "file_name": {"type": "string", "description": "要重命名的文件名（与file_id二选一，须在群内唯一）"}
            },
            "required": ["group_id", "new_name"]
        }
    )
    async def rename_file(self, event: KiraMessageBatchEvent, group_id: str,
                          new_name: str, file_id: str = None, file_name: str = None) -> str:
        if not group_id:
            group_id = self._get_group_id_from_event(event)
            if not group_id:
                return "❌ 无法确定群号"

        allowed, msg = self._check_feature_permission(group_id, "rename_file")
        if not allowed:
            return f"❌ {msg}"

        new_name = (new_name or "").strip()
        if not new_name:
            return "❌ 新文件名不能为空"
        if "/" in new_name or "\\" in new_name:
            return "❌ 新文件名不能包含路径分隔符"
        if not file_id and not file_name:
            return "❌ 请提供 file_id 或 file_name"

        if file_name:
            # 按名称定位：同名多处时拒绝，防止改错文件
            matches = await self._find_file_matches_all(group_id, file_name)
            if not matches:
                return f"❌ 未找到文件: {file_name}"
            if len(matches) > 1:
                lines = [f"❌ 群内存在 {len(matches)} 个同名文件「{file_name}」，请用 file_id 精确指定："]
                for m in matches:
                    lines.append(f"  - 🆔 {m['file_id']}（位于 {m['location']}）")
                return "\n".join(lines)
            target = matches[0]
            old_name = file_name
        else:
            # 按ID定位：扫描全群找到 file_id 实际所在文件夹，作为重命名所需的父目录
            all_files, _ = await self._collect_group_files(group_id, None)
            entry = next((f for f in all_files if f.get("file_id", f.get("id", "")) == file_id), None)
            if entry:
                target = {
                    "file_id": entry.get("file_id", file_id),
                    "folder_id": entry.get("_location_folder_id", "/") or "/",
                    "location": entry.get("_location")
                }
                old_name = entry.get("file_name", entry.get("name", file_id))
            else:
                # 未扫到该ID（可能在更深层目录），按根目录重试
                target = {"file_id": file_id, "folder_id": "/", "location": None}
                old_name = file_id

        success, msg = await self._rename_file_api(group_id, target["file_id"], target.get("folder_id", "/"), new_name)
        if success:
            await self._clear_cache(group_id)
            location = target.get("location")
            where = f"（位于 {location}）" if location else ""
            return f"✅ 已将文件 {old_name}{where} 重命名为 {new_name}"
        return f"❌ 重命名失败: {msg}"

    @tool(
        "qq_rename_folder",
        "重命名QQ群文件夹。优先用 folder_id 精确重命名，也可传 folder_name 定位。注意：LLOneBot 支持文件夹重命名，其他协议端可能不支持",
        {
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "QQ群号"},
                "new_folder_name": {"type": "string", "description": "新文件夹名"},
                "folder_id": {"type": "string", "description": "要重命名的文件夹ID（来自列表/搜索结果，优先使用）"},
                "folder_name": {"type": "string", "description": "要重命名的文件夹名称（与folder_id二选一）"}
            },
            "required": ["group_id", "new_folder_name"]
        }
    )
    async def rename_folder(self, event: KiraMessageBatchEvent, group_id: str,
                            new_folder_name: str, folder_id: str = None, folder_name: str = None) -> str:
        if not group_id:
            group_id = self._get_group_id_from_event(event)
            if not group_id:
                return "❌ 无法确定群号"

        allowed, msg = self._check_feature_permission(group_id, "rename_folder")
        if not allowed:
            return f"❌ {msg}"

        new_folder_name = (new_folder_name or "").strip()
        if not new_folder_name:
            return "❌ 新文件夹名不能为空"
        if "/" in new_folder_name or "\\" in new_folder_name:
            return "❌ 新文件夹名不能包含路径分隔符"

        if folder_id:
            target_id = folder_id
            old_name = await self._get_folder_name_by_id(group_id, folder_id) or folder_id
        elif folder_name:
            target_id = await self._get_folder_id_by_name(group_id, folder_name)
            if not target_id:
                return f"❌ 未找到文件夹: {folder_name}"
            old_name = folder_name
        else:
            return "❌ 请提供 folder_id 或 folder_name"

        success, msg = await self._rename_folder_api(group_id, target_id, new_folder_name)
        if success:
            await self._clear_cache(group_id)
            return f"✅ 已将文件夹 {old_name} 重命名为 {new_folder_name}"
        return f"❌ 重命名失败: {msg}"

    @tool(
        "qq_download_file",
        "下载QQ群文件到本地",
        {
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "QQ群号"},
                "file_name": {"type": "string", "description": "要下载的文件名"}
            },
            "required": ["group_id", "file_name"]
        }
    )
    async def download_file(self, event: KiraMessageBatchEvent, group_id: str, file_name: str) -> str:
        if not group_id:
            group_id = self._get_group_id_from_event(event)
            if not group_id:
                return "❌ 无法确定群号"
        
        allowed, msg = self._check_feature_permission(group_id, "download_file")
        if not allowed:
            return f"❌ {msg}"

        file_info = await self._find_file_in_all_folders(group_id, file_name)
        if not file_info or not file_info.get("file_id"):
            return f"❌ 未找到文件: {file_name}"

        # 扩展名白名单 / 大小限制预检，不通过则不创建下载任务
        not_allowed = self._check_download_allowed(file_name, file_info.get("size"))
        if not_allowed:
            return not_allowed

        session_id = self._get_session_id_from_event(event)
        
        task_id = f"{group_id}_{file_name}_{int(datetime.now().timestamp())}"
        self.pending_downloads[task_id] = {"status": "pending"}
        asyncio.create_task(self._download_file_async(group_id, file_name, task_id, session_id))
        
        return f"✅ 已开始下载: {file_name}\n📋 任务ID: {task_id}\n⏰ 下载完成后我会通知你~"

    @tool(
        "qq_check_download",
        "检查下载任务状态",
        {
            "type": "object",
            "properties": {"task_id": {"type": "string", "description": "下载任务ID"}},
            "required": ["task_id"]
        }
    )
    async def check_download(self, event: KiraMessageBatchEvent, task_id: str) -> str:
        if task_id not in self.pending_downloads:
            return f"❌ 未找到任务ID: {task_id}"
        task = self.pending_downloads[task_id]
        if task["status"] == "pending":
            return "⏳ 下载中，请稍后再问我吧~"
        elif task["status"] == "success":
            result = f"✅ 下载完成！\n📄 {task['file_name']}"
            del self.pending_downloads[task_id]
            return result
        else:
            error = task.get("error", "未知错误")
            del self.pending_downloads[task_id]
            return f"❌ 下载失败: {error}"