# QQ群文件管理插件

为KiraAI提供一个功能完整的 QQ 群文件管理插件，支持KiraAI文件列表查看、文件夹管理、文件移动、文件下载、批量删除等功能，并提供灵活的权限控制和调试模式。

## 功能特性

### 基础功能
- 📁 **文件列表** - 查看群根目录文件和文件夹
- 📂 **文件夹内容** - 查看指定文件夹内的文件列表
- 📄 **创建文件夹** - 在群中创建新文件夹
- 🗑️ **删除文件夹** - 删除空文件夹
- 📎 **删除文件** - 支持单个或批量删除文件
- 🔄 **移动文件** - 将文件移动到指定文件夹或根目录
- ⬇️ **下载文件** - 异步下载文件到本地，完成后自动通知

### 权限管理
- 按功能维度配置禁用群组
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
- 异步下载不阻塞主流程

#依赖
KiraAI v2.1.0+
NapCatQQ（QQ 适配器）
Python 3.10+

## 安装
将插件文件夹放入 KiraAI 的 `data/plugins/` 目录：
data/plugins/
└── KiraAI-plugins-qq_file_manager/
    ├── __init__.py
    ├── main.py
    ├── manifest.json
    ├── schema.json
    └── README.md


## 配置

### schema.json 配置项

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `download_path` | string | `"files"` | 文件下载保存路径（相对于 `data/` 目录） |
| `max_file_size_mb` | integer | `100` | 允许下载的最大文件大小（MB） |
| `allowed_extensions` | list | `[]` | 允许下载的文件扩展名列表，空则允许所有 |
| `max_files_list` | integer | `20` | 列出文件时的最大数量 |
| `download_timeout` | integer | `60` | 下载超时时间（秒） |
| `debug_mode` | boolean | `false` | 调试模式开关 |
| `disabled_create_folder` | list | `[]` | 禁用创建文件夹的群号列表 |
| `disabled_delete_folder` | list | `[]` | 禁用删除文件夹的群号列表 |
| `disabled_delete_file` | list | `[]` | 禁用删除文件的群号列表 |
| `disabled_move_file` | list | `[]` | 禁用移动文件的群号列表 |
| `disabled_download_file` | list | `[]` | 禁用下载文件的群号列表 |
| `disabled_list_files` | list | `[]` | 禁用查看文件列表的群号列表 |

### 配置示例

```json
{
  "download_path": "files",
  "max_file_size_mb": 100,
  "allowed_extensions": [],
  "max_files_list": 20,
  "download_timeout": 60,
  "debug_mode": false,
  "disabled_create_folder": [],
  "disabled_delete_folder": ["123456789"],
  "disabled_delete_file": ["123456789", "987654321"],
  "disabled_move_file": [],
  "disabled_download_file": [],
  "disabled_list_files": []
}
```

###使用示例
用户对话示例
用户：帮我看看这个群有哪些文件
AI：调用 qq_list_files(group_id="1234567")
AI：群里有 2 个文件和 1 个文件夹：
    📁 文件夹：ceshi
    📄 文件：图片1.jpg、文档.pdf

用户：把图片1.jpg移动到 ceshi 文件夹
AI：调用 qq_move_file(group_id="1234567", file_name="图片1.jpg", folder_name="ceshi")
AI：✅ 文件 '图片1.jpg' 已移动到文件夹 'ceshi'

用户：下载那个文档
AI：调用 qq_download_file(group_id="1234567", file_name="文档.pdf")
AI：✅ 已开始下载: 文档.pdf
    📋 任务ID: 1234567_文档.pdf_1734567890
    ⏰ 下载完成后我会通知你~

用户：刚才下载的好了吗？
AI：调用 qq_check_download(task_id="1234567_文档.pdf_1734567890")
AI：✅ 下载完成！📄 文档.pdf

日志输出
普通模式（debug_mode: false）
[QQFileManager] ✅ 初始化完成
[QQFileManager] ✅ 成功创建文件夹: 测试文件夹
[QQFileManager] ✅ 文件 'test.jpg' 已下载完成
调试模式（debug_mode: true）
[QQFileManager] 🔧 调试模式已开启，将显示详细日志
[QQFileManager] 🔍 找到QQ适配器: qq
[QQFileManager] 🔍 开始创建文件夹: 测试文件夹 (群: 1234567)
[QQFileManager] 🔍 创建文件夹成功: 测试文件夹, ID: xxxxx
[QQFileManager] 🔍 已清除群 1234567 的缓存
[QQFileManager] 🔍 开始搜索文件: test.jpg
[QQFileManager] 🔍 共获取到 3 个文件
[QQFileManager] 🔍 找到匹配文件: test.jpg
[QQFileManager] 🔍 开始下载文件: test.jpg -> D:\...\data\files\test.jpg
[QQFileManager] 🔍 文件下载完成: test.jpg

###注意事项
Bot 需要在群内拥有管理员权限才能执行创建/删除文件夹、删除文件、移动文件等操作
删除文件夹前需确保文件夹为空
下载链接有时效性，请尽快下载
批量删除时，如部分文件删除失败会返回成功/失败统计

###更新日志
v1.0.0
*初始版本
*支持文件列表、文件夹列表
*支持创建/删除文件夹
*支持删除文件
*支持移动文件
*支持异步下载文件
*支持群组权限管理
*支持调试模式
