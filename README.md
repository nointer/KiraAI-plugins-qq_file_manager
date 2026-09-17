# QQ群文件管理插件

一个功能完整的 QQ 群文件管理插件，支持文件列表查看、文件夹管理、文件移动、文件下载、批量删除等功能，并提供灵活的权限控制和调试模式。

## 功能特性

### 基础功能
- 📁 **文件列表** - 查看群根目录文件和文件夹
- 📂 **文件夹内容** - 查看指定文件夹内的文件列表
- 🔍 **文件搜索** - 按文件名关键词模糊搜索（默认搜索根目录及全部文件夹，可限定文件夹），结果带 file_id 便于直接删除/移动
- 📝 **重命名** - 重命名群文件和文件夹（按 file_id/folder_id 精确操作，按名称操作时要求名称唯一，防止误改）
- 📄 **创建文件夹** - 在群中创建新文件夹
- 🗑️ **删除文件夹** - 删除空文件夹
- 📎 **删除文件** - 支持单个或批量删除文件（按名称删除时若存在多个同名文件会拒绝并列出位置，防止连坐误删）
- 🔄 **移动文件** - 将文件移动到指定文件夹或根目录
- ⬇️ **下载文件** - 异步下载文件到本地，完成后自动通知

### 权限管理
- **两种权限模式**（`permission_mode` 下拉切换）：
  - **黑名单模式**（默认）：所有群可用，通过各功能的 `disabled_xxx` 列表按需禁用（支持 `all` 全局禁用）
  - **白名单模式**：默认禁止全部群，仅在 `enabled_groups` 名单内的群可使用全部功能（留空名单 = 所有群禁用）；名单内的群仍受单功能黑名单微调约束
- 配置界面按「基础设置 / 下载设置 / 权限设置」折叠分组展示，权限组默认收起，需要配置时展开
- 支持 `"all"` 全局禁用特定功能
- 可分别控制以下功能：
  - 创建文件夹
  - 删除文件夹
  - 删除文件
  - 移动文件
  - 下载文件
  - 查看文件列表

### 调试模式
- 布尔开关控制日志详细程度
- 开启时显示完整的 API 调用、文件搜索、缓存使用等调试信息
- 关闭时仅显示关键操作结果

### 性能优化
- 文件夹内容缓存（5秒有效）
- API 调用自动重试机制
- 搜索时顺序遍历各文件夹（兼容协议端并发限流），短时间重复搜索命中缓存
- 异步下载不阻塞主流程

#依赖
KiraAI v2.1.0+
NapCatQQ（QQ 适配器）
Python 3.10+

## 安装
将插件文件夹放入 KiraAI 的 `data/plugins/` 目录：
data/plugins/
└── qq_file_manager/
	├── init.py
	├── main.py
	├── manifest.json
	└── schema.json


## 配置

### schema.json 配置项

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `max_files_list` | integer | `20` | 列出/搜索文件时最多显示的条目数量 |
| `debug_mode` | boolean | `false` | 调试模式开关，开启后显示详细日志 |
| `api_provider` | select | `"auto"` | 协议端选择（下拉框）：auto 自动探测 / napcat / llonebot |
| `download_path` | string | `"files"` | 文件下载保存路径（相对于 `data/` 目录，仅支持相对路径） |
| `max_file_size_mb` | integer | `100` | 允许下载的最大文件大小（MB），`0` 表示不限制 |
| `allowed_extensions` | list | `[]` | 允许下载的文件扩展名白名单，每行一个（如 pdf、jpg），空则允许所有类型 |
| `download_timeout` | integer | `60` | 下载超时时间（秒） |
| `permission_mode` | select | `"deny_list"` | 权限模式（下拉框）：deny_list 黑名单（默认）/ allow_list 白名单 |
| `enabled_groups` | list | `[]` | 白名单模式下的允许群号列表（留空 = 所有群禁用）；黑名单模式下不生效 |
| `disabled_create_folder` | list | `[]` | 禁用创建文件夹的群号列表 |
| `disabled_delete_folder` | list | `[]` | 禁用删除文件夹的群号列表 |
| `disabled_delete_file` | list | `[]` | 禁用删除文件的群号列表 |
| `disabled_move_file` | list | `[]` | 禁用移动文件的群号列表 |
| `disabled_rename_file` | list | `[]` | 禁用重命名文件的群号列表 |
| `disabled_rename_folder` | list | `[]` | 禁用重命名文件夹的群号列表 |
| `disabled_download_file` | list | `[]` | 禁用下载文件的群号列表 |
| `disabled_list_files` | list | `[]` | 禁用查看文件列表/搜索的群号列表 |

