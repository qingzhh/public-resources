# Tracker 目录说明

本目录用于存储不同类型的PT网站tracker信息。

## 目录结构

- `private/` - 私有PT站点的tracker信息，需要邀请码才能注册的站点
- `semi_private/` - 半公开PT站点的tracker信息，定期开放注册的站点
- `public/` - 公开PT站点的tracker信息，任何人都可以注册的站点

## 文件格式

每个站点的tracker信息以单独的JSON文件存储，文件名为站点域名或简短标识符。

JSON格式示例：
```json
{
  "name": "Example Tracker",
  "url": "https://example.com",
  "tracker": "https://example.com/announce",
  "announce_list": [
    "https://example.com/announce",
    "https://backup.example.com/announce"
  ],
  "description": "示例PT站点",
  "last_updated": "2024-01-01",
  "status": "active",
  "type": "private"
}
```

## 字段说明

- `name` - 站点名称
- `url` - 站点网址
- `tracker` - 主tracker地址
- `announce_list` - tracker地址列表，包括备用tracker
- `description` - 站点简要描述
- `last_updated` - 最后更新日期（YYYY-MM-DD格式）
- `status` - 状态（active/inactive）
- `type` - 站点类型（private/semi_private/public）