"""
QQ群文件管理插件 - 完整版
支持：文件列表、文件夹列表、查看文件夹内容、创建文件夹、删除文件夹、删除文件、移动文件、下载文件
权限管理：按功能维度配置禁用群组，支持 all 全局禁用
调试模式：布尔开关，开启时显示详细日志
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

    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        self.download_history = {}
        self.history_file = None
        self.qq_adapter = None
        self.download_timeout = 60
        self.pending_downloads = {}
        self._folder_cache = {}
        self.debug_mode = False

    async def initialize(self):
        """插件初始化"""
        self.qq_adapter = self._get_qq_adapter()
        if not self.qq_adapter:
            logger.warning("[QQFileManager] QQ适配器未找到，插件功能将不可用")
            return

        # 读取调试模式配置（布尔开关）
        self.debug_mode = self.plugin_cfg.get("debug_mode", False)
        if self.debug_mode:
            logger.info("[QQFileManager] 🔧 调试模式已开启，将显示详细日志")

        # 设置下载路径
        download_path = self.plugin_cfg.get("download_path", "files")
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

        self.max_file_size = self.plugin_cfg.get("max_file_size_mb", 100) * 1024 * 1024
        self.allowed_extensions = self.plugin_cfg.get("allowed_extensions", [])
        self.max_files_list = self.plugin_cfg.get("max_files_list", 20)
        self.download_timeout = self.plugin_cfg.get("download_timeout", 60)

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
        feature: create_folder, delete_folder, delete_file, move_file, download_file, list_files
        """
        config_key = f"disabled_{feature}"
        disabled_list = self.plugin_cfg.get(config_key, [])
        
        if not disabled_list:
            return False
        
        if "all" in disabled_list:
            return True
        
        return str(group_id) in disabled_list

    def _check_feature_permission(self, group_id: str, feature: str) -> tuple[bool, str]:
        """检查群组是否有特定功能的权限"""
        if self._is_feature_disabled(group_id, feature):
            feature_names = {
                "create_folder": "创建文件夹",
                "delete_folder": "删除文件夹",
                "delete_file": "删除文件",
                "move_file": "移动文件",
                "download_file": "下载文件",
                "list_files": "查看文件列表"
            }
            return False, f"{feature_names.get(feature, feature)}功能已在当前群禁用"
        return True, ""

    async def _get_group_files_and_folders(self, group_id: str) -> tuple[List[Dict], List[Dict]]:
        """获取根目录文件和文件夹"""
        if not self.qq_adapter:
            return [], []
        try:
            bot = self.qq_adapter.get_client()
            result = await bot.send_action(
                "get_group_root_files",
                {"group_id": group_id}
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

    async def _get_folder_files(self, group_id: str, folder_id: str, retry: int = 2) -> List[Dict]:
        """获取文件夹内的文件，支持重试和缓存"""
        if not self.qq_adapter:
            return []
        
        clean_folder_id = folder_id.lstrip('/') if folder_id else folder_id
        cache_key = f"folder_files_{group_id}_{clean_folder_id}"
        current_time = datetime.now().timestamp()
        
        if cache_key in self._folder_cache:
            cache_time, cache_files = self._folder_cache[cache_key]
            if current_time - cache_time < 5:
                self._log_debug(f"使用缓存: 文件夹 {folder_id} 内有 {len(cache_files)} 个文件")
                return cache_files
        
        for attempt in range(retry + 1):
            try:
                bot = self.qq_adapter.get_client()
                result = await bot.send_action(
                    "get_group_files_by_folder",
                    {
                        "group_id": group_id,
                        "folder_id": clean_folder_id
                    }
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
                        "current_folder_id": folder_id or "/"
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

    # ========== API 方法 ==========

    async def _create_folder_api(self, group_id: str, folder_name: str) -> Optional[str]:
        """创建群文件夹"""
        if not self.qq_adapter:
            return None
        try:
            bot = self.qq_adapter.get_client()
            result = await bot.send_action(
                "create_group_file_folder",
                {"group_id": group_id, "folder_name": folder_name}
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

    async def _move_file_to_folder(self, group_id: str, file_uuid: str, current_folder_id: str, target_folder_id: str, file_name: str = "") -> tuple[bool, str]:
        """移动文件到指定文件夹"""
        if not self.qq_adapter:
            return False, "QQ适配器未就绪"

        clean_current = "/" if not current_folder_id or current_folder_id == "/" else current_folder_id.lstrip('/')
        clean_target = "/" if not target_folder_id or target_folder_id == "/" else target_folder_id.lstrip('/')

        self._log_debug(f"移动文件: {file_name}, 当前目录: {clean_current}, 目标目录: {clean_target}")

        try:
            bot = self.qq_adapter.get_client()
            result = await bot.send_action(
                "move_group_file",
                {
                    "group_id": int(group_id),
                    "file_id": file_uuid,
                    "current_parent_directory": clean_current,
                    "target_parent_directory": clean_target
                }
            )

            if not result:
                return False, "API返回为空"

            if isinstance(result, dict):
                if result.get("status") == "ok":
                    self._log_debug(f"移动文件成功: {file_name}")
                    return True, ""
                elif result.get("status") == "failed":
                    msg = result.get("message", "")
                    self._log_error(f"移动文件失败: {msg}")
                    return False, f"移动失败: {msg[:100]}"
            return False, "未知响应格式"
        except Exception as e:
            self._log_error(f"移动文件异常: {e}")
            return False, str(e)

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
        "删除QQ群中的文件（支持批量删除）",
        {
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "QQ群号"},
                "file_names": {"type": "array", "items": {"type": "string"}, "description": "要删除的文件名列表"},
                "file_ids": {"type": "array", "items": {"type": "string"}, "description": "要删除的文件ID列表"}
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

        if file_ids:
            delete_ids.extend(file_ids)

        if file_names:
            for name in file_names:
                file_info = await self._find_file_in_all_folders(group_id, name)
                if file_info and file_info.get("file_id"):
                    delete_ids.append(file_info["file_id"])
                    delete_names.append(name)
                else:
                    return f"❌ 未找到文件: {name}"

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