> 配置界面说明：以上配置项在 Kira 配置界面中按「基础设置 / 下载设置 / 权限设置」三个折叠分组展示，权限设置默认收起。配置存储支持 section 嵌套与旧版扁平两种格式（自动合并，section 优先），旧配置无需迁移。

> 下载限制说明：`allowed_extensions` 与 `max_file_size_mb` 在**创建下载任务前**预检（基于文件列表信息），下载过程中还会按实际响应大小二次校验，两层防护。

### 配置示例

```json
{
  "section_basic": {
    "max_files_list": 20,
    "debug_mode": false,
    "api_provider": "auto"
  },
  "section_download": {
    "download_path": "files",
    "max_file_size_mb": 100,
    "allowed_extensions": ["pdf", "docx", "jpg", "png", "zip"],
    "download_timeout": 60
  },
  "section_permission": {
    "permission_mode": "allow_list",
    "enabled_groups": ["878686562", "123456789"],
    "disabled_delete_folder": [],
    "disabled_delete_file": ["123456789"]
  }
}
```

（也兼容旧版扁平格式：所有字段直接写在顶层，无需 section 包裹，两种格式自动合并、section 优先）

权限配置说明
- 黑名单模式（默认）：所有群可用，`disabled_xxx` 列表填入要禁用的群号（每行一个，字符串/数字均可），填 "all" 全局禁用，留空不限制
- 白名单模式：默认禁止全部群，`enabled_groups` 填入允许的群号；留空 = 所有群禁用；名单内的群仍受 `disabled_xxx` 单功能微调约束

工具列表
插件注册了以下工具供 LLM 调用：

工具名 			 	 |	功能 					|		参数
qq_list_files		 	 |	获取群根目录文件和文件夹	|	group_id
qq_list_folder_files	 |	查看指定文件夹内文件		|	group_id, folder_id/folder_name
qq_search_files	 	 |	按关键词搜索群文件			|	group_id, keyword, folder_id/folder_name（可选）
qq_rename_file	 	 |	重命名群文件				|	group_id, new_name, file_id/file_name
qq_rename_folder	 |	重命名群文件夹				|	group_id, new_folder_name, folder_id/folder_name
qq_create_folder	  	 |	创建文件夹				|	group_id, folder_name
qq_delete_folder	 	 |	删除文件夹				|	group_id, folder_name/folder_id
qq_delete_file	  	 |	删除文件					|	group_id, file_names/file_ids
qq_move_file	 	 |	移动文件					|	group_id, file_name, folder_name
qq_download_file 	 |	下载文件					|	group_id, file_name
qq_check_download	 |	检查下载任务状态			|	task_id

#注意事项
Bot 需要在群内拥有管理员权限才能执行创建/删除文件夹、删除文件、移动文件等操作
删除文件夹前需确保文件夹为空
下载链接有时效性，请尽快下载
批量删除时，如部分文件删除失败会返回成功/失败统计

更新日志
v1.4.0
*新增全局白名单权限模式：permission_mode 下拉切换黑名单（默认）/白名单，白名单模式下仅 enabled_groups 名单内的群可用（与 Kira 官方 QQ 适配器同款设计）
*配置界面重构为折叠分组（基础设置/下载设置/权限设置），权限组默认收起，各配置项补充中文显示名
*配置存储兼容：折叠分组（section 嵌套）与旧版扁平配置双向兼容，旧配置无需迁移
v1.3.0
*新增 qq_rename_file / qq_rename_folder 重命名群文件与文件夹工具（自适应探测协议端支持情况，NapCat 支持文件重命名、LLOneBot 支持文件夹重命名）
*修复删除文件连坐问题：按 file_names 删除时若存在多个同名文件，拒绝执行并列出所有位置及 file_id，避免误删；file_ids 去重防止重复删除
*修复限定搜索空文件夹时误报错误的问题，现返回正常的空结果
*新增重命名功能的群权限开关（disabled_rename_file / disabled_rename_folder）
*修复 allowed_extensions / max_file_size_mb 配置不生效的问题：现于下载前预检 + 下载中按实际响应大小二次校验
*download_path 增加路径安全校验（仅相对路径），数值型配置增加类型容错，权限列表兼容数字类型群号
*配置界面优化：按「列表/调试/协议端/下载/权限」分组排序，提示语补充示例与取值说明
v1.2.0
*新增 qq_search_files 关键词搜索群文件工具
*支持搜索根目录及全部文件夹，可通过 folder_id/folder_name 限定范围
*搜索结果包含文件位置、大小、上传者、日期和 file_id，可直接配合删除/移动工具
*搜索复用查看列表（disabled_list_files）的权限开关
v1.0.0
*初始版本
*支持文件列表、文件夹列表
*支持创建/删除文件夹
*支持删除文件
*支持移动文件
*支持异步下载文件
*支持群组权限管理
*支持调试模式


